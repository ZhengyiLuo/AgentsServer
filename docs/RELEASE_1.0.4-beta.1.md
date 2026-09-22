# AgentsServer 1.0.4-beta.1

Beta update from **1.0.3**, paired with the new desktop beta's native Codex
account and custom-endpoint controls. Team Network remains a beta feature.

## Native Codex account and custom endpoints

- Add operator-only native Codex API-key sign-in, account status and explicit
  recheck support. Keep the installed Codex agent runtime and native account
  storage; this is not a replacement model-API agent implementation.
- Configure a custom Responses endpoint, exact model ID and separate provider
  key. Test explicitly through native Codex before saving. Tests use an
  isolated temporary conversation, make no tool calls and do not change the
  saved endpoint or normal account. Connection errors fail promptly with
  secret-free messages rather than hiding behind repeated retries.
- Select the custom endpoint per chat. Ordinary Codex retains its existing
  configuration and sign-in, and both kinds of chat can run concurrently.
  Preserve the choice across reloads, forks and side conversations. Started
  conversations remain bound to their original endpoint and model; changing
  or removing settings cannot silently send their history elsewhere.
- Preserve active work during account/configuration changes. Protect provider
  credentials from tool environments without replacing the operator's existing
  shell exclusions. Do not put keys into histories, command arguments or API
  responses. Custom credentials are stored in private files owned by the
  server user; they are not encrypted at rest.

## Native-context Side chat

- Replace recent-visible-text snapshots with the provider's native context,
  including tool results: ephemeral Codex forks and Claude's native side
  question control used by `/btw`.
- Keep side follow-ups separate from the main conversation. Clear/cancel
  closes only the selected side request; the parent keeps working. Reject
  stale provider identities and unsupported runtimes instead of silently
  falling back to a less complete copied transcript.

## Compatibility and rollout

- Requires desktop **1.0.4-beta.1 (build 1175)** for the new picker, Settings and
  native side-chat controls. Older desktop Side chat requests receive HTTP 409;
  update the app to use the v2 native-context contract even though the overall
  API contract remains 28. Existing ordinary chats retain their provider selection.
  Custom gateways must support the Responses protocol required by Codex;
  Chat Completions compatibility alone is not enough.
- API contract **28**, dependencies, Team Hub schema and signing key are
  unchanged from 1.0.3. No background inbox polling or per-keystroke requests
  are added by these changes.
- Install through the signed **Beta** managed updater. When-idle installation
  preserves active work and the existing Team Hub rollback checks. Publishing
  does not itself install, restart servers or opt stable users into the beta.
- After creating custom-endpoint chats, do not resume them on a downgraded
  1.0.3 server: that version does not understand their provider selection or
  endpoint bindings. Team Hub rollback checks do not protect this custom-chat
  compatibility boundary; restore a supporting server before resuming them.

Validation includes isolated transport/lifecycle regressions, native desktop
interaction through real HTTP and production session handlers, concurrent
normal/custom native Codex conversations against controlled endpoints, and a
separate authorized real-gateway connection probe. These checks do not certify
every third-party gateway, tool or billing feature. Signed publication is gated
by the complete release CI suite.
