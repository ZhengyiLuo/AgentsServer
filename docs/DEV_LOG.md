# Development and release log

## 2026-09-21 — Preserve user authorization in chat mail — source acceptance

- Preserve the originating user's exact instruction through permanent-route
  messages, durable storage, and authenticated provider inbox reads. Recipients
  can execute delegated work within that scope without requiring a second user
  approval. Agent-authored message text cannot grant or expand authorization.
- Keep generated wake prompts separate from user instructions, preserve public
  inbox projections, and include provenance in bounded pagination and retries.
  Reject unreadable messages before acceptance. Oversized legacy handoffs retain
  their original queue recovery path instead of blocking startup.
- Pass focused mailbox, route, helper, lifecycle, migration, and legacy handoff
  checks, plus Python compilation. In an isolated production server with actual
  native Codex and a real Responses endpoint, a user-authorized source message
  wakes an idle recipient, which creates and verifies the requested artifact
  without any user turn in the recipient chat.
- Forged-source rejection passes deterministic boundary checks. The live
  informational-mail follow-up is inconclusive because the custom endpoint
  stream ends before provider completion; it is not counted as a behavior pass.
- Availability: source correction prepared for the coordinated desktop/server
  release. This acceptance does not restart or update production servers.

## 2026-09-20 — Custom OpenAI model effort choices — source acceptance

- Accept native structured effort metadata from endpoint discovery. For an
  explicitly OpenAI-owned Responses model with no reasoning metadata, derive
  choices only from its exact matching installed Codex model. Explicit endpoint
  restrictions take precedence; discovery does not certify gateway compatibility.
- Persist discovered model IDs and effort choices privately for their exact
  credential revision. Keep compatibility and summary evidence independent;
  a different endpoint revision cannot inherit old choices. After updating an
  existing server, use the model picker's Refresh models action once.
- Pass 65 focused provider/session/readiness checks and Python compilation.
  In native offscreen Electron connected to an isolated production server,
  click Refresh models, select Low, reopen the picker and send a real Codex
  turn. Confirm the native Responses request carries the selected model and
  `reasoning.effort: "low"`; the endpoint returns a completed response. Model
  choices include the levels reported by the installed native catalog.
- The UI bootstrap uses an isolated seeded profile. Production preload, IPC,
  HTTP authorization, native Codex and the real endpoint are exercised.
  Availability: committed source correction, not deployed or published.

## 2026-09-20 — Claude side chat after settings changes — source acceptance

- Keep side questions on the connected parent Claude conversation when saved
  model or effort settings have changed for a later main turn. Avoid a false
  configuration conflict without replacing or interrupting the active parent.
  Preserve cold-resume identity checks and strict configuration checks for MCP.
- Pass 73 focused side-question checks. Verify the production HTTP API and
  native Claude with a running parent: change saved effort, ask about a fact
  available only in a completed tool result, ask a follow-up, and cancel a side
  request. The main run remains active and its transcript stays unchanged.
- Exercise the desktop popup through production preload, IPC and native HTTP
  into the same isolated server and provider. The parent subsequently completes
  normally; the test server is cleaned up.
- Availability: accepted source correction. No published release or running
  production server is changed by this acceptance.

## 2026-09-19 — Completed Codex plaintext preservation — source only

- Read native completed reasoning `content` arrays of strings, while retaining
  compatibility with previously supported plaintext forms. Preserve all
  sections separately from summaries. Authoritative completion replaces
  earlier deltas; summaries and encrypted fields are not treated as plaintext.
- Correct completion fixtures to use the native notification schema. Pass
  101 focused runner, stream and parser tests, including completion without
  plaintext deltas and revised final text.
- Verify controlled Responses through actual sandboxed Codex, the production
  server, authenticated WebSocket, desktop service, preload and timeline in
  isolated offscreen Electron. Both native completed items contain string
  arrays; no plaintext deltas occur, and both full texts persist and appear
  after using the actual Settings toggle. Native tool execution completes;
  the renderer reports no errors. This checks transport and display, not
  external-model summary length or native GUI parity.
- Availability: committed source correction only. No release version change
  or running-server deployment is included in this check.

## 2026-09-19 — Supplied reasoning and per-chat limits — 1.0.4-beta.9

- Retain Codex's explicitly supplied plaintext in a distinct reasoning event,
  separate from summaries sharing the same native item ID. Negotiate its
  transient stream independently for older clients, preserve chronological
  anchors, and retain partial text on interruption. Never inspect or decode
  encrypted reasoning.
- Add strictly validated optional per-chat native concurrency limits.
  Preserve existing private and server configuration when clearing the new
  override. Scope Codex settings to its thread and Claude settings to its
  process; never modify shared provider credentials or configuration.
- Apply Claude changes only when its provider can restart while idle, keeping
  tracked background agents alive. Report Codex's native loaded-thread reset
  limitation explicitly and track pending application without restarting a
  shared process or inventing a default limit.
- Verify a real authenticated Claude turn: one child runs at a configured
  limit of one, additional Agent attempts receive native limit rejection,
  and the turn completes. Verify Codex native admission with deterministic
  Responses: limits of one and two admit exactly those child counts and
  preserve sibling configuration. A separate authenticated Codex turn
  verifies the live provider boundary. Normal auth/config files remain intact.
- Exercise per-chat saving, clearing, busy-state preservation, fork/config
  inheritance, stale process markers and save-during-start races. Test
  summary/plaintext streaming, native completion, WebSocket negotiation,
  semantic paging and interruption through isolated production harnesses.
- Keep session-list metadata compact without increasing its payload budget.
  Omit never-configured limits while preserving explicit null tombstones
  after clearing an override. The 182-row regression stays below 150,000 bytes;
  186 related session, configuration, fork and paging checks pass.
