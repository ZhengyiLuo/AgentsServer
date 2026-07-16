import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

import agent_server


def request(admin_token: str | None = None) -> Request:
    headers = [] if admin_token is None else [(b"x-agentsserver-admin-token", admin_token.encode())]
    return Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/admin/update",
        "raw_path": b"/api/admin/update",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 7850),
    })


class ServerUpdateEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_token_is_separate_and_required(self):
        with patch.object(agent_server, "ADMIN_TOKEN", "admin-secret"):
            with self.assertRaises(HTTPException) as missing:
                await agent_server.server_update_status(request())
            with self.assertRaises(HTTPException) as wrong:
                await agent_server.server_update_status(request("agent-token"))

        self.assertEqual(missing.exception.status_code, 403)
        self.assertEqual(wrong.exception.status_code, 403)

    async def test_check_reports_a_signed_newer_release(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "ADMIN_TOKEN", "admin-secret"), \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0"})):
            status = await agent_server.check_server_update(request("admin-secret"))

        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["latest_version"], "1.1.0")
        self.assertTrue(status["update_available"])

    async def test_start_on_current_version_does_not_require_tmux(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(agent_server, "ADMIN_TOKEN", "admin-secret"), \
             patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
             patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", Path(temporary) / "status.json"), \
             patch.object(agent_server, "server_update_is_active", return_value=False), \
             patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.0.0"})), \
             patch.object(agent_server.shutil, "which", return_value=None):
            status = await agent_server.start_server_update(
                request("admin-secret"),
                agent_server.ServerUpdateRequest(version="1.0.0"),
            )

        self.assertEqual(status["phase"], "current")
        self.assertFalse(status["update_available"])

    async def test_start_launches_a_detached_verified_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner = root / "update_runner.py"
            key = root / "release-public-key.pem"
            runner.write_text("# runner\n")
            key.write_text("public key\n")
            with patch.object(agent_server, "ADMIN_TOKEN", "admin-secret"), \
                 patch.object(agent_server, "SERVER_VERSION", "1.0.0"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", root / "status.json"), \
                 patch.object(agent_server, "SERVER_UPDATE_RUNNER", runner), \
                 patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", key), \
                 patch.object(agent_server, "server_update_is_active", return_value=False), \
                 patch.object(agent_server, "signed_release_manifest", new=AsyncMock(return_value={"version": "1.1.0"})), \
                 patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
                 patch.object(agent_server, "run_tmux", return_value=None) as run_tmux:
                status = await agent_server.start_server_update(
                    request("admin-secret"),
                    agent_server.ServerUpdateRequest(version="1.1.0"),
                )

        self.assertEqual(status["phase"], "starting")
        self.assertEqual(status["target_version"], "1.1.0")
        command = run_tmux.call_args.args[0]
        self.assertEqual(command[:3], ["new-session", "-d", "-s"])
        self.assertIn("--expected-version 1.1.0", command[-1])


if __name__ == "__main__":
    unittest.main()
