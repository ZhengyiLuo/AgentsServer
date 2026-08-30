"""Cursor Agent CLI translation and command construction for AgentsServer.

The parser normalizes newline-delimited ``stream-json`` emitted by compatible
``cursor-agent`` and ``agent`` builds into the provider-neutral lifecycle used
by AgentsDock.  Fixtures in :mod:`test_cursor_agent_client` are captured from
an authenticated Cursor CLI, and the server independently probes the selected
executable before advertising this backend.

Permission names describe headless behavior, not an interactive approval UI:
``default`` allows workspace reads and edits while rejecting shell commands,
``full_access`` forces commands, and ``plan`` requests planning mode. Cursor's
interactive auto-review mode is intentionally not exposed until AgentsServer
has a bounded approval bridge. The
server also owns wall-clock/startup timeouts and bounded concurrent stderr
draining; this module deliberately contains no process lifecycle state.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator


class CursorEventParseError(ValueError):
    """A stream-json line was not valid JSON or had an unrecognized shape."""


CURSOR_SESSION_IDENTIFIER_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$"
)
CURSOR_CALL_ID_MAX_CHARS = 240


def canonical_cursor_session_id(value: Any) -> str | None:
    """Return one bounded Cursor resume id or reject protocol/schema drift."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise CursorEventParseError("Cursor session_id must be a string")
    clean = value.strip()
    if not CURSOR_SESSION_IDENTIFIER_RE.fullmatch(clean):
        raise CursorEventParseError(
            "Cursor session_id is not a valid bounded local identifier"
        )
    return clean


def canonical_cursor_call_id(value: Any) -> str:
    """Validate the opaque correlation key used by Cursor tool events."""

    if not isinstance(value, str):
        raise CursorEventParseError("tool_call call_id must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > CURSOR_CALL_ID_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise CursorEventParseError(
            "tool_call call_id must be a bounded nonempty identifier"
        )
    return value


def cursor_approval_request_text(value: str) -> bool:
    """Recognize approval prompts that a headless runner cannot answer."""

    compact = re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()
    return any(marker in compact for marker in (
        "request approval",
        "approval required",
        "waiting for approval",
        "permission request",
        "requires confirmation",
        "approve this command",
    ))


