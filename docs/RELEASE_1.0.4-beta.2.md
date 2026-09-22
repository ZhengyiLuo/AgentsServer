# AgentsServer 1.0.4-beta.2

Fixes the API-key login control introduced in 1.0.4-beta.1, which could replace
the shared credentials used by ordinary Codex chats and the Codex CLI.

- Reject the legacy API-key login route before reading a credential or opening
  the native manager. Older desktop clients receive instructions to configure
  a custom endpoint instead. Account status remains read-only.
- Keep endpoint URL, model and key configuration in the separate custom
  provider store. Test, save and removal do not sign in to the normal account.
- Pair with AgentsDock 1.0.4-beta.2, which removes the shared-login action and
  makes custom endpoint selection instructions visible in Settings.

This update prevents another overwrite; it cannot reconstruct credentials
already replaced by beta.1. If affected, restore normal Codex authentication
using Codex's own sign-in flow. An existing native process may retain its old
account until it restarts. The managed when-idle update recreates that process
after active work finishes.

API contract 28 and the custom provider contract are unchanged. Use the signed
Beta updater with when-idle installation. Custom-endpoint chats still require
a supporting server and must not be resumed on 1.0.3.
