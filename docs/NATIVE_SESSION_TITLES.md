# Automatic session titles

Unnamed new chats immediately show their first prompt line. Existing native
provider titles are adopted when available. If Cursor or Codex still has no
native title after a successful ordinary user turn, AgentsServer makes one
independent background request for a short summarized title.

This is server-side metadata: no extra user/assistant bubbles, no changes to
the main provider conversation, and no client changes are needed. Manual
renames always win. Old chats without explicit title-ownership metadata,
explicitly named imports, forks, and child agents are not retroactively renamed.

## Resume and import labels

- Claude import candidates prefer the last exact-session `custom-title`, then
  the last `ai-title`, then the first-user-message fallback. Metadata
  from another session or a sidechain is never used as the parent title.
- Codex import candidates prefer a valid `session_index.jsonl` `thread_name`;
  missing, malformed, oversized, or placeholder names use the first user message.
- Preview labels for Claude, Codex and Cursor do not repeat the project name;
  workspace information stays in `cwd`. If no title or user text is readable,
  use a provider/short-ID label. Listing never generates a new title with a model.
  Preview-only follow-up: 201 focused tests passed, including first-message
  selection, display sanitization, separate workspace metadata and distinct
  sessions with identical previews. The updated test server's authenticated list
  was checked against native titles and first messages after restart.
- Legacy AgentsDock-wrapped inputs are not display titles: preview extraction
  handles old Codex context without a jobs snapshot and Cursor's complete
  instruction/current-prompt/tool-binding envelopes. Known human quotations and
  incomplete wrappers remain intact. This is display-only; existing transcripts,
  imported timeline events and reconciliation fingerprints are unchanged.
- Resume by session ID reads existing titles for Claude, Codex, and Cursor before
  returning the new chat. Explicit custom names win. For compatibility with
  existing clients, the exact generated `Resumed <Provider> <first 8 ID chars>`
  label is treated as a placeholder **only at creation**. A later manual rename,
  including that same text, always remains manual.
- These are bounded local metadata reads, not title-generation requests. Busy,
  missing, or changed stores do not prevent resume. Discovery does not modify
  provider conversations, rename existing AgentsDock chats, or spend model usage.

Cursor additionally supports opt-in **CLI local discovery and bulk import of an
initial public-text snapshot**. Existing clients still receive Claude/Codex only;
compatible clients request `include_cursor=true` after checking the separate
capability. Native resume retains the original ID/workspace. This is not Cursor
IDE/cloud import or automatic external-history synchronization. See the
[Cursor import contract](CURSOR_LOCAL_IMPORT.md) for requirements and limits.

### Import discovery safeguards on the 1.0 release line

The title branch is based on `release/1.0` (`c2caa7f`), which does not contain
main's earlier #108/#112 import safeguards. Those safeguards are now explicitly
backported alongside naming rather than inferred from the release version:

- Prune Claude `subagents/` directories and confirmed legacy sidechain message
  records. Copied child title metadata or a different session's records do not
  hide a main conversation.
- Exclude Codex child-source/parent metadata and `archived_sessions/`, including
  broad/custom scan roots. Ordinary user forks and stopped main chats remain.
- Exclude provider identities used by the current or other installed same-user
  local instances **before** applying the response limit. Parked provider IDs,
  stopped instances and archived AgentsDock chats still count as owned.
- Recheck cross-instance ownership for bulk import and manual resume, including
  `import_history: false`. A shared nonblocking lock serializes cooperating
  imports; unreadable/unsafe ownership indexes fail closed with a retryable error.
  Fresh chats without an existing provider ID do not need that scan or lock.

`local_session_ownership.py` contains the compatible registry/index reader and
import lock extracted from main's `server_instances.py`, without backporting
service-install/remove commands. It is included in all runtime packaging lists.
No service, registry or other instance's history is modified during discovery.
Older servers do not participate in the import lock until upgraded; this is not
a global provider lock or a cross-machine ownership service.

Discovery still has its existing scan/response limits; this is not pagination
or a fixed 50-chat cap. Distinct main sessions with identical labels remain
distinct. Deleting an AgentsDock entry does not delete its native transcript or
create a permanent import-hide tombstone. A missing native title still falls
back to the first user message; discovery does not generate names with a model.

Verification: 104 focused tests passed, including a real HTTP picker fixture
with 510 children, native titles, archives and cross-instance ownership together,
stale-picker/manual-resume rejection, read-only checks, cancellation, corrupt
indexes, and packaging. The wider 427-test run passed 426 tests; the existing
Cursor hung-process test exceeded its two-second wall-clock assertion. That
same failure reproduced on the unchanged `7d44880` title-branch baseline (2.49s),
so it is recorded separately, not hidden by relaxing the assertion. Syntax and
diff checks passed. No full-suite or client UI success is claimed.

### Cursor metadata compatibility

The CLI 2026.09.18 local format was verified read-only: configuration directory
`chats/<md5(absolute working directory)>/<session ID>/store.db`, `meta` key `0`,
hex-encoded UTF-8 JSON containing `agentId` and `name`. Configuration root
precedence matches the CLI: `CURSOR_CONFIG_DIR`, `XDG_CONFIG_HOME/cursor`, then
`~/.cursor`. `CURSOR_DATA_DIR` is not the chat metadata root in this version.

The reader checks the exact workspace and ID, excludes subagent metadata,
rejects symlinked chat paths and unknown schemas, bounds the metadata row and SQL
work, and opens SQLite read-only. It never reads/decrypts conversation blobs or
returns other metadata. This is a best-effort private-format adapter, not a
guarantee for all future Cursor versions or the separate Cursor IDE history.

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
| Cursor | Exact-workspace, exact-ID read-only CLI naming metadata (see above). Stream-json exposes no supported title event. Conversation blobs are not decoded; the independent request above remains the fallback when no usable native name exists. |
| OpenCode | Fixture-tested read-only exact-ID SQLite metadata reader only. This beta.9 runtime does not expose OpenCode as a provider. |

Native metadata is checked before normal terminal events and when idle history
is reopened. Child-name notifications continue updating child identities, never
the parent title. Unknown or manually owned titles are never inferred from their
wording. Persistence failures roll back title fields without losing unrelated
live metadata.

## Resume/import verification

Focused tests cover provider-specific title precedence, exact session/workspace
matching, original prompt fallback, malformed/oversized/deeply nested metadata,
Cursor read-only SQLite and WAL visibility, symlink rejection, missing/busy stores,
manual-name preservation, the resume deadline, and cancellation before creation.
Each provider's synthetic metadata is also exercised through session creation.
Tests use isolated temporary state and do not need model calls or a live server.
The focused 384-test run passed across import labels, native/generated naming,
Cursor parsing/execution, provider-history synchronization, Codex history repair,
and the Codex app-server adapter. This is not a full-suite or client UI test.

## Earlier background-generation rollout verification

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
