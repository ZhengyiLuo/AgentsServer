"""Release only an unchanged, idle split-worker handoff before installer entry.

The detached update runner first settles its own failed status and Hub fence.
Once it invokes the installer, the installer's activation journals exclusively
own recovery. This helper never stops processes, changes update status, deletes
evidence, or guesses that a legacy monolith owns a split-worker lease.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import stat
from typing import Any
import uuid

import execution_install as files
from execution_manage import InstallationLock, NativeServices, WorkerControl


def _no_activation(root: Path) -> None:
    for name in (".activation-transaction", ".execution-transaction"):
        path = root / name
        if path.exists() or path.is_symlink():
            raise RuntimeError("activation already owns handoff recovery")


def _no_handoff_recovery_overlap(root: Path) -> None:
    _no_activation(root)
    marker = root / ".execution-uninstall.json"
    if marker.exists() or marker.is_symlink():
        raise RuntimeError("uninstall already owns execution recovery")


def _read_handoff(root: Path, path: Path) -> dict[str, Any]:
    path = files._path(path)
    if (path.parent.parent != root / ".update-preparations"
            or re.fullmatch(r"[0-9a-f]{32}", path.parent.name) is None
            or re.fullmatch(r"handoff-[0-9a-f]{32}\.json", path.name) is None):
        raise ValueError("handoff is outside its owned preparation directory")
    files._owned_directory(path.parent.parent, private=True)
    files._owned_directory(path.parent, private=True)
    data, _mode = files._read_file(path, private=True)

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate handoff field")
            value[key] = item
        return value

    value = json.loads(data, object_pairs_hook=unique)
    expected = {"schema", "operation_id", "worker_instance_id", "lease_id", "expected_server_identity"}
    if (not isinstance(value, dict) or set(value) != expected
            or type(value["schema"]) is not int or value["schema"] != 1):
        raise ValueError("invalid handoff schema")
    for key in ("operation_id", "lease_id"):
        if not isinstance(value[key], str) or str(uuid.UUID(value[key])) != value[key]:
            raise ValueError("invalid handoff operation or lease")
    for key in ("worker_instance_id", "expected_server_identity"):
        if (not isinstance(value[key], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value[key])):
            raise ValueError("invalid handoff worker or server identity")
    return value


def _installed_layout(root: Path) -> files.ExecutionLayout:
    data, _mode = files._read_file(root / files.LAYOUT_NAME, private=True)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("installed execution layout is invalid")
    layout = files.ExecutionLayout.from_dict(value.get("layout"))
    if layout.install_root != root or value != files.layout_manifest(layout):
        raise ValueError("installed execution layout changed")
    return layout


def _matching_status(handoff: dict, layout: files.ExecutionLayout, record: dict,
                     status: dict, native: dict, *, allow_released: bool = False) -> bool:
    lease = status.get("lease")
    blockers = status.get("blockers")
    unheld = allow_released and "lease" in status and lease is None
    if ((not unheld and record.get("instance_id") != handoff["worker_instance_id"])
            or status.get("worker_instance_id") != record.get("instance_id")
            or record.get("release_root") != str(layout.worker_release)
            or native.get("worker", {}).get("state") != "running"
            or type(record.get("pid")) is not int or record["pid"] <= 0
            or native["worker"].get("pid") != record["pid"]):
        raise RuntimeError("native worker no longer owns the handoff")
    if (not isinstance(blockers, dict) or not blockers
            or any(type(value) is not int or value < 0 for value in blockers.values())):
        raise RuntimeError("worker cleanup or admission has not settled")
    if unheld:
        return False
    if status.get("idle") is not True or any(blockers.values()):
        raise RuntimeError("worker cleanup or admission has not settled")
    if (not isinstance(lease, dict) or lease.get("operation_id") != handoff["operation_id"]
            or lease.get("lease_id") != handoff["lease_id"] or lease.get("sealed") is not True
            or lease.get("expires_at", "missing") is not None):
        raise RuntimeError("worker no longer holds the exact sealed handoff")
    return True


def _held_state_ownership(layout: files.ExecutionLayout) -> tuple[int, int]:
    """Prove an exclusive state owner exists, without claiming flock exposes PID.

    Native service and authenticated callback probes bind the maintained worker
    PID separately. This checks that its lifetime ownership boundary is intact.
    """
    parent = files._path(layout.state_root / "admin")
    files._owned_directory(parent, private=True)
    path = parent / "state-owner.lock"
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("worker state ownership lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise RuntimeError("replacement worker does not hold exclusive state ownership")
        linked = path.lstat()
        identity = info.st_dev, info.st_ino
        if (linked.st_dev, linked.st_ino) != identity:
            raise RuntimeError("worker state ownership lock changed")
        return identity
    finally:
        os.close(descriptor)


def release_existing_handoff(root: Path, handoff_file: Path, *,
                             control: WorkerControl | None = None,
                             services: NativeServices | None = None) -> dict[str, Any]:
    """Release once with epoch/operation/lease CAS; propagate every uncertain result.

    The caller must have atomically changed only its owned update to failed and
    settled provider teardown. The private status confirms quiescence again.
    ``control`` and ``services`` are injectable for isolated boundary tests.
    """
    return _release_handoff(root, handoff_file, control=control, services=services)


def retry_failed_handoff(root: Path, status_path: Path, expected_status: dict[str, Any], *,
                         control: WorkerControl | None = None,
                         services: NativeServices | None = None) -> dict[str, Any]:
    """Finish only the exact saved failed cleanup, including a lost release reply.

    The caller holds its update operation lock, never the status flock across
    this call, then CASes its status after success. No saved evidence is changed.
    An unheld authenticated replacement worker needs no maintenance POST either;
    its native process and exclusive state ownership must also remain unchanged.
    """
    root, status_path = files._path(root), files._path(status_path)
    if not isinstance(expected_status, dict):
        raise ValueError("failed handoff status is invalid")
    # Freeze the caller's snapshot across callback requests.
    expected_status = json.loads(json.dumps(expected_status))
    update_id = expected_status.get("update_id")
    preparation_id = expected_status.get("preparation_id") or update_id
    if (not isinstance(update_id, str) or re.fullmatch(r"[0-9a-f]{32}", update_id) is None
            or not isinstance(preparation_id, str) or re.fullmatch(r"[0-9a-f]{32}", preparation_id) is None
            or expected_status.get("phase") != "failed"
            or expected_status.get("error_code") != "server_update_handoff_release_failed"
            or expected_status.get("retryable") is not True
            or expected_status.get("runner_pid") is not None):
        raise ValueError("status does not own a failed preinstaller handoff")
    handoff_file = root / ".update-preparations" / preparation_id / f"handoff-{update_id}.json"

    def verify_status(layout: files.ExecutionLayout, handoff: dict) -> None:
        if status_path != layout.state_root / "admin/server-update.json":
            raise ValueError("handoff status is outside the installed state directory")
        files._owned_directory(status_path.parent, private=True)
        data, _mode = files._read_file(status_path, private=True)
        if json.loads(data) != expected_status:
            raise RuntimeError("failed update status changed before handoff recovery")
        if (expected_status.get("_execution_handoff") != handoff
                or handoff["operation_id"] != str(uuid.UUID(hex=update_id))):
            raise RuntimeError("saved failed status does not own this handoff")

    return _release_handoff(root, handoff_file, control=control, services=services,
                            allow_released=True, verify_status=verify_status)


def _release_handoff(root: Path, handoff_file: Path, *, control=None, services=None,
                     allow_released: bool = False, verify_status=None) -> dict[str, Any]:
    root = files._path(root)
    files._owned_directory(root)
    with InstallationLock(root):
        _no_handoff_recovery_overlap(root)
        handoff = _read_handoff(root, handoff_file)
        layout = _installed_layout(root)
        if verify_status is not None:
            verify_status(layout, handoff)
        control = control or WorkerControl()
        services = services or NativeServices(layout)
        record, status = control.status(layout)
        _matching_status(handoff, layout, record, status, services.snapshot(), allow_released=allow_released)
        replacement = allow_released and record.get("instance_id") != handoff["worker_instance_id"]
        state_owner = _held_state_ownership(layout) if replacement else None
        # Authenticate the stable worker callback itself; a public gateway can
        # be unavailable without changing ownership of this execution epoch.
        health = control.callback_health(layout, record)
        execution = health.get("execution_service")
        if (health.get("ok") is not True or health.get("server_identity") != handoff["expected_server_identity"]
                or not isinstance(execution, dict) or execution.get("instance_id") != record["instance_id"]
                or execution.get("pid") != record["pid"]):
            raise RuntimeError("authenticated worker identity does not match handoff")
        # Re-read local provenance and live admission after the health request.
        # A takeover or failed provider close must not disappear in that gap.
        _no_handoff_recovery_overlap(root)
        if _read_handoff(root, handoff_file) != handoff or _installed_layout(root) != layout:
            raise RuntimeError("handoff provenance changed before release")
        latest_record, latest_status = control.status(layout)
        held = _matching_status(handoff, layout, latest_record, latest_status, services.snapshot(),
                                allow_released=allow_released)
        if latest_record != record:
            raise RuntimeError("worker process receipt changed before release")
        if replacement and _held_state_ownership(layout) != state_owner:
            raise RuntimeError("worker state ownership changed before acknowledgment")
        if verify_status is not None:
            verify_status(layout, handoff)
        if not held:
            return {"released": True, "already_released": True, "operation_id": handoff["operation_id"],
                    "worker_instance_id": record["instance_id"]}
        result = control._maintenance(layout, latest_record, action="release",
                                      operation=handoff["operation_id"], lease_id=handoff["lease_id"])
        if (result.get("worker_instance_id") != handoff["worker_instance_id"]
                or result.get("lease", "missing") is not None):
            raise RuntimeError("worker did not confirm exact handoff release")
        return {"released": True, "already_released": False, "operation_id": handoff["operation_id"],
                "worker_instance_id": handoff["worker_instance_id"]}
