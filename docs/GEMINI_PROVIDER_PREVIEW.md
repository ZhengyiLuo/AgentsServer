# Gemini provider development preview

Status: **experimental, disabled by default, not release-ready**. The first
implementation targets Gemini CLI **0.64.0**, ACP protocol version 1, and desktop.
Do not enable it in a public build until the acceptance gaps below are closed.

## Authentication is separate from ACP

- **Google login:** run `gemini` on the AgentsServer machine and choose Google's
  interactive sign-in. The server reads the CLI's cached OAuth configuration;
  credential presence is not proof that the next remote request will succeed.
- **Gemini Developer API:** configure a Gemini API key in My Agents. The official
  endpoint is `https://generativelanguage.googleapis.com`; compatible endpoints
  must speak the native Gemini API with `x-goog-api-key`. OpenAI Responses,
  Chat Completions and Anthropic protocols are not interchangeable with it.
- **ACP** is the JSON-RPC transport between AgentsServer and Gemini CLI. It is
  neither an account nor a third login method. Both connections still execute
  tasks through the CLI; their credentials and quotas remain separate.

API chats use private server-owned Gemini profiles. They do not modify the
ordinary Google login, silently fall back to it, or write keys into Gemini
settings. Forget revokes the API binding; it does not erase native history.
Conflicting project/system auth settings fail closed rather than bypassing
organization policy. Vertex AI/ADC, gateway-specific auth and native-account
email/plan display are deferred.

No background discovery calls ACP `authenticate`. In the pinned CLI that method
can clear cached credentials and persist a different selected auth type.

