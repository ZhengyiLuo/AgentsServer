# Automatic completion of explicitly requested secure-peer joins

This guest-server feature is opt-in and requires a guest release containing this
contract. Durable approval waiting additionally requires both the host and
joining server to support the negotiated extension below. It does not activate
legacy pending or already-approved connections, and does not deploy itself.

## Approval requests wait for a decision

An invite link is reusable. With durable approval support on both servers, each
explicit Join stays pending until the host approves or rejects it, or the
requester cancels it. Going away for dinner, closing the desktop, or restarting
the server does not expire the request or the corresponding automatic-join
consent. Host approval, pinned identity, exact requested permissions and the
requester's current connection choice still determine whether it can activate.

The host's secure-peer health advertises `durable_pairing_approval_v1: true`.
The joining server then includes `durable_pairing_approval` in the existing
signed request capabilities. The negotiated wire/storage deadline is `0`,
meaning no approval deadline; the native UI projection exposes `expires_at:
null`. Certificate validity and renewal are separate and unchanged. Legacy
requests without that capability retain their original positive deadline;
older clients cannot safely understand the new deadline and must be updated.
Expired, cancelled or rejected historical requests are not revived by an
upgrade or by replaying the same request. Submit a new Join instead.
Manually downgrading a server to a release without this extension cannot
preserve pending durable joins: that binary treats the stored zero deadline
as expired. Update both servers before creating the replacement request.

There is no per-source-IP pending-request cap: teammates behind a shared
network must not consume a misleading per-person allowance. Bounded overall
state/response capacity, unauthenticated transport flood protection, signature
checks and explicit approval remain in place.

## Capability and consent

`/api/health` advertises `capabilities.automatic_pairing_completion_v1`:

```json
{
  "available": true,
  "version": 1,
  "completion_path": "/api/admin/secure-peers/v1/pairings/{pairing_id}/completion",
  "max_wait_seconds": 600
}
```

The existing authenticated native `POST /api/admin/secure-peers/v1/pairings`
accepts optional `complete_on_approval: true`. Its default is false; values must
be literal JSON booleans. Only a new explicit Join action can create durable
consent. Request replay, crash recovery and retry preserve its original request
identity and negotiated waiting policy; they never create a different consent.
An existing active binding prevents automatic replacement.

The guest's existing 30-second maintenance pass checks at most one eligible
request. It validates the pinned approval and authenticated peer health before
atomically consuming consent and selecting the exact connection. Cancel fences
consent before waiting for network work. Legacy expiry, rejection, revocation,
disconnect, an intervening binding choice and Host-mode changes prevent later
automatic activation. Transport maintenance then works with the desktop closed.

Control status remains version 2. Outgoing pairing records additionally expose
`complete_on_approval`, true for eligible pending consent or its exact completed
active connection, and false for legacy or invalidated choices.

## One-shot completion observation

The completion GET requires the same exact native administrator authentication
and browser rejection as existing secure-peer controls. Query parameters are:

- `expected_server_identity`
- `expected_server_instance_id`
- `expected_transcript_hash` (64 lowercase hexadecimal characters)

The path pairing ID and immutable transcript bind the original request, host,
Hub, pin and scopes. The server derives connection identifiers from durable
state, so a client need not supply nullable pending-approval fields.

The request waits on runtime notifications, without database polling, until a
terminal result or a legacy consent deadline, capped at 600 seconds per HTTP
observation. The cap limits the connection, not the lifetime of a Join. It
returns `{version: 1, completion_state, pairing}` with the existing outgoing
pairing projection and `Cache-Control: no-store`. States are `completed`,
`cancelled`, `expired` or `unavailable`. These describe automatic-join consent,
not necessarily retained peer trust: expired/cancelled consent can coexist with
an inactive approved credential that requires a new explicit action.

If the observation window ends while the exact Join is still pending, the
receipt is `completion_state: unavailable` with
`reason: observation_window_elapsed`. It is not an expiry or cancellation.
After a genuinely long-held response, updated desktops quietly re-arm the
same observer under the same profile, request identity and cancellation signal.
Early or malformed responses, profile changes, lost consent and other errors
do not create an automatic retry loop. No renderer polling, roster refresh or
new Join is performed by this observation renewal.

There are at most 32 concurrent observers. Disconnecting or aborting the HTTP
request removes only its observer; it does not cancel durable consent. Explicit
Cancel still uses the existing cancellation endpoint. Server shutdown releases
observers with an unavailable result when possible.

Desktop clients should capability-gate the opt-in, observe once outside any
operation guard that disables Cancel, and allow a request timeout slightly over
600 seconds. On completion they re-read authenticated control status and adopt
only the matching active connection; no activation replay or renderer/inbox
polling is needed. Guests without the capability keep the existing manual flow.
