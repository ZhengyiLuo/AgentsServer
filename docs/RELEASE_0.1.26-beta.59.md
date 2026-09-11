# AgentsServer 0.1.26-beta.59

Integrates the latest committed server work with the released beta.58 lineage.
Retains the earlier paired-grant rollback, active-goal protection, Host write
draining, migration-read, history-pruning and operator moderation fixes.

Authorized same-server asynchronous chat routes no longer have the arbitrary
stored-route and hourly-message limits. The route-list API negotiates a nullable
limit for new clients while preserving the old numeric compatibility hint.
Pending async replies expose their public message body and support recipient
edits with an exact revision check. Source identity, route permissions and the
sender's original body remain unchanged. Explicit Send now can overtake pending
messages without deleting or reordering those predecessors; already-starting
deliveries remain protected.

Claude history projection now reconciles source-proven scheduled reports and
async delivery input/output replays, including already-imported records. Proof
requires exact provider ownership, bounded source checkpoints and full-body
matching; genuine or ambiguous content remains visible. A committed import
refreshes the bounded proof cache once, with no polling or transcript rewrites.

Individual Team Mail routes can remain authorized for a source chat until
revoked, bound to the exact recipient incarnation and checked on every use.
Queued snapshots cannot resurrect a revoked route or downgrade it to a legacy
grant. Team Mail adds passive arrival hints and bounded on-demand threads built
from exact parent IDs, with per-message visibility checks. Mail arrivals do not
automatically run an Agent or send a reply. Existing Host rename now supports a
negotiated rename-only guard, preventing stale UI state from re-enabling a Host
or changing its network role.

API contract 28 is unchanged. Hub schema advances from 19 to 21 through additive
migrations for Mail arrival cursors and indexed thread parents. Installer and
archive manifests include both migrations and the new Mail modules.

Local validation uses source-extracted server functions and isolated temporary
Hub stores; the AgentsServer monolith is not imported or started on Studio.
The merged candidate passed 183 queue, history and goal checks, plus 50 Mail,
Host, database and package-manifest checks. The dependency lock check passed.
Signed publication still requires the complete release workflow. Installation
uses the managed updater's normal idle activation and continuity checks;
publication alone does not update running servers or stop active research jobs.
