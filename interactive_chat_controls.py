"""Explicit one-chat controls; no server imports, HTTP proxy, or authority lookup.

The router authenticates the share and supplies its chat identity. The owner
injects native models/callbacks and projects their results for the guest. Native
exceptions are deliberately not converted to safe denials: a callback may have
already committed work. Only ChatControlError is known to precede invocation.
"""
from __future__ import annotations

import json
import re
from types import MappingProxyType


SETTINGS_FIELDS = frozenset({
    "title", "backend", "model", "effort", "system_prompt",
    "claude_permission_mode", "codex_approval_policy", "codex_sandbox_mode",
    "codex_permission_profile", "codex_approvals_reviewer", "cursor_permission_mode",
    "provider_jobs_access",
})
GOAL_FIELDS = frozenset({"objective", "status", "token_budget", "time_budget_seconds"})
JOB_FIELDS = frozenset({
    "title", "prompt", "schedule_kind", "interval_seconds", "cron_expression",
    "rrule", "timezone", "loop", "max_runs", "enabled", "backend",
})
ACTIONS = frozenset({
    "state", "turn.stop", "turn.steer", "queue.run_now", "queue.edit", "queue.delete",
    "queue.move", "settings.update", "goal.set", "goal.resume", "goal.pause", "goal.delete",
    "job.create", "job.update", "job.delete", "job.toggle", "job.run", "approval.respond",
})


class ChatControlError(ValueError):
    """A bounded rejection before any native callback or mutation."""

    def __init__(self, code="invalid_request"):
        self.code = code
        super().__init__("This chat control request is not permitted" if code == "forbidden"
                         else "Invalid chat control request")


def _identity(value):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", value) is None:
        raise ChatControlError()
    return value


def _fields(payload, allowed=(), required=(), *, nonempty=False):
    if set(payload) - set(allowed) or not set(required).issubset(payload) or nonempty and not payload:
        raise ChatControlError()


class InteractiveChatControls:
    """Dispatch to fixed native functions with a server-bound first argument.

    Callback keys normally equal action names; approval uses approval.codex or
    approval.claude. Native models are keyed by their class names. turn.steer is
    the owner's enqueue/exact-promotion wrapper; job.run is the owner's atomic
    expected-session wrapper, never the global unscoped run_job endpoint.
    """

    def __init__(self, callbacks, models):
        self.callbacks = MappingProxyType(dict(callbacks))
        self.models = MappingProxyType(dict(models))

    def _model(self, name, fields):
        model = self.models.get(name)
        if model is None:
            raise ChatControlError("forbidden")
        try:
            return model.model_validate(fields)
        except ValueError:
            # Never expose native validation input, paths or object reprs.
            raise ChatControlError() from None

    async def dispatch(self, session_id, action, payload, *, share_id=None, request_id=None):
        _identity(session_id)
        if not isinstance(action, str) or action not in ACTIONS:
            raise ChatControlError("forbidden")
        if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
            raise ChatControlError()
        try:
            encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise ChatControlError() from None
        if len(encoded) > 64 * 1024:
            raise ChatControlError()
        payload = json.loads(encoded)  # Callback sees an owned immutable-in-transit value.
        callback_key, args, kwargs = action, [session_id], {}

        if action in {"state", "turn.stop", "goal.delete", "goal.resume", "goal.pause"}:
            _fields(payload)
            if action in {"goal.resume", "goal.pause"}:
                args.append(self._model("CodexGoalRequest", {"status": "active" if action == "goal.resume" else "paused"}))
        elif action == "turn.steer":
            _fields(payload, {"prompt"}, {"prompt"})
            if not isinstance(payload["prompt"], str) or not payload["prompt"].strip():
                raise ChatControlError()
            if not isinstance(share_id, str) or re.fullmatch(r"interactive_[a-f0-9]{32}", share_id) is None:
                raise ChatControlError("forbidden")
            if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id) is None:
                raise ChatControlError()
            args.append(payload["prompt"])
            kwargs = {"share_id": share_id, "request_id": request_id}
        elif action in {"queue.delete", "queue.run_now", "job.delete", "job.run"}:
            _fields(payload, {"id"}, {"id"})
            args.append(_identity(payload["id"]))
            if action == "queue.run_now":
                args.append(self._model("RunQueuedTurnNowRequest", {"accept_deferred_queue_response": True}))
        elif action == "queue.edit":
            _fields(payload, {"id", "prompt", "expected_message_revision"}, {"id", "prompt"})
            args.append(_identity(payload.pop("id")))
            if not isinstance(payload["prompt"], str):
                raise ChatControlError()
            args.append(self._model("UpdateQueuedTurnRequest", payload))
        elif action == "queue.move":
            _fields(payload, {"id", "direction", "expected_adjacent_queued_id"}, {"id", "direction"})
            args.append(_identity(payload.pop("id")))
            if not isinstance(payload["direction"], str) or payload["direction"] not in {"up", "down"}:
                raise ChatControlError()
            if "expected_adjacent_queued_id" in payload:
                _identity(payload["expected_adjacent_queued_id"])
            args.append(self._model("MoveQueuedTurnRequest", payload))
        elif action == "settings.update":
            _fields(payload, SETTINGS_FIELDS, nonempty=True)
            args.append(self._model("UpdateSessionRequest", payload))
        elif action == "goal.set":
            _fields(payload, GOAL_FIELDS, nonempty=True)
            args.append(self._model("CodexGoalRequest", payload))
        elif action == "job.create":
            _fields(payload, JOB_FIELDS | {"first_run_at"}, {"title", "prompt"})
            args.append(self._model("CreateScopedJobRequest", {**payload, "context_mode": "chat"}))
        elif action in {"job.update", "job.toggle"}:
            allowed = {"id", "enabled"} if action == "job.toggle" else JOB_FIELDS | {"id", "next_run_at"}
            _fields(payload, allowed, {"id", "enabled"} if action == "job.toggle" else {"id"})
            args.append(_identity(payload.pop("id")))
            if not payload or action == "job.toggle" and type(payload["enabled"]) is not bool:
                raise ChatControlError()
            args.append(self._model("UpdateJobRequest", payload))
        elif action == "approval.respond":
            _fields(payload, {"id", "backend", "response"}, {"id", "backend", "response"})
            backend = payload["backend"]
            if backend not in ("codex", "claude") or not isinstance(payload["response"], dict):
                raise ChatControlError()
            args.append(_identity(payload["id"]))
            args.append(self._model("CodexInteractionResponseRequest" if backend == "codex" else "ClaudeInteractionResponseRequest",
                                    {"response": payload["response"]}))
            callback_key = "approval." + backend

        callback = self.callbacks.get(callback_key)
        if not callable(callback):
            raise ChatControlError("forbidden")
        return await callback(*args, **kwargs)
