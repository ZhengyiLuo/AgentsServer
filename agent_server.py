#!/usr/bin/env python3
"""Zenithbot Agent Server.

FastAPI service for a native Mac frontend. The server owns agent execution on
the agent host and streams normalized events from Claude Code / Codex CLI runs.

This intentionally mirrors the newest Slack bot's runner shape while removing
Slack-specific transport, formatting, and upload constraints.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
import uuid
from collections import deque
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

logger = logging.getLogger("zenithbot-agent")

BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
VALID_BACKENDS = {BACKEND_CLAUDE, BACKEND_CODEX}

STATE_DIR = Path(os.environ.get("ZENITHBOT_AGENT_DIR", Path.home() / ".zenithbot-agent"))
SESSIONS_FILE = STATE_DIR / "sessions.json"
JOBS_FILE = STATE_DIR / "jobs.json"
FILES_ROOT = STATE_DIR / "files"
HOST_HEALTH_FILE = STATE_DIR / "host_health.jsonl"
CLAUDE_PROJECTS_ROOT = Path(os.environ.get("CLAUDE_PROJECTS_ROOT", Path.home() / ".claude" / "projects"))
CODEX_SESSIONS_ROOT = Path(os.environ.get("CODEX_SESSIONS_ROOT", Path.home() / ".codex" / "sessions"))
CODEX_BIN = os.environ.get("CODEX_BIN", "codex")
DEFAULT_CWD = os.environ.get("ZENITHBOT_AGENT_CWD", str(Path.home()))
DEFAULT_BACKEND = os.environ.get("ZENITHBOT_BACKEND", BACKEND_CLAUDE).lower()
if DEFAULT_BACKEND not in VALID_BACKENDS:
    DEFAULT_BACKEND = BACKEND_CLAUDE

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("ZENITHBOT_REQUEST_TIMEOUT_SECONDS", "86400"))
CODEX_APP_SERVER_TIMEOUT_SECONDS = int(os.environ.get("ZENITHBOT_CODEX_APP_SERVER_TIMEOUT_SECONDS", "30"))
RUNTIME_CATALOG_TIMEOUT_SECONDS = float(os.environ.get("ZENITHBOT_RUNTIME_CATALOG_TIMEOUT_SECONDS", "6"))
JOB_SCHEDULER_INTERVAL_SECONDS = float(os.environ.get("ZENITHBOT_JOB_SCHEDULER_INTERVAL_SECONDS", "5"))
JOB_BUSY_RETRY_SECONDS = int(os.environ.get("ZENITHBOT_JOB_BUSY_RETRY_SECONDS", "60"))
JOB_MAX_ACTIVE_RUNS = int(os.environ.get("ZENITHBOT_JOB_MAX_ACTIVE_RUNS", "2"))
JOB_MAX_LOAD_PER_CPU = float(os.environ.get("ZENITHBOT_JOB_MAX_LOAD_PER_CPU", "1.25"))
JOB_MIN_AVAILABLE_MEM_MB = int(os.environ.get("ZENITHBOT_JOB_MIN_AVAILABLE_MEM_MB", "4096"))
JOB_DEFER_EVENT_MIN_SECONDS = int(os.environ.get("ZENITHBOT_JOB_DEFER_EVENT_MIN_SECONDS", "300"))
MAX_ACTIVE_AGENT_RUNS = int(os.environ.get("ZENITHBOT_MAX_ACTIVE_AGENT_RUNS", "10"))
MAX_START_LOAD_PER_CPU = float(os.environ.get("ZENITHBOT_MAX_START_LOAD_PER_CPU", "2.0"))
MIN_START_AVAILABLE_MEM_MB = int(os.environ.get("ZENITHBOT_MIN_START_AVAILABLE_MEM_MB", "2048"))
HOST_MONITOR_INTERVAL_SECONDS = float(os.environ.get("ZENITHBOT_HOST_MONITOR_INTERVAL_SECONDS", "15"))
HOST_HEALTH_MAX_BYTES = int(os.environ.get("ZENITHBOT_HOST_HEALTH_MAX_BYTES", str(20 * 1024 * 1024)))
IDLE_WARN_SECONDS = int(os.environ.get("ZENITHBOT_IDLE_WARN_SECONDS", "1800"))
IDLE_KILL_SECONDS = int(os.environ.get("ZENITHBOT_IDLE_KILL_SECONDS", "21600"))
STOP_GRACE_SECONDS = float(os.environ.get("ZENITHBOT_STOP_GRACE_SECONDS", "2.0"))
PROCESS_STREAM_LIMIT = int(os.environ.get("ZENITHBOT_PROCESS_STREAM_LIMIT", str(16 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(os.environ.get("ZENITHBOT_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024 * 1024)))
MAX_IMPORT_MESSAGES = int(os.environ.get("ZENITHBOT_HISTORY_IMPORT_LIMIT", "400"))
MAX_IMPORTED_TEXT_CHARS = int(os.environ.get("ZENITHBOT_HISTORY_IMPORT_TEXT_CHARS", "12000"))
MAX_FORK_MEMORY_CHARS = int(os.environ.get("ZENITHBOT_FORK_MEMORY_CHARS", "24000"))
MAX_FORK_MEMORY_ITEM_CHARS = int(os.environ.get("ZENITHBOT_FORK_MEMORY_ITEM_CHARS", "1800"))
MAX_HANDOFF_DIGEST_CHARS = int(os.environ.get("ZENITHBOT_HANDOFF_DIGEST_CHARS", "56000"))
DEFAULT_SESSION_EVENT_LIMIT = int(os.environ.get("ZENITHBOT_SESSION_EVENT_LIMIT", "100"))
MAX_EVENT_RESPONSE_LIMIT = int(os.environ.get("ZENITHBOT_MAX_EVENT_RESPONSE_LIMIT", "1000"))
AGENT_TOKEN = os.environ.get("ZENITHDOCK_AGENT_TOKEN") or os.environ.get("ZENITHBOT_AGENT_TOKEN") or ""
API_CONTRACT_VERSION = 3
SESSION_ORDER_STEP = 1000.0

SYSTEM_PROMPT = """\
You are responding through Zenith Dock, a native Mac frontend for Zenithbot.

