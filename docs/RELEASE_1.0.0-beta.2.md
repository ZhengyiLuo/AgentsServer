# AgentsServer 1.0.0-beta.2

This candidate did not pass the complete release gate and was not published.
Its tag is retained unchanged. Use the corrected `1.0.0-beta.3` release.

## Cross-chat delivery

- Accept Chats provider-tool message bodies through `stdin` for send, ask and
  the current inbound reply. Previously such a call could fail before storing
  a message, leaving nothing for the other chat to display or consume.
- Preserve existing explicit `--message` calls, exact route authorization and
  idempotency. Reject empty or conflicting body inputs before sending.
- Return the original canceled mailbox receipt on an exact send retry instead
  of a misleading delivery failure. A canceled retry never recreates the
  message or wakes its recipient; revoked routes remain unavailable.
- Cover accepted receipts, both chat event records, reply relationships,
  idle wake admission, busy-chat non-interruption, and cancellation before and
  after a committed send. An exact-key retry recovers the original receipt
  rather than creating another message.

## Team Network

- Extend the existing single notification stream with separate Mail and
  Bulletin cursors. Bulletin posts, revisions and deletions publish only small
  metadata hints after their content transaction commits.
- Keep older Mail-only clients and hosts compatible. No content polling,
  automatic feed fetch, agent interruption or new remote-agent turn is added.
- Retain author-only, versioned Bulletin revisions. Team mail remains a server
  inbox operation; it does not automatically start a remote chat.

## Compatibility and installation

The API contract remains 28. New Bulletin indicators require AgentsDock
`1.0.0-beta.2`; the Chats stdin correction is server-side and also benefits
compatible existing clients.

Team Hub migration 22 adds a metadata-only Bulletin change journal. Back up
the existing Hub before upgrade; for downgrade, restore its pre-update backup
rather than opening the migrated database with an older runtime. Provider
transcripts and user messages are not rewritten.

This beta is published through the existing signed server release channel.
Publication does not install it or restart a live server. Stable-track users
are not automatically opted into the beta.
