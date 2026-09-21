"""Same-user provider-history ownership, compatible with main's instance registry.

Backported from main's server_instances.py (8fcfeec) without service installation,
removal, or manager commands. Discovery reads only; the import guard creates a
private shared lock. Keep registry/path/ownership semantics aligned with main.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys

INSTANCE_PROTOCOL = 1


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
    def config(self) -> Path:
        return self.home / ".config" / ("agents-server" if self.name == "default" else f"agents-server-instances/{self.name}")

    @property
    def state(self) -> Path:
        return self.home / (".agentsdock" if self.name == "default" else f".agentsdock-instances/{self.name}")

    def service_file(self, platform: str = sys.platform) -> Path:
        if platform == "darwin":
            return self.home / "Library/LaunchAgents" / (launchd_label(self.name) + ".plist")
        if platform.startswith("linux"):
            return self.home / ".config/systemd/user" / (service_name(self.name) + ".service")
        raise ValueError("Only macOS and Linux user services are supported.")

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
