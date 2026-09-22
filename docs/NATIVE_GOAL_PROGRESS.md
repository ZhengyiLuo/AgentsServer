# Native goal progress and retry notices

## Ordinary parent runs with unfinished subagents

An ordinary run does not need a persistent goal to collect delegated results.
If its native parent completes while owned children are active, retain the same
run, native thread subscription and authority. Track child lifecycle events in
receive order, rather than polling or consulting asynchronously updated cards.
Once those children drain, send one native `turn/start` with `input: []` to
consume Codex's pending child notifications. Do not manufacture a user prompt
or copy the child answer into another input. A native spontaneous continuation
takes precedence over that request at the actual transport write boundary.

Keep earlier answers at their original positions. Only the latest collected
answer becomes the surrounding run's terminal result. Explicit Stop and
owner/process changes fence further continuation; Send now retains its existing
control semantics. Historical child cards, unrelated chats and children of a
fork's source do not establish live ownership for the fork.

The empty-input primitive was verified against native Codex
`0.154.0-alpha.6.2`, with both v1 and v2 subagents, using a sandboxed local fake
Responses provider. In each case the child completed after the parent, no
spontaneous parent turn followed during the bounded observation, and empty
input produced a new parent turn with the exact native child result and no new
`userMessage` item. These are real native protocol checks with synthetic model
responses, not a claim of real-model reasoning or production GUI validation.

## Persistent native goals

One persistent native Codex goal reservation spans several provider turns.
An intermediate final answer is not the end of that AgentsDock operation.
Keep the shared `run_id` and preserve `provider_turn_id`, `item_id`, and the
public phase on projected commentary/final/tool activity. The desktop uses
chronological answer boundaries to show later work below an earlier answer.

Reasoning projection accepts public summary data only, not raw reasoning text
deltas or a reasoning item's raw text. Plans and public commentary have their
own phases and buffers. Old unclassified history is not rewritten or guessed
to be commentary.

An error notification with boolean `willRetry: true` is retry progress, not a
terminal failure. Legacy `Reconnecting... N/N` notices receive the same treatment
unless `willRetry` is explicitly false. Successful `turn/completed` clears stale
per-turn error state; a new native goal turn starts with clean terminal state.
Actual failed completion, timeout, subscription closure and explicit non-retry
errors remain subject to the existing failure and cleanup handling.

An ordinary send remains queued. Send now can steer compatible text and uploaded
attachments into the exact active native Codex goal turn without stopping that
turn or pausing its persistent goal. The client must advertise
`codex_goal_steer_v1`; model/runtime settings and the existing authority ceiling
must match. Structured references, `/mail`, and other special-purpose
deliveries do not use this lane. An incompatible follow-up stays queued with an
actionable conflict instead of falling through the explicit Stop lifecycle.
Between turns of an owned native goal, Send now holds one follow-up on the
existing notification stream and binds it to the next ready native turn.
It does not require repeated clicks, add a poller or resume the goal. If an
active cached goal has no local owner/steering transport, the follow-up remains
queued; missing ownership is never permission to invoke Stop.

The transport rechecks the exact goal, run, reservation, native turn, process
generation, and Pause/Stop state immediately before writing. Accepted input is
recorded once as `turn_steered`, after all provider notifications preceding its
acknowledgement. This creates a chronological user-message boundary, not a new
run or a synthetic goal prompt. Later progress stays below that follow-up;
history reconciliation credits it so reopening cannot import a duplicate.

Explicit Pause and Stop still pause the goal. They are never silently undone.
A safely rejected, already-dequeued follow-up is restored with a durable pause
hold and requires an explicit retry. Uncertain delivery is fenced against
automatic replay. Consumer cleanup settles outstanding callers even when it
is cancelled again during cleanup.

## Goals activated during an ordinary reply

The first turn that creates a goal supports the same owner-preserving steering
as its later continuations. Its separate goal-only queue stays available when
run-bound provider authority disables ordinary logical-run replacement. It
reuses the exact provider turn, subscription, run ID and authority; it does not
issue a new authority, interrupt background tools or create a replacement run.
The native goal consumer takes over only after the first turn actually ends.
Standalone and selected-provider-command runs do not gain this additional lane.

The original consumer also rechecks goal activation after queue admission so
an instruction admitted just before goal creation does not switch logical
ownership. Attachments use the ordinary validated upload prompt path, and
their display IDs are recorded on the single user-authored steering event.
Both steering queues are retired before completion/cancellation cleanup and
excluded from public runtime snapshots.

If a steer acknowledgement arrives after the first turn completed and the next
one already started, its pending receipt transfers with the unread notification
stream. The native consumer processes that next turn's identity and progress
before committing the follow-up at its acknowledgement boundary. Cancellation
or failed handoff settles the receipt as uncertain without replaying it.
Once a goal follow-up is accepted or its delivery is uncertain, context-error
recovery cannot replay the first turn's stale prompt. Continued goal operations
likewise do not fall back to replaying that original request; their later
progress remains visible even when a steering acknowledgement is lost.

Resuming or creating a goal while an ordinary reply is running must not end its
local owner when that first provider turn completes. This was missing in beta.54:
the ordinary runner finalized, and the ownerless-continuation guard later paused
the otherwise active goal. Force Send-only verification did not cover this path.

Ordinary chat runs now retain their existing thread notification stream from
before `turn/start`. When a goal continues, the same supervised run consumes it
inline, preserving its runtime authority, thread pin, manifest watcher and event
identity. No synthetic user prompt, detached successor or new polling is added.
The current native turn and matching control reservation support Stop, Delete
and follow-up steering. Only the outer runner publishes the final terminal event
and releases the run.

The ordinary-to-goal transition and the eventual terminal decision serialize
with goal controls under the existing chat lifecycle lock. An accepted Resume
therefore keeps its owner; activation after terminal admission has closed is
rejected for retry, rather than accepted without a consumer. Already-buffered
continuation output is drained even if the provider completed the goal before
the consumer caught up. Receive-order watermarks survive the handoff.

Validation uses the actual extracted runner, native consumer and Stop handler,
plus retained-stream transport checks. These are isolated protocol/lifecycle
checks, not a claim of live-provider or production-UI acceptance. No live goal,
saved transcript or running research task is modified by these checks.

This is a local change. No live goal, server, saved history or provider transcript
is changed by verification, and no deployment is performed. Handler regression
tests extract only the relevant AST functions and use fake notifications,
ownership, clocks and cleanup; they must never import or start `agent_server`.
Focused checks cover admission, transport/writer races, chronological history,
and source-renderer follow-up/reopen/Pause behavior. In-memory provider tests
and isolated Studio renderer checks are not live-provider acceptance.
