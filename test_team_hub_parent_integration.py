import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_server
from fastapi.testclient import TestClient

from team_hub_host import TEAM_HUB_MODE_HOST, ManagedTeamHubHost


HOST_ID = "server-parent-integration-12345678"


class TeamHubParentIntegrationTests(unittest.TestCase):
    def test_parent_listener_preserves_hub_transport_and_credential_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = ManagedTeamHubHost(
                mode=TEAM_HUB_MODE_HOST,
                data_dir=Path(temporary) / "hub",
                server_identity=HOST_ID,
                allowed_hosts={"localhost", "127.0.0.1"},
            )
            runtime.initialize()
            mount = next(
                route
                for route in agent_server.app.routes
                if getattr(route, "name", None) == "team-hub"
            )
            original_mount = mount.app
            mount.app = runtime
            try:
                with patch.object(agent_server, "TEAM_HUB_RUNTIME", runtime), \
                     patch.object(agent_server, "AGENT_TOKEN", "agents-server-token"):
                    local = TestClient(
                        agent_server.app,
                        base_url="http://localhost",
                        client=("127.0.0.1", 41000),
                    )
                    bare = local.get(
                        "/api/team-hub",
                        headers={"Host": "evil.test"},
                        follow_redirects=False,
                    )
                    self.assertEqual(bare.status_code, 404)
                    self.assertNotIn("location", bare.headers)
                    self.assertNotIn("evil.test", bare.text)
                    for method in ("post", "options"):
                        response = getattr(local, method)(
                            "/api/team-hub",
                            headers={"Host": "evil.test"},
                        )
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn("location", response.headers)

                    health = local.get("/api/team-hub/v1/health")
                    self.assertEqual(health.status_code, 200, health.text)
                    self.assertEqual(health.json()["hub_id"], runtime.capability()["hub_id"])

                    proof = (runtime.data_dir / "bootstrap-owner.proof").read_text().strip()
                    bootstrap = local.post(
                        "/api/team-hub/v1/bootstrap/redeem",
                        headers={"X-Team-Hub-Bootstrap-Proof": proof},
                        json={
                            "email": "owner@example.com",
                            "display_name": "Owner",
                            "device_label": "Local Mac",
                        },
                    )
                    self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
                    bundle = bootstrap.json()

                    hub_session = "/api/team-hub/v1/session"
                    for kwargs in (
                        {"headers": {"Authorization": "Bearer agents-server-token"}},
                        {"headers": {"X-AgentsDock-Token": "agents-server-token"}},
                        {"headers": {"X-ZenithDock-Token": "agents-server-token"}},
                        {"params": {"token": "agents-server-token"}},
                    ):
                        denied = local.get(hub_session, **kwargs)
                        self.assertEqual(denied.status_code, 401, denied.text)

                    for kwargs in (
                        {"headers": {"Authorization": f"Bearer {bundle['access_token']}"}},
                        {"params": {"token": bundle["refresh_token"]}},
                    ):
                        denied = local.get("/api/health", **kwargs)
                        self.assertEqual(denied.status_code, 401, denied.text)

                    hostile_origin = local.get(
                        "/api/team-hub/v1/health",
                        headers={"Origin": "https://evil.test"},
                    )
                    self.assertEqual(hostile_origin.status_code, 403)
                    self.assertNotIn("access-control-allow-origin", hostile_origin.headers)
                    preflight = local.options(
                        "/api/team-hub/v1/session",
                        headers={
                            "Origin": "https://evil.test",
                            "Access-Control-Request-Method": "GET",
                        },
                    )
                    self.assertEqual(preflight.status_code, 403)
                    self.assertNotIn("access-control-allow-origin", preflight.headers)

                    remote = TestClient(
                        agent_server.app,
                        base_url="http://localhost",
                        client=("192.0.2.20", 41000),
                    )
                    remote_health = remote.get(
                        "/api/team-hub/v1/health",
                        headers={"Host": "localhost"},
                    )
                    self.assertEqual(remote_health.status_code, 403)

                    with patch.object(
                        agent_server,
                        "managed_server_update_blocks_work",
                        return_value=True,
                    ):
                        maintenance = local.post(
                            "/api/team-hub/v1/sessions/refresh",
                            json={"refresh_token": bundle["refresh_token"]},
                        )
                    self.assertEqual(maintenance.status_code, 503)
                    self.assertEqual(
                        maintenance.json()["error"]["code"],
                        "hub_maintenance",
                    )

                    # Mounted path normalization must keep bootstrap in its
                    # sensitive 8/minute bucket instead of generic POST limits.
                    attempts = [
                        local.post(
                            "/api/team-hub/v1/bootstrap/redeem",
                            headers={
                                "X-Team-Hub-Bootstrap-Proof":
                                    "invalid-proof-that-is-long-enough"
                            },
                            json={
                                "email": "second@example.com",
                                "display_name": "Second",
                                "device_label": "Local Mac",
                            },
                        )
                        for _ in range(9)
                    ]
                    self.assertEqual(attempts[-1].status_code, 429)
                    self.assertEqual(attempts[-1].json()["error"]["code"], "rate_limited")
                    local.close()
                    remote.close()
            finally:
                mount.app = original_mount
                asyncio.run(runtime.shutdown())


if __name__ == "__main__":
    unittest.main()