References: [Gemini authentication](https://geminicli.com/docs/get-started/authentication/),
[ACP mode](https://geminicli.com/docs/cli/acp-mode/),
[Developer API models](https://ai.google.dev/api/models).

## Enable only in an isolated development service

Use a compatible desktop built from this branch and a disposable server state,
workspace, token and port. Set `AGENTSDOCK_GEMINI_PREVIEW=1` for that service.
`AGENTSDOCK_GEMINI_BIN` may point to an isolated CLI 0.64.0 executable; otherwise
the server discovers `gemini` on its executable search path. No automatic CLI
installation or upgrade occurs. Existing services and user login profiles must
not be repurposed as test fixtures.

The health capability is `gemini_backend`, with required client capability
`gemini_acp_v1` on turns. Without the preview flag the ordinary four-provider
behavior remains. Old clients cannot send Gemini turns accidentally.

My Agents gains a fifth, expandable Gemini CLI row with separate CLI and API
cards. Initial API setup does not require guessing a model ID. It discovers
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
- Run-owned approve-once/deny-once requests through native desktop IPC. No
  persistent blanket shell or MCP grant. Server task authorization is separate.
- Run-bound AgentsDock MCP using the existing authenticated broker and server
  capability checks. Real CLI/loopback-API acceptance covers authorized send,
  idle mailbox wake, inbox/read, bounded user delegation and reply, with native
  one-time approvals and no user-role reinjection. Gemini does not load MCP in
  an untrusted workspace. An explicit handoff fails with a folder-trust/MCP
  instruction if the tool was not loaded; AgentsDock never grants folder trust
  or substitutes shell helpers. Review trust in the native CLI yourself.
- Gemini sparkle mark and separate native/API choices in the desktop picker.
  Slim session polling retains custom model lists instead of replacing them
  with a model-less summary. Gemini errors are not labeled as Codex errors.
- Skills are discovered by the pinned native `gemini skills list`, including
  its trust, precedence and disabled-skill decisions. The slash picker uses
  opaque chat-bound IDs and revalidates the file fingerprint at admission and
  launch. Selection is a **native activation request**, not a native slash
  command: Gemini must call `activate_skill`; ordinary approvals still apply.
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
  and subsequent resume are checked against the actual CLI. Audio transport has
  mapping coverage, not real-model/audio-understanding acceptance. Arbitrary
  file URLs, PDF/video and larger attachments are not advertised as supported.
- Adopt the native transcript `summary` as an automatic title when it exists;
  manual names win. Native summary generation is not guaranteed on a chat's
  first turn. No separate title-model request or hidden user prompt is added.
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
  activity; this is not an accepted goals/scheduling implementation.
- Failed first generation without any resumable model record may retry fresh;
  visible output/tool activity or an existing UUID prevents silent replacement.
  Missing/corrupt history then fails closed. Queue promotion and restart resume
  preserve the same provider UUID.
- Delete retains provider-native history. Idle and pending-tool deletion,
  unrelated-chat isolation, stale-approval rejection and direct native ACP
  continuation after Delete pass in disposable fixtures. Cleanup failure and
  escaped descendants still need their separate acceptance cases.

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
changed messages. Inline media placeholders use the exact pinned native format;
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
- Real Google OAuth and Developer API requests were attempted through desktop
  and directly through the pinned CLI. Upstream eligibility and billing errors
  prevented successful real-account acceptance; login/key presence alone is
  not a successful model request. Known eligibility, credits and model-removal
  errors now have actionable, secret-safe messages.
- The visible development desktop was restarted against the Gemini-enabled
  server: New Chat shows both native and API Gemini choices with the sparkle
  mark, the slash picker lists installed skills, and an explicit model refresh
  retains the returned model list through subsequent compact session polling.
  First-generation failure recovery is now separately covered with a real CLI
  and synthetic model failure. Existing/mutated UUIDs still fail closed rather
  than silently substituting a new conversation.
- Actual offscreen Electron window using the production preload, IPC, service,
  HTTP and built renderer: configure API without an initial model ID, display
  discovered model options, save a default via the manual-model form, refresh
  the chat list, send four UI turns, select a native skill from the slash picker,
  approve its activation in the App, and resume afterward with no duplicated
  user/reply events. The
  disposable chat was arranged through the actual HTTP client, not the new-chat
  dialog. Screenshots were visually checked in English/dark mode. The native
  select popup was not operable offscreen, so popup interaction itself is not
  accepted by this test. The visible desktop separately exercised the composer
  model menu and refresh. Chinese labels have component coverage, not full visual
  acceptance. No real Google credentials or model service were used.
- Extended actual offscreen desktop acceptance: see the live answer before its
  terminal event, open the grouped import picker, find the native Gemini title,
  resume the detected ID and render its initial user/assistant text. Fixture
  setup and native metadata are synthetic; rendering, IPC, HTTP and CLI are real.
- The extended Gemini test batch has 72 passing cases, including native
  queue/restart/resume, Stop, denied write, active Delete, API Forget/reconnect,
  attachments, cross-chat and title adoption/manual-title protection. An
  additional 247 related server regressions pass. Desktop: 5,191 tests pass,
  five skip; type checking, production compilation, eight package checks and
  nine server release-manifest checks pass. These are not real Google account
  acceptance or a full server-suite/release validation claim.

## Remaining before a merge/release recommendation

Apply the [complete provider checklist](PROVIDER_INTEGRATION_CHECKLIST.md), not
just the successful text fixture. In particular:

1. Real Google OAuth and real Developer API key requests, expired credentials,
   organization policy, proxies and billing/model availability.
2. Full desktop new-chat/model-popup and Chinese/narrow visual acceptance;
   complete native tool/trace parity with a usable real account. The implemented
   live text path is accepted through the actual App and local API fixture, not
   as proof of all native rendering behavior.
3. Gemini-specific live unauthorized-recipient, revoked-pair, busy-mailbox and
   retry/replay combinations. Shared authorization regressions pass, but do not
   replace every native integration scenario.
4. Server crash during tool execution, cleanup failure, escaped descendants,
   queued Delete and stress/memory bounds. Graceful restart and pending-tool
   Delete passing do not establish those cases.
5. External-history reconciliation is unsupported. Native Fork is unsupported:
   the pinned ACP implementation does not implement `session/fork`; a copied
   transcript is not a substitute. Native summary adoption does not promise
   immediate generation of a title.
6. Extension customization parity, subagents, jobs/goals, side questions,
   shared chats and non-default permission profiles remain individually
   unverified/unsupported. No native `/btw` interaction is claimed. Mobile
   requires its own implementation task.

Full suite/release packaging validation and a real account acceptance record
are still required. This branch is a starting implementation, not a release.
