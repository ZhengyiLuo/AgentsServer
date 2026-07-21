#!/usr/bin/env python3
"""AgentsServer.

FastAPI service for a native Mac frontend. The server owns agent execution on
the agent host and streams normalized events from Claude Code / Codex CLI runs.

This intentionally mirrors the newest Slack bot's runner shape while removing
Slack-specific transport, formatting, and upload constraints.
"""

from __future__ import annotations

import argparse
import asyncio
import errno
import fcntl
import hashlib
import hmac
import json
import logging
import mmap
import os
import pty
import re
import shlex
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import termios
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
import uvicorn
import websockets

from update_runner import atomic_json as atomic_update_json
from update_runner import ReleaseUnavailableError, check_release, utc_now as update_utc_now

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 agent hosts
    tomllib = None

logger = logging.getLogger("agents-server")

BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
VALID_BACKENDS = {BACKEND_CLAUDE, BACKEND_CODEX}
CODEX_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
CODEX_EFFORT_ALIASES = {
    "extra high": "xhigh",
    "extra-high": "xhigh",
    "extra_high": "xhigh",
}

def env_setting(primary: str, default: str | None = None, *legacy: str) -> str | None:
    """Read a canonical setting while honoring pre-AgentsDock deployments."""
    for name in (primary, *legacy):
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def agentsdock_setting(suffix: str, default: str) -> str:
    value = env_setting(f"AGENTSDOCK_{suffix}", default, f"ZENITHBOT_{suffix}")
    return value if value is not None else default


def resolve_state_dir() -> Path:
    configured = env_setting(
        "AGENTSDOCK_STATE_DIR",
        None,
        "AGENTS_SERVER_STATE_DIR",
        "ZENITHBOT_AGENT_DIR",
    )
    if configured:
        return Path(configured).expanduser()

    canonical = Path.home() / ".agentsdock"
    legacy = Path.home() / ".zenithbot-agent"
    if canonical.exists() or not legacy.exists() or legacy.is_symlink():
        return canonical
    try:
        legacy.rename(canonical)
        legacy.symlink_to(canonical, target_is_directory=True)
        logger.info("migrated legacy state directory to %s", canonical)
        return canonical
    except OSError as exc:
        logger.warning("could not migrate legacy state directory %s: %s", legacy, exc)
        return legacy


STATE_DIR = resolve_state_dir()
SERVER_ROOT = Path(__file__).resolve().parent
SERVER_VERSION_FILE = SERVER_ROOT / "VERSION"
try:
    SERVER_VERSION = SERVER_VERSION_FILE.read_text().strip() or "development"
except OSError:
    SERVER_VERSION = "development"
SESSIONS_FILE = STATE_DIR / "sessions.json"
JOBS_FILE = STATE_DIR / "jobs.json"
HANDOFF_DIGEST_JOBS_FILE = STATE_DIR / "handoff_digest_jobs.json"
FILES_ROOT = STATE_DIR / "files"
CODE_DIFFS_ROOT = STATE_DIR / "code_diffs"
HOST_HEALTH_FILE = STATE_DIR / "host_health.jsonl"
SERVER_ADMIN_ROOT = STATE_DIR / "admin"
SERVER_UPDATE_STATUS_FILE = SERVER_ADMIN_ROOT / "server-update.json"
SERVER_UPDATE_LOG_FILE = SERVER_ADMIN_ROOT / "server-update.log"
SERVER_UPDATE_PUBLIC_KEY = SERVER_ROOT / "release-public-key.pem"
SERVER_UPDATE_RUNNER = SERVER_ROOT / "update_runner.py"
CLAUDE_PROJECTS_ROOT = Path(os.environ.get("CLAUDE_PROJECTS_ROOT", Path.home() / ".claude" / "projects"))
CODEX_SESSIONS_ROOT = Path(os.environ.get("CODEX_SESSIONS_ROOT", Path.home() / ".codex" / "sessions"))
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
CODEX_DEFAULT_MODEL = agentsdock_setting("CODEX_MODEL", "gpt-5.5").strip() or "gpt-5.5"
_configured_codex_effort = agentsdock_setting("CODEX_EFFORT", "xhigh").strip().lower() or "xhigh"
CODEX_DEFAULT_EFFORT = CODEX_EFFORT_ALIASES.get(_configured_codex_effort, _configured_codex_effort)
if CODEX_DEFAULT_EFFORT not in CODEX_EFFORTS:
    CODEX_DEFAULT_EFFORT = "xhigh"
DEFAULT_CWD = agentsdock_setting("AGENT_CWD", str(Path.home()))
DEFAULT_BACKEND = agentsdock_setting("BACKEND", BACKEND_CLAUDE).lower()
if DEFAULT_BACKEND not in VALID_BACKENDS:
    DEFAULT_BACKEND = BACKEND_CLAUDE

CODEX_FALLBACK_MODELS = (
    ("gpt-5.6-sol", "GPT-5.6-Sol"),
    ("gpt-5.6-terra", "GPT-5.6-Terra"),
    ("gpt-5.6-luna", "GPT-5.6-Luna"),
    ("gpt-5.5", "GPT-5.5"),
    ("gpt-5.4", "GPT-5.4"),
    ("gpt-5.4-mini", "GPT-5.4-Mini"),
)
CODEX_FALLBACK_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
CODEX_FALLBACK_MODEL_EFFORTS = {
    "gpt-5.6-sol": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-terra": ("low", "medium", "high", "xhigh", "max", "ultra"),
    "gpt-5.6-luna": ("low", "medium", "high", "xhigh", "max"),
    "gpt-5.5": ("low", "medium", "high", "xhigh"),
    "gpt-5.4": ("low", "medium", "high", "xhigh"),
    "gpt-5.4-mini": ("low", "medium", "high", "xhigh"),
}
CODEX_FALLBACK_SERVICE_TIERS = {
    "gpt-5.6-sol": "priority",
    "gpt-5.6-terra": "priority",
    "gpt-5.6-luna": "priority",
}

