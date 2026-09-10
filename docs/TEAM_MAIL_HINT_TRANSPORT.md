# Team Mail hint transport — next-beta code, production disabled

This is not a shipped or accepted notification journey. The frozen beta54
candidate is unchanged. `SecurePeerRuntime(mail_hints_enabled=False)` remains
the default and AgentsServer does not opt in. Desktop production is likewise
gated. No live service, provider, monolith import, deployment or release is part
of this implementation. The isolated source transport chain now passes; the
remaining cross-process/compiled desktop acceptance must complete before
enabling either gate.

## Metadata contract

`/api/health` exposes `capabilities.team_mail_hints_v1` with `enabled`,
`version:1`, `websocket_path:"/api/team-mail-hints/events"`,
`websocket_protocol:"agentsdock.team-mail-hints.v1"`, and
`mailbox_coverage:true`. An enabled descriptor includes
`mailbox:{hub_id,team_id,recipient_server_id}`. The recipient can initially be
null: current durable peer configuration knows the team/Hub but not the random
Hub node ID. The authenticated initial snapshot establishes it. Health caches
realm metadata and never queries Mail arrivals or fetches an Inbox.
Disabled descriptors retain the exact six-field shape with `mailbox:null`.

Members additionally require the client's single in-memory verified receipt
for `mail_hints_available:true` from existing authenticated peer health. It is
bound to connection/certificate/Hub/team and expires after120 seconds. Missing
old-host fields, unknown/expired receipts and mismatched certificates keep the
descriptor disabled and prevent stream workers or endpoint probes. Activation
and existing maintenance supply those receipts; no new health poll or discovery
request is added. Capture and each actual send recheck that same cached getter.

The local websocket requires the fixed protocol plus existing authenticated
AgentsServer headers/token subprotocol. No query parameters or URL credentials
are accepted. Client sends one frame:

```json
{"version":1,"team_id":"team_example","previous_cursor":null}
```

`previous_cursor`, when present, is the existing version1 cursor with exact
team, recipient, safe integer sequence and immutable `tmsg_32hex` arrival ID.
Server frames contain exactly `type` (`snapshot` then `hint`),
`server_identity`, `hub_id`, a per-socket random32hex `stream_id`, and `cursor`.
The cursor contains the existing five metadata fields plus boolean `reset`.
Only the initial authenticated snapshot may reset; hints always use false.
No body, subject, sender, recipient list, token or agent instructions are sent.

The mTLS lane uses POST `/v1/mail-hints/stream` and the finite
`/v1/mail-hints/snapshot` with the same client frame. Its NDJSON envelope is
only `{type,hub_id,cursor}`; pinned authenticated peer transport binds the host.
All frames are limited to4096 bytes. Mailbox ownership comes from current
certificate claims and active Hub binding, never from a supplied recipient.

A same-team retained cursor for an old recipient binding resets to the
authenticated current owned snapshot without looking up the old mailbox.
Foreign teams still fail. The public store cursor method remains strict;
this old-binding recovery exists only at the transport boundary. Desktop may
adopt a changed recipient only from an initial reset snapshot when the health
descriptor's recipient is null; no old/new mail is thereby marked reviewed.

Local close codes:4401 authentication,4403 realm/peer authority,4406
unsupported/disabled protocol,1008 malformed protocol are terminal.1012
transport/role-generation/expiry and1013 capacity are retryable with desktop
transport backoff. No server Inbox polling or reconnection timer is added.

## Ownership, races and resource bounds

`MailHintLease` wraps the commit-only store broker. It rechecks exact current
authority before each serial write and uses one certificate expiry deadline,
not an idle query. Close wakes its reader, aborts its writer, and waits for that
actual writer to settle. A cancellation request is not a completed write.

`SecurePeerHubAdapter` retains separate passive budgets (64 total,2 per peer),
not `_in_flight`. Revocation fences pending admission, aborts active streams,
and drains them before projection revocation returns. Runtime role and Host
maintenance teardown close passive sockets outside ordinary Host/peer request
counters. A process-local generation plus Host admission epoch rejects a
snapshot that straddles close/reopen, including identical-store ABA.

