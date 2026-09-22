"""Opt-in chat snapshots: authenticated management, isolated public HTML.

No background tasks, live streams, provider calls, or public session APIs.
All disk/HTML work runs in bounded worker admissions off the server event loop.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from public_chat_shares import (
    PublicChatShareStore, PublicChatShareUnavailable, PublicChatShareValidationError,
    SHARE_ID_PATTERN, TOKEN_PATTERN, public_chat_share_headers, render_public_chat_html,
    render_public_chat_unlock_html,
)
from public_chat_transcript import PublicTranscriptError
from shared_chat_video_stream import SharedVideoResponse
from shared_chat_videos import SharedVideoUnavailable

PUBLIC_SHARE_PATH_RE = re.compile(r'''(/share/)[^/?\s"']+''')
SNAPSHOT_COOKIE = "__Secure-AgentsDock-View"
HTTP_SNAPSHOT_COOKIE = "AgentsDock-View"
WARNING = (
    "Anyone with this link and its access token can read and copy this snapshot. "
    "The optional token-in-link URL opens it directly. Review it for secrets "
    "before sharing. New messages are not added. Explicitly attached or published videos are included; "
    "other files, tools, and private runtime instructions are excluded. Revocation cannot erase copies already saved."
)


def redact_public_share_path(value: str) -> str:
    return PUBLIC_SHARE_PATH_RE.sub(r"\1<redacted>", value)


def chat_share_origin(base: str) -> str:
    """Use the browser's canonical origin spelling when binding capabilities."""
    if not isinstance(base, str) or not base:
        raise PublicChatShareValidationError("Chat link must use an HTTP or HTTPS origin")
    try:
        parsed = urlsplit(base)
        parsed.port  # Reject malformed/out-of-range ports before persistence.
    except (TypeError, ValueError):
        raise PublicChatShareValidationError("Chat link must use an HTTP or HTTPS origin") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"} or "\\" in base or any(c.isspace() for c in base)):
        raise PublicChatShareValidationError("Chat link must use an HTTP or HTTPS origin")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise PublicChatShareValidationError("Invalid chat link hostname") from None
    if ":" in hostname:
        try:
            if "%" in hostname:
                raise ValueError("Scoped IPv6 is not a browser origin")
            hostname = ipaddress.IPv6Address(hostname).compressed
        except ValueError:
            raise PublicChatShareValidationError("Invalid chat link hostname") from None
        host = f"[{hostname}]"
    else:
        host = hostname
    port = parsed.port
    suffix = f":{port}" if port is not None and port != (443 if parsed.scheme == "https" else 80) else ""
    return f"{parsed.scheme}://{host}{suffix}"


def public_share_url(base: str, token: str) -> str | None:
    return chat_share_origin(base) + "/share/" + token if base else None


