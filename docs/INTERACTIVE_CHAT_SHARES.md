# Trusted interactive chat sharing

Available starting with AgentsServer `0.1.26-beta.64` and a compatible desktop
sharing interface. The server release includes the scoped browser renderer.
Direct HTTP links and the two-action creation workflow described below require
AgentsServer `0.1.26-beta.65`; the original beta.64 release requires configured HTTPS.
Separate reusable access tokens require AgentsServer `0.1.26-beta.66` and
AgentsDock `0.2.13-beta.37` or later.

This opt-in feature shares control of one live chat with people holding its access token. It does not configure
ingress, open a public listener, or share anything automatically. The desktop
offers **View only** and **Interactive** and uses the connected server's HTTP or
HTTPS address. No separate domain or origin setup is required for direct access.
Recipients must be able to reach that address (for example, on the same LAN or VPN).
HTTP is supported for trusted networks, but does not encrypt chat contents or
capabilities in transit; use HTTPS for untrusted networks.

This is trusted collaboration, **not a provider sandbox**. A guest cannot directly
use native terminal, filesystem, downloads, administration, or other-chat APIs.
The collaborator can stop or steer work, manage the queue and goals, change
chat model/permission settings, respond to approvals, and create or run persistent
scheduled jobs for that chat. Prompts go to the existing chat's agent with its normal tools/context:
the guest can ask that agent to use tools or return sensitive information. Review
this trust boundary before confirming. Revoking a share cannot erase saved text
or undo work already accepted by the agent. It does not delete or disable jobs
already configured through the share. No general owner-file browsing or arbitrary
attachment downloads are included. Videos explicitly attached to a sent message
or published in the chat can be played; viewers can save those video bytes.
Unsent guest uploads are not exposed as playable media.

## Management and browser contract

Management requires the same native-only admin header guard as read-only public
snapshots; browser credentials never authorize management.

