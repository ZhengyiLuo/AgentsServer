# Provider integration checklist

Use this when adding a provider or upgrading its CLI/SDK. A successful text reply
is not enough: verify the server contract and the behavior exposed by that
provider's native runtime/client. This is a review template, not a support claim.

Record: **provider · transport · CLI/SDK versions · model/endpoint · server commit
· OS/client versions · reviewer/date**.

For each feature, record **native**, **server-provided equivalent**,
**unsupported**, or **unverified**, with a test/result link. Gate unsupported
features explicitly; do not silently substitute a different interaction.
Describe differences and extra provider usage for server-provided equivalents.

## 1. Runtime, authentication and settings

- [ ] Discover the correct executable and supported version; distinguish missing,
  incompatible, unauthenticated and temporarily unavailable runtimes. Re-check
  after login/update without blocking server health indefinitely.
- [ ] Verify working directory, model selection and model-specific reasoning
  settings. Unknown model capabilities stay unverified, not guessed from names.
- [ ] Keep custom endpoints, credentials, chats and server instances isolated.
  Do not rewrite shared native login/configuration. Bound probes and redact secrets.

## 2. Conversation lifecycle

- [ ] Start, resume and fork preserve the correct native conversation identity.
  Test queued input, steer/follow-up, Stop and provider error/timeout separately.
- [ ] Interrupted/restarted turns settle truthfully; late events cannot revive
  finished work. Clean up only owned processes and temporary resources.
- [ ] Check native session ownership/locking: document how a chat can return to
  the native app/terminal. Closing a client is not proof the server released it.

## 3. Reasoning, activity and tracing

- [ ] Compare against **this provider's** native output: full exposed thinking,
  summaries, tool activity and their chronological order—not just headings.
  Never invent reasoning or attempt to decode encrypted/private reasoning.
- [ ] Test delta-only, completed-only and revised completion payloads, interleaved
  tools, and partial output after Stop. Avoid duplicate or truncated final text.
- [ ] Correlate server session/run, provider message/item and tool-call IDs.
  Preserve tool inputs/results and success, denial, cancellation and failure.
  Malformed IDs must not silently associate unrelated calls.
- [ ] Distinguish durable timeline events from live snapshots; reconnect/replay
  retains content and ordering. Diagnostic logs are bounded and secret-safe;
  reported usage/cost must come from evidence, not estimates presented as facts.

## 4. Automatic titles

- [ ] Identify a real native title source, or explicitly implement a separate
  generation step. Having a rename API does not imply automatic summarization.
- [ ] Keep a usable fallback. Manual names win; check imports, forks, children,
  opt-out, stale results and rename/delete/archive races.
- [ ] Extra generation is isolated from the main conversation and its tools,
  permissions and transcript. Bound input, attempts, time and concurrency;
  keep the fallback on failure and disclose additional provider usage.
- [ ] Verify the saved title actually refreshes in supported clients.

## 5. Agent-to-agent chat and authorization

- [ ] Provide the run-bound AgentsDock tool with the narrowest supported approval.
  Verify ordinary permission mode; do not require blanket shell/MCP approval or
  full access merely to communicate. Document any runtime limitation.
- [ ] Enforce server/source/run/recipient scope, live pair grants and revocation.
  Existing authorized pairs may be reused; peer text cannot create new grants.
  Keep server-attested user delegation separate from agent-written message bodies.
- [ ] Test unauthorized targets, stale credentials, revoked pairs and forked
  chats. Repeat admission checks for resumed, background and mailbox-triggered runs.
- [ ] Verify durable acceptance, idempotent retry, ordered/paged reads and replies.
  Busy recipients must not be interrupted by mail; idle wakes must not become
  fake user turns, repeatedly wake on the same batch or resume paused goals.

## 6. History, import and cross-device synchronization

- [ ] Record support **separately** for resume by ID, local discovery, initial
  history import, later native-history reconciliation and fork. Resume working
  does not prove the other four work. Distinguish CLI, IDE and cloud histories;
  never assume they share a format. Apply this checklist to Claude, Codex,
  Cursor, OpenCode and each future adapter, including explicitly unsupported items.
- [ ] Keep the original provider ID and exact workspace when resuming; test a
  contextual follow-up against the same native conversation. A missing workspace
  or unreadable store must not silently create a fresh thread or replay history
  as new instructions. Do not reverse lossy folder slugs to guess a workspace.
- [ ] Prefer verified native titles over first-message previews. Test custom
  versus generated titles, placeholder/missing titles, duplicate labels, manual
  AgentsDock names and malicious/control-character metadata. Listing/renaming
  must not spend model usage or mutate native stores.
  Verify the picker and imported chat retain the same native name; when absent,
  use the first human message, never the latest message/tool output. Keep the
  project in `cwd`, not as a duplicated prefix in the preview label.
  Include actual server-generated prompt envelopes in preview tests (policy,
  current-prompt, memory and tool-binding wrappers); role=user alone does not
  establish that the first text is the user's request. Preserve quoted markers.
- [ ] Exclude child/subagent, confirmed archived, empty and unavailable native
  sessions as supported by each provider's metadata. Do not hide stopped main
  chats or ordinary user forks. Define what "deleted" means: deleting an
  AgentsDock entry is not deletion of its native transcript.
- [ ] Exclude identities held by this and other installed same-user instances,
  including parked IDs, before the response limit. Recheck at import/resume;
  cover stale pickers, simultaneous imports and unreadable ownership indexes.
  State clearly whether older servers participate in the coordination protocol.
