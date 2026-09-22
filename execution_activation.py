"""Split-service operations owned by install.sh's existing activation journal.

There is deliberately no independent commit, rollback, state snapshot, or lock.
The outer installer retains its authenticated health/Hub/secure-peer boundaries.
Every mutating operation requires its owned outer transaction and install lock.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import ast
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import stat
from typing import Any

import activation_transaction as activation
import execution_install as files
from execution_manage import NativeServices, WorkerControl


def environment_file(path: Path) -> dict[str, str]:
    data = activation._read_private(path, maximum=activation.MAX_CONFIG_BYTES)
    result = {}
    for line in data.decode().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
            raise ValueError("runtime environment is not a supported assignment file")
        # Generated paths are historically unquoted, including spaces. Only
        # decode explicit quoting; never evaluate shell substitutions or code.
        if value.startswith(('"', "'")):
            parsed = shlex.split(value)
            if len(parsed) != 1:
                raise ValueError("runtime environment quoting is invalid")
            value = parsed[0]
        result[key] = value
    return result


def command_layout(args: argparse.Namespace, *, environment: bool = False) -> files.ExecutionLayout:
    return files.ExecutionLayout(
        Path(args.root), Path(args.config_root), Path(args.state_root), Path(args.home), args.platform,
        Path(args.release_dir), Path(args.release_dir), args.bind, args.port,
        environment_file(Path(args.config_root) / "env") if environment else {})


def transaction(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root)
    value = activation.execution_context(root)
    if value is None:
        raise RuntimeError("outer transaction does not authorize split service changes")
    layout = command_layout(args)
    if (value["service_path"] != str(layout.service_path("worker"))
            or value["gateway_path"] != str(layout.service_path("gateway"))
            or value["env_path"] != str(layout.config_root / "env")
            or value["execution"]["runtime_dir"] != str(layout.runtime_dir)
            or value["release_dir"] != str(layout.worker_release)):
        raise RuntimeError("split service invocation differs from the outer transaction")
    assert_install_lock(root)
    return value


def assert_install_lock(root: Path) -> None:
    lock = root / ".install-lock"
    files._owned_directory(lock, private=True)
    pid_data = activation._read_owned_regular(lock / "pid", maximum=32)
    if re.fullmatch(rb"[1-9][0-9]*\n?", pid_data) is None:
        raise RuntimeError("installer lock identity is invalid")
    owner = int(pid_data)
    parent = os.getppid()
    for _ in range(8):
        if parent == owner or parent <= 1:
            break
        parent = int(subprocess.check_output(
            ["/bin/ps", "-p", str(parent), "-o", "ppid="], text=True).strip())
    if parent != owner:
        # run_without_server_secrets may introduce a short-lived shell, but
        # the helper must still descend from the exact lock-owning installer.
        raise RuntimeError("split operation is not a child of the owning installer")


def owned_args(value: dict[str, Any], root: Path) -> argparse.Namespace:
    return argparse.Namespace(root=str(root), current=str(root / "current"), previous=str(root / "previous"),
        env=value["env_path"], service=value["service_path"], release_dir=value["release_dir"],
        release_version=value["release_version"], transaction_id=value["transaction_id"])


def publish(args: argparse.Namespace, value: dict[str, Any]) -> None:
    layout = command_layout(args, environment=True)
    layout.validate()
    files.prepare_runtime(layout)
    bodies = {"service": files.render_service(layout, "worker"),
              "gateway": files.render_service(layout, "gateway"),
              "execution_layout": files._json_bytes(files.layout_manifest(layout))}
    for kind, payload in bodies.items():
        # Re-read after each publication; do not reuse stale desired coordinates.
        _directory, current = activation._read_manifest(layout.install_root)
        destination = Path(current[f"{kind}_path"])
        files._ensure_directory(destination.parent)
        source, _secured = activation._config_staging_paths(destination, current["transaction_id"], kind)
        mode = 0o644 if layout.platform == "Linux" and kind != "execution_layout" else 0o600
        desired = current[f"desired_{kind}"]
        if desired and activation._config_path_matches_desired(destination, desired):
            if activation._read_owned_regular(destination, maximum=activation.MAX_CONFIG_BYTES) != payload:
                raise RuntimeError("split candidate configuration changed during publication")
            continue
        if not source.exists() and not source.is_symlink() and desired is None:
            activation._write_new_private(source, payload)
        elif source.exists():
            if activation._read_private(source, maximum=activation.MAX_CONFIG_BYTES) != payload:
                raise RuntimeError("split candidate configuration staging changed")
        call = owned_args(current, layout.install_root)
        call.kind, call.source, call.mode = kind, str(source), oct(mode)[2:]
        activation.replace_config(call)


def verify_components(args: argparse.Namespace, value: dict[str, Any], services: NativeServices,
                      control: WorkerControl) -> None:
    # The outer installer obtained this response over its native-PID-pinned
    # connection, before transmitting the preserved administrator credential.
    health = json.loads(activation._read_private(Path(args.health_file), maximum=1024 * 1024))
    layout = command_layout(args)
    expected_worker_version = expected_gateway_version = value["release_version"]
    expected_worker_root = value["release_dir"]
    if args.command == "verify-restored":
        if not value["execution_layout"]["existed"]:
            return  # authenticated legacy health remains the outer boundary
        old_worker = activation._locate_release(value["execution"]["old_worker_release"])
        old_gateway = activation._locate_release(value["old_release"])
        if old_worker is None or old_gateway is None:
            raise RuntimeError("restored execution generations are not retained")
        expected_worker_root = str(old_worker)
        expected_worker_version = activation._read_owned_regular(old_worker / "VERSION").decode().strip()
        expected_gateway_version = activation._read_owned_regular(old_gateway / "VERSION").decode().strip()
    record = control.worker_record(layout)
    native = services.snapshot()
    gateway, worker = health.get("gateway"), health.get("execution_service")
    if (not isinstance(gateway, dict) or not isinstance(worker, dict)
            or gateway.get("version") != expected_gateway_version
            or worker.get("version") != expected_worker_version
            or gateway.get("protocol") != files.PROTOCOL_VERSION
            or worker.get("protocol") != files.PROTOCOL_VERSION
            or record.get("release_root") != expected_worker_root
            or record["pid"] != worker.get("pid")
            or record["instance_id"] != worker.get("instance_id")
            or native["worker"].get("pid") != worker.get("pid")
            or native["gateway"].get("pid") != gateway.get("pid")):
        raise RuntimeError("candidate gateway and worker do not match the paired release")
    _record, status = control.status(layout)
    lease = status.get("lease")
    if value["phase"] != "committed" and (
        not isinstance(lease, dict) or not lease.get("sealed")
        or lease.get("operation_id") != value["execution"]["operation_id"]
    ):
        raise RuntimeError("candidate worker is not held by this activation")


def candidate_api_contract(candidate: Path) -> int:
    source = activation._read_owned_regular(candidate / "agent_server.py", maximum=16 * 1024 * 1024)
    values = [node.value.value for node in ast.parse(source).body
              if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                  and target.id == "API_CONTRACT_VERSION" for target in node.targets)
              and isinstance(node.value, ast.Constant)]
    if len(values) != 1 or type(values[0]) is not int or values[0] < 1:
        raise RuntimeError("candidate API contract is not one positive literal integer")
    return values[0]


def admitted_update_id(args: argparse.Namespace, status: dict[str, Any]) -> str:
    identifier = status.get("update_id")
    if args.managed_update_id:
        if identifier != args.managed_update_id:
            raise RuntimeError("installer update identity differs from admitted status")
        return identifier
    # Old macOS detached runners did not forward this ID for ordinary updates.
    # Derive it only when this installer descends from the exact admitted runner.
    runner = status.get("runner_pid")
    if (args.platform == "Darwin" and "runner_pid" not in status
            and re.fullmatch(r"[0-9a-f]{32}", str(identifier)) is not None):
        # Pre-PID legacy runners require their full native process/session proof.
        # Never downgrade a supplied but invalid runner identity to this path.
        from execution_legacy_runner import verify_legacy_runner
        return verify_legacy_runner(args, status)
    if type(runner) is not int or runner <= 1 or re.fullmatch(r"[0-9a-f]{32}", str(identifier)) is None:
        raise RuntimeError("legacy installer has no admitted runner authority")
    parent = os.getppid()
    for _ in range(10):
        if parent == runner:
            return identifier
        if parent <= 1:
            break
        parent = int(subprocess.check_output(["/bin/ps", "-p", str(parent), "-o", "ppid="], text=True).strip())
    raise RuntimeError("legacy installer is not descended from the admitted updater")


def require_retired_legacy_intent(layout: files.ExecutionLayout, previous: Any, intent: dict) -> None:
    """Permit an old updater's carried-over intent only after proven retirement.

    This reads historical evidence without loading or executing its bootstrap.
    A retired owner's payload may predate additional bootstrap modules.
    """
    import execution_recovery as recovery
    from execution_preparation import _check_binding
    from execution_recovery_status import INTENT_KEYS

    root = layout.install_root
    def no_journal():
        for name in (".activation-transaction", ".execution-transaction", ".execution-uninstall.json"):
            if (root / name).exists() or (root / name).is_symlink():
                raise RuntimeError("a lifecycle journal still owns the previous recovery intent")
    no_journal()
    if (not isinstance(previous, dict) or set(previous) != INTENT_KEYS
            or type(previous["format"]) is not int or previous["format"] != 1
            or previous["root"] != str(root) or previous["server_identity"] != intent["server_identity"]
            or not isinstance(previous["version"], str) or not previous["version"]
            or type(previous["api_contract"]) is not int or previous["api_contract"] < 1
            or re.fullmatch(r"[0-9a-f]{32}", str(previous["update_id"])) is None
            or previous["update_id"] == intent["update_id"]):
        raise RuntimeError("previous recovery intent is not a different admitted update")
    _check_binding(root, previous["root_binding"])
    parent = root / recovery.DIRECTORY
    files._path(parent)
    if not parent.exists() and not parent.is_symlink():
        no_journal()
        return  # Pre-arm failure: no recovery owner was ever published.
    files._owned_directory(parent, private=True)
    directories = list(parent.iterdir())
    if len(directories) > 4096:
        raise RuntimeError("too many historical recovery owners")
    digest = hashlib.sha256(files._json_bytes(previous)).hexdigest()
    binding = {"format": 1, "path": str(layout.state_root / "admin/server-update.json"),
               "update_id": previous["update_id"], "target_version": previous["version"],
               "intent_sha256": digest}
    matched = 0
    for entry in directories:
        directory = recovery._directory(root, entry.name)
        owner = recovery._json(directory / "owner.json")
        if (set(owner) != recovery.OWNER_KEYS or type(owner["format"]) is not int or owner["format"] != 1
                or owner["root"] != str(root) or owner["transaction_id"] != entry.name
                or owner["expected_server_identity"] != previous["server_identity"]
                or owner["home"] != str(layout.home) or owner["platform"] != layout.platform
                or owner["state_root"] != str(layout.state_root) or owner["config_root"] != str(layout.config_root)):
            raise RuntimeError("historical recovery owner belongs to another installation")
        _check_binding(root, owner["root_binding"])
        previous_owner = (owner["managed_update_id"] == previous["update_id"]
            or (isinstance(owner["status_binding"], dict)
                and owner["status_binding"].get("update_id") == previous["update_id"]))
        if previous_owner and (owner["status_binding"] != binding
                or owner["root_binding"] != previous["root_binding"]
                or owner["source_binding"] != previous["candidate_binding"]
                or owner["managed_update_id"] != previous["update_id"]
                or owner["version"] != previous["version"] or owner["api_contract"] != previous["api_contract"]):
            raise RuntimeError("retired recovery owner differs from its admitted intent")
        payload = owner["payload"]
        required = set(recovery.PAYLOAD) - {"execution_http.py"}
        if not isinstance(payload, dict) or not required <= set(payload) <= set(recovery.PAYLOAD):
            raise RuntimeError("historical recovery bootstrap inventory is invalid")
        for name, expected in payload.items():
            if recovery._digest(directory / name) != expected:
                raise RuntimeError("historical recovery bootstrap changed")
        terminal = recovery._json(directory / "terminal.json")
        snapshot = terminal.get("snapshot")
        health = snapshot.get("health") if isinstance(snapshot, dict) else None
        final = {"format": 1, "transaction_id": entry.name,
                 "terminal_sha256": recovery._digest(directory / "terminal.json")}
        if (recovery._json(directory / "finalized.json") != final
                or recovery._json(directory / "retired.json") != final
                or set(terminal) != {"format", "transaction_id", "phase", "snapshot"}
                or type(terminal["format"]) is not int or terminal["format"] != 1
                or terminal["transaction_id"] != entry.name or terminal["phase"] != "rollback-healthy"
                or not isinstance(health, dict) or health.get("server_identity") != previous["server_identity"]):
            raise RuntimeError("previous recovery intent lacks finalized rollback proof")
        service = recovery._service_path(owner)
        if service.exists() or service.is_symlink() or recovery.RecoveryService(owner).running(service):
            raise RuntimeError("previous recovery owner is still registered or running")
        matched += int(previous_owner)
    if matched > 1:
        raise RuntimeError("previous recovery intent has no unique retired owner")
    # No matching owner is an interrupted pre-arm attempt. Every historical
    # owner above must nevertheless be terminal and absent from native jobs;
    # partial directories, unretired owners and any journal fail closed.
    no_journal()


@contextmanager
def legacy_process(args: argparse.Namespace, layout: files.ExecutionLayout,
                   services: NativeServices, control: WorkerControl):
    """Classify the incumbent without treating abandoned metadata as a process.

    A rolled-back monolith does not manage split-process receipts. Only a dead
    same-install receipt, an unheld worker lease, and native-authenticated legacy
    health allow that receipt to be ignored. Nothing here removes it or contacts
    its callback. The caller must still prove the complete legacy admission.
    """
    if files.active_worker_release(layout.install_root) is not None:
        yield False
        return
    path = layout.runtime_dir / "worker.json"
    before = services.snapshot()["worker"]
    if not path.exists() and not path.is_symlink():
        yield True
        if path.exists() or path.is_symlink() or files.active_worker_release(layout.install_root) is not None:
            raise RuntimeError("execution ownership changed during legacy admission")
        return
    if not args.health_file:
        raise RuntimeError("stale worker receipt requires authenticated legacy health")
    health_bytes = activation._read_private(Path(args.health_file), maximum=1024 * 1024)
    health = json.loads(health_bytes)
    if (not isinstance(health, dict) or any(health.get(key) is not None for key in ("execution_service", "gateway"))
            or args.expected_native_pid != before.get("pid")
            or not args.expected_server_identity or health.get("server_identity") != args.expected_server_identity):
        raise RuntimeError("worker receipt is not associated with authenticated legacy health")
    files._path(layout.runtime_dir)
    files._owned_directory(layout.runtime_dir, private=True)
    directory = layout.runtime_dir.stat()
    record = control.worker_record(layout)
    if (type(record.get("schema")) is not int or record["schema"] != 1
            or type(record.get("protocol")) is not int or record["protocol"] != files.PROTOCOL_VERSION
            or type(record.get("pid")) is not int or record["pid"] <= 1
            or re.fullmatch(r"[0-9a-f]{32}", str(record.get("instance_id"))) is None
            or not isinstance(record.get("version"), str) or not record["version"]
            or record.get("public_bind") != layout.bind
            or type(record.get("public_port")) is not int or record["public_port"] != layout.port):
        raise RuntimeError("abandoned worker receipt is invalid")
    receipt_bytes, _mode = files._read_file(path, private=True)
    receipt = path.lstat()
    lock_path = layout.runtime_dir / "worker.lock"
    fd = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        lock = os.fstat(fd)
        if (not stat.S_ISREG(lock.st_mode) or lock.st_uid != os.getuid() or lock.st_nlink != 1
                or stat.S_IMODE(lock.st_mode) != 0o600 or lock.st_size != 0):
            raise RuntimeError("abandoned worker lock is not an owned private lease")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def recheck():
            files._path(layout.runtime_dir)
            files._owned_directory(layout.runtime_dir, private=True)
            for target, expected in ((layout.runtime_dir, directory), (path, receipt), (lock_path, lock)):
                current = target.lstat()
                if (current.st_dev, current.st_ino, current.st_mode, current.st_uid, current.st_nlink, current.st_size) != (
                        expected.st_dev, expected.st_ino, expected.st_mode, expected.st_uid, expected.st_nlink, expected.st_size):
                    raise RuntimeError("abandoned worker ownership changed during admission")
            if (files._read_file(path, private=True)[0] != receipt_bytes
                    or control.worker_record(layout) != record
                    or activation._read_private(Path(args.health_file), maximum=1024 * 1024) != health_bytes
                    or files.active_worker_release(layout.install_root) is not None
                    or services.snapshot()["worker"] != before):
                raise RuntimeError("legacy worker classification changed during admission")
            try:
                os.kill(record["pid"], 0)
            except ProcessLookupError:
                pass
            else:
                raise RuntimeError("abandoned worker PID is still alive")

        recheck()
        yield True
        recheck()
    finally:
        os.close(fd)


def seed_legacy_recovery(args: argparse.Namespace, services: NativeServices) -> None:
    layout = command_layout(args)
    if services.snapshot()["worker"]["state"] != "running":
        return
    with legacy_process(args, layout, services, WorkerControl()) as legacy:
        if legacy:
            _seed_legacy_recovery(args, layout)


def _seed_legacy_recovery(args: argparse.Namespace, layout: files.ExecutionLayout) -> None:
    from update_recovery import activation_intent
    import update_runner as updates
    status_path = layout.state_root / "admin/server-update.json"
    with updates.server_update_status_lock(status_path):
        status = json.loads(activation._read_private(status_path))
        update_id = admitted_update_id(args, status)
        admitted = json.loads(activation._read_private(Path(args.update_file)))
        if (status.get("phase") not in updates.RUNNER_OWNED_ACTIVE_PHASES
                or admitted.get("update_id") != update_id
                or status.get("target_version") != args.release_version):
            raise RuntimeError("legacy admission changed before recovery intent publication")
        intent = activation_intent(root=layout.install_root, candidate=Path(args.candidate_source),
            version=args.release_version, api_contract=args.api_contract, update_id=update_id,
            server_identity=args.expected_server_identity)
        previous = status.get("_activation_recovery")
        if previous is not None and previous != intent:
            require_retired_legacy_intent(layout, previous, intent)
        updates._update_status_unlocked(status_path, status, _activation_recovery=intent)


def verify_stop(args: argparse.Namespace, value: dict[str, Any], services: NativeServices,
                control: WorkerControl) -> None:
    before = services.snapshot()["worker"]
    if before["state"] != "running":
        return
    layout = command_layout(args)
    with legacy_process(args, layout, services, control) as legacy:
        _verify_stop(args, value, services, control, layout, before, legacy)


def _verify_stop(args: argparse.Namespace, value: dict[str, Any], services: NativeServices,
                 control: WorkerControl, layout: files.ExecutionLayout, before: dict, legacy: bool) -> None:
    if not legacy:
        record, status = control.status(layout)
        lease = status.get("lease")
        if (record["pid"] != before.get("pid") or not isinstance(lease, dict)
                or lease.get("operation_id") != value["execution"]["operation_id"]
                or lease.get("sealed") is not True):
            raise RuntimeError("running worker has no exact sealed installer handoff")
        handoff = value["execution"]["handoff"]
        # The old epoch must match the server-admitted handoff. A candidate
        # epoch gets its own sealed startup lease from this exact journal.
        if handoff and record.get("release_root") != value["release_dir"] and (
            record["instance_id"] != handoff["worker_instance_id"]
            or lease.get("lease_id") != handoff["lease_id"]
        ):
            raise RuntimeError("retiring worker identity changed after admission")
    else:
        if not args.health_file:
            raise RuntimeError("running monolith requires authenticated managed idle handoff")
        health = json.loads(activation._read_private(Path(args.health_file), maximum=1024 * 1024))
        status = json.loads(activation._read_private(layout.state_root / "admin/server-update.json"))
        if not args.update_file:
            raise RuntimeError("legacy admission requires native authenticated update status")
        admitted = json.loads(activation._read_private(Path(args.update_file), maximum=1024 * 1024))
        projected = health.get("server_update")
        active_phases = {"starting", "checking", "downloading", "verifying", "installing", "restarting"}
        queued = health.get("update_blocking_queued_count")
        if queued is None:
            queued = sum(health.get("queued", {}).values())
        if (status.get("phase") not in active_phases or not isinstance(admitted, dict)
                or admitted.get("phase") not in active_phases
                or admitted.get("update_id") != status.get("update_id")
                or admitted.get("server_identity") != health.get("server_identity")
                or (health.get("server_instance_id") and admitted.get("server_instance_id") != health["server_instance_id"])
                or (projected is not None and (not isinstance(projected, dict)
                    or projected.get("update_id") != status.get("update_id")))
                or admitted_update_id(args, status) != status.get("update_id")
                or type(health.get("active_count")) is not int or health.get("active_count") != 0
                or type(queued) is not int or queued != 0
                or args.expected_native_pid != before.get("pid")
                or (args.expected_server_identity and health.get("server_identity") != args.expected_server_identity)):
            raise RuntimeError("running monolith is not in the exact admitted idle update")
        cgroup = health.get("update_service_cgroup", {})
        if (cgroup.get("safe") is not True or type(cgroup.get("unknown_descendant_count")) is not int
                or cgroup.get("unknown_descendant_count") != 0):
            raise RuntimeError("monolith process ownership is not safe for handoff")
    if services.snapshot()["worker"].get("pid") != before.get("pid"):
        raise RuntimeError("service process changed before installer stop")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("detect", "snapshot", "candidate-api-contract", "preflight", "durability", "publish", "stop", "start", "suppress", "restore", "verify", "verify-restored", "previous-worker", "release"))
    for name in ("root", "config-root", "state-root", "home", "platform", "release-dir", "bind"):
        parser.add_argument("--" + name, required=name == "root")
    parser.add_argument("--port", type=int)
    parser.add_argument("--candidate-source", default="")
    parser.add_argument("--release-version", default="")
    parser.add_argument("--api-contract", type=int, default=0)
    parser.add_argument("--health-file", default="")
    parser.add_argument("--update-file", default="")
    parser.add_argument("--handoff-file", default="")
    parser.add_argument("--managed-update-id", default="")
    parser.add_argument("--expected-native-pid", type=int, default=0)
    parser.add_argument("--expected-server-identity", default="")
    args = parser.parse_args(argv)
    if args.command == "candidate-api-contract":
        print(candidate_api_contract(Path(args.candidate_source)))
        return 0
    if args.command == "detect":
        root = files._path(Path(args.root))
        generic = root / files.TRANSACTION_NAME
        if generic.exists() or generic.is_symlink():
            raise RuntimeError("independent execution activation must be recovered first")
        outer = root / ".activation-transaction"
        if outer.exists() or outer.is_symlink():
            # Recovery must follow the journal even if layout publication had
            # not happened, or rollback has removed the candidate manifest.
            _directory, value = activation._read_manifest(root)
            print("split" if value.get("execution") else "legacy")
        elif (root / files.LAYOUT_NAME).exists() or (root / files.LAYOUT_NAME).is_symlink():
            files.active_worker_release(root)
            print("split")
        else:
            print("legacy")
        return 0
    layout = command_layout(args)
    services, control = NativeServices(layout), WorkerControl()
    if args.command == "snapshot":
        observed = services.snapshot()["gateway"]
        print(observed["state"] + "|" + ("true" if observed["enabled"] else "false"))
        return 0
    if args.command == "preflight":
        assert_install_lock(layout.install_root)
        handoff = None
        if args.handoff_file:
            from update_handoff import _read_handoff
            handoff = _read_handoff(layout.install_root, Path(args.handoff_file))
            if handoff["expected_server_identity"] != args.expected_server_identity:
                raise RuntimeError("preflight handoff server identity changed")
        # This is read-only admission before any outer snapshot/publication.
        # Stop repeats the proof from durable journal metadata immediately
        # before it touches either native job.
        value = {"release_dir":str(layout.worker_release), "execution":{
            "operation_id":handoff["operation_id"] if handoff else "",
            "handoff":handoff}}
        verify_stop(args, value, services, control)
        seed_legacy_recovery(args, services)
        return 0
    value = transaction(args)
    if args.command == "durability":
        from execution_durability import flush_activation
        flush_activation(layout.install_root, value)
        if transaction(args) != value:
            raise RuntimeError("activation journal changed during runtime durability barrier")
    elif args.command == "publish":
        publish(args, value)
    elif args.command in {"verify", "verify-restored"}:
        verify_components(args, value, services, control)
    elif args.command == "previous-worker":
        if value["execution_layout"]["existed"]:
            previous = activation._locate_release(value["execution"]["old_worker_release"])
            if previous is None:
                raise RuntimeError("previous worker generation is unavailable")
            print(previous)
    elif args.command == "stop":
        verify_stop(args, value, services, control)
        services.stop("gateway")
        services.stop("worker")
    elif args.command == "suppress":
        for role in ("gateway", "worker"):
            if services.snapshot()[role]["state"] != "absent":
                services.set_enabled(role, False)
    elif args.command == "start":
        services.reload()
        for role in ("worker", "gateway"):
            if services._observe(role)["state"] not in {"running", "transitioning"}:
                services.start(role)
    elif args.command == "restore":
        services.reload()
        for role, prior in (
            ("worker", {"state": value["service_state"], "enabled": value["service_enabled"]}),
            ("gateway", {"state": value["execution"]["gateway_state"], "enabled": value["execution"]["gateway_enabled"]}),
        ):
            if services._observe(role)["state"] != "running":
                services.restore(role, prior)
            elif prior["state"] != "absent":
                services.set_enabled(role, prior["enabled"])
    elif args.command == "release":
        if value["phase"] not in {"committed", "rollback-healthy"}:
            raise RuntimeError("admission cannot reopen before verified transaction settlement")
        if value["phase"] == "rollback-healthy" and not value["execution_layout"]["existed"]:
            return 0  # legacy monolith has no private execution lease
        if services.snapshot()["worker"]["state"] == "running":
            control.release(layout, value["execution"]["operation_id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