def create_public_chat_share_router(
    *, storage_root, authorize, session_exists, load_transcript, public_base_url, open_video=None,
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

    async def request_bytes(request, *, limit=8192):
        async def collect():
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > limit:
                    raise HTTPException(413, "Share request is too large")
                data.extend(chunk)
            return data
        try:
            # asyncio.timeout is unavailable on supported Python 3.10 hosts.
            return await asyncio.wait_for(collect(), timeout=5)
        except asyncio.TimeoutError:
            raise HTTPException(408, "Share request body was not received") from None

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise HTTPException(415, "Use application/json")
        data = await request_bytes(request)
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
        if set(value) - {"confirmed_public", "through_bytes", "digest", "title", "expires_at", "base_url"}:
            raise HTTPException(400, "Unknown share option")
        if value.get("confirmed_public") is not True:
            raise HTTPException(400, "Public sharing must be explicitly confirmed")
        reviewed = "through_bytes" in value or "digest" in value
        if reviewed and (type(value.get("through_bytes")) is not int or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("digest", "")))):
            raise HTTPException(400, "Snapshot boundary and digest must be supplied together")
        if "base_url" in value and (not isinstance(value["base_url"], str) or not value["base_url"]):
            raise HTTPException(400, "Invalid chat link origin")
        def publish():
            # Validate configuration before persisting any public capability.
            base = chat_share_origin(value.get("base_url") or public_base_url() or str(request.base_url).rstrip("/"))
            def capture(message_sink):
                snapshot = load_transcript(session_id, value.get("through_bytes"), message_sink=message_sink)
                if reviewed and not hmac.compare_digest(snapshot["digest"], value["digest"]):
                    raise PublicTranscriptError("Chat changed; preview it again")
            share = store(create=True).create_streamed_share(session_id, capture,
                title=value.get("title"), expires_at=value.get("expires_at"))
            token = share.pop("token")
            share.pop("session_id", None)
            path = "/shared-chat/" + share["share_id"]
            return {**share, "path": path, "url": base + path, "access_token": token,
                    "token_url": public_share_url(base, token), "warning": WARNING}
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

    def unlock_page(share_id, *, invalid=False, status=200):
        return Response(render_public_chat_unlock_html(share_id, invalid_token=invalid), status_code=status,
                        headers=public_chat_share_headers(allow_unlock_form=True))

    def cookie_name(request):
        return SNAPSHOT_COOKIE if request.url.scheme == "https" else HTTP_SNAPSHOT_COOKIE

    def cookie_token(request):
        cookies = [part.strip().partition("=")[2]
            for header in request.headers.getlist("cookie") for part in header.split(";")
            if part.strip().partition("=")[0] == cookie_name(request)]
        if len(cookies) != 1 or TOKEN_PATTERN.fullmatch(cookies[0]) is None:
            raise PublicChatShareUnavailable()
        return cookies[0]

    def requested_page(request):
        if not request.url.query:
            return None
        value = parse_qs(request.url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=1)
        if set(value) != {"page"} or len(value["page"]) != 1 or re.fullmatch(r"[0-9]{1,10}", value["page"][0]) is None:
            raise ValueError("Invalid page")
        page = int(value["page"][0])
        if page > 2**31 - 1:
            raise ValueError("Invalid page")
        return page

    def render_page(value, path):
        return render_public_chat_html(value["snapshot"], page=value["page"], page_count=value["page_count"],
            message_count=value["message_count"], navigation_base=path)

    def last_page_redirect(path, page):
        return Response(status_code=303, headers={**public_chat_share_headers(),
            "Location": f"{path}?page={page}#conversation-end"})

    @router.api_route("/shared-chat/{share_id}", methods=["GET", "HEAD"], include_in_schema=False)
    async def token_entry(share_id: str, request: Request):
        headers = public_chat_share_headers()
        try:
            page = requested_page(request)
        except ValueError:
            return Response("Shared conversation unavailable.", status_code=404, headers=headers)
        if SHARE_ID_PATTERN.fullmatch(share_id) is None:
            return Response("Shared conversation unavailable.", status_code=404, headers=headers)
        cookies = [part.strip().partition("=")[2]
            for header in request.headers.getlist("cookie") for part in header.split(";")
            if part.strip().partition("=")[0] == cookie_name(request)]
        value = None
        if len(cookies) == 1 and TOKEN_PATTERN.fullmatch(cookies[0]):
            try:
                value = await worker(lambda: store().get_snapshot_page(cookies[0], share_id=share_id, page=page), public=True)
            except PublicChatShareUnavailable:
                pass
            except Exception:
                return Response("Shared conversation temporarily unavailable.", status_code=503, headers=headers)
        if value is None:
            response = unlock_page(share_id)
            if request.method == "HEAD":
                response.body = b""
            return response
        if page is None:
            return last_page_redirect(request.url.path, value["page"])
        try:
            content = await worker(lambda: render_page(value, request.url.path), public=True)
        except Exception:
            return Response("Shared conversation temporarily unavailable.", status_code=503, headers=headers)
        return Response(content if request.method == "GET" else b"", headers=headers)

    @router.post("/shared-chat/{share_id}/unlock", include_in_schema=False)
    async def unlock(share_id: str, request: Request):
        headers = public_chat_share_headers()
        if SHARE_ID_PATTERN.fullmatch(share_id) is None or request.url.query:
            return Response("Shared conversation unavailable.", status_code=404, headers=headers)
        # Native form submissions carry Origin. Reject missing/null/ambiguous
        # origins and cross-site requests before reading a submitted token.
        try:
            expected = chat_share_origin(str(request.base_url))
        except PublicChatShareValidationError:
            return unlock_page(share_id, invalid=True, status=403)
        if (request.headers.getlist("origin") != [expected]
                or request.headers.get("sec-fetch-site") in {"cross-site", "same-site"}):
            return unlock_page(share_id, invalid=True, status=403)
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            return unlock_page(share_id, invalid=True, status=415)
        try:
            value = parse_qs(bytes(await request_bytes(request, limit=1024)).decode("utf-8"),
                             keep_blank_values=True, strict_parsing=True, max_num_fields=2)
            if (set(value) - {"access_token", "remember"} or len(value.get("access_token", [])) != 1
                    or "remember" in value and value["remember"] != ["1"]):
                raise ValueError("Invalid unlock fields")
            token = value["access_token"][0]
            if TOKEN_PATTERN.fullmatch(token) is None:
                raise ValueError("Invalid token")
            page = await worker(lambda: store().get_snapshot_page(token, share_id=share_id), public=True)
        except (ValueError, UnicodeError, PublicChatShareUnavailable):
            return unlock_page(share_id, invalid=True, status=403)
        except HTTPException as exc:
            return unlock_page(share_id, invalid=True, status=exc.status_code)
        except Exception:
            return Response("Shared conversation temporarily unavailable.", status_code=503, headers=headers)
        path = "/shared-chat/" + share_id
        response = last_page_redirect(path, page["page"])
        response.set_cookie(cookie_name(request), token, secure=request.url.scheme == "https", httponly=True,
                            samesite="strict", path=path, max_age=30 * 24 * 60 * 60 if "remember" in value else None)
        return response

    @router.api_route("/share/{token}", methods=["GET", "HEAD"], include_in_schema=False)
    async def view(token: str, request: Request):
        headers = public_chat_share_headers()
        try:
            page = requested_page(request)
        except ValueError:
            return Response("Shared conversation unavailable.", status_code=404, headers=headers)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return Response("Shared conversation unavailable.", status_code=404, headers=headers)
        try:
            value = await worker(lambda: store().get_snapshot_page(token, page=page), public=True)
            if page is None:
                return last_page_redirect(request.url.path, value["page"])
            content = await worker(lambda: render_page(value, request.url.path), public=True)
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

    async def video_response(request, token, page, message_index, video_index, *, share_id=None):
        headers = {**public_chat_share_headers(), "Cross-Origin-Resource-Policy": "same-origin"}
        file_fd = None
        try:
            if open_video is None or request.url.query:
                raise PublicChatShareUnavailable()
            expected_origin = chat_share_origin(str(request.base_url))
            origins = request.headers.getlist("origin")
            fetch_sites = request.headers.getlist("sec-fetch-site")
            if (origins and origins != [expected_origin] or len(fetch_sites) > 1
                    or any(site in {"cross-site", "same-site"} for site in fetch_sites)):
                raise PublicChatShareUnavailable()
            indices = (page, message_index, video_index)
            if any(re.fullmatch(r"[0-9]{1,10}", index) is None for index in indices):
                raise PublicChatShareUnavailable()
            value = await worker(lambda: store().get_snapshot_video(token, share_id=share_id,
                page=int(page), message_index=int(message_index), video_index=int(video_index)), public=True)
            descriptor = value["video"]
            opened = await open_video(value["session_id"], descriptor["id"])
            file_fd = opened.get("file_fd")
            if (type(file_fd) is not int or file_fd < 0
                    or any(opened.get(key) != descriptor[key] for key in ("size", "content_type", "filename"))):
                raise PublicChatShareUnavailable()

            async def reauthorize():
                try:
                    await worker(lambda: store().authorize_access(token, share_id=share_id), public=True)
                except PublicChatShareUnavailable:
                    raise HTTPException(404, "Shared video unavailable") from None

            response_fd, file_fd = file_fd, None  # Constructor owns it even on validation failure.
            response = SharedVideoResponse(response_fd, byte_size=opened["size"],
                content_type=opened["content_type"], filename=opened["filename"], request=request,
                reauthorize=reauthorize, file_revision=opened.get("file_revision"),
                extra_headers={key: value for key, value in headers.items() if key != "Content-Type"})
            return response
        except (PublicChatShareUnavailable, PublicChatShareValidationError, SharedVideoUnavailable):
            return Response("Shared video unavailable.", status_code=404, headers=headers)
        except HTTPException as exc:
            status = exc.status_code if exc.status_code in {404, 416, 503} else 404
            return Response("Shared video unavailable.", status_code=status, headers={**headers, **(exc.headers or {})})
        except Exception:
            return Response("Shared video temporarily unavailable.", status_code=503, headers=headers)
        finally:
            if type(file_fd) is int and file_fd >= 0:
                os.close(file_fd)

    @router.api_route("/shared-chat/{share_id}/media/{page}/{message_index}/{video_index}",
                      methods=["GET", "HEAD"], include_in_schema=False)
    async def common_video(share_id: str, page: str, message_index: str, video_index: str, request: Request):
        try:
            token = cookie_token(request)
        except PublicChatShareUnavailable:
            return Response("Shared video unavailable.", status_code=404, headers=public_chat_share_headers())
        return await video_response(request, token, page, message_index, video_index, share_id=share_id)

    @router.api_route("/share/{token}/media/{page}/{message_index}/{video_index}",
                      methods=["GET", "HEAD"], include_in_schema=False)
    async def bearer_video(token: str, page: str, message_index: str, video_index: str, request: Request):
        return await video_response(request, token, page, message_index, video_index)

    return router
