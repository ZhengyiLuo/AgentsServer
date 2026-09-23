# AgentsServer 1.0.7-beta.2

- Add native Claude Goal controls and goal-state updates for the desktop beta.
  Claude owns the completion evaluator and continuation behavior.
- Interrupt the exact active Claude run before clearing its goal, including
  when a tool is running. A missing clear receipt cannot permanently block
  later messages.
- Preserve queued messages while Stop is pending instead of falsely promoting
  them to Starting. Bound the Claude Send now stop operation.
- Preserve native interruption provenance across parallel tool-result branches.
- Exclude source-proven Codex compaction handoffs from ordinary assistant
  messages and repair affected imported history.

Use with AgentsDock 1.0.7-beta.2 and a Claude Code version providing `/goal`.
