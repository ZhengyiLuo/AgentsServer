"""Settings-only endpoint checks. Never changes a native login or chat routing."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import threading

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from codex_provider import validate_selection, validate_model

MAX_BODY_BYTES = 16 * 1024
PROTOCOLS = {"claude": {"anthropic"}, "opencode": {"anthropic", "chat_completions", "responses"}}
RESULTS = {"verified", "authentication_failed", "rate_limited", "unsupported", "connection_failed", "invalid_response"}


def backend_name(value: str) -> str:
    if value not in PROTOCOLS:
        raise HTTPException(400, "This runtime does not support these endpoint settings.")
    return value


def revision(value) -> int:
    if type(value) is not int or not 0 <= value < 2**53 - 1:
        raise HTTPException(400, "Refresh the saved endpoint before changing it.")
    return value


def selection(backend: str, value: object) -> dict:
    backend_name(backend)
    if not isinstance(value, dict) or set(value) != {"base_url", "api_key", "model", "protocol", "auth_header", "expected_revision"}:
        raise HTTPException(400, "Provide an endpoint, key, model, protocol and revision.")
    if not isinstance(value["protocol"], str) or not isinstance(value["auth_header"], str) or value["protocol"] not in PROTOCOLS[backend] or value["auth_header"] not in {"bearer", "x-api-key"}:
        raise HTTPException(400, "Unsupported API protocol or authentication header.")
    if value["protocol"] != "anthropic" and value["auth_header"] != "bearer":
        raise HTTPException(400, "This API protocol requires bearer authentication.")
    selected = validate_selection({key: value[key] for key in ("base_url", "api_key", "model")})
    selected["model"] = validate_model(value["model"])
    if selected["base_url"].lower().endswith("/messages"):
        raise HTTPException(400, "Enter the base URL, without an API operation suffix.")
    return {**selected, "protocol": value["protocol"], "auth_header": value["auth_header"],
            "expected_revision": revision(value["expected_revision"])}


class ConnectionStore:
    """Private, atomic, revision-fenced records; API responses never return keys."""
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()

    def _directory(self, create=False):
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise ValueError("unsafe directory")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.exists():
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("non-private directory")

    def read(self, backend: str) -> dict:
        backend_name(backend)
        with self.lock:
            try:
                self._directory()
                try:
                    fd = os.open(self.root / f"{backend}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except FileNotFoundError:
                    return {"revision": 0, "configured": False}
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                        raise ValueError("unsafe file")
                    raw = stream.read(MAX_BODY_BYTES + 1)
                    if len(raw) > MAX_BODY_BYTES:
                        raise ValueError("oversized record")
                value = json.loads(raw)
                revision(value["revision"])
                if type(value.get("configured")) is not bool:
                    raise ValueError("invalid record")
                if value["configured"]:
                    selection(backend, {key: value[key] for key in ("base_url", "api_key", "model", "protocol", "auth_header", "expected_revision")})
                    if value.get("last_result") not in RESULTS or not isinstance(value.get("checked_at"), str):
                        raise ValueError("invalid evidence")
                return value
            except Exception:
                raise HTTPException(503, "Saved endpoint settings are unavailable.") from None

    def public(self, backend: str) -> dict:
        value = self.read(backend)
        return {"backend": backend, "scope": "settings_only", "revision": value["revision"],
                "configured": value["configured"], "has_api_key": value["configured"],
                **{key: value.get(key) if value["configured"] else None for key in
                   ("base_url", "model", "protocol", "auth_header", "checked_at", "last_result")}}

    def write(self, backend: str, expected: int, selected: dict | None, result: str | None = None) -> dict:
        with self.lock:
            if self.read(backend)["revision"] != revision(expected):
                raise HTTPException(409, "Endpoint settings changed. Refresh and try again.")
            value = {"revision": expected + 1, "configured": selected is not None}
            if selected is not None:
                if result not in RESULTS:
                    raise ValueError("invalid check result")
                value.update(selection(backend, selected))
                value.update(last_result=result, checked_at=datetime.now(timezone.utc).isoformat())
            try:
                self._directory(create=True)
                target = self.root / f"{backend}.json"
                if target.is_symlink():
                    raise ValueError("unsafe target")
                fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.root)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(value, stream)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, target)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            except Exception:
                raise HTTPException(503, "Could not save endpoint settings.") from None
            return self.public(backend)


async def probe(selected: dict) -> str:
    """One small, explicit model request; no tools/history, redirects or retries."""
    protocol = selected["protocol"]
    headers = {"Content-Type": "application/json"}
    if selected["auth_header"] == "x-api-key":
        headers["x-api-key"] = selected["api_key"]
    else:
        headers["Authorization"] = "Bearer " + selected["api_key"]
    base = selected["base_url"]
    prompt = "Connection check. Reply with OK only."
    if protocol == "anthropic":
        suffix = "/messages" if base.endswith("/v1") else "/v1/messages"
        headers["anthropic-version"] = "2023-06-01"
        body = {"model": selected["model"], "max_tokens": 32, "messages": [{"role": "user", "content": prompt}]}
    elif protocol == "responses":
        suffix = "/responses"
        body = {"model": selected["model"], "max_output_tokens": 64, "input": prompt, "store": False}
    else:
        suffix = "/chat/completions"
        body = {"model": selected["model"], "max_tokens": 32, "messages": [{"role": "user", "content": prompt}]}
    async def send():
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            async with client.stream("POST", base + suffix, headers=headers, json=body) as response:
                if response.status_code in {401, 403}:
                    return "authentication_failed"
                if response.status_code == 429:
                    return "rate_limited"
                if response.status_code != 200:
                    return "unsupported" if response.status_code < 500 else "connection_failed"
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 128 * 1024:
                        return "invalid_response"
                value = json.loads(raw)
                if protocol == "anthropic":
                    valid = value.get("type") == "message" and any(block.get("type") == "text" and block.get("text", "").strip() for block in value.get("content", []))
                elif protocol == "responses":
                    valid = value.get("status") == "completed" and any(block.get("type") == "output_text" and block.get("text", "").strip()
                        for item in value.get("output", []) for block in item.get("content", []))
                else:
                    valid = any(item.get("message", {}).get("content", "").strip() for item in value.get("choices", []))
                return "verified" if valid else "invalid_response"
    try:
        return await asyncio.wait_for(send(), timeout=20)
    except (asyncio.TimeoutError, httpx.HTTPError):
        return "connection_failed"
    except (ValueError, TypeError, AttributeError, RecursionError):
        return "invalid_response"


def create_router(*, authorize, store: ConnectionStore, check=probe) -> APIRouter:
    router = APIRouter()
    locks = {name: asyncio.Lock() for name in PROTOCOLS}

    async def body(request: Request) -> dict:
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "JSON is required.")
        raw = bytearray()
        async def read():
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > MAX_BODY_BYTES:
                    raise HTTPException(413, "Endpoint request is too large.")
        try:
            await asyncio.wait_for(read(), timeout=10)
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except asyncio.TimeoutError:
            raise HTTPException(408, "Endpoint request timed out.") from None
        except (ValueError, RecursionError):
            raise HTTPException(400, "Invalid endpoint request.") from None
        finally:
            raw.clear()

    def reply(value):
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @router.get("/api/admin/provider-connections/{backend}")
    async def status(backend: str, request: Request):
        authorize(request)
        backend_name(backend)
        return reply(await asyncio.to_thread(store.public, backend))

    @router.api_route("/api/admin/provider-connections/{backend}", methods=["PUT", "DELETE"])
    @router.post("/api/admin/provider-connections/{backend}/check")
    async def change(backend: str, request: Request):
        authorize(request)
        backend_name(backend)
        payload = await body(request)
        expected = revision(payload.get("expected_revision"))
        if locks[backend].locked():
            raise HTTPException(409, "An endpoint operation is already in progress.")
        async with locks[backend]:
            current = await asyncio.to_thread(store.read, backend)
            if current["revision"] != expected:
                raise HTTPException(409, "Endpoint settings changed. Refresh and try again.")
            if request.method != "PUT" and set(payload) != {"expected_revision"}:
                raise HTTPException(400, "Only the saved revision is accepted.")
            if request.method == "DELETE":
                return reply(await asyncio.to_thread(store.write, backend, expected, None))
            selected = selection(backend, payload) if request.method == "PUT" else (
                {key: current[key] for key in ("base_url", "api_key", "model", "protocol", "auth_header", "expected_revision")}
                if current["configured"] else None)
            if selected is None:
                raise HTTPException(409, "Save an endpoint before checking it.")
            try:
                result = await check(selected)
                if request.method == "PUT" and result != "verified":
                    return reply({"ok": False, "status": result})
                configuration = await asyncio.to_thread(store.write, backend, expected, selected, result)
                return reply({"ok": result == "verified", "status": result, "configuration": configuration})
            finally:
                selected.clear()
                current.clear()
                payload.clear()

    return router
