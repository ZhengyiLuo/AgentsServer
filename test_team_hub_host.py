import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_hub_host import (
    TEAM_HUB_MODE_DISABLED,
    TEAM_HUB_MODE_HOST,
    TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
    TEAM_HUB_TRANSPORT_DIRECT_IP,
    ManagedTeamHubHost,
    configured_team_hub_endpoint,
)
from agentsdock_team_hub.store import HubError, HubStore


HOST_ID = "server-managed-host-12345678"
TAILNET_HOST = "sonic.example.ts.net"
TAILNET_HUB_URL = f"https://{TAILNET_HOST}:8444/api/team-hub"
DIRECT_IP = "100.73.184.23"
DIRECT_HUB_URL = f"http://{DIRECT_IP}:7850/api/team-hub"
TAILNET_HEADERS = {
    "X-Forwarded-Host": f"{TAILNET_HOST}:8444",
    "X-Forwarded-Proto": "https",
    "Tailscale-Headers-Info": "https://tailscale.com/s/serve-headers",
    "Tailscale-User-Login": "owner@example.com",
    "Tailscale-User-Name": "Owner",
}


def host(root: Path) -> ManagedTeamHubHost:
    return ManagedTeamHubHost(
        mode=TEAM_HUB_MODE_HOST,
        data_dir=root,
        server_identity=HOST_ID,
        allowed_hosts={"localhost", "127.0.0.1"},
    )


def tailnet_host(root: Path, instance_id: str) -> ManagedTeamHubHost:
    return ManagedTeamHubHost(
        mode=TEAM_HUB_MODE_HOST,
        data_dir=root,
        server_identity=HOST_ID,
        server_instance_id=instance_id,
        allowed_hosts={"localhost", "127.0.0.1", TAILNET_HOST},
        transport=TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
        hub_url=TAILNET_HUB_URL,
    )


def direct_ip_host(root: Path, instance_id: str) -> ManagedTeamHubHost:
    return ManagedTeamHubHost(
        mode=TEAM_HUB_MODE_HOST,
        data_dir=root,
        server_identity=HOST_ID,
        server_instance_id=instance_id,
        allowed_hosts={"localhost", "127.0.0.1", DIRECT_IP},
        transport=TEAM_HUB_TRANSPORT_DIRECT_IP,
        hub_url=DIRECT_HUB_URL,
    )


class ManagedTeamHubHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_ip_endpoint_requires_explicit_exact_plaintext_route(self) -> None:
        self.assertEqual(
            configured_team_hub_endpoint(
                TEAM_HUB_MODE_HOST,
                DIRECT_HUB_URL,
                TEAM_HUB_TRANSPORT_DIRECT_IP,
                7850,
            ),
            ("direct_ip", DIRECT_HUB_URL, DIRECT_IP, None),
        )
        for invalid in (
            "http://sonic.local:7850/api/team-hub",
            "https://100.73.184.23:7850/api/team-hub",
            "http://100.73.184.23:7851/api/team-hub",
            "http://127.0.0.1:7850/api/team-hub",
            "http://100.73.184.023:7850/api/team-hub",
            "http://user:secret@100.73.184.23:7850/api/team-hub",
            "http://100.73.184.23:7850/api/team-hub/",
            "http://100.73.184.23:7850/api/team-hub?token=secret",
        ):
            transport, hub_url, host_name, error = configured_team_hub_endpoint(
                TEAM_HUB_MODE_HOST,
                invalid,
                TEAM_HUB_TRANSPORT_DIRECT_IP,
                7850,
            )
            self.assertIsNone(transport, invalid)
            self.assertIsNone(hub_url, invalid)
            self.assertIsNone(host_name, invalid)
            self.assertIsNotNone(error, invalid)
        self.assertIsNotNone(
            configured_team_hub_endpoint(
                TEAM_HUB_MODE_HOST,
                DIRECT_HUB_URL,
                None,
                7850,
            )[3]
        )

    async def test_tailnet_endpoint_configuration_is_exact_and_fail_closed(self) -> None:
        self.assertEqual(
            configured_team_hub_endpoint(TEAM_HUB_MODE_HOST, TAILNET_HUB_URL),
            ("tailscale_serve", TAILNET_HUB_URL, TAILNET_HOST, None),
        )
        for invalid in (
            "http://sonic.example.ts.net:8444/api/team-hub",
            "https://sonic.example.ts.net/api/team-hub",
            "https://sonic.example.ts.net:443/api/team-hub",
            "https://100.73.184.23:8444/api/team-hub",
            "https://a.ts.net:8444/api/team-hub",
            "https://xn--sonic.example.ts.net:8444/api/team-hub",
            "https://SONIC.example.ts.net:8444/api/team-hub",
            "https://sonic.example.ts.net.:8444/api/team-hub",
            "https://user:secret@sonic.example.ts.net:8444/api/team-hub",
            "https://sonic.example.ts.net:8444/api/team-hub/",
            "https://sonic.example.ts.net:8444/api/team-hub?token=secret",
            "https://sonic.example.ts.net:8444/api/team-hub#fragment",
        ):
            transport, hub_url, host_name, error = configured_team_hub_endpoint(
                TEAM_HUB_MODE_HOST,
                invalid,
            )
            self.assertIsNone(transport, invalid)
            self.assertIsNone(hub_url, invalid)
            self.assertIsNone(host_name, invalid)
            self.assertIsNotNone(error, invalid)

    async def test_capability_advertises_primary_then_secondary_route_deterministically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = ManagedTeamHubHost(
                mode=TEAM_HUB_MODE_HOST,
                data_dir=Path(temporary) / "hub",
                server_identity=HOST_ID,
                allowed_hosts={"localhost", TAILNET_HOST, DIRECT_IP},
                transport=TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
                hub_url=TAILNET_HUB_URL,
                routes={
                    TEAM_HUB_TRANSPORT_DIRECT_IP: DIRECT_HUB_URL,
                    TEAM_HUB_TRANSPORT_TAILSCALE_SERVE: TAILNET_HUB_URL,
                },
            )
            self.assertEqual(
                runtime.capability()["routes"],
                [
                    {
                        "transport": TEAM_HUB_TRANSPORT_TAILSCALE_SERVE,
                        "hub_url": TAILNET_HUB_URL,
                    },
                    {
                        "transport": TEAM_HUB_TRANSPORT_DIRECT_IP,
                        "hub_url": DIRECT_HUB_URL,
                    },
                ],
            )

    async def test_server_session_availability_requires_issuable_managed_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = host(Path(temporary) / "hub")
            self.assertFalse(runtime.server_session_available())

            runtime.initialize()
            self.assertFalse(runtime.server_session_available())
            proof = runtime.store.bootstrap_proof_path.read_text().strip()  # type: ignore[union-attr]
            runtime.store.bootstrap(  # type: ignore[union-attr]
                proof,
                "owner@example.com",
                "Owner",
                "Owner Mac",
            )
            self.assertTrue(runtime.server_session_available())
            await runtime.shutdown()

    async def test_server_session_availability_rejects_legacy_multi_team_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "hub"
            legacy = HubStore(root)
            proof = legacy.bootstrap_proof_path.read_text().strip()
            legacy.bootstrap(
                proof,
                "owner@example.com",
                "Owner",
                "Owner Mac",
            )
            connection = legacy.connect()
            try:
                owner = connection.execute(
                    "SELECT principal_id FROM memberships WHERE role='owner'"
                ).fetchone()["principal_id"]
                with connection:
                    connection.execute(
                        """
                        INSERT INTO teams(
                            id, kind, slug, display_name,
                            personal_owner_principal_id, retention_days,
                            created_by_principal_id, created_at, updated_at
                        ) VALUES (?, 'shared', ?, ?, NULL, 365, ?, 1, 1)
                        """,
                        ("team_legacy_shared", "legacy-shared", "Legacy shared", owner),
                    )
                    connection.execute(
                        """
                        INSERT INTO memberships(
                            id, team_id, principal_id, role, status,
                            invited_by_principal_id, created_at, updated_at
                        ) VALUES (?, ?, ?, 'owner', 'active', NULL, 1, 1)
                        """,
                        ("membership_legacy_owner", "team_legacy_shared", owner),
                    )
            finally:
                connection.close()

            runtime = host(root)
            runtime.initialize()
            self.assertTrue(runtime.capability()["available"])
            with self.assertRaisesRegex(HubError, "Server-scoped Teamspace access is unavailable"):
                runtime.store.managed_server_claims()  # type: ignore[union-attr]
            self.assertFalse(runtime.server_session_available())
            await runtime.shutdown()

    async def test_disabled_host_creates_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "hub"
            runtime = ManagedTeamHubHost(
                mode=TEAM_HUB_MODE_DISABLED,
                data_dir=root,
                server_identity=HOST_ID,
                allowed_hosts={"localhost"},
            )
            runtime.initialize()
            self.assertFalse(root.exists())
            capability = runtime.capability()
            self.assertFalse(capability["available"])
            self.assertFalse(capability["designated_host"])

    async def test_runtime_lease_blocks_second_host_and_offline_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "hub"
            first = host(root)
            first.initialize()
            self.assertTrue(first.capability()["available"])
            snapshot = first.store.maintenance_snapshot_and_fence(  # type: ignore[union-attr]
                "server-update",
                operation_id="lease-test",
            )
            # Pre-install verification is read-only and intentionally works
            # while the old, fenced listener still owns the runtime lease.
            HubStore.verify_maintenance_snapshot(
                root,
                snapshot,
                expected_host_identity=HOST_ID,
                expected_hub_id=str(first.capability()["hub_id"]),
                expected_operation_id="lease-test",
            )

            second = host(root)
            second.initialize()
            self.assertFalse(second.capability()["available"])
            with self.assertRaisesRegex(RuntimeError, "already active"):
                HubStore.restore_maintenance_snapshot(
                    root,
                    snapshot,
                    expected_host_identity=HOST_ID,
                    expected_hub_id=str(first.capability()["hub_id"]),
                    expected_operation_id="lease-test",
                )

            await first.shutdown()
            second.initialize()
            self.assertTrue(second.capability()["available"])
            await second.shutdown()

    async def test_maintenance_drain_timeout_reopens_without_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = host(Path(temporary) / "hub")
            runtime.initialize()
            started = asyncio.Event()
            release = asyncio.Event()

            async def blocked(_scope, _receive, _send):
                started.set()
                await release.wait()

            runtime._delegate = blocked
            snapshot = MagicMock()
            runtime.store.maintenance_snapshot_and_fence = snapshot  # type: ignore[method-assign,union-attr]
            request = asyncio.create_task(runtime({}, None, None))
            await started.wait()
            with self.assertRaisesRegex(RuntimeError, "drain timed out"):
                await runtime.prepare_maintenance(
                    "timeout-test", drain_timeout_seconds=0.05
                )
            self.assertTrue(runtime.capability()["available"])
            snapshot.assert_not_called()
            release.set()
            await request
            await runtime.shutdown()

    async def test_cancelled_persistent_snapshot_settles_and_clears_exact_fence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = host(Path(temporary) / "hub")
            runtime.initialize()
            store = runtime.store
            self.assertIsNotNone(store)
            original = store.maintenance_snapshot_and_fence  # type: ignore[union-attr]
            started = threading.Event()
            release = threading.Event()

            def delayed(reason: str, *, operation_id: str, keep: int = 3):
                started.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test snapshot release timed out")
                return original(
                    reason,
                    operation_id=operation_id,
                    keep=keep,
                )

            store.maintenance_snapshot_and_fence = delayed  # type: ignore[method-assign,union-attr]
            maintenance = asyncio.create_task(
                runtime.prepare_maintenance(
                    "server-update",
                    operation_id="update-cancelled",
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            maintenance.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await maintenance
            self.assertIsNone(store.maintenance_fence())  # type: ignore[union-attr]
            self.assertTrue(runtime.capability()["available"])
            await runtime.shutdown()

    async def test_mounted_hub_is_local_only_and_server_bearer_is_not_hub_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "hub"
            runtime = host(root)
            runtime.initialize()
            application = FastAPI()
            application.mount("/api/team-hub", runtime)
            local = TestClient(
                application,
                base_url="http://localhost",
                client=("127.0.0.1", 41000),
            )
            health = local.get("/api/team-hub/v1/health")
            self.assertEqual(health.status_code, 200, health.text)
            self.assertEqual(health.json()["hub_id"], runtime.capability()["hub_id"])

            remote = TestClient(
                application,
                base_url="http://localhost",
                client=("192.0.2.20", 41000),
            )
            denied = remote.get("/api/team-hub/v1/health")
            self.assertEqual(denied.status_code, 403)
            proof = (root / "bootstrap-owner.proof").read_text().strip()
            bootstrapped = local.post(
                "/api/team-hub/v1/bootstrap/redeem",
                headers={"X-Team-Hub-Bootstrap-Proof": proof},
                json={
                    "email": "owner@example.com",
                    "display_name": "Owner",
                    "device_label": "Local Mac",
                },
            )
            self.assertEqual(bootstrapped.status_code, 200, bootstrapped.text)
            server_bearer = local.get(
                "/api/team-hub/v1/session",
                headers={"Authorization": "Bearer agents-server-secret-token"},
            )
            self.assertEqual(server_bearer.status_code, 401)
            await runtime.shutdown()

    async def test_tailnet_serve_transport_is_exact_and_direct_listener_is_denied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = tailnet_host(Path(temporary) / "hub", "server-instance-one")
            runtime.initialize()
            application = FastAPI()
            application.mount("/api/team-hub", runtime)

            serve = TestClient(
                application,
                base_url=f"http://{TAILNET_HOST}:8444",
                client=("127.0.0.1", 41000),
            )
            health = serve.get("/api/team-hub/v1/health", headers=TAILNET_HEADERS)
            self.assertEqual(health.status_code, 200, health.text)

            local = TestClient(
                application,
                base_url="http://localhost",
                client=("127.0.0.1", 41001),
            )
            self.assertEqual(local.get("/api/team-hub/v1/health").status_code, 200)

            direct = TestClient(
                application,
                base_url=f"http://{TAILNET_HOST}:7850",
                client=("100.73.184.23", 41002),
            )
            forged = direct.get(
                "/api/team-hub/v1/health",
                headers={**TAILNET_HEADERS, "Host": f"{TAILNET_HOST}:8444"},
            )
            self.assertEqual(forged.status_code, 403)

            missing_identity = serve.get(
                "/api/team-hub/v1/health",
                headers={
                    key: value
                    for key, value in TAILNET_HEADERS.items()
                    if key != "Tailscale-User-Login"
                },
            )
            self.assertEqual(missing_identity.status_code, 403)
            funnel = serve.get(
                "/api/team-hub/v1/health",
                headers={**TAILNET_HEADERS, "Tailscale-Funnel-Request": "?1"},
            )
            self.assertEqual(funnel.status_code, 403)
            duplicate_xfh = serve.get(
                "/api/team-hub/v1/health",
                headers=[
                    *TAILNET_HEADERS.items(),
                    ("X-Forwarded-Host", f"{TAILNET_HOST}:8444"),
                ],
            )
            self.assertEqual(duplicate_xfh.status_code, 403)
            await runtime.shutdown()

    async def test_direct_ip_transport_is_exact_unproxied_and_not_range_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = direct_ip_host(Path(temporary) / "hub", "server-instance-direct")
            runtime.initialize()
            application = FastAPI()
            application.mount("/api/team-hub", runtime)
            remote = TestClient(
                application,
                base_url=f"http://{DIRECT_IP}:7850",
                client=("192.0.2.44", 41000),
            )
            health = remote.get("/api/team-hub/v1/health")
            self.assertEqual(health.status_code, 200, health.text)
            for headers in (
                {"Cookie": "ambient=session"},
                {"Origin": f"http://{DIRECT_IP}:7850"},
                {"Forwarded": "for=192.0.2.44"},
                {"X-Forwarded-For": "192.0.2.44"},
                {"X-Forwarded-Host": f"{DIRECT_IP}:7850"},
                {"Tailscale-Funnel-Request": "?1"},
            ):
                self.assertEqual(
                    remote.get("/api/team-hub/v1/health", headers=headers).status_code,
                    403,
                    headers,
                )
            wrong_host = TestClient(
                application,
                base_url="http://192.168.1.8:7850",
                client=("192.0.2.44", 41001),
            )
            self.assertEqual(
                wrong_host.get("/api/team-hub/v1/health").status_code,
                400,
            )
            forged_from_loopback = TestClient(
                application,
                base_url=f"http://{DIRECT_IP}:7850",
                client=("127.0.0.1", 41002),
            )
            self.assertEqual(
                forged_from_loopback.get("/api/team-hub/v1/health").status_code,
                403,
            )
            self.assertEqual(runtime.capability()["transport"], "direct_ip")
            self.assertIn("unencrypted", runtime.capability()["message"])
            await runtime.shutdown()

    async def test_tailnet_rate_limits_aggregate_by_login_not_proxy_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = tailnet_host(Path(temporary) / "hub", "server-instance-limits")
            runtime.initialize()
            application = FastAPI()
            application.mount("/api/team-hub", runtime)
            serve = TestClient(
                application,
                base_url=f"http://{TAILNET_HOST}:8444",
                client=("127.0.0.1", 41010),
            )
            for _index in range(30):
                response = serve.post(
                    "/api/team-hub/v1/sessions/refresh",
                    headers=TAILNET_HEADERS,
                    json={"refresh_token": "invalid-refresh-token-long-enough"},
                )
                self.assertEqual(response.status_code, 401, response.text)
            other_login = serve.post(
                "/api/team-hub/v1/sessions/refresh",
                headers={
                    **TAILNET_HEADERS,
                    "Tailscale-User-Login": "other@example.com",
                    "Tailscale-User-Name": "Other",
                },
                json={"refresh_token": "invalid-refresh-token-long-enough"},
            )
            self.assertEqual(other_login.status_code, 401, other_login.text)
            same_login = serve.post(
                "/api/team-hub/v1/sessions/refresh",
                headers={**TAILNET_HEADERS, "Tailscale-User-Name": "Renamed Owner"},
                json={"refresh_token": "invalid-refresh-token-long-enough"},
            )
            self.assertEqual(same_login.status_code, 429, same_login.text)
            await runtime.shutdown()

    async def test_remote_bootstrap_is_bound_idempotent_and_restart_invalidated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "hub"
            first = tailnet_host(root, "server-instance-one")
            first.initialize()
            request_id = "f0d9a2d4-2e0f-43d8-bab6-f4934ef74677"
            grant = await first.issue_tailnet_bootstrap_proof(
                request_id=request_id,
                server_identity=HOST_ID,
                server_instance_id="server-instance-one",
                hub_url=TAILNET_HUB_URL,
                tailnet_login="owner@example.com",
                recipient_email="OWNER@example.com",
                display_name="Owner",
                device_label="Owner Mac",
            )
            repeated = await first.issue_tailnet_bootstrap_proof(
                request_id=request_id,
                server_identity=HOST_ID,
                server_instance_id="server-instance-one",
                hub_url=TAILNET_HUB_URL,
                tailnet_login="owner@example.com",
                recipient_email="owner@example.com",
                display_name="Owner",
                device_label="Owner Mac",
            )
            self.assertEqual(repeated, grant)
            self.assertTrue(grant["bootstrap_proof"].startswith("bootstrap_remote."))
            self.assertFalse((root / "bootstrap-owner.proof").exists())
            connection = first.store.connect()  # type: ignore[union-attr]
            try:
                row = connection.execute(
                    """
                    SELECT c.token_hash, d.request_id
                    FROM bootstrap_claims AS c
                    JOIN bootstrap_delegations AS d ON d.bootstrap_claim_id = c.id
                    WHERE c.revoked_at IS NULL
                    """
                ).fetchone()
                self.assertEqual(row["request_id"], request_id)
                self.assertIsInstance(row["token_hash"], bytes)
                self.assertNotIn(grant["bootstrap_proof"], str(dict(row)))
            finally:
                connection.close()

            await first.shutdown()
            second = tailnet_host(root, "server-instance-two")
            second.initialize()
            application = FastAPI()
            application.mount("/api/team-hub", second)
            serve = TestClient(
                application,
                base_url=f"http://{TAILNET_HOST}:8444",
                client=("127.0.0.1", 41003),
            )
            stale = serve.post(
                "/api/team-hub/v1/bootstrap/redeem",
                headers={
                    **TAILNET_HEADERS,
                    "X-Team-Hub-Bootstrap-Proof": grant["bootstrap_proof"],
                    "X-Team-Hub-Bootstrap-Request-Id": request_id,
                },
                json={
                    "email": "owner@example.com",
                    "display_name": "Owner",
                    "device_label": "Owner Mac",
                },
            )
            self.assertEqual(stale.status_code, 403, stale.text)
            self.assertTrue((root / "bootstrap-owner.proof").is_file())
            await second.shutdown()

    async def test_direct_ip_remote_bootstrap_is_bound_to_direct_route(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "hub"
            runtime = direct_ip_host(root, "server-instance-direct-bootstrap")
            runtime.initialize()
            request_id = "f7cc034a-5df7-4d30-a69f-9022616d05ad"
            grant = await runtime.issue_tailnet_bootstrap_proof(
                request_id=request_id,
                server_identity=HOST_ID,
                server_instance_id="server-instance-direct-bootstrap",
                hub_url=DIRECT_HUB_URL,
                tailnet_login="owner@example.com",
                recipient_email="owner@example.com",
                display_name="Owner",
                device_label="Owner Mac",
                transport=TEAM_HUB_TRANSPORT_DIRECT_IP,
            )
            application = FastAPI()
            application.mount("/api/team-hub", runtime)
            direct = TestClient(
                application,
                base_url=f"http://{DIRECT_IP}:7850",
                client=("192.0.2.45", 41000),
            )
            redeemed = direct.post(
                "/api/team-hub/v1/bootstrap/redeem",
                headers={
                    "X-Team-Hub-Bootstrap-Proof": grant["bootstrap_proof"],
                    "X-Team-Hub-Bootstrap-Request-Id": request_id,
                },
                json={
                    "email": "owner@example.com",
                    "display_name": "Owner",
                    "device_label": "Owner Mac",
                },
            )
            self.assertEqual(redeemed.status_code, 200, redeemed.text)
            self.assertIn("access_token", redeemed.json())
            await runtime.shutdown()

    async def test_remote_bootstrap_issuance_is_rate_limited_per_tailnet_login(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = tailnet_host(Path(temporary) / "hub", "server-instance-rate")
            runtime.initialize()
            arguments = {
                "request_id": "b62c156c-ffbf-413b-82fc-02bc63a27c70",
                "server_identity": HOST_ID,
                "server_instance_id": "server-instance-rate",
                "hub_url": TAILNET_HUB_URL,
                "tailnet_login": "owner@example.com",
                "recipient_email": "owner@example.com",
                "display_name": "Owner",
                "device_label": "Owner Mac",
            }
            for _index in range(8):
                await runtime.issue_tailnet_bootstrap_proof(**arguments)
            with self.assertRaisesRegex(Exception, "Too many bootstrap proof requests"):
                await runtime.issue_tailnet_bootstrap_proof(**arguments)
            self.assertLessEqual(len(runtime._bootstrap_rate_buckets), 4096)
            await runtime.shutdown()

    async def test_remote_bootstrap_ledger_has_a_hard_durable_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = tailnet_host(Path(temporary) / "hub", "server-instance-cap")
            runtime.initialize()
            arguments = {
                "request_id": "d4280d43-8ddd-498f-8bc8-bbf23491ec24",
                "server_identity": HOST_ID,
                "server_instance_id": "server-instance-cap",
                "hub_url": TAILNET_HUB_URL,
                "tailnet_login": "owner@example.com",
                "recipient_email": "owner@example.com",
                "display_name": "Owner",
                "device_label": "Owner Mac",
            }
            with patch("agentsdock_team_hub.store.MAX_BOOTSTRAP_DELEGATION_LEDGER_ROWS", 1):
                await runtime.issue_tailnet_bootstrap_proof(**arguments)
                connection = runtime.store.connect()  # type: ignore[union-attr]
                try:
                    connection.execute(
                        """
                        UPDATE bootstrap_claims SET revoked_at = created_at
                        WHERE revoked_at IS NULL AND consumed_at IS NULL
                        """
                    )
                finally:
                    connection.close()
                with self.assertRaisesRegex(Exception, "issuance is exhausted"):
                    await runtime.issue_tailnet_bootstrap_proof(
                        **{
                            **arguments,
                            "request_id": "8705a415-7f91-43a7-a591-14302d98c6d2",
                        }
                    )
            await runtime.shutdown()

    async def test_remote_bootstrap_mutation_participates_in_maintenance_drain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = tailnet_host(Path(temporary) / "hub", "server-instance-drain")
            runtime.initialize()
            expected_hub_id = str(runtime.capability()["hub_id"])
            started = threading.Event()
            release = threading.Event()
            original = runtime.store.issue_tailnet_bootstrap_proof  # type: ignore[union-attr]

            def delayed(**kwargs):
                started.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("test grant release timed out")
                return original(**kwargs)

            runtime.store.issue_tailnet_bootstrap_proof = delayed  # type: ignore[method-assign,union-attr]
            issuance = asyncio.create_task(
                runtime.issue_tailnet_bootstrap_proof(
                    request_id="05e5c547-9bd5-4571-ac5c-b69834b40a6e",
                    server_identity=HOST_ID,
                    server_instance_id="server-instance-drain",
                    hub_url=TAILNET_HUB_URL,
                    tailnet_login="owner@example.com",
                    recipient_email="owner@example.com",
                    display_name="Owner",
                    device_label="Owner Mac",
                )
            )
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            maintenance = asyncio.create_task(
                runtime.prepare_maintenance(
                    "server-update",
                    operation_id="bootstrap-drain-test",
                )
            )
            await asyncio.sleep(0.05)
            self.assertFalse(maintenance.done())
            release.set()
            grant = await issuance
            snapshot = await maintenance
            self.assertIsNotNone(snapshot)
            self.assertTrue(snapshot.is_dir())  # type: ignore[union-attr]
            manifest = json.loads((snapshot / "manifest.json").read_text())  # type: ignore[operator]
            self.assertEqual(manifest["proofs"], [])
            copied = sqlite3.connect(snapshot / "team-hub.sqlite3")  # type: ignore[operator]
            try:
                copied.row_factory = sqlite3.Row
                claim = copied.execute(
                    """
                    SELECT c.revoked_at
                    FROM bootstrap_claims AS c
                    JOIN bootstrap_delegations AS d
                      ON d.bootstrap_claim_id = c.id
                    WHERE d.request_id = ?
                    """,
                    (grant["request_id"],),
                ).fetchone()
                self.assertIsNotNone(claim["revoked_at"])
            finally:
                copied.close()
            HubStore.verify_maintenance_snapshot(
                runtime.data_dir,
                snapshot,  # type: ignore[arg-type]
                expected_host_identity=HOST_ID,
                expected_hub_id=expected_hub_id,
                expected_operation_id="bootstrap-drain-test",
            )
            await runtime.shutdown()
            HubStore.restore_maintenance_snapshot(
                runtime.data_dir,
                snapshot,  # type: ignore[arg-type]
                expected_host_identity=HOST_ID,
                expected_hub_id=expected_hub_id,
                expected_operation_id="bootstrap-drain-test",
            )
            restored = tailnet_host(runtime.data_dir, "server-instance-restored")
            restored.initialize()
            application = FastAPI()
            application.mount("/api/team-hub", restored)
            serve = TestClient(
                application,
                base_url=f"http://{TAILNET_HOST}:8444",
                client=("127.0.0.1", 41011),
            )
            stale = serve.post(
                "/api/team-hub/v1/bootstrap/redeem",
                headers={
                    **TAILNET_HEADERS,
                    "X-Team-Hub-Bootstrap-Proof": grant["bootstrap_proof"],
                    "X-Team-Hub-Bootstrap-Request-Id": grant["request_id"],
                },
                json={
                    "email": "owner@example.com",
                    "display_name": "Owner",
                    "device_label": "Owner Mac",
                },
            )
            self.assertEqual(stale.status_code, 403, stale.text)
            self.assertTrue((runtime.data_dir / "bootstrap-owner.proof").is_file())
            await restored.shutdown()

    async def test_same_operation_terminal_fence_blocks_posts_across_host_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "hub"
            first = host(root)
            first.initialize()
            snapshot = first.store.maintenance_snapshot_and_fence(  # type: ignore[union-attr]
                "server-update",
                operation_id="update-incomplete",
            )
            await first.shutdown()

            second = host(root)
            second.initialize()
            application = FastAPI()
            application.mount("/api/team-hub", second)
            local = TestClient(
                application,
                base_url="http://localhost",
                client=("127.0.0.1", 41000),
            )
            denied = local.post(
                "/api/team-hub/v1/bootstrap/redeem",
                headers={"X-Team-Hub-Bootstrap-Proof": "not-the-proof"},
                json={
                    "email": "owner@example.com",
                    "display_name": "Owner",
                    "device_label": "Local Mac",
                },
            )
            self.assertEqual(denied.status_code, 503)
            self.assertEqual(denied.json()["error"]["code"], "hub_maintenance")
            self.assertEqual(
                second.store.maintenance_fence()["operation_id"],  # type: ignore[union-attr,index]
                "update-incomplete",
            )
            self.assertTrue(snapshot.is_dir())
            await second.shutdown()

class VendoredTeamHubParityTests(unittest.TestCase):
    def test_vendored_runtime_matches_frozen_source_manifest_without_generated_files(self) -> None:
        server_root = Path(__file__).parent
        vendored = server_root / "agentsdock_team_hub"
        expected = {
            "__init__.py": "154fbe20574096cff3a5012d8720d51e024c5f077cc1044c3ea6cd5ad6f96861",
            "auth.py": "8f3ff2c2bf12845041acdb5f3f489cef4a9d827feca4de6b3925e4456be98f3d",
            "cli.py": "19ac402cceb29b5bb05cd31e507fffe38bc67f40433df336b1f95e4c3aace31d",
            "database.py": "c7a9bb1e132e6eba5d20de358e3c83b43d36a893d324643a944ae54a802cfcab",
            "migrations/0001_identity_auth.sql": "f55a62bf6dec527e1f71df91975deaf371e2af8b6e457b9d5577437e914dc186",
            "migrations/0002_teamspace_ledger.sql": "9681100d3d6eb3986e133d761ce9d000dbcf10b5e50954c96bd168391ecacbf3",
            "migrations/0003_service_runtime.sql": "e7668e2748a581a07aeaea78e78db3a62c6c28040881ab2b696b5d5de5ab34cc",
            "migrations/0004_managed_host_binding.sql": "6984cc095f23059c38c68092217254eb1419bae45287b4e3ab1217c60eb78696",
            "migrations/0005_tailnet_bootstrap_delegations.sql": "e47d25ea16353d023355cf875d008808cd3742cd038569abfb9607556cdbd09b",
            "migrations/0006_team_network_mailbox.sql": "c215068c903b4b65cd7a0e52506f859b5301d7e7d5ac9a66ad1636e6efd84d63",
            "migrations/0007_local_agent_mail.sql": "a01c33ef0d66de486e45bc3470a438ac0dd41edb5007eb6c12c26c240b4ca883",
            "migrations/0008_managed_server_session.sql": "487b29e425b7c53ef019e9ff476f4b18615c5142975c6d0bac8c134dc84e849c",
            "migrations/0009_team_messages.sql": "2ce774f0934e111c6443bda74fa7e8bbf8617094341c91b01b89cc0415eb05af",
            "migrations/0010_team_attachment_orphan_reclamation.sql": "85192a1c821378743a5abf89f916070cfccb1aa67a79eac95f6a07ec1d888bc5",
            "migrations/__init__.py": "aaf340c45c8d39c2939814977ba4cef8eb6b3bd0671b0f7542ebe06f5431d6ec",
            "security.py": "0c1895c7443e7be07a2f53c7e4c4228e3ee04c65d6cd36f039b7bbba1813e4fa",
            "secure_peer.py": "f5f666b78527220c20462a7abdc6552aa217100b02818080756b52ad7a0de2fa",
            "secure_peer_hub.py": "83c67c53a906877da9ab75029bbe87837a2b81fd2f45d6c12ef61b0ecf415f3d",
            "service.py": "c5b2e58f519cc4f23b0f4823a10f24cedbdafd46c8e6a0dd1c3728d3dbf009c0",
            "store.py": "ae7442c00700f03b8f0350be4ca97ab266cac4540d58519a7c48a90a897bdddf",
        }
        entries = list(vendored.rglob("*"))
        for path in entries:
            self.assertFalse(path.is_symlink(), path)
            self.assertTrue(path.is_file() or path.is_dir(), path)
        self.assertEqual(
            {path.relative_to(vendored).as_posix() for path in entries if path.is_dir()},
            {"migrations"},
        )
        files = [path for path in entries if path.is_file()]
        self.assertEqual(
            {path.relative_to(vendored).as_posix() for path in files},
            set(expected),
        )
        actual = {
            path.relative_to(vendored).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in files
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
