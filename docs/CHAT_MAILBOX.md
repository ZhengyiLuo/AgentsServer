# Async chat mailbox

Permanent same-server `async_route_v1` sends are stored as mailbox messages.
Acceptance stores each message immediately and returns without waiting for a
reply. If the recipient is idle, one event-driven wake lets its agent read the
mailbox. Busy recipients are never steered or interrupted; their existing tool
checkpoint hint and eventual idle transition handle availability. Existing
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
receiving agent should carry out work covered by that instruction without asking
for renewed user approval. It must compare the prepared task with the exact original scope,
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

### Message bodies and delivery confirmation

The provider tool accepts a Chats message body in its top-level `stdin` field
for `send`, `ask` and `respond-current`. It passes that body through a pipe to
the helper's explicit `--message-stdin` option, not through process arguments.
The standalone helper also accepts that option for `respond`. Existing
`--message` calls remain valid; supplying both forms is rejected before send.
Empty, invalid UTF-8 and oversized stdin are rejected before authority lookup
or network access. Reading stdin is never implicit for discovery commands.

An asynchronous send is confirmed by an accepted receipt with a `message_id`.
A tool error or an assistant's assertion is not evidence of delivery. A
rejected body creates no mailbox entry and therefore cannot wake a recipient.
Retry ambiguous transport outcomes with the same idempotency key; do not
construct a fresh send merely because confirmation was lost.

The local regression uses the real helper subprocess and loopback HTTP against
extracted server handlers and a temporary SQLite mailbox. It covers the
previously rejected stdin reply, both timeline events, idle wake admission,
non-interruption of busy recipients, reply identity and idempotent replay.
It also checks `respond-current` with asynchronous and legacy grants on both
provider paths. Cancellation before send stores nothing; cancellation after
commit remains an ambiguous delivery outcome, and an exact-key retry returns
the saved receipt without creating another message.
This input correction is available in AgentsServer `1.0.0-beta.3`. Installing
the desktop app alone does not update the running server.

Archiving a sender does not retract mail it already delivered. An active
recipient can still discover and read that stored mail using its exact issued
permanent pair. Reciprocal identities, route revisions, revocation and deletion
are rechecked; no unrelated route is added to a delivery run's authority.
The archived chat remains unavailable for new sends, replies or agent wakes.
An idle, non-archived recipient can still wake once to read that mail; wake
admission and its exact authority snapshot use the same receive-only check.
Regression coverage exercises send → archive sender → fresh-turn inbox/read,
pre-archive grants, stable read retries, idle wake, revocation and narrow delivery
authority using the extracted handlers and a temporary real mailbox ledger.

## Quiet availability

Unread availability is event-driven, with no polling loop. An active Codex run
can receive one bounded, body-free runtime hint at a tool completion through a
guarded `thread/inject_items` call. Claude uses exact-owner root-tool completion
hooks. A hint never consumes mail, starts a provider, or reconnects one. Missing
or uncertain hint support leaves mail unread until explicit discovery or idle
wake; it never interrupts running work. Native goal continuations use their own exact
reservation fence rather than an ordinary turn handle.

An idle wake uses the receiving chat's current provider settings and live
permanent pair permissions. It carries a bounded, body-free availability notice;
message bodies remain separate ordered mailbox items. It neither creates routes
nor resumes a paused goal. Normal queued user work wins admission first. No
message polling or extra UI refresh loop is added.

One durable per-chat cutoff prevents repeated wake-ups for the same unread
batch. Newer arrivals can cause one later idle wake; reading or cancelling them
first removes the need for it. Admission rechecks route permission, unread state,
deletion and Stop. Waking is not a read receipt. Stop suppresses the currently
pending cutoff without reading/deleting it; genuinely later mail may wake the
chat once Stop cleanup has finished. Startup releases only known unadmitted
reservations. An already admitted attempt is not replayed after a crash or
ambiguous provider failure; its ordinary run lifecycle reports that failure.
Idempotent send retries also recheck idle admission, so a saved message is not
stranded by a failed receipt publication. Stop consults the durable cutoff even
before the in-memory unread projection catches up with the committed message.
If that cutoff cannot be persisted (for example, a full disk), Stop still runs
and automatic mailbox wakes for that chat fail closed in the current process.
A later successful Stop can establish the durable cutoff and clear this fence.

The availability prompt is server-generated, never public user text. Its native
start records an empty public prompt plus the wake identity, cutoff and full
input hash. The desktop keeps progress and final output visible. History import
must prove provider ownership and exact input identity before suppressing a
replayed wake; ordinary human messages with similar words remain unchanged.

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
messages are excluded from legacy delivery execution recovery; receipt projection
is recovered before the one-time idle mailbox check. Batch reads atomically capture their high-water
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
untouched; migration itself never launches the recipient. The subsequent idle
check can wake an eligible chat for its still-unread mailbox.

Desktop capability: `cross_chat_handoffs_v1.features.chat_mailbox_v1`. The read-only
`GET /api/sessions/{id}/inbox` endpoint is requested on explicit expansion, not
on a timer. Recipient deletion uses the exact message ID. The timeline groups
only adjacent incoming messages from the same sender; human follow-ups and
other activity remain chronological boundaries.

These are additive server/client changes. Deploy the matching server before
claiming that an installed desktop supports idle mailbox wake-up.
