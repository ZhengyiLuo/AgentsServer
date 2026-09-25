"""Flush retained executable generations before an admitted native service stop.

This is a durability barrier, not an alternate integrity or admission proof.
The caller owns the install lock and supplies an already validated outer journal.
No bytes, modes, links, configuration or service state are changed here.
"""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import re
import stat
import sys

import activation_transaction as activation

MAX_ENTRIES = 250_000
MAX_BYTES = 16 * 1024 * 1024 * 1024


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _file_barrier(descriptor: int) -> None:
    os.fsync(descriptor)


def _full_barrier(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RuntimeError("durability anchor is not a regular file")
        os.fsync(descriptor)
        if sys.platform == "darwin":
            # fsync alone need not drain a device's volatile write cache on macOS.
            fcntl.fcntl(descriptor, getattr(fcntl, "F_FULLFSYNC", 51))
    finally:
        os.close(descriptor)


def flush_tree(root: Path, *, dependency_prefix: bool = False) -> dict[str, int]:
    """Flush one owned tree without following directory links or special files."""
    root = Path(root)
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError("durability tree must have no linked ancestors")
    root_info = root.lstat()
    # uv can retain a same-user runtime with 0775 directories and 0664 files.
    # Flushing that admitted dependency must not change its permission policy.
    writable_mask = 0o002 if dependency_prefix else 0o022
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
            or root_info.st_mode & writable_mask):
        raise PermissionError("durability tree root is unsafe")
    total = {"entries": 0, "bytes": 0}
    external: set[Path] = set()
    observed = {}

    def visit(directory: Path, parent_fd: int | None = None) -> None:
        before = directory.lstat()
        descriptor = os.open(directory if parent_fd is None else directory.name,
                             os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            if (_identity(before) != _identity(os.fstat(descriptor))
                    or before.st_uid != os.getuid() or before.st_dev != root_info.st_dev):
                raise PermissionError("durability directory ownership or device changed")
            names = sorted(os.listdir(descriptor))
            for name in names:
                if any(ord(character) < 32 for character in name):
                    raise ValueError("durability member contains control characters")
                child = directory / name
                info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if info.st_uid != os.getuid() or info.st_dev != root_info.st_dev:
                    raise PermissionError("durability member ownership or device changed")
                if dependency_prefix and not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o002:
                    raise PermissionError("standalone Python durability member is writable by others")
                total["entries"] += 1
                if total["entries"] > MAX_ENTRIES:
                    raise ValueError("durability tree exceeds entry limit")
                if stat.S_ISDIR(info.st_mode):
                    visit(child, descriptor)
                elif stat.S_ISREG(info.st_mode):
                    total["bytes"] += info.st_size
                    if total["bytes"] > MAX_BYTES:
                        raise ValueError("durability tree exceeds byte limit")
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
                    try:
                        if _identity(info) != _identity(os.fstat(fd)):
                            raise RuntimeError("durability file changed before flush")
                        _file_barrier(fd)
                        if _identity(info) != _identity(os.fstat(fd)):
                            raise RuntimeError("durability file changed while flushing")
                    finally:
                        os.close(fd)
                elif stat.S_ISLNK(info.st_mode):
                    target = os.readlink(name, dir_fd=descriptor)
                    resolved = child.resolve(strict=True)
                    if root not in resolved.parents:
                        relative = child.relative_to(root).parts
                        if (dependency_prefix or len(relative) != 3 or relative[:2] != (".venv", "bin")
                                or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", relative[2])
                                or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", resolved.name)):
                            raise ValueError("external durability link is not a Python interpreter")
                        external.add(resolved)
                    if target != os.readlink(name, dir_fd=descriptor):
                        raise RuntimeError("durability link changed while flushing")
                else:
                    raise ValueError("durability tree contains a special file")
                if _identity(info) != _identity(os.stat(name, dir_fd=descriptor, follow_symlinks=False)):
                    raise RuntimeError("durability member changed while flushing")
                observed[child] = _identity(info)
            if names != sorted(os.listdir(descriptor)) or _identity(before) != _identity(directory.lstat()):
                raise RuntimeError("durability directory changed while flushing")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    visit(root)
    for interpreter in sorted(external):
        info = interpreter.lstat()
        prefix = interpreter.parent.parent
        match = re.fullmatch(r"python([0-9]+\.[0-9]+)", interpreter.name)
        standalone = re.fullmatch(r"cpython-[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.]*)?-(?:macos|linux)-[a-z0-9_]+-[a-z0-9_]+", prefix.name)
        writable_mask = 0o002 if info.st_uid == os.getuid() else 0o022
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or info.st_mode & writable_mask or not info.st_mode & 0o111):
            raise PermissionError("external durability interpreter is unsafe")
        if info.st_uid == 0:
            # System interpreters are provisioned outside this updater. Never
            # traverse /usr or another OS-managed Python installation.
            continue
        if standalone:
            if (interpreter.parent.name != "bin" or match is None
                    or not (prefix / "lib" / ("python" + match[1])).is_dir()
                    or not (prefix / "BUILD").is_file() or (prefix / "BUILD").is_symlink()):
                raise ValueError("owned uv Python has no bounded standalone runtime")
            flush_tree(prefix, dependency_prefix=True)
            parent_fd = os.open(prefix.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        # Other selected interpreters (for example Homebrew) are preexisting
        # prerequisites. Do not infer and recursively scan an arbitrary prefix.
        _full_barrier(interpreter)
        if _identity(info) != _identity(interpreter.lstat()):
            raise RuntimeError("external durability interpreter changed")
    for path, identity in observed.items():
        if _identity(path.lstat()) != identity:
            raise RuntimeError("durability tree changed after flush")
    if _identity(root.lstat()) != _identity(root_info):
        raise RuntimeError("durability root changed after flush")
    return total


def flush_activation(root: Path, value: dict) -> None:
    root_info = root.lstat()
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
            or stat.S_IMODE(root_info.st_mode) != 0o700):
        raise PermissionError("activation durability requires a private install root")
    if value.get("phase") not in {"prepared", "guarded"}:
        raise RuntimeError("runtime durability must precede native quiescing")
    releases = [value["candidate_release"], value["old_release"], value["execution"]["old_worker_release"]]
    seen: set[Path] = set()
    for release in releases:
        if not release:
            continue
        path = activation._locate_release(release)
        if path is None:
            raise RuntimeError("durability runtime is not retained by the journal")
        if path in seen:
            continue
        seen.add(path)
        flush_tree(path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _full_barrier(path / "VERSION")
        if not activation._release_matches(path, release):
            raise RuntimeError("durability runtime identity changed")
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
