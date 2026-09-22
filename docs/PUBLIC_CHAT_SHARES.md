# Public read-only chat snapshots (AgentsServer 0.1.26-beta.64+)

Direct HTTP links and confirmed-only creation described below require
AgentsServer `0.1.26-beta.65`; the original beta.64 release requires the preview/digest workflow.
Separate-token entry and paged snapshots require AgentsServer `0.1.26-beta.66`
and AgentsDock `0.2.13-beta.37` or later.

This optional API creates a fixed, explicitly requested chat snapshot. Nothing is
shared automatically. It does not open a listener, configure ingress, publish an
existing chat, or add background polling. The public viewer cannot continue a
conversation or access session APIs, file browsing, tools, or the live transcript.
New snapshots can include videos explicitly attached or published in the chat;
this does not grant access to arbitrary files.

The normal share URL contains no token. Recipients enter the separately supplied
access token before seeing any title or conversation text. An optional view-only
token-in-link URL opens the snapshot directly; anyone holding that URL can read
and copy it. Check the chat for secrets before sharing access.
Revocation and expiry stop subsequent reads, but cannot erase saved copies or
bytes already returned by an in-flight request.

## Management contract

All management routes require one exact `X-AgentsDock-Token` (or legacy
`X-ZenithDock-Token`) native admin header. Browser `Origin`, `Cookie`, and
`Sec-Fetch-*` headers, bearer/URL credentials, and ambiguous token headers are
rejected. An unconfigured server token fails closed. Use `application/json` for
POSTs; bodies are limited to 8 KiB and a five-second receive deadline.

1. The desktop's **View only** button calls `POST /api/admin/chat-shares/{session_id}`
   with `{confirmed_public:true,title?,expires_at?,base_url?}`. It captures the current
   durable transcript in one streaming scan into bounded immutable pages.
   A preview round-trip and confirmation checkbox are not required.
   The optional legacy `POST /api/admin/chat-shares/{session_id}/preview` with `{}` returns
   `messages`, `through_bytes`, opaque `digest`, and a privacy `warning`.
   It creates no public capability or share storage. This legacy in-memory preview
   remains bounded to 2 MiB; large conversations use direct paged creation.
2. Review the exact messages, then `POST /api/admin/chat-shares/{session_id}`
   with `confirmed_public: true`, the returned `through_bytes` and `digest`,
   optional `title`, `base_url`, and optional `expires_at` (future Unix seconds).
   When supplied, `through_bytes` and `digest` must be supplied together.
   The digest binds both the durable prefix and the projected message text.
   Changed bytes or changed projection require another preview (409); later
   appends are excluded. A successful response (201) contains management
   metadata, token-free `path`/`url` (`/shared-chat/{share_id}`), a separately
   returned 43-character `access_token`, an optional-use `token_url`
   (`/share/{token}`), and the privacy warning. Secrets are returned at creation
   only. The snapshot access token is reusable until expiry or revocation.
3. `GET /api/admin/chat-shares/{session_id}` lists the newest 100 management
   records, including expiry/revocation state, but never recoverable link tokens.
4. `DELETE /api/admin/chat-shares/{session_id}/{share_id}` revokes that exact
   session's share. Revoking an existing share is idempotent. Listing/revocation
   remain available after the original chat is deleted.

Creation may stay on the existing authenticated connection while `base_url`
selects another reachable address of the same server for the returned links,
such as its LAN IP. This does not change the server's listener or firewall,
perform address discovery, or verify reachability. The common URL still needs
the separate access token; the optional `token_url` includes it. Unlike an
interactive share, a snapshot does not persist an origin binding: access is
token-gated, and its unlock form requires the exact origin being visited.
Cookies remain host-only and scoped to that snapshot's path.

`GET`/`HEAD /shared-chat/{share_id}` initially shows a generic token-entry page,
including for unknown well-formed IDs. It does not open or create snapshot
storage before a cookie or entered token needs checking. A native form submits
`access_token` and optional `remember=1` to `POST /shared-chat/{share_id}/unlock`.
The form is limited to 1 KiB and a five-second receive deadline. Exact same-origin
POSTs are required; missing/null/ambiguous Origin, cross-site requests, duplicate
fields and query credentials are rejected. Only the exact share ID plus matching
token hash can unlock the page. Responses never echo submitted tokens.

Successful entry sets an HttpOnly, SameSite=Strict cookie scoped to that share's
path. HTTPS uses `__Secure-AgentsDock-View` with Secure; HTTP uses `AgentsDock-View`.
By default it lasts for the browser session; Remember sets a 30-day maximum.
Expiry and revocation are checked against storage on every page request, so a
remembered cookie does not bypass them. The optional legacy `GET`/`HEAD /share/{token}`
route remains available without setting a cookie.

Both viewers open the latest page by default and redirect to
`?page=<last>#conversation-end`, positioning the browser at the conversation's end.
Zero-based `?page=N` selects an older/newer page. Navigation on the common URL
contains no token; navigation on the optional bearer URL retains its token.
Pages use escaped static HTML with
green user bubbles, readable assistant cards, timestamps, responsive light/dark
layouts and a safe basic assistant Markdown subset (headings, lists, code and tables).
No JSON mode, conversation actions, scripts, external links, images, or remote
resources are provided. Internal pagination links, the entry form, and native
video controls are active. CSP permits same-origin media only, allows same-origin
forms only on the entry page, disables scripts,
and retains same-origin sandbox identity so pagination preserves Strict cookies.
Invalid/revoked/expired access reveals no conversation metadata. Responses use
no-store, no-referrer and noindex headers. AgentsServer access logs redact the bearer path; any
operator-managed reverse proxy must redact or disable logging of `/share/*` too.

