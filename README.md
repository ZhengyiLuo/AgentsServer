# ZenithBotServer

Standalone agent server for ZenithDock-style clients. It runs on the machine
that owns the CLI tools and workspace, exposes a small HTTP/WebSocket API, and
streams normalized Claude/Codex events, artifacts, uploads, scheduled jobs,
process inspection, and tmux pane capture to native clients.

This repository is intentionally server-only. It should not contain local chat
state, uploaded files, tokens, compiled caches, private hostnames, or personal
machine paths.

## What It Does

- Creates and resumes chat sessions for Claude and Codex CLI backends.
- Streams live agent events over WebSocket while preserving a JSONL event
  history on disk.
- Accepts file uploads and serves generated artifacts, including videos.
- Supports queued turns, stop requests, chat forking, context digests, and rough
  history import from provider sessions.
- Creates handoff digests with an actual LLM summarizer; the raw transcript/file
  pack is only internal source material.
- Runs recurring/loop jobs per chat with host load/memory guardrails.
- Provides optional live process and tmux-pane inspection for active work.
- Discovers available runtime models/efforts from the installed CLI tools when
  possible.

## Requirements

- Linux or macOS host with Python 3.10+.
- `uv` recommended for the runtime environment.
- Claude CLI and/or Codex CLI installed and authenticated on the agent host.
- `tmux` if you want terminal/session inspection.
- Tailscale on the agent host and each client device if you want to use the
  server from another Mac, iPhone, or iPad.
- Optional: a user-level `systemd` service on Linux.

## Quick Onboarding

1. Clone this repo on the machine that will run the agents.

```bash
git clone <repo-url>
cd ZenithBotServer
```

2. Create a Python environment.

```bash
uv venv
uv pip install fastapi uvicorn python-multipart pydantic
```

3. Install and authenticate the backend CLI tools you want to use.

ZenithBotServer does not bundle Claude or Codex. It shells out to the CLI tools
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
`ZENITHBOT_BACKEND` accordingly.

4. Start the server locally.

```bash
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

5. Check health from the server machine.

```bash
curl http://127.0.0.1:7850/api/health
```

6. Connect a ZenithDock client.

The ZenithDock app is distributed through TestFlight for macOS, iOS, and
iPadOS. Install the app there first, then configure the server URL and access
token in the app.

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
another private network, and set `ZENITHDOCK_AGENT_TOKEN` for shared-token
access control.

## Run Locally

```bash
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

Health check:

```bash
curl http://127.0.0.1:7850/api/health
```

The default state directory is `~/.zenithbot-agent`. Override it when you want
state somewhere else:

```bash
ZENITHBOT_AGENT_DIR=/path/to/state \
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

## Security

Set `ZENITHDOCK_AGENT_TOKEN` to require a shared bearer token for HTTP calls,
uploads, file/video fetches, and WebSocket streams.

```bash
export ZENITHDOCK_AGENT_TOKEN='replace-with-a-long-random-token'
uv run python agent_server.py serve --bind 0.0.0.0 --port 7850
```

Clients should send either:

```http
Authorization: Bearer replace-with-a-long-random-token
```

or the `X-ZenithDock-Token` header. Leave the variable unset only for trusted
local development.

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
tailnet, then set the ZenithDock server URL to:

```text
http://<tailscale-ip>:7850
```

If the browser can open `/api/health` but the app cannot connect, check:

- the client is also connected to Tailscale
- the URL includes the correct port
- the same `ZENITHDOCK_AGENT_TOKEN` is configured in the app
- the server is bound to `0.0.0.0` or the Tailscale interface, not only
  `127.0.0.1`

## Deployment

`deploy.sh` copies `agent_server.py` to a remote app directory, compiles it,
restarts the configured user service, and checks local health on the remote
host.

```bash
./deploy.sh <ssh-host>
```

Optional variables:

```bash
ZENITHDOCK_REMOTE_APP_DIR='Zenithbot' \
ZENITHDOCK_AGENT_SERVICE='zenithbot-agent.service' \
ZENITHDOCK_AGENT_TOKEN='replace-with-a-long-random-token' \
./deploy.sh <ssh-host>
```

The deploy helper writes to:

```text
<remote-app-dir>/scripts/agent_server.py
```

Create that directory on the remote host before the first deploy, and install a
matching user service. A template lives at
`systemd/zenithbot-agent.service.example`.

## Systemd User Service

Example install flow on the remote host:

```bash
mkdir -p ~/.config/systemd/user ~/Zenithbot/scripts
cp systemd/zenithbot-agent.service.example ~/.config/systemd/user/zenithbot-agent.service
systemctl --user daemon-reload
systemctl --user enable --now zenithbot-agent.service
```

Then check it:

```bash
systemctl --user status zenithbot-agent.service --no-pager -l
curl -H 'Authorization: Bearer replace-with-a-long-random-token' \
  http://127.0.0.1:7850/api/health
