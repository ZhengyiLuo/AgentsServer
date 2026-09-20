#!/usr/bin/env python3
"""Current-user instance bindings and manager. No server import or discovery writes.

The registry contains names, statuses and optional ports, not deletion paths or credentials.
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
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

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


def read_regular(path: Path, *, max_bytes: int = 1024 * 1024) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError(f"Unsafe managed file: {path}")
        data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
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
            if not isinstance(record, dict) or not {"status"} <= set(record) <= {"status", "port"} or record["status"] not in {"pending", "installed", "removed", "failed"}:
                raise ValueError("Invalid instance record.")
            if "port" in record and (type(record["port"]) is not int or not 1 <= record["port"] <= 65535):
                raise ValueError("Invalid saved instance port.")
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

    def save(self, instance: Instance, status: str, port: int | None = None):
        if status not in {"pending", "installed", "removed", "failed"}:
            raise ValueError("Invalid lifecycle status.")
        records = self.records()
        record = {**records.get(instance.name, {}), "status": status}
        if port is not None:
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError("Invalid saved instance port.")
            record["port"] = port
        records[instance.name] = record
        self._write(records)

    def forget(self, instance: Instance):
        records = self.records()
        records.pop(instance.name, None)
        self._write(records)

    def _write(self, records: dict):
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


PROVIDER_ID_FIELDS = {
    "claude": "claude_session_id", "codex": "codex_thread_id",
    "cursor": "cursor_session_id", "opencode": "opencode_session_id",
}
MAX_IMPORT_INDEX_BYTES = 64 * 1024 * 1024
MAX_IMPORT_INDEX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_IMPORT_INSTANCES = 256


def provider_session_keys(session: dict, default_backend: str = "claude") -> set[tuple[str, str]]:
    """Include parked provider identities as well as the active legacy ID."""
    backend = str(session.get("backend") or default_backend).strip().lower()
    keys = set()
    for provider, field in PROVIDER_ID_FIELDS.items():
        value = session.get(field)
        if not value and provider == backend:
            value = session.get("provider_session_id") or session.get("session_id")
        if isinstance(value, str) and value.strip():
            keys.add((provider, value.strip()))
    return keys


def other_instance_provider_keys(current_state: Path, *, registry: Registry | None = None) -> set[tuple[str, str]]:
    """Read installed instances' indexes, never transcripts or another service API.

    Stopped services and archived chats still own their provider IDs. Removed
    instances with preserved history do not. No registry/configuration writes
    occur during discovery. Bad or unsafe indexes fail closed, not silently open.
    """
    registry = registry or Registry()
    instances = registry.instances()
    if len(instances) > MAX_IMPORT_INSTANCES:
        raise ValueError("Too many local instances to verify import ownership.")
    keys = set()
    remaining = MAX_IMPORT_INDEX_TOTAL_BYTES
    current_state = current_state.resolve()
    for instance in instances:
        check_path(instance.config, registry.home)
        config = read_config(instance)
        configured_state = (config.get("AGENTSDOCK_STATE_DIR")
                            or config.get("AGENTS_SERVER_STATE_DIR")
                            or config.get("ZENITHBOT_AGENT_DIR"))
        state = Path(configured_state) if configured_state else instance.state
        if instance.name != "default" and state != instance.state:
            raise ValueError(f"{instance.name}: unexpected state binding.")
        if not state.is_absolute() or ".." in state.parts:
            raise ValueError(f"{instance.name}: unsafe state binding.")
        if state == current_state:
            continue  # The current process has the authoritative in-memory map.
        check_path(state / "sessions.json", registry.home)
        try:
            data = read_regular(state / "sessions.json", max_bytes=min(MAX_IMPORT_INDEX_BYTES, remaining))
        except FileNotFoundError:
            continue  # A registered/new instance may not have any chats yet.
        remaining -= len(data)
        sessions = json.loads(data)
        if not isinstance(sessions, dict) or any(not isinstance(row, dict) for row in sessions.values()):
            raise ValueError(f"{instance.name}: invalid sessions index.")
        default_backend = config.get("AGENTSDOCK_BACKEND") or config.get("ZENITHBOT_BACKEND") or "claude"
        for session in sessions.values():
            keys.update(provider_session_keys(session, default_backend))
    return keys


@contextmanager
def history_import_lock(*, registry: Registry | None = None):
    """Serialize cooperating local imports until their index writes have landed.

    Separate from service-management locks; nonblocking and OS-released on exit
    or crash. Older servers do not participate, so this is not a provider lock.
    """
    registry = registry or Registry()
    check_path(registry.root, registry.home)
    registry.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with exclusive_lock(registry.root / "history-import.lock"):
        yield


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


def tailscale_status(*, home: Path | None = None, platform: str | None = None) -> dict[str, str]:
    """Inspect existing Tailscale only; never launch the GUI, install or log in.

    macOS app variants bundle the CLI without necessarily adding it to PATH.
    Keep failures distinct from absence, and do not publish stale logged-out IPs.
    """
    home = home or Path.home()
    platform = platform or sys.platform
    candidates = [shutil.which("tailscale"), "/usr/local/bin/tailscale", "/usr/bin/tailscale"]
    app_roots = []
    if platform == "darwin":
        candidates.append("/opt/homebrew/bin/tailscale")
        app_roots = [Path("/Applications/Tailscale.app"), home / "Applications/Tailscale.app"]
        candidates.extend(str(root / "Contents/MacOS/Tailscale") for root in app_roots)
    result = {"status": "unavailable" if any(root.is_dir() for root in app_roots) else "not-installed", "ipv4": ""}
    inspected = set()
    deadline = time.monotonic() + 5
    environment = {key: value for key, value in os.environ.items()
                   if key in {"HOME", "PATH", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR"}}
    environment["TAILSCALE_BE_CLI"] = "1"
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file() or not os.access(candidate, os.X_OK):
            continue
        resolved = str(Path(candidate).resolve())
        if resolved in inspected:
            continue
        inspected.add(resolved)
        if result["status"] == "not-installed":
            result["status"] = "unavailable"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            response = subprocess.run(
                [candidate, "status", "--json"], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, check=False, timeout=min(3, remaining), env=environment,
            )
            if response.returncode or len(response.stdout) > 4 * 1024 * 1024:
                continue
            status = json.loads(response.stdout)
            if not isinstance(status, dict) or not isinstance(status.get("BackendState"), str):
                continue
            if status["BackendState"] != "Running":
                result = {"status": "disconnected", "ipv4": ""}
                continue
            self_status = status.get("Self")
            if isinstance(self_status, dict) and self_status.get("Online") is False:
                result = {"status": "disconnected", "ipv4": ""}
                continue
            addresses = status.get("TailscaleIPs")
            if not isinstance(addresses, list):
                continue
            for value in addresses:
                if not isinstance(value, str):
                    continue
                try:
                    address = ipaddress.ip_address(value)
                except ValueError:
                    continue
                if (address.version == 4 and not address.is_unspecified
                        and not address.is_loopback and not address.is_multicast
                        and not address.is_link_local and str(address) == value):
                    return {"status": "connected", "ipv4": value}
            result = {"status": "connected", "ipv4": ""}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            continue  # Do not echo peer details or CLI errors into setup output.
    return result


def setup_network_bindings(bind: str, port: int) -> str:
    """Shell-quoted, fixed-key installer summary. No credentials or mutations."""
    if not 1 <= port <= 65535:
        raise ValueError("Invalid port.")
    address = ipaddress.ip_address("127.0.0.1" if bind == "localhost" else bind.strip("[]"))
    loopback = address.is_loopback
    host = "127.0.0.1" if bind == "localhost" or str(address) == "0.0.0.0" else "::1" if address.is_unspecified else str(address)
    local_url = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    tailscale = tailscale_status()
    ip = tailscale["ipv4"]
    tail_binding = bool(ip and (str(address) == "0.0.0.0" or str(address) == ip))
    server_url = f"http://{ip}:{port}" if tail_binding else local_url
    candidates = [url.split(" ", 1)[0] for url in candidate_addresses(bind, port)]
    values = {
        "TAILSCALE_STATUS": tailscale["status"], "TAILSCALE_IP": ip,
        "TAILSCALE_BIND_MATCH": "true" if tail_binding else "false",
        "SERVER_LOCAL_ONLY": "true" if loopback else "false", "SERVER_URL": server_url,
        "NETWORK_URLS": "\n".join(url for url in candidates if url != server_url),
    }
    return "\n".join(f"{key}={shlex.quote(value)}" for key, value in values.items())


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


def connection_choices(bind: str, port: int, addresses: list[str], network: dict[str, str]) -> dict:
    """Label candidates, never infer Tailscale connectivity from an IP range."""
    choices = {"local": [], "lan": [], "other": [], "tailscale": "", "tailscale_note": ""}
    state, tail_ip = network["status"], network["ipv4"]
    try:
        binding = ipaddress.ip_address("127.0.0.1" if bind == "localhost" else bind.strip("[]"))
    except ValueError:
        binding = None
    if state != "connected":
        choices["tailscale_note"] = {
            "disconnected": "Unavailable: Tailscale is disconnected.",
            "not-installed": "Unavailable: Tailscale was not found.",
        }.get(state, "Not verified: could not read Tailscale status.")
    elif not tail_ip:
        choices["tailscale_note"] = "Not verified: Tailscale has no usable IPv4 address."
    elif binding is not None and binding.is_loopback:
        choices["tailscale_note"] = "Unavailable: this server is bound to this machine only."
    elif binding is not None and (str(binding) == "0.0.0.0" or str(binding) == tail_ip):
        choices["tailscale"] = f"http://{tail_ip}:{port}"
    else:
        choices["tailscale_note"] = f"Not verified: server binding {bind!r} does not confirm access via the Tailscale IPv4 address."

    lan_ranges = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    for candidate in addresses:
        url = candidate.split(" ", 1)[0]
        if url == choices["tailscale"]:
            continue
        host = urlsplit(url).hostname
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            group = "local" if host == "localhost" else "other"
        else:
            if address.is_unspecified or address.is_multicast or address.is_link_local:
                continue
            # An unverified/stale VPN IP must not be presented as ordinary LAN.
            group = "local" if address.is_loopback else "lan" if host != tail_ip and any(address in subnet for subnet in lan_ranges) else "other"
        if url not in choices[group]:
            choices[group].append(url)
    return choices


def show(instance: Instance, *, network: dict[str, str] | None = None):
    item = describe(instance)
    name = terminal_color(f"{item['name']:<20}", "34")
    summary = f"{name} {item['status']:<10} {str(item['port'] or '—'):<6}"
    if network is None:
        print(summary + " " + "  ".join(item["addresses"]))
        return
    print(summary.rstrip())
    if not item["port"]:
        print("  No saved network configuration.\n")
        return
    choices = connection_choices(read_config(instance).get("AGENTSDOCK_AGENT_BIND", "0.0.0.0"), item["port"], item["addresses"], network)
    tail = choices["tailscale"]
    if tail:
        tail += " (recommended)" if item["status"] == "running" else f" (server status: {item['status']})"
    print(f"  Tailscale / other networks: {tail or choices['tailscale_note']}")
    print("  Same Wi-Fi / LAN:          " + (", ".join(choices["lan"]) or "No address detected for this binding."))
    print("  This machine only:         " + (", ".join(choices["local"]) or "No loopback address for this binding."))
    if choices["other"]:
        print("  Other / unverified:        " + ", ".join(choices["other"]))
    print()


def terminal_color(text: str, code: str) -> str:
    color = sys.stdout.isatty() and os.environ.get("TERM") != "dumb" and "NO_COLOR" not in os.environ
    return f"\033[{code}m{text}\033[0m" if color else text


def confirm_removal(instances: list[Instance], purge: bool, yes: bool) -> None:
    print(terminal_color(f"WARNING: uninstall {len(instances)} AgentsServer instance(s)", "1;31"))
    for instance in instances:
        show(instance)
        print(terminal_color(f"  Remove runtime: {instance.runtime}", "31"))
        print(terminal_color(f"  Remove configuration/token: {instance.config}", "31"))
        print(terminal_color(f"  {'PERMANENTLY DELETE' if purge else 'PRESERVE'} history: {instance.state}", "31" if purge else "32"))
    print("Services and tokens will be removed; reinstalling creates a new access token.")
    if purge:
        print(terminal_color("PERMANENT HISTORY DELETION CANNOT BE UNDONE. --yes cannot bypass confirmation.", "1;31"))
    else:
        print("Chat history and files are preserved. Service removal is reinstallable; deleted configuration is not restored automatically.")
        if not yes and any(instance.name != "default" for instance in instances):
            print("After this confirmation, you can also release each name while keeping its chat history in a local backup.")
    target_names = " ".join(instance.name for instance in instances)
    expected = f"{'delete history' if purge else 'uninstall'} {target_names}"
    if purge or not yes:
        if not sys.stdin.isatty() or input(f"Type {expected!r} to confirm: ") != expected:
            raise ValueError("Not confirmed; nothing was uninstalled.")


def install_instance(instance: Instance, port: int, bind: str):
    validate_binding(instance)
    command = ["/bin/bash", str(ROOT / "install.sh"), "--instance", instance.name,
               "--no-port-fallback", "--port", str(port), "--bind", bind]
    # Local terminal users get the installer's optional token-copy prompt;
    # redirected/app-driven installs retain the unattended contract.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        command.append("--non-interactive")
    run(command, env=clean_environment(instance), cwd=ROOT)


def validate_released_instance(instance: Instance) -> None:
    """Never reset default, an installed service, or a running state owner."""
    if instance.name == "default":
        raise ValueError("The default instance cannot be released.")
    validate_binding(instance)
    if any(path.exists() or path.is_symlink() for path in (instance.runtime, instance.config, instance.service_file())):
        raise ValueError(f"{instance.name}: still installed or has a partial installation. Remove it before reusing its name.")
    if service_status(instance) != "stopped":
        raise ValueError(f"{instance.name}: cannot confirm the old service is stopped; nothing released.")
    if instance.state.exists() and not instance.state.is_dir():
        raise ValueError(f"{instance.name}: preserved state is not a directory.")


def confirm_name_release(instance: Instance, port: int) -> None:
    print(f"This name was used before: {instance.name}.")
    print(terminal_color(f"The new instance will start with an empty AgentsDock chat list on port {port}.", "31"))
    print(f"Old AgentsDock history, uploads, jobs and credentials at {instance.state} will be moved to a private backup, not erased.")
    print("Original provider chats stored on this machine and project files are not deleted. AgentsDock-only content remains in the backup.")
    print("To keep using the existing history instead, cancel and run:")
    print(f"  ./install.sh --instance {instance.name} --port {port}")
    try:
        confirmed = sys.stdin.isatty() and input(f"Release {instance.name!r} and create a fresh instance? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        confirmed = False
    if not confirmed:
        raise ValueError("Name release not confirmed; existing history was not changed.")


def release_instance_name(instance: Instance, registry: Registry) -> Path | None:
    validate_released_instance(instance)  # Recheck after the user prompt.
    if not instance.state.exists():
        return None
    backups = registry.root / "history-backups"
    check_path(backups, registry.home)
    # Holding the old state's lock prevents archiving a running server. Rename
    # the directory itself; never traverse/delete its contents or follow links.
    with exclusive_lock(instance.state / ".server-process.lock"):
        validate_released_instance(instance)
        backups.mkdir(mode=0o700, exist_ok=True)
        destination = Path(tempfile.mkdtemp(prefix=f"{instance.name}-", dir=backups)) / "state"
        instance.state.rename(destination)
    print(terminal_color(f"Preserved old instance data in: {destination}", "32"), flush=True)
    return destination


def confirm_uninstall_name_release(instance: Instance) -> bool:
    if not sys.stdin.isatty():
        return False
    print(f"\nOptional name release: {instance.name}")
    print("Releasing the name clears this server's saved chat list from active use and makes the name available again.")
    print(terminal_color("No chat history is deleted. Original provider chats stay on your computer.", "32"))
    print(f"This server's saved AgentsDock history, uploads, jobs and credentials at {instance.state} will be moved to a private local backup; its location will be printed.")
    print("A new server using this name starts with an empty chat list; the old AgentsDock-only content remains in the backup.")
    print("Project files, earlier backups and independent terminal sessions are kept. Enter keeps the name and saved data in place.")
    try:
        return input("Do you want to release this name as well? [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def release_uninstalled_name(instance: Instance, registry: Registry) -> None:
    # Called only after a successful uninstall and separate affirmative answer.
    # Preserve even AgentsDock-only content before freeing the name. Reuse the
    # guarded archive path used by `new`, not the explicit --purge-state action.
    release_instance_name(instance, registry)
    registry.forget(instance)
    print(terminal_color(f"Released name {instance.name!r}; it can be used for a new instance.", "32"), flush=True)


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
    network = commands.add_parser("_setup-network", help=argparse.SUPPRESS)
    network.add_argument("--bind", required=True)
    network.add_argument("--port", type=int, required=True)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    registry = Registry()
    try:
        if args.command == "_setup-network":
            print(setup_network_bindings(args.bind, args.port))
            return 0
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
                network = tailscale_status() if items else None
                print("NAME                 STATUS     PORT")
                for item in items:
                    show(item, network=network)
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
                records = registry.records()
                names, ports, planned_names = set(existing), set(), set()
                for entry in entries:
                    if not isinstance(entry, dict) or set(entry) - {"name", "port", "bind"}:
                        raise ValueError("Invalid manifest entry.")
                    name = entry.get("name")
                    if name is None:
                        index = 1
                        while f"instance-{index}" in names:
                            index += 1
                        name = f"instance-{index}"
                    if name == "default" or name in planned_names:
                        raise ValueError(f"Instance {name!r} already exists/reserved.")
                    instance = Instance(instance_name(name), registry.home)
                    record = records.get(name)
                    reuse = record is not None and record["status"] in {"removed", "failed"}
                    if name in names and not reuse:
                        raise ValueError(f"Instance {name!r} already exists/reserved. Use update.")
                    if reuse:
                        validate_released_instance(instance)
                    elif any(item.exists() or item.is_symlink() for item in (instance.runtime, instance.config, instance.state, instance.service_file())):
                        raise ValueError(f"{name}: existing unmanaged files; refusing to adopt or overwrite.")
                    validate_binding(instance)
                    requested_port = entry.get("port")
                    if reuse and requested_port is None:
                        requested_port = record.get("port")
                        if requested_port is None and instance.state.exists():
                            raise ValueError(f"{name}: the previous port was not recorded. Specify --port to reuse this name.")
                    port = select_port(registry, requested_port, ports)
                    bind = entry.get("bind", "0.0.0.0")
                    ipaddress.ip_address(bind)  # Literal bind addresses only; no shell/XML injection.
                    names.add(name)
                    planned_names.add(name)
                    ports.add(port)
                    # A failed preflight with no state has nothing to release;
                    # retry it directly. Removed names always require consent.
                    release = reuse and (record["status"] == "removed" or instance.state.exists())
                    plan.append((instance, port, bind, release))
                # Validate the entire manifest and collect all confirmations
                # before moving any history or installing any instance.
                for item, port, _, release in plan:
                    if release:
                        confirm_name_release(item, port)
                print("Create: " + ", ".join(f"{item.name}:{port}" for item, port, _, _ in plan), flush=True)
                failures = 0
                for item, port, bind, release in plan:
                    backup = None
                    try:
                        if not port_available(port):
                            raise ValueError(f"Port {port} became occupied; no instance history was moved.")
                        if release:
                            backup = release_instance_name(item, registry)
                        registry.save(item, "pending", port)
                        install_instance(item, port, bind)
                        registry.save(item, "installed", port)
                    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                        registry.save(item, "failed", port)
                        print(f"{item.name}: failed ({exc}); other instances were not rolled back.", file=sys.stderr)
                        if backup is not None:
                            print(f"Original instance data is safe in {backup}; it was not deleted.", file=sys.stderr)
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
            release_names = set()
            if args.command == "remove":
                confirm_removal(selected, args.purge_state, args.yes)
                if not args.purge_state and not args.yes:
                    for item in selected:
                        if item.name != "default" and confirm_uninstall_name_release(item):
                            release_names.add(item.name)
            failures = 0
            for item in selected:
                try:
                    if args.command == "remove":
                        old_port = read_config(item).get("AGENTSDOCK_AGENT_PORT")
                        command = ["/bin/bash", str(ROOT / "uninstall.sh"), "--managed-instance", item.name, "--yes"]
                        if args.purge_state:
                            command.append("--purge-state")  # Still asks for each exact state path.
                        elif item.name in release_names:
                            # The manager will print the backup location; direct
                            # reinstall advice would incorrectly imply reuse.
                            command.append("--managed-release-name")
                        run(command, env=clean_environment(item), cwd=ROOT)
                        registry.save(item, "removed", int(old_port) if old_port else None)
                        if args.purge_state and item.name != "default" and not item.state.exists():
                            registry.forget(item)
                        elif item.name in release_names:
                            release_uninstalled_name(item, registry)
                        elif not args.purge_state and not args.yes and item.name != "default":
                            print(f"Kept name {item.name!r} and its saved data.", flush=True)
                    elif args.command == "update":
                        env = read_config(item)
                        install_instance(item, int(env["AGENTSDOCK_AGENT_PORT"]), env["AGENTSDOCK_AGENT_BIND"])
                        registry.save(item, "installed")
                    else:
                        control(item, args.command)
                    if args.command != "remove":
                        print(f"{item.name}: {args.command} completed", flush=True)
                except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
                    print(f"{item.name}: failed ({exc})", file=sys.stderr)
                    failures += 1
            if args.command == "remove" and not failures:
                print("\n" + terminal_color("Successful!", "32"), flush=True)
            return int(bool(failures))
    except (OSError, ValueError, KeyError) as exc:
        print(f"Instance manager: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
