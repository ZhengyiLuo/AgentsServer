# Public read-only chat snapshots (next server beta)

This optional API creates a fixed, explicitly reviewed text snapshot. Nothing is
shared automatically. It does not open a listener, configure ingress, publish an
existing chat, or add background polling. The public viewer cannot continue a
conversation or access session APIs, files, tools, or the live transcript.

Anyone holding a share URL can read and copy that snapshot without signing in.
Review the preview for secrets and personal information before confirming.
Revocation and expiry stop subsequent reads, but cannot erase saved copies or
bytes already returned by an in-flight request.

## Management contract

All management routes require one exact `X-AgentsDock-Token` (or legacy
`X-ZenithDock-Token`) native admin header. Browser `Origin`, `Cookie`, and
`Sec-Fetch-*` headers, bearer/URL credentials, and ambiguous token headers are
rejected. An unconfigured server token fails closed. Use `application/json` for
POSTs; bodies are limited to 8 KiB and a five-second receive deadline.

1. `POST /api/admin/chat-shares/{session_id}/preview` with `{}` returns
   `messages`, `through_bytes`, opaque `digest`, and a privacy `warning`.
   It creates no public capability or share storage.
2. Review the exact messages, then `POST /api/admin/chat-shares/{session_id}`
   with `confirmed_public: true`, the returned `through_bytes` and `digest`,
   optional `title`, and optional `expires_at` (future Unix seconds).
   The digest binds both the durable prefix and the projected message text.
   Changed bytes or changed projection require another preview (409); later
   appends are excluded. A successful response (201) contains management
   metadata, a one-time `path`, optional `url`, and the privacy warning.
3. `GET /api/admin/chat-shares/{session_id}` lists the newest 100 management
   records, including expiry/revocation state, but never recoverable link tokens.
4. `DELETE /api/admin/chat-shares/{session_id}/{share_id}` revokes that exact
   session's share. Revoking an existing share is idempotent. Listing/revocation
   remain available after the original chat is deleted.

Only `GET`/`HEAD /share/{token}` exposes a snapshot, as escaped static HTML.
No query options, JSON mode, actions, script execution, links, images, or remote
resources are provided. Missing, malformed, revoked, and expired links return
the same unavailable response. Responses use strict CSP, no-store, no-referrer,
and noindex headers. AgentsServer access logs redact the bearer path; any
operator-managed reverse proxy must redact or disable logging of `/share/*` too.

## Origin and storage

`AGENTSDOCK_PUBLIC_CHAT_BASE_URL` may contain a trusted HTTPS origin, such as
`https://share.example.org`, with no credentials, path, query, or fragment.
If unset, creation returns a relative `path` and `url: null`; it never infers an
origin from request headers. This setting does not make the server internet
reachable. Configure any desired HTTPS ingress separately and deliberately;
publishing a link does not grant authority to other routes.

Snapshot storage is a dedicated owner-only `public-chat-shares` directory under
the configured AgentsServer state directory, with a private SQLite database.
Each link uses a cryptographically random 256-bit token; only its SHA-256 hash
is persisted. Save the returned link if needed: listing cannot recover it.
Snapshot and revocation records are append-only. Public views, including cold
views after restart, open an existing database read-only and never create state.

Bounds: 64 MiB source log, 1 MiB record, 100,000 records, 1,000 public messages,
256 KiB per message, 2 MiB serialized snapshot, 256-character title, and 16 MiB
rendered page. Limits fail explicitly rather than publishing a truncated chat.
The projection keeps readable user/assistant text and visible commentary;
tools, hidden reasoning, artifacts, internal digest/status runs, and structurally
proven imported delivery-control segments are excluded. Existing provenance-
aware user-context projection is applied; ambiguous quotations are preserved.
Previewing/sharing reads only the local durable chat log, not provider logs.

This implementation has only been exercised with synthetic isolated test state;
no actual user conversation has been shared or deployed by this change.
