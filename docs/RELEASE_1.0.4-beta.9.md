# AgentsServer 1.0.4-beta.9

- Preserve plaintext reasoning explicitly supplied by Codex, separately from
  its summaries, and stream both channels to compatible desktop clients.
  Retain received text when a turn stops before item completion. Encrypted
  reasoning is not read or decoded.
- Add optional per-chat sub-agent limits for native Codex and Claude runtimes.
  Keep credentials, global provider configuration, and other chats unchanged.
  Saves do not interrupt current work.
- Preserve inherited configuration when clearing a chat override and report
  the provider boundary at which each change applies. Report pending native
  Codex default resets without restarting a shared provider process.
- Use Claude's native concurrency setting on supported versions, preserving
  its exceptions and protecting active background agents during reloads.

Use with AgentsDock 1.0.4-beta.9 for the reasoning display switch and per-chat
limit controls. Providers determine which reasoning text they expose.