REQUEST_TIMEOUT_SECONDS = int(agentsdock_setting("REQUEST_TIMEOUT_SECONDS", "86400"))
CODEX_APP_SERVER_TIMEOUT_SECONDS = int(agentsdock_setting("CODEX_APP_SERVER_TIMEOUT_SECONDS", "30"))
CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS = int(agentsdock_setting("CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS", "120"))
RUNTIME_CATALOG_TIMEOUT_SECONDS = float(agentsdock_setting("RUNTIME_CATALOG_TIMEOUT_SECONDS", "6"))
RUNTIME_DIAGNOSTIC_TTL_SECONDS = float(agentsdock_setting("RUNTIME_DIAGNOSTIC_TTL_SECONDS", "60"))
JOB_SCHEDULER_INTERVAL_SECONDS = float(agentsdock_setting("JOB_SCHEDULER_INTERVAL_SECONDS", "5"))
JOB_BUSY_RETRY_SECONDS = int(agentsdock_setting("JOB_BUSY_RETRY_SECONDS", "60"))
# Zero means scheduled jobs have no scheduler-specific concurrency ceiling.
# The global agent-run guard and low-memory check still protect the machine.
JOB_MAX_ACTIVE_RUNS = int(agentsdock_setting("JOB_MAX_ACTIVE_RUNS", "0"))
JOB_MIN_AVAILABLE_MEM_MB = int(agentsdock_setting("JOB_MIN_AVAILABLE_MEM_MB", "4096"))
JOB_DEFER_EVENT_MIN_SECONDS = int(agentsdock_setting("JOB_DEFER_EVENT_MIN_SECONDS", "300"))
MAX_ACTIVE_AGENT_RUNS = int(agentsdock_setting("MAX_ACTIVE_AGENT_RUNS", "10"))
MIN_START_AVAILABLE_MEM_MB = int(agentsdock_setting("MIN_START_AVAILABLE_MEM_MB", "2048"))
HOST_MONITOR_INTERVAL_SECONDS = float(agentsdock_setting("HOST_MONITOR_INTERVAL_SECONDS", "15"))
HOST_HEALTH_MAX_BYTES = int(agentsdock_setting("HOST_HEALTH_MAX_BYTES", str(20 * 1024 * 1024)))
IDLE_WARN_SECONDS = int(agentsdock_setting("IDLE_WARN_SECONDS", "1800"))
IDLE_KILL_SECONDS = int(agentsdock_setting("IDLE_KILL_SECONDS", "21600"))
STOP_GRACE_SECONDS = float(agentsdock_setting("STOP_GRACE_SECONDS", "2.0"))
PROCESS_STREAM_LIMIT = int(agentsdock_setting("PROCESS_STREAM_LIMIT", str(16 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(agentsdock_setting("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024 * 1024)))
MAX_IMPORT_MESSAGES = int(agentsdock_setting("HISTORY_IMPORT_LIMIT", "400"))
MAX_IMPORTED_TEXT_CHARS = int(agentsdock_setting("HISTORY_IMPORT_TEXT_CHARS", "12000"))
MAX_FORK_MEMORY_CHARS = int(agentsdock_setting("FORK_MEMORY_CHARS", "24000"))
MAX_FORK_MEMORY_ITEM_CHARS = int(agentsdock_setting("FORK_MEMORY_ITEM_CHARS", "1800"))
MAX_HANDOFF_DIGEST_CHARS = int(agentsdock_setting("HANDOFF_DIGEST_CHARS", "56000"))
MAX_SESSION_SYSTEM_PROMPT_CHARS = int(agentsdock_setting("SESSION_SYSTEM_PROMPT_CHARS", "24000"))
HANDOFF_DIGEST_TIMEOUT_SECONDS = int(agentsdock_setting("HANDOFF_DIGEST_TIMEOUT_SECONDS", "180"))
HANDOFF_DIGEST_BACKEND = agentsdock_setting("HANDOFF_DIGEST_BACKEND", BACKEND_CLAUDE).lower()
if HANDOFF_DIGEST_BACKEND not in VALID_BACKENDS:
    HANDOFF_DIGEST_BACKEND = BACKEND_CLAUDE
HANDOFF_DIGEST_MODEL = agentsdock_setting("HANDOFF_DIGEST_MODEL", "sonnet").strip()
HANDOFF_DIGEST_EFFORT = agentsdock_setting("HANDOFF_DIGEST_EFFORT", "").strip()
DEFAULT_SESSION_EVENT_LIMIT = int(agentsdock_setting("SESSION_EVENT_LIMIT", "100"))
MAX_EVENT_RESPONSE_LIMIT = int(agentsdock_setting("MAX_EVENT_RESPONSE_LIMIT", "1000"))
AGENT_TOKEN = env_setting(
    "AGENTSDOCK_AGENT_TOKEN",
    "",
    "ZENITHDOCK_AGENT_TOKEN",
    "ZENITHBOT_AGENT_TOKEN",
) or ""
SERVER_BIND_ADDRESS = agentsdock_setting("AGENT_BIND", "0.0.0.0")
SERVER_PORT = int(agentsdock_setting("AGENT_PORT", "7850"))
API_CONTRACT_VERSION = 7
SESSION_ORDER_STEP = 1000.0
CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS = int(agentsdock_setting("CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS", "120"))

SYSTEM_PROMPT = """\
You are responding through AgentsDock, backed by AgentsServer.

Use concise Markdown. Prefer clear sections, bullets, code fences, and direct
answers. The UI renders rich traces separately, so do not narrate every tool
call unless it matters to the user.
Avoid Markdown heading markers like `#`, `##`, or `###` in ordinary answers.
Use short bold labels such as `**What changed**` when a section label helps.
Do not use emoji, Slack-style emoji aliases, or decorative status prefixes
such as :mag:, :gear:, :rocket:, or :white_check_mark:.

Math and scientific notation:
- Write inline LaTeX as `$...$` and display equations as `$$...$$`.
- Keep prose outside the math delimiters and use fenced code blocks only when
  showing literal TeX source that should not be rendered.
- Prefer the dollar delimiters above instead of `\\(...\\)` or `\\[...\\]` so
  equations render consistently in the chat timeline.

Tool and inspection errors:
- Failed commands, malformed JSON reads, missing files, missing Python aliases,
  and other inspection mistakes are normal debugging signals, not stopping
  conditions.
- Do not end your turn just because a tool command failed. Read stderr/stdout,
  correct the command, and try a safer alternative such as `python3`, `jq`,
  `python3 -m json.tool`, `rg`, `sed`, `head`, `tail`, or a small script.
- If the likely fix is non-intrusive, do it yourself and continue. Examples:
  command typos, wrong JSON/file-reading command, missing `python` alias, small
  parser adjustments, read-only inspection changes, or narrow code edits the
  user already asked for.
- Continue until you can answer the user's request, complete the requested
  change, or identify a real blocker. Stop only if retrying would be
  destructive, removes/overwrites unrelated work, requires missing
  credentials/approval, or the user explicitly asked only for diagnosis.

Skills and environment playbooks:
- Before saying cluster paths such as `/mnt/amlfs-07` are unavailable, check the
  available local skills, memories, and playbooks for matching instructions.
  Look first under `~/.codex/skills`, `~/.claude/skills`, `~/.claude/agents`,
  project `AGENTS.md`/`CLAUDE.md`, and relevant `~/.claude/projects/.../memory`
  notes.
- For AMLFS/OSMO access, prefer the installed OSMO/SONIC skills and memories
  such as `osmo`, `osmo-exec`, `sonic`, `ssh-portforward`, and
  `reference-osmo-amlfs-ssh`. Follow those instructions before substituting
  local data or claiming the mount cannot be reached.
- If the needed skill/playbook is missing, blocked, or fails, say exactly what
  you checked and what blocked you.

Turn lifecycle and background work:
- This is not a persistent live chat process. Your Claude process ends when the
  current turn finishes.
- Claude Code's native Agent tool is available for bounded parallel work. A
  subagent may monitor a render or process while you do other work, but it is
  still owned by this parent turn.
- If you launch a subagent with `run_in_background: true`, keep its task ID and
  join it with `TaskOutput` using a blocking wait before you finish. Never leave
  a child task unjoined and never imply it will report after this process exits.
- If the user requested the output of a render, sweep, conversion, or other
  launched process, stay alive in this turn until it completes, inspect the
  result, publish the artifacts through the manifest, and then answer. Use a
  foreground subagent, a background subagent plus blocking `TaskOutput`, or
  bounded polling/waits in the parent.
- Do not say "monitor armed", "when the watcher fires I'll send it", "I'll
  check back", or similar future-tense promises and then end the turn.
- A tmux process or shell watcher can keep computation alive, but it cannot
  resume this Claude turn or make AgentsDock ingest a manifest after the turn
  has ended.
- If background monitoring is needed, create a real durable mechanism: a
  AgentsDock scheduled job that launches a later agent turn, a system service,
  or a script the user can inspect and run. State exactly what you created and
  how to inspect or stop it.
- If you did not create a durable mechanism, say that the user should ask again
  later instead of implying you will keep running.

Code changes and diffs:
- Validate code changes normally, but do not print a full repository diff just
  for AgentsDock. The server captures the complete per-turn Git diff directly.
- A short `git diff --stat` is useful when it helps explain validation. Keep the
  final answer focused unless the user explicitly asks to see the patch inline.

Files and artifacts:
- User uploads are available as local paths in the prompt.
- This is not Slack. Do not call Slack upload APIs or Slack file helpers.
- If your response creates files the user should receive, write a JSON manifest
  at exactly this path:
  {manifest_path}
- Manifest format:
  {{"files": ["/absolute/path/to/file.ext", {{"path": "/absolute/path/video.mp4", "title": "Demo", "text": "Optional note"}}]}}
- Include images, videos, PDFs, CSVs, notebooks, archives, logs, and documents
  that the user would reasonably want to preview or save.
- Do not include source files unless the user explicitly asks for them.
- Use absolute file paths. Videos should be normal playable files such as mp4
  or mov. If `python` is not installed, use `python3` or shell tools to write
  files and the manifest.

Persistent chat terminal:
- This chat's AgentsDock terminal is the tmux session named
  `{terminal_session}` on this same host.
- The terminal is a separate interactive shell. Its screen and user input are
  not automatically included in your context.
- When terminal state is relevant, inspect it read-only with
  `tmux capture-pane -p -J -t "$AGENTSDOCK_TMUX_SESSION" -S -200` and inspect
  windows with `tmux list-windows -t "$AGENTSDOCK_TMUX_SESSION"`.
- Do not send keys, resize panes, close windows, or kill the terminal session
  unless the user explicitly asks you to operate on it.
"""

CODEX_PROMPT_PRELUDE = """\
[AgentsDock context]
You are responding through AgentsDock, backed by AgentsServer.

Use concise Markdown. The UI renders tool calls, command output, reasoning
summaries, and artifacts separately, so keep the final answer focused.
Do not use emoji, Slack-style emoji aliases, or decorative status prefixes
such as :mag:, :gear:, :rocket:, or :white_check_mark:.

Math and scientific notation:
- Write inline LaTeX as `$...$` and display equations as `$$...$$`.
- Keep prose outside the math delimiters and use fenced code blocks only when
  showing literal TeX source that should not be rendered.
- Prefer the dollar delimiters above instead of `\\(...\\)` or `\\[...\\]` so
  equations render consistently in the chat timeline.

Tool and inspection errors:
- Failed commands, malformed JSON reads, missing files, missing Python aliases,
  and other inspection mistakes are normal debugging signals, not stopping
  conditions.
- Do not end your turn just because a tool command failed. Read stderr/stdout,
  correct the command, and try a safer alternative such as `python3`, `jq`,
  `python3 -m json.tool`, `rg`, `sed`, `head`, `tail`, or a small script.
- If the likely fix is non-intrusive, do it yourself and continue. Examples:
  command typos, wrong JSON/file-reading command, missing `python` alias, small
  parser adjustments, read-only inspection changes, or narrow code edits the
  user already asked for.
- Continue until you can answer the user's request, complete the requested
  change, or identify a real blocker. Stop only if retrying would be
  destructive, removes/overwrites unrelated work, requires missing
  credentials/approval, or the user explicitly asked only for diagnosis.

Skills and environment playbooks:
- Before saying cluster paths such as `/mnt/amlfs-07` are unavailable, check the
  available local skills, memories, and playbooks for matching instructions.
  Look first under `~/.codex/skills`, `~/.claude/skills`, `~/.claude/agents`,
  project `AGENTS.md`/`CLAUDE.md`, and relevant `~/.claude/projects/.../memory`
  notes.
- For AMLFS/OSMO access, prefer the installed OSMO/SONIC skills and memories
  such as `osmo`, `osmo-exec`, `sonic`, `ssh-portforward`, and
  `reference-osmo-amlfs-ssh`. Follow those instructions before substituting
  local data or claiming the mount cannot be reached.
- If the needed skill/playbook is missing, blocked, or fails, say exactly what
  you checked and what blocked you.

This is AgentsDock, not Slack. Do not call Slack upload APIs or Slack file
helpers. Create files locally on the agent host and publish them through the manifest.

If you create files the user should receive, write a JSON manifest at exactly:
{manifest_path}

Manifest format:
{{"files": ["/absolute/path/to/file.ext", {{"path": "/absolute/path/video.mp4", "title": "Demo", "text": "Optional note"}}]}}

Use absolute file paths. Videos should be normal playable files such as mp4 or
mov. If `python` is not installed, use `python3` or shell tools to write files
and the manifest.

Persistent chat terminal:
- This chat's AgentsDock terminal is the tmux session named
  `{terminal_session}` on this same host.
- The terminal is a separate interactive shell. Its screen and user input are
  not automatically included in your context.
- When terminal state is relevant, inspect it read-only with
  `tmux capture-pane -p -J -t "$AGENTSDOCK_TMUX_SESSION" -S -200` and inspect
  windows with `tmux list-windows -t "$AGENTSDOCK_TMUX_SESSION"`.
- Do not send keys, resize panes, close windows, or kill the terminal session
  unless the user explicitly asks you to operate on it.

User prompt follows.
]

"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_job_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    clean = str(value).strip()
    if not clean:
        return None
    with suppress(ValueError):
        return float(clean)
    normalized = clean[:-1] + "+00:00" if clean.endswith("Z") else clean
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid job timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def safe_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_", ".", " ") else "_" for c in name)
    return cleaned.strip(" .") or "file"


def session_dir(session_id: str) -> Path:
    return STATE_DIR / "sessions" / session_id


def events_path(session_id: str) -> Path:
    return session_dir(session_id) / "events.jsonl"


def uploads_dir(session_id: str) -> Path:
    return session_dir(session_id) / "uploads"


def manifests_dir(session_id: str) -> Path:
    return session_dir(session_id) / "manifests"


def code_diffs_dir(session_id: str) -> Path:
    return CODE_DIFFS_ROOT / session_id


def existing_cwd(requested: str | None) -> str:
    candidates = [requested, DEFAULT_CWD, str(Path.home()), "/tmp"]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        if path.is_dir():
            return str(path)
    return "/tmp"


def server_identity() -> str:
    machine = ""
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        with suppress(Exception):
            machine = path.read_text(encoding="utf-8").strip()
            if machine:
                break
    if not machine:
        machine = os.uname().nodename
    payload = f"{machine}|{STATE_DIR.resolve()}".encode("utf-8", errors="ignore")
    return hashlib.sha256(payload).hexdigest()[:24]


def ensure_dirs(session_id: str | None = None) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FILES_ROOT.mkdir(parents=True, exist_ok=True)
    CODE_DIFFS_ROOT.mkdir(parents=True, exist_ok=True)
    if session_id:
        session_dir(session_id).mkdir(parents=True, exist_ok=True)
        uploads_dir(session_id).mkdir(parents=True, exist_ok=True)
        manifests_dir(session_id).mkdir(parents=True, exist_ok=True)
        code_diffs_dir(session_id).mkdir(parents=True, exist_ok=True)


def _git_command(
    repo_root: str,
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    stdout: Any = subprocess.PIPE,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", "-C", repo_root, *args],
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=CODE_DIFF_SNAPSHOT_TIMEOUT_SECONDS,
        check=False,
    )


def _capture_git_tree(session_id: str, run_id: str, cwd: str) -> dict[str, str] | None:
    """Snapshot the worktree through a temporary index without touching the real index."""
    try:
        probe = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    repo_root = probe.stdout.strip()
    if probe.returncode != 0 or not repo_root:
        return None

    ensure_dirs(session_id)
    index_path = code_diffs_dir(session_id) / f".{safe_name(run_id)}-{uuid.uuid4().hex}.index"
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index_path)
    try:
        seeded_from_index = False
        index_probe = _git_command(repo_root, ["rev-parse", "--git-path", "index"])
        source_index = Path(index_probe.stdout.strip()) if index_probe.returncode == 0 else Path()
        if source_index and not source_index.is_absolute():
            source_index = Path(repo_root) / source_index
        if source_index.is_file():
            shutil.copy2(source_index, index_path)
            seeded_from_index = True
        else:
            head = _git_command(repo_root, ["rev-parse", "--verify", "HEAD"])
            read_args = ["read-tree", head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else ["read-tree", "--empty"]
            if _git_command(repo_root, read_args, env=env).returncode != 0:
                return None
        staged = _git_command(repo_root, ["add", "-A", "--", "."], env=env)
        if staged.returncode != 0 and seeded_from_index:
            with suppress(OSError):
                index_path.unlink()
            head = _git_command(repo_root, ["rev-parse", "--verify", "HEAD"])
            read_args = ["read-tree", head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else ["read-tree", "--empty"]
            if _git_command(repo_root, read_args, env=env).returncode == 0:
                staged = _git_command(repo_root, ["add", "-A", "--", "."], env=env)
        if staged.returncode != 0:
            logger.info(
                "code diff snapshot skipped session=%s run=%s: %s",
                session_id,
                run_id,
                staged.stderr.strip()[:500],
            )
            return None
        tree = _git_command(repo_root, ["write-tree"], env=env)
        tree_id = tree.stdout.strip()
        if tree.returncode != 0 or not tree_id:
            return None
        return {"repo_root": repo_root, "tree": tree_id}
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("code diff snapshot failed session=%s run=%s error=%s", session_id, run_id, exc)
        return None
    finally:
        with suppress(OSError):
            index_path.unlink()
        with suppress(OSError):
            Path(f"{index_path}.lock").unlink()


def _parse_git_numstat(source: str) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for raw_line in source.splitlines():
        parts = raw_line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path = parts
        added = int(added_raw) if added_raw.isdigit() else None
        deleted = int(deleted_raw) if deleted_raw.isdigit() else None
        files.append({
            "path": path.strip(),
            "additions": added,
            "deletions": deleted,
            "binary": added is None or deleted is None,
        })
    return files


def _patch_changed_paths(source: str) -> set[str]:
    paths: set[str] = set()
    # Codex's orchestration tool embeds apply_patch in a JavaScript string, so
    # patch newlines arrive as literal `\\n` sequences rather than line breaks.
    expanded = str(source or "").replace("\\r\\n", "\n").replace("\\n", "\n")
    for raw_line in expanded.splitlines():
        line = raw_line.strip()
        match = re.match(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", line)
        if match:
            paths.add(match.group(1).rstrip("\\").strip())
            continue
        match = re.match(r"^\*\*\* Move to:\s*(.+?)\s*$", line)
        if match:
            paths.add(match.group(1).rstrip("\\").strip())
            continue
        match = re.match(r"^(?:---|\+\+\+)\s+(?:[ab]/)?(.+?)\s*$", line)
        if match and match.group(1) != "/dev/null":
            paths.add(match.group(1).rstrip("\\").strip())
    return paths


def tool_changed_paths(tool: dict[str, Any]) -> set[str]:
    """Return only paths explicitly owned by a mutating provider tool call."""
    name = str(tool.get("name") or "").strip().lower().replace("-", "_")
    tool_input = tool.get("input") if isinstance(tool.get("input"), dict) else {}
    paths: set[str] = set()
    direct_path_tools = {
        "edit", "write", "multiedit", "multi_edit", "notebookedit", "notebook_edit",
        "str_replace_editor", "create_file", "delete_file",
    }
    if name in direct_path_tools:
        for key in ("file_path", "path", "notebook_path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                paths.add(value.strip())
    if name in {"apply_patch", "applypatch", "patch"}:
        patch = tool_input.get("patch") or tool_input.get("input") or tool_input.get("value")
        if isinstance(patch, str):
            paths.update(_patch_changed_paths(patch))
    elif name in {"exec", "run_javascript", "javascript"}:
        for key in ("value", "code", "script", "input"):
            source = tool_input.get(key)
            if isinstance(source, str) and "apply_patch" in source and "***" in source:
                paths.update(_patch_changed_paths(source))
    return paths


def _normalize_changed_paths(paths: set[str], repo_root: str, cwd: str) -> list[str]:
    root = Path(repo_root).expanduser().resolve()
    working = Path(cwd).expanduser().resolve()
    normalized: set[str] = set()
    for raw in paths:
        clean = str(raw or "").strip().strip("\"'`")
        if not clean or clean == "/dev/null":
            continue
        if clean.startswith("a/") or clean.startswith("b/"):
            clean = clean[2:]
        candidate = Path(clean).expanduser()
        candidates = [candidate] if candidate.is_absolute() else [working / candidate, root / candidate]
        for resolved in candidates:
            try:
                relative = resolved.resolve().relative_to(root)
            except (OSError, ValueError):
                continue
            if relative.parts and ".." not in relative.parts:
                normalized.add(relative.as_posix())
                break
    return sorted(normalized)


def _write_turn_code_diff(
    session_id: str,
    run_id: str,
    backend: str,
    baseline: dict[str, str],
    cwd: str,
    changed_paths: set[str],
) -> dict[str, Any] | None:
    current = _capture_git_tree(session_id, run_id, cwd)
    if not current or current.get("repo_root") != baseline.get("repo_root"):
        return None
    base_tree = str(baseline.get("tree") or "")
    current_tree = str(current.get("tree") or "")
    if not base_tree or not current_tree or base_tree == current_tree:
        return None

    repo_root = str(current["repo_root"])
    attributed_paths = _normalize_changed_paths(changed_paths, repo_root, cwd)
    if not attributed_paths:
        logger.info(
            "code diff skipped session=%s run=%s: no agent-owned edit paths",
            session_id,
            run_id,
        )
        return None
    diff_root = code_diffs_dir(session_id)
    patch_path = diff_root / f"{safe_name(run_id)}.patch"
    patch_tmp = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.patch"
    try:
        with patch_tmp.open("w", encoding="utf-8") as output:
            patch = _git_command(
                repo_root,
                ["diff", "--no-ext-diff", "--no-textconv", "--find-renames", "--unified=3", base_tree, current_tree, "--", *attributed_paths],
                stdout=output,
            )
        if patch.returncode != 0 or patch_tmp.stat().st_size == 0:
            with suppress(OSError):
                patch_tmp.unlink()
            return None
        patch_tmp.replace(patch_path)
        numstat = _git_command(repo_root, ["diff", "--numstat", "--find-renames", base_tree, current_tree, "--", *attributed_paths])
        files = _parse_git_numstat(numstat.stdout if numstat.returncode == 0 else "")
        additions = sum(int(item["additions"] or 0) for item in files)
        deletions = sum(int(item["deletions"] or 0) for item in files)
        metadata = {
            "run_id": run_id,
            "backend": backend,
            "repository_root": repo_root,
            "diff_files": files,
            "files_changed": len(files),
            "additions": additions,
            "deletions": deletions,
            "byte_count": patch_path.stat().st_size,
            "created_at": now_iso(),
            "attribution": "agent_tool_paths",
            "attributed_paths": attributed_paths,
        }
        metadata_path = diff_root / f"{safe_name(run_id)}.json"
        metadata_tmp = diff_root / f".{safe_name(run_id)}-{uuid.uuid4().hex}.json"
        metadata_tmp.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        metadata_tmp.replace(metadata_path)
        return metadata
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("code diff capture failed session=%s run=%s error=%s", session_id, run_id, exc)
        with suppress(OSError):
            patch_tmp.unlink()
        return None


async def capture_git_baseline(session_id: str, run_id: str, cwd: str) -> dict[str, str] | None:
    return await asyncio.to_thread(_capture_git_tree, session_id, run_id, cwd)


async def publish_turn_code_diff(
    session_id: str,
    run_id: str,
    backend: str,
    cwd: str,
    baseline: dict[str, str] | None,
    changed_paths: set[str],
) -> None:
    if not baseline:
        return
    metadata = await asyncio.to_thread(_write_turn_code_diff, session_id, run_id, backend, baseline, cwd, changed_paths)
    if metadata:
        await append_event(session_id, "code_diff", metadata)


EVENT_SEQ_CACHE: dict[str, int] = {}
EVENT_SEQ_LOCK = asyncio.Lock()
TIMELINE_INDEX_CACHE_MAX = int(agentsdock_setting("TIMELINE_INDEX_CACHE_MAX", "24"))
TIMELINE_INDEX_CACHE: OrderedDict[str, dict[str, Any]] = OrderedDict()
TIMELINE_INDEX_LOCKS: dict[str, threading.Lock] = {}
HISTORY_SEARCH_DB = STATE_DIR / "history_search.sqlite3"
HISTORY_SEARCH_LOCK = threading.Lock()
HISTORY_SEARCH_DIRTY: set[str] = set()
HISTORY_SEARCH_SYNC_INTERVAL_SECONDS = max(
    0.25, float(agentsdock_setting("HISTORY_SEARCH_SYNC_INTERVAL_SECONDS", "1.0"))
)
HISTORY_SEARCH_FULL_SYNC_INTERVAL_SECONDS = max(
    30.0, float(agentsdock_setting("HISTORY_SEARCH_FULL_SYNC_INTERVAL_SECONDS", "300"))
)


def last_event_seq_from_file(path: Path) -> int:
    """Read the last JSONL event seq without scanning huge chat histories."""
    if not path.exists():
        return 0
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= 0:
                return 0
            buffer = b""
            pos = size
            read_total = 0
            max_backscan = min(size, 16 * 1024 * 1024)
            while pos > 0 and read_total < max_backscan:
                chunk_size = min(pos, 256 * 1024, max_backscan - read_total)
                pos -= chunk_size
                f.seek(pos)
                buffer = f.read(chunk_size) + buffer
                read_total += chunk_size
                lines = buffer.splitlines()
                if pos > 0 and buffer and not buffer.startswith(b"\n"):
                    lines = lines[1:]
                for raw_line in reversed(lines):
                    if not raw_line.strip():
                        continue
                    with suppress(Exception):
                        event = json.loads(raw_line.decode("utf-8", "replace"))
                        return int(event.get("seq") or 0)
    except Exception as exc:
        logger.warning("failed to read last event seq for %s: %s", path, exc)
    return 0


async def next_event_seq(session_id: str, path: Path) -> int:
    async with EVENT_SEQ_LOCK:
        seq = EVENT_SEQ_CACHE.get(session_id)
        if seq is None:
            seq = await asyncio.to_thread(last_event_seq_from_file, path)
        seq += 1
        EVENT_SEQ_CACHE[session_id] = seq
        return seq


async def forget_event_seq(session_id: str) -> None:
    async with EVENT_SEQ_LOCK:
        EVENT_SEQ_CACHE.pop(session_id, None)
    TIMELINE_INDEX_CACHE.pop(session_id, None)
    TIMELINE_INDEX_LOCKS.pop(session_id, None)


def token_matches(candidate: str | None) -> bool:
    if not AGENT_TOKEN:
        return True
    if not candidate:
        return False
    return hmac.compare_digest(candidate, AGENT_TOKEN)


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return None


def request_authorized(request: Request) -> bool:
    if not AGENT_TOKEN:
        return True
    return (
        token_matches(bearer_token(request.headers.get("authorization")))
        or token_matches(request.headers.get("x-agentsdock-token"))
        or token_matches(request.headers.get("x-zenithdock-token"))
        or token_matches(request.query_params.get("token"))
    )


def websocket_authorized(ws: WebSocket) -> bool:
    if not AGENT_TOKEN:
        return True
    return (
        token_matches(bearer_token(ws.headers.get("authorization")))
        or token_matches(ws.headers.get("x-agentsdock-token"))
        or token_matches(ws.headers.get("x-zenithdock-token"))
        or token_matches(ws.query_params.get("token"))
    )


class CreateSessionRequest(BaseModel):
    title: str | None = None
    folder: str | None = None
    cwd: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    system_prompt: str | None = Field(default=None, max_length=MAX_SESSION_SYSTEM_PROMPT_CHARS)
    pinned: bool | None = None
    archived: bool | None = None
    provider_session_id: str | None = None
    session_id: str | None = None
    claude_session_id: str | None = None
    codex_thread_id: str | None = None
    import_history: bool | None = None


class UpdateSessionRequest(BaseModel):
    title: str | None = None
    folder: str | None = None
    cwd: str | None = None
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    system_prompt: str | None = Field(default=None, max_length=MAX_SESSION_SYSTEM_PROMPT_CHARS)
    pinned: bool | None = None
    archived: bool | None = None


class ReorderSessionRequest(BaseModel):
    direction: str | None = None
    target_id: str | None = None
    placement: str | None = None


class ReadSessionRequest(BaseModel):
    last_read_agent_event_seq: int | None = None


class TurnRequest(BaseModel):
    prompt: str
    file_ids: list[str] = Field(default_factory=list)
    backend: str | None = None
    model: str | None = None
    effort: str | None = None
    display_prompt: str | None = None
    purpose: str | None = None
    job_id: str | None = None
    job_title: str | None = None
    digest_job_id: str | None = None
    digest_detail: str | None = None
    source_session_id: str | None = None
    target_session_id: str | None = None
    steer_interrupted_run_id: str | None = None


class UpdateQueuedTurnRequest(BaseModel):
    prompt: str | None = None
    file_ids: list[str] | None = None


class MoveQueuedTurnRequest(BaseModel):
    direction: str


class ForkSessionRequest(BaseModel):
    title: str | None = None


class ImportHistoryRequest(BaseModel):
    force: bool = False
    limit: int | None = None


class HandoffDigestRequest(BaseModel):
    detail: str = "normal"
    user_prompt: str | None = None
    target_session_id: str | None = None
    summarizer_backend: str | None = None
    summarizer_model: str | None = None
    summarizer_effort: str | None = None


class HandoffDigestSendRequest(HandoffDigestRequest):
    target_session_id: str


class TerminalOpenRequest(BaseModel):
    cwd: str | None = None


class TerminalInputRequest(BaseModel):
    text: str | None = None
    enter: bool = True
    key: str | None = None


class TerminalResizeRequest(BaseModel):
    columns: int = 100
    rows: int = 30


class TerminalActionRequest(BaseModel):
    action: str
    target: str | None = None


class CreateJobRequest(BaseModel):
    session_id: str
    title: str
    prompt: str
    interval_seconds: int | None = None
    first_run_at: str | None = None
    loop: bool = False
    max_runs: int | None = None
    enabled: bool = True
    backend: str | None = None


def normalize_runtime_effort(backend: str, effort: Any, *, strict: bool = False) -> str | None:
    clean = str(effort or "").strip().lower()
    if not clean:
        return None
    if str(backend or "").strip().lower() != BACKEND_CODEX:
        return clean

    normalized = CODEX_EFFORT_ALIASES.get(clean, clean)
    if normalized in CODEX_EFFORTS:
        return normalized
    if strict:
        supported = ", ".join(sorted(CODEX_EFFORTS))
        raise HTTPException(status_code=400, detail=f"Codex effort must be one of: {supported}")
    logger.warning("dropping unsupported Codex effort value=%r", effort)
    return None


def clean_session_system_prompt(value: Any) -> str | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    if len(clean) > MAX_SESSION_SYSTEM_PROMPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"system prompt must be at most {MAX_SESSION_SYSTEM_PROMPT_CHARS} characters",
        )
    return clean


class UpdateJobRequest(BaseModel):
    title: str | None = None
    prompt: str | None = None
    interval_seconds: int | None = None
    next_run_at: str | None = None
    loop: bool | None = None
    max_runs: int | None = None
    enabled: bool | None = None
    backend: str | None = None


class ServerUpdateRequest(BaseModel):
    version: str | None = None


def session_folder(sess: dict[str, Any]) -> str:
    folder = str(sess.get("folder") or "General").strip()
    return folder or "General"


def session_order_value(sess: dict[str, Any]) -> float:
    value = sess.get("sort_order")
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def session_section_key(sess: dict[str, Any]) -> tuple[str, str]:
    if bool(sess.get("archived")):
        return ("archived", "")
    if bool(sess.get("pinned")):
        return ("pinned", "")
    return ("folder", session_folder(sess))


def legacy_session_sort_key(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = list(sessions)
    ordered.sort(key=lambda s: s.get("updated_at") or "", reverse=True)
    ordered.sort(key=lambda s: not bool(s.get("pinned")))
    ordered.sort(key=lambda s: bool(s.get("archived")))
    return ordered


def sorted_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        sessions,
        key=lambda s: (
            bool(s.get("archived")),
            not bool(s.get("pinned")) if not bool(s.get("archived")) else False,
            "" if bool(s.get("archived")) or bool(s.get("pinned")) else session_folder(s).casefold(),
            session_order_value(s),
            str(s.get("created_at") or ""),
            str(s.get("id") or ""),
        ),
    )


class SessionStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.sessions: dict[str, dict[str, Any]] = {}

    async def load(self) -> None:
        ensure_dirs()
        runtime_changed = False
        if SESSIONS_FILE.exists():
            try:
                self.sessions = json.loads(SESSIONS_FILE.read_text())
            except Exception as e:
                logger.warning("failed to load sessions: %s", e)
                self.sessions = {}
        for sess in self.sessions.values():
            backend = str(sess.get("backend") or DEFAULT_BACKEND).strip().lower()
            previous_effort = sess.get("effort")
            normalized_effort = normalize_runtime_effort(backend, previous_effort)
            if normalized_effort != previous_effort:
                sess["effort"] = normalized_effort
                runtime_changed = True
        await self.ensure_sort_orders()
        if runtime_changed:
            await self.save()

    async def save(self) -> None:
        ensure_dirs()
        tmp = SESSIONS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.sessions, indent=2))
        tmp.replace(SESSIONS_FILE)

    async def ensure_sort_orders(self) -> None:
        changed = False
        for index, sess in enumerate(legacy_session_sort_key(list(self.sessions.values()))):
            if sess.get("sort_order") is None:
                sess["sort_order"] = (index + 1) * SESSION_ORDER_STEP
                changed = True
        if changed:
            await self.save()

    def top_order_for_section(self, section: tuple[str, str], *, excluding_id: str | None = None) -> float:
        section_orders = [
            session_order_value(sess)
            for sess in self.sessions.values()
            if session_section_key(sess) == section
            and sess.get("id") != excluding_id
            and sess.get("sort_order") is not None
        ]
        if not section_orders:
            return SESSION_ORDER_STEP
        return min(section_orders) - SESSION_ORDER_STEP

    async def create(self, req: CreateSessionRequest, *, parent_id: str | None = None) -> dict[str, Any]:
        backend = (req.backend or DEFAULT_BACKEND).lower()
        if backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
        sid = f"sess_{uuid.uuid4().hex[:16]}"
        ensure_dirs(sid)
        now = now_iso()
        provider_id = req.provider_session_id or req.session_id
        claude_session_id = req.claude_session_id or (provider_id if backend == BACKEND_CLAUDE else None)
        codex_thread_id = req.codex_thread_id or (provider_id if backend == BACKEND_CODEX else None)
        active_provider_id = claude_session_id if backend == BACKEND_CLAUDE else codex_thread_id
        title = req.title or (
            f"Resumed {backend.title()} {str(active_provider_id)[:8]}" if active_provider_id else "New chat"
        )
        archived = bool(req.archived)
        pinned = bool(req.pinned) and not archived
        sess = {
            "id": sid,
            "title": title,
            "folder": req.folder or "General",
            "cwd": req.cwd or DEFAULT_CWD,
            "backend": backend,
            "model": req.model,
            "effort": normalize_runtime_effort(backend, req.effort, strict=True),
            "system_prompt": clean_session_system_prompt(req.system_prompt),
            "session_id": active_provider_id,
            "claude_session_id": claude_session_id,
            "codex_thread_id": codex_thread_id,
            "parent_id": parent_id,
            "fork_from": None,
            "pinned": pinned,
            "pinned_at": now if pinned else None,
            "archived": archived,
            "archived_at": now if archived else None,
            "created_at": now,
            "updated_at": now,
        }
        sess["sort_order"] = self.top_order_for_section(session_section_key(sess))
        async with self._lock:
            self.sessions[sid] = sess
            await self.save()
        await append_event(sid, "session_created", {"session": public_session(sess)})
        return sess

    async def update(self, sid: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            old_section = session_section_key(sess)
            if "backend" in patch and patch["backend"] is not None:
                backend = str(patch["backend"]).lower()
                if backend not in VALID_BACKENDS:
                    raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
                old = sess.get("backend") or DEFAULT_BACKEND
                if old != backend:
                    if session_backend_locked(sess):
                        raise HTTPException(
                            status_code=409,
                            detail="backend is locked after the chat starts; fork or create a new chat to use another backend",
                        )
                    if sess.get("session_id"):
                        sess["claude_session_id" if old == BACKEND_CLAUDE else "codex_thread_id"] = sess["session_id"]
                    sess["session_id"] = sess.get("claude_session_id" if backend == BACKEND_CLAUDE else "codex_thread_id")
                    sess["backend"] = backend
                    if "model" not in patch:
                        sess["model"] = None
                    if "effort" not in patch:
                        sess["effort"] = None
                    await append_event(sid, "backend_changed", {"old": old, "new": backend})
            for key in ("title", "folder", "cwd"):
                if key in patch and patch[key] is not None:
                    sess[key] = patch[key]
            if "system_prompt" in patch:
                sess["system_prompt"] = clean_session_system_prompt(patch["system_prompt"])
            for key in ("model", "effort"):
                if key in patch:
                    value = patch[key]
                    if key == "effort":
                        sess[key] = normalize_runtime_effort(
                            str(sess.get("backend") or DEFAULT_BACKEND),
                            value,
                            strict=True,
                        )
                    else:
                        clean = str(value).strip() if value is not None else ""
                        sess[key] = clean or None
            if "pinned" in patch and patch["pinned"] is not None:
                pinned = bool(patch["pinned"])
                if pinned and not sess.get("pinned"):
                    sess["pinned_at"] = now_iso()
                elif not pinned:
                    sess["pinned_at"] = None
                sess["pinned"] = pinned
            if "archived" in patch and patch["archived"] is not None:
                archived = bool(patch["archived"])
                if archived and not sess.get("archived"):
                    sess["archived_at"] = now_iso()
                elif not archived:
                    sess["archived_at"] = None
                sess["archived"] = archived
                if archived:
                    sess["pinned"] = False
                    sess["pinned_at"] = None
            new_section = session_section_key(sess)
            if new_section != old_section:
                sess["sort_order"] = self.top_order_for_section(new_section, excluding_id=sid)
            sess["updated_at"] = now_iso()
            await self.save()
            HISTORY_SEARCH_DIRTY.add(sid)
            return sess

    async def reorder(
        self,
        sid: str,
        direction: str | None = None,
        target_id: str | None = None,
        placement: str | None = None,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            section = session_section_key(sess)

            if target_id:
                normalized_placement = (placement or "before").strip().lower()
                if normalized_placement not in {"before", "after"}:
                    raise HTTPException(status_code=400, detail="placement must be before or after")
                target = self.sessions.get(target_id)
                if not target:
                    raise HTTPException(status_code=404, detail="target session not found")
                if session_section_key(target) != section:
                    raise HTTPException(status_code=400, detail="sessions must be in the same section")
                if target_id == sid:
                    return sorted_sessions(list(self.sessions.values()))

                peers = [
                    peer for peer in sorted_sessions(list(self.sessions.values()))
                    if session_section_key(peer) == section
                    and peer.get("id") != sid
                ]
                target_index = next((idx for idx, peer in enumerate(peers) if peer.get("id") == target_id), None)
                if target_index is None:
                    raise HTTPException(status_code=404, detail="target session not found")
                insert_index = target_index + 1 if normalized_placement == "after" else target_index
                reordered = peers[:insert_index] + [sess] + peers[insert_index:]
                for index, peer in enumerate(reordered):
                    peer["sort_order"] = (index + 1) * SESSION_ORDER_STEP
                sess["updated_at"] = now_iso()
                await self.save()
                return sorted_sessions(list(self.sessions.values()))

            normalized = (direction or "").strip().lower()
            if normalized not in {"up", "down"}:
                raise HTTPException(status_code=400, detail="direction must be up or down")
            peers = [
                peer for peer in sorted_sessions(list(self.sessions.values()))
                if session_section_key(peer) == section
            ]
            index = next((idx for idx, peer in enumerate(peers) if peer.get("id") == sid), None)
            if index is None:
                raise HTTPException(status_code=404, detail="session not found")
            target_index = index - 1 if normalized == "up" else index + 1
            if 0 <= target_index < len(peers):
                other = peers[target_index]
                current_order = session_order_value(sess)
                other_order = session_order_value(other)
                sess["sort_order"] = other_order
                other["sort_order"] = current_order
                sess["updated_at"] = now_iso()
                await self.save()
            return sorted_sessions(list(self.sessions.values()))

    async def mark_read(self, sid: str, last_read_agent_event_seq: int | None) -> dict[str, Any]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            latest = int(sess.get("latest_agent_event_seq") or 0)
            requested = latest if last_read_agent_event_seq is None else max(0, int(last_read_agent_event_seq))
            current = int(sess.get("last_read_agent_event_seq") or 0)
            if requested > latest:
                sess["latest_agent_event_seq"] = requested
                latest = requested
            sess["last_read_agent_event_seq"] = max(current, requested)
            sess["last_read_agent_event_at"] = now_iso()
            sess["manual_unread"] = False
            await self.save()
            return sess

    async def mark_unread(self, sid: str) -> dict[str, Any]:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            latest = int(sess.get("latest_agent_event_seq") or 0)
            sess["last_read_agent_event_seq"] = max(0, latest - 1)
            sess["last_read_agent_event_at"] = now_iso()
            sess["manual_unread"] = True
            await self.save()
            return sess

    async def delete(self, sid: str) -> bool:
        async with self._lock:
            existed = self.sessions.pop(sid, None)
            await self.save()
        if existed:
            shutil.rmtree(session_dir(sid), ignore_errors=True)
            await forget_event_seq(sid)
            HISTORY_SEARCH_DIRTY.add(sid)
        return existed is not None

    async def save_provider_session(self, sid: str, provider_id: str, backend: str, *, cwd: str | None = None) -> None:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                return
            sess["session_id"] = provider_id
            sess["backend"] = sess.get("backend") or backend
            sess["claude_session_id" if backend == BACKEND_CLAUDE else "codex_thread_id"] = provider_id
            if backend == BACKEND_CLAUDE and cwd:
                sess["claude_session_cwd"] = cwd
            if backend == BACKEND_CLAUDE and sess.get("fork_from") and provider_id != sess.get("fork_from"):
                sess["fork_from"] = None
            sess["updated_at"] = now_iso()
            await self.save()


STORE = SessionStore()


class JobStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self._scheduler_task: asyncio.Task | None = None

    async def load(self) -> None:
        ensure_dirs()
        if JOBS_FILE.exists():
            try:
                self.jobs = json.loads(JOBS_FILE.read_text())
            except Exception as e:
                logger.warning("failed to load jobs: %s", e)
                self.jobs = {}

    async def save(self) -> None:
        ensure_dirs()
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.jobs, indent=2))
        tmp.replace(JOBS_FILE)

    async def create(self, req: CreateJobRequest) -> dict[str, Any]:
        if req.session_id not in STORE.sessions:
            raise HTTPException(status_code=404, detail="session not found")
        if req.backend and req.backend not in VALID_BACKENDS:
            raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
        jid = f"job_{uuid.uuid4().hex[:16]}"
        now = now_iso()
        first_run_at = parse_job_timestamp(req.first_run_at)
        max_runs = None if req.loop else max(1, int(req.max_runs or 1))
        job = {
            "id": jid,
            "session_id": req.session_id,
            "title": req.title,
            "prompt": req.prompt,
            "interval_seconds": req.interval_seconds,
            "loop": req.loop,
            "max_runs": max_runs,
            "enabled": req.enabled,
            "backend": req.backend,
            "created_at": now,
            "updated_at": now,
            "last_run_at": None,
            "next_run_at": first_run_at if req.enabled and first_run_at is not None else time.time() + req.interval_seconds if req.enabled and req.interval_seconds else None,
            "run_count": 0,
        }
        async with self._lock:
            self.jobs[jid] = job
            await self.save()
        await append_event(req.session_id, "job_created", {
            "job": public_job(job),
            "job_id": jid,
            "message": f"Scheduled job created: {req.title}",
        })
        return job

    async def update(self, jid: str, patch: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            job = self.jobs.get(jid)
            if not job:
                raise HTTPException(status_code=404, detail="job not found")
            if "backend" in patch and patch["backend"] is not None and patch["backend"] not in VALID_BACKENDS:
                raise HTTPException(status_code=400, detail=f"backend must be one of {sorted(VALID_BACKENDS)}")
            has_next_run_patch = "next_run_at" in patch and patch["next_run_at"] is not None
            next_run_at = parse_job_timestamp(patch.get("next_run_at")) if has_next_run_patch else None
            should_reschedule = any(key in patch for key in ("interval_seconds", "loop", "max_runs", "enabled"))
            for key in ("title", "prompt", "interval_seconds", "loop", "enabled", "backend"):
                if key in patch and patch[key] is not None:
                    job[key] = patch[key]
            if "max_runs" in patch:
                if job.get("loop"):
                    job["max_runs"] = None
                elif patch["max_runs"] is not None:
                    job["max_runs"] = max(1, int(patch["max_runs"]))
            elif "loop" in patch and job.get("loop"):
                job["max_runs"] = None
            elif "loop" in patch and not job.get("loop") and not job.get("max_runs"):
                job["max_runs"] = max(1, int(job.get("run_count") or 0) + 1)
            if job.get("enabled") and has_next_run_patch:
                job["next_run_at"] = next_run_at
            elif job.get("enabled") and job.get("interval_seconds") and (should_reschedule or not job.get("next_run_at")):
                job["next_run_at"] = time.time() + int(job["interval_seconds"])
            if not job.get("enabled"):
                job["next_run_at"] = None
            job["updated_at"] = now_iso()
            await self.save()
            return job

    async def delete(self, jid: str) -> bool:
        async with self._lock:
            existed = self.jobs.pop(jid, None)
            await self.save()
            return existed is not None

    async def delete_for_session(self, session_id: str) -> int:
        async with self._lock:
            doomed = [jid for jid, job in self.jobs.items() if job.get("session_id") == session_id]
            for jid in doomed:
                self.jobs.pop(jid, None)
            await self.save()
            return len(doomed)

    async def mark_ran(self, jid: str) -> None:
        async with self._lock:
            job = self.jobs.get(jid)
            if not job:
                return
            job["last_run_at"] = now_iso()
            job["run_count"] = int(job.get("run_count") or 0) + 1
            run_count = int(job.get("run_count") or 0)
            max_runs = int(job.get("max_runs") or 1)
            finite_has_more = not job.get("loop") and run_count < max_runs
            if job.get("enabled") and job.get("interval_seconds") and (job.get("loop") or finite_has_more):
                job["next_run_at"] = time.time() + int(job["interval_seconds"])
            else:
                job["enabled"] = False
                job["next_run_at"] = None
            job["updated_at"] = now_iso()
            await self.save()

    async def defer(self, jid: str, reason: str, delay_seconds: int | None = None) -> None:
        delay = int(delay_seconds or JOB_BUSY_RETRY_SECONDS)
        emit_event = False
        event_job: dict[str, Any] | None = None
        async with self._lock:
            job = self.jobs.get(jid)
            if not job:
                return
            now = time.time()
            job["next_run_at"] = now + max(delay, 5)
            job["last_deferred_at"] = now_iso()
            job["last_defer_reason"] = reason
            last_emit = float(job.get("_last_defer_event_at") or 0)
            if JOB_DEFER_EVENT_MIN_SECONDS <= 0 or now - last_emit >= JOB_DEFER_EVENT_MIN_SECONDS:
                job["_last_defer_event_at"] = now
                emit_event = True
                event_job = public_job(job)
            job["updated_at"] = now_iso()
            await self.save()
        if emit_event and event_job and event_job.get("session_id"):
            await append_event(str(event_job["session_id"]), "job_deferred", {
                "job": event_job,
                "job_id": jid,
                "message": f"Scheduled job deferred: {event_job.get('title') or jid} — {reason}",
            })

    async def run_job(self, jid: str) -> dict[str, Any]:
        job = self.jobs.get(jid)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        req = TurnRequest(
            prompt=job["prompt"],
            file_ids=[],
            backend=job.get("backend"),
            purpose="scheduled_job",
            job_id=jid,
            job_title=str(job.get("title") or jid),
        )
        result = await start_turn(job["session_id"], req, queue_if_busy=False)
        await self.mark_ran(jid)
        ran_job = public_job(self.jobs[jid])
        await append_event(job["session_id"], "job_ran", {
            "job": ran_job,
            "job_id": jid,
            "run_id": result["run_id"],
            "message": f"Scheduled job ran: {ran_job.get('title') or jid}",
        })
        return result

    def start_scheduler(self) -> None:
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self.scheduler_loop())

    async def scheduler_loop(self) -> None:
        logger.info("job scheduler started")
        while True:
            await asyncio.sleep(max(JOB_SCHEDULER_INTERVAL_SECONDS, 1.0))
            now = time.time()
            due = [
                job["id"] for job in list(self.jobs.values())
                if job.get("enabled") and job.get("next_run_at") and float(job["next_run_at"]) <= now
            ]
            for jid in due:
                job = self.jobs.get(jid)
                if not job:
                    continue
                blocker = await scheduled_job_blocker(str(job.get("session_id") or ""))
                if blocker:
                    await self.defer(jid, blocker, JOB_BUSY_RETRY_SECONDS)
                    continue
                try:
                    await self.run_job(jid)
                except Exception as e:
                    logger.warning("scheduled job %s failed: %s", jid, e)
                    if job.get("session_id"):
                        await append_event(job["session_id"], "job_error", {
                            "job": public_job(job),
                            "job_id": jid,
                            "message": f"Scheduled job failed: {job.get('title') or jid} — {e}",
                        })
                    run_count = int(job.get("run_count") or 0)
                    max_runs = int(job.get("max_runs") or 1)
                    finite_has_more = not job.get("loop") and run_count < max_runs
                    if job.get("interval_seconds") and (job.get("loop") or finite_has_more):
                        job["next_run_at"] = time.time() + int(job.get("interval_seconds") or 300)
                    else:
                        job["enabled"] = False
                        job["next_run_at"] = None
                    await self.save()


JOBS = JobStore()


class SubscriberHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, sid: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._subscribers.setdefault(sid, set()).add(ws)

    async def unsubscribe(self, sid: str, ws: WebSocket) -> None:
        async with self._lock:
            subs = self._subscribers.get(sid)
            if subs:
                subs.discard(ws)
                if not subs:
                    self._subscribers.pop(sid, None)

    async def broadcast(self, sid: str, event: dict[str, Any]) -> None:
        async with self._lock:
            subs = list(self._subscribers.get(sid, set()))
        stale: list[WebSocket] = []
        for ws in subs:
            try:
                await ws.send_json(event)
            except Exception:
                stale.append(ws)
        if stale:
            async with self._lock:
                current = self._subscribers.get(sid, set())
                for ws in stale:
                    current.discard(ws)


HUB = SubscriberHub()
ACTIVE: dict[str, dict[str, Any]] = {}
BUSY_SESSIONS: set[str] = set()
CURRENT_TURNS: dict[str, dict[str, Any]] = {}
STOP_REQUESTS: set[str] = set()
STOPPED_RUNS: set[str] = set()
ACTIVE_LOCK = asyncio.Lock()
QUEUED_TURNS: dict[str, deque[dict[str, Any]]] = {}
RUN_NOW_TURNS: dict[str, dict[str, Any]] = {}
STEERING_SESSIONS: set[str] = set()
QUEUE_LOCK = asyncio.Lock()
RUN_METADATA: dict[str, dict[str, Any]] = {}
HANDOFF_DIGEST_JOBS: dict[str, dict[str, Any]] = {}
HANDOFF_DIGEST_JOBS_LOCK = asyncio.Lock()
HANDOFF_DIGEST_FINALIZING: set[str] = set()
RUNTIME_DIAGNOSTICS: dict[str, dict[str, Any]] = {}
RUNTIME_DIAGNOSTICS_LOCK = threading.RLock()

LOG_PATH_SUFFIXES = {
    ".log", ".out", ".err", ".stderr", ".stdout", ".txt", ".jsonl", ".trace"
}
LIVE_STDOUT_MAX_LINES = 400
LIVE_STDOUT_MAX_LINE_CHARS = 12_000
TMUX_CAPTURE_MAX_LINES = 2_000
TMUX_COMMAND_TIMEOUT_SECONDS = 4
TERMINAL_MIN_COLUMNS = 2
TERMINAL_MAX_COLUMNS = 500
TERMINAL_MIN_ROWS = 1
TERMINAL_MAX_ROWS = 200
TERMINAL_READ_BYTES = 64 * 1024


def run_event_metadata(run_id: str) -> dict[str, Any]:
    metadata = RUN_METADATA.get(run_id) or {}
    return {key: value for key, value in metadata.items() if value is not None}


async def append_event(session_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    ensure_dirs(session_id)
    path = events_path(session_id)
    seq = await next_event_seq(session_id, path)
    ts = now_iso()
    event = {
        "seq": seq,
        "id": f"evt_{uuid.uuid4().hex[:16]}",
        "session_id": session_id,
        "type": event_type,
        "ts": ts,
        **(payload or {}),
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")
    HISTORY_SEARCH_DIRTY.add(session_id)
    await update_session_event_metadata(session_id, event)
    await HUB.broadcast(session_id, event)
    return event


def is_agent_visible_event(event_type: str, event: dict[str, Any]) -> bool:
    if event_type == "assistant_text":
        return bool(str(event.get("text") or "").strip())
    if event_type == "turn_finished":
        return bool(str(event.get("result_text") or "").strip())
    return event_type in {"error", "artifact_created", "job_ran", "job_error", "handoff_digest_received"}


def should_bump_session_updated_at(event_type: str, event: dict[str, Any]) -> bool:
    if is_agent_visible_event(event_type, event):
        return True
    return event_type in {
        "turn_started",
        "turn_queued",
        "turn_unqueued",
        "turn_queue_updated",
        "turn_queue_reordered",
        "turn_queue_run_now",
        "file_uploaded",
        "job_created",
        "job_deferred",
        "handoff_digest_started",
        "handoff_digest_ready",
        "handoff_digest_received",
        "handoff_digest_sent",
        "handoff_digest_error",
        "backend_changed",
        "history_imported",
        "session_forked",
    }


async def update_session_event_metadata(session_id: str, event: dict[str, Any]) -> None:
    sess = STORE.sessions.get(session_id)
    if not sess:
        return
    event_type = str(event.get("type") or "")
    sess["latest_event_seq"] = int(event.get("seq") or 0)
    sess["latest_event_at"] = event.get("ts")
    sess["latest_event_type"] = event_type
    if is_agent_visible_event(event_type, event):
        sess["latest_agent_event_seq"] = int(event.get("seq") or 0)
        sess["latest_agent_event_at"] = event.get("ts")
        sess["latest_agent_event_type"] = event_type
    if should_bump_session_updated_at(event_type, event):
        sess["updated_at"] = event.get("ts") or now_iso()
        await STORE.save()


async def enqueue_turn(session_id: str, req: TurnRequest, sess: dict[str, Any]) -> dict[str, Any]:
    queued_id = f"queued_{uuid.uuid4().hex[:16]}"
    item = {
        "queued_id": queued_id,
        "prompt": req.prompt,
        "file_ids": list(req.file_ids),
        "backend": req.backend,
        "model": req.model,
        "effort": req.effort,
        "display_prompt": req.display_prompt,
        "purpose": req.purpose,
        "digest_job_id": req.digest_job_id,
        "digest_detail": req.digest_detail,
        "source_session_id": req.source_session_id,
        "target_session_id": req.target_session_id,
        "created_at": now_iso(),
    }
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.setdefault(session_id, deque())
        queue.append(item)
        position = len(queue)
    display_prompt = req.display_prompt if req.display_prompt is not None else req.prompt
    queued_event = await append_event(session_id, "turn_queued", {
        "queued_id": queued_id,
        "backend": req.backend or sess.get("backend") or DEFAULT_BACKEND,
        "model": req.model,
        "effort": req.effort,
        "prompt": display_prompt,
        "request_prompt": req.prompt,
        "display_prompt": req.display_prompt,
        "file_ids": list(req.file_ids),
        "position": position,
        "purpose": req.purpose,
        "digest_job_id": req.digest_job_id,
        "digest_detail": req.digest_detail,
        "source_session_id": req.source_session_id,
        "target_session_id": req.target_session_id,
    })
    return {
        "queued": True,
        "queued_id": queued_id,
        "position": position,
        "session": public_session(STORE.sessions[session_id]),
        "event": queued_event,
    }


async def unqueue_turn(session_id: str, queued_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")

    removed: dict[str, Any] | None = None
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.get(session_id)
        if queue:
            kept: deque[dict[str, Any]] = deque()
            for item in queue:
                if removed is None and item.get("queued_id") == queued_id:
                    removed = item
                    continue
                kept.append(item)
            if kept:
                QUEUED_TURNS[session_id] = kept
                remaining = len(kept)
            else:
                QUEUED_TURNS.pop(session_id, None)
                remaining = 0
        else:
            remaining = 0

    if removed is None:
        raise HTTPException(status_code=404, detail="queued turn not found")

    await append_event(session_id, "turn_unqueued", {
        "queued_id": queued_id,
        "backend": removed.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
        "prompt": removed.get("prompt") or "",
        "file_ids": list(removed.get("file_ids") or []),
        "message": "Removed queued message.",
        "remaining": remaining,
        "purpose": removed.get("purpose"),
        "digest_job_id": removed.get("digest_job_id"),
        "digest_detail": removed.get("digest_detail"),
        "source_session_id": removed.get("source_session_id"),
        "target_session_id": removed.get("target_session_id"),
    })
    if removed.get("digest_job_id"):
        await finish_handoff_digest_queue_item(
            session_id,
            removed,
            "Context digest request was canceled before it ran.",
            cancelled=True,
        )
    return {
        "ok": True,
        "unqueued": True,
        "queued_id": queued_id,
        "remaining": remaining,
    }


def queue_positions(queue: deque[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"queued_id": str(item.get("queued_id") or ""), "position": idx + 1}
        for idx, item in enumerate(queue)
    ]


def merged_file_ids(*groups: list[str] | tuple[str, ...]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            file_id = str(value or "").strip()
            if file_id and file_id not in seen:
                seen.add(file_id)
                merged.append(file_id)
    return merged


def file_attachment_prompt_lines(file_ids: list[str] | tuple[str, ...]) -> list[str]:
    lines: list[str] = []
    for file_id in merged_file_ids(file_ids):
        rec_path = FILES_ROOT / file_id / "meta.json"
        if not rec_path.exists():
            continue
        with suppress(Exception):
            rec = json.loads(rec_path.read_text())
            lines.append(
                f"- {rec.get('path')} ({rec.get('filename')}, {rec.get('content_type')})"
            )
    return lines


def prepare_steered_turn(selected: dict[str, Any], interrupted: dict[str, Any] | None) -> dict[str, Any]:
    """Build the provider request for a real steer without changing its UI prompt."""
    turn = dict(selected)
    steering_prompt = str(selected.get("prompt") or "").strip()
    interrupted_prompt = str((interrupted or {}).get("prompt") or "").strip()
    selected_file_ids = list(
        selected.get("display_file_ids")
        if selected.get("display_file_ids") is not None
        else selected.get("file_ids") or []
    )
    interrupted_file_ids = list((interrupted or {}).get("file_ids") or [])
    if not (steering_prompt or selected_file_ids) or not (interrupted_prompt or interrupted_file_ids):
        return turn
    if steering_prompt == interrupted_prompt and selected_file_ids == interrupted_file_ids:
        return turn

    interrupted_message = interrupted_prompt or "[Attachment-only message]"
    interrupted_attachment_lines = file_attachment_prompt_lines(interrupted_file_ids)
    if interrupted_attachment_lines:
        interrupted_message += (
            "\n\n[Interrupted message attachments]\n"
            + "\n".join(interrupted_attachment_lines)
            + "\nThese files belong to the interrupted message above, not the steering message.\n"
            "[End interrupted message attachments]"
        )

    turn["steering_prompt"] = steering_prompt
    turn["display_prompt"] = str(
        selected.get("display_prompt")
        if selected.get("display_prompt") is not None
        else steering_prompt
    )
    turn["prompt"] = (
        "The previous agent turn was interrupted before completion. Continue that request while "
        "applying the steering instruction below. Treat both messages as user input, and give the "
        "steering instruction priority wherever they conflict.\n\n"
        "[Interrupted message]\n"
        f"{interrupted_message}\n"
        "[End interrupted message]\n\n"
        "[Steering message]\n"
        f"{steering_prompt or '[Attachment-only steering message]'}\n"
        "[End steering message]"
    )
    turn["display_file_ids"] = selected_file_ids
    turn["file_ids"] = selected_file_ids
    turn["steer_interrupted_run_id"] = (interrupted or {}).get("run_id")
    turn["replays_interrupted_message"] = True
    return turn


def public_queued_turn(session_id: str, item: dict[str, Any], position: int) -> dict[str, Any]:
    display_file_ids = item.get("display_file_ids")
    display_prompt = item.get("display_prompt")
    return {
        "queued_id": str(item.get("queued_id") or ""),
        "session_id": session_id,
        "prompt": str(
            display_prompt
            if display_prompt is not None
            else item.get("steering_prompt") or item.get("prompt") or ""
        ),
        "file_ids": list(display_file_ids if display_file_ids is not None else item.get("file_ids") or []),
        "backend": item.get("backend"),
        "model": item.get("model"),
        "effort": item.get("effort"),
        "display_prompt": item.get("display_prompt"),
        "purpose": item.get("purpose"),
        "digest_job_id": item.get("digest_job_id"),
        "digest_detail": item.get("digest_detail"),
        "source_session_id": item.get("source_session_id"),
        "target_session_id": item.get("target_session_id"),
        "created_at": item.get("created_at"),
        "position": position,
    }


async def queued_turns_snapshot(session_id: str) -> list[dict[str, Any]]:
    async with QUEUE_LOCK:
        queue = list(QUEUED_TURNS.get(session_id) or [])
        run_now = RUN_NOW_TURNS.get(session_id)
    items: list[dict[str, Any]] = []
    if run_now is not None:
        items.append(run_now)
    items.extend(queue)
    return [
        public_queued_turn(session_id, item, idx + 1)
        for idx, item in enumerate(items)
        if str(item.get("queued_id") or "").strip()
    ]


async def update_queued_turn(session_id: str, queued_id: str, req: UpdateQueuedTurnRequest) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")

    updated: dict[str, Any] | None = None
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.get(session_id)
        if queue:
            for idx, item in enumerate(queue):
                if item.get("queued_id") == queued_id:
                    if req.prompt is not None:
                        prompt = req.prompt.strip()
                        if not prompt:
                            raise HTTPException(status_code=400, detail="prompt is empty")
                        item["prompt"] = prompt
                    if req.file_ids is not None:
                        item["file_ids"] = list(req.file_ids)
                    updated = dict(item)
                    updated["position"] = idx + 1
                    break

    if updated is None:
        raise HTTPException(status_code=404, detail="queued turn not found")

    await append_event(session_id, "turn_queue_updated", {
        "queued_id": queued_id,
        "backend": updated.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
        "prompt": updated.get("prompt") or "",
        "file_ids": list(updated.get("file_ids") or []),
        "position": updated.get("position"),
    })
    return {"ok": True, "queued_id": queued_id, "item": updated}


async def move_queued_turn(session_id: str, queued_id: str, req: MoveQueuedTurnRequest) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    direction = req.direction.strip().lower()
    if direction not in {"up", "down"}:
        raise HTTPException(status_code=400, detail="direction must be up or down")

    moved: dict[str, Any] | None = None
    positions: list[dict[str, Any]] = []
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.get(session_id)
        if queue:
            items = list(queue)
            idx = next((i for i, item in enumerate(items) if item.get("queued_id") == queued_id), None)
            if idx is not None:
                new_idx = idx - 1 if direction == "up" else idx + 1
                new_idx = max(0, min(len(items) - 1, new_idx))
                if new_idx != idx:
                    items[idx], items[new_idx] = items[new_idx], items[idx]
                    QUEUED_TURNS[session_id] = deque(items)
                    moved = dict(items[new_idx])
                else:
                    moved = dict(items[idx])
                positions = queue_positions(QUEUED_TURNS[session_id])

    if moved is None:
        raise HTTPException(status_code=404, detail="queued turn not found")

    await append_event(session_id, "turn_queue_reordered", {
        "queued_id": queued_id,
        "direction": direction,
        "positions": positions,
    })
    return {"ok": True, "queued_id": queued_id, "positions": positions}


async def run_queued_turn_now(session_id: str, queued_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")

    async with ACTIVE_LOCK:
        interrupted_turn = dict(CURRENT_TURNS.get(session_id) or {})

    selected: dict[str, Any] | None = None
    remaining: int
    async with QUEUE_LOCK:
        if session_id in STEERING_SESSIONS or RUN_NOW_TURNS.get(session_id) is not None:
            raise HTTPException(status_code=409, detail="another steering handoff is already in progress")
        queue = QUEUED_TURNS.get(session_id)
        if queue:
            items = list(queue)
            selected_index = next(
                (idx for idx, item in enumerate(items) if item.get("queued_id") == queued_id),
                None,
            )
            if selected_index is not None:
                selected = items[selected_index]
                kept = deque(
                    item
                    for idx, item in enumerate(items)
                    if idx != selected_index
                )
                if kept:
                    QUEUED_TURNS[session_id] = kept
                    remaining = len(kept)
                else:
                    QUEUED_TURNS.pop(session_id, None)
                    remaining = 0
            else:
                remaining = len(items)
        else:
            remaining = 0
        if selected is not None:
            RUN_NOW_TURNS[session_id] = selected
            STEERING_SESSIONS.add(session_id)

    if selected is None:
        raise HTTPException(status_code=404, detail="queued turn not found")

    try:
        stop_result = await stop_turn(session_id, emit_event=False, schedule_queue=False)
        interrupted = bool(stop_result.get("stopped") or stop_result.get("pending"))
        prepared = prepare_steered_turn(selected, interrupted_turn if interrupted else None)
        async with QUEUE_LOCK:
            current = RUN_NOW_TURNS.get(session_id)
            if current and current.get("queued_id") == queued_id:
                RUN_NOW_TURNS[session_id] = prepared

        display_prompt = str(prepared.get("display_prompt") or prepared.get("steering_prompt") or selected.get("prompt") or "")
        await append_event(session_id, "turn_queue_run_now", {
            "queued_id": queued_id,
            "backend": prepared.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
            "prompt": display_prompt,
            "request_prompt": prepared.get("prompt") or display_prompt,
            "display_prompt": display_prompt,
            "file_ids": list(prepared.get("file_ids") or []),
            "display_file_ids": list(
                prepared.get("display_file_ids")
                if prepared.get("display_file_ids") is not None
                else selected.get("file_ids") or []
            ),
            "interrupted_run_id": prepared.get("steer_interrupted_run_id"),
            "replays_interrupted_message": bool(prepared.get("replays_interrupted_message")),
            "message": "Steering message promoted; the interrupted request will be replayed with it.",
            "remaining": remaining,
            "superseded_queued_ids": [],
        })
        asyncio.create_task(wait_for_steered_turn_slot(session_id))
        return {
            "ok": True,
            "queued_id": queued_id,
            "interrupted": interrupted,
            "replays_interrupted_message": bool(prepared.get("replays_interrupted_message")),
            "superseded_queued_ids": [],
        }
    except Exception:
        async with QUEUE_LOCK:
            STEERING_SESSIONS.discard(session_id)
        schedule_next_queued_turn(session_id)
        raise


async def requeue_turn_front(session_id: str, item: dict[str, Any]) -> None:
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.setdefault(session_id, deque())
        queue.appendleft(item)


async def retry_next_queued_turn_later(session_id: str, delay_seconds: int | None = None) -> None:
    await asyncio.sleep(max(int(delay_seconds or JOB_BUSY_RETRY_SECONDS), 5))
    await start_next_queued_turn(session_id)


async def wait_for_steered_turn_slot(session_id: str) -> None:
    while True:
        async with ACTIVE_LOCK:
            busy = session_id in BUSY_SESSIONS
        if not busy:
            break
        await asyncio.sleep(0.05)
    async with QUEUE_LOCK:
        STEERING_SESSIONS.discard(session_id)
    await start_next_queued_turn(session_id)


async def start_next_queued_turn(session_id: str) -> None:
    async with QUEUE_LOCK:
        if session_id in STEERING_SESSIONS:
            return
        item = RUN_NOW_TURNS.pop(session_id, None)
        if item is None:
            queue = QUEUED_TURNS.get(session_id)
            item = queue.popleft() if queue else None
            if queue is not None and not queue:
                QUEUED_TURNS.pop(session_id, None)
    if not item:
        return

    req = TurnRequest(
        prompt=str(item.get("prompt") or ""),
        file_ids=list(item.get("file_ids") or []),
        backend=item.get("backend"),
        model=item.get("model"),
        effort=item.get("effort"),
        display_prompt=item.get("display_prompt"),
        purpose=item.get("purpose"),
        digest_job_id=item.get("digest_job_id"),
        digest_detail=item.get("digest_detail"),
        source_session_id=item.get("source_session_id"),
        target_session_id=item.get("target_session_id"),
        steer_interrupted_run_id=item.get("steer_interrupted_run_id"),
    )
    try:
        display_file_ids = item.get("display_file_ids")
        await start_turn(
            session_id,
            req,
            queue_if_busy=False,
            queued_id=str(item["queued_id"]),
            display_file_ids=list(display_file_ids) if display_file_ids is not None else None,
        )
    except HTTPException as e:
        if e.status_code in (409, 503):
            await requeue_turn_front(session_id, item)
            await append_event(session_id, "turn_deferred", {
                "queued_id": item.get("queued_id"),
                "message": f"Queued turn deferred: {e.detail}",
            })
            asyncio.create_task(retry_next_queued_turn_later(session_id))
            return
        logger.warning("queued turn failed session=%s queued_id=%s: %s", session_id, item.get("queued_id"), e.detail)
        await append_event(session_id, "error", {
            "queued_id": item.get("queued_id"),
            "message": f"queued turn failed: {e.detail}",
        })
        if item.get("digest_job_id"):
            await finish_handoff_digest_queue_item(session_id, item, f"queued turn failed: {e.detail}")
    except Exception as e:
        logger.warning("queued turn failed session=%s queued_id=%s: %s", session_id, item.get("queued_id"), e)
        await append_event(session_id, "error", {
            "queued_id": item.get("queued_id"),
            "message": f"queued turn failed: {e}",
        })
        if item.get("digest_job_id"):
            await finish_handoff_digest_queue_item(session_id, item, f"queued turn failed: {e}")


def schedule_next_queued_turn(session_id: str) -> None:
    asyncio.create_task(start_next_queued_turn(session_id))


def schedule_rebuilt_queued_turns() -> int:
    scheduled = 0
    for session_id, queue in list(QUEUED_TURNS.items()):
        if queue:
            schedule_next_queued_turn(session_id)
            scheduled += 1
    return scheduled


def should_schedule_queue_after_finish(session_id: str, stopped: bool) -> bool:
    # A steering handoff has one owner: wait_for_steered_turn_slot. Scheduling
    # here as well races that waiter and can pop a second queued item, including
    # when the interrupted provider finishes naturally just before stop_turn.
    return (
        not stopped
        and session_id not in STEERING_SESSIONS
        and session_id not in RUN_NOW_TURNS
    )


def queued_turn_from_event(event: dict[str, Any], sess: dict[str, Any], position: int) -> dict[str, Any]:
    return {
        "queued_id": str(event.get("queued_id") or ""),
        "prompt": event.get("request_prompt") or event.get("prompt") or "",
        "file_ids": list(event.get("file_ids") or []),
        "display_file_ids": (
            list(event.get("display_file_ids") or [])
            if event.get("display_file_ids") is not None
            else None
        ),
        "backend": event.get("backend") or sess.get("backend"),
        "model": event.get("model") if event.get("model") is not None else sess.get("model"),
        "effort": event.get("effort") if event.get("effort") is not None else sess.get("effort"),
        "display_prompt": event.get("display_prompt"),
        "purpose": event.get("purpose"),
        "digest_job_id": event.get("digest_job_id"),
        "digest_detail": event.get("digest_detail"),
        "source_session_id": event.get("source_session_id"),
        "target_session_id": event.get("target_session_id"),
        "steer_interrupted_run_id": event.get("interrupted_run_id") or event.get("steer_interrupted_run_id"),
        "replays_interrupted_message": bool(event.get("replays_interrupted_message")),
        "created_at": event.get("ts") or now_iso(),
        "position": int(event.get("position") or position),
    }


def rebuild_queued_turns_from_events() -> int:
    rebuilt = 0
    for session_id, sess in STORE.sessions.items():
        path = events_path(session_id)
        if not path.exists():
            continue
        pending: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for line in path.open("r", encoding="utf-8", errors="ignore"):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            queued_id = str(event.get("queued_id") or "")
            event_type = str(event.get("type") or "")
            if event_type == "turn_queue_run_now":
                for superseded_id in event.get("superseded_queued_ids") or []:
                    superseded_id = str(superseded_id or "")
                    pending.pop(superseded_id, None)
                    if superseded_id in order:
                        order.remove(superseded_id)
            if event_type == "turn_queued" and queued_id:
                pending[queued_id] = queued_turn_from_event(event, sess, len(order) + 1)
                if queued_id not in order:
                    order.append(queued_id)
            elif event_type in {"turn_queue_updated", "turn_queue_run_now"} and queued_id in pending:
                if event.get("request_prompt") is not None:
                    pending[queued_id]["prompt"] = event.get("request_prompt") or ""
                elif event.get("prompt") is not None:
                    pending[queued_id]["prompt"] = event.get("prompt") or ""
                if event.get("display_prompt") is not None:
                    pending[queued_id]["display_prompt"] = event.get("display_prompt")
                if event.get("file_ids") is not None:
                    pending[queued_id]["file_ids"] = list(event.get("file_ids") or [])
                if event.get("display_file_ids") is not None:
                    pending[queued_id]["display_file_ids"] = list(event.get("display_file_ids") or [])
                if event.get("interrupted_run_id") is not None:
                    pending[queued_id]["steer_interrupted_run_id"] = event.get("interrupted_run_id")
                if event.get("replays_interrupted_message") is not None:
                    pending[queued_id]["replays_interrupted_message"] = bool(event.get("replays_interrupted_message"))
                if event_type == "turn_queue_run_now" and queued_id in order:
                    order.remove(queued_id)
                    order.insert(0, queued_id)
            elif event_type == "turn_queue_reordered":
                positions = event.get("positions") or []
                try:
                    ordered = sorted(
                        [item for item in positions if item.get("queued_id") in pending],
                        key=lambda item: int(item.get("position") or 0),
                    )
                    seen = [str(item.get("queued_id")) for item in ordered]
                    order = seen + [qid for qid in order if qid not in seen]
                except Exception:
                    pass
            elif event_type in {"turn_started", "turn_unqueued"} and queued_id:
                pending.pop(queued_id, None)
                if queued_id in order:
                    order.remove(queued_id)
        items = [
            pending[qid]
            for qid in order
            if qid in pending and (
                str(pending[qid].get("prompt") or "").strip()
                or bool(pending[qid].get("file_ids"))
            )
        ]
        if items:
            QUEUED_TURNS[session_id] = deque(items)
            rebuilt += len(items)
    return rebuilt


async def terminate_process_tree(proc: asyncio.subprocess.Process, *, grace: float = STOP_GRACE_SECONDS) -> bool:
    if proc.returncode is not None:
        return False

    sent = False
    if os.name != "nt":
        with suppress(ProcessLookupError, PermissionError):
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGTERM)
                sent = True
    if not sent:
        with suppress(ProcessLookupError):
            proc.terminate()
            sent = True

    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return sent
    except asyncio.TimeoutError:
        pass

    killed = False
    if os.name != "nt":
        with suppress(ProcessLookupError, PermissionError):
            pgid = os.getpgid(proc.pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGKILL)
                killed = True
    if not killed:
        with suppress(ProcessLookupError):
            proc.kill()
            killed = True
    await proc.wait()
    return sent or killed


def process_group_for_pid(pid: int) -> int | None:
    with suppress(ProcessLookupError, PermissionError, OSError):
        return os.getpgid(pid)
    return None


def procfs_process_rows() -> list[dict[str, Any]]:
    if os.name == "nt" or not Path("/proc").is_dir():
        return []
    with suppress(Exception):
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    if "uptime" not in locals():
        uptime = 0.0
    clk_tck = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
    page_size = os.sysconf("SC_PAGE_SIZE")
    mem_total_kb = 0
    with suppress(Exception):
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = int(line.split()[1])
                break

    rows: list[dict[str, Any]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            stat = (entry / "stat").read_text()
            right = stat.rfind(")")
            if right < 0:
                continue
            comm = stat[stat.find("(") + 1:right]
            fields = stat[right + 2:].split()
            if len(fields) < 20:
                continue
            state = fields[0]
            ppid = int(fields[1])
            pgid = int(fields[2])
            sid = int(fields[3])
            start_ticks = int(fields[19])
            elapsed = int(max(0, uptime - (start_ticks / clk_tck))) if uptime else 0
            rss_kb = 0
            with suppress(Exception):
                statm = (entry / "statm").read_text().split()
                if len(statm) > 1:
                    rss_kb = int(int(statm[1]) * page_size / 1024)
            args = ""
            with suppress(Exception):
                raw = (entry / "cmdline").read_bytes()
                args = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
            if not args:
                args = comm
            rows.append({
                "pid": pid,
                "ppid": ppid,
                "pgid": pgid,
                "sid": sid,
                "stat": state,
                "elapsed_seconds": elapsed,
                "cpu_percent": 0.0,
                "mem_percent": (rss_kb / mem_total_kb * 100.0) if mem_total_kb else 0.0,
                "rss_kb": rss_kb,
                "command": comm,
                "args": args,
            })
        except (OSError, PermissionError, ValueError):
            continue
    return rows


def ps_process_rows() -> list[dict[str, Any]]:
    proc_rows = procfs_process_rows()
    if proc_rows:
        return proc_rows
    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,pgid=,sid=,stat=,etimes=,pcpu=,pmem=,rss=,comm=,args="],
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("process snapshot ps scan timed out")
        return []
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 10)
        if len(parts) < 10:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
            sid = int(parts[3])
            etimes = int(float(parts[5]))
            cpu = float(parts[6])
            mem = float(parts[7])
            rss = int(float(parts[8]))
        except ValueError:
            continue
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "sid": sid,
            "stat": parts[4],
            "elapsed_seconds": etimes,
            "cpu_percent": cpu,
            "mem_percent": mem,
            "rss_kb": rss,
            "command": parts[9],
            "args": parts[10] if len(parts) > 10 else parts[9],
        })
    return rows


def parse_ps_rows(stdout: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        parts = line.strip().split(None, 10)
        if len(parts) < 10:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            pgid = int(parts[2])
            sid = int(parts[3])
            etimes = int(float(parts[5]))
            cpu = float(parts[6])
            mem = float(parts[7])
            rss = int(float(parts[8]))
        except ValueError:
            continue
        rows.append({
            "pid": pid,
            "ppid": ppid,
            "pgid": pgid,
            "sid": sid,
            "stat": parts[4],
            "elapsed_seconds": etimes,
            "cpu_percent": cpu,
            "mem_percent": mem,
            "rss_kb": rss,
            "command": parts[9],
            "args": parts[10] if len(parts) > 10 else parts[9],
        })
    return rows


def top_process_rows(limit: int = 20) -> list[dict[str, Any]]:
    command = ["ps", "-eo", "pid=,ppid=,pgid=,sid=,stat=,etimes=,pcpu=,pmem=,rss=,comm=,args=", "--sort=-pcpu"]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=3, check=False)
        if result.returncode != 0:
            result = subprocess.run(command[:-1], text=True, capture_output=True, timeout=3, check=False)
    except subprocess.TimeoutExpired:
        logger.warning("top process ps scan timed out")
        return []
    rows = parse_ps_rows(result.stdout)
    if not rows:
        rows = sorted(procfs_process_rows(), key=lambda row: int(row.get("rss_kb") or 0), reverse=True)
    return rows[: max(1, min(limit, 50))]


def proc_cwd(pid: int) -> str | None:
    if os.name == "nt":
        return None
    with suppress(OSError, PermissionError):
        return os.readlink(f"/proc/{pid}/cwd")
    return None


def unique_log_hints(hints: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, str]] = []
    for hint in hints:
        path = hint.get("path", "")
        source = hint.get("source", "")
        key = (path, source)
        if not path or key in seen:
            continue
        seen.add(key)
        out.append(hint)
    return out[:8]


def fd_log_hints(pid: int) -> list[dict[str, str]]:
    hints: list[dict[str, str]] = []
    if os.name == "nt":
        return hints
    fd_dir = Path("/proc") / str(pid) / "fd"
    for fd, label in (("1", "stdout"), ("2", "stderr")):
        try:
            target = os.readlink(fd_dir / fd)
        except OSError:
            continue
        if target.startswith(("pipe:", "socket:", "anon_inode:")):
            continue
        clean = target.removesuffix(" (deleted)")
        path = Path(clean)
        if path.is_file():
            hints.append({"source": label, "path": str(path)})
    return hints


def command_log_hints(args: str, cwd: str | None) -> list[dict[str, str]]:
    try:
        tokens = shlex.split(args)
    except ValueError:
        tokens = args.split()
    hints: list[dict[str, str]] = []
    base = Path(cwd) if cwd else None
    for token in tokens:
        clean = token.strip("'\" ,;")
        if not clean:
            continue
        suffix = Path(clean).suffix.lower()
        if suffix not in LOG_PATH_SUFFIXES:
            continue
        candidates = [Path(clean)]
        if base and not Path(clean).is_absolute():
            candidates.append(base / clean)
        for candidate in candidates:
            if candidate.is_file():
                hints.append({"source": "command", "path": str(candidate.resolve())})
                break
    return hints


def tmux_bin() -> str:
    found = shutil.which("tmux")
    if not found:
        raise HTTPException(status_code=503, detail="tmux is not installed on the agent server")
    return found


def tmux_capability() -> dict[str, Any]:
    available = shutil.which("tmux") is not None
    if available:
        return {
            "available": True,
            "required": True,
            "message": "tmux is available.",
            "action": None,
        }
    if sys.platform == "darwin":
        action = "Install tmux with Homebrew (brew install tmux), then rerun the AgentsServer installer."
    elif sys.platform.startswith("linux"):
        action = (
            "Install tmux with your package manager (for example: sudo apt install tmux, "
            "sudo dnf install tmux, or sudo pacman -S tmux), then rerun the AgentsServer installer."
        )
    else:
        action = "Install tmux on the agent host, then rerun the AgentsServer installer."
    return {
        "available": False,
        "required": True,
        "message": "tmux is required for persistent terminals and detached managed updates but was not found on the server PATH.",
        "action": action,
    }


def terminal_session_name(session_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]", "_", session_id).strip("_") or "session"
    return f"zd_{clean[:80]}"


def run_tmux(args: list[str], *, check: bool = True, timeout: float = TMUX_COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            [tmux_bin(), *args],
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="tmux command timed out")
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "tmux command failed").strip()
        raise HTTPException(status_code=500, detail=detail[:1000])
    return result


def tmux_session_exists(name: str) -> bool:
    return run_tmux(["has-session", "-t", name], check=False).returncode == 0


def terminal_dimensions(columns: int | None = None, rows: int | None = None) -> tuple[int, int]:
    cols = max(TERMINAL_MIN_COLUMNS, min(int(columns or 120), TERMINAL_MAX_COLUMNS))
    lines = max(TERMINAL_MIN_ROWS, min(int(rows or 36), TERMINAL_MAX_ROWS))
    return cols, lines


def set_pty_dimensions(fd: int, columns: int, rows: int) -> None:
    cols, lines = terminal_dimensions(columns, rows)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", lines, cols, 0, 0))


def resize_terminal_window(session_id: str, columns: int, rows: int) -> tuple[int, int]:
    cols, lines = terminal_dimensions(columns, rows)
    name = terminal_session_name(session_id)
    # PTY TIOCSWINSZ alone is not enough when a persistent tmux session has
    # previously been attached from a differently sized Mac or iPad.
    run_tmux(["resize-window", "-t", name, "-x", str(cols), "-y", str(lines)], check=False)
    return cols, lines


def ensure_terminal_session(
    session_id: str,
    cwd: str | None = None,
    *,
    columns: int | None = None,
    rows: int | None = None,
) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if bool(sess.get("archived")):
        raise HTTPException(status_code=409, detail="unarchive this chat before opening its terminal")
    name = terminal_session_name(session_id)
    created = False
    if not tmux_session_exists(name):
        workdir = existing_cwd(cwd or sess.get("cwd") or DEFAULT_CWD)
        cols, lines = terminal_dimensions(columns, rows)
        run_tmux(["new-session", "-d", "-s", name, "-x", str(cols), "-y", str(lines), "-c", workdir])
        created = True
    # These are session-scoped defaults. Existing user-created windows and
    # panes remain untouched while newly created panes keep useful history.
    run_tmux(["set-option", "-t", name, "history-limit", "100000"], check=False)
    mouse_initialized = run_tmux(
        ["show-options", "-qv", "-t", name, "@agentsdock_mouse_initialized"],
        check=False,
    ).stdout.strip()
    if created or mouse_initialized != "1":
        # Local selection is the useful default in an embedded terminal. Users
        # can opt into tmux mouse capture from the terminal actions menu.
        run_tmux(["set-option", "-t", name, "mouse", "off"], check=False)
        run_tmux(["set-option", "-t", name, "@agentsdock_mouse_initialized", "1"], check=False)
    run_tmux(["set-option", "-t", name, "window-size", "latest"], check=False)
    if columns is not None or rows is not None:
        resize_terminal_window(session_id, columns or 120, rows or 36)
    # AgentsDock renders its own window tabs and terminal controls. The tmux
    # status line would duplicate those controls and consume a row in the PTY.
    run_tmux(["set-option", "-t", name, "status", "off"], check=False)
    return terminal_snapshot(session_id, created=created)


def spawn_terminal_client(
    session_id: str,
    cwd: str | None,
    columns: int,
    rows: int,
) -> tuple[subprocess.Popen[bytes], int, str]:
    snapshot = ensure_terminal_session(session_id, cwd, columns=columns, rows=rows)
    name = str(snapshot["name"])
    workdir = existing_cwd(snapshot.get("cwd") or cwd or STORE.sessions[session_id].get("cwd") or DEFAULT_CWD)
    master_fd, slave_fd = pty.openpty()
    env = os.environ.copy()
    # A server launched from tmux must still be able to attach a child client.
    # Keeping these variables makes tmux reject the nested attachment.
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env.setdefault("LANG", "C.UTF-8")
    try:
        set_pty_dimensions(slave_fd, columns, rows)
        process = subprocess.Popen(
            [tmux_bin(), "attach-session", "-t", name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=workdir,
            env=env,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return process, master_fd, name


def write_terminal_input(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "terminal input closed")
        view = view[written:]


async def read_terminal_output(fd: int) -> bytes:
    loop = asyncio.get_running_loop()
    ready: asyncio.Future[bytes] = loop.create_future()

    def on_readable() -> None:
        if ready.done():
            return
        try:
            ready.set_result(os.read(fd, TERMINAL_READ_BYTES))
        except OSError as exc:
            ready.set_exception(exc)

    loop.add_reader(fd, on_readable)
    try:
        return await ready
    finally:
        loop.remove_reader(fd)


def stop_terminal_client(process: subprocess.Popen[bytes], master_fd: int) -> None:
    with suppress(OSError):
        os.close(master_fd)
    if process.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=1.5)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=1)


def terminal_snapshot(session_id: str, *, lines: int = 240, created: bool = False) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    exists = tmux_session_exists(name)
    line_count = max(20, min(int(lines or 240), TMUX_CAPTURE_MAX_LINES))
    capture = ""
    cwd = None
    command = None
    pane_pid = None
    attached = None
    columns = None
    rows = None
    if exists:
        capture = run_tmux(["capture-pane", "-t", name, "-p", "-J", "-S", f"-{line_count}"]).stdout
        meta = run_tmux(
            [
                "display-message",
                "-p",
                "-t",
                name,
                "#{pane_current_path}\t#{pane_current_command}\t#{pane_pid}\t#{session_attached}\t#{pane_width}\t#{pane_height}",
            ],
            check=False,
        )
        if meta.returncode == 0:
            parts = meta.stdout.rstrip("\n").split("\t")
            cwd = parts[0] if len(parts) > 0 and parts[0] else None
            command = parts[1] if len(parts) > 1 and parts[1] else None
            pane_pid = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
            attached = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            columns = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else None
            rows = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else None
    return {
        "session_id": session_id,
        "name": name,
        "exists": exists,
        "created": created,
        "cwd": cwd,
        "command": command,
        "pane_pid": pane_pid,
        "attached": attached,
        "columns": columns,
        "rows": rows,
        "lines": line_count,
        "text": capture,
        "updated_at": now_iso(),
    }


def send_terminal_input(session_id: str, text: str | None = None, *, enter: bool = True, key: str | None = None) -> dict[str, Any]:
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        ensure_terminal_session(session_id)
    if key:
        run_tmux(["send-keys", "-t", name, key])
    if text:
        run_tmux(["send-keys", "-t", name, "-l", text])
    if enter:
        run_tmux(["send-keys", "-t", name, "Enter"])
    return terminal_snapshot(session_id)


def resize_terminal_pane(session_id: str, columns: int, rows: int) -> dict[str, Any]:
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        ensure_terminal_session(session_id)
    cols, line_count = resize_terminal_window(session_id, columns, rows)
    return terminal_snapshot(session_id, lines=line_count)


def scroll_terminal_history(session_id: str, delta: int) -> bool:
    """Scroll the active tmux pane's persistent history without enabling mouse capture."""
    if session_id not in STORE.sessions:
        return False
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        return False
    amount = max(1, min(abs(int(delta or 0)), 80))
    if not delta:
        return False
    pane_in_mode = run_tmux(
        ["display-message", "-p", "-t", name, "#{pane_in_mode}"],
        check=False,
    ).stdout.strip() == "1"
    if delta < 0:
        if not pane_in_mode:
            run_tmux(["copy-mode", "-e", "-t", name], check=False)
        run_tmux(["send-keys", "-X", "-N", str(amount), "-t", name, "scroll-up"], check=False)
    elif pane_in_mode:
        run_tmux(["send-keys", "-X", "-N", str(amount), "-t", name, "scroll-down"], check=False)
    return run_tmux(
        ["display-message", "-p", "-t", name, "#{pane_in_mode}"],
        check=False,
    ).stdout.strip() == "1"


def exit_terminal_auto_scroll(session_id: str) -> None:
    if shutil.which("tmux") is None:
        return
    name = terminal_session_name(session_id)
    if tmux_session_exists(name):
        run_tmux(["send-keys", "-X", "-t", name, "cancel"], check=False)


def kill_terminal_session(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    existed = False
    if shutil.which("tmux") is not None:
        existed = tmux_session_exists(name)
        if existed:
            run_tmux(["kill-session", "-t", name], check=False)
    return {
        "session_id": session_id,
        "name": name,
        "exists": False,
        "killed": existed,
        "text": "",
        "updated_at": now_iso(),
    }


def terminal_windows_snapshot(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        return {"session_id": session_id, "name": name, "exists": False, "mouse_enabled": False, "windows": []}
    result = run_tmux([
        "list-windows",
        "-t",
        name,
        "-F",
        "#{window_id}\t#{window_index}\t#{window_name}\t#{window_active}\t#{window_panes}",
    ])
    windows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        windows.append({
            "id": parts[0],
            "index": int(parts[1]) if parts[1].isdigit() else 0,
            "name": parts[2] or "shell",
            "active": parts[3] == "1",
            "panes": int(parts[4]) if parts[4].isdigit() else 1,
        })
    mouse = run_tmux(["show-options", "-qv", "-t", name, "mouse"], check=False).stdout.strip()
    return {
        "session_id": session_id,
        "name": name,
        "exists": True,
        "mouse_enabled": mouse == "on",
        "windows": windows,
    }


def terminal_action(session_id: str, action: str, target: str | None = None) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    if not tmux_session_exists(name):
        ensure_terminal_session(session_id)
    clean_action = str(action or "").strip().lower()
    active_cwd = run_tmux(
        ["display-message", "-p", "-t", name, "#{pane_current_path}"],
        check=False,
    ).stdout.strip()
    workdir = existing_cwd(active_cwd or STORE.sessions[session_id].get("cwd") or DEFAULT_CWD)
    if clean_action == "new-window":
        run_tmux(["new-window", "-t", name, "-c", workdir])
    elif clean_action == "split-right":
        run_tmux(["split-window", "-h", "-t", name, "-c", workdir])
    elif clean_action == "split-down":
        run_tmux(["split-window", "-v", "-t", name, "-c", workdir])
    elif clean_action == "next-window":
        run_tmux(["next-window", "-t", name])
    elif clean_action == "previous-window":
        run_tmux(["previous-window", "-t", name])
    elif clean_action == "select-window":
        clean_target = str(target or "").strip()
        if not re.fullmatch(r"\d+", clean_target):
            raise HTTPException(status_code=400, detail="invalid tmux window")
        run_tmux(["select-window", "-t", f"{name}:{clean_target}"])
    elif clean_action == "kill-window":
        clean_target = str(target or "").strip()
        if not re.fullmatch(r"\d+", clean_target):
            raise HTTPException(status_code=400, detail="invalid tmux window")
        windows = terminal_windows_snapshot(session_id)["windows"]
        if len(windows) <= 1:
            raise HTTPException(
                status_code=409,
                detail="This is the final window. Kill the terminal session instead.",
            )
        if not any(str(window["index"]) == clean_target for window in windows):
            raise HTTPException(status_code=404, detail="tmux window not found")
        run_tmux(["kill-window", "-t", f"{name}:{clean_target}"])
    elif clean_action == "kill-pane":
        pane_count = run_tmux(["list-panes", "-t", name, "-F", "#{pane_id}"]).stdout.splitlines()
        if len(pane_count) <= 1:
            raise HTTPException(status_code=409, detail="This is the last pane. Kill the terminal session instead.")
        run_tmux(["kill-pane", "-t", name])
    elif clean_action == "toggle-mouse":
        current = run_tmux(["show-options", "-qv", "-t", name, "mouse"], check=False).stdout.strip()
        run_tmux(["set-option", "-t", name, "mouse", "off" if current == "on" else "on"])
        run_tmux(["set-option", "-t", name, "@agentsdock_mouse_initialized", "1"], check=False)
    else:
        raise HTTPException(status_code=400, detail="unsupported terminal action")
    return terminal_windows_snapshot(session_id)


TMUX_SUBMITTER_KEYWORDS = (
    "submit", "submitter", "sbatch", "slurm", "osmo", "train", "training",
    "render", "rollout", "eval", "launch", "wandb", "ray", "torchrun",
)
TMUX_CHAT_MATCH_LABELS = {"chat tmux", "chat target"}
TMUX_TARGET_RE = re.compile(
    r"\btmux\b[^\n;&|]*"
    r"(?:\s-[A-Za-z]*[st]\s+|\s--(?:session-name|target|target-pane|target-session)\s+)"
    r"['\"]?([A-Za-z0-9_.:@+-]{4,})"
)
TMUX_NOISE_TOKENS = {
    "bash", "chat", "codex", "claude", "default", "false", "general",
    "home", "launch", "local", "login", "none", "null", "osmo", "python",
    "python3", "script", "scripts", "server", "sleep", "submit", "submitter",
    "tail", "this", "true", "wandb", "agentsdock", "agentsserver",
}


def path_is_within(path: str | None, root: str | None) -> bool:
    if not path or not root:
        return False
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
        base = Path(root).expanduser().resolve(strict=False)
        return candidate == base or base in candidate.parents
    except Exception:
        clean_path = path.rstrip("/")
        clean_root = root.rstrip("/")
        return clean_path == clean_root or clean_path.startswith(clean_root + "/")


def meaningful_chat_cwd(cwd: str | None) -> str | None:
    """Return a cwd only when it is specific enough to link tmux panes to a chat."""
    if not cwd:
        return None
    try:
        path = Path(cwd).expanduser().resolve(strict=False)
        broad_roots = {
            Path("/").resolve(strict=False),
            Path("/tmp").resolve(strict=False),
            Path.home().resolve(strict=False),
        }
        if path in broad_roots:
            return None
        parts = path.parts
        if len(parts) <= 3 and len(parts) >= 2 and parts[1] in {"home", "Users"}:
            return None
        return str(path)
    except Exception:
        clean = str(cwd).strip().rstrip("/")
        if not clean or clean in {"/", "/tmp", str(Path.home())}:
            return None
        if re.fullmatch(r"/(?:home|Users)/[^/]+", clean):
            return None
        return clean


def tmux_target_token(token: str) -> str | None:
    clean = token.strip(" \t\r\n\"'`.,;:()[]{}<>").lower()
    if len(clean) < 4 or clean in TMUX_NOISE_TOKENS:
        return None
    if clean.isdigit():
        return None
    if "/" in clean:
        clean = clean.rsplit("/", 1)[-1]
    if len(clean) < 8 and not re.search(r"[_@:+.-]|\d", clean):
        return None
    return clean


def tmux_explicit_chat_targets(session_id: str, sess: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()

    def add(value: Any) -> None:
        if value is None:
            return
        if clean := tmux_target_token(str(value)):
            tokens.add(clean)

    add(terminal_session_name(session_id))
    for key in ("session_id", "claude_session_id", "codex_thread_id"):
        add(sess.get(key))

    for event in read_events(session_id, limit=240, tail=True):
        text = json.dumps(event, ensure_ascii=False)
        for match in TMUX_TARGET_RE.finditer(text):
            add(match.group(1))
        if len(tokens) > 80:
            break
    return tokens


def descendant_processes(root_pid: int | None, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not root_pid:
        return []
    rows_by_pid = {int(row["pid"]): row for row in rows}
    selected: set[int] = set()
    if root_pid in rows_by_pid:
        selected.add(root_pid)
    changed = True
    while changed:
        changed = False
        for row in rows:
            row_pid = int(row["pid"])
            if row_pid in selected:
                continue
            if int(row.get("ppid") or -1) in selected:
                selected.add(row_pid)
                changed = True
    ordered = ordered_process_tree(root_pid, selected, rows_by_pid)
    out: list[dict[str, Any]] = []
    for row in ordered[:12]:
        pid = int(row["pid"])
        cwd = proc_cwd(pid)
        args = str(row.get("args") or "")
        out.append({
            **row,
            "cwd": cwd,
            "depth": process_depth(pid, rows_by_pid, selected),
            "log_hints": unique_log_hints(fd_log_hints(pid) + command_log_hints(args, cwd)),
        })
    return out


def best_process_label(processes: list[dict[str, Any]], fallback: str | None) -> str:
    shell_names = {"bash", "sh", "zsh", "fish", "tmux", "login"}
    for proc in reversed(processes):
        command = str(proc.get("command") or "").split("/")[-1]
        args = str(proc.get("args") or "").strip()
        if command and command not in shell_names and args:
            return compact_memory_text(args, 240)
    for proc in processes:
        args = str(proc.get("args") or "").strip()
        if args:
            return compact_memory_text(args, 240)
    return fallback or "tmux pane"


def tmux_panes_snapshot(session_id: str, *, include_all: bool = False) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    result = run_tmux([
        "list-panes",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{pane_index}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_active}\t#{session_attached}\t#{pane_title}",
    ], check=False)
    if result.returncode != 0:
        stderr = (result.stderr or result.stdout or "").strip().lower()
        if "no server running" in stderr:
            return {
                "session_id": session_id,
                "panes": [],
                "total_panes": 0,
                "filtered": not include_all,
                "generated_at": now_iso(),
            }
        raise HTTPException(status_code=500, detail=(result.stderr or result.stdout or "tmux list-panes failed").strip()[:1000])

    rows = ps_process_rows()
    chat_cwd = meaningful_chat_cwd(sess.get("cwd") or DEFAULT_CWD)
    chat_tokens = tmux_explicit_chat_targets(session_id, sess)
    owned_name = terminal_session_name(session_id)
    panes: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        while len(parts) < 11:
            parts.append("")
        pane_pid = int(parts[5]) if parts[5].isdigit() else None
        processes = descendant_processes(pane_pid, rows)
        search_text = "\n".join([
            parts[0],
            parts[2],
            parts[6],
            parts[7],
            parts[10],
            *[str(proc.get("args") or "") for proc in processes],
        ]).lower()
        matches: list[str] = []
        if parts[0] == owned_name:
            matches.append("chat tmux")
        if chat_cwd and path_is_within(parts[7], chat_cwd):
            matches.append("chat cwd")
        if chat_tokens and any(token in search_text for token in chat_tokens):
            matches.append("chat target")
        if any(keyword in search_text for keyword in TMUX_SUBMITTER_KEYWORDS):
            matches.append("submitter")
        if any(float(proc.get("cpu_percent") or 0.0) > 2.0 for proc in processes):
            matches.append("active")
        chat_linked = any(match in TMUX_CHAT_MATCH_LABELS for match in matches)
        if not include_all and not chat_linked:
            continue
        panes.append({
            "session_name": parts[0],
            "window_index": int(parts[1]) if parts[1].isdigit() else None,
            "window_name": parts[2] or None,
            "pane_index": int(parts[3]) if parts[3].isdigit() else None,
            "pane_id": parts[4],
            "pane_pid": pane_pid,
            "command": parts[6] or None,
            "cwd": parts[7] or None,
            "active": parts[8] == "1",
            "attached": int(parts[9]) if parts[9].isdigit() else None,
            "title": parts[10] or None,
            "matches": matches,
            "display": best_process_label(processes, parts[6] or parts[0]),
            "processes": processes,
        })
    panes.sort(key=lambda pane: (
        0 if "chat tmux" in pane.get("matches", []) else 1,
        0 if "chat target" in pane.get("matches", []) else 1,
        0 if "chat cwd" in pane.get("matches", []) else 1,
        0 if "submitter" in pane.get("matches", []) else 1,
        0 if "active" in pane.get("matches", []) else 1,
        str(pane.get("session_name") or ""),
        int(pane.get("window_index") or 0),
        int(pane.get("pane_index") or 0),
    ))
    return {
        "session_id": session_id,
        "panes": panes,
        "total_panes": len(result.stdout.splitlines()),
        "filtered": not include_all,
        "generated_at": now_iso(),
    }


def capture_tmux_pane(session_id: str, pane_id: str, *, lines: int = 500) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    clean = str(pane_id or "").strip()
    if not clean:
        raise HTTPException(status_code=400, detail="pane_id is required")
    line_count = max(20, min(int(lines or 500), TMUX_CAPTURE_MAX_LINES))
    # Validate that the target is one of tmux's pane IDs before using it.
    known = tmux_panes_snapshot(session_id, include_all=True)
    if clean not in {pane.get("pane_id") for pane in known.get("panes", [])}:
        raise HTTPException(status_code=404, detail="tmux pane not found")
    text = run_tmux(["capture-pane", "-t", clean, "-p", "-J", "-S", f"-{line_count}"]).stdout
    return {
        "session_id": session_id,
        "pane_id": clean,
        "lines": line_count,
        "text": text,
        "generated_at": now_iso(),
    }


def process_depth(pid: int, rows_by_pid: dict[int, dict[str, Any]], selected: set[int]) -> int:
    depth = 0
    seen: set[int] = set()
    current = rows_by_pid.get(pid)
    while current:
        ppid = int(current.get("ppid") or 0)
        if ppid not in selected or ppid in seen:
            break
        seen.add(ppid)
        depth += 1
        current = rows_by_pid.get(ppid)
    return min(depth, 12)


def ordered_process_tree(root_pid: int, selected: set[int], rows_by_pid: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for pid in selected:
        row = rows_by_pid.get(pid)
        if not row:
            continue
        ppid = int(row.get("ppid") or 0)
        if ppid in selected and ppid != pid:
            children.setdefault(ppid, []).append(pid)
        else:
            roots.append(pid)
    for values in children.values():
        values.sort()

    ordered: list[dict[str, Any]] = []
    visited: set[int] = set()

    def walk(pid: int) -> None:
        if pid in visited:
            return
        row = rows_by_pid.get(pid)
        if not row:
            return
        visited.add(pid)
        ordered.append(row)
        for child in children.get(pid, []):
            walk(child)

    if root_pid in selected:
        walk(root_pid)
    for root in sorted(roots):
        walk(root)
    for pid in sorted(selected - visited):
        walk(pid)
    return ordered


def active_process_snapshot(session_id: str, active: dict[str, Any]) -> dict[str, Any]:
    proc = active.get("proc")
    pid = int(active.get("pid") or (proc.pid if proc else 0) or 0)
    pgid = active.get("pgid")
    if pid and not pgid:
        pgid = process_group_for_pid(pid)
    if not pid:
        return {
            "session_id": session_id,
            "active": False,
            "processes": [],
            "generated_at": now_iso(),
        }

    rows = ps_process_rows()
    rows_by_pid = {int(row["pid"]): row for row in rows}
    selected: set[int] = set()
    if pid in rows_by_pid:
        selected.add(pid)
    if pgid is not None:
        selected.update(int(row["pid"]) for row in rows if int(row.get("pgid") or -1) == int(pgid))
    changed = True
    while changed:
        changed = False
        for row in rows:
            row_pid = int(row["pid"])
            if row_pid in selected:
                continue
            if int(row.get("ppid") or -1) in selected:
                selected.add(row_pid)
                changed = True

    ordered = ordered_process_tree(pid, selected, rows_by_pid)
    processes: list[dict[str, Any]] = []
    for row in ordered:
        row_pid = int(row["pid"])
        cwd = proc_cwd(row_pid)
        hints = unique_log_hints(fd_log_hints(row_pid) + command_log_hints(str(row.get("args") or ""), cwd))
        processes.append({
            **row,
            "cwd": cwd,
            "depth": process_depth(row_pid, rows_by_pid, selected),
            "log_hints": hints,
        })

    return {
        "session_id": session_id,
        "active": bool(processes),
        "run_id": active.get("run_id"),
        "backend": active.get("backend"),
        "pid": pid,
        "pgid": pgid,
        "cwd": active.get("cwd"),
        "argv": active.get("argv") or [],
        "started_at": active.get("started_at_iso"),
        "elapsed_seconds": int(max(0, time.time() - float(active.get("started_at") or time.time()))),
        "stop_requested": bool(active.get("stop_requested")),
        "processes": processes,
        "stdout_tail": live_output_tail(active),
        "generated_at": now_iso(),
    }


def live_output_tail(active: dict[str, Any]) -> dict[str, Any]:
    lines = list(active.get("stdout_lines") or active.get("stdout_tail") or [])
    total_lines = int(active.get("stdout_total_lines") or len(lines))
    return {
        "stream": "stdout",
        "run_id": active.get("run_id"),
        "backend": active.get("backend"),
        "lines": len(lines),
        "total_lines": total_lines,
        "truncated": total_lines > len(lines),
        "text": "\n".join(lines),
        "updated_at": active.get("stdout_updated_at"),
        "generated_at": now_iso(),
    }


def jsonable_active_run(session_id: str, active: dict[str, Any]) -> dict[str, Any]:
    started = float(active.get("started_at") or time.time())
    return {
        "session_id": session_id,
        "run_id": active.get("run_id"),
        "backend": active.get("backend"),
        "pid": active.get("pid"),
        "pgid": active.get("pgid"),
        "cwd": active.get("cwd"),
        "argv": active.get("argv") or [],
        "started_at": active.get("started_at_iso"),
        "elapsed_seconds": int(max(0, time.time() - started)),
        "stop_requested": bool(active.get("stop_requested")),
        "stdout_total_lines": int(active.get("stdout_total_lines") or 0),
        "stdout_updated_at": active.get("stdout_updated_at"),
    }


async def active_run_summaries() -> list[dict[str, Any]]:
    async with ACTIVE_LOCK:
        return [jsonable_active_run(session_id, active) for session_id, active in ACTIVE.items()]


def trim_process_args(row: dict[str, Any], max_chars: int = 500) -> dict[str, Any]:
    out = dict(row)
    args = str(out.get("args") or "")
    if len(args) > max_chars:
        out["args"] = args[:max_chars].rstrip() + "... [trimmed]"
    return out


def write_bounded_jsonl(path: Path, record: dict[str, Any], max_bytes: int) -> None:
    ensure_dirs()
    with suppress(Exception):
        if path.exists() and path.stat().st_size > max_bytes:
            rotated = path.with_suffix(path.suffix + ".1")
            with suppress(FileNotFoundError):
                rotated.unlink()
            path.replace(rotated)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


async def host_health_record() -> dict[str, Any]:
    active_runs = await active_run_summaries()
    active_pgids = {int(run["pgid"]) for run in active_runs if run.get("pgid") is not None}
    top_rows = await asyncio.to_thread(top_process_rows, 20)
    top_processes = []
    for row in top_rows:
        clean = trim_process_args(row)
        clean["tracked_by_agentsdock"] = int(clean.get("pgid") or -1) in active_pgids
        top_processes.append(clean)
    return {
        "ts": now_iso(),
        "pressure": host_pressure_snapshot(),
        "active_runs": active_runs,
        "top_processes": top_processes,
        "jobs": len(JOBS.jobs),
    }


async def host_monitor_loop() -> None:
    logger.info("host health monitor started path=%s", HOST_HEALTH_FILE)
    while True:
        try:
            record = await host_health_record()
            await asyncio.to_thread(write_bounded_jsonl, HOST_HEALTH_FILE, record, HOST_HEALTH_MAX_BYTES)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("host health monitor failed: %s", exc)
        await asyncio.sleep(max(HOST_MONITOR_INTERVAL_SECONDS, 5.0))


async def append_active_stdout(session_id: str, text: str) -> None:
    line = text.rstrip("\r\n")
    if len(line) > LIVE_STDOUT_MAX_LINE_CHARS:
        line = line[:LIVE_STDOUT_MAX_LINE_CHARS] + "\n... stdout line truncated ..."
    async with ACTIVE_LOCK:
        active = ACTIVE.get(session_id)
        if not active:
            return
        tail = active.get("stdout_tail")
        if tail is None:
            tail = deque(maxlen=LIVE_STDOUT_MAX_LINES)
            active["stdout_tail"] = tail
        tail.append(line)
        active["stdout_total_lines"] = int(active.get("stdout_total_lines") or 0) + 1
        active["stdout_updated_at"] = now_iso()


def tail_text_file(path: str, lines: int = 200, max_bytes: int = 512 * 1024) -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_file():
        raise HTTPException(status_code=404, detail="log file not found")
    line_count = max(1, min(int(lines or 200), 1000))
    byte_count = max(4096, min(int(max_bytes or 512 * 1024), 2 * 1024 * 1024))
    size = target.stat().st_size
    with target.open("rb") as f:
        if size > byte_count:
            f.seek(size - byte_count)
        data = f.read(byte_count)
    text = data.decode("utf-8", "replace")
    text = "\n".join(text.splitlines()[-line_count:])
    return {
        "path": str(target),
        "size": size,
        "truncated": size > byte_count,
        "lines": line_count,
        "text": text,
        "generated_at": now_iso(),
    }


def tail_jsonl_file(path: Path, limit: int = 40, max_bytes: int = 2 * 1024 * 1024) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    line_count = max(1, min(int(limit or 40), 200))
    size = path.stat().st_size
    byte_count = max(4096, min(int(max_bytes or 2 * 1024 * 1024), 8 * 1024 * 1024))
    with path.open("rb") as f:
        if size > byte_count:
            f.seek(size - byte_count)
        data = f.read(byte_count)
    records: list[dict[str, Any]] = []
    for line in data.decode("utf-8", "replace").splitlines()[-line_count:]:
        with suppress(Exception):
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                records.append(parsed)
    return records


def event_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = str(block.get("type") or "")
                if block_type in {"text", "input_text", "output_text"} and block.get("text"):
                    parts.append(str(block.get("text") or ""))
                elif block_type == "image":
                    parts.append("[image result]")
                elif block_type == "tool_reference":
                    name = str(block.get("tool_name") or block.get("name") or "tool")
                    parts.append(f"[tool reference: {name}]")
                else:
                    with suppress(Exception):
                        parts.append(json.dumps(block, separators=(",", ":")))
            else:
                parts.append(str(block))
        return compact_memory_text("\n".join(part for part in parts if part.strip()), 12_000)
    if isinstance(value, dict):
        if value.get("text"):
            return compact_memory_text(str(value.get("text") or ""), 12_000)
        if value.get("message"):
            return compact_memory_text(str(value.get("message") or ""), 12_000)
        if value.get("type") == "image":
            return "[image result]"
        with suppress(Exception):
            return compact_memory_text(json.dumps(value, separators=(",", ":")), 12_000)
    return str(value)


def client_safe_event(event: dict[str, Any]) -> dict[str, Any]:
    output = event.get("output")
    if output is None or isinstance(output, str):
        return event
    safe = dict(event)
    safe["output"] = event_output_text(output)
    return safe


async def release_turn_slot(session_id: str) -> None:
    async with ACTIVE_LOCK:
        ACTIVE.pop(session_id, None)
        BUSY_SESSIONS.discard(session_id)
        CURRENT_TURNS.pop(session_id, None)
        STOP_REQUESTS.discard(session_id)


async def clear_active_process(session_id: str) -> None:
    async with ACTIVE_LOCK:
        ACTIVE.pop(session_id, None)


def active_snapshot_input(active: dict[str, Any]) -> dict[str, Any]:
    snapshot_input = {
        key: value for key, value in active.items()
        if key not in {"proc", "stdout_tail"}
    }
    snapshot_input["proc"] = active.get("proc")
    snapshot_input["stdout_lines"] = list(active.get("stdout_tail") or [])
    return snapshot_input


def read_events(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = 500,
    *,
    tail: bool = False,
    visible: bool = False,
) -> list[dict[str, Any]]:
    path = events_path(session_id)
    if not path.exists():
        return []
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    out: list[dict[str, Any]] = []
    tail_out: deque[dict[str, Any]] | None = deque(maxlen=limit) if tail else None
    with path.open("r", encoding="utf-8", errors="ignore") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            seq = int(event.get("seq", 0))
            if seq > after and (before is None or seq < before):
                event = client_safe_event(event)
                if visible and not is_visible_timeline_event(event):
                    continue
                if tail_out is not None:
                    tail_out.append(event)
                else:
                    out.append(event)
            if tail_out is None and len(out) >= limit:
                break
    return list(tail_out) if tail_out is not None else out


COMPACT_TIMELINE_HIDDEN_TYPES = {
    "raw_event",
    "reasoning_summary",
    "tool_started",
    "tool_finished",
    "process_started",
    "provider_session",
    "cwd_fallback",
    "history_imported",
    "backend_changed",
    "session_created",
    "code_diff",
}


def is_visible_timeline_event(event: dict[str, Any], *, compact: bool = False) -> bool:
    event_type = str(event.get("type") or "")
    if compact:
        return event_type not in COMPACT_TIMELINE_HIDDEN_TYPES
    return event_type != "raw_event"


def read_visible_events_page(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = 500,
    *,
    tail: bool = False,
    compact: bool = False,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    path = events_path(session_id)
    if not path.exists():
        return [], 0, 0, 0, 0
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    out: list[dict[str, Any]] = []
    tail_out: deque[dict[str, Any]] | None = deque(maxlen=limit) if tail else None
    latest_seq = 0
    visible_count = 0
    with path.open("r", encoding="utf-8", errors="ignore") as source:
        for line in source:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            seq = int(event.get("seq", 0))
            if seq > 0:
                latest_seq = seq
            if seq <= after or (before is not None and seq >= before):
                continue
            event = client_safe_event(event)
            if not is_visible_timeline_event(event, compact=compact):
                continue
            visible_count += 1
            if tail_out is not None:
                tail_out.append(event)
            elif len(out) < limit:
                out.append(event)
    events = list(tail_out) if tail_out is not None else out
    if tail:
        omitted_before = max(0, visible_count - len(events))
        omitted_after = 0
    else:
        omitted_before = 0
        omitted_after = max(0, visible_count - len(events))
    return events, latest_seq, visible_count, omitted_before, omitted_after


def read_visible_events_after_page(
    session_id: str,
    after: int,
    limit: int = 500,
    *,
    compact: bool = False,
) -> tuple[list[dict[str, Any]], int, int, int, int]:
    """Read a recent append-only delta without rescanning the whole transcript."""
    path = events_path(session_id)
    if not path.exists() or path.stat().st_size <= 0:
        return [], 0, 0, 0, 0
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    selected: deque[dict[str, Any]] = deque(maxlen=limit)
    visible_count = 0
    latest_seq = last_event_seq_from_file(path)

    try:
        with path.open("rb") as source, mmap.mmap(source.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
            cursor = len(mapped)
            while cursor > 0:
                line_end = cursor
                newline = mapped.rfind(b"\n", 0, line_end)
                line_start = newline + 1
                cursor = newline if newline >= 0 else 0
                raw = mapped[line_start:line_end].strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except Exception:
                    continue
                seq = int(event.get("seq", 0))
                if seq <= after:
                    break
                if not is_visible_timeline_event(event, compact=compact):
                    continue
                visible_count += 1
                selected.appendleft(client_safe_event(event))
    except Exception as exc:
        logger.warning("fast event delta failed session=%s: %s", session_id, exc)
        return read_visible_events_page(session_id, after=after, limit=limit, tail=False, compact=compact)

    events = list(selected)
    return events, latest_seq, visible_count, 0, max(0, visible_count - len(events))


def event_seq_bounds(session_id: str) -> tuple[int, int, int]:
    path = events_path(session_id)
    if not path.exists():
        return 0, 0, 0
    first_seq = 0
    latest_seq = 0
    count = 0
    for line in path.open("r", encoding="utf-8", errors="ignore"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        seq = int(event.get("seq", 0))
        if seq <= 0:
            continue
        if first_seq <= 0:
            first_seq = seq
        latest_seq = seq
        count += 1
    return first_seq, latest_seq, count


TIMELINE_INDEX_HIDDEN_TYPES = {"turn_queued", "turn_unqueued", "queue_snapshot", "raw_event"}
TIMELINE_INDEX_JOB_TYPES = {"job_created", "job_ran", "job_started", "job_deferred", "job_finished", "job_error"}
TIMELINE_INDEX_TRACE_TYPES = {
    "reasoning_summary", "tool_started", "tool_finished", "process_started", "provider_session",
    "cwd_fallback", "history_imported", "backend_changed", "artifact_error", "session_created",
    "idle_warning",
}


def compact_timeline_index_text(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        if isinstance(value, dict):
            value = value.get("message") or value.get("error") or value
        try:
            value = json.dumps(value, ensure_ascii=False)
        except Exception:
            value = str(value)
    compact = re.sub(r"\s+", " ", value).strip()
    if len(compact) <= limit:
        return compact
    return compact[: max(1, limit - 1)].rstrip() + "…"


def timeline_index_event_text(event: dict[str, Any]) -> str:
    for field in ("result_text", "text", "prompt", "digest", "message", "error", "output"):
        text = compact_timeline_index_text(event.get(field))
        if text:
            return text
    return ""


def timeline_search_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    if isinstance(value, dict):
        for key in ("message", "error", "detail"):
            text = timeline_search_value(value.get(key))
            if text:
                return text
    try:
        return re.sub(r"\s+", " ", json.dumps(value, ensure_ascii=False)).strip()
    except Exception:
        return re.sub(r"\s+", " ", str(value)).strip()


def timeline_search_event_text(event: dict[str, Any]) -> str:
    for field in ("result_text", "text", "prompt", "digest", "message", "error"):
        text = timeline_search_value(event.get(field))
        if text:
            return text
    return ""


def timeline_search_snippet(text: str, tokens: list[str], limit: int = 260) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    folded = compact.casefold()
    positions = [folded.find(token) for token in tokens if token and folded.find(token) >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - limit // 3)
    end = min(len(compact), start + limit)
    start = max(0, end - limit)
    return ("…" if start else "") + compact[start:end].strip() + ("…" if end < len(compact) else "")


def timeline_search_tokens(query: str) -> list[str]:
    matches = re.findall(r'"([^"]+)"|(\S+)', query.strip())
    return [(phrase or word).casefold() for phrase, word in matches if phrase or word]


def timeline_search_fts_query(query: str) -> str:
    matches = re.findall(r'"([^"]+)"|(\S+)', query.strip())
    terms: list[str] = []
    for phrase, word in matches:
        value = (phrase or word).casefold()
        if not value:
            continue
        escaped = value.replace('"', '""')
        terms.append(f'"{escaped}"' if phrase else f'"{escaped}"*')
    return " AND ".join(terms)


def timeline_index_is_error(event: dict[str, Any]) -> bool:
    event_type = str(event.get("type") or "")
    if event_type in {"tool_started", "tool_finished", "raw_event"}:
        return False
    return event_type == "error" or event_type.endswith("_error") or event.get("is_error") is True or bool(event.get("error"))


def build_timeline_index(session_id: str) -> dict[str, Any]:
    lock = TIMELINE_INDEX_LOCKS.setdefault(session_id, threading.Lock())
    with lock:
        return _build_timeline_index_locked(session_id)


def _build_timeline_index_locked(session_id: str) -> dict[str, Any]:
    path = events_path(session_id)
    if not path.exists():
        return {
            "session_id": session_id,
            "landmarks": [],
            "latest_seq": 0,
            "event_count": 0,
            "generated_at": now_iso(),
        }
    stat = path.stat()
    signature = (stat.st_size, stat.st_mtime_ns)
    cached = TIMELINE_INDEX_CACHE.get(session_id)
    if cached and cached.get("signature") == signature:
        TIMELINE_INDEX_CACHE.move_to_end(session_id)
        return cached["payload"]

    can_append = bool(
        cached and cached.get("inode") == stat.st_ino and
        0 <= int(cached.get("offset") or 0) < stat.st_size
    )
    records: list[dict[str, Any]] = cached["records"] if can_append else []
    by_key: dict[str, dict[str, Any]] = {record["key"]: record for record in records}
    active_turn_key: str | None = cached.get("active_turn_key") if can_append else None
    current_turn_by_run: dict[str, str] = dict(cached.get("current_turn_by_run") or {}) if can_append else {}
    job_by_run: dict[str, str] = dict(cached.get("job_by_run") or {}) if can_append else {}
    visible_count = int(cached.get("visible_count") or 0) if can_append else 0
    latest_seq = int(cached.get("latest_seq") or 0) if can_append else 0
    scan_offset = int(cached.get("offset") or 0) if can_append else 0

    def ensure_record(key: str, kind: str, event: dict[str, Any]) -> dict[str, Any]:
        record = by_key.get(key)
        seq = int(event.get("seq") or 0)
        if record is None:
            record = {
                "key": key,
                "kind": kind,
                "start_seq": seq,
                "end_seq": seq,
                "title": "",
                "preview": "",
                "meta": "",
                "timestamp": event.get("ts"),
                "tool_count": 0,
                "thought_count": 0,
                "event_count": 0,
                "file_names": [],
                "has_user": False,
                "search_entries": [],
                "_search_values": set(),
            }
            by_key[key] = record
            records.append(record)
        record.setdefault("search_entries", [])
        record.setdefault("_search_values", set())
        record["start_seq"] = min(int(record["start_seq"]), seq)
        record["end_seq"] = max(int(record["end_seq"]), seq)
        record["event_count"] += 1
        return record

    def add_search_entry(record: dict[str, Any], event: dict[str, Any], role: str, text: str | None = None) -> None:
        value = text if text is not None else timeline_search_event_text(event)
        value = re.sub(r"\s+", " ", value or "").strip()
        if not value:
            return
        search_values = record.setdefault("_search_values", set())
        fingerprint = (role, value)
        if fingerprint in search_values:
            return
        search_values.add(fingerprint)
        record.setdefault("search_entries", []).append({
            "event_id": str(event.get("id") or event.get("seq") or ""),
            "seq": int(event.get("seq") or 0),
            "ts": event.get("ts"),
            "role": role,
            "text": value,
        })

    final_offset = scan_offset
    with path.open("rb") as source:
        source.seek(scan_offset)
        for raw_line in source:
            if not raw_line.strip():
                continue
            try:
                event = json.loads(raw_line.decode("utf-8", "replace"))
            except Exception:
                continue
            seq = int(event.get("seq") or 0)
            event_type = str(event.get("type") or "")
            run_id = str(event.get("run_id") or "").strip()
            if seq <= 0:
                continue
            latest_seq = max(latest_seq, seq)
            if event_type in TIMELINE_INDEX_HIDDEN_TYPES:
                continue
            visible_count += 1

            digest_id = str(event.get("digest_job_id") or "").strip()
            is_digest_lifecycle = event_type.startswith("handoff_digest_")
            is_digest_generation = event.get("purpose") == "handoff_digest"
            if digest_id and (is_digest_lifecycle or is_digest_generation):
                record = ensure_record(f"digest:{digest_id}", "digest", event)
                if run_id and is_digest_generation:
                    current_turn_by_run[run_id] = record["key"]
                    active_turn_key = record["key"]
                text = timeline_index_event_text(event)
                if text and (is_digest_lifecycle or not record.get("preview")):
                    record["preview"] = text
                if event_type == "handoff_digest_received":
                    record["title"] = "Context Digest"
                    add_search_entry(record, event, "system", timeline_search_event_text(event))
                elif event_type == "handoff_digest_sent":
                    record["title"] = "Digest Sent"
                elif event_type == "handoff_digest_error":
                    record["title"] = "Digest Failed"
                elif not record.get("title"):
                    record["title"] = "Creating Digest"
                if event_type == "turn_finished":
                    if active_turn_key == record["key"]:
                        active_turn_key = None
                    if run_id:
                        current_turn_by_run.pop(run_id, None)
                continue

            if event_type in TIMELINE_INDEX_JOB_TYPES or event.get("job_id") or (run_id and run_id in job_by_run):
                job = event.get("job") if isinstance(event.get("job"), dict) else {}
                job_id = event.get("job_id") or job.get("id") or job_by_run.get(run_id) or run_id or f"job-{seq}"
                if run_id:
                    job_by_run[run_id] = str(job_id)
                record = ensure_record(f"job:{job_id}", "job", event)
                prior_key = current_turn_by_run.pop(run_id, None) if run_id else None
                if prior_key and prior_key != record["key"]:
                    prior = by_key.pop(prior_key, None)
                    if prior is not None:
                        record["start_seq"] = min(int(record["start_seq"]), int(prior["start_seq"]))
                        record["end_seq"] = max(int(record["end_seq"]), int(prior["end_seq"]))
                        for field in ("event_count", "tool_count", "thought_count"):
                            record[field] = int(record.get(field) or 0) + int(prior.get(field) or 0)
                        for file_name in prior.get("file_names") or []:
                            if file_name not in record["file_names"]:
                                record["file_names"].append(file_name)
                        for field in ("prompt", "trace_preview"):
                            if not record.get(field) and prior.get(field):
                                record[field] = prior[field]
                        search_values = record.setdefault("_search_values", set())
                        for entry in prior.get("search_entries") or []:
                            fingerprint = (entry.get("role"), entry.get("text"))
                            if fingerprint in search_values:
                                continue
                            search_values.add(fingerprint)
                            record.setdefault("search_entries", []).append(entry)
                        records.remove(prior)
                    if active_turn_key == prior_key:
                        active_turn_key = None
                record["title"] = compact_timeline_index_text(job.get("title") or record["title"] or event.get("message") or "Scheduled job", 72)
                text = timeline_index_event_text(event)
                if text:
                    record["preview"] = text
                record["timestamp"] = event.get("ts") or record.get("timestamp")
                add_search_entry(record, event, "job")
                continue

            if timeline_index_is_error(event):
                text = timeline_index_event_text(event)
                record = ensure_record(f"event:{event.get('id') or seq}", "error", event)
                record["title"] = compact_timeline_index_text(event_type.replace("_", " ").title() or "Error", 72)
                record["preview"] = text or "Agent error"
                add_search_entry(record, event, "error")
                continue

            if event_type == "turn_started":
                run_key = run_id or f"seq-{seq}"
                key = f"turn:{run_key}"
                if key in by_key and by_key[key].get("has_user"):
                    key = f"turn:{run_key}:start-{seq}"
                active_turn_key = key
                if run_id:
                    current_turn_by_run[run_id] = key
                if event.get("purpose") == "handoff_digest_delivery":
                    ensure_record(key, "assistant", event)
                    continue
                record = ensure_record(key, "user", event)
                record["has_user"] = True
                prompt = compact_timeline_index_text(event.get("prompt"))
                if prompt:
                    record["title"] = compact_timeline_index_text(prompt, 72)
                    record["prompt"] = prompt
                add_search_entry(record, event, "user")
                continue

            key = current_turn_by_run.get(run_id) if run_id else active_turn_key
            if run_id and not key:
                key = f"turn:{run_id}"
            if event_type in {"assistant_text", "turn_finished"}:
                key = key or f"turn:seq-{seq}"
                record = ensure_record(key, "assistant", event)
                if not record.get("has_user") and record.get("kind") != "digest":
                    record["kind"] = "assistant"
                response = compact_timeline_index_text(event.get("result_text") if event_type == "turn_finished" else event.get("text"))
                if response:
                    record["preview"] = response
                    if not record["title"]:
                        record["title"] = compact_timeline_index_text(response, 72)
                add_search_entry(record, event, "assistant")
                if event_type == "turn_finished" and active_turn_key == key:
                    active_turn_key = None
                if event_type == "turn_finished" and run_id:
                    current_turn_by_run.pop(run_id, None)
                continue

            if event_type in {"artifact_created", "file_uploaded"}:
                file_payload = event.get("artifact") or event.get("file")
                if isinstance(file_payload, dict):
                    if event_type == "file_uploaded" and not key:
                        continue
                    key = key or f"turn:seq-{seq}"
                    record = ensure_record(key, "media", event)
                    file_name = compact_timeline_index_text(file_payload.get("title") or file_payload.get("filename"), 72)
                    if file_name and file_name not in record["file_names"]:
                        record["file_names"].append(file_name)
                    if not record["title"] and file_name:
                        record["title"] = file_name
                    add_search_entry(record, event, "file", file_name)
                continue

            if event_type in TIMELINE_INDEX_TRACE_TYPES or key:
                key = key or f"turn:seq-{seq}"
                record = ensure_record(key, "trace", event)
                if event_type == "tool_started":
                    record["tool_count"] += 1
                elif event_type == "reasoning_summary":
                    record["thought_count"] += 1
                text = timeline_index_event_text(event)
                if text and not record.get("trace_preview"):
                    record["trace_preview"] = text
                if event_type == "reasoning_summary":
                    add_search_entry(record, event, "trace")
                continue

            text = timeline_index_event_text(event)
            record = ensure_record(f"event:{event.get('id') or seq}", "system", event)
            record["title"] = compact_timeline_index_text(event_type.replace("_", " ").title() or "System", 72)
            record["preview"] = text or record["title"]
            add_search_entry(record, event, "system")
        final_offset = source.tell()

    landmarks: list[dict[str, Any]] = []
    for stored in records:
        file_names = list(stored.get("file_names") or [])
        tool_count = int(stored.get("tool_count") or 0)
        thought_count = int(stored.get("thought_count") or 0)
        event_count = int(stored.get("event_count") or 0)
        has_user = bool(stored.get("has_user"))
        prompt = str(stored.get("prompt") or "")
        trace_preview = str(stored.get("trace_preview") or "")
        kind = "user" if has_user and stored["kind"] != "digest" else stored["kind"]
        title = compact_timeline_index_text(
            stored.get("title") or prompt or stored.get("preview") or trace_preview or
            (file_names[0] if file_names else "Agent turn"),
            72,
        )
        preview = compact_timeline_index_text(
            stored.get("preview") or trace_preview or ", ".join(file_names) or prompt or title
        )
        meta_parts: list[str] = []
        if tool_count:
            meta_parts.append(f"{tool_count} tool{'s' if tool_count != 1 else ''}")
        if thought_count:
            meta_parts.append(f"{thought_count} thought{'s' if thought_count != 1 else ''}")
        if file_names:
            meta_parts.append(f"{len(file_names)} file{'s' if len(file_names) != 1 else ''}")
        if kind == "job" and not meta_parts:
            meta_parts.append(f"{event_count} update{'s' if event_count != 1 else ''}")
        landmark_seq = stored["end_seq"] if kind == "job" else stored["start_seq"]
        landmarks.append({
            "key": stored["key"],
            "kind": kind,
            "start_seq": landmark_seq,
            "end_seq": stored["end_seq"],
            "title": title,
            "preview": preview,
            "meta": " · ".join(meta_parts),
            "timestamp": stored.get("timestamp"),
        })

    landmarks.sort(key=lambda item: (int(item["start_seq"]), int(item["end_seq"]), str(item["key"])))

    payload = {
        "session_id": session_id,
        "landmarks": landmarks,
        "latest_seq": latest_seq,
        "event_count": visible_count,
        "generated_at": now_iso(),
    }
    TIMELINE_INDEX_CACHE[session_id] = {
        "signature": signature,
        "payload": payload,
        "records": records,
        "active_turn_key": active_turn_key,
        "current_turn_by_run": current_turn_by_run,
        "job_by_run": job_by_run,
        "visible_count": visible_count,
        "latest_seq": latest_seq,
        "offset": final_offset,
        "inode": stat.st_ino,
    }
    TIMELINE_INDEX_CACHE.move_to_end(session_id)
    while len(TIMELINE_INDEX_CACHE) > max(1, TIMELINE_INDEX_CACHE_MAX):
        TIMELINE_INDEX_CACHE.popitem(last=False)
    return payload


def search_timeline_index(session_id: str, query: str, limit: int = 40) -> dict[str, Any]:
    tokens = timeline_search_tokens(query)
    if not tokens:
        return {"session_id": session_id, "query": query, "results": []}
    connection = history_search_connection()
    try:
        state = connection.execute(
            "SELECT offset FROM history_search_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        rows = connection.execute("""
            SELECT event_id, seq, ts, role, text
            FROM history_search
            WHERE history_search MATCH ? AND session_id = ?
            ORDER BY CAST(seq AS INTEGER) DESC
            LIMIT ?
        """, (timeline_search_fts_query(query), session_id, max(1, min(100, limit)))).fetchall()
    finally:
        connection.close()
    with suppress(OSError):
        if not state or int(state[0]) < events_path(session_id).stat().st_size:
            HISTORY_SEARCH_DIRTY.add(session_id)
    results = [{
        "session_id": session_id,
        "event_id": row[0],
        "seq": int(row[1] or 0),
        "ts": row[2],
        "role": row[3] or "system",
        "snippet": timeline_search_snippet(str(row[4] or ""), tokens),
    } for row in rows]
    return {"session_id": session_id, "query": query, "results": results}


HISTORY_SEARCH_EVENT_TYPES = {
    "turn_started", "assistant_text", "turn_finished", "reasoning_summary", "error",
    "job_created", "job_ran", "job_started", "job_deferred", "job_finished", "job_error",
    "artifact_created", "artifact_error", "file_uploaded",
    "handoff_digest_started", "handoff_digest_ready", "handoff_digest_received", "handoff_digest_submitted", "handoff_digest_sent",
}
HISTORY_SEARCH_LINE_MARKERS = tuple(
    f'"type":"{event_type}"'.encode("utf-8") for event_type in sorted(HISTORY_SEARCH_EVENT_TYPES)
) + (b'_error"',)


def history_search_event_record(event: dict[str, Any]) -> tuple[str, str] | None:
    event_type = str(event.get("type") or "")
    if event_type not in HISTORY_SEARCH_EVENT_TYPES and not event_type.endswith("_error"):
        return None
    if event_type in TIMELINE_INDEX_JOB_TYPES or event.get("job_id"):
        role = "job"
    elif timeline_index_is_error(event):
        role = "error"
    elif event_type == "turn_started":
        role = "user"
    elif event_type in {"assistant_text", "turn_finished"}:
        role = "assistant"
    elif event_type == "reasoning_summary":
        role = "trace"
    elif event_type in {"artifact_created", "file_uploaded"}:
        role = "file"
    else:
        role = "system"

    values = [timeline_search_event_text(event)]
    job = event.get("job") if isinstance(event.get("job"), dict) else {}
    values.extend((timeline_search_value(job.get("title")), timeline_search_value(job.get("prompt"))))
    file_payload = event.get("artifact") or event.get("file")
    if isinstance(file_payload, dict):
        values.extend((timeline_search_value(file_payload.get("title")), timeline_search_value(file_payload.get("filename"))))
    text = " ".join(dict.fromkeys(value for value in values if value)).strip()
    return (role, text) if text else None


def history_search_connection() -> sqlite3.Connection:
    ensure_dirs()
    connection = sqlite3.connect(HISTORY_SEARCH_DB, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS history_search USING fts5(
            text,
            session_id UNINDEXED,
            event_id UNINDEXED,
            seq UNINDEXED,
            ts UNINDEXED,
            role UNINDEXED,
            tokenize='unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS history_search_state (
            session_id TEXT PRIMARY KEY,
            inode INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS history_search_sessions (
            session_id TEXT PRIMARY KEY,
            active INTEGER NOT NULL
        );
    """)
    return connection


def sync_history_search_index(
    connection: sqlite3.Connection,
    target_session_ids: set[str],
    active_session_ids: set[str],
    *,
    prune: bool = False,
) -> tuple[int, int]:
    indexed_events = 0
    indexed_sessions = 0
    if prune:
        connection.execute("DELETE FROM history_search_sessions")
        connection.executemany(
            "INSERT INTO history_search_sessions(session_id, active) VALUES (?, 1)",
            ((session_id,) for session_id in active_session_ids),
        )
        state_rows = connection.execute("SELECT session_id FROM history_search_state").fetchall()
        for (stale_id,) in state_rows:
            if stale_id in active_session_ids:
                continue
            connection.execute("DELETE FROM history_search WHERE session_id = ?", (stale_id,))
            connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (stale_id,))
    else:
        for session_id in target_session_ids:
            active = session_id in active_session_ids
            connection.execute(
                "INSERT INTO history_search_sessions(session_id, active) VALUES (?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET active = excluded.active",
                (session_id, int(active)),
            )
            if not active:
                connection.execute("DELETE FROM history_search WHERE session_id = ?", (session_id,))
                connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (session_id,))
    connection.commit()

    insert = "INSERT INTO history_search(text, session_id, event_id, seq, ts, role) VALUES (?, ?, ?, ?, ?, ?)"
    update_state = """
        INSERT INTO history_search_state(session_id, inode, offset, mtime_ns) VALUES (?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            inode = excluded.inode, offset = excluded.offset, mtime_ns = excluded.mtime_ns
    """
    for session_id in target_session_ids & active_session_ids:
        path = events_path(session_id)
        if not path.exists():
            connection.execute("DELETE FROM history_search WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (session_id,))
            connection.commit()
            continue
        stat = path.stat()
        state = connection.execute(
            "SELECT inode, offset FROM history_search_state WHERE session_id = ?", (session_id,)
        ).fetchone()
        offset = int(state[1]) if state else 0
        if state and (int(state[0]) != stat.st_ino or stat.st_size < offset):
            connection.execute("DELETE FROM history_search WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM history_search_state WHERE session_id = ?", (session_id,))
            connection.commit()
            offset = 0
        if stat.st_size <= offset:
            continue

        indexed_sessions += 1
        pending: list[tuple[str, str, str, int, str | None, str]] = []
        committed_offset = offset
        with path.open("rb") as source:
            source.seek(offset)
            while True:
                line_start = source.tell()
                raw_line = source.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    source.seek(line_start)
                    break
                committed_offset = source.tell()
                if not any(marker in raw_line for marker in HISTORY_SEARCH_LINE_MARKERS):
                    continue
                with suppress(Exception):
                    event = json.loads(raw_line.decode("utf-8", "replace"))
                    record = history_search_event_record(event)
                    if record:
                        role, text = record
                        pending.append((
                            text,
                            session_id,
                            str(event.get("id") or event.get("seq") or uuid.uuid4().hex),
                            int(event.get("seq") or 0),
                            event.get("ts"),
                            role,
                        ))
                if len(pending) >= 1000:
                    connection.executemany(insert, pending)
                    indexed_events += len(pending)
                    pending.clear()
                    connection.execute(update_state, (session_id, stat.st_ino, committed_offset, stat.st_mtime_ns))
                    connection.commit()
        if pending:
            connection.executemany(insert, pending)
            indexed_events += len(pending)
        connection.execute(update_state, (session_id, stat.st_ino, committed_offset, stat.st_mtime_ns))
        connection.commit()
    return indexed_sessions, indexed_events


def run_history_search_sync(
    target_session_ids: set[str],
    active_session_ids: set[str],
    *,
    prune: bool = False,
) -> tuple[int, int]:
    with HISTORY_SEARCH_LOCK:
        connection = history_search_connection()
        try:
            return sync_history_search_index(
                connection,
                target_session_ids,
                active_session_ids,
                prune=prune,
            )
        finally:
            connection.close()


async def history_search_index_loop() -> None:
    last_full_sync: float | None = None
    while True:
        active_session_ids = {
            session_id
            for session_id, session in STORE.sessions.items()
            if not bool(session.get("archived"))
        }
        now = time.monotonic()
        full_sync = last_full_sync is None or now - last_full_sync >= HISTORY_SEARCH_FULL_SYNC_INTERVAL_SECONDS
        if full_sync:
            target_session_ids = set(active_session_ids)
            HISTORY_SEARCH_DIRTY.difference_update(target_session_ids)
        else:
            target_session_ids = set(HISTORY_SEARCH_DIRTY)
            HISTORY_SEARCH_DIRTY.difference_update(target_session_ids)
        if target_session_ids or full_sync:
            started = time.monotonic()
            try:
                indexed_sessions, indexed_events = await asyncio.to_thread(
                    run_history_search_sync,
                    target_session_ids,
                    active_session_ids,
                    prune=full_sync,
                )
                if indexed_sessions or indexed_events:
                    logger.info(
                        "history search index synced sessions=%s events=%s elapsed=%.2fs",
                        indexed_sessions,
                        indexed_events,
                        time.monotonic() - started,
                    )
                if full_sync:
                    last_full_sync = now
            except asyncio.CancelledError:
                raise
            except Exception:
                HISTORY_SEARCH_DIRTY.update(target_session_ids)
                logger.exception("history search background sync failed")
        await asyncio.sleep(HISTORY_SEARCH_SYNC_INTERVAL_SECONDS)


def search_all_timelines(query: str, limit: int = 40) -> dict[str, Any]:
    tokens = timeline_search_tokens(query)
    if not tokens:
        return {"query": query, "results": []}
    fts_query = timeline_search_fts_query(query)
    connection = history_search_connection()
    try:
        rows = connection.execute("""
            WITH ranked AS (
                SELECT history_search.session_id, event_id, seq, ts, role, text,
                       ROW_NUMBER() OVER (
                           PARTITION BY history_search.session_id
                           ORDER BY COALESCE(ts, '') DESC, CAST(seq AS INTEGER) DESC
                       ) AS match_rank,
                       COUNT(*) OVER (PARTITION BY history_search.session_id) AS match_count
                FROM history_search
                JOIN history_search_sessions
                  ON history_search_sessions.session_id = history_search.session_id
                 AND history_search_sessions.active = 1
                WHERE history_search MATCH ?
            )
            SELECT session_id, event_id, seq, ts, role, text, match_count
            FROM ranked
            WHERE match_rank = 1
            ORDER BY COALESCE(ts, '') DESC, CAST(seq AS INTEGER) DESC
            LIMIT ?
        """, (fts_query, max(1, min(100, limit)))).fetchall()
    finally:
        connection.close()
    return {
        "query": query,
        "results": [{
            "session_id": row[0],
            "event_id": row[1],
            "seq": int(row[2] or 0),
            "ts": row[3],
            "role": row[4] or "system",
            "snippet": timeline_search_snippet(str(row[5] or ""), tokens),
            "match_count": int(row[6] or 1),
        } for row in rows],
    }


def compact_import_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text.strip())
    if len(text) <= MAX_IMPORTED_TEXT_CHARS:
        return text
    return text[:MAX_IMPORTED_TEXT_CHARS].rstrip() + "\n\n[import trimmed]"


def compact_memory_text(text: str, max_chars: int = MAX_FORK_MEMORY_ITEM_CHARS) -> str:
    text = re.sub(r"\n{3,}", "\n\n", str(text or "").strip())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n[trimmed]"


def byte_string(size: int | None) -> str:
    if size is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit = units[0]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            break
        value /= 1024
    if unit == "B":
        return f"{int(value)} {unit}"
    return f"{value:.1f} {unit}"


def digest_profile(detail: str) -> dict[str, int | bool]:
    normalized = str(detail or "normal").strip().lower()
    profiles: dict[str, dict[str, int | bool]] = {
        "short": {
            "events": 40,
            "message_chars": 650,
            "tool_chars": 120,
            "files": 8,
            "reasoning": False,
        },
        "normal": {
            "events": 180,
            "message_chars": 1600,
            "tool_chars": 420,
            "files": 24,
            "reasoning": True,
        },
        "deep": {
            "events": 420,
            "message_chars": 2600,
            "tool_chars": 850,
            "files": 48,
            "reasoning": True,
        },
    }
    return profiles.get(normalized, profiles["normal"])


def summarize_tool_input(tool_input: Any, max_chars: int) -> str:
    if tool_input is None:
        return ""
    if isinstance(tool_input, dict):
        for key in ("command", "cmd", "description", "query", "path"):
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return compact_memory_text(value, max_chars)
    if isinstance(tool_input, str):
        return compact_memory_text(tool_input, max_chars)
    return compact_memory_text(json.dumps(tool_input, separators=(",", ":"), ensure_ascii=False), max_chars)


def digest_file_line(file: dict[str, Any]) -> str:
    title = file.get("title") or file.get("filename") or file.get("id") or "file"
    bits = []
    if file.get("content_type"):
        bits.append(str(file["content_type"]))
    if file.get("size") is not None:
        bits.append(byte_string(int(file["size"])))
    if file.get("kind"):
        bits.append(str(file["kind"]))
    suffix = f" ({', '.join(bit for bit in bits if bit)})" if bits else ""
    line = f"- {title}{suffix}"
    path = file.get("source_path") or file.get("path")
    if path:
        line += f"\n  path: {path}"
    note = compact_memory_text(file.get("text") or "", 420)
    if note:
        line += f"\n  note: {note}"
    return line


def build_handoff_source_pack(session_id: str, detail: str = "normal", user_prompt: str | None = None) -> dict[str, Any]:
    source = STORE.sessions.get(session_id)
    if not source:
        raise HTTPException(status_code=404, detail="source session not found")

    profile = digest_profile(detail)
    events = read_events(session_id, limit=int(profile["events"]), tail=True)
    files = sorted(
        list_session_file_records(session_id),
        key=lambda rec: (str(rec.get("created_at") or ""), str(rec.get("filename") or "")),
        reverse=True,
    )
    provider_id = session_provider_id(source)
    prompt = compact_memory_text(user_prompt or "", 2200)

    lines = [
        "# AgentsDock Context Source Pack",
        "",
        "This is source material for an LLM-generated handoff digest.",
    ]
    if prompt:
        lines.extend(["", "## User Prompt For Target Agent", prompt])

    lines.extend([
        "",
        "## Source Chat",
        f"- Title: {source.get('title') or 'Untitled'}",
        f"- AgentsDock session: {session_id}",
        f"- Backend: {source.get('backend') or DEFAULT_BACKEND}",
        f"- Working directory: {source.get('cwd') or DEFAULT_CWD}",
    ])
    if provider_id:
        lines.append(f"- Provider session/thread: {provider_id}")
    if source.get("updated_at"):
        lines.append(f"- Updated: {source['updated_at']}")

    file_limit = int(profile["files"])
    if files:
        lines.extend(["", f"## Files And Videos ({min(len(files), file_limit)} of {len(files)}, newest first)"])
        for file in files[:file_limit]:
            lines.append(digest_file_line(file))

    lines.extend(["", f"## Recent Transcript ({len(events)} events, newest window)"])
    assistant_runs = {
        event.get("run_id")
        for event in events
        if event.get("type") == "assistant_text" and str(event.get("text") or "").strip()
    }
    message_chars = int(profile["message_chars"])
    tool_chars = int(profile["tool_chars"])
    include_reasoning = bool(profile["reasoning"])

    for event in events:
        event_type = event.get("type")
        if event_type == "turn_started":
            text = compact_memory_text(event.get("prompt") or "", message_chars)
            if text:
                lines.append(f"\nUser:\n{text}")
        elif event_type == "turn_queued":
            text = compact_memory_text(event.get("prompt") or "", min(message_chars, 900))
            if text:
                lines.append(f"\nQueued user turn:\n{text}")
        elif event_type == "assistant_text":
            text = compact_memory_text(clean_assistant_text(event.get("text") or ""), message_chars)
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "turn_finished" and event.get("run_id") not in assistant_runs:
            text = compact_memory_text(clean_assistant_text(event.get("result_text") or ""), message_chars)
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "reasoning_summary" and include_reasoning:
            text = compact_memory_text(event.get("text") or "", min(message_chars, 1200))
            if text:
                lines.append(f"\nReasoning summary:\n{text}")
        elif event_type == "tool_started":
            tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
            name = tool.get("name") or event.get("tool_id") or "tool"
            summary = summarize_tool_input(tool.get("input"), tool_chars)
            lines.append(f"\nTool started: {name}" + (f"\n{summary}" if summary else ""))
        elif event_type == "tool_finished" and str(detail or "normal").lower() == "deep":
            tool = event.get("tool") if isinstance(event.get("tool"), dict) else {}
            name = tool.get("name") or event.get("tool_id") or "tool"
            status = "error" if event.get("is_error") else "ok"
            output = compact_memory_text(event.get("output") or event.get("message") or "", tool_chars)
            lines.append(f"\nTool finished: {name} ({status})" + (f"\n{output}" if output else ""))
        elif event_type == "artifact_created":
            artifact = event.get("artifact") if isinstance(event.get("artifact"), dict) else {}
            if artifact:
                lines.append(f"\nArtifact:\n{digest_file_line(artifact)}")
        elif event_type in {"error", "turn_stopped"}:
            text = compact_memory_text(event.get("message") or event.get("error") or "", 1000)
            if text:
                lines.append(f"\n{event_type.replace('_', ' ').title()}:\n{text}")

    digest = "\n".join(lines).strip()
    if len(digest) > MAX_HANDOFF_DIGEST_CHARS:
        head, sep, tail = digest.partition("## Recent Transcript")
        tail_budget = max(4000, MAX_HANDOFF_DIGEST_CHARS - len(head) - len(sep) - 120)
        digest = f"{head}{sep}\n[Older digest content trimmed]\n{tail[-tail_budget:].lstrip()}"

    return {
        "source_pack": digest,
        "source_session": public_session(source),
        "event_count": len(events),
        "file_count": len(files),
        "detail": str(detail or "normal").strip().lower() or "normal",
    }


def handoff_target_context(target_session_id: str | None) -> str:
    if not target_session_id:
        return "- Target chat: not selected"
    target = STORE.sessions.get(target_session_id)
    if not target:
        return f"- Target chat: {target_session_id} (not found)"
    provider_id = session_provider_id(target)
    lines = [
        f"- Target title: {target.get('title') or 'Untitled'}",
        f"- Target AgentsDock session: {target_session_id}",
        f"- Target backend: {target.get('backend') or DEFAULT_BACKEND}",
        f"- Target working directory: {target.get('cwd') or DEFAULT_CWD}",
    ]
    if target.get("model"):
        lines.append(f"- Target model: {target['model']}")
    if target.get("effort"):
        lines.append(f"- Target effort: {target['effort']}")
    if provider_id:
        lines.append(f"- Target provider session/thread: {provider_id}")
    return "\n".join(lines)


def build_handoff_summary_prompt(
    source_pack: str,
    *,
    detail: str,
    user_prompt: str | None,
    target_session_id: str | None,
) -> str:
    prompt = compact_memory_text(user_prompt or "", 2200)
    target_context = handoff_target_context(target_session_id)
    detail_name = str(detail or "normal").strip().lower() or "normal"
    return f"""\
You are creating a concise, accurate LLM-summarized handoff digest for another coding/research agent.

This is NOT a chat response to the user. Do not solve the task. Do not ask questions.
Use the source packet below as evidence and produce a clean Markdown digest that the target agent can use as background context.

Required output:
- Start with "# AgentsDock Context Digest".
- Include "## User Prompt For Target Agent" only if the user supplied one.
- Include "## Executive Summary" with the current state in plain language.
- Include "## Important Decisions / Facts" for durable conclusions.
- Include "## Files, Videos, And Artifacts" with exact paths, filenames, IDs, and why they matter.
- Include "## Active / Pending Work" for running jobs, queued work, blockers, or next checks.
- Include "## Recommended Next Prompt" with a short instruction the target agent should follow.

Rules:
- Preserve exact commands, paths, session IDs, job names, URLs, and numeric results when they matter.
- Do not invent missing facts. If the source packet is unclear, say "Unknown".
- Prefer a useful summary over a transcript. Collapse repetitive traces aggressively.
- Keep the digest under {MAX_HANDOFF_DIGEST_CHARS} characters.
- No emoji or decorative prefixes.

Digest detail level requested: {detail_name}

Target agent context:
{target_context}

User prompt for target agent:
{prompt or "None"}

Source packet:
```markdown
{source_pack}
```
"""


def parse_claude_digest_output(stdout: str) -> str:
    text_parts: list[str] = []
    final_text = ""
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    text_parts.append(clean_assistant_text(block["text"]))
        elif event.get("type") == "result":
            error = claude_result_error(event)
            if error:
                raise RuntimeError(error)
            final_text = clean_assistant_text(event.get("result", "") or final_text)
    return clean_assistant_text(final_text or "\n\n".join(text_parts).strip())


async def run_claude_handoff_summarizer(prompt: str, *, model: str | None, effort: str | None) -> str:
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=existing_cwd(DEFAULT_CWD),
        env=runner_env(),
        limit=PROCESS_STREAM_LIMIT,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")),
            timeout=HANDOFF_DIGEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await terminate_process_tree(proc, grace=0.5)
        raise TimeoutError(f"handoff digest LLM timed out after {HANDOFF_DIGEST_TIMEOUT_SECONDS}s")
    decoded_stdout = stdout.decode("utf-8", "replace")
    decoded_stderr = stderr.decode("utf-8", "replace").strip()
    if proc.returncode not in (0, None):
        raise RuntimeError(decoded_stderr or f"Claude digest summarizer exited {proc.returncode}")
    digest = parse_claude_digest_output(decoded_stdout)
    if not digest:
        raise RuntimeError(decoded_stderr or "Claude digest summarizer returned empty output")
    return digest


def parse_codex_digest_output(stdout: str) -> str:
    text_parts: list[str] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") not in ("item.completed", "response.completed"):
            continue
        item = event.get("item", {}) or {}
        if item.get("type") == "agent_message":
            text = clean_assistant_text(item.get("text") or "")
            if text:
                text_parts.append(text)
    return clean_assistant_text("\n\n".join(text_parts).strip())


async def run_codex_handoff_summarizer(prompt: str, *, model: str | None, effort: str | None) -> str:
    cmd = [CODEX_BIN, "exec", "--json", "--skip-git-repo-check"]
    configured_model, _configured_effort, configured_service_tier = codex_user_config_defaults()
    effective_model = str(model or configured_model or CODEX_DEFAULT_MODEL).strip()
    effective_service_tier = configured_service_tier or codex_default_service_tier(effective_model)
    if model:
        cmd.extend(["--model", model])
    normalized_effort = normalize_runtime_effort(BACKEND_CODEX, effort)
    if normalized_effort:
        cmd.extend(["-c", f"model_reasoning_effort={normalized_effort}"])
    if effective_service_tier:
        cmd.extend(["-c", f"service_tier={effective_service_tier}"])
    cmd.extend(["-c", "model_reasoning_summary=none", prompt])
    env = runner_env()
    codex_dir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if codex_dir and codex_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = codex_dir + os.pathsep + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=existing_cwd(DEFAULT_CWD),
        env=env,
        limit=PROCESS_STREAM_LIMIT,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=HANDOFF_DIGEST_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        await terminate_process_tree(proc, grace=0.5)
        raise TimeoutError(f"handoff digest LLM timed out after {HANDOFF_DIGEST_TIMEOUT_SECONDS}s")
    decoded_stdout = stdout.decode("utf-8", "replace")
    decoded_stderr = stderr.decode("utf-8", "replace").strip()
    if proc.returncode not in (0, None):
        raise RuntimeError(decoded_stderr or f"Codex digest summarizer exited {proc.returncode}")
    digest = parse_codex_digest_output(decoded_stdout)
    if not digest:
        raise RuntimeError(decoded_stderr or "Codex digest summarizer returned empty output")
    return digest


async def build_handoff_digest(
    session_id: str,
    detail: str = "normal",
    user_prompt: str | None = None,
    target_session_id: str | None = None,
    summarizer_backend: str | None = None,
    summarizer_model: str | None = None,
    summarizer_effort: str | None = None,
) -> dict[str, Any]:
    source = build_handoff_source_pack(session_id, detail=detail, user_prompt=user_prompt)
    source_pack = str(source["source_pack"])
    backend = str(summarizer_backend or HANDOFF_DIGEST_BACKEND or BACKEND_CLAUDE).strip().lower()
    if backend not in VALID_BACKENDS:
        backend = HANDOFF_DIGEST_BACKEND
    model = (summarizer_model if summarizer_model is not None else HANDOFF_DIGEST_MODEL).strip() or None
    effort = (summarizer_effort if summarizer_effort is not None else HANDOFF_DIGEST_EFFORT).strip() or None
    prompt = build_handoff_summary_prompt(
        source_pack,
        detail=str(source["detail"]),
        user_prompt=user_prompt,
        target_session_id=target_session_id,
    )
    try:
        if backend == BACKEND_CODEX:
            digest = await run_codex_handoff_summarizer(prompt, model=model, effort=effort)
        else:
            digest = await run_claude_handoff_summarizer(prompt, model=model, effort=effort)
    except Exception as exc:
        logger.warning("handoff digest LLM failed session=%s backend=%s model=%s: %s", session_id, backend, model, exc)
        raise HTTPException(status_code=502, detail=f"LLM digest failed: {exc}") from exc
    if len(digest) > MAX_HANDOFF_DIGEST_CHARS:
        digest = digest[:MAX_HANDOFF_DIGEST_CHARS].rstrip() + "\n\n[Digest trimmed to server limit]"
    return {
        "digest": digest,
        "source_session": source["source_session"],
        "event_count": source["event_count"],
        "file_count": source["file_count"],
        "detail": source["detail"],
        "summarizer": {
            "backend": backend,
            "model": model or "",
            "effort": effort or "",
            "mode": "llm",
        },
    }


def build_source_chat_digest_turn_prompt(
    *,
    detail: str,
    user_prompt: str | None,
    target_session_id: str,
) -> str:
    prompt = compact_memory_text(user_prompt or "", 2200)
    target_context = handoff_target_context(target_session_id)
    detail_name = str(detail or "normal").strip().lower() or "normal"
    return f"""\
You are the source chat agent for an AgentsDock handoff.

Create a concise, accurate Markdown context digest for the target agent. Use your current source-chat context as the primary evidence. This is not a normal user task: do not continue the project work, do not ask questions, and do not include meta commentary about generating the digest.

Required output:
- Start with "# AgentsDock Context Digest".
- Include "## User Prompt For Target Agent" only if the user supplied one.
- Include "## Executive Summary" with the current state in plain language.
- Include "## Important Decisions / Facts" for durable conclusions.
- Include "## Files, Videos, And Artifacts" with exact paths, filenames, IDs, and why they matter.
- Include "## Active / Pending Work" for running jobs, queued work, blockers, or next checks.
- Include "## Recommended Next Prompt" with a short instruction the target agent should follow.

Rules:
- Preserve exact commands, paths, session IDs, job names, URLs, and numeric results when they matter.
- Do not invent missing facts. If the source context is unclear, say "Unknown".
- Prefer a useful summary over a transcript. Collapse repetitive traces aggressively.
- Keep the digest under {MAX_HANDOFF_DIGEST_CHARS} characters.
- No emoji or decorative prefixes.

Digest detail level requested: {detail_name}

Target agent context:
{target_context}

User prompt for target agent:
{prompt or "None"}
"""


def source_digest_display_prompt(target: dict[str, Any], user_prompt: str | None) -> str:
    target_title = str(target.get("title") or "target chat")
    prompt = compact_memory_text(user_prompt or "", 260)
    if prompt:
        return f"Generate a handoff digest for {target_title}.\n\nTarget prompt: {prompt}"
    return f"Generate a handoff digest for {target_title}."


def target_digest_display_prompt(source: dict[str, Any]) -> str:
    source_title = str(source.get("title") or "source chat")
    return f"Context digest from {source_title}."


def save_handoff_digest_jobs_unlocked() -> None:
    ensure_dirs()
    tmp = HANDOFF_DIGEST_JOBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"jobs": HANDOFF_DIGEST_JOBS}, indent=2), encoding="utf-8")
    tmp.replace(HANDOFF_DIGEST_JOBS_FILE)


def discover_recent_handoff_digest_jobs(max_sessions: int = 32) -> dict[str, dict[str, Any]]:
    """Migrate unfinished typed handoffs created before the durable job ledger existed."""
    sessions = sorted(
        STORE.sessions.values(),
        key=lambda session: str(session.get("latest_event_at") or session.get("updated_at") or ""),
        reverse=True,
    )[:max_sessions]
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for session in sessions:
        session_id = str(session.get("id") or "")
        if not session_id:
            continue
        for event in tail_jsonl_file(events_path(session_id), limit=200, max_bytes=2 * 1024 * 1024):
            digest_job_id = str(event.get("digest_job_id") or "")
            if digest_job_id:
                grouped.setdefault(digest_job_id, []).append((session_id, event))

    discovered: dict[str, dict[str, Any]] = {}
    for digest_job_id, records in grouped.items():
        records.sort(key=lambda record: int(record[1].get("seq") or 0))
        all_events = [event for _, event in records]
        started = next((event for event in all_events if event.get("type") == "handoff_digest_started"), {})
        source_finished = next((
            event for event in reversed(all_events)
            if event.get("type") == "turn_finished" and event.get("purpose") == "handoff_digest"
        ), None)
        delivery_finished = next((
            event for event in reversed(all_events)
            if event.get("type") == "turn_finished" and event.get("purpose") == "handoff_digest_delivery"
        ), None)
        source_queued = False
        source_prompt = ""
        for event in all_events:
            if event.get("purpose") != "handoff_digest":
                continue
            event_type = str(event.get("type") or "")
            if event_type == "turn_queued":
                source_queued = True
                source_prompt = str(event.get("request_prompt") or source_prompt)
            elif event_type in {"turn_started", "turn_unqueued", "turn_finished"}:
                source_queued = False

        source_session_id = str(
            started.get("source_session_id")
            or next((event.get("source_session_id") for event in all_events if event.get("source_session_id")), "")
        )
        target_session_id = str(
            started.get("target_session_id")
            or next((event.get("target_session_id") for event in all_events if event.get("target_session_id")), "")
        )
        if not source_session_id or not target_session_id:
            continue
        sent = any(event.get("type") == "handoff_digest_sent" for event in all_events)
        cancelled = any(
            event.get("type") == "handoff_digest_error" and event.get("cancelled") is True
            for event in all_events
        )
        digest = clean_assistant_text(
            (source_finished or {}).get("result_text")
            or next((event.get("digest") for event in reversed(all_events) if event.get("digest")), "")
        )
        if sent or delivery_finished:
            status = "sent"
        elif cancelled:
            status = "cancelled"
        elif source_finished and digest:
            status = "source_complete"
        elif source_queued:
            status = "source_queued"
        else:
            status = "created"
        discovered[digest_job_id] = {
            "id": digest_job_id,
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "detail": started.get("detail") or "normal",
            "user_prompt": started.get("user_prompt"),
            "source_prompt": source_prompt or None,
            "digest": digest or None,
            "status": status,
            "created_at": started.get("ts") or now_iso(),
            "updated_at": now_iso(),
            "recovered": True,
        }
    return discovered


async def load_handoff_digest_jobs() -> int:
    async with HANDOFF_DIGEST_JOBS_LOCK:
        HANDOFF_DIGEST_JOBS.clear()
        ledger_loaded = False
        if HANDOFF_DIGEST_JOBS_FILE.exists():
            try:
                payload = json.loads(HANDOFF_DIGEST_JOBS_FILE.read_text(encoding="utf-8"))
                jobs = payload.get("jobs") if isinstance(payload, dict) else None
                if isinstance(jobs, dict):
                    HANDOFF_DIGEST_JOBS.update({str(key): value for key, value in jobs.items() if isinstance(value, dict)})
                    ledger_loaded = True
            except Exception as exc:
                logger.warning("could not load handoff digest ledger %s: %s", HANDOFF_DIGEST_JOBS_FILE, exc)
        if not ledger_loaded:
            HANDOFF_DIGEST_JOBS.update(discover_recent_handoff_digest_jobs())
            if HANDOFF_DIGEST_JOBS:
                save_handoff_digest_jobs_unlocked()
        return len(HANDOFF_DIGEST_JOBS)


async def create_handoff_digest_job(
    digest_job_id: str,
    source_session_id: str,
    req: HandoffDigestSendRequest,
) -> dict[str, Any]:
    job = {
        "id": digest_job_id,
        "source_session_id": source_session_id,
        "target_session_id": req.target_session_id,
        "detail": req.detail,
        "user_prompt": req.user_prompt,
        "status": "created",
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    async with HANDOFF_DIGEST_JOBS_LOCK:
        HANDOFF_DIGEST_JOBS[digest_job_id] = job
        save_handoff_digest_jobs_unlocked()
    return dict(job)


async def update_handoff_digest_job(digest_job_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    async with HANDOFF_DIGEST_JOBS_LOCK:
        current = dict(HANDOFF_DIGEST_JOBS.get(digest_job_id) or {"id": digest_job_id, "created_at": now_iso()})
        current.update({key: value for key, value in patch.items() if value is not None})
        current["updated_at"] = now_iso()
        HANDOFF_DIGEST_JOBS[digest_job_id] = current
        save_handoff_digest_jobs_unlocked()
        return dict(current)


async def handoff_digest_jobs_snapshot() -> list[dict[str, Any]]:
    async with HANDOFF_DIGEST_JOBS_LOCK:
        return [dict(job) for job in HANDOFF_DIGEST_JOBS.values()]


def digest_job_events(session_id: str, digest_job_id: str) -> list[dict[str, Any]]:
    return [
        event
        for event in tail_jsonl_file(events_path(session_id), limit=200, max_bytes=8 * 1024 * 1024)
        if str(event.get("digest_job_id") or "") == digest_job_id
    ]


def digest_event_exists(session_id: str, digest_job_id: str, event_type: str) -> bool:
    return any(event.get("type") == event_type for event in digest_job_events(session_id, digest_job_id))


async def finish_handoff_digest_queue_item(
    session_id: str,
    item: dict[str, Any],
    message: str,
    *,
    cancelled: bool = False,
) -> None:
    digest_job_id = str(item.get("digest_job_id") or "")
    if not digest_job_id:
        return
    purpose = str(item.get("purpose") or "")
    source_session_id = str(item.get("source_session_id") or session_id)
    target_session_id = str(item.get("target_session_id") or "")
    status = "cancelled" if cancelled else "failed"
    await update_handoff_digest_job(digest_job_id, {"status": status, "error": message})
    event_session_id = source_session_id if source_session_id in STORE.sessions else session_id
    if not digest_event_exists(event_session_id, digest_job_id, "handoff_digest_error"):
        phase = "delivery" if purpose == "handoff_digest_delivery" else "generation"
        await append_event(event_session_id, "handoff_digest_error", {
            "digest_job_id": digest_job_id,
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "message": f"Context digest {phase} {'was canceled' if cancelled else 'failed'}: {message}",
            "detail": item.get("digest_detail") or "normal",
            "error": message,
            "cancelled": cancelled,
        })


async def digest_job_is_queued(session_id: str, digest_job_id: str, purpose: str) -> bool:
    async with QUEUE_LOCK:
        queued = list(QUEUED_TURNS.get(session_id) or [])
        run_now = RUN_NOW_TURNS.get(session_id)
    if run_now:
        queued.insert(0, run_now)
    return any(item.get("digest_job_id") == digest_job_id and item.get("purpose") == purpose for item in queued)


async def digest_job_is_active(session_id: str, digest_job_id: str, purpose: str) -> bool:
    async with ACTIVE_LOCK:
        active = ACTIVE.get(session_id)
        busy = session_id in BUSY_SESSIONS
    if not busy:
        return False
    run_id = str((active or {}).get("run_id") or "")
    metadata = RUN_METADATA.get(run_id) or {}
    return metadata.get("digest_job_id") == digest_job_id and metadata.get("purpose") == purpose


async def start_turn_durably(session_id: str, req: TurnRequest) -> dict[str, Any]:
    try:
        return await start_turn(session_id, req, queue_if_busy=True)
    except HTTPException as exc:
        if exc.status_code != 503:
            raise
        sess = STORE.sessions.get(session_id)
        if not sess:
            raise
        queued = await enqueue_turn(session_id, req, sess)
        schedule_next_queued_turn(session_id)
        return queued


async def submit_handoff_digest_source_turn(job: dict[str, Any], *, recovered: bool = False) -> dict[str, Any]:
    digest_job_id = str(job.get("id") or "")
    source_session_id = str(job.get("source_session_id") or "")
    target_session_id = str(job.get("target_session_id") or "")
    source = STORE.sessions.get(source_session_id)
    target = STORE.sessions.get(target_session_id)
    if not source:
        raise RuntimeError("source session not found")
    if not target:
        raise RuntimeError("target session not found")
    detail = str(job.get("detail") or "normal")
    user_prompt = str(job.get("user_prompt") or "").strip() or None
    source_prompt = str(job.get("source_prompt") or "").strip() or build_source_chat_digest_turn_prompt(
        detail=detail,
        user_prompt=user_prompt,
        target_session_id=target_session_id,
    )
    request = TurnRequest(
        prompt=source_prompt,
        display_prompt=source_digest_display_prompt(target, user_prompt),
        purpose="handoff_digest",
        digest_job_id=digest_job_id,
        digest_detail=detail,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
    )
    turn = await start_turn_durably(source_session_id, request)
    await update_handoff_digest_job(digest_job_id, {
        "status": "source_queued" if turn.get("queued") else "source_running",
        "source_queued_id": turn.get("queued_id"),
        "source_run_id": turn.get("run_id"),
        "source_prompt": source_prompt,
        "recovered": recovered or bool(job.get("recovered")),
    })
    return turn


async def run_handoff_digest_send(
    digest_job_id: str,
    source_session_id: str,
    req: HandoffDigestSendRequest,
) -> dict[str, Any]:
    job = HANDOFF_DIGEST_JOBS.get(digest_job_id)
    if not job:
        job = await create_handoff_digest_job(digest_job_id, source_session_id, req)
    try:
        return await submit_handoff_digest_source_turn(job)
    except Exception as exc:
        source_title = str((STORE.sessions.get(source_session_id) or {}).get("title") or source_session_id)
        logger.warning(
            "handoff digest submission failed job=%s source=%s target=%s: %s",
            digest_job_id,
            source_session_id,
            req.target_session_id,
            exc,
        )
        await append_event(source_session_id, "handoff_digest_error", {
            "digest_job_id": digest_job_id,
            "source_session_id": source_session_id,
            "target_session_id": req.target_session_id,
            "message": f"Context digest from {source_title} failed: {exc}",
            "detail": req.detail,
            "error": str(exc),
        })
        await update_handoff_digest_job(digest_job_id, {"status": "failed", "error": str(exc)})
        raise


def digest_delivery_event_state(target_session_id: str, digest_job_id: str) -> str | None:
    queued_ids: set[str] = set()
    state: str | None = None
    for event in digest_job_events(target_session_id, digest_job_id):
        event_type = str(event.get("type") or "")
        purpose = str(event.get("purpose") or "")
        queued_id = str(event.get("queued_id") or "")
        if purpose == "handoff_digest_delivery" and event_type == "turn_queued" and queued_id:
            queued_ids.add(queued_id)
            state = "queued"
        elif queued_id and event_type in {"turn_unqueued", "turn_started"}:
            queued_ids.discard(queued_id)
            if purpose == "handoff_digest_delivery" and event_type == "turn_started":
                state = "running"
        if purpose == "handoff_digest_delivery" and event_type == "turn_finished":
            state = "finished"
    if queued_ids:
        return "queued"
    return state


async def append_handoff_digest_sent_once(job: dict[str, Any], digest: str) -> None:
    digest_job_id = str(job.get("id") or "")
    source_session_id = str(job.get("source_session_id") or "")
    target_session_id = str(job.get("target_session_id") or "")
    if digest_event_exists(source_session_id, digest_job_id, "handoff_digest_sent"):
        return
    source = STORE.sessions.get(source_session_id) or {}
    target = STORE.sessions.get(target_session_id) or {}
    source_title = str(source.get("title") or source_session_id)
    target_title = str(target.get("title") or target_session_id)
    await append_event(source_session_id, "handoff_digest_sent", {
        "digest_job_id": digest_job_id,
        "source_session_id": source_session_id,
        "target_session_id": target_session_id,
        "message": f"Context digest from {source_title} was sent to {target_title}.",
        "detail": job.get("detail") or "normal",
        "digest_chars": len(digest),
    })


async def deliver_handoff_digest(job: dict[str, Any], digest: str, *, replay_interrupted: bool = False) -> None:
    digest_job_id = str(job.get("id") or "")
    source_session_id = str(job.get("source_session_id") or "")
    target_session_id = str(job.get("target_session_id") or "")
    source = STORE.sessions.get(source_session_id)
    target = STORE.sessions.get(target_session_id)
    if not source:
        raise RuntimeError("source session not found")
    if not target:
        raise RuntimeError("target session not found")
    source_title = str(source.get("title") or source_session_id)
    if not digest_event_exists(target_session_id, digest_job_id, "handoff_digest_received"):
        await append_event(target_session_id, "handoff_digest_received", {
            "digest_job_id": digest_job_id,
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "message": f"Context digest from {source_title} was delivered to this chat.",
            "digest": digest,
            "detail": job.get("detail") or "normal",
            "digest_chars": len(digest),
        })

    delivery_state = digest_delivery_event_state(target_session_id, digest_job_id)
    delivery_active = await digest_job_is_active(target_session_id, digest_job_id, "handoff_digest_delivery")
    delivery_queued = await digest_job_is_queued(target_session_id, digest_job_id, "handoff_digest_delivery")
    should_submit = delivery_state is None
    if replay_interrupted and delivery_state == "running" and not delivery_active:
        should_submit = True
    if delivery_queued or delivery_active or delivery_state == "finished":
        should_submit = False

    turn: dict[str, Any] = {}
    if should_submit:
        turn = await start_turn_durably(
            target_session_id,
            TurnRequest(
                prompt=digest,
                display_prompt=target_digest_display_prompt(source),
                purpose="handoff_digest_delivery",
                digest_job_id=digest_job_id,
                digest_detail=str(job.get("detail") or "normal"),
                source_session_id=source_session_id,
                target_session_id=target_session_id,
            ),
        )
        delivery_state = "queued" if turn.get("queued") else "running"

    await append_handoff_digest_sent_once(job, digest)
    await update_handoff_digest_job(digest_job_id, {
        "status": "sent" if delivery_state == "finished" else f"target_{delivery_state or 'submitted'}",
        "target_queued_id": turn.get("queued_id") or job.get("target_queued_id"),
        "target_run_id": turn.get("run_id") or job.get("target_run_id"),
        "digest": digest,
    })


async def finalize_handoff_digest_turn(
    source_session_id: str,
    event: dict[str, Any],
    *,
    replay_interrupted: bool = False,
) -> None:
    digest_job_id = str(event.get("digest_job_id") or "")
    if not digest_job_id:
        return
    async with HANDOFF_DIGEST_JOBS_LOCK:
        if digest_job_id in HANDOFF_DIGEST_FINALIZING:
            return
        HANDOFF_DIGEST_FINALIZING.add(digest_job_id)
        job = dict(HANDOFF_DIGEST_JOBS.get(digest_job_id) or {})
    try:
        if not job:
            job = await update_handoff_digest_job(digest_job_id, {
                "source_session_id": source_session_id,
                "target_session_id": event.get("target_session_id"),
                "detail": event.get("digest_detail") or "normal",
                "status": "source_complete",
            })
        if job.get("status") == "cancelled":
            return
        if event.get("stopped"):
            raise RuntimeError("digest generation was stopped")
        digest = clean_assistant_text(event.get("result_text") or job.get("digest") or "")
        if not digest:
            raise RuntimeError("digest generation finished without text")
        job = await update_handoff_digest_job(digest_job_id, {
            "status": "source_complete",
            "source_run_id": event.get("run_id") or job.get("source_run_id"),
            "target_session_id": event.get("target_session_id") or job.get("target_session_id"),
            "detail": event.get("digest_detail") or job.get("detail") or "normal",
            "digest": digest,
            "error": "",
        })
        if not digest_event_exists(source_session_id, digest_job_id, "handoff_digest_ready"):
            await append_event(source_session_id, "handoff_digest_ready", {
                "digest_job_id": digest_job_id,
                "source_session_id": source_session_id,
                "target_session_id": job.get("target_session_id"),
                "message": "Context digest is ready; submitting it to the target chat.",
                "detail": job.get("detail") or "normal",
                "digest_chars": len(digest),
            })
        await deliver_handoff_digest(job, digest, replay_interrupted=replay_interrupted)
    except Exception as exc:
        logger.warning("handoff digest finalization failed job=%s source=%s: %s", digest_job_id, source_session_id, exc)
        if not digest_event_exists(source_session_id, digest_job_id, "handoff_digest_error"):
            await append_event(source_session_id, "handoff_digest_error", {
                "digest_job_id": digest_job_id,
                "source_session_id": source_session_id,
                "target_session_id": event.get("target_session_id") or job.get("target_session_id"),
                "message": f"Context digest failed: {exc}",
                "detail": event.get("digest_detail") or job.get("detail") or "normal",
                "error": str(exc),
            })
        await update_handoff_digest_job(digest_job_id, {"status": "failed", "error": str(exc)})
    finally:
        async with HANDOFF_DIGEST_JOBS_LOCK:
            HANDOFF_DIGEST_FINALIZING.discard(digest_job_id)


async def finish_handoff_digest_delivery(event: dict[str, Any]) -> None:
    digest_job_id = str(event.get("digest_job_id") or "")
    if not digest_job_id:
        return
    status = "failed" if event.get("stopped") else "sent"
    await update_handoff_digest_job(digest_job_id, {
        "status": status,
        "target_run_id": event.get("run_id"),
        "delivery_finished_at": event.get("ts") or now_iso(),
    })


async def append_turn_finished_event(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    event = await append_event(session_id, "turn_finished", payload)
    purpose = str(event.get("purpose") or "")
    if purpose == "handoff_digest":
        await finalize_handoff_digest_turn(session_id, event)
    elif purpose == "handoff_digest_delivery":
        await finish_handoff_digest_delivery(event)
    return event


async def reconcile_handoff_digest_jobs() -> int:
    recovered = 0
    for job in await handoff_digest_jobs_snapshot():
        status = str(job.get("status") or "created")
        if status in {"sent", "failed", "cancelled"}:
            continue
        digest_job_id = str(job.get("id") or "")
        source_session_id = str(job.get("source_session_id") or "")
        target_session_id = str(job.get("target_session_id") or "")
        if not digest_job_id or source_session_id not in STORE.sessions or target_session_id not in STORE.sessions:
            continue
        source_events = digest_job_events(source_session_id, digest_job_id)
        source_finished = next((
            event for event in reversed(source_events)
            if event.get("type") == "turn_finished" and event.get("purpose") == "handoff_digest"
        ), None)
        if source_finished:
            await finalize_handoff_digest_turn(source_session_id, source_finished, replay_interrupted=True)
            recovered += 1
            continue
        if status.startswith("target_") and job.get("digest"):
            await deliver_handoff_digest(job, str(job.get("digest") or ""), replay_interrupted=True)
            recovered += 1
            continue
        if await digest_job_is_queued(source_session_id, digest_job_id, "handoff_digest"):
            continue
        if await digest_job_is_active(source_session_id, digest_job_id, "handoff_digest"):
            continue
        try:
            await submit_handoff_digest_source_turn(job, recovered=True)
            recovered += 1
        except Exception as exc:
            logger.warning("could not recover handoff digest job=%s: %s", digest_job_id, exc)
    return recovered


LEADING_DECORATION_RE = re.compile(
    r"(?m)^[ \t]*(?:(?::[A-Za-z0-9_+\-]+:|[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?)[ \t]*)+"
)


def clean_assistant_text(text: str) -> str:
    return LEADING_DECORATION_RE.sub("", str(text or "")).strip()


def is_import_boilerplate(text: str) -> bool:
    stripped = text.strip()
    boilerplate_prefixes = (
        "<environment_context>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "# AGENTS.md instructions",
        "# Context from my IDE setup:",
    )
    return any(stripped.startswith(prefix) for prefix in boilerplate_prefixes)


def text_from_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return compact_import_text(content)
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type")
                if block_type in {"text", "input_text", "output_text"} and block.get("text"):
                    parts.append(str(block["text"]))
        return compact_import_text("\n".join(p for p in parts if p.strip()))
    if isinstance(content, dict):
        if content.get("text"):
            return compact_import_text(str(content["text"]))
        if content.get("message"):
            return compact_import_text(str(content["message"]))
    return ""


def message_text(message: Any) -> str:
    if isinstance(message, dict):
        return text_from_content(message.get("content"))
    return text_from_content(message)


def add_history_item(items: list[dict[str, str]], kind: str, text: str) -> None:
    text = compact_import_text(text)
    if not text or (kind == "user" and is_import_boilerplate(text)):
        return
    if items and items[-1]["kind"] == kind and items[-1]["text"].strip() == text.strip():
        return
    items.append({"kind": kind, "text": text})


def tail_limit_items(items: list[dict[str, str]], limit: int | None) -> list[dict[str, str]]:
    effective_limit = limit or MAX_IMPORT_MESSAGES
    if effective_limit > 0 and len(items) > effective_limit:
        return items[-effective_limit:]
    return items


def path_if_jsonl(value: str) -> Path | None:
    raw = value.strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_file() and candidate.suffix == ".jsonl":
        return candidate
    return None


def find_claude_history(provider_id: str) -> Path | None:
    direct = path_if_jsonl(provider_id)
    if direct:
        return direct
    if not CLAUDE_PROJECTS_ROOT.exists():
        return None
    matches = [p for p in CLAUDE_PROJECTS_ROOT.rglob("*.jsonl") if p.stem == provider_id]
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def claude_project_dir_for_cwd(cwd: str) -> Path:
    # Claude Code stores transcript JSONL files under a cwd-derived project name.
    project_name = str(Path(cwd).expanduser()).replace("/", "-")
    return CLAUDE_PROJECTS_ROOT / project_name


def claude_resume_file_for_cwd(provider_id: str, cwd: str) -> Path:
    return claude_project_dir_for_cwd(cwd) / f"{provider_id}.jsonl"


def claude_provider_id_for_session(sess: dict[str, Any]) -> str | None:
    provider_id = sess.get("claude_session_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CLAUDE else None
    )
    if sess.get("fork_from"):
        provider_id = sess["fork_from"]
    provider_id = str(provider_id or "").strip()
    return provider_id or None


def resolve_claude_resume_provider(sess: dict[str, Any], cwd: str) -> tuple[str | None, str | None]:
    provider_id = claude_provider_id_for_session(sess)
    if not provider_id:
        return None, None

    saved_cwd = str(sess.get("claude_session_cwd") or "").strip()
    if saved_cwd and str(Path(saved_cwd).expanduser()) != str(Path(cwd).expanduser()):
        return None, f"Claude resume skipped: provider session {provider_id} was created in {saved_cwd}, not {cwd}."

    expected = claude_resume_file_for_cwd(provider_id, cwd)
    if expected.exists():
        return provider_id, None

    found_elsewhere = find_claude_history(provider_id)
    if found_elsewhere:
        return None, f"Claude resume skipped: provider session {provider_id} is not available for cwd {cwd}."

    return None, f"Claude resume skipped: provider session {provider_id} has no local transcript for cwd {cwd}."


def find_codex_history(provider_id: str) -> Path | None:
    direct = path_if_jsonl(provider_id)
    if direct:
        return direct
    if not CODEX_SESSIONS_ROOT.exists():
        return None
    name_matches = [p for p in CODEX_SESSIONS_ROOT.rglob("*.jsonl") if provider_id in p.name]
    if name_matches:
        return max(name_matches, key=lambda p: p.stat().st_mtime)
    for path in sorted(CODEX_SESSIONS_ROOT.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        with suppress(Exception):
            first = path.open("r", encoding="utf-8", errors="ignore").readline()
            meta = json.loads(first)
            if meta.get("type") == "session_meta" and meta.get("payload", {}).get("id") == provider_id:
                return path
    return None


def parse_claude_history(path: Path, limit: int | None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in path.open("r", encoding="utf-8", errors="ignore"):
        if not line.strip():
            continue
        with suppress(Exception):
            event = json.loads(line)
            event_type = event.get("type")
            if event_type == "user":
                add_history_item(items, "user", message_text(event.get("message")))
            elif event_type == "assistant":
                add_history_item(items, "assistant", message_text(event.get("message")))
    return tail_limit_items(items, limit)


def parse_codex_history(path: Path, limit: int | None) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for line in path.open("r", encoding="utf-8", errors="ignore"):
        if not line.strip():
            continue
        with suppress(Exception):
            event = json.loads(line)
            event_type = event.get("type")
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event_type == "event_msg":
                payload_type = payload.get("type")
                if payload_type == "user_message":
                    add_history_item(items, "user", str(payload.get("message") or ""))
                elif payload_type == "agent_message":
                    add_history_item(items, "assistant", str(payload.get("message") or ""))
            elif event_type == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                if role == "user":
                    add_history_item(items, "user", text_from_content(payload.get("content")))
                elif role == "assistant":
                    add_history_item(items, "assistant", text_from_content(payload.get("content")))
    return tail_limit_items(items, limit)


def session_provider_id(sess: dict[str, Any]) -> str | None:
    backend = (sess.get("backend") or DEFAULT_BACKEND).lower()
    if backend == BACKEND_CLAUDE:
        return sess.get("claude_session_id") or sess.get("session_id")
    if backend == BACKEND_CODEX:
        return sess.get("codex_thread_id") or sess.get("session_id")
    return sess.get("session_id")


def provider_history(sess: dict[str, Any], limit: int | None) -> tuple[Path | None, list[dict[str, str]]]:
    backend = (sess.get("backend") or DEFAULT_BACKEND).lower()
    provider_id = session_provider_id(sess)
    if not provider_id:
        return None, []
    if backend == BACKEND_CLAUDE:
        path = find_claude_history(provider_id)
        return path, parse_claude_history(path, limit) if path else []
    if backend == BACKEND_CODEX:
        path = find_codex_history(provider_id)
        return path, parse_codex_history(path, limit) if path else []
    return None, []


async def import_session_history(sess: dict[str, Any], *, force: bool = False, limit: int | None = None) -> dict[str, Any]:
    session_id = sess["id"]
    provider_id = session_provider_id(sess)
    backend = (sess.get("backend") or DEFAULT_BACKEND).lower()
    if not provider_id:
        return {"imported": 0, "source_path": None, "message": "No provider session ID set."}
    if not force and any(event.get("type") == "history_imported" for event in read_events(session_id, limit=10000)):
        return {"imported": 0, "source_path": None, "message": "History already imported."}

    try:
        source_path, items = provider_history(sess, limit)
    except Exception as e:
        logger.warning("history import failed session=%s provider=%s: %s", session_id, provider_id, e)
        message = f"History import failed: {e}"
        await append_event(session_id, "history_imported", {
            "backend": backend,
            "provider_session_id": provider_id,
            "message": message,
        })
        return {"imported": 0, "source_path": None, "message": message}

    if not source_path:
        message = f"No local {backend} transcript found for {provider_id}."
        await append_event(session_id, "history_imported", {
            "backend": backend,
            "provider_session_id": provider_id,
            "message": message,
        })
        return {"imported": 0, "source_path": None, "message": message}

    if not items:
        message = f"Found {backend} transcript, but no chat messages to import."
        await append_event(session_id, "history_imported", {
            "backend": backend,
            "provider_session_id": provider_id,
            "source_path": str(source_path),
            "message": message,
        })
        return {"imported": 0, "source_path": str(source_path), "message": message}

    run_id = f"import_{uuid.uuid4().hex[:12]}"
    message = f"Imported {len(items)} rough messages from {backend} history."
    await append_event(session_id, "history_imported", {
        "run_id": run_id,
        "backend": backend,
        "provider_session_id": provider_id,
        "source_path": str(source_path),
        "message": message,
    })
    for item in items:
        if item["kind"] == "user":
            await append_event(session_id, "turn_started", {
                "run_id": run_id,
                "backend": backend,
                "prompt": item["text"],
                "imported": True,
            })
        elif item["kind"] == "assistant":
            await append_event(session_id, "assistant_text", {
                "run_id": run_id,
                "backend": backend,
                "text": item["text"],
                "imported": True,
            })
    return {"imported": len(items), "source_path": str(source_path), "message": message}


async def copy_fork_history(parent_id: str, child_id: str) -> int:
    # Fork history copy is an internal clone operation, not an API page. Do not
    # route it through read_events(), which clamps responses for UI pagination.
    parent_events = list(iter_session_events(parent_id))
    assistant_runs = {
        event.get("run_id")
        for event in parent_events
        if event.get("type") == "assistant_text" and str(event.get("text") or "").strip()
    }
    copied = 0
    for event in parent_events:
        event_type = event.get("type")
        if event_type not in {"turn_started", "assistant_text", "reasoning_summary", "tool_started", "tool_finished", "artifact_created", "turn_finished"}:
            continue
        if event_type == "turn_finished":
            if not str(event.get("result_text") or "").strip() or event.get("run_id") in assistant_runs:
                continue
        payload = {
            key: value
            for key, value in event.items()
            if key not in {"seq", "id", "session_id", "ts"}
        }
        payload["forked"] = True
        payload["original_session_id"] = parent_id
        payload["original_seq"] = event.get("seq")
        await append_event(child_id, event_type, payload)
        copied += 1
    await append_event(parent_id, "session_forked", {"child_id": child_id})
    return copied


def build_fork_memory(
    parent: dict[str, Any],
    parent_id: str,
    *,
    reason: str | None = None,
    exclude_run_id: str | None = None,
) -> str:
    provider_id = session_provider_id(parent)
    header = [
        "[AgentsDock memory fork]",
        "This is a fresh provider thread seeded from a compact memory dump because the original provider-level fork was unavailable.",
        "Use this memory as background context. Do not treat it as a new user request.",
        "",
        f"Parent AgentsDock session: {parent_id}",
        f"Parent title: {parent.get('title') or 'Untitled'}",
        f"Backend: {parent.get('backend') or DEFAULT_BACKEND}",
        f"Working directory: {parent.get('cwd') or DEFAULT_CWD}",
    ]
    if provider_id:
        header.append(f"Original provider session/thread: {provider_id}")
    if reason:
        header.append(f"Fork fallback reason: {compact_memory_text(reason, 800)}")

    lines: list[str] = header + ["", "Recent rough conversation:"]
    events = [
        event
        for event in read_events(parent_id, limit=160, tail=True)
        if not exclude_run_id or event.get("run_id") != exclude_run_id
    ]
    assistant_runs = {
        event.get("run_id")
        for event in events
        if event.get("type") == "assistant_text" and str(event.get("text") or "").strip()
    }

    for event in events:
        event_type = event.get("type")
        if event_type == "turn_started":
            text = compact_memory_text(event.get("prompt") or "")
            if text:
                lines.append(f"\nUser:\n{text}")
        elif event_type == "assistant_text":
            text = compact_memory_text(event.get("text") or "")
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "turn_finished" and event.get("run_id") not in assistant_runs:
            text = compact_memory_text(event.get("result_text") or "")
            if text:
                lines.append(f"\nAssistant:\n{text}")
        elif event_type == "reasoning_summary":
            text = compact_memory_text(event.get("text") or "", 900)
            if text:
                lines.append(f"\nReasoning summary:\n{text}")
        elif event_type == "artifact_created":
            artifact = event.get("artifact") if isinstance(event.get("artifact"), dict) else {}
            title = artifact.get("title") or artifact.get("filename") or artifact.get("id") or "artifact"
            note = compact_memory_text(artifact.get("text") or "", 600)
            artifact_line = f"\nArtifact: {title}"
            if artifact.get("content_type"):
                artifact_line += f" ({artifact.get('content_type')})"
            if note:
                artifact_line += f"\n{note}"
            lines.append(artifact_line)

    memory = "\n".join(lines).strip()
    if len(memory) > MAX_FORK_MEMORY_CHARS:
        memory = memory[-MAX_FORK_MEMORY_CHARS:].lstrip()
        memory = "[AgentsDock memory fork]\n[Older memory trimmed]\n" + memory
    return memory


def is_codex_compaction_failure(message: str) -> bool:
    lowered = str(message or "").lower()
    return "remote compact task" in lowered and "stream disconnected" in lowered


def is_codex_resume_failure(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(
        marker in lowered
        for marker in (
            "no conversation found with session id",
            "conversation not found",
            "thread not found",
            "session not found",
            "failed to resume",
            "could not resume",
            "unable to resume",
        )
    )


def is_silent_codex_completion(
    *,
    stopped: bool,
    stream_error: str | None,
    idle_killed: bool,
    returncode: int | None,
    produced_activity: bool,
) -> bool:
    return (
        not stopped
        and not stream_error
        and not idle_killed
        and returncode in (0, None)
        and not produced_activity
    )


def should_recover_codex_resume(
    *,
    allow_rollover: bool,
    resumed_provider_id: str | None,
    stopped: bool,
    stream_error: str | None,
    idle_killed: bool,
    resume_stalled: bool,
    returncode: int | None,
    produced_activity: bool,
    terminal_error: str,
) -> bool:
    if (
        not allow_rollover
        or not resumed_provider_id
        or stopped
        or stream_error
        or idle_killed
        or produced_activity
    ):
        return False
    if resume_stalled or returncode in (0, None):
        return True
    return is_codex_resume_failure(terminal_error) or is_codex_compaction_failure(terminal_error)


async def rollover_codex_provider_session(
    session_id: str,
    run_id: str,
    provider_id: str,
    reason: str,
    *,
    message: str,
) -> tuple[dict[str, Any], str] | None:
    current = STORE.sessions.get(session_id)
    if not current:
        return None
    snapshot = dict(current)
    memory = build_fork_memory(snapshot, session_id, reason=reason, exclude_run_id=run_id)
    async with STORE._lock:
        current = STORE.sessions.get(session_id)
        if not current or session_provider_id(current) != provider_id:
            return None
        current["session_id"] = None
        current["codex_thread_id"] = None
        current["memory_seed"] = memory
        current["memory_seed_used"] = True
        current["memory_forked"] = True
        current["memory_fork_reason"] = compact_memory_text(reason, 2000)
        current["updated_at"] = now_iso()
        await STORE.save()
        fresh_session = dict(current)
    await append_event(session_id, "provider_rollover", {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "old_provider_session_id": provider_id,
        "message": message,
        "reason": compact_memory_text(reason, 1200),
    })
    return fresh_session, memory


def public_session(sess: dict[str, Any]) -> dict[str, Any]:
    return {
        k: sess.get(k)
        for k in (
            "id", "title", "folder", "cwd", "backend", "model", "effort", "system_prompt",
            "session_id", "claude_session_id", "codex_thread_id",
            "parent_id", "fork_from", "memory_forked", "memory_seed_used",
            "pinned", "pinned_at", "archived", "archived_at", "sort_order", "created_at", "updated_at",
            "latest_event_seq", "latest_event_at", "latest_event_type",
            "latest_agent_event_seq", "latest_agent_event_at", "latest_agent_event_type",
            "last_read_agent_event_seq", "last_read_agent_event_at", "manual_unread",
        )
    }


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in job.items() if not str(k).startswith("_")}
    if out.get("next_run_at"):
        out["next_run_at_iso"] = datetime.fromtimestamp(float(out["next_run_at"]), tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return out


def host_pressure_snapshot() -> dict[str, Any]:
    load_1m: float | None = None
    load_per_cpu: float | None = None
    cpu_count = os.cpu_count() or 1
    with suppress(Exception):
        load_1m = float(os.getloadavg()[0])
        load_per_cpu = load_1m / max(cpu_count, 1)

    available_mem_mb: int | None = None
    meminfo = Path("/proc/meminfo")
    if meminfo.exists():
        with suppress(Exception):
            for line in meminfo.read_text(errors="replace").splitlines():
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        available_mem_mb = int(int(parts[1]) / 1024)
                    break

    return {
        "load_1m": load_1m,
        "load_per_cpu": load_per_cpu,
        "cpu_count": cpu_count,
        "available_mem_mb": available_mem_mb,
        "job_min_available_mem_mb": JOB_MIN_AVAILABLE_MEM_MB,
        "job_max_active_runs": JOB_MAX_ACTIVE_RUNS,
        "start_min_available_mem_mb": MIN_START_AVAILABLE_MEM_MB,
        "start_max_active_runs": MAX_ACTIVE_AGENT_RUNS,
    }


async def scheduled_job_blocker(session_id: str) -> str | None:
    async with ACTIVE_LOCK:
        if session_id in BUSY_SESSIONS:
            return "chat already has a running turn"
        active_count = len(BUSY_SESSIONS)

    if JOB_MAX_ACTIVE_RUNS > 0 and active_count >= JOB_MAX_ACTIVE_RUNS:
        return f"{active_count} active agent run(s)"

    pressure = host_pressure_snapshot()
    available_mem_mb = pressure.get("available_mem_mb")
    if (
        isinstance(available_mem_mb, int)
        and JOB_MIN_AVAILABLE_MEM_MB > 0
        and available_mem_mb < JOB_MIN_AVAILABLE_MEM_MB
    ):
        return f"low available memory ({available_mem_mb} MB)"

    return None


async def turn_start_blocker(*, ignore_session_id: str | None = None) -> str | None:
    async with ACTIVE_LOCK:
        active_count = len(BUSY_SESSIONS - ({ignore_session_id} if ignore_session_id else set()))

    if MAX_ACTIVE_AGENT_RUNS > 0 and active_count >= MAX_ACTIVE_AGENT_RUNS:
        return f"server already has {active_count} active agent run(s)"

    pressure = host_pressure_snapshot()
    available_mem_mb = pressure.get("available_mem_mb")
    if (
        isinstance(available_mem_mb, int)
        and MIN_START_AVAILABLE_MEM_MB > 0
        and available_mem_mb < MIN_START_AVAILABLE_MEM_MB
    ):
        return f"low available memory ({available_mem_mb} MB)"

    return None


def runner_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDECODE")}
    home = env.get("HOME", str(Path.home()))
    extra = [
        f"{home}/.local/bin",
        f"{home}/.npm-global/bin",
        f"{home}/.cargo/bin",
        f"{home}/.bun/bin",
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    nvm_root = Path(home) / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        extra.extend(str(path / "bin") for path in sorted(nvm_root.glob("*"), reverse=True))
    env["PATH"] = ":".join(extra + [env.get("PATH", "/usr/bin:/bin")])
    return env


def agent_runner_env(session_id: str) -> dict[str, str]:
    env = runner_env()
    env["AGENTSDOCK_CHAT_ID"] = session_id
    env["AGENTSDOCK_TMUX_SESSION"] = terminal_session_name(session_id)
    return env


def runtime_option(value: str, label: str | None = None, **extra: Any) -> dict[str, Any]:
    clean = str(value or "").strip()
    return {"value": clean, "label": str(label or clean or "Server default").strip(), **extra}


def server_default_runtime_option(label: str | None = None) -> dict[str, Any]:
    return runtime_option("", label or "Server default")


def title_model_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "Server default"
    known = {
        "fable": "Fable",
        "claude-fable-5": "Fable 5",
        "opus[1m]": "Opus 1M",
        "claude-opus-4-8": "Opus 4.8",
        "claude-opus-4-8[1m]": "Opus 4.8 1M",
    }
    if clean.lower() in known:
        return known[clean.lower()]
    special = {
        "gpt": "GPT",
        "codex": "Codex",
        "claude": "Claude",
    }
    return " ".join(special.get(part.lower(), part.upper() if part.lower().startswith("gpt") else part.capitalize()) for part in re.split(r"[-_\s]+", clean) if part)


def title_effort_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "Server default"
    if clean.lower() == "xhigh":
        return "XHigh"
    return clean.capitalize()


def unique_runtime_options(options: list[dict[str, Any]], default_label: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for option in [server_default_runtime_option(default_label), *options]:
        value = str(option.get("value") or "").strip()
        if value in seen:
            continue
        seen.add(value)
        extra = {key: val for key, val in option.items() if key not in {"value", "label"}}
        out.append(runtime_option(value, option.get("label") or None, **extra))
    return out


def run_catalog_command(cmd: list[str]) -> str:
    result = subprocess.run(
        cmd,
        cwd=DEFAULT_CWD if Path(DEFAULT_CWD).exists() else str(Path.home()),
        env=runner_env(),
        text=True,
        capture_output=True,
        timeout=RUNTIME_CATALOG_TIMEOUT_SECONDS,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        raise RuntimeError(f"{cmd[0]} exited {result.returncode}: {stderr}")
    return result.stdout


def claude_supports_effort(effort: str) -> bool:
    clean = str(effort or "").strip().lower()
    if not clean:
        return False
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--effort", clean, "--version"],
            cwd=DEFAULT_CWD if Path(DEFAULT_CWD).exists() else str(Path.home()),
            env=runner_env(),
            text=True,
            capture_output=True,
            timeout=RUNTIME_CATALOG_TIMEOUT_SECONDS,
            check=False,
        )
    except Exception as exc:
        logger.debug("claude effort probe failed effort=%s: %s", clean, exc)
        return False
    output = f"{result.stdout}\n{result.stderr}".lower()
    return result.returncode == 0 and "unknown --effort value" not in output


def runtime_executable(backend: str) -> str:
    return CLAUDE_BIN if backend == BACKEND_CLAUDE else CODEX_BIN


def runtime_display_name(backend: str) -> str:
    return "Claude Code" if backend == BACKEND_CLAUDE else "Codex"


def runtime_action(backend: str, status: str) -> str | None:
    executable = runtime_executable(backend)
    if status == "missing":
        return f"Install {runtime_display_name(backend)} for the server user, make `{executable}` available on PATH, then restart the agent server."
    if status == "unauthenticated":
        command = "claude auth login" if backend == BACKEND_CLAUDE else "codex login"
        return f"Run `{command}` as the server user, then refresh runtime status."
    if status == "error":
        command = "claude auth status" if backend == BACKEND_CLAUDE else "codex login status"
        return f"Run `{executable} --version` and `{command}` as the server user, then refresh runtime status."
    return None


def runtime_diagnostic_payload(
    backend: str,
    status: str,
    *,
    installed: bool | None,
    authenticated: bool | None,
    version: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    display = runtime_display_name(backend)
    default_messages = {
        "ready": f"{display} is installed and authenticated.",
        "missing": f"{display} is not installed or is not available to the agent server.",
        "unauthenticated": f"{display} is installed but the server user is not authenticated.",
        "error": f"{display} is installed but its runtime check failed.",
        "unknown": f"{display} has not been checked yet.",
    }
    return {
        "backend": backend,
        "status": status,
        "available": status == "ready",
        "installed": installed,
        "authenticated": authenticated,
        "version": version,
        "message": message or default_messages.get(status, default_messages["unknown"]),
        "action": runtime_action(backend, status),
        "checked_at": now_iso(),
        "checked_at_epoch": time.time(),
        "last_error": None,
        "last_error_at": None,
    }


def safe_runtime_version(output: str) -> str | None:
    first_line = next((line.strip() for line in str(output or "").splitlines() if line.strip()), "")
    if not first_line:
        return None
    clean = re.sub(r"[^A-Za-z0-9._+()\- /]", "", first_line).strip()
    return clean[:120] or None


def runtime_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=DEFAULT_CWD if Path(DEFAULT_CWD).exists() else str(Path.home()),
        env=runner_env(),
        text=True,
        capture_output=True,
        timeout=RUNTIME_CATALOG_TIMEOUT_SECONDS,
        check=False,
    )


def auth_failure_text(value: str) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in (
        "authorizationrequired", "authentication required", "not authenticated",
        "not logged in", "login required", "unauthorized", "invalid api key",
        "missing api key", "please log in", "please login",
    ))


def probe_runtime(backend: str) -> dict[str, Any]:
    configured = runtime_executable(backend)
    resolved = shutil.which(configured, path=runner_env().get("PATH"))
    if not resolved:
        return runtime_diagnostic_payload(backend, "missing", installed=False, authenticated=False)

    try:
        version_result = runtime_command([resolved, "--version"])
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s version probe failed: %s", backend, type(exc).__name__)
        return runtime_diagnostic_payload(backend, "error", installed=True, authenticated=None)
    version = safe_runtime_version(version_result.stdout or version_result.stderr)
    if version_result.returncode != 0:
        return runtime_diagnostic_payload(backend, "error", installed=True, authenticated=None, version=version)

    auth_cmd = [resolved, "auth", "status", "--json"] if backend == BACKEND_CLAUDE else [resolved, "login", "status"]
    try:
        auth_result = runtime_command(auth_cmd)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("%s auth probe failed: %s", backend, type(exc).__name__)
        return runtime_diagnostic_payload(backend, "error", installed=True, authenticated=None, version=version)

    combined = f"{auth_result.stdout}\n{auth_result.stderr}"
    if backend == BACKEND_CLAUDE and auth_result.stdout.strip():
        try:
            auth_payload = json.loads(auth_result.stdout)
        except (TypeError, ValueError):
            auth_payload = None
        if isinstance(auth_payload, dict) and auth_payload.get("loggedIn") is False:
            return runtime_diagnostic_payload(backend, "unauthenticated", installed=True, authenticated=False, version=version)
        if isinstance(auth_payload, dict) and auth_payload.get("loggedIn") is True:
            return runtime_diagnostic_payload(backend, "ready", installed=True, authenticated=True, version=version)
    if auth_result.returncode == 0:
        return runtime_diagnostic_payload(backend, "ready", installed=True, authenticated=True, version=version)
    if auth_failure_text(combined):
        return runtime_diagnostic_payload(backend, "unauthenticated", installed=True, authenticated=False, version=version)
    return runtime_diagnostic_payload(backend, "error", installed=True, authenticated=None, version=version)


def store_runtime_diagnostic(diagnostic: dict[str, Any], *, preserve_last_error: bool = True) -> dict[str, Any]:
    backend = str(diagnostic.get("backend") or "")
    with RUNTIME_DIAGNOSTICS_LOCK:
        previous = RUNTIME_DIAGNOSTICS.get(backend) or {}
        current = dict(diagnostic)
        if preserve_last_error and previous.get("last_error"):
            current["last_error"] = previous.get("last_error")
            current["last_error_at"] = previous.get("last_error_at")
        RUNTIME_DIAGNOSTICS[backend] = current
        return dict(current)


def runtime_diagnostic(backend: str, *, force: bool = False) -> dict[str, Any]:
    with RUNTIME_DIAGNOSTICS_LOCK:
        cached = dict(RUNTIME_DIAGNOSTICS.get(backend) or {})
    checked_at = cached.get("checked_at_epoch")
    if not force and isinstance(checked_at, (int, float)) and time.time() - checked_at < RUNTIME_DIAGNOSTIC_TTL_SECONDS:
        return cached
    return store_runtime_diagnostic(probe_runtime(backend))


def refresh_runtime_diagnostics(*, force: bool = False) -> dict[str, dict[str, Any]]:
    return {backend: runtime_diagnostic(backend, force=force) for backend in sorted(VALID_BACKENDS)}


def public_runtime_diagnostic(diagnostic: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in diagnostic.items() if key != "checked_at_epoch"}


def runtime_diagnostics_snapshot() -> dict[str, dict[str, Any]]:
    with RUNTIME_DIAGNOSTICS_LOCK:
        return {
            backend: public_runtime_diagnostic(dict(RUNTIME_DIAGNOSTICS.get(backend) or runtime_diagnostic_payload(
                backend, "unknown", installed=None, authenticated=None
            )))
            for backend in sorted(VALID_BACKENDS)
        }


def record_runtime_failure(backend: str, error: Any, *, spawn_failure: bool = False) -> None:
    text = str(error or "").strip()
    lower = text.lower()
    with RUNTIME_DIAGNOSTICS_LOCK:
        previous = dict(RUNTIME_DIAGNOSTICS.get(backend) or runtime_diagnostic_payload(
            backend, "unknown", installed=None, authenticated=None
        ))
    if auth_failure_text(text):
        current = runtime_diagnostic_payload(
            backend, "unauthenticated", installed=True, authenticated=False, version=previous.get("version")
        )
    elif spawn_failure and (
        isinstance(error, FileNotFoundError)
        or any(marker in lower for marker in ("no such file", "enoent", "failed to start"))
    ):
        current = runtime_diagnostic_payload(
            backend, "missing", installed=False, authenticated=False, version=previous.get("version")
        )
    else:
        current = previous
    failure = compact_memory_text(text, 700) if text else ""
    current["last_error"] = failure or "The latest provider run failed. Open the chat error for details, then retry or refresh runtime status."
    current["last_error_at"] = now_iso()
    store_runtime_diagnostic(current, preserve_last_error=False)


def record_runtime_success(backend: str) -> None:
    with RUNTIME_DIAGNOSTICS_LOCK:
        previous = dict(RUNTIME_DIAGNOSTICS.get(backend) or {})
    current = runtime_diagnostic_payload(
        backend,
        "ready",
        installed=True,
        authenticated=True,
        version=previous.get("version"),
    )
    store_runtime_diagnostic(current, preserve_last_error=False)


async def ensure_runtime_available(backend: str) -> dict[str, Any]:
    diagnostic = await asyncio.to_thread(runtime_diagnostic, backend)
    if diagnostic.get("status") == "ready":
        return diagnostic
    raise HTTPException(status_code=503, detail={
        "code": "runtime_unavailable",
        "backend": backend,
        "status": diagnostic.get("status") or "unknown",
        "message": diagnostic.get("message") or f"{runtime_display_name(backend)} is unavailable.",
        "action": diagnostic.get("action"),
    })


def runtime_priority(model: dict[str, Any]) -> int:
    try:
        return int(model["priority"])
    except Exception:
        return 9999


def session_backend_locked(sess: dict[str, Any]) -> bool:
    return any(str(sess.get(key) or "").strip() for key in ("session_id", "claude_session_id", "codex_thread_id"))


def codex_user_config_path() -> Path:
    return Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser() / "config.toml"


def load_codex_user_config(path: Path) -> dict[str, Any]:
    if tomllib is not None:
        with path.open("rb") as f:
            return tomllib.load(f)

    # Python 3.10 has no stdlib TOML parser. Only these top-level strings are
    # needed here; nested Codex configuration remains owned by the CLI.
    wanted = {"model", "model_reasoning_effort", "service_tier"}
    payload: dict[str, Any] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            break
        key, separator, raw_value = line.partition("=")
        key = key.strip()
        if not separator or key not in wanted:
            continue
        lexer = shlex.shlex(raw_value, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        values = list(lexer)
        if values:
            payload[key] = values[0]
    return payload


def codex_user_config_defaults() -> tuple[str, str, str]:
    path = codex_user_config_path()
    try:
        payload = load_codex_user_config(path)
    except Exception as exc:
        logger.debug("codex config default discovery skipped path=%s: %s", path, exc)
        return "", "", ""
    model = str(payload.get("model") or "").strip()
    effort = str(payload.get("model_reasoning_effort") or "").strip()
    service_tier = str(payload.get("service_tier") or "").strip()
    return model, effort, service_tier


def codex_default_service_tier(model: str) -> str:
    return CODEX_FALLBACK_SERVICE_TIERS.get(str(model or "").strip(), "")


def discovered_codex_default_model(models: list[dict[str, Any]], preferred_slug: str = "") -> dict[str, Any] | None:
    if preferred_slug:
        for model in models:
            slug = str(model.get("slug") or model.get("id") or "").strip()
            if slug == preferred_slug:
                return model
    for model in models:
        slug = str(model.get("slug") or model.get("id") or "").strip()
        if slug:
            return model
    return None


def discover_codex_catalog() -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    model_options: list[dict[str, Any]] = []
    effort_options: list[dict[str, Any]] = []
    model_efforts: dict[str, list[dict[str, Any]]] = {}
    default_model = ""
    default_model_label = ""
    default_effort = ""
    default_effort_label = ""
    default_service_tier = ""
    model_source = "codex debug models"
    effort_source = "codex debug models"
    configured_model, configured_effort, configured_service_tier = codex_user_config_defaults()
    try:
        payload = json.loads(run_catalog_command([CODEX_BIN, "debug", "models"]))
        raw_models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(raw_models, list):
            models = [model for model in raw_models if isinstance(model, dict)]
    except Exception as exc:
        logger.warning("codex model discovery failed: %s", exc)
        model_source = f"{model_source} failed"
        effort_source = f"{effort_source} failed"

    visible_models = [
        model for model in models
        if str(model.get("visibility") or "list") == "list" and model.get("supported_in_api", True) is not False
    ]
    visible_models.sort(key=runtime_priority)
    default_entry = discovered_codex_default_model(visible_models, configured_model)
    if default_entry:
        default_model = configured_model or str(default_entry.get("slug") or default_entry.get("id") or "").strip()
        default_model_label = str(default_entry.get("display_name") or title_model_label(default_model)).strip()
        default_effort = configured_effort or str(default_entry.get("default_reasoning_level") or "").strip()
        default_effort_label = title_effort_label(default_effort) if default_effort else ""
        default_service_tier = (
            configured_service_tier
            or str(default_entry.get("default_service_tier") or "").strip()
            or codex_default_service_tier(default_model)
        )
    elif configured_model:
        default_model = configured_model
        default_model_label = title_model_label(configured_model)
        default_effort = configured_effort
        default_effort_label = title_effort_label(default_effort) if default_effort else ""
        default_service_tier = configured_service_tier or codex_default_service_tier(default_model)
    elif not visible_models:
        default_model, default_model_label = CODEX_FALLBACK_MODELS[0]
        default_effort = configured_effort or "medium"
        default_effort_label = title_effort_label(default_effort)
        default_service_tier = configured_service_tier or codex_default_service_tier(default_model)
    if default_model == "gpt-5.5" and default_effort == "medium":
        default_effort = "xhigh"
        default_effort_label = "XHigh"
    for model in visible_models:
        slug = str(model.get("slug") or model.get("id") or "").strip()
        if not slug:
            continue
        label = str(model.get("display_name") or title_model_label(slug)).strip()
        model_effort_options: list[dict[str, Any]] = []
        levels = model.get("supported_reasoning_levels")
        if isinstance(levels, list):
            for level in levels:
                if not isinstance(level, dict):
                    continue
                effort = str(level.get("effort") or "").strip()
                if effort:
                    option = runtime_option(effort, title_effort_label(effort))
                    effort_options.append(option)
                    model_effort_options.append(option)
        if model_effort_options:
            model_efforts[slug] = unique_runtime_options(model_effort_options, None)[1:]
        service_tier = str(model.get("default_service_tier") or "").strip()
        model_options.append(runtime_option(slug, label, efforts=model_efforts.get(slug, []), service_tier=service_tier or None))

    if not model_options:
        for slug, label in CODEX_FALLBACK_MODELS:
            fallback_efforts = [
                runtime_option(effort, title_effort_label(effort))
                for effort in CODEX_FALLBACK_MODEL_EFFORTS.get(slug, CODEX_FALLBACK_EFFORTS)
            ]
            model_efforts[slug] = fallback_efforts
            service_tier = codex_default_service_tier(slug)
            model_options.append(runtime_option(slug, label, efforts=fallback_efforts, service_tier=service_tier or None))
    if not effort_options:
        effort_options.extend(runtime_option(effort, title_effort_label(effort)) for effort in CODEX_FALLBACK_EFFORTS)
    if configured_model and not any(option.get("value") == configured_model for option in model_options):
        model_options.insert(0, runtime_option(configured_model, title_model_label(configured_model)))
    if configured_effort and not any(option.get("value") == configured_effort for option in effort_options):
        effort_options.append(runtime_option(configured_effort, title_effort_label(configured_effort)))

    return {
        "models": unique_runtime_options(model_options, f"Server default ({default_model_label})" if default_model_label else None),
        "efforts": unique_runtime_options(effort_options, f"Server default ({default_effort_label})" if default_effort_label else None),
        "model_efforts": model_efforts,
        "model_source": model_source,
        "effort_source": effort_source,
        "default_model": default_model or None,
        "default_effort": default_effort or None,
        "default_service_tier": default_service_tier or None,
    }


def parse_claude_help_catalog() -> dict[str, Any]:
    model_options: list[dict[str, str]] = []
    effort_options: list[dict[str, str]] = []
    model_source = "claude --help"
    effort_source = "claude --help"
    default_model = (
        os.environ.get("CLAUDE_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or agentsdock_setting("CLAUDE_MODEL", "")
        or "sonnet"
    )
    default_effort = os.environ.get("CLAUDE_EFFORT") or agentsdock_setting("CLAUDE_EFFORT", "")
    supports_ultracode = claude_supports_effort("ultracode")
    try:
        help_text = run_catalog_command([CLAUDE_BIN, "--help"])
    except Exception as exc:
        logger.warning("claude model discovery failed: %s", exc)
        return {
            "models": unique_runtime_options(
                [
                    runtime_option("fable", "Fable"),
                    runtime_option("claude-fable-5", "Fable 5"),
                    runtime_option("sonnet", "Sonnet"),
                    runtime_option("opus", "Opus"),
                    runtime_option("opus[1m]", "Opus 1M"),
                    runtime_option("claude-opus-4-8", "Opus 4.8"),
                    runtime_option("claude-opus-4-8[1m]", "Opus 4.8 1M"),
                    runtime_option("haiku", "Haiku"),
                ],
                title_model_label(default_model),
            ),
            "efforts": unique_runtime_options(
                [
                    runtime_option("low", "Low"),
                    runtime_option("medium", "Medium"),
                    runtime_option("high", "High"),
                    runtime_option("xhigh", "XHigh"),
                    runtime_option("max", "Max"),
                    *([runtime_option("ultracode", "Ultracode")] if supports_ultracode else []),
                ],
                title_effort_label(default_effort) if default_effort else "",
            ),
            "model_source": f"{model_source} failed",
            "effort_source": f"{effort_source} failed",
            "default_model": default_model,
            "default_effort": default_effort or None,
        }

    model_match = re.search(r"--model <model>.*?\((?:e\.g\.\s*)?([^)]+)\)", help_text, re.IGNORECASE | re.DOTALL)
    if model_match:
        aliases = re.findall(r"'([^']+)'", model_match.group(1))
        for alias in aliases:
            model_options.append(runtime_option(alias, title_model_label(alias)))
    for alias, label in (
        ("fable", "Fable"),
        ("claude-fable-5", "Fable 5"),
        ("sonnet", "Sonnet"),
        ("opus", "Opus"),
        ("opus[1m]", "Opus 1M"),
        ("claude-opus-4-8", "Opus 4.8"),
        ("claude-opus-4-8[1m]", "Opus 4.8 1M"),
        ("haiku", "Haiku"),
    ):
        model_options.append(runtime_option(alias, label))

    effort_match = re.search(r"--effort\s+<level>.*?\(([^)]+)\)", help_text, re.IGNORECASE | re.DOTALL)
    if effort_match:
        for effort in re.split(r"[,/\s]+", effort_match.group(1)):
            clean = effort.strip()
            if clean and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", clean):
                effort_options.append(runtime_option(clean, title_effort_label(clean)))
    if supports_ultracode:
        effort_options.append(runtime_option("ultracode", "Ultracode"))

    return {
        "models": unique_runtime_options(model_options, title_model_label(default_model)),
        "efforts": unique_runtime_options(effort_options, title_effort_label(default_effort) if default_effort else ""),
        "model_source": model_source,
        "effort_source": effort_source,
        "default_model": default_model,
        "default_effort": default_effort or None,
    }


def discover_runtime_catalog(*, force_runtime_probe: bool = False) -> dict[str, Any]:
    diagnostics = refresh_runtime_diagnostics(force=force_runtime_probe)
    catalog = {
        "generated_at": now_iso(),
        "backends": {
            BACKEND_CLAUDE: parse_claude_help_catalog(),
            BACKEND_CODEX: discover_codex_catalog(),
        },
    }
    for backend, diagnostic in diagnostics.items():
        catalog["backends"][backend]["diagnostic"] = public_runtime_diagnostic(diagnostic)
    return catalog


def session_prompt_addendum(sess: dict[str, Any]) -> str:
    custom_prompt = str(sess.get("system_prompt") or "").strip()
    if not custom_prompt:
        return ""
    return (
        "\n[Per-chat system instructions]\n"
        f"{custom_prompt}\n"
        "[End per-chat system instructions]\n\n"
    )


def session_system_prompt(session_id: str, sess: dict[str, Any], manifest_path: Path) -> str:
    return SYSTEM_PROMPT.format(
        manifest_path=str(manifest_path),
        terminal_session=terminal_session_name(session_id),
    ) + session_prompt_addendum(sess)


def build_claude_cmd(session_id: str, sess: dict[str, Any], manifest_path: Path, *, provider_id: str | None = None) -> list[str]:
    system_prompt = session_system_prompt(
        session_id,
        sess,
        manifest_path,
    )
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt", system_prompt,
        "--disallowedTools", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
    ]
    if sess.get("model"):
        cmd.extend(["--model", str(sess["model"])])
    if sess.get("effort"):
        cmd.extend(["--effort", str(sess["effort"])])
    if provider_id:
        cmd.extend(["--resume", provider_id])
        if sess.get("fork_from"):
            cmd.append("--fork-session")
            cmd.extend(["--name", f"Fork: {sess.get('title') or sess['id']}"])
    return cmd


def build_codex_cmd(session_id: str, sess: dict[str, Any], prompt: str, manifest_path: Path) -> list[str]:
    provider_id = sess.get("codex_thread_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CODEX else None
    )
    configured_model, configured_effort, configured_service_tier = codex_user_config_defaults()
    full_prompt = CODEX_PROMPT_PRELUDE.format(
        manifest_path=str(manifest_path),
        terminal_session=terminal_session_name(session_id),
    ) + session_prompt_addendum(sess) + prompt
    model = str(sess.get("model") or configured_model or CODEX_DEFAULT_MODEL).strip()
    normalized_effort = normalize_runtime_effort(
        BACKEND_CODEX,
        sess.get("effort") or configured_effort or CODEX_DEFAULT_EFFORT,
    )
    effective_service_tier = configured_service_tier or codex_default_service_tier(model)
    cmd = [CODEX_BIN, "exec"]
    if provider_id:
        cmd.append("resume")
    if model:
        cmd.extend(["--model", model])
    if normalized_effort:
        cmd.extend(["-c", f"model_reasoning_effort={normalized_effort}"])
    if effective_service_tier:
        cmd.extend(["-c", f"service_tier={effective_service_tier}"])
    if provider_id:
        cmd.append(str(provider_id))
    cmd.append("--json")
    cmd.extend(["-c", "model_reasoning_summary=detailed"])
    cmd.extend(["--disable", "image_generation"])
    if not provider_id:
        cmd.append("--skip-git-repo-check")
    cmd.append("--dangerously-bypass-approvals-and-sandbox")
    cmd.append(full_prompt)
    return cmd


def claude_result_error(event: dict[str, Any]) -> str | None:
    if event.get("type") != "result":
        return None
    errors = event.get("errors")
    if event.get("subtype") == "error_during_execution" or errors:
        if isinstance(errors, list) and errors:
            visible_errors = [
                str(item).strip()
                for item in errors
                if str(item or "").strip() and not is_claude_internal_diagnostic(item)
            ]
            if visible_errors:
                return "; ".join(visible_errors)
            return "Claude stopped before completing the turn."
        result = event.get("result")
        return str(result or "Claude execution failed")
    return None


def is_claude_internal_diagnostic(value: Any) -> bool:
    return str(value or "").strip().startswith("[ede_diagnostic]")


def is_expected_claude_interruption_result(event: dict[str, Any]) -> bool:
    # This classifier is only used after the run has been placed in
    # STOPPED_RUNS. Claude has emitted multiple terminal shapes for that same
    # intentional interruption (including aborted_tools/tool_use and
    # aborted_streaming/null). The internal-only diagnostic is the stable
    # signal; keep any real provider error visible.
    errors = event.get("errors")
    return (
        event.get("type") == "result"
        and event.get("subtype") == "error_during_execution"
        and isinstance(errors, list)
        and bool(errors)
        and all(is_claude_internal_diagnostic(item) for item in errors)
    )


def concise_error_message(value: Any) -> str:
    if value is None:
        return "Unknown error"
    if isinstance(value, str):
        text = value.strip()
        with suppress(Exception):
            parsed = json.loads(text)
            return concise_error_message(parsed)
        return text or "Unknown error"
    if isinstance(value, dict):
        if "error" in value:
            message = concise_error_message(value.get("error"))
            status = value.get("status")
            if status and f"status {status}" not in message.lower():
                return f"{message} (status {status})"
            return message
        message = str(value.get("message") or value.get("detail") or "").strip()
        code = str(value.get("code") or value.get("type") or "").strip()
        if message and code:
            return f"{message} ({code})"
        if message:
            return message
        if code:
            return code
        with suppress(Exception):
            return compact_memory_text(json.dumps(value, separators=(",", ":")), 4000)
    return str(value)


def is_codex_reconnect_notice(message: str) -> bool:
    return bool(re.match(r"^Reconnecting\.\.\.\s+\d+/\d+\b", str(message or "").strip(), re.IGNORECASE))


def codex_result_error(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type") or "")
    if event_type == "error":
        message = concise_error_message(event.get("message") or event.get("error") or event)
        return None if is_codex_reconnect_notice(message) else message
    if event_type == "turn.failed":
        return concise_error_message(event.get("error") or event.get("message") or event)
    if event_type == "event_msg":
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        payload_type = str(payload.get("type") or "")
        if payload_type in {"error", "turn_failed"}:
            return concise_error_message(payload.get("message") or payload.get("error") or payload)
    return None


def parse_codex_call_arguments(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        with suppress(Exception):
            return json.loads(text)
        return text
    return value if value is not None else {}


def codex_call_tool(call_id: str, name: str, arguments: Any) -> dict[str, Any]:
    parsed = parse_codex_call_arguments(arguments)
    tool_name = str(name or "tool")
    tool_input: Any = parsed
    if tool_name == "exec_command" and isinstance(parsed, dict):
        command = str(parsed.get("cmd") or parsed.get("command") or "")
        tool_input = {"command": command}
        if parsed.get("workdir"):
            tool_input["workdir"] = parsed["workdir"]
    elif tool_name == "apply_patch" and isinstance(parsed, str):
        tool_input = {"patch": parsed}
    elif tool_name == "apply_patch" and isinstance(parsed, dict) and "input" in parsed:
        tool_input = {"patch": parsed.get("input")}
    return {
        "id": call_id or f"tool_{uuid.uuid4().hex[:8]}",
        "name": "Bash" if tool_name == "exec_command" else tool_name,
        "input": tool_input if isinstance(tool_input, dict) else {"value": tool_input},
    }


def codex_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    with suppress(Exception):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def codex_output_exit_code(text: str) -> int | None:
    for pattern in (r"Process exited with code (-?\d+)", r"Exit code:\s*(-?\d+)"):
        match = re.search(pattern, text)
        if match:
            with suppress(Exception):
                return int(match.group(1))
    return None


def codex_reasoning_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("text") or "").strip()
    if text:
        return text
    summary = payload.get("summary")
    if isinstance(summary, list):
        parts: list[str] = []
        for item in summary:
            if isinstance(item, dict):
                part = str(item.get("text") or item.get("summary_text") or "").strip()
                if part:
                    parts.append(part)
            elif isinstance(item, str) and item.strip():
                parts.append(item.strip())
        return "\n".join(parts).strip()
    return ""


async def codex_app_server_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    env = runner_env()
    codex_dir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if codex_dir and codex_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = codex_dir + os.pathsep + env.get("PATH", "")
    proc = await asyncio.create_subprocess_exec(
        CODEX_BIN,
        "app-server",
        "--listen",
        "stdio://",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=DEFAULT_CWD,
        env=env,
        limit=PROCESS_STREAM_LIMIT,
        start_new_session=True,
    )
    stderr_lines: list[str] = []

    async def read_stderr() -> None:
        if not proc.stderr:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_lines.append(line.decode("utf-8", "replace").strip())

    async def send(request_id: int, request_method: str, request_params: dict[str, Any]) -> None:
        if not proc.stdin:
            raise RuntimeError("codex app-server stdin unavailable")
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": request_method,
            "params": request_params,
        }
        proc.stdin.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        await proc.stdin.drain()

    async def read_response(request_id: int) -> dict[str, Any]:
        if not proc.stdout:
            raise RuntimeError("codex app-server stdout unavailable")
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=CODEX_APP_SERVER_TIMEOUT_SECONDS)
            if not line:
                raise RuntimeError("codex app-server exited before response")
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                continue
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(json.dumps(message["error"], separators=(",", ":")))
            return message.get("result", {})

    stderr_task = asyncio.create_task(read_stderr())
    try:
        await send(1, "initialize", {
            "clientInfo": {"name": "agents-server", "version": "0"},
            "capabilities": {"experimentalApi": True},
        })
        await read_response(1)
        await send(2, method, params)
        return await read_response(2)
    finally:
        await terminate_process_tree(proc, grace=3)
        stderr_task.cancel()
        with suppress(asyncio.CancelledError):
            await stderr_task
        if stderr_lines:
            logger.debug("codex app-server stderr: %s", "\n".join(stderr_lines[-20:]))


async def fork_codex_thread(source_thread_id: str, sess: dict[str, Any]) -> str:
    cwd = existing_cwd(str(sess.get("cwd") or DEFAULT_CWD))
    params: dict[str, Any] = {
        "threadId": source_thread_id,
        "cwd": cwd,
        "sandbox": "danger-full-access",
        "approvalPolicy": "never",
        "ephemeral": False,
    }
    if sess.get("model"):
        params["model"] = sess["model"]
    result = await codex_app_server_request("thread/fork", params)
    thread = result.get("thread") if isinstance(result, dict) else None
    forked_id = thread.get("id") if isinstance(thread, dict) else None
    if not forked_id:
        raise RuntimeError("codex app-server did not return a forked thread id")
    return str(forked_id)


def artifact_record(session_id: str, entry: str | dict[str, Any]) -> dict[str, Any] | None:
    if isinstance(entry, str):
        path = entry
        title = None
        text = None
    elif isinstance(entry, dict):
        path = entry.get("path", "")
        title = entry.get("title")
        text = entry.get("text")
    else:
        return None
    if not path or not os.path.isfile(path):
        return None
    file_id = f"art_{uuid.uuid4().hex[:16]}"
    src = Path(path)
    ext = src.suffix
    dest_dir = FILES_ROOT / file_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name(src.name)
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    rec = {
        "id": file_id,
        "session_id": session_id,
        "kind": "artifact",
        "title": title or src.name,
        "text": text,
        "path": str(dest),
        "source_path": str(src),
        "filename": dest.name,
        "size": dest.stat().st_size,
        "content_type": guess_content_type(dest.name),
        "created_at": now_iso(),
    }
    (dest_dir / "meta.json").write_text(json.dumps(rec, indent=2))
    return rec


def path_is_relative_to(path: Path, root: Path) -> bool:
    with suppress(ValueError):
        path.relative_to(root)
        return True
    return False


def normalized_link_target(target: str) -> str:
    clean = target.strip()
    if clean.startswith("file://"):
        parsed = urlparse(clean)
        return unquote(parsed.path)
    return unquote(clean)


def session_file_for_link(session_id: str, target: str) -> dict[str, Any]:
    clean = normalized_link_target(target)
    if not clean:
        raise HTTPException(status_code=400, detail="missing target")
    clean_name = Path(clean).name
    files_root = FILES_ROOT.resolve()
    for rec in list_session_file_records(session_id):
        candidates = {
            str(rec.get("id") or ""),
            str(rec.get("filename") or ""),
            str(rec.get("title") or ""),
            str(rec.get("source_path") or ""),
            str(rec.get("path") or ""),
        }
        if clean not in candidates and clean_name not in candidates:
            continue
        path = Path(str(rec.get("path") or "")).resolve()
        if path.is_file() and path_is_relative_to(path, files_root):
            return rec
    raise HTTPException(status_code=404, detail=f"linked file is not a registered artifact: {clean}")


def guess_content_type(filename: str) -> str:
    lower = filename.lower()
    if lower.endswith((".mp4", ".m4v")):
        return "video/mp4"
    if lower.endswith(".mov"):
        return "video/quicktime"
    if lower.endswith(".webm"):
        return "video/webm"
    if lower.endswith(".png"):
        return "image/png"
    if lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if lower.endswith(".gif"):
        return "image/gif"
    if lower.endswith(".pdf"):
        return "application/pdf"
    if lower.endswith((".md", ".markdown")):
        return "text/markdown"
    if lower.endswith(".txt"):
        return "text/plain"
    if lower.endswith(".csv"):
        return "text/csv"
    if lower.endswith(".json"):
        return "application/json"
    return "application/octet-stream"


def effective_content_type(filename: str, recorded: str | None = None) -> str:
    content_type = str(recorded or "").strip()
    base_type = content_type.split(";", 1)[0].strip().lower()
    if not base_type or base_type in {"application/octet-stream", "binary/octet-stream"}:
        return guess_content_type(filename)
    return content_type


def file_response_media_type(meta: dict[str, Any]) -> str:
    filename = str(meta.get("filename") or Path(str(meta.get("path") or "")).name)
    return effective_content_type(filename, str(meta.get("content_type") or ""))


def manifest_entry_path(entry: str | dict[str, Any]) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("path") or "")
    return ""


def file_signature(path: str) -> tuple[int, int] | None:
    try:
        stat = Path(path).expanduser().stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def live_manifest_entry_ready(path: str, stable_files: dict[str, tuple[int, int]]) -> bool:
    signature = file_signature(path)
    if signature is None:
        return False
    previous = stable_files.get(path)
    stable_files[path] = signature
    return previous == signature and time.time_ns() - signature[1] > 750_000_000


async def collect_manifest(
    session_id: str,
    run_id: str,
    manifest_path: Path,
    *,
    seen_artifacts: set[str] | None = None,
    stable_files: dict[str, tuple[int, int]] | None = None,
    final: bool = True,
) -> None:
    if not manifest_path.exists():
        return
    try:
        data = json.loads(manifest_path.read_text())
    except Exception as e:
        if final:
            await append_event(session_id, "artifact_error", {"run_id": run_id, "error": f"manifest parse failed: {e}"})
        return
    if final:
        with suppress(OSError):
            manifest_path.unlink()
    seen = seen_artifacts if seen_artifacts is not None else set()
    for entry in data.get("files", []):
        path = manifest_entry_path(entry)
        if not path or path in seen:
            continue
        if not final and stable_files is not None and not live_manifest_entry_ready(path, stable_files):
            continue
        seen.add(path)
        rec = artifact_record(session_id, entry)
        if rec:
            await append_event(session_id, "artifact_created", {"run_id": run_id, "artifact": rec})
        elif final:
            await append_event(session_id, "artifact_error", {"run_id": run_id, "path": path, "error": "file not found"})


async def watch_manifest_artifacts(session_id: str, run_id: str, manifest_path: Path, seen_artifacts: set[str]) -> None:
    stable_files: dict[str, tuple[int, int]] = {}
    while True:
        await collect_manifest(
            session_id,
            run_id,
            manifest_path,
            seen_artifacts=seen_artifacts,
            stable_files=stable_files,
            final=False,
        )
        await asyncio.sleep(1.0)


async def collect_recent_leftover_manifests(
    session_id: str,
    run_id: str,
    primary_manifest_path: Path,
    *,
    seen_artifacts: set[str],
    max_age_seconds: int = 6 * 60 * 60,
) -> None:
    """Recover artifacts when an agent writes to a stale run manifest path."""
    root = manifests_dir(session_id)
    if not root.exists():
        return
    cutoff = time.time() - max_age_seconds
    try:
        candidates = sorted(root.glob("*.json"), key=lambda path: path.stat().st_mtime)
    except OSError:
        return
    for candidate in candidates:
        if candidate == primary_manifest_path:
            continue
        try:
            if candidate.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        logger.info(
            "collecting leftover manifest session=%s run=%s path=%s",
            session_id,
            run_id,
            candidate,
        )
        await collect_manifest(session_id, run_id, candidate, seen_artifacts=seen_artifacts, final=True)


async def run_claude(session_id: str, run_id: str, prompt: str, sess: dict[str, Any], manifest_path: Path) -> None:
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    resume_provider_id, resume_skip_message = resolve_claude_resume_provider(sess, cwd)
    cmd = build_claude_cmd(session_id, sess, manifest_path, provider_id=resume_provider_id)
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    if resume_skip_message:
        await append_event(session_id, "cwd_fallback", {
            "run_id": run_id,
            "requested_cwd": requested_cwd,
            "cwd": cwd,
            "provider_session_id": claude_provider_id_for_session(sess),
            "message": resume_skip_message,
        })
    await append_event(session_id, "process_started", {"run_id": run_id, "backend": BACKEND_CLAUDE, "argv": cmd, "cwd": cwd})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=agent_runner_env(session_id),
            limit=PROCESS_STREAM_LIMIT,
            start_new_session=True,
        )
    except Exception as e:
        record_runtime_failure(BACKEND_CLAUDE, e, spawn_failure=True)
        await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": f"failed to start Claude: {e}", **run_event_metadata(run_id)})
        await append_turn_finished_event(session_id, {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "exit_code": None,
            "result_text": "",
            **run_event_metadata(run_id),
        })
        RUN_METADATA.pop(run_id, None)
        await release_turn_slot(session_id)
        schedule_next_queued_turn(session_id)
        return
    async with ACTIVE_LOCK:
        BUSY_SESSIONS.add(session_id)
        stop_requested = session_id in STOP_REQUESTS
        if stop_requested:
            STOP_REQUESTS.discard(session_id)
            STOPPED_RUNS.add(run_id)
        pgid = process_group_for_pid(proc.pid)
        ACTIVE[session_id] = {
            "proc": proc,
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "pid": proc.pid,
            "pgid": pgid,
            "cwd": cwd,
            "argv": cmd,
            "started_at": time.time(),
            "started_at_iso": now_iso(),
            "stop_requested": stop_requested,
            "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
            "stdout_total_lines": 0,
            "stdout_updated_at": None,
        }
    if stop_requested:
        await terminate_process_tree(proc)
    if not stop_requested and proc.stdin:
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    final_text = ""
    text_parts: list[str] = []
    provider_id: str | None = None
    current_tools: dict[str, dict[str, Any]] = {}
    changed_paths: set[str] = set()
    last_event = time.time()
    idle_killed = False
    stream_error: str | None = None
    result_error: str | None = None
    seen_artifacts: set[str] = set()
    manifest_watch_task = asyncio.create_task(watch_manifest_artifacts(session_id, run_id, manifest_path, seen_artifacts))

    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5)  # type: ignore[union-attr]
            except asyncio.TimeoutError:
                idle = time.time() - last_event
                if idle >= IDLE_WARN_SECONDS:
                    await append_event(session_id, "idle_warning", {"run_id": run_id, "idle_seconds": int(idle)})
                if idle >= IDLE_KILL_SECONDS:
                    idle_killed = True
                    await terminate_process_tree(proc)
                    break
                continue
            if not raw:
                break
            last_event = time.time()
            decoded = raw.decode("utf-8", "replace").rstrip("\r\n")
            await append_active_stdout(session_id, decoded)
            line = decoded.strip()
            if not line:
                continue
            await append_event(session_id, "raw_event", {"run_id": run_id, "backend": BACKEND_CLAUDE, "raw": line})
            try:
                event = json.loads(line)
            except Exception:
                continue
            if event.get("session_id") and not provider_id:
                provider_id = event["session_id"]
            etype = event.get("type")
            if etype == "assistant":
                for block in event.get("message", {}).get("content", []):
                    btype = block.get("type")
                    if btype == "text" and block.get("text"):
                        text = clean_assistant_text(block["text"])
                        if text:
                            text_parts.append(text)
                            await append_event(session_id, "assistant_text", {"run_id": run_id, "text": text, **run_event_metadata(run_id)})
                    elif btype == "thinking" and block.get("thinking"):
                        await append_event(session_id, "reasoning_summary", {"run_id": run_id, "text": block["thinking"]})
                    elif btype in ("tool_use", "server_tool_use"):
                        tid = block.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        tool = {"id": tid, "name": block.get("name", "tool"), "input": block.get("input", {})}
                        current_tools[tid] = tool
                        changed_paths.update(tool_changed_paths(tool))
                        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})
            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        content = event_output_text(block.get("content", ""))
                        await append_event(session_id, "tool_finished", {
                            "run_id": run_id,
                            "tool_id": tid,
                            "tool": current_tools.pop(tid, None),
                            "output": content,
                            "is_error": block.get("is_error") is True,
                        })
            elif etype == "result":
                if run_id in STOPPED_RUNS and is_expected_claude_interruption_result(event):
                    continue
                result_error = claude_result_error(event)
                if result_error:
                    provider_id = None
                    await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": result_error, **run_event_metadata(run_id)})
                    continue
                final_text = event.get("result", "") or final_text
                if event.get("session_id"):
                    provider_id = event["session_id"]
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Claude run failed session=%s run=%s", session_id, run_id)
    finally:
        manifest_watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await manifest_watch_task
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in STOPPED_RUNS
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Claude stream failed: {stream_error}", **run_event_metadata(run_id)})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout", **run_event_metadata(run_id)})
    if not stopped and proc.returncode not in (0, None) and stderr:
        await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode, **run_event_metadata(run_id)})
    if not stopped and (result_error or stream_error or proc.returncode not in (0, None)):
        record_runtime_failure(BACKEND_CLAUDE, result_error or stream_error or stderr or f"exit {proc.returncode}")
    elif not stopped:
        record_runtime_success(BACKEND_CLAUDE)
    if provider_id and not result_error:
        await STORE.save_provider_session(session_id, provider_id, BACKEND_CLAUDE, cwd=cwd)
        await append_event(session_id, "provider_session", {"run_id": run_id, "backend": BACKEND_CLAUDE, "provider_session_id": provider_id})
    result_text = clean_assistant_text(final_text or "\n\n".join(text_parts).strip())
    await collect_manifest(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts, final=True)
    await collect_recent_leftover_manifests(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts)
    await publish_turn_code_diff(session_id, run_id, BACKEND_CLAUDE, cwd, diff_baseline, changed_paths)
    await append_turn_finished_event(session_id, {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "exit_code": proc.returncode,
        "result_text": result_text,
        "stopped": stopped,
        **run_event_metadata(run_id),
    })
    RUN_METADATA.pop(run_id, None)
    await release_turn_slot(session_id)
    drain_queue = should_schedule_queue_after_finish(session_id, stopped)
    STOPPED_RUNS.discard(run_id)
    if drain_queue:
        schedule_next_queued_turn(session_id)


async def run_codex(
    session_id: str,
    run_id: str,
    prompt: str,
    sess: dict[str, Any],
    manifest_path: Path,
    *,
    allow_compaction_rollover: bool = True,
    diff_baseline: dict[str, str] | None = None,
) -> None:
    resumed_provider_id = session_provider_id(sess)
    cmd = build_codex_cmd(session_id, sess, prompt, manifest_path)
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    if diff_baseline is None:
        diff_baseline = await capture_git_baseline(session_id, run_id, cwd)
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    await append_event(session_id, "process_started", {"run_id": run_id, "backend": BACKEND_CODEX, "argv": cmd[:-1] + ["<prompt>"], "cwd": cwd})
    env = agent_runner_env(session_id)
    codex_dir = os.path.dirname(os.path.abspath(CODEX_BIN))
    if codex_dir and codex_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = codex_dir + os.pathsep + env.get("PATH", "")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=PROCESS_STREAM_LIMIT,
            start_new_session=True,
        )
    except Exception as e:
        record_runtime_failure(BACKEND_CODEX, e, spawn_failure=True)
        await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CODEX, "message": f"failed to start Codex: {e}", **run_event_metadata(run_id)})
        await append_turn_finished_event(session_id, {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "exit_code": None,
            "result_text": "",
            **run_event_metadata(run_id),
        })
        RUN_METADATA.pop(run_id, None)
        await release_turn_slot(session_id)
        schedule_next_queued_turn(session_id)
        return
    async with ACTIVE_LOCK:
        BUSY_SESSIONS.add(session_id)
        stop_requested = session_id in STOP_REQUESTS
        if stop_requested:
            STOP_REQUESTS.discard(session_id)
            STOPPED_RUNS.add(run_id)
        pgid = process_group_for_pid(proc.pid)
        ACTIVE[session_id] = {
            "proc": proc,
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "pid": proc.pid,
            "pgid": pgid,
            "cwd": cwd,
            "argv": cmd[:-1] + ["<prompt>"],
            "started_at": time.time(),
            "started_at_iso": now_iso(),
            "stop_requested": stop_requested,
            "stdout_tail": deque(maxlen=LIVE_STDOUT_MAX_LINES),
            "stdout_total_lines": 0,
            "stdout_updated_at": None,
        }
    if stop_requested:
        await terminate_process_tree(proc)

    text_parts: list[str] = []
    provider_id: str | None = sess.get("codex_thread_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CODEX else None
    )
    last_event = time.time()
    resume_started_at = last_event
    idle_killed = False
    resume_stalled = False
    stream_error: str | None = None
    codex_error: str | None = None
    seen_artifacts: set[str] = set()
    seen_raw_lines: set[str] = set()
    seen_text_parts: set[str] = set()
    seen_reasoning: set[str] = set()
    tool_calls: dict[str, dict[str, Any]] = {}
    started_tool_ids: set[str] = set()
    finished_tool_ids: set[str] = set()
    changed_paths: set[str] = set()
    codex_history_path = find_codex_history(provider_id) if provider_id else None
    codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0
    manifest_watch_task = asyncio.create_task(watch_manifest_artifacts(session_id, run_id, manifest_path, seen_artifacts))

    async def emit_provider_session(new_provider_id: str) -> None:
        nonlocal provider_id, codex_history_path, codex_history_pos
        if not new_provider_id:
            return
        provider_id = new_provider_id
        await STORE.save_provider_session(session_id, provider_id, BACKEND_CODEX)
        await append_event(session_id, "provider_session", {"run_id": run_id, "backend": BACKEND_CODEX, "provider_session_id": provider_id})
        if not codex_history_path:
            codex_history_path = find_codex_history(provider_id)
            codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0

    async def emit_assistant_text(text: str) -> None:
        text = clean_assistant_text(text)
        if not text or text in seen_text_parts:
            return
        seen_text_parts.add(text)
        text_parts.append(text)
        await append_event(session_id, "assistant_text", {"run_id": run_id, "text": text, **run_event_metadata(run_id)})

    async def emit_reasoning_text(text: str) -> None:
        text = str(text or "").strip()
        if not text or text in seen_reasoning:
            return
        seen_reasoning.add(text)
        await append_event(session_id, "reasoning_summary", {"run_id": run_id, "text": text})

    async def emit_tool_started(tool: dict[str, Any]) -> None:
        tool_id = str(tool.get("id") or f"tool_{uuid.uuid4().hex[:8]}")
        tool["id"] = tool_id
        tool_calls[tool_id] = tool
        changed_paths.update(tool_changed_paths(tool))
        if tool_id in started_tool_ids:
            return
        started_tool_ids.add(tool_id)
        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})

    async def emit_tool_finished(tool_id: str, output: Any, exit_code: int | None = None) -> None:
        tool_id = str(tool_id or f"tool_{uuid.uuid4().hex[:8]}")
        if tool_id in finished_tool_ids:
            return
        finished_tool_ids.add(tool_id)
        output_text = codex_output_text(output)
        tool = tool_calls.get(tool_id) or {"id": tool_id, "name": "Tool", "input": {}}
        if exit_code is None:
            exit_code = codex_output_exit_code(output_text)
        await append_event(session_id, "tool_finished", {
            "run_id": run_id,
            "tool_id": tool_id,
            "tool": tool,
            "output": output_text,
            "exit_code": exit_code,
        })

    async def handle_codex_event(event: dict[str, Any]) -> bool:
        handled = False
        etype = str(event.get("type") or "")
        result_error = codex_result_error(event)
        if result_error:
            nonlocal codex_error
            codex_error = result_error
            return True
        if etype == "thread.started" and event.get("thread_id"):
            await emit_provider_session(str(event["thread_id"]))
            return True
        if etype in ("item.started", "item.completed"):
            item = event.get("item", {}) or {}
            itype = item.get("type", "")
            if itype == "command_execution":
                tool = {
                    "id": item.get("id") or f"cmd_{uuid.uuid4().hex[:8]}",
                    "name": "Bash",
                    "input": {"command": item.get("command", "")},
                }
                if etype == "item.started":
                    await emit_tool_started(tool)
                else:
                    await emit_tool_finished(str(tool["id"]), item.get("aggregated_output", ""), item.get("exit_code"))
                handled = True
            elif itype == "agent_message" and etype == "item.completed":
                await emit_assistant_text(str(item.get("text") or ""))
                handled = True
            elif itype in ("reasoning", "agent_reasoning") and etype == "item.completed":
                await emit_reasoning_text(codex_reasoning_text(item))
                handled = True
            return handled
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if etype == "event_msg":
            payload_type = str(payload.get("type") or "")
            if payload_type == "agent_message":
                await emit_assistant_text(str(payload.get("message") or ""))
                return True
            if payload_type == "agent_reasoning":
                await emit_reasoning_text(str(payload.get("text") or ""))
                return True
            return False
        if etype == "response_item":
            payload_type = str(payload.get("type") or "")
            if payload_type == "message" and payload.get("role") == "assistant":
                await emit_assistant_text(text_from_content(payload.get("content")))
                return True
            if payload_type in {"function_call", "custom_tool_call"}:
                call_id = str(payload.get("call_id") or payload.get("id") or f"tool_{uuid.uuid4().hex[:8]}")
                arguments = payload.get("arguments") if "arguments" in payload else payload.get("input")
                await emit_tool_started(codex_call_tool(call_id, str(payload.get("name") or "tool"), arguments))
                return True
            if payload_type in {"function_call_output", "custom_tool_call_output"}:
                await emit_tool_finished(str(payload.get("call_id") or payload.get("id") or ""), payload.get("output"))
                return True
            if payload_type in {"reasoning", "agent_reasoning"}:
                await emit_reasoning_text(codex_reasoning_text(payload))
                return True
        return False

    async def handle_codex_line(line: str, *, record_raw: bool) -> bool:
        line = line.strip()
        if not line or not line.startswith("{") or line in seen_raw_lines:
            return False
        seen_raw_lines.add(line)
        if record_raw:
            await append_event(session_id, "raw_event", {"run_id": run_id, "backend": BACKEND_CODEX, "raw": line})
        try:
            event = json.loads(line)
        except Exception:
            return False
        return await handle_codex_event(event)

    async def drain_codex_history() -> bool:
        nonlocal codex_history_path, codex_history_pos
        if not codex_history_path and provider_id:
            codex_history_path = find_codex_history(provider_id)
            codex_history_pos = codex_history_path.stat().st_size if codex_history_path and codex_history_path.exists() else 0
        if not codex_history_path or not codex_history_path.exists():
            return False
        try:
            size = codex_history_path.stat().st_size
            if size < codex_history_pos:
                codex_history_pos = 0
            if size <= codex_history_pos:
                return False
            with codex_history_path.open("r", encoding="utf-8", errors="ignore") as f:
                f.seek(codex_history_pos)
                lines = f.readlines()
                codex_history_pos = f.tell()
        except OSError:
            return False
        consumed = False
        for history_line in lines:
            if await handle_codex_line(history_line, record_raw=False):
                consumed = True
            elif history_line.strip():
                consumed = True
        return consumed

    try:
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=5)  # type: ignore[union-attr]
            except asyncio.TimeoutError:
                if await drain_codex_history():
                    last_event = time.time()
                produced_activity = bool(
                    text_parts
                    or seen_reasoning
                    or started_tool_ids
                    or finished_tool_ids
                    or seen_artifacts
                )
                if (
                    resumed_provider_id
                    and CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS > 0
                    and not produced_activity
                    and time.time() - resume_started_at >= CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS
                ):
                    resume_stalled = True
                    logger.warning(
                        "Codex resume produced no activity before startup timeout session=%s run=%s provider=%s timeout=%ss",
                        session_id,
                        run_id,
                        resumed_provider_id,
                        CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS,
                    )
                    await terminate_process_tree(proc)
                    break
                idle = time.time() - last_event
                if idle >= IDLE_WARN_SECONDS:
                    await append_event(session_id, "idle_warning", {"run_id": run_id, "idle_seconds": int(idle)})
                if idle >= IDLE_KILL_SECONDS:
                    idle_killed = True
                    await terminate_process_tree(proc)
                    break
                continue
            if not raw:
                break
            last_event = time.time()
            decoded = raw.decode("utf-8", "replace").rstrip("\r\n")
            await append_active_stdout(session_id, decoded)
            line = decoded.strip()
            if line.startswith("{"):
                await handle_codex_line(line, record_raw=True)
            if await drain_codex_history():
                last_event = time.time()
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Codex run failed session=%s run=%s", session_id, run_id)
    finally:
        with suppress(Exception):
            await drain_codex_history()
        manifest_watch_task.cancel()
        with suppress(asyncio.CancelledError):
            await manifest_watch_task
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in STOPPED_RUNS
    terminal_error = codex_error or stderr
    produced_activity = bool(
        text_parts
        or seen_reasoning
        or started_tool_ids
        or finished_tool_ids
        or seen_artifacts
    )
    silent_completion = is_silent_codex_completion(
        stopped=stopped,
        stream_error=stream_error,
        idle_killed=idle_killed,
        returncode=proc.returncode,
        produced_activity=produced_activity,
    )
    should_rollover = should_recover_codex_resume(
        allow_rollover=allow_compaction_rollover,
        resumed_provider_id=resumed_provider_id,
        stopped=stopped,
        stream_error=stream_error,
        idle_killed=idle_killed,
        resume_stalled=resume_stalled,
        returncode=proc.returncode,
        produced_activity=produced_activity,
        terminal_error=terminal_error,
    )
    if should_rollover:
        recovery_reason = (
            f"Codex resume produced no activity for {CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS} seconds."
            if resume_stalled
            else terminal_error or "Codex resume exited successfully without producing any response events."
        )
        if resume_stalled:
            recovery_message = "Codex resume stalled before producing a reply. Retrying this turn once on a fresh thread with bounded recent memory."
        elif silent_completion:
            recovery_message = "Codex resume produced no reply. Retrying this turn once on a fresh thread with bounded recent memory."
        else:
            recovery_message = "Codex could not continue the provider thread. Retrying this turn once on a fresh thread with bounded recent memory."
        logger.warning(
            "recovering empty Codex resume session=%s run=%s provider=%s exit=%s reason=%s",
            session_id,
            run_id,
            resumed_provider_id,
            proc.returncode,
            compact_memory_text(recovery_reason, 500),
        )
        rollover = await rollover_codex_provider_session(
            session_id,
            run_id,
            str(resumed_provider_id),
            recovery_reason,
            message=recovery_message,
        )
        if rollover:
            fresh_session, memory = rollover
            retry_prompt = f"{memory}\n\n[Current user prompt]\n{prompt}"
            await run_codex(
                session_id,
                run_id,
                retry_prompt,
                fresh_session,
                manifest_path,
                allow_compaction_rollover=False,
                diff_baseline=diff_baseline,
            )
            return
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Codex stream failed: {stream_error}", **run_event_metadata(run_id)})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout", **run_event_metadata(run_id)})
    if resume_stalled:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": f"Codex resume produced no activity for {CODEX_RESUME_ACTIVITY_TIMEOUT_SECONDS} seconds.",
            "exit_code": proc.returncode,
            **run_event_metadata(run_id),
        })
    elif not stopped and proc.returncode not in (0, None):
        if codex_error:
            await append_event(session_id, "error", {"run_id": run_id, "message": codex_error, "exit_code": proc.returncode, **run_event_metadata(run_id)})
        elif stderr:
            await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode, **run_event_metadata(run_id)})
        else:
            await append_event(session_id, "error", {"run_id": run_id, "message": f"Codex exited {proc.returncode} without error output.", "exit_code": proc.returncode, **run_event_metadata(run_id)})
    elif silent_completion:
        await append_event(session_id, "error", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "message": "Codex exited without producing a reply. Retry the message; if this was a resumed external thread, create a fresh memory-backed continuation.",
            "exit_code": proc.returncode,
            **run_event_metadata(run_id),
        })
    if not stopped and (stream_error or idle_killed or resume_stalled or proc.returncode not in (0, None) or silent_completion):
        record_runtime_failure(BACKEND_CODEX, terminal_error or stream_error or f"exit {proc.returncode}")
    elif not stopped:
        record_runtime_success(BACKEND_CODEX)
    if provider_id:
        await STORE.save_provider_session(session_id, provider_id, BACKEND_CODEX)
    await collect_manifest(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts, final=True)
    await collect_recent_leftover_manifests(session_id, run_id, manifest_path, seen_artifacts=seen_artifacts)
    await publish_turn_code_diff(session_id, run_id, BACKEND_CODEX, cwd, diff_baseline, changed_paths)
    await append_turn_finished_event(session_id, {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "exit_code": proc.returncode,
        "result_text": clean_assistant_text("\n\n".join(text_parts).strip()),
        "stopped": stopped,
        **run_event_metadata(run_id),
    })
    RUN_METADATA.pop(run_id, None)
    await release_turn_slot(session_id)
    drain_queue = should_schedule_queue_after_finish(session_id, stopped)
    STOPPED_RUNS.discard(run_id)
    if drain_queue:
        schedule_next_queued_turn(session_id)