## Origin and storage

The authenticated management request can supply `base_url`, a valid HTTP or
HTTPS origin with no credentials, path, query, or fragment. The native client
derives this from the connected server. Otherwise `AGENTSDOCK_PUBLIC_CHAT_BASE_URL`
is used, falling back to the authenticated management request's origin.
This does not make a private IP internet reachable. Recipients must be on a network
that reaches the server. HTTP does not encrypt the snapshot or its bearer link;
use HTTPS on untrusted networks. Configure any desired ingress deliberately;
publishing a link does not grant authority to other routes.

Snapshot storage is a dedicated owner-only `public-chat-shares` directory under
the configured AgentsServer state directory, with a private SQLite database.
Each share uses a cryptographically random 256-bit token; only its SHA-256 hash
is persisted. Save the returned token if needed: listing cannot recover it.
Snapshot and revocation records are append-only. Public views, including cold
views after restart, open an existing database read-only and never create state.
The first authenticated creation upgrades an older snapshot database transactionally
to schema v3, retaining prior message-count migration behavior and adding the
immutable `public_chat_share_pages` table. Existing snapshots remain unchanged
in their original rows. New shares retain their first bounded page in that row
and later pages in the new table; page and share rows commit in one transaction.
A late source or digest failure rolls back the entire new share. Existing token
hashes, revocations and immutability triggers are preserved. Anonymous views can
read old schemas without migrating them. Older binaries that do not understand
v3 must not be used to open a database after its authenticated upgrade.

There is no raw-history byte ceiling or public-message-count ceiling. The reader
streams a fixed durable prefix one record at a time, including tool-noise bytes
in its confirmation digest without accumulating them as messages. A large raw
history can therefore produce a complete, much smaller readable snapshot.
Resource bounds remain: a 30-second scan deadline, 1 MiB JSONL record,
256 KiB per public message, 100 messages and 2 MiB serialized JSON per page
(including escaping and metadata), 256-character title, and 16 MiB rendered page. Retained
internal-run filtering metadata is also bounded. Limits fail explicitly rather
than publishing a truncated chat. There is no aggregate readable-snapshot byte
ceiling: larger conversations produce more pages without building one large
JSON object or HTML document. A single oversized record/message still fails;
this API never silently drops the remaining conversation to fit.
The projection keeps readable user/assistant text, visible commentary, and
video descriptors from actual user attachments or committed artifact publication;
tools, hidden reasoning, other artifacts, internal digest/status runs, and structurally
proven imported delivery-control segments are excluded. Existing provenance-
aware user-context projection is applied; ambiguous quotations are preserved.
Previewing/sharing reads only the local durable chat log, not provider logs.

## Explicitly shared videos

Video playback is limited to registered videos explicitly attached or published
in the captured chat prefix. Merely uploading an unused file, mentioning a local
path, or returning a tool payload does not share it. Supported media types are
MP4, WebM, QuickTime, and Ogg video; actual codec support depends on the browser.
The viewer uses native controls with metadata preloading and no autoplay.

Each captured message may carry a bounded `videos` array of opaque signed IDs,
filenames, content types, and sizes. The confirmation digest includes those
descriptors, so a changed video projection invalidates a prior preview. Existing
text-only preview digests retain their exact representation. Descriptors are
stored in the existing immutable JSON pages; no media database, background scan,
or extra video copy is created. The SQLite schema remains v3. Older binaries can
still read unchanged text-only snapshots, but their strict message validator
cannot render newly created video-bearing pages; do not downgrade a deployment
that needs to serve those pages.

The common viewer requests `/shared-chat/{share_id}/media/{page}/{message}/{video}`
with its existing share-scoped cookie. Optional bearer-link viewers use the same
indices below `/share/{token}/media/`. These GET/HEAD routes authenticate the exact
share, resolve only the selected frozen page, and then securely open the registered
video for the stored chat. They never accept native file IDs, paths, workspace
URLs, or admin credentials from the browser. Native registry handles are not
included in rendered HTML. Query credentials and cross-origin media requests
are rejected. Streams support one HTTP byte range, including seek, suffix ranges,
HEAD and 416 responses. Every request and active stream access check verifies
expiry/revocation; no background polling is added. Responses are no-store,
same-origin, no-referrer, and nosniff. Operator-managed proxies must also redact
the optional bearer media paths under `/share/*`.

Snapshot media identity is fixed, but its bytes are not copied into snapshot
storage. Removal or alteration of a registered video, or rotation of the server
signing token, can make that video unavailable. The server must reject changed
registry files rather than serve replacement content. Text remains immutable;
old snapshots never gain videos from later events or new projection rules.
Create a new snapshot to include newly supported videos. Revocation cannot
retract video bytes already delivered or copies saved by a recipient.

This implementation has only been exercised with synthetic isolated test state;
checks include more than 64 MiB of raw tool noise, over 2,000 public messages,
stable preview cutoffs, and migration rollback. No actual user conversation has
been shared or deployed by these tests.
