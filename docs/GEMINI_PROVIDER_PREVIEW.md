# Gemini provider

Gemini is a **built-in, enabled-by-default provider** in this source version.
The integration targets Gemini CLI **0.64.0**, ACP protocol version 1, and desktop.
API setup and a compatible installed CLI are still required to send messages.
This source change does not itself publish a release; validation and known
capability limits are recorded below.

## Authentication is separate from ACP

- **API-only in AgentsDock:** Google/CLI login is not an available connection.
  Native-history imports remain readable without an API. To continue, explicitly
  choose the saved Gemini API during import, or click **Connect Gemini API** in
  an already-imported chat. No account logout, credential deletion or automatic
  rebinding occurs. The API destination is disclosed before connection.
- **Gemini Developer API:** configure a Gemini API key in My Agents. The official
  endpoint is `https://generativelanguage.googleapis.com`; compatible endpoints
  must speak the native Gemini API with `x-goog-api-key`. OpenAI Responses,
  Chat Completions and Anthropic protocols are not interchangeable with it.
- **ACP** is the JSON-RPC transport between AgentsServer and Gemini CLI. It is
  neither an account nor a login method. API connections still execute tasks
  through the installed CLI; its runtime remains required.

New API chats use private server-owned Gemini profiles. They do not modify the
ordinary Google login, silently fall back to it, or write keys into Gemini
settings. Forget revokes the API binding; it does not erase native history.
Conflicting project/system auth settings fail closed rather than bypassing
organization policy. Vertex AI/ADC, gateway-specific auth and native-account
email/plan display are deferred.

No background discovery calls ACP `authenticate`. In the pinned CLI that method
can clear cached credentials and persist a different selected auth type.

### Continue an imported native conversation with API

History storage and authentication are independent. Explicitly connected imports
keep their original native profile, workspace, transcript and UUID. The pinned
CLI's in-memory `GEMINI_CLI_AUTH_OVERRIDE=gemini-api-key` selects the saved API for
that child process; no transcript copy, new native chat, persistent login change
or ACP `authenticate` call is used. Existing native tool context is loaded by the
CLI even though the initial AgentsDock projection remains a text snapshot.
Managed/workspace authentication policy still wins; conflicts fail closed.

Health advertises `gemini_backend.features.native_import_api`. Gemini bulk-import
items may explicitly include `provider_connection: "custom"`. To connect an
existing history-only import, native clients may PATCH the session with only
`{"gemini_connect_api": true}`. This is not general account switching: other
providers, missing native history, archived/deleting/busy/queued chats and
revoked existing bindings are rejected. Persistence must succeed before success
is returned. Shared-chat viewers cannot change a connection.

Forget still blocks sends; reconnecting the same saved credential identity can
restore them. Binding does not silently select a different saved API for a
previously API-bound chat. Later native transcript edits are not automatically
reconciled into the AgentsDock timeline; avoid concurrent writers to one native
session. Delete removes AgentsDock data, not the original provider history.

### API model defaults

For Google's official Developer API endpoint, an unset model now uses Gemini
CLI's native `auto` routing alias. The server passes `--model auto`, not a
hard-coded model version or an arbitrary first catalog entry. My Agents and
chat catalogs expose **Auto (Gemini CLI)** independently of REST discovery;
this is not a claim that every routed model is covered by the key's quota.
Existing empty-model chats work without reconnecting or rewriting credentials.
An explicit chat model wins over its saved connection default, and both reach
the CLI. Third-party URLs still require an explicit model; their support for
Google's Auto model family is unknown. Native Google login is not offered in AgentsDock.

