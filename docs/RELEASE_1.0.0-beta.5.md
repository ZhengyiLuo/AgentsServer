# AgentsServer 1.0.0-beta.5

This candidate was not published: the full release gate rejected legacy
history-cursor test fixtures that did not provide valid provider identities or
a durable event ledger. Its source tag remains unchanged. Beta.6 carries the
same runtime corrections with realistic cursor fixtures.

## History and cross-chat display corrections

- Repair large native histories without re-importing scheduled runs, internal
  mailbox wake instructions or answers already recorded live. Source proof uses
  streaming reads rather than excluding an entire history at a size threshold.
- Preserve original message identities, timestamps, peer deliveries and genuine
  human text. Incomplete or cancelled reconciliation does not advance its cursor.
- Cover forked provider histories and checkpoint deltas, including repeated
  imported answers after a completed mailbox wake. This changes history
  reconciliation, not delivery, wake frequency or route permissions.

## Shared chats and subagent names

- Serve token-scoped video playback and seeking in Interactive and View only
  shares. Revocation and exact-chat access checks remain enforced; unrelated
  workspace files are not exposed. Older text-only snapshots must be recreated
  to include their videos.
- Supply explicit Codex child-thread titles and live name updates through the
  existing metadata stream. A matching desktop update displays these names.
- Refresh the bundled shared-chat renderer from the tested desktop source,
  including live compaction and quiet-run indicators. Desktop Running/Stop
  freshness corrections require the separate app update.
- Document and test alternate reachable addresses of the same server for LAN
  sharing. Existing links and tokens are unchanged.

## Compatibility and rollout

API contract 28, dependencies and Team Hub schema 22 are unchanged from beta.4.
No new mailbox polling, provider requests or background notification loops are
introduced. The history correction works with the existing compatible desktop;
an app-only installation cannot correct the old server's imported duplicates.

Local checks use isolated state and extracted runtime boundaries, without
starting a production server. Publication requires the complete signed release
workflow and downloaded-asset signature, digest and source verification.

Use the managed updater and wait for idle unless the operator explicitly
authorizes interruption. Accepting a pending update is not confirmation that
the running server has upgraded. This is a beta, not stable-release acceptance.
