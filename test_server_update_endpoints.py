import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server
from fastapi import HTTPException


class ServerUpdateEndpointTests(unittest.IsolatedAsyncioTestCase):
    def test_linux_runner_environment_restores_the_user_service_bus(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            (runtime / "bus").touch()
            with patch.object(agent_server.sys, "platform", "linux"), \
                 patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}, clear=True):
                environment = agent_server.server_update_runner_environment()

        self.assertEqual(environment, {
            "XDG_RUNTIME_DIR": str(runtime),
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime / 'bus'}",
        })

    async def test_health_reports_missing_tmux_and_disables_managed_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server.shutil, "which", return_value=None):
                response = await agent_server.health()

        capability = response["capabilities"]["tmux"]
        self.assertEqual(capability["available"], False)
        self.assertEqual(capability["required"], True)
        self.assertIn("not found", capability["message"])
        self.assertIn("Install tmux", capability["action"])
        self.assertFalse(response["managed_updates"])

    async def test_health_reports_available_tmux_and_managed_updates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"):
                response = await agent_server.health()

        capability = response["capabilities"]["tmux"]
        self.assertEqual(capability, {
            "available": True,
            "required": True,
            "message": "tmux is available.",
            "action": None,
        })
        self.assertTrue(response["managed_updates"])
        self.assertEqual(
            response["capabilities"]["server_updates"]["tracks"],
            ["stable", "beta"],
        )

    async def test_check_reports_a_signed_newer_release(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0"})):
            status = await agent_server.check_server_update()

        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["latest_version"], "1.1.0")
        self.assertTrue(status["update_available"])
        self.assertEqual(status["track"], "stable")

    async def test_check_beta_track_discovers_and_persists_beta_release(self):
        manifest = AsyncMock(return_value={"version": "1.1.0-beta.3"})
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest):
            status = await agent_server.check_server_update(
                agent_server.ServerUpdateCheckRequest(track="beta"),
            )
            persisted = agent_server.read_server_update_status()

        manifest.assert_awaited_once_with("beta")
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["track"], "beta")
        self.assertEqual(status["current_track"], "stable")
        self.assertTrue(status["channel_switch"])
        self.assertEqual(persisted["track"], "beta")

    async def test_check_without_body_infers_beta_for_legacy_status(self):
        manifest = AsyncMock(return_value={"version": "1.1.0-beta.4"})
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.3"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest):
            status = await agent_server.check_server_update()

        manifest.assert_awaited_once_with("beta")
        self.assertEqual(status["track"], "beta")
        self.assertFalse(status["channel_switch"])

    async def test_check_stable_track_allows_beta_to_latest_stable_switch(self):
        manifest = AsyncMock(return_value={"version": "1.0.0"})
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.3"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest):
            status = await agent_server.check_server_update(
                agent_server.ServerUpdateCheckRequest(track="stable"),
            )

        self.assertEqual(status["phase"], "available")
        self.assertTrue(status["update_available"])
        self.assertTrue(status["channel_switch"])
        self.assertIn("Switch to stable", status["message"])

    async def test_check_does_not_offer_a_signed_older_release(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.0.0"})):
            status = await agent_server.check_server_update()

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        self.assertIn("current", status["message"])

    async def test_check_reports_an_unpublished_release_without_failing_ipc(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(side_effect=HTTPException(status_code=404, detail="No signed AgentsServer release has been published yet."))):
            status = await agent_server.check_server_update()

        self.assertEqual(status["phase"], "unavailable")
        self.assertFalse(status["update_available"])
        self.assertIn("No signed AgentsServer release", status["message"])

    async def test_status_keeps_a_just_started_update_active_while_tmux_appears(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "SERVER_UPDATE_START_GRACE_SECONDS", 45.0), \
             patch.object(agent_server, "server_update_status_age_seconds", return_value=44.9), \
             patch.object(agent_server.shutil, "which", return_value=None):
            agent_server.write_server_update_status(
                update_id="new-update",
                phase="starting",
                target_version="1.1.0",
                message="Starting detached update.",
            )
            status = await agent_server.server_update_status()

        self.assertEqual(status["phase"], "starting")
        self.assertEqual(status["target_version"], "1.1.0")
        self.assertNotIn("finished_at", status)

    async def test_status_normalizes_an_active_target_that_is_now_current(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.1.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server.shutil, "which", return_value=None):
            agent_server.write_server_update_status(
                update_id="completed-update",
                phase="restarting",
                target_version="1.1.0",
                update_available=True,
                message="Restarting updated server.",
            )
            status = await agent_server.server_update_status()

        self.assertEqual(status["phase"], "complete")
        self.assertFalse(status["update_available"])
        self.assertEqual(status["installed_version"], "1.1.0")
        self.assertIn("installed and healthy", status["message"])
        self.assertTrue(status["finished_at"])

    async def test_start_on_current_version_does_not_require_tmux(self):
        manifest = AsyncMock(side_effect=AssertionError("/start must not perform release discovery"))
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest), \
             patch.object(agent_server.shutil, "which", return_value=None):
            status = await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.0.0"))

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        manifest.assert_not_awaited()

    async def test_start_newer_version_without_tmux_returns_actionable_503(self):
        manifest = AsyncMock(side_effect=AssertionError("/start must not perform release discovery"))
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=manifest), \
             patch.object(agent_server.shutil, "which", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.1.0"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("tmux", str(raised.exception.detail))
        self.assertIn("Install tmux", str(raised.exception.detail))
        manifest.assert_not_awaited()

    async def test_start_launches_a_detached_verified_update_without_manifest_discovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            manifest = AsyncMock(side_effect=AssertionError("/start must not perform release discovery"))
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "signed_release_manifest", new=manifest), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.1.0"))

        self.assertEqual(status["phase"], "starting")
        self.assertEqual(status["target_version"], "1.1.0")
        manifest.assert_not_awaited()
        command = run_tmux.call_args.args[0]
        self.assertEqual(command[:3], ["new-session", "-d", "-s"])
        self.assertIn("--expected-version 1.1.0", command[-1])
        self.assertIn("--current-version 1.0.0", command[-1])
        self.assertIn("--track stable", command[-1])

    async def test_start_passes_user_service_environment_into_detached_tmux(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "server_update_runner_environment", return_value={
                     "XDG_RUNTIME_DIR": "/run/user/123",
                     "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/123/bus",
                 }), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0"),
                )

        command = run_tmux.call_args.args[0][-1]
        self.assertIn("env XDG_RUNTIME_DIR=/run/user/123", command)
        self.assertIn(
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/123/bus",
            command,
        )
        self.assertIn("--expected-version 1.1.0", command)

    async def test_start_beta_release_passes_beta_track_to_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(
                        version="1.1.0-beta.3",
                        track="beta",
                    )
                )

        self.assertEqual(status["track"], "beta")
        self.assertTrue(status["channel_switch"])
        self.assertIn("--track beta", run_tmux.call_args.args[0][-1])

    async def test_legacy_start_without_track_infers_installed_beta_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.2"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0-beta.3")
                )

        self.assertEqual(status["track"], "beta")
        self.assertIn("--track beta", run_tmux.call_args.args[0][-1])

    async def test_start_allows_explicit_beta_to_stable_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "SERVER_VERSION", "1.1.0-beta.3"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(
                        version="1.0.0",
                        track="stable",
                    )
                )

        self.assertEqual(status["phase"], "starting")
        self.assertTrue(status["channel_switch"])
        self.assertIn("--track stable", run_tmux.call_args.args[0][-1])

    async def test_start_rejects_version_that_does_not_match_track(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(
                        version="1.1.0-beta.3",
                        track="stable",
                    )
                )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("stable track", str(raised.exception.detail))

    async def test_start_refuses_stable_to_older_stable_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.2.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "run_tmux") as run_tmux:
            status = await agent_server.start_server_update(
                agent_server.ServerUpdateRequest(version="1.1.0", track="stable")
            )

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        run_tmux.assert_not_called()

    async def test_start_refuses_beta_to_older_beta_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.2.0-beta.4"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "run_tmux") as run_tmux:
            status = await agent_server.start_server_update(
                agent_server.ServerUpdateRequest(
                    version="1.2.0-beta.3",
                    track="beta",
                )
            )

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        run_tmux.assert_not_called()


if __name__ == "__main__":
    unittest.main()
