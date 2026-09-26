"""Narrow guest routes; callbacks never receive arbitrary chat IDs or tool options."""
from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path
import re
import threading
import tempfile
from urllib.parse import unquote
import weakref

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from interactive_chat_shares import (
    InteractiveChatShareStore, Unavailable, ValidationError, Conflict, SHARE_ID,
    MAX_PROMPT_BYTES, csrf_token, _utf8_size,
)
from public_chat_share_routes import chat_share_origin
import interactive_chat_share_web as web
from interactive_chat_controls import ChatControlError
from shared_chat_video_stream import SharedVideoResponse, SharedFileResponse, SharedImageResponse
from shared_chat_videos import VIDEO_ID, SHARED_FILE_ID, SharedVideoUnavailable

WARNING = (
    "Trusted full chat control: this person can read this chat, send or steer prompts, upload files, "
    "stop work, manage the queue and goals, change model/permission settings, respond to approvals, "
    "and create or run persistent scheduled jobs for this chat. "
    "Sent and published attachments can be downloaded; images and videos can be viewed. "
    "No general file browsing, terminal, other chats, or server administration are shared. "
    "The existing agent retains its normal tools and context, so they can ask it to use tools "
    "or return sensitive information. This is not a sandbox. Share the URL and reusable access token separately. "
    "Revocation stops future access but cannot erase saved copies, undo accepted work, or remove already configured jobs."
)
CONTROL_ACTIONS = frozenset({
    "turn.stop", "turn.steer", "queue.run_now", "queue.edit", "queue.delete", "queue.move",
    "settings.update", "goal.set", "goal.resume", "goal.pause", "goal.delete", "job.create", "job.update",
    "job.delete", "job.toggle", "job.run", "approval.respond",
})
CONTROL_READ_ACTIONS = frozenset({"timeline.older", "timeline.around", "timeline.trace", "timeline.index", "jobs.runs", "runtime.catalog", "handoffs.get"})
NATIVE_STATE_FIELDS = frozenset({"revision", "session", "events", "queue", "active", "goal", "jobs",
    "codex_runtime", "claude_runtime", "health", "runtime_catalog", "hasMoreEvents", "nextTimelineBefore", "eventsTotal"})
