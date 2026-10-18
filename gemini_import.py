"""Read-only Gemini 0.64 main-session discovery and initial text snapshots.

The native .project_root marker plus transcript hash binds an exact workspace.
Never reverse a slug, launch Gemini, or treat tool responses as user messages.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat

from gemini_acp_client import GeminiProtocolError, identifier
from gemini_history import MAX_ENTRIES, read_snapshot
from gemini_runtime import native_home


def _regular_text(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
            raise ValueError("Invalid Gemini workspace marker")
        raw = handle.read(16385)
    if len(raw) > 16384:
        raise ValueError("Invalid Gemini workspace marker")
    return raw.decode("utf-8").strip()


def records(env: dict):
    root = native_home(env) / ".gemini" / "tmp"
    if not root.exists():
        return
    if root.is_symlink() or root.parent.is_symlink():
        raise GeminiProtocolError("Linked Gemini history is not supported.")
    count = 0
    with os.scandir(root) as projects:
        for project in projects:
            count += 1
            if count > MAX_ENTRIES:
                raise GeminiProtocolError("Gemini discovery exceeded its scan limit.")
            if not project.is_dir(follow_symlinks=False):
                continue
            directory = Path(project.path)
            chats = directory / "chats"
            try:
                cwd = _regular_text(directory / ".project_root")
                if not Path(cwd).is_absolute() or not Path(cwd).is_dir() or os.path.normpath(cwd) != cwd:
                    continue
                if chats.is_symlink() or not chats.is_dir():
                    continue
                with os.scandir(chats) as entries:
                    for entry in entries:
                        count += 1
                        if count > MAX_ENTRIES:
                            raise GeminiProtocolError("Gemini discovery exceeded its scan limit.")
                        if not entry.name.startswith("session-") or not entry.name.endswith((".json", ".jsonl")) or not entry.is_file(follow_symlinks=False):
                            continue
                        try:
                            path = Path(entry.path)
                            value = read_snapshot(path)
                            provider_id = identifier(value.get("sessionId"), session=True)
                            if (value.get("kind", "main") != "main" or value.get("archived") or value.get("deleted")
                                    or value.get("parentSessionId") or value.get("projectHash") != hashlib.sha256(cwd.encode()).hexdigest()
                                    or not any(row.get("type") == "gemini" for row in value["messages"])):
                                continue
                            yield path, cwd, provider_id, value
                        except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError, GeminiProtocolError):
                            continue  # A malformed entry cannot hide other main conversations.
            except (OSError, ValueError, UnicodeError):
                continue


def visible_items(api, snapshot: dict) -> list[dict]:
    result = []
    for row in snapshot["messages"]:
        role = row.get("type")
        if role not in {"user", "gemini"}:
            continue
        content = row.get("content")
        parts = content if isinstance(content, list) else [content]
        # A user-role functionResponse is tool I/O, never a new human turn.
        if role == "user" and any(isinstance(part, dict) and "functionResponse" in part for part in parts):
            continue
        text = "".join(part if isinstance(part, str) else part.get("text", "")
                       if isinstance(part, dict) and not part.get("thought") else "" for part in parts)
        if role == "user":
            if text.lstrip().startswith(("<session_context>", "<hook_context>", "/", "?")):
                continue
            text = api.strip_agentsdock_provider_context(text, allow_jobless_legacy=True)
            text = api.strip_agentsdock_generated_user_text(text, provider_history=True)
        if text.strip():
            result.append({"kind": "user" if role == "user" else "assistant", "text": text,
                           "provider_message_id": identifier(row.get("id"))})
    return result


def candidates(api, env: dict, known: set[str]) -> list[dict]:
    unique, ambiguous = {}, set()
    for path, cwd, provider_id, snapshot in records(env):
        if provider_id in known:
            continue
        if provider_id in unique:
            ambiguous.add(provider_id)
            continue
        try:
            items = visible_items(api, snapshot)
            first_user = next((item["text"] for item in items if item["kind"] == "user"), None)
            if not first_user:
                continue
            unique[provider_id] = {"provider_session_id": provider_id, "backend": "gemini", "cwd": cwd,
                "label": api.local_session_label(api.native_session_title(snapshot.get("summary")) or first_user, f"Gemini chat {provider_id[:8]}"),
                "updated_at": api.iso_from_timestamp(path.stat().st_mtime)}
        except (OSError, ValueError, GeminiProtocolError):
            continue
    return [value for key, value in unique.items() if key not in ambiguous]


def initial_history(api, env: dict, provider_id: str, cwd: str, limit=None):
    identifier(provider_id, session=True)
    matches = [(path, value) for path, workspace, pid, value in records(env) if pid == provider_id and workspace == cwd]
    if len(matches) != 1:
        raise GeminiProtocolError("Gemini history does not uniquely match this ID and workspace.")
    path, value = matches[0]
    items = visible_items(api, value)
    return path, items[-limit:] if limit else items
