"""Private settings and native administration tests, with synthetic credentials."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import httpx
import codex_provider
import provider_connections as connections
from tests import test_codex_auth_isolated as auth_fixture

KEY = "synthetic-endpoint-test-not-a-real-key"
INPUT = {"base_url": "https://example.invalid/api", "api_key": KEY, "model": "test/model",
         "protocol": "anthropic", "auth_header": "bearer", "expected_revision": 0}
NATIVE = {"X-AgentsDock-Token": "synthetic-native-token"}
PATH = "/api/admin/provider-connections/claude"


class SettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="endpoint-settings-")
        self.addCleanup(self.temp.cleanup)
        self.store = connections.ConnectionStore(Path(self.temp.name) / "private")
        self.check = AsyncMock(return_value="verified")
        fixture = auth_fixture.CodexAuthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ns = fixture.ns
        self.ns["codex_provider"] = codex_provider
        self.app = FastAPI()
        self.app.middleware("http")(self.ns["require_agent_token"])
        self.app.include_router(connections.create_router(authorize=self.ns["require_native_admin_control"], store=self.store, check=self.check))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_success_persists_private_key_and_returns_only_metadata(self):
        self.assertFalse(self.client.get(PATH, headers=NATIVE).json()["configured"])
        response = self.client.put(PATH, headers=NATIVE, json=INPUT)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["ok"])
        self.assertNotIn(KEY, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.store.read("claude")["api_key"], KEY)
        self.assertEqual((self.store.root / "claude.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.store.public("opencode")["configured"])
        self.ns["codex_app_server_manager"].assert_not_awaited()

    def test_failed_replacement_preserves_old_key_and_stale_revision_never_probes(self):
        self.client.put(PATH, headers=NATIVE, json=INPUT)
        self.check.return_value = "authentication_failed"
        response = self.client.put(PATH, headers=NATIVE, json={**INPUT, "expected_revision": 1, "api_key": "invalid-synthetic"})
        self.assertFalse(response.json()["ok"])
        self.assertEqual(self.store.read("claude")["api_key"], KEY)
        self.assertEqual(self.store.public("claude")["revision"], 1)
        self.check.reset_mock()
        self.assertEqual(self.client.put(PATH, headers=NATIVE, json=INPUT).status_code, 409)
        self.check.assert_not_awaited()

    def test_recheck_records_failure_and_forget_removes_only_settings(self):
        self.client.put(PATH, headers=NATIVE, json=INPUT)
        self.check.return_value = "rate_limited"
        response = self.client.post(PATH + "/check", headers=NATIVE, json={"expected_revision": 1})
        self.assertEqual(response.json()["configuration"]["last_result"], "rate_limited")
        self.assertNotIn(KEY, response.text)
        native = Path(self.temp.name) / "native-history.json"
        native.write_text("unchanged")
        response = self.client.delete(PATH, headers=NATIVE)  # missing body must fail
        self.assertNotEqual(response.status_code, 200)
        response = self.client.request("DELETE", PATH, headers=NATIVE, json={"expected_revision": 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["configured"])
        self.assertNotIn(KEY, (self.store.root / "claude.json").read_text())
        self.assertEqual(native.read_text(), "unchanged")

    def test_browser_and_unauthenticated_requests_cannot_read_or_probe(self):
        for headers, status in [({}, 401), ({"Authorization": "Bearer synthetic-native-token"}, 401),
            ({**NATIVE, "Origin": "https://example.invalid"}, 403), ({**NATIVE, "Sec-Fetch-Mode": "cors"}, 403)]:
            for method in ("GET", "PUT", "POST", "DELETE"):
                result = self.client.request(method, PATH + ("/check" if method == "POST" else ""), headers=headers, json=INPUT)
                self.assertEqual(result.status_code, status, result.text)
                self.assertNotIn(KEY, result.text)
        self.check.assert_not_awaited()
        self.assertFalse(self.store.root.exists())

    def test_rejects_malformed_protocol_model_and_secrets_without_echo(self):
        for field in INPUT:
            for invalid in (None, [], {}, True):
                result = self.client.put(PATH, headers=NATIVE, json={**INPUT, field: invalid})
                self.assertEqual(result.status_code, 400, (field, invalid, result.text))
                self.assertNotIn(KEY, result.text)
        for path in (PATH.replace("claude", "cursor"), PATH.replace("claude", "codex")):
            self.assertEqual(self.client.put(path, headers=NATIVE, json=INPUT).status_code, 400)
        self.check.assert_not_awaited()

    def test_oversized_body_and_symlink_fail_closed(self):
        response = self.client.put(PATH, headers=NATIVE, json={**INPUT, "api_key": "x" * (connections.MAX_BODY_BYTES + 1)})
        self.assertEqual(response.status_code, 413)
        self.store.root.mkdir(mode=0o700)
        target = Path(self.temp.name) / "target"
        target.write_text(KEY)
        (self.store.root / "claude.json").symlink_to(target)
        self.assertEqual(self.client.get(PATH, headers=NATIVE).status_code, 503)
        self.assertEqual(target.read_text(), KEY)
        self.check.assert_not_awaited()

    async def test_concurrent_check_is_rejected_and_cancel_does_not_save(self):
        entered, blocked = asyncio.Event(), asyncio.Event()
        async def slow(_):
            entered.set()
            await blocked.wait()
            return "verified"
        app = FastAPI()
        app.include_router(connections.create_router(authorize=lambda r: None, store=self.store, check=slow))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            task = asyncio.create_task(client.put(PATH, json=INPUT))
            await entered.wait()
            self.assertEqual((await client.put(PATH, json=INPUT)).status_code, 409)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.store.public("claude")["configured"])


class WireProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.status, self.payload = 200, {}
        case = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                case.calls.append((self.path, dict(self.headers), json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(case.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                self.wfile.write(json.dumps(case.payload).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close()

    async def test_real_http_protocol_paths_headers_and_response_validation(self):
        cases = [("anthropic", "bearer", "/api", "/api/v1/messages", {"type": "message", "content": [{"type": "text", "text": "OK"}]}),
            ("anthropic", "x-api-key", "/v1", "/v1/messages", {"type": "message", "content": [{"type": "text", "text": "OK"}]}),
            ("responses", "bearer", "/v1", "/v1/responses", {"status": "completed", "output": [{"content": [{"type": "output_text", "text": "OK"}]}]}),
            ("chat_completions", "bearer", "/v1", "/v1/chat/completions", {"choices": [{"message": {"content": "OK"}}]})]
        for protocol, auth, base, path, payload in cases:
            self.payload = payload
            selected = {**INPUT, "base_url": self.base + base, "protocol": protocol, "auth_header": auth}
            self.assertEqual(await connections.probe(selected), "verified")
            actual, headers, body = self.calls[-1]
            self.assertEqual(actual, path)
            self.assertEqual(headers.get("x-api-key") if auth == "x-api-key" else headers.get("Authorization"), KEY if auth == "x-api-key" else "Bearer " + KEY)
            self.assertNotIn("tools", body)
            self.assertNotIn(KEY, json.dumps(body))
            self.assertEqual(body["model"], "test/model")

    async def test_redirect_auth_errors_and_invalid_responses_never_verify(self):
        for status, expected in ((307, "unsupported"), (401, "authentication_failed"), (403, "authentication_failed"), (429, "rate_limited"), (500, "connection_failed")):
            self.status = status
            self.payload = {"error": KEY}
            self.assertEqual(await connections.probe({**INPUT, "base_url": self.base}), expected)
        self.assertEqual(len(self.calls), 5)  # no redirects or retries
        self.status = 200
        for payload in ({}, [], {"type": "message", "content": "wrong"}, {"text": "x" * 150000}):
            self.payload = payload
            self.assertEqual(await connections.probe({**INPUT, "base_url": self.base}), "invalid_response")


if __name__ == "__main__":
    unittest.main()
