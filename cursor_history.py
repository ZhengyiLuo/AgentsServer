"""Read-only Cursor CLI discovery (verified with 2026.09.18).

Only the CLI's metadata and plaintext user-facing JSONL export are read. Never
open conversation blobs, decrypt data, invoke the CLI, or migrate native stores.
Cursor IDE and cloud histories are different formats and are not advertised.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
from typing import Iterator

MAX_METADATA_BYTES = 64 * 1024
MAX_SCAN_ENTRIES = 10_000
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


def native_title(value: object) -> str | None:
    """Bound display metadata before choosing between native naming sources.

    Cursor's default is New Agent. Other names (including a user's literal
    'New chat' or 'Untitled') are not generic AgentsDock placeholders here.
    """
    if not isinstance(value, str) or len(value) > 4096:
        return None
    clean = " ".join("".join(
        character for character in value
        if character.isspace()
        or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    ).split())[:120]
    return clean if clean and clean.casefold() != "new agent" else None


def config_root(cwd: str | None = None) -> Path:
    config = os.environ.get("CURSOR_CONFIG_DIR", "").strip()
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(config) if config else Path(xdg) / "cursor" if xdg else Path.home() / ".cursor"
    return Path(os.path.abspath(root if root.is_absolute() else Path(cwd or os.getcwd()) / root))


def data_root(cwd: str) -> Path:
    data = os.environ.get("CURSOR_DATA_DIR", "").strip()
    root = Path(data) if data else Path.home() / ".cursor"
    return Path(os.path.abspath(root if root.is_absolute() else Path(cwd) / root))


def project_slug(cwd: str) -> str:
    # Cursor utils/dist/workspace-paths.js; do not guess a cwd by reversing it.
    return re.sub(r"-+", "-", re.sub(r"[^a-zA-Z0-9]", "-", cwd)).strip("-")


def safe_path(path: Path, root: Path) -> bool:
    """Do not follow symlinks beneath the configured native root."""
    try:
        relative = path.relative_to(root)
        if ".." in relative.parts or root.is_symlink():
            return False
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                return False
        return True
    except (OSError, ValueError):
        return False


def read_json(path: Path, root: Path) -> dict | None:
    if not safe_path(path, root):
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.read(MAX_METADATA_BYTES + 1)
        if len(raw) > MAX_METADATA_BYTES:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def store_metadata(database: Path, root: Path) -> dict | None:
    if not safe_path(database, root) or not database.is_file():
        return None
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.05)
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.set_progress_handler(lambda: 1, 10000)
            row = connection.execute(
                "SELECT substr(value, 1, 32769) FROM meta WHERE key = '0' AND typeof(value) = 'text'",
            ).fetchone()
        finally:
            connection.close()
        if not row or len(row[0]) > 32768:
            return None
        value = json.loads(bytes.fromhex(row[0]))
        if not isinstance(value, dict):
            return None
        # Do not return encryption keys, root blobs, or unrelated metadata.
        return {
            "id": value.get("agentId"), "title": value.get("name"),
            "is_subagent": value.get("subagentInfo") is not None,
        }
    except (OSError, ValueError, RecursionError, sqlite3.Error):
        return None


@dataclass(frozen=True)
class LocalSession:
    provider_id: str
    cwd: str
    title: str | None
    updated_at: float
    transcript: Path


def read_session(directory: Path, root: Path, *, expected_cwd: str | None = None) -> LocalSession | None:
    provider_id = directory.name
    if not IDENTIFIER.fullmatch(provider_id) or not safe_path(directory, root):
        return None
    sidecar = read_json(directory / "meta.json", root)
    if not sidecar or type(sidecar.get("schemaVersion")) is not int or sidecar["schemaVersion"] != 1:
        return None
    if sidecar.get("hasConversation") is not True or sidecar.get("isSubagent") is True:
        return None
    cwd = sidecar.get("cwd")
    if (not isinstance(cwd, str) or not cwd or len(cwd) > 16384
            or any(ord(char) < 32 or ord(char) == 127 for char in cwd)
            or not Path(cwd).is_absolute() or os.path.normpath(cwd) != cwd):
        return None
    if expected_cwd is not None and cwd != expected_cwd:
        return None
    try:
        bucket = hashlib.md5(cwd.encode(), usedforsecurity=False).hexdigest()
        usable = Path(cwd).is_dir() and config_root(cwd).absolute() == root.absolute()
    except (OSError, ValueError):
        return None
    if bucket != directory.parent.name:
        return None
    # CLI resume is workspace-bound. Do not silently substitute DEFAULT_CWD
    # for a vanished temporary project, which would create an unrelated chat.
    if not usable:
        return None
    metadata = store_metadata(directory / "store.db", root)
    if not metadata or metadata["id"] != provider_id or metadata["is_subagent"]:
        return None
    # Native configuration and transcript data have independent roots.
    data = data_root(cwd)
    projects = data / "projects"
    slug = project_slug(cwd)
    if not slug:
        return None
    transcript = projects / slug / "agent-transcripts" / provider_id / f"{provider_id}.jsonl"
    if not safe_path(transcript, data):
        return None
    try:
        info = transcript.stat()
        if not stat.S_ISREG(info.st_mode):
            return None
    except OSError:
        return None
    updated = sidecar.get("updatedAtMs")
    timestamp = updated / 1000 if type(updated) is int and 0 < updated < 253402300800000 else info.st_mtime
    title = native_title(metadata["title"]) or native_title(sidecar.get("title"))
    return LocalSession(provider_id, cwd, title, timestamp, transcript)


def find_session(provider_id: str, cwd: str) -> LocalSession | None:
    if not isinstance(provider_id, str) or not IDENTIFIER.fullmatch(provider_id) or not isinstance(cwd, str):
        return None
    root = config_root(cwd)
    try:
        bucket = hashlib.md5(cwd.encode(), usedforsecurity=False).hexdigest()
    except ValueError:
        return None
    return read_session(root / "chats" / bucket / provider_id, root, expected_cwd=cwd)


def local_sessions() -> Iterator[LocalSession]:
    root = config_root()
    chats = root / "chats"
    if not safe_path(chats, root):
        return
    remaining = MAX_SCAN_ENTRIES
    try:
        with os.scandir(chats) as buckets:
            for bucket in buckets:
                remaining -= 1
                if remaining < 0:
                    return
                if not re.fullmatch(r"[0-9a-f]{32}", bucket.name) or not bucket.is_dir(follow_symlinks=False):
                    continue
                try:
                    with os.scandir(bucket.path) as directories:
                        for entry in directories:
                            remaining -= 1
                            if remaining < 0:
                                return
                            if not entry.is_dir(follow_symlinks=False):
                                continue
                            session = read_session(Path(entry.path), root)
                            if session is not None:
                                yield session
                except OSError:
                    continue
    except OSError:
        return