Use concise Markdown. Prefer clear sections, bullets, code fences, and direct
answers. The UI renders rich traces separately, so do not narrate every tool
call unless it matters to the user.
Do not use emoji, Slack-style emoji aliases, or decorative status prefixes
such as :mag:, :gear:, :rocket:, or :white_check_mark:.

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
"""

CODEX_PROMPT_PRELUDE = """\
[Zenith Dock context]
You are responding through a native Mac frontend for Zenithbot.

Use concise Markdown. The UI renders tool calls, command output, reasoning
summaries, and artifacts separately, so keep the final answer focused.
Do not use emoji, Slack-style emoji aliases, or decorative status prefixes
such as :mag:, :gear:, :rocket:, or :white_check_mark:.

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

This is Zenith Dock, not Slack. Do not call Slack upload APIs or Slack file
helpers. Create files locally on the agent host and publish them through the manifest.

If you create files the user should receive, write a JSON manifest at exactly:
{manifest_path}

Manifest format:
{{"files": ["/absolute/path/to/file.ext", {{"path": "/absolute/path/video.mp4", "title": "Demo", "text": "Optional note"}}]}}

Use absolute file paths. Videos should be normal playable files such as mp4 or
mov. If `python` is not installed, use `python3` or shell tools to write files
and the manifest.

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
    if session_id:
        session_dir(session_id).mkdir(parents=True, exist_ok=True)
        uploads_dir(session_id).mkdir(parents=True, exist_ok=True)
        manifests_dir(session_id).mkdir(parents=True, exist_ok=True)


EVENT_SEQ_CACHE: dict[str, int] = {}
EVENT_SEQ_LOCK = asyncio.Lock()


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
        or token_matches(request.headers.get("x-zenithdock-token"))
        or token_matches(request.query_params.get("token"))
    )


