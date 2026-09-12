# AgentsServer 0.1.26-beta.62

- Remove the default ten-active-chat limit. Normal chat sends, queued turns,
  scheduled jobs and goal resumes no longer stop admitting work solely because
  ten other chats are active.
- Keep low-memory, same-chat ownership and managed-update admission protection.
  An operator's explicit positive `AGENTSDOCK_MAX_ACTIVE_AGENT_RUNS` override
  (or its legacy `ZENITHBOT_` equivalent) is still respected; the default is `0`.

This is a server-only fix; no desktop update is required. API contract remains
28. Existing active work is not interrupted by publishing the release; the
managed updater activates it when idle.
