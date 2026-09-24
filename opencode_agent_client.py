"""OpenCode CLI translation and command construction for AgentsServer.

The parser normalizes the newline-delimited JSON that ``opencode run
--format json`` emits into the same provider-neutral event shape the Cursor
backend already produces, so the production turn runner can reuse the existing
timeline vocabulary rather than inventing a fourth one. Fixtures in
test_opencode_agent_client.py are captured verbatim from a real run of
opencode 1.18.29, not inferred from documentation.

Several behaviours here exist because they were measured, not assumed:

  - OpenCode allows every tool by default, including ``bash``, with no
    prompt. Restricting a turn therefore means *adding* deny rules rather
    than withholding an allow flag, which is the opposite of Cursor.
  - Denying one tool does not deny the outcome: with only ``bash`` denied a
    model reached for ``write`` and produced the same file. A read-only mode
    has to deny the whole mutating set.

  - The workspace is passed explicitly as ``--dir`` as well as the child
    process cwd, avoiding reliance on implicit CLI workspace resolution.
  - Config sources deep-merge, and managed config loads after
    ``OPENCODE_CONFIG_CONTENT``. Per-turn restrictions therefore live on a
    cryptographically unique primary agent selected with ``--agent``. OpenCode
    appends that selected agent's permissions after global/user permissions,
    while later config sources cannot predict the random agent key to replace
    it.

The default mode deliberately matches OpenCode's own behaviour rather than
Cursor's, so an operator who already runs ``opencode`` gets the same agent
through AgentsDock that they get from their terminal.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from typing import Any, Iterator


class OpenCodeEventParseError(ValueError):
    """A stream line was not valid JSON or had an unrecognized shape."""


OPENCODE_SESSION_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
OPENCODE_CALL_ID_MAX_CHARS = 240

OPENCODE_PERMISSION_MODES = ("default", "full_access", "plan")
OPENCODE_DEFAULT_PERMISSION_MODE = "default"

# Every tool that can change the workspace or the machine. Denying only one of
# these is not a restriction: a model blocked from `bash` will use `write` to
# reach the same result (observed directly against the real CLI).
OPENCODE_MUTATING_TOOLS = ("bash", "write", "edit", "patch")
OPENCODE_DELEGATION_TOOL = "task"

# Not a real tool: OpenCode reports a call to a withheld tool under this name.
OPENCODE_UNAVAILABLE_TOOL_NAME = "invalid"
OPENCODE_ENFORCED_AGENT_RE = re.compile(
    r"^agentsdock-turn-[0-9a-f]{64}$"
)


def new_opencode_enforced_agent_name() -> str:
    """Return an unguessable one-turn primary-agent config key."""

    return f"agentsdock-turn-{secrets.token_hex(32)}"


def canonical_opencode_session_id(value: Any) -> str | None:
    """Return one bounded resume id, or reject protocol/schema drift."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise OpenCodeEventParseError("OpenCode sessionID must be a string")
    clean = value.strip()
    if not OPENCODE_SESSION_IDENTIFIER_RE.fullmatch(clean):
        raise OpenCodeEventParseError(
            "OpenCode sessionID is not a valid bounded local identifier"
        )
    return clean


def canonical_opencode_call_id(value: Any) -> str:
    """Validate the opaque correlation key used by OpenCode tool events."""

    if not isinstance(value, str):
        raise OpenCodeEventParseError("tool callID must be a string")
    if (
        not value
        or value != value.strip()
        or len(value) > OPENCODE_CALL_ID_MAX_CHARS
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise OpenCodeEventParseError(
            "tool callID must be a bounded nonempty identifier"
        )
    return value


def _bounded_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or value > 2_147_483_647:
        return None
    return value


def normalize_opencode_usage(tokens: Any, cost: Any) -> dict[str, Any]:
    """Project the token/cost accounting a step_finish carries."""

    usage: dict[str, Any] = {}
    if isinstance(tokens, dict):
        for source, target in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("reasoning", "reasoning_tokens"),
            ("total", "total_tokens"),
        ):
            bounded = _bounded_int(tokens.get(source))
            if bounded is not None:
                usage[target] = bounded
        cache = tokens.get("cache")
        if isinstance(cache, dict):
            for source, target in (
                ("read", "cache_read_tokens"),
                ("write", "cache_write_tokens"),
            ):
                bounded = _bounded_int(cache.get(source))
                if bounded is not None:
                    usage[target] = bounded
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        if 0 <= float(cost) < 1_000_000:
            usage["cost"] = float(cost)
    return usage


