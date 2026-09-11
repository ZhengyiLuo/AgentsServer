# Passive chat mailbox

Permanent same-server `async_route_v1` sends are stored as mailbox messages.
Acceptance never starts, queues, steers, or interrupts a recipient turn. Existing
legacy exchanges retain their negotiated delivery behavior.

## Agent workflow

- `chats inbox [--cursor …]` lists unread senders using the current run's existing
  route authority. It does not read message bodies or acknowledge them.
- `chats read --sender <source_session_id> --request-id <stable-key>` returns an
  ordered unread snapshot with individual IDs, timestamps, bodies, and reply
  relationships. Continue a snapshot with its cursor and the same request key.
  A new request key reads later arrivals. Responses are bounded pages, not quotas
  on messages, routes, or stored mail.
- Reads retain durable replay receipts. Cancellation, deletion, and current pair
  authorization are checked again on every page and retry.
- A reply is an independent `send --route <route> --reply-to <message_id>`.
  There is no automatic reply, reply obligation, wait lease, or goal pause.

Peer content remains untrusted message content, never new user authorization.
Opening a message in the desktop does not mark it read by the agent.

## Quiet availability

Unread availability is event-driven, with no polling loop. An active Codex run
can receive one bounded, body-free runtime hint at a tool completion through a
guarded `thread/inject_items` call. Claude uses exact-owner root-tool completion
hooks. A hint never consumes mail, starts a provider, or reconnects one. Missing
or uncertain hint support leaves mail unread for explicit discovery; it never
falls back to a queued execution. Native goal continuations use their own exact
reservation fence rather than an ordinary turn handle.

Known limitation: explicitly resuming an idle native Codex goal currently creates
a control operation without fresh provider-tool authority or client metadata.
The installed app-server schema cannot rebind that metadata through goal/resume
control calls. Mail remains safely stored, but agent reads on that path are not
yet supported. This implementation does not reuse an old proof, weaken the
authorization gate, or start a synthetic user turn to work around it. A normal
authorized turn continuing into its existing goal retains its original run
authority; that is a different path from explicit idle Resume.

## Durability and presentation

The existing SQLite envelope and mailbox metadata commit together. Stored
messages are excluded from execution recovery; receipt projection is recovered
without running the recipient. Batch reads atomically capture their high-water
mark and retain individual message identity across reconnects.

Startup can migrate an older permanent-route queue item only when its ledger
and complete, unchanged history prove that it never started or crossed a
delivery fence. Migration preserves the message identity and removes the exact
old queue entry. Ambiguous, already-started and legacy exchange work is left
untouched; migration recovery never launches the recipient.

Desktop capability: `cross_chat_handoffs_v1.features.chat_mailbox_v1`. The read-only
`GET /api/sessions/{id}/inbox` endpoint is requested on explicit expansion, not
on a timer. Recipient deletion uses the exact message ID. The timeline groups
only adjacent incoming messages from the same sender; human follow-ups and
other activity remain chronological boundaries.

These are additive server/client changes. Deploy the matching server before
claiming that an installed desktop supports passive mailbox delivery.
