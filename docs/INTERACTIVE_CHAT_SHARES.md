# Trusted interactive chat sharing

This opt-in feature shares control of one live chat with one browser. It does not configure
ingress, open a public listener, or share anything automatically. Configure the
HTTPS origin `AGENTSDOCK_PUBLIC_CHAT_BASE_URL` deliberately before creation.

This is trusted collaboration, **not a provider sandbox**. A guest cannot directly
use native terminal, filesystem, downloads, administration, or other-chat APIs.
The collaborator can stop or steer work, manage the queue and goals, change
chat model/permission settings, respond to approvals, and create or run persistent
scheduled jobs for that chat. Prompts go to the existing chat's agent with its normal tools/context:
the guest can ask that agent to use tools or return sensitive information. Review
this trust boundary before confirming. Revoking a share cannot erase saved text
or undo work already accepted by the agent. It does not delete or disable jobs
already configured through the share. No owner-file browsing, attachment reads,
or downloads are included; guest-selected uploads remain one-way.

## Management and browser contract

Management requires the same native-only admin header guard as read-only public
snapshots; browser credentials never authorize management.

- `POST /api/admin/interactive-chat-shares/{session_id}` accepts
  `{confirmed_interactive:true,title?,expires_at?}`. Expiry is future Unix seconds.
  The response contains `id`, `title`, `created_at`, `expires_at`, `redeemed_at`,
  `revoked_at`, `warning`, and the one-time `path`/`url`.
- `GET` on that management path returns `{shares:[metadata]}` (newest 100), with
  no recoverable invitation/browser token or URL.
- `DELETE /api/admin/interactive-chat-shares/{session_id}/{share_id}` revokes that
  exact chat/share and returns `{revoked:true}`. Repeated revocation is harmless.

Invitations use `/interactive-chat/{id}#invite={token}`. The secret is in the URL
fragment, not the server request or access-log path. `GET`/`HEAD` serve only the
static shell and never consume the invitation. An explicit Join action POSTs the
token to `/redeem`. The ledger atomically permits one redemption and issues a
separate browser capability in a Secure, HttpOnly, SameSite=Strict, share-path
cookie. Only token hashes are persisted. Lost cookies require a new invitation;
redeemed invitation tokens cannot recreate access.

All guest routes remain below `/interactive-chat/{id}`. `/state` returns the
sanitized native DTO for that one chat: session, timeline events, queue, activity,
goal, schedules, provider/runtime settings, bounded paging metadata, an opaque
revision, and a CSRF value. The viewer reuses AgentsDock's chat components rather
than creating a separate conversation UI. Native event IDs/order are retained;
raw provider transcripts, authority data, and file-reading capabilities are not
added. Callback projections must remove private authority/file fields.
`/events` is same-origin SSE driven by relevant chat revision signals. Updates
coalesce for one second. Twenty-second heartbeats recheck access but do not read
the transcript; there is no event-log polling. Deletion, expiry, and revocation
deny subsequent access; an idle stream closes on its next access check.

POST requests require the exact configured origin and browser CSRF header. Prompt
fields are only `prompt`, `upload_ids`, and stable `request_id`. Native model,
backend, references, purpose, force-send, filesystem, and other-chat options are
not accepted. Uploads are raw bytes with `X-Chat-Filename` and a content type;
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
method. They do not create mutation receipts. Runtime discovery is cached and
demand-driven. Mutations return their sanitized native result in the saved
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

Bounds are 64 KiB prompt text, four attachments per prompt, 8 MiB per upload,
64 MiB total uploaded bytes per share, and 2 MiB projected JSON per state/read
response. Native history uses explicitly bounded pages. Admission bounds are four upload reads, eight ledger workers/submissions,
and 32 SSE connections. These are resource protections, not automatic processing
or background jobs. No attachment download or general file viewer is exposed.

Validation uses synthetic isolated stores/routes, including AST-loaded native
adapters without importing or starting the server process. The compiled native
browser components were exercised for queue/edit/reorder/send-now/stop, goal
pause/resume, settings/permissions/model selection, schedules and approvals.
These are isolated browser checks, not claims of a production provider run.
Creating a real invitation, configuring ingress, and deployment are separate
explicit actions.

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
