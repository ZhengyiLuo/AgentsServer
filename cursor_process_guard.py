"""Small parent-death guard for a Cursor CLI subprocess.

AgentsServer launches this helper in its own process group. The helper launches
Cursor in a child group, forwards inherited stdio unchanged, and terminates the
entire Cursor tree when its leader exits or AgentsServer disappears abruptly.
This prevents a detached agent from continuing workspace edits after the turn.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from threading import Event
from typing import NamedTuple


POLL_SECONDS = 0.02
DESCENDANT_SNAPSHOT_INTERVAL_SECONDS = 0.1
TERMINATE_GRACE_SECONDS = 0.5
# The server gives this wrapper a bounded grace window before killing it. Keep
# each process-table probe well below that outer deadline even if ``ps`` wedges.
PROCESS_SNAPSHOT_TIMEOUT_SECONDS = 0.2
MAX_PROCESS_SNAPSHOT_ROWS = 32_768
MAX_CAPTURED_DESCENDANTS = 4_096


class _ProcessIdentity(NamedTuple):
    pid: int
    parent_pid: int
    process_group: int
    started_at: str


def _process_snapshot() -> dict[int, _ProcessIdentity]:
    """Return one bounded POSIX process snapshot with stable-enough identities.

    ``lstart`` is intentionally retained as a PID-reuse fence. Before sending
    an individual signal we require both the PID and captured start marker to
    match a fresh snapshot, so a descendant that exits during the grace period
    cannot cause its recycled PID to be killed.
    """

    ps_path = next(
        (candidate for candidate in ("/bin/ps", "/usr/bin/ps") if os.path.isfile(candidate)),
        None,
    )
    if ps_path is None:
        return {}
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        result = subprocess.run(
            [ps_path, "-axo", "pid=,ppid=,pgid=,lstart="],
            text=True,
            capture_output=True,
            timeout=PROCESS_SNAPSHOT_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}

    snapshot: dict[int, _ProcessIdentity] = {}
    for index, raw_line in enumerate(result.stdout.splitlines()):
        if index >= MAX_PROCESS_SNAPSHOT_ROWS:
            break
        parts = raw_line.strip().split(None, 3)
        if len(parts) != 4:
            continue
        try:
            pid, parent_pid, process_group = map(int, parts[:3])
        except ValueError:
            continue
        if pid <= 1 or parent_pid < 0 or process_group <= 0:
            continue
        snapshot[pid] = _ProcessIdentity(
            pid,
            parent_pid,
            process_group,
            parts[3],
        )
    return snapshot


def _recursive_descendants(
    root_pid: int,
    snapshot: dict[int, _ProcessIdentity],
) -> tuple[_ProcessIdentity, ...]:
    return _recursive_descendants_from_roots((root_pid,), snapshot)


def _recursive_descendants_from_roots(
    root_pids: tuple[int, ...],
    snapshot: dict[int, _ProcessIdentity],
) -> tuple[_ProcessIdentity, ...]:
    children: dict[int, list[_ProcessIdentity]] = {}
    for identity in snapshot.values():
        children.setdefault(identity.parent_pid, []).append(identity)

    descendants: list[_ProcessIdentity] = []
    pending = list(root_pids)
    seen = set(root_pids)
    while pending and len(descendants) < MAX_CAPTURED_DESCENDANTS:
        parent_pid = pending.pop()
        for identity in children.get(parent_pid, ()):
            if identity.pid in seen:
                continue
            seen.add(identity.pid)
            descendants.append(identity)
            pending.append(identity.pid)
            if len(descendants) >= MAX_CAPTURED_DESCENDANTS:
                break
    return tuple(descendants)


def _matching_survivors(
    identities: tuple[_ProcessIdentity, ...],
    snapshot: dict[int, _ProcessIdentity],
) -> tuple[_ProcessIdentity, ...]:
    return tuple(
        identity
        for identity in identities
        if (
            (current := snapshot.get(identity.pid)) is not None
            and current.started_at == identity.started_at
        )
    )


def _signal_identities(
    identities: tuple[_ProcessIdentity, ...],
    signum: int,
    snapshot: dict[int, _ProcessIdentity],
) -> None:
    guard_pid = os.getpid()
    parent_pid = os.getppid()
    for identity in _matching_survivors(identities, snapshot):
        if identity.pid in {guard_pid, parent_pid}:
            continue
        try:
            os.kill(identity.pid, signum)
        except (OSError, ProcessLookupError):
            pass


def _track_child_processes(
    child_pid: int,
    child_group: int,
    known: dict[int, _ProcessIdentity],
    snapshot: dict[int, _ProcessIdentity],
    *,
    child_alive: bool,
) -> None:
    """Accumulate bounded child identities before they can be reparented.

    Same-group processes remain discoverable after the Cursor leader exits.
    Detached descendants do not, so every still-matching captured descendant is
    also treated as a root while the leader is alive. This follows children of
    a previously detached tool without trusting a recycled PID.
    """

    roots: list[int] = [child_pid] if child_alive else []
    roots.extend(
        identity.pid
        for identity in known.values()
        if (
            (current := snapshot.get(identity.pid)) is not None
            and current.started_at == identity.started_at
        )
    )
    # A normal leader exit reparents children before the next snapshot, but
    # members of its dedicated process group remain attributable to this run.
    discovered: dict[int, _ProcessIdentity] = {}
    for identity in snapshot.values():
        if identity.pid != child_pid and identity.process_group == child_group:
            discovered.setdefault(identity.pid, identity)
            if len(discovered) >= MAX_CAPTURED_DESCENDANTS:
                break

    for identity in _recursive_descendants_from_roots(
        tuple(roots),
        snapshot,
    ):
        discovered.setdefault(identity.pid, identity)
        if len(discovered) >= MAX_CAPTURED_DESCENDANTS:
            break

    for identity in discovered.values():
        if len(known) >= MAX_CAPTURED_DESCENDANTS:
            break
        previous = known.get(identity.pid)
        if previous is None or previous.started_at == identity.started_at:
            known[identity.pid] = identity


def _terminate_child_group(
    child: subprocess.Popen[bytes],
    group: int,
    tracked_descendants: tuple[_ProcessIdentity, ...] = (),
) -> None:
    known = {identity.pid: identity for identity in tracked_descendants}

    initial_snapshot = _process_snapshot()
    _track_child_processes(
        child.pid,
        group,
        known,
        initial_snapshot,
        child_alive=child.poll() is None,
    )
    descendants = tuple(known.values())
    detached = tuple(
        identity for identity in descendants if identity.process_group != group
    )
    group_members = tuple(
        identity for identity in descendants if identity.process_group == group
    )
    if child.poll() is None or group_members:
        try:
            os.killpg(group, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    # A child can use setsid()/setpgid() for a background shell and escape the
    # Cursor process group. Signal only descendants captured before teardown,
    # and revalidate their start marker immediately before each signal.
    _signal_identities(detached, signal.SIGTERM, _process_snapshot())

    deadline = time.monotonic() + TERMINATE_GRACE_SECONDS
    survivors: tuple[_ProcessIdentity, ...] = descendants
    while time.monotonic() < deadline:
        snapshot = _process_snapshot()
        _track_child_processes(
            child.pid,
            group,
            known,
            snapshot,
            child_alive=child.poll() is None,
        )
        descendants = tuple(known.values())
        survivors = _matching_survivors(descendants, snapshot)
        if child.poll() is not None and not survivors:
            return
        time.sleep(0.05)

    final_snapshot = _process_snapshot()
    _track_child_processes(
        child.pid,
        group,
        known,
        final_snapshot,
        child_alive=child.poll() is None,
    )
    descendants = tuple(known.values())
    survivors = _matching_survivors(descendants, final_snapshot)
    group_survives = child.poll() is None or any(
        final_snapshot[identity.pid].process_group == group
        for identity in survivors
    )
    if group_survives:
        try:
            os.killpg(group, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    _signal_identities(
        tuple(
            identity
            for identity in survivors
            if final_snapshot[identity.pid].process_group != group
        ),
        signal.SIGKILL,
        final_snapshot,
    )


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[0] != "--parent-pid" or argv[2] != "--":
        return 64
    try:
        expected_parent = int(argv[1])
    except ValueError:
        return 64
    if expected_parent <= 1:
        return 64
    command = argv[3:]
    if not command:
        return 64

    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGHUP, request_stop)

    # The server can disappear after spawning this wrapper but before Python
    # reaches Popen. Do not briefly launch an editing agent in that window.
    if stop.is_set() or os.getppid() != expected_parent:
        return 143

    try:
        child = subprocess.Popen(command, start_new_session=True)
    except OSError:
        return 127

    interrupted = False
    child_group = child.pid
    tracked_descendants: dict[int, _ProcessIdentity] = {}
    next_descendant_snapshot = 0.0
    while True:
        now = time.monotonic()
        child_alive = child.poll() is None
        if now >= next_descendant_snapshot:
            snapshot = _process_snapshot()
            child_alive = child.poll() is None
            _track_child_processes(
                child.pid,
                child_group,
                tracked_descendants,
                snapshot,
                child_alive=child_alive,
            )
            next_descendant_snapshot = (
                now + DESCENDANT_SNAPSHOT_INTERVAL_SECONDS
            )
        if not child_alive:
            break
        if stop.is_set() or os.getppid() != expected_parent:
            interrupted = True
            break
        time.sleep(POLL_SECONDS)
    _terminate_child_group(
        child,
        child_group,
        tuple(tracked_descendants.values()),
    )
    return 143 if interrupted else int(child.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
