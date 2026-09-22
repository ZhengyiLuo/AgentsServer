# AgentsServer 1.0.4-beta.6

- Fix a reconnect race that could display imported scheduled-job prompts as
  ordinary user messages while provider-history repair was still finishing.
- Keep scheduled results in their existing job cards during event catch-up.
- Preserve genuine user messages, complete event delivery and active work.