- Accept source `b0044cb59d581b7d65120c203a5731f2057a3ac2` from
  [release run 35493194839](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35493194839):
  all eight shards pass, with 4,457 tests passed and two skipped. Verify the
  Ed25519 manifest signature, safe archive membership, and all 85 packaged
  source files and modes against that exact commit.
- Publish [beta.9](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.9)
  using the exact three held assets. Independently verify unauthenticated
  public downloads, API metadata, asset listing, Git tag and rendered release
  notes; all hashes, the signature and the manifest match the accepted source.
  Archive SHA-256:
  `fb7a4703274eed77d5205f42088fc7631f7df1c09ad1132ec7e0b5177d651263`.
- Accept identity- and instance-bound managed idle updates on both target
  servers. Replace only the exact owned pending beta.8 reservation where one
  remains. At receipt verification both beta.9 updates are pending behind
  active work; running versions remain beta.8 and beta.6 respectively.
  No forced restart is performed, and accepted reservations do not imply
  completed installation.

## 2026-09-19 — Live thinking summaries — 1.0.4-beta.8

- Add live Codex summary snapshots with section ordering, revision fencing,
  reconnect recovery and stable chronological anchors. Keep partial updates
  outside the durable event ledger; save the authoritative completed item once.
- Keep interrupted summaries as partial items. Join completion persistence and
  stream cleanup on cancellation; isolate transient broadcasts to opted-in
  subscribers without waking shared-chat projections.
- Enable custom-model summaries only with explicit capability evidence, using
  a separate optional summary check after basic tool compatibility succeeds.
  Retain support observations per model and private saved credential revision
  across restart. A later successful check supersedes older catalog metadata;
  fresh explicit rejection disables support. Unknown observations preserve
  prior proof. Optional metadata read/write failures never block chat or a
  successful basic check, and unsaved credentials create no durable proof.
- Verify summary request gating against controlled native Codex 0.155.0 and
  0.153.4: canonical and unfamiliar model IDs request summaries only when
  explicitly enabled, without inheriting effort or internal context fields.
  Native tool/continuation probes emit a visible summary in the positive case;
  summary rejection preserves basic compatibility success. No paid endpoint
  request or production authentication/configuration change is used here.
- Pass 120 isolated provider, per-chat routing and side-question checks,
  including optional check failures, revision-scoped restart persistence,
  newer capability evidence, safe metadata failures and retained credentials.
- Pass 170 production runner, native-control and WebSocket checks, plus six
  isolated streaming checks. Exercise actual native Codex with a controlled
  Responses endpoint through the production runner, WebSocket and Electron
  service, preload, store and timeline. Confirm live arrival before completion,
  reconnect recovery, authoritative replacement, section and tool ordering,
  interruption retention and historical reopening through the native cache.
- Accept source `51f4a4c6b4c4d718de8a03a80f354c6a69939b05` from
  [release run 35489981739](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35489981739):
  all eight shards pass, with 4,440 tests passed and two platform/opt-in skips.
  Verify the Ed25519 manifest signature and all 85 packaged source files and
  modes against the accepted commit. The final fixture and synchronization
  repairs leave every packaged source byte unchanged from native acceptance.
- Publish [beta.8](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.8)
  using the exact three held assets. Reverify anonymous public downloads,
  the public asset listing, the exact Git tag and rendered release notes.
  Anonymous API verification is rate-limited; authenticated release metadata
  supplements the independent public checks. Archive SHA-256:
  `fa68bcccd2630d8447a5a3b9a8fc5dccdfe2d5512975fb46cd8ca387db030f83`.
- Replace only this release pass's exact pending beta.7 reservations with
  identity- and instance-bound beta.8 updates on both target servers, retaining
  `when_idle`. Both requests are accepted; verified health still reports
  beta.6 running while active work drains. No forced restart is performed.

## 2026-09-19 — Custom endpoint model compatibility — 1.0.4-beta.7

- Add explicit saved-model checks using the endpoint's retained credential
  revision. Exercise isolated native tool calls, a dynamic tool-result token
  and continuation in the same thread; distinguish unsupported models from
  inconclusive, authentication and transport failures.
- Keep discovery separate from compatibility proof. Filter affirmative
  non-chat models, preserve unfamiliar model IDs and cache capability evidence
  per endpoint, key and model without exposing credentials.
- Remove universal custom-model effort defaults. Use advertised per-model
  efforts, clear stale persisted settings when changing models, and replace
  native turn settings to avoid inheriting ordinary account effort. Disable
  custom reasoning summaries while preserving ordinary Codex behavior.
- Pass 72 focused provider and side-chat checks. Capture eight native Codex
  requests against a controlled loopback Responses endpoint using the
  production override helper: known and unknown models clear inherited and
  earlier explicit effort, retain thread instructions, and leave a separate
  control thread's effort unchanged. The native request still contains an
  empty reasoning object; this does not prove compatibility with every gateway.
- Complete one isolated check against the user's saved endpoint and selected
  model through native Codex: the native plan tool, dynamic tool-result token,
  and same-thread continuation all pass. Before/after hashes confirm unchanged
  normal account authentication, native configuration, and saved provider
  settings and credentials. Existing sessions and provider processes remain
  untouched.
- Verify the same three-request flow against a controlled Responses endpoint.
  An explicit unsupported-parameter response fails after one request, reports
  the fixed compatibility error, and never reflects the synthetic credential.
  These checks establish basic isolated compatibility, not every production
  workspace tool, integration, or reasoning setting.
