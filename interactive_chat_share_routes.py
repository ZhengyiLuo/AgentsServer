"""Narrow guest routes; callbacks never receive arbitrary chat IDs or tool options."""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
import re
import threading
from urllib.parse import unquote
import weakref

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from interactive_chat_shares import (
    InteractiveChatShareStore, Unavailable, ValidationError, Conflict, SHARE_ID,
    MAX_PROMPT_BYTES, MAX_UPLOAD_BYTES, csrf_token, _utf8_size,
)
from public_chat_share_routes import public_share_url
import interactive_chat_share_web as web
from interactive_chat_controls import ChatControlError

WARNING = (
    "Trusted full chat control: this person can read this chat, send or steer prompts, upload files, "
    "stop work, manage the queue and goals, change model/permission settings, respond to approvals, "
    "and create or run persistent scheduled jobs for this chat. "
    "No file browsing, downloads, terminal, other chats, or server administration are shared. "
    "The existing agent retains its normal tools and context, so they can ask it to use tools "
    "or return sensitive information. This is not a sandbox. The link can be redeemed once. "
    "Revocation stops future access but cannot erase saved copies, undo accepted work, or remove already configured jobs."
)
CONTROL_ACTIONS = frozenset({
    "turn.stop", "turn.steer", "queue.run_now", "queue.edit", "queue.delete", "queue.move",
    "settings.update", "goal.set", "goal.resume", "goal.pause", "goal.delete", "job.create", "job.update",
    "job.delete", "job.toggle", "job.run", "approval.respond",
})
CONTROL_READ_ACTIONS = frozenset({"timeline.older", "timeline.around", "timeline.trace", "timeline.index", "jobs.runs", "runtime.catalog"})
NATIVE_STATE_FIELDS = frozenset({"revision", "session", "events", "queue", "active", "goal", "jobs",
    "codex_runtime", "claude_runtime", "health", "runtime_catalog", "hasMoreEvents", "nextTimelineBefore", "eventsTotal"})
