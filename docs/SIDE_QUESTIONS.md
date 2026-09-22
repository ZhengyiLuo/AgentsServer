# Side questions (legacy v1)

Superseded in AgentsServer 1.0.4-beta.1 by
[Native side conversations](NATIVE_SIDE_CHAT.md). This document describes the
historical snapshot-based v1 API, not the current implementation. Current clients
must use the v2 native-context contract; v1 requests are rejected with HTTP 409.

Side questions answer a question or follow-up about existing conversation text in
a separate, temporary provider invocation. They do not resume or fork the parent
provider session, append events, create a saved chat, modify goals, acquire main-turn
authority, enter the main queue, or interrupt active work. There is no background
poller. Requests use the native owner token boundary; shared-chat guests and
provider helper credentials cannot call these endpoints.

Health advertises the additive capability without changing older turn contracts:

```json
{"side_questions":{"available":true,"version":1,"backends":["codex","claude"],"max_question_chars":8000,"history":true,"max_history_items":32,"max_history_chars":60000}}
```

`POST /api/sessions/{session_id}/side-questions` accepts `request_id`, `question`,
and optional `history`, with no other fields. The existing first-question shape
remains valid:

```json
{"request_id":"c5dd5825-9eea-42af-b0af-2069d147589a","question":"Why did we choose that approach?"}
```

For a follow-up, clients include the previous completed side-conversation pairs:

```json
{"request_id":"new-request-id","question":"What was the main tradeoff?","history":[{"role":"user","text":"Why did we choose that approach?"},{"role":"assistant","text":"It matched the existing design."}]}
```

`history` is an array of at most 32 messages and 60,000 total Unicode characters.
Every entry contains exactly `role` and `text`; roles alternate `user`,
`assistant`, starting with `user` and ending with `assistant`. Text must be a
nonblank, valid Unicode string and is preserved verbatim, including whitespace.
Empty or omitted history starts a first question. Explicit `null`, incomplete
pairs, extra fields, unsupported roles, malformed Unicode, and oversized history
return 400 before provider execution. The entire encoded JSON body is bounded to
512 KiB; larger bodies return 413.

Clients gate nonempty history on `history: true`; capability version remains 1
because existing one-question requests and responses are unchanged. A client can
keep a longer local panel transcript while submitting the newest complete pairs
that fit these limits. The server never trims supplied side history silently.

It waits for a single JSON answer:

```json
{"request_id":"c5dd5825-9eea-42af-b0af-2069d147589a","session_id":"chat-id","backend":"claude","answer":"...","context_note":"Uses recent visible conversation text; excludes tool results, attachments, automated turns, and hidden provider context."}
```

Questions must contain 1–8,000 Unicode characters. Identifiers use 1–128 ASCII
letters, digits, underscores or hyphens. Provider execution has a 150-second
request deadline. Responses are `Cache-Control: no-store`. There is no new
per-chat or server-wide concurrency limit.

`DELETE /api/sessions/{session_id}/side-questions/{request_id}` cancels only that
owner's exact side question and waits for its independent provider cleanup. It
returns `{"request_id":"...","status":"cancelled"}` or `"not_found"`. A
DELETE arriving before POST creates a short cancellation tombstone, preventing
the delayed POST from starting after the panel closes. Cancellation never calls
the parent's stop/interrupt endpoint. HTTP disconnect cancels work when the last
duplicate waiter leaves. Server shutdown closes the independent registry.

Identical concurrent/retried POSTs coalesce on owner, session and request ID.
Changing a question or its side history under the same ID returns 409. Omitted
and empty history are equivalent. Accepted history is copied into immutable
receipt identity, so later caller or callback mutations cannot change a retry.
Answers, failures and cancellation receipts remain only in process memory for ten minutes and are
pruned on demand. There is no durable side-question history. Clients should use
a new random request ID for each new question, and retain the original server
profile when cancelling after a profile switch.

## Context and provider isolation

The adapter reads at most the final 4 MiB of the existing session event log at a
fixed byte boundary. It keeps up to 60,000 characters of recent user and public
assistant text, deduplicates final answers, and applies the existing provenance
and generated-context stripping. Partial JSONL records, queued messages, hidden
reasoning, tool events, scheduled/internal turns and their output are excluded.
A tail starting inside an unknown run is skipped until a proven user boundary.
This can omit the beginning of a very long active run; an empty usable snapshot
returns 409. A context note always describes these limitations and additionally
discloses older text omitted by the byte or text budget. No provider-history
import, repair, transcript indexing, or session save is triggered.

The parent snapshot and optional `side_history` are separate quoted JSON fields
under a side-question system instruction. Side history is client-supplied
background, not trusted provider messages, instructions, or authority; its role
labels never become provider API roles. Every follow-up still uses a fresh
temporary provider invocation and the current bounded parent snapshot. The
adapter never includes the parent's tool schema, provider thread ID, goal state,
hook configuration, chat helper authority or terminal
identity. Fresh process environments retain CLI authentication while stripping
AgentsDock and legacy chat/run credentials.

Claude uses a fresh print process in a temporary directory, with `--safe-mode`,
`--tools ''`, an empty strict MCP configuration, disabled settings sources and
hooks, disabled slash commands, and `--no-session-persistence`. Installed versions
without the required safety flags return 503 before submitting a question.
Temporary directories are removed after the exact owned process group is reaped.
Failures expose concise provider errors rather than raw stderr or credentials.
An explicit temporary name avoids a separate model request to generate a title.

Codex is handled by the separate `codex_side_question.py` adapter. Its isolated
ephemeral app-server thread receives the same bounded text snapshot and no parent
session identity. See that adapter's explicit tool/environment configuration;
unsupported isolation must fail before submitting a normal agent turn.

Codex validates the installed protocol's explicit empty-environment semantics
before starting. Both the fresh ephemeral thread and its single turn use empty
environments; workspace shell/file access is absent. Integrations, subagents,
goals, skills, hosted tools and notification hooks are disabled independently.
The owned app-server process keeps the configured provider runtime and sign-in
state, with temporary logs and no persistent side-conversation history. Runtime
database/cache writes by Codex remain possible; ephemeral conversation history
does not imply a separate provider database. This avoids re-indexing existing
history into a fresh database for every question. Harmless
model-advertised utility tools can remain, but execution-code hosting is disabled;
the panel never dispatches tools or permission requests itself.

Errors use the normal `detail` response: 400 malformed input, 401/403 owner auth,
404 unavailable chat, 409 empty context/conflicting ID/cancellation, 413 oversized
body, 502 invalid/oversized provider answer, 503 unavailable provider, and 504
deadline. No failed side question is silently converted into a normal main turn.

## Isolated verification

`test_side_questions_isolated.py` tests context provenance and trimming, hidden
reasoning and authority exclusion, body/auth validation, coalescing, cancellation
before POST, duplicate disconnects, provider isolation arguments, chunked output,
spawn/cancel races and cleanup when a leader exits before its child. Follow-up
checks cover strict pair validation, Unicode and size boundaries, immutable
history identity, changed-history conflicts, backward-compatible first questions,
and history reaching both isolated providers without parent-state writes. The
selected server glue is inspected or extracted through AST; `agent_server.py` is never
imported or started. Run it through the guarded QA runner with
`AGENTSDOCK_TEST_SOURCE` and `PYTHONDONTWRITEBYTECODE=1`, not the full server suite.

Provider calls in these tests are synthetic. Real authenticated provider
execution and deployment are separate validation steps.
