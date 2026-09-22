# AgentsServer 1.0.4-beta.3

Fixes Custom endpoint connection tests timing out before contacting the
provider on installations with existing Codex history. The test previously
created a fresh database while retaining the ordinary Codex history root,
which could trigger a full history rebuild during native startup.

- Run each connection test in a temporary Codex home, with its own history,
  database, logs and ephemeral authentication. Remove it after the owned
  native process exits.
- Keep the entered endpoint, model and dedicated key confined to that test.
  Ordinary Codex authentication, configuration and conversations are unchanged.
- Keep the legacy shared API-key login route blocked.

Compatible with AgentsDock 1.0.4-beta.2; no replacement desktop build is
required. API contract 28 and the custom provider contract are unchanged.
Install through the signed Beta updater when active work is idle, then retry
**Test connection** in Settings.
