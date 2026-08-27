"""Managed in-process Team Hub boundary for one designated AgentsServer."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import JSONResponse

from agentsdock_team_hub.service import (
    ManagedTransportIdentity,
    classify_managed_transport,
    create_app,
)
from agentsdock_team_hub.store import HubError, HubStore


TEAM_HUB_MOUNT_PATH = "/api/team-hub"
TEAM_HUB_CAPABILITY_VERSION = 1
TEAM_HUB_MODE_DISABLED = "disabled"
TEAM_HUB_MODE_HOST = "host"
TEAM_HUB_TRANSPORT_LOOPBACK = "loopback"
TEAM_HUB_TRANSPORT_TAILSCALE_SERVE = "tailscale_serve"
TEAM_HUB_TRANSPORT_DIRECT_IP = "direct_ip"
TEAM_HUB_TAILSCALE_SERVE_PORT = 8444
TEAM_HUB_TAILNET_LABEL_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$"
)


def _canonical_tailnet_hostname(value: str) -> bool:
    labels = value.split(".")
    return (
        value == value.lower()
        and not value.endswith(".")
        and len(labels) >= 4
        and labels[-2:] == ["ts", "net"]
        and all(
            1 <= len(label) <= 63
            and not label.startswith("xn--")
            and TEAM_HUB_TAILNET_LABEL_PATTERN.fullmatch(label) is not None
            for label in labels
        )
    )


def configured_team_hub_mode(value: str | None) -> tuple[str, str | None]:
    normalized = str(value or "disabled").strip().lower()
    if normalized in {"", TEAM_HUB_MODE_DISABLED}:
        return TEAM_HUB_MODE_DISABLED, None
    if normalized == TEAM_HUB_MODE_HOST:
        return TEAM_HUB_MODE_HOST, None
    return TEAM_HUB_MODE_DISABLED, "AGENTSDOCK_TEAM_HUB_MODE must be disabled or host"


def configured_team_hub_hosts(
    bind_address: str | None,
    configured: str | None,
) -> set[str]:
    hosts = {"127.0.0.1", "localhost", "[::1]", "::1"}
    bind = str(bind_address or "").strip().lower()
    if bind and bind not in {"0.0.0.0", "::", "[::]", "localhost"}:
        hosts.add(bind)
    for value in str(configured or "").split(","):
        host = value.strip().lower()
        if host:
            hosts.add(host)
    return hosts


def configured_team_hub_endpoint(
    mode: str,
    configured_url: str | None,
    configured_transport: str | None = None,
    server_port: int = 7850,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return transport, public Hub URL, public hostname, and config error."""

    if mode != TEAM_HUB_MODE_HOST:
        return None, None, None, None
    value = str(configured_url or "").strip()
    selected = str(configured_transport or "").strip().lower()
    if not value:
        if selected not in {"", TEAM_HUB_TRANSPORT_LOOPBACK}:
            return (
                None,
                None,
                None,
                "AGENTSDOCK_TEAM_HUB_URL is required for the selected Team Hub transport",
            )
        return TEAM_HUB_TRANSPORT_LOOPBACK, None, None, None
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None, None, None, "AGENTSDOCK_TEAM_HUB_URL is invalid"
    hostname = parsed.hostname
    if selected == TEAM_HUB_TRANSPORT_DIRECT_IP:
        try:
            address = ipaddress.ip_address(str(hostname or ""))
        except ValueError:
            address = None
        canonical_host = str(address) if address is not None else ""
        canonical = (
            f"http://{canonical_host}:{server_port}{TEAM_HUB_MOUNT_PATH}"
            if canonical_host
            else ""
        )
        if (
            parsed.scheme != "http"
            or address is None
            or address.version != 4
            or address.is_loopback
            or not 1 <= int(str(address).split(".", 1)[0]) < 224
            or parsed.username is not None
            or parsed.password is not None
            or port != server_port
            or parsed.path != TEAM_HUB_MOUNT_PATH
            or parsed.query
            or parsed.fragment
            or value != canonical
        ):
            return (
                None,
                None,
                None,
                "AGENTSDOCK_TEAM_HUB_URL must be the exact same-origin "
                f"http://<literal-ip>:{server_port}{TEAM_HUB_MOUNT_PATH} URL "
                "for direct_ip transport",
            )
        return TEAM_HUB_TRANSPORT_DIRECT_IP, canonical, canonical_host, None

    if selected not in {"", TEAM_HUB_TRANSPORT_TAILSCALE_SERVE}:
        return (
            None,
            None,
            None,
            "AGENTSDOCK_TEAM_HUB_TRANSPORT must be loopback, tailscale_serve, or direct_ip",
        )
    canonical = (
        f"https://{hostname}:{TEAM_HUB_TAILSCALE_SERVE_PORT}{TEAM_HUB_MOUNT_PATH}"
        if hostname is not None
        else ""
    )
    if (
        parsed.scheme != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port != TEAM_HUB_TAILSCALE_SERVE_PORT
        or parsed.path != TEAM_HUB_MOUNT_PATH
        or parsed.query
        or parsed.fragment
        or not _canonical_tailnet_hostname(hostname)
        or value != canonical
    ):
        return (
            None,
            None,
            None,
            "AGENTSDOCK_TEAM_HUB_URL must be the canonical private "
            "https://<host>.ts.net:8444/api/team-hub URL",
        )
    return TEAM_HUB_TRANSPORT_TAILSCALE_SERVE, canonical, hostname, None


