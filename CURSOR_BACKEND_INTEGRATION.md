# Cursor backend — server-side summary for client integration

Branch: `research/cursor-backend-prototype` (local only, **not pushed**, 5 commits, not merged to `main`).
Server-side work is done and stress-tested. This doc is the contract the AgentsDock client
(`/Volumes/SSD/Codes/ZenithDock/electron`) needs to build against.

## What changed on the server

`"cursor"` is now a fully valid, activated `backend` value — same status as `"claude"`/`"codex"`.
No API shapes changed; `cursor` just became a legal value wherever `backend` already appears.

- `VALID_BACKENDS = {"claude", "codex", "cursor"}`
- Turn runner: `run_cursor()` — spawns the real `agent` CLI (`agent -p "<prompt>" --output-format stream-json ...`)
  per turn (stateless, not a persistent connection), streams back the same event types Claude/Codex already emit.
- Runtime detection (`GET /api/health` → `runtimes.cursor`) is real: checks `agent --version` / `agent status`,
  reports `ready` / `unauthenticated` / `missing` / `error` exactly like the other two backends.

## Session fields relevant to `cursor`

All via the existing `POST /api/sessions` and `PATCH /api/sessions/{id}` endpoints — no new endpoints.

| Field | Type | Notes |
|---|---|---|
| `backend` | `"claude" \| "codex" \| "cursor"` | now accepts `"cursor"` |
| `model` | `string \| null` | **reused generic field**, not cursor-specific. Passed straight through to `--model`. |
| `cursor_permission_mode` | `"default" \| "auto_review" \| "full_access" \| "plan"` | **new field**, mirrors `claude_permission_mode`'s single-enum pattern (not Codex's 4-field style). Default: `"default"`. |
| `cursor_session_id` | `string \| null`, **read-only** | server-populated resume handle (like `codex_thread_id`/`claude_session_id`). Returned in session detail responses now (was silently `null` until this was fixed). |

### Model discovery — now live, real per-account model list

`GET /api/runtime/catalog` → `backends.cursor.models` is real, not hardcoded: the server runs
`agent --list-models` and returns every model the account can see (~200+ entries on a paid account),
same shape as `backends.claude.models`/`backends.codex.models` (`{value, label}` pairs, empty-value
entry first = "use server default"). No cursor-specific parsing needed on the client side — same
`runtimeCatalogOptions()` helper that already works for Claude/Codex works here unchanged.

**Free-plan accounts see the full list too**, but Cursor's own API rejects actually *using* any model
other than `"auto"` with `ActionRequiredError: Named models unavailable Free plans can only use Auto`
— that's an account-tier restriction from Cursor's backend, not something the server filters out.
It surfaces as a normal turn `error` event (visible in-chat), the same as any other provider rejection —
no special client-side handling needed, but worth knowing so a free-plan user's "why did picking a
model fail" question has a real answer instead of looking like a bug.

### `cursor_permission_mode` semantics (for the picker UI)

| Value | Real CLI flags | Behavior (verified live) |
|---|---|---|
| `default` | `--trust` | File reads/edits allowed. **Shell commands always rejected.** Safest, recommended default. |
| `auto_review` | `--trust --auto-review` | A server-side classifier auto-runs commands it judges safe, prompts-equivalent (rejects) the rest. |
| `full_access` | `--trust --force` | Every command runs, no restrictions. |
| `plan` | `--mode plan` | Read-only planning mode; no edits, no shell, produces a plan instead. |

Suggested UI copy, following `claude-permission-copy.ts`'s existing convention:
```ts
export const CURSOR_PERMISSION_MODES = ['default', 'auto_review', 'full_access', 'plan'] as const
export const CURSOR_PERMISSION_MODE_LABELS = {
  default: 'Ask for access',       // shell blocked, edits ok
  auto_review: 'Smart auto-review', // safe commands auto-run
  full_access: 'Full access',       // --force, everything runs
  plan: 'Plan only',                // read-only
}
```

## Event stream — no client-side changes needed

`run_cursor()` emits the exact same event *type* strings the client already renders for Claude/Codex:
`process_started`, `provider_session`, `assistant_text`, `reasoning_summary`, `tool_started`, `tool_finished`,
`turn_finished`, `error`, `idle_warning`. Payload shapes match (e.g. `tool_finished.tool.name` /
`tool_finished.output` / `tool_finished.exit_code`). Tool names differ from Claude/Codex's capitalized
convention — Cursor's are lowercase: `edit`, `read`, `shell`, `glob`, `grep`, `createPlan`, `getMcpTools`,
etc. — worth checking the UI doesn't have a closed switch/icon map keyed only on the Claude/Codex tool-name set.

