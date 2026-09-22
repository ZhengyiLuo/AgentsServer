# AgentsServer 1.0.4

Candidate release notes. Exact stable migration acceptance and publication are
pending.

This stable bridge pairs the managed server with AgentsDock updates. Existing
stable 1.0.3 installations migrate through their signed updater, retaining
identity, credentials, chats, Team Hub data, and installation paths. The app
requests its matching npm package automatically after the bridge is installed.

- Keep the original signed standalone download available for older updaters.
- Verify old macOS updater ownership directly when its status omits a process
  ID; retain authenticated identity, idle, native process, and installation-lock
  checks before changing services.
- Prepare subsequent updates while work continues and replace execution only
  when idle. Gateway restarts preserve agents and approvals.
- Recover interrupted activation through an independent native recovery owner.
  Verify both running components before reporting success; retain the previous
  runtime and data for rollback.

Existing users do not rerun npm installation over their server. Fresh supported
installations can use `npx @agentsdock/server@1.0.4 install` after publication.
