# AgentsServer 1.0.1-beta.2

Changes since 1.0.1-beta.1.

## Independent Side chat

- Support temporary side questions and follow-up conversations for Codex and
  Claude. The desktop's Side chat uses a bounded snapshot of recent visible
  messages, plus its own separate question-and-answer history.
- Side questions do not send, steer, stop or queue a main chat turn, pause its
  goal, or write messages into its history. Each question owns its provider
  process and cancellation; hiding the desktop panel does not cancel it.
- Isolate provider tools, workspace access and inherited runtime instructions.
  Context excludes hidden reasoning, tool results and attachments. Missing
  provider isolation support fails explicitly instead of using the main agent.
- Authenticate each request, validate and bound its payload, distinguish
  changed-content retries, and cancel only the owned question on disconnect.
  No inbox polling or background UI subscription is introduced.

## Team Network

- Add indexed, permission-scoped Mail and Bulletin search, with explicit
  queries and pagination rather than per-keystroke requests or polling.
- Let agents read messages from the exact recipient selected with `@@` through
  the native Team Network tools. Keep durable route checks and recipient scope.
- Clarify sender formatting guidance so ordinary text, words and technical
  values keep their whitespace. Existing message bodies are not rewritten.

## Compatibility and availability

- Side chat and indexed search require a matching desktop build. Older clients
  keep their existing behavior; newer clients explain when a server lacks the
  necessary capability. Follow-up history is an additive capability.
- Search adds the indexed-search database migration. The side-question service
  has no persistent conversation database or transcript migration.
- Install this beta through the existing server updater on the Beta channel.
  Publishing the package does not install it or restart a running server.
- Desktop 1.0.1 local build 1170 includes the matching Side chat interface;
  installing this server alone does not add that interface to older apps.
- Uncommitted cron-history and subagent-display work is excluded from this
  package. The existing stable release is unchanged.
