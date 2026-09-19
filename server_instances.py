#!/usr/bin/env python3
"""Current-user instance bindings and manager. No server import or discovery writes.

The registry contains names, not caller-controlled deletion paths or credentials.
Named roots are siblings of the legacy roots: removing default cannot remove them.
Services are independently configured, but are NOT a same-user security sandbox.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import time

INSTANCE_PROTOCOL = 1
ROOT = Path(__file__).resolve().parent


def instance_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value):
        raise ValueError("Instance names must be 1–32 lowercase letters, digits or dashes, starting with a letter.")
    return value


def service_name(name: str) -> str:
    return "agents-server" + ("" if instance_name(name) == "default" else "-" + name)


def launchd_label(name: str) -> str:
    return "com.agentsdock.server" + ("" if instance_name(name) == "default" else "." + name)


@dataclass(frozen=True)
class Instance:
    name: str
    home: Path

    def __post_init__(self):
        instance_name(self.name)

    @property
    def runtime(self) -> Path:
        return self.home / ".local/share" / ("agents-server" if self.name == "default" else f"agents-server-instances/{self.name}")

    @property
    def config(self) -> Path:
        return self.home / ".config" / ("agents-server" if self.name == "default" else f"agents-server-instances/{self.name}")

    @property
    def state(self) -> Path:
        return self.home / (".agentsdock" if self.name == "default" else f".agentsdock-instances/{self.name}")

    @property
    def logs(self) -> Path:
        return self.home / "Library/Logs" / ("AgentsServer" if self.name == "default" else f"AgentsServer-instances/{self.name}")

    def service_file(self, platform: str = sys.platform) -> Path:
        if platform == "darwin":
            return self.home / "Library/LaunchAgents" / (launchd_label(self.name) + ".plist")
        if platform.startswith("linux"):
            return self.home / ".config/systemd/user" / (service_name(self.name) + ".service")
        raise ValueError("Only macOS and Linux user services are supported.")

    def environment(self) -> dict[str, str]:
        return {
            "AGENTS_SERVER_INSTANCE": self.name,
            "AGENTS_SERVER_INSTALL_DIR": str(self.runtime),
            "AGENTS_SERVER_CONFIG_DIR": str(self.config),
            "AGENTSDOCK_STATE_DIR": str(self.state),
        }

    def shell_bindings(self) -> str:
        values = {
            "INSTALL_ROOT": str(self.runtime), "CONFIG_ROOT": str(self.config),
            "STATE_ROOT": str(self.state), "INSTANCE_LOG_DIR": str(self.logs),
            "SERVICE_NAME": service_name(self.name), "LABEL": launchd_label(self.name),
        }
        return "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


def check_path(path: Path, home: Path) -> None:
    """Reject symlinks, foreign owners and unsafe writable parents before mutation."""
    path.relative_to(home)
    if path == home:
        raise ValueError("Refusing a home-directory target.")
    for parent in (path, *path.parents):
        if parent == home.parent:
            break
        try:
            info = parent.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"Unsafe managed path (link, owner or permissions): {parent}")


def read_regular(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"Unsafe managed file: {path}")
        data = handle.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise ValueError(f"Managed file too large: {path}")
        return data


def read_config(instance: Instance) -> dict[str, str]:
    try:
        text = read_regular(instance.config / "env").decode()
    except FileNotFoundError:
        return {}
    result = {}
    for line in text.splitlines():
        key, separator, value = line.partition("=")
        if separator and re.fullmatch(r"[A-Z_]+", key):
            values = shlex.split(value, comments=False)
            result[key] = values[0] if len(values) == 1 else value
    return result


def validate_binding(instance: Instance, platform: str = sys.platform) -> None:
    for path in (instance.runtime, instance.config, instance.state, instance.logs, instance.service_file(platform)):
        check_path(path, instance.home)
    env = read_config(instance)
    for key, value in instance.environment().items():
        if key in env and env[key] != value:
            raise ValueError(f"{instance.name}: {key} does not match this instance; refusing service changes.")
    service = instance.service_file(platform)
    if service.exists():
        data = read_regular(service)
        if platform == "darwin":
            job = plistlib.loads(data)
            if job.get("Label") != launchd_label(instance.name):
                raise ValueError("Service label does not match instance.")
            job_env = job.get("EnvironmentVariables", {})
            # Legacy default plists did not set CONFIG_DIR / INSTANCE.
            for key, value in instance.environment().items():
                if job_env.get(key, value if instance.name == "default" else None) != value:
                    raise ValueError(f"Service {key} does not match instance.")
            args = job.get("ProgramArguments", [])
            if args[:2] != [str(instance.runtime / "current/.venv/bin/python"), str(instance.runtime / "current/agent_server.py")]:
                raise ValueError("Service executable does not match instance.")
        else:
            lines = data.decode().splitlines()
            if f"EnvironmentFile={instance.config / 'env'}" not in lines or not any(
                line.startswith(f"ExecStart={instance.runtime / 'current/.venv/bin/python'} {instance.runtime / 'current/agent_server.py'} serve ")
                for line in lines
            ):
                raise ValueError("Service runtime/configuration does not match instance.")


class Registry:
    def __init__(self, home: Path | None = None):
        self.home = (home or Path.home()).resolve()
        self.root = self.home / ".config/agents-server-manager"
        self.file = self.root / "instances.json"

    def records(self) -> dict:
        check_path(self.file, self.home)
        try:
            value = json.loads(read_regular(self.file))
        except FileNotFoundError:
            return {}
        if not isinstance(value, dict) or value.get("version") != INSTANCE_PROTOCOL or not isinstance(value.get("instances"), dict):
            raise ValueError("Invalid instance registry; refusing to guess removal targets.")
        for name, record in value["instances"].items():
            instance_name(name)
            if not isinstance(record, dict) or set(record) != {"status"} or record["status"] not in {"pending", "installed", "removed", "failed"}:
                raise ValueError("Invalid instance record.")
        return value["instances"]

    def instances(self, include_removed: bool = False) -> list[Instance]:
        records = self.records()
        names = {name for name, record in records.items() if include_removed or record["status"] != "removed"}
        default = Instance("default", self.home)
        if default.service_file().exists() or (default.config / "env").exists():
            names.add("default")
        # Direct --instance installs are discovered only in fixed owned roots.
        configs = self.home / ".config/agents-server-instances"
        check_path(configs, self.home)
        if configs.exists():
            for child in configs.iterdir():
                instance_name(child.name)
                check_path(child, self.home)
                if (child / "env").is_file():
                    names.add(child.name)
        return [Instance(name, self.home) for name in sorted(names, key=lambda name: (name != "default", name))]

    @contextmanager
    def locked(self):
        check_path(self.root, self.home)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_lock(self.root / "operation.lock"):
            yield

    def save(self, instance: Instance, status: str):
        if status not in {"pending", "installed", "removed", "failed"}:
            raise ValueError("Invalid lifecycle status.")
        records = self.records()
        records[instance.name] = {"status": status}
        fd, name = tempfile.mkstemp(prefix=".instances-", dir=self.root)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump({"version": INSTANCE_PROTOCOL, "instances": records}, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, self.file)
        finally:
            if os.path.exists(name):
                os.unlink(name)


@contextmanager
def exclusive_lock(path: Path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_mode & 0o077:
            raise ValueError(f"Unsafe lock file: {path}")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError(f"Another process owns {path}; no changes made.") from exc
        yield
    finally:
        os.close(fd)  # Never unlink: waiters must keep the same lock inode.


def acquire_state_lock(state: Path):
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = exclusive_lock(state / ".server-process.lock")
    lock.__enter__()
    return lock  # The serving process retains this object until exit.


def validate_runtime_environment(environment: dict[str, str], home: Path) -> None:
    name = instance_name(environment.get("AGENTS_SERVER_INSTANCE", "default"))
    if name == "default":
        return  # Existing custom-root default deployments remain supported.
    instance = Instance(name, home.resolve())
    for key, expected in instance.environment().items():
        if environment.get(key) != expected:
            raise ValueError(f"Named instance {name}: {key} must match its isolated binding.")


def clean_environment(instance: Instance) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENTSDOCK_", "AGENTS_SERVER_", "ZENITHBOT_AGENT_", "ZENITHDOCK_"))}
    env.update(instance.environment())
    if instance.name != "default":
        env["AGENTSDOCK_SERVER_NAME"] = instance.name
    return env


def run(command: list[str], **kwargs):
    return subprocess.run(command, check=True, **kwargs)


def service_status(instance: Instance, platform: str = sys.platform) -> str:
    command = ["launchctl", "print", f"gui/{os.getuid()}/{launchd_label(instance.name)}"] if platform == "darwin" else ["systemctl", "--user", "show", service_name(instance.name) + ".service", "--property=ActiveState", "--value"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if platform == "darwin":
        if result.returncode == 0:
            return "running" if re.search(r"\bpid = [1-9][0-9]*", result.stdout) else "loaded"
        return "stopped" if "Could not find service" in result.stderr else "unknown"
    return "running" if result.returncode == 0 and result.stdout.strip() == "active" else "stopped" if result.returncode == 0 else "unknown"


def port_available(port: int) -> bool:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    for family, address in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as listener:
                if family == socket.AF_INET6:
                    listener.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                listener.bind((address, port))
        except OSError as exc:
            import errno
            if family == socket.AF_INET6 and exc.errno in {errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT, errno.EADDRNOTAVAIL}:
                continue
            return False
    return True


def select_port(registry: Registry, explicit: int | None, extra_reserved: set[int] | None = None) -> int:
    reserved = {int(read_config(item).get("AGENTSDOCK_AGENT_PORT", "7850")) for item in registry.instances()}
    reserved.update(extra_reserved or ())
    candidates = [explicit] if explicit is not None else range(7851, 65536)
    for port in candidates:
        if port not in reserved and port_available(port):
            return port
    raise ValueError("Requested port is occupied/reserved, or no free port is available. No existing listener was stopped.")


def candidate_addresses(bind: str, port: int) -> list[str]:
    try:
        address = ipaddress.ip_address(bind.strip("[]"))
    except ValueError:
        if bind == "localhost":
            return [f"http://localhost:{port} (This machine only)"]
        return [f"http://{bind}:{port}"]
    if not address.is_unspecified:
        host = f"[{address}]" if address.version == 6 else str(address)
        return [f"http://{host}:{port}" + (" (This machine only)" if address.is_loopback else "")]
    hosts = set()
    try:
        hosts.update(row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET if address.version == 4 else socket.AF_INET6))
        if sys.platform == "darwin":
            output = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3, check=False).stdout
            hosts.update(re.findall(r"\binet " + r"(\d+\.\d+\.\d+\.\d+)", output))
        elif sys.platform.startswith("linux"):
            output = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3, check=False).stdout
            hosts.update(output.split())
    except (OSError, subprocess.TimeoutExpired):
        pass
    urls = [f"http://127.0.0.1:{port}" if address.version == 4 else f"http://[::1]:{port}"]
    for host in sorted(hosts):
        try:
            item = ipaddress.ip_address(host)
        except ValueError:
            continue
        if item.version != address.version or item.is_loopback or item.is_link_local or item.is_unspecified:
            continue
        urls.append(f"http://{'[' + host + ']' if item.version == 6 else host}:{port}")
    return urls


def describe(instance: Instance) -> dict:
    env = read_config(instance)
    port = int(env.get("AGENTSDOCK_AGENT_PORT", "7850")) if env else None
    try:
        version = (instance.runtime / "current/VERSION").read_text().strip()[:80]
    except OSError:
        version = "not installed"
    return {
        "name": instance.name, "status": service_status(instance), "port": port,
        "addresses": candidate_addresses(env.get("AGENTSDOCK_AGENT_BIND", "0.0.0.0"), port) if port else [],
        "runtime": str(instance.runtime), "state": str(instance.state),
        "service": str(instance.service_file()),
        "version": version,
    }


def show(instance: Instance):
    item = describe(instance)
    print(f"{item['name']:<20} {item['status']:<10} {str(item['port'] or '—'):<6} " + "  ".join(item["addresses"]))


def confirm_removal(instances: list[Instance], purge: bool, yes: bool) -> None:
    color = sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ
    red, reset = ("\033[1;31m", "\033[0m") if color else ("", "")
    print(f"{red}WARNING: uninstall {len(instances)} AgentsServer instance(s){reset}")
    for instance in instances:
        show(instance)
        print(f"  Remove runtime: {instance.runtime}\n  Remove configuration/token: {instance.config}\n  {'PERMANENTLY DELETE' if purge else 'PRESERVE'} history: {instance.state}")
    print("Services and tokens will be removed; reinstalling creates a new access token.")
    if purge:
        print(f"{red}PERMANENT HISTORY DELETION CANNOT BE UNDONE. --yes cannot bypass confirmation.{reset}")
    else:
        print("Chat history and files are preserved. Service removal is reinstallable; deleted configuration is not restored automatically.")
    expected = f"{'DELETE HISTORY' if purge else 'UNINSTALL'} {len(instances)}"
    if purge or not yes:
        if not sys.stdin.isatty() or input(f"Type {expected!r} to confirm: ") != expected:
            raise ValueError("Not confirmed; nothing was uninstalled.")


def install_instance(instance: Instance, port: int, bind: str):
    validate_binding(instance)
    command = ["/bin/bash", str(ROOT / "install.sh"), "--instance", instance.name,
               "--non-interactive", "--no-port-fallback", "--port", str(port), "--bind", bind]
    run(command, env=clean_environment(instance), cwd=ROOT)


def control(instance: Instance, action: str, platform: str = sys.platform):
    validate_binding(instance, platform)
    if not instance.service_file(platform).exists():
        raise ValueError(f"{instance.name}: no installed service.")
    if platform == "darwin":
        target = f"gui/{os.getuid()}/{launchd_label(instance.name)}"
        status = service_status(instance, platform)
        if status == "unknown":
            raise ValueError("Cannot establish service state; no service changes made.")
        if action == "stop":
            if status != "stopped":
                run(["launchctl", "bootout", target])
        elif status == "stopped":
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(instance.service_file(platform))])
        elif action == "restart":
            run(["launchctl", "bootout", target])
            deadline = time.monotonic() + 180
            while service_status(instance, platform) != "stopped":
                if time.monotonic() >= deadline:
                    raise ValueError("Service did not stop; refusing to start a second process.")
                time.sleep(0.1)
            run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(instance.service_file(platform))])
    else:
        run(["systemctl", "--user", action, service_name(instance.name) + ".service"])


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Independent AgentsServer instances for the current OS user. Defaults to list.")
    commands = result.add_subparsers(dest="command")
    commands.add_parser("list")
    info = commands.add_parser("info")
    info.add_argument("name", type=instance_name)
    new = commands.add_parser("new")
    new.add_argument("--name", type=instance_name)
    new.add_argument("--port", type=int)
    new.add_argument("--bind", default="0.0.0.0")
    for action in ("start", "stop", "restart", "update", "remove"):
        command = commands.add_parser(action)
        command.add_argument("name", nargs="?", type=instance_name)
        command.add_argument("--instance", dest="named", type=instance_name)
        command.add_argument("--all", action="store_true")
        command.add_argument("--exclude", action="append", default=[], type=instance_name)
        if action == "remove":
            command.add_argument("--yes", action="store_true")
            command.add_argument("--purge-state", action="store_true")
    install = commands.add_parser("install")
    install.add_argument("--manifest", type=Path, required=True)
    # Internal shared bindings; pure read-only output, safely shell-quoted.
    bindings = commands.add_parser("_bindings", help=argparse.SUPPRESS)
    bindings.add_argument("name", type=instance_name)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    registry = Registry()
    try:
        if args.command == "_bindings":
            instance = Instance(args.name, registry.home)
            validate_binding(instance)
            print(instance.shell_bindings())
            return 0
        if args.command in {None, "list", "info"}:
            items = registry.instances()
            if args.command == "info":
                items = [item for item in items if item.name == args.name]
                if not items:
                    raise ValueError("Unknown instance.")
                print(json.dumps(describe(items[0]), indent=2))
            else:
                print("NAME                 STATUS     PORT   CONNECTION URLS (reachability depends on network/firewall)")
                for item in items:
                    show(item)
                if not items:
                    print("No installations found. Run ./install.sh for default, or ./instances.sh new.")
            return 0
        with registry.locked():
            existing = {item.name: item for item in registry.instances(include_removed=True)}
            if args.command in {"new", "install"}:
                entries = [{"name": args.name, "port": args.port, "bind": args.bind}] if args.command == "new" else json.loads(args.manifest.read_text())
                if not isinstance(entries, list) or not entries:
                    raise ValueError("Manifest must be a nonempty JSON array of {name, port?, bind?} objects.")
                plan = []
                names, ports = set(existing), set()
                for entry in entries:
                    if not isinstance(entry, dict) or set(entry) - {"name", "port", "bind"}:
                        raise ValueError("Invalid manifest entry.")
                    name = entry.get("name")
                    if name is None:
                        index = 1
                        while f"instance-{index}" in names:
                            index += 1
                        name = f"instance-{index}"
                    if name == "default" or name in names:
                        raise ValueError(f"Instance {name!r} already exists/reserved. Use update; names with preserved history cannot be reused by new.")
                    instance = Instance(instance_name(name), registry.home)
                    if any(item.exists() or item.is_symlink() for item in (instance.runtime, instance.config, instance.state, instance.service_file())):
                        raise ValueError(f"{name}: existing unmanaged files; refusing to adopt or overwrite.")
                    validate_binding(instance)
                    port = select_port(registry, entry.get("port"), ports)
                    bind = entry.get("bind", "0.0.0.0")
                    ipaddress.ip_address(bind)  # Literal bind addresses only; no shell/XML injection.
                    names.add(name)
                    ports.add(port)
                    plan.append((instance, port, bind))
                print("Create: " + ", ".join(f"{item.name}:{port}" for item, port, _ in plan), flush=True)
                failures = 0
                for item, port, bind in plan:
                    registry.save(item, "pending")
                    try:
                        install_instance(item, port, bind)
                        registry.save(item, "installed")
                    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                        registry.save(item, "failed")
                        print(f"{item.name}: failed ({exc}); other instances were not rolled back.", file=sys.stderr)
                        failures += 1
                return int(bool(failures))
            name = args.name or args.named
            if (args.name and args.named) or bool(name) == bool(args.all) or (args.exclude and not args.all):
                raise ValueError("Select exactly one instance or --all; --exclude requires --all.")
            unknown_exclusions = set(args.exclude) - set(existing)
            if unknown_exclusions:
                raise ValueError("Unknown excluded instance(s): " + ", ".join(sorted(unknown_exclusions)))
            selected = registry.instances() if args.all else [existing[name]] if name in existing else []
            selected = [item for item in selected if item.name not in args.exclude]
            if not selected:
                raise ValueError("No matching instances; nothing changed.")
            for item in selected:
                validate_binding(item)
            if args.command == "remove":
                confirm_removal(selected, args.purge_state, args.yes)
            failures = 0
            for item in selected:
                try:
                    if args.command == "remove":
                        command = ["/bin/bash", str(ROOT / "uninstall.sh"), "--managed-instance", item.name, "--yes"]
                        if args.purge_state:
                            command.append("--purge-state")  # Still asks for each exact state path.
                        run(command, env=clean_environment(item), cwd=ROOT)
                        registry.save(item, "removed")
                    elif args.command == "update":
                        env = read_config(item)
                        install_instance(item, int(env["AGENTSDOCK_AGENT_PORT"]), env["AGENTSDOCK_AGENT_BIND"])
                        registry.save(item, "installed")
                    else:
                        control(item, args.command)
                    print(f"{item.name}: {args.command} completed", flush=True)
                except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                    print(f"{item.name}: failed ({exc})", file=sys.stderr)
                    failures += 1
            return int(bool(failures))
    except (OSError, ValueError, KeyError) as exc:
        print(f"Instance manager: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