class ManagedTeamHubHost:
    """An unavailable-first ASGI mount whose credential realm is Hub-only."""

    def __init__(
        self,
        *,
        mode: str,
        data_dir: Path,
        server_identity: str,
        server_instance_id: str = "server-instance-local-preview",
        allowed_hosts: set[str],
        transport: str = TEAM_HUB_TRANSPORT_LOOPBACK,
        hub_url: str | None = None,
        routes: dict[str, str | None] | None = None,
        secure_peer_manager: Any | None = None,
        config_error: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.mode = mode
        self.data_dir = Path(data_dir)
        self.server_identity = str(server_identity)
        self.server_instance_id = str(server_instance_id)
        self.allowed_hosts = set(allowed_hosts)
        self.transport = transport
        self.hub_url = hub_url
        configured_routes = dict(routes or {transport: hub_url})
        route_error: str | None = None
        if (
            transport not in {
                TEAM_HUB_TRANSPORT_LOOPBACK,
                TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
                TEAM_HUB_TRANSPORT_DIRECT_IP,
            }
            or configured_routes.get(transport) != hub_url
            or any(
                route not in {
                    TEAM_HUB_TRANSPORT_LOOPBACK,
                    TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
                    TEAM_HUB_TRANSPORT_DIRECT_IP,
                }
                for route in configured_routes
            )
        ):
            route_error = "Team Hub routes do not contain the exact primary transport"
        self.routes = {
            route: configured_routes[route]
            for route in (
                transport,
                TEAM_HUB_TRANSPORT_LOOPBACK,
                TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
                TEAM_HUB_TRANSPORT_DIRECT_IP,
            )
            if route in configured_routes
        }
        self.secure_peer_manager = secure_peer_manager
        self.config_error = config_error or route_error
        self.logger = logger or logging.getLogger("agents-server.team-hub")
        self._guard = threading.RLock()
        self._admission = asyncio.Condition()
        self._in_flight = 0
        self._delegate: Any | None = None
        self._store: HubStore | None = None
        self._runtime_lease_fd: int | None = None
        self._accepting = False
        self._startup_failed = False
        self._bootstrap_rate_guard = threading.Lock()
        self._bootstrap_rate_buckets: dict[str, tuple[int, int]] = {}

    @property
    def designated_host(self) -> bool:
        return self.mode == TEAM_HUB_MODE_HOST

    @property
    def store(self) -> HubStore | None:
        with self._guard:
            return self._store

    def initialize(self) -> None:
        if not self.designated_host:
            if self.config_error:
                self.logger.error("Team Hub is disabled: %s", self.config_error)
            return
        if self.config_error:
            with self._guard:
                self._startup_failed = True
                self._accepting = False
            self.logger.error("Team Hub host configuration is invalid: %s", self.config_error)
            return
        lease: int | None = None
        try:
            lease = HubStore.acquire_managed_runtime_lease(self.data_dir)
            application = create_app(
                self.data_dir,
                allowed_hosts=self.allowed_hosts,
                managed_host_identity=self.server_identity,
                managed_server_instance_id=self.server_instance_id,
                managed_transport=self.transport,
                managed_hub_url=(
                    self.hub_url
                    or f"http://127.0.0.1{TEAM_HUB_MOUNT_PATH}"
                ),
                managed_routes={
                    route: (
                        url
                        or f"http://127.0.0.1{TEAM_HUB_MOUNT_PATH}"
                    )
                    for route, url in self.routes.items()
                },
                secure_peer_manager=self.secure_peer_manager,
            )
        except Exception as exc:
            HubStore.release_managed_runtime_lease(lease)
            with self._guard:
                self._startup_failed = True
                self._delegate = None
                self._store = None
                self._accepting = False
            self.logger.error(
                "managed Team Hub activation failed error_type=%s",
                type(exc).__name__,
            )
            return
        with self._guard:
            self._runtime_lease_fd = lease
            self._delegate = application
            self._store = application.state.store
            self._accepting = True
            self._startup_failed = False
        if self.secure_peer_manager is not None:
            try:
                self.secure_peer_manager.attach_host_hub(
                    hub_id=application.state.store.hub_id,
                    hub_data_dir=self.data_dir,
                    hub_store=application.state.store,
                )
            except Exception as exc:
                self.logger.error(
                    "secure peer host attachment failed error_type=%s error_code=%s",
                    type(exc).__name__,
                    getattr(exc, "code", "secure_peer_host_recovery_failed"),
                )
                error_code = str(
                    getattr(exc, "code", "secure_peer_host_recovery_failed")
                )
                self.secure_peer_manager.mark_host_unavailable(
                    (
                        "An existing secure peer connection could not be "
                        "reconciled safely."
                        if error_code == "peer_identity_conflict"
                        else "The secure peer host could not finish recovery."
                    ),
                    error_code=error_code,
                    action=(
                        "Review or remove the conflicting logical server "
                        "connection, then retry host recovery."
                        if error_code == "peer_identity_conflict"
                        else "Retry after the Team Hub database is available."
                    ),
                )

    def capability(self) -> dict[str, Any]:
        with self._guard:
            available = bool(self._accepting and self._store is not None)
            hub_id = self._store.hub_id if available and self._store is not None else None
            startup_failed = self._startup_failed
        if not self.designated_host:
            message = (
                "Team Hub host mode is misconfigured on this AgentsServer."
                if self.config_error
                else "This AgentsServer is not the designated Team Hub host."
            )
            return {
                "available": False,
                "designated_host": False,
                "version": TEAM_HUB_CAPABILITY_VERSION,
                "base_path": None,
                "transport": None,
                "hub_url": None,
                "routes": [],
                "hub_id": None,
                "host_server_identity": None,
                "message": message,
                "action": (
                    "Set AGENTSDOCK_TEAM_HUB_MODE to disabled or host."
                    if self.config_error
                    else "Configure AGENTSDOCK_TEAM_HUB_MODE=host on exactly one AgentsServer."
                ),
            }
        return {
            "available": available,
            "designated_host": True,
            "version": TEAM_HUB_CAPABILITY_VERSION,
            "base_path": TEAM_HUB_MOUNT_PATH,
            "transport": self.transport,
            "hub_url": self.hub_url,
            "routes": [
                {"transport": route, "hub_url": url}
                for route, url in self.routes.items()
            ],
            "hub_id": hub_id,
            "host_server_identity": self.server_identity,
            "message": (
                "This AgentsServer hosts Team Hub over private Tailscale Serve."
                if available and self.transport == TEAM_HUB_TRANSPORT_TAILSCALE_SERVE
                else (
                    "This AgentsServer hosts Team Hub over direct, unencrypted IP access."
                )
                if available and self.transport == TEAM_HUB_TRANSPORT_DIRECT_IP
                else "This AgentsServer hosts the local Team Hub preview."
                if available
                else "The designated Team Hub is unavailable."
            ),
            "action": (
                None
                if available
                else "Restart the designated host after checking its local Team Hub logs."
                if startup_failed
                else "Wait for the designated Team Hub to finish starting."
            ),
        }

    def tailscale_serve_identity(
        self,
        request: Request,
    ) -> ManagedTransportIdentity | None:
        route_url = self.routes.get(TEAM_HUB_TRANSPORT_TAILSCALE_SERVE)
        if route_url is None:
            return None
        identity = classify_managed_transport(
            request,
            managed_transport=TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
            managed_hub_url=route_url,
        )
        if identity is None or identity.kind != TEAM_HUB_TRANSPORT_TAILSCALE_SERVE:
            return None
        return identity

    def direct_ip_identity(
        self,
        request: Request,
    ) -> ManagedTransportIdentity | None:
        route_url = self.routes.get(TEAM_HUB_TRANSPORT_DIRECT_IP)
        if route_url is None:
            return None
        identity = classify_managed_transport(
            request,
            managed_transport=TEAM_HUB_TRANSPORT_DIRECT_IP,
            managed_hub_url=route_url,
        )
        if identity is None or identity.kind != TEAM_HUB_TRANSPORT_DIRECT_IP:
            return None
        return identity

    async def issue_tailnet_bootstrap_proof(
        self,
        *,
        request_id: str,
        server_identity: str,
        server_instance_id: str,
        hub_url: str,
        tailnet_login: str,
        recipient_email: str,
        display_name: str,
        device_label: str,
        transport: str = TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
    ) -> dict[str, Any]:
        """Admit one route-bound remote proof mutation into the Hub drain boundary."""

        if transport not in {
            TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
            TEAM_HUB_TRANSPORT_DIRECT_IP,
        } or transport not in self.routes:
            raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)

        async with self._admission:
            with self._guard:
                store = self._store if self._accepting else None
            if store is not None:
                self._in_flight += 1
        if store is None:
            raise HubError("hub_unavailable", "Team Hub is unavailable", 503)

        window = int(time.monotonic() // 60)
        normalized_login = str(tailnet_login).strip().lower()
        with self._bootstrap_rate_guard:
            allowed = True
            for key, limit in ((normalized_login, 8), ("*", 200)):
                prior_window, count = self._bootstrap_rate_buckets.get(
                    key, (window, 0)
                )
                if prior_window != window:
                    count = 0
                count += 1
                self._bootstrap_rate_buckets[key] = (window, count)
                allowed = allowed and count <= limit
            if len(self._bootstrap_rate_buckets) > 4096:
                self._bootstrap_rate_buckets = {
                    key: value
                    for key, value in self._bootstrap_rate_buckets.items()
                    if value[0] >= window - 1
                }
                while len(self._bootstrap_rate_buckets) > 4096:
                    self._bootstrap_rate_buckets.pop(
                        next(iter(self._bootstrap_rate_buckets))
                    )
        if not allowed:
            async with self._admission:
                self._in_flight = max(0, self._in_flight - 1)
                if self._in_flight == 0:
                    self._admission.notify_all()
            raise HubError("rate_limited", "Too many bootstrap proof requests", 429)

        def issue() -> dict[str, Any]:
            with HubStore.maintenance_control_lock(self.data_dir):
                if store.maintenance_fence() is not None:
                    raise HubError(
                        "hub_maintenance",
                        "Team Hub is unavailable during server maintenance",
                        503,
                    )
                return store.issue_tailnet_bootstrap_proof(
                    request_id=request_id,
                    server_identity=server_identity,
                    server_instance_id=server_instance_id,
                    hub_url=hub_url,
                    tailnet_login=tailnet_login,
                    recipient_email=recipient_email,
                    display_name=display_name,
                    device_label=device_label,
                    transport=transport,
                )

        try:
            return await asyncio.to_thread(issue)
        finally:
            async with self._admission:
                self._in_flight = max(0, self._in_flight - 1)
                if self._in_flight == 0:
                    self._admission.notify_all()

    async def _close_and_drain(self) -> None:
        if self.secure_peer_manager is not None:
            # The TLS gateway bypasses ASGI but shares the authoritative Hub
            # database. Close and drain it before a maintenance snapshot can
            # begin so an acknowledged peer write cannot land post-snapshot.
            await asyncio.to_thread(
                self.secure_peer_manager.close_host_admission
            )
        async with self._admission:
            with self._guard:
                self._accepting = False
            while self._in_flight:
                await self._admission.wait()

    async def prepare_maintenance(
        self,
        reason: str,
        *,
        drain_timeout_seconds: float = 10.0,
        persistent_fence: bool = True,
        operation_id: str | None = None,
    ) -> Path | None:
        """Close Hub admission, drain delegated requests, then snapshot."""

        if not self.designated_host:
            return None
        try:
            await asyncio.wait_for(
                self._close_and_drain(),
                timeout=max(0.1, float(drain_timeout_seconds)),
            )
        except TimeoutError as exc:
            await self.reopen_admission()
            raise RuntimeError("Team Hub request drain timed out") from exc
        with self._guard:
            store = self._store
        if store is None:
            await self.reopen_admission()
            raise RuntimeError("designated Team Hub is unavailable for maintenance")
        if persistent_fence and not operation_id:
            await self.reopen_admission()
            raise ValueError("persistent Team Hub maintenance requires an operation_id")
        snapshot_call = (
            asyncio.to_thread(
                store.maintenance_snapshot_and_fence,
                reason,
                operation_id=str(operation_id),
            )
            if persistent_fence
            else asyncio.to_thread(store.maintenance_snapshot, reason)
        )
        snapshot_task = asyncio.create_task(
            snapshot_call
        )
        try:
            return await asyncio.shield(snapshot_task)
        except asyncio.CancelledError:
            snapshot: Path | None = None
            try:
                snapshot = await snapshot_task
            except Exception:
                pass
            if persistent_fence and snapshot is not None:
                try:
                    await asyncio.to_thread(
                        store.clear_maintenance_fence,
                        expected_reason=reason,
                        expected_operation_id=str(operation_id),
                        expected_snapshot=snapshot,
                    )
                except Exception:
                    pass
            await self.reopen_admission()
            raise
        except BaseException:
            await self.reopen_admission()
            raise

    async def clear_maintenance(
        self,
        reason: str,
        operation_id: str,
        snapshot: Path,
    ) -> bool:
        with self._guard:
            store = self._store
        if store is None:
            if self.designated_host:
                raise RuntimeError("designated Team Hub is unavailable")
            return False
        return await asyncio.to_thread(
            store.clear_maintenance_fence,
            expected_reason=reason,
            expected_operation_id=operation_id,
            expected_snapshot=snapshot,
        )

    def clear_maintenance_sync(
        self,
        reason: str,
        operation_id: str,
        snapshot: Path,
    ) -> bool:
        with self._guard:
            store = self._store
        if store is None:
            if self.designated_host:
                raise RuntimeError("designated Team Hub is unavailable")
            return False
        return store.clear_maintenance_fence(
            expected_reason=reason,
            expected_operation_id=operation_id,
            expected_snapshot=snapshot,
        )

    def maintenance_fence_sync(self) -> dict[str, Any] | None:
        with self._guard:
            store = self._store
        if store is None:
            if self.designated_host:
                raise RuntimeError("designated Team Hub is unavailable")
            return None
        return store.maintenance_fence()

    async def reopen_admission(self) -> None:
        with self._guard:
            ready = self._delegate is not None and self._store is not None
        async with self._admission:
            with self._guard:
                self._accepting = ready
            self._admission.notify_all()
        if self.secure_peer_manager is not None:
            await asyncio.to_thread(
                self.secure_peer_manager.reopen_host_admission
            )

    def reopen_admission_sync(self) -> None:
        """Reopen after a synchronous restart signal worker fails.

        Request admission only holds the asyncio condition long enough to
        increment a counter; changing the guarded flag cannot strand a waiter.
        The next request observes the restored value without crossing event
        loops from Starlette's background thread.
        """

        with self._guard:
            self._accepting = self._delegate is not None and self._store is not None
        if self.secure_peer_manager is not None:
            self.secure_peer_manager.reopen_host_admission()

    async def shutdown(self) -> None:
        if not self.designated_host:
            return
        try:
            try:
                with self._guard:
                    store = self._store
                already_fenced = bool(
                    store is not None
                    and await asyncio.to_thread(store.maintenance_fence) is not None
                )
                if already_fenced:
                    await asyncio.wait_for(self._close_and_drain(), timeout=10.0)
                else:
                    await self.prepare_maintenance(
                        "server-shutdown", persistent_fence=False
                    )
            except Exception as exc:
                self.logger.error(
                    "managed Team Hub shutdown snapshot failed error_type=%s",
                    type(exc).__name__,
                )
                async with self._admission:
                    self._accepting = False
        finally:
            with self._guard:
                lease = self._runtime_lease_fd
                self._runtime_lease_fd = None
            HubStore.release_managed_runtime_lease(lease)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        async with self._admission:
            with self._guard:
                delegate = self._delegate if self._accepting else None
            if delegate is not None:
                self._in_flight += 1
        if delegate is None:
            response = JSONResponse(
                {
                    "error": {
                        "code": "hub_unavailable",
                        "message": "Team Hub is unavailable",
                    }
                },
                status_code=503,
            )
            await response(scope, receive, send)
            return
        try:
            await delegate(scope, receive, send)
        finally:
            async with self._admission:
                self._in_flight = max(0, self._in_flight - 1)
                if self._in_flight == 0:
                    self._admission.notify_all()