def websocket_authorized(ws: WebSocket) -> bool:
    if not AGENT_TOKEN:
        return True
    return (
        token_matches(bearer_token(ws.headers.get("authorization")))
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
    pinned: bool | None = None
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
    pinned: bool | None = None
    archived: bool | None = None


class ReorderSessionRequest(BaseModel):
    direction: str


class ReadSessionRequest(BaseModel):
    last_read_agent_event_seq: int | None = None


class TurnRequest(BaseModel):
    prompt: str
    file_ids: list[str] = Field(default_factory=list)
    backend: str | None = None
    model: str | None = None
    effort: str | None = None


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


class TerminalOpenRequest(BaseModel):
    cwd: str | None = None


class TerminalInputRequest(BaseModel):
    text: str | None = None
    enter: bool = True
    key: str | None = None


class TerminalResizeRequest(BaseModel):
    columns: int = 100
    rows: int = 30


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


class UpdateJobRequest(BaseModel):
    title: str | None = None
    prompt: str | None = None
    interval_seconds: int | None = None
    next_run_at: str | None = None
    loop: bool | None = None
    max_runs: int | None = None
    enabled: bool | None = None
    backend: str | None = None


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
        if SESSIONS_FILE.exists():
            try:
                self.sessions = json.loads(SESSIONS_FILE.read_text())
            except Exception as e:
                logger.warning("failed to load sessions: %s", e)
                self.sessions = {}
        await self.ensure_sort_orders()

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
        sess = {
            "id": sid,
            "title": title,
            "folder": req.folder or "General",
            "cwd": req.cwd or DEFAULT_CWD,
            "backend": backend,
            "model": req.model,
            "effort": req.effort,
            "session_id": active_provider_id,
            "claude_session_id": claude_session_id,
            "codex_thread_id": codex_thread_id,
            "parent_id": parent_id,
            "fork_from": None,
            "pinned": bool(req.pinned),
            "pinned_at": now if req.pinned else None,
            "archived": False,
            "archived_at": None,
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
                    await append_event(sid, "backend_changed", {"old": old, "new": backend})
            for key in ("title", "folder", "cwd"):
                if key in patch and patch[key] is not None:
                    sess[key] = patch[key]
            for key in ("model", "effort"):
                if key in patch:
                    value = patch[key]
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
            return sess

    async def reorder(self, sid: str, direction: str) -> list[dict[str, Any]]:
        normalized = direction.strip().lower()
        if normalized not in {"up", "down"}:
            raise HTTPException(status_code=400, detail="direction must be up or down")
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                raise HTTPException(status_code=404, detail="session not found")
            section = session_section_key(sess)
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
        return existed is not None

    async def save_provider_session(self, sid: str, provider_id: str, backend: str) -> None:
        async with self._lock:
            sess = self.sessions.get(sid)
            if not sess:
                return
            sess["session_id"] = provider_id
            sess["backend"] = sess.get("backend") or backend
            sess["claude_session_id" if backend == BACKEND_CLAUDE else "codex_thread_id"] = provider_id
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
STOP_REQUESTS: set[str] = set()
STOPPED_RUNS: set[str] = set()
ACTIVE_LOCK = asyncio.Lock()
QUEUED_TURNS: dict[str, deque[dict[str, Any]]] = {}
RUN_NOW_TURNS: dict[str, dict[str, Any]] = {}
QUEUE_LOCK = asyncio.Lock()

LOG_PATH_SUFFIXES = {
    ".log", ".out", ".err", ".stderr", ".stdout", ".txt", ".jsonl", ".trace"
}
LIVE_STDOUT_MAX_LINES = 400
LIVE_STDOUT_MAX_LINE_CHARS = 12_000
TMUX_CAPTURE_MAX_LINES = 2_000
TMUX_COMMAND_TIMEOUT_SECONDS = 4


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
    await update_session_event_metadata(session_id, event)
    await HUB.broadcast(session_id, event)
    return event


def is_agent_visible_event(event_type: str, event: dict[str, Any]) -> bool:
    if event_type == "assistant_text":
        return bool(str(event.get("text") or "").strip())
    if event_type == "turn_finished":
        return bool(str(event.get("result_text") or "").strip())
    return event_type in {"error", "artifact_created", "job_ran", "job_error"}


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
        "created_at": now_iso(),
    }
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.setdefault(session_id, deque())
        queue.append(item)
        position = len(queue)
    await append_event(session_id, "turn_queued", {
        "queued_id": queued_id,
        "backend": req.backend or sess.get("backend") or DEFAULT_BACKEND,
        "prompt": req.prompt,
        "file_ids": list(req.file_ids),
        "position": position,
    })
    return {
        "queued": True,
        "queued_id": queued_id,
        "position": position,
        "session": public_session(STORE.sessions[session_id]),
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
    })
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

    selected: dict[str, Any] | None = None
    remaining: int
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.get(session_id)
        if queue:
            kept: deque[dict[str, Any]] = deque()
            for item in queue:
                if selected is None and item.get("queued_id") == queued_id:
                    selected = item
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
        if selected is not None:
            RUN_NOW_TURNS[session_id] = selected

    if selected is None:
        raise HTTPException(status_code=404, detail="queued turn not found")

    await append_event(session_id, "turn_queue_run_now", {
        "queued_id": queued_id,
        "backend": selected.get("backend") or STORE.sessions[session_id].get("backend") or DEFAULT_BACKEND,
        "prompt": selected.get("prompt") or "",
        "file_ids": list(selected.get("file_ids") or []),
        "message": "Queued message moved to the front and current turn interrupted.",
        "remaining": remaining,
    })
    stop_result = await stop_turn(session_id, emit_event=False, schedule_queue=False)
    if not stop_result.get("stopped") and not stop_result.get("pending"):
        schedule_next_queued_turn(session_id)
    return {"ok": True, "queued_id": queued_id, "interrupted": bool(stop_result.get("stopped"))}


async def requeue_turn_front(session_id: str, item: dict[str, Any]) -> None:
    async with QUEUE_LOCK:
        queue = QUEUED_TURNS.setdefault(session_id, deque())
        queue.appendleft(item)


async def retry_next_queued_turn_later(session_id: str, delay_seconds: int | None = None) -> None:
    await asyncio.sleep(max(int(delay_seconds or JOB_BUSY_RETRY_SECONDS), 5))
    await start_next_queued_turn(session_id)


async def start_next_queued_turn(session_id: str) -> None:
    async with QUEUE_LOCK:
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
    )
    try:
        await start_turn(session_id, req, queue_if_busy=False, queued_id=str(item["queued_id"]))
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
    except Exception as e:
        logger.warning("queued turn failed session=%s queued_id=%s: %s", session_id, item.get("queued_id"), e)
        await append_event(session_id, "error", {
            "queued_id": item.get("queued_id"),
            "message": f"queued turn failed: {e}",
        })


def schedule_next_queued_turn(session_id: str) -> None:
    asyncio.create_task(start_next_queued_turn(session_id))


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
            if event_type == "turn_queued" and queued_id:
                pending[queued_id] = {
                    "queued_id": queued_id,
                    "prompt": event.get("prompt") or "",
                    "file_ids": list(event.get("file_ids") or []),
                    "backend": event.get("backend") or sess.get("backend"),
                    "model": sess.get("model"),
                    "effort": sess.get("effort"),
                    "created_at": event.get("ts") or now_iso(),
                    "position": int(event.get("position") or (len(order) + 1)),
                }
                if queued_id not in order:
                    order.append(queued_id)
            elif event_type in {"turn_queue_updated", "turn_queue_run_now"} and queued_id in pending:
                if event.get("prompt") is not None:
                    pending[queued_id]["prompt"] = event.get("prompt") or ""
                if event.get("file_ids") is not None:
                    pending[queued_id]["file_ids"] = list(event.get("file_ids") or [])
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
        items = [pending[qid] for qid in order if qid in pending and str(pending[qid].get("prompt") or "").strip()]
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