## Known gaps (server-side, intentional, not yet built)

- **No standalone/scheduled-job context.** A scheduled job (or anything passing
  `provider_context_mode="standalone"`) against a `cursor` session now returns a clear `400` instead of silently
  misbehaving. Interactive chat turns are fully supported; scheduled jobs on a Cursor backend are not, yet.
- **No resume-rollover recovery.** Codex has a "resume produced nothing, retry on a fresh thread" fallback;
  Cursor doesn't have an equivalent yet (not observed to be needed in ~15 real resumed turns during testing).

## What the client needs to change

Confirmed by reading `/Volumes/SSD/Codes/ZenithDock/electron` — these are hardcoded to a claude/codex binary
world and need a third case added:

1. `src/shared/types.ts`: `export type Backend = 'claude' | 'codex'` → add `'cursor'`
2. `src/shared/runtime-catalog.ts`: `REQUIRED_BACKENDS = ['claude', 'codex']` → add `'cursor'`
   (`runtimeCatalogOptions()` itself is already generic/backend-agnostic — only this constant is closed)
3. Three hardcoded backend-picker arrays, all `(['claude', 'codex'] as const).map(...)`:
   - `src/renderer/src/components/Inspector.tsx` (~line 123)
   - `src/renderer/src/components/Composer.tsx` (~line 1391)
   - `src/renderer/src/components/Dialogs.tsx` (~line 1640, new-session dialog)
4. Binary `backend === 'codex' ? 'Codex' : 'Claude'`-style label ternaries scattered through
   `Inspector.tsx`/`Composer.tsx` — these will mislabel a cursor session as "Claude" if not updated
   (search for `=== 'codex'` / `=== 'claude'` in those two files)
5. `Inspector.tsx` line ~946: `knownAgentRouteBackend()` type guard (`value === 'codex' || value === 'claude'`)
   — returns `false` for `'cursor'`, would filter cursor sessions out of cross-chat routing UI
6. Permission menu wiring in `Composer.tsx` (~964-967): currently
   `backend === 'codex' ? <CodexPermissionMenu/> : backend === 'claude' ? <ClaudePermissionMenu/> : null`
   — needs a new `CursorPermissionMenu` component (single-enum style, closer to `ClaudePermissionMenu.tsx`
   than `CodexPermissionMenu.tsx`) and a third branch here

Request/response wire format itself (`createSession()` in `src/main/server-client.ts`) needs **no changes** —
it already passes `backend`/`model` straight through generically.

## How to test locally against the real server code

This branch has an isolated test-server launchd job, separate from the real production instance
(`com.agentsdock.server`, port 7850, untouched):

```
launchctl bootout gui/$(id -u)/com.agentsdock.test-backend 2>/dev/null
launchctl submit -l com.agentsdock.test-backend -- /bin/zsh -lc \
  'cd /Volumes/SSD/Codes/ZenithBotServer && \
   AGENTSDOCK_STATE_DIR=/tmp/agentsdock-test-backend-state AGENT_PORT=7852 AGENT_BIND=127.0.0.1 \
   exec .venv/bin/python agent_server.py serve --bind 127.0.0.1 --port 7852 \
   > /tmp/agentsdock-test-backend.log 2>&1'
```

Point the AgentsDock client at `http://127.0.0.1:7852` (no auth token required, `auth_required: false`
on this test instance) to drive it against real server code on this branch.

## Server-side test coverage already run (real, not just unit tests)

Against a real authenticated Cursor CLI account (both free and, later, upgraded-to-paid), on the isolated
test server above: concurrency, idle-timeout kill, mid-flight interrupt/stop, 6-turn resume stability,
200-function large-diff edit, full permission-mode matrix (all 4 modes, confirmed both correct CLI flags
*and* correct actual tool execution behavior), model switching with a real named model
(`claude-sonnet-5-thinking-high`, confirmed the model itself responded correctly), live per-account model
catalog discovery (205 real models on the paid account), missing-binary detection, and the
standalone-context safety guard. Full existing test suite (`pytest`, 1302 tests) passes unchanged.

## Note on concurrent editing

This repo's working tree was edited by more than one agent in parallel during this work (no git worktree
isolation between sessions). One collision was found and resolved: two near-identical, independently
written `discover_cursor_catalog()` implementations, both with the same real bug (default-model detection
only matched the older `"(current, default)"` label format, not the current `"(default)"` one). If picking
up this branch, diff against `origin/main` before assuming the working tree state matches what's described
here — another agent may have made further uncommitted changes since.
