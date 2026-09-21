"""Render and journal the two-service execution layout without running services.

The installer owns native service-manager commands and authenticated health
probes. This module owns deterministic configuration and recoverable filesystem
publication only. Importing it never starts, stops, or inspects a service.

``stage`` snapshots both jobs, the gateway current link, and the previous layout.
``publish`` and ``rollback`` are resumable after interruption. A caller must stop
the affected jobs before publication/restoration and pass actual probe results to
``commit``. Gateway-only operations never rewrite or stop the worker job.
The caller must hold the installation lock across these operations and native
service changes. The journal excludes another activation; it is not a substitute
for that lock when resuming an operation from another process.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
from typing import Any
import uuid


FORMAT = 1
PROTOCOL_VERSION = 1
WORKER_LABEL = "com.agentsdock.server"
GATEWAY_LABEL = "com.agentsdock.gateway"
WORKER_UNIT = "agents-server.service"
GATEWAY_UNIT = "agents-server-gateway.service"
TRANSACTION_NAME = ".execution-transaction"
LAYOUT_NAME = "execution-layout.json"
MAX_CONFIG_BYTES = 1_048_576


def maintenance_operation_id(transaction: dict[str, Any]) -> str:
    identifier = transaction.get("id")
    if not isinstance(identifier, str) or not re.fullmatch(r"execution-[0-9a-f]{24}", identifier):
        raise ValueError("invalid execution transaction identity")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "agentsdock-execution:" + identifier))


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError("configuration text must be nonempty and contain no control characters")
    return value


def _path(value: str | Path) -> Path:
    result = Path(value)
    if not result.is_absolute() or ".." in result.parts:
        raise ValueError("execution paths must be absolute without traversal")
    if result.resolve(strict=False) != result:
        raise ValueError(f"execution path contains a symlink: {result}")
    _text(str(result))
    return result


def _owned_directory(path: Path, *, private: bool = False) -> None:
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError(f"execution directory is not an owned directory: {path}")
    mode = stat.S_IMODE(info.st_mode)
    if mode & 0o022 or (private and mode != 0o700):
        raise PermissionError(f"execution directory permissions are unsafe: {path}")


def _ensure_directory(path: Path, *, private: bool = False) -> None:
    _path(path)
    if not path.exists():
        _ensure_directory(path.parent)
        path.mkdir(mode=0o700)
        _fsync_directory(path.parent)
    _owned_directory(path, private=private)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_file(path: Path, *, private: bool = False) -> tuple[bytes, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        mode = stat.S_IMODE(info.st_mode)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or mode & 0o022
                or (private and mode != 0o600) or info.st_size > MAX_CONFIG_BYTES):
            raise PermissionError(f"execution configuration is unsafe: {path}")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(MAX_CONFIG_BYTES + 1)
        current = path.lstat()
        if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise RuntimeError("execution configuration changed while reading")
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("execution configuration exceeds size limit")
        return data, mode
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    _ensure_directory(path.parent)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _release(path: Path, root: Path) -> str:
    path = _path(path)
    if path.parent != root / "releases" or path.name.startswith("."):
        raise ValueError("execution runtime must be a retained versioned release")
    _owned_directory(root)
    _owned_directory(path.parent)
    _owned_directory(path)
    data, _mode = _read_file(path / "VERSION")
    version = data.decode().strip()
    _text(version)
    if len(version) > 120:
        raise ValueError("release version is too long")
    for required in ("execution_service.py", ".venv/bin/python"):
        item = path / required
        # uv's interpreter can be a symlink into its managed Python cache.
        if not item.is_file() or (required.endswith("python") and not os.access(item, os.X_OK)):
            raise ValueError(f"execution release is incomplete: {required}")
    _read_file(path / "execution_service.py")
    return version


@dataclass(frozen=True)
class ExecutionLayout:
    install_root: Path
    config_root: Path
    state_root: Path
    home: Path
    platform: str
    worker_release: Path
    gateway_release: Path
    bind: str
    port: int
    environment: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ExecutionLayout":
        expected = {"install_root", "config_root", "state_root", "home", "platform",
                    "worker_release", "gateway_release", "bind", "port", "environment"}
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("execution layout fields are invalid")
        converted = dict(value)
        for key in ("install_root", "config_root", "state_root", "home", "worker_release", "gateway_release"):
            converted[key] = _path(converted[key])
        result = cls(**converted)
        result.validate()
        return result

    def to_dict(self) -> dict[str, Any]:
        return {"install_root": str(self.install_root), "config_root": str(self.config_root),
                "state_root": str(self.state_root), "home": str(self.home),
                "platform": self.platform, "worker_release": str(self.worker_release),
                "gateway_release": str(self.gateway_release), "bind": self.bind,
                "port": self.port, "environment": dict(self.environment)}

    @property
    def runtime_dir(self) -> Path:
        return self.state_root / "execution"

    @property
    def current(self) -> Path:
        return self.install_root / "current"

    @property
    def manifest_path(self) -> Path:
        return self.install_root / LAYOUT_NAME

    @property
    def transaction_dir(self) -> Path:
        return self.install_root / TRANSACTION_NAME

    def service_path(self, role: str) -> Path:
        if role not in {"worker", "gateway"}:
            raise ValueError("unknown execution service role")
        if self.platform == "Darwin":
            label = WORKER_LABEL if role == "worker" else GATEWAY_LABEL
            return self.home / "Library/LaunchAgents" / f"{label}.plist"
        unit = WORKER_UNIT if role == "worker" else GATEWAY_UNIT
        return self.home / ".config/systemd/user" / unit

    def validate(self) -> None:
        for path in (self.install_root, self.config_root, self.state_root, self.home):
            _path(path)
            _owned_directory(path)
        roots = (self.install_root, self.config_root, self.state_root)
        for i, first in enumerate(roots):
            for second in roots[i + 1:]:
                if first == second or first in second.parents or second in first.parents:
                    raise ValueError("execution roots must not overlap")
        if self.platform not in {"Darwin", "Linux"}:
            raise ValueError("unsupported execution service platform")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("public port must be between 1 and 65535")
        _text(self.bind)
        if not isinstance(self.environment, dict):
            raise ValueError("environment must be a string mapping")
        for key, value in self.environment.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("invalid execution environment variable name")
            if not isinstance(value, str) or any(ord(c) < 32 for c in value):
                raise ValueError("invalid execution environment value")
        _release(self.worker_release, self.install_root)
        _release(self.gateway_release, self.install_root)
        for role in ("worker", "gateway"):
            _path(self.service_path(role))
            parent = self.service_path(role).parent
            while not parent.exists():
                parent = parent.parent
            _owned_directory(parent)


def service_arguments(layout: ExecutionLayout, role: str) -> list[str]:
    runtime = layout.worker_release if role == "worker" else layout.current
    args = [str(runtime / ".venv/bin/python"), str(runtime / "execution_service.py"),
            role, "--runtime-dir", str(layout.runtime_dir), "--bind", layout.bind,
            "--port", str(layout.port)]
    if role == "worker":
        args += ["--callback-port", "0"]
    return args


def _unit_word(value: str, *, command: bool = False) -> str:
    _text(value)
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return f'"{value}"'


def _unit_directory(path: Path) -> str:
    # WorkingDirectory consumes one entire path, not shell-style words. Quotes
    # become literal filename bytes and make an absolute path invalid in systemd.
    value = _text(str(path))
    if value.strip() != value:
        raise ValueError("systemd working directory cannot have outer whitespace")
    return value.replace("%", "%%")


def render_service(layout: ExecutionLayout, role: str) -> bytes:
    layout.validate()
    if role not in {"worker", "gateway"}:
        raise ValueError("unknown execution service role")
    runtime = layout.worker_release if role == "worker" else layout.current
    environment = dict(layout.environment)
    authoritative = {"AGENTS_SERVER_INSTALL_DIR": str(layout.install_root),
                        "AGENTS_SERVER_CONFIG_DIR": str(layout.config_root),
                        "AGENTSDOCK_STATE_DIR": str(layout.state_root),
                        "AGENTS_SERVER_STATE_DIR": str(layout.state_root),
                        "AGENTSDOCK_AGENT_BIND": layout.bind,
                        "AGENTSDOCK_AGENT_PORT": str(layout.port),
                        "AGENTSDOCK_EXECUTION_ROLE": role,
                        "AGENTSDOCK_EXECUTION_RUNTIME_DIR": str(layout.runtime_dir)}
    environment.update(authoritative)
    if layout.platform == "Darwin":
        return plistlib.dumps({
            "Label": WORKER_LABEL if role == "worker" else GATEWAY_LABEL,
            "ProgramArguments": service_arguments(layout, role),
            "WorkingDirectory": str(runtime), "EnvironmentVariables": environment,
            "RunAtLoad": True, "KeepAlive": True,
            # launchd interprets zero as no automatic SIGKILL deadline. The
            # controller reports a bounded failure while keeping the journal.
            "ExitTimeOut": 0 if role == "worker" else 10,
            "Umask": 0o077,
            "StandardOutPath": str(layout.runtime_dir / "logs" / f"{role}.stdout.log"),
            "StandardErrorPath": str(layout.runtime_dir / "logs" / f"{role}.stderr.log"),
        }, sort_keys=True)
    lines = ["[Unit]", f"Description=AgentsDock {role}", "After=network-online.target"]
    if role == "gateway":
        # Wants starts a missing worker without propagating gateway stop/restart.
        lines += [f"Wants={WORKER_UNIT}", f"After={WORKER_UNIT}"]
    lines += ["", "[Service]", "Type=simple", f"WorkingDirectory={_unit_directory(runtime)}",
              f"EnvironmentFile={_unit_word(str(layout.config_root / 'env'))}"]
    for key, value in sorted(environment.items()):
        lines.append(f"Environment={_unit_word(key + '=' + value)}")
    # systemd EnvironmentFile takes precedence over Environment. Reassert only
    # layout authority with exec(1), preserving file-loaded custom provider vars.
    # env immediately execs the pinned Python; there is no shell or extra owner.
    command = ["/usr/bin/env", *[f"{key}={value}" for key, value in sorted(authoritative.items())],
               *service_arguments(layout, role)]
    lines += ["ExecStart=" + " ".join(_unit_word(arg, command=True) for arg in command),
              "Restart=always", "RestartSec=2",
              "TimeoutStopSec=180s" if role == "worker" else "TimeoutStopSec=10s",
              *(["SendSIGKILL=no"] if role == "worker" else []), "UMask=0077",
              "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines).encode()


def layout_manifest(layout: ExecutionLayout) -> dict[str, Any]:
    layout.validate()
    return {"format": FORMAT, "protocol_version": PROTOCOL_VERSION, "layout": layout.to_dict(),
            "worker_version": _release(layout.worker_release, layout.install_root),
            "gateway_version": _release(layout.gateway_release, layout.install_root),
            "runtime_dir": str(layout.runtime_dir), "socket_path": str(layout.runtime_dir / "worker.socket"),
            "token_path": str(layout.runtime_dir / "control.token"),
            "callback": {"bind": "127.0.0.1", "port": 0, "lifetime": "worker-process"},
            "retained_releases": sorted({str(layout.worker_release), str(layout.gateway_release)})}


def prepare_runtime(layout: ExecutionLayout) -> None:
    """Create private IPC state; never rotate an existing control token."""
    from execution_transport import ensure_execution_secret

    layout.validate()
    _ensure_directory(layout.runtime_dir, private=True)
    _ensure_directory(layout.runtime_dir / "logs", private=True)
    ensure_execution_secret(layout.runtime_dir / "control.token")
    _fsync_directory(layout.runtime_dir)


def _snapshot(path: Path) -> dict[str, Any]:
    try:
        data, mode = _read_file(path)
    except FileNotFoundError:
        return {"exists": False}
    return {"exists": True, "mode": mode, "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode()}


def _desired(data: bytes) -> dict[str, Any]:
    return {"exists": True, "mode": 0o600, "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode()}


def _current_target(layout: ExecutionLayout) -> str | None:
    try:
        info = layout.current.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
        raise PermissionError("execution current must be an owned release symlink")
    target = layout.current.resolve(strict=True)
    if target.parent != layout.install_root / "releases":
        raise PermissionError("execution current must point into retained releases")
    _owned_directory(target)
    return os.readlink(layout.current)


def _service_states(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"worker", "gateway"}:
        raise ValueError("both prior native service states are required")
    for item in value.values():
        if (not isinstance(item, dict) or not {"state", "enabled"} <= set(item)
                or not set(item) <= {"state", "enabled", "pid", "instance_id"}
                or item["state"] not in {"absent", "stopped", "running"}
                or type(item["enabled"]) is not bool
                or (item["state"] == "absent" and item["enabled"])):
            raise ValueError("invalid prior native service state")
        if "pid" in item and (type(item["pid"]) is not int or item["pid"] <= 0):
            raise ValueError("invalid native service process identity")
        if "instance_id" in item:
            _text(item["instance_id"])
    return value


def stage(layout: ExecutionLayout, *, scope: str, prior_services: dict[str, Any],
          expected_server_identity: str | None = None) -> dict[str, Any]:
    layout.validate()
    if expected_server_identity is not None:
        _text(expected_server_identity)
    if scope not in {"migration", "gateway", "worker"}:
        raise ValueError("unknown execution activation scope")
    _service_states(prior_services)
    if scope == "gateway" and prior_services["worker"]["state"] == "running":
        if not {"pid", "instance_id"} <= set(prior_services["worker"]):
            raise ValueError("gateway activation requires the retained worker's process identity")
    legacy = layout.install_root / ".activation-transaction"
    if legacy.exists() or legacy.is_symlink():
        raise RuntimeError("legacy activation recovery must finish before execution activation")
    if layout.transaction_dir.exists() or layout.transaction_dir.is_symlink():
        raise FileExistsError("an execution activation is already pending")
    prior_layout = _snapshot(layout.manifest_path)
    if scope == "migration":
        if prior_layout["exists"]:
            raise ValueError("execution layout already exists; migration is not an update")
    else:
        if not prior_layout["exists"]:
            raise ValueError("execution activation requires a previous layout")
        previous = json.loads(base64.b64decode(prior_layout["data"]))
        old = ExecutionLayout.from_dict(previous["layout"])
        changed = {k for k, v in old.to_dict().items() if layout.to_dict()[k] != v}
        allowed = {"gateway_release"} if scope == "gateway" else {"worker_release"}
        if not changed <= allowed:
            raise ValueError(f"{scope} activation changes another component's configuration")
        role = "worker" if scope == "gateway" else "gateway"
        if _snapshot(layout.service_path(role)) != _desired(render_service(old, role)):
            raise RuntimeError("retained service configuration no longer matches its layout")
    paths = {role: layout.service_path(role) for role in ("worker", "gateway")}
    paths["layout"] = layout.manifest_path
    snapshots = {key: _snapshot(path) for key, path in paths.items()}
    for role in ("worker", "gateway"):
        if not snapshots[role]["exists"] and prior_services[role]["state"] != "absent":
            raise ValueError("loaded native service has no restorable configuration")
    current = _current_target(layout)
    value = {"format": FORMAT, "id": "execution-" + secrets.token_hex(12), "scope": scope,
             "phase": "prepared", "layout": layout.to_dict(), "prior_services": prior_services,
             "paths": {key: str(path) for key, path in paths.items()}, "before": snapshots,
             "after": {"worker": _desired(render_service(layout, "worker")),
                       "gateway": _desired(render_service(layout, "gateway")),
                       "layout": _desired(_json_bytes(layout_manifest(layout)))},
             "current_before": current, "current_after": str(layout.gateway_release),
             "expected_server_identity": expected_server_identity,
             "receipt": None}
    # Publish a complete journal before any configuration change. An interruption
    # during preparation leaves only an unreferenced private draft, never an
    # empty pending journal that blocks a safe retry. The caller owns install-lock.
    draft = layout.install_root / (".execution-draft-" + value["id"])
    draft.mkdir(mode=0o700)
    _atomic_write(draft / "manifest.json", _json_bytes(value))
    if layout.transaction_dir.exists() or layout.transaction_dir.is_symlink():
        raise FileExistsError("an execution activation is already pending")
    os.rename(draft, layout.transaction_dir)
    _fsync_directory(layout.install_root)
    return value


def _load(root: Path) -> tuple[dict[str, Any], ExecutionLayout]:
    root = _path(root)
    _owned_directory(root)
    directory = root / TRANSACTION_NAME
    _owned_directory(directory, private=True)
    data, _mode = _read_file(directory / "manifest.json", private=True)
    value = json.loads(data)
    required = {"format", "id", "scope", "phase", "layout", "prior_services", "paths",
                "before", "after", "current_before", "current_after", "receipt", "expected_server_identity"}
    if not isinstance(value, dict) or set(value) != required or value["format"] != FORMAT:
        raise ValueError("invalid execution transaction")
    maintenance_operation_id(value)
    if value["expected_server_identity"] is not None:
        _text(value["expected_server_identity"])
    layout = ExecutionLayout.from_dict(value["layout"])
    if layout.install_root != root:
        raise ValueError("execution transaction belongs to another install root")
    if value["scope"] not in {"migration", "gateway", "worker"} or value["phase"] not in {
            "prepared", "publishing", "published", "committed", "rolling-back", "rolled-back"}:
        raise ValueError("invalid execution transaction state")
    _service_states(value["prior_services"])
    expected_paths = {role: str(layout.service_path(role)) for role in ("worker", "gateway")}
    expected_paths["layout"] = str(layout.manifest_path)
    if value["paths"] != expected_paths or value["current_after"] != str(layout.gateway_release):
        raise ValueError("execution transaction paths changed")
    expected_after = {"worker": _desired(render_service(layout, "worker")),
                      "gateway": _desired(render_service(layout, "gateway")),
                      "layout": _desired(_json_bytes(layout_manifest(layout)))}
    if value["after"] != expected_after or set(value["before"]) != set(expected_paths):
        raise ValueError("execution transaction content changed")
    for item in value["before"].values():
        if item == {"exists": False}:
            continue
        if not isinstance(item, dict) or set(item) != {"exists", "mode", "sha256", "data"}:
            raise ValueError("invalid execution snapshot")
        content = base64.b64decode(item["data"], validate=True)
        if (item["exists"] is not True or type(item["mode"]) is not int
                or not 0 <= item["mode"] <= 0o777 or item["mode"] & 0o022
                or len(content) > MAX_CONFIG_BYTES
                or hashlib.sha256(content).hexdigest() != item["sha256"]):
            raise ValueError("invalid execution snapshot content")
    before = value["current_before"]
    if before is not None:
        target = Path(before)
        if not target.is_absolute():
            target = root / target
        if target.resolve(strict=True).parent != root / "releases":
            raise ValueError("previous execution current escaped release root")
    return value, layout


def _save(value: dict[str, Any], layout: ExecutionLayout) -> None:
    _atomic_write(layout.transaction_dir / "manifest.json", _json_bytes(value))


def _affected(value: dict[str, Any]) -> tuple[str, ...]:
    if value["scope"] == "gateway":
        return "gateway", "layout"
    if value["scope"] == "worker":
        return "worker", "layout"
    return "worker", "gateway", "layout"


def _check_publication(value: dict[str, Any], layout: ExecutionLayout) -> None:
    for key in ("worker", "gateway", "layout"):
        actual = _snapshot(Path(value["paths"][key]))
        accepted = [value["before"][key]]
        if key in _affected(value):
            accepted.append(value["after"][key])
        if actual not in accepted:
            raise RuntimeError(f"{key} configuration changed outside execution activation")
    if _current_target(layout) not in (value["current_before"], value["current_after"]):
        raise RuntimeError("gateway current changed outside execution activation")


def _replace_link(layout: ExecutionLayout, target: str | None) -> None:
    if _current_target(layout) == target:
        return
    if target is None:
        layout.current.unlink()
    else:
        temporary = layout.install_root / (".execution-current-" + secrets.token_hex(12))
        temporary.symlink_to(target)
        os.replace(temporary, layout.current)
    _fsync_directory(layout.install_root)


def publish(root: Path) -> dict[str, Any]:
    value, layout = _load(root)
    if value["phase"] not in {"prepared", "publishing", "published"}:
        raise ValueError("execution activation cannot publish in this phase")
    _check_publication(value, layout)
    value["phase"] = "publishing"
    _save(value, layout)
    for key in _affected(value):
        item = value["after"][key]
        if _snapshot(Path(value["paths"][key])) != item:
            _atomic_write(Path(value["paths"][key]), base64.b64decode(item["data"]), item["mode"])
    if value["scope"] != "worker":
        _replace_link(layout, value["current_after"])
    value["phase"] = "published"
    _save(value, layout)
    return value


def rollback(root: Path) -> dict[str, Any]:
    value, layout = _load(root)
    if value["phase"] == "committed":
        raise ValueError("committed execution activation cannot roll back")
    _check_publication(value, layout)
    value["phase"] = "rolling-back"
    _save(value, layout)
    for key in _affected(value):
        path, item = Path(value["paths"][key]), value["before"][key]
        if _snapshot(path) == item:
            continue
        if item["exists"]:
            _atomic_write(path, base64.b64decode(item["data"]), item["mode"])
        else:
            path.unlink()
            _fsync_directory(path.parent)
    if value["scope"] != "worker":
        _replace_link(layout, value["current_before"])
    value["phase"] = "rolled-back"
    _save(value, layout)
    return value


def commit(root: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    """Record caller-verified component health, never infer health from files."""
    value, layout = _load(root)
    if value["phase"] != "published":
        raise ValueError("execution activation has not been published")
    _check_publication(value, layout)
    for key in _affected(value):
        if _snapshot(Path(value["paths"][key])) != value["after"][key]:
            raise RuntimeError("execution publication is incomplete")
    if _current_target(layout) != value["current_after"]:
        raise RuntimeError("gateway release link is not the candidate")
    expected = layout_manifest(layout)
    if (not isinstance(receipt, dict)
            or receipt.get("gateway_version") != expected["gateway_version"]
            or receipt.get("worker_version") != expected["worker_version"]
            or receipt.get("worker_release") != str(layout.worker_release)
            or receipt.get("protocol_version") != PROTOCOL_VERSION
            or not isinstance(receipt.get("server_identity"), str)
            or not receipt["server_identity"]
            or any(type(receipt.get(key)) is not int or receipt[key] <= 0
                   for key in ("gateway_pid", "worker_pid"))):
        raise ValueError("authenticated component health does not match the activation")
    retained_worker = value["prior_services"]["worker"]
    if (value["expected_server_identity"] is not None
            and receipt["server_identity"] != value["expected_server_identity"]):
        raise ValueError("activation changed the authenticated server identity")
    if value["scope"] == "gateway" and retained_worker["state"] == "running":
        if (receipt["worker_pid"] != retained_worker["pid"]
                or receipt.get("worker_instance_id") != retained_worker["instance_id"]):
            raise ValueError("gateway activation did not preserve the exact worker process")
    value["receipt"] = receipt
    value["phase"] = "committed"
    _save(value, layout)
    return value


def finish(root: Path) -> None:
    """Retire only a completed journal; never garbage-collect any runtime."""
    value, layout = _load(root)
    if value["phase"] not in {"committed", "rolled-back"}:
        raise ValueError("execution activation is not finished")
    _check_publication(value, layout)
    side = "after" if value["phase"] == "committed" else "before"
    for key in ("worker", "gateway", "layout"):
        expected = value[side][key] if key in _affected(value) else value["before"][key]
        if _snapshot(Path(value["paths"][key])) != expected:
            raise RuntimeError("execution terminal configuration no longer matches its receipt")
    if _current_target(layout) != value["current_" + side]:
        raise RuntimeError("execution terminal release no longer matches its receipt")
    if set(path.name for path in layout.transaction_dir.iterdir()) != {"manifest.json"}:
        raise RuntimeError("execution transaction contains unexpected files")
    retired = root / (".execution-completed-" + value["id"])
    os.rename(layout.transaction_dir, retired)
    _fsync_directory(root)
    # Keep the compact receipt as audit/recovery evidence; no active lease is lost.


def retained_releases(root: Path) -> list[str]:
    """Return all releases the active layout or an unfinished journal can need."""
    root = _path(root)
    _owned_directory(root)
    retained: set[str] = set()
    outer = root / ".activation-transaction"
    if outer.exists() or outer.is_symlink():
        from activation_transaction import execution_context
        pending = execution_context(root)
        if pending is not None:
            for release in (pending["candidate_release"], pending["old_release"],
                            pending["execution"]["old_worker_release"]):
                if release:
                    retained.update((release["source"], release["target"]))
    try:
        data, _mode = _read_file(root / LAYOUT_NAME, private=True)
    except FileNotFoundError:
        pass
    else:
        layout = ExecutionLayout.from_dict(json.loads(data)["layout"])
        if layout.install_root != root:
            raise ValueError("execution manifest belongs to another installation")
        retained.update((str(layout.worker_release), str(layout.gateway_release)))
    if (root / TRANSACTION_NAME).exists():
        value, layout = _load(root)
        retained.update((str(layout.worker_release), str(layout.gateway_release)))
        if value["current_before"]:
            target = Path(value["current_before"])
            retained.add(str((target if target.is_absolute() else root / target).resolve(strict=True)))
        previous = value["before"]["layout"]
        if previous["exists"]:
            old = ExecutionLayout.from_dict(json.loads(base64.b64decode(previous["data"]))["layout"])
            retained.update((str(old.worker_release), str(old.gateway_release)))
    return sorted(retained)


def pending_worker_operation(root: Path, runtime_root: Path) -> str | None:
    """Bind startup admission to either worker generation until finalization."""
    root, runtime_root = _path(root), _path(runtime_root)
    _owned_directory(root)
    journal = root / TRANSACTION_NAME
    outer = root / ".activation-transaction"
    uninstall = root / ".execution-uninstall.json"
    if uninstall.exists() or uninstall.is_symlink():
        if outer.exists() or outer.is_symlink() or journal.exists() or journal.is_symlink():
            raise RuntimeError("activation and uninstall both claim execution ownership")
        from execution_uninstall import pending_uninstall_operation
        return pending_uninstall_operation(root, runtime_root)
    if outer.exists() or outer.is_symlink():
        if journal.exists() or journal.is_symlink():
            raise RuntimeError("two activation journals claim execution ownership")
        from activation_transaction import pending_execution_worker_operation
        return pending_execution_worker_operation(root, runtime_root)
    if not journal.exists() and not journal.is_symlink():
        return None
    value, layout = _load(root)
    if value["scope"] == "gateway":
        return None
    retained = {layout.worker_release}
    previous = value["before"]["layout"]
    if previous["exists"]:
        old = ExecutionLayout.from_dict(json.loads(base64.b64decode(previous["data"]))["layout"])
        if old.install_root != root:
            raise ValueError("previous execution layout belongs to another installation")
        retained.add(old.worker_release)
    elif value["current_before"]:
        target = Path(value["current_before"])
        retained.add((target if target.is_absolute() else root / target).resolve(strict=True))
    if runtime_root not in retained:
        raise RuntimeError("worker runtime is outside the pending activation generations")
    _owned_directory(runtime_root)
    _read_file(runtime_root / "VERSION")
    return maintenance_operation_id(value)


def active_worker_release(root: Path) -> Path | None:
    """Resolve a validated installed worker pin without following gateway current."""
    root = _path(root)
    _owned_directory(root)
    try:
        data, _mode = _read_file(root / LAYOUT_NAME, private=True)
    except FileNotFoundError:
        return None
    value = json.loads(data)
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ValueError("installed execution layout is invalid")
    layout = ExecutionLayout.from_dict(value.get("layout"))
    if layout.install_root != root or value != layout_manifest(layout):
        raise ValueError("installed execution layout no longer matches its runtime")
    return layout.worker_release


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("validate", "manifest", "prepare-runtime", "render", "stage"):
        sub = commands.add_parser(command)
        sub.add_argument("--layout", type=Path, required=True)
        if command == "render":
            sub.add_argument("--role", choices=("worker", "gateway"), required=True)
        if command in {"render", "manifest"}:
            sub.add_argument("--output", type=Path, required=True)
        if command == "stage":
            sub.add_argument("--scope", choices=("migration", "gateway", "worker"), required=True)
            sub.add_argument("--prior-services", type=Path, required=True)
    for command in ("publish", "rollback", "commit", "finish", "retained", "inspect"):
        sub = commands.add_parser(command)
        sub.add_argument("--root", type=Path, required=True)
        if command == "commit":
            sub.add_argument("--health-receipt", type=Path, required=True)
        if command == "inspect":
            sub.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if hasattr(args, "layout"):
        data, _mode = _read_file(args.layout, private=True)
        layout = ExecutionLayout.from_dict(json.loads(data))
        if args.command == "validate":
            layout.validate()
            return
        if args.command == "render":
            _write_cli_output(args.output, render_service(layout, args.role))
            print(json.dumps({"ok": True, "operation": args.command}))
            return
        if args.command == "prepare-runtime":
            prepare_runtime(layout)
            return
        if args.command == "stage":
            states, _mode = _read_file(args.prior_services, private=True)
            result = stage(layout, scope=args.scope, prior_services=json.loads(states))
        else:
            result = layout_manifest(layout)
    elif args.command == "commit":
        data, _mode = _read_file(args.health_receipt, private=True)
        result = commit(args.root, json.loads(data))
    elif args.command == "inspect":
        result, _layout = _load(args.root)
    else:
        operation = {"publish": publish, "rollback": rollback, "finish": finish,
                     "retained": retained_releases}[args.command]
        result = operation(args.root)
    if hasattr(args, "output"):
        _write_cli_output(args.output, _json_bytes(result))
    if isinstance(result, dict):
        result = {key: result[key] for key in ("id", "scope", "phase") if key in result}
    elif args.command != "retained":
        result = None
    print(json.dumps({"ok": True, "operation": args.command, "result": result}, sort_keys=True))


def _write_cli_output(path: Path, data: bytes) -> None:
    """Private detailed output is opt-in and never sent to installer logs."""
    path = _path(path)
    if path.exists():
        _read_file(path, private=True)
    _atomic_write(path, data)


if __name__ == "__main__":
    main()