def ensure_terminal_session(session_id: str, cwd: str | None = None) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
    created = False
    if not tmux_session_exists(name):
        workdir = existing_cwd(cwd or sess.get("cwd") or DEFAULT_CWD)
        run_tmux(["new-session", "-d", "-s", name, "-c", workdir])
        created = True
    return terminal_snapshot(session_id, created=created)


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
    cols = max(40, min(int(columns or 100), 300))
    line_count = max(10, min(int(rows or 30), 120))
    run_tmux(["resize-pane", "-t", name, "-x", str(cols), "-y", str(line_count)], check=False)
    return terminal_snapshot(session_id, lines=line_count)


def kill_terminal_session(session_id: str) -> dict[str, Any]:
    if session_id not in STORE.sessions:
        raise HTTPException(status_code=404, detail="session not found")
    name = terminal_session_name(session_id)
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
    "tail", "this", "true", "wandb", "zenithbot", "zenithdock",
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
        clean["tracked_by_zenithdock"] = int(clean.get("pgid") or -1) in active_pgids
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


async def release_turn_slot(session_id: str) -> None:
    async with ACTIVE_LOCK:
        ACTIVE.pop(session_id, None)
        BUSY_SESSIONS.discard(session_id)
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
) -> list[dict[str, Any]]:
    path = events_path(session_id)
    if not path.exists():
        return []
    limit = max(1, min(int(limit or 500), MAX_EVENT_RESPONSE_LIMIT))
    out: list[dict[str, Any]] = []
    tail_out: deque[dict[str, Any]] | None = deque(maxlen=limit) if tail else None
    for line in path.open("r", encoding="utf-8", errors="ignore"):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        seq = int(event.get("seq", 0))
        if seq > after and (before is None or seq < before):
            if tail_out is not None:
                tail_out.append(event)
            else:
                out.append(event)
        if tail_out is None and len(out) >= limit:
            break
    return list(tail_out) if tail_out is not None else out


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


