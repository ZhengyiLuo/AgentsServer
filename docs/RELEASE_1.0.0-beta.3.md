# AgentsServer 1.0.0-beta.3

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

The beta.2 candidate was stopped before publication by the full release gate.
This candidate corrects legacy-schema test fixtures and the obsolete canceled
receipt assertion while retaining the same reviewed runtime changes. The
beta.2 tag is not rewritten or reused.

Publication uses the existing signed server release channel and does not
install it or restart a live server. Stable-track users are not automatically
opted into the beta.

## Accepted release

Published from `b689dcef593c1d6591c49fad2fc357973eb15213`. The complete release
suite passed. Downloaded assets passed Ed25519 signature and checksum checks;
all 76 packaged files match that exact source commit. The archive SHA-256 is
`521304efafb423ad0d0d4b5a48bec2a7fd6aa9dfbd923b505ef1fafc1fb9620e`.

Original updater functions from `0.1.25`, `0.1.26-beta.66` and `1.0.0-beta.1`
accepted the actual signed release and selected it on the Beta track while
excluding it from Stable. This verifies release compatibility, not a live
installation or restart.

Focused lifecycle acceptance exercised real helper subprocesses, temporary
SQLite ledgers and certificate-bound TLS: reply identity, exact retries,
cancel/read races, arrivals during paged reads, reconnects, access revocation
and idle-wake recovery. Independent migration interruption probes preserved
existing Bulletin revisions and Mail receipts before a successful one-time
upgrade. No user's active provider run or research job was used for these checks.
