# Development rules

- Match each provider's native user-facing behavior: Codex chats must match
  Codex, and Claude chats must match Claude. Preserve the thinking text and
  activity each provider exposes, including live updates, chronological order,
  and retention after completion or interruption.
- Verify provider parity separately against the corresponding native runtime
  and client. Do not infer Claude parity from Codex tests, treat headings as
  proof of complete content, or claim parity from visibility changes alone.
