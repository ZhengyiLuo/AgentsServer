# Native Codex subagent identity

Codex provides a thread's display title separately from its agent nickname and
agent path. AgentsServer preserves the explicit `Thread.name` value as optional
`subagent_title`; it does not derive this title from `preview`, prompts, tool
output or a task-name guess. Existing `subagent_nickname`, `subagent_path` and
legacy `subagent_name` composition remain unchanged.

The existing descendant reconciliation reads `name` directly from each returned
thread. A known child's `thread/started` notification can supply the same field
immediately. Subsequent `thread/name/updated` notifications carry `threadId` and
`threadName`; these update only that child's title. No new provider RPC, polling,
directory scan or reconciliation loop is introduced.

An omitted title retains the previous value. Explicit null or an empty string
clears it, allowing clients to fall back to the real nickname/path. Malformed
non-string values are ignored rather than converted into labels. Valid strings
use the existing compact identity text bound. A title never overwrites the
legacy name field, so clearing it cannot resurrect the old title as a task name.

Identity-only updates preserve status, run/parent ownership, activity, summary,
log, start time, lifecycle timestamp and live-process generation. Late titles cannot reactivate a
completed child. Unknown, root or differently owned threads cannot create or
rename a child through these notifications. Repeated identical titles do not
append duplicate state events. A conflicting top-level/nested `thread/started`
identity is rejected.

All state transitions for one child serialize through a weakly held per-child
async lock, covering the previous-state read, event append and state publication.
This prevents concurrent parent lifecycle and child rename notifications from
overwriting each other's fields or resurrecting terminal state. Unrelated
children do not share the lock, and idle locks are not retained indefinitely.

Reopening a chat also repairs an already-recorded terminal child's changed native
title, nickname or path with one durable `subagent_state` correction. It preserves
the original lifecycle timestamp, status, run, start time, summary and log, so it
does not make completed work look new or active. Unchanged reads append nothing.
Recovery compares against the durable child record even if an older server
already learned the corrected identity in memory without an event ID/sequence;
omitted provider fields retain that recovered identity. Legacy metadata without
event identity can still rehydrate silently, and unknown historical terminal
children remain memory-only. Neither is advertised as a durable timeline event.

Reconciliation captures the child state before reading the provider and checks
it again under the child transition lock. A newer lifecycle or rename that
arrives during that read wins over the stale snapshot. No polling or additional
provider reads are added.

`test_codex_subagent_identity_isolated.py` exercises the extracted server
projector, emitter and reconciliation with synthetic state, plus the actual
app-server notification router without starting its process. Tests cover
real-title recovery, explicit clear, malformed values, ownership isolation,
terminal/live-state preservation, ordered started/rename notifications, and
gated rename/completion races in both orders.
It also covers terminal identity recovery, ID-less memory and legacy metadata,
repeat/restart idempotence, strict snapshot event identity, unchanged unread and
chat-recency projection, and reconciliation/new-run races in both orders.
