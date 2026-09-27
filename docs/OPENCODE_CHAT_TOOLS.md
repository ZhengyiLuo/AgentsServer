# OpenCode chat tools

OpenCode uses a local stdio MCP bridge backed by the same private, run-bound
broker as Cursor. The server advertises OpenCode cross-chat targets only when
its cached runtime is ready. Native and custom API chats share this transport;
the chosen model must support tools.

Each helper-capable turn gets an unpredictable MCP name and a loopback bearer.
Only its exact `run` tool receives an allow rule; shell permissions, unrelated
MCP servers, native login and project configuration are not rewritten. The
default native agent remains unchanged. Plan/selected-skill restrictions retain
their existing per-turn agent boundaries. Higher-priority native managed rules
may still deny the tool; there is no shell/full-access fallback.

The broker checks live session/run/process ownership, stop/deletion state and
the server's existing capability on every call, including replay. Native startup
must establish the provider session first. Stop, completion and cancellation
close the broker. Old transcript tool names do not carry future authority.
Routes remain explicit-user-authorized and same-server; no automatic access to
unmentioned chats is introduced. Mailbox delegation and receipts use the existing
server ledger. A unique delivery capability prevents queued OpenCode work from
being confused with Cursor's headless transport.

References: [OpenCode local MCP configuration](https://opencode.ai/docs/mcp-servers/),
[mailbox behavior](CHAT_MAILBOX.md), [async routes](ASYNC_CHAT_ROUTES.md).

Regression coverage: `tests.test_opencode_provider_mcp`, existing OpenCode runner
and API contract tests, Cursor broker tests and mailbox/pair transport tests.
Provider end-to-end acceptance must include actual A → B → A receipts, an
ungranted target rejection, repeated use and stopped/busy turns; unit results
alone are not a claim of model or endpoint compatibility.
