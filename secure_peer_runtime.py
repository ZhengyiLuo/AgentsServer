"""AgentsServer lifecycle and local-control boundary for secure peer V1."""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import threading
import time
from typing import Any, Mapping
from urllib.parse import quote
import uuid

from agentsdock_team_hub.secure_peer import (
    PAIRING_STATUS_LIMIT,
    SecurePeerClient,
    SecurePeerError,
    SecurePeerGateway,
    SecurePeerStore,
    canonical_peer_ipv4,
    canonical_peer_port,
)
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from agentsdock_team_hub.security import canonical_json, ensure_private_directory
from agentsdock_team_hub.store import HubError, HubStore
from secure_peer_delivery import SecurePeerDeliveryLedger


SECURE_PEER_CONTROL_VERSION = 1
SECURE_PEER_PROXY_PREFIX = "/api/team-hub-secure"
_PAIRING_ACTIONABLE_STATUSES = frozenset(
    {"requesting", "pending_approval", "approved", "connected"}
)
_CONFIG_KEYS = {
    "version",
    "server_identity",
    "enabled",
    "advertised_host",
    "listen_port",
}


class _UnavailableSecurePeerClient:
    """Fail-closed placeholder that keeps the optional feature boot-isolated."""

    def __init__(self, message: str) -> None:
        self.message = message

    def list_connections(self) -> list[dict[str, Any]]:
        return []

    def __getattr__(self, _name: str):
        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise SecurePeerError(
                "secure_peer_state_unavailable",
                self.message,
                503,
            )

        return unavailable


