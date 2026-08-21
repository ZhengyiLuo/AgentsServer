"""Managed in-process Team Hub boundary for one designated AgentsServer."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import threading
from typing import Any

from starlette.responses import JSONResponse

from agentsdock_team_hub.service import create_app
from agentsdock_team_hub.store import HubStore


TEAM_HUB_MOUNT_PATH = "/api/team-hub"
TEAM_HUB_CAPABILITY_VERSION = 1
TEAM_HUB_MODE_DISABLED = "disabled"
TEAM_HUB_MODE_HOST = "host"


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


class ManagedTeamHubHost:
    """An unavailable-first ASGI mount whose credential realm is Hub-only."""

    def __init__(
        self,
        *,
        mode: str,
        data_dir: Path,
        server_identity: str,
        allowed_hosts: set[str],
        config_error: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.mode = mode
        self.data_dir = Path(data_dir)
        self.server_identity = str(server_identity)
        self.allowed_hosts = set(allowed_hosts)
        self.config_error = config_error
        self.logger = logger or logging.getLogger("agents-server.team-hub")
        self._guard = threading.RLock()
        self._admission = asyncio.Condition()
        self._in_flight = 0
        self._delegate: Any | None = None
        self._store: HubStore | None = None
        self._runtime_lease_fd: int | None = None
        self._accepting = False
        self._startup_failed = False

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
        lease: int | None = None
        try:
            lease = HubStore.acquire_managed_runtime_lease(self.data_dir)
            application = create_app(
                self.data_dir,
                allowed_hosts=self.allowed_hosts,
                managed_host_identity=self.server_identity,
                require_loopback_transport=True,
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
            "hub_id": hub_id,
            "host_server_identity": self.server_identity,
            "message": (
                "This AgentsServer hosts the local-only Team Hub preview."
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

    async def _close_and_drain(self) -> None:
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

    def reopen_admission_sync(self) -> None:
        """Reopen after a synchronous restart signal worker fails.

        Request admission only holds the asyncio condition long enough to
        increment a counter; changing the guarded flag cannot strand a waiter.
        The next request observes the restored value without crossing event
        loops from Starlette's background thread.
        """

        with self._guard:
            self._accepting = self._delegate is not None and self._store is not None

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
