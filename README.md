# Frozen AgentsServer Guardrail Fixture

This embedded tree is a frozen fixture retained for legacy cross-stack
guardrail assertions. It is not authoritative AgentsServer source, is not kept
release-current, and must never be deployed.

Authoritative development and releases live in the standalone
[AgentsServer repository](https://github.com/ZhengyiLuo/AgentsServer). Use its
signed installer for a new host and the desktop app's signed managed-update
channel for an existing installation. `deploy.sh` is permanently disabled
before any SSH or target mutation.

Do not edit this fixture as a substitute for a standalone server change. Keep
it only where a desktop guardrail explicitly depends on frozen input.

The standalone installer checks `tmux`, `curl`, and the platform service
command before changing state, releases, configuration, or services. A failed
preflight—including an unavailable launchd/systemd user domain—prints
platform-specific guidance and never invokes a package manager or `sudo`
itself.

Useful checks:

```bash
ssh <ssh-host> 'systemctl --user status agents-server.service --no-pager -l'
ssh <ssh-host> 'curl -s http://127.0.0.1:7850/api/health'
```

## Agent Team Network Mail

API contract v24 advertises `capabilities.agent_team_mail_v1`. Agent mail is
default-deny: only an ordinary user prompt whose first token is exactly
`/mail` receives a short-lived provider capability. The helper lazily freezes
at most 512 currently visible destinations as opaque run-local routes, accepts
at most four sends, and reads the UTF-8 body from stdin so message content does
not appear in process arguments. It creates a passive Team Network Inbox item;
it never starts or steers a chat or agent. Scheduled jobs, synthetic handoffs,
and near-matches such as `/mailbox` do not receive this authority. The provider
mail harness creates message items only; it cannot create new Team Network
requests. Existing request records and the separate Team Hub request lifecycle
remain readable for backward data compatibility.

The additive strict form `/mail server NAME MESSAGE` treats `NAME` as one
case-sensitive token and the remainder as the exact normalized message body.
AgentsServer filters the private route snapshot to active server destinations
whose raw display name exactly equals `NAME`. Zero matches fail as not found;
multiple matches, including equal names on different Team Networks, fail as
ambiguous. A successful strict command exposes only that one opaque route and
permits exactly one idempotent `kind=message` effect with the exact body. It
cannot fall back to an agent destination, a case-insensitive name, a request,
rewritten content, or a second send. Legacy `/mail` remains available for
older clients. Health advertises the strict syntax and feature flags inside
`agent_team_mail_v1`, so clients can discover it without a global API-contract
revision.

## Backend Runtime Health

The current API contract reports Claude Code, Codex, and Cursor readiness
independently from basic HTTP connectivity. `GET /api/health` includes cached `runtimes`
status, and `GET /api/runtime/catalog?refresh=true` forces safe version and
authentication probes. Responses distinguish `ready`, `missing`,
`unauthenticated`, and probe `error` states without exposing account or token
details. Current servers advertise Cursor support even before its CLI is
installed, so clients can show setup guidance without allowing a run.

The server rejects a new turn with an actionable `503 runtime_unavailable`
response when its selected CLI cannot launch safely. A provider failure after
launch is retained as `last_error` while the installed/authenticated runtime
remains available, so an ordinary model or conversation error is not confused
with a missing executable.

Use `CLAUDE_BIN`, `CODEX_BIN`, and `CURSOR_BIN` when an executable is outside
the standard server runner path. Cursor probes compatible `cursor-agent` and
`agent` executables, verifies the Cursor identity and required headless flags,
and checks sign-in status. Install or update from `cursor.com/install`, sign in
with `agent login` (or configure `CURSOR_API_KEY` for the server process), then
use the client's **Recheck CLIs** action. API-key readiness is verified with a
provider-owned model-list request; failures remain an ambiguous runtime error
rather than being misreported as invalid credentials because network failures
can use auth-like wording. Cursor's
default permission mode honors its configured rules, full access adds
`--force` while explicit Cursor deny rules remain authoritative, and plan mode
uses read-only planning. Runtime probes are cached for 60 seconds by default; set
`AGENTSDOCK_RUNTIME_DIAGNOSTIC_TTL_SECONDS` to tune that interval. Legacy
`ZENITHBOT_*` names remain compatibility aliases for existing installations.

## Claude Agent SDK Transport

Interactive desktop clients can opt individual Claude chats into a persistent,
per-chat `ClaudeSDKClient`. This enables native steering plus approval and
question cards without sharing one Claude process across chats. The server
advertises the exact `claude_sdk_interactive_v1` capability before the desktop
app opts in.

Set `AGENTSDOCK_CLAUDE_TRANSPORT` to `auto` (default), `agent-sdk`, or `print`.
`print` always uses the compatible `claude -p` path. Clients that do not send
the exact capability—including older iOS builds and scheduled jobs—also remain
on `claude -p`, regardless of the server's interactive transport setting.

Idle SDK clients are retained for at most five minutes by default, with up to
four chat processes loaded. Tune those limits with
`AGENTSDOCK_CLAUDE_SDK_IDLE_TTL_SECONDS` and
`AGENTSDOCK_CLAUDE_SDK_MAX_LOADED_CHATS`. Active chats are never evicted to
enforce the idle limit.

Claude-controls capability v3 adds authenticated MCP management without
changing global API contract v13. Clients gate on
`capabilities.claude_controls.features.mcp_management` and use the additive
v1 endpoints:

```text
GET  /api/sessions/{session_id}/claude/mcp
POST /api/sessions/{session_id}/claude/mcp
```

GET lazily connects an idle chat's Agent SDK client and returns an opaque
generation string plus at most 100 exact-name-deduplicated, sorted, allowlisted
server rows; `truncated` marks an incomplete list. POST requires that exact
generation and supports `reconnect`, `reconnect_all`, `enable`, and `disable`.
Both operations reject active/provider-starting turns, are lifecycle-serialized
with managed-update admission, and have a bounded native-control timeout.
Responses never expose MCP commands, environment, headers, configuration URLs,
raw provider errors, or tool metadata. Print transport and SDK-unavailable
hosts return an explicit unavailable snapshot; older servers continue to
return 404. Tune the default 15-second bound with
`AGENTSDOCK_CLAUDE_MCP_CONTROL_TIMEOUT_SECONDS`.

## Provider scheduled-jobs access

API contract v13 includes the durable per-chat `provider_jobs_access` setting.
Its default `full` mode exposes the existing agent helper surface,
`read_only` permits only `list`, `get`, and `runs`, and `blocked` permits no
agent Jobs calls. Capability issuance records the mode at turn start and every
agent Jobs route also checks the live session setting, so a human can tighten
access while a provider turn is running. Authenticated app/human Jobs
endpoints are unaffected. Clients detect support through
`capabilities.provider_jobs_access_control_v1` in `/api/health`.

Native steering rotates a fresh Jobs/Publish authority with the new logical
run before provider delivery. During that narrow handoff, the predecessor is
suspended and only the exact candidate may use Jobs; Publish and cross-chat
routes wait for committed ownership. A proven-safe rejection restores the
predecessor, while accepted, uncertain, stopped, and restarted runs fail
closed and remove their authority files.

## Cross-chat handoffs

### Current route-hint contract (API contract 25, capability v8)

An inline structured `@Chat` is an optional target hint. It never forwards the
raw user prompt. On successful ordinary-turn admission, an exact local
single-`@Chat` reference authored by a v2 client with `grant_intent: true`
idempotently creates or refreshes a durable directional source-to-target
grant. Subsequent turns receive only that source chat's current grants; there
is no ambient all-chat authority. The agent decides whether to `send` a
prepared instruction, `ask` for an asynchronous correlated reply, or make no
contact. Every accepted configured-route Send carries one optional terminal
reply path back to its immutable source; it creates no reply obligation, never
automatically relays the target's ordinary final answer, cannot request a
follow-up, and grants no durable reverse route. Ask explicitly requests one
asynchronous terminal answer over the same exchange-scoped return mechanism.
`/chat` is a composer alias for selecting the same structured hint.

Scheduled runs never inherit the source chat's grants. Each job stores its own
exact route selection, authorized by route ID in the job editor/helper flow,
and revalidates its target, revision, and action on every firing. Its prompt
contains the corresponding exact single `@Chat` marker for display and
binding; no `@@` authoring syntax is required. The health surface advertises
cross-chat version 8, `durable_route_grants`, configured-route
`instruction_reply_once`,
`agent_ambient_local_handoffs: false`, scheduled Jobs version 5, and global
API contract 25.

Capability v8 intentionally applies this reply-once behavior to existing
configured `instruction` grants as well as newly created ones. The return path
is a property of each accepted delivery, not a new durable target grant: it is
bound to the original source, delivery run, exchange generation, two-leg
budget, and expiry. Revoking or revising the source route still blocks future
deliveries immediately.

Beta-era local `direct_message` and `@@` references remain readable only for
safe migration/recovery. They are quarantined from ordinary authority and can
never mint a durable grant; queued v6 snapshots are narrowed against current
persisted grants and cannot recreate a revoked route. Nonterminal legacy UI
envelopes are failed/cancelled during upgrade before they can be resubmitted.
Existing configured-route Send/Ask effects, action-specific secure-peer hints,
and final-result obligations retain their separate exact authorization and
lifecycle fences.

For a current ordinary turn, the provider-authority block exposes only opaque
route IDs. The agent lists the available routes, then decides whether to Send,
Ask, or make no contact:

```bash
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json list
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json \
  send --route route_opaque --message "Verify the API contract."
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json \
  ask --route route_opaque --message "Which rollout is blocked?"
```

The helper never exposes or accepts an inferred chat ID. It accepts only the
opaque IDs in that run's authority snapshot, uses loopback AgentsServer URLs,
disables redirects and proxies, and submits one bounded agent-authored
message. Send creates a correlated two-leg exchange with one optional terminal
reply capability available only to its exact delivery run. If the target does
not deliberately use that capability, its ordinary final stays local and the
exchange closes without sending anything back. Ask creates the same bounded
exchange but explicitly requests that the terminal answer or failure status
return asynchronously to the source chat.

### Historical action-specific grants (v1-v2)

API contract v11 added durable, same-server handoffs selected by the user in
the AgentsDock composer. API contract v13 upgraded
`capabilities.cross_chat_handoffs_v1` to version 2 and added bounded
request/reply exchanges. These historical structured references authorized
one exact exchange, instruction, or automatic final-result delivery. They are
retained for readable stored records and secure action-specific compatibility;
they are not the current `@Chat` authoring model. Self, archived, deleted,
legacy-transport, and unpaired foreign-server targets fail closed.

When such an action-specific grant is replayed, the provider-authority block
may print a one-use opaque target handle:

```bash
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json \
  send --target OPAQUE_HANDLE --message "Verify the API contract."
```

`OPAQUE_HANDLE` is capability data, never a session ID. Delivery is a normal
durable target turn using the target chat's own provider, permission policy,
queue, and timeline. A server restart reconciles the ledger and lifecycle
outbox without silently duplicating a handoff. The desktop bearer remains
required to inspect or cancel handoffs.

Request/reply grants use the exact exchange ID reserved when the source turn
is admitted. The source agent starts it with `ask`; a recipient may answer or
ask a clarification with the exact `respond` command printed in that delivery
turn's provider-authority block:

```bash
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json \
  ask --target OPAQUE_HANDLE --message "Which rollout is blocked?"
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json \
  respond --exchange exchange_exact --inbound-leg leg_exact \
  --message "Do you mean the desktop or server rollout?" --request-response
```

An exchange is limited to six directed conversational legs (three rounds) and
expires after 72 hours. A successful non-empty recipient final automatically
returns one terminal answer when the inbound leg expects a reply and no
explicit response won the one-use CAS. Failures, stops, expiry, participant
deletion/archive, and queue-owner loss are durable visible outcomes; non-user
failures also create one bounded native status wake for the waiting sender.
Exchange turns reuse the existing hidden `cross_chat_handoff_delivery`
purpose so older clients do not expose synthetic prompts or queue controls.

### Durable directional route management (v8)

The default-empty per-source-chat grant list is the sole ordinary cross-chat
authority ceiling. Inline `@Chat` admission manages it automatically, while
these authenticated endpoints support inspection and explicit administration:

```text
GET    /api/sessions/{source_session_id}/agent-handoff-routes
POST   /api/sessions/{source_session_id}/agent-handoff-routes
PATCH  /api/sessions/{source_session_id}/agent-handoff-routes/{route_id}
DELETE /api/sessions/{source_session_id}/agent-handoff-routes/{route_id}?expected_revision={revision}
```

`PATCH` and `DELETE` use the route revision as a compare-and-swap precondition;
a stale or missing valid route returns HTTP 409 with
`code: route_revision_conflict`, a safe message, and the current route or
`null`. A chat can hold at most 16 routes, aliases and targets are unique,
routes cannot point to their source chat, and forks inherit neither routes nor
their private mutation journal. Only v2 `agent_cross_chat_routes_v2`
submissions carrying exact `grant_intent` provenance may persist an inline
grant. Older v1/v6 turns and recovered queue hints remain one-run legacy data
and never become durable policy.

Grant mutation and turn admission cross two durable files. AgentsServer first
stores a hidden pending route journal, then fsyncs the exact `turn_started` or
`turn_queued` admission ID, and only then exposes the grant. Startup rolls the
exact route revisions back when no matching event exists, or finalizes them
when it does; later edits and revocations are never recreated. Scheduled,
internal, digest, standalone, and cross-chat delivery turns receive no source
grant snapshot. Every helper call intersects its issued snapshot with the
exact live route revision and allowed actions, so removal or policy edits
block future acceptance immediately; an already accepted ledger item remains
visible and cancelable through authenticated desktop APIs.

The turn-scoped helper surface accepts only opaque issued route IDs:

```bash
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json list
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json \
  send --route route_opaque --message "Apply the corresponding mobile change."
"$AGENTSDOCK_CHATS_CLI" --authority-file /path/from/turn.json \
  ask --route route_opaque --message "Which mobile behavior must match?"
```

`list` is capability-scoped; there is no provider chat search or arbitrary
target parameter. `Ask` is not transcript access: it creates a normal target
turn containing only the bounded relayed message, then returns one asynchronous
terminal answer to the source chat. Configured-route Send and Ask are limited
to two legs and 24 hours. A Send reply is always terminal; Ask also has no
follow-up under the configured-route contract. Route bodies and answers are
limited to 16,000 characters and 64 KiB UTF-8. A live run can accept at most
one effect per route and four route handoffs total; durable source and target
limits are 12 accepted route effects per rolling hour.

Provider projections contain only the opaque route ID, safe alias, sanitized
bounded title, backend, allowed actions, and generic availability. They never
expose target/session IDs, folders, working directories, models, provider IDs,
transcripts, rate counts, or route mutation history. Configured delivery
prompts likewise omit route and ledger identifiers; a returning answer may
include only the target's sanitized, explicitly untrusted display label.

## Access Token

Set `AGENTSDOCK_AGENT_TOKEN` on the agent host to require a shared bearer token for
all API calls, uploads, file/video fetches, and websocket event streams.

```bash
systemctl --user edit agents-server.service
```

Add:

```ini
[Service]
Environment=AGENTSDOCK_AGENT_TOKEN=replace-with-a-long-random-token
```

Then restart:

```bash
systemctl --user daemon-reload
systemctl --user restart agents-server.service
# Set AGENTSDOCK_AGENT_TOKEN in this shell to the same value configured above.
curl -H "Authorization: Bearer ${AGENTSDOCK_AGENT_TOKEN}" \
  http://127.0.0.1:7850/api/health
```

Leave the variable unset for open local development.

## Authenticated loopback port forwarding

AgentsServer `0.1.26-beta.4` adds an authenticated raw TCP tunnel for remote
development servers such as Viser. Desktop creates a listener on its own
`127.0.0.1`, then opens one binary WebSocket per local TCP connection:

```text
WS /api/sessions/{session_id}/ports/{port}/tunnel/ws
Sec-WebSocket-Protocol: agentsdock-port-tunnel-v1, agentsdock-token.<base64url>
```

The server destination is fixed to its own `127.0.0.1`; clients cannot choose
another host. Both local and remote ports are limited to `1024–65535`, active
connections are bounded, and archiving or deleting the associated chat closes
its live tunnels. Query-string tokens are rejected so credentials do not enter
request or proxy logs. Support is advertised as `capabilities.port_forwarding_v1`
only when the server has an access token configured.

This is an administrative convenience for a holder of the AgentsServer bearer,
which already authorizes terminal and workspace operations. The chat ID owns
the tunnel lifecycle and UI grouping; it is not an operating-system network
namespace and does not prove that the listening process was spawned by that
chat. Do not distribute the bearer to clients that should not be able to reach
high-numbered loopback services.

## Managed server restart

Authenticated clients can discover restart support through the additive
`capabilities.server_restart` v1 health capability and the top-level
`server_instance_id`. Restart is available only when AgentsServer proves that
the running process belongs to the supported launchd or systemd user service
and is executing from the installer's resolved `current` release.

```text
GET  /api/admin/restart
POST /api/admin/restart
```

Both endpoints require the access token in an authorization header; URL token
parameters and browser-originated requests are rejected. POST accepts a small
JSON body containing a UUID `request_id`, the exact
`expected_server_identity`, the exact `expected_server_instance_id`, and
`confirmed: true`. It returns `202` before signaling the managed process.
Replaying the same request ID is idempotent; another pending or recently
completed request is rejected.

Restart admission is fail-closed while an active update, active turn, provisional
queue write, provider background task, lifecycle operation, or HTTP mutation
is in flight. Durable queued turns remain queued for recovery after relaunch.
There is no force mode and unmanaged processes cannot use this control.

## Managed server updates

`capabilities.server_updates` v7 defines install-when-idle as a passive,
durable reservation. Chats, messages, terminal connections, settings changes,
and manual restart remain available while the reservation is pending. The
server begins maintenance only when one shared-lock snapshot proves it is
actually idle; that same atomic transition closes new-work admission. A
pending reservation survives a manual restart and is re-armed after startup.

## Working-directory completion

AgentsDock can complete New Chat working-directory paths against the active
AgentsServer host (including remote hosts) when
`capabilities.working_directory_completion` is advertised:

```text
GET /api/working-directories/complete?path=/srv/pro&limit=24
```

The authenticated endpoint performs one bounded, shallow scan and returns
directories only. Older clients and servers continue to use the ordinary path
field without a global API compatibility failure.

## Workspace Files

The optional `workspace_files` health capability exposes a chat-scoped text
workspace rooted at that chat's exact `cwd`. It is additive to API contract v10,
so older clients continue to work without a global compatibility failure.

```text
GET /api/sessions/{session_id}/workspace
GET /api/sessions/{session_id}/workspace/entries?path=&offset=0&limit=500
GET /api/sessions/{session_id}/workspace/search?q=app&limit=100
GET /api/sessions/{session_id}/workspace/file?path=src/App.tsx
PUT /api/sessions/{session_id}/workspace/file
POST /api/sessions/{session_id}/workspace/entry
PATCH /api/sessions/{session_id}/workspace/entry
DELETE /api/sessions/{session_id}/workspace/entry?path=src/old.ts&expected_revision=...&recursive=false
```

Reads accept complete UTF-8 regular files up to 32 MiB by default, replacing
the legacy 2 MiB editor ceiling. Writes are atomic,
require the SHA-256 revision returned by the read endpoint, and reject stale
revisions, symlinks, special files, hard links, archived chats, and read-only
targets. Directory traversal is descriptor-relative and fails closed when the
host lacks secure no-follow file APIs. Configure the text limit with
`AGENTSDOCK_WORKSPACE_TEXT_MAX_BYTES`; a positive value selects a bounded
transport ceiling, while an explicit zero disables the AgentsServer ceiling.
Negative values are rejected during startup.

Workspace-files capability v2 adds an opaque `revision` to every explorer and
search entry. Rename accepts `{path, new_name, expected_revision}`, is limited
to the same parent directory, and uses the host's atomic no-replace primitive,
so it never overwrites another entry. Delete requires the current entry
revision. A non-recursive delete removes files, symlinks, or empty
directories; `recursive=true` confirms deletion of a non-empty directory.
Recursive traversal remains descriptor-relative, never follows symlinks, and
rejects mounted filesystems rather than crossing them.

Workspace-files capability v4 adds no-overwrite creation. Post
`{path, kind: "file"}` to create an empty UTF-8 file or
`{path, kind: "directory"}` to create one directory. Creation is
descriptor-relative, rejects symlinked parents and existing destinations, and
is unavailable for archived chats.

## Whole-History Search

`GET /api/search?q=<query>&limit=<chat-count>` searches user, assistant, error,
job, reasoning-summary, and file text across every chat. Quoted phrases remain
phrases; unquoted terms use prefix matching for responsive type-ahead search.

The first request incrementally builds `history_search.sqlite3` inside the
agent state directory. Each transcript stores its indexed byte offset, so later
requests ingest only newly appended JSONL records. The index is persistent and
safe across server restarts; a replaced or truncated transcript is rebuilt
automatically. Indexing runs in a worker thread and does not block agent turns.

## Per-Turn Code Diffs

For Git worktrees, the server captures the complete change made by each agent
turn independently of provider tool output. It snapshots the worktree before
and after the turn with an isolated temporary index, so pre-existing dirty or
staged changes are preserved and the real Git index is never modified.

The timeline receives a compact `code_diff` event with file and line-count
metadata. The full patch is stored outside the event log and can be fetched
with the normal token authentication:

```text
GET /api/sessions/{session_id}/diffs/{run_id}
```

The response is an uncapped textual Git patch (`text/x-diff`). Binary changes
are represented by Git's compact binary-file marker rather than embedding the
binary payload in chat history.
