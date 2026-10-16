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
  hidden reasoning. Assistant text is currently buffered until a block boundary
  or completion, so streaming parity is **not yet complete**.
- Run-owned approve-once/deny-once requests through native desktop IPC. No
  persistent blanket shell or MCP grant. Server task authorization is separate.
- Run-bound AgentsDock MCP wiring using the existing authenticated broker and
  server capability checks. Complete real cross-chat acceptance is still pending.
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
- Delete retains provider-native history. Idle process cleanup and retained
  files were tested; live-tool/queued deletion and native CLI continuation after
  Delete still need their separate acceptance cases.

### Important native replay behavior

In CLI 0.64.0, `session/load` starts `streamHistory` without awaiting completion.
Its JSON-RPC response is **not** an end-of-history marker. Dropping replay only
until that response can duplicate the previous answer in the next turn.

The adapter reads a bounded, read-only snapshot of the exact native UUID and
workspace, then verifies the expected replay sequence before sending a prompt.
It checks the snapshot again after load. This is not fuzzy content deduplication:
matching takes place before the new prompt, so legitimate identical new replies
remain distinct. JSON/JSONL text records and native patch/reset/rewind ordering
are recognized. Unknown multimodal/history formats, ambiguous files, symlinks,
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
  reconnecting that binding, and Delete retaining native transcripts.
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
- Actual offscreen Electron window using the production preload, IPC, service,
  HTTP and built renderer: configure API without an initial model ID, display
  discovered model options, save a default via the manual-model form, refresh
  the chat list, send two UI turns and render two nonduplicated replies. The
  disposable chat was arranged through the actual HTTP client, not the new-chat
  dialog. Screenshots were visually checked in English/dark mode. The native
  select popup was not operable offscreen, so popup interaction itself is not
  accepted by this test. Chinese labels have component coverage, not full visual
  acceptance. No real Google credentials or model service were used.

## Remaining before a merge/release recommendation

Apply the [complete provider checklist](PROVIDER_INTEGRATION_CHECKLIST.md), not
just the successful text fixture. In particular:

1. Real Google OAuth and real Developer API key requests, expired credentials,
   organization policy, proxies and billing/model availability.
2. Actual desktop new-chat/model-popup workflow and native tool approval/denial;
   real tools, thinking order/retention, live assistant deltas and trace parity.
3. Two real chats with request/reply, normal permissions, unauthorized recipient,
   revocation, replay and busy mailbox delivery; no user-role reinjection.
4. Stop/cancel, queued follow-up, disconnect/reconnect, server restart, active
   Delete/cleanup failure, escaped descendants and unrelated-chat isolation.
5. Native titles and external discovery/import/reconciliation. External import,
   attachments and fork are currently rejected, not silently substituted.
6. Complete skill-picker/tool acceptance with a usable real account, extension
   customization parity, plans/usage, subagents, jobs/goals, side questions,
   shared chats and permission profiles. Do not claim support from
   another provider's tests. Mobile requires its own implementation task.

Full suite/release packaging validation and a real account acceptance record
are still required. This branch is a starting implementation, not a release.
