# AgentsServer 1.0.0-beta.1

Start the 1.0 beta release line alongside AgentsDock desktop 1.0.0-beta.1.
This is a version-line release based on AgentsServer 0.1.26-beta.66; its
runtime, API contract 28, dependencies, release signing key, and data formats
are unchanged. Stable 1.0.0 is not being released.

Included beta.66 fixes remain available:

- Successful asynchronous cross-chat sends return compatible receipts;
  retrying with the same idempotency key does not duplicate the message.
- Chat sharing uses a separate, reusable access token. Interactive tokens
  support multiple collaborators and revocation invalidates share access.
- View-only snapshots are paged, open at the end, and no longer have a
  whole-conversation 2 MiB limit.

## Updating

Existing managed installations can discover this release on the **Beta**
server-update track. Numeric version ordering accepts the move from 0.1.x
to 1.0.0-beta.1; signed manifest verification and immutable archive URLs are
unchanged. Stable-track installations do not silently opt into this beta.

There is no additional data migration compared with beta.66. If upgrading
from an earlier version, the beta.66 snapshot-store migration still applies:
after a new snapshot is created, older server versions cannot read the
upgraded snapshot store. Retain the pre-update backup for downgrade recovery.
Provider transcripts are not rewritten by this version-line change.

Existing compatible desktop clients can connect without an API-contract
upgrade. The separate-token sharing UI is included in desktop 1.0.0-beta.1;
the original desktop requirement was 0.2.13-beta.37 or later.

Publishing this beta does not install it, restart a live server, change
permissions, add polling, or interrupt active chats. Installation remains an
explicit managed-update action.

## Release verification

Published from source `3151c409364b7b22c244b4cc07a579559177c1aa` after the full
release CI gate. The actual CI-signed manifest and archive were accepted by
both the v0.1.25 and beta.66 updater verifiers, including Beta discovery,
immutable URL, signature, version, and forward-update checks.

All 73 packaged files other than `VERSION` match the beta.66 source byte for
byte. API contract remains 28. Archive SHA-256:
`862d7ecb8c1754e4d79b4c093e49c11a7016b0071d1229c35315868f9b8eef36`.