- `POST /api/admin/interactive-chat-shares/{session_id}` accepts
  `{confirmed_interactive:true,title?,expires_at?,base_url?}`. Expiry is future Unix seconds.
  `base_url` may be a different reachable address of this same server, such as
  a LAN IP, while creation still uses the existing authenticated connection.
  Clients can default it to that connection's origin and let the operator
  choose another address before creating the share.
  The exact validated HTTP/HTTPS origin is bound to the share in the ledger;
  changing global configuration does not rebind an existing share. Legacy clients
  can omit it to use `AGENTSDOCK_PUBLIC_CHAT_BASE_URL` or the management request's origin.
  For example, creating through a Tailscale connection with
  `base_url:"http://192.0.2.42:7850"` returns a LAN-address link (substitute the
  server's actual LAN address). Recipients enter the same separately returned
  access token at that origin. This does not open a listener, change firewall
  rules, discover interfaces, or verify that the chosen address is reachable.
  Editing an existing link's hostname/IP is not supported: create a new share
  for the desired address. The previous share keeps its own origin and token.
  The response contains `id`, `title`, `created_at`, `expires_at`, `redeemed_at`,
  `revoked_at`, `warning`, token-free `path`/`url`, and a separate 43-character
  `access_token`. The raw token is returned only at creation; using it is repeatable.
- `GET` on that management path returns `{shares:[metadata]}` (newest 100), with
  no recoverable invitation/browser token or URL.
- `DELETE /api/admin/interactive-chat-shares/{session_id}/{share_id}` revokes that
  exact chat/share and returns `{revoked:true}`. Repeated revocation is harmless.

URLs use `/interactive-chat/{id}` and never contain the access token, including
in query parameters or fragments. Share the URL and token separately.
`GET`/`HEAD` serve only the static shell. The browser requires manual token entry
and an explicit Open action; it does not redeem query/fragment tokens.
The form POSTs `{invitation_token: access_token}` to `/redeem` (the request field
name is retained for compatibility). The same token can open the share in multiple
browsers and can be entered again after a cookie is lost. Each browser receives
an HttpOnly, SameSite=Strict, share-path cookie. Opening another browser never
rotates or invalidates existing browser access.
HTTPS uses the Secure-prefixed cookie with Secure enabled; HTTP uses a distinct
non-prefixed cookie so browsers can actually retain it. Only token hashes are
persisted. Cookies from earlier releases remain valid, and earlier invitation
tokens are also reusable under this contract. `redeemed_at` records first use;
it no longer means the share has been exclusively claimed.
Reloading an already redeemed URL resumes through the existing browser cookie:
the viewer reads authenticated state and opens a new scoped stream without
redeeming again. A browser without a valid cookie requires the explicit Open action.
Browser back/forward-cache restoration reloads instead of reusing a closed
connection. Expired or revoked browser access cannot be restored this way.

All guest routes remain below `/interactive-chat/{id}`. `/state` returns the
sanitized native DTO for that one chat: session, timeline events, queue, activity,
goal, schedules, provider/runtime settings, bounded paging metadata, an opaque
revision, and a CSRF value. The viewer reuses AgentsDock's chat components rather
than creating a separate conversation UI. Native event IDs/order are retained;
raw provider transcripts, authority data, and general file-reading capabilities
are not added. Callback projections remove private authority/file fields and
advertise only signed, path-free `shared_videos` descriptors on visible sent
attachments and published video events. Paging uses the same projection.
`/events` is same-origin SSE driven by relevant chat revision signals. Updates
coalesce for one second. Twenty-second heartbeats recheck access but do not read
the transcript; there is no event-log polling. Deletion, expiry, and revocation
deny subsequent access; an idle stream closes on its next access check.

Redemption requires the exact share-bound origin and access token. Subsequent
POST requests require that origin, browser cookie and CSRF header. Revocation
and expiry deny every browser using the share. Prompt
fields are only `prompt`, `upload_ids`, and stable `request_id`. Native model,
backend, references, purpose, force-send, filesystem, and other-chat options are
not accepted by the prompt endpoint; the approved same-chat controls use the
separate action allowlist below. Uploads are raw bytes with `X-Chat-Filename` and a content type;
responses contain only share-owned upload IDs/name/type/size. Private native file
references remain in the ledger. Another share, even for the same chat, cannot
attach those uploads.

Chat-control requests use an explicit action allowlist and the exact chat from
the share ledger, never a browser-supplied server URL, method, or chat identity.
The same grant covers these controls; it is not a separate permission tier.
Control receipts use the same durable request ledger as prompts, with an
operation-specific fingerprint so a prompt receipt cannot authorize a control.
Fixed read actions are `timeline.older`, `timeline.around`, `timeline.trace`,
`timeline.index`, `jobs.runs`, and `runtime.catalog`; none accepts a URL or HTTP
method. `handoffs.get` additionally accepts only `{id: envelope_id}` to expand an
existing cross-chat message. The server checks exact source/target membership
before its native detail read, then rechecks the returned envelope and participant
identities. The recipient can receive the revisioned recipient-edited body; a
sender-only viewer receives the original body without recipient-edit fields.
Body hashes and conversation/message IDs remain available for the renderer's
correlation checks. Route-authority fields are removed. This read grants no
routes, sends no message, marks nothing read, and exposes no other chat's API or
files. These read actions do not create mutation receipts. Runtime discovery is
cached and demand-driven. Mutations return their sanitized native result in the saved
receipt. A typed prevalidation rejection is a durable known denial; arbitrary
callback exceptions remain indeterminate because a native write may have occurred.

## Durability and bounds

The private owner-only SQLite ledger resides separately under server state.
It records invitations, revocation, browser hashes, uploads, and request receipts.
Exact accepted request retries return the saved receipt. A reused request ID with
different text/files fails with 409. A pending request left by a crash or uncertain
callback also fails with 409 and never automatically invokes the callback again:
inspect the chat before choosing another request. In-process disconnects shield
the accepted callback/receipt commit. Failed or indeterminate uploads retain their
byte reservation because a file might already exist; disconnect does not refund
quota or create an untracked capability.
The browser also checks action/request identity in prompt/control acknowledgments.
A write transport failure, indeterminate receipt, or malformed acknowledgment stops further writes
from that page, even if live state later arrives. It does not automatically resend
the action under a new request ID: reopen and inspect the chat before deciding
what to do next. If acceptance was confirmed but the following state refresh
fails, the accepted action is still reported as accepted, not offered as a retry.

Bounds are 64 KiB prompt text, four attachments per prompt, 8 MiB per upload,
64 MiB total uploaded bytes per share, and 2 MiB projected JSON per state/read
response. Native history uses explicitly bounded pages. Admission bounds are four upload reads, eight ledger workers/submissions,
and 32 SSE connections. These are resource protections, not automatic processing
or background jobs. No general attachment download or file viewer is exposed.
Video playback uses `GET`/`HEAD /interactive-chat/{id}/media/{handle}` with the
same origin-bound, redeemed share cookie. The signed handle is bound to the
exact chat, registered media identity and file revision, not a filesystem path.
Native credentials are never sent to the browser. Known MP4, WebM, QuickTime
and Ogg video types are served with single-byte-range support; the browser must
support the video's codec. Players load on demand, not through an inbox poll.
Registry reads are no-follow and descriptor-relative; links, nonregular files,
foreign chat ownership and changed registry copies are denied. Files are not
copied again. Deletion, mutation or server authentication-key rotation can make
old handles unavailable; refresh the Interactive share to obtain current media
descriptors. Media egress rechecks revocation before headers and while consuming
the response, without a background timer. Native file editors, reveal/download
actions, dragging and unrelated file APIs remain unavailable in the web UI.

An SSE admission slot belongs to the whole response lifecycle and is released
on completion or disconnect, including a disconnect while sending headers before
the stream body begins. Constructing a response alone reserves no slot.

Validation uses synthetic isolated stores/routes, including AST-loaded native
adapters without importing or starting the server process. The compiled native
browser components were exercised for queue/edit/reorder/send-now/stop, goal
pause/resume, settings/permissions/model selection, schedules and approvals.
Browser checks also verified used-invitation denial, live revocation and disabled
controls with no subsequent accepted write or new content after revocation.
These are isolated browser checks, not certification of real provider runs or
public ingress configuration.
Creating a real invitation, configuring ingress, and deployment are separate
explicit actions.

### Cursor availability

The restricted snapshot includes the safe `health.capabilities.cursor_backend`
contract (version and permission modes), separately from runtime readiness.
For a Cursor chat, its baseline catalog is available only when the cached runtime
diagnostic is ready. Missing, signed-out, failed or unchecked runtimes are not
reported ready. Reading state does not probe the CLI or copy full health,
credentials, executable paths or raw diagnostic messages into the share.
Explicit model discovery uses the existing `runtime.catalog` read action;
ordinary server-side turn admission still validates the runtime.

## Renderer artifact

`interactive_chat_share_web.py` is generated from the AgentsDock shared-chat
renderer, not maintained as a second handwritten UI. Build the shared entry
using `electron/vite.shared-chat.config.ts` into its dedicated
`electron/out/shared-chat` output, then run `scripts/package_shared_chat_web.mjs`
from AgentsDock with that directory and the explicit destination Python module. The generated module
contains `HTML`, exact-name `ASSETS`, and `SOURCE_SHA256` for the encoded source
payload. Do not manually edit its bundled JavaScript or styles.

The server serves only keys in that generated map under
`/interactive-chat/assets/`; there is no arbitrary filesystem lookup. Bundled
backend icons, fonts, CSS and JavaScript are included explicitly. Trusted inline
styles used by the shared React components are allowed, but inline scripts and
remote scripts are not. The browser bridge exposes only the approved one-chat
operations and does not run desktop initialization or open native workspaces.
