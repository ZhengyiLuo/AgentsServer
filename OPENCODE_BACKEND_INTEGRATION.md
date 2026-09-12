# OpenCode backend — findings, state of the work, and how to resume

Working notes for adding `opencode` as a fourth AgentsServer backend. Everything
below was established by running the real CLI (opencode **1.18.29**), not read
from documentation. Where a claim came from a single observation that is hard to
reproduce, it says so.

**The integration branch supports OpenCode; it is not deployed.** `opencode` is a selectable backend, a turn
dispatches to `run_opencode`, and `/api/health` advertises
`capabilities.opencode_backend` version 1. See
[the September 11 readiness report](docs/OPENCODE_READINESS_2026-09-11.md) for
current verification, packaging fixes, and the client-facing support boundary.

---

## 1. Where the work stands

| # | Step | Status |
|---|---|---|
| 0 | Research the CLI, auth, events, permissions against the real binary | **done** |
| 1 | `opencode_agent_client.py` — parsing, argv, permissions, diagnostics | **done**, 55 tests |
| 2 | Runtime detection — executable probe, compatibility fence, model catalog | **done**, verified live |
| 3 | Permission modes — decided: match OpenCode | **done** |
| 4 | `run_opencode()` turn runner | **done**, 40 subprocess tests |
| 5 | Session/resume wiring (`opencode_session_id`) | **done** |
| 6 | Client support (types, pickers, permission menu) — separate repo | **done**, companion PR prepared |
| 7 | Activation — `VALID_BACKENDS`, dispatch, capability, catalog, permission API | **done**, 20 HTTP tests |

Files:

- `opencode_agent_client.py` — pure translation layer, no server imports
- `test_opencode_agent_client.py` — 55 tests, real captured fixtures
- `agent_server.py` — detection, catalog, `run_opencode`, session fields
- `test_run_opencode.py` — 40 subprocess-level tests through real pipes
- `test_opencode_api_contract.py` — 20 HTTP tests against the real ASGI app
- `test_opencode_runtime_resolution.py` — service-manager executable discovery
- `test_opencode_session_lifecycle.py` — resume identity and fork regressions

End-to-end verified against the real binary, not just the fake CLIs: driving
`run_opencode` with the real `opencode` produced `tool_started` / `tool_finished`
/ `assistant_text` / a clean `turn_finished` with accumulated usage
(11878 in / 292 out), wrote its file **into the session workspace** rather than
`$HOME` (so `--dir` works through the runner, not just in the argv test), and
persisted its resume id. A second turn resuming that id recalled a number stated
in the first and reused the same session id.

Historical prototype test status (not the current readiness result): 2724 tests, 69 failures, reported as pre-existing
and confined to `test_installer` (60), team hub (5), `test_codex_app_server` (3).
Verified by stashing the `agent_server.py` changes and re-running those modules:
identical 60 failures / 9 errors with and without this work.

---

## 2. Why this was cheaper than Cursor

The Cursor integration established the whole shape — stateless spawn-per-turn
runner, isolated parsing module with real-fixture tests, capability negotiation,
permission modes, artifact delivery. OpenCode reuses all of it, and its event
stream is *more* regular than Cursor's.

| Need | OpenCode | Verified |
|---|---|---|
| Non-interactive mode | `opencode run` | yes |
| Structured stream | `--format json`, newline-delimited | yes |
| Session resume | `-s <sessionID>` | yes — recalled a number across two turns |
| Model selection | `-m provider/model` | yes |
| Tool visibility | `tool_use` events with name/input/output | yes |
| Prompt off argv | reads the prompt from **stdin** | yes |
| Auth | **none needed** for its free models | yes — ran with no `auth.json` on disk |
| Workspace | explicit `--dir <path>` plus process cwd | verified through the runner |

Every event already carries `sessionID`, so the runner never waits for an init
event to learn the resume handle, and the tool name is a plain `part.tool` field
rather than Cursor's dynamic `<name>ToolCall` key.

---