def build_handoff_digest(session_id: str, detail: str = "normal", user_prompt: str | None = None) -> dict[str, Any]:
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
        "# ZenithDock Context Digest",
        "",
        "This is an explicit handoff from another ZenithDock chat. Use it as background context for the next response.",
    ]
    if prompt:
        lines.extend(["", "## User Prompt For Target Agent", prompt])

    lines.extend([
        "",
        "## Source Chat",
        f"- Title: {source.get('title') or 'Untitled'}",
        f"- ZenithDock session: {session_id}",
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
        "digest": digest,
        "source_session": public_session(source),
        "event_count": len(events),
        "file_count": len(files),
        "detail": str(detail or "normal").strip().lower() or "normal",
    }


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


def build_fork_memory(parent: dict[str, Any], parent_id: str, *, reason: str | None = None) -> str:
    provider_id = session_provider_id(parent)
    header = [
        "[ZenithDock memory fork]",
        "This is a fresh provider thread seeded from a compact memory dump because the original provider-level fork was unavailable.",
        "Use this memory as background context. Do not treat it as a new user request.",
        "",
        f"Parent ZenithDock session: {parent_id}",
        f"Parent title: {parent.get('title') or 'Untitled'}",
        f"Backend: {parent.get('backend') or DEFAULT_BACKEND}",
        f"Working directory: {parent.get('cwd') or DEFAULT_CWD}",
    ]
    if provider_id:
        header.append(f"Original provider session/thread: {provider_id}")
    if reason:
        header.append(f"Fork fallback reason: {compact_memory_text(reason, 800)}")

    lines: list[str] = header + ["", "Recent rough conversation:"]
    events = read_events(parent_id, limit=160, tail=True)
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
        memory = "[ZenithDock memory fork]\n[Older memory trimmed]\n" + memory
    return memory


def public_session(sess: dict[str, Any]) -> dict[str, Any]:
    return {
        k: sess.get(k)
        for k in (
            "id", "title", "folder", "cwd", "backend", "model", "effort",
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
        "job_max_load_per_cpu": JOB_MAX_LOAD_PER_CPU,
        "job_min_available_mem_mb": JOB_MIN_AVAILABLE_MEM_MB,
        "job_max_active_runs": JOB_MAX_ACTIVE_RUNS,
        "start_max_load_per_cpu": MAX_START_LOAD_PER_CPU,
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
    load_per_cpu = pressure.get("load_per_cpu")
    if isinstance(load_per_cpu, (int, float)) and load_per_cpu >= JOB_MAX_LOAD_PER_CPU:
        return f"host load high ({load_per_cpu:.2f}/CPU)"

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
    load_per_cpu = pressure.get("load_per_cpu")
    if isinstance(load_per_cpu, (int, float)) and load_per_cpu >= MAX_START_LOAD_PER_CPU:
        return f"host load high ({load_per_cpu:.2f}/CPU)"

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


def runtime_option(value: str, label: str | None = None) -> dict[str, str]:
    clean = str(value or "").strip()
    return {"value": clean, "label": str(label or clean or "Server default").strip()}


def server_default_runtime_option(label: str | None = None) -> dict[str, str]:
    return runtime_option("", label or "Server default")


def title_model_label(value: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        return "Server default"
    known = {
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


def unique_runtime_options(options: list[dict[str, str]], default_label: str | None = None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for option in [server_default_runtime_option(default_label), *options]:
        value = str(option.get("value") or "").strip()
        if value in seen:
            continue
        seen.add(value)
        out.append(runtime_option(value, option.get("label") or None))
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


def runtime_priority(model: dict[str, Any]) -> int:
    try:
        return int(model["priority"])
    except Exception:
        return 9999


def session_backend_locked(sess: dict[str, Any]) -> bool:
    return any(str(sess.get(key) or "").strip() for key in ("session_id", "claude_session_id", "codex_thread_id"))


def discover_codex_catalog() -> dict[str, Any]:
    models: list[dict[str, Any]] = []
    model_options: list[dict[str, str]] = []
    effort_options: list[dict[str, str]] = []
    default_model = ""
    default_model_label = ""
    default_effort = ""
    default_effort_label = ""
    model_source = "codex debug models"
    effort_source = "codex debug models"
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
    for model in visible_models:
        slug = str(model.get("slug") or model.get("id") or "").strip()
        if not slug:
            continue
        label = str(model.get("display_name") or title_model_label(slug)).strip()
        model_options.append(runtime_option(slug, label))
        if not default_model:
            default_model = slug
            default_model_label = label
            default_effort = str(model.get("default_reasoning_level") or "").strip()
            default_effort_label = title_effort_label(default_effort) if default_effort else ""
        levels = model.get("supported_reasoning_levels")
        if isinstance(levels, list):
            for level in levels:
                if not isinstance(level, dict):
                    continue
                effort = str(level.get("effort") or "").strip()
                if effort:
                    effort_options.append(runtime_option(effort, title_effort_label(effort)))

    return {
        "models": unique_runtime_options(model_options, f"Server default ({default_model_label})" if default_model_label else None),
        "efforts": unique_runtime_options(effort_options, f"Server default ({default_effort_label})" if default_effort_label else None),
        "model_source": model_source,
        "effort_source": effort_source,
        "default_model": default_model or None,
        "default_effort": default_effort or None,
    }


def parse_claude_help_catalog() -> dict[str, Any]:
    model_options: list[dict[str, str]] = []
    effort_options: list[dict[str, str]] = []
    model_source = "claude --help"
    effort_source = "claude --help"
    default_model = (
        os.environ.get("CLAUDE_MODEL")
        or os.environ.get("ANTHROPIC_MODEL")
        or os.environ.get("ZENITHBOT_CLAUDE_MODEL")
        or "sonnet"
    )
    default_effort = os.environ.get("CLAUDE_EFFORT") or os.environ.get("ZENITHBOT_CLAUDE_EFFORT") or ""
    try:
        help_text = run_catalog_command(["claude", "--help"])
    except Exception as exc:
        logger.warning("claude model discovery failed: %s", exc)
        return {
            "models": unique_runtime_options(
                [
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
        ("sonnet", "Sonnet"),
        ("opus", "Opus"),
        ("opus[1m]", "Opus 1M"),
        ("claude-opus-4-8", "Opus 4.8"),
        ("claude-opus-4-8[1m]", "Opus 4.8 1M"),
        ("haiku", "Haiku"),
    ):
        model_options.append(runtime_option(alias, label))

    effort_match = re.search(r"--effort <level>.*?\(([^)]+)\)", help_text, re.IGNORECASE)
    if effort_match:
        for effort in re.split(r"[,/\s]+", effort_match.group(1)):
            clean = effort.strip()
            if clean:
                effort_options.append(runtime_option(clean, title_effort_label(clean)))

    return {
        "models": unique_runtime_options(model_options, title_model_label(default_model)),
        "efforts": unique_runtime_options(effort_options, title_effort_label(default_effort) if default_effort else ""),
        "model_source": model_source,
        "effort_source": effort_source,
        "default_model": default_model,
        "default_effort": default_effort or None,
    }


def discover_runtime_catalog() -> dict[str, Any]:
    return {
        "generated_at": now_iso(),
        "backends": {
            BACKEND_CLAUDE: parse_claude_help_catalog(),
            BACKEND_CODEX: discover_codex_catalog(),
        },
    }


def build_claude_cmd(sess: dict[str, Any], manifest_path: Path) -> list[str]:
    cmd = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--append-system-prompt", SYSTEM_PROMPT.format(manifest_path=str(manifest_path)),
        "--disallowedTools", "AskUserQuestion", "EnterPlanMode", "ExitPlanMode",
    ]
    if sess.get("model"):
        cmd.extend(["--model", str(sess["model"])])
    if sess.get("effort"):
        cmd.extend(["--effort", str(sess["effort"])])
    provider_id = sess.get("claude_session_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CLAUDE else None
    )
    if sess.get("fork_from"):
        provider_id = sess["fork_from"]
    if provider_id:
        cmd.extend(["--resume", provider_id])
    if sess.get("fork_from"):
        cmd.append("--fork-session")
        cmd.extend(["--name", f"Fork: {sess.get('title') or sess['id']}"])
    return cmd


def build_codex_cmd(sess: dict[str, Any], prompt: str, manifest_path: Path) -> list[str]:
    provider_id = sess.get("codex_thread_id") or (
        sess.get("session_id") if sess.get("backend") == BACKEND_CODEX else None
    )
    full_prompt = CODEX_PROMPT_PRELUDE.format(manifest_path=str(manifest_path)) + prompt
    cmd = [CODEX_BIN, "exec"]
    if sess.get("model"):
        cmd.extend(["--model", str(sess["model"])])
    if sess.get("effort"):
        cmd.extend(["-c", f"model_reasoning_effort={sess['effort']}"])
    if provider_id:
        cmd.extend(["resume", provider_id])
    cmd.append("--json")
    cmd.extend(["-c", "model_reasoning_summary=detailed"])
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
            return "; ".join(str(item) for item in errors if item)
        result = event.get("result")
        return str(result or "Claude execution failed")
    return None


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
            "clientInfo": {"name": "zenithdock-agent-server", "version": "0"},
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


def file_response_media_type(meta: dict[str, Any]) -> str:
    filename = str(meta.get("filename") or Path(str(meta.get("path") or "")).name)
    guessed = guess_content_type(filename)
    recorded = str(meta.get("content_type") or "")
    if not recorded or recorded == "application/octet-stream":
        return guessed
    return recorded


async def collect_manifest(session_id: str, run_id: str, manifest_path: Path) -> None:
    if not manifest_path.exists():
        return
    try:
        data = json.loads(manifest_path.read_text())
    except Exception as e:
        await append_event(session_id, "artifact_error", {"run_id": run_id, "error": f"manifest parse failed: {e}"})
        return
    finally:
        with suppress(OSError):
            manifest_path.unlink()
    seen: set[str] = set()
    for entry in data.get("files", []):
        path = entry if isinstance(entry, str) else entry.get("path", "") if isinstance(entry, dict) else ""
        if not path or path in seen:
            continue
        seen.add(path)
        rec = artifact_record(session_id, entry)
        if rec:
            await append_event(session_id, "artifact_created", {"run_id": run_id, "artifact": rec})
        else:
            await append_event(session_id, "artifact_error", {"run_id": run_id, "path": path, "error": "file not found"})


async def run_claude(session_id: str, run_id: str, prompt: str, sess: dict[str, Any], manifest_path: Path) -> None:
    cmd = build_claude_cmd(sess, manifest_path)
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    await append_event(session_id, "process_started", {"run_id": run_id, "backend": BACKEND_CLAUDE, "argv": cmd, "cwd": cwd})
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=runner_env(),
            limit=PROCESS_STREAM_LIMIT,
            start_new_session=True,
        )
    except Exception as e:
        await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": f"failed to start Claude: {e}"})
        await append_event(session_id, "turn_finished", {
            "run_id": run_id,
            "backend": BACKEND_CLAUDE,
            "exit_code": None,
            "result_text": "",
        })
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
    last_event = time.time()
    idle_killed = False
    stream_error: str | None = None
    result_error: str | None = None

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
                            await append_event(session_id, "assistant_text", {"run_id": run_id, "text": text})
                    elif btype == "thinking" and block.get("thinking"):
                        await append_event(session_id, "reasoning_summary", {"run_id": run_id, "text": block["thinking"]})
                    elif btype in ("tool_use", "server_tool_use"):
                        tid = block.get("id") or f"tool_{uuid.uuid4().hex[:8]}"
                        tool = {"id": tid, "name": block.get("name", "tool"), "input": block.get("input", {})}
                        current_tools[tid] = tool
                        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})
            elif etype == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        content = block.get("content", "")
                        await append_event(session_id, "tool_finished", {
                            "run_id": run_id,
                            "tool_id": tid,
                            "tool": current_tools.pop(tid, None),
                            "output": content,
                            "is_error": block.get("is_error") is True,
                        })
            elif etype == "result":
                result_error = claude_result_error(event)
                if result_error:
                    provider_id = None
                    await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CLAUDE, "message": result_error})
                    continue
                final_text = event.get("result", "") or final_text
                if event.get("session_id"):
                    provider_id = event["session_id"]
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Claude run failed session=%s run=%s", session_id, run_id)
    finally:
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in STOPPED_RUNS
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Claude stream failed: {stream_error}"})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout"})
    if not stopped and proc.returncode not in (0, None) and stderr:
        await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode})
    if provider_id and not result_error:
        await STORE.save_provider_session(session_id, provider_id, BACKEND_CLAUDE)
        await append_event(session_id, "provider_session", {"run_id": run_id, "backend": BACKEND_CLAUDE, "provider_session_id": provider_id})
    result_text = clean_assistant_text(final_text or "\n\n".join(text_parts).strip())
    await collect_manifest(session_id, run_id, manifest_path)
    await append_event(session_id, "turn_finished", {
        "run_id": run_id,
        "backend": BACKEND_CLAUDE,
        "exit_code": proc.returncode,
        "result_text": result_text,
        "stopped": stopped,
    })
    await release_turn_slot(session_id)
    STOPPED_RUNS.discard(run_id)
    schedule_next_queued_turn(session_id)


