"""HTTP transport guards; run through the repository's isolated server harness."""
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import agent_server


class ServerUpdateEnsureHTTPTests(unittest.TestCase):
    def request_body(self):
        return {
            "expected_server_identity": "server-current",
            "expected_server_instance_id": "instance-current",
            "manifest_base64": "e30=",
            "signature_base64": "A" * 88,
        }

    def test_native_authentication_precedes_json_parsing(self):
        cases = [
            ("/api/admin/update/ensure", [], 401),
            ("/api/admin/update/ensure?token=secret", [], 401),
            ("/api/admin/update/ensure", [("Authorization", "Bearer secret")], 401),
            ("/api/admin/update/ensure", [("X-AgentsDock-Token", "secret"), ("X-AgentsDock-Token", "secret")], 401),
            ("/api/admin/update/ensure", [("X-AgentsDock-Token", "secret"), ("Origin", "https://example.invalid")], 403),
        ]
        with patch.object(agent_server, "AGENT_TOKEN", "secret"), \
                patch.object(agent_server, "ensure_server_update", new_callable=AsyncMock) as ensure:
            client = TestClient(agent_server.app)
            for path, headers, expected in cases:
                with self.subTest(headers=headers):
                    response = client.post(path, content=b"{", headers=[("Content-Type", "application/json"), *headers])
                    self.assertEqual(response.status_code, expected)
            ensure.assert_not_awaited()

    def test_authenticated_route_binds_both_server_identity_and_instance(self):
        with patch.object(agent_server, "AGENT_TOKEN", "secret"), \
                patch.object(agent_server, "server_identity", return_value="server-current"), \
                patch.object(agent_server, "SERVER_INSTANCE_ID", "instance-current"), \
                patch.object(agent_server, "ensure_server_update", new_callable=AsyncMock, return_value={"phase": "pending", "reconciliation": "pending"}) as ensure:
            client = TestClient(agent_server.app)
            response = client.post("/api/admin/update/ensure", json=self.request_body(), headers={"X-AgentsDock-Token": "secret"})
            self.assertEqual(response.status_code, 200, response.text)
            ensure.assert_awaited_once()
            ensure.reset_mock()
            for changed in ({"expected_server_instance_id": "old-instance"}, {"expected_server_identity": None, "expected_server_instance_id": None}):
                response = client.post("/api/admin/update/ensure", json={**self.request_body(), **changed}, headers={"X-AgentsDock-Token": "secret"})
                self.assertIn(response.status_code, (400, 409), response.text)
            ensure.assert_not_awaited()

    def test_ensure_transport_has_a_finite_body_bound(self):
        with patch.object(agent_server, "AGENT_TOKEN", "secret"), \
                patch.object(agent_server, "ensure_server_update", new_callable=AsyncMock) as ensure:
            response = TestClient(agent_server.app).post(
                "/api/admin/update/ensure", content=b" " * 16_385,
                headers={"X-AgentsDock-Token": "secret", "Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 413)
            ensure.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