## 3. The findings that actually shaped the code

### 3.1 Explicit workspace selection; earlier cwd claim corrected

The first investigation reported files being written under the user's home
despite a different process cwd. That observation was not reproduced in the
subsequent 1.18.29 verification: without `--dir`, `pwd` matched the process cwd
and a project `opencode.json` denying bash was loaded. The original cause is
unknown; it is not evidence that OpenCode generally ignores cwd.

The runner intentionally retains both `cwd=` and explicit `--dir` so workspace
selection does not depend on implicit defaults. `test_workspace_is_passed_as_dir`
and the CLI compatibility fence guard that invocation contract, not the
superseded claim about a CLI safety defect.

### 3.2 Permission precedence — decided: match OpenCode

**By default OpenCode allows everything, including `bash`, with no prompt.** With
no flags at all it ran `echo ... > proof.txt` and the file appeared. `--auto` is
not required for that; `--auto` only auto-approves permissions explicitly
configured as `ask`. This is the opposite of Cursor, whose default rejects shell.

**Decision (Georgia, 2026-09-06): match OpenCode.** The `default` mode injects
nothing at all, so a session behaves exactly as `opencode` does in the operator's
own terminal, including any `opencode.json` they wrote for themselves.
`full_access` and `plan` remain explicit overrides, reusing Cursor's three mode
names so the client needs no new vocabulary.

The enabler is `OPENCODE_CONFIG_CONTENT`, an environment variable holding inline
config JSON. Since AgentsServer already composes a fresh environment per turn,
that gives real per-session permission modes without touching the user's config
file. Measured precedence:

| project `opencode.json` | `OPENCODE_CONFIG_CONTENT` | result |
|---|---|---|
| `bash: deny` | unset | bash denied — operator config honoured |
| `bash: deny` | unrelated key | bash still denied — **sources merge** |
| `bash: deny` | `bash: allow` | bash ran — **env wins per key** |

So an injected mode overrides the operator only for the tools it names. Any entry
sent in the default mode, even a permissive one, would overrule their choice.
Naming nothing is the only way to genuinely defer to them.

### 3.3 Denying one tool does not deny the outcome

With only `bash` denied, the model used the `write` tool and produced the same
file — reproduced twice. A read-only mode must deny the whole mutating set
(`bash`, `write`, `edit`, `patch`), which is what `plan` does.

### 3.4 A fully denied turn can spin

With those four denied, the model retried past **ten minutes** without finishing.
The restriction held — the file was never created — but nothing inside OpenCode
bounds that. The server has both an idle timeout and an absolute turn timeout:
`OPENCODE_IDLE_TIMEOUT_SECONDS` handles silence; `OPENCODE_TURN_TIMEOUT_SECONDS`
also bounds a model that keeps emitting activity without finishing.

### 3.5 Two event shapes that lie about success

- A tool withheld by config comes back as a **synthetic tool named `invalid`
  with status `completed`**. Keying off status alone would report a blocked
  action as one that ran. The parser special-cases the name.
  *(Reproduced in the subsequent 1.18.29 permission verification. Its occurrence
  still depends on the model trying to call a withheld tool.)*
- An `error` event carries **`error`, not `part`**:
  `{"type":"error","sessionID":...,"error":{"name":...,"data":{"message":...}}}`.
  The first prototype required `part` and so turned every provider failure into a
  parse failure, reporting the wrong cause for the turn.

### 3.6 Usage is per step, not per turn

OpenCode emits token counts on every `step_finish`. Cursor reports once at the
end. Keeping only the last step would under-report every turn that used a tool,
so the runner accumulates (`merge_opencode_usage`).

### 3.7 Resume has two failure modes, and the bad one is silent

Sessions live in one global SQLite database (`~/.local/share/opencode/opencode.db`)
and every row is bound to the `directory` it was created in. That produces two
very different failures:

| Situation | Behaviour |
|---|---|
| Session does not exist | exits 1 in ~1.3s, `Error: Session not found` on stderr, zero JSON on stdout |
| Session exists, different `--dir` | **hangs indefinitely** — no events, no stderr, never exits |