async def run_codex(session_id: str, run_id: str, prompt: str, sess: dict[str, Any], manifest_path: Path) -> None:
    cmd = build_codex_cmd(sess, prompt, manifest_path)
    requested_cwd = str(sess.get("cwd") or DEFAULT_CWD)
    cwd = existing_cwd(requested_cwd)
    if str(Path(requested_cwd).expanduser()) != cwd:
        await append_event(session_id, "cwd_fallback", {"run_id": run_id, "requested_cwd": requested_cwd, "cwd": cwd})
    await append_event(session_id, "process_started", {"run_id": run_id, "backend": BACKEND_CODEX, "argv": cmd[:-1] + ["<prompt>"], "cwd": cwd})
    env = runner_env()
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
        await append_event(session_id, "error", {"run_id": run_id, "backend": BACKEND_CODEX, "message": f"failed to start Codex: {e}"})
        await append_event(session_id, "turn_finished", {
            "run_id": run_id,
            "backend": BACKEND_CODEX,
            "exit_code": None,
            "result_text": "",
        })
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
    provider_id: str | None = None
    last_event = time.time()
    idle_killed = False
    stream_error: str | None = None

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
            if not line or not line.startswith("{"):
                continue
            await append_event(session_id, "raw_event", {"run_id": run_id, "backend": BACKEND_CODEX, "raw": line})
            try:
                event = json.loads(line)
            except Exception:
                continue
            etype = event.get("type", "")
            if etype == "thread.started" and event.get("thread_id"):
                provider_id = event["thread_id"]
                await STORE.save_provider_session(session_id, provider_id, BACKEND_CODEX)
                await append_event(session_id, "provider_session", {"run_id": run_id, "backend": BACKEND_CODEX, "provider_session_id": provider_id})
                continue
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
                        await append_event(session_id, "tool_started", {"run_id": run_id, "tool": tool})
                    else:
                        await append_event(session_id, "tool_finished", {
                            "run_id": run_id,
                            "tool_id": tool["id"],
                            "tool": tool,
                            "output": item.get("aggregated_output", ""),
                            "exit_code": item.get("exit_code"),
                        })
                elif itype == "agent_message" and etype == "item.completed":
                    text = clean_assistant_text(item.get("text") or "")
                    if text:
                        text_parts.append(text)
                        await append_event(session_id, "assistant_text", {"run_id": run_id, "text": text})
                elif itype in ("reasoning", "agent_reasoning") and etype == "item.completed":
                    text = (item.get("text") or "").strip()
                    if not text and isinstance(item.get("summary"), list):
                        text = "\n".join(x.get("text", "") for x in item["summary"] if isinstance(x, dict)).strip()
                    if text:
                        await append_event(session_id, "reasoning_summary", {"run_id": run_id, "text": text})
    except Exception as e:
        stream_error = f"{type(e).__name__}: {e}"
        logger.exception("Codex run failed session=%s run=%s", session_id, run_id)
    finally:
        await terminate_process_tree(proc, grace=0.5)
        await clear_active_process(session_id)

    stderr = ""
    if proc.stderr:
        stderr = (await proc.stderr.read()).decode("utf-8", "replace").strip()
    stopped = run_id in STOPPED_RUNS
    if stream_error and not stopped:
        await append_event(session_id, "error", {"run_id": run_id, "message": f"Codex stream failed: {stream_error}"})
    if idle_killed:
        await append_event(session_id, "error", {"run_id": run_id, "message": "killed after idle timeout"})
    if not stopped and proc.returncode not in (0, None) and stderr:
        await append_event(session_id, "error", {"run_id": run_id, "message": stderr[:4000], "exit_code": proc.returncode})
    if provider_id:
        await STORE.save_provider_session(session_id, provider_id, BACKEND_CODEX)
    await collect_manifest(session_id, run_id, manifest_path)
    await append_event(session_id, "turn_finished", {
        "run_id": run_id,
        "backend": BACKEND_CODEX,
        "exit_code": proc.returncode,
        "result_text": clean_assistant_text("\n\n".join(text_parts).strip()),
        "stopped": stopped,
    })
    await release_turn_slot(session_id)
    STOPPED_RUNS.discard(run_id)
    schedule_next_queued_turn(session_id)


