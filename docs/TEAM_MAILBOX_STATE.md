# Server inbox read/unread attention

Local implementation only; no running Hub, deployed server, or release is
changed by this work. Migration 0019 must accompany the server update. Back up
the Hub database before an explicitly authorized deployment; rolling back the
code alone does not reverse an applied migration.

The optional sibling health capability is
`team_mailbox_state_v1: {available: true, version: 1, address_kinds: ["server"]}`.
The frozen `team_messages_v1` capability and recipient receipt fields are
unchanged. Older clients continue seeing historical receipts.

GET message list/detail accepts `include_mailbox_state=true`. Only an owned,
undismissed server recipient of ordinary mail receives the optional private
`mailbox_state: {address_kind: "server", address_id, unread, version}` projection.
It is not added to the shared recipient rows, Sent list, or Bulletin. On the
opted-in server Inbox list, `unread=true` filters by this same effective state.
Default requests retain their old response shapes and historical unread filter.

POST `/v1/teams/{team_id}/network/messages/{message_id}/mailbox-state` requires
`address_kind: "server"`, the exact owned `address_id`, a boolean `unread`, a
nonnegative integer `expected_version`, and an `idempotency_key`. It returns
`message_id`, the new `mailbox_state`, and that recipient's historical receipt.
Both authenticated server-session and secure-peer route allowlists include the
bounded route; the peer's existing write scope is still required.

Migration 0019 adds a nullable attention override and a version to each
recipient. Before the first explicit mutation, attention follows the existing
receipt (`state != "read"`). A mutation runs in a write transaction, compares
the version, sets the override, and increments the version. A stale version
returns `mailbox_state_conflict` (409), requiring a user-directed refresh.
An exact idempotent retry returns its original result without another write,
even if a newer attention mutation has subsequently occurred. Ownership,
global deletion, and dismissal are checked before replay.

`unread: false` also records the first historical read when necessary, retaining
existing delivery/read timestamps. `unread: true` never rewinds that history.
Late legacy read receipts cannot overwrite an explicit attention override.
Repeated reads do not duplicate the historical read outbox event. Changing one
server's copy does not change any other server's attention state. Human
mailboxes, skills, and Bulletin do not gain this mutation.

The desktop negotiates the connected Hub capability, offers Mark as unread in
the existing server Inbox row menu, and uses the same versioned mutation for
Mark as read and opening unread ordinary mail. Successful actions update the
local list/snapshot only; stale list/detail responses cannot replace a newer
attention version. Uncertain retries retain their exact key until a new state
is observed. No polling, notification subscription, global refresh, or automatic
inbox request is added. Notification transport remains separate work.

Validation: `test_team_mailbox_state_isolated.py` uses temporary Hub stores,
mocked secure transport, and AST-extracted service forwarding/allowlists;
`test_release_file_manifest_isolated.py` checks migration and frozen hashes.
Run only with the approved QA safe runner and repository virtual environment,
not by importing or starting `agent_server`.
