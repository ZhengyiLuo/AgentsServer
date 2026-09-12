# OpenCode server readiness — 2026-09-11 (hardened 2026-09-12)

## Scope and branch

**Decision: ready for core client integration on this local branch.** This is
not a declaration that the published server already supports OpenCode.

Server integration verification; no AgentsDock/mobile files changed in this
repository and no deployment or published release performed.

- Worktree: `../ZenithBotServer-opencode-readiness`
- Branch: `feature/opencode-provider-server`
- Hardened implementation through `48d7f95` (`Stabilize OpenCode readiness verification`).
- Base synchronized with `origin/main` at `c68018b`.
- The original `test/opencode-verify` and research branch are unchanged.
- `VERSION` is `0.1.26-beta.62` from the synchronized base; published beta.62 does **not** contain
  this OpenCode work. Negotiate capabilities, not just version strings.

## Gaps repaired during verification

| Area | Repair |
| --- | --- |
| Distribution | Include `opencode_agent_client.py` in archive, installer, direct deployment, compile checks, and import smokes. |
| Runtime discovery | Find the official `~/.opencode/bin/opencode` installation under a service-manager PATH; respect an explicit executable override, retain/pin the probed executable, and degrade catalog refresh safely if the CLI disappears. |
| Resume/session identity | Honor both supplied OpenCode ID fields and the generic provider ID; use the right parked-ID field on backend changes. |
| Workspace reset | Rebuild provider instructions and fork memory after deciding to start a replacement provider session. |
| Scoped server helpers | Pass only the validated current-turn environment and runtime context into the OpenCode process. This does not enable unsupported cross-chat routes. |
| Permissions | Merge explicit mode overrides into inherited inline configuration without dropping model/provider settings; malformed configuration fails visibly without disclosing its contents. |
| Idle fork | Preserve the permission mode and seed a fresh provider session with bounded parent memory. Running forks remain rejected. |
| Stop/timeouts | Cover pre-bind hard Stop; recognize `step_start` as startup activity; honor the earliest startup/absolute/idle deadline. |
| Stream validation | Reject inconsistent provider identities, mismatched part types, malformed text, and empty completion reasons. |

The original claim that OpenCode generally ignores process cwd was removed.
The supplied follow-up investigation disproved it on 1.18.29. Explicit `--dir`
is retained as a deterministic invocation contract.

## Follow-up hardening — 2026-09-12

The hardening commits through `48d7f95` extend the core integration without
changing the published server:

- OpenCode local slash skills now use the existing provider-command contract.
  Discovery is a bounded, side-effect-free scan of documented default local
  skill roots. Clients receive display-safe metadata plus opaque,
  revision-bound selectors; the server revalidates a selection at actual turn
  admission and privately injects the selected `SKILL.md`.
- Current-turn uploads use OpenCode's native repeatable `--file` input after
  regular-file, session-ownership, and size validation. Defaults permit at most
  16 files, 10 MiB per file, and 64 MiB total. Image understanding remains
  dependent on the selected model.
- Enforced Plan/full-access and selected-skill turns use an unpredictable
  256-bit one-turn primary agent. Plan denies `bash`, `write`, `edit`, `patch`,
  and `task`; selected-skill turns deny `skill` and `task`. Default mode still
  leaves the operator's OpenCode settings untouched.
- An enforced resumed turn uses `--fork`, requires a different returned
  provider ID, and persists that new ID. This preserves conversation context
  without allowing mutable permissions stored on the old OpenCode session to
  override the one-turn policy.
- Runtime admission is pinned to exactly OpenCode `1.18.29`. Permission
  precedence, fork isolation, event projection, and native file ingestion must
  be revalidated before another CLI version is accepted.
- Only the validated `SKILL.md` body is injected. Referenced skill resources
  receive no automatic `read` or `external_directory` grant and remain subject
  to the operator's existing OpenCode permissions; a bounded, no-symlink
  resource snapshot is deferred.

## Automated and packaging verification

312 targeted automated checks passed across non-overlapping test groups:

- 173: OpenCode parser/runner/HTTP/resolution/lifecycle, existing session forks,
  helper environment, provider-command API, and WebSocket catch-up.
- 130: existing runtime diagnostics, backend switching, Cursor runner, and
  subprocess guard regressions.
- 9: release manifest and targeted installer/archive checks.

An actual release archive was built from a clean temporary source mirror,
checked for the OpenCode module, extracted, and used to import both the server
and adapter from the extracted directory with isolated state/config. Shell
syntax and `git diff --check` also passed. This was a targeted regression run,
not a claim that the entire repository test suite or a live deployment passed.

After the hardening pass, the 173-test focused OpenCode group passed again in 34.581
seconds, and the nine targeted release/installer/archive checks passed. These
remain targeted results, not a claim that every repository test is portable to
this external-volume test environment.

