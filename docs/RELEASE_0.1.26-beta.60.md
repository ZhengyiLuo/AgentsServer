# AgentsServer 0.1.26-beta.60

Companion to AgentsDock desktop `0.2.13-beta.33`.

- Same-server asynchronous chat routes have no arbitrary stored-route or
  hourly-message quota. Route authorization, revocation and exact-message
  idempotency remain enforced.
- Queued agent messages expose their body and support exact-revision recipient
  edits. Explicit Send now may prioritize a pending message while retaining
  earlier pending rows; already-starting deliveries remain protected.
- Claude history projection suppresses source-proven replay copies of scheduled
  reports and asynchronous input/output, including already-imported copies.
  Ambiguous or genuine authored content remains visible. Provider transcripts
  are not rewritten, and bounded repair does not introduce polling.
- Individual Team Mail routes can remain authorized for a chat until revoked.
  Mail includes passive arrival hints and on-demand exact-parent threads.
  Arrival never automatically runs an agent or sends a reply.
- Host rename uses a negotiated rename-only guard so stale UI state cannot
  re-enable a Host or change its network role. Prior beta.58 deployment,
  paired-grant rollback, active-goal and operator moderation fixes are retained.

API contract remains 28. Hub schema advances from 19 to 21 using additive Mail
arrival/thread migrations, both included in the installer and archive.
Deployment uses the managed updater's when-idle activation and continuity
checks; publication does not restart active research jobs.

Beta.59 stopped at its full CI test gate and was never packaged or published.
This candidate keeps that runtime unchanged and corrects obsolete route-limit,
Mail runtime and synthetic legacy-schema fixtures. Focused source-extracted
and isolated checks passed; the complete signed release workflow remains the
publication gate. The failed beta.59 tag is preserved.
