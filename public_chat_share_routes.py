"""Opt-in chat snapshots: authenticated management, isolated public HTML.

No background tasks, live streams, provider calls, or public session APIs.
All disk/HTML work runs in bounded worker admissions off the server event loop.
"""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from public_chat_shares import (
    PublicChatShareStore, PublicChatShareUnavailable, PublicChatShareValidationError,
    public_chat_share_headers, render_public_chat_html,
)
from public_chat_transcript import PublicTranscriptError

PUBLIC_SHARE_PATH_RE = re.compile(r'''(/share/)[^/?\s"']+''')
WARNING = (
    "Anyone with this link can read and copy this snapshot. Review it for secrets "
    "before sharing. New messages are not added. Files, tools, and private runtime "
    "instructions are excluded. Revocation cannot erase copies already saved."
)


def redact_public_share_path(value: str) -> str:
    return PUBLIC_SHARE_PATH_RE.sub(r"\1<redacted>", value)


def public_share_url(base: str, token: str) -> str | None:
    if not base:
        return None
    try:
        parsed = urlsplit(base)
        parsed.port  # Reject malformed/out-of-range ports before persistence.
    except (TypeError, ValueError):
        raise PublicChatShareValidationError("Public chat base URL must be an HTTPS origin") from None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or "\\" in base or any(c.isspace() for c in base)):
        raise PublicChatShareValidationError("Public chat base URL must be an HTTPS origin")
    return base.rstrip("/") + "/share/" + token


def create_public_chat_share_router(
    *, storage_root, authorize, session_exists, load_transcript, public_base_url,
) -> APIRouter:
    """Callbacks are explicit: no access to a global server/token on import."""
    router = APIRouter()
    active_management = 0
    active_views = 0
    cached_store = None
    store_lock = threading.Lock()

    async def worker(operation, *, public=False):
        nonlocal active_management, active_views
        if (active_views if public else active_management) >= (4 if public else 1):
            raise HTTPException(503, "Sharing is busy; retry shortly", headers={"Retry-After": "2"})
        if public:
            active_views += 1
        else:
            active_management += 1
        # Shield worker completion on disconnect: a cancelled caller must not
        # release admission while its filesystem thread is still running.
        task = asyncio.create_task(asyncio.to_thread(operation))
        def finished(_):
            nonlocal active_management, active_views
            if public:
                active_views -= 1
            else:
                active_management -= 1
            # Retrieve an exception even when the requesting client disconnected.
            if not task.cancelled():
                task.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def guard(request, session_id):
        authorize(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) or not session_exists(session_id):
            raise HTTPException(404, "Chat not found")

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json")
        async def collect():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > 8192:
                    raise HTTPException(413, "Share request is too large")
                data.extend(chunk)
            return data
        try:
            # asyncio.timeout is unavailable on supported Python 3.10 hosts.
            data = await asyncio.wait_for(collect(), timeout=5)
        except asyncio.TimeoutError:
            raise HTTPException(408, "Share request body was not received") from None
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object")
        return value

    def store(*, create=False):
        nonlocal cached_store
        with store_lock:
            if cached_store is None:
                # Anonymous guesses must never initialize storage on a server
                # that has not created a share. Initialize an existing store
                # once, then use its read-only connections for public views.
                if not create and not (Path(storage_root) / "snapshots.sqlite3").is_file():
                    raise PublicChatShareUnavailable()
                cached_store = (
                    PublicChatShareStore(storage_root) if create
                    else PublicChatShareStore.open_existing(storage_root)
                )
            return cached_store

    def result(value, status=200):
        return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/chat-shares/{session_id}/preview")
    async def preview(session_id: str, request: Request):
        guard(request, session_id)
        if await body(request):
            raise HTTPException(400, "Preview takes an empty JSON object")
        try:
            snapshot = await worker(lambda: load_transcript(session_id, None))
        except PublicTranscriptError as exc:
            raise HTTPException(409, str(exc)) from None
        return result({**snapshot, "warning": WARNING})

    @router.post("/api/admin/chat-shares/{session_id}")
    async def create(session_id: str, request: Request):
        guard(request, session_id)
        value = await body(request)
        if set(value) - {"confirmed_public", "through_bytes", "digest", "title", "expires_at"}:
            raise HTTPException(400, "Unknown share option")
        if value.get("confirmed_public") is not True:
            raise HTTPException(400, "Public sharing must be explicitly confirmed")
        if type(value.get("through_bytes")) is not int or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("digest", ""))):
            raise HTTPException(400, "Preview the chat before sharing")
        def publish():
            # Validate configuration before persisting any public capability.
            base = public_base_url()
            public_share_url(base, "validation")
            snapshot = load_transcript(session_id, value["through_bytes"])
            if not hmac.compare_digest(snapshot["digest"], value["digest"]):
                raise PublicTranscriptError("Chat changed; preview it again")
            share = store(create=True).create_share(session_id, snapshot["messages"],
                title=value.get("title"), expires_at=value.get("expires_at"))
            token = share.pop("token")
            share.pop("session_id", None)
            return {**share, "path": "/share/" + token,
                    "url": public_share_url(base, token), "warning": WARNING}
        try:
            return result(await worker(publish), 201)
        except PublicTranscriptError as exc:
            raise HTTPException(409, str(exc)) from None
        except PublicChatShareValidationError as exc:
            raise HTTPException(400, str(exc)) from None

    @router.get("/api/admin/chat-shares/{session_id}")
    async def list_shares(session_id: str, request: Request):
        # Listing/revocation remain available after deleting the original chat.
        authorize(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise HTTPException(404, "Chat not found")
        try:
            shares = await worker(lambda: store().list_shares(session_id))
        except PublicChatShareUnavailable:
            shares = []
        return result({"shares": shares})

    @router.delete("/api/admin/chat-shares/{session_id}/{share_id}")
    async def revoke(session_id: str, share_id: str, request: Request):
        authorize(request)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
            raise HTTPException(404, "Chat not found")
        try:
            revoked = await worker(lambda: store().revoke_share(share_id, session_id=session_id))
        except PublicChatShareUnavailable:
            revoked = False
        if not revoked:
            raise HTTPException(404, "Share not found")
        return result({"revoked": True})

    @router.api_route("/share/{token}", methods=["GET", "HEAD"], include_in_schema=False)
    async def view(token: str, request: Request):
        headers = public_chat_share_headers()
        if request.url.query or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return Response("Shared conversation unavailable.", status_code=404, headers=headers)
        try:
            content = await worker(lambda: render_public_chat_html(store().get_snapshot(token)), public=True)
        except PublicChatShareUnavailable:
            return Response("Shared conversation unavailable.", status_code=404, headers=headers)
        except HTTPException as exc:
            # In particular, keep CSP/no-store on bounded-admission 503s.
            if exc.headers and "Retry-After" in exc.headers:
                headers["Retry-After"] = exc.headers["Retry-After"]
            return Response("Shared conversation temporarily unavailable.", status_code=exc.status_code, headers=headers)
        except Exception:
            # Never expose filesystem, database, or implementation details on
            # the unauthenticated viewer, including an unexpected failure.
            return Response("Shared conversation temporarily unavailable.", status_code=503, headers=headers)
        return Response(content if request.method == "GET" else b"", media_type="text/html", headers=headers)

    return router
