# Native Codex authentication status

The desktop Codex settings can read the selected server's native Codex account.
AgentsServer does not change the login shared by ordinary Codex chats and the
server host's Codex CLI. Both routes require the
exact native operator token header and reject browser origin, cookies, fetch
metadata and preflight requests. Provider helpers and shared-chat guests cannot
use these controls. The `codex_auth_v1` health capability advertises status
support with `api_key_login: false`.

- `GET /api/admin/codex/auth` calls native `account/read` with
  `refreshToken: false` and returns only `available`, `auth_mode`, `email`,
  `plan_type` and `requires_openai_auth`. Email and plan apply only to ChatGPT
  accounts. Responses are not cached.
- `POST /api/admin/codex/auth/api-key` is retained for older clients but always
  rejects authenticated requests with 409 and instructions to use **Custom
  endpoint**. It does not read the submitted key, open a native manager, or
  issue `account/login/start`. Request authorization and framing checks still
  apply before the route.

Provider API keys belong to [Custom endpoints](CODEX_PROVIDER.md), which stores
them separately and requires an explicit endpoint and model. Select **Codex ·
Custom endpoint** for a new chat to use that key. Manage ordinary Codex sign-in
through the Codex CLI on the server host.

Authentication status does not write credentials, restart the provider, or
refresh tokens. Provider errors are replaced with fixed messages, and
authentication notifications are excluded from chat subscribers. A stalled
account request never retires the shared provider transport.

An ambiguous failure is never retried automatically. Exec-only Codex transport
does not expose native authentication status.
These controls do not perform ChatGPT browser login, logout, or a direct model
API request.

`test_codex_auth_isolated.py` exercises the real router and extracted native
authorization/admission functions with synthetic state and transport, including
rejection of the legacy mutation before accessing the provider. It never
imports the server runtime, accesses a real credential store or calls a model.
