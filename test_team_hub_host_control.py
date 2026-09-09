import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import agent_server
import agentsdock_team_hub.store as team_hub_store_module
from agentsdock_team_hub.store import HubStore
from team_hub_host import (
    TEAM_HUB_MODE_DISABLED,
    TEAM_HUB_TRANSPORT_LOOPBACK,
    ManagedTeamHubHost,
)


class _PeerRuntime:
    def __init__(self, display_name: str) -> None:
        self.display_name = display_name

    def set_display_name(self, display_name: str) -> str:
        self.display_name = display_name
        return display_name

    def team_hub_capability(self):
        return None

    def pause_member_for_host(self):
        return None

    def resume_member_after_host(self):
        return None

    def publish_display_name(self, display_name):
        return None


def _request(name: str, *, request_id: str | None = None, network_name: str | None = None):
    return agent_server.TeamHubHostEnableRequest(
        request_id=request_id or str(uuid.uuid4()),
        expected_server_identity="server-control-test-12345678",
        expected_server_instance_id="instance-control-test-12345678",
        confirmed=True,
        server_name=name,
        network_name=network_name,
    )


class TeamHubHostControlTests(unittest.IsolatedAsyncioTestCase):
    def test_request_requires_canonical_bounded_server_name(self) -> None:
        for name in ("", " Studio", "Studio\nInjected", "界" * 54):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                _request(name)
        self.assertEqual(_request("Studio").server_name, "Studio")

    def test_native_route_rejects_browser_and_oversized_bodies(self) -> None:
        with patch.object(agent_server, "AGENT_TOKEN", "test-secret"):
            client = TestClient(agent_server.app)
            browser = client.post(
                "/api/admin/team-hub/host/enable",
                headers={
                    "Origin": "https://attacker.example",
                    "X-AgentsDock-Token": "test-secret",
                    "Content-Type": "application/json",
                },
                content=b"{",
            )
            oversized = client.post(
                "/api/admin/team-hub/host/enable",
                headers={
                    "X-AgentsDock-Token": "test-secret",
                    "Content-Type": "application/json",
                    "Content-Length": str(
                        agent_server.TEAM_HUB_HOST_CONTROL_MAX_BODY_BYTES + 1
                    ),
                },
                content=b"{",
            )
        self.assertEqual(browser.status_code, 403)
        self.assertEqual(oversized.status_code, 413)

    def test_failed_preserved_host_activation_restores_exact_fenced_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "team-hub"
            original = HubStore(
                data_dir,
                managed_host_identity="server-control-test-12345678",
            )
            hub_id = original.hub_id
            runtime = Mock()
            runtime.designated_host = False
            runtime.managed_host_display_name = "Old Studio"
            runtime.enable_live_host.side_effect = RuntimeError("candidate failed")
            peer = _PeerRuntime("Old Studio")
            configuration = {
                "transport": TEAM_HUB_TRANSPORT_LOOPBACK,
                "hub_url": None,
                "routes": {TEAM_HUB_TRANSPORT_LOOPBACK: None},
                "allowed_hosts": {"127.0.0.1"},
                "public_host": None,
                "direct_ip_url": None,
                "direct_ip_public_host": None,
            }
            with (
                patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime),
                patch.object(agent_server, "SECURE_PEER_RUNTIME", peer),
                patch.object(agent_server, "TEAM_HUB_DATA_DIR", data_dir),
                patch.object(
                    agent_server,
                    "TEAM_HUB_HOST_CONTROL_STATUS_FILE",
                    root / "team-hub-host.json",
                ),
                patch.object(
                    agent_server,
                    "SERVER_INSTANCE_ID",
                    "instance-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "server_identity",
                    return_value="server-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "requested_live_team_hub_configuration",
                    return_value=configuration,
                ),
                self.assertRaises(agent_server.TeamHubHostControlFailure),
            ):
                agent_server.activate_team_hub_host_sync("Studio")

            self.assertFalse((data_dir / "maintenance-fence.json").exists())
            self.assertFalse(
                (data_dir / ".host-reactivation-handoff.json").exists()
            )
            recovered = HubStore(
                data_dir,
                managed_host_identity="server-control-test-12345678",
            )
            self.assertEqual(recovered.hub_id, hub_id)
            # Simulate process death after exact rollback acknowledgment but
            # before the endpoint clears its private reactivation marker.
            with (
                patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime),
                patch.object(agent_server, "TEAM_HUB_DATA_DIR", data_dir),
                patch.object(
                    agent_server,
                    "TEAM_HUB_HOST_CONTROL_STATUS_FILE",
                    root / "team-hub-host.json",
                ),
                patch.object(
                    agent_server,
                    "SERVER_INSTANCE_ID",
                    "instance-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "server_identity",
                    return_value="server-control-test-12345678",
                ),
            ):
                agent_server.write_team_hub_host_control_status(
                    phase="starting",
                    _live_reactivation={
                        "hub_id": hub_id,
                        "snapshot": str(
                            data_dir
                            / "maintenance-backups"
                            / "snapshot_interrupted_rollback"
                        ),
                        "operation_id": "host-reactivation-test-rollback",
                        "fence_device": 1,
                        "fence_inode": 2,
                        "adopted": True,
                        "authority_published": True,
                    },
                )
                agent_server.recover_interrupted_team_hub_host_control()
                status = agent_server.read_team_hub_host_control_status()
            self.assertEqual(status["phase"], "failed")
            self.assertIsNone(status.get("_live_reactivation"))

    async def test_live_demote_and_reactivate_preserves_exact_hub_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "team-hub"
            config = root / "config" / "env"
            status = root / "admin" / "team-hub-host.json"
            runtime = ManagedTeamHubHost(
                mode=TEAM_HUB_MODE_DISABLED,
                data_dir=data_dir,
                server_identity="server-control-test-12345678",
                server_instance_id="instance-control-test-12345678",
                managed_host_display_name="Old Studio",
                allowed_hosts={"127.0.0.1"},
                transport=TEAM_HUB_TRANSPORT_LOOPBACK,
                hub_url=None,
                routes={TEAM_HUB_TRANSPORT_LOOPBACK: None},
            )
            peer = _PeerRuntime("Old Studio")
            configuration = {
                "transport": TEAM_HUB_TRANSPORT_LOOPBACK,
                "hub_url": None,
                "routes": {TEAM_HUB_TRANSPORT_LOOPBACK: None},
                "allowed_hosts": {"127.0.0.1"},
                "public_host": None,
                "direct_ip_url": None,
                "direct_ip_public_host": None,
            }
            globals_to_restore = {
                name: getattr(agent_server, name)
                for name in (
                    "TEAM_HUB_MODE",
                    "TEAM_HUB_TRANSPORT",
                    "TEAM_HUB_URL",
                    "TEAM_HUB_PUBLIC_HOST",
                    "TEAM_HUB_ROUTES",
                    "TEAM_HUB_DIRECT_IP_URL",
                    "TEAM_HUB_DIRECT_IP_PUBLIC_HOST",
                    "TEAM_HUB_ALLOWED_HOSTS",
                    "AGENTSDOCK_SERVER_DISPLAY_NAME",
                )
            }
            self.addCleanup(
                lambda: [
                    setattr(agent_server, name, value)
                    for name, value in globals_to_restore.items()
                ]
            )
            with (
                patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime),
                patch.object(agent_server, "SECURE_PEER_RUNTIME", peer),
                patch.object(agent_server, "TEAM_HUB_DATA_DIR", data_dir),
                patch.object(agent_server, "CONFIG_ENV_FILE", config),
                patch.object(
                    agent_server,
                    "TEAM_HUB_HOST_CONTROL_STATUS_FILE",
                    status,
                ),
                patch.object(
                    agent_server,
                    "SERVER_INSTANCE_ID",
                    "instance-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "server_identity",
                    return_value="server-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "requested_live_team_hub_configuration",
                    return_value=configuration,
                ),
                patch.object(
                    agent_server,
                    "managed_server_restart_blocks_work",
                    return_value=False,
                ),
                patch.object(agent_server, "read_server_update_status", return_value={}),
                patch.dict(os.environ, {}, clear=False),
            ):
                first = await agent_server.enable_team_hub_host(_request("Studio", network_name="Research"))
                hub_id = runtime.store.hub_id
                original_team = runtime.store.managed_server_claims().team_id
                disabled = await agent_server.disable_team_hub_host(
                    _request("Studio")
                )
                second = await agent_server.enable_team_hub_host(
                    _request("Studio Two", network_name="Studio Two")
                )

                self.assertEqual(first["operation"], "create")
                self.assertEqual(disabled["operation"], "disable")
                self.assertEqual(second["operation"], "reactivate")
                self.assertEqual(runtime.store.hub_id, hub_id)
                self.assertEqual(runtime.store.managed_server_claims().team_id, original_team)
                self.assertEqual(
                    runtime.store.get_team(runtime.store.managed_server_claims(), original_team)["team"]["display_name"],
                    "Research",
                )
                self.assertEqual(second["server_name"], "Studio Two")
                self.assertFalse(second["reconnect_required"])
                self.assertFalse((data_dir / "maintenance-fence.json").exists())
                persisted = agent_server.parse_config_env_file(config.read_text())
                self.assertEqual(persisted["AGENTSDOCK_TEAM_HUB_MODE"], "host")
                self.assertEqual(persisted["AGENTSDOCK_SERVER_NAME"], "Studio Two")
                await runtime.disable_live_host()

    async def test_partial_first_host_creation_is_quarantined_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "team-hub"
            runtime = ManagedTeamHubHost(
                mode=TEAM_HUB_MODE_DISABLED,
                data_dir=data_dir,
                server_identity="server-control-test-12345678",
                server_instance_id="instance-control-test-12345678",
                managed_host_display_name="Studio",
                allowed_hosts={"127.0.0.1"},
                transport=TEAM_HUB_TRANSPORT_LOOPBACK,
                hub_url=None,
                routes={TEAM_HUB_TRANSPORT_LOOPBACK: None},
            )
            peer = _PeerRuntime("Studio")
            configuration = {
                "transport": TEAM_HUB_TRANSPORT_LOOPBACK,
                "hub_url": None,
                "routes": {TEAM_HUB_TRANSPORT_LOOPBACK: None},
                "allowed_hosts": {"127.0.0.1"},
                "public_host": None,
                "direct_ip_url": None,
                "direct_ip_public_host": None,
            }
            with (
                patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime),
                patch.object(agent_server, "SECURE_PEER_RUNTIME", peer),
                patch.object(agent_server, "TEAM_HUB_DATA_DIR", data_dir),
                patch.object(agent_server, "CONFIG_ENV_FILE", root / "config" / "env"),
                patch.object(
                    agent_server,
                    "TEAM_HUB_HOST_CONTROL_STATUS_FILE",
                    root / "team-hub-host.json",
                ),
                patch.object(
                    agent_server,
                    "SERVER_INSTANCE_ID",
                    "instance-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "server_identity",
                    return_value="server-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "requested_live_team_hub_configuration",
                    return_value=configuration,
                ),
            ):
                with (
                    patch.object(
                        team_hub_store_module,
                        "load_or_create_signing_key",
                        side_effect=RuntimeError("crash before signing key"),
                    ),
                    self.assertRaises(agent_server.TeamHubHostControlFailure),
                ):
                    agent_server.activate_team_hub_host_sync("Studio")

                self.assertIsNotNone(
                    HubStore.managed_host_binding_without_source_mutation(data_dir)
                )
                self.assertFalse((data_dir / "access-token-signing.key").exists())

                operation, _capability, _configuration = (
                    agent_server.activate_team_hub_host_sync("Studio")
                )
                self.assertEqual(operation, "create")
                self.assertTrue(runtime.designated_host)
                quarantined = list(
                    (root / "team-hub-partial-creations").iterdir()
                )
                self.assertEqual(len(quarantined), 1)
                self.assertTrue((quarantined[0] / "team-hub.sqlite3").is_file())
                await runtime.disable_live_host()

    def test_partial_creation_quarantine_refuses_an_active_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "team-hub"
            with patch.object(
                team_hub_store_module,
                "load_or_create_signing_key",
                side_effect=RuntimeError("crash before signing key"),
            ), self.assertRaises(RuntimeError):
                HubStore(
                    data_dir,
                    managed_host_identity="server-control-test-12345678",
                )
            database_identity = data_dir.joinpath("team-hub.sqlite3").stat()
            lease = HubStore.acquire_managed_runtime_lease(data_dir)
            try:
                with (
                    patch.object(agent_server, "TEAM_HUB_DATA_DIR", data_dir),
                    patch.object(
                        agent_server,
                        "server_identity",
                        return_value="server-control-test-12345678",
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    agent_server.prepare_team_hub_state_for_live_activation()
            finally:
                HubStore.release_managed_runtime_lease(lease)
            current = data_dir.joinpath("team-hub.sqlite3").stat()
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (database_identity.st_dev, database_identity.st_ino),
            )
            self.assertFalse(
                (Path(temporary) / "team-hub-partial-creations").exists()
            )

    def test_unbound_hub_with_identity_data_is_never_auto_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "team-hub"
            store = HubStore(data_dir)
            connection = store.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO principals(
                        id,kind,scope_team_id,display_name,status,
                        created_at,updated_at
                    ) VALUES ('human_recovery_test','human',NULL,'Recovery',
                              'active',1,1)
                    """
                )
                connection.execute("COMMIT")
            finally:
                connection.close()
            database_identity = data_dir.joinpath("team-hub.sqlite3").stat()
            with (
                patch.object(agent_server, "TEAM_HUB_DATA_DIR", data_dir),
                patch.object(
                    agent_server,
                    "server_identity",
                    return_value="server-control-test-12345678",
                ),
                self.assertRaisesRegex(RuntimeError, "manual recovery"),
            ):
                agent_server.prepare_team_hub_state_for_live_activation()
            current = data_dir.joinpath("team-hub.sqlite3").stat()
            self.assertEqual(
                (current.st_dev, current.st_ino),
                (database_identity.st_dev, database_identity.st_ino),
            )
            self.assertFalse(
                (root / "team-hub-partial-creations").exists()
            )

    async def test_incomplete_reactivation_rollback_preserves_recovery_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data_dir = root / "team-hub"
            HubStore(
                data_dir,
                managed_host_identity="server-control-test-12345678",
            )
            runtime = Mock()
            runtime.designated_host = False
            runtime.managed_host_display_name = "Studio"
            runtime.enable_live_host.side_effect = RuntimeError("activation failed")
            peer = _PeerRuntime("Studio")
            peer.resume_member_after_host = Mock(
                side_effect=RuntimeError("member resume failed")
            )
            configuration = {
                "transport": TEAM_HUB_TRANSPORT_LOOPBACK,
                "hub_url": None,
                "routes": {TEAM_HUB_TRANSPORT_LOOPBACK: None},
                "allowed_hosts": {"127.0.0.1"},
                "public_host": None,
                "direct_ip_url": None,
                "direct_ip_public_host": None,
            }
            with (
                patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime),
                patch.object(agent_server, "SECURE_PEER_RUNTIME", peer),
                patch.object(agent_server, "TEAM_HUB_DATA_DIR", data_dir),
                patch.object(agent_server, "CONFIG_ENV_FILE", root / "config" / "env"),
                patch.object(
                    agent_server,
                    "TEAM_HUB_HOST_CONTROL_STATUS_FILE",
                    root / "team-hub-host.json",
                ),
                patch.object(
                    agent_server,
                    "SERVER_INSTANCE_ID",
                    "instance-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "server_identity",
                    return_value="server-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "requested_live_team_hub_configuration",
                    return_value=configuration,
                ),
                patch.object(
                    agent_server,
                    "managed_server_restart_blocks_work",
                    return_value=False,
                ),
                patch.object(agent_server, "read_server_update_status", return_value={}),
                patch.object(
                    agent_server,
                    "_rollback_team_hub_reactivation",
                    side_effect=RuntimeError("hub rollback failed"),
                ) as rollback,
            ):
                with self.assertRaises(
                    agent_server.TeamHubHostControlFailure
                ) as raised:
                    await agent_server.enable_team_hub_host(_request("Studio"))
                first_status = json.loads(
                    (root / "team-hub-host.json").read_text()
                )
                with self.assertRaises(
                    agent_server.TeamHubHostControlFailure
                ) as retried:
                    await agent_server.enable_team_hub_host(_request("Studio"))
                retry_status = json.loads(
                    (root / "team-hub-host.json").read_text()
                )
                with self.assertRaises(
                    agent_server.TeamHubHostControlFailure
                ) as disable_retry:
                    await agent_server.disable_team_hub_host(_request("Studio"))
                disable_status = json.loads(
                    (root / "team-hub-host.json").read_text()
                )

            self.assertEqual(raised.exception.code, "team_hub_host_rollback_failed")
            self.assertEqual(
                retried.exception.code,
                "team_hub_host_recovery_pending",
            )
            self.assertEqual(
                disable_retry.exception.code,
                "team_hub_host_recovery_pending",
            )
            self.assertEqual(rollback.call_count, 3)
            self.assertIsInstance(first_status.get("_live_reactivation"), dict)
            self.assertEqual(
                retry_status.get("_live_reactivation"),
                first_status.get("_live_reactivation"),
            )
            self.assertEqual(
                disable_status.get("_live_reactivation"),
                first_status.get("_live_reactivation"),
            )

    async def test_committed_demotion_publishes_member_before_resume_retry(self) -> None:
        class _HostRuntime:
            designated_host = True
            managed_host_display_name = "Studio"

            async def disable_live_host(self):
                self.designated_host = False
                return {"designated_host": False, "available": False}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = _HostRuntime()
            peer = _PeerRuntime("Studio")
            peer.resume_member_after_host = Mock(
                side_effect=RuntimeError("remote host offline")
            )
            globals_to_restore = {
                name: getattr(agent_server, name)
                for name in (
                    "TEAM_HUB_MODE",
                    "TEAM_HUB_TRANSPORT",
                    "TEAM_HUB_URL",
                    "TEAM_HUB_PUBLIC_HOST",
                    "TEAM_HUB_ROUTES",
                    "TEAM_HUB_DIRECT_IP_URL",
                    "TEAM_HUB_DIRECT_IP_PUBLIC_HOST",
                    "TEAM_HUB_ALLOWED_HOSTS",
                    "AGENTSDOCK_SERVER_DISPLAY_NAME",
                )
            }
            self.addCleanup(
                lambda: [
                    setattr(agent_server, name, value)
                    for name, value in globals_to_restore.items()
                ]
            )
            with (
                patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime),
                patch.object(agent_server, "SECURE_PEER_RUNTIME", peer),
                patch.object(agent_server, "CONFIG_ENV_FILE", root / "config" / "env"),
                patch.object(
                    agent_server,
                    "TEAM_HUB_HOST_CONTROL_STATUS_FILE",
                    root / "team-hub-host.json",
                ),
                patch.object(
                    agent_server,
                    "SERVER_INSTANCE_ID",
                    "instance-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "server_identity",
                    return_value="server-control-test-12345678",
                ),
                patch.object(
                    agent_server,
                    "managed_server_restart_blocks_work",
                    return_value=False,
                ),
                patch.object(agent_server, "read_server_update_status", return_value={}),
                self.assertRaises(agent_server.TeamHubHostControlFailure) as raised,
            ):
                await agent_server.disable_team_hub_host(_request("Studio Member"))

            self.assertEqual(raised.exception.code, "team_hub_member_resume_failed")
            self.assertFalse(runtime.designated_host)
            self.assertEqual(agent_server.TEAM_HUB_MODE, "disabled")
            self.assertEqual(
                agent_server.AGENTSDOCK_SERVER_DISPLAY_NAME,
                "Studio Member",
            )
            status = json.loads((root / "team-hub-host.json").read_text())
            self.assertEqual(status["phase"], "failed")
            self.assertEqual(status["error_code"], "team_hub_member_resume_failed")


if __name__ == "__main__":
    unittest.main()
