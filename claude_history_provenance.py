"""Pure, bounded Claude source-lineage classification. Never edits transcripts.

The marker's wording is not evidence of a human Stop action. A synthetic user
record must belong to an already established prompt, not begin a new prompt.
Only source identifiers are carried between byte-cursor reads; never chat text.
"""
from __future__ import annotations

from datetime import datetime
import re


INTERRUPTION_MARKERS = frozenset({
    "[Request interrupted by user]",
    "[Request interrupted by user for tool use]",
})
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z")


def _uuid(value) -> str | None:
    return value if isinstance(value, str) and _UUID.fullmatch(value) else None


def _timestamp(value) -> str | None:
    if not isinstance(value, str) or not _TIME.fullmatch(value):
        return None
    try:
        return value if datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo else None
    except ValueError:
        return None


def normalize_claude_interruption_context(value, provider_session_id=None) -> dict:
    """Allowlist a tiny initialized cursor state, including conservative empties."""
    empty = {"version": 1}
    if not isinstance(value, dict) or value.get("version") != 1:
        return empty
    keys = ("session_id", "prompt_id", "anchor_event_id", "last_event_id")
    if any(not _uuid(value.get(key)) for key in keys):
        return empty
    if provider_session_id and value["session_id"] != provider_session_id:
        return empty
    return {"version": 1, **{key: value[key] for key in keys}}


class ClaudeInterruptionTracker:
    def __init__(self, initial_context=None):
        self._context = normalize_claude_interruption_context(initial_context)

    def export_context(self) -> dict:
        return dict(self._context)

    def consume(self, event) -> dict | None:
        if not isinstance(event, dict):
            self._context = {"version": 1}
            return None
        if event.get("isSidechain") is True:
            return None
        event_id = _uuid(event.get("uuid"))
        # Non-message queue bookkeeping has no UUID and is outside the lineage.
        if event_id is None:
            if not isinstance(event.get("type"), str) or event.get("type") in ("user", "assistant", "attachment"):
                self._context = {"version": 1}
            return None
        session_id = _uuid(event.get("sessionId"))
        parent_id = _uuid(event.get("parentUuid"))
        prompt_id = _uuid(event.get("promptId"))
        timestamp = _timestamp(event.get("timestamp"))
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        blocks = content if isinstance(content, list) else []
        marker = (
            event.get("type") == "user"
            and isinstance(message, dict) and message.get("role") == "user"
            and len(blocks) == 1 and isinstance(blocks[0], dict)
            and blocks[0].get("type") == "text"
            and isinstance(blocks[0].get("text"), str)
            and blocks[0]["text"] in INTERRUPTION_MARKERS
        )
        context = self._context
        continued = (
            session_id is not None and session_id == context.get("session_id")
            and parent_id is not None and parent_id == context.get("last_event_id")
        )
        origin = None
        if marker and session_id and timestamp and (
            event.get("isMeta") is True
            or (continued and prompt_id is not None and prompt_id == context.get("prompt_id")
                and event_id != context.get("anchor_event_id"))
        ):
            origin = {
                "provider": "claude", "kind": "interruption", "cause": "unknown",
                "event_id": event_id, "session_id": session_id, "timestamp": timestamp,
            }
            if parent_id:
                origin["parent_event_id"] = parent_id
            if prompt_id:
                origin["prompt_id"] = prompt_id

        has_text = isinstance(content, str) and bool(content.strip()) or any(
            isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str) and bool(block["text"].strip())
            for block in blocks
        )
        tool_result = any(isinstance(block, dict) and block.get("type") == "tool_result" for block in blocks)
        real_prompt = (
            event.get("type") == "user" and isinstance(message, dict)
            and message.get("role") == "user" and has_text and not tool_result
            and not marker and event.get("isMeta") is not True
            and session_id and prompt_id and timestamp
        )
        if real_prompt:
            self._context = {"version": 1, "session_id": session_id, "prompt_id": prompt_id,
                             "anchor_event_id": event_id, "last_event_id": event_id}
        elif continued:
            self._context = {**context, "last_event_id": event_id}
        else:
            self._context = {"version": 1}
        return origin