async def start_turn(
    session_id: str,
    req: TurnRequest,
    *,
    queue_if_busy: bool = True,
    queued_id: str | None = None,
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

        run_id = f"run_{uuid.uuid4().hex[:16]}"
        manifest_path = manifests_dir(session_id) / f"{run_id}.json"
        prompt = req.prompt
        uploads = []
        for file_id in req.file_ids:
            rec_path = FILES_ROOT / file_id / "meta.json"
            if rec_path.exists():
                with suppress(Exception):
                    rec = json.loads(rec_path.read_text())
                    uploads.append(rec)
        if uploads:
            prompt += "\n\n[Attached files]\n"
            for rec in uploads:
                prompt += f"- {rec.get('path')} ({rec.get('filename')}, {rec.get('content_type')})\n"
            prompt += "Use these local paths directly when needed.\n"

        backend = sess.get("backend") or DEFAULT_BACKEND
        memory_seed = str(sess.get("memory_seed") or "").strip()
        if backend == BACKEND_CODEX and memory_seed and not sess.get("memory_seed_used"):
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

        started_payload = {
            "run_id": run_id,
            "backend": backend,
            "prompt": req.prompt,
            "file_ids": req.file_ids,
        }
        if queued_id:
            started_payload["queued_id"] = queued_id
        await append_event(session_id, "turn_started", started_payload)
        task = run_codex(session_id, run_id, prompt, dict(sess), manifest_path) if backend == BACKEND_CODEX else run_claude(session_id, run_id, prompt, dict(sess), manifest_path)
        asyncio.create_task(task)
        current_title = str(sess.get("title") or "").strip()
        if not current_title or current_title == "New chat":
            first_line = (req.prompt.strip().splitlines() or ["New chat"])[0]
            await STORE.update(session_id, {"title": first_line[:72] or "New chat"})
        else:
            await STORE.update(session_id, {})
        return {"run_id": run_id, "queued": False, "session": public_session(STORE.sessions[session_id])}
    except Exception:
        if reserved:
            await release_turn_slot(session_id)
        raise


@asynccontextmanager
async def lifespan(app: FastAPI):
    await STORE.load()
    await JOBS.load()
    ensure_dirs()
    rebuilt_queue_count = rebuild_queued_turns_from_events()
    JOBS.start_scheduler()
    host_monitor_task = asyncio.create_task(host_monitor_loop())
    logger.info("agent server ready state=%s sessions=%d jobs=%d queued=%d", STATE_DIR, len(STORE.sessions), len(JOBS.jobs), rebuilt_queue_count)
    try:
        yield
    finally:
        host_monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await host_monitor_task


app = FastAPI(title="Zenithbot Agent Server", lifespan=lifespan)
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
    return {
        "ok": True,
        "api_contract_version": API_CONTRACT_VERSION,
        "server_identity": server_identity(),
        "state_dir": str(STATE_DIR),
        "default_backend": DEFAULT_BACKEND,
        "default_cwd": existing_cwd(DEFAULT_CWD),
        "auth_required": bool(AGENT_TOKEN),
        "active": active,
        "active_count": len(active),
        "queued": queued,
        "jobs": len(JOBS.jobs),
        "job_guard": pressure,
        "host_health_log": str(HOST_HEALTH_FILE),
    }


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
async def runtime_catalog() -> dict[str, Any]:
    return await asyncio.to_thread(discover_runtime_catalog)


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


@app.post("/api/sessions")
async def create_session(req: CreateSessionRequest) -> dict[str, Any]:
    sess = await STORE.create(req)
    provider_id = session_provider_id(sess)
    should_import = bool(provider_id) if req.import_history is None else req.import_history
    if should_import:
        await import_session_history(sess)
    return {"session": public_session(sess)}


@app.get("/api/sessions/{session_id}")
async def get_session(
    session_id: str,
    after: int = 0,
    before: int | None = None,
    limit: int = DEFAULT_SESSION_EVENT_LIMIT,
    tail: bool = True,
) -> dict[str, Any]:
    sess = STORE.sessions.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    events = read_events(session_id, after=after, before=before, limit=limit, tail=tail and after <= 0)
    if after > 0 and before is None:
        _, latest_seq, event_count = event_seq_bounds(session_id)
    else:
        latest_seq = int(events[-1].get("seq", 0)) if events else 0
        event_count = 0
    omitted_before = max(0, int(events[0].get("seq", 1)) - 1) if tail and after <= 0 and events else 0
    if events:
        omitted_after = max(0, latest_seq - int(events[-1].get("seq", 0)))
    elif after > 0:
        omitted_after = max(0, latest_seq - after)
    else:
        omitted_after = 0
    return {
        "session": public_session(sess),
        "events": events,
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
    return build_handoff_digest(session_id, detail=req.detail, user_prompt=req.user_prompt)


@app.patch("/api/sessions/{session_id}")
async def update_session(session_id: str, req: UpdateSessionRequest) -> dict[str, Any]:
    sess = await STORE.update(session_id, req.model_dump(exclude_unset=True))
    return {"session": public_session(sess)}


@app.post("/api/sessions/{session_id}/order")
async def reorder_session(session_id: str, req: ReorderSessionRequest) -> dict[str, Any]:
    sessions = await STORE.reorder(session_id, req.direction)
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
            provider_session_id=forked_codex_thread_id if parent_backend == BACKEND_CODEX else None,
            codex_thread_id=forked_codex_thread_id if parent_backend == BACKEND_CODEX else None,
        ),
        parent_id=session_id,
    )
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
    return {"session": public_session(child)}


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
        "content_type": file.content_type or guess_content_type(dest.name),
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

    return sorted(
        records.values(),
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
    parser = argparse.ArgumentParser(description="Zenithbot Agent Server")
    parser.add_argument("cmd", nargs="?", default="serve", choices=["serve"])
    parser.add_argument("--bind", default=os.environ.get("ZENITHBOT_AGENT_BIND", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("ZENITHBOT_AGENT_PORT", "7850")))
    args = parser.parse_args()
    uvicorn.run("agent_server:app", host=args.bind, port=args.port, app_dir=str(Path(__file__).parent), log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