- Accept source `c5dd8740d74b7f87c6874ad53d821287edeedd6c` from
  [release run 35486963546](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35486963546):
  all eight shards pass, with 4,421 tests passed and two platform/opt-in skips.
  Verify the Ed25519 manifest signature and all 85 packaged source files and
  modes against the accepted commit. Publish the exact three held assets and
  reverify unauthenticated public downloads, tag and release-note parity.
  Archive SHA-256:
  `f71ffaab88287477c1a27f2fb56e775a518aa8b626f3c3cec2231a99d0da5da9`.
- [Public beta.7](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.7)
  is available on the Beta track. Managed idle update requests are accepted
  by the two target servers; receipt verification still reports beta.6 running
  while active work drains. No forced restart is performed.
- Exercise the production chat runner in isolated state: custom unknown-model
  turns clear effort and summaries and surface an empty response once without
  rollover, while ordinary Codex retains its existing recovery behavior.
  Native capture confirms the turn override removes inherited detailed
  summaries as well as effort; normal sibling settings stay unchanged.
- Extend native acceptance to canonical built-in model IDs: their metadata can
  restore reasoning defaults after a turn clears them. Prepare an isolated
  private catalog on each custom process launch, preserving native tools and
  instructions while removing those defaults and selecting standard Responses.
  Eight controlled requests verify canonical and unfamiliar models send an
  empty reasoning object, retain instructions, and honor explicit effort.
- Verify the same catalog, initialization and eight-request behavior on the
  second deployment's Codex 0.153.4, alongside the primary 0.155.0 acceptance.
  Normal authentication and configuration hashes remain unchanged, and all
  isolated native processes and temporary state are closed after acceptance.

## 2026-09-19 — Scheduled history catch-up — 1.0.4-beta.6

- Capture the initial WebSocket replay boundary under the same delivery lock
  used by durable imports. Wait for source-proven history projection before
  reading newly committed rows, so scheduled prompts cannot escape as user
  messages while a subscriber is catching up.
- Keep socket writes outside the lock. Preserve complete catch-up, exact
  sequence delivery and the transition to live events.
- Reproduce the race with a real durable import paused after fsync and before
  proof publication. The regression sends an unrepaired prompt before the fix
  and a corrected record afterward. All 11 WebSocket catch-up tests pass,
  including slow-client liveness and concurrent append/prune cases.
- Exercise the overlap in native offscreen Electron through the production
  client, authenticated WebSocket, server replay handler, durable import,
  SQLite cache and Timeline. Before the fix, duplicate scheduled prompts and
  answers arrive unrepaired and survive reopening. Afterward, every duplicate
  arrives corrected; the job card, latest output, Previous runs and genuine
  user messages remain correct after reopening. Provider proof timing and the
  initial HTTP page use controlled fixtures; no scheduled command is executed.
- Release discovery copies no longer retain every completed test case and
  its fixture graph for the entire shard. Restore unittest's normal cleanup
  without changing test selection, assertions or deadlines.
- Accept [1.0.4-beta.6](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.6)
  from `21f7fca42adb2f0e8736af7cfc38b3a7e7279811` after all eight test workers
  and signed packaging pass in
  [release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35482901609).
  Verify the Ed25519 signature, exact archive membership and all 85 packaged
  source files. Public unauthenticated downloads match the three held assets;
  the prerelease tag and authored release notes match the accepted source.
  Archive SHA-256:
  `d2ea302d1b36eacc3001a873cd54ca17d2701f89aa75f1c0bd6466c27932b96d`.
- Confirm both identity-bound managed updates are pending for idle installation.
  Active work keeps beta.4 running; no forced restart is performed. Publication
  and accepted update reservations do not imply installation has completed.

## 2026-09-19 — AgentsServer 1.0.4-beta.5 accepted

- Compare resolved workspace paths when verifying a native Codex fork and
  identifying a late-created child for cleanup. Preserve ancestry, exact
  completed-turn and cleanup ownership checks.
- Reproduce the false rejection with native Codex and a symlinked workspace.
  Click Fork chat in native offscreen Electron through the production store,
  preload, service, HTTP and standalone server. The previous server returns
  409; the corrected server creates two separate forks while the parent runs.
- Continue a child through the native provider. Its request contains the
  completed context and excludes the parent's active prompt; the parent stays
  active. The model endpoint uses controlled loopback responses and disposable
  credentials/state; external model billing is outside this check.
- Pass 129 focused native transport and fork tests, including rejection and
  cleanup of a genuinely different workspace. Record safe failure categories
  before returning a live-fork error; do not expose raw provider exceptions.
- Accept [1.0.4-beta.5](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.5)
  from `e470f98727526966aeadf4de08e35a5038a5d06c` after all eight test workers
  and signed packaging pass in
  [release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35477550385).
  Verify the Ed25519 signature, manifest, exact archive membership and all 85
  packaged source files. Public unauthenticated downloads match held assets;
  authored release notes and the exact source target are verified.
  Archive SHA-256:
  `1c504c820808b53723f8b2997e366cf7e3d262f40eb266d87b58643bd0ba0baa`.
- Submit an identity-bound managed update from beta.4 to beta.5 for idle
  installation. It remains pending while active work continues; publication
  does not imply the running service has already changed.

## 2026-09-18 — AgentsServer 1.0.4-beta.4 accepted

- Save endpoint/key settings without testing or interrupting active native
  sessions. Isolate normal Codex and immutable custom credential generations;
  retain each existing chat's original routing across settings edits.
- Discover models from the selected provider, support per-chat model/effort
  changes, and migrate existing provider bindings without key reentry.
- Route controls, forks, subagents and update-admission checks to their owning
  native process. Scope cached state to the correct manager generation.
