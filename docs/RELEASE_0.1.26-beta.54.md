# AgentsServer 0.1.26-beta.54

Matching desktop beta release, based on the validated `8d7b025` candidate.
The current published `main` ancestry (`2089d3cf`) is retained by a tree-empty
merge. This is a feature release beyond published beta.53, not a fixes-only port.

- Preserve native Codex goals during supported follow-up steering; reject
  unsafe, older-client, idle-owner, and pre-binding Force Send before dequeue
  without pausing or automatically resuming the goal.
- Preserve proven runtime-only goal history metadata and validated imported
  commentary/final phases and original content timestamps. Explicit Stop/Pause
  remains authoritative; ambiguous historical user text is retained.
- Include durable asynchronous chat pairs, scoped Mail subjects/replies and
  read/unread state, automatic completion of explicitly approved peer joins,
  poster-owned skill bulletin deletion, queue ordering, and private-by-default
  public chat snapshots.
- Drain direct Host dispatch/mail/upload writes before maintenance, and avoid
  migration writer locks for verified current-schema Hub reads.

Hub schema advances from published beta.53's 16 to 19 (migrations 17–19).
The disabled Mail-hint broker/migration 20 and unrelated uncommitted scheduled
job queue work are deliberately excluded. API contract remains 28.

The beta.53 paging fixture correction is carried forward using the current
combined history-preparation helper. GitHub release creation pins new tags to
the exact tested workflow checkout (`GITHUB_SHA`), not mutable `main`.

Local validation: 192 guarded regression tests passed against this clean
candidate, plus the exact paging fixture executed with AST-extracted
`get_session` and isolated boundary stubs. All 174 Python source/test files
compiled without imports; shell syntax and whitespace checks passed. No
monolithic server, live provider, production state, or listener was started.

The initial full gate (run `34501473187`) found two paired-grant rollback
regressions and seven stale contract fixtures; publication did not run. Pair
journals now retain detached revision-CAS snapshots, and rollback retries
durable persistence without restoring unaccepted authority. Follow-up checks
passed 104 guarded regressions (including repeated cancellation), the four
exact legacy cross-chat cases via AST extraction, and 36 focused Mail/CLI/schema
checks. Fixtures assert the reciprocal pair proof, supported Mail title and
projection behavior, bounded expiry scan, and exact schema 19. A fresh full
workflow gate is still required before publication.

Local validation and release acceptance are separate. The signed release
workflow must pass its full gate before publication. Deploy with the managed
beta updater's `when_idle` reservation and exact server identity; never use a
forced restart to interrupt active goals/jobs. Record signed artifact checks
and post-update health separately after release and deployment.
