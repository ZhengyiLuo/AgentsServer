# AgentsServer 0.1.26-beta.61

This beta pairs with the new local desktop candidate. Passive mailbox rendering
requires the matching desktop changes; it is not included in the previously
published desktop beta.33 binary.

- Store same-server agent messages in a passive per-chat mailbox. Receiving
  mail does not queue a provider turn, interrupt work, or pause a goal. Agents
  explicitly read sender-grouped snapshots and choose whether to reply.
- Preserve message identity, order, reply relationships and exact read receipts.
  Stable read keys survive a fresh authorized run. Cancellation, deletion and
  current route authorization are rechecked on replay.
- Use bounded provider checkpoint hints and the existing event stream, without
  inbox polling. Older pending messages migrate only when durable history proves
  that execution never started; ambiguous work is preserved.
- Include the historical Claude scheduled-message projection repair and the
  latest native provider-command discovery changes from main.
- Repair duplicated Codex history only when a verified provider checkpoint,
  native turn ownership, message identity, timestamp and full text prove the
  replay. Preserve original human messages and scheduled reports. Retain source
  identity on new imports and prevent known native messages becoming new input.
- Retire exact run ownership and capability state when a terminal event cannot
  be saved. Do not claim successful persistence or automatically drain Claude's
  next queued turn from that failed completion. Storage exhaustion before
  provider launch remains retryable through the existing admission path after
  space is freed; no new polling or global admission latch is added.

Known limitation: explicit idle native Codex Goal Resume does not currently
issue fresh provider-tool authority. Mail remains stored, but agent reads on
that path are not yet supported. This beta does not reuse stale authority or
weaken that boundary. Ordinary authorized-turn goal continuation is a different
path. See `docs/CHAT_MAILBOX.md`.

API contract remains 28. Installation uses the signed managed updater and
when-idle activation; publishing this release does not restart active work.