- Replace broad cross-chat prohibitions in the Codex, Claude and Cursor
  preludes with concrete messaging-helper guidance. Runtime grants and
  authorization remain enforced by the harness; no automatic access is added.
- Exercise the full HTTP/session/settings/native-manager flow with normal and
  two custom loopback providers. Hold two turns active, save a new endpoint,
  start its chat immediately, and change model/effort on an older chat. Save
  and reset during a pending native test succeed; no account login is called.
- Repeat with simultaneous cold native startup. These checks use disposable
  state and synthetic availability/credentials; live external gateway access
  and production background startup are outside their acceptance boundary.
- Accept [1.0.4-beta.4](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.4)
  from `b4b116d022ba9d73949e56476cbfc46fdba27160` after all eight workers pass
  in [release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35396277887).
  Verify the Ed25519 signature, manifest and all 85 packaged source files;
  unauthenticated public downloads byte-match the verified held assets.
  Archive SHA-256:
  `664133cd5ad39251b6276cb823d527c0ba9d59c48f96c187c1cf866042cb2888`.
- Publish matching desktop **1.0.4-beta.3 / 1175**. Accept the managed server
  update from **1.0.4-beta.3** to **1.0.4-beta.4** with idle installation;
  the update remains pending while active work continues, with no forced
  restart or interruption.

## 2026-09-18 — AgentsServer 1.0.4-beta.3 accepted

- Reproduce a live connection-test failure before any endpoint request. The
  owned native process rebuilds existing rollout history against a fresh
  temporary database, then exceeds its 15-second startup/request deadline.
  The earlier QA sandbox denied history reads and therefore hid this delay.
- Give the test a temporary Codex home as well as temporary state. Keep HOME
  unchanged and use only the explicitly entered endpoint, model and separate
  key. Normal account configuration, authentication and history are untouched.
- Validate the corrected production probe without the QA sandbox, using the
  installed runtime environment and native Codex. Startup reaches the owned
  local endpoint in under a second, completes a successful Responses stream
  with status `ready`, and maps a controlled 401 correctly;
  existing auth/config hashes remain unchanged. Eighteen focused provider
  checks pass, including the temporary-home boundary. No real gateway key or
  external provider request is used in this acceptance check.
- Publish [1.0.4-beta.3](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.3)
  from `bd49fb1ded65cf168668d68574d00cef9334eb6e` after all eight release
  workers pass in [preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35390360764).
  Verify the Ed25519 signature, checksum and exact contents of all 85 runtime
  files. Public downloads match the held, verified assets. Archive SHA-256:
  `4ddb1528f3c958b17e98202ddfab181cccba3bc441156af817685447435c0a93`.
- Compatible with desktop 1.0.4-beta.2; the app and API contract do not change.
  Submit an identity-bound when-idle update. The running server remains on
  beta.2 while this work is active; beta.3 installation is queued.

## 2026-09-18 — AgentsServer 1.0.4-beta.2 accepted

- Publish [1.0.4-beta.2](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.2)
  from `dfc05e997b7c97b366c87400f35e4c640f8f2f85`. Disable legacy shared
  Codex API-key login before manager access and advertise read-only account
  controls. Custom endpoint credentials remain separate.
- Focused local auth/provider checks pass. Native offscreen Electron Settings
  exercises the full imported server middleware, auth/provider routes, private
  store, manager recreation and real Codex against controlled Responses
  endpoints. Verify failure/retry, exact URL/model save/reopen, busy rejection,
  removal, account-read failure recovery and legacy login rejection with zero
  native login calls. A separate real manager completes simultaneous normal
  and custom turns plus follow-ups with distinct credentials and models.
- Test accounts, endpoints and state are disposable. Production background
  startup tasks and live external gateway billing are outside this check.
  No existing credentials are reconstructed by this release.
- All eight release workers pass in
  [the accepted preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35375031749).
  Verify the Ed25519 signature, archive checksum and all 85 allowlisted files
  against the committed source. Published downloads match the held assets.
  Archive SHA-256:
  `bfdb60938aecedfa16aa72e04bb50450461dce20b71c8b5602e4e0abc5cc301b`.
- An identity-bound when-idle update reservation was accepted. Installation
  remains pending while active work runs; publication is not deployment.

## 2026-09-17 — AgentsServer 1.0.4-beta.1 accepted; faster release gates

- Publish [1.0.4-beta.1](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.4-beta.1)
  from `393976cd9a1b9bf024f42f6a854e5517a1727ea4`. Includes native Codex
  API-key authentication, per-chat custom Responses endpoints and native-context
  Side chat. Matching desktop beta build 1175 is required for the new controls.
- Run the focused local gate against an exported committed tree first: 229
  checks pass in 9.659 seconds under the production-state/import-blocking runner.
  Additional extracted fixture checks correct stale provider/import expectations,
  preserve the 150 KB wire-response limit, and isolate a leaked test steering flag.
  No production server import, test turn or forced restart is used locally.
- Replace serial release/PR testing with eight disjoint test-case shards; keep
  every regression, including slow installer/migration/rollback cases. Sharding
  whole files left installer tests on the critical path, so distribute cases
  while retaining standard unittest class/module fixture handling.
- [Accepted release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35312125393)
  passes all 4,393 cases across eight workers, then packages and signs. Test-job
  wall time is 3m43s; packaging/signing completes 12 seconds afterward. Verify
  Ed25519 with Studio's installed trusted key and exact equality of all 85
  packaged files with the release commit. Fresh unauthenticated public downloads
  match all three held assets byte-for-byte. Cancel the redundant tag rebuild.