def opencode_error_message(error: Any) -> str:
    """Extract a human message from OpenCode's nested error payload.

    Real shape: ``{"name": "UnknownError", "data": {"message": ..., "ref": ...}}``.
    The name is kept when a message is absent so a failure is never reported as
    an empty string. Text is provider-controlled; the caller bounds it.
    """

    if isinstance(error, str):
        return error
    if not isinstance(error, dict):
        return ""
    data = error.get("data")
    message = ""
    if isinstance(data, dict):
        message = str(data.get("message") or "")
    if not message:
        message = str(error.get("message") or "")
    name = str(error.get("name") or "")
    if message and name:
        return f"{name}: {message}"
    return message or name


def normalize_opencode_stream_event(raw_line: str) -> dict[str, Any] | None:
    """Parse one ``--format json`` line into a provider-neutral event.

    Returns None for lines that carry no timeline projection. Raises
    OpenCodeEventParseError for anything unrecognized, deliberately: silently
    dropping an unknown event type would hide a real schema change in a future
    OpenCode release, which is exactly the failure mode that made a Codex
    regression invisible.
    """

    raw_line = raw_line.strip()
    if not raw_line:
        return None
    try:
        event = json.loads(raw_line)
    except json.JSONDecodeError as exc:
        raise OpenCodeEventParseError("OpenCode stream line is not valid JSON") from exc
    if not isinstance(event, dict):
        raise OpenCodeEventParseError("OpenCode stream line must be a JSON object")

    event_type = event.get("type")
    session_id = canonical_opencode_session_id(event.get("sessionID"))

    if event_type == "error":
        # Captured shape: an error event carries `error`, not `part`. Requiring
        # `part` here would turn every provider failure into a parse failure
        # and report the wrong cause for the turn.
        return {
            "kind": "turn_error",
            "session_id": session_id,
            "message": opencode_error_message(event.get("error")),
        }

    part = event.get("part")
    if not isinstance(part, dict):
        raise OpenCodeEventParseError("OpenCode event is missing its part payload")

    part_session_id = canonical_opencode_session_id(part.get("sessionID"))
    if session_id and part_session_id and session_id != part_session_id:
        raise OpenCodeEventParseError(
            "OpenCode event and part carry different sessionIDs"
        )
    session_id = session_id or part_session_id

    expected_part_types = {
        "step_start": "step-start",
        "text": "text",
        "reasoning": "reasoning",
        "tool_use": "tool",
        "step_finish": "step-finish",
    }
    expected_part_type = expected_part_types.get(event_type)
    part_type = part.get("type")
    if (
        expected_part_type is not None
        and part_type is not None
        and part_type != expected_part_type
    ):
        raise OpenCodeEventParseError(
            "OpenCode event type does not match its part payload"
        )

    if event_type == "step_start":
        # A step boundary carries no user-visible content of its own.
        return None

    if event_type == "text":
        text = part.get("text")
        if not isinstance(text, str):
            raise OpenCodeEventParseError("OpenCode text payload must be a string")
        return {
            "kind": "assistant_text",
            "session_id": session_id,
            "text": text,
        }

    if event_type == "reasoning":
        text = part.get("text")
        if not isinstance(text, str):
            raise OpenCodeEventParseError(
                "OpenCode reasoning payload must be a string"
            )
        return {
            "kind": "reasoning_delta",
            "session_id": session_id,
            "text": text,
        }

    if event_type == "tool_use":
        return _normalize_tool_use(session_id, part)

    if event_type == "step_finish":
        reason = part.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise OpenCodeEventParseError(
                "OpenCode step_finish reason must be a nonempty string"
            )
        return {
            "kind": "step_finished",
            "session_id": session_id,
            "reason": reason.strip(),
            "usage": normalize_opencode_usage(part.get("tokens"), part.get("cost")),
        }

    raise OpenCodeEventParseError("unrecognized OpenCode stream event type")