`RuntimeMailHints` owns at most16 local subscribers and one Member upstream.
Host subscribers read the exact shared authoritative HubStore directly.
Member subscribers share one dedicated upstream reader and scalar broker;
each subscribes locally **before** its own finite authenticated snapshot proof.
An upstream's retained anchor cannot prove a desktop's different old anchor.
No work starts while disabled or merely idle without a subscriber.

Each local socket owns a dedicated Mail worker and bounded serial ASGI writer.
There is no wait in the default executor, ordinary HTTP gateway slots, peer
request budget, managed Hub ASGI drain, or shared chat websocket fanout.
Adapter pre-header store/authority failures preserve terminal403 and
capacity429 across TLS instead of becoming retryable internal500 responses.
The actual event-loop Task's done callback releases its writer fence, including
cancel-before-first-step. A cancellation-resistant ASGI writer retains only its
bounded Mail worker/slot until it truly settles; a timeout does not manufacture
completion. Last-local-subscriber close retires the Member upstream.

Transport loss closes the affected local cohort. Desktop keeps pending state
and reconnects with its retained immutable anchor; reconnect is a scalar
snapshot, never history replay or body loading. Broker coalescing and fresh
snapshot maximum merging preserve commits raced with subscription.

## User-loaded page coverage

Existing list routes now accept opt-in `include_mailbox_coverage=1` and
`after_arrival_id` for positive `after_sequence`. Local Hub service, secure
allowlists/adapter, and Host runtime dispatch forward these unchanged.
Unrequested responses retain their old shape. Only unfiltered owned server
Inbox pages can return `mailbox_coverage` (cursor without reset). A truncated
page covers its actual last returned ID/sequence; a complete page can cover
the durable maximum, including a deleted/dismissed empty catch-up. Positive
predecessors must prove the exact owned immutable ID in the same transaction.

This metadata never acknowledges itself: only desktop application of a fresh,
same-generation, contiguous user-loaded page may advance its seen cursor.
Socket receipt, cached rows, opening Mail and unread state are not coverage.

## Validation and remaining acceptance

`test_team_mail_stream_runtime_isolated.py` uses real Store, adapter, lease,
runtime coordinator, actual Host SecurePeerRuntime and local ASGI handler.
Its Member peer stream is an explicit in-memory adapter-backed replacement;
it does not test TLS or network framing. Cases cover single-upstream fanout,
per-desktop proof, no idle database calls, exact recipient reset, maintenance
ABA, revoke/expiry, coalescing, actual writer drain, cancellation-resistant
ASGI and cancel-before-coroutine-entry. Existing cursor/page and invitation
regressions run through the guarded QA runner as well.

`test_peer_mail_hint_tls_isolated.py` now passes real pinned mTLS, HTTP framing,
idle close/reopen, wrong CA/missing client certificate rejection, and actual
backpressured writer teardown. It replaces listener creation and TCP dialing
with private AF_UNIX socket pairs; no network port is opened.

`test_team_mail_pipeline_tls_isolated.py` composes the actual HubStore commit,
adapter lease, that real TLS gateway/client, actual Member SecurePeerRuntime,
and local ASGI handler. It seeds one approved client descriptor from an
actually issued credential, then executes real health validation, activation
CAS and capability negotiation. Only listener/dial and the ASGI socket object
are substituted. Ordinary Mail and `@@all` cross every source boundary;
Bulletin and another recipient do not emit hints. Idle database/Inbox/receipt
calls are forbidden. Disconnect, an offline commit and retained-anchor reopen
restore the durable scalar; one explicitly requested capped Inbox page proves
only that page's coverage. A body-free golden frame artifact permits separate
validation with the actual desktop parser. Real TLS denial cases preserve
403/429 and release all passive/request capacity.

Pending: full local server→compiled desktop→fresh page badge-clear journey,
offline process restart/restore and authority teardown across actual
processes. These private-socket tests do not claim listener, ASGI framework,
desktop UI, release or production acceptance, and do not enable either gate.
