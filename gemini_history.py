"""Read-only replay fence for the pinned CLI's asynchronous session/load.

Gemini 0.64.0 returns load before streamHistory finishes, with no replay-end
notification. Match a bounded native snapshot BEFORE sending a new prompt.
Unknown formats fail closed; this is not an import/reconciliation API.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

from gemini_acp_client import GeminiProtocolError, identifier
from gemini_runtime import native_home

MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 10000


def fingerprint(update: dict) -> tuple[str, str]:
    kind = update.get("sessionUpdate")
    if kind in {"user_message_chunk", "agent_message_chunk", "agent_thought_chunk"}:
        content = update.get("content")
        if not isinstance(content, dict) or content.get("type") != "text" or not isinstance(content.get("text"), str):
            raise GeminiProtocolError("Unsupported Gemini replay content.")
        return kind, hashlib.sha256(content["text"].encode()).hexdigest()
    if kind == "tool_call":
        return kind, identifier(update.get("toolCallId"))
    raise GeminiProtocolError("Unsupported Gemini replay event.")


def replay_snapshot(env: dict[str, str], cwd: str, session_id: str) -> list[tuple[str, str]]:
    identifier(session_id, session=True)
    root = native_home(env) / ".gemini" / "tmp"
    found = []
    count = 0
    def entries(directory):
        nonlocal count
        if directory.is_symlink():
            raise GeminiProtocolError("Linked Gemini history cannot be resumed in this preview.")
        with os.scandir(directory) as iterator:
            for entry in iterator:
                count += 1
                if count > MAX_ENTRIES:
                    raise GeminiProtocolError("Gemini history exceeds the preview scan limit.")
                yield entry
    try:
        for project in entries(root):
            if not project.is_dir(follow_symlinks=False):
                continue
            chats = Path(project.path) / "chats"
            if not chats.is_dir():
                continue
            for entry in entries(chats):
                if (entry.name.startswith("session-") and entry.name.endswith((f"-{session_id[:8]}.json", f"-{session_id[:8]}.jsonl"))
                        and entry.is_file(follow_symlinks=False)):
                    value = read_snapshot(Path(entry.path))
                    if value.get("sessionId") == session_id:
                        found.append(value)
    except (OSError, ValueError, TypeError, RecursionError):
        raise GeminiProtocolError("Gemini native history cannot be safely read for resume.") from None
    if len(found) != 1 or found[0].get("projectHash") != hashlib.sha256(cwd.encode()).hexdigest():
        raise GeminiProtocolError("Gemini native history does not uniquely match this chat and workspace.")
    result = []
    for message in found[0]["messages"]:
        if not isinstance(message, dict):
            raise GeminiProtocolError("Invalid Gemini native message.")
        kind = message.get("type")
        if kind not in {"user", "gemini"}:
            continue  # Pinned streamHistory ignores native info/error records.
        content = message.get("content") or ""
        if isinstance(content, list) and all(isinstance(part, dict) and set(part) <= {"text"} and isinstance(part.get("text"), str) for part in content):
            content = "".join(part["text"] for part in content)
        if not isinstance(content, str):
            raise GeminiProtocolError("This Gemini history format needs a newer resume adapter; continue it in Gemini CLI.")
        if kind == "gemini":
            for thought in message.get("thoughts") or []:
                if not isinstance(thought, dict) or not all(isinstance(thought.get(key), str) for key in ("subject", "description")):
                    raise GeminiProtocolError("Invalid Gemini native thought.")
                result.append(fingerprint({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": f"**{thought['subject']}**\n{thought['description']}"}}))
        if content.strip():
            result.append(fingerprint({"sessionUpdate": "user_message_chunk" if kind == "user" else "agent_message_chunk", "content": {"type": "text", "text": content}}))
        if kind == "gemini":
            for tool in message.get("toolCalls") or []:
                result.append(fingerprint({"sessionUpdate": "tool_call", "toolCallId": tool.get("id")}))
        if len(result) > MAX_ENTRIES:
            raise GeminiProtocolError("Gemini replay exceeds the preview limit.")
    return result


def read_snapshot(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise GeminiProtocolError("Gemini history exceeds the preview file limit.")
        raw = handle.read(MAX_BYTES + 1)
        after = os.fstat(handle.fileno())
        if len(raw) > MAX_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise GeminiProtocolError("Gemini history changed while preparing resume; retry after the other client stops.")
    if path.suffix == ".json":
        return json.loads(raw)
    metadata, messages = {}, {}
    for line in raw.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise GeminiProtocolError("Invalid Gemini history record.")
        if "$rewindTo" in value:
            ids = list(messages)
            target = value["$rewindTo"]
            messages = {key: messages[key] for key in ids[:ids.index(target)]} if target in ids else {}
        elif "$patch" in value:
            patch = value["$patch"]
            if not isinstance(patch, dict) or set(patch) - {"id", "content", "toolCalls", "updates", "removeIds", "orderIds"}:
                raise GeminiProtocolError("Unsupported Gemini history patch.")
            for update in ([patch] if "id" in patch else []) + patch.get("updates", []):
                existing = messages.get(update.get("id"))
                if existing and "content" in update:
                    existing["content"] = update["content"]
                # Native tool-result patches cannot change tool identities or
                # the number/order of replay notifications.
            for key in patch.get("removeIds", []):
                messages.pop(key, None)
            order = patch.get("orderIds", [])
            if order:
                messages = {**{key: row for key, row in messages.items() if key not in order},
                            **{key: messages[key] for key in order if key in messages}}
        elif "id" in value:
            messages[value["id"]] = value
        else:
            update = value.get("$set", value)
            if "messages" in update:
                messages = {row["id"]: row for row in update["messages"]}
            metadata.update(update)
    return {**metadata, "messages": list(messages.values())}
