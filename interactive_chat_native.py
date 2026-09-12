"""Shared-chat DTOs reuse native UI contracts without file or server authority."""
from __future__ import annotations

import math

PRIVATE_FIELDS = frozenset({
    "cwd", "path", "source_path", "root", "file", "files", "artifact", "artifacts",
    "file_ids", "display_file_ids", "file_path", "notebook_path", "old_path", "new_path",
    "attachments", "env", "environment", "runtime_env", "runtime_context", "authorization", "headers",
    "token", "access_token", "refresh_token", "api_key", "password", "secret",
    "authority", "authority_file", "authority_path", "request_prompt", "provider_cross_chat_route_snapshot",
    "secure_peer_route_snapshots", "chat_references", "team_references",
    "claude_session_id", "codex_thread_id", "cursor_session_id",
})
SESSION_FIELDS = frozenset({
    "id", "title", "backend", "model", "effort", "system_prompt", "backend_locked",
    "claude_permission_mode", "codex_approval_policy", "codex_sandbox_mode",
    "codex_permission_profile", "codex_approvals_reviewer", "cursor_permission_mode",
    "provider_jobs_access", "codex_goal", "codex_goal_time_budget_seconds",
    "codex_goal_time_budget_exhausted", "codex_thread_status",
    "codex_pending_interaction_count", "codex_needs_user_action",
    "claude_pending_interaction_count", "claude_needs_user_action",
    "claude_stop_fence_pending", "created_at", "updated_at", "archived",
    "latest_event_seq", "latest_event_at", "latest_agent_event_seq", "latest_agent_event_at",
})


def shared_native_value(value, depth=0):
    """Remove transport/file metadata, not words deliberately present in chat text.

    This is an egress filter, never an authorization decision. Every callback
    has already selected a single chat and used the native ownership checks.
    """
    if depth > 20:
        raise ValueError("Shared chat data is too deeply nested")
    if isinstance(value, dict):
        return {key: shared_native_value(item, depth + 1) for key, item in value.items()
                if isinstance(key, str) and not key.startswith("_")
                and key.lower() not in PRIVATE_FIELDS}
    if isinstance(value, (list, tuple)):
        return [shared_native_value(item, depth + 1) for item in value]
    if type(value) is float and not math.isfinite(value):
        raise ValueError("Shared chat data contains an invalid number")
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError("Shared chat data is not serializable")


def shared_session(value):
    return shared_native_value({key: item for key, item in value.items() if key in SESSION_FIELDS})


def shared_events(events, session_id):
    output = []
    for event in events:
        if not isinstance(event, dict) or event.get("session_id") not in (None, "", session_id):
            raise ValueError("Shared chat event ownership is invalid")
        kind = str(event.get("type") or "")
        # No artifact endpoint or file metadata is shared. Tool output already
        # in the conversation is chat content, not direct filesystem access.
        if kind.startswith(("file_", "artifact_", "workspace_", "terminal_")):
            continue
        output.append(shared_native_value(event))
    return output