The second is the dangerous one and the more likely one in practice: it is what
happens whenever a user changes a chat's working directory. Nothing in the CLI
reports the binding, and `opencode session list` is **global, not per-directory**
(verified: identical output from two different directories), so no pre-flight
check against OpenCode can distinguish it. A pre-flight would also cost ~0.9s on
every resumed turn.

The two are therefore handled separately:

- **Wrong directory — prevented.** `opencode_session_cwd` is stored next to the
  session id (following the existing `claude_session_cwd` precedent), and
  `resolve_opencode_resume_provider` declines to resume when the turn's cwd no
  longer matches. The chat starts a new session and says so in the timeline.
  Verified live: after moving a chat's cwd, the next turn answered in 8 seconds
  with an explanation, instead of hanging for the full 120-second startup
  timeout and then failing with a generic message.
- **Session gone — self-healing.** Detection stays where it was (stderr, ~1.3s,
  no latency tax on healthy turns), but the runner now also clears the dead
  pointer, so the user's next message simply starts a new session instead of the
  chat failing identically forever. It deliberately calls
  `record_runtime_success` — a dead chat-level id must not mark the whole backend
  unhealthy and hide OpenCode from every other chat on the server.

If a resume does hang for some other reason, the startup-timeout message now
names the resumed session and the directory binding as the likely cause rather
than reporting a bare timeout.

### 3.8 Auth is informational, never a gate

`opencode auth list` reports `0 credentials` on a working install. The
`opencode/*` free models run with no `auth.json` on disk at all. `probe_runtime`
therefore reports `ready` with zero credentials and only mentions it in the
message. Adding a provider adds its models to `opencode models`.

---

## 4. Turn contract as implemented

Stream: every line is one JSON object with `type`, `timestamp`, `sessionID`, and
either `part` or (for errors) `error`.

```
step_start   part.type=step-start
tool_use     part.type=tool        part.tool, part.callID, part.state{status,input,output,...}
text         part.type=text        part.text
step_finish  part.type=step-finish part.reason, part.tokens{...}, part.cost
error        error.name, error.data.message
```

A turn is a sequence of steps: `step_start` → (`tool_use` | `text`)* →
`step_finish`. There is **no separate terminal result event**: the turn ends at
a `step_finish` whose `reason` is anything other than `tool-calls` (observed:
`stop`). Tool statuses observed: `completed`, and `invalid` when denied.

Runner decisions that follow from that:

- The session is considered started at the **first event of any kind**, since
  every event carries `sessionID` and there is no init event.
- Most tools arrive as a single `completed` event with no preceding start, so
  the runner synthesises the `tool_started` timeline event before the finish.
- Spawn-per-turn, not `opencode serve --attach`. Attach mode exists and would
  avoid cold starts, but a long-lived shared provider process is exactly what
  produced the Codex app-server problems (threads that cannot be released,
  silently dropped notifications). Not worth it for v1.
- The prompt goes over **stdin**, keeping it off the argv size limit and out of
  the process table. `redacted_provider_argv` treats OpenCode like Cursor.
- The turn is wrapped in `cursor_process_guard.py` (provider-agnostic). This
  matters more here than for Cursor: `opencode run` starts a local server
  process of its own, which must not outlive the turn.

---

## 5. Reproducing the findings

```bash
export PATH="$HOME/.opencode/bin:$PATH"

# process cwd is respected; --dir makes the workspace contract explicit
mkdir -p /tmp/oc && (cd /tmp/oc && opencode run --format json \
  -m opencode/big-pickle "Use the bash tool to run exactly: pwd")   # -> /tmp/oc
opencode run --format json --dir /tmp/oc -m opencode/big-pickle \
  "Use the bash tool to run exactly: pwd"                            # -> /tmp/oc

# permission precedence
echo '{"permission":{"bash":"deny"}}' > /tmp/oc/opencode.json
OPENCODE_CONFIG_CONTENT='{"permission":{"read":"allow"}}' \
  opencode run --format json --dir /tmp/oc -m opencode/big-pickle "run bash: echo hi"   # denied
OPENCODE_CONFIG_CONTENT='{"permission":{"bash":"allow"}}' \
  opencode run --format json --dir /tmp/oc -m opencode/big-pickle "run bash: echo hi"   # runs

# dead resume
opencode run --format json --dir /tmp/oc -s ses_doesnotexist "hi"    # exit 1, stderr only
```

