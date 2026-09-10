# Codex goal continuation history

Codex persists automatic goal continuations as `response_item` messages with
`role: "user"`. That role describes model input, not human authorship. The old
history parser discarded source metadata and imported the continuation as a
`turn_started` user bubble. The local transcript preview used the same incorrect
assumption independently.

The shared parser now checks
`internal_chat_message_metadata_passthrough.content_item_kinds`. It skips a
complete goal continuation envelope only when every content kind is
`goal.internal_context`. A `user.text` entry, a client user-message identifier,
or explicit human origin preserves the full user text. Missing, malformed, mixed,
and unknown provenance stays visible. Assistant text is unaffected. Positive
human authorship survives both import paths as `provider_user_authored: true`.

Already imported rows require a separate source proof. On an explicit requested
chat history boundary, `CodexGoalHistoryRepairCache` verifies the original import
batch checkpoint, provider identity, source identity and prefix digests, source
range, completed batch, and matching normalized text. Any identical human or
unknown user record makes the repair ambiguous and leaves the row visible.
Preparation is bounded and cached per chat/provider; per-event projection uses
memory only. It never edits an event ledger or a provider transcript.

Previously proven ledger identities survive an in-process provider-thread
rotation. After a restart or cache eviction, preparation can also recover proof
from up to two prior source paths recorded in that chat's completed checkpoints,
newest first. It opens those exact paths under the configured Codex sessions
root; it never walks that root or scans another chat's ledger. The current source
has priority, and all selected sources share a 96 MiB and 100,000-record budget.
The ledger retains its separate 32 MiB/100,000-record limit. A source must be
completely scanned and validated before its proofs are admitted. Missing,
ambiguous, changed or over-budget sources stay visible; sources beyond the two
prior-path cap stay visible too. Positive and negative preparations are cached
for the current chat/provider, so a temporarily unavailable source requires cache
eviction, explicit cache invalidation or a server restart before retry.

A proven old runtime prompt projects to an empty turn boundary with
`provider_runtime_context: "goal"` and `metadata_only: true`. The private hidden
prompt marker preserves assistant routing inside semantic timeline construction
and is removed before egress. Timeline and search projection versions advance so
cached text is rebuilt. A newly prepared proof also queues a rebuild of that
chat's search projection if it was indexed before the proof was available.
Imported turns already do not own running state or mark
agent output unread; the repair preserves that behavior.

Verification uses isolated AST-selected server helpers, fake notifications and
temporary source/checkpoint fixtures. It never imports or starts `agent_server`:

```sh
python3 -m unittest -q test_codex_goal_history_isolated test_codex_history_repair test_codex_native_turn_projection_isolated test_release_file_manifest_isolated
```
