# Native side conversations

Side chat uses the provider's conversation context, not a projection of the
desktop timeline. No main turn, queue item, goal mutation, or provider-history
import is created by a side question.

- Codex: `thread/fork`, reusing that fork for follow-ups. Synced Side chat saves
  its native fork for resumption; the legacy request-scoped API uses an
  ephemeral fork. Native full-history subagent forks require a saved rollout
  and therefore work in synced Side chat, not the legacy ephemeral fork.
  Its dedicated app-server process uses the parent's workspace and selected
  permissions. Like native Codex Side chat, it can read/search files, run
  non-mutating checks and use configured tools. Inherited tasks and approvals
  are reference context; mutations require a new explicit request in Side chat.
  Native subagents remain available. Approval requests use the existing UI
  only for the side fork and descendants whose native lineage reaches it;
  inherited parent agents remain outside that ownership. The main run's internal
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
Claude receives up to twenty server-owned side exchanges; Codex retains its
own native transcript. Existing Side chat question, request, response and
legacy follow-up-history limits remain unchanged in this release.
Codex captures the main context on its first question; Clear starts a new fork
with the latest main context. Its workspace tools can inspect current files
even when the inherited conversation predates those files.

Servers advertising `side_questions.runtime_settings` accept optional Codex
`model` and `effort` on both submission routes. Selection changes apply to the
next turn in the existing child; they do not clear history or change the main
chat. Empty effort requests the normal Codex server default or the custom
endpoint default, while omitted fields preserve the side selection. Synchronized side chats persist their effective selection
alongside the native thread reference, and expose it in snapshots; legacy
answers expose it too. Claude `/btw` continues using its connected parent.

`DELETE /api/sessions/{session}/side-questions/{request}` cancels one request.
`DELETE /api/sessions/{session}/side-chats/{side_chat}` closes the side chat.
Both are native-operator-only and scoped to the authenticated owner and parent.
Clear, profile shutdown and server shutdown release owned resources. Synchronized Codex side
conversations resume their persisted native thread after transport cleanup;
there is no request execution deadline. Cancellation/error closes the owned
transport without interrupting the parent. The legacy ephemeral route cannot
resume a closed context.
Idle side contexts remain available while their owning runtime is active;
elapsed idle time does not discard native context.

Tests must cover real tool-result context, successive questions, cancellation
with a running parent, unchanged parent goals/transcripts, native cold resume,
HTTP ownership, request deduplication and Clear arriving before a delayed POST.
Never import the production server or use a real user chat in test setup.

## Live Codex activity

Synchronized exchanges expose additive `activity` items with stable `id`,
`kind` (`reasoning_summary`, `reasoning`, `commentary`, `tool`, `answer`),
`text` and `status`. Tool items can include their native `tool` type. Items
retain native order; completed aggregates replace streamed deltas without
duplication. Only user-visible native items from the side thread are projected.
Parent/descendant messages, user input, internal notifications and encrypted
reasoning payloads never become side assistant text. Reasoning availability
depends on what the selected provider actually exposes.

Updates coalesce token bursts, persist through the existing side-chat store,
and use revision invalidations rather than polling. Cancellation, failures and
reopening retain partial activity. The existing owner/conversation/request
checks prevent late progress from resurrecting a cleared exchange.

The desktop defaults to a single pulsing current-activity line, streams
commentary and answer text, and applies the shared Show reasoning traces
preference while reasoning is live. Completed/stopped activity is collapsed
under either preference and remains explicitly expandable. Claude native
`/btw` is unchanged; Codex streaming does not imply Claude streaming parity.