COOKIE = "__Secure-AgentsDock-Chat"
HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive", "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def create_interactive_chat_share_router(*, storage_root, authorize, session_exists, public_base_url,
    load_transcript, submit_prompt, save_upload, wait_for_change, chat_control=None):
    """Callbacks are scoped by the durable share ledger, not browser identities.

    A durable request ledger prevents repeated callback execution; ambiguous
    pending results fail closed. wait_for_change returns False on timeout without reading a
    transcript. load_transcript supplies sanitized one-chat DTOs, never raw provider transcripts.
    """
    router = APIRouter()
    cached_store = None
    store_lock = threading.Lock()
    locks = weakref.WeakValueDictionary()
    streams = 0
    active_workers = 0
    active_submissions = 0
    active_uploads = 0

    def share_lock(share_id):
        lock = locks.get(share_id)
        if lock is None:
            lock = asyncio.Lock()
            locks[share_id] = lock
        return lock

    async def worker(operation):
        nonlocal active_workers
        if active_workers >= 8:
            raise HTTPException(503, "Sharing is busy; retry shortly")
        active_workers += 1
        task = asyncio.create_task(asyncio.to_thread(operation))
        def finished(_):
            nonlocal active_workers
            active_workers -= 1
            if not task.cancelled():
                task.exception()
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    def store(create=False):
        nonlocal cached_store
        with store_lock:
            if cached_store is None:
                if not create and not (Path(storage_root) / "interactive.sqlite3").is_file():
                    raise Unavailable()
                cached_store = InteractiveChatShareStore(storage_root) if create else InteractiveChatShareStore.open_existing(storage_root)
            return cached_store

    def origin():
        base = public_base_url()
        if not base:
            raise HTTPException(409, "Configure an HTTPS public chat origin before enabling interactive sharing")
        try:
            public_share_url(base, "validation")
        except ValidationError:
            raise HTTPException(409, "Interactive sharing requires a configured HTTPS origin") from None
        return base.rstrip("/")

    def public_guard(request, *, write=False, shell=False):
        expected = origin()
        # Never use forwarded/Host headers to choose the allowed origin. The
        # configured origin is the authority; ingress must preserve its host.
        if str(request.base_url).rstrip("/") != expected or request.url.query:
            raise HTTPException(403, "This origin is not permitted")
        supplied = request.headers.get("origin")
        if (write and supplied != expected) or (supplied is not None and supplied != expected):
            raise HTTPException(403, "This origin is not permitted")
        public_navigation = (
            shell and request.method in {"GET", "HEAD"}
            and request.headers.get("sec-fetch-mode") == "navigate"
            and request.headers.get("sec-fetch-dest") == "document"
        )
        if request.headers.get("sec-fetch-site") in {"cross-site", "same-site"} and not public_navigation:
            raise HTTPException(403, "Cross-origin access is not permitted")

    def management(request, session_id, *, exists=False):
        authorize(request)
        if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None or (exists and not session_exists(session_id)):
            raise HTTPException(404, "Chat not found")

    def result(value, status=200):
        return JSONResponse(value, status_code=status, headers=HEADERS)

    async def body_bytes(request, limit, *, timeout=10):
        async def read():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(413, "Request is too large")
                data.extend(chunk)
            return bytes(data)
        try:
            return await asyncio.wait_for(read(), timeout)
        except asyncio.TimeoutError:
            raise HTTPException(408, "Request body was not received") from None

    async def json_body(request, *, limit=8192):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json")
        try:
            value = json.loads(await body_bytes(request, limit))
        except (ValueError, UnicodeError, RecursionError):
            raise HTTPException(400, "Invalid JSON") from None
        if not isinstance(value, dict):
            raise HTTPException(400, "Expected a JSON object")
        return value

    async def auth(request, share_id, *, write=False):
        public_guard(request, write=write)
        if SHARE_ID.fullmatch(share_id) is None:
            raise HTTPException(404, "Shared conversation unavailable")
        # Reject ambiguous same-name cookies rather than choosing browser order.
        cookies = [part.strip().partition("=")[2] for part in request.headers.get("cookie", "").split(";")
            if part.strip().partition("=")[0] == COOKIE]
        if len(cookies) != 1:
            raise HTTPException(404, "Shared conversation unavailable")
        token = cookies[0]
        try:
            grant = await worker(lambda: store().authenticate(share_id, token))
        except Unavailable:
            raise HTTPException(404, "Shared conversation unavailable") from None
        if not session_exists(grant["session_id"]):
            raise HTTPException(404, "Shared conversation unavailable")
        supplied_csrf = request.headers.get("x-chat-csrf", "")
        if write and (re.fullmatch(r"[a-f0-9]{64}", supplied_csrf) is None or not hmac.compare_digest(supplied_csrf, csrf_token(token))):
            raise HTTPException(403, "Missing or invalid request confirmation")
        return grant, token

    async def snapshot(grant):
        try:
            value = await load_transcript(grant["session_id"])
        except Exception:
            raise HTTPException(503, "Shared conversation temporarily unavailable; reopen it after checking the original chat") from None
        if not isinstance(value, dict) or not isinstance(value.get("revision"), str) or len(value["revision"]) > 128:
            raise HTTPException(503, "Shared conversation temporarily unavailable")
        if "session" in value:
            if (set(value) - NATIVE_STATE_FIELDS or not isinstance(value["session"], dict)
                    or value["session"].get("id") != grant["session_id"] or not isinstance(value.get("events"), list)
                    or any(not isinstance(item, dict) or item.get("session_id", grant["session_id"]) != grant["session_id"] for item in value["events"])):
                raise HTTPException(503, "Shared conversation temporarily unavailable")
            projected = {**value, "title": grant["title"]}
        else:
            # Compatibility for the isolated text-projection adapter; the
            # deployed renderer receives native DTOs, never invented events.
            if type(value.get("busy")) is not bool or not isinstance(value.get("messages"), list) or len(value["messages"]) > 1000:
                raise HTTPException(503, "Shared conversation temporarily unavailable")
            messages = []
            for item in value["messages"]:
                if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"} or not isinstance(item.get("text"), str):
                    raise HTTPException(503, "Shared conversation temporarily unavailable")
                message = {"role": item["role"], "text": item["text"]}
                if type(item.get("timestamp")) in (int, float):
                    message["timestamp"] = item["timestamp"]
                if item.get("pending") is True:
                    message["pending"] = True
                messages.append(message)
            projected = {"revision": value["revision"], "busy": value["busy"], "messages": messages, "title": grant["title"]}
        try:
            size = len(json.dumps(projected, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (ValueError, UnicodeError):
            raise HTTPException(503, "Shared conversation temporarily unavailable") from None
        if size > 2 * 1024 * 1024:
            raise HTTPException(503, "Shared conversation exceeds the safe display size")
        return projected

    @router.post("/api/admin/interactive-chat-shares/{session_id}")
    async def create(session_id: str, request: Request):
        management(request, session_id, exists=True)
        value = await json_body(request)
        if value.get("confirmed_interactive") is not True or set(value) - {"confirmed_interactive", "title", "expires_at"}:
            raise HTTPException(400, "Trusted interactive collaboration must be explicitly confirmed")
        base = origin()
        try:
            created = await worker(lambda: store(True).create_share(session_id, title=value.get("title"), expires_at=value.get("expires_at")))
        except ValidationError as exc:
            raise HTTPException(400, str(exc)) from None
        invite = created.pop("invitation_token")
        path = f"/interactive-chat/{created['id']}#invite={invite}"
        return result({**created, "path": path, "url": base + path, "warning": WARNING}, 201)

    @router.get("/api/admin/interactive-chat-shares/{session_id}")
    async def listing(session_id: str, request: Request):
        management(request, session_id)
        try:
            shares = await worker(lambda: store().list_shares(session_id))
        except Unavailable:
            shares = []
        return result({"shares": shares})

    @router.delete("/api/admin/interactive-chat-shares/{session_id}/{share_id}")
    async def revoke(session_id: str, share_id: str, request: Request):
        management(request, session_id)
        async with share_lock(share_id):
            try:
                revoked = await worker(lambda: store().revoke_share(share_id, session_id=session_id))
            except Unavailable:
                revoked = False
        if not revoked:
            raise HTTPException(404, "Share not found")
        return result({"revoked": True})

    @router.api_route("/interactive-chat/assets/{asset:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def asset(asset: str, request: Request):
        assets = getattr(web, "ASSETS", None)
        if assets is None:
            assets = {"viewer.js": ("text/javascript", web.JAVASCRIPT), "viewer.css": ("text/css", web.CSS)}
        selected = assets.get(asset)
        if selected is None:
            raise HTTPException(404)
        media_type, data = selected
        return Response(data if request.method == "GET" else b"", media_type=media_type, headers=HEADERS)

    @router.api_route("/interactive-chat/{share_id}", methods=["GET", "HEAD"], include_in_schema=False)
    async def shell(share_id: str, request: Request):
        public_guard(request, shell=True)
        if SHARE_ID.fullmatch(share_id) is None:
            raise HTTPException(404)
        return Response(web.HTML if request.method == "GET" else b"", media_type="text/html", headers=HEADERS)

    @router.post("/interactive-chat/{share_id}/redeem", include_in_schema=False)
    async def redeem(share_id: str, request: Request):
        public_guard(request, write=True)
        value = await json_body(request)
        if set(value) != {"invitation_token"} or SHARE_ID.fullmatch(share_id) is None:
            raise HTTPException(404, "Invitation unavailable")
        async with share_lock(share_id):
            try:
                token = await worker(lambda: store().redeem(share_id, value["invitation_token"]))
            except Unavailable:
                raise HTTPException(404, "Invitation unavailable") from None
        response = result({"redeemed": True, "csrf": csrf_token(token)})
        response.set_cookie(COOKIE, token, secure=True, httponly=True, samesite="strict", path=f"/interactive-chat/{share_id}")
        return response

    @router.get("/interactive-chat/{share_id}/state", include_in_schema=False)
    async def state(share_id: str, request: Request):
        grant, token = await auth(request, share_id)
        value = await snapshot(grant)
        await auth(request, share_id)
        return result({**value, "csrf": csrf_token(token)})

    @router.post("/interactive-chat/{share_id}/prompts", include_in_schema=False)
    async def prompt(share_id: str, request: Request):
        nonlocal active_submissions
        await auth(request, share_id, write=True)
        value = await json_body(request, limit=MAX_PROMPT_BYTES + 4096)
        if set(value) - {"prompt", "upload_ids", "request_id"} or not isinstance(value.get("prompt"), str):
            raise HTTPException(400, "Only a prompt and this share's uploads are accepted")
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id) is None:
            raise HTTPException(400, "A stable request ID is required")
        async def deliver():
            async with share_lock(share_id):
                grant, token = await auth(request, share_id, write=True)
                try:
                    _utf8_size(value["prompt"], "Prompt", MAX_PROMPT_BYTES)
                    refs = await worker(lambda: store().upload_refs(share_id, token, value.get("upload_ids", [])))
                except ValidationError as exc:
                    raise HTTPException(400, str(exc)) from None
                except Unavailable:
                    raise HTTPException(404, "Upload unavailable") from None
                if not value["prompt"].strip() and not refs:
                    raise HTTPException(400, "Write a prompt or attach a file")
                try:
                    previous = await worker(lambda: store().reserve_submission(share_id, token, request_id, value["prompt"], refs))
                except Conflict as exc:
                    raise HTTPException(409, str(exc)) from None
                if previous is not None:
                    return previous
                try:
                    receipt = await submit_prompt(grant["session_id"], share_id, value["prompt"], refs, request_id)
                    if not isinstance(receipt, dict) or receipt.get("accepted") is not True or type(receipt.get("queued")) is not bool:
                        raise ValueError("Invalid acceptance receipt")
                    public_receipt = {"accepted": True, "queued": receipt["queued"], "request_id": request_id}
                    queued_id = receipt.get("queued_id")
                    if queued_id is not None:
                        if not receipt["queued"] or not isinstance(queued_id, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", queued_id) is None:
                            raise ValueError("Invalid queued message receipt")
                        public_receipt["queued_id"] = queued_id
                    await worker(lambda: store().accept_submission(share_id, request_id, public_receipt))
                except Exception:
                    raise HTTPException(503, "Message acceptance is unconfirmed. Inspect the chat before sending again; this request will not be automatically retried") from None
                return public_receipt
        if active_submissions >= 8:
            raise HTTPException(503, "Message submission is busy; retry shortly")
        active_submissions += 1
        task = asyncio.create_task(deliver())
        def finished(_):
            nonlocal active_submissions
            active_submissions -= 1
            if not task.cancelled():
                task.exception()
        task.add_done_callback(finished)
        return result(await asyncio.shield(task), 202)

    @router.post("/interactive-chat/{share_id}/uploads", include_in_schema=False)
    async def upload(share_id: str, request: Request):
        nonlocal active_uploads
        await auth(request, share_id, write=True)
        if active_uploads >= 4:
            raise HTTPException(503, "Uploads are busy; retry shortly")
        active_uploads += 1
        task = None
        try:
            name = unquote(request.headers.get("x-chat-filename", ""))
            media_type = request.headers.get("content-type", "application/octet-stream").split(";")[0]
            data = await body_bytes(request, MAX_UPLOAD_BYTES, timeout=30)
            async def commit_upload():
                async with share_lock(share_id):
                    grant, token = await auth(request, share_id, write=True)
                    try:
                        upload_id = await worker(lambda: store().reserve_upload(share_id, token, name=name, media_type=media_type, byte_size=len(data)))
                    except ValidationError as exc:
                        raise HTTPException(400, str(exc)) from None
                    try:
                        private_ref = await save_upload(grant["session_id"], share_id, name, media_type, data)
                        await worker(lambda: store().complete_upload(share_id, token, upload_id, private_ref))
                    except Exception:
                        # A callback may already have durably written the file.
                        # Keep its reservation charged when completion is unknown.
                        raise HTTPException(503, "File upload could not be confirmed; its storage reservation is retained") from None
                    return {"id": upload_id, "name": name, "media_type": media_type, "byte_size": len(data)}
            task = asyncio.create_task(commit_upload())
            def finished(_):
                nonlocal active_uploads
                active_uploads -= 1
                if not task.cancelled():
                    task.exception()
            task.add_done_callback(finished)
            return result(await asyncio.shield(task), 201)
        finally:
            if task is None:
                active_uploads -= 1

    @router.get("/interactive-chat/{share_id}/controls", include_in_schema=False)
    async def controls(share_id: str, request: Request):
        grant, _ = await auth(request, share_id)
        value = await snapshot(grant)
        await auth(request, share_id)
        return result(value)

    @router.post("/interactive-chat/{share_id}/controls", include_in_schema=False)
    async def control(share_id: str, request: Request):
        nonlocal active_submissions
        await auth(request, share_id, write=True)
        value = await json_body(request, limit=MAX_PROMPT_BYTES + 4096)
        if (set(value) != {"action", "payload", "request_id"} or not isinstance(value.get("action"), str)
                or value["action"] not in CONTROL_ACTIONS | CONTROL_READ_ACTIONS
                or not isinstance(value.get("payload"), dict)):
            raise HTTPException(400, "Choose an explicitly supported chat action")
        request_id = value["request_id"]
        if not isinstance(request_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{16,128}", request_id) is None:
            raise HTTPException(400, "A stable request ID is required")
        if chat_control is None:
            raise HTTPException(503, "Chat controls are unavailable")
        action, payload = value["action"], value["payload"]
        try:
            encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            _utf8_size(encoded, "Action payload", MAX_PROMPT_BYTES)
        except (ValueError, UnicodeError):
            raise HTTPException(400, "Invalid action payload") from None
        if action in CONTROL_READ_ACTIONS:
            grant, _ = await auth(request, share_id, write=True)
            try:
                public_result = await chat_control(grant["session_id"], action, payload)
                if len(json.dumps(public_result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
                    raise ValueError("Read result is too large")
            except ChatControlError as exc:
                raise HTTPException(403 if exc.code == "forbidden" else 400, "Invalid or unavailable chat read") from None
            except Exception:
                raise HTTPException(503, "This chat read could not be completed") from None
            await auth(request, share_id)
            return result({"accepted": True, "action": action, "request_id": request_id, "result": public_result})
        async def apply_control():
            async with share_lock(share_id):
                grant, token = await auth(request, share_id, write=True)
                try:
                    previous = await worker(lambda: store().reserve_submission(share_id, token, request_id, encoded, [], operation="control:" + action))
                except Conflict as exc:
                    raise HTTPException(409, str(exc)) from None
                if previous is not None:
                    return previous
                try:
                    accepted = await chat_control(grant["session_id"], action, payload,
                        share_id=share_id, request_id=request_id)
                    if not isinstance(accepted, dict) or accepted.get("accepted") is not True:
                        raise ValueError("Invalid control receipt")
                    public_result = accepted.get("result")
                    if len(json.dumps(public_result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
                        raise ValueError("Control result is too large")
                    receipt = {"accepted": True, "action": action, "request_id": request_id, "result": public_result}
                    await worker(lambda: store().accept_submission(share_id, request_id, receipt))
                except ChatControlError as exc:
                    # Only this typed validator error guarantees no callback ran.
                    receipt = {"accepted": False, "action": action, "request_id": request_id,
                        "error_code": "forbidden" if exc.code == "forbidden" else "invalid_request",
                        "detail": "This chat control request is not permitted" if exc.code == "forbidden" else "Invalid chat control request"}
                    await worker(lambda: store().accept_submission(share_id, request_id, receipt))
                except Exception:
                    raise HTTPException(503, "Action acceptance is unconfirmed. Inspect this chat before trying again; this request will not be automatically retried") from None
                return receipt
        if active_submissions >= 8:
            raise HTTPException(503, "Chat controls are busy; retry shortly")
        active_submissions += 1
        task = asyncio.create_task(apply_control())
        def finished(_):
            nonlocal active_submissions
            active_submissions -= 1
            if not task.cancelled():
                task.exception()
        task.add_done_callback(finished)
        receipt = await asyncio.shield(task)
        return result(receipt, 202 if receipt["accepted"] else 403 if receipt["error_code"] == "forbidden" else 400)

    @router.get("/interactive-chat/{share_id}/events", include_in_schema=False)
    async def events(share_id: str, request: Request):
        nonlocal streams
        grant, _ = await auth(request, share_id)
        if streams >= 32:
            raise HTTPException(503, "Too many shared conversation streams")
        streams += 1
        async def stream():
            nonlocal streams
            try:
                value = await snapshot(grant)
                await auth(request, share_id)
                revision = value["revision"]
                yield "event: state\ndata: " + json.dumps(value, ensure_ascii=False) + "\n\n"
                while not await request.is_disconnected():
                    changed = await wait_for_change(grant["session_id"], revision, 20)
                    await auth(request, share_id)
                    if not changed:
                        yield ": keepalive\n\n"
                        continue
                    # Coalesce bursty text notifications; this is not a poll.
                    await asyncio.sleep(1)
                    value = await snapshot(grant)
                    await auth(request, share_id)
                    revision = value["revision"]
                    yield "event: state\ndata: " + json.dumps(value, ensure_ascii=False) + "\n\n"
            except (Unavailable, HTTPException):
                yield "event: unavailable\ndata: {}\n\n"
            finally:
                streams -= 1
        return StreamingResponse(stream(), media_type="text/event-stream", headers={**HEADERS, "X-Accel-Buffering": "no"})

    return router