- Archive SHA-256:
  `dba40f59c594cd71296a3cf9fa2e2904c71f5d0131746bb96ece0376793d6017`.
  API contract remains 28; dependencies, signing key and Team Hub schema remain
  unchanged. Unrelated dirty history/subagent work is excluded from the release.
- Submit and verify the authorized, identity-bound Studio update reservation
  with `when_idle: true`. At handoff, Studio remains on 1.0.3 with one active run;
  installation is pending, not yet claimed complete. No Supersonic change or
  desktop publication was performed in this release pass.

## 2026-09-17 — AgentsServer 1.0.3 stable accepted

- Publish [AgentsServer 1.0.3](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.3)
  from `9021327a91da63e08d194bb8176e01ea67d35290`, including native workspace
  Git controls and the Codex Side chat startup correction.
- [Release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35278319954)
  validates the dependency lock, compilation and 4,306 tests (two skipped),
  packages the source and signs its manifest. Verify the Ed25519 signature and
  exact equality of all 82 packaged files with the release commit. Fresh public
  downloads match the accepted archive, manifest and signature byte-for-byte.
- Real native offscreen Side chat checks cover first answer, contextual
  follow-up and cancellation. An additional disposable real-provider overlap
  check cancels the side turn while a separate synthetic main turn remains
  active and completes normally. No production research task is involved.
- Archive SHA-256:
  `304a1a26a54aeb2336da9557212df8614b41d3ec005654760eb69fc112b043a4`.
  API contract remains 28. Publishing does not deploy or restart live servers.
- Clarify provider-runtime sharing in the Side questions documentation. Keep
  unrelated in-progress history/subagent changes out of the release source.

## 2026-09-17 — Codex Side chat startup correction (unreleased)

- Retain Codex's configured runtime state instead of creating a fresh SQLite
  database against its existing history root on every side question. Preserve
  provider-owned authentication, temporary threads, disabled workspace/tools,
  per-request process ownership and cancellation; do not copy credentials.
- Exercise a real Codex first answer, contextual follow-up and cancellation
  through native offscreen Electron, production preload/client and the native
  server authorization/router. Observe temporary threads with no saved path,
  no transcript for those threads, and cleanup of all owned child processes.
  The main-chat context and app shell are fixtures, not a concurrently running
  production research task or full installed-profile acceptance.
- Validate focused isolation/cleanup regressions. Also make the Git worktree
  assertion compare canonical paths on macOS's symlinked temporary directory.
  These changes are prepared for 1.0.3; publication and deployment are separate.

## 2026-09-17 — Native workspace Git controls (unreleased)

- Add on-demand worktree status, staged/unstaged file diffs, conflict versions,
  stage/unstage, reviewed commits, text resolutions and existing operation
  continuation/confirmed abort. Restrict every endpoint to native operator
  authorization, not shared-chat guests or arbitrary workspace file access.
- Resolve the canonical worktree and serialize mutations using Git's index
  lock and revision checks. Preserve coherent conflict indexes when a rebase
  continues into another conflict. Refuse executable hooks/affected custom
  filters with actionable guidance rather than silently bypassing them.
- Exercise real disposable repositories for stale state, unborn and linked
  worktrees, renames, merge/rebase continuation, abort, unsafe paths and storage
  failures; validate package file inclusion. Native offscreen desktop checks
  cross the actual Git router/auth boundary, with fixture session lookup.
- No server restart, deployment or publication performed. Matching desktop
  support is required; these endpoints do not add any background polling.

## 2026-09-17 — AgentsServer 1.0.2 stable accepted

- Publish [AgentsServer 1.0.2](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.2)
  from `c1f3cc45d0317cf8f7789a62a787ada2f3728515`. Includes the endpoint
  recovery API and the preceding collaboration and Side chat beta changes.
- [Release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35193337072)
  passes dependency-lock validation, compilation and all 4,295 server tests
  (two skipped), then packages and signs the release manifest.
- Verify the Ed25519 signature, Stable/API 28 metadata and byte-for-byte
  equality of all 81 packaged files with the release commit. Fresh public
  downloads of the archive, manifest and signature match the verified assets.
  Archive SHA-256:
  `75c7c6f9d0285ac341a29e2e6e445c3c9be234518426c653fe437f0e6eafdb43`.
- Cancel the redundant tag-triggered rebuild after publishing those verified
  assets. Unrelated uncommitted work remains excluded. Installation is a
  separate managed operation; publication itself does not restart servers.

## 2026-09-17 — 1.0.2 stable release prepared

- Set the standalone server release version to `1.0.2` for the explicitly
  requested stable app/server release. The committed runtime includes the
  1.0.1 beta mail gateway, durable joins, indexed search, exact-recipient reads,
  isolated Side chat and shutdown-budget fixes, followed by endpoint recovery
  in source `4753c8dd0b70e6896c04b81608c813b84010ca25`.
- Add [stable release notes](RELEASE_1.0.2.md) against the published 1.0.0
  baseline. API contract remains 28; Team Hub schema is 23, introduced by the
  indexed-search migration in beta.2. Existing trust, approvals and routes are
  preserved by explicit endpoint recovery; each member requires its own update
  and endpoint migration after a host address change.
- Prepare this release only from committed source. Unrelated uncommitted
  Claude history, subagent and deployment/packaging changes remain excluded.
  Local validation uses a temporary source snapshot, rejects `agent_server`
  imports and process launches, and allows only private test transports.
- All 39 endpoint recovery checks pass on that committed snapshot. Four of
  five release-manifest checks also pass; the remaining check identifies the
  frozen `secure_peer.py` source digest that must be refreshed after the
  recovery change before the full release gate can pass.
