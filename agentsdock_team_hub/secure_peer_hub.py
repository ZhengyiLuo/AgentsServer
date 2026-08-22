"""Fixed, in-process Team Hub adapter for authenticated secure peers.

The TLS gateway authenticates and authorizes the peer before constructing a
``ProxyRequest``.  This adapter never accepts a bearer token and never makes an
HTTP request: it maps the small V1 allowlist directly onto ``HubStore`` using a
synthetic, live-checked automation principal.
"""

from __future__ import annotations

import json
from collections import deque
import threading
import time
from typing import Any
from urllib.parse import parse_qsl

from .secure_peer import PeerAuthorization, ProxyRequest, ProxyResponse
from .security import canonical_json
from .store import HubError, HubStore


_TEAM_PREFIX = "/v1/teams/"
_CHANNEL_PREFIX = "/v1/channels/"
_MESSAGE_SUFFIX = "/messages"


class SecurePeerHubAdapter:
    """Translate the gateway's exact allowlist into Team Hub store calls."""

    def __init__(self, store: HubStore) -> None:
        self.store = store
        self._rate_lock = threading.Lock()
        self._rate_events: dict[tuple[str, str], deque[float]] = {}
        self._in_flight: dict[str, int] = {}

    def _admit(self, peer_id: str, *, write: bool) -> None:
        """Bound authenticated-peer CPU/concurrency before touching SQLite."""

        now = time.monotonic()
        with self._rate_lock:
            in_flight = self._in_flight.get(peer_id, 0)
            if in_flight >= 4:
                raise HubError(
                    "rate_limited",
                    "Secure peer has too many concurrent requests",
                    429,
                )
            for kind, limit in (("all", 240), ("write", 60)):
                if kind == "write" and not write:
                    continue
                key = (peer_id, kind)
                events = self._rate_events.setdefault(key, deque())
                while events and events[0] <= now - 60.0:
                    events.popleft()
                if len(events) >= limit:
                    raise HubError(
                        "rate_limited",
                        "Secure peer request limit exceeded",
                        429,
                    )
                events.append(now)
            self._in_flight[peer_id] = in_flight + 1

    def _release(self, peer_id: str) -> None:
        with self._rate_lock:
            remaining = self._in_flight.get(peer_id, 0) - 1
            if remaining > 0:
                self._in_flight[peer_id] = remaining
            else:
                self._in_flight.pop(peer_id, None)

    def provision_peer(self, peer: dict[str, Any], *, display_name: str) -> str:
        """Bind an approved peer once, before request-time read-only claims."""

        return self.store.ensure_secure_peer_service(
            peer_id=str(peer["peer_id"]),
            peer_server_identity=str(peer["peer_server_identity"]),
            team_id=str(peer["team_id"]),
            display_name=display_name,
        )

    def preflight_team(self, team_id: str) -> None:
        self.store.require_secure_peer_target_team(team_id)

    def revoke_peer(self, *, peer_id: str, team_id: str) -> None:
        self.store.revoke_secure_peer_service(peer_id=peer_id, team_id=team_id)

    def resource_team(self, resource_kind: str, resource_id: str) -> str | None:
        return self.store.secure_peer_resource_team(resource_kind, resource_id)

    @staticmethod
    def _json(status: int, value: dict[str, Any]) -> ProxyResponse:
        return ProxyResponse(
            status=status,
            headers=(("content-type", "application/json"), ("cache-control", "no-store")),
            body=canonical_json(value),
        )

    @staticmethod
    def _claims(store: HubStore, peer: PeerAuthorization):
        return store.secure_peer_claims(
            peer_id=peer.peer_id,
            peer_server_identity=peer.peer_server_identity,
            team_id=peer.team_id,
            scopes=peer.scopes,
            expires_at=peer.certificate_expires_at,
            display_name=peer.peer_display_name,
        )

    @staticmethod
    def _message_body(request: ProxyRequest) -> dict[str, Any]:
        try:
            value = json.loads(request.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HubError("invalid_request", "Request body is invalid", 422) from exc
        if not isinstance(value, dict):
            raise HubError("invalid_request", "Request body is invalid", 422)
        allowed = {
            "body",
            "body_format",
            "kind",
            "thread_root_message_id",
            "parent_message_id",
            "idempotency_key",
        }
        if not set(value).issubset(allowed) or not {"body", "idempotency_key"}.issubset(value):
            raise HubError("invalid_request", "Request body is invalid", 422)
        body = value.get("body")
        key = value.get("idempotency_key")
        body_format = value.get("body_format", "markdown")
        kind = value.get("kind", "post")
        if (
            not isinstance(body, str)
            or not 1 <= len(body.encode("utf-8", "strict")) <= 65_536
            or not isinstance(key, str)
            or key != key.strip()
            or not 8 <= len(key.encode("utf-8", "strict")) <= 240
            or body_format not in {"plain", "markdown"}
            or kind not in {"post", "announcement"}
        ):
            raise HubError("invalid_request", "Request body is invalid", 422)
        result = {
            "body": body,
            "body_format": body_format,
            "kind": kind,
            "thread_root_message_id": value.get("thread_root_message_id"),
            "parent_message_id": value.get("parent_message_id"),
            "idempotency_key": key,
        }
        for field in ("thread_root_message_id", "parent_message_id"):
            item = result[field]
            if item is not None and (
                not isinstance(item, str)
                or item != item.strip()
                or not 1 <= len(item.encode("utf-8", "strict")) <= 240
            ):
                raise HubError("invalid_request", "Request body is invalid", 422)
        return result

    def forward(self, request: ProxyRequest) -> ProxyResponse:
        """Serve one already-sanitized request without network recursion."""

        admitted = False
        try:
            self._admit(
                request.peer.peer_id,
                write=request.method not in {"GET", "HEAD"},
            )
            admitted = True
            claims = self._claims(self.store, request.peer)
            path = request.path
            if request.method == "GET" and path == "/v1/health":
                result = {**self.store.health(), "peer_session_available": True}
            elif request.method == "GET" and path in {"/v1/peer-session", "/v1/session"}:
                result = self.store.session_snapshot(claims)
            elif request.method == "GET" and path == "/v1/teams":
                result = self.store.list_teams(claims)
            elif request.method == "GET" and path.startswith(_TEAM_PREFIX):
                remainder = path[len(_TEAM_PREFIX) :]
                pieces = remainder.split("/")
                team_id = pieces[0]
                if len(pieces) == 1:
                    result = self.store.get_team(claims, team_id)
                elif len(pieces) == 2 and pieces[1] == "members":
                    result = self.store.list_members(claims, team_id)
                elif len(pieces) == 2 and pieces[1] == "nodes":
                    result = self.store.list_nodes(claims, team_id)
                elif len(pieces) == 2 and pieces[1] == "channels":
                    result = self.store.list_channels(claims, team_id)
                else:  # The gateway sanitizer should make this unreachable.
                    raise HubError("not_found", "Resource not found", 404)
            elif path.startswith(_CHANNEL_PREFIX) and path.endswith(_MESSAGE_SUFFIX):
                channel_id = path[len(_CHANNEL_PREFIX) : -len(_MESSAGE_SUFFIX)]
                if request.method == "GET":
                    values = dict(parse_qsl(request.query, keep_blank_values=True))
                    result = self.store.list_messages(
                        claims,
                        channel_id,
                        int(values.get("limit", "50")),
                        (
                            int(values["before_sequence"])
                            if "before_sequence" in values
                            else None
                        ),
                    )
                elif request.method == "POST":
                    result = self.store.create_message(
                        claims,
                        channel_id,
                        self._message_body(request),
                    )
                else:
                    raise HubError("not_found", "Resource not found", 404)
            else:
                raise HubError("not_found", "Resource not found", 404)
            return self._json(200, result)
        except HubError as exc:
            return self._json(
                exc.status_code,
                {"error": {"code": exc.code, "message": exc.message}},
            )
        finally:
            if admitted:
                self._release(request.peer.peer_id)
