# AgentsServer 0.1.26-beta.55

Narrow fixes on the published beta.54 source. API contract 28 and Hub schema
19 are unchanged. No new Mail polling, subscriptions, or migrations are added.

- Match imported provider messages using full normalized source fingerprints,
  not truncated display text. Long scheduled prompts no longer become duplicate
  human messages during reconciliation.
- Classify Claude's explicit `isCompactSummary` records as provider context,
  never human input. Literal user quotations remain user messages.
- Repair older proven cron/metadata imports at read boundaries without editing
  the durable ledger or provider transcript. Exact source identity, checkpoint
  bounds and structured provenance are required. Large histories use bounded
  recent windows; unproven records stay visible. Cached proof lookups perform
  no file reads and ordinary refreshes do not repeatedly scan transcripts.
- Mark source-proven repaired rows so updated clients can replace stale cached
  content without restoring bogus messages on refresh, replay, or reopening.
- Retain an existing native goal owner when an ordinary reply completes after
  Resume. One owned stream and one terminal cleanup remain responsible for the
  run; explicit Stop/Pause stays authoritative.

Local validation uses pure modules and allowlisted AST helpers, never a local
monolithic server. Focused history checks passed, including same-prefix genuine
messages, metadata quotations, duplicate source identities, large histories,
unchanged per-event I/O, original timestamps and provider phases. Read-only
production-data validation proved both reported scheduled duplicate records.
Desktop acceptance and artifact validation are recorded separately.

Release acceptance requires the full signed GitHub workflow gate. Production
updates must use normal managed idle activation; this release does not authorize
interrupting active goals or research jobs.