- [ ] Test metadata-to-transcript ID/workspace binding, read-only access,
  symlinks, corrupt/oversized files, partial writes and scan limits. Unknown
  private formats fail closed per entry. Never decrypt internal conversation
  blobs to claim native history parity.
- [ ] Negotiate provider additions end-to-end: server capability, opt-in list
  parameter, client backend validation, bulk results and UI copy. Test old
  client/new server and new client/old server; one new backend must not break
  all existing import results. Verify desktop and mobile independently.
- [ ] Import atomically with truthful per-item failures; preserve native files
  on cancellation/rollback. Test repeated import, restart, changed candidates,
  identical legitimate messages and terminal imported-run boundaries. Disclose
  text-only snapshots, missing tools/images/reasoning and unavailable catch-up.
- [ ] Reconcile live output with native history using stable identity where
  available. Test formatting differences and legitimate repeated replies.
  Tool/image reinjections and internal wake prompts must not become user bubbles.
- [ ] Exclude confirmed archived/child sessions from normal import discovery;
  enforce applicable ownership claims and validate labels. Check large histories,
  scan bounds and pagination—one malformed entry must not hide everything.
- [ ] Verify list freshness and timeline catch-up across disconnects, app reopen
  and server restart, including desktop/iPhone/iPad where supported. Import,
  release and repair must not silently delete original provider transcripts.

## 7. Existing provider skills and commands

- [ ] Preserve discovery of the user's existing project, user and plugin skills
  under the provider's native scope/precedence rules. Use the correct working
  directory and configuration; do not copy, overwrite or reinstall their skills.
- [ ] Verify runtime availability separately from AgentsDock's skill/command
  picker. Test explicit selection and automatic invocation where natively
  supported; do not assume every provider has the same syntax or discovery API.
- [ ] Preserve skill arguments, instructions and access to supporting files.
  Skill use must retain normal tool approvals and server authorization—it must
  not grant shell access or permission to contact other agents by itself.
- [ ] Test refresh, disabled/removed skills, duplicate names and stale selections
  after queueing/resume or a directory change. Revalidate selections server-side;
  never accept arbitrary client-supplied skill paths or leak private metadata.
- [ ] Compare a harmless installed skill in the native client and AgentsDock,
  including a denied tool request. Keep skills disabled in isolated title/probe
  runs where required; missing dependencies should produce an actionable error.

## 8. Other server features: declare support individually

- [ ] Plans/goals, approval requests, user questions, compaction and scheduled jobs.
- [ ] Subagent identity, limits, lifecycle and continuation, isolated from parent state.
- [ ] Images/files, code diffs, artifacts and terminal output, including path safety.
- [ ] Side questions (`/btw`): native context, independent cancellation and no main
  history/queue mutation. An ordinary message is not an equivalent implementation.
- [ ] Shared/interactive chats and optional tools: retain normal authorization
  boundaries; never enable a feature just because another provider supports it.

## 9. Verification and release gate

Run this minimum smoke flow with disposable chats/workspaces:

**New chat → reasoning + real tool result → existing skill → title → resume/fork
→ allowed and denied agent chat → busy input + Stop → reconnect/restart → import/reconcile.**

- [ ] Add sanitized protocol fixtures and adapter/server regressions, including
  malformed events, permission denial, timeout and cleanup. Keep tests off real
  user history; importing server code must use isolated home/state directories.
- [ ] Run live checks on the minimum and current supported runtime versions,
  claimed operating systems, permission modes and relevant model/endpoint types.
  Exercise advertised optional features; record anything not tested.
- [ ] Verify native parity per provider and rendering in supported clients.
  Keep **fixture tests**, **live runtime checks** and **client acceptance** separate;
  passing one does not prove the others.
- [ ] Run affected-provider regressions and repository CI. Check release packaging,
  capability advertisement and installed-source/version identity, with rollback.
  Do not ship required contracts as “unverified.”

## Repository starting points

- [Native-parity rules](../AGENTS.md); [runtime diagnostics tests](../test_runtime_diagnostics.py).
- [Cursor CLI import contract](CURSOR_LOCAL_IMPORT.md); [Cursor import tests](../test_cursor_local_import.py), [cross-instance import tests](../test_cross_instance_import.py).
- [Titles](NATIVE_SESSION_TITLES.md); [title lifecycle tests](../test_generated_title_lifecycle.py).
- [Chat authorization](ASYNC_CHAT_ROUTES.md), [mailbox](CHAT_MAILBOX.md); [authority tests](../test_provider_authority_lifecycle.py).
- [Reasoning tests](../test_reasoning_stream_isolated.py), [WebSocket catch-up tests](../test_event_websocket_catchup.py), [history reconciliation tests](../test_provider_history_sync.py).
- [Side questions](NATIVE_SIDE_CHAT.md), [subagents](SUBAGENT_IDENTITY.md), [custom endpoints](CODEX_PROVIDER.md).
- [Skill/command projection](../provider_commands.py), [discovery and selection tests](../test_provider_command_api.py).
- [CI](../.github/workflows/server-ci.yml), [packaging](../scripts/package_release.py).

Research baseline: `release/1.0` at `929bd47`, 2026-09-20. Also checked the
[official Codex app-server protocol](https://developers.openai.com/codex/app-server/):
completion payloads, reasoning events and scoped approvals require separate
handling. Re-check the corresponding official protocol for every provider/version;
these Codex details are not evidence of support elsewhere.
