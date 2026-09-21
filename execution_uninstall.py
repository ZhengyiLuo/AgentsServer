"""Idle-only uninstall of a healthy, independently managed execution worker.

Unjournaled stopped or broken installations require separate ownership recovery.
A retained uninstall intent can resume only after its ownership is revalidated.
Pending updates/activation, live agents, changed native jobs, or a failed native
stop preserve all runtime and data files. State is retained unless the operator
separately confirms its exact path at an interactive terminal.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import uuid

import execution_install as files
from execution_manage import InstallationLock, NativeServices, WorkerControl
from update_handoff import _installed_layout, _no_activation
from execution_preparation import _binding, _check_binding


INTENT_NAME = ".execution-uninstall.json"


def _read_intent(root: Path, layout: files.ExecutionLayout) -> dict | None:
    try:
        data, _mode = files._read_file(root / INTENT_NAME, private=True)
    except FileNotFoundError:
        return None
    value = json.loads(data)
    expected = {"format", "root", "worker_release", "gateway_release", "operation_id",
                "purge_state", "identities", "layout_sha256", "phase", "server_identity"}
    if (not isinstance(value, dict) or set(value) != expected or type(value["format"]) is not int
            or value["format"] != 1 or type(value["purge_state"]) is not bool
            or value["root"] != str(root) or value["worker_release"] != str(layout.worker_release)
            or value["gateway_release"] != str(layout.gateway_release)
            or value["phase"] not in {"prepared", "sealed", "stopped"}
            or not isinstance(value["server_identity"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value["server_identity"]) is None
            or not isinstance(value["operation_id"], str)
            or str(uuid.UUID(value["operation_id"])) != value["operation_id"]):
        raise ValueError("uninstall intent does not match this installed runtime")
    identities = value["identities"]
    if not isinstance(identities, dict) or set(identities) != {"root", "worker", "gateway"}:
        raise ValueError("uninstall intent identities are invalid")
    for name, path in (("root", root), ("worker", layout.worker_release), ("gateway", layout.gateway_release)):
        _check_binding(path, identities[name])
    manifest, _mode = files._read_file(root / files.LAYOUT_NAME, private=True)
    if hashlib.sha256(manifest).hexdigest() != value["layout_sha256"]:
        raise RuntimeError("installed execution layout changed after uninstall admission")
    return value


def pending_uninstall_operation(root: Path, runtime_root: Path) -> str:
    """Startup-only reader: hold this exact retained worker without mutations."""
    _no_activation(root)
    layout = _installed_layout(root)
    value = _read_intent(root, layout)
    if value is None or files._path(runtime_root) != layout.worker_release:
        raise RuntimeError("worker is not retained by the pending uninstall")
    return value["operation_id"]


def _write_intent(root: Path, layout: files.ExecutionLayout, operation: str, purge_state: bool,
                  server_identity: str) -> dict:
    if (root / INTENT_NAME).exists() or (root / INTENT_NAME).is_symlink():
        raise RuntimeError("another uninstall intent already exists")
    manifest, _mode = files._read_file(root / files.LAYOUT_NAME, private=True)
    value = {"format": 1, "root": str(root), "worker_release": str(layout.worker_release),
             "gateway_release": str(layout.gateway_release), "operation_id": operation,
             "server_identity": server_identity,
             "purge_state": purge_state, "phase": "prepared", "layout_sha256": hashlib.sha256(manifest).hexdigest(),
             "identities": {"root": _binding(root), "worker": _binding(layout.worker_release),
                            "gateway": _binding(layout.gateway_release)}}
    files._atomic_write(root / INTENT_NAME, files._json_bytes(value))
    return value


def _no_provider_children(state_root: Path) -> None:
    try:
        data, _mode = files._read_file(state_root / "admin/provider-children.json", private=True)
    except FileNotFoundError:
        return
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("children") != []:
        raise RuntimeError("provider child ownership remains unresolved; runtime files were retained")


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


def _no_pending_update(layout: files.ExecutionLayout) -> None:
    path = layout.state_root / "admin/server-update.json"
    try:
        data, _mode = files._read_file(path, private=True)
    except FileNotFoundError:
        return
    value = json.loads(data)
    if (not isinstance(value, dict) or value.get("phase") not in {
            "idle", "available", "current", "complete", "failed", "cancelled", "canceled"}):
        raise RuntimeError("an update or prepared candidate is pending; cancel or finish it before uninstalling")


@contextmanager
def _owned_lease(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise PermissionError("uninstall ownership lease is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another process still owns execution or candidate preparation") from None
        if _identity(path) != (info.st_dev, info.st_ino):
            raise RuntimeError("uninstall ownership lease changed")
        yield
    finally:
        os.close(descriptor)


def _hold_preparation_leases(root: Path, stack: ExitStack) -> None:
    parent = root / ".update-preparations"
    if not parent.exists() and not parent.is_symlink():
        return
    files._owned_directory(parent, private=True)
    for directory in sorted(parent.iterdir()):
        if re.fullmatch(r"[0-9a-f]{32}", directory.name) is None:
            raise RuntimeError("unknown candidate preparation directory; no files were removed")
        files._owned_directory(directory, private=True)
        lock = directory / "runner.lock"
        if lock.exists() or lock.is_symlink():
            stack.enter_context(_owned_lease(lock))


def _service_files(layout: files.ExecutionLayout) -> dict[str, tuple[bytes, int]]:
    result = {}
    for role in ("worker", "gateway"):
        path = files._path(layout.service_path(role))
        files._owned_directory(path.parent)
        data, mode = files._read_file(path)
        if data != files.render_service(layout, role) or mode not in {0o600, 0o644}:
            raise RuntimeError("native service configuration is not this installed execution layout")
        result[role] = data, mode
    if not layout.current.is_symlink() or layout.current.resolve(strict=True) != layout.gateway_release:
        raise RuntimeError("gateway current link no longer matches its installed generation")
    return result


def _removable_tree(path: Path, *, device: int | None = None) -> None:
    """Inspect owned descendants behind a private root without following links.

    uv and Python can inherit umask 0002, producing 0775 directories inside
    this 0700 boundary. Those bits grant no traversal through the private root.
    Requiring every descendant to be 0700/0755 would strand legitimate installs.
    """
    info = path.lstat()
    if device is None:
        files._owned_directory(path, private=True)
        device = info.st_dev
    else:
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o700 != 0o700):
            raise PermissionError("managed removal tree contains an inaccessible or foreign directory")
        if info.st_dev != device:
            raise RuntimeError("managed removal tree contains another mounted filesystem")
    for child in path.iterdir():
        child_info = child.lstat()
        if stat.S_ISDIR(child_info.st_mode):
            _removable_tree(child, device=device)


def _remove_child(path: Path) -> None:
    if stat.S_ISDIR(path.lstat().st_mode):
        if not shutil.rmtree.avoids_symlink_attacks:
            raise RuntimeError("this Python cannot safely remove a managed directory")
        shutil.rmtree(path)
    else:
        path.unlink()


def uninstall(root: Path, *, config_root: Path, state_root: Path, home: Path,
              purge_state: bool = False, services: NativeServices | None = None,
              control: WorkerControl | None = None) -> dict:
    root, config_root, state_root, home = map(files._path, (root, config_root, state_root, home))
    with InstallationLock(root), ExitStack() as leases:
        _no_activation(root)
        from execution_recovery import require_retired_owners
        require_retired_owners(root)
        layout = _installed_layout(root)
        if (layout.config_root, layout.state_root, layout.home) != (config_root, state_root, home):
            raise RuntimeError("uninstall roots do not match the installed execution layout")
        native = services or NativeServices(layout)
        control = control or WorkerControl()
        originals = _service_files(layout)
        _no_pending_update(layout)
        _hold_preparation_leases(root, leases)
        _removable_tree(root)
        _removable_tree(config_root)
        if purge_state:
            _removable_tree(state_root)
        roots = {path: _identity(path) for path in (root, config_root, state_root)}
        intent = _read_intent(root, layout)
        if intent is not None and intent["purge_state"] != purge_state:
            raise RuntimeError("retry must preserve the existing uninstall data-removal choice")
        prior = native.snapshot()
        # An interrupted graceful stop can precede the stopped-phase write.
        # The exact intent, empty child registry and exclusive state lease
        # below supply the missing proof; an unjournaled stopped install fails.
        stopped_resume = (intent is not None
                          and all(prior[role].get("state") in {"stopped", "absent"} for role in ("worker", "gateway")))
        partial_resume = (intent is not None and prior["worker"].get("state") == "running"
                          and prior["gateway"].get("state") in {"stopped", "absent"})
        if not stopped_resume and not partial_resume and any(prior[role].get("state") != "running" for role in ("worker", "gateway")):
            raise RuntimeError("safe uninstall requires a healthy running worker and gateway; stopped or broken installations require ownership recovery")
        if partial_resume:
            record, status = control.status(layout)
            lease = status.get("lease")
            if (record.get("pid") != prior["worker"]["pid"] or record.get("release_root") != str(layout.worker_release)
                    or status.get("idle") is not True or not isinstance(lease, dict)
                    or lease.get("operation_id") != intent["operation_id"] or lease.get("sealed") is not True):
                raise RuntimeError("interrupted uninstall no longer owns the running worker")
            health = control.callback_health(layout, record)
            worker = health.get("execution_service")
            if (health.get("ok") is not True or health.get("server_identity") != intent["server_identity"]
                    or not isinstance(worker, dict) or worker.get("pid") != record["pid"]
                    or worker.get("instance_id") != record.get("instance_id")):
                raise RuntimeError("interrupted uninstall worker health identity changed")
            receipt = {"worker_pid": record["pid"], "gateway_pid": prior["gateway"].get("pid"),
                       "worker_release": str(layout.worker_release), "worker_instance_id": record["instance_id"],
                       "server_identity": health["server_identity"]}
        else:
            receipt = {} if stopped_resume else control.receipt(layout, native)
        if not stopped_resume and (receipt.get("worker_pid") != prior["worker"].get("pid")
                or receipt.get("gateway_pid") != prior["gateway"].get("pid")
                or receipt.get("worker_release") != str(layout.worker_release)
                or not receipt.get("server_identity")):
            raise RuntimeError("authenticated runtime is not owned by both installed native jobs")
        operation = intent["operation_id"] if intent else str(uuid.uuid4())
        sealed = None
        native_changed = False
        deleting = False
        try:
            if not stopped_resume:
                if intent is None:
                    record, status = control.status(layout)
                    if (record.get("instance_id") != receipt["worker_instance_id"]
                            or record.get("pid") != prior["worker"]["pid"]
                            or status.get("idle") is not True or status.get("lease") is not None):
                        raise RuntimeError("execution is busy or another operation owns admission")
                    # Persist before acquiring the lease: a lost seal response
                    # must not strand an otherwise unidentifiable hold.
                    intent = _write_intent(root, layout, operation, purge_state, receipt["server_identity"])
                sealed = control.seal_for_stop(layout, operation, prior["worker"]["pid"])
                if sealed["record"].get("instance_id") != receipt.get("worker_instance_id"):
                    raise RuntimeError("worker epoch changed before uninstall admission")
                intent = {**intent, "phase": "sealed"}
                files._atomic_write(root / INTENT_NAME, files._json_bytes(intent))
            _no_pending_update(layout)
            _no_activation(root)
            if _service_files(layout) != originals or native.snapshot() != prior:
                raise RuntimeError("native service ownership changed before uninstall")
            # Disabling boot activation precedes the graceful stops; no forced
            # signal or broad process-group/provider cleanup is ever used.
            native_changed = True
            for role in (() if stopped_resume else ("gateway", "worker")):
                native.set_enabled(role, False)
            for role in (() if stopped_resume else ("gateway", "worker")):
                if prior[role].get("state") in {"stopped", "absent"}:
                    continue
                observed = native.snapshot()[role]
                if observed.get("state") != "running" or observed.get("pid") != prior[role]["pid"]:
                    raise RuntimeError("native process changed before its authorized stop")
                if role == "worker":
                    record, status = control.status(layout)
                    if (record != sealed["record"] or status.get("lease") != sealed["lease"]
                            or status.get("idle") is not True):
                        raise RuntimeError("worker admission changed before its authorized stop")
                native.stop(role)
            stopped = native.snapshot()
            if any(stopped[role].get("state") not in {"stopped", "absent"} for role in ("worker", "gateway")):
                raise RuntimeError("native services did not both stop; all files were retained")
            # Native PID disappearance is insufficient if a separate process
            # still owns the state store. Retain this lease through deletion.
            leases.enter_context(_owned_lease(state_root / "admin/state-owner.lock"))
            _no_provider_children(state_root)
            _no_pending_update(layout)
            _no_activation(root)
            if _service_files(layout) != originals or any(_identity(path) != identity for path, identity in roots.items()):
                raise RuntimeError("managed files changed during service shutdown")
            if _read_intent(root, layout) != intent:
                raise RuntimeError("uninstall intent changed before removal")
            intent = {**intent, "phase": "stopped"}
            files._atomic_write(root / INTENT_NAME, files._json_bytes(intent))
            deleting = True
            for role in ("gateway", "worker"):
                layout.service_path(role).unlink()
            native.reload()
            for child in root.iterdir():
                if child.name != ".install-lock":
                    _remove_child(child)
            _remove_child(config_root)
            if purge_state:
                _remove_child(state_root)
                legacy_alias = home / ".zenithbot-agent"
                if (legacy_alias.is_symlink() and legacy_alias.lstat().st_uid == os.getuid()
                        and legacy_alias.resolve(strict=False) == state_root):
                    legacy_alias.unlink()
            files._fsync_directory(root)
        except BaseException as original_error:
            if sealed is None and intent is not None and not stopped_resume:
                # A transport failure can occur after the worker accepted the
                # lease. Inspect that exact operation once; never acquire a
                # replacement lease or discard another epoch's evidence.
                record, status = control.status(layout)
                if (record.get("instance_id") == receipt.get("worker_instance_id")
                        and record.get("pid") == prior["worker"].get("pid")):
                    lease = status.get("lease")
                    if lease is None and _read_intent(root, layout) == intent:
                        (root / INTENT_NAME).unlink()
                        files._fsync_directory(root)
                        intent = None
                    elif isinstance(lease, dict) and lease.get("operation_id") == operation:
                        sealed = {"record": record, "lease": lease}
            # Only the same still-running worker can have its exact lease
            # released. A stopped or uncertain epoch is never restarted here.
            if sealed is not None and not deleting:
                try:
                    current = native.snapshot()
                    if current["worker"].get("pid") != prior["worker"].get("pid") or current["worker"].get("state") != "running":
                        raise RuntimeError("worker no longer has its original running identity")
                    if _service_files(layout) != originals:
                        raise RuntimeError("service configuration changed")
                    if native_changed:
                        if prior["gateway"]["state"] == "running" and current["gateway"].get("state") != "running":
                            native.start("gateway")
                        for role in ("worker", "gateway"):
                            native.set_enabled(role, prior[role]["enabled"])
                    record, status = control.status(layout)
                    lease = status.get("lease")
                    if (record != sealed["record"] or not isinstance(lease, dict)
                            or lease.get("operation_id") != operation or lease.get("lease_id") != sealed["lease"].get("lease_id")):
                        raise RuntimeError("worker lease changed")
                    result = control._maintenance(layout, record, action="release", operation=operation,
                                                  lease_id=lease["lease_id"])
                    if result.get("lease", "missing") is not None:
                        raise RuntimeError("worker did not release uninstall admission")
                    if intent is not None:
                        if _read_intent(root, layout) != intent:
                            raise RuntimeError("uninstall intent changed during recovery")
                        (root / INTENT_NAME).unlink()
                        files._fsync_directory(root)
                except Exception as recovery_error:
                    raise RuntimeError("uninstall stopped before file removal; service/admission recovery requires ownership verification") from recovery_error
            raise original_error
    # The shared lock remains present until every requested removal completes.
    root.rmdir()
    return {"removed": True, "state_preserved": not purge_state, "state_root": str(state_root)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "config-root", "state-root", "home"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--purge-state", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        # Validate provenance before prompting or executing native operations.
        _no_activation(arguments.root)
        _installed_layout(files._path(arguments.root))
        if arguments.purge_state:
            if not sys.stdin.isatty():
                raise RuntimeError("Refusing to purge state without an interactive terminal; --yes never bypasses the state guard")
            if input(f"Type the exact state path to confirm ({arguments.state_root}): ") != str(arguments.state_root):
                raise RuntimeError("State path did not match; nothing was changed")
        if not arguments.yes:
            if not sys.stdin.isatty() or input("Remove both idle services, runtime and configuration? [y/N] ").lower() not in {"y", "yes"}:
                raise RuntimeError("Uninstall was not confirmed; nothing was changed")
        result = uninstall(arguments.root, config_root=arguments.config_root, state_root=arguments.state_root,
                           home=arguments.home, purge_state=arguments.purge_state)
        print("AgentsServer gateway, execution service, release runtime and configuration removed.")
        if result["state_preserved"]:
            print("Preserved chat history, jobs, files, Team Hub state and secure-peer credentials at " + result["state_root"])
        print("Persistent chat terminals remain independent and were not stopped.")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        # NativeServices/WorkerControl never echo service environments/tokens.
        print("Cannot uninstall the separate gateway/execution layout: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
