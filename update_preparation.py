"""Detached, resumable signed-update preparation while agent admission stays open.

This process may download and build a private candidate, never activate it.
The server's existing pending-update waiter owns the later idle handoff.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from typing import Any

import update_runner as updates


class PreparationSuperseded(RuntimeError):
    pass


def _owned_directory(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError("Preparation paths must be absolute and contain no links")
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise ValueError("Preparation directory is not safely owned")
    return path


def preparation_directory(root: Path, preparation_id: str, *, create: bool = False) -> Path:
    if re.fullmatch(r"[0-9a-f]{32}", preparation_id) is None:
        raise ValueError("Invalid preparation identity")
    _owned_directory(root)
    parent = _owned_directory(root / ".update-preparations", create=create)
    return _owned_directory(parent / preparation_id, create=create)


@contextmanager
def preparation_lease(directory: Path, *, blocking: bool = False):
    path = directory / "runner.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("Preparation lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Preparation lock changed")
        yield descriptor
    finally:
        os.close(descriptor)


def preparation_is_active(root: Path, preparation_id: str) -> bool:
    try:
        directory = preparation_directory(root, preparation_id)
    except FileNotFoundError:
        return False
    try:
        with preparation_lease(directory):
            return False
    except BlockingIOError:
        return True


def update_pending(status_path: Path, preparation_id: str, **changes: Any) -> dict[str, Any]:
    with updates.server_update_status_lock(status_path):
        current = updates._read_status_unlocked(status_path)
        if current.get("phase") != "pending" or current.get("preparation_id") != preparation_id:
            raise PreparationSuperseded("Preparation no longer owns the pending update")
        return updates._update_status_unlocked(status_path, current,
            preparation_heartbeat_at=updates.utc_now(), **changes)


def receipt_path(root: Path, preparation_id: str) -> Path:
    if re.fullmatch(r"[0-9a-f]{32}", preparation_id) is None:
        raise ValueError("Invalid preparation identity")
    return root / ".prepared-receipts" / f"{preparation_id}.json"


def receipt_digest(path: Path) -> str:
    from execution_preparation import MAX_RECEIPT_BYTES
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size > MAX_RECEIPT_BYTES):
            raise ValueError("Prepared candidate receipt is unsafe")
        digest = hashlib.sha256()
        length = 0
        while True:
            data = os.read(descriptor, min(1024 * 1024, MAX_RECEIPT_BYTES + 1 - length))
            if not data:
                break
            length += len(data)
            if length > MAX_RECEIPT_BYTES:
                raise ValueError("Prepared candidate receipt exceeds its limit")
            digest.update(data)
        current = path.lstat()
        after = os.fstat(descriptor)
        if (length != info.st_size or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (info.st_size, info.st_mtime_ns, info.st_ctime_ns)):
            raise ValueError("Prepared candidate receipt changed while reading")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def verify_prepared_status(status: dict[str, Any], *, root: Path, public_key: Path) -> dict[str, Any]:
    prepared = status.get("_prepared_update")
    if not isinstance(prepared, dict) or prepared.get("preparation_id") != status.get("preparation_id"):
        raise ValueError("Prepared update is not bound to its pending intent")
    envelope = prepared.get("envelope")
    if (not isinstance(envelope, dict) or set(envelope) != {"manifest_base64", "signature_base64"}
            or not all(isinstance(value, str) for value in envelope.values())
            or len(envelope["manifest_base64"]) > updates.MAX_METADATA_BYTES * 2
            or len(envelope["signature_base64"]) != 88):
        raise ValueError("Prepared update signed envelope is invalid")
    manifest = updates.verify_manifest(
        base64.b64decode(envelope["manifest_base64"], validate=True),
        base64.b64decode(envelope["signature_base64"], validate=True), public_key,
        expected_version=status["target_version"], track=status["track"], allow_npm=True)
    preparation_directory(root, status["preparation_id"])
    if prepared.get("mode") == "legacy":
        return {"manifest": manifest, "candidate": None, "receipt": None}
    if prepared.get("mode") != "prepared":
        raise ValueError("Unknown prepared update mode")
    receipt = receipt_path(root, status["preparation_id"])
    if receipt_digest(receipt) != prepared.get("receipt_sha256"):
        raise ValueError("Prepared candidate receipt changed")
    from execution_preparation import validate_prepared
    validated = validate_prepared(root=root, version=manifest["version"],
        api_contract=int(manifest["api_contract_version"]), archive_sha256=manifest["archive"]["sha256"], receipt=receipt)
    return {"manifest": manifest, "candidate": str(validated["candidate"]), "receipt": str(receipt)}


def _signed_envelope(status: dict[str, Any], key: Path) -> tuple[dict[str, str], dict[str, Any]]:
    envelope = status.get("_npm_release")
    if envelope is not None:
        manifest = updates.verify_npm_release_envelope(envelope, key, expected_version=status["target_version"])
        if manifest["track"] != status["track"]:
            raise ValueError("Preparation release track changed")
        return envelope, manifest
    version = status["target_version"]
    document = updates.download_bytes(updates.release_manifest_url(version), updates.MAX_METADATA_BYTES)
    signature = updates.download_bytes(updates.release_signature_url(version), updates.MAX_METADATA_BYTES)
    manifest = updates.verify_manifest(document, signature, key, expected_version=version, track=status["track"])
    return {"manifest_base64": base64.b64encode(document).decode(),
            "signature_base64": base64.b64encode(signature).decode()}, manifest


def run_preparer(command: list[str], *, source: Path, directory: Path,
                 status_path: Path, preparation_id: str, lease_descriptor: int) -> None:
    log = directory / "prepare.log"
    descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        os.close(descriptor)
        raise ValueError("Preparation log is unsafe")
    with os.fdopen(descriptor, "ab") as output:
        process = subprocess.Popen(command, cwd=source, env=updates.installer_environment(),
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            # Keep the preparation lease with the actual staging shell if its
            # observer dies. A resumed observer waits, then consumes its receipt.
            pass_fds=(lease_descriptor,))
        deadline = time.monotonic() + updates.INSTALLER_TIMEOUT_SECONDS
        try:
            while process.poll() is None:
                update_pending(status_path, preparation_id, preparation_runner_pid=os.getpid())
                if time.monotonic() >= deadline:
                    raise RuntimeError("Candidate dependency preparation timed out")
                time.sleep(0.5)
            if process.returncode:
                raise RuntimeError("Candidate preparation failed; inspect the preparation log")
        finally:
            if process.poll() is None:
                # Prepare-only cannot activate a service. Still allow its own
                # cleanup to finish; never kill a process outside this owner.
                updates.terminate_installer(process)


def run_preparation(*, status_path: Path, root: Path, public_key: Path, preparation_id: str) -> None:
    directory = preparation_directory(root, preparation_id, create=True)
    with preparation_lease(directory) as lease_descriptor:
        status = update_pending(status_path, preparation_id, preparation_phase="checking",
            preparation_runner_pid=os.getpid(), message="Preparing the signed server update; agents can keep working.")
        envelope, manifest = _signed_envelope(status, public_key)
        receipt = receipt_path(root, preparation_id)
        if receipt.exists() or receipt.is_symlink():
            # A killed runner may have finished staging before persisting ready.
            prepared = {"mode": "prepared", "preparation_id": preparation_id, "envelope": envelope,
                        "receipt_sha256": receipt_digest(receipt)}
            verify_prepared_status({**status, "_prepared_update": prepared}, root=root, public_key=public_key)
        else:
            update_pending(status_path, preparation_id, preparation_phase="downloading")
            data = (updates.download_npm_archive(manifest) if manifest.get("schema") == 2
                    else updates.download_bytes(manifest["archive"]["url"], updates.MAX_ARCHIVE_BYTES, timeout=120))
            if hashlib.sha256(data).hexdigest() != manifest["archive"]["sha256"]:
                raise ValueError("Preparation archive does not match its signed hash")
            with tempfile.TemporaryDirectory(prefix="source-", dir=directory) as temporary:
                work = Path(temporary)
                archive = work / "release.tar.gz"
                archive.write_bytes(data)
                source = updates.safe_extract(archive, work / "extracted",
                    npm_manifest=manifest if manifest.get("schema") == 2 else None)
                instance_arguments = updates.instance_installer_arguments(source)
                if instance_arguments or not (source / "execution_preparation.py").is_file():
                    # Named instances use their isolated legacy service, not the
                    # default-only split gateway preparation/activation protocol.
                    # Older signed releases retain their established installer.
                    prepared = {"mode": "legacy", "preparation_id": preparation_id, "envelope": envelope}
                else:
                    update_pending(status_path, preparation_id, preparation_phase="staging")
                    command = ["/bin/bash", str(source / "install.sh"), "--non-interactive", "--prepare-only",
                        "--prepared-receipt", str(receipt), "--release-version", manifest["version"],
                        "--expected-api-contract", str(manifest["api_contract_version"]),
                        "--prepared-archive-sha256", manifest["archive"]["sha256"]]
                    run_preparer(command, source=source, directory=directory,
                                 status_path=status_path, preparation_id=preparation_id,
                                 lease_descriptor=lease_descriptor)
                    prepared = {"mode": "prepared", "preparation_id": preparation_id, "envelope": envelope,
                                "receipt_sha256": receipt_digest(receipt)}
                    verify_prepared_status({**status, "_prepared_update": prepared}, root=root, public_key=public_key)
        update_pending(status_path, preparation_id, preparation_phase="ready", _prepared_update=prepared,
            preparation_runner_pid=None, message="Server update prepared; it will activate when current work finishes.")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--public-key", type=Path, required=True)
    parser.add_argument("--preparation-id", required=True)
    args = parser.parse_args()
    try:
        run_preparation(status_path=args.status_file, root=args.install_root,
                        public_key=args.public_key, preparation_id=args.preparation_id)
        return 0
    except (PreparationSuperseded, BlockingIOError):
        return 0
    except Exception:
        try:
            update_pending(args.status_file, args.preparation_id, phase="failed", preparation_runner_pid=None,
                message="Server update preparation failed; running agents were left untouched.",
                error_code="server_update_preparation_failed", error_action="Inspect the preparation log and retry the update.",
                retryable=True, finished_at=updates.utc_now())
        except PreparationSuperseded:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