def _iso8601(value: Any) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint_public_key_pem(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        from cryptography.hazmat.primitives import serialization

        key = serialization.load_pem_public_key(value.encode("ascii"))
        der = key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except Exception:
        return None
    return "sha256:" + hashlib.sha256(der).hexdigest()


def _stable_uuid4(label: str) -> str:
    value = bytearray(hashlib.sha256(label.encode("utf-8")).digest()[:16])
    value[6] = (value[6] & 0x0F) | 0x40
    value[8] = (value[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(value)))


def _safe_status_error(value: Any) -> str:
    """Project a peer/local exception without controls or oversized UTF-8."""

    try:
        raw = str(value)
    except Exception:
        raw = "Secure peer operation failed"
    printable = "".join(
        " " if ord(character) < 0x20 or ord(character) == 0x7F else character
        for character in raw
    ).strip()
    if not printable:
        printable = "Secure peer operation failed"
    encoded = printable.encode("utf-8", "replace")
    if len(encoded) > 400:
        printable = encoded[:400].decode("utf-8", "ignore").rstrip()
    return printable or "Secure peer operation failed"


class SecurePeerRuntime:
    """Own the durable client, optional host listener, and Hub adapter."""

    def __init__(
        self,
        data_dir: Path,
        *,
        server_identity: str,
        server_instance_id: str,
        display_name: str | None = None,
        logger: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.server_identity = str(server_identity)
        self.server_instance_id = str(server_instance_id)
        self.display_name = str(display_name or socket.gethostname() or "AgentsServer")[:160]
        self.logger = logger
        self.config_path = self.data_dir / "host-config.json"
        self._guard = threading.RLock()
        # Linearizes durable outbound intent creation with every local
        # route/connection retirement boundary. A handoff is either durably
        # pending before retirement (so retirement returns 409) or observes
        # the retired route and cannot be created.
        self._outbound_guard = threading.RLock()
        self._peer_admission = threading.Condition(threading.RLock())
        self._peer_accepting = False
        self._peer_in_flight = 0
        self._hub_store: HubStore | None = None
        self._host_store: SecurePeerStore | None = None
        self._adapter: SecurePeerHubAdapter | None = None
        self._gateway: SecurePeerGateway | None = None
        self._host_error: str | None = None
        self._client_error: str | None = None
        self._initialization_error: str | None = None
        # Cross-server chat authority is deliberately opened only after the
        # connector, both route-revision CAS boundaries, and the durable local
        # delivery ledger are attached.  Pairing/Teamspace never implicitly
        # grants this later capability.
        # This release ships the complete route/relay connector together with
        # its durable consent epoch. Pairing never grants chat authority by
        # itself: the requested cross_chat scopes, host approval, and two
        # explicit published route revisions are all still required.
        self._relay_enabled = True
        self._remote_routes_cache: dict[str, list[dict[str, Any]]] = {}
        self._remote_routes_refreshed_at: dict[str, int] = {}
        self._delivery_target_validator: Any = None
        try:
            ensure_private_directory(self.data_dir)
            self._config = self._read_config()
            self.client: SecurePeerClient | _UnavailableSecurePeerClient = (
                SecurePeerClient(
                    self.data_dir / "client",
                    self.server_identity,
                    self.display_name,
                    pairing_capacity_lock=self._guard,
                    external_actionable_pairing_count=(
                        self._host_actionable_pairing_count
                    ),
                )
            )
            self.delivery_ledger: SecurePeerDeliveryLedger | None = (
                SecurePeerDeliveryLedger(self.data_dir / "deliveries.sqlite3")
            )
        except Exception as exc:
            # Secure peer is optional. Quarantine its state rather than taking
            # down the entire local control server, but never start a listener
            # or use credentials from a state tree that failed validation.
            self._config = self._default_config()
            self._initialization_error = (
                "Secure peer state failed safety validation; repair or remove "
                "it before enabling secure pairing."
            )
            self._host_error = self._initialization_error
            self.client = _UnavailableSecurePeerClient(self._initialization_error)
            self.delivery_ledger = None
            if self.logger is not None:
                self.logger.error(
                    "secure peer state quarantined error_type=%s",
                    type(exc).__name__,
                )

    def _default_config(self) -> dict[str, Any]:
        return {
            "version": 1,
            "server_identity": self.server_identity,
            "enabled": False,
            "advertised_host": None,
            "listen_port": 7851,
        }

    def _read_config(self) -> dict[str, Any]:
        try:
            descriptor = os.open(
                self.config_path,
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            return self._default_config()
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or not 2 <= info.st_size <= 4096
            ):
                raise PermissionError("secure peer host configuration is unsafe")
            raw = os.read(descriptor, 4097)
            if len(raw) != info.st_size:
                raise PermissionError("secure peer host configuration changed while reading")
        finally:
            os.close(descriptor)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PermissionError("secure peer host configuration is invalid") from exc
        if not isinstance(value, dict) or set(value) != _CONFIG_KEYS:
            raise PermissionError("secure peer host configuration fields are invalid")
        if value.get("version") != 1 or value.get("server_identity") != self.server_identity:
            raise PermissionError("secure peer host configuration identity changed")
        enabled = value.get("enabled")
        if type(enabled) is not bool:
            raise PermissionError("secure peer host configuration is invalid")
        try:
            port = canonical_peer_port(value.get("listen_port"))
            host = (
                canonical_peer_ipv4(value.get("advertised_host"))
                if value.get("advertised_host") is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise PermissionError("secure peer host configuration is invalid") from exc
        if enabled != (host is not None):
            raise PermissionError("secure peer host configuration is invalid")
        return {**value, "listen_port": port, "advertised_host": host}

    def _write_config(self, value: Mapping[str, Any]) -> None:
        exact = dict(value)
        if set(exact) != _CONFIG_KEYS:
            raise ValueError("secure peer host configuration fields are invalid")
        encoded = canonical_json(exact) + b"\n"
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(self.data_dir, directory_flags)
        temporary_name = f".host-config.{os.getpid()}.{threading.get_ident()}.tmp"
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory,
            )
            os.fchmod(descriptor, 0o600)
            written = 0
            while written < len(encoded):
                written += os.write(descriptor, encoded[written:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.replace(temporary_name, self.config_path.name, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        except BaseException:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise
        finally:
            os.close(directory)

    def mark_host_unavailable(self, message: str) -> None:
        with self._guard:
            self._host_error = _safe_status_error(message)

    def _host_actionable_pairing_count(self) -> int:
        store = self._host_store
        return store.actionable_pairing_count() if store is not None else 0

    def _client_actionable_pairing_count(self) -> int:
        return self.client.actionable_pairing_count()

    def attach_host_hub(
        self,
        *,
        hub_id: str,
        hub_data_dir: Path,
        hub_store: HubStore | None = None,
    ) -> None:
        """Attach after the authoritative Hub has acquired its runtime lease."""

        del hub_data_dir
        if self._initialization_error is not None:
            return
        if hub_store is None:
            raise RuntimeError("secure peer host attachment requires the live Hub store")
        with self._guard:
            if self._hub_store is not None and self._hub_store is not hub_store:
                raise RuntimeError("secure peer host is already attached")
            host_store = SecurePeerStore(
                self.data_dir / "host",
                self.server_identity,
                str(hub_id),
                cross_chat_enabled=lambda: self._relay_enabled,
                pairing_capacity_lock=self._guard,
                external_actionable_pairing_count=(
                    self._client_actionable_pairing_count
                ),
            )
            consent = host_store.cross_chat_consent_status()
            if int(consent.get("consent_epoch") or 0) == 0:
                seed = bytearray(hashlib.sha256(
                    (
                        "AgentsDock secure peer consent v1\0"
                        + self.server_identity
                        + "\0"
                        + str(hub_id)
                    ).encode("utf-8")
                ).digest()[:16])
                seed[6] = (seed[6] & 0x0F) | 0x40
                seed[8] = (seed[8] & 0x3F) | 0x80
                host_store.activate_cross_chat_consent(
                    expected_epoch=0,
                    idempotency_key=str(uuid.UUID(bytes=bytes(seed))),
                    activated_by="agentsserver-runtime-v1",
                )
            adapter = SecurePeerHubAdapter(hub_store)
            # Recover the approval -> service-principal transaction boundary.
            pairings = {
                item.get("pairing_id"): item
                for item in host_store.list_pairings(status=None)
            }
            for peer in host_store.list_peers(team_id=None):
                if peer.get("status") == "active":
                    pairing = pairings.get(peer.get("pairing_id")) or {}
                    adapter.provision_peer(
                        peer,
                        display_name=str(
                            peer.get("peer_display_name")
                            or pairing.get("peer_display_name")
                            or peer.get("peer_server_identity")
                        )[:160],
                    )
                elif peer.get("team_id"):
                    adapter.revoke_peer(
                        peer_id=str(peer["peer_id"]),
                        team_id=str(peer["team_id"]),
                    )
            self._hub_store = hub_store
            self._host_store = host_store
            self._adapter = adapter
            self._host_error = None
            if self._config["enabled"]:
                gateway = SecurePeerGateway(
                    host_store,
                    str(self._config["advertised_host"]),
                    int(self._config["listen_port"]),
                    forwarder=self._forward_peer_request,
                    resource_team_resolver=adapter.resource_team,
                    relay_enabled=lambda: self._relay_enabled,
                )
                gateway.start()
                self._gateway = gateway
            with self._peer_admission:
                self._peer_accepting = self._gateway is not None
                self._peer_admission.notify_all()

    def configure_host(
        self,
        *,
        enabled: bool,
        advertised_host: str | None,
        listen_port: int,
    ) -> dict[str, Any]:
        if self._initialization_error is not None:
            raise SecurePeerError(
                "secure_peer_state_unavailable",
                self._initialization_error,
                503,
            )
        if type(enabled) is not bool:
            raise SecurePeerError("invalid_request", "Host setting is invalid", 422)
        host = canonical_peer_ipv4(advertised_host) if advertised_host is not None else None
        port = canonical_peer_port(listen_port)
        if enabled != (host is not None):
            raise SecurePeerError("invalid_request", "Advertised IP is required exactly when hosting", 422)
        with self._guard:
            if enabled and (self._host_store is None or self._adapter is None):
                raise SecurePeerError(
                    "host_unavailable",
                    "Secure hosting requires the active designated Team Hub",
                    409,
                )
            old_gateway = self._gateway
            old_config = dict(self._config)
            if enabled and old_gateway is not None and (
                old_gateway.address == (host, port)
            ):
                return self.status()
        # Disable admission and drain every already accepted mTLS request
        # before stopping or rebinding the listener.  Returning from this
        # control mutation therefore proves that no request from the prior
        # endpoint can commit afterward.
        self.close_host_admission()
        with self._guard:
            old_gateway = self._gateway
            old_config = dict(self._config)
            new_gateway: SecurePeerGateway | None = None
            try:
                if old_gateway is not None:
                    old_gateway.stop()
                    self._gateway = None
                if enabled:
                    assert self._host_store is not None and self._adapter is not None and host is not None
                    new_gateway = SecurePeerGateway(
                        self._host_store,
                        host,
                        port,
                        forwarder=self._forward_peer_request,
                        resource_team_resolver=self._adapter.resource_team,
                        relay_enabled=lambda: self._relay_enabled,
                    )
                    new_gateway.start()
                next_config = {
                    "version": 1,
                    "server_identity": self.server_identity,
                    "enabled": enabled,
                    "advertised_host": host,
                    "listen_port": port,
                }
                self._write_config(next_config)
                self._config = next_config
                self._gateway = new_gateway
                self._host_error = None
            except BaseException:
                if new_gateway is not None:
                    new_gateway.stop()
                # Restore the previous live listener when persistence failed.
                if old_config["enabled"] and self._host_store is not None and self._adapter is not None:
                    restored = SecurePeerGateway(
                        self._host_store,
                        str(old_config["advertised_host"]),
                        int(old_config["listen_port"]),
                        forwarder=self._forward_peer_request,
                        resource_team_resolver=self._adapter.resource_team,
                        relay_enabled=lambda: self._relay_enabled,
                    )
                    restored.start()
                    self._gateway = restored
                raise
            finally:
                if self._gateway is not None:
                    self.reopen_host_admission()
            return self.status()

    def begin_pairing(
        self,
        *,
        host: str,
        port: int,
        expected_ca_fingerprint: str | None,
        request_id: str,
        display_name: str,
        requested_scopes: list[str],
    ) -> dict[str, Any]:
        # The core persists the key/request before network delivery so an
        # ambiguous response can be retried with the exact same signed bytes.
        result = self.client.begin_pairing(
            host,
            port,
            expected_ca_fingerprint=expected_ca_fingerprint,
            request_id=request_id,
            requested_scopes=requested_scopes,
            display_name=display_name,
            resume_matching=True,
        )
        return self._outgoing_pairing(result)

    def poll_pairing(self, pairing_id: str) -> dict[str, Any]:
        connection = self._outgoing_for_pairing(pairing_id)
        result = self.client.poll_pairing(str(connection["connection_id"]))
        return self._outgoing_pairing(result)

    def cancel_pairing(self, pairing_id: str, *, idempotency_key: str) -> dict[str, Any]:
        connection = self._outgoing_for_pairing(pairing_id)
        self.client.cancel_pairing(
            str(connection["connection_id"]),
            idempotency_key=idempotency_key,
        )
        return self.status()

    def activate_pairing(
        self,
        pairing_id: str,
        *,
        expected_connection_id: str,
        expected_host_server_identity: str,
        expected_hub_id: str,
    ) -> dict[str, Any]:
        with self._outbound_guard:
            connection = self._outgoing_for_pairing(pairing_id)
            if (
                connection.get("connection_id") != expected_connection_id
                or connection.get("host_server_identity")
                != expected_host_server_identity
                or connection.get("hub_id") != expected_hub_id
            ):
                raise SecurePeerError(
                    "pairing_changed",
                    "Secure peer identity changed before activation",
                    409,
                )
            active = next(
                (
                    item
                    for item in self.client.list_connections()
                    if item.get("active")
                ),
                None,
            )
            active_id = str((active or {}).get("connection_id") or "")
            if active_id and active_id != expected_connection_id:
                # Switching would abandon every route published through the old
                # connection because the connector intentionally polls only one
                # active peer. Require an explicit, fully acknowledged
                # retirement/forget of the old connection first.
                raise SecurePeerError(
                    "active_connection_conflict",
                    "Forget the active secure peer before activating another one",
                    409,
                )
            self.client.set_active_connection(
                expected_connection_id,
                expected_current=active_id or None,
            )
        return self.status()

    def deactivate_connection(
        self,
        connection_id: str,
        *,
        expected_host_server_identity: str,
        expected_hub_id: str,
    ) -> dict[str, Any]:
        with self._outbound_guard:
            self._require_connection_delivery_quiescent(connection_id)
            self.client.deactivate_connection(
                connection_id,
                expected_host_server_identity=expected_host_server_identity,
                expected_hub_id=expected_hub_id,
            )
        return self.status()

    def forget_connection(
        self,
        connection_id: str,
        *,
        expected_host_server_identity: str,
        expected_hub_id: str,
        expected_certificate_fingerprint: str,
    ) -> dict[str, Any]:
        with self._outbound_guard:
            self._require_connection_delivery_quiescent(connection_id)
            self._retire_client_routes_for_connection(connection_id)
            self.client.forget_connection(
                connection_id,
                expected_host_server_identity=expected_host_server_identity,
                expected_hub_id=expected_hub_id,
                expected_certificate_fingerprint=expected_certificate_fingerprint,
            )
        return self.status()

    def _require_connection_delivery_quiescent(self, connection_id: str) -> None:
        if self.delivery_ledger is None:
            raise SecurePeerError(
                "secure_peer_unavailable",
                "Secure peer durable delivery state is unavailable",
                503,
            )
        pending_outbound = self.delivery_ledger.pending_outbound_for_connection(
            connection_id
        )
        pending_inbound = self.delivery_ledger.nonterminal_for_connection(
            connection_id
        )
        if pending_outbound or pending_inbound:
            raise SecurePeerError(
                "connection_delivery_pending",
                "Wait for encrypted peer deliveries to finish before changing this connection",
                409,
            )

    def _retire_client_routes_for_connection(self, connection_id: str) -> None:
        try:
            # A previous offline attempt may already have created local
            # tombstones. They must be acknowledged remotely before any key or
            # connection row can be deleted, or the remote route could remain
            # usable forever with no local outbox able to revoke it.
            self.client.flush_pending_route_revocations_for_connection(
                connection_id
            )
        except SecurePeerError as exc:
            self._client_error = _safe_status_error(exc.message)
            raise
        routes = [
            route
            for route in self.client.list_published_routes()
            if str(route.get("connection_id") or "") == connection_id
            and route.get("status") in {"publishing", "active"}
        ]
        for route in routes:
            route_id = str(route.get("route_id") or "")
            revision = str(route.get("revision") or "")
            try:
                self.client.revoke_published_route(
                    connection_id,
                    route_id,
                    revision,
                    _stable_uuid4(
                        "secure-peer-connection-retire\0"
                        + self.server_identity
                        + "\0"
                        + connection_id
                        + "\0"
                        + route_id
                        + "\0"
                        + revision
                    ),
                )
            except SecurePeerError as exc:
                self._client_error = _safe_status_error(exc.message)
                raise

    def _outgoing_for_pairing(self, pairing_id: str) -> dict[str, Any]:
        matches = [
            item
            for item in self.client.list_connections()
            if item.get("pairing_id") == pairing_id
        ]
        if len(matches) != 1:
            raise SecurePeerError("pairing_unavailable", "Pairing is unavailable", 404)
        return matches[0]

    @staticmethod
    def _status(value: Any, *, active: bool = False) -> str:
        if active and value == "approved":
            return "connected"
        return {
            "requesting": "requesting",
            "pending": "pending_approval",
            "pending_approval": "pending_approval",
            "approved": "approved",
            "connected": "connected" if active else "approved",
            "deactivated": "approved",
            "active": "approved",
            "rejected": "rejected",
            "revoked": "revoked",
            "cancelled": "rejected",
            "expired": "expired",
            "error": "error",
        }.get(str(value), "error")

    def _incoming_pairing(self, item: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = str(item.get("source_endpoint") or item.get("remote_endpoint") or "")
        public_fp = item.get("peer_public_key_fingerprint") or _fingerprint_public_key_pem(
            item.get("peer_public_key_pem")
        )
        peer_id = item.get("peer_id")
        return {
            "id": item.get("pairing_id"),
            "direction": "incoming",
            "status": self._status(item.get("status")),
            "peer_server_identity": item.get("peer_server_identity"),
            "peer_display_name": item.get("peer_display_name") or item.get("peer_server_identity"),
            "remote_endpoint": endpoint,
            "host_server_identity": self.server_identity,
            "host_ca_fingerprint": self._host_store.ca_fingerprint if self._host_store else None,
            "peer_public_key_fingerprint": public_fp,
            "transcript_hash": item.get("transcript_hash"),
            "sas_words": item.get("sas_words") or [],
            "requested_scopes": item.get("requested_scopes") or [],
            "granted_scopes": item.get("scopes") or [],
            "team_id": item.get("team_id"),
            "team_display_name": item.get("team_display_name"),
            "hub_id": self._host_store.hub_id if self._host_store else None,
            # On a host, the durable peer record is the local connection
            # audience used for route publication. Pending requests do not
            # acquire one before approval commits.
            "connection_id": peer_id,
            "local_proxy_base_path": None,
            "certificate_expires_at": _iso8601(item.get("certificate_expires_at")),
            "certificate_fingerprint": item.get("certificate_fingerprint"),
            "last_seen_at": _iso8601(item.get("last_seen_at")),
            "expires_at": _iso8601(item.get("expires_at")),
            "error": item.get("error"),
        }

    def _outgoing_pairing(self, item: Mapping[str, Any]) -> dict[str, Any]:
        connection_id = str(item.get("connection_id"))
        active = bool(item.get("active"))
        return {
            "id": item.get("pairing_id"),
            "direction": "outgoing",
            "status": self._status(item.get("status"), active=active),
            "peer_server_identity": item.get("host_server_identity"),
            "peer_display_name": item.get("host_display_name") or item.get("host_server_identity"),
            "remote_endpoint": f"{item.get('host_ip')}:{item.get('port')}",
            "host_server_identity": item.get("host_server_identity"),
            "host_ca_fingerprint": item.get("host_ca_fingerprint"),
            "peer_public_key_fingerprint": item.get("peer_public_key_fingerprint"),
            "transcript_hash": item.get("transcript_hash"),
            "sas_words": item.get("sas_words") or [],
            "requested_scopes": item.get("requested_scopes") or [],
            "granted_scopes": item.get("scopes") or [],
            "team_id": item.get("team_id"),
            "team_display_name": item.get("team_display_name"),
            "hub_id": item.get("hub_id"),
            "connection_id": connection_id,
            "local_proxy_base_path": (
                f"{SECURE_PEER_PROXY_PREFIX}/{connection_id}" if active else None
            ),
            "certificate_expires_at": _iso8601(item.get("certificate_expires_at")),
            "certificate_fingerprint": item.get("certificate_fingerprint"),
            "last_seen_at": _iso8601(item.get("last_validated_at")),
            "expires_at": _iso8601(item.get("expires_at") or item.get("pairing_expires_at")),
            "error": item.get("error"),
        }

    def list_pairings(self, *, team_id: str | None, status: str | None) -> dict[str, Any]:
        with self._guard:
            store = self._host_store
        if store is None:
            return {"pairings": []}
        pending = status in {"pending", "pending_approval"}
        return {
            "pairings": [
                self._incoming_pairing(item)
                for item in store.list_pairings(
                    # Pending entries are deliberately unassigned and are
                    # visible only after the service proves team ownership.
                    # Historical rows remain strictly team-scoped.
                    team_id=None if pending else team_id,
                    status="pending" if pending else None,
                )
            ]
        }

    def approve_pairing(self, **values: Any) -> dict[str, Any]:
        with self._guard:
            store, adapter = self._host_store, self._adapter
        if store is None or adapter is None:
            raise HubError("secure_peer_unavailable", "Secure peer pairing is unavailable", 503)
        try:
            # Validate the target in the live Hub before certificate/peer
            # approval commits in the separate secure-peer database.  A
            # stale or mistyped team can therefore never poison restart
            # reconciliation with a permanently approved, unprovisionable
            # peer.
            adapter.preflight_team(str(values["team_id"]))
            all_pairings = store.list_pairings(team_id=None, status=None)
            pairing = next(
                (
                    item
                    for item in all_pairings
                    if item.get("pairing_id") == values["pairing_id"]
                ),
                None,
            )
            if pairing is None:
                raise SecurePeerError(
                    "pairing_unavailable",
                    "Secure peer pairing is unavailable",
                    404,
                )
            if pairing.get("status") == "approved":
                # Recover a core-approval -> Hub-provision split even when the
                # desktop had no response and generated a new operation UUID.
                # Recovery is authorized only by an exact immutable target.
                if (
                    pairing.get("peer_server_identity")
                    != values["expected_peer_server_identity"]
                    or pairing.get("transcript_hash")
                    != values["expected_transcript_hash"]
                    or pairing.get("team_id") != values["team_id"]
                    or list(pairing.get("scopes") or []) != list(values["scopes"])
                ):
                    raise SecurePeerError(
                        "pairing_changed",
                        "Secure peer approval changed before recovery",
                        409,
                    )
                recovered_peer = next(
                    (
                        item
                        for item in store.list_peers(team_id=values["team_id"])
                        if item.get("pairing_id") == values["pairing_id"]
                    ),
                    None,
                )
                if recovered_peer is None:
                    raise SecurePeerError(
                        "pairing_incomplete",
                        "Secure peer approval is missing its peer record",
                        409,
                    )
                result = {"peer_id": recovered_peer.get("peer_id")}
            else:
                result = store.approve_pairing(**values)
                all_pairings = store.list_pairings(team_id=None, status=None)
                pairing = next(
                    item
                    for item in all_pairings
                    if item.get("pairing_id") == values["pairing_id"]
                )
            peer = next(
                item for item in store.list_peers(team_id=values["team_id"])
                if item.get("peer_id") == result.get("peer_id")
            )
            adapter.provision_peer(
                peer,
                display_name=str(pairing.get("peer_display_name") or peer["peer_server_identity"]),
            )
            return {"pairing": self._incoming_pairing({**pairing, **peer})}
        except SecurePeerError as exc:
            raise HubError(exc.code, exc.message, exc.status_code) from exc

    def reject_pairing(self, **values: Any) -> dict[str, Any]:
        with self._guard:
            store = self._host_store
        if store is None:
            raise HubError("secure_peer_unavailable", "Secure peer pairing is unavailable", 503)
        try:
            store.reject_pairing(**values)
            pairing = next(
                item
                for item in store.list_pairings(team_id=None, status=None)
                if item.get("pairing_id") == values["pairing_id"]
            )
            return {"pairing": self._incoming_pairing(pairing)}
        except SecurePeerError as exc:
            raise HubError(exc.code, exc.message, exc.status_code) from exc

    def list_peers(self, *, team_id: str | None) -> dict[str, Any]:
        with self._guard:
            store = self._host_store
        if store is None:
            return {"peers": []}
        pairings = {
            item.get("pairing_id"): item
            for item in store.list_pairings(team_id=None, status=None)
        }
        return {
            "peers": [
                self._incoming_pairing({**pairings.get(item.get("pairing_id"), {}), **item})
                for item in store.list_peers(team_id=team_id)
            ]
        }

    def revoke_peer(self, **values: Any) -> dict[str, Any]:
        with self._guard:
            store, adapter = self._host_store, self._adapter
        if store is None or adapter is None:
            raise HubError("secure_peer_unavailable", "Secure peer pairing is unavailable", 503)
        try:
            store.revoke_peer(**values)
            # Always replay the Hub-side revoke after a cached core response.
            adapter.revoke_peer(peer_id=values["peer_id"], team_id=values["team_id"])
            peers = store.list_peers(team_id=values["team_id"])
            peer = next(item for item in peers if item.get("peer_id") == values["peer_id"])
            pairings = store.list_pairings(team_id=None, status=None)
            pairing = next(
                (item for item in pairings if item.get("pairing_id") == peer.get("pairing_id")),
                {},
            )
            return {"peer": self._incoming_pairing({**pairing, **peer})}
        except SecurePeerError as exc:
            raise HubError(exc.code, exc.message, exc.status_code) from exc

    def _client_connection(self, connection_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.client.list_connections()
                if str(item.get("connection_id") or "") == connection_id
            ),
            None,
        )

    def _host_peer(self, connection_id: str) -> dict[str, Any] | None:
        with self._guard:
            store = self._host_store
        if store is None:
            return None
        return next(
            (
                item
                for item in store.list_peers(team_id=None)
                if str(item.get("peer_id") or "") == connection_id
            ),
            None,
        )

    @staticmethod
    def _route_projection(
        route: Mapping[str, Any],
        *,
        connection_id: str,
        peer_server_identity: str,
        peer_display_name: str,
        chat_id: str | None = None,
    ) -> dict[str, Any]:
        projected = {
            "peer_server_identity": peer_server_identity,
            "peer_display_name": peer_display_name,
            "connection_id": connection_id,
            "route_id": route.get("route_id"),
            "revision": route.get("revision"),
            "alias": route.get("alias"),
            "display_title": route.get("display_title"),
            "actions": list(route.get("actions") or []),
        }
        if chat_id is not None:
            projected.update({
                "chat_id": chat_id,
                "status": route.get("status") or "active",
            })
        return projected

    def _published_routes(self) -> list[dict[str, Any]]:
        routes: list[dict[str, Any]] = []
        connections = {
            str(item.get("connection_id") or ""): item
            for item in self.client.list_connections()
        }
        for route in self.client.list_published_routes():
            connection_id = str(route.get("connection_id") or "")
            connection = connections.get(connection_id)
            if connection is None:
                continue
            peer_identity = str(connection.get("host_server_identity") or "")
            routes.append(self._route_projection(
                route,
                connection_id=connection_id,
                peer_server_identity=peer_identity,
                peer_display_name=str(
                    connection.get("host_display_name") or peer_identity
                ),
                chat_id=str(route.get("chat_id") or ""),
            ))
        with self._guard:
            store = self._host_store
        if store is not None:
            for route in store.list_local_routes(include_revoked=True):
                routes.append(self._route_projection(
                    route,
                    connection_id=str(route.get("audience_peer_id") or ""),
                    peer_server_identity=str(
                        route.get("audience_peer_server_identity") or ""
                    ),
                    peer_display_name=str(
                        route.get("audience_peer_display_name")
                        or route.get("audience_peer_server_identity")
                        or "Paired server"
                    ),
                    chat_id=str(route.get("chat_id") or ""),
                ))
        return routes

    def _remote_routes(self) -> list[dict[str, Any]]:
        routes: list[dict[str, Any]] = []
        with self._guard:
            cache = {
                key: [dict(item) for item in value]
                for key, value in self._remote_routes_cache.items()
            }
            store = self._host_store
        for connection_id, values in cache.items():
            connection = self._client_connection(connection_id)
            if connection is None or not connection.get("active"):
                continue
            peer_identity = str(connection.get("host_server_identity") or "")
            for route in values:
                routes.append(self._route_projection(
                    route,
                    connection_id=connection_id,
                    peer_server_identity=peer_identity,
                    peer_display_name=str(
                        connection.get("host_display_name") or peer_identity
                    ),
                ))
        if store is not None:
            for peer in store.list_peers(team_id=None):
                if peer.get("status") != "active":
                    continue
                peer_id = str(peer.get("peer_id") or "")
                for route in store.list_remote_routes_for_peer(
                    peer_id,
                    include_revoked=False,
                ):
                    routes.append(self._route_projection(
                        route,
                        connection_id=peer_id,
                        peer_server_identity=str(
                            route.get("peer_server_identity")
                            or peer.get("peer_server_identity")
                            or ""
                        ),
                        peer_display_name=str(
                            route.get("peer_display_name")
                            or peer.get("peer_display_name")
                            or peer.get("peer_server_identity")
                            or "Paired server"
                        ),
                    ))
        return routes

    def publish_route(
        self,
        *,
        connection_id: str,
        chat_id: str,
        alias: str,
        display_title: str,
        actions: list[str],
        idempotency_key: str,
    ) -> dict[str, Any]:
        if not self.remote_route_delivery_available():
            raise SecurePeerError(
                "remote_route_delivery_unavailable",
                "Secure peer chat delivery is unavailable",
                503,
            )
        connection = self._client_connection(connection_id)
        if connection is not None:
            if not connection.get("active"):
                raise SecurePeerError(
                    "connection_unavailable",
                    "Secure peer connection is not active",
                    409,
                )
            self.client.publish_route(
                connection_id,
                chat_id,
                alias,
                display_title,
                actions,
            )
            return self.status()
        peer = self._host_peer(connection_id)
        with self._guard:
            store = self._host_store
        if peer is None or store is None or peer.get("status") != "active":
            raise SecurePeerError(
                "connection_unavailable",
                "Secure peer connection is unavailable",
                404,
            )
        store.publish_local_route(
            str(peer.get("team_id") or ""),
            connection_id,
            chat_id,
            alias,
            display_title,
            actions,
            idempotency_key=idempotency_key,
            published_by=f"host-admin:{self.server_identity}",
        )
        return self.status()

    def revoke_route(
        self,
        *,
        route_id: str,
        expected_connection_id: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self._outbound_guard:
            self._require_route_outbound_quiescent(
                expected_connection_id,
                route_id,
                expected_revision,
            )
            return self._revoke_route_locked(
                route_id=route_id,
                expected_connection_id=expected_connection_id,
                expected_revision=expected_revision,
                idempotency_key=idempotency_key,
            )

    def _require_route_outbound_quiescent(
        self,
        connection_id: str,
        route_id: str,
        revision: str,
    ) -> None:
        if self.delivery_ledger is None:
            raise SecurePeerError(
                "secure_peer_unavailable",
                "Secure peer durable delivery state is unavailable",
                503,
            )
        if self.delivery_ledger.pending_outbound_for_route(
            connection_id,
            route_id,
            revision,
        ):
            raise SecurePeerError(
                "outbound_handoff_pending",
                "Wait for the encrypted handoff to finish before retiring this route",
                409,
            )

    def _revoke_route_locked(
        self,
        *,
        route_id: str,
        expected_connection_id: str,
        expected_revision: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        published = next(
            (
                item
                for item in self._published_routes()
                if str(item.get("route_id") or "") == route_id
            ),
            None,
        )
        if (
            published is None
            or published.get("connection_id") != expected_connection_id
        ):
            raise SecurePeerError(
                "route_changed",
                "Published secure peer route is unavailable or changed",
                409,
            )
        connection = self._client_connection(expected_connection_id)
        if connection is not None:
            try:
                self.client.revoke_published_route(
                    expected_connection_id,
                    route_id,
                    expected_revision,
                    idempotency_key,
                )
            except SecurePeerError as exc:
                if exc.status_code < 500 and exc.status_code not in {408, 425, 429}:
                    raise
                self._client_error = _safe_status_error(exc.message)
            return self.status()
        peer = self._host_peer(expected_connection_id)
        with self._guard:
            store = self._host_store
        if peer is None or store is None:
            raise SecurePeerError(
                "connection_unavailable",
                "Secure peer connection is unavailable",
                404,
            )
        store.revoke_route(
            route_id,
            str(peer.get("team_id") or ""),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            revoked_by=f"host-admin:{self.server_identity}",
        )
        return self.status()

    def route_local_chat(
        self,
        *,
        route_id: str,
        expected_connection_id: str,
        expected_revision: str,
    ) -> str:
        route = next((
            item
            for item in self._published_routes()
            if str(item.get("route_id") or "") == route_id
            and str(item.get("connection_id") or "")
            == expected_connection_id
            and str(item.get("revision") or "") == expected_revision
            and item.get("status") in {"publishing", "active"}
        ), None)
        if route is None or not str(route.get("chat_id") or ""):
            raise SecurePeerError(
                "route_changed",
                "Published secure peer route is unavailable or changed",
                409,
            )
        return str(route["chat_id"])

    def revoke_routes_for_chat(self, chat_id: str) -> int:
        """CAS-revoke every advertised route before archive/delete commits."""

        canonical_chat = str(chat_id or "")
        if not canonical_chat:
            raise SecurePeerError("invalid_request", "Chat id is invalid", 422)
        with self._outbound_guard:
            if self.delivery_ledger is None:
                raise SecurePeerError(
                    "secure_peer_unavailable",
                    "Secure peer durable delivery state is unavailable",
                    503,
                )
            if self.delivery_ledger.pending_outbound_for_chat(canonical_chat):
                raise SecurePeerError(
                    "outbound_handoff_pending",
                    "Wait for the encrypted handoff to finish before retiring this chat",
                    409,
                )
            return self._revoke_routes_for_chat_locked(canonical_chat)

    def _revoke_routes_for_chat_locked(self, canonical_chat: str) -> int:
        revoked = 0
        for route in self.client.list_published_routes():
            if (
                route.get("chat_id") != canonical_chat
                or route.get("status") not in {"publishing", "active"}
            ):
                continue
            connection_id = str(route.get("connection_id") or "")
            route_id = str(route.get("route_id") or "")
            revision = str(route.get("revision") or "")
            try:
                self.client.revoke_published_route(
                    connection_id,
                    route_id,
                    revision,
                    _stable_uuid4(
                        "secure-peer-chat-retire\0"
                        + self.server_identity
                        + "\0"
                        + canonical_chat
                        + "\0"
                        + route_id
                        + "\0"
                        + revision
                    ),
                )
            except SecurePeerError as exc:
                if exc.status_code < 500 and exc.status_code not in {408, 425, 429}:
                    raise
                # The local tombstone is already durable. The maintenance
                # outbox will replay the exact remote CAS once the peer is
                # reachable; archive/delete must not be hostage to its uptime.
                self._client_error = _safe_status_error(exc.message)
            revoked += 1
        with self._guard:
            store = self._host_store
        if store is not None:
            for route in store.list_local_routes(include_revoked=False):
                if route.get("chat_id") != canonical_chat:
                    continue
                route_id = str(route.get("route_id") or "")
                revision = str(route.get("revision") or "")
                store.revoke_route(
                    route_id,
                    str(route.get("team_id") or ""),
                    expected_revision=revision,
                    idempotency_key=_stable_uuid4(
                        "secure-peer-chat-retire\0"
                        + self.server_identity
                        + "\0"
                        + canonical_chat
                        + "\0"
                        + route_id
                        + "\0"
                        + revision
                    ),
                    revoked_by=f"host-admin:{self.server_identity}",
                )
                revoked += 1
        return revoked

    def _status_pairings(
        self,
        incoming_items: list[dict[str, Any]],
        outgoing_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ranked: list[tuple[int, str, str, dict[str, Any]]] = []
        for direction, items, projector in (
            ("incoming", incoming_items, self._incoming_pairing),
            ("outgoing", outgoing_items, self._outgoing_pairing),
        ):
            for item in items:
                public = projector(item)
                ranked.append((
                    int(item.get("updated_at") or item.get("created_at") or 0),
                    direction,
                    str(public.get("id") or ""),
                    public,
                ))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
        actionable = [
            public
            for _timestamp, _direction, _identifier, public in ranked
            if public.get("status") in _PAIRING_ACTIONABLE_STATUSES
        ]
        if len(actionable) > PAIRING_STATUS_LIMIT:
            # Admissions share the snapshot lock and reserve durable client
            # attempts, so this can only represent pre-contract/corrupt state.
            # Do not silently hide a live trust relationship from its owner.
            raise SecurePeerError(
                "pairing_capacity",
                "Actionable secure peer pairing state exceeds the safe status limit",
                503,
            )
        terminal = [
            public
            for _timestamp, _direction, _identifier, public in ranked
            if public.get("status") not in _PAIRING_ACTIONABLE_STATUSES
        ]
        return actionable + terminal[: PAIRING_STATUS_LIMIT - len(actionable)]

    def status(self) -> dict[str, Any]:
        with self._guard:
            config = dict(self._config)
            store = self._host_store
            gateway = self._gateway
            host_error = self._host_error
            connections = self.client.list_connections()
            incoming_items: list[dict[str, Any]] = []
            if store is not None:
                pairings = {
                    item.get("pairing_id"): item
                    for item in store.list_pairings(team_id=None, status=None)
                }
                peers = {
                    item.get("pairing_id"): item
                    for item in store.list_peers(team_id=None)
                }
                incoming_items = [
                    {**item, **peers.get(pairing_id, {})}
                    for pairing_id, item in pairings.items()
                ]
            status_pairings = self._status_pairings(incoming_items, connections)
        host_available = store is not None and self._initialization_error is None
        host = config.get("advertised_host")
        port = int(config["listen_port"])
        ca_fingerprint = store.ca_fingerprint if store is not None else None
        route_delivery_available = self.remote_route_delivery_available()
        return {
            "version": SECURE_PEER_CONTROL_VERSION,
            "server_identity": self.server_identity,
            "server_instance_id": self.server_instance_id,
            "active_connection_id": next(
                (item.get("connection_id") for item in connections if item.get("active")),
                None,
            ),
            "host": {
                "available": host_available,
                "enabled": bool(config["enabled"] and gateway is not None),
                "listen_port": port,
                "advertised_host": host,
                "advertised_hosts": [host] if host else [],
                "ca_fingerprint": ca_fingerprint,
                "pairing_link": (
                    f"agentsdock://secure-peer/join?host={host}&port={port}&fingerprint={quote(ca_fingerprint, safe='')}"
                    if host and ca_fingerprint and gateway is not None
                    else None
                ),
                "certificate_expires_at": (
                    _iso8601(store.server_certificate_expires_at)
                    if store is not None
                    and bool(config["enabled"])
                    and gateway is not None
                    else None
                ),
                "error": host_error or self._initialization_error,
            },
            "pairings": status_pairings,
            "remote_routes": (
                self._remote_routes() if route_delivery_available else []
            ),
            "published_routes": (
                self._published_routes() if route_delivery_available else []
            ),
            "remote_route_delivery_available": route_delivery_available,
            "connection_error": self._client_error,
        }

    def state_available(self) -> bool:
        """Report only whether optional secure-peer state passed safety init."""

        return self._initialization_error is None

    def state_error_code(self) -> str | None:
        return (
            None
            if self._initialization_error is None
            else "secure_peer_state_unavailable"
        )

    def _retire_remote_revoked_active_connection(
        self,
        observed: Mapping[str, Any],
        pairing_recovery: Mapping[str, Any],
    ) -> dict[str, Any]:
        connection_id = str(observed.get("connection_id") or "")
        with self._outbound_guard:
            connections = self.client.list_connections()
            current = next(
                (
                    item
                    for item in connections
                    if item.get("connection_id") == connection_id
                ),
                None,
            )
            if current is not None and (
                current.get("host_server_identity")
                != observed.get("host_server_identity")
                or current.get("hub_id") != observed.get("hub_id")
            ):
                raise SecurePeerError(
                    "connection_changed",
                    "Secure peer connection changed during revocation",
                    409,
                )
            if current is not None:
                self.client.retire_remote_revoked_connection(
                    connection_id,
                    expected_host_server_identity=str(
                        current.get("host_server_identity") or ""
                    ),
                    expected_hub_id=str(current.get("hub_id") or ""),
                    expected_certificate_fingerprint=str(
                        current.get("certificate_fingerprint") or ""
                    ),
                )
            replacement_active = any(
                item.get("active")
                and item.get("connection_id") != connection_id
                for item in connections
            )
        with self._guard:
            self._remote_routes_cache.pop(connection_id, None)
            self._remote_routes_refreshed_at.pop(connection_id, None)
        self._client_error = None
        return {
            "active": replacement_active,
            "renewed": False,
            "healthy": False,
            "revoked": True,
            "revoked_connection_id": connection_id,
            "error": "peer_revoked",
            "pairing_recovery": dict(pairing_recovery),
        }

    def maintenance_once(self) -> dict[str, Any]:
        """Renew and revalidate the one explicitly active peer connection."""

        if self._initialization_error is not None:
            return {
                "active": False,
                "renewed": False,
                "healthy": False,
                "error": "secure_peer_state_unavailable",
            }
        pairing_recovery = self.client.recover_pairing_attempts(limit=2)
        recovery_error = pairing_recovery.get("error")
        active = next(
            (item for item in self.client.list_connections() if item.get("active")),
            None,
        )
        if active is None:
            self._client_error = (
                _safe_status_error(recovery_error) if recovery_error else None
            )
            return {
                "active": False,
                "renewed": False,
                "healthy": False,
                "pairing_recovery": pairing_recovery,
            }
        connection_id = str(active.get("connection_id") or "")
        revocation_observation = active
        try:
            try:
                retired_routes = (
                    self.client.flush_pending_route_revocations_for_connection(
                        connection_id,
                        limit=8,
                    )
                )
                renewal = self.client.renew_if_due(connection_id)
                renewed_connection = renewal.get("connection")
                if (
                    isinstance(renewed_connection, Mapping)
                    and renewed_connection.get("connection_id") == connection_id
                ):
                    revocation_observation = renewed_connection
                health = self.client.peer_health(connection_id)
                remote_routes = (
                    self.client.list_remote_routes(connection_id)
                    if self._relay_enabled
                    else []
                )
            except SecurePeerError as exc:
                if exc.code == "peer_revoked" and exc.status_code == 401:
                    return self._retire_remote_revoked_active_connection(
                        revocation_observation,
                        pairing_recovery,
                    )
                raise
            with self._guard:
                self._remote_routes_cache[connection_id] = [
                    dict(item) for item in remote_routes
                ]
                self._remote_routes_refreshed_at[connection_id] = int(time.time())
            self._client_error = None
            return {
                "active": True,
                "renewed": bool(renewal.get("renewed")),
                "healthy": True,
                "hub_id": health.get("hub_id"),
                "retired_routes": retired_routes,
                "pairing_recovery": pairing_recovery,
            }
        except Exception as exc:
            self._client_error = _safe_status_error(
                exc.message if isinstance(exc, SecurePeerError) else exc
            )
            return {
                "active": True,
                "renewed": False,
                "healthy": False,
                "error": (
                    exc.code
                    if isinstance(exc, SecurePeerError)
                    else "secure_peer_maintenance_failed"
                ),
                "pairing_recovery": pairing_recovery,
            }

    def remote_route_delivery_available(self) -> bool:
        """Return true only when both relay-side route CAS gates are live."""

        if (
            not self._relay_enabled
            or self._initialization_error is not None
            or self.delivery_ledger is None
            or self._delivery_target_validator is None
        ):
            return False
        active = next(
            (item for item in self.client.list_connections() if item.get("active")),
            None,
        )
        now = int(time.time())
        client_ready = self._client_delivery_ready(active, now=now)
        with self._guard:
            store = self._host_store
            gateway = self._gateway
            host_config_enabled = bool(self._config.get("enabled"))
        with self._peer_admission:
            peer_accepting = bool(self._peer_accepting)
        host_ready = False
        if (
            store is not None
            and gateway is not None
            and host_config_enabled
            and peer_accepting
        ):
            try:
                consent = store.cross_chat_consent_status()
                epoch = int(consent.get("consent_epoch") or 0)
                host_ready = bool(
                    consent.get("runtime_enabled")
                    and epoch > 0
                    and any(
                        peer.get("status") == "active"
                        and int(peer.get("certificate_expires_at") or 0)
                        > now + 60
                        and int(peer.get("cross_chat_grant_epoch") or 0) == epoch
                        and any(
                            str(scope).startswith("cross_chat.")
                            for scope in peer.get("scopes") or []
                        )
                        for peer in store.list_peers(team_id=None)
                    )
                )
            except Exception:
                host_ready = False
        return client_ready or host_ready

    def _client_delivery_ready(
        self,
        connection: Mapping[str, Any] | None,
        *,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        return bool(
            connection
            and connection.get("active")
            and connection.get("status") == "connected"
            and connection.get("remote_route_delivery_available") is True
            and self._client_error is None
            and int(connection.get("last_validated_at") or 0)
            >= timestamp - 120
            and int(connection.get("certificate_expires_at") or 0)
            > timestamp + 60
        )

    def _host_peer_delivery_ready(
        self,
        peer: Mapping[str, Any] | None,
        *,
        now: int | None = None,
    ) -> bool:
        timestamp = int(time.time()) if now is None else int(now)
        with self._guard:
            store = self._host_store
            gateway = self._gateway
            enabled = bool(self._config.get("enabled"))
        with self._peer_admission:
            accepting = bool(self._peer_accepting)
        if (
            peer is None
            or store is None
            or gateway is None
            or not enabled
            or not accepting
            or peer.get("status") != "active"
            or int(peer.get("certificate_expires_at") or 0)
            <= timestamp + 60
            or not any(
                str(scope).startswith("cross_chat.")
                for scope in peer.get("scopes") or []
            )
        ):
            return False
        try:
            consent = store.cross_chat_consent_status()
            return bool(
                consent.get("runtime_enabled")
                and int(consent.get("consent_epoch") or 0) > 0
                and int(peer.get("cross_chat_grant_epoch") or 0)
                == int(consent.get("consent_epoch") or 0)
            )
        except Exception:
            return False

    def set_delivery_target_validator(self, validator: Any) -> None:
        """Attach the local chat admission check used before remote receipt.

        Route resolution proves the opaque route revision. AgentsServer still
        owns live chat/archive/deletion state, so that final check is injected
        here and must complete before the durable delivered receipt is sent.
        """

        if validator is not None and not callable(validator):
            raise TypeError("secure peer delivery target validator must be callable")
        with self._guard:
            self._delivery_target_validator = validator

    def validate_remote_reference(
        self,
        source_session_id: str,
        reference: Mapping[str, Any],
        *,
        expected_snapshot: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve both route grants without trusting renderer metadata."""

        if not self.remote_route_delivery_available():
            raise SecurePeerError(
                "remote_route_delivery_unavailable",
                "Secure peer chat delivery is not enabled",
                503,
            )
        action = str(reference.get("action") or "")
        connection_id = str(reference.get("target_connection_id") or "")
        target_route_id = str(reference.get("target_route_id") or "")
        target_revision = str(reference.get("target_route_revision") or "")
        target_server_identity = str(
            reference.get("target_server_identity") or ""
        )
        target = next(
            (
                route
                for route in self._remote_routes()
                if route.get("connection_id") == connection_id
                and route.get("route_id") == target_route_id
                and route.get("revision") == target_revision
                and route.get("peer_server_identity") == target_server_identity
                and action in set(route.get("actions") or [])
            ),
            None,
        )
        client_connection = self._client_connection(connection_id)
        host_peer = self._host_peer(connection_id)
        if target is None and client_connection is not None:
            with self._guard:
                refreshed_at = int(
                    self._remote_routes_refreshed_at.get(connection_id) or 0
                )
            if refreshed_at <= 0 or refreshed_at < int(time.time()) - 120:
                # An empty/stale cache is not evidence that the remote owner
                # revoked a route. In particular it is empty after restart
                # until a pinned mTLS catalog refresh succeeds. Keep durable
                # response intents retryable instead of misclassifying an
                # offline peer as an authoritative route revocation.
                raise SecurePeerError(
                    "remote_route_catalog_unavailable",
                    "Secure peer route catalog has not been freshly verified",
                    503,
                )
        sources = [
            route
            for route in self._published_routes()
            if route.get("connection_id") == connection_id
            and route.get("chat_id") == source_session_id
            and route.get("status") == "active"
            and action in set(route.get("actions") or [])
        ]
        if target is None or len(sources) != 1:
            raise SecurePeerError(
                "route_changed",
                "Secure peer route is unavailable or changed",
                409,
            )
        source = sources[0]
        if client_connection is not None:
            if not self._client_delivery_ready(client_connection):
                raise SecurePeerError(
                    "connection_unavailable",
                    "Secure peer connection is not freshly verified",
                    503,
                )
            role = "client"
            team_id = str(client_connection.get("team_id") or "")
            hub_id = str(client_connection.get("hub_id") or "")
        elif self._host_peer_delivery_ready(host_peer):
            role = "host"
            team_id = str(host_peer.get("team_id") or "")
            with self._guard:
                host_store = self._host_store
            hub_id = str(host_store.hub_id if host_store is not None else "")
        else:
            raise SecurePeerError(
                "connection_unavailable",
                "Secure peer connection is unavailable",
                409,
            )
        snapshot = {
            "version": 1,
            "role": role,
            "connection_id": connection_id,
            "team_id": team_id,
            "hub_id": hub_id,
            "source_server_identity": self.server_identity,
            "source_chat_id": source_session_id,
            "source_route_id": source.get("route_id"),
            "source_route_revision": source.get("revision"),
            "target_server_identity": target_server_identity,
            "target_route_id": target_route_id,
            "target_route_revision": target_revision,
            "action": action,
        }
        if expected_snapshot is not None and dict(expected_snapshot) != snapshot:
            raise SecurePeerError(
                "route_changed",
                "Secure peer route changed while the turn was queued",
                409,
            )
        return snapshot

    def submit_remote_handoff(
        self,
        snapshot: Mapping[str, Any],
        *,
        body: str,
        action: str,
        request_id: str,
        exchange_id: str | None = None,
        parent_envelope_id: str | None = None,
        expires_at: int | None = None,
        request_response: bool | None = None,
        expected_used_legs: int | None = None,
    ) -> dict[str, Any]:
        """Submit an initial or response leg through the exact route pair."""

        if not self.remote_route_delivery_available():
            raise SecurePeerError(
                "remote_route_delivery_unavailable",
                "Secure peer chat delivery is unavailable",
                503,
            )
        role = str(snapshot.get("role") or "")
        connection_id = str(snapshot.get("connection_id") or "")
        if role == "client":
            if not self._client_delivery_ready(
                self._client_connection(connection_id)
            ):
                raise SecurePeerError(
                    "connection_unavailable",
                    "Secure peer connection is not freshly verified",
                    503,
                )
        elif role == "host":
            if not self._host_peer_delivery_ready(
                self._host_peer(connection_id)
            ):
                raise SecurePeerError(
                    "connection_unavailable",
                    "Secure peer host route is not currently reachable",
                    503,
                )
        else:
            raise SecurePeerError(
                "connection_unavailable",
                "Secure peer route owner is unavailable",
                409,
            )
        matching_sources = [
            route
            for route in self._published_routes()
            if route.get("connection_id") == snapshot.get("connection_id")
            and route.get("chat_id") == snapshot.get("source_chat_id")
            and route.get("route_id") == snapshot.get("source_route_id")
            and route.get("revision") == snapshot.get("source_route_revision")
            and route.get("status") == "active"
            and action in set(route.get("actions") or [])
        ]
        if len(matching_sources) != 1:
            raise SecurePeerError(
                "route_changed",
                "Secure peer source route is unavailable or changed",
                409,
            )
        initial = exchange_id is None
        kind = action if initial else ("request_reply" if request_response else "response")
        deadline = int(expires_at or (time.time() + 72 * 60 * 60))
        payload = {
            "request_id": request_id,
            "source_route_id": snapshot.get("source_route_id"),
            "target_route_id": snapshot.get("target_route_id"),
            "target_route_revision": snapshot.get("target_route_revision"),
            "kind": kind,
            "exchange_id": exchange_id,
            "parent_envelope_id": parent_envelope_id,
            "expires_at": deadline,
            "body": {"message": body},
        }
        if role == "client":
            response = self.client.submit_envelope_from_published_route(
                connection_id,
                source_route_id=str(snapshot.get("source_route_id") or ""),
                source_route_revision=str(
                    snapshot.get("source_route_revision") or ""
                ),
                source_chat_id=str(snapshot.get("source_chat_id") or ""),
                action=action,
                payload=payload,
            )
        elif role == "host":
            with self._guard:
                store = self._host_store
            if store is None:
                raise SecurePeerError(
                    "host_unavailable",
                    "Secure peer host is unavailable",
                    503,
                )
            response = store.submit_local_envelope(
                str(snapshot.get("team_id") or ""),
                str(snapshot.get("source_route_id") or ""),
                payload,
            )
        else:
            raise SecurePeerError(
                "connection_unavailable",
                "Secure peer route owner is unavailable",
                409,
            )
        if not isinstance(response, dict) or set(response) != {
            "envelope_id",
            "status",
            "used_legs",
            "max_legs",
            "expires_at",
            "exchange_id",
        }:
            raise SecurePeerError(
                "remote_invalid",
                "Secure peer relay returned an invalid confirmation",
                502,
            )
        try:
            envelope_uuid = uuid.UUID(str(response.get("envelope_id") or ""))
            exchange_uuid = uuid.UUID(str(response.get("exchange_id") or ""))
        except (ValueError, AttributeError) as exc:
            raise SecurePeerError(
                "remote_invalid",
                "Secure peer relay returned invalid identifiers",
                502,
            ) from exc
        if (
            envelope_uuid.version != 4
            or str(envelope_uuid) != response.get("envelope_id")
            or exchange_uuid.version != 4
            or str(exchange_uuid) != response.get("exchange_id")
            or response.get("status")
            not in {"queued", "claimed", "delivered", "failed", "expired"}
            or type(response.get("used_legs")) is not int
            or not 1 <= int(response["used_legs"]) <= 6
            or response.get("max_legs") != 6
            or response.get("expires_at") != deadline
            or (exchange_id is not None and response.get("exchange_id") != exchange_id)
            or (
                expected_used_legs is not None
                and response.get("used_legs") != expected_used_legs
            )
            or (initial and response.get("used_legs") != 1)
        ):
            raise SecurePeerError(
                "remote_invalid",
                "Secure peer relay confirmation changed the accepted request",
                502,
            )
        return response

    def _resolve_claim_target(
        self,
        envelope: Mapping[str, Any],
        *,
        role: str,
        connection_id: str,
    ) -> tuple[str, str]:
        route_id = str(envelope.get("target_route_id") or "")
        revision = str(envelope.get("target_route_revision") or "")
        if role == "client":
            match = next(
                (
                    route
                    for route in self.client.list_published_routes()
                    if route.get("connection_id") == connection_id
                    and route.get("route_id") == route_id
                    and route.get("revision") == revision
                    and route.get("status") == "active"
                ),
                None,
            )
            if match is None:
                raise SecurePeerError(
                    "route_changed",
                    "Secure peer receive route is unavailable or changed",
                    409,
                )
            connection = self._client_connection(connection_id) or {}
            return str(match.get("chat_id") or ""), str(connection.get("team_id") or "")
        with self._guard:
            store = self._host_store
        if store is None:
            raise SecurePeerError("host_unavailable", "Secure peer host is unavailable", 503)
        match = next(
            (
                route
                for route in store.list_local_routes(
                    connection_id,
                    include_revoked=False,
                )
                if route.get("route_id") == route_id
                and route.get("revision") == revision
            ),
            None,
        )
        if match is None:
            raise SecurePeerError(
                "route_changed",
                "Secure peer receive route is unavailable or changed",
                409,
            )
        return str(match.get("chat_id") or ""), str(match.get("team_id") or "")

    def _receipt_claim(
        self,
        envelope: Mapping[str, Any],
        *,
        role: str,
        connection_id: str,
        team_id: str,
        lease_token: str,
        outcome: str,
    ) -> dict[str, Any]:
        if role == "client":
            return self.client.receipt_envelope_for_published_route(
                connection_id,
                str(envelope.get("envelope_id") or ""),
                target_route_id=str(
                    envelope.get("target_route_id") or ""
                ),
                target_route_revision=str(
                    envelope.get("target_route_revision") or ""
                ),
                lease_token=lease_token,
                outcome=outcome,
            )
        with self._guard:
            store = self._host_store
        if store is None:
            raise SecurePeerError("host_unavailable", "Secure peer host is unavailable", 503)
        return store.receipt_local_envelope(
            team_id,
            str(envelope.get("target_route_id") or ""),
            str(envelope.get("envelope_id") or ""),
            lease_token,
            outcome,
        )

    def claim_deliveries_once(self, *, limit: int = 20) -> list[dict[str, Any]]:
        # Linearize client claim + durable prepare with deactivate/forget. Once
        # retirement acquires this guard it either observes the prepared row and
        # returns 409, or completes before a claim can observe an active client.
        with self._outbound_guard:
            return self._claim_deliveries_once_locked(limit=limit)

    def _claim_deliveries_once_locked(
        self, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Claim and durably prepare inbound envelopes for lifecycle admission.

        The remote delivered receipt is intentionally deferred. AgentsServer
        must hold the target chat's lifecycle lock across that receipt and the
        durable queue/run owner handoff so archive/delete cannot cross it.
        """

        if not self.remote_route_delivery_available() or self.delivery_ledger is None:
            return []
        claims: list[tuple[str, str, str, dict[str, Any]]] = []
        lease_owner = f"agentsserver-{self.server_instance_id}"
        active = next(
            (item for item in self.client.list_connections() if item.get("active")),
            None,
        )
        if active is not None:
            connection_id = str(active.get("connection_id") or "")
            try:
                response = self.client.claim_inbox(
                    connection_id,
                    lease_owner=lease_owner,
                    limit=limit,
                )
                token = str(response.get("lease_token") or "")
                for envelope in response.get("envelopes") or []:
                    claims.append(("client", connection_id, token, dict(envelope)))
            except Exception as exc:
                self._client_error = _safe_status_error(
                    exc.message if isinstance(exc, SecurePeerError) else exc
                )
        with self._guard:
            store = self._host_store
        if store is not None:
            remaining = max(0, limit - len(claims))
            if remaining:
                try:
                    response = store.claim_local_inbox(
                        lease_owner,
                        limit=remaining,
                    )
                    token = str(response.get("lease_token") or "")
                    for envelope in response.get("envelopes") or []:
                        claims.append((
                            "host",
                            str(envelope.get("source_peer_id") or ""),
                            token,
                            dict(envelope),
                        ))
                except Exception as exc:
                    self._host_error = _safe_status_error(
                        exc.message if isinstance(exc, SecurePeerError) else exc
                    )
        ready: list[dict[str, Any]] = []
        for role, connection_id, lease_token, envelope in claims:
            if not lease_token:
                continue
            try:
                target_chat_id, team_id = self._resolve_claim_target(
                    envelope,
                    role=role,
                    connection_id=connection_id,
                )
                record, _created = self.delivery_ledger.prepare(
                    envelope,
                    transport_role=role,
                    connection_id=connection_id,
                    lease_token=lease_token,
                    target_chat_id=target_chat_id,
                )
                if record.get("state") == "prepared":
                    ready.append(record)
                elif record.get("state") in {"completed", "failed"}:
                    # A terminal local owner may be redelivered after a lost
                    # receipt acknowledgement. Reassert only its terminal
                    # failure; never recreate a turn.
                    self._receipt_claim(
                        envelope,
                        role=role,
                        connection_id=connection_id,
                        team_id=team_id,
                        lease_token=lease_token,
                        outcome=(
                            "delivered"
                            if record.get("state") == "completed"
                            else "failed"
                        ),
                    )
            except Exception:
                with suppress(Exception):
                    _target, team_id = self._resolve_claim_target(
                        envelope,
                        role=role,
                        connection_id=connection_id,
                    )
                    self._receipt_claim(
                        envelope,
                        role=role,
                        connection_id=connection_id,
                        team_id=team_id,
                        lease_token=lease_token,
                        outcome="failed",
                    )
                continue
        self.delivery_ledger.prune()
        return ready

    def recover_prepared_deliveries(self) -> list[dict[str, Any]]:
        """Return prepared rows for lifecycle-fenced receipt reconciliation."""

        if self.delivery_ledger is None:
            return []
        ready: list[dict[str, Any]] = []
        timestamp = int(time.time())
        for record in self.delivery_ledger.recoverable():
            if record.get("state") != "prepared":
                continue
            if int(record.get("expires_at") or 0) <= timestamp:
                self.delivery_ledger.finish(
                    str(record.get("envelope_id") or ""),
                    succeeded=False,
                    error="secure peer exchange expired before local admission",
                )
                continue
            ready.append(record)
        return ready

    def accept_prepared_delivery(self, envelope_id: str) -> dict[str, Any] | None:
        """Receipt then authorize one prepared row under the caller's lock."""

        if self.delivery_ledger is None:
            return None
        record = self.delivery_ledger.get(envelope_id)
        if record is None:
            return None
        if record.get("state") == "authorized":
            return record
        if record.get("state") != "prepared":
            return None
        if int(record.get("expires_at") or 0) <= int(time.time()):
            return self.delivery_ledger.finish(
                envelope_id,
                succeeded=False,
                error="secure peer exchange expired before local admission",
            )
        self._receipt_claim(
            record,
            role=str(record.get("transport_role") or ""),
            connection_id=str(record.get("connection_id") or ""),
            team_id=str(record.get("team_id") or ""),
            lease_token=str(record.get("lease_token") or ""),
            outcome="delivered",
        )
        return self.delivery_ledger.authorize(envelope_id)

    def reject_prepared_delivery(
        self,
        envelope_id: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        """Fail a prepared target after best-effort remote rejection."""

        if self.delivery_ledger is None:
            return None
        record = self.delivery_ledger.get(envelope_id)
        if record is None:
            return None
        if record.get("state") == "prepared":
            try:
                self._receipt_claim(
                    record,
                    role=str(record.get("transport_role") or ""),
                    connection_id=str(record.get("connection_id") or ""),
                    team_id=str(record.get("team_id") or ""),
                    lease_token=str(record.get("lease_token") or ""),
                    outcome="failed",
                )
            except SecurePeerError as exc:
                if exc.code != "lease_unavailable" and exc.status_code < 500:
                    raise
            return self.delivery_ledger.finish(
                envelope_id,
                succeeded=False,
                error=error,
            )
        return record

    def bind_delivery_owner(
        self,
        envelope_id: str,
        *,
        queued_id: str | None,
        run_id: str | None,
    ) -> dict[str, Any] | None:
        if self.delivery_ledger is None:
            return None
        return self.delivery_ledger.bind_owner(
            envelope_id,
            queued_id=queued_id,
            run_id=run_id,
        )

    def defer_delivery_admission(
        self,
        envelope_id: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.defer_admission(
                envelope_id,
                error=error,
            )
            if self.delivery_ledger is not None
            else None
        )

    def pending_delivery_admissions(
        self,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return (
            self.delivery_ledger.pending_admissions(limit=limit)
            if self.delivery_ledger is not None
            else []
        )

    def nonterminal_deliveries_for_chat(
        self,
        chat_id: str,
    ) -> list[dict[str, Any]]:
        return (
            self.delivery_ledger.nonterminal_for_chat(chat_id)
            if self.delivery_ledger is not None
            else []
        )

    def delivery(self, envelope_id: str) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.get(envelope_id)
            if self.delivery_ledger is not None
            else None
        )

    def recoverable_deliveries(self) -> list[dict[str, Any]]:
        return (
            self.delivery_ledger.recoverable()
            if self.delivery_ledger is not None
            else []
        )

    def delivery_for_run(self, run_id: str) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.for_run(run_id)
            if self.delivery_ledger is not None
            else None
        )

    def prepare_delivery_response(
        self,
        envelope_id: str,
        *,
        request_id: str,
        body: str,
        request_response: bool,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.prepare_response(
                envelope_id,
                request_id=request_id,
                body=body,
                request_response=request_response,
            )
            if self.delivery_ledger is not None
            else None
        )

    def mark_delivery_response(
        self,
        envelope_id: str,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.mark_response_committed(
                envelope_id,
                request_id=request_id,
            )
            if self.delivery_ledger is not None
            else None
        )

    def clear_delivery_response(
        self,
        envelope_id: str,
        *,
        request_id: str,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.clear_response_intent(
                envelope_id,
                request_id=request_id,
            )
            if self.delivery_ledger is not None
            else None
        )

    def defer_delivery_response(
        self,
        envelope_id: str,
        *,
        request_id: str,
        error: str,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.defer_response(
                envelope_id,
                request_id=request_id,
                error=error,
            )
            if self.delivery_ledger is not None
            else None
        )

    def pending_delivery_responses(
        self,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        return (
            self.delivery_ledger.pending_responses(limit=limit)
            if self.delivery_ledger is not None
            else []
        )

    def prepare_outbound_handoff(
        self,
        *,
        request_id: str,
        source_session_id: str,
        source_run_id: str,
        snapshot: Mapping[str, Any],
        body: str,
        action: str,
        expires_at: int,
    ) -> tuple[dict[str, Any], bool]:
        if self.delivery_ledger is None:
            raise SecurePeerError(
                "secure_peer_unavailable",
                "Secure peer durable delivery state is unavailable",
                503,
            )
        with self._outbound_guard:
            exact_sources = [
                route
                for route in self._published_routes()
                if str(route.get("connection_id") or "")
                == str(snapshot.get("connection_id") or "")
                and str(route.get("chat_id") or "") == source_session_id
                and str(route.get("route_id") or "")
                == str(snapshot.get("source_route_id") or "")
                and str(route.get("revision") or "")
                == str(snapshot.get("source_route_revision") or "")
                and route.get("status") == "active"
                and action in set(route.get("actions") or [])
            ]
            if (
                len(exact_sources) != 1
                or str(snapshot.get("source_chat_id") or "")
                != source_session_id
                or str(snapshot.get("action") or "") != action
            ):
                raise SecurePeerError(
                    "route_changed",
                    "Secure peer source route is unavailable or changed",
                    409,
                )
            return self.delivery_ledger.prepare_outbound(
                request_id=request_id,
                source_session_id=source_session_id,
                source_run_id=source_run_id,
                snapshot=snapshot,
                body=body,
                action=action,
                expires_at=expires_at,
            )

    def outbound_handoff(self, request_id: str) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.outbound(request_id)
            if self.delivery_ledger is not None
            else None
        )

    def commit_outbound_handoff(
        self,
        request_id: str,
        response: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.commit_outbound(request_id, response)
            if self.delivery_ledger is not None
            else None
        )

    def defer_outbound_handoff(
        self,
        request_id: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.defer_outbound(request_id, error)
            if self.delivery_ledger is not None
            else None
        )

    def fail_outbound_handoff(
        self,
        request_id: str,
        *,
        error: str,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.fail_outbound(request_id, error)
            if self.delivery_ledger is not None
            else None
        )

    def pending_outbound_handoffs(
        self,
        *,
        limit: int = 8,
    ) -> list[dict[str, Any]]:
        return (
            self.delivery_ledger.pending_outbound(limit=limit)
            if self.delivery_ledger is not None
            else []
        )

    def finish_delivery(
        self,
        envelope_id: str,
        *,
        succeeded: bool,
        result_text: str = "",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        return (
            self.delivery_ledger.finish(
                envelope_id,
                succeeded=succeeded,
                result_text=result_text,
                error=error,
            )
            if self.delivery_ledger is not None
            else None
        )

    def team_hub_capability(self) -> dict[str, Any] | None:
        active = next(
            (item for item in self.client.list_connections() if item.get("active")),
            None,
        )
        if active is None or active.get("status") not in {"approved", "connected"}:
            return None
        now = int(time.time())
        if (
            int(active.get("certificate_expires_at") or 0) <= now + 60
            or int(active.get("last_validated_at") or 0) < now - 120
            or self._client_error is not None
        ):
            return None
        connection_id = str(active["connection_id"])
        base_path = f"{SECURE_PEER_PROXY_PREFIX}/{connection_id}"
        route = {
            "transport": "secure_peer",
            "hub_url": None,
            "base_path": base_path,
            "connection_id": connection_id,
            "host_server_identity": active.get("host_server_identity"),
            "hub_id": active.get("hub_id"),
        }
        return {
            "available": True,
            "designated_host": False,
            "version": 1,
            "base_path": base_path,
            "transport": "secure_peer",
            "hub_url": None,
            "routes": [route],
            "hub_id": active.get("hub_id"),
            "host_server_identity": active.get("host_server_identity"),
            "connection_id": connection_id,
            "message": "This AgentsServer is paired to Teamspace over pinned TLS 1.3 and mutual certificates.",
            "action": None,
        }

    def _forward_peer_request(self, request):
        """Enter the same crash-consistency boundary as mounted Hub traffic."""

        with self._peer_admission:
            if not self._peer_accepting:
                raise SecurePeerError(
                    "hub_maintenance",
                    "Team Hub is unavailable during server maintenance",
                    503,
                )
            self._peer_in_flight += 1
        try:
            with self._guard:
                adapter, hub_store = self._adapter, self._hub_store
            if adapter is None or hub_store is None:
                raise SecurePeerError("hub_unavailable", "Team Hub is unavailable", 503)
            if request.method == "POST":
                # Snapshot/fence creation takes this exact lock.  A write
                # therefore either commits before the snapshot begins or sees
                # the durable fence and fails before mutation.
                with HubStore.maintenance_control_lock(hub_store.data_dir):
                    if hub_store.maintenance_fence() is not None:
                        raise SecurePeerError(
                            "hub_maintenance",
                            "Team Hub is unavailable during server maintenance",
                            503,
                        )
                    return adapter.forward(request)
            if hub_store.maintenance_fence() is not None:
                raise SecurePeerError(
                    "hub_maintenance",
                    "Team Hub is unavailable during server maintenance",
                    503,
                )
            return adapter.forward(request)
        finally:
            with self._peer_admission:
                self._peer_in_flight = max(0, self._peer_in_flight - 1)
                if self._peer_in_flight == 0:
                    self._peer_admission.notify_all()

    def close_host_admission(self) -> None:
        with self._peer_admission:
            self._peer_accepting = False
            while self._peer_in_flight:
                self._peer_admission.wait(timeout=0.25)

    def reopen_host_admission(self) -> None:
        with self._guard:
            ready = (
                self._hub_store is not None
                and self._adapter is not None
                and self._gateway is not None
            )
        with self._peer_admission:
            self._peer_accepting = ready
            self._peer_admission.notify_all()

    def proxy(
        self,
        connection_id: str,
        method: str,
        path: str,
        *,
        query: str,
        headers: Mapping[str, str] | None,
        body: bytes | None,
    ):
        with self._outbound_guard:
            active = next(
                (
                    item
                    for item in self.client.list_connections()
                    if item.get("active")
                ),
                None,
            )
            if active is None or active.get("connection_id") != connection_id:
                raise SecurePeerError(
                    "connection_unavailable",
                    "Secure peer connection is unavailable",
                    404,
                )
            try:
                return self.client.proxy(
                    connection_id,
                    method,
                    path,
                    query=query,
                    headers=headers,
                    body=body,
                )
            except SecurePeerError as exc:
                if exc.code == "peer_revoked" and exc.status_code == 401:
                    self._retire_remote_revoked_active_connection(active, {})
                raise

    def shutdown(self) -> None:
        self.close_host_admission()
        with self._guard:
            gateway = self._gateway
            self._gateway = None
        if gateway is not None:
            gateway.stop()
