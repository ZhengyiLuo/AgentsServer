# AgentsServer 1.0.4-beta.4

- Save a custom Codex endpoint with its URL and separate key, independently of
  optional connection tests and running chats.
- Discover endpoint models and select the model and reasoning effort per chat.
  Existing chats retain their original endpoint and credentials after edits.
- Keep normal Codex and each custom credential generation in separate native
  managers, including chat controls, forks, subagents, and idle-update checks.
- Remove broad cross-chat prompt restrictions. Messaging uses the documented
  helper tools and remains authorized by the server harness.
- Keep shared Codex account status read-only and reject the legacy API-key
  login route. Requires desktop 1.0.4-beta.3 for the new model controls.
