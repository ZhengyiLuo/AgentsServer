"""Settle the exact admitted update after independent native recovery.

This module and its dependencies use only the standard library: the durable
recovery job runs independently of the candidate virtual environment. A terminal
installer receipt, not a matching version string, authorizes settlement.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any

import execution_install as files
from execution_preparation import _check_binding


ACTIVE = {"starting", "checking", "downloading", "verifying", "installing", "restarting"}
INTENT_KEYS = {"format", "root", "root_binding", "candidate_binding", "version",
               "api_contract", "update_id", "server_identity"}


def _read(path: Path) -> dict:
    raw, _mode = files._read_file(path, private=True)
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate update recovery status field")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("update recovery status is not an object")
    return value


@contextmanager
def _status_lock(path: Path):
    files._path(path)
    files._owned_directory(path.parent)
    lock = path.with_name(f".{path.name}.lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("update recovery status lock is unsafe")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        current = lock.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError("update recovery status lock changed")
        yield
    finally:
        os.close(descriptor)


def _intent_digest(intent: dict) -> str:
    return hashlib.sha256(files._json_bytes(intent)).hexdigest()


def capture_status_binding(root: Path, context: dict, *, supplied_update_id: str,
                           expected_server_identity: str) -> dict | None:
    """Bind old-runner status only through its already verified recovery intent."""
    root = files._path(root)
    state = Path(context["execution"]["runtime_dir"]).parent
    path = files._path(state / "admin/server-update.json")
    if not path.exists() and not path.is_symlink():
        if supplied_update_id:
            raise RuntimeError("admitted update status is missing")
        return None
    with _status_lock(path):
        status = _read(path)
        intent = status.get("_activation_recovery")
        if not supplied_update_id and status.get("phase") not in ACTIVE:
            return None  # historical receipt during an explicit manual install
        if intent is None and not supplied_update_id:
            raise RuntimeError("active update has no bound native recovery intent")
        if (not isinstance(intent, dict) or set(intent) != INTENT_KEYS
                or type(intent["format"]) is not int or intent["format"] != 1
                or intent["root"] != str(root)
                or intent["server_identity"] != expected_server_identity
                or not expected_server_identity
                or re.fullmatch(r"[0-9a-f]{32}", str(intent["update_id"])) is None
                or intent["update_id"] != status.get("update_id")
                or (supplied_update_id and supplied_update_id != intent["update_id"])
                or status.get("phase") not in ACTIVE
                or intent["version"] != status.get("target_version")
                or intent["version"] != context["release_version"]
                or type(intent["api_contract"]) is not int or intent["api_contract"] < 1
                or intent["api_contract"] != context["execution"]["api_contract"]):
            raise RuntimeError("native recovery does not own the admitted update")
        _check_binding(root, intent["root_binding"])
        saved, candidate = intent["candidate_binding"], context["candidate_release"]
        if (not isinstance(saved, dict) or set(saved) != {"device", "inode", "volume_uuid"}
                or any(type(saved[name]) is not int or saved[name] < 1 for name in ("device", "inode"))
                or saved["inode"] != candidate["inode"]):
            raise RuntimeError("native recovery candidate differs from the admitted update")
        if sys.platform == "darwin":
            if not saved["volume_uuid"] or saved["volume_uuid"] != intent["root_binding"]["volume_uuid"]:
                raise RuntimeError("native recovery candidate volume changed")
        elif saved["volume_uuid"] is not None or saved["device"] != candidate["device"]:
            raise RuntimeError("native recovery candidate device changed")
        if context["execution"]["handoff"] is not None and context["execution"]["handoff"] != status.get("_execution_handoff"):
            raise RuntimeError("native recovery handoff differs from the admitted update")
        return {"format": 1, "path": str(path), "update_id": intent["update_id"],
                "target_version": intent["version"], "intent_sha256": _intent_digest(intent)}


def _live_runner(status: dict, update_id: str) -> bool:
    pid = status.get("runner_pid")
    if pid is None:
        # A newly launched recovery runner may not have published its PID yet.
        try:
            updated = datetime.fromisoformat(str(status["updated_at"]).replace("Z", "+00:00"))
            return 0 <= (datetime.now(timezone.utc) - updated).total_seconds() < 45
        except (KeyError, ValueError, TypeError):
            return False
    if type(pid) is not int or pid <= 1:
        raise RuntimeError("saved update runner identity is invalid")
    result = subprocess.run(["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
                            capture_output=True, text=True, timeout=5, check=False)
    if result.returncode not in {0, 1}:
        raise RuntimeError("cannot inspect the admitted update runner")
    command = result.stdout.strip()
    return bool("update_runner.py" in command and re.search(
        rf"(?:^|\s)--update-id\s+{re.escape(update_id)}(?:\s|$)", command))


def settle_status(owner: dict, terminal: dict) -> bool:
    """Return False while a live runner owns settlement; never replace a new row.

    The caller has validated native terminal health and finalization under the
    installation lock. This adds status ownership, not another recovery path.
    """
    binding = owner.get("status_binding")
    if binding is None:
        return True
    if (not isinstance(binding, dict)
            or set(binding) != {"format", "path", "update_id", "target_version", "intent_sha256"}
            or type(binding["format"]) is not int or binding["format"] != 1
            or binding["target_version"] != owner["version"]
            or re.fullmatch(r"[0-9a-f]{32}", str(binding["update_id"])) is None
            or re.fullmatch(r"[0-9a-f]{64}", str(binding["intent_sha256"])) is None):
        raise RuntimeError("native recovery status binding is invalid")
    path = files._path(binding["path"])
    if path != Path(owner["state_root"]) / "admin/server-update.json":
        raise RuntimeError("native recovery status path changed")
    if (not isinstance(terminal, dict) or type(terminal.get("format")) is not int
            or terminal["format"] != 1
            or terminal.get("transaction_id") != owner["transaction_id"]
            or not isinstance(terminal.get("snapshot"), dict)
            or terminal.get("phase") not in {"committed", "rollback-healthy"}):
        raise RuntimeError("native recovery terminal proof is invalid")
    health = terminal["snapshot"].get("health")
    if not isinstance(health, dict) or health.get("server_identity") != owner["expected_server_identity"]:
        raise RuntimeError("native recovery terminal identity changed")
    completed = terminal["phase"] == "committed"
    if completed and (health.get("server_version") != owner["version"]
            or health.get("api_contract_version") != owner["api_contract"]
            or health.get("worker_version") != owner["version"]
            or health.get("gateway_version") != owner["version"]):
        raise RuntimeError("native recovery completion is not the paired target")
    with _status_lock(path):
        current = _read(path)
        if current.get("update_id") != binding["update_id"] or current.get("phase") not in ACTIVE:
            return True  # another update, or the original runner already settled
        if (current.get("target_version") != binding["target_version"]
                or _intent_digest(current.get("_activation_recovery")) != binding["intent_sha256"]):
            raise RuntimeError("admitted update changed before recovery settlement")
        if any((Path(owner["root"]) / name).exists() or (Path(owner["root"]) / name).is_symlink()
               for name in (".activation-transaction", ".execution-transaction", ".execution-uninstall.json")):
            raise RuntimeError("lifecycle transaction still owns recovery settlement")
        if _live_runner(current, binding["update_id"]):
            return False
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        current.update(phase="complete" if completed else "failed", runner_pid=None,
            heartbeat_at=None, elapsed_seconds=None, updated_at=now, finished_at=now,
            error_code=None if completed else "server_update_rolled_back",
            error_action=None if completed else "Retry the bundled update when ready.",
            retryable=None if completed else True,
            message=(f"AgentsServer {owner['version']} recovered and is healthy." if completed else
                     "The interrupted update was rolled back to the verified previous installation."))
        if completed:
            current.update(installed_version=owner["version"], update_available=False)
        files._atomic_write(path, files._json_bytes(current))
        return True
