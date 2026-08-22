"""Strict loopback-first HTTP boundary for AgentsDock Team Hub V1."""

import asyncio
from dataclasses import dataclass
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import threading
import time
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .auth import AuthenticationError, AuthorizationError
from .store import AccessClaims, HubError, HubStore


MAX_JSON_BODY_BYTES = 65_536
BODY_READ_TIMEOUT_SECONDS = 10.0
HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
TAILSCALE_SERVE_HEADERS_INFO = "https://tailscale.com/s/serve-headers"


@dataclass(frozen=True)
class ManagedTransportIdentity:
    kind: Literal["loopback", "tailscale_serve", "direct_ip"]
    tailnet_login: str | None = None
    tailnet_user_name: str | None = None


class _RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[tuple[str, str], tuple[int, int]] = {}

    def allow(self, peer: str, action: str, limit: int) -> bool:
        window = int(time.monotonic() // 60)
        key = (peer, action)
        with self._lock:
            prior_window, count = self._buckets.get(key, (window, 0))
            if prior_window != window:
                count = 0
            count += 1
            self._buckets[key] = (window, count)
            if len(self._buckets) > 4096:
                self._buckets = {
                    item_key: item_value
                    for item_key, item_value in self._buckets.items()
                    if item_value[0] >= window - 1
                }
                while len(self._buckets) > 4096:
                    self._buckets.pop(next(iter(self._buckets)))
            return count <= limit


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BootstrapRequest(StrictModel):
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=160)
    device_label: str = Field(min_length=1, max_length=160)


class RefreshRequest(StrictModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class RevokeSessionRequest(StrictModel):
    refresh_token: str = Field(min_length=16, max_length=512)


class InviteRequest(StrictModel):
    invitee_email: str = Field(min_length=3, max_length=320)
    role: Literal["admin", "member", "guest"]
    ttl_seconds: int = Field(default=900, ge=30, le=86_400)


class RedeemInviteRequest(StrictModel):
    token: str = Field(min_length=16, max_length=512)
    email: str = Field(min_length=3, max_length=320)
    display_name: str = Field(min_length=1, max_length=160)
    device_label: str = Field(min_length=1, max_length=160)


class AcceptInviteRequest(StrictModel):
    token: str = Field(min_length=16, max_length=512)


class RedeemRecoveryRequest(StrictModel):
    device_label: str = Field(min_length=1, max_length=160)


class NodeGrantRequest(StrictModel):
    server_identity: str = Field(min_length=8, max_length=240)
    display_name: str = Field(min_length=1, max_length=160)
    public_key: str = Field(min_length=32, max_length=16_384)
    ttl_seconds: int = Field(default=300, ge=30, le=900)


class NodeChallengeRequest(StrictModel):
    token: str = Field(min_length=16, max_length=512)
    server_identity: str = Field(min_length=8, max_length=240)
    display_name: str = Field(min_length=1, max_length=160)
    public_key: str = Field(min_length=32, max_length=16_384)


class NodeRedeemRequest(StrictModel):
    challenge_id: str = Field(min_length=8, max_length=240)
    signature: str = Field(min_length=80, max_length=128)


class ChannelRequest(StrictModel):
    kind: Literal["board", "announcements", "direct"]
    visibility: Literal["team", "private"] = "team"
    slug: str | None = Field(default=None, min_length=1, max_length=80)
    display_name: str | None = Field(default=None, min_length=1, max_length=160)
    participant_principal_ids: list[str] = Field(default_factory=list, max_length=256)
    idempotency_key: str = Field(min_length=8, max_length=240)


class MessageRequest(StrictModel):
    body: str = Field(min_length=1, max_length=65_536)
    body_format: Literal["plain", "markdown"] = "markdown"
    kind: Literal["post", "announcement"] = "post"
    thread_root_message_id: str | None = Field(default=None, min_length=1, max_length=240)
    parent_message_id: str | None = Field(default=None, min_length=1, max_length=240)
    idempotency_key: str = Field(min_length=8, max_length=240)


SecurePeerScope = Literal[
    "teamspace.read",
    "teamspace.write",
    "cross_chat.instruction",
    "cross_chat.request_reply",
]


class SecurePeerApprovalRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    sas_confirmed: Literal[True]
    expected_peer_server_identity: str = Field(min_length=8, max_length=240)
    expected_transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scopes: list[SecurePeerScope] = Field(min_length=1, max_length=4)


class SecurePeerRejectionRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_peer_server_identity: str = Field(min_length=8, max_length=240)
    expected_transcript_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, min_length=1, max_length=160)