def _normalize_tool_use(session_id: str | None, part: dict[str, Any]) -> dict[str, Any]:
    tool = str(part.get("tool") or "tool")
    call_id = canonical_opencode_call_id(part.get("callID"))
    state = part.get("state")
    if not isinstance(state, dict):
        raise OpenCodeEventParseError("tool event is missing its state payload")
    status = str(state.get("status") or "")

    if tool == OPENCODE_UNAVAILABLE_TOOL_NAME:
        # Second observed denial shape: when a tool is withheld by config,
        # OpenCode drops it from the model's toolset, and a model that calls
        # it anyway gets back a synthetic tool literally named "invalid" with
        # status "completed". Classifying that as a successful tool call would
        # report a blocked action as having run.
        return {
            "kind": "tool_rejected",
            "session_id": session_id,
            "call_id": call_id,
            "tool": tool,
            "args": state.get("input") if isinstance(state.get("input"), dict) else {},
            "reason": str(state.get("output") or "") or None,
        }

    common = {
        "session_id": session_id,
        "call_id": call_id,
        "tool": tool,
        "args": state.get("input") if isinstance(state.get("input"), dict) else {},
    }

    if status in {"pending", "running"}:
        return {"kind": "tool_started", **common}
    if status == "completed":
        return {
            "kind": "tool_finished",
            **common,
            "result": state.get("output"),
        }
    if status in {"invalid", "denied"}:
        # OpenCode reports a configuration-denied tool as `invalid` rather
        # than erroring the turn, so this must stay distinguishable from a
        # tool that ran and failed.
        return {
            "kind": "tool_rejected",
            **common,
            "reason": str(state.get("error") or "") or None,
        }
    if status == "error":
        return {
            "kind": "tool_failed",
            **common,
            "reason": str(state.get("error") or "") or None,
        }
    raise OpenCodeEventParseError("unrecognized OpenCode tool status")


def normalize_opencode_stream(lines: Iterator[str]) -> Iterator[dict[str, Any]]:
    """Normalize a whole stream, skipping events with no projection."""

    for raw_line in lines:
        normalized = normalize_opencode_stream_event(raw_line)
        if normalized is not None:
            yield normalized


def opencode_permission_config(mode: str) -> dict[str, Any] | None:
    """Build the server-enforced subset for one turn's tool permissions.

    OpenCode only documents a global `opencode.json`, which would be useless
    for per-session modes, but the binary also honours an inline
    `OPENCODE_CONFIG_CONTENT` environment variable - verified live to actually
    block a denied tool. Because AgentsServer already composes a fresh
    environment per turn, that gives real per-session permission modes without
    touching the operator's own config file.

    Returns None for the default mode. ``build_opencode_env_overrides`` places
    this subset on a random selected primary agent instead of in global
    permission config. The default mode names nothing, leaving OpenCode and
    operator permission rules intact.
    """

    if mode == "full_access":
        # An explicit override, unlike the default: this one still applies
        # when the operator's own config denies something.
        return {"permission": {tool: "allow" for tool in OPENCODE_MUTATING_TOOLS}}
    if mode == "plan":
        return {
            "permission": {
                **{tool: "deny" for tool in OPENCODE_MUTATING_TOOLS},
                # OpenCode task children do not inherit the selected primary
                # agent's permission object. Without this boundary a Plan
                # agent could delegate the same bash/write operation.
                OPENCODE_DELEGATION_TOOL: "deny",
            }
        }
    # "default" and any unrecognized mode: defer entirely to OpenCode. It
    # allows every tool including bash with no prompt, which is broader than
    # the Cursor default, and that difference is intentional - the backend
    # behaves the way its own users already expect it to.
    return None


