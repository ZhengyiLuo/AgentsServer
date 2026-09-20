#!/usr/bin/env python3
"""Windows-native release staging, switching, and rollback for AgentsServer.

The POSIX lifecycle (systemd/launchd + ``install.sh``) has no Windows
equivalent, so this module implements the native path:

1. ``verify_release`` reuses :func:`update_runner.verify_manifest` unchanged
   (ed25519 manifest signature, sha256 archive format, immutable release
   tag/URL prefix trust rules).
2. ``extract_safe`` unpacks the signed tar.gz (zip accepted for tests) with
   path-traversal rejection, symlink/hardlink member rejection, and member
   count/size caps mirroring ``update_runner.safe_extract``.
3. ``stage_release`` verifies the archive checksum and atomically stages the
   release under a staging root with a ``update_status``-style JSON heartbeat.
4. ``switch_with_rollback`` stops the *owned* server through an injected
   callable, moves the live tree aside (``<live>.old``), moves the staged tree
   in, starts the server, and polls an injected ``health_check()``. On failure
   it restores ``<live>.old`` and restarts the previous version. On success it
   removes the backup.

Windows locking semantics: a directory rename fails while any process keeps an
open handle inside it (the stopped server's current directory, log files, DLLs,
or the venv it is running from if it lives under ``live_dir``). The stop
callable must therefore ensure the server process has *fully exited* (and the
process was reaped, e.g. ``Popen.wait()``) before the move is attempted. Moves
are retried with exponential backoff; an unrecoverable ``PermissionError``
surfaces as :class:`FileLockError` naming the locked path.

Nothing here routes through ``install.sh``; nothing deletes outside the live
directory and its same-parent siblings (``<live>.old`` / ``<live>.failed``).

The CLI (``python winupdate.py --mode drive|finish ...``) wires this into the
managed self-update: the server spawns the *drive* stage under the project
virtualenv (signature verification needs cryptography), which verifies,
downloads, and stages the release, asserts the server idle, and detaches the
*finish* stage under the BASE interpreter — one that lives outside the live
tree and loads nothing from it, so no process holds a handle inside the tree
while it is moved. The server self-exits after handing off; the finish stage
waits for the server's pid to die and its health endpoint to go down, then
runs :func:`switch_with_rollback` (stop only terminates servers the finish
stage itself spawned — the previous server is already gone — then a detached
start of the new release and an HTTP health check) and reports every phase
through the status JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.request
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# update_runner imports cryptography, which only the project virtualenv has.
# The detached "finish" stage of the Windows update runs under the base
# interpreter (outside the live tree, so it holds no locks on the tree it
# moves), so every update_runner touchpoint resolves lazily and every status
# write has a byte-format-identical stdlib fallback.

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024  # mirrors update_runner.MAX_ARCHIVE_BYTES
MAX_MEMBERS = 10_000
MAX_TOTAL_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MOVE_RETRY_ATTEMPTS = 5
MOVE_RETRY_BASE_SECONDS = 0.25
STATUS_WRITE_ATTEMPTS = 5
STATUS_WRITE_BASE_SECONDS = 0.05
SICK_DIR_SUFFIX = ".failed"
BACKUP_SUFFIX = ".old"
RELEASE_TRACKS = {"stable", "beta"}

SERVER_EXIT_TIMEOUT_SECONDS = 120.0
SERVER_EXIT_POLL_SECONDS = 2.0
DEPENDENCY_SYNC_TIMEOUT_SECONDS = 900.0
DEPENDENCY_SYNC_HEARTBEAT_SECONDS = 10.0
FINISH_HEALTH_ATTEMPTS = 8
FINISH_HEALTH_SETTLE_SECONDS = 2.0
SWITCH_LOCK_MAX_AGE_SECONDS = 150.0
UPDATE_USER_AGENT = "AgentsServer-WinUpdate/1"

_DETACHED_CREATIONFLAGS = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    | getattr(subprocess, "CREATE_NO_WINDOW", 0)
)

_UPDATE_RUNNER_RESOLVED = False
_UPDATE_RUNNER_MODULE: Any = None


def _update_runner() -> Any:
    global _UPDATE_RUNNER_RESOLVED, _UPDATE_RUNNER_MODULE
    if not _UPDATE_RUNNER_RESOLVED:
        _UPDATE_RUNNER_RESOLVED = True
        try:
            import update_runner as module

            _UPDATE_RUNNER_MODULE = module
        except Exception:
            _UPDATE_RUNNER_MODULE = None
    return _UPDATE_RUNNER_MODULE


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _native_atomic_json(path: Path, value: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    with suppress(OSError):
        os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _native_update_status(path: Path, **changes: Any) -> dict[str, Any]:
    try:
        current = json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    current.update(changes)
    current["updated_at"] = utc_now()
    _native_atomic_json(path, current)
    return current


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    _native_atomic_json(path, value)


def update_status(path: Path, **changes: Any) -> dict[str, Any]:
    """Durable JSON heartbeat, byte-format-identical to update_runner's.

    Prefers update_runner.update_status when that module (and cryptography)
    is importable; otherwise falls back to the native stdlib writer above.
    Both paths retry transient PermissionError — real-time antivirus /
    Search indexer briefly hold freshly written files, so the final
    os.replace can hit a transient sharing violation (WinError 5).
    """

    runner = _update_runner()
    writer = runner.update_status if runner is not None else None
    last_error: PermissionError | None = None
    for index in range(STATUS_WRITE_ATTEMPTS):
        try:
            if writer is not None:
                return writer(path, **changes)
            return _native_update_status(path, **changes)
        except PermissionError as exc:
            last_error = exc
            if index < STATUS_WRITE_ATTEMPTS - 1:
                time.sleep(STATUS_WRITE_BASE_SECONDS * (index + 1))
    assert last_error is not None
    raise last_error


class WindowsUpdateError(RuntimeError):
    """Base class for every Windows update failure."""


class ReleaseVerificationError(WindowsUpdateError):
    """The signed manifest or the release checksum failed verification."""


class ExtractionError(WindowsUpdateError):
    """The release archive is unsafe or malformed."""


class StagingError(WindowsUpdateError):
    """The staged release directory is missing, invalid, or misplaced."""


class ServerStopError(WindowsUpdateError):
    """The owned server could not be stopped before the switch."""


class HealthCheckError(WindowsUpdateError):
    """The new release (or the restored backup) never became healthy."""


class FileLockError(WindowsUpdateError):
    """A file or directory is locked; the message names the locked path."""


@dataclass
class SwitchResult:
    """Outcome of :func:`switch_with_rollback`."""

    switched: bool
    rolled_back: bool
    live_dir: Path


def verify_release(
    manifest_bytes: bytes,
    signature_bytes: bytes,
    public_key_path: Path,
    *,
    expected_version: str | None = None,
    track: str = "stable",
) -> dict[str, Any]:
    """Verify a signed release manifest; same trust rules as update_runner."""

    runner = _update_runner()
    if runner is None:
        raise ReleaseVerificationError(
            "release verification requires update_runner (and cryptography); "
            "run under the project virtualenv"
        )
    try:
        return runner.verify_manifest(
            manifest_bytes,
            signature_bytes,
            Path(public_key_path),
            expected_version=expected_version,
            track=track,
        )
    except WindowsUpdateError:
        raise
    except Exception as exc:
        raise ReleaseVerificationError(f"release verification failed: {exc}") from exc


def _require_update_runner() -> Any:
    runner = _update_runner()
    if runner is None:
        raise WindowsUpdateError(
            "update_runner is unavailable in this interpreter; "
            "the drive stage must run under the project virtualenv"
        )
    return runner


# Lazy update_runner touchpoints (module-level names so tests can patch them).

def check_release(public_key_path: Path, track: str = "stable") -> dict[str, Any]:
    return _require_update_runner().check_release(Path(public_key_path), track)


def download_release(url: str, limit: int, timeout: float = 30.0) -> bytes:
    return _require_update_runner().download_bytes(url, limit, timeout=timeout)


def release_transition_allowed(current: str, target: str, track: str = "stable") -> bool:
    return _require_update_runner().release_transition_allowed(current, target, track)


def assert_server_idle(port: int, *, token: str | None = None) -> None:
    _require_update_runner().assert_server_idle(int(port), token=token)


def consume_auth_token_file(path: str | None) -> str:
    runner = _update_runner()
    if runner is not None:
        return runner.consume_auth_token_file(path)
    clean_path = str(path or "").strip()
    if not clean_path:
        return ""
    token_path = Path(clean_path).expanduser().resolve()
    try:
        value = json.loads(token_path.read_text())
        token = str(value.get("token") or "") if isinstance(value, dict) else ""
    finally:
        with suppress(FileNotFoundError):
            token_path.unlink()
    if not token:
        raise WindowsUpdateError("server update health credential is empty")
    return token


def _reject_unsafe_member(destination: Path, member_name: str) -> Path:
    target = (destination / member_name).resolve()
    if destination != target and destination not in target.parents:
        raise ExtractionError(f"release archive contains an unsafe path: {member_name!r}")
    return target


def _validate_members(destination: Path, members: list[tuple[str, int, bool]]) -> None:
    if len(members) > MAX_MEMBERS:
        raise ExtractionError(
            f"release archive has {len(members)} members (limit {MAX_MEMBERS})"
        )
    total = 0
    for name, size, is_link in members:
        if is_link:
            raise ExtractionError(f"release archive must not contain links: {name!r}")
        _reject_unsafe_member(destination, name)
        total += size
    if total > MAX_TOTAL_UNCOMPRESSED_BYTES:
        raise ExtractionError(
            f"release archive expands to {total} bytes (limit {MAX_TOTAL_UNCOMPRESSED_BYTES})"
        )


def extract_safe(archive_bytes: bytes, destination: Path) -> Path:
    """Extract a tar.gz (or zip) byte payload into ``destination`` safely.

    Traversal members (``../evil``), absolute paths, and symlink/hardlink
    members are rejected before anything is written; member count and total
    uncompressed size are capped. Returns the single top-level directory when
    the archive has exactly one (the packaged layout), else ``destination``.
    """

    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise ExtractionError(
            f"release archive exceeds the {MAX_ARCHIVE_BYTES}-byte safety limit"
        )
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    source = io.BytesIO(archive_bytes)
    head = archive_bytes[:4]

    if head[:2] == b"\x1f\x8b":
        with tarfile.open(fileobj=source, mode="r:gz") as archive:
            members = archive.getmembers()
            _validate_members(
                destination,
                [
                    (m.name, m.size if m.isfile() else 0, m.issym() or m.islnk())
                    for m in members
                ],
            )
            archive.extractall(destination, members=members, filter="data")
    elif head[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(source) as archive:
            infos = archive.infolist()
            _validate_members(
                destination,
                [
                    (
                        i.filename,
                        i.file_size if not i.is_dir() else 0,
                        stat.S_IFMT(i.external_attr >> 16) == stat.S_IFLNK,
                    )
                    for i in infos
                ],
            )
            archive.extractall(destination)
    else:
        raise ExtractionError("unsupported release archive format (expected tar.gz or zip)")

    roots = [entry for entry in destination.iterdir()]
    if len(roots) == 1 and roots[0].is_dir():
        return roots[0]
    return destination


def _move_with_retry(
    source: Path,
    target: Path,
    *,
    attempts: int = MOVE_RETRY_ATTEMPTS,
    base_seconds: float = MOVE_RETRY_BASE_SECONDS,
) -> None:
    """Move ``source`` to ``target`` with backoff, raising FileLockError."""

    last_error: OSError | None = None
    for index in range(max(1, attempts)):
        try:
            os.replace(source, target)
            return
        except FileNotFoundError:
            raise
        except OSError as exc:
            last_error = exc
            if index < attempts - 1:
                time.sleep(base_seconds * (2 ** index))
    raise FileLockError(
        f"{source} is locked or still in use after {attempts} attempts "
        f"(last error: {last_error}); the previous server must fully stop and "
        f"release its files before the live directory can move"
    ) from last_error


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def stage_release(
    *,
    status_path: Path,
    manifest: dict[str, Any],
    archive_bytes: bytes,
    staging_root: Path,
) -> Path:
    """Checksum-verify and stage a release; return the staged directory.

    The archive is extracted to a unique temporary directory inside
    ``staging_root`` and then renamed to its final staged name, so a staging
    failure never leaves a half-extracted target behind and a crashed updater
    never confuses the next run (temporary names start with ``.`` and are
    unique per process/nonce).
    """

    status_path = Path(status_path)
    staging_root = Path(staging_root).resolve()
    version = str(manifest.get("version") or "")
    update_status(
        status_path,
        phase="staging",
        target_version=version,
        message=f"Staging AgentsServer {version}.",
    )
    archive = manifest.get("archive")
    expected_sha = str(archive.get("sha256") or "").lower() if isinstance(archive, dict) else ""
    digest = hashlib.sha256(archive_bytes).hexdigest()
    if digest != expected_sha:
        update_status(
            status_path,
            phase="failed",
            message="release archive checksum does not match the signed manifest",
            finished_at=utc_now(),
        )
        raise ReleaseVerificationError(
            "release archive checksum does not match the signed manifest"
        )

    staging_root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(4)
    temporary = staging_root / f".extract-{os.getpid()}-{token}"
    final = staging_root / f"staged-{version}-{os.getpid()}-{token}"
    try:
        extracted = extract_safe(archive_bytes, temporary)
        os.replace(extracted, final)
    except BaseException:
        _remove_tree(temporary)
        if final.exists() and not any(final.iterdir()):
            _remove_tree(final)
        raise
    _remove_tree(temporary)
    update_status(
        status_path,
        phase="staged",
        staged_dir=str(final),
        message=f"AgentsServer {version} staged for switching.",
    )
    return final


def _poll_health(health_check: Callable[[], bool], attempts: int, settle_seconds: float) -> bool:
    for index in range(max(1, attempts)):
        if index:
            time.sleep(settle_seconds)
        try:
            if health_check():
                return True
        except Exception:
            pass
    return False


def _stop_quietly(stop_server: Callable[[], Any]) -> None:
    try:
        stop_server()
    except Exception:
        pass


def _recover_interrupted_switch(
    live_dir: Path,
    backup_dir: Path,
    *,
    health_check: Callable[[], bool],
    stop_server: Callable[[], Any],
    start_server: Callable[[], Any],
    status_path: Path | None,
) -> None:
    """Restore consistency after a previous switch died mid-operation.

    Leftover states and their resolution:

    - ``<live>.old`` exists, ``live`` exists: interrupted after both moves.
      Health-check the current (new) tree; keep it when healthy (drop the
      backup), otherwise roll back to the backup.
    - ``<live>.old`` exists, ``live`` missing: interrupted between the two
      moves; move the backup back into place.
    - ``<live>.failed`` leftover is cleaned whenever a live tree exists.
    """

    def _status(**changes: Any) -> None:
        if status_path is not None:
            update_status(status_path, **changes)

    sick_dir = live_dir.with_name(live_dir.name + SICK_DIR_SUFFIX)
    if sick_dir.exists() and live_dir.exists():
        _remove_tree(sick_dir)
    if not backup_dir.exists():
        return
    _status(
        phase="recovering",
        message="Detected an interrupted switch; restoring a consistent live tree.",
    )
    if not live_dir.exists():
        _move_with_retry(backup_dir, live_dir)
        return
    healthy = False
    try:
        healthy = bool(health_check())
    except Exception:
        healthy = False
    if healthy:
        _remove_tree(backup_dir)
        return
    _stop_quietly(stop_server)
    _remove_tree(sick_dir)
    _move_with_retry(live_dir, sick_dir)
    _move_with_retry(backup_dir, live_dir)
    _remove_tree(sick_dir)
    try:
        start_server()
    except Exception:
        pass


def recover_interrupted_switch(
    live_dir: Path,
    backup_dir: Path,
    *,
    health_check: Callable[[], bool],
    stop_server: Callable[[], Any],
    start_server: Callable[[], Any],
    status_path: Path | None = None,
) -> None:
    """Public entry point for restoring a consistent live tree (supervisor).

    Safe to call on every restart: it returns immediately when no backup
    exists, and callers are expected to bound it further (health down, no
    switch lock, no fresh active update status) so it never fights a human
    mid-surgery or a live finisher.
    """

    _recover_interrupted_switch(
        Path(live_dir),
        Path(backup_dir),
        health_check=health_check,
        stop_server=stop_server,
        start_server=start_server,
        status_path=status_path,
    )


def switch_with_rollback(
    *,
    staged_dir: Path,
    live_dir: Path,
    stop_server: Callable[[], Any],
    start_server: Callable[[], Any],
    health_check: Callable[[], bool],
    settle_seconds: float = 2.0,
    attempts: int = 3,
    status_path: Path | None = None,
    move_attempts: int = MOVE_RETRY_ATTEMPTS,
    retry_base_seconds: float = MOVE_RETRY_BASE_SECONDS,
) -> SwitchResult:
    """Switch the live tree to ``staged_dir`` with automatic rollback.

    Order of operations: recover leftovers from a previously interrupted run,
    stop the owned server (``stop_server`` must wait until the process has
    fully exited — see module docstring for the Windows locking rule), move
    ``live_dir`` aside to ``<live>.old``, move ``staged_dir`` into place, start
    the server, and poll ``health_check()``. On health failure the previous
    tree is restored and restarted. Returns a :class:`SwitchResult`.
    """

    staged_dir = Path(staged_dir).resolve()
    live_dir = Path(live_dir).resolve()
    backup_dir = live_dir.with_name(live_dir.name + BACKUP_SUFFIX)
    sick_dir = live_dir.with_name(live_dir.name + SICK_DIR_SUFFIX)
    status_path = Path(status_path) if status_path else None

    if not staged_dir.is_dir():
        raise StagingError(f"staged release directory does not exist: {staged_dir}")
    if staged_dir == live_dir or live_dir in staged_dir.parents or staged_dir in live_dir.parents:
        raise StagingError(
            f"staged directory {staged_dir} must not overlap the live directory {live_dir}"
        )

    def _status(**changes: Any) -> None:
        if status_path is not None:
            update_status(status_path, **changes)

    _status(phase="preparing", message="Preparing to switch the live AgentsServer tree.")
    _recover_interrupted_switch(
        live_dir,
        backup_dir,
        health_check=health_check,
        stop_server=stop_server,
        start_server=start_server,
        status_path=status_path,
    )

    _status(phase="stopping", message="Stopping the owned AgentsServer process.")
    try:
        stop_server()
    except Exception as exc:
        _status(phase="failed", message=f"could not stop the owned server: {exc}", finished_at=utc_now())
        raise ServerStopError(f"could not stop the owned server: {exc}") from exc

    _status(phase="switching", message="Moving the staged release into place.")
    try:
        if live_dir.exists():
            _move_with_retry(
                live_dir, backup_dir, attempts=move_attempts, base_seconds=retry_base_seconds
            )
        _move_with_retry(
            staged_dir, live_dir, attempts=move_attempts, base_seconds=retry_base_seconds
        )
    except FileLockError as exc:
        if not live_dir.exists() and backup_dir.exists():
            try:
                _move_with_retry(backup_dir, live_dir, attempts=2, base_seconds=0.05)
            except WindowsUpdateError:
                pass
        _status(phase="failed", message=str(exc), finished_at=utc_now())
        raise
    except FileNotFoundError as exc:
        _status(phase="failed", message=f"switch failed, path vanished: {exc}", finished_at=utc_now())
        raise WindowsUpdateError(f"switch failed, path vanished: {exc}") from exc

    _status(phase="starting", message="Starting the updated AgentsServer.")
    start_server()

    _status(phase="health_check", message="Waiting for the updated server to become healthy.")
    if _poll_health(health_check, attempts, settle_seconds):
        _remove_tree(backup_dir)
        _status(
            phase="complete",
            message="AgentsServer update is installed and healthy.",
            installed_version=_read_version(live_dir),
            finished_at=utc_now(),
        )
        return SwitchResult(switched=True, rolled_back=False, live_dir=live_dir)

    _status(
        phase="rolling_back",
        message="The updated server failed its health check; restoring the previous release.",
    )
    _stop_quietly(stop_server)
    had_backup = backup_dir.exists()
    if had_backup:
        _remove_tree(sick_dir)
        try:
            _move_with_retry(
                live_dir, sick_dir, attempts=move_attempts, base_seconds=retry_base_seconds
            )
            _move_with_retry(
                backup_dir, live_dir, attempts=move_attempts, base_seconds=retry_base_seconds
            )
        except FileLockError as exc:
            _status(phase="failed", message=str(exc), finished_at=utc_now())
            raise
        _remove_tree(sick_dir)
        try:
            start_server()
        except Exception as exc:
            _status(phase="failed", message=f"rolled-back server failed to start: {exc}", finished_at=utc_now())
            raise HealthCheckError(f"rolled-back server failed to start: {exc}") from exc
        if _poll_health(health_check, attempts, settle_seconds):
            _status(
                phase="rolled_back",
                message="The update failed its health check; the previous release is live and healthy.",
                rolled_back=True,
                finished_at=utc_now(),
            )
            return SwitchResult(switched=False, rolled_back=True, live_dir=live_dir)
        _status(phase="failed", message="rollback completed but the server is not healthy", finished_at=utc_now())
        raise HealthCheckError("rollback completed but the server is not healthy")

    _remove_tree(sick_dir)
    try:
        _move_with_retry(live_dir, sick_dir, attempts=move_attempts, base_seconds=retry_base_seconds)
    except WindowsUpdateError:
        pass
    _status(
        phase="failed",
        message="the new release failed its health check and no previous release exists to restore",
        finished_at=utc_now(),
    )
    raise HealthCheckError(
        "the new release failed its health check and no previous release exists to restore "
        f"(moved aside to {sick_dir})"
    )


def rollback(
    live_dir: Path,
    backup_dir: Path,
    stop_server: Callable[[], Any],
    start_server: Callable[[], Any],
    health_check: Callable[[], bool],
    *,
    settle_seconds: float = 2.0,
    attempts: int = 3,
    status_path: Path | None = None,
    move_attempts: int = MOVE_RETRY_ATTEMPTS,
    retry_base_seconds: float = MOVE_RETRY_BASE_SECONDS,
) -> bool:
    """Explicitly restore ``backup_dir`` over ``live_dir`` and restart."""

    live_dir = Path(live_dir).resolve()
    backup_dir = Path(backup_dir).resolve()
    sick_dir = live_dir.with_name(live_dir.name + SICK_DIR_SUFFIX)
    status_path = Path(status_path) if status_path else None

    def _status(**changes: Any) -> None:
        if status_path is not None:
            update_status(status_path, **changes)

    if not backup_dir.is_dir():
        raise WindowsUpdateError(f"no backup release exists to roll back to: {backup_dir}")

    _status(phase="rolling_back", message="Rolling back to the previous AgentsServer release.")
    _stop_quietly(stop_server)
    _remove_tree(sick_dir)
    if live_dir.exists():
        _move_with_retry(live_dir, sick_dir, attempts=move_attempts, base_seconds=retry_base_seconds)
    _move_with_retry(backup_dir, live_dir, attempts=move_attempts, base_seconds=retry_base_seconds)
    _remove_tree(sick_dir)
    try:
        start_server()
    except Exception as exc:
        _status(phase="failed", message=f"rolled-back server failed to start: {exc}", finished_at=utc_now())
        raise HealthCheckError(f"rolled-back server failed to start: {exc}") from exc
    if not _poll_health(health_check, attempts, settle_seconds):
        _status(phase="failed", message="rollback completed but the server is not healthy", finished_at=utc_now())
        raise HealthCheckError("rollback completed but the server is not healthy")
    _status(
        phase="rolled_back",
        message="The previous AgentsServer release is live and healthy.",
        rolled_back=True,
        installed_version=_read_version(live_dir),
        finished_at=utc_now(),
    )
    return True


def _read_version(live_dir: Path) -> str:
    try:
        return (Path(live_dir) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def autostart_command(port: int, log_path: Path) -> list[str]:
    """Return the schtasks argv that registers the user-level supervisor.

    The task runs ``scripts/agents_server_supervise.py`` at user logon under
    the current account (``/RU %USERNAME%``, no password stored). This
    function only *builds* the argument list — registration is explicitly
    user-consented and is never executed here or by any test.
    """

    repo_root = Path(__file__).resolve().parent
    python = repo_root / ".venv" / "Scripts" / "python.exe"
    if not python.is_file():
        python = Path(sys.executable)
    script = repo_root / "scripts" / "agents_server_supervise.py"
    task_command = f'"{python}" "{script}" --port {int(port)} --log "{Path(log_path).resolve()}"'
    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return [
        "schtasks",
        "/Create",
        "/F",
        "/TN",
        "AgentsServer",
        "/RU",
        username,
        "/SC",
        "ONLOGON",
        "/TR",
        task_command,
    ]


__all__ = [
    "WindowsUpdateError",
    "ReleaseVerificationError",
    "ExtractionError",
    "StagingError",
    "ServerStopError",
    "HealthCheckError",
    "FileLockError",
    "SwitchResult",
    "verify_release",
    "extract_safe",
    "stage_release",
    "switch_with_rollback",
    "rollback",
    "recover_interrupted_switch",
    "autostart_command",
    "update_status",
    "atomic_json",
    "utc_now",
]


# --------------------------------------------------------------------------
# Detached update driver / finisher (CLI)
#
# The server spawns the driver under the project virtualenv (it needs
# cryptography for signature verification). The driver verifies, downloads,
# and stages the release, asserts the server idle, then hands the actual
# tree switch to a short-lived "finish" stage running under the BASE
# interpreter — an interpreter that lives outside the live tree and loads
# nothing from it, so no process holds a handle inside the tree while it is
# moved. The server self-exits after handing off; the finisher waits for the
# server's pid to die and its health endpoint to go down, then switches.
# --------------------------------------------------------------------------


def default_live_dir() -> Path:
    return Path(__file__).resolve().parent


def default_state_dir() -> Path:
    configured = os.environ.get("AGENTSDOCK_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".agentsdock"


def default_staging_root(state_dir: Path) -> Path:
    return Path(state_dir) / "updates" / "staging"


def load_server_env(state_dir: Path, env: dict[str, str]) -> None:
    """Fold optional KEY=VALUE lines from server.env into env (setdefault).

    Mirrors scripts/agents_server_supervise.py; values are never printed.
    """

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


def poll_health_ok(port: int, token: str | None = None, timeout: float = 5.0) -> bool:
    """True when /api/health answers {"ok": true} (Bearer token when given)."""

    headers = {"User-Agent": UPDATE_USER_AGENT}
    clean_token = str(token or "").strip()
    if clean_token:
        headers["Authorization"] = f"Bearer {clean_token}"
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{int(port)}/api/health", headers=headers
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read(1_000_001)
        if len(content) > 1_000_000:
            return False
        return json.loads(content).get("ok") is True
    except Exception:
        return False


def _windows_pid_alive(pid: int) -> bool:
    """OpenProcess-based existence check for Windows.

    ``os.kill(pid, 0)`` is unreliable from a process created with
    ``CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`` on current CPython builds
    (it can fail with WinError 87 even for live pids), and the detached drive
    and finish stages are created with exactly those flags.
    """

    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    synchronize = 0x00100000
    query_limited = 0x1000
    handle = kernel32.OpenProcess(synchronize | query_limited, False, int(pid))
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def process_id_alive(pid: Any) -> bool:
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid_int)
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def find_base_interpreter(live_dir: Path) -> Path | None:
    """The interpreter that finishes the switch; it must live outside the tree.

    A running interpreter locks its own image, so a finish stage running from
    inside live_dir would make the tree move fail with FileLockError.
    """

    raw = str(getattr(sys, "_base_executable", "") or "").strip() or sys.executable
    candidate = Path(raw)
    if not candidate.is_file():
        return None
    try:
        candidate.resolve().relative_to(Path(live_dir).resolve())
    except ValueError:
        return candidate
    return None


def switch_lock_path(state_dir: Path) -> Path:
    return Path(state_dir) / "updates" / "switch.lock"


def acquire_switch_lock(state_dir: Path) -> Path:
    path = switch_lock_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    _native_atomic_json(path, {"pid": os.getpid(), "started_at": utc_now()})
    return path


def release_switch_lock(state_dir: Path) -> None:
    with suppress(FileNotFoundError):
        switch_lock_path(state_dir).unlink()


def switch_lock_active(state_dir: Path, max_age_seconds: float = SWITCH_LOCK_MAX_AGE_SECONDS) -> bool:
    """Fresh switch lock ⇒ a finisher holds the tree; restarts must defer.

    Mirrors the check in scripts/agents_server_supervise.py.
    """

    try:
        age = time.time() - switch_lock_path(state_dir).stat().st_mtime
    except OSError:
        return False
    return 0 <= age < max_age_seconds


def _spawn_detached(argv: list[str], *, cwd: Path, log_path: Path | None = None) -> subprocess.Popen:
    handle = open(log_path, "ab") if log_path is not None else subprocess.DEVNULL
    try:
        return subprocess.Popen(
            [str(part) for part in argv],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=_DETACHED_CREATIONFLAGS,
            close_fds=True,
        )
    finally:
        if handle is not subprocess.DEVNULL:
            handle.close()


def _ensure_runtime(live_dir: Path, status_path: Path | None = None) -> None:
    """Create the release .venv with `uv sync --frozen` when it is missing."""

    python = Path(live_dir) / ".venv" / "Scripts" / "python.exe"
    if python.is_file():
        return
    uv = shutil.which("uv")
    if not uv:
        fallback = Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".local" / "bin" / "uv.exe"
        if fallback.is_file():
            uv = str(fallback)
    if not uv:
        raise StagingError(
            "the staged release has no .venv and uv was not found to create one"
        )

    def _heartbeat(elapsed: int) -> None:
        if status_path is not None:
            update_status(
                status_path,
                phase="installing",
                message=f"Creating the release runtime ({elapsed}s elapsed).",
                heartbeat_at=utc_now(),
                elapsed_seconds=elapsed,
            )

    _heartbeat(0)
    started = time.monotonic()
    process = subprocess.Popen(
        [str(uv), "sync", "--frozen"],
        cwd=str(live_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_DETACHED_CREATIONFLAGS,
    )
    last_beat = 0.0
    while process.poll() is None:
        elapsed = int(time.monotonic() - started)
        if time.monotonic() - last_beat >= DEPENDENCY_SYNC_HEARTBEAT_SECONDS:
            _heartbeat(elapsed)
            last_beat = time.monotonic()
        if time.monotonic() - started > DEPENDENCY_SYNC_TIMEOUT_SECONDS:
            process.kill()
            process.wait()
            raise StagingError("timed out creating the release runtime")
        time.sleep(1.0)
    if process.returncode != 0:
        raise StagingError(f"uv sync --frozen failed with exit code {process.returncode}")


def spawn_server_detached(
    live_dir: Path,
    *,
    port: int,
    bind: str,
    state_dir: Path,
    status_path: Path | None = None,
) -> None:
    """Launch the (new) release's server detached and return the process.

    Releases with ``agent_server.py`` are production trees; a bare
    ``fake_server.py`` release is a test/development seam whose health flavor
    comes from its HEALTH file. The caller owns termination (rollback must
    stop the process before the tree can move again).
    """

    live_dir = Path(live_dir).resolve()
    state_dir = Path(state_dir)
    python = live_dir / ".venv" / "Scripts" / "python.exe"
    if (live_dir / "agent_server.py").is_file():
        _ensure_runtime(live_dir, status_path=status_path)
        command = [
            str(python),
            str(live_dir / "agent_server.py"),
            "serve",
            "--bind",
            str(bind),
            "--port",
            str(int(port)),
        ]
    elif (live_dir / "fake_server.py").is_file():
        flavor = "healthy"
        with suppress(OSError):
            flavor = (live_dir / "HEALTH").read_text(encoding="utf-8").strip() or "healthy"
        command = [
            str(Path(sys.executable)),
            str(live_dir / "fake_server.py"),
            str(int(port)),
            flavor,
            str(os.getpid()),
        ]
    else:
        raise StagingError(f"no server entry point found in {live_dir}")
    env = os.environ.copy()
    load_server_env(state_dir, env)
    env.setdefault("AGENTSDOCK_STATE_DIR", str(state_dir))
    log_handle = open(live_dir / "server.log", "ab")
    try:
        return subprocess.Popen(
            command,
            cwd=str(live_dir),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=_DETACHED_CREATIONFLAGS,
            close_fds=True,
        )
    finally:
        log_handle.close()


def terminate_processes(processes: list[subprocess.Popen]) -> None:
    """Terminate spawned server processes so rollback moves can take the tree.

    A server started with cwd inside the live tree holds a handle that blocks
    the directory rename; rollback must fully stop it before moving.
    """

    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    processes.clear()


def _read_token_file(path: Path) -> str:
    token_path = Path(path)
    try:
        value = json.loads(token_path.read_text())
        return str(value.get("token") or "") if isinstance(value, dict) else ""
    finally:
        with suppress(FileNotFoundError):
            token_path.unlink()


def build_finish_argv(
    args: argparse.Namespace,
    *,
    staged_dir: Path,
    server_pid: int,
    health_token_file: Path | None,
) -> list[str]:
    base = find_base_interpreter(Path(args.live_dir).expanduser().resolve())
    if base is None:
        raise WindowsUpdateError(
            "no interpreter outside the live tree is available to finish the update"
        )
    argv = [
        str(base),
        str(Path(__file__).resolve()),
        "--mode",
        "finish",
        "--status-file",
        str(args.status_file),
        "--public-key",
        str(args.public_key),
        "--port",
        str(int(args.port)),
        "--bind",
        str(args.bind),
        "--live-dir",
        str(args.live_dir),
        "--staging-root",
        str(args.staging_root),
        "--state-dir",
        str(args.state_dir),
        "--staged-dir",
        str(staged_dir),
        "--server-pid",
        str(int(server_pid)),
    ]
    if health_token_file is not None:
        argv.extend(["--health-token-file", str(health_token_file)])
    return argv


def run_drive(args: argparse.Namespace) -> int:
    """Verify, download, and stage the release; hand the switch to the finisher."""

    status_path = Path(args.status_file).expanduser().resolve()
    try:
        track = str(args.track or "stable").strip().lower()
        if track not in RELEASE_TRACKS:
            raise ReleaseVerificationError(f"invalid release track: {args.track}")
        public_key = Path(args.public_key).expanduser().resolve()
        live_dir = Path(args.live_dir).expanduser().resolve()
        staging_root = Path(args.staging_root).expanduser().resolve()
        state_dir = Path(args.state_dir).expanduser().resolve()
        server_pid = os.getppid()  # the spawning server; capture before it exits
        if find_base_interpreter(live_dir) is None:
            raise WindowsUpdateError(
                "the interpreter running this updater lives inside the live tree; "
                "cannot safely finish the switch"
            )
        update_status(
            status_path,
            phase="checking",
            track=track,
            message=f"Checking the signed {track} release manifest.",
        )
        manifest = check_release(public_key, track)
        version = str(manifest["version"])
        if args.expected_version and version != args.expected_version:
            raise ReleaseVerificationError(
                f"latest signed release is {version}, not {args.expected_version}"
            )
        if args.current_version and not release_transition_allowed(
            args.current_version, version, track
        ):
            raise WindowsUpdateError(
                f"resolved release {version} is not newer than installed version "
                f"{args.current_version}; managed updates only permit forward "
                "updates or an explicit beta-to-stable channel switch"
            )
        update_status(
            status_path,
            phase="downloading",
            track=track,
            target_version=version,
            message=f"Downloading AgentsServer {version}.",
        )
        archive = manifest.get("archive") if isinstance(manifest, dict) else None
        if not isinstance(archive, dict):
            raise ReleaseVerificationError("release manifest is missing archive metadata")
        archive_bytes = download_release(str(archive["url"]), MAX_ARCHIVE_BYTES, timeout=120.0)
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if digest != str(archive.get("sha256") or "").lower():
            raise ReleaseVerificationError(
                "release archive checksum does not match the signed manifest"
            )
        update_status(status_path, phase="verifying", message="Signature and archive checksum verified.")
        staged_dir = stage_release(
            status_path=status_path,
            manifest=manifest,
            archive_bytes=archive_bytes,
            staging_root=staging_root,
        )
        token = consume_auth_token_file(getattr(args, "auth_token_file", None))
        try:
            assert_server_idle(int(args.port), token=token or None)
        except Exception as exc:
            raise WindowsUpdateError(
                f"could not verify that AgentsServer is idle before the switch: {exc}"
            ) from exc
        health_token_file: Path | None = None
        if token:
            health_token_file = status_path.with_name(
                f".winupdate-finish-{os.getpid()}-{secrets.token_hex(4)}.auth.json"
            )
            _native_atomic_json(health_token_file, {"token": token})
        update_status(
            status_path,
            phase="awaiting_server_exit",
            target_version=version,
            message="Release staged and verified; waiting for the server to hand off.",
        )
        finish_argv = build_finish_argv(
            args,
            staged_dir=staged_dir,
            server_pid=server_pid,
            health_token_file=health_token_file,
        )
        process = _spawn_detached(
            finish_argv,
            cwd=staging_root,
            log_path=status_path.with_name("server-update.log"),
        )
        update_status(
            status_path,
            phase="awaiting_server_exit",
            update_pid=process.pid,
            message=f"Finish stage detached (pid {process.pid}); server may exit.",
        )
        return 0
    except Exception as exc:
        update_status(status_path, phase="failed", message=str(exc), finished_at=utc_now())
        return 1


def run_finish(args: argparse.Namespace) -> int:
    """Wait for the old server to release the tree, then switch and restart."""

    status_path = Path(args.status_file).expanduser().resolve()
    live_dir = Path(args.live_dir).expanduser().resolve()
    staged_dir = Path(args.staged_dir).expanduser().resolve()
    state_dir = Path(args.state_dir).expanduser().resolve()
    health_token_file = getattr(args, "health_token_file", None)
    token = ""
    try:
        if health_token_file:
            token = _read_token_file(Path(health_token_file))
        deadline = time.monotonic() + float(
            getattr(args, "exit_timeout", None) or SERVER_EXIT_TIMEOUT_SECONDS
        )
        update_status(
            status_path,
            phase="awaiting_server_exit",
            message="Waiting for the previous server to exit.",
        )
        last_heartbeat = 0.0
        while True:
            pid_dead = not process_id_alive(args.server_pid)
            health_down = not poll_health_ok(int(args.port), token=token or None)
            if pid_dead and health_down:
                break
            if time.monotonic() >= deadline:
                update_status(
                    status_path,
                    phase="failed",
                    message=(
                        "the previous server did not exit before the handoff "
                        "deadline; aborting without changing the live tree"
                    ),
                    finished_at=utc_now(),
                )
                return 1
            if time.monotonic() - last_heartbeat >= 15.0:
                reason = "pid still alive" if not pid_dead else "health endpoint still answering"
                update_status(
                    status_path,
                    phase="awaiting_server_exit",
                    message=f"Still waiting for the previous server to exit ({reason}).",
                )
                last_heartbeat = time.monotonic()
            time.sleep(SERVER_EXIT_POLL_SECONDS)

        acquire_switch_lock(state_dir)
        spawned: list[subprocess.Popen] = []
        try:
            def start_server() -> None:
                spawned.append(
                    spawn_server_detached(
                        live_dir,
                        port=int(args.port),
                        bind=args.bind,
                        state_dir=state_dir,
                        status_path=status_path,
                    )
                )

            def stop_server() -> None:
                terminate_processes(spawned)

            result = switch_with_rollback(
                staged_dir=staged_dir,
                live_dir=live_dir,
                stop_server=stop_server,
                start_server=start_server,
                health_check=lambda: poll_health_ok(int(args.port), token=token or None),
                settle_seconds=FINISH_HEALTH_SETTLE_SECONDS,
                attempts=FINISH_HEALTH_ATTEMPTS,
                status_path=status_path,
            )
            return 0 if result.switched else 1
        finally:
            release_switch_lock(state_dir)
    except Exception as exc:
        update_status(status_path, phase="failed", message=str(exc), finished_at=utc_now())
        return 1
    finally:
        if health_token_file:
            with suppress(FileNotFoundError):
                Path(health_token_file).unlink()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Windows-native AgentsServer release updater (no install.sh)"
    )
    parser.add_argument("--mode", choices=["drive", "finish"], default="drive")
    parser.add_argument("--status-file", required=True)
    parser.add_argument("--public-key", required=True)
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--live-dir", default=str(default_live_dir()))
    parser.add_argument("--staging-root", default="")
    parser.add_argument("--state-dir", default=str(default_state_dir()))
    parser.add_argument("--expected-version")
    parser.add_argument("--current-version")
    parser.add_argument("--track", choices=sorted(RELEASE_TRACKS), default="stable")
    parser.add_argument("--auth-token-file")
    # finish stage only
    parser.add_argument("--staged-dir")
    parser.add_argument("--server-pid", type=int)
    parser.add_argument("--health-token-file")
    parser.add_argument("--exit-timeout", type=float, default=SERVER_EXIT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.staging_root:
        args.staging_root = str(default_staging_root(args.state_dir))
    if args.mode == "finish":
        if not args.staged_dir or args.server_pid is None:
            parser.error("--mode finish requires --staged-dir and --server-pid")
        return run_finish(args)
    return run_drive(args)


if __name__ == "__main__":
    raise SystemExit(main())