class SecurePeerRevocationRequest(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_certificate_fingerprint: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$"
    )


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "message": message}})


def _is_loopback_peer(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _has_loopback_authority(request: Request) -> bool:
    value = _exact_ascii_header(request, b"host")
    if value is None:
        return False
    host = _host_name(value)
    if host is None:
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _host_name(authority: str) -> str | None:
    if not authority or "@" in authority or any(ord(char) < 33 or ord(char) > 126 for char in authority):
        return None
    port: str | None = None
    if authority.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\](?::([0-9]+))?", authority)
        if match is None:
            return None
        try:
            address = ipaddress.ip_address(match.group(1))
        except ValueError:
            return None
        if address.version != 6:
            return None
        host = f"[{address.compressed}]"
        port = match.group(2)
    elif authority.count(":") > 1:
        try:
            address = ipaddress.ip_address(authority)
        except ValueError:
            return None
        if address.version != 6:
            return None
        host = address.compressed
    else:
        if ":" in authority:
            host, port = authority.rsplit(":", 1)
        else:
            host = authority
        if HOSTNAME_PATTERN.fullmatch(host) is None:
            return None
    if port is not None and (not port.isdigit() or not 1 <= int(port) <= 65535):
        return None
    return host.lower()


def _exact_ascii_header(request: Request, name: bytes) -> str | None:
    values = [
        value
        for key, value in request.scope.get("headers", [])
        if key.lower() == name
    ]
    if len(values) != 1:
        return None
    try:
        return values[0].decode("ascii")
    except UnicodeDecodeError:
        return None


def classify_managed_transport(
    request: Request,
    *,
    managed_transport: str,
    managed_hub_url: str,
) -> ManagedTransportIdentity | None:
    """Classify only a direct loopback call or the configured Serve proxy.

    Uvicorn deliberately runs with ``proxy_headers=False``. Therefore the
    socket peer and ASGI scheme remain the local Serve proxy and ``http``;
    forwarded values are evidence only when every Serve-owned header exactly
    matches the one configured public origin.
    """

    if request.url.scheme != "http":
        return None
    raw_names = [key.lower() for key, _value in request.scope.get("headers", [])]
    forwarded_names = {
        b"forwarded",
        b"x-forwarded-host",
        b"x-forwarded-proto",
        b"tailscale-user-login",
        b"tailscale-user-name",
        b"tailscale-headers-info",
        b"tailscale-funnel-request",
    }
    loopback_peer = _is_loopback_peer(request)
    if loopback_peer and _has_loopback_authority(request):
        if any(name in forwarded_names for name in raw_names):
            return None
        return ManagedTransportIdentity("loopback")
    if managed_transport == "direct_ip":
        direct_forbidden_headers = forwarded_names | {
            b"cookie",
            b"x-forwarded-for",
            b"x-forwarded-port",
            b"via",
        }
        if loopback_peer or any(name in direct_forbidden_headers for name in raw_names):
            return None
        try:
            parsed = urlsplit(managed_hub_url)
        except ValueError:
            return None
        if (
            parsed.scheme != "http"
            or not parsed.netloc
            or _exact_ascii_header(request, b"host") != parsed.netloc
        ):
            return None
        return ManagedTransportIdentity("direct_ip")
    if not loopback_peer:
        return None
    if managed_transport != "tailscale_serve":
        return None
    try:
        parsed = urlsplit(managed_hub_url)
    except ValueError:
        return None
    expected_authority = parsed.netloc
    if (
        parsed.scheme != "https"
        or not expected_authority
        or _exact_ascii_header(request, b"host") != expected_authority
        or _exact_ascii_header(request, b"x-forwarded-host") != expected_authority
        or _exact_ascii_header(request, b"x-forwarded-proto") != "https"
        or _exact_ascii_header(request, b"tailscale-headers-info")
        != TAILSCALE_SERVE_HEADERS_INFO
        or b"tailscale-funnel-request" in raw_names
        or b"forwarded" in raw_names
    ):
        return None
    login = _exact_ascii_header(request, b"tailscale-user-login")
    user_name = _exact_ascii_header(request, b"tailscale-user-name")
    if (
        login is None
        or user_name is None
        or login != login.strip().lower()
        or not 3 <= len(login) <= 320
        or "@" not in login
        or not 1 <= len(user_name.strip()) <= 160
        or any(ord(char) < 32 or ord(char) > 126 for char in login + user_name)
    ):
        return None
    return ManagedTransportIdentity(
        "tailscale_serve",
        tailnet_login=login,
        tailnet_user_name=user_name.strip(),
    )