def _tool_call_name_and_body(tool_call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Cursor nests each tool call under a dynamic ``<name>ToolCall`` key
    (e.g. ``readToolCall``, ``editToolCall``, ``shellToolCall``, ``globToolCall``)
    instead of a stable ``name`` field. Extract both generically.
    """
    for key, body in tool_call.items():
        if key.endswith("ToolCall") and isinstance(body, dict):
            return key[: -len("ToolCall")], body
    raise CursorEventParseError("tool_call payload has no supported *ToolCall key")


def normalize_cursor_stream_event(raw_line: str) -> dict[str, Any] | None:
    """Parse one raw stream-json line into a small, neutral event shape.

    Returns None for event types the server deliberately does not
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
        raise CursorEventParseError("Cursor stream line is not valid JSON") from exc
    if not isinstance(event, dict):
        raise CursorEventParseError("Cursor stream line must be a JSON object")

    event_type = event.get("type")
    session_id = canonical_cursor_session_id(event.get("session_id"))

    if event_type == "system" and event.get("subtype") == "init":
        if not session_id:
            raise CursorEventParseError("system init missing session_id")
        return {
            "kind": "session_started",
            "session_id": session_id,
            "cwd": event.get("cwd"),
            "model": event.get("model"),
        }

    if event_type == "user":
        # AgentsServer already has the prompt it sent; nothing new here.
        return None

    if event_type == "interaction_query":
        # The CLI raises and immediately answers its own interactive query
        # (observed live: a `request` is always followed by a `response`
        # with no input from us). It carries no turn output, and treating it
        # as unrecognized used to abort otherwise-healthy turns - plan mode
        # emits one on every run, so every plan turn died on it.
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
        raise CursorEventParseError("unrecognized thinking subtype")

    if event_type == "tool_call":
        subtype = event.get("subtype")
        tool_call = event.get("tool_call")
        if not isinstance(tool_call, dict):
            raise CursorEventParseError("tool_call event missing tool_call payload")
        name, body = _tool_call_name_and_body(tool_call)
        call_id = canonical_cursor_call_id(event.get("call_id"))
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
            if not isinstance(result, dict):
                raise CursorEventParseError(
                    "completed tool_call result must be an object"
                )
            # Shell calls can come back as {"rejected": {...}} instead of
            # {"success": {...}} - approval/sandbox policy declined the
            # call rather than it failing. Surface that distinctly; it is
            # not the same thing as a tool erroring.
            if "rejected" in result:
                rejected = result.get("rejected")
                if not isinstance(rejected, dict):
                    raise CursorEventParseError(
                        "rejected tool_call result must be an object"
                    )
                return {
                    "kind": "tool_rejected",
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool": name,
                    "reason": rejected.get("reason") or None,
                }
            return {
                "kind": "tool_finished",
                "session_id": session_id,
                "call_id": call_id,
                "tool": name,
                "result": result.get("success", result),
            }
        raise CursorEventParseError("unrecognized tool_call subtype")

    if event_type == "assistant":
        message = event.get("message") or {}
        if not isinstance(message, dict):
            raise CursorEventParseError("assistant message must be an object")
        content = message.get("content") or []
        if not isinstance(content, list):
            raise CursorEventParseError("assistant content must be a list")
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return {
            "kind": "assistant_text",
            "session_id": session_id,
            "text": "".join(parts),
        }

    if event_type == "result":
        if not session_id:
            raise CursorEventParseError("result missing session_id")
        subtype = event.get("subtype")
        is_error = event.get("is_error")
        result_text = event.get("result", "")
        if subtype not in {"success", "error"}:
            raise CursorEventParseError("unrecognized result subtype")
        if not isinstance(is_error, bool):
            raise CursorEventParseError("result is_error must be a boolean")
        if (subtype == "success") == is_error:
            raise CursorEventParseError(
                "result subtype and is_error disagree"
            )
        if not isinstance(result_text, str):
            raise CursorEventParseError("result text must be a string")
        return {
            "kind": "turn_finished",
            "session_id": session_id,
            "is_error": is_error,
            "result_text": result_text,
            "duration_ms": event.get("duration_ms"),
            "usage": event.get("usage"),
        }

    raise CursorEventParseError("unrecognized Cursor stream event type")


def normalize_cursor_stream(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Normalize a full stream-json output, skipping events with no projection."""
    for raw_line in lines:
        normalized = normalize_cursor_stream_event(raw_line)
        if normalized is not None:
            yield normalized


def parse_cursor_models_list(output: str) -> list[dict[str, Any]]:
    """Parse Cursor's ``--list-models`` plain-text output.

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


CURSOR_PERMISSION_MODES = ("default", "full_access", "plan")


def cursor_permission_flags(mode: str) -> list[str]:
    """Translate a single-enum permission mode (matching the precedent set by
    AgentsDock's ClaudePermissionMenu.tsx - one user-chosen mode per session,
    not a server-hardcoded default) into real Cursor CLI flags.

    Verified live against the actual `agent` CLI: --trust alone permits file
    reads/edits but shell calls come back rejected; --force/--yolo allows
    every command unconditionally. Interactive auto-review is deliberately
    unavailable because this headless runner cannot answer approval prompts.
    """
    if mode == "plan":
        # --trust is workspace trust, not an edit grant, and the CLI refuses
        # to run non-interactively without it in any mode ("Pass --trust,
        # --yolo, or -f if you trust this directory") - so omitting it made
        # every plan-mode turn fail to launch. Verified live that
        # `--trust --mode plan` still refuses to write files.
        return ["--trust", "--mode", "plan"]
    if mode == "full_access":
        return ["--trust", "--force"]
    # "default" and any unrecognized mode use workspace access: reads and
    # edits are allowed, while shell commands are rejected. This is not an
    # interactive "ask" mode and clients must not label it as one.
    return ["--trust"]


def build_cursor_cmd(
    sess: dict[str, Any],
    prompt: str,
    *,
    cursor_bin: str = "cursor-agent",
) -> list[str]:
    """Build one Cursor CLI argv from a session dict and composed prompt.

    Verified live: `agent -p "<prompt>" --output-format json --trust` runs
    non-interactively and returns a result event with a `session_id`; passing
    that id back via `--resume <session_id>` on a later call correctly
    resumes conversation context (both confirmed by hand against the real
    CLI, not inferred from --help text alone). The prompt remains a positional
    argument for this beta because the exact compatible CLI build has not yet
    been authenticated and proven to preserve resume + stream-json semantics
    when the prompt is supplied over stdin.
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


def parse_cursor_auth_status(
    status_output: str,
    *,
    executable_name: str = "cursor-agent",
) -> dict[str, Any]:
    """Parse Cursor ``status`` output into a ready/unauthenticated
    shape matching the vocabulary /api/runtime/catalog already reports for
    Claude and Codex (ready, missing, unauthenticated, error).
    """
    text = status_output.strip()
    lower = text.lower()
    if any(marker in lower for marker in (
        "not logged in",
        "not authenticated",
        "authentication required",
        "login required",
    )):
        return {
            "state": "unauthenticated",
            "action": (
                f"Run '{executable_name} login', or set CURSOR_API_KEY."
            ),
        }
    logged_in_match = re.search(r"(?:✓\s*)?logged in as\s+(.+)$", text, re.IGNORECASE)
    if logged_in_match:
        email = logged_in_match.group(1).strip()
        return {"state": "ready", "email": email or None}
    if "authenticated" in lower and not any(
        marker in lower for marker in ("not authenticated", "unauthenticated")
    ):
        return {"state": "ready", "email": None}
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