async def start_turn(
    session_id: str,
    req: TurnRequest,
    *,
    queue_if_busy: bool = True,
    queued_id: str | None = None,
    display_file_ids: list[str] | None = None,
) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    reserved = False
    should_queue = False
    async with ACTIVE_LOCK:
        if session_id in BUSY_SESSIONS:
            if queue_if_busy:
                should_queue = True
            else:
                raise HTTPException(status_code=409, detail="session already has a running turn")
        else:
            BUSY_SESSIONS.add(session_id)
            CURRENT_TURNS[session_id] = {
                "run_id": None,
                "prompt": req.prompt,
                "display_prompt": req.display_prompt,
                "file_ids": list(req.file_ids),
                "backend": req.backend or sess.get("backend") or DEFAULT_BACKEND,
                "purpose": req.purpose,
                "queued_id": queued_id,
            }
            reserved = True
    if should_queue:
        return await enqueue_turn(session_id, req, sess)

    blocker = await turn_start_blocker(ignore_session_id=session_id)
    if blocker:
        if reserved:
            await release_turn_slot(session_id)
            reserved = False
        raise HTTPException(status_code=503, detail=f"agent launch deferred: {blocker}")

    try:
        if req.backend:
            sess = await STORE.update(session_id, {"backend": req.backend})
        fields_set = getattr(req, "model_fields_set", getattr(req, "__fields_set__", set()))
        runtime_patch: dict[str, Any] = {}
        if "model" in fields_set and req.model is not None:
            runtime_patch["model"] = req.model
        if "effort" in fields_set and req.effort is not None:
            runtime_patch["effort"] = req.effort
        if runtime_patch:
            sess = await STORE.update(session_id, runtime_patch)

        backend = sess.get("backend") or DEFAULT_BACKEND
        await ensure_runtime_available(backend)

        run_id = f"run_{uuid.uuid4().hex[:16]}"
        async with ACTIVE_LOCK:
            current_turn = CURRENT_TURNS.get(session_id)
            if current_turn is not None:
                current_turn["run_id"] = run_id
                current_turn["backend"] = backend
        manifest_path = manifests_dir(session_id) / f"{run_id}.json"
        prompt = req.prompt
        attachment_lines = file_attachment_prompt_lines(req.file_ids)
        if attachment_lines:
            prompt += "\n\n[Attached files]\n"
            prompt += "\n".join(attachment_lines) + "\n"
            prompt += "Use these local paths directly when needed.\n"

        memory_seed = str(sess.get("memory_seed") or "").strip()
        if (
            backend == BACKEND_CODEX
            and memory_seed
            and (not sess.get("memory_seed_used") or not session_provider_id(sess))
        ):
            prompt = f"{memory_seed}\n\n[Current user prompt]\n{prompt}"
            async with STORE._lock:
                current = STORE.sessions.get(session_id)
                if current:
                    current["memory_seed_used"] = True
                    current["updated_at"] = now_iso()
                    sess = current
                    await STORE.save()
            await append_event(session_id, "history_imported", {
                "run_id": run_id,
                "backend": BACKEND_CODEX,
                "message": "Applied memory fork context to this first Codex turn.",
            })

        display_prompt = req.display_prompt if req.display_prompt is not None else req.prompt
        started_payload = {
            "run_id": run_id,
            "backend": backend,
            "prompt": display_prompt,
            "file_ids": display_file_ids if display_file_ids is not None else req.file_ids,
        }
        if queued_id:
            started_payload["queued_id"] = queued_id
        run_metadata = {
            "purpose": req.purpose,
            "job_id": req.job_id,
            "job_title": req.job_title,
            "digest_job_id": req.digest_job_id,
            "digest_detail": req.digest_detail,
            "source_session_id": req.source_session_id,
            "target_session_id": req.target_session_id,
            "steer_interrupted_run_id": req.steer_interrupted_run_id,
        }
        run_metadata = {key: value for key, value in run_metadata.items() if value is not None}
        if run_metadata:
            RUN_METADATA[run_id] = run_metadata
            started_payload.update(run_metadata)
        started_event = await append_event(session_id, "turn_started", started_payload)
        task = run_codex(session_id, run_id, prompt, dict(sess), manifest_path) if backend == BACKEND_CODEX else run_claude(session_id, run_id, prompt, dict(sess), manifest_path)
        asyncio.create_task(task)
        current_title = str(sess.get("title") or "").strip()
        if not current_title or current_title == "New chat":
            first_line = (req.prompt.strip().splitlines() or ["New chat"])[0]
            await STORE.update(session_id, {"title": first_line[:72] or "New chat"})
        else:
            await STORE.update(session_id, {})
        return {"run_id": run_id, "queued": False, "session": public_session(STORE.sessions[session_id]), "event": started_event}
    except Exception:
        if reserved:
            await release_turn_slot(session_id)
        raise


