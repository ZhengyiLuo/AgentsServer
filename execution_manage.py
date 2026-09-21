"""Explicit native activation of separately managed gateway/execution jobs.

This module has no import-time service effects. The CLI's apply/recover commands
are mutating installer operations; inspect is read-only. Native service changes
are serialized with install.sh's .install-lock. Every worker stop requires a
fresh, sealed admission hold for that exact worker epoch. A legacy monolith
without the private control contract must enter through its existing managed
idle-update handoff; direct migration of that running process is refused here.
"""

from __future__ import annotations

import argparse
import base64
import errno
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time
from typing import Any, Callable
import urllib.parse

import execution_install as files
from execution_transport import _read_secret
from execution_http import request_json


class InstallationLock:
    """Use the same durable PID-directory namespace as the legacy installer."""

    def __init__(self, root: Path) -> None:
        self.root = files._path(root)
        self.path = self.root / ".install-lock"
        self.identity: tuple[int, int] | None = None

    def __enter__(self) -> "InstallationLock":
        files._owned_directory(self.root)
        draft = self.root / f".install-lock.{os.getpid()}.{secrets.token_hex(12)}.tmp"
        draft.mkdir(mode=0o700)
        files._atomic_write(draft / "pid", f"{os.getpid()}\n".encode())
        try:
            for _ in range(16):
                try:
                    os.rename(draft, self.path)
                except OSError as exc:
                    if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                        raise
                    self._reap_dead_owner()
                else:
                    info = self.path.lstat()
                    self.identity = info.st_dev, info.st_ino
                    files._fsync_directory(self.root)
                    return self
            raise RuntimeError("installation lock ownership kept changing")
        finally:
            if draft.exists():
                (draft / "pid").unlink()
                draft.rmdir()

    def _reap_dead_owner(self) -> None:
        files._owned_directory(self.path, private=True)
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            info = os.fstat(fd)
            try:
                owner_fd = os.open("pid", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if os.listdir(fd):
                    raise RuntimeError("installation lock contains unknown files")
                return  # atomic rename may replace this exact empty directory
            try:
                owner_info = os.fstat(owner_fd)
                if (not stat.S_ISREG(owner_info.st_mode) or owner_info.st_uid != os.getuid()
                        or owner_info.st_nlink != 1 or stat.S_IMODE(owner_info.st_mode) not in {0o600, 0o644}):
                    raise PermissionError("installation lock owner is unsafe")
                raw = os.read(owner_fd, 33)
            finally:
                os.close(owner_fd)
            if not re.fullmatch(rb"[0-9]{1,20}\n?", raw) or int(raw) <= 1:
                raise ValueError("installation lock owner is invalid")
            try:
                os.kill(int(raw), 0)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise RuntimeError("another installer owns the installation lock") from exc
            else:
                raise RuntimeError("another installer owns the installation lock")
            current = os.stat("pid", dir_fd=fd, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (owner_info.st_dev, owner_info.st_ino):
                return
            # unlinkat is pinned to the dead owner's directory, never a newer lock.
            os.unlink("pid", dir_fd=fd)
            os.fsync(fd)
            linked = self.path.lstat()
            if (linked.st_dev, linked.st_ino) != (info.st_dev, info.st_ino):
                return
        finally:
            os.close(fd)

    def __exit__(self, *unused: Any) -> None:
        if self.identity is None:
            return
        files._owned_directory(self.path, private=True)
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
        try:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != self.identity:
                raise RuntimeError("installation lock identity changed")
            data, _mode = files._read_file(self.path / "pid", private=True)
            if data != f"{os.getpid()}\n".encode() or set(os.listdir(fd)) != {"pid"}:
                raise RuntimeError("installation lock owner changed")
            os.unlink("pid", dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        info = self.path.lstat()
        if (info.st_dev, info.st_ino) != self.identity:
            raise RuntimeError("installation lock path changed")
        self.path.rmdir()
        files._fsync_directory(self.root)
        self.identity = None


class NativeServices:
    def __init__(self, layout: files.ExecutionLayout, *, run: Callable[..., Any] | None = None,
                 timeout: float = 180.0) -> None:
        self.layout = layout
        self.run = run or subprocess.run
        self.timeout = timeout
        if run is None and (sys.platform not in {"darwin", "linux"}
                            or ((layout.platform == "Darwin") != (sys.platform == "darwin"))):
            raise ValueError("native execution platform does not match this host")

    def _command(self, args: list[str], *, allow_failure: bool = False, timeout: float = 10) -> Any:
        result = self.run(args, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=timeout, check=False)
        if result.returncode and not allow_failure:
            # launchctl print can contain environment secrets. Never echo output.
            raise RuntimeError(f"native service command failed ({args[0]} {args[1]})")
        return result

    def _unit(self, role: str) -> str:
        return files.WORKER_UNIT if role == "worker" else files.GATEWAY_UNIT

    def _target(self, role: str) -> str:
        label = files.WORKER_LABEL if role == "worker" else files.GATEWAY_LABEL
        return f"gui/{os.getuid()}/{label}"

    def _observe(self, role: str) -> dict[str, Any]:
        path = self.layout.service_path(role)
        exists = files._snapshot(path)["exists"]
        if self.layout.platform == "Linux":
            result = self._command(["systemctl", "--user", "show", self._unit(role), "--no-pager",
                                    "--property=LoadState,ActiveState,UnitFileState,MainPID"])
            values: dict[str, str] = {}
            for line in result.stdout.splitlines():
                key, separator, value = line.partition("=")
                if not separator or key in values:
                    raise RuntimeError("systemd returned ambiguous service ownership")
                values[key] = value
            if set(values) != {"LoadState", "ActiveState", "UnitFileState", "MainPID"}:
                raise RuntimeError("systemd omitted required service ownership")
            if values["LoadState"] not in {"loaded", "not-found", "bad-setting", "error"}:
                raise RuntimeError("systemd service is not safely manageable")
            if not values["MainPID"].isdigit():
                raise RuntimeError("systemd returned an invalid service PID")
            if values["UnitFileState"] not in {"enabled", "enabled-runtime", "disabled", "static", ""}:
                raise RuntimeError("systemd service enablement is unsupported")
            pid = int(values["MainPID"])
            enabled = values["UnitFileState"] in {"enabled", "enabled-runtime"}
            active = values["ActiveState"]
            if values["LoadState"] in {"bad-setting", "error"}:
                # A malformed staged job can be restored only after the manager
                # proves no main process is active. Exact config ownership is
                # separately checked against the activation journal by recover.
                if exists and pid == 0 and active in {"inactive", "failed"}:
                    return {"state": "stopped", "enabled": enabled}
                raise RuntimeError("invalid native service still has an active process")
            if not exists:
                if values["LoadState"] != "not-found" or pid or active not in {"inactive", "failed"}:
                    raise RuntimeError("loaded native service has no safely restorable file")
                return {"state": "absent", "enabled": False}
            if active == "active" and pid > 0:
                return {"state": "running", "enabled": enabled, "pid": pid}
            if active in {"inactive", "failed"} and pid == 0:
                return {"state": "stopped", "enabled": enabled}
            return {"state": "transitioning", "enabled": enabled}
        disabled = self._command(["/bin/launchctl", "print-disabled", f"gui/{os.getuid()}"])
        label = self._target(role).rsplit("/", 1)[1]
        entries = re.findall(r'"' + re.escape(label) + r'"\s*=>\s*(true|false)', disabled.stdout)
        if len(entries) > 1:
            raise RuntimeError("launchd returned ambiguous enablement")
        enabled = entries != ["true"]
        result = self._command(["/bin/launchctl", "print", self._target(role)], allow_failure=True)
        if result.returncode:
            if "Could not find service" not in result.stderr:
                raise RuntimeError("launchd service ownership is unknown")
            return {"state": "stopped" if exists else "absent", "enabled": enabled if exists else False}
        if not exists:
            raise RuntimeError("loaded native service has no safely restorable plist")
        pids = re.findall(r"(?m)^\s*pid\s*=\s*([0-9]+)\s*$", result.stdout)
        if len(pids) == 1 and int(pids[0]) > 0:
            return {"state": "running", "enabled": enabled, "pid": int(pids[0])}
        return {"state": "transitioning", "enabled": enabled}

    def snapshot(self) -> dict[str, Any]:
        states = {role: self._observe(role) for role in ("worker", "gateway")}
        if any(item["state"] == "transitioning" for item in states.values()):
            raise RuntimeError("native service is transitioning; retry after it settles")
        return states

    def stop(self, role: str) -> None:
        state = self._observe(role)
        if state["state"] in {"absent", "stopped"}:
            return
        if self.layout.platform == "Linux":
            self._command(["systemctl", "--user", "stop", "--no-block", self._unit(role)])
        else:
            self._command(["/bin/launchctl", "bootout", self._target(role)],
                          timeout=self.timeout if role == "worker" else min(self.timeout, 30))
        deadline = time.monotonic() + (self.timeout if role == "worker" else min(self.timeout, 30))
        while time.monotonic() < deadline:
            if self._observe(role)["state"] in {"absent", "stopped"}:
                return
            time.sleep(0.1)
        raise RuntimeError("native service did not stop; no force kill was attempted")

    def reload(self) -> None:
        if self.layout.platform == "Linux":
            self._command(["systemctl", "--user", "daemon-reload"])

    def set_enabled(self, role: str, enabled: bool) -> None:
        if self.layout.platform == "Linux":
            self._command(["systemctl", "--user", "enable" if enabled else "disable", self._unit(role)])
        else:
            self._command(["/bin/launchctl", "enable" if enabled else "disable", self._target(role)])

    def start(self, role: str) -> None:
        files._read_file(self.layout.service_path(role))
        self.set_enabled(role, True)
        if self.layout.platform == "Linux":
            self._command(["systemctl", "--user", "start", "--no-block", self._unit(role)])
        else:
            self._command(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(self.layout.service_path(role))])

    def restore(self, role: str, prior: dict[str, Any]) -> None:
        if prior["state"] == "absent":
            return
        if prior["state"] == "running":
            self.start(role)
        if prior["state"] != "running" or not prior["enabled"]:
            self.set_enabled(role, prior["enabled"])


class WorkerControl:
    def __init__(self, *, request_timeout: float = 5.0, services: NativeServices | None = None) -> None:
        self.timeout = request_timeout
        self.services = services

    def _native(self, layout: files.ExecutionLayout) -> dict[str, Any]:
        return (self.services or NativeServices(layout)).snapshot()

    @staticmethod
    def _running_pid(native: dict, role: str) -> int:
        value = native[role]
        if value.get("state") != "running" or type(value.get("pid")) is not int or value["pid"] <= 1:
            raise RuntimeError("credential endpoint has no running native owner")
        return value["pid"]

    def _json(self, url: str, token: str, body: dict[str, Any] | None = None, *,
              expected_pid: int, platform: str, verify_owner: Callable[[], None]) -> dict[str, Any]:
        return request_json(url, token, body=body, expected_pid=expected_pid, platform=platform,
                            verify_owner=verify_owner, timeout=self.timeout, maximum_body=files.MAX_CONFIG_BYTES)

    def _callback_request(self, layout: files.ExecutionLayout, record: dict, path: str,
                          token: str, body: dict | None = None) -> dict:
        def verify_owner():
            if (self._running_pid(self._native(layout), "worker") != record["pid"]
                    or self.worker_record(layout) != record):
                raise RuntimeError("native worker or callback receipt changed before credentials were sent")
        verify_owner()
        return self._json(record["callback_origin"] + path, token, body,
                          expected_pid=record["pid"], platform=layout.platform, verify_owner=verify_owner)

    def callback_health(self, layout: files.ExecutionLayout, record: dict) -> dict:
        return self._callback_request(layout, record, "/api/health", self._agent_token(layout))

    def worker_record(self, layout: files.ExecutionLayout) -> dict[str, Any]:
        files._owned_directory(layout.runtime_dir, private=True)
        data, _mode = files._read_file(layout.runtime_dir / "worker.json", private=True)
        record = json.loads(data)
        if (not isinstance(record, dict) or record.get("role") != "worker"
                or record.get("protocol") != files.PROTOCOL_VERSION
                or type(record.get("pid")) is not int or record["pid"] <= 0
                or not isinstance(record.get("instance_id"), str) or not record["instance_id"]):
            raise RuntimeError("worker process receipt is invalid")
        origin = urllib.parse.urlsplit(record.get("callback_origin", ""))
        if (origin.scheme != "http" or origin.hostname != "127.0.0.1" or not origin.port
                or origin.path or origin.query or origin.fragment or origin.username or origin.password):
            raise RuntimeError("worker callback receipt is not an exact loopback origin")
        release = files._path(record.get("release_root", ""))
        if release.parent != layout.install_root / "releases":
            raise RuntimeError("worker receipt release is outside this installation")
        return record

    def status(self, layout: files.ExecutionLayout) -> tuple[dict[str, Any], dict[str, Any]]:
        record = self.worker_record(layout)
        result = self._callback_request(layout, record, "/api/admin/execution/status",
                                        _read_secret(layout.runtime_dir / "control.token"))
        if result.get("worker_instance_id") != record["instance_id"] or type(result.get("idle")) is not bool:
            raise RuntimeError("private worker status does not match its process receipt")
        return record, result

    def _maintenance(self, layout: files.ExecutionLayout, record: dict[str, Any], *, action: str,
                     operation: str, lease_id: str | None = None) -> dict[str, Any]:
        payload = {"action": action, "expected_worker_instance_id": record["instance_id"],
                   "operation_id": operation}
        if lease_id is not None:
            payload["lease_id"] = lease_id
        result = self._callback_request(layout, record, "/api/admin/execution/maintenance",
                                        _read_secret(layout.runtime_dir / "control.token"), payload)
        if result.get("worker_instance_id") != record["instance_id"]:
            raise RuntimeError("worker changed during maintenance admission")
        return result

    def seal_for_stop(self, layout: files.ExecutionLayout, operation: str, native_pid: int) -> dict[str, Any]:
        record, status = self.status(layout)
        if record["pid"] != native_pid:
            raise RuntimeError("native service does not own the worker that granted admission")
        lease = status.get("lease")
        if lease is None:
            status = self._maintenance(layout, record, action="acquire", operation=operation)
            lease = status.get("lease")
        if (not isinstance(lease, dict) or lease.get("operation_id") != operation
                or not isinstance(lease.get("lease_id"), str) or not lease["lease_id"]):
            raise RuntimeError("another operation owns worker maintenance admission")
        sealed = self._maintenance(layout, record, action="seal", operation=operation, lease_id=lease["lease_id"])
        final = sealed.get("lease")
        if (sealed.get("idle") is not True or not isinstance(final, dict)
                or final.get("lease_id") != lease["lease_id"] or final.get("operation_id") != operation
                or final.get("sealed") is not True):
            raise RuntimeError("worker did not seal an idle admission hold")
        return {"record": record, "lease": final}

    def require_startup_hold(self, layout: files.ExecutionLayout, operation: str) -> None:
        _record, status = self.status(layout)
        lease = status.get("lease")
        if (status.get("idle") is not True or not isinstance(lease, dict)
                or lease.get("operation_id") != operation or lease.get("sealed") is not True):
            raise RuntimeError("candidate worker did not retain its activation startup hold")

    def release(self, layout: files.ExecutionLayout, operation: str) -> None:
        record, status = self.status(layout)
        lease = status.get("lease")
        if lease is None:
            return
        if not isinstance(lease, dict) or lease.get("operation_id") != operation:
            raise RuntimeError("another operation owns the worker admission hold")
        result = self._maintenance(layout, record, action="release", operation=operation,
                                   lease_id=lease.get("lease_id"))
        if result.get("lease") is not None:
            raise RuntimeError("worker admission hold was not released")

    def _agent_token(self, layout: files.ExecutionLayout) -> str:
        values = dict(layout.environment)
        try:
            data, _mode = files._read_file(layout.config_root / "env", private=True)
        except FileNotFoundError:
            pass
        else:
            for line in data.decode().splitlines():
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                key, separator, value = line.partition("=")
                if separator:
                    parsed = shlex.split(value)
                    if len(parsed) == 1:
                        values[key] = parsed[0]
        for key in ("AGENTSDOCK_AGENT_TOKEN", "ZENITHDOCK_AGENT_TOKEN", "ZENITHBOT_AGENT_TOKEN", "AGENT_TOKEN"):
            if values.get(key):
                return values[key]
        raise RuntimeError("authenticated activation health requires the preserved server token")

    def health(self, layout: files.ExecutionLayout) -> dict[str, Any]:
        host = ("127.0.0.1" if layout.bind in {"0.0.0.0", "localhost"} else
                "::1" if layout.bind in {"::", "[::]", "::1", "[::1]"} else layout.bind)
        if ":" in host:
            host = f"[{host}]"
        native = self._native(layout)
        role = "gateway" if native["gateway"]["state"] == "running" else "worker"
        if role == "worker" and (native["gateway"]["state"] != "absent"
                or layout.manifest_path.exists() or layout.manifest_path.is_symlink()):
            raise RuntimeError("public credentials require the installed native gateway")
        pid = self._running_pid(native, role)
        def verify_owner():
            if self._running_pid(self._native(layout), role) != pid:
                raise RuntimeError("native health endpoint changed before credentials were sent")
        return self._json(f"http://{host}:{layout.port}/api/health", self._agent_token(layout),
                          expected_pid=pid, platform=layout.platform, verify_owner=verify_owner)

    def receipt(self, layout: files.ExecutionLayout, services: NativeServices) -> dict[str, Any]:
        health = self.health(layout)
        record = self.worker_record(layout)
        gateway, worker = health.get("gateway"), health.get("execution_service")
        if (health.get("ok") is not True or not isinstance(gateway, dict) or not isinstance(worker, dict)
                or gateway.get("protocol") != files.PROTOCOL_VERSION
                or worker.get("protocol") != files.PROTOCOL_VERSION
                or worker.get("instance_id") != record["instance_id"] or worker.get("pid") != record["pid"]
                or record.get("release_root") != str(layout.worker_release)):
            raise RuntimeError("authenticated gateway/worker health does not match owned runtime receipts")
        native = services.snapshot()
        if (native["worker"].get("pid") != worker.get("pid")
                or native["gateway"].get("pid") != gateway.get("pid")):
            raise RuntimeError("native services do not own the authenticated component processes")
        return {"gateway_version": gateway.get("version"), "worker_version": worker.get("version"),
                "worker_release": record["release_root"], "protocol_version": files.PROTOCOL_VERSION,
                "server_identity": health.get("server_identity"), "gateway_pid": gateway.get("pid"),
                "worker_pid": worker.get("pid"), "worker_instance_id": worker.get("instance_id"),
                "maintenance_held": worker.get("maintenance_held")}


class ActivationController:
    def __init__(self, layout: files.ExecutionLayout, *, services: NativeServices | None = None,
                 control: WorkerControl | None = None, health_timeout: float = 30.0) -> None:
        self.layout = layout
        self.services = services or NativeServices(layout)
        self.control = control or WorkerControl()
        self.health_timeout = health_timeout

    def _wait_receipt(self, layout: files.ExecutionLayout) -> dict[str, Any]:
        deadline = time.monotonic() + self.health_timeout
        error: Exception | None = None
        while True:
            try:
                receipt = self.control.receipt(layout, self.services)
                expected = files.layout_manifest(layout)
                if (receipt.get("gateway_version") != expected["gateway_version"]
                        or receipt.get("worker_version") != expected["worker_version"]):
                    raise RuntimeError("authenticated component versions are not the requested release")
                return receipt
            except (OSError, ValueError, RuntimeError) as exc:
                error = exc
            if time.monotonic() >= deadline:
                raise RuntimeError("execution components did not reach authenticated health") from error
            time.sleep(0.1)

    def _stop_worker(self, transaction: dict[str, Any]) -> None:
        state = self.services.snapshot()["worker"]
        if state["state"] == "running":
            self.control.seal_for_stop(self.layout, files.maintenance_operation_id(transaction), state["pid"])
            current = self.services.snapshot()["worker"]
            if current.get("pid") != state["pid"]:
                raise RuntimeError("worker changed after sealing; no stop was attempted")
        self.services.stop("worker")

    @staticmethod
    def _check_identity(transaction: dict[str, Any], receipt: dict[str, Any]) -> None:
        expected = transaction["expected_server_identity"]
        if expected is not None and receipt.get("server_identity") != expected:
            raise RuntimeError("authenticated server identity changed during activation recovery")

    def apply(self, scope: str) -> dict[str, Any]:
        """Caller must own InstallationLock (including recovery on exceptions)."""
        self.layout.validate()
        prior = self.services.snapshot()
        expected_identity = None
        if prior["gateway"]["state"] == "running":
            expected_identity = self.control.health(self.layout).get("server_identity")
            if not isinstance(expected_identity, str) or not expected_identity:
                raise RuntimeError("current authenticated server identity is unavailable")
        if prior["worker"]["state"] == "running":
            # Refuse a direct legacy migration before staging anything. Its
            # existing normal-update handoff needs a separate verified bridge.
            record, _status = self.control.status(self.layout)
            if record["pid"] != prior["worker"]["pid"]:
                raise RuntimeError("native worker ownership does not match its private receipt")
            if scope != "gateway" and _status.get("lease") is not None:
                raise RuntimeError("another execution operation already owns worker admission")
            prior["worker"]["instance_id"] = record["instance_id"]
        transaction = files.stage(self.layout, scope=scope, prior_services=prior,
                                  expected_server_identity=expected_identity)
        operation = files.maintenance_operation_id(transaction)
        changed_services = False
        try:
            files.prepare_runtime(self.layout)
            if scope in {"migration", "worker"} and prior["worker"]["state"] == "running":
                # Acquire + seal before ANY stop, including the public gateway.
                self.control.seal_for_stop(self.layout, operation, prior["worker"]["pid"])
            if scope in {"migration", "gateway"}:
                changed_services = True
                self.services.stop("gateway")
            if scope in {"migration", "worker"}:
                changed_services = True
                self._stop_worker(transaction)
            files.publish(self.layout.install_root)
            self.services.reload()
            if scope in {"migration", "worker"}:
                self.services.start("worker")
            if scope in {"migration", "gateway"}:
                self.services.start("gateway")
            receipt = self._wait_receipt(self.layout)
            if scope != "gateway":
                self.control.require_startup_hold(self.layout, operation)
            files.commit(self.layout.install_root, receipt)
            if scope != "gateway":
                self.control.release(self.layout, operation)
            files.finish(self.layout.install_root)
            return {"ok": True, "scope": scope, "receipt": receipt}
        except Exception:
            # The commit boundary is irreversible. Failed finalization leaves a
            # terminal journal and hold so recover can retry without rollback.
            pending, _layout = files._load(self.layout.install_root)
            if pending["phase"] == "committed":
                raise
            if not changed_services:
                files.rollback(self.layout.install_root)
                if scope != "gateway" and prior["worker"]["state"] == "running":
                    _record, status = self.control.status(self.layout)
                    lease = status.get("lease")
                    # A private maintenance request can win admission between
                    # preflight and seal. No service changed: retire only our
                    # journal and leave that other operation's hold intact.
                    if isinstance(lease, dict) and lease.get("operation_id") == operation:
                        self.control.release(self.layout, operation)
                files.finish(self.layout.install_root)
            else:
                self.recover()
            raise

    def recover(self) -> dict[str, Any]:
        """Restore a pending activation, or finalize an already committed one."""
        transaction, layout = files._load(self.layout.install_root)
        if layout != self.layout:
            raise ValueError("controller layout differs from the pending activation")
        files._check_publication(transaction, layout)
        operation = files.maintenance_operation_id(transaction)
        scope = transaction["scope"]
        if transaction["phase"] == "committed":
            receipt = self._wait_receipt(layout)
            self._check_identity(transaction, receipt)
            if scope == "gateway":
                prior = transaction["prior_services"]["worker"]
                if prior["state"] == "running" and (receipt["worker_pid"] != prior["pid"]
                        or receipt["worker_instance_id"] != prior["instance_id"]):
                    raise RuntimeError("retained worker changed before gateway finalization")
            else:
                self.control.release(layout, operation)
            files.finish(layout.install_root)
            return {"ok": True, "phase": "committed", "receipt": receipt}
        if transaction["phase"] != "rolled-back":
            if scope in {"migration", "worker"}:
                self._stop_worker(transaction)
            if scope in {"migration", "gateway"}:
                self.services.stop("gateway")
            files.rollback(layout.install_root)
        self.services.reload()
        affected = ("worker", "gateway") if scope == "migration" else (scope,)
        for role in affected:
            # Recovery may be retried after the old service was already started.
            current = self.services.snapshot()[role]
            prior = transaction["prior_services"][role]
            if prior["state"] == "running" and current["state"] == "running":
                continue
            self.services.restore(role, prior)
        previous = transaction["before"]["layout"]
        receipt = None
        if previous["exists"]:
            old_layout = files.ExecutionLayout.from_dict(json.loads(base64.b64decode(previous["data"]))["layout"])
            if all(transaction["prior_services"][role]["state"] == "running" for role in ("worker", "gateway")):
                receipt = self._wait_receipt(old_layout)
                self._check_identity(transaction, receipt)
        elif transaction["prior_services"]["worker"]["state"] == "running":
            # A legacy normal-update handoff needs its original health validator;
            # do not claim this isolated controller restored that boundary yet.
            raise RuntimeError("legacy migration rollback requires managed updater health verification")
        if scope != "gateway" and transaction["prior_services"]["worker"]["state"] == "running":
            self.control.release(layout, operation)
        files.finish(layout.install_root)
        return {"ok": True, "phase": "rolled-back", "receipt": receipt}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    apply_parser = commands.add_parser("apply")
    apply_parser.add_argument("--layout", type=Path, required=True)
    apply_parser.add_argument("--scope", choices=("migration", "gateway", "worker"), required=True)
    for name in ("recover", "inspect"):
        commands.add_parser(name).add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "apply":
        data, _mode = files._read_file(args.layout, private=True)
        layout = files.ExecutionLayout.from_dict(json.loads(data))
        with InstallationLock(layout.install_root):
            result = ActivationController(layout).apply(args.scope)
    else:
        transaction, layout = files._load(args.root)
        if args.command == "inspect":
            result = {"id": transaction["id"], "scope": transaction["scope"], "phase": transaction["phase"],
                      "operation_id": files.maintenance_operation_id(transaction)}
        else:
            with InstallationLock(layout.install_root):
                result = ActivationController(layout).recover()
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
