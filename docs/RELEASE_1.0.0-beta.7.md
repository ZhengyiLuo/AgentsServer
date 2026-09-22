# AgentsServer 1.0.0-beta.7

## Completed subagent names

Correct stale Codex subagent names when reopening a chat. Previously, the
server could discover the native nickname or title of a completed agent but
omit that correction from the client snapshot, leaving an old task path visible.

Known completed agents now receive a durable identity correction through the
existing event stream. Their completed status, original lifecycle timestamp,
run ownership and activity history remain unchanged. Reopening the chat again
does not append the same correction, and an intervening live transition is
preserved. Previously unknown historical children are not turned into new
timeline activity.

This is a server-side correction. A desktop that already supports native
subagent names can display it without another app installation.

## Included history correction

Includes the source-proven Claude history replay correction merged after
beta.6: decorated replies are matched to their native message identity, while
distinct messages and ambiguous records remain visible. Provider transcripts
are not rewritten.

## Compatibility and rollout

API contract 28, Team Hub schema 22, dependencies and the release signing key
are unchanged. No new background polling, provider requests or wake loops are
introduced. The unpublished stable candidate tag remains unchanged; this is
a beta release, not a stable promotion.

Release acceptance requires the complete server workflow and verification of
the downloaded manifest signature, archive digest and exact committed source.
Publication does not install or restart a live server. Use the managed updater
when idle unless the operator explicitly authorizes interruption.
