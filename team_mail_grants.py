"""Bounded, secret-free durable chat-to-server mail grant projections.

Only server-authored admission code may create these records. Runtime authority
is deliberately absent: every use must intersect a fresh live capability.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

ROUTES_KEY = "provider_team_mail_routes"
PENDING_KEY = "_pending_provider_team_mail_grant"
MAX_ROUTES = 16
ROUTE_RE = re.compile(r"mailgrant_[0-9a-f]{32}")
REVISION_RE = re.compile(r"rev_[0-9a-f]{32}")
ADMISSION_RE = re.compile(r"grant_admission_[0-9a-f]{32}")


def text(value: Any, limit: int = 256) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= limit and not any(
        ord(char) < 32 for char in value
    ) else ""


def normalize_routes(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_ROUTES:
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    route_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return []
        route_id, revision = text(raw.get("route_id")), text(raw.get("revision"))
        team_id, target_id = text(raw.get("team_id")), text(raw.get("target_id"))
        binding = raw.get("durable_server_binding")
        # Runtime supplies a strictly projected identity-only binding. Keep
        # this parser independent of credentials and transient run authority.
        if (not ROUTE_RE.fullmatch(route_id) or not REVISION_RE.fullmatch(revision)
                or not team_id or not target_id or (team_id, target_id) in seen
                or route_id in route_ids or raw.get("recipient_kind") != "server"
                or not isinstance(binding, dict)
                or set(binding) != {"version", "team_id", "hub_id", "target_id", "server_identity", "lifecycle_id"}
                or type(binding.get("version")) is not int or binding["version"] != 1
                or any(not text(binding.get(key), 512) for key in (
                    "team_id", "hub_id", "target_id", "server_identity", "lifecycle_id"))
                or binding["team_id"] != team_id or binding["target_id"] != target_id
                or not text(raw.get("display_name"))):
            return []
        seen.add((team_id, target_id))
        route_ids.add(route_id)
        result.append({
            "route_id": route_id, "revision": revision,
            "team_id": team_id, "target_id": target_id,
            "recipient_kind": "server", "display_name": raw["display_name"],
            "durable_server_binding": dict(binding),
            "created_at": text(raw.get("created_at"), 40),
            "updated_at": text(raw.get("updated_at"), 40),
        })
    return result


def snapshot(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_ROUTES * 2:
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return []
        if raw.get("kind") == "legacy_reference":
            if (set(raw) != {"kind", "team_id", "target_id", "display_name"}
                    or not all(text(raw.get(key)) for key in ("team_id", "target_id", "display_name"))):
                return []
            key = "legacy:" + raw["team_id"] + "\0" + raw["target_id"]
            if key in seen:
                return []
            seen.add(key)
            result.append(dict(raw))
            continue
        route_id, revision = text(raw.get("route_id")), text(raw.get("revision"))
        if not ROUTE_RE.fullmatch(route_id) or not REVISION_RE.fullmatch(revision) or route_id in seen:
            return []
        seen.add(route_id)
        result.append({"route_id": route_id, "revision": revision})
    return result


def admission_snapshot(mutation: dict[str, Any] | None, session: dict[str, Any] | None) -> list[dict[str, str]]:
    return snapshot([
        *snapshot(mutation["after"] if mutation else live_routes(session)),
        *((mutation or {}).get("legacy_references") or []),
    ])


def pending(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or not ADMISSION_RE.fullmatch(text(value.get("admission_id"))):
        return None
    if value.get("event_type") not in {"turn_started", "turn_queued"}:
        return None
    before, after = normalize_routes(value.get("before")), normalize_routes(value.get("after"))
    if before != value.get("before") or after != value.get("after") or before == after:
        return None
    return {"admission_id": value["admission_id"], "event_type": value["event_type"],
            "before": before, "after": after}


def live_routes(session: dict[str, Any] | None) -> list[dict[str, Any]]:
    session = session or {}
    if session.get("archived"):
        return []
    routes = normalize_routes(session.get(ROUTES_KEY, []))
    if session.get(PENDING_KEY) is None:
        return routes
    staged = pending(session[PENDING_KEY])
    # A malformed or concurrently replaced marker cannot prove permission.
    return deepcopy(staged["before"]) if staged and routes == staged["after"] else []


def intersect(session: dict[str, Any] | None, ceiling: Any) -> list[dict[str, Any]]:
    allowed = {(item["route_id"], item["revision"]) for item in snapshot(ceiling) if "route_id" in item}
    return [route for route in live_routes(session) if (route["route_id"], route["revision"]) in allowed]


def settle(session: dict[str, Any], admission_id: str, *, accepted: bool) -> bool:
    staged = pending(session.get(PENDING_KEY))
    if staged is None or staged["admission_id"] != admission_id:
        return False
    if normalize_routes(session.get(ROUTES_KEY)) != staged["after"]:
        # Never resurrect an older revision over a later policy mutation.
        session[ROUTES_KEY] = []
    elif not accepted:
        session[ROUTES_KEY] = deepcopy(staged["before"])
    session.pop(PENDING_KEY, None)
    return True
