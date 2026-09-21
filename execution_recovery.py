"""Durable native owner for one admitted two-service activation.

The installer arms this independent job before disabling either application
service. The job never invents an installation: it invokes only the retained
installer's recover-only entry point, with the exact journal and release pins.
All evidence stays private and survives service loss, process loss and reboot.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import stat
import subprocess
import sys
import time
from typing import Any

import activation_transaction as activation
import execution_install as files
from execution_manage import InstallationLock, NativeServices, WorkerControl
from execution_preparation import _binding, _check_binding


DIRECTORY = ".activation-recovery"
PAYLOAD = ("execution_recovery.py", "activation_transaction.py", "execution_install.py",
           "execution_manage.py", "execution_transport.py", "execution_preparation.py",
           "execution_recovery_status.py", "execution_http.py")
OWNER_KEYS = {"format", "root", "root_binding", "transaction_id", "version", "api_contract",
              "source_binding", "source_inventory", "home", "platform", "config_root", "state_root",
              "bind", "port", "expected_server_identity", "managed_update_id", "interpreter",
              "interpreter_sha256", "payload", "fresh", "status_binding", "expected_service_cgroup"}


def _json(path: Path) -> dict[str, Any]:
    data, _mode = files._read_file(path, private=True)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("recovery evidence is not an object")
    return value


def _write(path: Path, value: dict) -> None:
    files._atomic_write(path, files._json_bytes(value))


def _directory(root: Path, transaction: str) -> Path:
    if re.fullmatch(r"activation-[0-9a-f]{24}", transaction) is None:
        raise ValueError("invalid recovery transaction")
    root = files._path(root)
    files._owned_directory(root, private=True)
    parent = root / DIRECTORY
    files._path(parent)
    files._owned_directory(parent, private=True)
    result = parent / transaction
    files._path(result)
    files._owned_directory(result, private=True)
    return result


def _digest(path: Path) -> str:
    data = activation._read_owned_regular(path, maximum=16 * 1024 * 1024)
    return hashlib.sha256(data).hexdigest()


def _interpreter_digest(path: Path) -> str:
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("recovery interpreter must be a resolved executable")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in {0, os.getuid()}
                or info.st_mode & 0o022 or not info.st_mode & 0o111):
            raise PermissionError("recovery interpreter is not a trusted executable")
        digest = hashlib.sha256()
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
        after = os.fstat(descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
                info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise RuntimeError("recovery interpreter changed while inspected")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _source_inventory(source: Path) -> dict[str, str]:
    result = {}
    for parent, directories, names in os.walk(source, followlinks=False):
        directories[:] = [name for name in directories if name not in {".venv", "__pycache__"}]
        for name in directories:
            files._path(Path(parent) / name)
        for name in names:
            path = Path(parent) / name
            if path.suffix in {".py", ".sh", ".sql", ".pem"} or name in {
                    "VERSION", "uv.lock", "pyproject.toml", "LICENSE", "NOTICE"}:
                result[str(path.relative_to(source))] = _digest(path)
    if not {"install.sh", "VERSION", *PAYLOAD}.issubset(result):
        raise RuntimeError("retained runtime lacks required recovery sources")
    return result


def _conflicts(root: Path) -> None:
    for name in (".execution-transaction", ".execution-uninstall.json"):
        if (root / name).exists() or (root / name).is_symlink():
            raise RuntimeError("another lifecycle operation owns this installation")


def _owner(root: Path, transaction: str) -> tuple[Path, dict]:
    directory = _directory(root, transaction)
    value = _json(directory / "owner.json")
    if (set(value) != OWNER_KEYS or type(value["format"]) is not int or value["format"] != 1
            or value["root"] != str(root) or value["transaction_id"] != transaction
            or value["platform"] not in {"Darwin", "Linux"}
            or type(value["port"]) is not int or not 1 <= value["port"] <= 65535
            or type(value["api_contract"]) is not int or value["api_contract"] < 1
            or type(value["fresh"]) is not bool
            or set(value["payload"]) != set(PAYLOAD)):
        raise ValueError("recovery owner does not match its transaction")
    _check_binding(root, value["root_binding"])
    for key in ("home", "config_root", "state_root"):
        files._path(value[key])
    for name, digest in value["payload"].items():
        if _digest(directory / name) != digest:
            raise RuntimeError("recovery bootstrap source changed")
    if _interpreter_digest(Path(value["interpreter"])) != value["interpreter_sha256"]:
        raise RuntimeError("recovery interpreter changed")
    return directory, value


def _context(value: dict) -> tuple[dict, Path]:
    root = Path(value["root"])
    _conflicts(root)
    context = activation.execution_context(root)
    if (context is None or context["transaction_id"] != value["transaction_id"]
            or context["release_version"] != value["version"]
            or context["execution"]["api_contract"] != value["api_contract"]
            or context["env_path"] != str(Path(value["config_root"]) / "env")
            or context["execution"]["runtime_dir"] != str(Path(value["state_root"]) / "execution")):
        raise RuntimeError("recovery journal pins changed")
    source = activation._locate_release(context["candidate_release"], extras=(
        root / ".activation-transaction/candidate.retired",
        root / "releases" / (".activation-candidate-retired-" + value["transaction_id"])))
    if source is None:
        raise RuntimeError("exact recovery runtime is unavailable")
    _check_binding(source, value["source_binding"])
    if _source_inventory(source) != value["source_inventory"]:
        raise RuntimeError("retained recovery runtime source changed")
    return context, source


def _boot_id() -> str:
    if sys.platform == "linux":
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    if sys.platform == "darwin":
        result = subprocess.run(["/usr/sbin/sysctl", "-n", "kern.boottime"],
                                capture_output=True, text=True, check=True, timeout=5)
        match = re.search(r"sec\s*=\s*([0-9]+),\s*usec\s*=\s*([0-9]+)", result.stdout)
        if match:
            return "darwin-" + "-".join(match.groups())
    raise RuntimeError("cannot bind recovery lock to this boot")


def _process_start(pid: int) -> str | None:
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, timeout=5, check=False,
                            env={**os.environ, "LC_ALL": "C"})
    return result.stdout.strip() or None


def _ancestor(pid: int) -> bool:
    current = os.getpid()
    for _ in range(16):
        if current == pid:
            return True
        if current <= 1:
            return False
        result = subprocess.run(["/bin/ps", "-p", str(current), "-o", "ppid="],
                                capture_output=True, text=True, timeout=5, check=True)
        current = int(result.stdout.strip())
    return False


def _lock_observation(root: Path, transaction: str) -> dict:
    path = root / ".install-lock"
    files._owned_directory(path, private=True)
    data, _mode = files._read_file(path / "pid", private=True)
    if re.fullmatch(rb"[0-9]{1,20}\n?", data) is None or not _ancestor(int(data)):
        raise RuntimeError("recovery caller does not own the installer lock")
    start = _process_start(int(data))
    if not start:
        raise RuntimeError("installer process incarnation is unavailable")
    return {"format": 1, "transaction_id": transaction,
        "binding": _binding(path), "pid": int(data), "pid_sha256": hashlib.sha256(data).hexdigest(),
        "boot_id": _boot_id(), "process_start": start}


def observe_lock(root: Path, transaction: str) -> None:
    directory, _value = _owner(root, transaction)
    _write(directory / "lock.json", _lock_observation(root, transaction))


def _reap_observed_lock(root: Path, directory: Path, transaction: str) -> bool:
    """Retire only a recorded old process incarnation, never PID-only guessing."""
    path = root / ".install-lock"
    if not path.exists() and not path.is_symlink():
        return False
    saved = _json(directory / "lock.json")
    if set(saved) != {"format", "transaction_id", "binding", "pid", "pid_sha256", "boot_id", "process_start"}:
        raise ValueError("recovery lock provenance is invalid")
    if saved["format"] != 1 or saved["transaction_id"] != transaction:
        raise ValueError("recovery lock belongs to another transaction")
    try:
        _check_binding(path, saved["binding"])
    except (RuntimeError, ValueError):
        return False  # another installer owns a different lock directory
    data, _mode = files._read_file(path / "pid", private=True)
    if hashlib.sha256(data).hexdigest() != saved["pid_sha256"] or int(data) != saved["pid"]:
        return False
    if _boot_id() == saved["boot_id"] and _process_start(saved["pid"]) == saved["process_start"]:
        return False
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != (path.stat().st_dev, path.stat().st_ino):
            raise RuntimeError("recovery lock changed during inspection")
        if set(os.listdir(descriptor)) != {"pid"}:
            raise RuntimeError("recovery lock contains unknown evidence")
        _check_binding(path, saved["binding"])
        latest, _mode = files._read_file(path / "pid", private=True)
        if latest != data:
            raise RuntimeError("recovery lock owner changed")
        os.unlink("pid", dir_fd=descriptor)
        os.fsync(descriptor)
        path.rmdir()
        files._fsync_directory(root)
    finally:
        os.close(descriptor)
    return True


def _label(value: dict) -> str:
    suffix = value["transaction_id"].removeprefix("activation-")
    return ("com.agentsdock.recovery." if value["platform"] == "Darwin" else "agents-server-recovery-") + suffix


def _service_path(value: dict) -> Path:
    home = Path(value["home"])
    if value["platform"] == "Darwin":
        return home / "Library/LaunchAgents" / (_label(value) + ".plist")
    return home / ".config/systemd/user" / (_label(value) + ".service")


def render_service(directory: Path, value: dict) -> bytes:
    command = [value["interpreter"], "-B", str(directory / "execution_recovery.py"), "run",
               "--root", value["root"], "--transaction-id", value["transaction_id"]]
    environment = {"PATH": f"{value['home']}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    if value["platform"] == "Darwin":
        return plistlib.dumps({"Label": _label(value), "ProgramArguments": command,
            "WorkingDirectory": str(directory), "EnvironmentVariables": environment,
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
            "Umask": 0o077, "ExitTimeOut": 0,
            "StandardOutPath": str(directory / "native.stdout.log"),
            "StandardErrorPath": str(directory / "native.stderr.log")}, sort_keys=True)
    return ("[Unit]\nDescription=AgentsDock admitted activation recovery\nAfter=network-online.target\n"
            "[Service]\nType=simple\nUMask=0077\nRestart=on-failure\nRestartSec=30s\n"
            "TimeoutStopSec=infinity\nSendSIGKILL=no\n"
            f"WorkingDirectory={files._unit_directory(directory)}\n"
            f"Environment={files._unit_word('PATH=' + environment['PATH'])}\n"
            "ExecStart=" + " ".join(files._unit_word(item, command=True) for item in command) +
            "\n[Install]\nWantedBy=default.target\n").encode()


class RecoveryService:
    def __init__(self, value: dict):
        self.value = value

    def _command(self, arguments: list[str], *, allow_failure=False):
        result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True,
                                timeout=20, check=False)
        if result.returncode and not allow_failure:
            raise RuntimeError("native recovery service operation failed")
        return result

    def start(self, path: Path) -> None:
        if self.value["platform"] == "Linux":
            self._command(["systemctl", "--user", "daemon-reload"])
            self._command(["systemctl", "--user", "enable", "--now", path.name])
            # systemctl owns link creation, but our pre-quiesce boundary also
            # requires its exact registration to survive filesystem recovery.
            wants = files._path(path.parent / "default.target.wants")
            files._owned_directory(wants)
            link = wants / path.name
            info = link.lstat()
            if (not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid()
                    or link.resolve(strict=True) != path):
                raise RuntimeError("native recovery enablement does not target its owned unit")
            files._fsync_directory(wants)
            files._fsync_directory(path.parent)
        else:
            target = f"gui/{os.getuid()}/{_label(self.value)}"
            self._command(["/bin/launchctl", "enable", target])
            loaded = self._command(["/bin/launchctl", "print", target], allow_failure=True)
            if loaded.returncode:
                self._command(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)])
            elif re.search(rb"(?m)^\s*pid\s*=\s*[1-9][0-9]*\s*$", loaded.stdout) is None:
                # A previously loaded but stopped job needs an explicit start.
                # No -k: a concurrently restarted owner must never be killed.
                self._command(["/bin/launchctl", "kickstart", target])

    def assert_absent(self, path: Path) -> None:
        if self.value["platform"] == "Linux":
            result = self._command(["systemctl", "--user", "show", path.name,
                "--property=LoadState,ActiveState,MainPID"], allow_failure=True)
            values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
            if values != {"LoadState": "not-found", "ActiveState": "inactive", "MainPID": "0"}:
                raise RuntimeError("a native recovery job is loaded without its owned registration")
        else:
            result = self._command(["/bin/launchctl", "print", f"gui/{os.getuid()}/{_label(self.value)}"], allow_failure=True)
            if not result.returncode or b"Could not find service" not in result.stderr:
                raise RuntimeError("a native recovery job is loaded without its owned registration")

    def disable(self, path: Path) -> None:
        if self.value["platform"] == "Linux":
            self._command(["systemctl", "--user", "disable", path.name])
        else:
            self._command(["/bin/launchctl", "disable", f"gui/{os.getuid()}/{_label(self.value)}"])

    def reload(self) -> None:
        if self.value["platform"] == "Linux":
            self._command(["systemctl", "--user", "daemon-reload"])

    def running(self, path: Path) -> bool:
        if self.value["platform"] == "Linux":
            result = self._command(["systemctl", "--user", "show", path.name,
                "--property=ActiveState,MainPID"], allow_failure=True)
            values = dict(line.split("=", 1) for line in result.stdout.decode().splitlines() if "=" in line)
            if set(values) != {"ActiveState", "MainPID"} or not values["MainPID"].isdigit():
                raise RuntimeError("cannot inspect native recovery process ownership")
            return values["MainPID"] != "0" or values["ActiveState"] not in {"inactive", "failed"}
        result = self._command(["/bin/launchctl", "print", f"gui/{os.getuid()}/{_label(self.value)}"], allow_failure=True)
        if result.returncode:
            if b"Could not find service" not in result.stderr:
                raise RuntimeError("cannot inspect native recovery process ownership")
            return False
        return re.search(rb"(?m)^\s*pid\s*=\s*[1-9][0-9]*\s*$", result.stdout) is not None


def _register(directory: Path, value: dict, native=None) -> None:
    path = files._path(_service_path(value))
    files._ensure_directory(path.parent)
    service = native or RecoveryService(value)
    if path.exists() or path.is_symlink():
        if files._read_file(path)[0] != render_service(directory, value):
            raise RuntimeError("recovery native registration changed")
    else:
        service.assert_absent(path)
        files._atomic_write(path, render_service(directory, value))
    service.start(path)
    _registration_barrier(directory, value)


def _registration_barrier(directory: Path, value: dict) -> None:
    """Persist arm's later writes before either application job is quiesced."""
    path = _service_path(value)
    files._fsync_directory(directory)
    files._fsync_directory(path.parent)
    if value["platform"] != "Darwin":
        return
    # The runtime barrier precedes arm. Owner/bootstrap and plist may be on
    # different volumes, so each needs its own post-registration flush anchor.
    for anchor in (directory / "owner.json", path):
        data, mode = files._read_file(anchor, private=True)
        descriptor = os.open(anchor, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            before, linked = os.fstat(descriptor), anchor.lstat()
            if ((before.st_dev, before.st_ino) != (linked.st_dev, linked.st_ino)
                    or before.st_uid != os.getuid() or not stat.S_ISREG(before.st_mode)
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != mode):
                raise RuntimeError("native recovery flush anchor changed")
            os.fsync(descriptor)
            fcntl.fcntl(descriptor, getattr(fcntl, "F_FULLFSYNC", 51))
        finally:
            os.close(descriptor)


def arm(root: Path, source: Path, *, home: Path, platform: str, bind: str, port: int,
        expected_server_identity: str, managed_update_id: str = "", expected_service_cgroup: str = "", native=None) -> dict:
    root, source, home = map(files._path, (root, source, home))
    files._owned_directory(root, private=True)
    _conflicts(root)
    context = activation.execution_context(root)
    if context is None or context["execution"]["api_contract"] < 1:
        raise RuntimeError("only an admitted, API-pinned split transaction can arm recovery")
    transaction = context["transaction_id"]
    fresh = (not context["old_release"] and context["current"]["kind"] == "missing"
             and context["service_state"] == "absent" and context["execution"]["gateway_state"] == "absent")
    if not expected_server_identity and not fresh:
        raise RuntimeError("existing installation recovery requires its authenticated identity")
    if expected_server_identity and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", expected_server_identity) is None:
        raise ValueError("recovery server identity is invalid")
    if managed_update_id and re.fullmatch(r"[0-9a-f]{32}", managed_update_id) is None:
        raise ValueError("recovery managed operation is invalid")
    if expected_service_cgroup and re.fullmatch(r"/([A-Za-z0-9_.@:-]+/)*[A-Za-z0-9_.@:-]+", expected_service_cgroup) is None:
        raise ValueError("recovery service cgroup is invalid")
    lock = _lock_observation(root, transaction)
    files._ensure_directory(root / DIRECTORY, private=True)
    directory = root / DIRECTORY / transaction
    if directory.exists() or directory.is_symlink():
        existing_directory, existing = _owner(root, transaction)
        _context(existing)
        observe_lock(root, transaction)
        _register(existing_directory, existing, native)
        return existing
    interpreter = Path(sys.executable).resolve(strict=True)
    value = {"format": 1, "root": str(root), "root_binding": _binding(root),
        "transaction_id": transaction, "version": context["release_version"],
        "api_contract": context["execution"]["api_contract"], "source_binding": _binding(source),
        "source_inventory": _source_inventory(source), "home": str(home), "platform": platform,
        "config_root": str(Path(context["env_path"]).parent),
        "state_root": str(Path(context["execution"]["runtime_dir"]).parent), "bind": bind, "port": port,
        "expected_server_identity": expected_server_identity, "managed_update_id": managed_update_id,
        "interpreter": str(interpreter), "interpreter_sha256": _interpreter_digest(interpreter),
        "payload": {name: _digest(source / name) for name in PAYLOAD}, "fresh": fresh,
        "expected_service_cgroup": expected_service_cgroup}
    from execution_recovery_status import capture_status_binding
    value["status_binding"] = capture_status_binding(root, context, supplied_update_id=managed_update_id,
                                                     expected_server_identity=expected_server_identity)
    if value["status_binding"] is not None:
        value["managed_update_id"] = value["status_binding"]["update_id"]
    _context(value)
    layout = _layout(value)
    if str(layout.service_path("worker")) != context["service_path"]:
        raise ValueError("recovery native service directory belongs to another home")
    path = files._path(_service_path(value))
    files._ensure_directory(path.parent)
    if path.exists() or path.is_symlink():
        raise RuntimeError("another native job already uses the recovery registration")
    staging = directory.with_name("." + transaction + "." + secrets.token_hex(12) + ".tmp")
    staging.mkdir(mode=0o700)
    try:
        for name in PAYLOAD:
            data, _mode = files._read_file(source / name)
            if hashlib.sha256(data).hexdigest() != value["payload"][name]:
                raise RuntimeError("bootstrap source changed during arm")
            files._atomic_write(staging / name, data)
        _write(staging / "owner.json", value)
        _write(staging / "lock.json", lock)
        staging.rename(directory)
        files._fsync_directory(directory.parent)
    finally:
        if staging.exists():
            for child in staging.iterdir():
                child.unlink()
            staging.rmdir()
    _owner(root, transaction)
    _register(directory, value, native)
    return value


def _layout(value: dict) -> files.ExecutionLayout:
    runtime = Path(value["root"]) / "releases" / value["version"]
    return files.ExecutionLayout(Path(value["root"]), Path(value["config_root"]), Path(value["state_root"]),
        Path(value["home"]), value["platform"], runtime, runtime, value["bind"], value["port"])


def _file_proof(path: Path) -> dict:
    try:
        data, mode = files._read_file(path)
    except FileNotFoundError:
        return {"exists": False}
    return {"exists": True, "mode": mode, "sha256": hashlib.sha256(data).hexdigest()}


def _terminal_snapshot(value: dict, *, services=None, control=None) -> dict:
    layout = _layout(value)
    native = (services or NativeServices(layout)).snapshot()
    configurations = {name: _file_proof(path) for name, path in {
        "env": layout.config_root / "env", "worker": layout.service_path("worker"),
        "gateway": layout.service_path("gateway"), "layout": layout.manifest_path}.items()}
    current = layout.current
    if current.is_symlink():
        link = {"kind": "symlink", "target": os.readlink(current)}
    elif current.exists():
        link = {"kind": "directory", "binding": _binding(current)}
    else:
        link = None
    if all(item["state"] == "absent" for item in native.values()):
        if not value["fresh"] or link is not None or any(item["exists"] for item in configurations.values()):
            raise RuntimeError("absent recovery target is not the original fresh installation")
        children = layout.state_root / "admin/provider-children.json"
        if children.exists() and _json(children).get("children") != []:
            raise RuntimeError("fresh rollback retains unresolved provider ownership")
        state_lock = layout.state_root / "admin/state-owner.lock"
        if state_lock.exists() or state_lock.is_symlink():
            files._read_file(state_lock, private=True)
            descriptor = os.open(state_lock, os.O_RDWR | os.O_NOFOLLOW)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        health = None
    else:
        if native["worker"]["state"] != "running":
            raise RuntimeError("recovered native worker is not healthy")
        control = control or WorkerControl()
        raw = control.health(layout)
        identity = raw.get("server_identity")
        if (raw.get("ok") is not True or not isinstance(identity, str) or not identity
                or (value["expected_server_identity"] and identity != value["expected_server_identity"])):
            raise RuntimeError("recovered server identity is not authenticated")
        identity_file, _mode = files._read_file(layout.state_root / "server-identity")
        if identity_file.decode().strip() != identity:
            raise RuntimeError("recovered server does not own the persistent identity")
        worker, gateway = raw.get("execution_service"), raw.get("gateway")
        if configurations["layout"]["exists"]:
            if (not isinstance(worker, dict) or not isinstance(gateway, dict)
                    or worker.get("pid") != native["worker"].get("pid")
                    or gateway.get("pid") != native["gateway"].get("pid")
                    or worker.get("maintenance_held") is not False):
                raise RuntimeError("recovered worker and gateway are not both active and released")
        health = {"server_identity": identity, "server_version": raw.get("server_version"),
                  "api_contract_version": raw.get("api_contract_version"),
                  "worker_version": worker.get("version") if isinstance(worker, dict) else None,
                  "gateway_version": gateway.get("version") if isinstance(gateway, dict) else None}
    return {"configurations": configurations, "current": link, "health": health,
            "services": {role: {"state": item["state"], "enabled": item["enabled"]}
                         for role, item in native.items()}}


def complete(root: Path, transaction: str, *, services=None, control=None) -> None:
    directory, value = _owner(root, transaction)
    observe_lock(root, transaction)
    context, _source = _context(value)
    if context["phase"] not in {"committed", "rollback-healthy"}:
        raise RuntimeError("activation has not crossed a verified terminal boundary")
    snapshot = _terminal_snapshot(value, services=services, control=control)
    if context["phase"] == "committed":
        health = snapshot["health"]
        if (not health or health["server_version"] != value["version"]
                or health["api_contract_version"] != value["api_contract"]
                or health["worker_version"] != value["version"] or health["gateway_version"] != value["version"]):
            raise RuntimeError("committed recovery target does not match the paired release")
    latest, _source = _context(value)
    if latest != context:
        raise RuntimeError("activation changed while recording its terminal proof")
    proof = {"format": 1, "transaction_id": transaction, "phase": context["phase"], "snapshot": snapshot}
    target = directory / "terminal.json"
    if target.exists() and _json(target) != proof:
        raise RuntimeError("a different terminal recovery proof already exists")
    _write(target, proof)


def finalized(root: Path, transaction: str, *, services=None, control=None) -> None:
    directory, value = _owner(root, transaction)
    observe_lock(root, transaction)
    _conflicts(root)
    if (root / ".activation-transaction").exists() or (root / ".activation-transaction").is_symlink():
        raise RuntimeError("activation journal has not been retired")
    proof = _json(directory / "terminal.json")
    if (set(proof) != {"format", "transaction_id", "phase", "snapshot"} or proof["format"] != 1
            or proof["transaction_id"] != transaction or proof["phase"] not in {"committed", "rollback-healthy"}
            or proof["snapshot"] != _terminal_snapshot(value, services=services, control=control)):
        raise RuntimeError("terminal recovery proof no longer matches the installed services")
    _write(directory / "finalized.json", {"format": 1, "transaction_id": transaction,
                                          "terminal_sha256": _digest(directory / "terminal.json")})


def _retire(directory: Path, value: dict, native=None) -> bool:
    proof = _json(directory / "finalized.json")
    if proof != {"format": 1, "transaction_id": value["transaction_id"],
                 "terminal_sha256": _digest(directory / "terminal.json")}:
        raise RuntimeError("recovery finalization proof is invalid")
    from execution_recovery_status import settle_status
    if not settle_status(value, _json(directory / "terminal.json")):
        return False  # An exact live updater still owns completion reporting.
    path = _service_path(value)
    service = native or RecoveryService(value)
    # Record authority before removing registration: a crash after unlink must
    # not leave an unidentifiable job that blocks uninstall forever. Readers
    # also require the registration absent and native process stopped.
    _write(directory / "retired.json", proof)
    if path.exists() or path.is_symlink():
        if files._read_file(path)[0] != render_service(directory, value):
            raise RuntimeError("refusing to remove a changed recovery native job")
        service.disable(path)
        path.unlink()
        files._fsync_directory(path.parent)
        service.reload()
    return True


def inspect_owner(root: Path, transaction: str) -> dict | None:
    """Read-only join authority; old journals without an owner return None."""
    root = files._path(root)
    directory = root / DIRECTORY / transaction
    if not directory.exists() and not directory.is_symlink():
        return None
    directory, value = _owner(root, transaction)
    terminal = (directory / "finalized.json").exists()
    retired = (directory / "retired.json").exists()
    if terminal:
        final = _json(directory / "finalized.json")
        expected = {"format": 1, "transaction_id": transaction,
                    "terminal_sha256": _digest(directory / "terminal.json")}
        if final != expected or (retired and _json(directory / "retired.json") != expected):
            raise RuntimeError("native recovery retirement evidence changed")
    elif retired:
        raise RuntimeError("native recovery retirement lacks finalization proof")
    journal = root / ".activation-transaction"
    if journal.exists() or journal.is_symlink():
        context = activation.execution_context(root)
        if context and context["transaction_id"] == transaction:
            if terminal:
                raise RuntimeError("finalized recovery owner conflicts with its retained journal")
            _context(value)
    if not retired and files._read_file(_service_path(value))[0] != render_service(directory, value):
        raise RuntimeError("recovery native registration changed")
    result = {"transaction_id": transaction, "finalized": terminal, "retired": retired,
              "service_path": str(_service_path(value))}
    failure = directory / "failure.json"
    if failure.exists():
        result["failure"] = _json(failure)
    return result


def _observed_installer_is_live(root: Path, directory: Path, transaction: str) -> bool:
    """A busy API retry may join only the exact installer recorded at arm."""
    path = root / ".install-lock"
    saved = _json(directory / "lock.json")
    if (set(saved) != {"format", "transaction_id", "binding", "pid", "pid_sha256", "boot_id", "process_start"}
            or saved["format"] != 1 or saved["transaction_id"] != transaction):
        raise RuntimeError("recovery lock provenance is invalid")
    try:
        _check_binding(path, saved["binding"])
        data, _mode = files._read_file(path / "pid", private=True)
    except (FileNotFoundError, RuntimeError, ValueError):
        return False
    return (hashlib.sha256(data).hexdigest() == saved["pid_sha256"] and int(data) == saved["pid"]
            and _boot_id() == saved["boot_id"] and bool(saved["process_start"])
            and _process_start(saved["pid"]) == saved["process_start"])


def resume_owner(root: Path, transaction: str, *, native=None) -> dict | None:
    """Join or finish registering one existing owner; never invent a recovery.

    Invoke off the server event loop. Registration changes use the same install
    lock as activation; a verified live installer is joined without waiting.
    """
    root = files._path(root)
    directory = root / DIRECTORY / transaction
    if not directory.exists() and not directory.is_symlink():
        return None
    directory, value = _owner(root, transaction)
    _context(value)
    if (directory / "finalized.json").exists() or (directory / "retired.json").exists():
        raise RuntimeError("terminal recovery owner cannot resume a retained journal")
    path = _service_path(value)
    if path.exists() or path.is_symlink():
        if files._read_file(path)[0] != render_service(directory, value):
            raise RuntimeError("recovery native registration changed")
    _reap_observed_lock(root, directory, transaction)
    lock = InstallationLock(root)
    try:
        lock.__enter__()
    except RuntimeError as error:
        if (str(error) == "another installer owns the installation lock"
                and _observed_installer_is_live(root, directory, transaction)):
            # arm may not have written its registration yet. The exact live
            # installer still owns that boundary; do not claim a job is active.
            return {"transaction_id": transaction, "finalized": False, "retired": False,
                    "service_path": str(path), "joining": True}
        raise
    try:
        _context(value)
        _register(directory, value, native)
        return {**inspect_owner(root, transaction), "joining": True}
    finally:
        lock.__exit__(None, None, None)


def require_retired_owners(root: Path) -> None:
    """Called under the install lock before removing any runtime/bootstrap."""
    parent = root / DIRECTORY
    if not parent.exists() and not parent.is_symlink():
        return
    files._path(parent)
    files._owned_directory(parent, private=True)
    for directory in parent.iterdir():
        result = inspect_owner(root, directory.name)
        if result is None or result["retired"] is not True:
            raise RuntimeError("a durable activation recovery owner has not retired")
        _directory_path, value = _owner(root, directory.name)
        path = _service_path(value)
        if path.exists() or path.is_symlink() or RecoveryService(value).running(path):
            raise RuntimeError("a durable activation recovery job is still registered or running")


def run_once(root: Path, transaction: str, *, native=None, installer=subprocess.run,
             services=None, control=None) -> bool:
    directory, value = _owner(root, transaction)
    if (directory / "finalized.json").exists():
        _reap_observed_lock(root, directory, transaction)
        with InstallationLock(root):
            return _retire(directory, value, native)
    _reap_observed_lock(root, directory, transaction)
    with InstallationLock(root):
        _conflicts(root)
        if not (root / ".activation-transaction").exists():
            finalized(root, transaction, services=services, control=control)
            return _retire(directory, value, native)
        context, source = _context(value)
    # The child owns the same shared lock and checks this exact transaction
    # again. Never hold it in the parent while launching an installer.
    command = ["/bin/bash", str(source / "install.sh"), "--recover-only", "--non-interactive",
        "--release-version", value["version"], "--expected-api-contract", str(value["api_contract"]),
        "--expected-activation-id", transaction, "--expected-server-identity", value["expected_server_identity"],
        "--port", str(value["port"]), "--bind", value["bind"]]
    if value["managed_update_id"]:
        command += ["--managed-update-id", value["managed_update_id"]]
    if value["expected_service_cgroup"]:
        command += ["--expected-service-cgroup", value["expected_service_cgroup"]]
    environment = {name: content for name, content in os.environ.items() if name in {
        "HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "TMPDIR"}}
    environment.update(AGENTS_SERVER_INSTALL_DIR=value["root"], AGENTS_SERVER_CONFIG_DIR=value["config_root"],
                       AGENTSDOCK_STATE_DIR=value["state_root"])
    descriptor = os.open(directory / "installer.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        os.close(descriptor)
        raise PermissionError("recovery installer log is unsafe")
    with os.fdopen(descriptor, "ab") as output:
        result = installer(command, cwd=source, env=environment, stdin=subprocess.DEVNULL,
                           stdout=output, stderr=subprocess.STDOUT, check=False)
    if result.returncode not in {0, 75}:
        raise RuntimeError("retained installer recovery did not complete")
    with InstallationLock(root):
        if not (directory / "finalized.json").exists():
            finalized(root, transaction, services=services, control=control)
        return _retire(directory, value, native)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("arm", "observe-lock", "complete", "finalized", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--transaction-id")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--home", type=Path)
    parser.add_argument("--platform", choices=("Darwin", "Linux"))
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=17850)
    parser.add_argument("--expected-server-identity", default="")
    parser.add_argument("--managed-update-id", default="")
    parser.add_argument("--expected-service-cgroup", default="")
    args = parser.parse_args(argv)
    try:
        root = files._path(args.root)
        if args.operation == "arm":
            if args.source is None or args.home is None or args.platform is None:
                raise ValueError("arm requires exact source, home and platform")
            value = arm(root, args.source, home=args.home, platform=args.platform, bind=args.bind, port=args.port,
                expected_server_identity=args.expected_server_identity, managed_update_id=args.managed_update_id,
                expected_service_cgroup=args.expected_service_cgroup)
            print(value["interpreter"])
            print(root / DIRECTORY / value["transaction_id"] / "execution_recovery.py")
        elif args.operation != "run":
            {"observe-lock": observe_lock, "complete": complete, "finalized": finalized}[args.operation](root, args.transaction_id)
        else:
            while True:
                try:
                    if run_once(root, args.transaction_id):
                        # No installer remains alive now. Only the dedicated
                        # recovery job can be unloaded; worker/gateway untouched.
                        _directory_path, value = _owner(root, args.transaction_id)
                        if value["platform"] == "Darwin":
                            os.execv("/bin/launchctl", ["launchctl", "bootout", f"gui/{os.getuid()}/{_label(value)}"])
                        return 0
                except RuntimeError as error:
                    if "another installer owns" not in str(error):
                        raise
                time.sleep(2)
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        if args.operation == "run":
            try:
                directory, value = _owner(root, args.transaction_id)
                _write(directory / "failure.json", {"transaction_id": args.transaction_id,
                    "error_type": type(error).__name__, "message": str(error), "recorded_at": time.time()})
            except Exception:
                pass  # Invalid provenance must never be repaired here.
        print("Activation recovery retained its evidence: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
