# AgentsServer on Windows (native port)

This repo runs natively on Windows (originally Linux/macOS only), with **kimi** added as a
third backend alongside claude and codex. All features are implemented with native Windows
APIs — no WSL, MSYS2, or Cygwin required at runtime.

## Requirements

- Windows 10 1809+ / Windows 11 (ConPTY), native CPython 3.10+ (developed on 3.13)
- [uv](https://docs.astral.sh/uv/) on PATH
- One or more agent CLIs on PATH: `kimi`, `codex`, and/or `claude` (npm shims are resolved
  to their underlying Node CLI automatically)

## Run

```bat
start-agents-server.bat
```

or manually:

```bat
cd %USERPROFILE%\AgentsServer
uv sync --frozen
set AGENTSDOCK_AGENT_TOKEN=<64-hex-token>
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

State lives in `%USERPROFILE%\.agentsdock` (override with `AGENTSDOCK_STATE_DIR`).
Logs: `server.log` beside `agent_server.py`.

**Credentials:** the launcher no longer hardcodes a token. `start-agents-server.bat` and
`scripts/agents_server_supervise.py` read optional `KEY=VALUE` lines from
`%AGENTSDOCK_STATE_DIR%\server.env` (already-set environment variables win). Put
`AGENTSDOCK_AGENT_TOKEN=...` there for autostart deployments, or export it in the shell.

## What works (measured on this repo's Windows test suite)

- **Chat turns, event streaming (WebSocket + JSONL history), file upload/download,
  whole-history search, scheduled jobs, token auth, health/runtime catalog** for
  four agent runtimes: **claude**, **codex**, **kimi**, and **reasonix** (the
  Reasonix CLI, verified v1.33.0 with its `run -p --output-format stream-json`
  interface and `run --resume <machine-session-id>` session continuation; catalog
  and auth status come from `reasonix doctor --json`).
- **Secure workspace file operations** (browse/read/preview/download/write/create/rename/
  delete/search) through `winfs`: handle-based `NtCreateFile` traversal with junction/
  symlink reparse rejection. Roots open read-only and mutate on demand, so browsing
  works even when a directory is held with restrictive sharing (Explorer/indexer/AV);
  conflicts surface as precise 403s (permission vs. sharing violation), not generic
  "unavailable" errors.
- **Process ownership**: agent runs live in Windows Job Objects
  (`CREATE_SUSPENDED` → assign → resume, kill-on-close), so stopping a turn kills its
  whole process tree. Real process/CPU/memory diagnostics in health and the inspector.
- **In-app persistent terminal** through ConPTY (`winterminal`): per-chat `cmd.exe` shells
  (or any shell), resize, bounded history, reconnect after WebSocket disconnect (shell
  keeps running), kill from the UI. Requires the `pywinpty` dependency (in lockfile).
- **Managed self-updates** (`winupdate` + `scripts/agents_server_supervise.py`): signed
  release verification (ed25519), staged extraction with traversal/link rejection,
  stop → switch → health-check → automatic rollback, interrupted-update recovery. The
  supervisor restarts the server, adopts a healthy replacement, and restores the previous
  release if an update fails.

## Windows differences (by design, reported in `/api/health`)

- Terminal sessions are ConPTY shells owned by the server process; they do not survive a
  server restart (POSIX tmux daemon persistence has no native equivalent). History is
  bounded (512 KiB); scrollback lives in the client's xterm.js.
- Stopping a turn is immediate job termination (Windows has no SIGTERM); a CTRL_BREAK
  grace window is attempted when a console is attached.
- Workspace semantics are NTFS-native: case-insensitive, reparse points (junctions,
  symlinks, OneDrive placeholders) are rejected rather than followed, POSIX mode bits have
  no equivalent (read-only attribute is preserved across atomic replace).
- Process rows report `pgid`/`sid`/`stat` as `null` (no such concepts on Windows).

## Mobile client compatibility

The **kimi** and **reasonix** runtimes are local server extensions. The stock mobile
client's provider definitions only know claude and codex, so choosing or managing these
runtimes from an unmodified client requires **client-side changes** (provider/model
definitions). The server side is complete: health/runtime catalog advertise all four
runtimes with truthful installed/auth status, and the API accepts `backend: "kimi"` or
`"reasonix"` on session creation.

Model labels are verified before being advertised: the kimi catalog only lists aliases
the kimi CLI actually accepts (from its config), and Reasonix appears as its own runtime
whose models are the provider names from `reasonix doctor --json` — not as a kimi model.

## Updating

Managed: use the server's update API/UX — the native updater handles staging,
verification, switch, health check, and rollback. For auto-restart across updates run
under `scripts/agents_server_supervise.py` (see `winupdate.autostart_command(...)` for the
prepared Task Scheduler `ONLOGON` registration command; registration is user-consented and
not performed by the installer).

Manual: `git pull`, `uv sync --frozen`, restart. Do not run `install.sh`/`uninstall.sh`
or `deploy.sh` on Windows (POSIX service managers only); the native lifecycle above
replaces them.

## Tests

```bat
uv run python -m unittest discover -p "test_*.py"
```

778 tests. Platform-gated skips are honest and runtime-probed: POSIX installer-contract
tests (`install.sh`/systemd/launchd), POSIX-syscall mechanism mocks, and symlink-
privilege-dependent tests on hosts without `SeCreateSymbolicLink` (junction tests cover
the same reparse-rejection code path). See `WINDOWS_PORT_REPORT.md` for the full
accounting and `WINDOWS_PORT_PLAN.md` / `WINDOWS_PORT_PROGRESS.md` for port internals.

`windows-port.patch` is a historical artifact of the first port phase; the working tree
supersedes it.
