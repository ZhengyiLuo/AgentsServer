# Cursor run-bound provider tools

Cursor helper-enabled turns keep the native print/stream-json transport,
working directory, login, and original `--resume` ID. They load one private
MCP plugin for the current run instead of executing chat helpers through Shell.
No desktop change is required for this transport.

## Permissions and lifecycle

The internal server is `plugin-agentsdock-internal-9f3a2c71-provider` and its
only tool is `run`, with the existing `helper`, `arguments`, and optional
`stdin` schema. It uses the same capability resolver as Codex and Claude.
The server, chat, live run, owner token, process, and capability are checked
before executing a helper or returning a cached result. The provider cannot
supply those identities in tool arguments. No new chat routes are granted.

A private temporary `CURSOR_CONFIG_DIR` carries a snapshot of the native
configuration, adding only `Mcp(plugin-agentsdock-internal-9f3a2c71-provider:run)`.
Native sibling settings remain linked to their original locations; HOME,
native credential stores and Cursor data directories remain unchanged. Global
and project files are not rewritten. Project allow lists retain native
replacement semantics; explicit denies from every applicable layer are
retained conservatively, even if a deeper layer has an empty deny list.

Project permissions are resolved from the Git root to the working directory
and included in the snapshot. `--disable-project-configs` prevents reapplying
those files over the exact tool addition. Malformed or unsupported permission
configuration fails closed. Neither `--force` nor `--approve-mcps` is added by
this integration; a user's existing Full Access choice still has its normal
meaning. Explicit native tool denies and disabled MCP servers remain effective.

The plugin's 0600 configuration, inside a 0700 directory, carries only a
short-lived loopback IPC lease. The actual helper authority stays in the
server. IPC is bounded and authenticated; Stop, process exit, superseding
runs, capability revocation and server exit invalidate execution. Normal
cleanup closes the endpoint, cancels in-flight callbacks, and removes the
private configuration. The existing Cursor process guard owns child cleanup.

## Delivery and retry semantics

- An accepted receipt with `message_id` means durable delivery, not a read or
  a reply. Unread mail stays unread when native permissions block its reader.
- A rejected tool call reports the native permission failure. It must not be
  represented as a successful read or a failed send if delivery was accepted.
- Resume reading after resolving the blocker. Do not send another message
  merely because no reply has arrived.
- Retry an uncertain send with the same helper idempotency key. Repeated MCP
  request IDs also use the existing run-bound replay cache; that cache never
  substitutes for live authorization.

## Compatibility and verification

Native verification used Cursor CLI `2026.09.26-dd393fe` on macOS. It retained
the same print-mode session and context across fresh private profiles, used
the injected MCP under Default permissions, rejected an unrelated Shell
command, respected a project `Mcp(*:run)` deny, and left the original global
configuration byte-for-byte unchanged. These transport probes used a
synthetic inbox, not production chat messages.

Cursor ACP was investigated but is not used: this CLI stores ACP sessions
separately from print-mode sessions, so switching an existing chat to ACP
would not preserve its native continuation. There is no hidden transcript
copy, session migration, ACP fallback, or shell-permission workaround.

Required native flags are `--plugin-dir` and `--disable-project-configs`.
An incompatible CLI must be updated; do not bypass permissions to compensate.
Windows-native acceptance is not covered by these macOS tests.

Server tests cover native runner → stdio MCP → broker → live authorization,
request replay, expired/wrong-owner rejection, endpoint revocation, cleanup,
permission preservation, and the existing durable mailbox contracts. The
runner subprocess in that integration test is a fixture; the separate native
Cursor probes cover the real CLI permission and continuation behavior.

**Local manual acceptance:** the patched test server was restarted normally,
preserving server identity and existing native chat associations. The user
subsequently reported that local App testing looked good and authorized main
integration. Individual checklist outcomes were not separately recorded; the
agent's independent native probes above are not full App round-trip evidence.

The repeatable App checklist remains: Codex → Cursor → Codex and the reverse
direction with Cursor on Default permissions, then reopen the App and repeat.
Verify one stored message per send, denied unauthorized/stale access, and
unchanged ordinary Shell permissions. Source integration and local manual
acceptance do not establish production-release acceptance or Windows support.
