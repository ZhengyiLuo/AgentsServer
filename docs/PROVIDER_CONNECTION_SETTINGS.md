# Custom endpoints in AI Providers settings

An endpoint is the base URL of an API service. You do not need to host one:
an API vendor or company gateway can provide the URL, API key and model ID.
An API subscription/credit balance is separate from a ChatGPT, Claude or
Cursor consumer subscription. Never paste a real key into chat or a screenshot.

## Start without an existing endpoint

One option covering three runtimes is [OpenRouter](https://openrouter.ai).
Create an account, create a key in [Keys](https://openrouter.ai/settings/keys),
set a spending limit and add credits if your selected model requires them.
Choose a model from its catalog after connecting. Only send data to a gateway you
trust; it and its upstream provider receive the requests you submit.

| Runtime | Base URL | Protocol / key |
| --- | --- | --- |
| Codex | `https://openrouter.ai/api/v1` | Responses; OpenRouter API key |
| Claude Code | `https://openrouter.ai/api` | Anthropic Messages; Bearer; OpenRouter API key |
| OpenCode | `https://openrouter.ai/api/v1` | Chat Completions; Bearer; OpenRouter API key |
| Cursor | Not a generic OpenRouter endpoint | Native Cursor login / Cursor-issued CLI key |

Claude Code and OpenCode custom APIs can now be selected **per chat**. Select the
intended server, open **Settings → AI Providers → Configure API**, enter the
URL and key, and choose **Connect**. Protocol, authentication header and an
optional model ID live in **Advanced**. Saving does not alter native login or
reroute existing chats. Choose the separate custom-endpoint option in a new
chat and select an API model (or enter its exact ID if discovery is unavailable).
Codex uses the same Configure API entry while retaining its separate endpoint
credentials, chat routing and optional model check.

Neutral icons mean unconnected or unconfirmed, not necessarily signed out.
A green API check means the last explicit credential check passed; it does
not certify every model or tool. Merely saving a legacy configuration cannot
turn it green. The native account card is independent of custom API billing.

Four collapsed provider rows show a check when either login method is detected
or connected. Expand to distinguish native credentials from a checked API.
Only unsigned native providers show a short CLI sign-in command and a Read more
link. Run the command as the
user owning the selected server: [Codex](https://developers.openai.com/codex/auth),
[Claude Code](https://code.claude.com/docs/en/authentication),
[Cursor](https://cursor.com/docs/cli/reference/authentication), or
[OpenCode](https://opencode.ai/docs/cli/#auth). They do not start a local login
on a potentially unrelated client machine or claim login succeeded. Claude
continues to verify authentication on actual sends, not a background recheck.

For direct Anthropic API access, create a key and billing setup in the
Anthropic console. Use base URL `https://api.anthropic.com`, Anthropic Messages,
`x-api-key` authentication and a model ID available to that account. For a
company gateway, ask its administrator for the corresponding protocol, base
URL, exact model ID and authentication header instead of guessing these.

A default connection check uses a read-only authenticated route: OpenRouter's
private `/api/v1/key`, or a protected model catalog for other APIs. A public
model list alone never produces a verified state. If a gateway cannot check
keys this way, Claude/OpenCode offer an explicit model request through Advanced.
A successful check does **not** establish native agent login, tool use,
streaming, reasoning, context continuity or full runtime compatibility.
The UI labels this **Connected**, not “logged in”; it records the last explicit
check, not continuous verification.
Checks happen only on explicit user actions. Optional model checks can incur
a small charge; default read-only checks do not run inference. Checks
send neither conversation history nor tools. Rechecking failure removes the
success state. Failed replacements leave the previous saved credentials intact.

Each provider uses matching **CLI Login** and **Custom API** cards. Account
details belong only to CLI Login, never to an independent API key. Codex reports
email and plan through its account API. Claude can expose saved profile email
and subscription metadata, labeled as cached; this does not run auth-status,
renew credentials or assert freshness. Cursor reports email from `status` and
plan from `about` when available. OpenCode has no universal email/plan across
its many providers; missing fields stay absent. Optional account reads occur
on expanding a connected CLI card, use native-only administration and are fenced
to the selected server. Failure to read details does not invalidate the login.

Codex's native account and custom API are separate cards; native usage/account
data is not queried for custom API chats. Connect requires server credential
verification before saving. Its optional advanced model-discovery check is
not authentication proof. The saved verification flag is server-written and
bound to the exact credential revision, not inferred from native login or
public model discovery. Legacy saved keys remain usable but are shown as
saved/unverified until explicitly checked again with the key.

### Model selection

CLI Login keeps the native runtime's model catalog. Custom API independently
queries the saved endpoint using its own key: OpenAI-compatible `/models`,
Anthropic `/v1/models` with pagination, or OpenRouter's user-filtered
`/api/v1/models/user`. Opening a connected Custom API card reads its inventory;
reopening the card refreshes it. No inference runs during discovery.

Choose **Default model** once to reuse that choice for new
chats, without entering the key again. We do not randomly pick a model or treat
the first inventory row as a default. Selection saves automatically. If discovery
is unavailable, the card does not invent a model; advanced connection setup and
the chat selector still allow an exact ID. Friendly names are
shown alongside exact IDs; confirmed non-chat/non-tool models are filtered out.
Inventory presence is not proof of permission, billing or agent-tool compatibility.

Connected Custom API cards offer **⋯ → Forget endpoint**, with confirmation.
This removes the saved selection for new chats, not native CLI login. Existing
custom chats keep their pinned credentials. CLI Login has no Disconnect or
global logout action, so other server instances and terminal logins are untouched.

Saving a default retains the previous credential-check timestamp and creates a
new settings revision. Existing chats retain their original credentials/model;
refreshing their catalog uses their own saved binding, not the new global key.
Cursor does not expose this generic custom-endpoint flow.

The native-only `/api/admin/provider-models/{codex|claude|opencode}` route supports
GET for inventory (optional `session_id`) and PUT with only `model` and
`expected_revision` to change the default. Lists are bounded to five pages,
512 models, 8 MiB and a ten-second discovery deadline. Pagination stays on the
original endpoint; redirects and arbitrary next-page URLs are never followed.

Unconfigured custom APIs do not appear in new-chat/composer provider menus.
Removing a current endpoint does not switch existing custom chats
to native Codex or erase the immutable credentials those chats already use.

Claude/OpenCode bindings are private immutable snapshots, retained across
profile replacement or removal. Endpoint changes require a new chat after the
first turn. Forks inherit the original binding. Importing a native conversation
directly into a custom API is rejected. Claude uses chat-local process env and a
private flag-settings file; OpenCode uses a dedicated custom provider namespace,
explicit model, and process-local config. Neither rewrites global CLI settings.

On macOS/Linux, desktop startup also discovers this OS user's managed default
and named AgentsServer installations. It reads only owned private credentials,
authenticates loopback health without redirects, verifies the persisted server
identity and adds missing profiles to the existing switcher. Existing profiles
and the active selection are preserved; stopped/untrusted installations are
skipped. It is not LAN discovery and does not install or restart servers.
Native credential presence is not a promise that a token remains valid; actual
sends still determine authentication. Claude discovery never runs auth-status
or renews credentials. Custom API keys are always entered explicitly.

Cursor CLI's `--endpoint` configures a Cursor service API, not an arbitrary
OpenAI-compatible model endpoint. Its API key is issued by Cursor. Cursor
desktop BYOK is a separate feature; this does not certify CLI BYOK. The Cursor
card explains the limitation and links to native authentication instructions.

## Storage and boundary

- Keys stay in the selected server's private admin directory. They are not
  returned by read/check responses and are cleared from the desktop form on
  close, navigation and completion. Saved keys are local files, not encrypted
  against the server's OS account; protect that account and its backups.
- The native administration guard rejects browser-origin requests and requires
  the server token. HTTPS is required except for loopback development APIs.
  API requests do not follow redirects or implicitly use environment proxies.
- The optional native bridge uses
  `/api/admin/provider-connections/{claude|opencode}`: GET for metadata, PUT
  for verify-and-save, DELETE to forget, POST `/check` for a saved check.
  Mutations require `expected_revision`; successful writes increment it.
- Removal affects only this connection profile, not native provider history
  or authentication. Old servers show an upgrade notice, not a fake success.
- A compatible desktop **and** server build are required. No automatic
  deployment or account migration is part of this feature.
- Codex's native GET response advertises `connection_check_available`; PUT
  accepts `verify_connection: true`. Only a successful server check can produce
  `connection_verified: true`. Legacy PUT remains compatible but cannot claim
  verification. Neither mutation changes the server's native Codex login.

References: [OpenRouter Codex](https://openrouter.ai/docs/cookbook/coding-agents/codex-cli),
[OpenRouter key status](https://openrouter.ai/docs/api/api-reference/api-keys/get-current-api-key),
[OpenRouter Claude Code](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration),
[Claude gateways](https://code.claude.com/docs/en/llm-gateway-connect),
[OpenCode providers](https://opencode.ai/docs/providers#custom-provider),
[Cursor CLI authentication](https://cursor.com/docs/cli/reference/authentication).
