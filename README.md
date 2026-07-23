# AgentsServer

**AgentsServer is the self-hosted execution backend for
[AgentsDock](https://github.com/ZhengyiLuo/AgentsDock).** AgentsDock provides
the polished desktop and mobile chat experience; AgentsServer runs on the
machine that owns your workspaces, Claude Code installation, and Codex CLI.
Together they provide persistent agent chats without routing private project
files through a third-party chat service.

AgentsServer exposes an authenticated HTTP/WebSocket API and streams normalized
Claude/Codex events, files, videos, uploads, scheduled jobs, process inspection,
and persistent tmux terminals to AgentsDock clients.

```text
AgentsDock (Mac, iPhone, iPad, Linux)
        |
        | private HTTP/WebSocket connection
        v
AgentsServer (your workstation or server)
        |
        +-- Claude Code CLI
        +-- Codex CLI
        +-- local workspaces, files, jobs, and tmux sessions
```

This repository is intentionally server-only. It should not contain local chat
state, uploaded files, tokens, compiled caches, private hostnames, or personal
machine paths.

## What It Gives AgentsDock

- Creates and resumes chat sessions for Claude and Codex CLI backends.
- Streams live agent events over WebSocket while preserving a JSONL event
  history on disk.
- Accepts file uploads and serves generated artifacts, including videos.
- Supports queued turns, stop requests, chat forking, context digests, and rough
  history import from provider sessions.
- Recovers oversized Codex provider threads by rolling the same chat onto a
  fresh thread with bounded recent memory when remote compaction fails.
- Creates handoff digests with an actual LLM summarizer; the raw transcript/file
  pack is only internal source material.
- Runs recurring/loop jobs per chat with host load/memory guardrails.
- Provides optional live process and tmux-pane inspection for active work.
- Hosts one persistent interactive tmux terminal per chat. Clients attach over
  an authenticated PTY WebSocket; disconnecting a client does not stop the
  tmux session, its panes, or processes. Structured actions create, select,
  split, and close individual windows while guarding the final persistent
  window from accidental destruction. Archiving a chat kills this owned tmux
  session and prevents it from being recreated until the chat is unarchived.
- Gives every Claude and Codex turn the owning chat's tmux session name through
  `AGENTSDOCK_TMUX_SESSION` and prompt context. Agents can inspect the current
  pane with `tmux capture-pane` when it is relevant, but are instructed not to
  type into, resize, or destroy the interactive terminal without an explicit
  user request.
- Discovers available runtime models/efforts from the installed CLI tools when
  possible.
- Reports Claude Code and Codex installation, authentication, version, and
  latest-run health separately from basic server connectivity. New turns are
  rejected with an actionable error before timeline activity when their
  selected runtime is unavailable.

## Requirements

- Linux or macOS host with Python 3.10+.
- `uv` recommended for the runtime environment.
- Claude CLI and/or Codex CLI installed and authenticated on the agent host.
- `tmux` for persistent chat terminals, tmux-pane inspection, and detached
  managed updates.
- Tailscale on the agent host and each client device if you want to use the
  server from another Mac, iPhone, or iPad.
- Optional: a user-level `systemd` service on Linux.

## AgentsDock

AgentsDock is the companion client for this server. It provides multiple chats
and folders, queues and scheduled jobs, rich Markdown/code rendering, inline
media, downloads and drag-out, code review, search, notifications, and
persistent per-chat terminals.

Get the client and current installation instructions from the
[AgentsDock repository](https://github.com/ZhengyiLuo/AgentsDock). The macOS
desktop app is available as a Developer ID-signed and Apple-notarized build;
Apple-platform test builds are also distributed through TestFlight.

## One-Command Setup

Clone the repository and run the idempotent installer as the user who will run
Claude Code or Codex:

```bash
git clone https://github.com/ZhengyiLuo/AgentsServer.git
cd AgentsServer
./install.sh
```

Before changing state, releases, configuration, or services, the installer
checks for `tmux`, `curl`, and the platform service command (`launchctl` on
macOS or `systemctl` on Linux), and verifies that the current user's service
domain responds. Missing tools or an unavailable user service session produce
platform-specific guidance. The preflight never invokes a package manager or
`sudo` itself.

After that preflight, the installer uses `uv`, installs a user-level service,
creates a private access token, verifies authenticated health, and preserves existing
`~/.agentsdock` chat state on every update. Existing
`~/.zenithbot-agent` state is migrated automatically and left behind as a
compatibility link. The installer does not use `sudo`.

AgentsDock desktop can run this same installer locally or over an existing SSH
key connection from its first-run setup window. Remote clients should use the
Tailscale URL printed by the installer.

The same guided flow is available later from **Settings > Install or update
AgentsServer**. Rerunning it is the supported app-managed update path: it
replaces the server runtime and restarts the user service while preserving the
access token, configuration roots, chat history, jobs, files, and terminals.
Direct desktop builds can perform local/SSH setup; App Store-sandboxed builds
can configure the server URL and token but cannot launch service installers.

Once a versioned installation is present, AgentsDock can also check and apply
signed releases directly from Settings. The server downloads a release only
from this repository's GitHub Releases page, verifies an Ed25519-signed
manifest and the archive SHA-256, installs into a versioned directory, restarts
the user service, and accepts the release only after authenticated health
passes. The previous healthy release remains available for automatic rollback.

## Manual Onboarding

1. Clone this repo on the machine that will run the agents.

```bash
git clone https://github.com/ZhengyiLuo/AgentsServer.git
cd AgentsServer
```

2. Create a Python environment.

```bash
uv venv
uv sync --frozen
```

3. Install and authenticate the backend CLI tools you want to use.

AgentsServer does not bundle Claude or Codex. It shells out to the CLI tools
that are already installed on the agent host. Install the official Claude CLI
and/or Codex CLI, sign in or configure credentials for each, then verify the
commands work in the same shell/user that will run the server:

```bash
command -v claude
claude --version

command -v codex
codex --version
```

If you only want one backend, install only that backend and set
`AGENTSDOCK_BACKEND` accordingly.

4. Start the server locally.

```bash
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

5. Check health from the server machine.

```bash
curl http://127.0.0.1:7850/api/health
```

6. Connect AgentsDock.

Open AgentsDock, enter the server URL printed by the installer, and paste its
access token. The desktop app can also run the installer for you during
first-run setup.

For a client on the same machine, use:

```text
http://127.0.0.1:7850
```

For another Mac, iPhone, or iPad, use Tailscale. Install Tailscale on the agent
host and client device, confirm both devices are in the same tailnet, then use
the server's Tailscale IP:

```text
http://<tailscale-ip>:7850
```

Do not expose port `7850` directly to the public internet. Use Tailscale or
another private network, and set `AGENTSDOCK_AGENT_TOKEN` for shared-token
access control.

## Run Locally

```bash
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

Health check:

```bash
curl http://127.0.0.1:7850/api/health
```

The default state directory is `~/.agentsdock`. Override it when you want
state somewhere else:

```bash
AGENTSDOCK_STATE_DIR=/path/to/state \
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

## Security

Set `AGENTSDOCK_AGENT_TOKEN` to require a shared bearer token for HTTP calls,
uploads, file/video fetches, and WebSocket streams.

```bash
export AGENTSDOCK_AGENT_TOKEN='replace-with-a-long-random-token'
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

Clients should send either:

```http
Authorization: Bearer replace-with-a-long-random-token
```

or `X-AgentsDock-Token`. The legacy `X-ZenithDock-Token` header remains
accepted for existing clients.
Leave the variable unset only for trusted local development.

## Remote Access With Tailscale

Remote access is expected to go through Tailscale. This keeps the server
reachable from phones, tablets, and laptops without publishing the raw agent
port on the internet.

On the agent host:

```bash
tailscale status
tailscale ip -4
```

On the client device, make sure Tailscale is connected to the same account or
tailnet, then set the AgentsDock server URL to:

```text
http://<tailscale-ip>:7850
```

If the browser can open `/api/health` but the app cannot connect, check:

- the client is also connected to Tailscale
- the URL includes the correct port
- the same `AGENTSDOCK_AGENT_TOKEN` is configured in the app
- the server is bound to `0.0.0.0` or the Tailscale interface, not only
  `127.0.0.1`

## Updating AgentsServer

Pull the newest version and rerun the installer. It updates the runtime and
service while preserving the access token and all chat state:

```bash
git pull --ff-only
./install.sh
```

On Linux, inspect the installed service with:

```bash
systemctl --user status agents-server.service --no-pager -l
journalctl --user -u agents-server.service -f
```

On macOS, the installer creates the LaunchAgent
`com.agentsdock.server` and writes logs under
`~/Library/Logs/AgentsServer/`.

### Managed updates from AgentsDock

Managed update endpoints use the same access token as the rest of the API.
Release manifests and archives are also verified with the public Ed25519 key
bundled by the installer, so the endpoint can install only an official signed
AgentsServer release:

```text
GET  /api/admin/update
POST /api/admin/update/check
POST /api/admin/update/start
```

The update runs in a detached tmux session so restarting AgentsServer cannot
terminate its own installer. Progress is written to
`~/.agentsdock/admin/server-update.json`, and installer output is kept in
`server-update.log` beside it. Chat history, files, jobs, tokens, and tmux
sessions remain under the persistent state/configuration roots and are never
placed inside a release directory.

## Development Deployment Helper

New installations should use `install.sh`. For a managed installation,
`deploy.sh` copies the complete server runtime into the active release,
compiles it, restarts the configured user service, and checks local health on
the remote host. It is intended for development, not end-user upgrades.

```bash
./deploy.sh <ssh-host>
```

Optional variables:

```bash
AGENTSDOCK_REMOTE_APP_DIR='.local/share/agents-server/current' \
AGENTSDOCK_SERVER_SERVICE='agents-server.service' \
AGENTSDOCK_AGENT_TOKEN='replace-with-a-long-random-token' \
./deploy.sh <ssh-host>
```

The deploy helper writes to:

```text
<remote-app-dir>/agent_server.py
```

Run `install.sh` before the first deploy so the versioned runtime, environment,
token, and service are present. A reference Linux unit lives at
`systemd/agents-server.service.example`.

## Systemd Template

New installations do not need to copy the template because `install.sh`
creates and manages `agents-server.service` automatically. The template is
provided for inspection and custom deployments.

Manual install flow:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/agents-server.service.example ~/.config/systemd/user/agents-server.service
systemctl --user daemon-reload
systemctl --user enable --now agents-server.service
```

Then check it:

```bash
systemctl --user status agents-server.service --no-pager -l
curl -H 'Authorization: Bearer replace-with-a-long-random-token' \
  http://127.0.0.1:7850/api/health
```

## Useful Configuration

Most settings are environment variables. New configurations should use
`AGENTSDOCK_*`. Historical `ZENITHBOT_*` and `ZENITHDOCK_AGENT_TOKEN` names are
accepted only as compatibility aliases so existing installations can migrate
without losing chat state:

| Variable | Purpose | Default |
|---|---|---|
| `AGENTSDOCK_STATE_DIR` | Persistent session/job/file state directory | `~/.agentsdock` |
| `AGENTSDOCK_AGENT_CWD` | Default working directory for new sessions | user home |
| `AGENTSDOCK_AGENT_BIND` | Bind address | `0.0.0.0` |
| `AGENTSDOCK_AGENT_PORT` | Port | `7850` |
| `AGENTSDOCK_AGENT_TOKEN` | Shared bearer token | unset |
| `AGENTS_SERVER_INSTALL_DIR` | Versioned server runtime root | `~/.local/share/agents-server` |
| `AGENTSDOCK_BACKEND` | Default backend, `claude` or `codex` | `claude` |
| `CLAUDE_BIN` | Claude Code executable name/path | `claude` |
| `CODEX_BIN` | Codex executable name/path | `codex` |
| `AGENTSDOCK_RUNTIME_DIAGNOSTIC_TTL_SECONDS` | Cache lifetime for safe CLI version/auth probes | `60` |
| `CLAUDE_PROJECTS_ROOT` | Claude history search root | `~/.claude/projects` |
| `CODEX_SESSIONS_ROOT` | Codex history search root | `~/.codex/sessions` |
| `AGENTSDOCK_JOB_MAX_ACTIVE_RUNS` | Scheduled-job concurrency cap (`0` disables this dedicated cap) | `0` |
| `AGENTSDOCK_MAX_ACTIVE_AGENT_RUNS` | Interactive agent concurrency cap | `10` |
| `AGENTSDOCK_JOB_MIN_AVAILABLE_MEM_MB` | Job launch memory guardrail | `4096` |
| `AGENTSDOCK_MIN_START_AVAILABLE_MEM_MB` | Interactive launch memory guardrail | `2048` |
| `AGENTSDOCK_HANDOFF_DIGEST_BACKEND` | LLM backend for context digests, `claude` or `codex` | `claude` |
| `AGENTSDOCK_HANDOFF_DIGEST_MODEL` | LLM model for context digests | `sonnet` |
| `AGENTSDOCK_HANDOFF_DIGEST_EFFORT` | Optional digest reasoning/effort setting | unset |
| `AGENTSDOCK_HANDOFF_DIGEST_TIMEOUT_SECONDS` | Digest summarizer timeout | `180` |
| `AGENTSDOCK_HANDOFF_DIGEST_CHARS` | Final digest character cap | `56000` |
| `AGENTSDOCK_CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS` | Maximum time for each isolated Git worktree snapshot | `120` |

## Context Digests

`POST /api/sessions/{session_id}/digest` creates a real LLM-summarized handoff
for another chat. The server first builds a bounded source packet from recent
events and files, then asks the configured digest backend to summarize it into
a clean Markdown handoff. If the LLM summarizer fails, the endpoint fails
visibly instead of returning the raw source packet as if it were a digest.

By default, the digest summarizer uses Claude Sonnet:

```bash
AGENTSDOCK_HANDOFF_DIGEST_BACKEND=claude
AGENTSDOCK_HANDOFF_DIGEST_MODEL=sonnet
```

You can switch it to Codex or another installed CLI model, but the relevant CLI
must already be authenticated for the same Unix user that runs the service.

## Backend CLI Notes

The backend selection in AgentsDock only chooses which CLI the server invokes.
The model, effort, authentication, provider-side session storage, and available
commands still come from the installed CLI tools and their local configuration.

Recommended checks before connecting clients:

```bash
# Claude backend
command -v claude
claude --version

# Codex backend
command -v codex
codex --version
```

Run these as the same Unix user that owns the systemd service. If the CLI works
in your login shell but fails under systemd, check the service `PATH`, virtual
environment, and any provider-specific auth/config files.

### Runtime diagnostics

API contract v9 exposes privacy-safe runtime status in two places:

```text
GET /api/health
GET /api/runtime/catalog?refresh=true
```

The health response includes cached `runtimes` entries. The catalog endpoint's
`refresh=true` query forces a fresh version/authentication probe. Each backend
reports `ready`, `missing`, `unauthenticated`, or probe `error`, plus an
actionable recovery instruction. It never returns account identity, auth
output, or tokens.

A new prompt performs the same preflight before reserving real agent work. If
the selected CLI is unavailable, the endpoint returns a structured
`503 runtime_unavailable` response. Failures after a healthy launch remain a
`last_error` on a ready runtime so model overloads, bad thread IDs, and ordinary
provider failures are not mislabeled as missing installations.

## Agent-managed scheduled jobs

Every Claude and Codex turn receives a compact snapshot of up to 25 jobs
belonging to that chat. The snapshot contains only job ID, title, enabled
state, schedule, IANA timezone, and next-run time; job prompts are omitted.
The helper's `list` command returns the complete current set. Agents are
instructed to change jobs only after an explicit scheduling request.

The installed `agentsdock_jobs.py` helper is the authoritative interface from
an agent turn. AgentsServer supplies its URL, bearer token, executable path,
and active chat ID through the child-process environment. Those values cannot
be overridden with CLI flags. The helper uses server-enforced chat-scoped
routes for `list`, `create`, `update`, and `delete`, and deliberately provides
no run-now command. For example, from an active agent process:

```bash
"$AGENTSDOCK_JOBS_CLI" list
"$AGENTSDOCK_JOBS_CLI" create --title "Daily status" \
  --prompt "Summarize the current project status." \
  --interval-seconds 86400 --loop
"$AGENTSDOCK_JOBS_CLI" create --title "Weekday status" \
  --prompt "Summarize the current project status." \
  --cron "0 9 * * MON-FRI" --timezone America/Los_Angeles
"$AGENTSDOCK_JOBS_CLI" update JOB_ID \
  --rrule "FREQ=WEEKLY;BYDAY=MO,WE,FR;BYHOUR=8;BYMINUTE=0;BYSECOND=0" \
  --timezone Europe/London
"$AGENTSDOCK_JOBS_CLI" delete JOB_ID
```

Cron accepts Vixie five-field expressions, aliases such as `@daily`, and
seconds-first six- or seven-field expressions (the seventh field is year).
Hashed `H` fields are stable per job; nondeterministic `R` fields are rejected.
RRULE accepts one RFC 5545 recurrence-rule property with an optional `RRULE:`
prefix. Explicit first-run timestamps run exactly once even when off-rule;
subsequent runs return to the calendar schedule. Missed occurrences are
skipped, retries do not move the canonical schedule, nonexistent DST times are
skipped, and ambiguous fall-back times run once.

Interval schedules are capped at ten years. RRULE `COUNT` is capped at 10,000,
and leap-second `BYSECOND=60` is rejected because the runtime clock cannot
represent second 60.

Agent-created jobs use the same job store as the desktop and mobile clients,
so they immediately appear in the Jobs panel. The scoped helper routes are:

- `GET/POST /api/sessions/{session_id}/jobs`
- `PATCH/DELETE /api/sessions/{session_id}/jobs/{job_id}`

## Workspace Files

The optional `workspace_files` health capability exposes a chat-scoped text
workspace rooted at that chat's exact `cwd`. It is additive to API contract v9,
so older clients continue to work without a global compatibility failure.

```text
GET /api/sessions/{session_id}/workspace
GET /api/sessions/{session_id}/workspace/entries?path=&offset=0&limit=500
GET /api/sessions/{session_id}/workspace/search?q=app&limit=100
GET /api/sessions/{session_id}/workspace/file?path=src/App.tsx
PUT /api/sessions/{session_id}/workspace/file
```

Reads accept UTF-8 regular files up to 2 MiB by default. Writes are atomic,
require the SHA-256 revision returned by the read endpoint, and reject stale
revisions, symlinks, special files, hard links, archived chats, and read-only
targets. Directory traversal is descriptor-relative and fails closed when the
host lacks secure no-follow file APIs. Configure the text limit with
`AGENTSDOCK_WORKSPACE_TEXT_MAX_BYTES`.

## Whole-History Search

`GET /api/search?q=<query>&limit=<chat-count>` searches user, assistant, error,
job, reasoning-summary, and file text across every chat. Quoted phrases remain
phrases; unquoted terms use prefix matching for responsive type-ahead search.

The first request incrementally builds `history_search.sqlite3` inside the
agent state directory. Each transcript stores its indexed byte offset, so later
requests ingest only newly appended JSONL records. The index is persistent and
safe across server restarts; a replaced or truncated transcript is rebuilt
automatically. Indexing runs in a worker thread and does not block agent turns.

## Per-Turn Code Review

For Git worktrees, the server snapshots the repository immediately before and
after each agent turn through an isolated temporary index. This captures the
complete turn-specific textual patch without modifying the user's real index
or folding pre-existing dirty changes into the review.

The append-only timeline stores only a compact `code_diff` event with file and
line-count metadata. Clients fetch the complete patch on demand:

```text
GET /api/sessions/{session_id}/diffs/{run_id}
```

The endpoint uses the same bearer-token authentication as the rest of the API.
Binary changes remain compact Git binary markers rather than being copied into
the event log.

## Public API Sketch

The server exposes JSON endpoints under `/api`.

- `GET /api/health`
- `GET /api/sessions`
- `GET /api/search`
- `POST /api/sessions`
- `PATCH /api/sessions/{session_id}`
- `GET /api/sessions/{session_id}/events`
- `GET /api/sessions/{session_id}/subagents`
- `POST /api/sessions/{session_id}/prompt`
- `POST /api/sessions/{session_id}/stop`
- `POST /api/sessions/{session_id}/fork`
- `POST /api/sessions/{session_id}/digest`
- `GET /api/sessions/{session_id}/files`
- `GET /api/sessions/{session_id}/diffs/{run_id}`
- `POST /api/sessions/{session_id}/upload`
- `GET /api/jobs`
- `POST /api/jobs`
- `PATCH /api/jobs/{job_id}`
- `DELETE /api/jobs/{job_id}`
- `GET /api/sessions/{session_id}/jobs`
- `POST /api/sessions/{session_id}/jobs`
- `PATCH /api/sessions/{session_id}/jobs/{job_id}`
- `DELETE /api/sessions/{session_id}/jobs/{job_id}`
- `GET /api/sessions/{session_id}/processes`
- `GET /api/sessions/{session_id}/tmux`
- `GET /api/runtime/catalog`
- `GET /api/admin/update`
- `POST /api/admin/update/check`
- `POST /api/admin/update/start`
- `GET /ws/sessions/{session_id}`

The event stream is append-only JSONL on disk and paged through the events API.
Large clients should page history instead of loading every event at once.
The subagents endpoint folds Claude local-agent lifecycle records into bounded
`subagent_state` snapshots without returning provider prompts, raw events,
tool-result output, commands, or output-file paths.

## Repository Hygiene

Before publishing:

```bash
python3 -m py_compile agent_server.py
rg -n 'private-host|/home/<name>|/Users/<name>|token-value' .
```

Do not commit:

- `~/.agentsdock` state (and the legacy `~/.zenithbot-agent` compatibility link)
- uploads or generated artifacts
- `.env` files or access tokens
- machine-specific hostnames, IP addresses, or user home paths
- compiled Python caches
