# Mail and Bulletin search

The connected Hub advertises the optional sibling capability
`team_message_search_v1`:

```json
{"available":true,"version":1,"fields":["subject","body","sender"],"max_query_chars":200}
```

Clients submit `q` on the existing
`GET /v1/teams/{team}/network/messages` route. Inbox, Sent and Bulletin use
`box=inbox`, `box=sent` and `box=feed` respectively. Existing owned-address,
unread, exact-sender, date and `after_sequence`/`limit` filters still apply.
The page shape and ascending sequence order are unchanged. Search covers
stored history, not just rows previously loaded by the client. It does not
fetch attachment contents or send an agent message.

## Matching and limits

- `q` is 1–200 Unicode codepoints. Blank strings, controls and invalid Unicode
  are rejected. Omit `q` to return to the normal list.
- Unicode letter/number words are safely quoted and combined with AND; each
  word is a prefix. Punctuation separates words. User quotes, `*`, `OR`,
  `NEAR`, parentheses and column syntax never become search operators.
  Punctuation-only input returns an empty page, not the unfiltered list.
- All words must match across the current subject and body together, **or**
  all words must match the sender's current display name. Terms split between
  sender and body do not match. SQLite `unicode61 remove_diacritics 2` supplies
  case/diacritic handling; this is word-prefix search, not arbitrary substring
  matching, stemming or language-specific word segmentation.
- Ordinary messages search their visible `mail_subject`, never a hidden
  legacy `title`. Skill posts search their displayed title. Only the latest
  body revision is searchable. Old revisions, filenames, attachments and
  provenance labels are excluded.
- Sender names are looked up through separate current node/principal indexes.
  Renaming an identity updates one indexed name, not every historical message.
- Results retain existing authorization, deletion and inbox-dismissal rules.
  Read-only members may search only content they could already list; searching
  does not mark mail read or alter receipts.
- Pages remain limited to 100 items and the existing response-byte ceiling.
  Indexed search additionally has a one-second SQLite execution budget. If
  exceeded, `search_too_broad` (503) asks the user for more specific words;
  the server never returns a misleading partial or empty success.

Search pages never return `mailbox_coverage`, even when requested. Clients
must also keep search results separate from full Bulletin refresh coverage:
search completion cannot clear unseen Mail/Bulletin hints. Requests should be
explicit submissions and explicit pagination, not per-keystroke or periodic
background work. Changing query or server/Hub/team/mailbox scope resets the
search cursor; a page is a current authorized view, not a cross-page snapshot
of concurrent edits or sender renames.

## Compatibility and deployment

No-`q` reads and their response shapes remain unchanged. Both the Host and a
Member's connected AgentsServer need the new query allowlist. A new Host's
capability alone cannot upgrade an old Member forwarder; its refusal should
report that both servers need compatible support, not silently drop the query
or substitute client-side loaded-page filtering.

Migration 23 creates the current-content FTS5 index, separate external-content
name indexes and a server-sender lookup index. SQLite backfills current,
nondeleted message content once within the existing atomic migration
transaction. Inserts, body revisions, deletions and identity renames update
the projections in the same transaction as their authoritative writes.
Rollback rolls the projections back as well. There is no lazy request-time
backfill, background indexer, timer or polling task.

Backfill is bounded in Python memory, **not constant-time or constant-disk**:
it reads every existing current message and stores an additional current
subject/body search projection, plus FTS index and transaction/WAL data. Large
Hubs need maintenance time and free disk proportional to their current message
content. Take and verify the normal Hub snapshot before an authorized upgrade.
Schema-23 rollback requires restoring that matching pre-upgrade snapshot;
replacing only the binary is not safe. No existing migration is rewritten.

FTS5 was already required by the legacy schema. A SQLite build without it
fails initialization rather than falling back to body scans. A missing or
unavailable required search index makes the capability `available:false` and
search returns `search_unavailable` (503); ordinary list reads remain separate.

Validation uses only temporary Hub stores, AST-extracted HTTP forwarding and
the real secure-peer sanitizer/adapter under the guarded isolated runner.
No server monolith import, live state, real peer request or deployment is
needed for these checks.
