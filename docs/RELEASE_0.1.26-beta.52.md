# AgentsServer 0.1.26-beta.52

Fixes-only beta based on the published `0.1.26-beta.51` release.

- Keep native Codex goal commentary, final answers, public summaries, plans,
  and tools associated with their exact provider turn and item. Retriable
  reconnect notifications no longer become terminal errors, and successful
  completion clears stale per-turn error state.
- Preserve validated Claude source provenance when importing history. Project
  interruption markers as interruption metadata, not new user prompts, and
  classify Stop/steer only when the source chain and recorded control events
  prove the cause. Keep ambiguous history unchanged.
- Repair old Claude metadata imports through bounded, read-only projection;
  saved chat events and provider transcripts are not rewritten. Metadata-only
  imports do not reorder chats as if a new user turn had arrived.
- Record a planned Claude supervisor shutdown as an interrupted run. Unexpected
  supervisor failures and earlier provider errors remain failures.
- Package and validate the two required history modules consistently in the
  archive, installer, and development deployment helper.

Source ports: Claude-history portions of `b0ad2fc5`, plus `0a9cc70f`,
`ddc4ef7d`, and `76a22ad3`. This release does not include public-chat sharing,
queue reordering, bulletin deletion, new database migrations, automatic peer
joins, mail subjects/replies, or uncommitted scheduled-job queue work.

Validation: 95 existing AST-isolated projection/shutdown, standalone
history-repair/tracker, and release-manifest checks passed on Python 3.13.
Dependency-lock, shell-syntax, Python-syntax, and whitespace checks passed.
No production server is imported, started, stopped, or reconfigured by these
checks. Release acceptance and deployment are recorded separately after
the signed release workflow and managed-update health verification.
