import asyncio
import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_hub_host import (
    TEAM_HUB_MODE_DISABLED,
    TEAM_HUB_MODE_HOST,
    ManagedTeamHubHost,
)
from agentsdock_team_hub.store import HubStore


HOST_ID = "server-managed-host-12345678"


def host(root: Path) -> ManagedTeamHubHost:
    return ManagedTeamHubHost(
        mode=TEAM_HUB_MODE_HOST,
        data_dir=root,
        server_identity=HOST_ID,
        allowed_hosts={"localhost", "127.0.0.1"},
    )


class ManagedTeamHubHostTests(unittest.IsolatedAsyncioTestCase):
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
    def test_vendored_runtime_matches_canonical_source_without_generated_files(self) -> None:
        server_root = Path(__file__).parent
        vendored = server_root / "agentsdock_team_hub"
        expected = {
            "__init__.py": "154fbe20574096cff3a5012d8720d51e024c5f077cc1044c3ea6cd5ad6f96861",
            "auth.py": "8f3ff2c2bf12845041acdb5f3f489cef4a9d827feca4de6b3925e4456be98f3d",
            "cli.py": "3e5ca2a0eb87b71379df48e97e0b2513b84aefecba5de0b7ff936965338c38a1",
            "database.py": "c7a9bb1e132e6eba5d20de358e3c83b43d36a893d324643a944ae54a802cfcab",
            "migrations/0001_identity_auth.sql": "f55a62bf6dec527e1f71df91975deaf371e2af8b6e457b9d5577437e914dc186",
            "migrations/0002_teamspace_ledger.sql": "9681100d3d6eb3986e133d761ce9d000dbcf10b5e50954c96bd168391ecacbf3",
            "migrations/0003_service_runtime.sql": "e7668e2748a581a07aeaea78e78db3a62c6c28040881ab2b696b5d5de5ab34cc",
            "migrations/0004_managed_host_binding.sql": "6984cc095f23059c38c68092217254eb1419bae45287b4e3ab1217c60eb78696",
            "migrations/__init__.py": "aaf340c45c8d39c2939814977ba4cef8eb6b3bd0671b0f7542ebe06f5431d6ec",
            "security.py": "0c1895c7443e7be07a2f53c7e4c4228e3ee04c65d6cd36f039b7bbba1813e4fa",
            "service.py": "5869d77ca3dc8c7c8a1e3ccc70cad1306c5dd633a3cbd035934e12503792d66d",
            "store.py": "16face427a63ca8289007c9f738d7473c237b71a3c64f5945192e947dded1b1f",
        }
        actual = {
            path.relative_to(vendored).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in vendored.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
