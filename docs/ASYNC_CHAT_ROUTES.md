# Permanent pair messaging: async_route_v1

One explicit, accepted structured `@Chat` creates both members of an exact
same-server permission pair. Stored route members have a shared `pair_id` and
each other's `paired_route_id`. Later ordinary runs receive fresh snapshots of
their live pair routes. Agents discover them on demand with Chats `list`;
pair permission does not add per-turn prose or copy source history into a
recipient message.

Credentials remain bound to the server, source chat, and active logical run.
The send path intersects its issued route snapshot with the live pair and
current route revision. Delivery turns receive only the exact reverse member
for the message's immutable sender. They cannot discover or contact the
recipient's other peers. Forks inherit no pair permission.

## Negotiation and helper behavior

Health adds `capabilities.cross_chat_handoffs_v1.features.async_route_v1: true`
and `agent_routes.async_route_v1` with:

```json
{
  "available": true,
  "client_capability": "chat_conversation_async_route_v1",
  "mode": "async_route_v1",
  "delivery": "individual_messages",
  "automatic_final_response": false
}
```

A supporting desktop sends that client capability on ordinary turns. A
provider's private runtime advertises support; Chats checks the route listing
before changing behavior. Only permanent pair routes in a negotiated run have
`mode: "async_route_v1"` in that listing. A helper request explicitly using
`--mode async_route_v1` rejects an unsupported route/server before its POST.
Legacy invocations keep their legacy wire contract and do not perform an
additional discovery request.

In this mode, both Chats `send --route` and `ask --route` post one instruction:

```json
{
  "mode": "async_route_v1",
  "action": "instruction",
  "body": "The agent-prepared message or question",
  "idempotency_key": "stable-request-identity",
  "artifact_grants": []
}
```

The endpoint remains `POST /api/agent/cross-chat/routes/{route_id}/handoffs`.
The receipt contains exactly `ok`, `route_id`, `action: "instruction"`,
`accepted`, `mode: "async_route_v1"`, `message_id`, and `duplicate`.
No lease or pending response is returned, and the helper never calls `wait`.
`respond-current` uses its private exact reverse route and sends a new
instruction through the same path. `--request-response` conveys no extra
authority or wait behavior for a paired response; questions belong in the
explicit message body.

## Persistence, admission, and recovery

Each message is a one-way `cross_chat_envelopes` instruction with immutable
`authorization_kind: configured_route`, `authorization_route_id`, and
`authorization_pair_id`. New messages do not create exchange rows, consume
one-use per-route permission, or maintain in-memory reply counters. The
existing 16,000-character/64-KiB body bounds and source/target rolling rate
limits remain. Legacy `max_handoffs_per_run` and exchange-leg fields apply to
legacy exchange modes only.

The durable `(source_run_id, idempotency_key)` constraint compares message
body, route, target, and pair identity. Retries reuse the same envelope and
charge the rate limiter once. A new message requires a new key. The helper's
default key is stable for the same live authority, route, and body; use an
explicit new key when intentionally repeating identical text in the same run.

The recipient uses ordinary turn admission: idle starts, busy queues. Cancel
keeps the durable message visible and prevents an unsent message from starting.
Revoking either pair member removes both permissions and retires unsent work.
The final provider-launch CAS checks current pair authorization under the
same policy lock as revoke; revoked envelopes cannot become legacy records or
restart under a newly granted pair. A running delivery can finish locally;
its later explicit response still requires live reverse permission.

HTTP cancellation waits for accepted work to settle; reconnecting with the
same key cannot submit twice. Recovery reads the durable queue/admission
records and permission fence. A successful paired delivery need not produce
a nonempty final answer, and ordinary final-answer text is never forwarded.

## Desktop projection

The mode remaps only paired instruction envelope events to
`chat_conversation_message_registered`, `received`, `queued`, `started`,
`delivered`, `cancelled`, and `failed`. Every event retains existing handoff
fields and adds `conversation_mode: async_route_v1`, `conversation_id` (pair
ID), `message_id` (envelope ID), source/target session IDs and display titles.

`message_id`, `cross_chat_envelope_id`, and `handoff_id` identify the same
individual message. Existing fields include `handoff_status`, `handoff_action`,
`kind`, `queued_id`, `queue_position`, `target_run_id`, `handoff_preview`
(up to 4,096 characters), `handoff_body_chars`, `handoff_body_sha256`, and
`handoff_body_truncated`. A sender card begins at registration; an incoming
card begins at execution start (or delivered fallback after fast completion).
Queue metadata carries the explicit mode, message/pair IDs and sender title,
so a busy incoming message stays in the queue until it starts.

`GET /api/cross-chat/handoffs/{envelope_id}` still returns `{"handoff": ...}`;
the record adds the same conversation identity fields and uses its existing
`body`, `body_chars`, and `body_sha256` fields for full-text retrieval. No new
poller, subscription, or exchange fetch contract is required. Existing
unpaired rows, job references, legacy clients, and explicit legacy exchanges
retain their existing format and bounded exchange behavior.

## Isolated validation

`test_async_route_transport_isolated.py` extracts selected server functions
through AST and runs ledger effects against SQLite `:memory:`. Helper
authority and transport are mocked. Tests cover repeated messages beyond
legacy one-use counters, durable idempotency and rate charging, unavailable
mode/run/pair, cancellation, lifecycle replay, exact reverse responses,
legacy helper compatibility, successful empty completion, and native-control
metadata isolation. Pair admission/queue/revoke tests live in
`test_provider_chat_pairs_isolated.py` and
`test_provider_pair_revoke_cleanup_isolated.py`.

Run these through the guarded QA runner used for this change. It blocks
server imports, external processes/network, and production state access.
