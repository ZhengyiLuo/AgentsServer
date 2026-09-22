# AgentsServer 1.0.0-beta.8

## Steer an active Codex goal

Plain text and attachment follow-ups can steer an active goal even when the
client automatically attaches previously saved chat-route metadata. The goal
keeps its existing owner, authority, model and native turn. The follow-up does
not apply the queued route snapshot, pause the goal or interrupt its work.

This covers both resumed native goals and goals created during an ordinary or
scheduled turn. Explicit new references, provider commands, changed runtime
settings and stale-owner deliveries still require their existing safe paths.
User Stop and Pause remain authoritative. Ordinary non-goal steering retains
its existing authority-rotation checks.

## Configure native Codex subagent concurrency

An authenticated native-admin endpoint exposes the server's existing Codex
subagent concurrency override for the desktop Server settings form:
`GET/PUT /api/admin/codex/subagents`. Set
`max_concurrent_threads_per_session` to a positive integer, or `null` to remove
the server override. An unset value uses Codex's own default; it is not an
unlimited setting. The primary agent is excluded from this provider setting.

Changes apply to new or reloaded native Codex threads. Existing chats can use
Reload provider when idle; saving the setting never reloads or interrupts
running agents. Chat-specific overrides retain priority. The legacy exec
transport reports this control as unavailable. Settings writes preserve other
Codex settings, including when changing the persistent-goals switch.

## Compatibility and rollout

Includes beta.7's completed-subagent identity recovery. Existing desktop builds
with goal steering support can use the steering correction after a server
update. The new settings field requires the matching desktop build.

API contract 28, Team Hub schema 22, dependencies and signing key are unchanged.
No background polling, provider requests on settings save, or wake loops are
introduced. Publication is separate from installing or restarting a live
server; use the managed updater when idle unless interruption is authorized.
