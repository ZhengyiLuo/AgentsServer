"""One process owns a state directory across legacy and split entry points.

The descriptor stays open for the process lifetime, including bounded shutdown
stragglers. Versions released before this lease existed do not participate;
initial migration must separately prove that the old service has stopped.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import threading


@dataclass(frozen=True)
class StateOwnership:
    path: Path
    descriptor: int
    pid: int
    identity: tuple[int, int]


_OWNERS: dict[Path, StateOwnership] = {}
_LOCK = threading.Lock()


def _owned_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise RuntimeError("State ownership directory must be owned and not writable by other users")


def _validate_file(info: os.stat_result) -> tuple[int, int]:
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise RuntimeError("State ownership lock must be an owned 0600 regular file")
    return info.st_dev, info.st_ino


def acquire_state_ownership(state_directory: Path) -> StateOwnership:
    """Acquire before loading state/providers; repeated acquisition is harmless."""
    root = Path(state_directory).expanduser().resolve()
    path = root / "admin" / "state-owner.lock"
    with _LOCK:
        existing = _OWNERS.get(path)
        if existing is not None:
            if existing.pid != os.getpid():
                raise RuntimeError("A forked process cannot reuse its parent's state ownership")
            if (_validate_file(path.lstat()) != existing.identity
                    or _validate_file(os.fstat(existing.descriptor)) != existing.identity):
                raise RuntimeError("State ownership lock changed while held")
            return existing
        _owned_directory(root)
        _owned_directory(path.parent)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            identity = _validate_file(os.fstat(descriptor))
            os.set_inheritable(descriptor, False)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("Another AgentsServer process already owns this state directory") from error
            if _validate_file(path.lstat()) != identity:
                raise RuntimeError("State ownership lock changed during acquisition")
            owner = StateOwnership(path, descriptor, os.getpid(), identity)
            _OWNERS[path] = owner
            return owner
        except BaseException:
            os.close(descriptor)
            raise


def _release_state_ownership_for_tests(owner: StateOwnership) -> None:
    """Isolated unit-test cleanup only; production must retain its descriptor."""
    with _LOCK:
        if owner.pid != os.getpid() or _OWNERS.get(owner.path) is not owner:
            raise RuntimeError("Test cannot release another process's state ownership")
        del _OWNERS[owner.path]
        os.close(owner.descriptor)
