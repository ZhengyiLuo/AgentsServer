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
    --auto-review. AgentsDock already solves this exact problem for Claude
    and Codex the same way (a per-session user-selectable permission mode,
    e.g. ClaudePermissionMenu.tsx / CodexPermissionMenu.tsx, stored on the
    session and read by the turn runner) rather than the server hard-coding
    one default - a Cursor permission mode should follow that precedent,
    not invent a new pattern. Not implemented here; this file has no
    server-side session model to attach it to yet.
  - stream-json can hang for minutes on some failure paths (reproduced
    directly: an unauthenticated run hung well past 120s before finally
    surfacing the same error --output-format json returned in under two
    seconds). Any real integration needs its own hard timeout around the
    subprocess, independent of whatever timeout the CLI itself claims.
"""

from __future__ import annotations

import json
import re
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


def parse_cursor_models_list(output: str) -> list[dict[str, Any]]:
    """Parse ``agent --list-models``'s plain-text output.

    Unlike Claude/Codex, this is not JSON - one "id - display name" pair per
    line, with a header line, a blank-line-separated footer tip, and no
    other structure. The model matching the id "auto" is Cursor's own
    server-side model router, not a concrete model; every other line is a
    concrete selectable model.
    """
    models: list[dict[str, Any]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("Available models") or line.startswith("Tip:"):
            continue
        if " - " not in line:
            continue
        model_id, _, label = line.partition(" - ")
        model_id = model_id.strip()
        label = label.strip()
        # Real captured formats have varied by CLI version: "(current,
        # default)" and, later, plain "(default)". Match "default" as a
        # word inside a trailing parenthetical rather than one exact phrase.
        default_match = re.search(r"\(\s*[^)]*\bdefault\b[^)]*\)\s*$", label, re.IGNORECASE)
        is_default = default_match is not None
        if default_match:
            label = label[: default_match.start()].strip()
        models.append({
            "id": model_id,
            "label": label,
            "is_router": model_id == "auto",
            "is_default": is_default,
        })
    return models


CURSOR_PERMISSION_MODES = ("default", "auto_review", "full_access", "plan")


def cursor_permission_flags(mode: str) -> list[str]:
    """Translate a single-enum permission mode (matching the precedent set by
    AgentsDock's ClaudePermissionMenu.tsx - one user-chosen mode per session,
    not a server-hardcoded default) into real Cursor CLI flags.

    Verified live against the actual `agent` CLI: --trust alone permits file
    reads/edits but shell calls come back rejected; --auto-review runs a
    server-side classifier that auto-approves safe commands and prompts for
    the rest (not usable headless, so treated as approval-required here);
    --force/--yolo allows every command unconditionally.
    """
    if mode == "plan":
        return ["--mode", "plan"]
    if mode == "auto_review":
        return ["--trust", "--auto-review"]
    if mode == "full_access":
        return ["--trust", "--force"]
    # "default" and any unrecognized mode fail safe to the most restrictive
    # real option: workspace trust only, shell commands rejected outright.
    return ["--trust"]


def build_cursor_cmd(
    sess: dict[str, Any],
    prompt: str,
    *,
    cursor_bin: str = "agent",
) -> list[str]:
    """Build the `agent` CLI argv for one turn from a session dict + prompt.

    Verified live: `agent -p "<prompt>" --output-format json --trust` runs
    non-interactively and returns a result event with a `session_id`; passing
    that id back via `--resume <session_id>` on a later call correctly
    resumes conversation context (both confirmed by hand against the real
    CLI, not inferred from --help text alone).
    """
    cmd = [cursor_bin, "-p", prompt, "--output-format", "stream-json"]
    resume_id = sess.get("cursor_session_id")
    if resume_id:
        cmd += ["--resume", str(resume_id)]
    # Reuses the generic "model" field (same one Claude/Codex sessions use)
    # rather than a cursor-specific field - already fully wired through
    # CreateSessionRequest/UpdateSessionRequest with no schema changes
    # needed, and matches the client's existing reset-on-backend-switch
    # behavior (`updateSession(id, { backend, model: null, effort: null })`).
    model = sess.get("model")
    if model:
        cmd += ["--model", str(model)]
    cmd += cursor_permission_flags(str(sess.get("cursor_permission_mode") or "default"))
    return cmd


def parse_cursor_auth_status(status_output: str) -> dict[str, Any]:
    """Parse ``agent status``'s one-line output into a ready/unauthenticated
    shape matching the vocabulary /api/runtime/catalog already reports for
    Claude and Codex (ready, missing, unauthenticated, error).
    """
    text = status_output.strip()
    if text.startswith("Not logged in"):
        return {
            "state": "unauthenticated",
            "action": "Run 'agent login', or set CURSOR_API_KEY.",
        }
    if text.startswith("✓ Logged in as "):
        email = text[len("✓ Logged in as "):].strip()
        return {"state": "ready", "email": email or None}
    return {"state": "error", "message": text}


# Real captured `agent about` output only ever showed a single free-form
# "Subscription Tier" value (e.g. "Pro"). Any tier that isn't recognized as a
# paid one is treated as free/unknown by cursor_account_is_free_tier() below,
# so a future tier name we haven't seen yet still locks named models rather
# than silently allowing turns that would fail server-side - failing toward
# the safer (if less convenient) UI state.
CURSOR_KNOWN_PAID_TIERS = {"pro", "pro+", "ultra", "business", "enterprise", "teams"}


def parse_cursor_account_tier(about_output: str) -> str | None:
    """Parse ``agent about``'s table output for the ``Subscription Tier`` row.

    Real captured output (2026.08.11-e8db854, authenticated):
        About Cursor CLI

        CLI Version         2026.08.11-e8db854
        Model               Claude Sonnet 5 300K High
        Subscription Tier   Pro
        OS                  darwin (arm64)
        ...

    Returns the raw tier string (e.g. "Pro") verbatim, or None if the row
    isn't present (older CLI, or output shape changed).
    """
    for raw_line in about_output.splitlines():
        line = raw_line.strip()
        if not line.lower().startswith("subscription tier"):
            continue
        _, _, value = line.partition("Subscription Tier")
        return value.strip() or None
    return None


def cursor_account_is_free_tier(tier: str | None) -> bool:
    """True unless the tier is recognized as a paid one.

    Deliberately fails toward "free" (locks named models in the picker) for
    an unparseable/unrecognized/missing tier, rather than toward "paid" -
    picking a named model that then fails server-side with
    ActionRequiredError is worse UX than a model staying locked one CLI
    release longer than strictly necessary.
    """
    clean = str(tier or "").strip().lower()
    return clean not in CURSOR_KNOWN_PAID_TIERS