def build_opencode_env_overrides(
    mode: str,
    existing_config: str | None = None,
    instruction_paths: list[str] | tuple[str, ...] = (),
    deny_skill_tool: bool = False,
    enforced_agent_name: str | None = None,
) -> dict[str, str]:
    """Return the environment additions that carry the permission mode.

    Empty for the pure default mode, so the operator's own config and default
    agent stay in force. Enforced Plan/full-access and selected-skill rules are
    attached to one cryptographically unique primary agent. OpenCode resolves
    an unknown selected agent from defaults plus global/user configuration,
    then appends that agent's permissions last. Project, global, environment,
    and managed wildcard permissions therefore cannot supersede the exact
    one-turn rules, and a later managed source cannot predict the random key.

    Attachments are deliberately absent from permission composition.
    AgentsServer bounds them before turn acceptance, and native ``--file``
    ingestion bypasses the ``external_directory`` permission check.
    Selected-skill resources likewise receive no automatic read grant: the
    validated SKILL.md body is injected, while referenced files remain subject
    to the operator's existing OpenCode permissions until a no-symlink bounded
    resource snapshot is implemented.
    """

    permission_config = opencode_permission_config(mode)
    enforced_permissions: dict[str, Any] = {}
    if permission_config is not None:
        enforced_permissions.update(permission_config["permission"])
    if deny_skill_tool:
        enforced_permissions["skill"] = "deny"
        # A selected-skill turn must not escape its skill=deny boundary by
        # creating an OpenCode subagent, whose permissions are resolved anew.
        enforced_permissions[OPENCODE_DELEGATION_TOOL] = "deny"

    if enforced_permissions:
        if (
            not isinstance(enforced_agent_name, str)
            or OPENCODE_ENFORCED_AGENT_RE.fullmatch(enforced_agent_name) is None
        ):
            raise ValueError(
                "OpenCode enforced turns require a valid one-turn agent name"
            )
    elif enforced_agent_name is not None:
        raise ValueError(
            "OpenCode default turns cannot select an enforced agent"
        )

    normalized_instruction_paths: list[str] = []
    for value in instruction_paths:
        raw = str(value or "").strip()
        if not raw or "\x00" in raw:
            raise ValueError("OpenCode instruction paths must be absolute files")
        normalized = os.path.normpath(os.path.expanduser(raw))
        if not os.path.isabs(normalized):
            raise ValueError("OpenCode instruction paths must be absolute files")
        if normalized not in normalized_instruction_paths:
            normalized_instruction_paths.append(normalized)

    if (
        permission_config is None
        and not normalized_instruction_paths
        and not deny_skill_tool
    ):
        return {}

    config: dict[str, Any] = {}
    if existing_config is not None:
        try:
            decoded = json.loads(existing_config)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError(
                "existing OpenCode inline config is not valid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError(
                "existing OpenCode inline config must be a JSON object"
            )
        config.update(decoded)

    if enforced_permissions:
        existing_agents = config.get("agent", {})
        if not isinstance(existing_agents, dict):
            raise ValueError(
                "existing OpenCode inline agents must be a JSON object"
            )
        agents = dict(existing_agents)
        # A generated name has 256 bits of entropy, but remove/reinsert anyway
        # so a deterministic test name or an astronomically unlikely collision
        # cannot retain any inherited fields.
        agents.pop(enforced_agent_name, None)
        agents[enforced_agent_name] = {
            "description": "AgentsDock one-turn permission boundary",
            "mode": "primary",
            "permission": dict(enforced_permissions),
        }
        config["agent"] = agents
    if normalized_instruction_paths:
        existing_instructions = config.get("instructions", [])
        if not isinstance(existing_instructions, list) or not all(
            isinstance(value, str) for value in existing_instructions
        ):
            raise ValueError(
                "existing OpenCode inline instructions must be a string array"
            )
        merged_instructions = list(existing_instructions)
        for path in normalized_instruction_paths:
            if path not in merged_instructions:
                merged_instructions.append(path)
        config["instructions"] = merged_instructions
    return {
        "OPENCODE_CONFIG_CONTENT": json.dumps(config, separators=(",", ":"))
    }


