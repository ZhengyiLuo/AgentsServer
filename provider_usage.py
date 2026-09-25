"""Read-only provider allowance observations, separate from chat token usage.

Codex exposes an account snapshot and update notifications. Claude exposes
individual rate-limit events only; an omitted percentage never means zero.
No credentials or provider payloads are persisted by this module.
"""
from __future__ import annotations

import asyncio
import copy
import math
import re
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def unavailable(backend: str, reason: str = "not_reported", *, account_kind: str = "unknown") -> dict:
    return {"backend": backend, "status": "unavailable", "source": None,
            "account_kind": account_kind, "observed_at": None, "windows": [], "reason": reason}


def _number(value: Any, *, minimum: float = 0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) and value >= minimum else None


def _text(value: Any, limit: int = 100) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    return value if all(char.isprintable() for char in value) else None


def _credits(value: Any) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("hasCredits"), bool) or not isinstance(value.get("unlimited"), bool):
        return None
    # The native balance has no currency field. Keep its decimal representation;
    # never infer dollars or return arbitrary provider text as a balance.
    balance = value.get("balance")
    if not isinstance(balance, str) or not re.fullmatch(r"\d{1,20}(?:\.\d{1,12})?", balance):
        balance = None
    return {"balance": balance, "has_credits": value["hasCredits"], "unlimited": value["unlimited"]}


def _codex_bucket(value: Any, key: str, observed_at: str, previous: dict | None = None) -> dict:
    bucket = copy.deepcopy(previous or {"windows": {}})
    if not isinstance(value, dict):
        return bucket
    label = _text(value.get("limitName")) or (_text(value.get("limitId")) if key != "codex" else None)
    for slot in ("primary", "secondary"):
        if slot not in value:
            continue
        raw = value[slot]
        used = _number(raw.get("usedPercent")) if isinstance(raw, dict) else None
        if used is None:
            # Notifications are sparse rolling updates. Null means unavailable
            # in this observation, not recovery/reset of an earlier allowance.
            continue
        old_window = bucket["windows"].get(slot) or {}
        bucket["windows"][slot] = {
            "id": f"{key}:{slot}", "label": label or old_window.get("label"), "used_percent": used,
            "resets_at": _number(raw.get("resetsAt")) if _number(raw.get("resetsAt")) is not None else old_window.get("resets_at"),
            "window_minutes": _number(raw.get("windowDurationMins"), minimum=1) or old_window.get("window_minutes"),
            "observed_at": observed_at,
        }
    credits = _credits(value.get("credits"))
    if credits is not None:
        if credits["balance"] is None and isinstance(bucket.get("credits"), dict):
            credits["balance"] = bucket["credits"].get("balance")
        bucket["credits"] = credits
    return bucket


def codex_buckets(payload: Any, observed_at: str, *, previous: dict | None = None) -> dict:
    """Merge available event fields; full reads pass no previous snapshot."""
    buckets = copy.deepcopy(previous or {})
    if not isinstance(payload, dict):
        return buckets
    many = payload.get("rateLimitsByLimitId")
    if isinstance(many, dict):
        for raw_key, value in list(many.items())[:32]:
            key = _text(raw_key)
            if key:
                buckets[key] = _codex_bucket(value, key, observed_at, buckets.get(key))
    single = payload.get("rateLimits")
    if isinstance(single, dict):
        key = _text(single.get("limitId")) or "codex"
        # The multi-bucket field is authoritative when both are supplied.
        if not isinstance(many, dict) or key not in many:
            buckets[key] = _codex_bucket(single, key, observed_at, buckets.get(key))
    return buckets


def codex_snapshot(buckets: dict, observed_at: str) -> dict:
    windows = [window for bucket in buckets.values() for window in bucket["windows"].values()]
    credits = (buckets.get("codex") or {}).get("credits")
    if credits is None:
        credits = next((bucket.get("credits") for bucket in buckets.values() if bucket.get("credits") is not None), None)
    if not windows and credits is None:
        return unavailable("codex", account_kind="chatgpt")
    return {"backend": "codex", "status": "available", "source": "codex-account",
            "account_kind": "chatgpt", "observed_at": observed_at, "windows": windows,
            **({"credits": credits} if credits is not None else {})}


def _field(value: Any, name: str) -> Any:
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


_CLAUDE_WINDOWS = {
    "five_hour": (None, 300), "seven_day": (None, 10080),
    "seven_day_opus": ("Opus", 10080),
    "seven_day_sonnet": ("Sonnet", 10080), "overage": (None, None),
}
_CLAUDE_STATUSES = {"allowed", "allowed_warning", "rejected"}