def _mounted_route_path(request: Request) -> str:
    """Return the route-local path for standalone and mounted deployments."""

    path = str(request.scope.get("path") or request.url.path)
    root_path = str(request.scope.get("root_path") or "").rstrip("/")
    if root_path and path.startswith(root_path + "/"):
        return path[len(root_path) :]
    return path


def create_app(
    data_dir: str | Path,
    *,
    allowed_hosts: set[str] | None = None,
    allowed_origins: set[str] | None = None,
    managed_host_identity: str | None = None,
    managed_server_instance_id: str | None = None,
    managed_transport: str | None = None,
    managed_hub_url: str | None = None,
    managed_routes: dict[str, str] | None = None,
    secure_peer_manager: Any | None = None,
    require_https_for_non_loopback: bool = False,
    require_loopback_transport: bool = False,
) -> FastAPI:
    store = HubStore(
        Path(data_dir),
        managed_host_identity=managed_host_identity,
        managed_server_instance_id=managed_server_instance_id,
    )
    hosts = allowed_hosts or {"127.0.0.1", "localhost", "[::1]", "::1"}
    origins = allowed_origins or set()
    rate_limiter = _RateLimiter()
    app = FastAPI(
        title="AgentsDock Team Hub",
        version="1",
        docs_url=None,
        redoc_url=None,
        openapi_url="/v1/openapi.json",
    )
    app.state.store = store
    app.state.rate_limiter = rate_limiter
    app.state.secure_peer_manager = secure_peer_manager

    @app.middleware("http")
    async def strict_transport(request: Request, call_next):
        raw_headers = request.scope.get("headers", [])
        values: dict[bytes, list[bytes]] = {}
        for key, value in raw_headers:
            values.setdefault(key.lower(), []).append(value)
        host_values = values.get(b"host", [])
        if len(host_values) != 1:
            return _error("invalid_request", "Invalid Host header", 400)
        try:
            host_header = host_values[0].decode("ascii")
        except UnicodeDecodeError:
            return _error("invalid_request", "Invalid Host header", 400)
        host_name = _host_name(host_header)
        if host_name is None or host_name not in {item.lower() for item in hosts}:
            return _error("invalid_request", "Invalid Host header", 400)
        managed_identity: ManagedTransportIdentity | None = None
        managed_identity_url: str | None = None
        configured_routes = managed_routes or (
            {managed_transport: managed_hub_url}
            if managed_transport is not None and managed_hub_url is not None
            else {}
        )
        if managed_transport is not None or managed_routes is not None:
            if not configured_routes:
                return _error("transport_configuration_invalid", "Transport is unavailable", 503)
            for route_kind, route_url in configured_routes.items():
                managed_identity = classify_managed_transport(
                    request,
                    managed_transport=route_kind,
                    managed_hub_url=route_url,
                )
                if managed_identity is not None:
                    managed_identity_url = route_url
                    break
            if managed_identity is None:
                return _error(
                    "local_preview_only",
                    "Embedded Team Hub transport is not permitted",
                    403,
                )
            request.state.team_hub_transport = managed_identity.kind
            request.state.team_hub_url = managed_identity_url
            request.state.tailnet_login = managed_identity.tailnet_login
        origin_values = values.get(b"origin", [])
        if len(origin_values) > 1:
            return _error("origin_forbidden", "Origin is not permitted", 403)
        if origin_values:
            try:
                origin = origin_values[0].decode("ascii")
            except UnicodeDecodeError:
                return _error("origin_forbidden", "Origin is not permitted", 403)
            if origin not in origins:
                return _error("origin_forbidden", "Origin is not permitted", 403)
        else:
            origin = None
        fetch_site = values.get(b"sec-fetch-site", [])
        if fetch_site and fetch_site[-1].lower() == b"cross-site":
            return _error("origin_forbidden", "Origin is not permitted", 403)
        if require_loopback_transport and (
            not _is_loopback_peer(request)
            or not _has_loopback_authority(request)
            or request.url.scheme != "http"
        ):
            return _error(
                "local_preview_only",
                "Embedded Team Hub is available only to this host",
                403,
            )
        if (
            require_https_for_non_loopback
            and not _is_loopback_peer(request)
            and request.url.scheme != "https"
        ):
            return _error(
                "transport_security_required",
                "Direct HTTPS is required for remote Team Hub access",
                403,
            )
        if request.method == "OPTIONS":
            requested_method = request.headers.get("access-control-request-method")
            if origin is None or requested_method not in {"GET", "POST"}:
                return _error("origin_forbidden", "Origin is not permitted", 403)
            requested_headers = request.headers.get("access-control-request-headers", "")
            allowed_request_headers = {
                item.strip().lower() for item in requested_headers.split(",") if item.strip()
            }
            if not allowed_request_headers.issubset(
                {
                    "authorization",
                    "content-type",
                    "x-team-hub-bootstrap-proof",
                    "x-team-hub-bootstrap-request-id",
                    "x-team-hub-device-recovery-proof",
                    "x-team-hub-owner-recovery-proof",
                }
            ):
                return _error("origin_forbidden", "Origin is not permitted", 403)
            return Response(
                status_code=204,
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Access-Control-Allow-Methods": "GET, POST",
                    "Access-Control-Allow-Headers": requested_headers,
                    "Vary": "Origin",
                },
            )
        if request.method == "POST":
            try:
                maintenance_fenced = store.maintenance_fence() is not None
            except Exception:
                maintenance_fenced = True
            if maintenance_fenced:
                return _error(
                    "hub_maintenance",
                    "Team Hub is unavailable during server maintenance",
                    503,
                )
            if (
                managed_identity is not None
                and managed_identity.kind == "tailscale_serve"
                and managed_identity.tailnet_login is not None
            ):
                peer = "tailnet:" + hashlib.sha256(
                    managed_identity.tailnet_login.encode("utf-8")
                ).hexdigest()
            else:
                peer = request.client.host if request.client is not None else "unknown"
            route_path = _mounted_route_path(request)
            sensitive_limits = {
                "/v1/bootstrap/redeem": 8,
                "/v1/owner-recovery/redeem": 8,
                "/v1/device-recovery/redeem": 8,
                "/v1/sessions/refresh": 30,
                "/v1/invitations/redeem": 30,
                "/v1/node-enrollments/challenge": 30,
                "/v1/node-enrollments/redeem": 30,
            }
            action = route_path if route_path in sensitive_limits else "other_post"
            limit = sensitive_limits.get(route_path, 120)
            if (
                not rate_limiter.allow(peer, action, limit)
                or not rate_limiter.allow("*", action, limit * 100)
            ):
                return _error("rate_limited", "Too many requests", 429)
        if values.get(b"transfer-encoding"):
            return _error("invalid_request", "Transfer-Encoding is not accepted", 400)
        lengths = values.get(b"content-length", [])
        if len(lengths) > 1:
            return _error("invalid_request", "Duplicate Content-Length is not accepted", 400)
        if request.method in {"POST", "PUT", "PATCH"}:
            if len(lengths) != 1:
                return _error("invalid_request", "Exactly one Content-Length is required", 400)
            try:
                raw_length = lengths[0].decode("ascii")
                body_length = int(raw_length, 10)
            except (UnicodeDecodeError, ValueError):
                return _error("invalid_request", "Content-Length is invalid", 400)
            if raw_length != str(body_length) or not 2 <= body_length <= MAX_JSON_BODY_BYTES:
                return _error("invalid_request", "JSON body size is invalid", 413)
            content_types = values.get(b"content-type", [])
            if content_types != [b"application/json"]:
                return _error("invalid_request", "Content-Type must be application/json", 415)
            try:
                body = await asyncio.wait_for(
                    request.body(), timeout=BODY_READ_TIMEOUT_SECONDS
                )
            except TimeoutError:
                return _error("request_timeout", "Request body timed out", 408)
            if len(body) != body_length:
                return _error("invalid_request", "Content-Length does not match body", 400)
            try:
                parsed = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return _error("invalid_request", "Request body must be valid JSON", 400)
            if not isinstance(parsed, dict):
                return _error("invalid_request", "Request body must be a JSON object", 400)
            try:
                json.dumps(parsed, ensure_ascii=False).encode("utf-8")
            except UnicodeEncodeError:
                return _error("invalid_request", "Request body contains invalid Unicode", 400)
        response = await call_next(request)
        if origin is not None:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    @app.exception_handler(HubError)
    async def hub_error_handler(_request: Request, exc: HubError):
        return _error(exc.code, exc.message, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, _exc: RequestValidationError):
        return _error("invalid_request", "Request validation failed", 422)

    @app.exception_handler(HTTPException)
    async def http_error_handler(_request: Request, exc: HTTPException):
        if exc.status_code == 404:
            return _error("not_found", "Resource not found", 404)
        return _error("request_failed", "Request failed", exc.status_code)

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(_request: Request, _exc: AuthenticationError):
        return _error("authentication_required", "Authentication required", 401)

    @app.exception_handler(AuthorizationError)
    async def authorization_error_handler(_request: Request, _exc: AuthorizationError):
        return _error("forbidden", "Operation is not permitted", 403)

    @app.exception_handler(ValueError)
    async def value_error_handler(_request: Request, _exc: ValueError):
        return _error("invalid_request", "Request validation failed", 422)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception):
        return _error("internal_error", "Internal server error", 500)

    def claims_from_request(request: Request) -> AccessClaims:
        raw = request.scope.get("headers", [])
        authorization = [value for key, value in raw if key.lower() == b"authorization"]
        if len(authorization) != 1:
            raise HubError("authentication_required", "Authentication required", 401)
        try:
            value = authorization[0].decode("ascii")
        except UnicodeDecodeError as exc:
            raise HubError("authentication_required", "Authentication required", 401) from exc
        if not value.startswith("Bearer ") or " " in value[7:] or not value[7:]:
            raise HubError("authentication_required", "Authentication required", 401)
        return store.verify_access(value[7:])

    Auth = Annotated[AccessClaims, Depends(claims_from_request)]

    @app.get("/v1/health")
    def health() -> dict[str, Any]:
        return store.health()

    @app.post("/v1/bootstrap/redeem")
    def bootstrap(request: Request, body: BootstrapRequest) -> dict[str, Any]:
        proof = _exact_ascii_header(request, b"x-team-hub-bootstrap-proof")
        if proof is None:
            raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
        transport = getattr(request.state, "team_hub_transport", None)
        if transport is None:
            transport = (
                "loopback"
                if _is_loopback_peer(request) and _has_loopback_authority(request)
                else None
            )
        if transport == "loopback":
            if _exact_ascii_header(
                request, b"x-team-hub-bootstrap-request-id"
            ) is not None:
                raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
            return store.bootstrap(
                proof,
                body.email,
                body.display_name,
                body.device_label,
                transport="loopback",
            )
        if transport == "tailscale_serve":
            request_id = _exact_ascii_header(
                request, b"x-team-hub-bootstrap-request-id"
            )
            tailnet_login = getattr(request.state, "tailnet_login", None)
            route_hub_url = getattr(request.state, "team_hub_url", None)
            if request_id is None or tailnet_login is None or route_hub_url is None:
                raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
            return store.bootstrap(
                proof,
                body.email,
                body.display_name,
                body.device_label,
                transport="tailscale_serve",
                request_id=request_id,
                tailnet_login=tailnet_login,
                hub_url=route_hub_url,
            )
        if transport == "direct_ip":
            request_id = _exact_ascii_header(
                request, b"x-team-hub-bootstrap-request-id"
            )
            route_hub_url = getattr(request.state, "team_hub_url", None)
            if request_id is None or route_hub_url is None:
                raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
            return store.bootstrap(
                proof,
                body.email,
                body.display_name,
                body.device_label,
                transport="direct_ip",
                request_id=request_id,
                tailnet_login=body.email,
                hub_url=route_hub_url,
            )
        raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)

    @app.post("/v1/owner-recovery/redeem")
    def owner_recovery(request: Request, body: RedeemRecoveryRequest) -> dict[str, Any]:
        if not _is_loopback_peer(request) or not _has_loopback_authority(request):
            raise HubError("recovery_unavailable", "Owner recovery is unavailable", 403)
        proof = _exact_ascii_header(request, b"x-team-hub-owner-recovery-proof")
        if proof is None:
            raise HubError("recovery_unavailable", "Owner recovery is unavailable", 403)
        return store.redeem_owner_recovery(proof, body.device_label)

    @app.post("/v1/device-recovery/redeem")
    def device_recovery(request: Request, body: RedeemRecoveryRequest) -> dict[str, Any]:
        classified = getattr(request.state, "team_hub_transport", None)
        loopback_peer = _is_loopback_peer(request)
        if classified not in {"loopback", "tailscale_serve", "direct_ip"} and (
            (loopback_peer and not _has_loopback_authority(request))
            or (not loopback_peer and request.url.scheme != "https")
        ):
            raise HubError("recovery_unavailable", "Device recovery is unavailable", 403)
        proof = _exact_ascii_header(request, b"x-team-hub-device-recovery-proof")
        if proof is None:
            raise HubError("recovery_unavailable", "Device recovery is unavailable", 403)
        return store.redeem_device_recovery(proof, body.device_label)

    @app.post("/v1/sessions/refresh")
    def refresh(body: RefreshRequest) -> dict[str, Any]:
        return store.refresh(body.refresh_token)

    @app.post("/v1/sessions/revoke")
    def revoke_session(body: RevokeSessionRequest, claims: Auth) -> dict[str, Any]:
        return store.revoke_session(claims, body.refresh_token)

    @app.get("/v1/session")
    def session(claims: Auth) -> dict[str, Any]:
        return store.session_snapshot(claims)

    @app.get("/v1/teams")
    def teams(claims: Auth) -> dict[str, Any]:
        return store.list_teams(claims)

    @app.get("/v1/teams/{team_id}")
    def team(team_id: str, claims: Auth) -> dict[str, Any]:
        return store.get_team(claims, team_id)

    @app.get("/v1/teams/{team_id}/members")
    def members(team_id: str, claims: Auth) -> dict[str, Any]:
        return store.list_members(claims, team_id)

    @app.post("/v1/teams/{team_id}/invitations")
    def invite(team_id: str, body: InviteRequest, claims: Auth) -> dict[str, Any]:
        return store.issue_invite(
            claims, team_id, body.invitee_email, body.role, body.ttl_seconds
        )

    @app.post("/v1/invitations/redeem")
    def redeem_invite(body: RedeemInviteRequest) -> dict[str, Any]:
        return store.redeem_invite(
            body.token, body.email, body.display_name, body.device_label
        )

    @app.post("/v1/invitations/accept")
    def accept_invite(body: AcceptInviteRequest, claims: Auth) -> dict[str, Any]:
        return store.accept_invite(claims, body.token)

    @app.post("/v1/teams/{team_id}/node-enrollments")
    def node_grant(team_id: str, body: NodeGrantRequest, claims: Auth) -> dict[str, Any]:
        return store.issue_node_grant(
            claims,
            team_id,
            body.server_identity,
            body.display_name,
            body.public_key,
            body.ttl_seconds,
        )

    @app.post("/v1/node-enrollments/challenge")
    def node_challenge(body: NodeChallengeRequest) -> dict[str, Any]:
        return store.node_challenge(
            body.token, body.server_identity, body.display_name, body.public_key
        )

    @app.post("/v1/node-enrollments/redeem")
    def node_redeem(body: NodeRedeemRequest) -> dict[str, Any]:
        return store.redeem_node_challenge(body.challenge_id, body.signature)

    @app.get("/v1/teams/{team_id}/nodes")
    def nodes(team_id: str, claims: Auth) -> dict[str, Any]:
        return store.list_nodes(claims, team_id)

    @app.get("/v1/teams/{team_id}/secure-peers")
    def secure_peers(team_id: str, claims: Auth) -> dict[str, Any]:
        store.require_team_admin(claims, team_id)
        if secure_peer_manager is None:
            raise HubError(
                "secure_peer_unavailable",
                "Secure peer pairing is unavailable",
                503,
            )
        return secure_peer_manager.list_peers(team_id=team_id)

    @app.post("/v1/teams/{team_id}/secure-peers/{peer_id}/revoke")
    def revoke_secure_peer(
        team_id: str,
        peer_id: str,
        body: SecurePeerRevocationRequest,
        claims: Auth,
    ) -> dict[str, Any]:
        store.require_team_admin(claims, team_id)
        if secure_peer_manager is None:
            raise HubError(
                "secure_peer_unavailable",
                "Secure peer pairing is unavailable",
                503,
            )
        return secure_peer_manager.revoke_peer(
            peer_id=peer_id,
            team_id=team_id,
            revoked_by=claims.principal_id,
            expected_certificate_fingerprint=(
                body.expected_certificate_fingerprint
            ),
            idempotency_key=body.idempotency_key,
        )

    @app.get("/v1/teams/{team_id}/channels")
    def channels(team_id: str, claims: Auth) -> dict[str, Any]:
        return store.list_channels(claims, team_id)

    @app.post("/v1/teams/{team_id}/channels")
    def create_channel(team_id: str, body: ChannelRequest, claims: Auth) -> dict[str, Any]:
        return store.create_channel(claims, team_id, body.model_dump())

    @app.get("/v1/channels/{channel_id}/messages")
    def messages(
        channel_id: str,
        claims: Auth,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before_sequence: Annotated[int | None, Query(ge=1)] = None,
    ) -> dict[str, Any]:
        return store.list_messages(claims, channel_id, limit, before_sequence)

    @app.post("/v1/channels/{channel_id}/messages")
    def create_message(channel_id: str, body: MessageRequest, claims: Auth) -> dict[str, Any]:
        return store.create_message(claims, channel_id, body.model_dump())

    @app.post("/v1/dispatches")
    def dispatch_unavailable(claims: Auth) -> dict[str, Any]:
        store.session_snapshot(claims)
        raise HubError(
            "dispatch_unavailable",
            "Dispatch requires a scoped capability and node connector",
            501,
        )

    return app
