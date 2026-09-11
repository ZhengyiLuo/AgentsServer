"""Cursor Agent CLI translation and command construction for AgentsServer.

The parser normalizes newline-delimited ``stream-json`` emitted by compatible
``cursor-agent`` and ``agent`` builds into the provider-neutral lifecycle used
by AgentsDock.  Fixtures in :mod:`test_cursor_agent_client` are captured from
an authenticated Cursor CLI, and the server independently probes the selected
executable before advertising this backend.

Permission names describe headless behavior, not an interactive approval UI:
``default`` uses Cursor's configured permissions without forcing tools,
``full_access`` force-allows commands except explicit Cursor deny rules, and
``plan`` requests planning mode. Cursor's
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
CURSOR_LIFECYCLE_VALUE_MAX = 2_147_483_647
CURSOR_ANSI_ESCAPE_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
CURSOR_TOOL_SUCCESS_OUTCOMES = (
    "success",
    "complete",
    "registered",
    "startSuccess",
    "saveSuccess",
    "discardSuccess",
)
CURSOR_TOOL_STATUS_OUTCOMES = (
    # These current protobuf-JSON oneofs describe a valid tool response, but
    # not completion of the underlying work. Project them as neutral status
    # instead of the green/successful tool completion used for real outcomes.
    "approved",
    "async",
    "stillRunning",
)
CURSOR_TOOL_CONFIRMATION_OUTCOMES = (
    # AgentsServer has no Cursor approval bridge. This must fail closed rather
    # than claim that the requested PR-management action completed.
    "needsConfirmation",
)
CURSOR_TOOL_FAILURE_OUTCOMES = (
    "failure",
    "error",
    "timeout",
    "spawnError",
    "permissionDenied",
    "readPermissionDenied",
    "writePermissionDenied",
    "fileNotFound",
    "invalidFile",
    "notFile",
    "fileBusy",
    "noSpace",
    "sandboxUnsupported",
    "notFound",
    "serverNotFound",
    "toolNotFound",
)


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


def _cursor_lifecycle_session_id(event: dict[str, Any], event_name: str) -> str:
    session_id = canonical_cursor_session_id(event.get("session_id"))
    if not session_id:
        raise CursorEventParseError(f"{event_name} missing session_id")
    return session_id


def _bounded_cursor_lifecycle_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > CURSOR_LIFECYCLE_VALUE_MAX:
        return None
    return value


def _cursor_tool_exit_code(value: Any, *, default: int) -> int:
    """Return a bounded tool exit status from a Cursor result envelope."""

    if not isinstance(value, dict):
        return default
    exit_code = value.get("exitCode", value.get("exit_code"))
    if (
        isinstance(exit_code, bool)
        or not isinstance(exit_code, int)
        or exit_code < -CURSOR_LIFECYCLE_VALUE_MAX
        or exit_code > CURSOR_LIFECYCLE_VALUE_MAX
    ):
        return default
    return exit_code


def _cursor_tool_result_outcome(
    value: Any,
) -> tuple[str, dict[str, Any]]:
    """Extract one protobuf-JSON tool result outcome and fail on drift."""

    if not isinstance(value, dict):
        raise CursorEventParseError(
            "completed tool_call result must be an object"
        )
    outcome_keys = (
        *CURSOR_TOOL_SUCCESS_OUTCOMES,
        *CURSOR_TOOL_STATUS_OUTCOMES,
        *CURSOR_TOOL_CONFIRMATION_OUTCOMES,
        "rejected",
        *CURSOR_TOOL_FAILURE_OUTCOMES,
    )
    present = [key for key in outcome_keys if key in value]
    if len(present) != 1:
        raise CursorEventParseError(
            "completed tool_call result must contain exactly one supported "
            "outcome"
        )
    outcome = present[0]
    payload = value.get(outcome)
    if not isinstance(payload, dict):
        raise CursorEventParseError(
            f"{outcome} tool_call result must be an object"
        )
    return outcome, payload


def _bounded_cursor_query_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if len(clean) > 80 or not re.fullmatch(r"[A-Za-z0-9_.:-]+", clean):
        return None
    return clean


def _normalize_cursor_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for source, target in (
        ("inputTokens", "input_tokens"),
        ("outputTokens", "output_tokens"),
        ("cacheReadTokens", "cache_read_tokens"),
        ("cacheWriteTokens", "cache_write_tokens"),
    ):
        bounded = _bounded_cursor_lifecycle_int(value.get(source))
        if bounded is not None:
            normalized[target] = bounded
    return normalized


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

    if event_type == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            if not session_id:
                raise CursorEventParseError("system init missing session_id")
            return {
                "kind": "session_started",
                "session_id": session_id,
                "cwd": event.get("cwd"),
                "model": event.get("model"),
            }
        if subtype == "task_notification":
            return {
                "kind": "background_task_notice",
                "session_id": _cursor_lifecycle_session_id(
                    event, "system task_notification"
                ),
            }
        if subtype == "background_shell_timeout":
            return {
                "kind": "background_task_timeout",
                "session_id": _cursor_lifecycle_session_id(
                    event, "system background_shell_timeout"
                ),
                "aborted_count": _bounded_cursor_lifecycle_int(
                    event.get("aborted_count")
                ),
                "timeout_ms": _bounded_cursor_lifecycle_int(
                    event.get("timeout_ms")
                ),
            }
        raise CursorEventParseError("unrecognized system subtype")

    if event_type == "user":
        # AgentsServer already has the prompt it sent; nothing new here.
        return None

    if event_type == "retry":
        subtype = event.get("subtype")
        if subtype not in {"starting", "resuming"}:
            raise CursorEventParseError("unrecognized retry subtype")
        return {
            "kind": "provider_status",
            "session_id": _cursor_lifecycle_session_id(event, "retry"),
            "status": f"retry_{subtype}",
            "attempt": _bounded_cursor_lifecycle_int(event.get("attempt")),
        }

    if event_type == "connection":
        subtype = event.get("subtype")
        if subtype not in {"reconnecting", "reconnected"}:
            raise CursorEventParseError("unrecognized connection subtype")
        return {
            "kind": "provider_status",
            "session_id": _cursor_lifecycle_session_id(event, "connection"),
            "status": subtype,
            "attempt": _bounded_cursor_lifecycle_int(event.get("attempt")),
        }

    if event_type == "interaction_query":
        subtype = event.get("subtype")
        if subtype not in {"request", "response"}:
            raise CursorEventParseError("unrecognized interaction_query subtype")
        # The query/response objects can contain prompts, commands, URLs, and
        # provider permission decisions. Cursor headless mode resolves these
        # internally, so retain only a schema marker and never project either
        # payload into AgentsDock history or logs.
        return {
            "kind": "provider_interaction",
            "session_id": _cursor_lifecycle_session_id(
                event, "interaction_query"
            ),
            "phase": subtype,
            "query_type": _bounded_cursor_query_type(event.get("query_type")),
        }

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
            outcome, outcome_payload = _cursor_tool_result_outcome(
                body.get("result")
            )
            if outcome in CURSOR_TOOL_STATUS_OUTCOMES:
                status_messages = {
                    "approved": (
                        "Cursor approved the requested action; any underlying "
                        "execution is reported separately."
                    ),
                    "async": "Cursor queued an asynchronous question.",
                    "stillRunning": (
                        "Cursor reports that the underlying task is still "
                        "running."
                    ),
                }
                return {
                    "kind": "tool_status",
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool": name,
                    "args": body.get("args"),
                    "status": outcome,
                    "message": status_messages[outcome],
                }
            if outcome in CURSOR_TOOL_CONFIRMATION_OUTCOMES:
                return {
                    "kind": "tool_confirmation_required",
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool": name,
                    "args": body.get("args"),
                    "message": (
                        "Cursor requires confirmation before this action can "
                        "continue."
                    ),
                }
            # Shell calls can come back as {"rejected": {...}} instead of
            # {"success": {...}} - approval/sandbox policy declined the
            # call rather than it failing. Surface that distinctly; it is
            # not the same thing as a tool erroring.
            if outcome == "rejected":
                return {
                    "kind": "tool_rejected",
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool": name,
                    "args": body.get("args"),
                    "reason": outcome_payload.get("reason") or None,
                }
            if outcome in CURSOR_TOOL_FAILURE_OUTCOMES:
                return {
                    "kind": "tool_finished",
                    "session_id": session_id,
                    "call_id": call_id,
                    "tool": name,
                    "args": body.get("args"),
                    "result": outcome_payload,
                    "exit_code": _cursor_tool_exit_code(
                        outcome_payload,
                        default=1,
                    ),
                }
            return {
                "kind": "tool_finished",
                "session_id": session_id,
                "call_id": call_id,
                "tool": name,
                "args": body.get("args"),
                "result": outcome_payload,
                "exit_code": _cursor_tool_exit_code(
                    outcome_payload,
                    default=0,
                ),
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
            "usage": _normalize_cursor_usage(event.get("usage")),
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

    ``--trust`` acknowledges workspace trust but does not override Cursor's
    global/project permission configuration. ``--force`` auto-allows commands
    except operations explicitly denied by Cursor configuration. Interactive
    auto-review is deliberately unavailable because this headless runner
    cannot answer approval prompts.
    """
    if mode == "plan":
        return ["--trust", "--mode", "plan"]
    if mode == "full_access":
        return ["--trust", "--force"]
    # "default" and any unrecognized mode use Cursor's configured permissions
    # without force. This is not an interactive "ask" mode and clients must
    # not label it as one.
    return ["--trust"]