```

## Useful Configuration

Most settings are environment variables:

| Variable | Purpose | Default |
|---|---|---|
| `ZENITHBOT_AGENT_DIR` | Persistent session/job/file state directory | `~/.zenithbot-agent` |
| `ZENITHBOT_AGENT_CWD` | Default working directory for new sessions | user home |
| `ZENITHBOT_AGENT_BIND` | Bind address | `0.0.0.0` |
| `ZENITHBOT_AGENT_PORT` | Port | `7850` |
| `ZENITHDOCK_AGENT_TOKEN` | Shared bearer token | unset |
| `ZENITHBOT_BACKEND` | Default backend, `claude` or `codex` | `claude` |
| `CODEX_BIN` | Codex executable name/path | `codex` |
| `CLAUDE_PROJECTS_ROOT` | Claude history search root | `~/.claude/projects` |
| `CODEX_SESSIONS_ROOT` | Codex history search root | `~/.codex/sessions` |
| `ZENITHBOT_JOB_MAX_ACTIVE_RUNS` | Scheduled-job concurrency cap | `2` |
| `ZENITHBOT_MAX_ACTIVE_AGENT_RUNS` | Interactive agent concurrency cap | `10` |
| `ZENITHBOT_JOB_MIN_AVAILABLE_MEM_MB` | Job launch memory guardrail | `4096` |
| `ZENITHBOT_MIN_START_AVAILABLE_MEM_MB` | Interactive launch memory guardrail | `2048` |
| `ZENITHBOT_HANDOFF_DIGEST_BACKEND` | LLM backend for context digests, `claude` or `codex` | `claude` |
| `ZENITHBOT_HANDOFF_DIGEST_MODEL` | LLM model for context digests | `sonnet` |
| `ZENITHBOT_HANDOFF_DIGEST_EFFORT` | Optional digest reasoning/effort setting | unset |
| `ZENITHBOT_HANDOFF_DIGEST_TIMEOUT_SECONDS` | Digest summarizer timeout | `180` |
| `ZENITHBOT_HANDOFF_DIGEST_CHARS` | Final digest character cap | `56000` |

## Context Digests

`POST /api/sessions/{session_id}/digest` creates a real LLM-summarized handoff
for another chat. The server first builds a bounded source packet from recent
events and files, then asks the configured digest backend to summarize it into
a clean Markdown handoff. If the LLM summarizer fails, the endpoint fails
visibly instead of returning the raw source packet as if it were a digest.

By default, the digest summarizer uses Claude Sonnet:

```bash
ZENITHBOT_HANDOFF_DIGEST_BACKEND=claude
ZENITHBOT_HANDOFF_DIGEST_MODEL=sonnet
```

You can switch it to Codex or another installed CLI model, but the relevant CLI
must already be authenticated for the same Unix user that runs the service.

## Backend CLI Notes

The backend selection in ZenithDock only chooses which CLI the server invokes.
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

## Public API Sketch

The server exposes JSON endpoints under `/api`.

- `GET /api/health`
- `GET /api/sessions`
- `POST /api/sessions`
- `PATCH /api/sessions/{session_id}`
- `GET /api/sessions/{session_id}/events`
- `POST /api/sessions/{session_id}/prompt`
- `POST /api/sessions/{session_id}/stop`
- `POST /api/sessions/{session_id}/fork`
- `POST /api/sessions/{session_id}/digest`
- `GET /api/sessions/{session_id}/files`
- `POST /api/sessions/{session_id}/upload`
- `GET /api/sessions/{session_id}/processes`
- `GET /api/sessions/{session_id}/tmux`
- `GET /api/runtime/catalog`
- `GET /ws/sessions/{session_id}`

The event stream is append-only JSONL on disk and paged through the events API.
Large clients should page history instead of loading every event at once.

## Repository Hygiene

Before publishing:

```bash
python3 -m py_compile agent_server.py
rg -n 'private-host|/home/<name>|/Users/<name>|token-value' .
```

Do not commit:

- `~/.zenithbot-agent` state
- uploads or generated artifacts
- `.env` files or access tokens
- machine-specific hostnames, IP addresses, or user home paths
- compiled Python caches
