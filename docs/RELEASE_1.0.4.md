# AgentsServer 1.0.4

AgentsServer 1.0.4 is available as the signed standalone bridge and matching npm
package. Native migration, rollback and retry pass on macOS and Linux. Existing
managed servers migrate through their updater; do not rerun fresh npm installation
over existing state.

This stable bridge pairs the managed server with AgentsDock updates. Existing
stable 1.0.3 installations migrate through their signed updater, retaining
identity, credentials, chats, Team Hub data, and installation paths. The app
requests its matching npm package automatically after the bridge is installed.

- Keep the original signed standalone download available for older updaters.
- Verify old macOS updater ownership directly when its status omits a process
  ID; retain authenticated identity, idle, native process, and installation-lock
  checks before changing services. The original signed macOS beta.29 native
  server route passes after explicit Stable selection; this does not change
  users' release channels or establish every historical desktop update path.
- Fetch authenticated legacy health when no split layout is installed, including
  after rollback leaves a dead candidate receipt. Classify that receipt under
  its private worker lock, verify the process is gone, and retain all identity
  and admission checks. Retry uses the same signed archive without deleting the
  receipt or sending credentials to its stale callback.
- Prepare subsequent updates while work continues and replace execution only
  when idle. Gateway restarts preserve agents and approvals.
- Recover interrupted activation through an independent native recovery owner.
  Verify both running components before reporting success; retain the previous
  runtime and data for rollback.

Existing users do not rerun npm installation over their server. Fresh supported
installations can use `npx @agentsdock/server@1.0.4 install`.

The accepted native tests preserve populated chats/events, synthetic provider and
terminal credential/configuration files, Hub authority/messages and existing
mutual-TLS peers, including new authenticated reads/writes after rollback and
retry. The exact npm archive on `latest` and signed standalone bridge were
published and verified before exposing the paired stable desktop update.
