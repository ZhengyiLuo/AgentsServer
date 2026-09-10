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

Force Send still pauses the persistent goal and runs the promoted message as
an ordinary turn. It does not silently resume a goal afterward. Desktop copy
distinguishes a paused goal from an active ordinary message.

This is a local change. No live goal, server, saved history or provider transcript
is changed by verification, and no deployment is performed. Handler regression
tests extract only the relevant AST functions and use fake notifications,
ownership, clocks and cleanup; they must never import or start `agent_server`.
