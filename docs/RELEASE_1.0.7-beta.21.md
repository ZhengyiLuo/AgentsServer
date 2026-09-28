# AgentsServer 1.0.7-beta.21

This beta packages the recent server fixes for Codex endpoint switching,
startup queue recovery, and inter-chat messages.

- Apply a saved normal/custom Codex selection when the temporary runtime work
  delaying it finishes. Preserve the existing conversation and goal, and release
  the old native writer before another Codex process resumes that conversation.
- Recover chat queues independently at startup. Opening one chat no longer waits
  for every other transcript to be scanned. Persist queue checkpoints so later
  restarts read only new events, while retaining message order and Stop pauses.
- Use the asynchronous mailbox for same-server inter-chat delivery, including
  older paired Send and Ask callers. Disable legacy request/reply execution and
  automatic final-reply turns. Keep existing conversations and mailbox access.
- Hide duplicate internal status messages only when stored provider receipts
  prove their origin; preserve genuine user messages with identical text.
- Include the intervening goal-continuation, provider-context, attachment,
  reasoning-stream and subagent fixes from the local beta builds.

This is a server-only beta; the existing desktop beta can use these fixes
without a new app build. The server updater installs it when the agent worker
is idle. Forcing installation restarts that worker and interrupts its active
agent turns; it does not migrate running agents between worker versions.

Existing installation locations, credentials, release signing, and update
channels are retained. This GitHub server release does not by itself publish
an npm package.
