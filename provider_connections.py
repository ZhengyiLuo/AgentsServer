"""Private endpoint settings and immutable, explicitly selected chat bindings."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import stat
import tempfile
import threading
import hashlib
import re
import time
import uuid
import subprocess
import sys

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from codex_provider import validate_selection

MAX_BODY_BYTES = 16 * 1024
PROTOCOLS = {"claude": {"anthropic"}, "opencode": {"anthropic", "chat_completions", "responses"}}
RESULTS = {"verified", "authentication_failed", "rate_limited", "unsupported", "connection_failed", "invalid_response", "model_required"}


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
    selected = validate_selection({"base_url": value["base_url"], "api_key": value["api_key"],
                                   "model": None if value["model"] == "" else value["model"]})
    selected.setdefault("model", None)
    if selected["base_url"].lower().endswith("/messages"):
        raise HTTPException(400, "Enter the base URL, without an API operation suffix.")
    return {**selected, "protocol": value["protocol"], "auth_header": value["auth_header"],
            "expected_revision": revision(value["expected_revision"])}


class ConnectionStore:
    """Private, atomic, revision-fenced records; API responses never return keys."""
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.catalog_cache = {}

    def _directory(self, create=False):
        if self.root.is_symlink() or self.root.parent.is_symlink():
            raise ValueError("unsafe directory")
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.exists():
            info = self.root.stat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("non-private directory")

    def read(self, backend: str, credential_id: str | None = None) -> dict:
        backend_name(backend)
        if credential_id is not None and not re.fullmatch(r"[a-f0-9]{32}", credential_id):
            raise HTTPException(409, "Invalid chat endpoint binding.")
        with self.lock:
            try:
                self._directory()
                try:
                    name = f"{backend}-{credential_id}" if credential_id else backend
                    fd = os.open(self.root / f"{name}.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
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
        return {"backend": backend, "scope": "per_chat", "revision": value["revision"],
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

    def bind(self, session: dict) -> dict:
        """Pin once. A later settings replacement/forget cannot change a chat."""
        if session.get("provider_connection") != "custom":
            return {}
        backend = backend_name(session.get("backend"))
        with self.lock:
            credential_id = session.get("provider_connection_revision")
            value = self.read(backend, credential_id)
            if not value.get("configured") or value.get("last_result") != "verified":
                raise HTTPException(409, "Connect this custom API in AI Providers before creating a chat.")
            if not credential_id:
                credential_id = uuid.uuid4().hex
                self._directory(create=True)
                path = self.root / f"{backend}-{credential_id}.json"
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(value, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
            return {**value, "credential_id": credential_id, "model": session.get("model") or value.get("model")}

    def for_session(self, session: dict) -> dict:
        if session.get("provider_connection") != "custom":
            return {}
        if not session.get("provider_connection_revision"):
            raise HTTPException(409, "This chat has no saved custom API binding; create a new chat.")
        return self.bind(session)

    def catalog(self, backend: str, *, installed: bool = True, session: dict | None = None) -> dict:
        try:
            value = self.for_session(session) if session else self.read(backend)
            configured = value.get("configured") is True and value.get("last_result") == "verified"
            models = []
            if configured:
                cache_key = (backend, value["revision"])
                cached = self.catalog_cache.get(cache_key)
                if cached and time.monotonic() - cached[0] < 300:
                    models = cached[1]
                elif session is None:  # Public session projection never makes network calls.
                    models = discover_models(value)
                    self.catalog_cache[cache_key] = (time.monotonic(), models)
                    if len(self.catalog_cache) > 32:
                        self.catalog_cache.pop(next(iter(self.catalog_cache)))
                model = value.get("model")
                if model and not any(item["value"] == model for item in models):
                    models = [{"value": model, "label": model}, *models]
            return {"configured": configured, "available": configured and installed,
                    "models": models, "efforts": [], "default_model": value.get("model"),
                    "model": value.get("model"), "default_effort": None, "base_url": value.get("base_url")}
        except HTTPException:
            return {"configured": False, "available": False, "models": [], "efforts": [], "model": None, "base_url": None}

    def claude_overrides(self, session: dict) -> tuple[dict, str | None]:
        value = self.for_session(session)
        if not value:
            return {}, None
        model = require_model(value)
        env = {"ANTHROPIC_BASE_URL": value["base_url"].removesuffix("/v1"),
               "ANTHROPIC_API_KEY": value["api_key"] if value["auth_header"] == "x-api-key" else "",
               "ANTHROPIC_AUTH_TOKEN": value["api_key"] if value["auth_header"] == "bearer" else "",
               "CLAUDE_CODE_OAUTH_TOKEN": "", "CLAUDE_CODE_USE_BEDROCK": "0",
               "CLAUDE_CODE_USE_VERTEX": "0", "CLAUDE_CODE_USE_FOUNDRY": "0",
               "ANTHROPIC_MODEL": model, "ANTHROPIC_SMALL_FAST_MODEL": model,
               "ANTHROPIC_DEFAULT_OPUS_MODEL": model, "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
               "ANTHROPIC_DEFAULT_HAIKU_MODEL": model}
        if type(session.get("subagent_limit")) is int:
            env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] = str(session["subagent_limit"])
        # Flag settings outrank project/user settings. Pass a private file path,
        # never a key-bearing JSON argument visible in process listings.
        content = json.dumps({"env": env, "apiKeyHelper": ""}, sort_keys=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        path = self.root / f"claude-runtime-{digest}.json"
        with self.lock:
            self._directory(create=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                    raise HTTPException(503, "Custom API runtime settings are unavailable.") from None
            else:
                with os.fdopen(fd, "w") as stream:
                    stream.write(content)
            return env, str(path)

    def opencode_overrides(self, session: dict, env: dict) -> dict:
        value = self.for_session(session)
        if not value:
            return {}
        model = require_model(value)
        config = json.loads(env.get("OPENCODE_CONFIG_CONTENT") or "{}")
        adapter = {"anthropic": "@ai-sdk/anthropic", "chat_completions": "@ai-sdk/openai-compatible", "responses": "@ai-sdk/openai"}[value["protocol"]]
        options = {"baseURL": value["base_url"], "apiKey": "{env:AGENTSDOCK_CUSTOM_API_KEY}"}
        if value["protocol"] == "anthropic" and value["auth_header"] == "bearer":
            options["headers"] = {"Authorization": "Bearer {env:AGENTSDOCK_CUSTOM_API_KEY}", "x-api-key": ""}
        config.setdefault("provider", {})["agentsdock_custom"] = {"npm": adapter, "name": "Custom endpoint", "options": options, "models": {model: {"name": model}}}
        config["model"] = "agentsdock_custom/" + model
        config["small_model"] = config["model"]
        config["enabled_providers"] = ["agentsdock_custom"]
        return {"AGENTSDOCK_CUSTOM_API_KEY": value["api_key"], "OPENCODE_CONFIG_CONTENT": json.dumps(config)}

    def redact(self, session: dict, value):
        if session.get("provider_connection") != "custom":
            return value
        try:
            key = self.for_session(session)["api_key"]
        except HTTPException:
            return "Custom API output unavailable." if isinstance(value, str) else {"message": "Custom API output unavailable."}
        def clean(item):
            if isinstance(item, str): return item.replace(key, "<api-key>")
            if isinstance(item, list): return [clean(x) for x in item]
            if isinstance(item, dict): return {k: clean(v) for k, v in item.items()}
            return item
        return clean(value)


def require_model(value: dict) -> str:
    model = value.get("model")
    if not isinstance(model, str) or not re.fullmatch(r"[\x21-\x7e]{1,256}", model):
        raise HTTPException(409, "Choose a model for this custom endpoint before sending a message.")
    return model


def discover_models(value: dict) -> list[dict]:
    """Bounded catalog lookup; no inference, redirects, proxies or secret output."""
    base = value["base_url"]
    headers = {"anthropic-version": "2023-06-01", "Accept": "application/json"}
    headers["x-api-key" if value["auth_header"] == "x-api-key" else "Authorization"] = value["api_key"] if value["auth_header"] == "x-api-key" else "Bearer " + value["api_key"]
    target = base + ("/v1/models" if value["protocol"] == "anthropic" and not base.endswith("/v1") else "/models")
    try:
        with httpx.Client(timeout=3, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", target, headers=headers) as response:
                if response.status_code != 200: return []
                raw = bytearray()
                deadline = time.monotonic() + 5
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024 or time.monotonic() > deadline: return []
                items = json.loads(raw).get("data", [])
                if not isinstance(items, list): return []
                ids = dict.fromkeys(item["id"] for item in items[:2000] if isinstance(item, dict) and isinstance(item.get("id"), str) and re.fullmatch(r"[\x21-\x7e]{1,256}", item["id"]))
                return [{"value": model, "label": model} for model in ids]
    except Exception:
        return []


def native_credentials_present(backend: str, diagnostic: dict) -> bool:
    """Presence only, not a paid/auth-refresh probe; never return credential data."""
    if diagnostic.get("installed") is not True:
        return False
    # Claude/OpenCode successful turns may have used a chat-local custom API.
    # Their runtime-ready cache is not proof of a separate native login.
    if backend not in {"claude", "opencode"} and diagnostic.get("authenticated") is True:
        return True
    try:
        if backend == "claude":
            if any(os.environ.get(key) for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")):
                return True
            root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
            path = root / ".credentials.json"
            if path.is_file() and path.stat().st_size < 65536:
                value = json.loads(path.read_text())
                if value.get("claudeAiOauth", {}).get("accessToken"):
                    return True
            if sys.platform == "darwin":
                # Omit -w/-g: test existence without extracting a Keychain secret.
                return subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2).returncode == 0
        if backend == "opencode":
            if any(os.environ.get(key) for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENCODE_API_KEY")):
                return True
            path = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "opencode/auth.json"
            if path.is_file() and path.stat().st_size < 65536:
                value = json.loads(path.read_text())
                return isinstance(value, dict) and any(isinstance(item, dict) and (item.get("key") or item.get("access")) for item in value.values())
    except Exception:
        pass
    return False


async def probe_credentials(selected: dict) -> str:
    """Read-only key check. Public model lists alone never verify a credential."""
    base = selected["base_url"]
    protocol = selected.get("protocol", "responses")
    headers = {"Accept": "application/json", "anthropic-version": "2023-06-01"}
    if selected.get("auth_header") == "x-api-key":
        headers["x-api-key"] = selected["api_key"]
    else:
        headers["Authorization"] = "Bearer " + selected["api_key"]
    url = urlsplit(base)
    router = url.scheme == "https" and url.netloc == "openrouter.ai" and url.path in {"/api", "/api/v1"}
    target = "https://openrouter.ai/api/v1/key" if router else base + (
        "/v1/models" if protocol == "anthropic" and not base.endswith("/v1") else "/models")
    async def send():
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            async with client.stream("GET", target, headers=headers) as response:
                if response.status_code in {401, 403}: return "authentication_failed"
                if response.status_code == 429: return "rate_limited"
                if response.status_code in {404, 405}: return "model_required"
                if response.status_code != 200: return "connection_failed"
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024: return "invalid_response"
                payload = json.loads(raw)
                data = payload.get("data") if isinstance(payload, dict) else None
                if router:
                    # Do not return the key label, account metadata or spend.
                    return "verified" if isinstance(data, dict) and isinstance(data.get("label"), str) else "invalid_response"
                if not isinstance(data, list) or not all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in data):
                    return "invalid_response"
            # A gateway may expose its catalog publicly. Confirm this route
            # actually distinguishes credentials before showing a green check.
            async with client.stream("GET", target, headers={"Accept": "application/json", "anthropic-version": "2023-06-01"}) as public:
                return "verified" if public.status_code in {401, 403} else "model_required"
    try:
        return await asyncio.wait_for(send(), timeout=20)
    except (asyncio.TimeoutError, httpx.HTTPError):
        return "connection_failed"
    except (ValueError, TypeError, AttributeError, RecursionError):
        return "invalid_response"


async def probe(selected: dict) -> str:
    """One small, explicit model request; no tools/history, redirects or retries."""
    if not selected.get("model"):
        return await probe_credentials(selected)
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


def native_account_metadata(backend: str, *, env=None, cursor_executable=None, command=None) -> dict:
    """Allowlisted account display fields only; never log in or renew credentials.

    Claude's profile is cached metadata, not proof of current authentication.
    OpenCode can hold several providers and has no single email/subscription.
    """
    if backend not in {"claude", "cursor", "opencode"}:
        raise HTTPException(400, "Unsupported CLI account.")
    env = os.environ if env is None else env
    result = {"backend": backend, "email": None, "plan_type": None, "source": "unavailable"}

    def clean(value, *, email=False):
        if not isinstance(value, str) or not 1 <= len(value) <= (254 if email else 80):
            return None
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            return None
        if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
            return None
        return value

    def document(path, limit):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                    return {}
                raw = stream.read(limit + 1)
            value = json.loads(raw) if len(raw) <= limit else {}
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, RecursionError):
            return {}

    if backend == "claude":
        # Environment credentials may represent an entirely different account.
        if any(env.get(key) for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                                       "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")):
            return result
        root = Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        profile = document(root / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else Path.home() / ".claude.json", 2 * 1024 * 1024)
        account = profile.get("oauthAccount")
        oauth = document(root / ".credentials.json", 65536).get("claudeAiOauth")
        if isinstance(account, dict):
            result["email"] = clean(account.get("emailAddress"), email=True)
        if isinstance(oauth, dict) and oauth.get("accessToken"):
            result["plan_type"] = clean(oauth.get("subscriptionType"))
        if result["email"] or result["plan_type"]:
            result["source"] = "local_profile"
    elif backend == "cursor" and cursor_executable and command and not env.get("CURSOR_API_KEY"):
        from cursor_agent_client import parse_cursor_auth_status, parse_cursor_account_tier
        try:
            status = command([cursor_executable, "status"])
            if status.returncode != 0:
                return result
            parsed = parse_cursor_auth_status(status.stdout + "\n" + status.stderr, executable_name=Path(cursor_executable).name)
            if parsed.get("state") != "ready":
                return result
            result["email"] = clean(parsed.get("email"), email=True)
            result["source"] = "cli"
            about = command([cursor_executable, "about"])
            if about.returncode == 0:
                result["plan_type"] = clean(parse_cursor_account_tier(about.stdout + "\n" + about.stderr))
        except (OSError, subprocess.SubprocessError):
            pass
    return result


def create_router(*, authorize, store: ConnectionStore, check=probe, account=native_account_metadata) -> APIRouter:
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

    @router.get("/api/admin/provider-accounts/{backend}")
    async def native_account(backend: str, request: Request):
        authorize(request)
        if backend not in {"claude", "cursor", "opencode"}:
            raise HTTPException(400, "Unsupported CLI account.")
        return reply(await asyncio.to_thread(account, backend))

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
