"""Read-only replay fence for the pinned CLI's asynchronous session/load.

Gemini 0.64.0 returns load before streamHistory finishes, with no replay-end
notification. Match a bounded native snapshot BEFORE sending a new prompt.
Unknown formats fail closed; this is not an import/reconciliation API.
"""
from __future__ import annotations

import hashlib
from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import math

from gemini_acp_client import GeminiProtocolError, identifier
from gemini_runtime import native_home

MAX_BYTES = 16 * 1024 * 1024
MAX_ENTRIES = 10000


def replay_text(value) -> str:
    """Match pinned partListUnionToString for supported text/tool history.

    Native skill/tool responses are user-role functionResponse parts. They
    replay as placeholders, not additional user turns. This is only a replay
    fence: do not import those placeholders into the AgentsDock timeline.
    Unknown media/part shapes still fail closed.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if len(value) > MAX_ENTRIES or any(isinstance(part, list) for part in value):
            raise GeminiProtocolError("Unsupported Gemini replay part list.")
        return "".join(replay_text(part) for part in value)
    if isinstance(value, dict):
        if set(value) <= {"text", "thought", "thoughtSignature"} and isinstance(value.get("text"), str):
            if "thought" in value:
                if not isinstance(value["thought"], bool):
                    raise GeminiProtocolError("Invalid Gemini native thought part.")
                return "[Thought: true]" if value["thought"] else "[Thought: false]"
            return value["text"]
        for key, label in (("functionCall", "Function Call"), ("functionResponse", "Function Response")):
            if set(value) <= {key, "thoughtSignature"} and isinstance(value.get(key), dict):
                name = identifier(value[key].get("name"))
                return f"[{label}: {name}]"
        if set(value) <= {"inlineData", "thoughtSignature"} and isinstance(value.get("inlineData"), dict):
            media = value["inlineData"]
            mime, data = media.get("mimeType"), media.get("data")
            if isinstance(mime, str) and isinstance(data, str):
                category = next((label for prefix, label in (("audio/", "Audio"), ("video/", "Video"), ("image/", "Image")) if mime.startswith(prefix)), "Media")
                # Match JS toFixed(1), including its half-up behavior.
                kb = math.floor(math.ceil(len(data) * 3 / 4) / 1024 * 10 + .5) / 10
                return f"[{category}: {mime}, {kb:.1f} KB]"
    raise GeminiProtocolError("This Gemini history format needs a newer resume adapter; continue it in Gemini CLI.")


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


def native_snapshot(env: dict[str, str], cwd: str, session_id: str, *, missing_ok: bool = False) -> dict | None:
    identifier(session_id, session=True)
    root = native_home(env) / ".gemini" / "tmp"
    found = []
    count = 0
    def entries(directory):
        nonlocal count
        if directory.is_symlink():
            raise GeminiProtocolError("Linked Gemini history cannot be resumed safely.")
        with os.scandir(directory) as iterator:
            for entry in iterator:
                count += 1
                if count > MAX_ENTRIES:
                    raise GeminiProtocolError("Gemini history exceeds the supported scan limit.")
                yield entry
    try:
        if missing_ok and not root.exists():
            return None
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
    if not found and missing_ok:
        return None
    if len(found) != 1 or found[0].get("projectHash") != hashlib.sha256(cwd.encode()).hexdigest():
        raise GeminiProtocolError("Gemini native history does not uniquely match this chat and workspace.")
    return found[0]


def replay_snapshot(env: dict[str, str], cwd: str, session_id: str) -> list[tuple[str, str]]:
    return snapshot_replay(native_snapshot(env, cwd, session_id))


def snapshot_replay(snapshot: dict) -> list[tuple[str, str]]:
    result = []
    for message in snapshot["messages"]:
        if not isinstance(message, dict):
            raise GeminiProtocolError("Invalid Gemini native message.")
        kind = message.get("type")
        if kind not in {"user", "gemini"}:
            continue  # Pinned streamHistory ignores native info/error records.
        content = replay_text(message.get("content"))
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
            raise GeminiProtocolError("Gemini replay exceeds the supported limit.")
    return result


def verify_resume_snapshot(before: dict, after: dict) -> None:
    """Allow only the pinned loader's exact text/tool normalization.

    streamHistory emits the pre-normalization messages, while resumeChat writes
    converted model parts and deterministic *_response rows back to disk. Merely
    comparing replay fingerprints therefore rejects every resumed tool turn.
    Preserve the concurrency fence by predicting those mutations, not by
    dropping it or allowing arbitrary appended records.
    """
    failure = "Gemini history changed during resume; close the other client before retrying."
    if any(before.get(key) != after.get(key) for key in ("sessionId", "projectHash")):
        raise GeminiProtocolError(failure)
    if before["messages"] == after["messages"]:
        return
    records = deepcopy(before["messages"])
    by_id = {row["id"]: row for row in records}
    if len(by_id) != len(records):
        raise GeminiProtocolError(failure)
    converted = []
    generated = set()
    empty_model_ids = set()

    def parts(value):
        values = value if isinstance(value, list) else [value]
        return [{"text": part} if isinstance(part, str) else deepcopy(part) for part in values]

    for row in records:
        kind, content = row.get("type"), row.get("content")
        if kind == "user":
            text = replay_text(content).strip()
            if not text or text.startswith(("/", "?", "<session_context>", "<hook_context>")):
                continue
            row["content"] = parts(content)
            converted.append(row)
        elif kind == "gemini":
            model_parts = parts(content) if content else []
            if not any(p.get("functionCall") or p.get("thought") for p in model_parts):
                thoughts = [{"text": f"**{t['subject']}** {t['description']}" if t.get("subject") else t["description"],
                             "thought": True} for t in row.get("thoughts", [])]
                calls = [{"functionCall": {key: tool[key] for key in ("id", "name", "args")}}
                         for tool in row.get("toolCalls", [])]
                model_parts = thoughts + model_parts + calls
            if not model_parts:
                empty_model_ids.add(row["id"])
                continue
            row["content"] = model_parts
            converted.append(row)
            responses = []
            for tool in row.get("toolCalls", []):
                value = tool.get("result")
                if not value:
                    continue
                if isinstance(value, str):
                    responses.append({"functionResponse": {"id": tool["id"], "name": tool["name"], "response": {"output": value}}})
                else:
                    responses.extend(parts(value))
            if responses:
                key = row["id"] + "_response"
                response = deepcopy(by_id.get(key, {"id": key, "type": "user"}))
                if response.get("type") != "user":
                    raise GeminiProtocolError(failure)
                response["content"] = responses
                generated.add(key)
                converted.append(response)
    # updateMessagesFromHistory keeps ignored native context/info records, then
    # writes converted orderIds. Native JSONL loading de-duplicates those IDs.
    ordered = {row["id"]: row for row in converted}
    tools = {tool["id"]: tool for row in records for tool in row.get("toolCalls", [])}
    for row in converted:
        if row.get("type") == "user":
            for part in row["content"]:
                response = part.get("functionResponse", {})
                if response.get("id") in tools:
                    tools[response["id"]]["result"] = deepcopy(row["content"])
    # Cancelling while awaiting approval can leave an empty model record. The
    # native loader drops precisely these records, not user text/tool history.
    expected = [row for row in records if row["id"] not in ordered and row["id"] not in empty_model_ids] + list(ordered.values())
    actual = deepcopy(after["messages"])
    # Pinned startChat rebuilds its leading environment header (directory tree,
    # date, etc.) on resume. A tool that created a file legitimately changes it.
    # Only that first, exact native context row is allowed to refresh; real user
    # content, tool results, identities, metadata and additional rows stay exact.
    def environment_header(row):
        parts = row.get("content")
        return (row.get("type") == "user" and isinstance(parts, list) and len(parts) == 1
                and isinstance(parts[0], dict) and set(parts[0]) == {"text"}
                and isinstance(parts[0]["text"], str)
                and parts[0]["text"].startswith("<session_context>\n")
                and parts[0]["text"].endswith("\n</session_context>"))
    if expected and actual and environment_header(expected[0]) and environment_header(actual[0]):
        if {k: v for k, v in expected[0].items() if k != "content"} == {k: v for k, v in actual[0].items() if k != "content"}:
            actual[0]["content"] = expected[0]["content"]
    for rows in (expected, actual):
        for row in rows:
            if row["id"] in generated and row["id"] not in by_id:
                row.pop("timestamp", None)  # The only newly generated metadata.
    if expected != actual:
        raise GeminiProtocolError(failure)


def read_snapshot(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            raise GeminiProtocolError("Gemini history exceeds the supported file limit.")
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
                if not isinstance(update, dict) or set(update) - {"id", "content", "toolCalls"}:
                    raise GeminiProtocolError("Unsupported Gemini message patch.")
                existing = messages.get(update.get("id"))
                if existing and "content" in update:
                    existing["content"] = update["content"]
                for tool_patch in update.get("toolCalls", []):
                    if not isinstance(tool_patch, dict) or set(tool_patch) - {"id", "result"}:
                        raise GeminiProtocolError("Unsupported Gemini tool-result patch.")
                    tool = next((row for row in (existing or {}).get("toolCalls", []) if row.get("id") == tool_patch.get("id")), None)
                    if tool is not None and "result" in tool_patch:
                        tool["result"] = tool_patch["result"]
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