def build_opencode_cmd(
    sess: dict[str, Any],
    prompt: str,
    *,
    opencode_bin: str = "opencode",
    workdir: str | None = None,
    attachment_paths: list[str] | tuple[str, ...] = (),
    enforced_agent_name: str | None = None,
) -> list[str]:
    """Build one `opencode run` argv from a session dict and composed prompt.

    Verified live: `opencode run --format json` streams newline-delimited
    events, every one of which already carries the sessionID, and passing that
    id back via `-s <id>` on a later call resumes the conversation (a number
    stated in the first turn was recalled in the second).

    `workdir` is passed explicitly as ``--dir`` so workspace selection does not
    depend on implicit CLI cwd or configuration resolution. Each validated
    current-turn upload is attached through OpenCode's repeatable ``--file``
    option, which is required for true multimodal/image input.
    """

    cmd = [opencode_bin, "run", "--format", "json"]
    workdir = workdir or str(sess.get("cwd") or "").strip()
    if workdir:
        cmd += ["--dir", workdir]
    resume_id = sess.get("opencode_session_id")
    if resume_id:
        cmd += ["-s", str(resume_id)]
        # Session-level permission rules are applied after agent rules. Forking
        # an enforced resume preserves message history but intentionally does
        # not copy those mutable session permissions.
        if enforced_agent_name is not None:
            cmd.append("--fork")
    model = sess.get("model")
    if model:
        cmd += ["-m", str(model)]
    variant = sess.get("effort")
    if variant:
        cmd += ["--variant", str(variant)]
    if enforced_agent_name is not None:
        if OPENCODE_ENFORCED_AGENT_RE.fullmatch(enforced_agent_name) is None:
            raise ValueError("invalid OpenCode one-turn agent name")
        cmd += ["--agent", enforced_agent_name]
    for value in attachment_paths:
        raw = str(value or "").strip()
        if not raw or "\x00" in raw:
            raise ValueError("OpenCode attachment paths must be absolute files")
        normalized = os.path.normpath(os.path.expanduser(raw))
        if not os.path.isabs(normalized):
            raise ValueError("OpenCode attachment paths must be absolute files")
        # A separate argv element keeps whitespace, leading dashes in the file
        # name, and shell metacharacters inert.
        cmd += ["--file", normalized]
    # ``--file`` is variadic in OpenCode 1.18.29, so a following positional
    # prompt is otherwise consumed as one more filename. Terminate option
    # parsing when this helper is used with both files and a non-empty prompt.
    # The production runner sends its prompt over stdin and removes the empty
    # positional placeholder, so that path deliberately needs no sentinel.
    if attachment_paths and prompt:
        cmd.append("--")
    # The prompt is positional and must stay last so no later flag can be
    # parsed out of provider-controlled text.
    cmd.append(prompt)
    return cmd


def parse_opencode_models_list(output: str) -> list[dict[str, Any]]:
    """Parse `opencode models` output: one `provider/model` id per line."""

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or "/" not in line or " " in line:
            continue
        if line in seen:
            continue
        seen.add(line)
        provider, _, name = line.partition("/")
        models.append({
            "id": line,
            "provider": provider,
            "label": name.replace("-", " ").strip() or line,
            "is_free": name.endswith("-free"),
        })
    return models


OPENCODE_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Captured verbatim: resuming an unknown session exits 1 with this on stderr
# and not a single JSON event on stdout, so the resume failure is invisible to
# the stream parser and has to be recognised here.
OPENCODE_RESUME_FAILURE_MARKERS = (
    "session not found",
    "no such session",
)


def strip_opencode_ansi(text: str) -> str:
    """Remove the colour codes OpenCode writes to a non-tty stderr."""

    return OPENCODE_ANSI_ESCAPE_RE.sub("", str(text or ""))


def opencode_stderr_diagnostic(stderr: bytes | str) -> str:
    """Return a short, decoded, colour-free tail of OpenCode's stderr."""

    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    clean = strip_opencode_ansi(stderr)
    lines = [line.strip() for line in clean.splitlines() if line.strip()]
    if not lines:
        return ""
    return " / ".join(lines[-3:])[:500]


def opencode_resume_failure(stderr: bytes | str) -> bool:
    """Report whether a turn failed because the resumed session is gone.

    OpenCode does not fall back to a new session; it exits nonzero having done
    nothing. Treating that as a generic failure would strand the chat on a
    dead session id forever, so the runner needs to tell this case apart.
    """

    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", "replace")
    lowered = strip_opencode_ansi(stderr).lower()
    return any(marker in lowered for marker in OPENCODE_RESUME_FAILURE_MARKERS)


def merge_opencode_usage(
    total: dict[str, Any], step_usage: dict[str, Any]
) -> dict[str, Any]:
    """Accumulate per-step usage into one per-turn total.

    Unlike Cursor, which reports usage once at the end of a turn, OpenCode
    emits token counts on every `step_finish`. Taking only the last one would
    under-report a multi-step turn.
    """

    merged = dict(total)
    for key, value in step_usage.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        merged[key] = merged.get(key, 0) + value
    return merged


def opencode_live_event_summary(event: dict[str, Any]) -> str:
    """One short line describing a normalized event for the live stdout tail."""

    kind = str(event.get("kind") or "")
    if kind in {
        "tool_started", "tool_finished", "tool_rejected", "tool_failed"
    }:
        return f"{kind}: {str(event.get('tool') or 'tool')[:80]}"
    if kind in {"assistant_text", "reasoning_delta"}:
        return f"{kind}: {str(event.get('text') or '')[:120]}"
    if kind == "step_finished":
        return f"step_finished: {str(event.get('reason') or '')[:40]}"
    if kind == "turn_error":
        return f"turn_error: {str(event.get('message') or '')[:120]}"
    return kind
