# Claude model names in the runtime catalog

Claude discovery now prefers the installed CLI's SDK `initialize` response,
the source used by TypeScript `supportedModels()` / Python `get_server_info()`.
It needs no user prompt or model generation. No client API shape changes.

## Display and selection

- Keep each native `value` unchanged. For example, `opus[1m]` remains an alias,
  while its label can read `Opus 5.5 (1M context)`. Do not silently pin it to
  `claude-opus-5-5` or change existing chats' saved model values.
- Prefer native `resolvedModel` for versioned names. Older CLIs can expose a
  leading versioned model name in the picker description; use that only when
  no resolved ID exists. Opaque gateway IDs retain the native display name.
- Do not infer versions from our static list, website announcements, or CLI
  version numbers. If native metadata only says `Opus`, keep `Opus`.
- The default native lineup is not the complete selectable list: it can omit
  supported older versions. For first-party Anthropic installs, read native
  effective settings and run a second, disposable initialization with extra
  `modelPicker.options`. Its candidates come from the account's Models API
  when an API key is configured, otherwise from the documented supported-model
  seed in `claude_model_catalog.py`. This seed includes current and older IDs,
  but **is never appended directly to the returned catalog**.
- Only candidates returned by that native picker are included. Claude retains
  enforcement of managed settings, `availableModels`, account restrictions and
  context-window rules. Disabled rows are excluded. A successful empty list
  stays empty, including if policy changes between the two probes.
- Existing custom picker rows are preserved. Explicit replacement lineups,
  third-party/cloud providers and custom gateways are not expanded with the
  Anthropic seed. Missing/unknown effective-settings metadata also keeps the
  original list. Gateway-native discovery stays responsible for its own IDs.
- Discovery means selectable according to native metadata, not a billed
  inference test or a guarantee against quota/auth changes on the next turn.
- The empty value remains AgentsServer's configured default. Native `default`
  is a separate selection and is labeled `Default — <native version>` when
  the CLI provides its resolution.

The next catalog request reads the current executable again; there is no
permanent version cache. Updating Claude Code and refreshing the app's runtime
catalog is sufficient for new native names. This does not switch already-open
provider processes or upgrade the CLI on the user's behalf.

## Failure and privacy boundaries

Discovery runs in its own temporary working directory with no session
persistence, no built-in tools, hooks disabled, and strict empty MCP config.
Only user/managed settings apply, retaining provider/account/organization
configuration without loading a particular project's settings. Auto-update is
disabled for this metadata process. No existing chat is resumed or interrupted.

Stdin contains only `initialize` and read-only `get_settings` control requests,
never user messages, model switches or generation. Extra picker rows apply only
to the disposable process's `--settings`; user settings, saved model choices,
permissions and live chats are not changed. Claude itself can refresh its own
metadata cache timestamp. Only validated model values and labels leave the
parser; account and effective settings remain private. Stderr is not exposed.

On macOS/Linux, the complete probe pair is serialized and shares a six-second
deadline. Each child has a 2 MiB output cap;
its private process group is killed and the child reaped on all exits. Cleanup
has a separate bounded one-second reap allowance per child.
No detached probe, provider SDK session, or cache of account information stays
alive. Other platforms use the existing fallback discovery.

On missing/older CLIs, invalid metadata, or timeout, retain the previous order:
account-scoped Anthropic Models API when an API key is configured, otherwise
CLI help plus the static fallback. Metadata failure does not mark login broken.

## Verification

- Unit coverage: current and synthetic future versions, old descriptions,
  dated IDs, context suffixes, custom gateways, authoritative empty lists,
  malformed/oversized output, timeout, request correlation, privacy, process
  cleanup and observing an executable update without a server restart.
- Runtime integration: native labels preserve values/defaults, filtered older
  models coexist with current choices, disabled/omitted candidates stay absent,
  API results are passed through native enforcement, explicit picker settings
  survive, and optional expansion failure retains the original native list.
- Distribution manifests include the module in npm and legacy release archives,
  installers, direct deploy and staged import checks. Actual offline npm/legacy
  archive tests verify its bytes, not only manifest membership.
- Local native probes on Claude Code 2.1.281 returned Opus 5.5 (1M), Fable 5.1
  (1M), Sonnet 5 and Haiku 4.5, plus older Opus 5/4.8/4.7/4.6/4.5, Fable 5 and
  Sonnet 4.6/4.5. `settings.json` and saved model choices were unchanged. No
  model turn was executed; this is not an inference compatibility test.
- Real native allowlist check (process-only `availableModels` set to Sonnet 5)
  retained only the native Default and Sonnet choices. An isolated server
  catalog build returned the combined, duplicate-free list in 1.78 seconds.
- Canonical monorepo verification: 189 focused server tests passed; a full local
  npm build included the exact module and all 78 packaged Python sources compiled.
  No npm package was published. Electron UI acceptance of this monorepo build
  remains separate from these metadata and packaging checks.

References: [Python SDK](https://code.claude.com/docs/en/agent-sdk/python),
[model configuration](https://code.claude.com/docs/en/model-config),
[supported-model seed](https://support.claude.com/en/articles/11940350-claude-code-model-configuration).