COOKIE = "__Secure-AgentsDock-Chat"
HTTP_COOKIE = "AgentsDock-Chat"
HEADERS = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow, noarchive", "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; font-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def create_interactive_chat_share_router(*, storage_root, authorize, session_exists, public_base_url,
    load_transcript, submit_prompt, save_upload, wait_for_change, chat_control=None, open_video=None,
    open_file=None, max_upload_bytes=25 * 1024 * 1024 * 1024):
    """Callbacks are scoped by the durable share ledger, not browser identities.

    A durable request ledger prevents repeated callback execution; ambiguous
    pending results fail closed. wait_for_change returns False on timeout without reading a
    transcript. load_transcript supplies sanitized one-chat DTOs, never raw provider transcripts.
    """
    router = APIRouter()
    cached_store = None
    writable_store = False
    store_lock = threading.Lock()
    locks = weakref.WeakValueDictionary()
    streams = 0
    active_workers = 0
    active_submissions = 0

    class SharedChatStreamResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            nonlocal streams
            if streams >= 32:
                raise HTTPException(503, "Too many shared conversation streams")
            streams += 1
            try:
                # Header delivery can disconnect before the body generator
                # starts, so its finally block cannot own this lease.
                await super().__call__(scope, receive, send)
            finally:
                streams -= 1

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
        nonlocal cached_store, writable_store
        with store_lock:
            if cached_store is None or (create and not writable_store):
                if not create and not (Path(storage_root) / "interactive.sqlite3").is_file():
                    raise Unavailable()
                cached_store = InteractiveChatShareStore(storage_root) if create else InteractiveChatShareStore.open_existing(storage_root)
                writable_store = create
            return cached_store

    def origin(base):
        if not isinstance(base, str) or not base:
            raise HTTPException(409, "This link has no server address; create a new share")
        try:
            return chat_share_origin(base)
        except ValidationError:
            raise HTTPException(400, "Chat link must use an HTTP or HTTPS origin") from None

    async def public_guard(request, share_id, *, write=False, shell=False):
        if SHARE_ID.fullmatch(share_id) is None:
            raise HTTPException(404, "Shared conversation unavailable")
        try:
            bound_origin = await worker(lambda: store().share_origin(share_id))
        except Unavailable:
            raise HTTPException(404, "Shared conversation unavailable") from None
        expected = origin(bound_origin or public_base_url())
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
        return expected

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
        expected = await public_guard(request, share_id, write=write)
        if SHARE_ID.fullmatch(share_id) is None:
            raise HTTPException(404, "Shared conversation unavailable")
        # Reject ambiguous same-name cookies rather than choosing browser order.
        cookies = [part.strip().partition("=")[2] for part in request.headers.get("cookie", "").split(";")
            if part.strip().partition("=")[0] == (COOKIE if expected.startswith("https:") else HTTP_COOKIE)]
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
            json.dumps(projected, ensure_ascii=False, allow_nan=False)
        except (ValueError, UnicodeError):
            raise HTTPException(503, "Shared conversation temporarily unavailable") from None
        return projected

    @router.post("/api/admin/interactive-chat-shares/{session_id}")
    async def create(session_id: str, request: Request):
        management(request, session_id, exists=True)
        value = await json_body(request)
        if value.get("confirmed_interactive") is not True or set(value) - {"confirmed_interactive", "title", "expires_at", "base_url"}:
            raise HTTPException(400, "Trusted interactive collaboration must be explicitly confirmed")
        if "base_url" in value and (not isinstance(value["base_url"], str) or not value["base_url"]):
            raise HTTPException(400, "Invalid chat link origin")
        base = origin(value.get("base_url") or public_base_url() or str(request.base_url).rstrip("/"))
        try:
            created = await worker(lambda: store(True).create_share(session_id, title=value.get("title"), expires_at=value.get("expires_at"), public_origin=base))
        except ValidationError as exc:
            raise HTTPException(400, str(exc)) from None
        invite = created.pop("invitation_token")
        path = f"/interactive-chat/{created['id']}"
        return result({**created, "path": path, "url": base + path, "access_token": invite, "warning": WARNING}, 201)

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
        await public_guard(request, share_id, shell=True)
        if SHARE_ID.fullmatch(share_id) is None:
            raise HTTPException(404)
        return Response(web.HTML if request.method == "GET" else b"", media_type="text/html", headers=HEADERS)

    @router.post("/interactive-chat/{share_id}/redeem", include_in_schema=False)
    async def redeem(share_id: str, request: Request):
        expected = await public_guard(request, share_id, write=True)
        value = await json_body(request)
        if set(value) != {"invitation_token"} or SHARE_ID.fullmatch(share_id) is None:
            raise HTTPException(404, "Invitation unavailable")
        async with share_lock(share_id):
            try:
                token = await worker(lambda: store().redeem(share_id, value["invitation_token"]))
            except Unavailable:
                raise HTTPException(404, "Invitation unavailable") from None
        response = result({"redeemed": True, "csrf": csrf_token(token)})
        secure = expected.startswith("https:")
        response.set_cookie(COOKIE if secure else HTTP_COOKIE, token, secure=secure, httponly=True, samesite="strict", path=f"/interactive-chat/{share_id}")
        return response

    @router.get("/interactive-chat/{share_id}/state", include_in_schema=False)
    async def state(share_id: str, request: Request):
        grant, token = await auth(request, share_id)
        value = await snapshot(grant)
        await auth(request, share_id)
        return result({**value, "csrf": csrf_token(token)})

    async def attachment_response(share_id, handle, request, *, download):
        grant, _ = await auth(request, share_id)
        video = VIDEO_ID.fullmatch(handle) is not None
        opener = open_video if video else open_file
        if opener is None or len(handle) > 1024 or not (video or SHARED_FILE_ID.fullmatch(handle)):
            raise HTTPException(404, "Shared attachment is unavailable")
        try:
            opened = await opener(grant["session_id"], handle)
        except (SharedVideoUnavailable, OSError, ValueError):
            raise HTTPException(404, "Shared attachment is unavailable") from None
        async def reauthorize():
            await auth(request, share_id)
        try:
            # Construction owns the descriptor even if its pinned revision
            # changed between resolution and response initialization.
            response_class = SharedFileResponse if download else SharedVideoResponse if video else SharedImageResponse
            return response_class(opened["file_fd"], byte_size=opened["size"],
                content_type=opened["content_type"], filename=opened["filename"], request=request,
                reauthorize=reauthorize, extra_headers=HEADERS, file_revision=opened.get("file_revision"))
        except (OSError, ValueError):
            raise HTTPException(404, "Shared attachment is unavailable") from None

    @router.api_route("/interactive-chat/{share_id}/media/{handle}", methods=["GET", "HEAD"], include_in_schema=False)
    async def video(share_id: str, handle: str, request: Request):
        return await attachment_response(share_id, handle, request, download=False)

    @router.api_route("/interactive-chat/{share_id}/files/{handle}", methods=["GET", "HEAD"], include_in_schema=False)
    async def download_file(share_id: str, handle: str, request: Request):
        return await attachment_response(share_id, handle, request, download=True)

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
        await auth(request, share_id, write=True)
        task = None
        data = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        try:
            name = unquote(request.headers.get("x-chat-filename", ""))
            media_type = request.headers.get("content-type", "application/octet-stream").split(";")[0]
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_upload_bytes:
                    raise HTTPException(413, "File exceeds this server's upload size limit")
                writing = asyncio.create_task(asyncio.to_thread(data.write, chunk))
                try:
                    await asyncio.shield(writing)
                except asyncio.CancelledError:
                    # Keep the temporary file owned until its disk write ends.
                    while not writing.done():
                        try:
                            await asyncio.shield(writing)
                        except asyncio.CancelledError:
                            continue
                    writing.result()
                    raise
            data.seek(0)
            async def commit_upload():
                try:
                    async with share_lock(share_id):
                        grant, token = await auth(request, share_id, write=True)
                        try:
                            upload_id = await worker(lambda: store().reserve_upload(share_id, token, name=name, media_type=media_type, byte_size=size))
                        except ValidationError as exc:
                            raise HTTPException(400, str(exc)) from None
                        try:
                            private_ref = await save_upload(grant["session_id"], share_id, name, media_type, data)
                            await worker(lambda: store().complete_upload(share_id, token, upload_id, private_ref))
                        except Exception:
                            raise HTTPException(503, "File upload could not be confirmed; inspect this chat before uploading again") from None
                        return {"id": upload_id, "name": name, "media_type": media_type, "byte_size": size}
                finally:
                    data.close()
            task = asyncio.create_task(commit_upload())
            def finished(_):
                if not task.cancelled():
                    task.exception()
            task.add_done_callback(finished)
            return result(await asyncio.shield(task), 201)
        finally:
            if task is None:
                data.close()

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
                json.dumps(public_result, ensure_ascii=False, allow_nan=False)
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
                    json.dumps(public_result, ensure_ascii=False, allow_nan=False)
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
        grant, _ = await auth(request, share_id)
        async def stream():
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
            except Unavailable:
                yield "event: unavailable\ndata: {}\n\n"
            except HTTPException as exc:
                if exc.status_code in {401, 403, 404, 410}:
                    yield "event: unavailable\ndata: {}\n\n"
                # A failed snapshot or busy ledger does not revoke access.
                # End the response so EventSource's existing reconnect path
                # can obtain fresh state once the server is available again.
        return SharedChatStreamResponse(stream(), media_type="text/event-stream", headers={**HEADERS, "X-Accel-Buffering": "no"})

    return router