- Publication and deployment are pending. Run the release workflow in
  `prepare_only` mode, then verify the held signed assets against the committed
  source before publication. Record the accepted workflow, archive digest and
  public-download verification in a subsequent entry after they succeed.

## 2026-09-16 — Secure Team Network endpoint recovery (unreleased)

- Identify a vanished host bind address explicitly instead of repeatedly
  reporting a generic initialization/database failure. Permit a confirmed
  reconfiguration to a current address without restarting the main server,
  resetting the host CA or revoking approved members. Failed changes preserve
  the prior configuration and restore a previously live listener when possible.
- Add an authenticated member endpoint-migration control. Probe the explicitly
  selected endpoint with the existing pinned CA and client certificate; verify
  host, Hub, team and peer identity before changing the saved address. Preserve
  routes and active selection, reject concurrent trust/renewal/endpoint changes,
  and never reactivate a disconnected member or fall back to unverified TLS.
- Preserve notification callbacks across host rebind/rollback. An inactive
  connection migration does not invalidate the active connection's hint stream.
  Add no renderer polling, network discovery or automatic address switching.
- Focused tests cover actual missing-address socket failures, real TLS endpoint
  verification, wrong certificates/identities, persistence failure, rollback,
  revocation and renewal races, inactive connections, schema compatibility and
  the authenticated asynchronous API boundary. Tests use isolated state and
  private test transports, never the monolithic server or production chats.
- See [endpoint recovery](SECURE_PEER_ENDPOINT_RECOVERY.md) for operator steps
  and compatibility. This implementation is not yet published or deployed;
  migrating a host alone cannot update endpoints saved by member servers.

## 2026-09-16 — 1.0.1-beta.2 publication verified

- Published [AgentsServer 1.0.1-beta.2](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.1-beta.2)
  from source `da063729e20a6163f96a26ec119a6c77b77c6a3c`. Includes isolated
  Side chat follow-ups, indexed Mail/Bulletin search and exact-recipient
  Team Network reads; unrelated unfinished worktree changes are excluded.
- Release validation caught obsolete installer import assertions and the
  missing Side chat cleanup allowance. Align all 18 bounded shutdown phases
  with the cooperative watchdog and the installer's 185-second maximum
  launchd wait. Add a guarded check of the complete budget relationship;
  preserve existing forced-restart and systemd service bounds.
- [Release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/35164753174)
  passed the full suite (4,256 tests, two skipped), packaging and Ed25519
  manifest signing. Verify the held assets before uploading and publishing;
  cancel the redundant tag-triggered run after publication.
- Fresh unauthenticated public downloads pass signature verification, all
  three GitHub asset digests, Beta/API 28 metadata and byte-for-byte comparison
  of all 81 packaged source files with the release commit. Archive SHA-256:
  `2ac24796d4a2cd6dd88c0c1b1060021a247dc90f7ff1cabc7791d138db87af46`.
- The matching Side chat interface is in desktop 1.0.1 local build 1170.
  Publication does not install or restart a running server or desktop app.
  The stable server release remains 1.0.0.

## 2026-09-15 — Direct Team Network reads from mentions (unreleased)

- Teach both provider runtimes and their shared tool description that
  `@@bulletin` and named `@@` mentions refer to Team Network, not external
  connectors. Reading mail or the Bulletin does not require manual routing
  and does not authorize an automatic send or post.
- Expose `team bulletin` as a read-only alias for the existing feed. Follow
  sender-filtered inbox pages on demand, retaining explicit continuation and
  incomplete status instead of claiming no mail after filtering one page.
- Resolve selected mentions on demand to exact team/sender identities, so
  duplicate names and renamed members cannot silently select different mail.
- Keep runtime guidance out of user messages; add no polling, refresh timer,
  new route grant or message mutation. See [agent reads](TEAM_AGENT_READS.md).
  Guarded helper-to-endpoint-to-Hub journeys cover duplicate names, renames,
  exact Bulletin selection, history pages and unchanged unread mail. Isolated
  checks also cover multi-team scope, revoked authority and bounded output.
  No installed app or running server is changed by this implementation.

## 2026-09-15 — 1.0.1-beta.1 replacement accepted

- Republished the explicitly authorized same-version beta from source
  `d209ab4c77ac5a970232359fb4317aafd79dfc1b`. The canonical
  [beta.1 release](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.1-beta.1)
  and tag now identify the revised durable-join implementation while retaining
  the earlier Team Mail gateway correction. A verified backup of the original
  package is retained; unrelated unfinished changes are excluded.
- [Release preparation](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/34946030454)
  passed 4,128 tests with two skipped, packaging and manifest signing. Its
  prepare-only mode held the signed assets for verification before the existing
  release was replaced. The redundant tag-push run was cancelled afterward.
- Fresh unauthenticated public downloads pass Ed25519 signature verification,
  all three asset digest checks, Beta/API 28 metadata and byte-for-byte
  comparison of all 78 packaged source files with the accepted commit.
  Archive SHA-256:
  `24e3a1d388fa4730a8020230491562cc63d976bea3329b0fdd8a45c5af759f62`.
- Durable joins require the revised server on both host and joining server.
  Already-installed beta.1 instances require manual reinstall from the
  verified archive; Check for updates and Force update reject equal versions.
  Matching desktop observation/display changes remain separately committed.
- The stable release remains 1.0.0. Publication performed no live server
  restart, installation, request approval or mail send.

## 2026-09-15 — Durable join approval waiting implementation

- New explicitly submitted joins negotiate a signed durable-approval
  capability when both servers support it. Pending approval and automatic-join
  consent then wait for a decision rather than expiring after ten minutes.
  Preserve the exact request and consent through restart and lost-response
  recovery; no extra authority or automatic approval is created.
