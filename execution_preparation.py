"""Read-only activation proof for a completely prepared, inactive release.

The caller verifies the signed archive and supplies its digest plus a source
inventory captured from that verified archive *before* dependency installation.
This receipt is local staging evidence, not a replacement for signature checks.
Writing it never changes current, configuration, service jobs, or chat state.
Validate again while holding the installation lock immediately before activation.
Use ``python -B`` when executing a helper inside the candidate release.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import sys
from typing import Any

from activation_transaction import _volume_uuid


FORMAT = 1
MAX_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TREE_BYTES = 8 * 1024 * 1024 * 1024
MAX_ENTRIES = 250_000
_DIGEST = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-beta\.[0-9]+)?")
_REQUIRED = {"VERSION", "agent_server.py", "execution_service.py", "pyproject.toml", "uv.lock"}


def _path(value: str | Path) -> Path:
    if not isinstance(value, (str, Path)):
        raise ValueError("preparation path must be text")
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts or any(ord(c) < 32 for c in str(path))
            or path.resolve(strict=False) != path):
        raise ValueError("preparation path must be absolute without symlink ancestors")
    return path


def _owned_directory(path: Path, *, private: bool = False) -> os.stat_result:
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or mode & 0o022 or mode & 0o500 != 0o500 or (private and mode != 0o700)):
        raise PermissionError("preparation directory is not safely owned")
    return info


def _stable(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def _file(path: Path, *, private: bool = False, dependency: bool = False,
          external: bool = False, contents: bool = False,
          uv_lock: bool = False) -> tuple[dict, bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        mode = stat.S_IMODE(before.st_mode)
        maximum = MAX_RECEIPT_BYTES if private else MAX_FILE_BYTES
        owners = {os.getuid(), 0} if external else {os.getuid()}
        # Existing user-managed Python installations can inherit a group-write
        # umask. Bind their bytes and mode in the receipt without chmodding a
        # shared prerequisite. Private staging files retain their usual policy.
        writable_mask = 0o002 if external and dependency and before.st_uid == os.getuid() else 0o022
        if (not stat.S_ISREG(before.st_mode) or before.st_uid not in owners
                or (mode & writable_mask and not uv_lock) or mode & 0o7000 or (private and mode != 0o600)
                or (uv_lock and (before.st_size != 0 or before.st_nlink != 1))
                or (not dependency and before.st_nlink != 1) or before.st_size > maximum):
            raise PermissionError("preparation file is unsafe or too large")
        digest = hashlib.sha256()
        data = bytearray()
        size = 0
        while chunk := os.read(fd, 1024 * 1024):
            size += len(chunk)
            if size > maximum:
                raise ValueError("preparation file exceeds size limit")
            digest.update(chunk)
            if contents:
                data.extend(chunk)
        if (_stable(before) != _stable(os.fstat(fd)) or _stable(before) != _stable(path.lstat())
                or size != before.st_size):
            raise RuntimeError("preparation file changed while reading")
        return {"kind": "file", "mode": mode, "size": size, "sha256": digest.hexdigest()}, bytes(data)
    finally:
        os.close(fd)


def _json(path: Path, *, private: bool = True) -> dict:
    _metadata, data = _file(path, private=private, contents=True)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate preparation JSON field")
            value[key] = item
        return value
    value = json.loads(data, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("preparation JSON must be an object")
    return value


def _source_inventory(value: Any) -> dict[str, dict]:
    if not isinstance(value, dict) or set(value) != {"format", "files"} or type(value["format"]) is not int or value["format"] != FORMAT:
        raise ValueError("invalid source inventory format")
    files = value["files"]
    if not isinstance(files, dict) or not _REQUIRED <= files.keys() or len(files) > MAX_ENTRIES:
        raise ValueError("source inventory is incomplete")
    for name, entry in files.items():
        if (not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
                or str(PurePosixPath(name)) != name or any(part in {"..", ".venv", "__pycache__"} for part in PurePosixPath(name).parts)
                or any(ord(c) < 32 for c in name) or "\\" in name):
            raise ValueError("unsafe source inventory member")
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size"}
                or not isinstance(entry["sha256"], str) or not _DIGEST.fullmatch(entry["sha256"])
                or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_FILE_BYTES):
            raise ValueError("invalid source inventory entry")
    return files


def _pins(version: str, api_contract: int, archive_sha256: str) -> None:
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError("invalid preparation version")
    if type(api_contract) is not int or api_contract < 1:
        raise ValueError("invalid preparation API contract")
    if not isinstance(archive_sha256, str) or not _DIGEST.fullmatch(archive_sha256):
        raise ValueError("invalid preparation archive digest")


def _binding(path: Path) -> dict:
    info = _owned_directory(path)
    return {"device": info.st_dev, "inode": info.st_ino,
            "volume_uuid": _volume_uuid(path, info) if sys.platform == "darwin" else None}


def _check_binding(path: Path, saved: Any) -> None:
    if (not isinstance(saved, dict) or set(saved) != {"device", "inode", "volume_uuid"}
            or any(type(saved[key]) is not int or saved[key] < 1 for key in ("device", "inode"))):
        raise ValueError("invalid preparation directory identity")
    current = _binding(path)
    if current["inode"] != saved["inode"]:
        raise RuntimeError("prepared directory identity changed")
    if sys.platform == "darwin":
        # st_dev changes across reboot/remount; the UUID and inode must not.
        if not saved["volume_uuid"] or saved["volume_uuid"] != current["volume_uuid"]:
            raise RuntimeError("prepared filesystem identity changed")
    elif saved["volume_uuid"] is not None or saved["device"] != current["device"]:
        raise RuntimeError("prepared filesystem identity changed")


def _candidate(root: Path, candidate: Path) -> None:
    _owned_directory(root)
    _owned_directory(root / "releases")
    if candidate.parent != root / "releases":
        raise ValueError("candidate must be a direct retained release")
    _owned_directory(candidate)


def _inventory_tree(candidate: Path, source: dict[str, dict]) -> dict[str, dict]:
    result: dict[str, dict] = {}
    total = 0
    parents = {parent.as_posix() for name in source for parent in PurePosixPath(name).parents if str(parent) != "."}

    def visit(directory: Path) -> None:
        nonlocal total
        before = _owned_directory(directory)
        for child in sorted(directory.iterdir()):
            relative = child.relative_to(candidate).as_posix()
            parts = PurePosixPath(relative).parts
            if any(ord(character) < 32 for character in relative):
                raise ValueError("prepared runtime member contains control characters")
            dependency = parts[0] == ".venv"
            cache = ((parts[-1] == "__pycache__" and (len(parts) == 1 or str(PurePosixPath(relative).parent) in parents))
                     or (len(parts) >= 2 and parts[-2] == "__pycache__" and child.suffix in {".pyc", ".pyo"}
                         and (len(parts) == 2 or str(PurePosixPath(relative).parent.parent) in parents)))
            if not dependency and not cache and relative not in source and relative not in parents:
                raise ValueError("unexpected prepared runtime member")
            info = child.lstat()
            if stat.S_ISLNK(info.st_mode):
                if not dependency or info.st_uid != os.getuid():
                    raise PermissionError("prepared source links are forbidden")
                target = os.readlink(child)
                resolved = child.resolve(strict=True)
                if resolved.is_dir():
                    if candidate not in resolved.parents:
                        raise ValueError("external prepared dependency directory link")
                    _owned_directory(resolved)
                    target_info = {"kind": "directory"}
                else:
                    if candidate not in resolved.parents:
                        # uv normally links only the Python interpreter out of
                        # a venv. Never scan arbitrary external files (including
                        # provider credentials) through a substituted link.
                        if (len(parts) != 3 or parts[:2] != (".venv", "bin")
                                or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", parts[2])
                                or not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)*)?", resolved.name)
                                or not os.access(resolved, os.X_OK)):
                            raise ValueError("external prepared dependency link is not an interpreter")
                    target_info, _data = _file(resolved, dependency=True, external=candidate not in resolved.parents)
                    total += target_info["size"]
                if _stable(info) != _stable(child.lstat()) or target != os.readlink(child):
                    raise RuntimeError("prepared dependency link changed")
                entry = {"kind": "symlink", "target": target, "resolved": str(resolved), "content": target_info}
            elif stat.S_ISDIR(info.st_mode):
                _owned_directory(child)
                entry = {"kind": "directory", "mode": stat.S_IMODE(info.st_mode)}
            else:
                # uv intentionally creates this empty advisory lock as0666,
                # even with a private staging parent. It contains no runtime
                # code; no other writable dependency receives this exception.
                entry, _data = _file(child, dependency=dependency, uv_lock=relative == ".venv/.lock")
                total += entry["size"]
            result[relative] = entry
            if len(result) > MAX_ENTRIES or total > MAX_TREE_BYTES:
                raise ValueError("prepared runtime inventory exceeds limit")
            if stat.S_ISDIR(info.st_mode):
                visit(child)
        if _stable(before) != _stable(directory.lstat()):
            raise RuntimeError("prepared directory changed while reading")

    visit(candidate)
    for name, expected in source.items():
        actual = result.get(name, {})
        if (actual.get("kind") != "file" or actual.get("sha256") != expected["sha256"]
                or actual.get("size") != expected["size"]):
            raise RuntimeError("prepared source differs from verified archive")
    python = candidate / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("prepared runtime interpreter is missing")
    return result


def _check_release(candidate: Path, version: str, api_contract: int) -> None:
    _metadata, data = _file(candidate / "VERSION", contents=True)
    if data.decode().strip() != version:
        raise ValueError("prepared runtime version differs from signed candidate")
    _metadata, data = _file(candidate / "agent_server.py", contents=True)
    contracts = re.findall(rb"^API_CONTRACT_VERSION\s*=\s*([0-9]+)\s*$", data, re.MULTILINE)
    if len(contracts) != 1 or int(contracts[0]) != api_contract:
        raise ValueError("prepared runtime API differs from signed candidate")


def _receipt_path(root: Path, receipt: Path, *, create_parent: bool = False) -> None:
    if receipt.parent not in {root, root / ".prepared-receipts"}:
        raise ValueError("preparation receipt must be inside its installation")
    if create_parent and not receipt.parent.exists():
        receipt.parent.mkdir(mode=0o700)
        _fsync(root)
    _owned_directory(receipt.parent, private=True)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_prepared(*, candidate: Path, root: Path, version: str, api_contract: int,
                   archive_sha256: str, inventory: Path, output: Path) -> dict:
    """Freeze verified source and the complete staged tree; never overwrite a receipt."""
    root, candidate, inventory, output = map(_path, (root, candidate, inventory, output))
    _pins(version, api_contract, archive_sha256)
    _candidate(root, candidate)
    source = _json(inventory)
    files = _source_inventory(source)
    identities = {"root": _binding(root), "releases": _binding(root / "releases"), "candidate": _binding(candidate)}
    tree = _inventory_tree(candidate, files)
    _check_release(candidate, version, api_contract)
    for name, path in (("root", root), ("releases", root / "releases"), ("candidate", candidate)):
        _check_binding(path, identities[name])
    value = {"format": FORMAT, "root": str(root), "candidate": str(candidate),
             "version": version, "api_contract": api_contract, "archive_sha256": archive_sha256,
             "identities": identities, "source_inventory": source, "runtime_inventory": tree}
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(data) > MAX_RECEIPT_BYTES:
        raise ValueError("prepared receipt exceeds size limit")
    _receipt_path(root, output, create_parent=True)
    temporary = output.parent / f".{output.name}.{secrets.token_hex(12)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output, follow_symlinks=False)
    finally:
        temporary.unlink()
        _fsync(output.parent)
    return value


def validate_prepared(*, root: Path, version: str, api_contract: int,
                      archive_sha256: str, receipt: Path) -> dict:
    """Validate all caller pins and staged bytes without modifying anything."""
    root, receipt = map(_path, (root, receipt))
    _pins(version, api_contract, archive_sha256)
    _owned_directory(root)
    _receipt_path(root, receipt)
    value = _json(receipt)
    fields = {"format", "root", "candidate", "version", "api_contract", "archive_sha256",
              "identities", "source_inventory", "runtime_inventory"}
    if (set(value) != fields or type(value["format"]) is not int or value["format"] != FORMAT
            or value["root"] != str(root) or value["version"] != version
            or type(value["api_contract"]) is not int or value["api_contract"] != api_contract
            or value["archive_sha256"] != archive_sha256):
        raise ValueError("preparation receipt does not match the signed candidate")
    candidate = _path(value["candidate"])
    _candidate(root, candidate)
    identities = value["identities"]
    if not isinstance(identities, dict) or set(identities) != {"root", "releases", "candidate"}:
        raise ValueError("invalid preparation directory identities")
    for name, path in (("root", root), ("releases", root / "releases"), ("candidate", candidate)):
        _check_binding(path, identities[name])
    source = _source_inventory(value["source_inventory"])
    if _inventory_tree(candidate, source) != value["runtime_inventory"]:
        raise RuntimeError("prepared runtime changed after preparation")
    _check_release(candidate, version, api_contract)
    for name, path in (("root", root), ("releases", root / "releases"), ("candidate", candidate)):
        _check_binding(path, identities[name])
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("write", "validate"):
        command = subparsers.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--version", required=True)
        command.add_argument("--api-contract", type=int, required=True)
        command.add_argument("--archive-sha256", required=True)
        if action == "write":
            command.add_argument("--candidate", type=Path, required=True)
            command.add_argument("--inventory", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
        else:
            command.add_argument("--receipt", type=Path, required=True)
    arguments = vars(parser.parse_args(argv))
    action = arguments.pop("action")
    try:
        if action == "write":
            write_prepared(**arguments)
            print(arguments["output"])
        else:
            print(validate_prepared(**arguments)["candidate"])
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        # Do not print input JSON, environment, file bytes, or subprocess logs.
        print(f"Preparation validation failed: {type(error).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