References: [Gemini authentication](https://geminicli.com/docs/get-started/authentication/),
[ACP mode](https://geminicli.com/docs/cli/acp-mode/),
[Developer API models](https://ai.google.dev/api/models).

## Set up Gemini

Use matching desktop/server versions that include Gemini. No feature flag is
required. Install `@google/gemini-cli@0.64.0` on the server machine, then configure
your Gemini API connection in **Settings → My Agents → Gemini**.
`AGENTSDOCK_GEMINI_BIN` may point to an isolated CLI 0.64.0 executable; otherwise
the server discovers `gemini` on its executable search path. No automatic CLI
installation or upgrade occurs. For development tests, use disposable server
state, workspaces, tokens and ports; do not repurpose existing services or user
login profiles as fixtures.

The health capability is `gemini_backend`, with required client capability
`gemini_acp_v1` on turns. Server capability describes integration support, not
CLI installation or successful authentication. Old clients cannot send Gemini
turns accidentally, and old servers do not advertise Gemini support.

My Agents gains a fifth, expandable Gemini row with an API card only.
Initial API setup does not require guessing a model ID. It discovers
models from the configured endpoint, then uses the existing default-model
control. A public catalog alone is not proof of authentication: such endpoints
require an explicit model check. Manual model selection remains available.

## Implemented boundaries

- Owned ACP process per turn, explicit workspace, persisted native session UUID,
  continuation in the same workspace, bounded protocol input/output and probes.
- Native text/thought/tool event mapping; exposed thoughts only, no inference of
  hidden reasoning. Live assistant deltas use a reconnectable stream snapshot,
  displayed as answers rather than thinking; durable blocks replace that
  snapshot without a second copy. Stop preserves already exposed thoughts/text.
  Native diff/location content is retained in tool traces even when ACP omits
  raw arguments; omitted arguments are not invented.
- Run-owned approve-once/deny-once requests through native desktop IPC. Normal
  approvals remain the default; per-chat native modes are described below.
  Server task authorization is separate from provider tool approval.
- Run-bound AgentsDock MCP using the existing authenticated broker and server
  capability checks. Real CLI/loopback-API acceptance covers authorized send,
  idle mailbox wake, inbox/read, bounded user delegation and reply, with native
  one-time approvals and no user-role reinjection. Gemini does not load MCP in
  an untrusted workspace. An explicit handoff fails with a folder-trust/MCP
  instruction if the tool was not loaded; AgentsDock never grants folder trust
  or substitutes shell helpers. Review trust in the native CLI yourself.
  This is separate from account authentication: signing in in a different
  directory does not trust the chat's workspace. An existing route alone does
  not make the MCP tool mandatory for an unrelated ordinary question; explicit
  turn references and internal mailbox/delivery turns still require it.
  Additional real-CLI fixtures reject an unissued recipient and a route revoked
  between list/send, deduplicate retried delivery, and leave a busy recipient's
  existing turn uninterrupted. Authority-file paths are not put in the prompt.
- Gemini sparkle mark and an API-only choice in the desktop picker.
  Slim session polling retains custom model lists instead of replacing them
  with a model-less summary. Gemini errors are not labeled as Codex errors.
- Skills are discovered by the pinned native `gemini skills list --all`, including
  built-ins (marked `built-in`), extensions, user and trusted workspace skills.
  The plain command hides built-ins. Native trust, precedence and disabled-skill
  decisions still apply; unrelated provider-private skill folders are not scanned.
  The slash picker uses
  opaque chat-bound IDs and revalidates the file fingerprint at admission and
  launch. Selection is a **native activation request**, not a native slash
  command: Gemini must call `activate_skill`; ordinary approvals still apply.
- A route-access receipt is permission, not proof of message delivery. Queued
  user turns do not send mail before execution. Gemini must load its run-bound
  MCP tool in a natively trusted workspace and receive any required tool
  approval. A blocked MCP startup sends no cross-chat message; AgentsDock does
  not automatically trust a workspace. Failed first turns without saved native
  history have no resumable Gemini ID; the desktop explains this pending state.
  Its current text-only inventory is bounded and parsed strictly. Ambiguous
  output fails closed; names outside the safe slash grammar are not listed.
- Private API profiles reference the existing user skill/extension directories,
  trust file and policies without copying/installing skills or linking login
  credentials. Skill disables and relevant user security/tool settings are
  retained. This is not a claim of parity for every native customization/hook.
- Strict API protocol/header validation, separate model discovery, existing
  per-chat connection snapshots, Forget/reconnect, and secret-safe diagnostics.
- Small registered text/image/audio attachments reach native ACP. Limits are
  eight files, 512 KiB each and 2 MiB total; images are PNG/JPEG/WebP/GIF, audio is
  WAV/MP3/OGG/FLAC, other files must be UTF-8 text without NUL. Mixed text/image
  and subsequent resume are checked against the actual CLI. WAV bytes also reach
  the actual CLI/API boundary, but this is not real-model/audio-understanding
  acceptance. Arbitrary
  file URLs, PDF/video and larger attachments are not advertised as supported.
- Adopt the native transcript `summary` as an automatic title when it exists;
  manual names win. After the first successful ordinary user turn, a missing
  native title triggers one bounded, tool-free background API request using
  this chat's saved endpoint/key/model. CLI Auto resolves from the exact native
  transcript's model, not an arbitrary model default. Long pasted prompts keep
  both beginning and end within the excerpt budget. No message or native
  session is created; naming failure keeps the first-line fallback without
  retries. Forget, manual rename, identity changes and opt-out fence pending
  results. See [automatic titles](NATIVE_SESSION_TITLES.md) for limits and usage.
- Opt-in native discovery and initial **text-only** history import, preserving
  exact UUID/workspace and title (first actual human message when absent).
  Child, archived, deleted, unavailable, ambiguous and already-owned identities
  are excluded. This lists the ordinary native profile, not private API profiles.
  `.project_root` and the native hash must agree; never infer a path from a slug.
  Earlier media/tools/thoughts and later native edits are not reconciled.
  The desktop requests `include_gemini=true` only with the negotiated capability;
  older clients retain their existing candidate set.
- Provider-reported token/context usage is retained without invented costs.
  ACP plan-update mapping is bounded and unit-tested, but the pinned CLI has not
  demonstrated a dedicated native plan stream. Native todo tools remain tool
  activity; this is not a native goals implementation.
- Server-provided scheduled jobs can use Gemini in chat and standalone context.
  They now carry the required client capability; standalone execution does not
  replace the parent's native identity. Scheduling is AgentsDock behavior, not
  a claimed Gemini ACP scheduling feature.
- Interactive shared chats can send/queue messages and approve or deny Gemini
  tools using their existing single-chat grant, cookie and CSRF boundary. The
  same exact session/run checks apply as in the desktop. Shared runtime updates
  refresh the mounted approval shelf; no polling or fabricated chat events are
  added. Revoking the share removes access. No server-wide authority is exposed.
- Failed first generation without any resumable model record may retry fresh;
  visible output/tool activity or an existing UUID prevents silent replacement.
  Missing/corrupt history then fails closed. Queue promotion and restart resume
  preserve the same provider UUID. Bind identity before exposing output or
  approving tools, not only after completion.
- Delete retains provider-native history. Idle and pending-tool deletion,
  unrelated-chat isolation, stale-approval rejection and direct native ACP
  continuation after Delete pass in disposable fixtures, including queued work.
  An owned parent-death guard cleans up on abrupt server exit. A real tool with
  a sampled detached descendant is terminated after server SIGKILL; restart
  resumes the same UUID. Failed cleanup keeps the chat fenced and Delete fails
  without erasing it; Stop/Delete can retry the owned cleanup. A process exit
  alone is not accepted as proof of cleanup. Process sampling is not an OS
  sandbox and does not prove containment of arbitrary unobserved double-forks.
  The failure fence is durable: restarting does not clear it. If the live owned
  cleanup handle has been lost, an operator must verify the old runtime's exit
  before repairing the fence; there is no automatic unlock or public bypass.

### Startup Stop and Delete regression

Gemini's process guard now acknowledges readiness before the owner may signal
it. Cancellation during subprocess creation retains the process handle so the
runner can verify cleanup. Stop and Delete use that owned cleanup operation,
not generic process-tree termination that can kill the guard before it reports
the result. Failed native exec is distinguished from failed cleanup; legacy
Cursor exit behavior is unchanged. Unverified cleanup still blocks reuse and
Delete, including after restart. This fix does not automatically clear an old
persisted cleanup fence.

Regression coverage includes 20 simultaneous-start/Stop cases in bounded
batches, cancellation while constructing the subprocess transport, 12 early
Stop/retry cycles across three concurrent chats and an unaffected neighbor,
four early Deletes, and five actual desktop Stop/retry/reopen cycles. The
desktop uses production IPC/HTTP and the actual CLI with a loopback model.

The repeatable long-message stress test uses four concurrent chats, ten turns
each, 134,890 UTF-8 bytes per reply and 40 WebSocket reconnects. It checks exact
content, stable native IDs and preserved native history after Delete. Run with
an isolated test environment and the supported CLI:

```sh
AGENTSDOCK_TEST_GEMINI_BIN=/path/to/gemini \
AGENTSDOCK_TEST_GEMINI_STRESS=1 \
uv run --python 3.13 python -m unittest tests.test_gemini_stress -v
```

These tests create disposable state and a local model fixture; they neither
exercise Google inference nor certify low-memory or sustained production load.

### Native permission modes

The desktop composer offers Gemini's own **Default**, **Auto Edit**, **YOLO**
and **Plan**, for API connections. These are not Codex
sandbox/access profiles. New chats inherit the last selected Gemini chat's
mode; absent a saved preference, they use Default. YOLO selection requires an
explicit risk confirmation. A change applies at the next turn's start and
does not release a current approval or replace the native session UUID.

Create/PATCH accepts `gemini_permission_mode` with exact ACP IDs `default`,
`autoEdit`, `yolo`, `plan`. Health advertises `permission_mode_control` and the
baseline three modes. Detailed sessions and Gemini runtime responses include
`gemini_permission_modes` / `permission_modes` after a native handshake; runtime
also distinguishes the saved `permission_mode` from `active_permission_mode`.
Plan only appears after Gemini reports it enabled for that workspace during a
turn. This capability read does not launch another CLI or check credentials.
Workspace/connection changes invalidate the saved mode-list evidence.

Every turn passes an explicit startup approval mode and confirms it using
`session/set_mode` before sending the prompt. Native trust, administrator
settings and deny policies remain authoritative. In the pinned CLI, ACP mode
switching alone does not enforce the administrator's YOLO-disable flag, so
YOLO must also go through the CLI's startup validation. Conversely, disabled
Plan silently falls back to Default through the CLI flag: we start in Default,
require Plan in the actual ACP mode list and select it explicitly. Unavailable
modes fail closed, without sending the prompt. API profiles preserve native
Plan enable/disable settings without changing login or native settings.

Default requests tool approval according to native policy. Auto Edit approves
edits but still requests approval for non-allowed commands. YOLO can run commands
without confirmation, subject to native restrictions. Plan blocks ordinary
project writes, but may save native plan artifacts; it is not an OS sandbox.
Neither mode grants AgentsDock route/task authority. Shared-chat viewers do not
receive this admin permission editor. Ordinary answer text is retained; only
the exact pinned `[MODE_UPDATE]` notice correlated with our pending mode-change
RPC is treated as runtime state rather than a model answer.

Acceptance: real CLI/HTTP fixtures verify tool execution/denial and next-turn
ownership, and actual offscreen Electron uses production IPC/HTTP to save modes,
confirm/cancel YOLO, preserve pending approvals and block a project write in
Plan. A separate actual-App run with Google's Developer API completes Default
then Plan with the same native identity. Google OAuth is intentionally
unavailable in AgentsDock; broader organization policies and other platforms
are not claimed by these checks.

### Important native replay behavior

In CLI 0.64.0, `session/load` starts `streamHistory` without awaiting completion.
Its JSON-RPC response is **not** an end-of-history marker. Dropping replay only
until that response can duplicate the previous answer in the next turn.

The adapter reads a bounded, read-only snapshot of the exact native UUID and
workspace, then verifies the expected replay sequence before sending a prompt.
It checks the snapshot again after load. This is not fuzzy content deduplication:
matching takes place before the new prompt, so legitimate identical new replies
remain distinct. JSON/JSONL text records and native patch/reset/rewind ordering
are recognized. Native tool-response placeholders are included in the replay
fence, never emitted as new user turns. The pinned loader itself normalizes
model/tool parts and inserts deterministic response records during load; the
second snapshot accepts only that predicted conversion, not arbitrary new or
changed messages. It also permits the pinned loader to refresh only its first
exact native `session_context` header, with identity/metadata unchanged: a tool
creating a file changes that header's directory listing. Actual user messages
and tool history remain exact. Inline media placeholders use the exact pinned native format;
unknown history formats, ambiguous files, symlinks,
changed snapshots and limits fail closed. Limits are 16 MiB per transcript and
10,000 scanned entries/replay events. This private-format dependency is one
reason for the exact CLI version fence; it needs an upstream protocol completion
signal or equivalent before broad compatibility can be promised.

## Verification recorded so far

- Unit/transport tests: version/output limits; malformed protocol; auth/profile
  isolation; explicit one-time approval; wrong-chat/stale ownership; delayed
  replay after the load response; history mapping and bounded failure paths.
- Actual CLI 0.64.0 against a **loopback fake Gemini API**, with disposable homes:
  initialize, new, response, native models, continuation without duplicated
  replay, process cleanup and retained native transcripts.
- Actual AgentsServer HTTP boundary with that CLI/API fixture: API configuration,
  old-client rejection, repeated turns, Forget blocking an existing chat,
  reconnecting that binding, and Delete retaining native transcripts. Skill
  inventory and stale-selection rejection, explicit native activation approval,
  and two subsequent resumed turns also pass without duplicate user/reply events.
- Actual CLI/local API skill contract: installed user skills appear in the API
  profile, native `activate_skill` requests approval, denied writes leave no
  file, approved writes produce the file, and owned processes exit. Native
  project/user precedence, trust and disabled-skill handling have a separate
  contract fixture. No Google model service is involved in these fixtures.
- Initial Google OAuth and Developer API attempts encountered upstream
  eligibility and billing errors. Subsequent actual desktop Developer API
  acceptance succeeds, including Default/Plan, skills and cross-chat flows
  recorded above. Google OAuth is no longer exposed as an AgentsDock connection;
  cached login presence is not proof that inference works. Known eligibility, credits
  and model-removal errors have actionable, secret-safe messages.
- The visible development desktop was restarted against the Gemini-enabled
  server: New Chat originally showed native and API Gemini choices; the API-only
  policy now exposes just the API choice with the sparkle
  mark, the slash picker lists installed skills, and an explicit model refresh
  retains the returned model list through subsequent compact session polling.
  First-generation failure recovery is now separately covered with a real CLI
  and synthetic model failure. Existing/mutated UUIDs still fail closed rather
  than silently substituting a new conversation.
- Actual offscreen Electron window using the production preload, IPC, service,
  HTTP and built renderer: configure API without an initial model ID, display
  discovered model options, save a default via the manual-model form, refresh
  the chat list, create the Gemini API chat through New Chat, select its model
  through the composer menu, send four UI turns, select a native skill from the slash picker,
  approve its activation in the App, and resume afterward with no duplicated
  user/reply events. Screenshots were visually checked in English/dark and
  Chinese/light/narrow modes; keyboard focus and the expandable settings row
  were exercised. The native
  select popup was not operable offscreen, so popup interaction itself is not
  accepted by this test. The composer model popup is a different, exercised
  control. No real Google credentials or model service were used.
- Extended actual offscreen desktop acceptance: see the live answer before its
  terminal event, open the grouped import picker, find the native Gemini title,
  resume the detected ID and render its initial user/assistant text. Fixture
  setup and native metadata are synthetic; rendering, IPC, HTTP and CLI are real.
- Actual shared webpage in a separate offscreen Chromium session: enter the
  invitation token, send a skill request, approve its native activation, receive
  two consecutive replies without replay duplicates, then revoke access. Light/
  narrow and dark layouts were visually checked. This runs the packaged web
  asset, real HTTP/SSE authorization and actual CLI, with a loopback fake API.
- The extended Gemini tests include native
  queue/restart/resume, Stop, denied write, active Delete, API Forget/reconnect,
  attachments, cross-chat and title adoption/manual-title protection, plus
  crash-mid-tool recovery, cleanup failure, scheduled jobs, shared approvals and
  three concurrent chats with three resumed turns each. The latter is bounded
  concurrency acceptance, not a low-memory or production-load certification.
  Related server regressions, desktop checks and build results are recorded in
  the development log. None substitutes for real Google account acceptance.
- Additional bounded stress acceptance: an installed development instance served
  8,515 authenticated HTTP reads and 1,854 WebSocket catch-up/reconnections over
  60 seconds with 16 workers of each kind and no request failures or restart.
  This used four disposable empty chats; existing event logs and configuration
  were unchanged. It is a loopback connection test, not remote-network or
  large-history acceptance.
- Separately, four concurrent real CLI chats completed ten turns each against a
  loopback fake model API. Each streamed reply contained 134,890 UTF-8 bytes,
  including Chinese. Forty event-stream reconnects, exact reply retention,
  distinct/stable native UUIDs and Delete preserving native history passed.
  A repeated-text fixture triggered native loop protection; a direct CLI probe
  also returned `max_turn_requests`. The protection was retained and the long
  success fixture uses nonrepeating text. These short bounded runs are not
  long-duration, low-memory or successful Google-account acceptance.

## Validation status and known limitations

### Checklist disposition

| Checklist area | Current disposition |
| --- | --- |
| Runtime/auth/models | CLI runtime + isolated API profiles; local fixtures and bounded actual desktop Developer API requests pass. Google/CLI login is intentionally unsupported, not a pending acceptance item. |
| New/resume/Stop/Delete | Native UUID, queue, crash recovery, retained history and truthful cleanup tested. Early startup Stop/Delete race fixed and exercised through the actual desktop; old persisted cleanup fences are not automatically cleared. No native Fork. |
| Reasoning/trace/usage | Exposed native ACP events retained; fixture ordering/reconnect pass. Full real-model parity still unverified. |
| Titles | Native-summary priority and tool-free API fallback implemented. Actual offscreen App + Google Developer API verifies first-turn short English and long Chinese/log naming, Auto resolution, no hidden user turn, repeated-turn identity and reopen. Native fixtures cover missing summary/manual-name protection; failure/revocation/race cases have focused regressions. |
| Agent-to-agent | Server-provided, run-bound MCP; allowed/denied/revoked/busy/retry cases tested without blanket approvals. Actual offscreen App + Google API verifies queue/cancel, one message each way, purple cards, native IDs and reopen; automatic mailbox wakes do not create user bubbles. |
| Import/sync | Native discovery and initial text snapshot implemented; exact identity, workspace and ownership checked. Explicit API connection preserves the original native history/UUID and terminal login settings. Later native edits are not reconciled. |
| Existing skills | Native inventory/activation, trust, precedence, stale selection and approval tested; extension/hook parity not claimed. |
| Jobs/shares/media | Server-provided jobs and interactive shares tested. Text/image/WAV transport tested; media understanding not accepted. |
| Permission modes | Native Default/Auto Edit/YOLO/Plan wired through desktop and ACP, with explicit startup checks, native denial, next-turn semantics and same-session resume tested. Real Google API Default/Plan passed; broader organization/platform policy coverage remains unverified. |
| Other native features | No native Fork/Goal/side-question controls in this integration. Gemini itself has subagents (`invoke_agent` is exposed to the model in ACP), but AgentsDock-managed child lifecycle controls are not implemented. Native terminal `/compress` exists; the pinned ACP command registry does not expose it. |
| Client/release | Desktop and shared web exercised with actual transports/local API fixture. Mobile separate; real-account/platform and release validation pending. |

Apply the [complete provider checklist](PROVIDER_INTEGRATION_CHECKLIST.md), not
just the successful text fixture. In particular:

1. Broader real Developer API failure coverage:
   expired credentials, organization policy, proxies and billing/model availability.
   Google/CLI login is deliberately out of scope under the API-only policy.
2. Complete native tool/trace parity with a usable real account. The implemented
   live text path is accepted through the actual App and local API fixture, not
   as proof of all native rendering behavior.
3. Broader long-duration/resource-pressure and platform acceptance, including
   unobserved escaping processes. Do not interpret a three-chat concurrency
   fixture or bounded protocol-output tests as a memory sizing recommendation.
4. External-history reconciliation is unsupported. Native Fork is unsupported:
   the pinned ACP implementation does not implement `session/fork`; a copied
   transcript is not a substitute. First-successful-turn summarized titles have
   separate actual App/real-model acceptance; this does not establish external
   history reconciliation or native Fork support.
5. Extension/hook customization parity remains unverified. Native permission
   modes have isolated native CLI and actual desktop boundary tests, not full
   organization/platform policy coverage. AgentsDock-managed subagent controls,
   native goals, explicit compaction control and side questions are not exposed
   by this integration; health capabilities say so. This does not mean Gemini
   lacks native subagents or terminal compaction. Actual CLI 0.64.0 ACP probes
   find `invoke_agent` among model tools; child lifecycle management is not
   accepted merely because that tool exists. `session/fork` returns method-not-found,
   while supported `session/set_mode` selections succeed. ACP `/help` lists no
   `/compress`, `/agents`, `/fork`, `/goal` or `/btw` command; sending those strings
   invokes ordinary model inference, not the terminal command. No native `/btw`
   semantics or persistent Goal API is claimed. See the official
   [subagent](https://geminicli.com/docs/core/subagents/),
   [command](https://geminicli.com/docs/reference/commands/) and
   [ACP](https://geminicli.com/docs/cli/acp-mode/) documentation. Mobile requires
   its own implementation task.

Old preview chats with an already-saved native UUID but missing native history
are not automatically repaired. The first-failure prevention fix covers new
attempts; it does not authorize replacing existing conversation identity.

Full suite/release packaging validation and completion of the declared
real-account feature checks are still required. Bounded Developer API success
is recorded above; it is not full-provider acceptance. Default availability does
not remove these limits or imply that a public release has been published.
