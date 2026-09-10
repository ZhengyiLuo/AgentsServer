# Team Mail hint prerequisites — transport disabled

This is a store/broker foundation, not working notifications. No capability,
HTTP route, secure-peer stream, websocket, renderer listener, badge, polling,
outbox dispatcher, or agent execution is enabled. Do not package or deploy it
as an accepted notification lane. End-to-end transport acceptance is pending.

## Durable recipient cursor

Migration `0020_team_mail_arrivals.sql` adds one existence watermark per exact
Team Messages server recipient. It backfills existing ordinary mail once and
updates atomically when the creation transaction inserts a server recipient.
The message's immutable `queue_ordinal` is the sequence; its `tmsg_` ID is the
arrival ID. Feed/Bulletin, skills, and human-only copies do not advance it.
Frozen `all_servers` recipients do; idempotent replay does not create arrivals.
Receipts, unread changes, revisions, dismissals, and soft deletion do not erase
the original arrival proof. This is not an unread count.

Wire cursor v1:

```json
{"version":1,"team_id":"team_example","recipient_server_id":"node_example","through_sequence":0,"arrival_id":null,"reset":true}
```

Zero requires a null ID. A positive sequence is an exact safe integer at most
9,007,199,254,740,991 and requires `tmsg_` followed by 32 lowercase hexadecimal
characters. Team/recipient identifiers are bounded to 128 ASCII identifier
characters. Bodies, subjects, senders, recipients lists, and tokens are absent.
Hub/server realm and current authority must come from the future authenticated
transport wrapper, not from a client's cursor fields.

`HubStore.team_mail_arrival_snapshot(claims, team_id, previous_cursor=...)`
derives the exact server mailbox from current authenticated claims and reads
the durable highwater in one read transaction. A supplied foreign mailbox is
rejected. Initial snapshots set `reset`; reconnect validates the retained
sequence **and ID** against the immutable owned recipient copy, including
deleted/dismissed mail. A missing/mismatched anchor or regressed maximum resets.
This detects restored/replayed sequence reuse even if new arrivals already
advanced beyond the previous maximum. No backup/restore plumbing is changed.

Watermark reads use the `(team_id, recipient_node_id)` primary key. Anchor reads
use the message integer primary key plus an indexed exact recipient lookup.
Isolated `EXPLAIN QUERY PLAN` checks require SEARCH-only plans, with no mailbox
history/body scan. Migration backfill is one-time schema work, not a request
timer; its cost is proportional to existing recipient history.

## Commit-only broker and future ownership

Creation publishes metadata after the database transaction commits and releases
its write lock. Rollback and cached idempotent responses publish nothing. A
publication failure cannot fail committed mail: it retires the exact affected
subscriptions so a future transport cannot stay silently healthy after a lost
hint. Reconnect then reads the durable watermark.

`MailHintBroker` is process-local to one authoritative HubStore, starts no
threads, and performs no database/socket work or application callbacks. Default
bounds are 256 subscriptions and four per recipient (hard configurable ceilings
4096 and 64). Each owns one replaceable pending watermark and one last cursor;
duplicates/older commits are ignored. Only matching recipient conditions wake.
Same-sequence/conflicting IDs close that subscription; unsubscribe/invalidate/
close wake blocked readers and release capacity. No unbounded mailbox cache,
message queue, worker creation, or periodic task exists.

`subscribe_team_mail_arrivals` validates the binding, subscribes, then starts a
**fresh** authenticated read transaction for its authoritative snapshot. It does
not reuse the identity lookup's SQLite snapshot, which would lose a commit in
the bind/subscribe gap. Consumers must max-merge raced hints with the snapshot
within the same valid generation. Failed revalidation closes the subscription.

The broker is not authority. The future transport still must bind and recheck
certificate, recipient, membership, realm, connection generation, and role;
abort on revoke/expiry/role change/shutdown; close blocked sockets; serialize
writes; bound packet size/send time; and avoid shared interactive worker,
per-source, adapter, or runtime in-flight slots. Multiple independent writers
or HubStore instances do not share this in-memory broker. No cross-process
notification guarantee is provided by this prerequisite.

Supported writer topology was traced on September 10: the managed host's
`service.create_app` creates one store retained in `app.state.store`, shared by
identity with `SecurePeerHubAdapter` and the local provider helper dispatch.
Managed/standalone serve entry points acquire the runtime lease. Provider helper
subprocesses use HTTP back to AgentsServer, not independent HubStore writers.
No second V2 mail INSERT path was found. CLI recovery/proof commands and legacy
network mailbox tables are not ordinary Team Messages writers. Constructors
alone do not enforce single-owner embedding; a custom embedder is not covered.

Future passive local subscriptions must also avoid the managed host's ordinary
ASGI `_in_flight` drain, not only secure-peer request counters. A Member stream
fanout cannot assume its own retained anchor proves each desktop's older one:
use a one-time authenticated exact-anchor check per new local subscriber or a
conservative reset. Neither option requires polling or an automatic Inbox fetch.

## Fresh page coverage

The store-only `list_team_messages(..., include_mailbox_coverage=True)` option
adds `mailbox_coverage` with the same cursor shape **without** `reset`, only for
unfiltered owned server Inbox pages. Existing callers and routes are unchanged.
For `after_sequence > 0`, optional `after_arrival_id` must prove the exact owned
predecessor in the same read transaction; absent/stale proof omits coverage.
Zero requires no arrival ID. Filtered and non-Inbox pages cannot acknowledge a
global arrival prefix.

Truncated pages cover only their actual last returned message, including pages
shortened by the JSON byte limit. Complete pages cover the durable maximum,
including complete empty/deleted/dismissed catch-up. Coverage is included in
response byte accounting and does not mutate receipts or seen state. A future
desktop may advance seen only after applying a fresh user-loaded page whose
captured stream/profile/realm generation and contiguous predecessor still match;
socket receipt, cached rows, opening Mail, and unread snapshots are not proof.

## Validation boundary

`test_team_mail_hints_isolated.py` runs through the guarded QA runner, with
temporary Hub databases and no monolith import, provider, socket, subprocess,
or production state access. It covers commit/rollback/replay, frozen recipients,
human/feed/skill exclusions, cursor restoration/reuse, query plans, snapshot
races, exact ownership, bounded coalescing/recipient wakeups, subscriber cleanup,
safe integers, capped pages, missing anchors, and deleted complete coverage.
The release allowlists include the module/migration; this is not a release or
deployment instruction. Transport, reconnect/backpressure/revocation acceptance,
and the actual desktop notification journey remain unimplemented.
