# AgentsServer 1.0.1-beta.1

## Fix Team Mail from joined servers

Fix valid `@@` mail sends from a joined server failing with “Team Network mail
is unavailable.” The receiving host's secure-peer gateway rejected the
recipient inbox-identity field used by durable chat-scoped mail routes before
the message could be stored. This affected approved members too; rejoining or
changing their permissions was not a solution.

The gateway now preserves that field for the existing transactional checks.
Revoked or replaced recipients, malformed identities and unknown fields remain
rejected. Stable-key retries still return the original mail receipt rather
than creating duplicate messages.

## Updating

- Update the **receiving Team Network host** to this beta to apply this fix.
  No desktop update or member re-enrollment is required for this correction.
- Previously rejected mail was not delivered and is not resent automatically.
  After the host updates, the sender can submit it again.
- No API-version, database-schema, dependency or signing-key change. No new
  polling or background inbox refresh has been added.
- Join approvals remain separate: the reusable server invite link still
  requires the host to approve each joining server's request.

## Team joins no longer expire after ten minutes

- New join requests stay pending until approved, rejected or cancelled when
  **both host and joining server** run this revised beta. Waiting survives
  restarts and recovery from a lost response; it does not grant extra access
  or approve a request automatically.
- Remove the limit of 16 pending requests per source IP, so teammates sharing
  one network do not block each other. Overall resource and flood bounds,
  signature and identity validation, cancellation and revocation remain.
- A long-held connection reaching its observation limit no longer expires
  the underlying join. No five-second inbox polling is added.
- Already expired requests need a fresh join after both servers are updated;
  rejected and cancelled requests are not revived. Legacy peers retain their
  original deadlines. Downgrading to an older server does not preserve pending
  non-expiring joins.
- Desktop support for continuously observing long waits and displaying old
  expired requests is committed separately, not distributed in this server
  archive.

This is an explicitly authorized **same-version replacement** of
`1.0.1-beta.1`. If that version is already installed, manually reinstall from
the revised, signature-verified server archive using the existing service
user and configuration. **Check for updates and Force update cannot install
this same-version replacement.** The installer retains the previous runtime
for rollback. Publication itself does not restart any server.

## Validation and scope

Focused regression checks cover a freshly approved member's exact `@@`
resolution, durable grant admission and first send to another member through
private-socketpair mTLS, threaded replies, idempotent retries and recipient
revocation/rejoin during a send. These use isolated data, not live user mail.
Join checks cover approval after eight days, restart, lost-response recovery,
shared-IP admission, cancellation/approval races, observer cleanup and legacy
compatibility.

This is a narrowly scoped beta after **1.0.0**. Other in-progress desktop,
cron-history and subagent-display changes are not included. The stable release
channel remains on 1.0.0.
