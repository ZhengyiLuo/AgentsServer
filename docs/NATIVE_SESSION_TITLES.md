# Automatic session titles

Unnamed new chats immediately show their first prompt line. Existing native
provider titles are adopted when available. If Cursor or Codex still has no
native title after a successful ordinary user turn, AgentsServer makes one
independent background request for a short summarized title.

This is server-side metadata: no extra user/assistant bubbles, no changes to
the main provider conversation, and no client changes are needed. Manual
renames always win. Old chats without explicit title-ownership metadata,
imported chats, forks, and child agents are not retroactively renamed.

## Background generation: Cursor and Codex

- Uses the chat's provider/model and existing login. Custom Codex provider
  bindings remain isolated; no separate title API key is required.
- One optional attempt per eligible chat, claimed durably before provider usage.
  It consumes additional provider usage. Failures/timeouts keep the fallback;
  automatic retries do not accumulate usage.
- Sends only the first 1,600 characters of the initial user prompt and 800 of
  the successful reply, quoted as data. No attachments, tool output, or full
  transcript is sent. Output must be a single-line title of at most 72 characters.
- At most two requests run concurrently, with a queue cap of 16. A request has
  a 45-second deadline, followed by bounded cleanup of its own processes.
- Manual rename, opt-out, archive, deletion, and shutdown cancel pending work.
  Provider/model/identity/ownership checks also reject stale results.
- A global `AGENTSDOCK_AUTO_TITLES=0` environment setting disables additional
  title requests. Creation or PATCH can set `auto_title_enabled: false` for one
  chat. No settings toggle has been added to the clients. Native metadata
  synchronization does not require or consume an additional model request.

### Isolation

**Codex:** a fresh ephemeral app-server thread, never a resume or fork. Every
title turn has `environments: []`; integrations, hooks, shell, browsing, skills,
and agents are disabled. An independent client declines tool/approval requests.
The generated native schema must advertise the isolation fields, and the
provider must confirm an ephemeral thread with no history path before the
request is made. Older/unsupported runtimes keep the fallback.

**Cursor:** a fresh Ask-mode CLI request in disposable HOME/config/data/workspace
directories. Ask mode alone is not tool-free: deny rules plus fail-closed
`preToolUse` hooks block tools, with additional read/shell/MCP/subagent hooks.
No main-chat resume, shell auto-approval, or MCP approval is used. Inherited
hooks, MCP settings, and AgentsDock run credentials are not forwarded. Only the
selected model and supported login/transport inputs are reused. Machine-managed
hooks cause optional naming to be skipped rather than bypassing policy.

Cursor currently requires CLI **2026.09.18 or newer**, the first tested build
with these enforcement semantics. With macOS native login, the exact Cursor
access/refresh pair is privately read from Keychain into the temporary profile;
file-based login and `CURSOR_API_KEY` are also supported. An expired/unknown
access-token format skips optional naming; it is not used to repair or refresh
the user's login. Credentials are never logged, and the disposable profile is
removed after success, error, timeout, or cancellation.

### When the new title appears

Generation starts after the main reply finishes, without delaying that reply.
Desktop's existing session-list polling picks up the saved name. Mobile may
take up to its existing 60-second foreground refresh interval, or a manual
list refresh/reopen. Web shares get a metadata invalidation signal. Immediate
mobile push of title-only updates would be a separate client improvement.

## Native metadata synchronization

| Provider | Existing title source |
| --- | --- |
| Claude | Bounded exact-session transcript head/tail reads of `custom-title` / `ai-title` records. No additional generation request. SDK sessions may not produce these records. |
| Codex | App-server start/resume/read names, `thread/name/updated`, and existing `session_index.jsonl`. Custom providers use their own manager cache. Native names take precedence over generated fallback names. |
| Cursor | Stream-json exposes no supported title event. Private serialized history is not decoded; the independent request above supplies summarized titles. |
| OpenCode | Fixture-tested read-only exact-ID SQLite metadata reader only. This beta.9 runtime does not expose OpenCode as a provider. |

Native metadata is checked before normal terminal events and when idle history
is reopened. Child-name notifications continue updating child identities, never
the parent title. Unknown or manually owned titles are never inferred from their
wording. Persistence failures roll back title fields without losing unrelated
live metadata.

## Verification

The change targets the `release/1.0` runtime. Existing provider, side-question,
and child-continuation behavior is retained. Development validation used a
local build based on `v1.0.4-beta.9`; this feature does not publish a release.

Local installation was verified with authenticated health, exact installed
source hashes, unchanged token/server identity/state directory, and all prior
sessions retained. The previous runtime remains available for rollback.

Synthetic tests cover bounded input/output, isolated request parameters, cleanup,
eligibility, durable once-only claims, concurrency, opt-out, manual-name races,
provider/model changes, shutdown, native readers, and child-name projection.
Tests import the server only under a temporary synthetic home/state.
The 496-test regression run passed; the final 72-test naming/packaging run also
passed after adding three more lifecycle/default-model checks (499 distinct
tests across the overlapping runs). Python/shell syntax and diff checks passed.
This is focused validation, not a claim that the full repository suite is green.

Live adapter checks on 2026-09-20 used synthetic song prompts and existing local
provider logins. Both Codex and Cursor returned summarized titles. A separate
Cursor negative test requested a synthetic file read: the tool result reported
the deny-hook error, the canary content was not disclosed, and the file remained
unchanged. After the local server installation, the user also confirmed that
the new naming behavior worked in the client.

References: [Codex app-server](https://developers.openai.com/codex/app-server/),
[Cursor CLI parameters](https://cursor.com/docs/cli/reference/parameters),
[Cursor permissions](https://cursor.com/docs/cli/reference/permissions),
[Cursor hooks](https://cursor.com/docs/hooks).
