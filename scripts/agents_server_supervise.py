#!/usr/bin/env python3
"""User-level supervisor for AgentsServer on Windows (no systemd).

Runs ``agent_server.py serve`` under the repository's ``.venv`` interpreter,
appends output to a log file, and restarts the server with capped exponential
backoff after unexpected exits (at most ~5 rapid restarts per 10 minute
window, then a stable capped backoff). A single Ctrl+C is passed through: the
supervisor stops restarting, lets the child exit, and returns.

Configuration comes from the environment only — never from hardcoded
credentials:

- ``AGENTSDOCK_STATE_DIR``: state directory (default ``~/.agentsdock``).
  Optional ``server.env`` inside it contributes KEY=VALUE defaults (values
  are never printed); variables already set in the environment win. This is
  how the agent token reaches the server when launched at logon.
- ``AGENTSDOCK_AGENT_TOKEN``: shared bearer token (usually via server.env).
- ``AGENT_BIND`` / ``AGENT_PORT``: bind address/port (defaults 0.0.0.0:7850).
- ``AGENTSDOCK_SUPERVISE_LOG``: log path (default ``<repo>\\server.log``).

Command-line flags (``--port``, ``--bind``, ``--log``, ``--once``) exist so
the schtasks entry point and tests can pin them down; secrets stay in the
environment. Standard library only.

Update coordination: while a detached ``winupdate`` finisher holds a fresh
``<state_dir>\\updates\\switch.lock`` — or the admin update status file
(``<state_dir>\\admin\\server-update.json``) is fresh and in an active phase,
which covers the window before the finisher acquires the lock — the
supervisor defers restarts. Before every (re)start it adopts, rather than
duplicates, a server that already answers ``/api/health`` on the configured
port (that is how the finisher-started replacement is picked up). If the
finisher crashed mid-switch (``<repo>.old`` exists, health stays down, no
lock, stale status), the supervisor restores the backup via winupdate's
interrupted-switch recovery and starts it. Spawn failures are logged and
retried with the normal backoff instead of crashing the loop, and the
supervisor never keeps the live tree as its working directory (that would
block the updater from moving it). The supervisor records a heartbeat in
``<state_dir>\\supervisor.lock`` so other tools can tell a live supervisor
from a stale lock file.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BACKOFF_BASE_SECONDS = 1.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP_SECONDS = 60.0
BACKOFF_WINDOW_SECONDS = 600.0
BACKOFF_WINDOW_RESTARTS = 5
GRACEFUL_SHUTDOWN_SECONDS = 10.0


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_state_dir() -> Path:
    configured = os.environ.get("AGENTSDOCK_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".agentsdock"


def load_server_env(state_dir: Path, env: dict[str, str]) -> None:
    """Fold optional KEY=VALUE lines from server.env into env (setdefault)."""

    env_file = Path(state_dir) / "server.env"
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if key and key not in env:
            env[key] = value.strip()


def build_server_command(root: Path, bind: str, port: int) -> list[str]:
    python = root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    return [
        str(python),
        str(root / "agent_server.py"),
        "serve",
        "--bind",
        bind,
        "--port",
        str(port),
    ]


def log_line(stream, message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stream.write(f"{stamp} {message}\n")
    stream.flush()


def compute_backoff(recent_restarts: list[float], now: float) -> float:
    """Capped exponential backoff; stable cap once ~5 restarts hit 10 minutes."""

    window = [
        stamp for stamp in recent_restarts if now - stamp < BACKOFF_WINDOW_SECONDS
    ]
    if len(window) >= BACKOFF_WINDOW_RESTARTS:
        return BACKOFF_CAP_SECONDS
    delay = BACKOFF_BASE_SECONDS * (BACKOFF_FACTOR ** len(window))
    return min(BACKOFF_CAP_SECONDS, delay)


def terminate_child(process: subprocess.Popen, log) -> None:
    if process.poll() is not None:
        return
    try:
        process.wait(timeout=GRACEFUL_SHUTDOWN_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    process.terminate()
    try:
        process.wait(timeout=GRACEFUL_SHUTDOWN_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    process.kill()
    process.wait()


def supervisor_lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "supervisor.lock"


def write_supervisor_lock(state_dir: Path) -> None:
    """Heartbeat so update stages can tell a live supervisor from a stale one."""

    path = supervisor_lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "heartbeat_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
            }
        )
        + "\n"
    )
    os.replace(temporary, path)


def process_id_alive(pid: int) -> bool:
    if os.name == "nt":
        # os.kill(pid, 0) is unreliable from CREATE_NO_WINDOW children on
        # current CPython builds (WinError 87); use OpenProcess directly.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000 | 0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def supervisor_alive(state_dir: Path, max_age_seconds: float = 60.0) -> bool:
    try:
        value = json.loads(supervisor_lock_path(state_dir).read_text())
        pid = int(value.get("pid") or 0)
        age = time.time() - supervisor_lock_path(state_dir).stat().st_mtime
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return False
    return pid > 0 and 0 <= age < max_age_seconds and process_id_alive(pid)


def switch_lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "updates" / "switch.lock"


def switch_lock_active(state_dir: Path, max_age_seconds: float = 150.0) -> bool:
    """Fresh switch lock means a detached updater holds the live tree."""

    try:
        age = time.time() - switch_lock_path(state_dir).stat().st_mtime
    except OSError:
        return False
    return 0 <= age < max_age_seconds


# Phases written by the detached updater while it owns the box (POSIX
# update_runner or the native winupdate drive/finish stages). Anything else
# is terminal (idle/available/current/unavailable/complete/rolled_back/failed)
# or unknown; unknown phases defer too — safer than spawning mid-update.
UPDATE_TERMINAL_PHASES = frozenset(
    {"idle", "available", "current", "unavailable", "complete", "rolled_back", "failed"}
)
UPDATE_STATUS_MAX_AGE_SECONDS = 90.0


def update_status_path(state_dir: Path) -> Path:
    return Path(state_dir) / "admin" / "server-update.json"


def update_status_active_fresh(
    state_dir: Path,
    max_age_seconds: float = UPDATE_STATUS_MAX_AGE_SECONDS,
) -> bool:
    """True when a fresh, non-terminal admin/update status means a detached
    updater owns the box and restarts must defer.

    Failure-tolerant: a missing or corrupt status file means no defer. The
    updater heartbeats keep ``updated_at`` fresh, so a crashed updater goes
    stale here and normal restart/recovery behavior resumes.
    """

    try:
        value = json.loads(update_status_path(state_dir).read_text())
        phase = str(value.get("phase") or "")
        updated_at = datetime.fromisoformat(
            str(value.get("updated_at") or "").replace("Z", "+00:00")
        )
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        return False
    if phase in UPDATE_TERMINAL_PHASES:
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    return 0 <= age < max_age_seconds


def health_endpoint_ok(bind: str, port: int, env: dict[str, str]) -> bool:
    """True when /api/health already answers ok on the configured port.

    Lets the supervisor adopt a server the detached update finisher (or a
    manual launch) started, instead of spawning a duplicate.
    """

    host = "127.0.0.1" if str(bind) in {"0.0.0.0", "", "::"} else str(bind)
    headers = {"User-Agent": "AgentsServer-Supervisor/1"}
    token = str(env.get("AGENTSDOCK_AGENT_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(
            f"http://{host}:{int(port)}/api/health", headers=headers
        )
        with urllib.request.urlopen(request, timeout=3.0) as response:
            return json.loads(response.read(1_000_001)).get("ok") is True
    except Exception:
        return False


def spawn_server_process(
    command: list[str],
    root: Path,
    env: dict[str, str],
    log,
) -> subprocess.Popen:
    return subprocess.Popen(
        command,
        cwd=str(root),
        stdin=None,
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )


def recover_interrupted_update(
    root: Path,
    state_dir: Path,
    env: dict[str, str],
    bind: str,
    port: int,
    log,
) -> bool:
    """Restore ``<root>.old`` over a half-switched tree and start it.

    Bounded by contract: callers only invoke this when health is down, no
    switch lock is held, and no fresh active update status exists — i.e. the
    detached finisher crashed mid-switch and nobody owns the box. Reuses
    winupdate's interrupted-switch recovery rather than duplicating the move
    logic. Returns True when a recovery was attempted (callers re-check
    health on the next loop iteration).
    """

    root = Path(root)
    backup = root.with_name(root.name + ".old")
    if not backup.is_dir():
        return False
    if (
        health_endpoint_ok(bind, port, env)
        or switch_lock_active(state_dir)
        or update_status_active_fresh(state_dir)
    ):
        return False
    try:
        repo_root = Path(__file__).resolve().parents[1]
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        import winupdate
    except Exception as exc:
        log_line(log, f"supervisor: interrupted-update recovery unavailable: {exc}")
        return False

    def start_server() -> None:
        spawn_server_process(build_server_command(root, bind, port), root, env, log)

    try:
        winupdate.recover_interrupted_switch(
            root,
            backup,
            health_check=lambda: health_endpoint_ok(bind, port, env),
            stop_server=lambda: None,
            start_server=start_server,
            status_path=update_status_path(state_dir),
        )
    except Exception as exc:
        log_line(log, f"supervisor: interrupted-update recovery failed: {exc}")
        return False
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if health_endpoint_ok(bind, port, env):
            log_line(log, "supervisor: restored the previous release and it is healthy")
            return True
        time.sleep(0.5)
    log_line(
        log,
        "supervisor: restored the previous release but it is not healthy yet; "
        "the restart loop will keep trying",
    )
    return True


def serve(root: Path, env: dict[str, str], bind: str, port: int, log_path: Path, once: bool) -> int:
    command = build_server_command(root, bind, port)
    state_dir = default_state_dir()
    restarts: list[float] = []
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # A cwd inside the live tree would hold a handle that blocks the updater
    # from moving the tree (and block our own recovery moves). Move to the
    # state directory, which is outside the tree.
    with contextlib.suppress(OSError):
        state_dir.mkdir(parents=True, exist_ok=True)
        os.chdir(str(state_dir))
    try:
        with log_path.open("a", encoding="utf-8", buffering=1) as log:
            log_line(log, f"supervisor: root={root} bind={bind} port={port}")
            while True:
                write_supervisor_lock(state_dir)
                if not once and (
                    switch_lock_active(state_dir)
                    or update_status_active_fresh(state_dir)
                ):
                    log_line(log, "supervisor: update in progress; deferring restart")
                    time.sleep(5.0)
                    continue
                if not once and health_endpoint_ok(bind, port, env):
                    log_line(log, f"supervisor: server already healthy on port {port}; monitoring")
                    time.sleep(5.0)
                    continue
                if not once and recover_interrupted_update(root, state_dir, env, bind, port, log):
                    continue
                try:
                    process = spawn_server_process(command, root, env, log)
                except Exception as exc:
                    # e.g. the live tree is mid-move or temporarily gone:
                    # log, back off, and keep the loop alive.
                    log_line(log, f"supervisor: could not start server: {exc}")
                    if once:
                        return 1
                    now = time.monotonic()
                    delay = compute_backoff(restarts, now)
                    restarts = [stamp for stamp in restarts if now - stamp < BACKOFF_WINDOW_SECONDS]
                    restarts.append(now)
                    log_line(log, f"supervisor: retrying in {delay:.1f}s")
                    deadline = time.monotonic() + delay
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        try:
                            time.sleep(min(remaining, 0.5))
                        except KeyboardInterrupt:
                            log_line(log, "supervisor: interrupt received; not restarting")
                            return 130
                    continue
                try:
                    returncode = process.wait()
                except KeyboardInterrupt:
                    log_line(log, "supervisor: interrupt received; shutting down")
                    terminate_child(process, log)
                    log_line(log, "supervisor: stopped")
                    return 130
                log_line(log, f"supervisor: server exited with code {returncode}")
                if once:
                    return returncode if returncode is not None else 1
                now = time.monotonic()
                delay = compute_backoff(restarts, now)
                restarts = [stamp for stamp in restarts if now - stamp < BACKOFF_WINDOW_SECONDS]
                restarts.append(now)
                log_line(log, f"supervisor: restarting in {delay:.1f}s")
                deadline = time.monotonic() + delay
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        time.sleep(min(remaining, 0.5))
                    except KeyboardInterrupt:
                        log_line(log, "supervisor: interrupt received; not restarting")
                        return 130
    finally:
        with contextlib.suppress(OSError):
            supervisor_lock_path(state_dir).unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentsServer user-level supervisor")
    parser.add_argument("--bind", default=os.environ.get("AGENT_BIND", "0.0.0.0"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("AGENT_PORT", "7850")),
    )
    parser.add_argument(
        "--log",
        default=os.environ.get("AGENTSDOCK_SUPERVISE_LOG", ""),
        help="log file path (default <repo>/server.log)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run a single server lifetime, no restart loop (for tests)",
    )
    args = parser.parse_args(argv)

    root = repo_root()
    state_dir = default_state_dir()
    env = os.environ.copy()
    load_server_env(state_dir, env)
    log_path = Path(args.log).expanduser() if args.log else root / "server.log"
    return serve(root, env, args.bind, int(args.port), log_path, args.once)


if __name__ == "__main__":
    raise SystemExit(main())
