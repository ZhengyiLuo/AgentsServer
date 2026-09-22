# AgentsServer 1.0.0-beta.6

This supersedes the unpublished beta.5 candidate. Beta.5's failed source tag is
retained unchanged; no beta.5 release artifacts were distributed.

## History and cross-chat display

- Repair large native histories without re-importing scheduled runs, internal
  mailbox wake instructions or answers already recorded live. Source proof uses
  streaming reads rather than excluding an entire history at a size threshold.
- Preserve original identities, timestamps, peer deliveries and genuine human
  text. Incomplete or cancelled reconciliation leaves its cursor unchanged.
- Cover forked histories and checkpoint deltas, including repeated imported
  answers after a completed mailbox wake. These changes affect history
  reconciliation, not message delivery, wake frequency or route permissions.

## Shared chats and subagent names

- Enable token-scoped video playback and seeking in Interactive and View only
  shares while preserving revocation and exact-chat access checks. Older
  text-only snapshots must be recreated to include their videos.
- Supply explicit Codex child-thread titles and live name updates through the
  existing metadata stream. A matching desktop update displays these names.
- Refresh the shared-chat renderer, including live compaction and quiet-run
  indicators. Desktop Running/Stop freshness corrections require the app update.
- Cover alternate reachable addresses of the same server for LAN sharing.
  Existing links and tokens are unchanged.

## Validation and rollout

The runtime is unchanged from the beta.5 candidate. Durable history-cursor
fixtures now use valid provider identities and temporary on-disk event ledgers,
so they exercise the real duplicate filter before persistence and recovery.
The production requirement for complete native-history proof is not relaxed.

API contract 28, dependencies and Team Hub schema 22 are unchanged from beta.4.
No new mailbox polling, provider requests or notification loops are introduced.
An app-only installation cannot correct old server history imports.

Publication requires the complete release workflow. Downloaded assets must
match the committed source, archive digest and signed manifest before rollout.
Use the managed updater and wait for idle unless the operator explicitly
authorizes interruption. A queued update is not an installed update.
