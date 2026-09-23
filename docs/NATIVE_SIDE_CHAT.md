# Native side conversations

Side chat uses the provider's conversation context, not a projection of the
desktop timeline. No main turn, queue item, goal mutation, or provider-history
import is created by a side question.

- Codex: `thread/fork` with `ephemeral: true`, reusing that fork for follow-ups.
  Its dedicated app-server process uses the parent's workspace and selected
  permissions. Like native Codex Side chat, it can read/search files, run
  non-mutating checks and use configured tools. Inherited tasks and approvals
  are reference context; mutations require a new explicit request in Side chat.
  Subagents remain unavailable. Native approval requests use the existing
  approval UI and belong only to the ephemeral child. The main run's internal
  AgentsDock helper transport and credentials are not inherited. The source
  thread is never resumed, interrupted or modified.
- Claude: native `side_question` control (the `/btw` operation) on the exact
  chat-owned SDK connection. Only connection acquisition is actor-serialized;
  waiting for a side answer never occupies the main-turn actor. Cancellation
  addresses only its control request. Cold connections must resume the exact
  parent identity; an empty conversation is not an acceptable fallback.
  A connected parent keeps its current settings for side questions, even if
  saved model or effort settings have changed for the next main turn.

The desktop requires `side_questions.version: 2` and `native_context: true`.
Each request supplies `request_id`, `side_chat_id`, `question`, and, for a
follow-up, `after_request_id`. The server deduplicates receipts and verifies
the predecessor before asking the provider. Client-authored history is refused.
Claude receives its native maximum of twenty server-owned side exchanges;
Codex retains its own ephemeral transcript. No parent text budget is applied.
Codex captures the main context on its first question; Clear starts a new fork
with the latest main context. Its workspace tools can inspect current files
even when the inherited conversation predates those files.

`DELETE /api/sessions/{session}/side-questions/{request}` cancels one request.
`DELETE /api/sessions/{session}/side-chats/{side_chat}` closes the side chat.
Both are native-operator-only and scoped to the authenticated owner and parent.
Clear, profile shutdown and server shutdown release owned resources. Idle side
contexts expire after thirty minutes without polling; stale follow-ups receive
410 rather than silently starting with different context. Cancellation/error
also closes the side conversation; the user can Clear to start again.

Tests must cover real tool-result context, successive questions, cancellation
with a running parent, unchanged parent goals/transcripts, native cold resume,
HTTP ownership, request deduplication and Clear arriving before a delayed POST.
Never import the production server or use a real user chat in test setup.