def build_cursor_cmd(
    sess: dict[str, Any],
    *,
    cursor_bin: str = "cursor-agent",
) -> list[str]:
    """Build one Cursor CLI argv from a session dict and composed prompt.

    The prompt is deliberately supplied over stdin by the runner. Cursor's
    documented headless mode accepts piped input, and keeping generated policy,
    user text, and scoped authority out of argv prevents same-user process
    listings from exposing the complete composed prompt.
    """
    cmd = [cursor_bin, "-p", "--output-format", "stream-json"]
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
    text = CURSOR_ANSI_ESCAPE_RE.sub("", status_output).strip()
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
    if re.search(
        r"(?:is_?authenticated|authenticated)\s*[\":=]*\s*false\b",
        lower,
    ):
        return {
            "state": "unauthenticated",
            "action": (
                f"Run '{executable_name} login', or set CURSOR_API_KEY."
            ),
        }
    logged_in_match = re.search(
        r"(?:^|\n)\s*(?:✓\s*)?logged in as\s+([^\r\n]+)",
        text,
        re.IGNORECASE,
    )
    if logged_in_match:
        email = logged_in_match.group(1).strip()
        return {"state": "ready", "email": email or None}
    if re.search(
        r"(?:is_?authenticated|authenticated)\s*[\":=]*\s*true\b",
        lower,
    ) or re.search(r"(?:^|\n)\s*authenticated\s*$", lower):
        return {"state": "ready", "email": None}
    return {"state": "error", "message": text}


# Cursor reports a free-form "Subscription Tier" value. Only explicit free
# labels lock named models; unknown/future paid labels remain usable and Cursor
# itself returns the authoritative model-access error when necessary.
CURSOR_EXPLICIT_FREE_TIERS = {"free", "hobby", "individual free"}


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
        match = re.match(
            r"^\s*subscription\s+tier\s*(?::\s*|\s+)(.*?)\s*$",
            raw_line,
            re.IGNORECASE,
        )
        if match:
            return match.group(1).strip() or None
    return None


def cursor_account_is_free_tier(tier: str | None) -> bool:
    """True only for an explicit Cursor free-tier label.

    ``agent about`` may report service accounts, API-key users, or future paid
    plans with an unfamiliar label. Treating every unknown as free silently
    disabled named models for paying users; Cursor remains the final authority
    if an account cannot use a selected model.
    """
    clean = re.sub(r"[^a-z0-9]+", " ", str(tier or "").strip().lower()).strip()
    return clean in CURSOR_EXPLICIT_FREE_TIERS
