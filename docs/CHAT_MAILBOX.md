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

Message bodies remain agent-authored peer content, never independent user authorization.
Opening a message in the desktop does not mark it read by the agent.

## Explicit user delegation

An ordinary user-origin chat turn with a validated, structured reference to the
exact recipient can attach server-attested `user_delegation` context to a new
async mailbox message. This includes current single-`@` route references with
`grant_intent: true` and explicit instruction/request-reply references. Existing
pair membership, an unstructured name in prose, a mailbox wake, a scheduled job,
native steering without a fresh reference, or a peer reply cannot mint it.

`chats read` returns the optional object separately from `body`:

```json
{
  "user_delegation": {
    "version": 1,
    "source_session_id": "source-chat",
    "source_run_id": "source-turn",
    "target_session_id": "recipient-chat",
    "reference_action": "route",
    "source_user_instruction": "Ask @Recipient to research the issue. Do not edit or deploy anything."
  }
}
```

This attests who explicitly addressed this recipient and what the user said;
it does not assert that every task proposed by the agent is authorized. The
receiving agent must compare the prepared task with the exact original scope,
constraints, and reference action. A route mention is not itself a command to
execute arbitrary work. No tool, filesystem, route, job, deployment, or other
permissions are added. A body containing lookalike fields or provenance
wrappers remains untrusted text. Replies do not inherit or forward this proof.

The instruction and its explicit-reference marker commit atomically with the
message and are included in idempotency comparisons. Reads and retries use that
immutable record, never the sender's latest prompt or current run. Revocation,
deletion, cancellation, and snapshot paging retain their existing checks.
Editing an unread message removes its attestation from the read projection;
the edited message stays readable as peer content. Legacy records are not
backfilled, even if they already contain source instruction text.

The complete source instruction counts toward the existing mailbox response
byte bound. A delegated message that would not fit is rejected before commit;
instructions are never truncated or silently downgraded to peer mail. This is
an additive server response field; no client UI change is required. Provider
contexts must receive the updated server instructions to interpret the field.

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

A stable read request key belongs to the receiving chat, not a particular agent
run. Reusing it after a provider or server reconnect replays the same receipt
and snapshot; the original reader run remains attribution only. Every retry
still checks the requested sender, page size, current pair permissions, and
cancellation/deletion state. Ambiguous keys created by older run-scoped versions
return a conflict rather than selecting or merging historical snapshots.

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