Tests:

```bash
.venv/bin/python -m unittest test_opencode_agent_client test_run_opencode
```

---

## 6. Client work (separate repo, AgentsDock)

Mirrors the Cursor client work exactly; nothing conceptually new:

- accept `opencode` in the backend union / pickers
- read `capabilities.opencode_backend` from `/api/health`
- permission menu reuses `default` / `full_access` / `plan`, but the **label for
  `default` must not say "no shell"** — here it means "OpenCode's own defaults",
  which do allow shell
- model picker consumes `catalog.backends.opencode.models`; there is no `auto`
  router equivalent, so the empty value means "let the CLI choose"

---

## 7. The HTTP contract the client talks to

Activation is done and verified over the real ASGI app, using the same
endpoints the AgentsDock client uses.

| Endpoint | OpenCode surface |
|---|---|
| `GET /api/health` | `capabilities.opencode_backend` → `{available: true, required: false, version: 1}` |
| `GET /api/runtime/catalog` | `backends.opencode` → `models`, `default_model`, `permission_modes`, `default_permission_mode`, `available`, `diagnostic` |
| `POST /api/sessions` | accepts `backend: "opencode"` and `opencode_permission_mode` |
| `PATCH /api/sessions/{id}` | `opencode_permission_mode` (lifecycle-locked like the other policy fields) |
| `GET /api/sessions/{id}` | exposes `opencode_session_id` and `opencode_permission_mode` |
| `POST /api/sessions/{id}/turns` | dispatches to `run_opencode` |

Notes for the client:

- `capabilities.opencode_backend.available` advertises the **server contract**.
  Runtime readiness is separate, in `catalog.backends.opencode.available`, so an
  old server and a missing CLI are distinguishable.
- Permission mode values are `default` / `full_access` / `plan` — the same three
  Cursor uses — but the **label for `default` must not say "no shell"**. Here it
  means OpenCode's own defaults, which do allow shell.
- There is no `auto` router equivalent. The empty model value is the "server
  default" option, meaning "let the CLI choose".
- An unknown permission mode is rejected with 422 at the request model.

Verified end to end against the real binary over HTTP, not only against the fake
CLIs the tests use: health advertised version 1, the catalog listed 8 models from
the real `opencode models`, a session was created on the backend, a turn returned
`HTTP_PATH_OK` with a clean `turn_finished`, and the resumable session id was
bound and readable back from `GET /api/sessions/{id}`.

The companion AgentsDock desktop branch implements the client contract and was
validated against this server branch. Mobile remains out of scope.

## 8. Open questions

- **Free-model quality.** All testing used `opencode/*` free models, which are
  weak and occasionally ignore instructions (one test run answered via `write`
  when asked for `bash`). Behaviour with a real provider configured is unverified.
- **`invalid` tool shape** (§3.5) has now been reproduced; it is covered by fixtures.
- **History import.** `opencode export <sessionID>` is now used by the live
  verification harness to inspect fork continuity and permissions. A complete
  external-history discovery/import/reconciliation flow remains out of scope.
- **Concurrency scale.** Two simultaneous real `opencode run` processes sharing
  an isolated SQLite database passed on September 11, including independent
  workspace writes and separate resume IDs. Sustained/high-concurrency load
  and multiple server hosts are not covered by that smoke test.
- **Why a session goes missing at all** is still unknown — the dead-session path
  is handled, but no expiry, pruning or size limit has been identified, so it is
  unclear how often users will actually hit it.
