# Native goal progress and retry notices

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

An ordinary send remains queued. Send now can steer a compatible plain-text
follow-up into the exact active native Codex goal turn without stopping that
turn or pausing its persistent goal. The client must advertise
`codex_goal_steer_v1`; model/runtime settings and the existing authority ceiling
must match. Files, structured references, `/mail`, and other special-purpose
deliveries do not use this lane. An incompatible follow-up stays queued with an
actionable conflict instead of falling through the explicit Stop lifecycle.

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

This is a local change. No live goal, server, saved history or provider transcript
is changed by verification, and no deployment is performed. Handler regression
tests extract only the relevant AST functions and use fake notifications,
ownership, clocks and cleanup; they must never import or start `agent_server`.
Focused checks cover admission, transport/writer races, chronological history,
and source-renderer follow-up/reopen/Pause behavior. In-memory provider tests
and isolated Studio renderer checks are not live-provider acceptance.
