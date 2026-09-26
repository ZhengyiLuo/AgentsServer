"""Read-only native Codex authentication; custom keys belong to codex_provider."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import stat
import tomllib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


MAX_BODY_BYTES = 8192
MAX_API_KEY_CHARS = 4096
AUTH_TIMEOUT_SECONDS = 30.0
BUSY_MESSAGE = "Wait for Codex chats, goals, queued turns and Side chat requests to finish before changing authentication."
HANDOFF_MESSAGE = (
    "Refreshing Codex sign-in. Wait for this chat's running work, approvals, "
    "goals and background terminals to finish, then retry. Your message has "
    "not been sent to Codex."
)
_REVISION_KEY = secrets.token_bytes(32)
_MAX_AUTH_BYTES = 128 * 1024


@dataclass(frozen=True)
class LoginRevision:
    # Process-private change detection only, never authentication evidence.
    # Do not put paths, account IDs, credentials or digests in repr/logs/API.
    value: bytes = field(repr=False)


def _bounded_file(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_AUTH_BYTES:
            raise ValueError("unsupported credential file")
        raw = stream.read(_MAX_AUTH_BYTES + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > _MAX_AUTH_BYTES or (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            raise ValueError("credential file changed during read")
        return raw


def _native_login_config(env: dict[str, str], cwd: str) -> tuple[Path, dict]:
    home = env.get("HOME")
    location = env.get("CODEX_HOME")
    if not location and not home:
        raise ValueError("native home unavailable")
    if location and location.startswith("~/"):
        if not home:
            raise ValueError("native home unavailable")
        location = str(Path(home) / location[2:])
    root = Path(location) if location else Path(home) / ".codex"
    if not root.is_absolute():
        root = Path(cwd) / root
    try:
        config = tomllib.loads(_bounded_file(root / "config.toml").decode())
    except FileNotFoundError:
        config = {}
    return root, config


def native_login_handoff_supported(env: dict[str, str], *, cwd: str) -> bool:
    try:
        _, config = _native_login_config(env, cwd)
        # A process-only login cannot be recovered by starting a replacement.
        # A malformed config must not retire an otherwise working generation.
        return config.get("cli_auth_credentials_store", "file") in {"file", "keyring", "auto"}
    except (OSError, ValueError, TypeError, RecursionError):
        return False


def native_login_revision(env: dict[str, str], *, cwd: str) -> LoginRevision | None:
    """Conservative file-store signal; native Codex still owns all credentials.

    Ignore rotating access/refresh tokens and last_refresh. OIDC auth_time is
    stable across refresh but changes on an interactive authentication. Without
    it we can detect an account switch, not prove a same-account re-login;
    Recheck CLIs provides an explicit handoff for that case and keyring stores.
    Missing/malformed files are not evidence of a new login. No secret survives
    this bounded read except a process-keyed comparison digest.
    """
    try:
        root, config = _native_login_config(env, cwd)
        if config.get("cli_auth_credentials_store", "file") != "file":
            return None  # Never infer keyring/auto/ephemeral state from a leftover file.
        if any(env.get(key) for key in ("CODEX_ACCESS_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY", "OPENAI_IDENTITY_TOKEN_FILE")):
            return None
        data = json.loads(_bounded_file(root / "auth.json"))
        if not isinstance(data, dict):
            return None
        key = data.get("OPENAI_API_KEY")
        if isinstance(key, str) and key:
            identity = ("apiKey", key)
        else:
            tokens = data.get("tokens")
            if not isinstance(tokens, dict) or data.get("auth_mode") not in (None, "chatgpt"):
                return None
            encoded = tokens.get("id_token")
            if not isinstance(encoded, str) or len(encoded) > 32768:
                return None
            parts = encoded.split(".")
            if len(parts) != 3:
                return None
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            if not isinstance(claims, dict):
                return None
            details = claims.get("https://api.openai.com/auth") or {}
            if not isinstance(details, dict):
                return None
            account = tokens.get("account_id") or details.get("chatgpt_account_id")
            subject = claims.get("sub")
            if not isinstance(account, str) or not account or not isinstance(subject, str) or not subject:
                return None
            auth_time = claims.get("auth_time")
            if auth_time is not None and (isinstance(auth_time, bool) or not isinstance(auth_time, int)):
                return None
            # Unverified claims are a change hint, never permission/account proof.
            identity = ("chatgpt", account, subject, auth_time)
        raw = json.dumps((str(root.resolve()), identity), separators=(",", ":")).encode()
        return LoginRevision(hmac.new(_REVISION_KEY, raw, hashlib.sha256).digest())
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def capability(*, available: bool) -> dict:
    return {"available": available, "version": 1, "native_only": True,
            "api_key_login": False, "max_api_key_chars": MAX_API_KEY_CHARS}


def account_summary(result: object) -> dict:
    """Project only account kind and bounded display metadata; never return tokens."""
    if not isinstance(result, dict) or not isinstance(result.get("requiresOpenaiAuth"), bool):
        raise HTTPException(502, "Codex returned an invalid authentication status.")
    account = result.get("account")
    if account is not None and not isinstance(account, dict):
        raise HTTPException(502, "Codex returned an invalid authentication status.")
    mode = "none" if account is None else account.get("type")
    if mode not in ("none", "apiKey", "chatgpt"):
        mode = "other"
    email = plan = None
    if mode == "chatgpt":
        raw_email = account.get("email")
        if isinstance(raw_email, str) and len(raw_email) <= 254 and all(33 <= ord(char) <= 126 for char in raw_email) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", raw_email):
            email = raw_email
        raw_plan = account.get("planType")
        if raw_plan in ("free", "go", "plus", "pro", "team", "business", "enterprise", "edu", "unknown"):
            plan = raw_plan
    return {"available": True, "auth_mode": mode, "email": email,
            "plan_type": plan, "requires_openai_auth": result["requiresOpenaiAuth"]}


def validate_api_key(value: object) -> str:
    # Manual validation deliberately never serializes an invalid secret input,
    # unlike a framework validation error's standard `input` property.
    if not isinstance(value, dict) or set(value) != {"api_key"}:
        raise HTTPException(400, "Provide only an API key.")
    key = value["api_key"]
    if not isinstance(key, str) or not 1 <= len(key) <= MAX_API_KEY_CHARS or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise HTTPException(400, "API key must contain 1 to 4096 printable characters without whitespace.")
    return key


async def read_account(manager) -> dict:
    try:
        result = await manager.request("account/read", {"refreshToken": False}, timeout=AUTH_TIMEOUT_SECONDS)
    except Exception:
        raise HTTPException(503, "Codex authentication status is unavailable. Check Codex on this server and refresh.") from None
    return account_summary(result)


def create_router(*, authorize, operation, available) -> APIRouter:
    router = APIRouter()

    @router.get("/api/admin/codex/auth")
    async def status(request: Request):
        authorize(request)
        if not available():
            return JSONResponse({"available": False, "auth_mode": "none", "email": None,
                "plan_type": None, "requires_openai_auth": True,
                "message": "Codex authentication controls require the app-server transport."},
                headers={"Cache-Control": "no-store"})
        try:
            async with operation(mutate=False) as manager:
                result = await read_account(manager)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "Codex authentication status is unavailable.") from None
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/api/admin/codex/auth/api-key")
    async def login(request: Request):
        authorize(request)
        # Older clients must not replace the login shared by every normal
        # Codex chat and the host CLI. Do not read a key or open the manager.
        raise HTTPException(409,
            "API key sign-in is disabled because it changes the shared Codex CLI login. "
            "Configure Custom endpoint with its base URL, model and API key, then select "
            "Codex · Custom endpoint for a new chat. Manage normal Codex sign-in in the server's CLI.",
            headers={"Cache-Control": "no-store"})

    return router
