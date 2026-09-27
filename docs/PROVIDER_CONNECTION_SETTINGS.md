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
URL, protocol, model ID and key, and choose **Verify and save**. They do not
alter native login, environment files, chat choices or message routing.
Codex retains its existing separate Custom endpoint workflow and model check.

For direct Anthropic API access, create a key and billing setup in the
Anthropic console. Use base URL `https://api.anthropic.com`, Anthropic Messages,
`x-api-key` authentication and a model ID available to that account. For a
company gateway, ask its administrator for the corresponding protocol, base
URL, exact model ID and authentication header instead of guessing these.

A successful new-card check means one short text request to the exact API
and model succeeded. It does **not** establish native agent login, tool use,
streaming, reasoning, context continuity or full runtime compatibility.
The UI labels this **Last API check passed** with a timestamp, not “logged in”.
Checks happen only on explicit user actions, can incur a small charge, and
send neither conversation history nor tools. Rechecking failure removes the
success state. Failed replacements leave the previous saved credentials intact.

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

References: [OpenRouter Codex](https://openrouter.ai/docs/cookbook/coding-agents/codex-cli),
[OpenRouter Claude Code](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration),
[Claude gateways](https://code.claude.com/docs/en/llm-gateway-connect),
[OpenCode providers](https://opencode.ai/docs/providers#custom-provider),
[Cursor CLI authentication](https://cursor.com/docs/cli/reference/authentication).
