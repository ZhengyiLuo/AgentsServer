"""Bounded native ACP content and provider-reported metadata (no estimates)."""
from __future__ import annotations

import base64
import json
import os
import stat

from fastapi import HTTPException
from gemini_acp_client import GeminiProtocolError

MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_FILES = 8
MEDIA = {"image/png", "image/jpeg", "image/webp", "image/gif", "audio/wav", "audio/mpeg", "audio/ogg", "audio/flac"}


def tool_output(content) -> str:
    """Keep native diffs/resources in traces, not just text-only tool results."""
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    parts = []
    for item in content:
        block = item.get("content") if isinstance(item, dict) and item.get("type") == "content" else None
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(json.dumps(item, ensure_ascii=False))
    return "\n".join(parts)


def attachments(api, session_id: str, file_ids: list[str]) -> list[dict]:
    ids = api.validate_session_file_ids(session_id, file_ids)
    if len(ids) > MAX_FILES:
        raise HTTPException(413, "Gemini accepts at most 8 attachments per turn.")
    result, total = [], 0
    for file_id in ids:
        record = api.load_file_meta(file_id)
        source = api.fork_file_source_path(file_id, record)
        if source is None:
            raise HTTPException(409, "Gemini attachment is no longer available.")
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                before = os.fstat(handle.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_FILE_BYTES:
                    raise HTTPException(413, "Gemini attachments must be regular files of at most 512 KiB each.")
                raw = handle.read(MAX_FILE_BYTES + 1)
                after = os.fstat(handle.fileno())
            if len(raw) > MAX_FILE_BYTES or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise HTTPException(409, "Gemini attachment changed while reading; attach it again.")
        except OSError:
            raise HTTPException(409, "Gemini attachment could not be safely read.") from None
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(413, "Gemini attachments exceed the 2 MiB total limit.")
        mime = str(record.get("content_type") or "").split(";", 1)[0].strip().lower()
        if mime in MEDIA:
            result.append({"type": "image" if mime.startswith("image/") else "audio",
                           "mimeType": mime, "data": base64.b64encode(raw).decode("ascii")})
        else:
            try:
                text = raw.decode("utf-8")
                if "\0" in text:
                    raise ValueError()
            except (UnicodeError, ValueError):
                raise HTTPException(415, "Gemini supports UTF-8 text, PNG/JPEG/WebP/GIF images and WAV/MP3/OGG/FLAC audio attachments.") from None
            # Pinned ACP resolvePrompt drops inline media when resource blocks
            # are mixed with it. Explicit text parts preserve mixed uploads;
            # no URL fetching or arbitrary path resolution is involved.
            result.append({"type": "text", "text": "\nAttached file (quoted content): "
                + json.dumps({"name": str(record.get("filename") or file_id)[:256], "text": text}, ensure_ascii=False)})
    return result


def plan_entries(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 128:
        raise GeminiProtocolError("Gemini returned an invalid plan.")
    result = []
    for row in value:
        if (not isinstance(row, dict) or not isinstance(row.get("content"), str)
                or len(row["content"]) > 2048 or row.get("status") not in {"pending", "in_progress", "completed"}
                or row.get("priority") not in {"high", "medium", "low"}):
            raise GeminiProtocolError("Gemini returned an invalid plan entry.")
        result.append({key: row[key] for key in ("content", "status", "priority")})
    return result


def usage(value) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for source, target in (("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
                           ("totalTokens", "total_tokens"), ("cachedReadTokens", "cached_input_tokens"),
                           ("thoughtTokens", "reasoning_tokens")):
        count = value.get(source)
        if count is None:
            continue
        if type(count) is not int or not 0 <= count <= 2**53 - 1:
            raise GeminiProtocolError("Gemini returned invalid token usage.")
        result[target] = count
    return result