def claude_window(message: Any, observed_at: str) -> dict | None:
    info = _field(message, "rate_limit_info")
    kind = _field(info, "rate_limit_type")
    if not isinstance(kind, str) or kind not in _CLAUDE_WINDOWS:
        return None
    status = _field(info, "status")
    if not isinstance(status, str) or status not in _CLAUDE_STATUSES:
        status = None
    utilization = _number(_field(info, "utilization"))
    reset = _number(_field(info, "resets_at"))
    if status is None and utilization is None and reset is None:
        return None
    label, minutes = _CLAUDE_WINDOWS[kind]
    return {"id": kind, "label": label,
            "used_percent": round(utilization * 100, 6) if utilization is not None else None,
            "resets_at": reset, "window_minutes": minutes, "observed_at": observed_at,
            **({"status": status} if status is not None else {})}


@dataclass
class _CodexState:
    binding: tuple = ()
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    snapshot: dict | None = None
    buckets: dict = field(default_factory=dict)
    account_id: str | None = None


class ProviderUsage:
    def __init__(self) -> None:
        self._codex: weakref.WeakKeyDictionary[Any, _CodexState] = weakref.WeakKeyDictionary()
        self._claude: dict[str, tuple[str, dict]] = {}

    @staticmethod
    def _binding(manager: Any) -> tuple:
        return (manager.generation, manager.client.account_epoch, manager.ready)

    def invalidate_codex(self, manager: Any) -> None:
        state = self._codex.get(manager)
        if state is not None:
            state.binding, state.snapshot, state.buckets, state.account_id = (), None, {}, None

    async def read_codex(self, manager: Any, *, refresh: bool = False) -> dict:
        if getattr(manager, "_agentsdock_provider_revision", None):
            return unavailable("codex", "custom_endpoint", account_kind="custom")
        state = self._codex.setdefault(manager, _CodexState())
        async with state.lock:
            if not refresh and state.snapshot is not None and state.binding == self._binding(manager):
                return copy.deepcopy(state.snapshot)
            try:
                # Existing native authentication only. No login, token refresh,
                # billing API, thread, or model request is issued here.
                account = await manager.request("account/read", {"refreshToken": False})
                binding = self._binding(manager)
                raw_account = account.get("account") if isinstance(account, dict) else None
                kind = raw_account.get("type") if isinstance(raw_account, dict) else None
                if kind != "chatgpt" or account.get("requiresOpenaiAuth") is False:
                    result = unavailable("codex", "account_usage_unavailable",
                                         account_kind="api_key" if kind == "apiKey" else "custom" if kind == "chatgpt" else "unknown")
                    state.buckets = {}
                else:
                    payload = await manager.request("account/rateLimits/read", {})
                    if binding != self._binding(manager) or self._codex.get(manager) is not state:
                        return unavailable("codex", "account_changed")
                    state.buckets = codex_buckets(payload, timestamp())
                    state.account_id = _text(payload.get("accountId"), 256) if isinstance(payload, dict) else None
                    result = codex_snapshot(state.buckets, timestamp())
                state.binding, state.snapshot = binding, result
                return copy.deepcopy(result)
            except Exception:
                # Neither provider diagnostics nor old account values are a
                # usable balance. A failed refresh stays non-fatal to the chat.
                result = unavailable("codex", "temporarily_unavailable")
                state.snapshot = None
                state.buckets = {}
                state.binding = self._binding(manager)
                return result

    def observe_codex(self, manager: Any, notification: dict) -> bool:
        state = self._codex.get(manager)
        if state is None or state.snapshot is None or state.snapshot.get("account_kind") != "chatgpt":
            return False
        if getattr(manager, "_agentsdock_provider_revision", None) or state.binding != self._binding(manager):
            self.invalidate_codex(manager)
            return True
        payload = notification.get("params")
        if not isinstance(payload, dict):
            return False
        account_id = _text(payload.get("accountId"), 256)
        if state.account_id and account_id and account_id != state.account_id:
            self.invalidate_codex(manager)
            return True
        observed_at = timestamp()
        state.buckets = codex_buckets(payload, observed_at, previous=state.buckets)
        state.snapshot = codex_snapshot(state.buckets, observed_at)
        return True

    def observe_claude(self, session_id: str, generation: str | None, message: Any) -> bool:
        if not generation:
            return False
        observed_at = timestamp()
        window = claude_window(message, observed_at)
        if window is None:
            return False
        old_generation, old_windows = self._claude.get(session_id, (None, {}))
        windows = dict(old_windows) if old_generation == generation else {}
        windows[window["id"]] = window
        self._claude[session_id] = (generation, windows)
        return True

    def read_claude(self, session_id: str, generation: str | None) -> dict:
        old_generation, windows = self._claude.get(session_id, (None, {}))
        if not generation or old_generation != generation:
            self._claude.pop(session_id, None)
            return unavailable("claude")
        return {"backend": "claude", "status": "available", "source": "claude-events",
                "account_kind": "subscription", "observed_at": max(w["observed_at"] for w in windows.values()),
                "windows": copy.deepcopy(list(windows.values()))}