SERVER_UPDATE_ACTIVE_PHASES = {"starting", "checking", "downloading", "verifying", "installing", "restarting"}


def read_server_update_status() -> dict[str, Any]:
    try:
        value = json.loads(SERVER_UPDATE_STATUS_FILE.read_text())
        if not isinstance(value, dict):
            value = {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        value = {}
    value.setdefault("phase", "idle")
    value["current_version"] = SERVER_VERSION
    value["api_contract_version"] = API_CONTRACT_VERSION
    return value


def write_server_update_status(**changes: Any) -> dict[str, Any]:
    value = read_server_update_status()
    value.update(changes)
    value["updated_at"] = update_utc_now()
    atomic_update_json(SERVER_UPDATE_STATUS_FILE, value)
    return value


def server_update_tmux_name(update_id: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "", update_id)[:32]
    return f"agents_server_update_{clean or 'current'}"


def server_update_is_active(status: dict[str, Any]) -> bool:
    if str(status.get("phase") or "") not in SERVER_UPDATE_ACTIVE_PHASES:
        return False
    update_id = str(status.get("update_id") or "")
    if not update_id or shutil.which("tmux") is None:
        return False
    return run_tmux(["has-session", "-t", server_update_tmux_name(update_id)], check=False).returncode == 0


async def signed_release_manifest() -> dict[str, Any]:
    if not SERVER_UPDATE_PUBLIC_KEY.is_file():
        raise HTTPException(status_code=503, detail="release verification key is missing from this server installation")
    try:
        return await asyncio.to_thread(check_release, SERVER_UPDATE_PUBLIC_KEY)
    except ReleaseUnavailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"signed release check failed: {exc}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    await STORE.load()
    await JOBS.load()
    ensure_dirs()
    digest_job_count = await load_handoff_digest_jobs()
    rebuilt_queue_count = rebuild_queued_turns_from_events()
    JOBS.start_scheduler()
    scheduled_queue_drains = schedule_rebuilt_queued_turns()
    digest_recovery_task = asyncio.create_task(reconcile_handoff_digest_jobs())
    host_monitor_task = asyncio.create_task(host_monitor_loop())
    history_search_task = asyncio.create_task(history_search_index_loop())
    runtime_probe_task = asyncio.create_task(asyncio.to_thread(refresh_runtime_diagnostics, force=True))
    logger.info(
        "agent server ready state=%s sessions=%d jobs=%d digests=%d queued=%d queue_drains=%d",
        STATE_DIR,
        len(STORE.sessions),
        len(JOBS.jobs),
        digest_job_count,
        rebuilt_queue_count,
        scheduled_queue_drains,
    )
    try:
        yield
    finally:
        digest_recovery_task.cancel()
        host_monitor_task.cancel()
        history_search_task.cancel()
        runtime_probe_task.cancel()
        with suppress(asyncio.CancelledError):
            await digest_recovery_task
        with suppress(asyncio.CancelledError):
            await host_monitor_task
        with suppress(asyncio.CancelledError):
            await history_search_task
        with suppress(asyncio.CancelledError):
            await runtime_probe_task


app = FastAPI(title="AgentsServer", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def require_agent_token(request: Request, call_next):
    if request.method == "OPTIONS" or not request.url.path.startswith("/api/"):
        return await call_next(request)
    if not request_authorized(request):
        logger.warning("unauthorized request method=%s path=%s host=%s", request.method, request.url.path, request.client.host if request.client else "-")
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    async with ACTIVE_LOCK:
        active = sorted(BUSY_SESSIONS)
    async with QUEUE_LOCK:
        queued = {sid: len(queue) for sid, queue in QUEUED_TURNS.items() if queue}
    pressure = host_pressure_snapshot()
    tmux = tmux_capability()
    return {
        "ok": True,
        "server_version": SERVER_VERSION,
        "api_contract_version": API_CONTRACT_VERSION,
        "server_identity": server_identity(),
        "state_dir": str(STATE_DIR),
        "default_backend": DEFAULT_BACKEND,
        "default_cwd": existing_cwd(DEFAULT_CWD),
        "auth_required": bool(AGENT_TOKEN),
        "managed_updates": (
            SERVER_UPDATE_RUNNER.is_file()
            and SERVER_UPDATE_PUBLIC_KEY.is_file()
            and bool(tmux["available"])
        ),
        "capabilities": {"tmux": tmux},
        "websocket_runtime": True,
        "websocket_runtime_version": websockets.__version__,
        "active": active,
        "active_count": len(active),
        "queued": queued,
        "jobs": len(JOBS.jobs),
        "job_guard": pressure,
        "host_health_log": str(HOST_HEALTH_FILE),
        "runtimes": runtime_diagnostics_snapshot(),
    }


@app.get("/api/admin/update")
async def server_update_status() -> dict[str, Any]:
    status = read_server_update_status()
    if str(status.get("phase") or "") in SERVER_UPDATE_ACTIVE_PHASES and not await asyncio.to_thread(server_update_is_active, status):
        status = write_server_update_status(
            phase="failed",
            message="The detached updater exited before reporting completion. See server-update.log.",
            finished_at=update_utc_now(),
        )
    return status


@app.post("/api/admin/update/check")
async def check_server_update() -> dict[str, Any]:
    status = read_server_update_status()
    if await asyncio.to_thread(server_update_is_active, status):
        raise HTTPException(status_code=409, detail="a server update is already running")
    try:
        manifest = await signed_release_manifest()
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        return write_server_update_status(
            phase="unavailable",
            update_available=False,
            message=str(exc.detail),
            checked_at=update_utc_now(),
        )
    latest = str(manifest["version"])
    return write_server_update_status(
        phase="available" if latest != SERVER_VERSION else "current",
        latest_version=latest,
        update_available=latest != SERVER_VERSION,
        message=(f"AgentsServer {latest} is available." if latest != SERVER_VERSION else f"AgentsServer {SERVER_VERSION} is current."),
        checked_at=update_utc_now(),
    )


@app.post("/api/admin/update/start")
async def start_server_update(body: ServerUpdateRequest) -> dict[str, Any]:
    status = read_server_update_status()
    if await asyncio.to_thread(server_update_is_active, status):
        raise HTTPException(status_code=409, detail="a server update is already running")
    manifest = await signed_release_manifest()
    latest = str(manifest["version"])
    requested = str(body.version or latest).strip()
    if requested != latest:
        raise HTTPException(status_code=409, detail=f"the latest signed release is {latest}")
    if requested == SERVER_VERSION:
        return write_server_update_status(
            phase="current",
            latest_version=latest,
            update_available=False,
            message=f"AgentsServer {SERVER_VERSION} is already installed.",
            checked_at=update_utc_now(),
        )
    tmux = tmux_capability()
    if not tmux["available"]:
        raise HTTPException(status_code=503, detail=f"{tmux['message']} {tmux['action']}")
    if not SERVER_UPDATE_RUNNER.is_file() or not SERVER_UPDATE_PUBLIC_KEY.is_file():
        raise HTTPException(status_code=503, detail="this server installation predates managed updates; run the installer once")

    update_id = uuid.uuid4().hex
    tmux_name = server_update_tmux_name(update_id)
    command = [
        sys.executable,
        str(SERVER_UPDATE_RUNNER),
        "--status-file", str(SERVER_UPDATE_STATUS_FILE),
        "--public-key", str(SERVER_UPDATE_PUBLIC_KEY),
        "--port", str(SERVER_PORT),
        "--bind", SERVER_BIND_ADDRESS,
        "--expected-version", requested,
    ]
    status = write_server_update_status(
        update_id=update_id,
        phase="starting",
        target_version=requested,
        latest_version=latest,
        update_available=True,
        message=f"Starting detached update to AgentsServer {requested}.",
        started_at=update_utc_now(),
        finished_at=None,
    )
    try:
        await asyncio.to_thread(run_tmux, ["new-session", "-d", "-s", tmux_name, shlex.join(command)])
    except Exception as exc:
        write_server_update_status(phase="failed", message=f"Could not start detached updater: {exc}", finished_at=update_utc_now())
        raise HTTPException(status_code=500, detail="could not start detached updater") from exc
    return status


@app.get("/api/diagnostics/host")
async def host_diagnostics(limit: int = 40) -> dict[str, Any]:
    latest = await host_health_record()
    records = await asyncio.to_thread(tail_jsonl_file, HOST_HEALTH_FILE, limit)
    return {
        "latest": latest,
        "records": records,
        "log_path": str(HOST_HEALTH_FILE),
    }


@app.get("/api/runtime/catalog")
async def runtime_catalog(refresh: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(discover_runtime_catalog, force_runtime_probe=refresh)


@app.get("/api/sessions/{session_id}/terminal")
async def get_session_terminal(session_id: str, lines: int = 240) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_snapshot, session_id, lines=lines)


@app.post("/api/sessions/{session_id}/terminal/open")
async def open_session_terminal(session_id: str, req: TerminalOpenRequest) -> dict[str, Any]:
    return await asyncio.to_thread(ensure_terminal_session, session_id, req.cwd)


@app.post("/api/sessions/{session_id}/terminal/input")
async def input_session_terminal(session_id: str, req: TerminalInputRequest) -> dict[str, Any]:
    return await asyncio.to_thread(send_terminal_input, session_id, req.text, enter=req.enter, key=req.key)


@app.post("/api/sessions/{session_id}/terminal/resize")
async def resize_session_terminal(session_id: str, req: TerminalResizeRequest) -> dict[str, Any]:
    return await asyncio.to_thread(resize_terminal_pane, session_id, req.columns, req.rows)


@app.get("/api/sessions/{session_id}/terminal/windows")
async def get_session_terminal_windows(session_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_windows_snapshot, session_id)


@app.post("/api/sessions/{session_id}/terminal/action")
async def run_session_terminal_action(session_id: str, req: TerminalActionRequest) -> dict[str, Any]:
    return await asyncio.to_thread(terminal_action, session_id, req.action, req.target)


@app.delete("/api/sessions/{session_id}/terminal")
async def delete_session_terminal(session_id: str) -> dict[str, Any]:
    return await asyncio.to_thread(kill_terminal_session, session_id)


@app.get("/api/sessions/{session_id}/tmux")
async def get_session_tmux_panes(session_id: str, include_all: bool = False) -> dict[str, Any]:
    return await asyncio.to_thread(tmux_panes_snapshot, session_id, include_all=include_all)


@app.get("/api/sessions/{session_id}/tmux/capture")
async def capture_session_tmux_pane(session_id: str, pane_id: str, lines: int = 500) -> dict[str, Any]:
    return await asyncio.to_thread(capture_tmux_pane, session_id, pane_id, lines=lines)


@app.get("/api/sessions/{session_id}/processes")
async def get_session_processes(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    async with ACTIVE_LOCK:
        active = ACTIVE.get(session_id)
        snapshot_input = active_snapshot_input(active) if active else None
    if not active:
        return {
            "session_id": session_id,
            "active": False,
            "processes": [],
            "generated_at": now_iso(),
        }
    return await asyncio.to_thread(active_process_snapshot, session_id, snapshot_input)


@app.get("/api/sessions/{session_id}/processes/log")
async def tail_session_process_log(session_id: str, path: str, lines: int = 200) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    async with ACTIVE_LOCK:
        active = ACTIVE.get(session_id)
        snapshot_input = active_snapshot_input(active) if active else None
    if not active:
        raise HTTPException(status_code=404, detail="session has no live process")
    snapshot = await asyncio.to_thread(active_process_snapshot, session_id, snapshot_input)
    allowed: set[Path] = set()
    for proc in snapshot.get("processes", []):
        for hint in proc.get("log_hints", []) or []:
            with suppress(OSError):
                allowed.add(Path(str(hint.get("path") or "")).expanduser().resolve())
    with suppress(OSError):
        target = Path(path).expanduser().resolve()
        if target in allowed:
            return await asyncio.to_thread(tail_text_file, str(target), lines)
    raise HTTPException(status_code=403, detail="log path is not attached to the live process")


@app.get("/api/sessions")
async def list_sessions() -> dict[str, Any]:
    await STORE.ensure_sort_orders()
    sessions = [public_session(s) for s in sorted_sessions(list(STORE.sessions.values()))]
    return {"sessions": sessions}


@app.get("/api/search")
async def search_all_session_timelines(
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    return await asyncio.to_thread(search_all_timelines, q, limit)


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    sess = await STORE.create(req)
    provider_id = session_provider_id(sess)
    should_import = bool(provider_id) if req.import_history is None else req.import_history
    if should_import:
        await import_session_history(sess)
    return {"session": public_session(sess)}


@app.get("/api/sessions/{session_id}/timeline-index")
async def get_timeline_index(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    return await asyncio.to_thread(build_timeline_index, session_id)


@app.get("/api/sessions/{session_id}/search")
async def search_session_timeline(
    session_id: str,
    q: str = Query(min_length=2, max_length=500),
    limit: int = Query(default=40, ge=1, le=100),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    return await asyncio.to_thread(search_timeline_index, session_id, q, limit)


@app.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = DEFAULT_SESSION_EVENT_LIMIT,
    tail: bool = True,
    visible: bool = False,
    compact: bool = False,
) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    page_tail = tail and after <= 0
    if visible or compact:
        if after > 0 and before is None and not page_tail:
            events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
                read_visible_events_after_page,
                session_id,
                after=after,
                limit=limit,
                compact=compact,
            )
        else:
            events, latest_seq, event_count, omitted_before, omitted_after = await asyncio.to_thread(
                read_visible_events_page,
                session_id,
                after=after,
                before=before,
                limit=limit,
                tail=page_tail,
                compact=compact,
            )
    elif after > 0 and before is None:
        events = read_events(session_id, after=after, before=before, limit=limit, tail=page_tail)
        _, latest_seq, event_count = event_seq_bounds(session_id)
        omitted_before = 0
        if events:
            omitted_after = max(0, latest_seq - int(events[-1].get("seq", 0)))
        else:
            omitted_after = max(0, latest_seq - after)
    else:
        events = read_events(session_id, after=after, before=before, limit=limit, tail=page_tail)
        latest_seq = int(events[-1].get("seq", 0)) if events else 0
        event_count = 0
        omitted_before = max(0, int(events[0].get("seq", 1)) - 1) if page_tail and events else 0
        if events:
            omitted_after = max(0, latest_seq - int(events[-1].get("seq", 0)))
        elif after > 0:
            omitted_after = max(0, latest_seq - after)
        else:
            omitted_after = 0
    return {
        "session": public_session(sess),
        "events": events,
        "queued_turns": await queued_turns_snapshot(session_id),
        "events_omitted_before": omitted_before,
        "events_omitted_after": omitted_after,
        "latest_seq": latest_seq,
        "event_count": event_count,
    }


@app.post("/api/sessions/{session_id}/import-history")
async def import_history(session_id: str, req: ImportHistoryRequest) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    result = await import_session_history(sess, force=req.force, limit=req.limit)
    return {"ok": True, **result}


@app.post("/api/sessions/{session_id}/digest")
async def create_handoff_digest(session_id: str, req: HandoffDigestRequest) -> dict[str, Any]:
    return await build_handoff_digest(
        session_id,
        detail=req.detail,
        user_prompt=req.user_prompt,
        target_session_id=req.target_session_id,
        summarizer_backend=req.summarizer_backend,
        summarizer_model=req.summarizer_model,
        summarizer_effort=req.summarizer_effort,
    )


@app.post("/api/sessions/{session_id}/digest/send")
async def send_handoff_digest_background(session_id: str, req: HandoffDigestSendRequest) -> dict[str, Any]:
    source = STORE.sessions.get(session_id)
    if not source:
        raise HTTPException(status_code=404, detail="source session not found")
    target = STORE.sessions.get(req.target_session_id)
    if not target:
        raise HTTPException(status_code=404, detail="target session not found")
    if session_id == req.target_session_id:
        raise HTTPException(status_code=400, detail="target chat must be different from source chat")
    digest_job_id = f"digest_{uuid.uuid4().hex[:16]}"
    await create_handoff_digest_job(digest_job_id, session_id, req)
    started_event = await append_event(session_id, "handoff_digest_started", {
        "digest_job_id": digest_job_id,
        "source_session_id": session_id,
        "target_session_id": req.target_session_id,
        "message": f"Creating a context digest for {target.get('title') or req.target_session_id}.",
        "detail": req.detail,
        "user_prompt": req.user_prompt,
    })
    turn = await run_handoff_digest_send(digest_job_id, session_id, req)
    return {
        "ok": True,
        "digest_job_id": digest_job_id,
        "queued": bool(turn.get("queued")),
        "queued_id": turn.get("queued_id"),
        "run_id": turn.get("run_id"),
        "event": started_event,
        "session": public_session(STORE.sessions[session_id]),
    }


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest) -> dict[str, Any]:
    sess = await STORE.update(session_id, req.model_dump(exclude_unset=True))
    if req.archived is True:
        try:
            await asyncio.to_thread(kill_terminal_session, session_id)
        except Exception as exc:
            # Archiving is already durable at this point. Terminal cleanup is
            # best-effort and must not turn a successful archive into a 500.
            logger.warning("could not clean up terminal for archived session %s: %s", session_id, exc)
    return {"session": public_session(sess)}


@app.post("/api/sessions/{session_id}/order")
async def reorder_session(session_id: str, req: ReorderSessionRequest) -> dict[str, Any]:
    sessions = await STORE.reorder(session_id, req.direction, req.target_id, req.placement)
    return {"sessions": [public_session(sess) for sess in sessions]}


@app.post("/api/sessions/{session_id}/read")
async def mark_session_read(session_id: str, req: ReadSessionRequest) -> dict[str, Any]:
    sess = await STORE.mark_read(session_id, req.last_read_agent_event_seq)
    return {"session": public_session(sess)}


@app.post("/api/sessions/{session_id}/unread")
async def mark_session_unread(session_id: str) -> dict[str, Any]:
    sess = await STORE.mark_unread(session_id)
    return {"session": public_session(sess)}


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, Any]:
    async with ACTIVE_LOCK:
        active = ACTIVE.pop(session_id, None)
        BUSY_SESSIONS.discard(session_id)
    async with QUEUE_LOCK:
        QUEUED_TURNS.pop(session_id, None)
    if active:
        proc = active.get("proc")
        if proc:
            await terminate_process_tree(proc)
    with suppress(Exception):
        await asyncio.to_thread(kill_terminal_session, session_id)
    deleted = await STORE.delete(session_id)
    deleted_jobs = await JOBS.delete_for_session(session_id)
    return {"ok": True, "deleted": deleted, "deleted_jobs": deleted_jobs}


@app.post("/api/sessions/{session_id}/fork")
async def fork_session(session_id: str, req: ForkSessionRequest) -> dict[str, Any]:
    parent = STORE.sessions.get(session_id)
    if not parent:
        raise HTTPException(status_code=404, detail="session not found")
    parent_backend = (parent.get("backend") or DEFAULT_BACKEND).lower()
    parent_codex_thread_id = parent.get("codex_thread_id") or (
        parent.get("session_id") if parent_backend == BACKEND_CODEX else None
    )
    forked_codex_thread_id: str | None = None
    codex_fork_error: str | None = None

    if parent_backend == BACKEND_CODEX and parent_codex_thread_id:
        try:
            forked_codex_thread_id = await fork_codex_thread(str(parent_codex_thread_id), parent)
            logger.info(
                "forked codex thread parent_session=%s source_thread=%s forked_thread=%s",
                session_id,
                parent_codex_thread_id,
                forked_codex_thread_id,
            )
        except Exception as e:
            logger.warning(
                "codex fork failed parent_session=%s source_thread=%s: %s",
                session_id,
                parent_codex_thread_id,
                e,
            )
            codex_fork_error = str(e)

    child = await STORE.create(
        CreateSessionRequest(
            title=req.title or f"Fork of {parent.get('title') or session_id}",
            folder=parent.get("folder"),
            cwd=parent.get("cwd"),
            backend=parent_backend,
            model=parent.get("model"),
            effort=parent.get("effort"),
            system_prompt=parent.get("system_prompt"),
            pinned=bool(parent.get("pinned")),
            archived=bool(parent.get("archived")),
            provider_session_id=forked_codex_thread_id if parent_backend == BACKEND_CODEX else None,
            codex_thread_id=forked_codex_thread_id if parent_backend == BACKEND_CODEX else None,
        ),
        parent_id=session_id,
    )
    ordered_sessions = await STORE.reorder(child["id"], target_id=session_id, placement="after")
    child = STORE.sessions[child["id"]]
    if parent_backend == BACKEND_CODEX and codex_fork_error:
        child["memory_seed"] = build_fork_memory(parent, session_id, reason=codex_fork_error)
        child["memory_seed_used"] = False
        child["memory_forked"] = True
        child["memory_fork_reason"] = codex_fork_error[:2000]
        async with STORE._lock:
            STORE.sessions[child["id"]] = child
            await STORE.save()
    if parent_backend == BACKEND_CLAUDE and (parent.get("claude_session_id") or parent.get("session_id")):
        child["fork_from"] = parent.get("claude_session_id") or parent.get("session_id")
        async with STORE._lock:
            STORE.sessions[child["id"]] = child
            await STORE.save()
    copied = await copy_fork_history(session_id, child["id"])
    if forked_codex_thread_id:
        await append_event(
            child["id"],
            "provider_session",
            {
                "backend": BACKEND_CODEX,
                "provider_session_id": forked_codex_thread_id,
                "forked_from_provider_id": parent_codex_thread_id,
            },
        )
    elif codex_fork_error:
        await append_event(
            child["id"],
            "history_imported",
            {
                "backend": BACKEND_CODEX,
                "provider_session_id": parent_codex_thread_id,
                "message": "Codex provider fork was too large, so this is a memory fork with bounded rough history. The first turn will seed a fresh Codex thread with the memory dump.",
                "error": codex_fork_error[:4000],
                "copied_events": copied,
            },
        )
    return {"session": public_session(child), "sessions": [public_session(sess) for sess in ordered_sessions]}


@app.post("/api/sessions/{session_id}/turns")
async def post_turn(session_id: str, req: TurnRequest) -> dict[str, Any]:
    return await start_turn(session_id, req)


@app.delete("/api/sessions/{session_id}/queue/{queued_id}")
async def delete_queued_turn(session_id: str, queued_id: str) -> dict[str, Any]:
    return await unqueue_turn(session_id, queued_id)


@app.patch("/api/sessions/{session_id}/queue/{queued_id}")
async def patch_queued_turn(session_id: str, queued_id: str, req: UpdateQueuedTurnRequest) -> dict[str, Any]:
    return await update_queued_turn(session_id, queued_id, req)


@app.post("/api/sessions/{session_id}/queue/{queued_id}/move")
async def post_move_queued_turn(session_id: str, queued_id: str, req: MoveQueuedTurnRequest) -> dict[str, Any]:
    return await move_queued_turn(session_id, queued_id, req)


@app.post("/api/sessions/{session_id}/queue/{queued_id}/run-now")
async def post_run_queued_turn_now(session_id: str, queued_id: str) -> dict[str, Any]:
    return await run_queued_turn_now(session_id, queued_id)


@app.post("/api/sessions/{session_id}/stop")
async def stop_turn_endpoint(session_id: str) -> dict[str, Any]:
    return await stop_turn(session_id)


async def stop_turn(session_id: str, *, emit_event: bool = True, schedule_queue: bool = True) -> dict[str, Any]:
    async with ACTIVE_LOCK:
        active = ACTIVE.get(session_id)
        busy = session_id in BUSY_SESSIONS
        if active:
            active["stop_requested"] = True
            if active.get("run_id"):
                STOPPED_RUNS.add(str(active["run_id"]))
        elif busy:
            STOP_REQUESTS.add(session_id)
    if not active:
        if busy:
            if emit_event:
                await append_event(session_id, "turn_stopped", {
                    "run_id": None,
                    "message": "Stop requested before the agent process was ready.",
                })
            return {"ok": True, "stopped": True, "pending": True}
        if schedule_queue:
            schedule_next_queued_turn(session_id)
        return {"ok": True, "stopped": False}
    proc = active.get("proc") if active else None
    if proc:
        await terminate_process_tree(proc)
    if emit_event:
        await append_event(session_id, "turn_stopped", {
            "run_id": active.get("run_id") if active else None,
            "backend": active.get("backend") if active else None,
        })
    return {"ok": True, "stopped": True}


@app.get("/api/jobs")
async def list_jobs() -> dict[str, Any]:
    jobs = sorted(
        (public_job(j) for j in JOBS.jobs.values()),
        key=lambda j: j.get("updated_at") or "",
        reverse=True,
    )
    return {"jobs": jobs}


@app.post("/api/jobs")
async def create_job(req: CreateJobRequest) -> dict[str, Any]:
    job = await JOBS.create(req)
    return {"job": public_job(job)}


@app.patch("/api/jobs/{job_id}")
async def update_job(job_id: str, req: UpdateJobRequest) -> dict[str, Any]:
    job = await JOBS.update(job_id, req.model_dump(exclude_unset=True))
    return {"job": public_job(job)}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict[str, Any]:
    deleted = await JOBS.delete(job_id)
    return {"ok": True, "deleted": deleted}


@app.post("/api/jobs/{job_id}/run")
async def run_job(job_id: str) -> dict[str, Any]:
    return await JOBS.run_job(job_id)


@app.websocket("/api/sessions/{session_id}/terminal/ws")
async def session_terminal(
    session_id: str,
    ws: WebSocket,
    columns: int = 120,
    rows: int = 36,
    cwd: str | None = None,
) -> None:
    if not websocket_authorized(ws):
        await ws.accept()
        await ws.close(code=4401)
        return
    if session_id not in STORE.sessions:
        await ws.accept()
        await ws.close(code=4404)
        return
    if bool(STORE.sessions[session_id].get("archived")):
        await ws.accept()
        await ws.close(code=4409)
        return

    cols, lines = terminal_dimensions(columns, rows)
    await ws.accept()
    process: subprocess.Popen[bytes] | None = None
    master_fd: int | None = None
    try:
        process, master_fd, name = await asyncio.to_thread(
            spawn_terminal_client,
            session_id,
            cwd,
            cols,
            lines,
        )
        await ws.send_json({
            "type": "ready",
            "session_id": session_id,
            "name": name,
            "columns": cols,
            "rows": lines,
        })

        async def pump_output() -> None:
            assert master_fd is not None
            while True:
                try:
                    data = await read_terminal_output(master_fd)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        return
                    raise
                if not data:
                    return
                await ws.send_bytes(data)

        async def receive_input() -> None:
            assert master_fd is not None
            auto_scroll_mode = False
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data:
                    if auto_scroll_mode:
                        await asyncio.to_thread(exit_terminal_auto_scroll, session_id)
                        auto_scroll_mode = False
                    write_terminal_input(master_fd, data)
                    continue
                text = message.get("text")
                if not text:
                    continue
                try:
                    control = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if control.get("type") == "resize":
                    next_cols, next_rows = terminal_dimensions(control.get("columns"), control.get("rows"))
                    set_pty_dimensions(master_fd, next_cols, next_rows)
                    await asyncio.to_thread(resize_terminal_window, session_id, next_cols, next_rows)
                elif control.get("type") == "scroll":
                    with suppress(TypeError, ValueError):
                        auto_scroll_mode = await asyncio.to_thread(
                            scroll_terminal_history,
                            session_id,
                            int(control.get("delta") or 0),
                        )

        output_task = asyncio.create_task(pump_output())
        input_task = asyncio.create_task(receive_input())
        done, pending = await asyncio.wait(
            {output_task, input_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            with suppress(WebSocketDisconnect, RuntimeError, OSError):
                task.result()
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("terminal websocket failed session=%s error=%s", session_id, exc)
        with suppress(RuntimeError):
            await ws.send_json({"type": "error", "message": str(exc)[:1000]})
    finally:
        if process is not None and master_fd is not None:
            await asyncio.to_thread(stop_terminal_client, process, master_fd)
        with suppress(RuntimeError):
            await ws.close()


@app.websocket("/api/sessions/{session_id}/events")
async def session_events(session_id: str, ws: WebSocket, after: int = 0) -> None:
    if not websocket_authorized(ws):
        await ws.close(code=4401)
        return
    if session_id not in STORE.sessions:
        await ws.close(code=4404)
        return
    await HUB.subscribe(session_id, ws)
    try:
        for event in read_events(session_id, after=after):
            await ws.send_json(event)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await HUB.unsubscribe(session_id, ws)


@app.get("/api/sessions/{session_id}/diffs/{run_id}")
async def get_turn_code_diff(session_id: str, run_id: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or ""):
        raise HTTPException(status_code=404, detail="code diff not found")
    patch_path = code_diffs_dir(session_id) / f"{run_id}.patch"
    if not patch_path.is_file():
        raise HTTPException(status_code=404, detail="code diff not found")
    return FileResponse(
        patch_path,
        media_type="text/x-diff; charset=utf-8",
        filename=f"{run_id}.patch",
        content_disposition_type="inline",
    )


@app.post("/api/sessions/{session_id}/files")
async def upload_file(session_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    file_id = f"file_{uuid.uuid4().hex[:16]}"
    dest_dir = FILES_ROOT / file_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / safe_name(file.filename or "upload")
    size = 0
    with dest.open("wb") as out:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail="upload too large")
            out.write(chunk)
    meta = {
        "id": file_id,
        "session_id": session_id,
        "kind": "upload",
        "filename": dest.name,
        "path": str(dest),
        "size": size,
        "content_type": effective_content_type(dest.name, file.content_type),
        "created_at": now_iso(),
    }
    (dest_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    await append_event(session_id, "file_uploaded", {"file": meta})
    return {"file": meta}


def load_file_meta(file_id: str) -> dict[str, Any]:
    meta_path = FILES_ROOT / file_id / "meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    file_dir = FILES_ROOT / file_id
    if not file_dir.is_dir():
        raise HTTPException(status_code=404, detail="file not found")
    files = [p for p in file_dir.iterdir() if p.is_file() and p.name != "meta.json"]
    if len(files) != 1:
        raise HTTPException(status_code=404, detail="file not found")
    path = files[0]
    meta = {
        "id": file_id,
        "kind": "artifact",
        "filename": path.name,
        "path": str(path),
        "size": path.stat().st_size,
        "content_type": guess_content_type(path.name),
        "created_at": now_iso(),
    }
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


def iter_session_events(session_id: str):
    path = events_path(session_id)
    if not path.exists():
        return
    for line in path.open("r", encoding="utf-8", errors="ignore"):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except Exception:
            continue


def file_event_record(event: dict[str, Any], file_id: str) -> dict[str, Any] | None:
    for key in ("file", "artifact"):
        rec = event.get(key)
        if isinstance(rec, dict) and str(rec.get("id") or "") == file_id:
            out = dict(rec)
            out["event_id"] = event.get("id")
            out["event_seq"] = event.get("seq")
            out["event_type"] = event.get("type")
            return out
    return None


def list_session_file_records(session_id: str) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for meta_path in FILES_ROOT.glob("*/meta.json"):
        try:
            rec = json.loads(meta_path.read_text())
        except Exception:
            continue
        if rec.get("session_id") == session_id and rec.get("id"):
            records[str(rec["id"])] = rec

    for event in iter_session_events(session_id):
        for key in ("file", "artifact"):
            rec = event.get(key)
            if isinstance(rec, dict) and rec.get("id"):
                out = dict(rec)
                out["event_id"] = event.get("id")
                out["event_seq"] = event.get("seq")
                out["event_type"] = event.get("type")
                records[str(rec["id"])] = out

    normalized = []
    for rec in records.values():
        current = dict(rec)
        filename = str(current.get("filename") or Path(str(current.get("path") or "")).name)
        current["content_type"] = effective_content_type(filename, str(current.get("content_type") or ""))
        normalized.append(current)

    return sorted(
        normalized,
        key=lambda rec: (str(rec.get("created_at") or ""), str(rec.get("filename") or "")),
        reverse=True,
    )


@app.get("/api/sessions/{session_id}/files")
async def list_session_files(
    session_id: str,
    limit: int | None = Query(default=None, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    content_prefix: str | None = Query(default=None),
) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    records = list_session_file_records(session_id)
    if content_prefix:
        prefix = content_prefix.strip().lower()
        records = [
            rec for rec in records
            if str(rec.get("content_type") or "").lower().startswith(prefix)
        ]
    total = len(records)
    if limit is None:
        return {
            "files": records,
            "total": total,
            "offset": 0,
            "limit": total,
            "has_more": False,
        }
    page = records[offset:offset + limit]
    return {
        "files": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
    }


@app.get("/api/sessions/{session_id}/files/{file_id}/event")
async def get_session_file_event(session_id: str, file_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    clean_file_id = str(file_id or "").strip()
    if not clean_file_id:
        raise HTTPException(status_code=404, detail="file not found")
    for event in iter_session_events(session_id):
        if file_event_record(event, clean_file_id):
            return {"event": event}
    raise HTTPException(status_code=404, detail="file event not found")


@app.get("/api/sessions/{session_id}/links/file")
@app.head("/api/sessions/{session_id}/links/file")
async def get_session_linked_file(session_id: str, target: str) -> FileResponse:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    meta = session_file_for_link(session_id, target)
    return FileResponse(
        meta["path"],
        media_type=file_response_media_type(meta),
        filename=meta.get("filename"),
        content_disposition_type="inline",
    )


@app.get("/api/files/{file_id}")
@app.head("/api/files/{file_id}")
async def get_file(file_id: str) -> FileResponse:
    meta = load_file_meta(file_id)
    return FileResponse(
        meta["path"],
        media_type=file_response_media_type(meta),
        filename=meta.get("filename"),
        content_disposition_type="inline",
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="AgentsServer")
    parser.add_argument("cmd", nargs="?", default="serve", choices=["serve"])
    parser.add_argument("--bind", default=agentsdock_setting("AGENT_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(agentsdock_setting("AGENT_PORT", "7850")))
    args = parser.parse_args()
    uvicorn.run("agent_server:app", host=args.bind, port=args.port, app_dir=str(Path(__file__).parent), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
