"""Native address discovery without starting providers or inspecting real networks."""
import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from tests import test_codex_subagents_admin_isolated as admin_fixture
import server_instances

SOURCE = Path(__file__).resolve().parents[1] / "agent_server.py"
NODE = next(node for node in ast.parse(SOURCE.read_text()).body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "connection_addresses")
CODE = compile(ast.Module(body=[NODE], type_ignores=[]), str(SOURCE), "exec")


class ConnectionAddressTests(unittest.TestCase):
    def setUp(self):
        fixture = admin_fixture.CodexSubagentsAdminTests()
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        self.ns = fixture.ns
        self.ns.update(app=FastAPI(), asyncio=asyncio, Request=Request, HTTPException=HTTPException,
                       SERVER_BIND_ADDRESS="0.0.0.0", SERVER_PORT=7852,
                       server_identity=lambda: "fixture-server")
        exec(CODE, self.ns)
        self.client = TestClient(self.ns["app"])
        self.addCleanup(self.client.close)
        self.headers = {"X-AgentsDock-Token": self.ns["AGENT_TOKEN"]}

    def test_discovery_requires_native_token_and_returns_binding_aware_networks_without_credentials(self):
        with patch.object(server_instances, "candidate_addresses", return_value=["http://127.0.0.1:7852", "http://192.168.1.2:7852"]), patch.object(server_instances, "tailscale_status", return_value={"status": "connected", "ipv4": "100.64.0.2"}):
            response = self.client.get("/api/connection-addresses", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": 1, "server_identity": "fixture-server", "urls": ["http://100.64.0.2:7852", "http://192.168.1.2:7852"]})
        self.assertNotIn(self.ns["AGENT_TOKEN"], response.text)

    def test_rejects_browser_query_bearer_and_duplicate_credentials_before_discovery(self):
        with patch.object(server_instances, "candidate_addresses") as discovery:
            for headers in ({}, {**self.headers, "Origin": "https://example.com"},
                            {**self.headers, "Sec-Fetch-Mode": "cors"},
                            {"Authorization": "Bearer " + self.ns["AGENT_TOKEN"]},
                            {**self.headers, "X-ZenithDock-Token": self.ns["AGENT_TOKEN"]}):
                self.assertIn(self.client.get("/api/connection-addresses", headers=headers).status_code, (401, 403))
            self.assertEqual(self.client.get("/api/connection-addresses?token=fixture", headers=self.headers).status_code, 401)
            discovery.assert_not_called()

    def test_loopback_binding_never_advertises_other_interfaces(self):
        self.ns["SERVER_BIND_ADDRESS"] = "127.0.0.1"
        with patch.object(server_instances, "tailscale_status", return_value={"status": "connected", "ipv4": "100.64.0.2"}):
            response = self.client.get("/api/connection-addresses", headers=self.headers)
        self.assertEqual(response.json()["urls"], [])
