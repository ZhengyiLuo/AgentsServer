# Scoped external mailbox peers

This optional contract connects an authenticated local gateway to exactly one
existing chat. It creates neither a session nor a provider run for the external
agent. It is disabled until the server owner explicitly provisions policy.

The public gateway owns OAuth, user consent, and its own revocation switch.
AgentsServer sees a separate high-entropy gateway credential, restricted to two
loopback endpoints. Never place the server's client/admin token or a run-bound
provider capability in the gateway. Forwarded/proxy headers, browser-origin
requests, cookies, duplicate credentials, and non-loopback sockets are rejected.
Funnel must terminate at the gateway, never at these AgentsServer endpoints.

## Wire contract

Send `Authorization: Bearer <scoped gateway secret>` directly over loopback.

`POST /api/external-chat/messages` accepts only:

```json
{"text":"Request text","idempotency_key":"new-stable-request-key"}
```

Text is at most 16,000 characters / 64 KiB of UTF-8; keys are printable ASCII,
8–128 characters. Unknown JSON fields are rejected. An accepted response has
`ok`, `accepted`, `message_id`, `duplicate`, and `state`. Retry an ambiguous
response with the same key and text. A changed body with that key returns 409.
Acceptance is durable delivery, not proof of recipient execution or a reply.

`GET /api/external-chat/replies?since=<cursor>&limit=25` returns:

```json
{"messages":[{"message_id":"…","text":"Reply text","created_at":"…","reply_to":"…"}],"next_cursor":"…","has_more":false}
```

Replies are oldest first. Omit `since` for the first read; persist `next_cursor`
after processing a successful page, including when `has_more` is false. Reads
are nondestructive and replayable. Cursor signatures bind peer, policy revision,
pair and sequence. A later message appears after the previously returned tail
cursor. No caller can select another session or peer. Authentication errors are
401, unavailable targets / conflicting sends 409, malformed cursors 400. All
external endpoint responses use `Cache-Control: no-store`.

## Explicit owner provisioning

Provisioning is an offline administrative action, not an MCP tool. No runtime
API accepts peer policy. The owner-private `STATE_DIR/external-chat-peers.json`
must be a regular nonsymlink file owned by the server user with mode 0600.
The enclosing state directory must remain owner-private. Write replacements
atomically. Missing, malformed, duplicate-identity or unsafe files grant nothing.

The schema is `{"version":1,"peers":[…]}`, with up to eight entries. Every entry
requires these exact fields:

| Field | Value |
|---|---|
| `peer_id` | `extpeer_` followed by 32 random lowercase hex characters |
| `display_name` | Short display label, e.g. `Duet`; never authorization |
| `target_session_id` | The single existing receiving chat ID |
| `credential_sha256` | Lowercase SHA-256 of a new ≥256-bit random gateway secret |
| `revision` | `rev_` followed by 32 fresh lowercase hex characters |
| `pair_id` | `pair_` followed by 32 fresh lowercase hex characters |
| `route_id` | `route_` followed by 32 fresh lowercase hex characters; chat → external |
| `inbound_route_id` | A different fresh route ID; external → chat |
| `enabled` | Boolean; set false to immediately deny new sends/reads/replies |
| `policy` | The explicit approval record described below |

`policy` has exactly `approval_id`, `approved_at`, `delegated_by`, and
`source_user_instruction`. Preserve the owner's actual approval instruction,
including scope and confirmation limits, in `source_user_instruction`. Record
its verifiable approval reference and timestamp. Do not substitute an agent's
specification, invent an approval, or copy incoming tool text into that field.
No production approval or gateway credential ships with the implementation.

Policy and route identity are immutable for a permission pair. To change scope
or regrant after revocation, replace **revision, pair ID and both route IDs**.
For credential rotation without changing scope, change the secret/hash and
revision; existing cursors become invalid. Deleting the native chat route also
revokes that exact route ID. Only a new explicitly provisioned pair can regrant it.

## Provenance, replies and wake behavior

Incoming messages store an `extadmission_…` identity and immutable version-2
`user_delegation` beside the envelope in the same SQLite transaction. There is
no invented `source_run_id`, no local user turn, and no permission minted from
the request body. The read projection identifies `origin_kind: external_peer`
and includes the approved policy separately from the external agent's request.
Editing a message removes its attestation, as for local delegated messages.

The receiving chat discovers Duet through its normal Chats inbox and replies
with `chats send --route <issued route> --reply-to <message_id>`. Its normal
live-run capability, route revision, current peer policy, and exact reply-parent
checks still apply. Other chats do not receive this route. Existing user-approved
routes can be used within the delegated task; the external connection creates
no additional route, file, job or deployment permission.

Incoming mail uses ordinary coalesced idle mailbox admission. Busy work is not
interrupted, Stop fences remain effective, and paused goals are not resumed.
Replies remain in the external mailbox and never start an external agent.

## Verification

Run `PYTHONPATH=server:server/tests python3 -m unittest
test_external_chat_peers_isolated test_chat_mailbox
test_chat_mailbox_runtime_isolated test_chat_mailbox_lifecycle_isolated
test_async_route_transport_isolated test_provider_chat_pairs_isolated`.

The isolated suite uses synthetic owner policy, actual extracted handlers and
route resolvers, and a temporary real SQLite ledger. Provider authorization and
launch are controlled fixtures.

From `server/`, `python3 -m unittest tests.test_external_chat_http_integration`
also exercises the complete FastAPI application over disposable loopback TCP,
native capability issuance, mailbox replies and revocation. It covers scheduled
dispatch through SDK MCP registration, the helper subprocess and the Jobs API.
Host-wide lifespan services and model execution remain off; the live-run owner
is a synthetic fixture. This does not prove live Google/Duet login or a native
model turn. Exercise those remaining boundaries before production acceptance.
