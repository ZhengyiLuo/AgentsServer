"""Read-only native Codex authentication; custom keys belong to codex_provider."""
from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse


MAX_BODY_BYTES = 8192
MAX_API_KEY_CHARS = 4096
AUTH_TIMEOUT_SECONDS = 30.0
BUSY_MESSAGE = "Wait for Codex chats, goals, queued turns and Side chat requests to finish before changing authentication."


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
