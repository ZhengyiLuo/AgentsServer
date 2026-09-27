# Custom endpoints in AI Providers settings

An endpoint is the base URL of an API service. You do not need to host one:
an API vendor or company gateway can provide the URL, API key and model ID.
An API subscription/credit balance is separate from a ChatGPT, Claude or
Cursor consumer subscription. Never paste a real key into chat or a screenshot.

## Start without an existing endpoint

One option covering three runtimes is [OpenRouter](https://openrouter.ai).
Create an account, create a key in [Keys](https://openrouter.ai/settings/keys),
set a spending limit and add credits if your selected model requires them.
Choose an exact model ID from its catalog. Only send data to a gateway you
trust; it and its upstream provider receive the requests you submit.

| Runtime | Base URL | Protocol / key |
| --- | --- | --- |
| Codex | `https://openrouter.ai/api/v1` | Responses; OpenRouter API key |
| Claude Code | `https://openrouter.ai/api` | Anthropic Messages; Bearer; OpenRouter API key |
| OpenCode | `https://openrouter.ai/api/v1` | Chat Completions; Bearer; OpenRouter API key |
| Cursor | Not a generic OpenRouter endpoint | Native Cursor login / Cursor-issued CLI key |

The new Claude Code and OpenCode cards are **settings-only**. Select the
intended server, open **Settings → AI Providers → Configure API**, enter the
URL and key, and choose **Connect**. Protocol, authentication header and an
optional model ID live in **Advanced**. They do not
alter native login, environment files, chat choices or message routing.
Codex retains its existing separate Custom endpoint workflow and model check.

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
The UI labels this **Connected · last check passed**, not “logged in”.
Checks happen only on explicit user actions. Optional model checks can incur
a small charge; default read-only checks do not run inference. Checks
send neither conversation history nor tools. Rechecking failure removes the
success state. Failed replacements leave the previous saved credentials intact.

Codex's native account and custom API are separate cards; native usage/account
data is not queried for custom API chats. Connect requires server credential
verification before saving. Its optional advanced model-discovery check is
not authentication proof. The saved verification flag is server-written and
bound to the exact credential revision, not inferred from native login or
public model discovery. Legacy saved keys remain usable but are shown as
saved/unverified until explicitly checked again with the key.

Unconfigured custom APIs do not appear in new-chat/composer provider menus.
Removing the current Codex endpoint does not switch existing custom chats
to native Codex or erase the immutable credentials those chats already use.

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