- Remove the shared-IP cap of 16 pending requests, which can reject legitimate
  teammates behind one network. Keep overall storage/response/flood bounds,
  identity and signature checks, host approval, cancellation, revocation,
  connection replacement fences and certificate validity.
- A 600-second HTTP observation window no longer falsely expires a pending
  Join. Return an explicit observation-window result while retaining consent;
  desktop observers can renew the same long-held read without inbox polling.
  Project a durable approval deadline as null, not the Unix epoch.
- Existing positive legacy deadlines remain unchanged; old expired, rejected
  or cancelled requests are never revived. A new request and updated host and
  joining servers are required for durable waiting. Older binaries do not
  understand the new stored zero deadline; see the contract's downgrade note.
- Local isolated checks exercise eight-day approval waiting, restart,
  lost-response replay beyond the old attempt-retention window, shared-NAT
  admission, cancel/approval races, observer cleanup and old/new compatibility.
  The combined guarded acceptance run passes 121 checks, including the fresh
  member mail flow and release source manifest. Production state, provider
  processes and live network endpoints are excluded from this local harness.
  These local checks performed no live approval, server restart, installation
  or publication.

## 2026-09-15 — 1.0.1-beta.1 publication verified (UTC)

- Published source: `ce5a245546cbc8d4b55c16a05afc31a25104e6fa`.
- [Release workflow](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/34938118639)
  passed its full gate: 4,098 tests, two skipped. Packaging and Ed25519
  manifest signing succeeded. Publication completed at 06:59:23 UTC.
- Fresh public downloads passed signature verification, all three GitHub
  asset digests, Beta/API 28 metadata, and byte-for-byte comparison of all 78
  packaged source files with the release commit. Archive SHA-256:
  `8d4b6ff0d08b96df8cda5b418f988a243068eee672a6f866aca6ae3abde0af2d`.
- [Public beta](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.1-beta.1)
  is available; the latest stable remains 1.0.0. The exact committed source
  also passed 43 guarded local mail/lifecycle/package checks, independently
  of unrelated uncommitted work in the development tree.
- Publication did not install or restart Supersonic, Studio or any member
  server; it did not send/retry mail or approve pending joining servers.

## 2026-09-14 — 1.0.1-beta.1 member mail gateway correction

- Preserve the recipient's `mail_route_lifecycle_id` in the secure-peer mail
  adapter. Durable `@@` grants already attach this inbox-identity precondition,
  and the Hub API/store already support it; the adapter's older field allowlist
  rejected valid member sends before the message could reach the store.
- Keep value, recipient ownership and incarnation checks inside the existing
  message transaction. Do not remove the precondition, widen peer permissions,
  re-enroll members or retry mail automatically.
- Reproduce the failure through real private-socketpair mTLS with a freshly
  approved member, exact mention resolution and durable grant admission. The
  former tests passed an unbound recipient and missed this field mismatch.
- This correction belongs on the receiving Team host. No desktop contract,
  database migration, polling or live restart is introduced by the source fix.
- Focused guarded acceptance passed 38 tests, including fresh-member-to-host
  and fresh-member-to-member mail through actual private-socketpair mTLS,
  exact `@@` resolution and durable route admission, threaded replies,
  idempotent retries, and revocation/rejoin between resolution and commit.
  Unknown fields and malformed or stale inbox identities still reject without
  committing mail. No live sends, approvals, provider runs or server restarts
  were performed. Publication and downloaded-asset acceptance follow below.
- Keep this beta limited to the mail gateway correction. Uncommitted cron
  history and subagent assignment work is not part of this release.

## 2026-09-14 — 1.0.0 continuation and history replacement accepted

- Published source: `6f7a43c324a252f4ca847375b17524092752d3c2`.
- [Clean release workflow](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/34915559905)
  passed: 4,087 tests, two skipped; package and manifest signing succeeded.
  Published at 2026-09-15 01:18:12 UTC as release ID `388807461`.
- Fresh public downloads passed Ed25519 signature verification, all three
  GitHub asset digests, stable/API 28 metadata and byte-for-byte comparison of
  all 78 packaged source files against the release commit. Archive SHA-256:
  `3dc9f0466f314d9e9f75dfc586cc3fc180e3f179de77e5657f578aa81e6b7bfb`.
- [The replacement](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.0)
  is the latest public stable server release. Version remains 1.0.0 by explicit
  approval; existing 1.0.0 installations require an explicit reinstall.
  Earlier same-version publication records below are historical.

- Retain ordinary parent ownership across native completion while its current
  children finish. Use a receive-order child lifecycle tracker and one guarded
  empty-input native continuation to collect pending results. Keep existing
  run authority and original answer positions; add no fake prompt or polling.
- Fence Stop, steering, delayed native turn acknowledgements and unsubscribe
  cleanup by their exact owner and native turn. Uncertain continuation delivery
  is not permission to replay the original user request.
- Native Codex `0.154.0-alpha.6.2` v1 and v2 subagent probes confirmed the
  empty-input primitive consumes the exact child result without a new native
  user-message item. Probes used an isolated OS-sandboxed native binary and a
  local synthetic Responses provider, not AgentsServer or real model calls.
- Six immediate child-completion repetitions using the modified transport
  passed (three per native subagent mode). The native continuation DTO does not
  expose a client user-message identity: uncertain acceptance is proved only
  by the retained native stream, never by guessing the latest history turn.
- Preserve sidebar recency, unread state and active ownership when importing
  typed native child notifications. Both import paths keep those records silent;
  identical user-authored quotations remain visible.
