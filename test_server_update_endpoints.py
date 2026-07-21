import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server
from fastapi import HTTPException
from pydantic import ValidationError


class ServerUpdateEndpointTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(response["capabilities"]["server_update_tracks"], ["stable", "beta"])

    async def test_update_status_defaults_existing_installations_to_stable_track(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"):
            status = agent_server.read_server_update_status()

        self.assertEqual(status["track"], "stable")
        self.assertFalse(status["current_is_prerelease"])

    async def test_check_reports_a_signed_newer_release(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0"})):
            status = await agent_server.check_server_update()

        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["latest_version"], "1.1.0")
        self.assertEqual(status["track"], "stable")
        self.assertFalse(status["latest_is_prerelease"])
        self.assertTrue(status["update_available"])

    async def test_check_can_select_and_persist_beta_track(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0-beta.2", "prerelease": True})) as signed:
            status = await agent_server.check_server_update(agent_server.ServerUpdateCheckRequest(track="beta"))
            persisted = agent_server.read_server_update_status()

        signed.assert_awaited_once_with("beta")
        self.assertEqual(status["track"], "beta")
        self.assertEqual(status["latest_version"], "1.1.0-beta.2")
        self.assertTrue(status["latest_is_prerelease"])
        self.assertEqual(persisted["track"], "beta")

    async def test_check_uses_strict_semver_and_never_offers_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.2.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.2.0-beta.9", "prerelease": True})):
            status = await agent_server.check_server_update(agent_server.ServerUpdateCheckRequest(track="beta"))

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])
        self.assertIn("no newer", status["message"])

    async def test_check_without_body_reuses_persisted_track(self):
        with tempfile.TemporaryDirectory() as temporary:
            status_path = Path(temporary) / "status.json"
            status_path.write_text('{"track":"beta","phase":"idle"}\n')
            with patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", status_path), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0-beta.1"})) as signed:
                status = await agent_server.check_server_update()

        signed.assert_awaited_once_with("beta")
        self.assertEqual(status["track"], "beta")

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

    async def test_invalid_update_track_is_rejected_by_the_api_model(self):
        with self.assertRaises(ValidationError):
            agent_server.ServerUpdateCheckRequest(track="nightly")

    async def test_start_on_current_version_does_not_require_tmux(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.0.0"})), \
             patch.object(agent_server.shutil, "which", return_value=None):
            status = await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.0.0"))

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])

    async def test_start_newer_version_without_tmux_returns_actionable_503(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0"})), \
             patch.object(agent_server.shutil, "which", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.1.0"))

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("tmux", str(raised.exception.detail))
        self.assertIn("Install tmux", str(raised.exception.detail))

    async def test_start_rejects_a_track_downgrade(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "SERVER_VERSION", "1.2.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.2.0-beta.9", "prerelease": True})), \
             patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.2.0-beta.9", track="beta")
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("downgrades", str(raised.exception.detail))

    async def test_start_launches_a_detached_verified_update(self):
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
                 patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0"})), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    agent_server.ServerUpdateRequest(version="1.1.0", track="beta")
                )

        self.assertEqual(status["phase"], "starting")
        self.assertEqual(status["target_version"], "1.1.0")
        self.assertEqual(status["track"], "beta")
        command = run_tmux.call_args.args[0]
        self.assertEqual(command[:3], ["new-session", "-d", "-s"])
        self.assertIn("--expected-version 1.1.0", command[-1])
        self.assertIn("--current-version 1.0.0", command[-1])
        self.assertIn("--track beta", command[-1])


if __name__ == "__main__":
    unittest.main()
