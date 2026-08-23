"""Prototype translation layer for the Cursor CLI (``agent``) backend.

Research scaffold only. Nothing in agent_server.py imports this yet, and no
existing Claude/Codex code path is touched by this file. It exists to answer
one question concretely, against real captured CLI output rather than
documentation: can Cursor's ``--output-format stream-json`` events be
normalized into the same shape AgentsServer already uses for Claude/Codex
turns (tool call lifecycle, assistant text, terminal result)?

Captured against real `agent` CLI output (version 2026.08.11-e8db854,
authenticated), not inferred from documentation. See
test_cursor_agent_client.py for the exact captured fixture lines.

Known gaps this prototype does NOT attempt to resolve yet:
  - Permission/approval strategy for unattended (job/scheduled) turns:
    --trust alone permits file reads/edits but not shell execution; shell
    calls come back as {"result": {"rejected": ...}} without --force or
    --auto-review. Which of those AgentsServer should default to is a
    product decision, not resolved here.
  - stream-json can hang for minutes on some failure paths (reproduced
    directly: an unauthenticated run hung well past 120s before finally
    surfacing the same error --output-format json returned in under two
    seconds). Any real integration needs its own hard timeout around the
    subprocess, independent of whatever timeout the CLI itself claims.
  - Model catalog discovery: `agent --list-models` returns a flat text
    list ("id - name" per line), not JSON. A real discover_cursor_catalog()
    would parse that text, the same shape of problem
    parse_claude_help_catalog() already solves for Claude's --help output,
    but that parser is not written here.
"""

from __future__ import annotations

import json
from typing import Any, Iterator


class CursorEventParseError(ValueError):
    """A stream-json line was not valid JSON or had an unrecognized shape."""


def _tool_call_name_and_body(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Cursor nests each tool call under a dynamic ``<name>ToolCall`` key
    (e.g. ``readToolCall``, ``editToolCall``, ``shellToolCall``, ``globToolCall``)
    instead of a stable ``name`` field. Extract both generically.
    """
    for key, body in tool_call.items():
        if key.endswith("ToolCall") and isinstance(body, dict):
            return key[: -len("ToolCall")], body
    raise CursorEventParseError(
        f"no *ToolCall key found in tool_call payload: {sorted(tool_call)}"
    )


def normalize_cursor_stream_event(raw_line: str) -> dict[str, Any] | None:
    """Parse one raw stream-json line into a small, neutral event shape.

    Returns None for event types this prototype deliberately does not
    project (e.g. the "user" echo, which AgentsServer already has from the
    prompt it sent). Raises CursorEventParseError for anything unrecognized,
    on purpose - silently dropping an unknown event type would hide a real
    schema change in a future Cursor CLI release.
    """
    raw_line = raw_line.strip()
    if not raw_line:
        return None
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise CursorEventParseError(f"not valid JSON: {raw_line!r}") from exc
    if not isinstance(event, dict):
        raise CursorEventParseError(f"expected a JSON object, got: {raw_line!r}")

    event_type = event.get("type")
    session_id = event.get("session_id")

    if event_type == "system" and event.get("subtype") == "init":
        return {
            "kind": "session_started",
            "session_id": session_id,
            "cwd": event.get("cwd"),
            "model": event.get("model"),
        }

    if event_type == "user":
        # AgentsServer already has the prompt it sent; nothing new here.
        return None

    if event_type == "thinking":
        subtype = event.get("subtype")
        if subtype == "delta":
            return {
                "kind": "reasoning_delta",
                "session_id": session_id,
                "text": event.get("text", ""),
            }
        if subtype == "completed":
            return {"kind": "reasoning_completed", "session_id": session_id}
        raise CursorEventParseError(f"unrecognized thinking subtype: {subtype!r}")

    if event_type == "tool_call":
        subtype = event.get("subtype")
        tool_call = event.get("tool_call")
        if not isinstance(tool_call, dict):
            raise CursorEventParseError("tool_call event missing tool_call payload")
        name, body = _tool_call_name_and_body(tool_call)
        call_id = event.get("call_id")
        if subtype == "started":
            return {
                "kind": "tool_started",
                "session_id": session_id,
                "call_id": call_id,
                "tool": name,
                "args": body.get("args"),
            }
        if subtype == "completed":
            result = body.get("result") or {}
            # Shell calls can come back as {"rejected": {...}} instead of
            # {"success": {...}} - approval/sandbox policy declined the
            # call rather than it failing. Surface that distinctly; it is
            # not the same thing as a tool erroring.
            if "rejected" in result:
                return {
                    "kind": "tool_rejected",
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool": name,
                    "reason": (result.get("rejected") or {}).get("reason") or None,
                }
            return {
                "kind": "tool_finished",
                "session_id": session_id,
                "call_id": call_id,
                "tool": name,
                "result": result.get("success", result),
            }
        raise CursorEventParseError(f"unrecognized tool_call subtype: {subtype!r}")

    if event_type == "assistant":
        message = event.get("message") or {}
        parts = [
            part.get("text", "")
            for part in message.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return {
            "kind": "assistant_text",
            "session_id": session_id,
            "text": "".join(parts),
        }

    if event_type == "result":
        return {
            "kind": "turn_finished",
            "session_id": session_id,
            "is_error": bool(event.get("is_error")),
            "result_text": event.get("result", ""),
            "duration_ms": event.get("duration_ms"),
            "usage": event.get("usage"),
        }

    raise CursorEventParseError(f"unrecognized event type: {event_type!r}")


def normalize_cursor_stream(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Normalize a full stream-json output, skipping events with no projection."""
    for raw_line in lines:
        normalized = normalize_cursor_stream_event(raw_line)
        if normalized is not None:
            yield normalized
