# AgentsServer 1.0.7-beta.1

- Add native Claude Goal controls and goal-state updates for the desktop beta.
  Claude owns the completion evaluator and continuation behavior.
- Interrupt the exact active Claude run before clearing its goal, including
  when a tool is running. A missing clear receipt cannot permanently block
  later messages.
- Preserve queued messages while Stop is pending instead of falsely promoting
  them to Starting. Bound the Claude Send now stop operation.
- Preserve native interruption provenance across parallel tool-result branches
  and repair source-proven history display issues.

Use with AgentsDock 1.0.7-beta.1 and a Claude Code version providing `/goal`.