## Real CLI verification

Platform: macOS ARM64, Python 3.13.14, OpenCode 1.18.29,
`opencode/big-pickle`. The test drives the real ASGI HTTP application and its
real subprocess runner, without starting or contacting a production server.
Server state, configuration, OpenCode database/cache, and workspaces are isolated.

All 13 checks passed again on 2026-09-12:

1. Runtime probe, model discovery, and advertised server capability.
2. Missing and non-directory workspaces are rejected before provider launch.
3. Two concurrent sessions sharing one isolated OpenCode database: distinct
   provider IDs, independent workspace files, visible tools/text, token usage.
4. A second turn recalls its previous marker and reuses the provider ID.
5. A changed working directory creates a fresh provider session and emits a
   visible reset notice.
6. A nonexistent resume ID emits an error and clears the stale pointer.
7. Plan mode does not create the requested file.
8. A validated external attachment is readable in Plan without changing its
   source.
9. Explicit full access writes successfully despite a project deny policy and
   rotates the provider session.
10. An invalid model emits a visible error and releases the turn slot.
11. Concurrent wrappers cannot use the same provider session simultaneously.
12. Stop interrupts a real shell tool, exits the runner, releases ownership,
   and prevents the command's post-sleep write (about 0.08 seconds in this run).
13. The turn after Stop starts with a fresh provider session.

A separate seven-check hardening harness also passed against the real CLI:
bounded slash-skill inventory and exact capability admission; private
selected-skill execution with a native attachment; stale-selector rejection;
shell-looking suffix safety; and hostile resumed-session permission isolation
while retaining prior context. The hostile-session check confirmed that
`--fork` rotated the provider ID, retained the continuity token, exposed none
of `bash`, `write`, `edit`, or `task`, and created no marker file. Separate
native single/multiple-file and stdin probes passed, as did PNG vision with a
Mimo vision-capable model.

Re-run explicitly with network access:

```sh
PYTHONDONTWRITEBYTECODE=1 /path/to/server/python scripts/verify_opencode_live.py \
  --binary /path/to/opencode

PYTHONDONTWRITEBYTECODE=1 /path/to/server/python \
  scripts/verify_opencode_skills_live.py \
  --binary /path/to/opencode --expected-version 1.18.29
```

The script prints the retained temporary diagnostics directory. It does not
copy provider credential files or modify existing chats. Free-model latency
and behavior vary; this is a smoke test, not a capacity or model-quality claim.

## Client integration contract

- Gate backend support on `/api/health` → `capabilities.opencode_backend`
  version 1. A supported server can still lack a usable OpenCode executable.
- Read `/api/runtime/catalog` → `backends.opencode` for runtime availability,
  diagnostics, models, and permission modes. Do not hard-code model counts.
- Create with `backend: "opencode"`, `cwd`, optional `model`, and
  `opencode_permission_mode: "default" | "full_access" | "plan"`.
- Use the existing turns, Stop, session list, timeline, and WebSocket endpoints.
  Tools, assistant text, errors, usage, and terminal events use existing shapes.
- Read `opencode_session_id` and permission mode back from the session.
- Gate the slash palette on `/api/health` →
  `capabilities.local_provider_commands_v1`, including OpenCode support, and
  advertise `opencode_provider_commands_v1` on selected-skill turns. Fetch
  `/api/sessions/{session_id}/provider-commands`; submit only its opaque
  `skill_selection` plus the matching `/skill` prompt.
- Upload through the existing session-file endpoint and submit returned
  `file_ids` with the turn. Do not convert image uploads into prompt paths.
- Label `default` as **OpenCode's own settings**, not “no shell access.”
  Plan is provider tool policy, not an operating-system sandbox.
- Idle Fork is a bounded-memory continuation, not a native complete-history
  provider clone. It does not reuse the parent's provider ID.

## Boundaries for the initial client feature

- External OpenCode history discovery/import/reconciliation is not implemented.
  Direct resume requires the correct existing provider ID and working directory.
- Running-chat fork and native live steer remain unavailable. The initial
  OpenCode palette covers server-discovered local slash skills only; configured
  commands/URLs, plugin commands, agents, MCP prompts, built-ins, and external
  history are not included.
- Cross-chat target routing remains capability-gated; the helper environment
  fix does not grant route support or bypass server policy.
- Paid/custom providers, other CLI versions, Linux execution, heavy concurrency,
  and long-duration runs were not live-tested here.
- OpenCode itself must be installed separately on the server host. The server
  archive supplies the adapter, not the OpenCode binary.

The companion client branch uses this branch's hardened chat, slash-skill, and
attachment contracts. These changes are prepared for review only: no
deployment or release was performed, and published `0.1.26-beta.62` still does
not contain the OpenCode integration.