- The guarded transport, ordinary/native-goal lifecycle and history
  regression group passed 254 tests, including 30 new ordinary continuation
  scenarios. The guard rejects any import of the server monolith.
- Final ordering review added an exact completed-parent check so delayed
  consumption of an already-finished native follow-up cannot start another
  unnecessary turn. Stop skips interruption only for an explicitly completed
  native handle. The final targeted group passed 153 checks, including both
  regressions; full clean release acceptance follows separately.
- Correct the Cursor idle-warning test to arm its short test deadline after
  actual provider readiness, rather than counting process startup as a second
  idle period. Runtime deadlines and behavior are unchanged by that fixture fix.
- Read-only compatibility review of desktop build 1167 confirms its existing
  active-run contract keeps Working/Running and Stop available during child
  collection. This was a source review, not a live UI test; no desktop runtime
  change is required for this correction.
- No live server, app or user task was installed, opened, restarted or changed
  by these checks or publication.

## 2026-09-14 — 1.0.0 cross-chat history correction included

- Repair legacy asynchronous delivery wrappers that provider history could
  re-import as user messages when the native ledger stored only their clean
  bodies. Require the exact completed native owner, delivery receipts, body
  digest and checkpointed provider item; retain genuine user quotations.
- Apply the same proof before a new import is committed or broadcast, and
  project already-imported duplicates silently on partial history pages.
  Preserve original agent messages, replies, identities and source timestamps;
  do not rewrite provider transcripts or grant messaging authority.
- Guarded, isolated parser/proof/import-boundary checks passed (51 tests),
  including cancellation, changed source, split import ranges, steering,
  conflicting receipts and large histories. No server process was imported or
  started locally. Desktop regression checks and isolated component rendering
  preserve original purple messages, real user quotations and inactive state.
- Read-only validation against the reported stored records passed for both
  historical projection and first-import filtering; original delivery and
  answer events and provider transcript bytes remain unchanged.
- Keep control-only Codex imports from moving sidebar recency forward to the
  import time or backward to an old source timestamp. Eight focused boundary
  checks passed, including unchanged unread state and native run ownership.
- Included in the accepted 1.0.0 replacement above. No live deployment or
  restart performed.

## 2026-09-14 — original 1.0.0 stable accepted (superseded above)

- With explicit approval, replaced the published release record in place at
  22:44 UTC (release ID `388756248`) using the exact original three signed
  assets. Original metadata and downloads were preserved for recovery.
  Fresh draft and public downloads passed signature, digest and source-file
  verification. The source tag, runtime, release notes and version remain
  unchanged; no rebuild or live deployment was performed. The paired desktop
  replacement corrects untitled subagent headings without a server change.
- Published source: `c12efa92c8e91358bbbbba041f29b7a00a1d434e`.
- Promotes the validated beta.8 runtime without additional API, dependency,
  signing-key or storage-schema changes. The release notes compare the full
  upgrade from the previous stable 0.1.25 and distinguish the latest beta.
- [Release workflow](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/34901118266)
  passed: 4,026 tests, two skipped; package and manifest signing succeeded.
- Independent download verification passed: Ed25519 signature, stable channel,
  API contract 28, all three asset digests and all 78 packaged source files.
  Archive SHA-256:
  `0e9dfa4711c1d5ae6d46f83e3d8078c93980d2d9e0fdc997af24c5b640a345c8`.
- [Stable release](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.0)
  is the latest stable server release. Publication did not install or restart
  a live server or change active work. Team Network remains a beta feature.

## 2026-09-14 — 1.0.0-beta.8 accepted

- Published source: `0abf6c7777aff11cecc758c31ae100a88c125781`.
- Plain follow-ups with automatic saved-route metadata now steer scheduled and
  resumed native goals under their unchanged owner and permissions. Explicit
  grants, provider commands, settings changes and Stop/Pause fences remain
  protected. Old/new source differential checks reproduce the former rejection
  and verify exactly-once delivery without goal interruption.
- Add native-admin subagent concurrency settings, preserving other settings
  and active work. Defaults remain owned by Codex; no unlimited sentinel or
  hidden AgentsServer subagent cap is introduced.
- [Release workflow](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/34894048014)
  passed its full gate: 4,026 tests, two skipped.
- Download verification passed: Ed25519 manifest signature, all three GitHub
  asset digests and all 78 packaged source files match the release commit.
  Archive SHA-256:
  `18ce190d7c274c8d6072692cc677feaaf2bf62bc607613800bcfeadd410064b7`.
- [Public beta](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.0-beta.8)
  is available on the Beta track. The Stable channel remains unchanged.
  Publication did not install or restart a live server.

## 2026-09-14 — 1.0.0-beta.7 accepted

- Published source: `a78fd465caed255a356708230f1a8f045d5fce4b`.
- Fix: durable native identity recovery for known completed Codex subagents,
  including repeat/restart deduplication and concurrent lifecycle protection.
- Focused guarded checks passed, with independent old-source failure/new-source
  success and compatibility verification against the existing desktop parser.
- [Release workflow](https://github.com/ZhengyiLuo/AgentsServer/actions/runs/34888789011)
  passed its full gate: 4,008 tests, two skipped.
- Download verification passed: Ed25519 manifest signature, archive digest,
  GitHub asset digests and all 78 packaged source files match the release commit.
- Archive SHA-256:
  `2704bf8783f25e3b0898cbd37f8090b85dea4d0bf0c59c181c5ac31e6d66db98`.
- [Public beta](https://github.com/ZhengyiLuo/AgentsServer/releases/tag/v1.0.0-beta.7)
  is available on the Beta track; the Stable channel was not changed.
- Publication did not deploy or restart a live server. This release does not
  change goal-steering admission or account/model access behavior.
