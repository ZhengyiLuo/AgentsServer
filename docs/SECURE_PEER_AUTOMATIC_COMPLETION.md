# Automatic completion of explicitly requested secure-peer joins

This guest-server feature is opt-in and requires a guest release containing this
contract. The existing host pairing protocol is unchanged. It does not activate
legacy pending or already-approved connections, and does not deploy itself.

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
identity and ten-minute deadline; they never create or extend consent. An
existing active binding prevents automatic replacement.

The guest's existing 30-second maintenance pass checks at most one eligible
request. It validates the pinned approval and authenticated peer health before
atomically consuming consent and selecting the exact connection. Cancel fences
consent before waiting for network work. Expiry, rejection, revocation,
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
terminal result or the original consent deadline, capped at 600 seconds. It
returns `{version: 1, completion_state, pairing}` with the existing outgoing
pairing projection and `Cache-Control: no-store`. States are `completed`,
`cancelled`, `expired` or `unavailable`. These describe automatic-join consent,
not necessarily retained peer trust: expired/cancelled consent can coexist with
an inactive approved credential that requires a new explicit action.

There are at most 32 concurrent observers. Disconnecting or aborting the HTTP
request removes only its observer; it does not cancel durable consent. Explicit
Cancel still uses the existing cancellation endpoint. Server shutdown releases
observers with an unavailable result when possible.

Desktop clients should capability-gate the opt-in, observe once outside any
operation guard that disables Cancel, and allow a request timeout slightly over
600 seconds. On completion they re-read authenticated control status and adopt
only the matching active connection; no activation replay or renderer/inbox
polling is needed. Guests without the capability keep the existing manual flow.
