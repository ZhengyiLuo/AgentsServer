"""Bounded Gemini Developer API discovery; never OpenAI protocol guessing."""
from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import quote, urlsplit

import httpx

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


def supports_auto(selected: dict) -> bool:
    """Only Google's API is known to support the models chosen by CLI Auto.

    Auto is a CLI routing alias, not a model returned by the REST inventory.
    Do not assume a Gemini-compatible gateway implements that model family.
    """
    url = urlsplit(selected.get("base_url") or "")
    return (url.scheme == "https" and url.netloc.lower() in {
        "generativelanguage.googleapis.com", "generativelanguage.googleapis.com:443"
    } and url.path.rstrip("/") in {"", "/v1", "/v1beta"}
        and not url.query and not url.fragment)


def default_model(selected: dict) -> str | None:
    # Preserve an explicit chat model or its immutable connection default.
    return selected.get("model") or ("auto" if supports_auto(selected) else None)


def model_options(selected: dict, models: list[dict]) -> list[dict]:
    if supports_auto(selected):
        return [{"value": "auto", "label": "Auto (Gemini CLI)"},
                *[item for item in models if item["value"] != "auto"]]
    return models


def api_base(selected):
    base = selected["base_url"].rstrip("/")
    return base if base.endswith(("/v1", "/v1beta")) else base + "/v1beta"


def parse_models(value):
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        raise ValueError("Invalid Gemini model catalog")
    result = []
    for item in value["models"]:
        if not isinstance(item, dict):
            raise ValueError("Invalid Gemini model entry")
        model = str(item.get("name") or "").removeprefix("models/")
        if not MODEL.fullmatch(model) or "generateContent" not in item.get("supportedGenerationMethods", []):
            continue
        label = item.get("displayName") or model
        if not isinstance(label, str) or not label or len(label) > 256 or any(ord(c) < 32 or ord(c) == 127 for c in label):
            label = model
        result.append({"value": model, "label": label})
    return result


def catalog(selected):
    models, seen = [], set()
    deadline = time.monotonic() + 10
    status = "unavailable"
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as client:
            page = None
            for _ in range(5):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                params = {"pageSize": "100", **({"pageToken": page} if page else {})}
                with client.stream("GET", api_base(selected) + "/models", params=params,
                        headers={"x-goog-api-key": selected["api_key"]}, timeout=remaining) as response:
                    if response.status_code in {401, 403}: return {"models": [], "discovery_status": "authentication_failed"}
                    if response.status_code != 200: break
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 2 * 1024 * 1024 or time.monotonic() > deadline: raise ValueError()
                    value = json.loads(raw)
                    for item in parse_models(value):
                        if item["value"] not in seen:
                            seen.add(item["value"]); models.append(item)
                    page = value.get("nextPageToken")
                    if not page:
                        return {"models": models[:512], "discovery_status": "ready" if models else "empty"}
                    if not isinstance(page, str) or len(page) > 4096: raise ValueError()
    except (ValueError, TypeError, httpx.HTTPError):
        pass
    return {"models": models[:512], "discovery_status": "partial" if models else status}


async def probe(selected):
    async def send():
        headers = {"x-goog-api-key": selected["api_key"]}
        model = selected.get("model")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False) as client:
            if model:
                if not MODEL.fullmatch(model): return "unsupported"
                method, target = "POST", api_base(selected) + "/models/" + quote(model, safe="") + ":generateContent"
                kwargs = {"json": {"contents": [{"role": "user", "parts": [{"text": "Reply OK only."}]}],
                                   "generationConfig": {"maxOutputTokens": 256}}}
            else:
                method, target, kwargs = "GET", api_base(selected) + "/models?pageSize=1", {}
            async with client.stream(method, target, headers=headers, **kwargs) as response:
                if response.status_code in {400, 401, 403}: return "authentication_failed" if not model or response.status_code != 400 else "unsupported"
                if response.status_code == 429: return "rate_limited"
                if response.status_code != 200: return "connection_failed"
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 128 * 1024: return "invalid_response"
                value = json.loads(raw)
            if model:
                return "verified" if any(part.get("text", "").strip() for candidate in value.get("candidates", [])
                    for part in candidate.get("content", {}).get("parts", []) if not part.get("thought")) else "invalid_response"
            parse_models(value)
            # A public gateway catalog cannot prove its key was accepted.
            async with client.stream("GET", target) as public:
                return "verified" if public.status_code in {400, 401, 403} else "model_required"
    try:
        return await asyncio.wait_for(send(), 20)
    except (asyncio.TimeoutError, httpx.HTTPError):
        return "connection_failed"
    except (ValueError, TypeError, AttributeError):
        return "invalid_response"
