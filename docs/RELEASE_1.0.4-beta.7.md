# AgentsServer 1.0.4-beta.7

- Add optional basic compatibility checks for a saved custom endpoint model,
  covering native tool calls and a follow-up in the same thread.
- Filter known non-chat models from discovery while retaining unfamiliar IDs
  as unverified choices.
- Use explicitly advertised reasoning capabilities and clear inherited effort
  settings for custom models that do not support them.
- Report unusable custom model responses clearly without repeatedly rolling
  over the conversation. Preserve ordinary Codex account settings.

Use with AgentsDock 1.0.4-beta.7 for the new model controls. Basic compatibility
checks do not certify every tool, integration or reasoning setting.
