"""Private settings and native administration tests, with synthetic credentials."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import threading
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import AsyncMock, patch

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
        self.cursor_catalog = AsyncMock(return_value={"models": [{"value": "auto", "label": "Auto"}], "discovery_status": "ready"})
        fixture = auth_fixture.CodexAuthTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ns = fixture.ns
        self.ns["codex_provider"] = codex_provider
        self.app = FastAPI()
        self.app.middleware("http")(self.ns["require_agent_token"])
        self.app.include_router(connections.create_router(authorize=self.ns["require_native_admin_control"], store=self.store, check=self.check, cursor_catalog=self.cursor_catalog))
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

    def test_cursor_route_checks_selected_key_only_and_forget_is_revision_fenced(self):
        path = "/api/admin/provider-connections/cursor"
        selected = {**INPUT, "base_url": "https://api2.cursor.sh", "protocol": "cursor", "auth_header": "bearer", "model": None}
        self.assertEqual(self.client.put(path, headers={**NATIVE, "Origin": "https://browser.invalid"}, json=selected).status_code, 403)
        self.cursor_catalog.assert_not_awaited()
        reply = self.client.put(path, headers=NATIVE, json=selected)
        self.assertTrue(reply.json()["ok"])
        self.assertEqual(reply.json()["configuration"]["model"], "auto")
        self.assertNotIn(KEY, reply.text)
        self.check.assert_not_awaited()
        self.assertEqual(self.cursor_catalog.await_count, 1)
        self.assertEqual(self.client.get("/api/admin/provider-models/cursor", headers=NATIVE).json()["models"][0]["value"], "auto")
        self.cursor_catalog.return_value = {"models": [], "discovery_status": "unavailable"}
        reply = self.client.put(path, headers=NATIVE, json={**selected, "api_key": "invalid-fixture", "expected_revision": 1})
        self.assertFalse(reply.json()["ok"])
        self.assertEqual(self.store.read("cursor")["api_key"], KEY)
        self.assertEqual(self.client.request("DELETE", path, headers=NATIVE, json={"expected_revision": 0}).status_code, 409)
        self.assertFalse(self.client.request("DELETE", path, headers=NATIVE, json={"expected_revision": 1}).json()["configured"])
        self.assertFalse(self.store.public("claude")["configured"])

    def test_account_metadata_requires_native_admin_and_never_changes_auth(self):
        path = "/api/admin/provider-accounts/opencode"
        self.assertNotEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get(path, headers={**NATIVE, "Origin": "https://example.test"}).status_code, 403)
        result = self.client.get(path, headers=NATIVE)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["cache-control"], "no-store")
        self.assertEqual(result.json(), {"backend": "opencode", "email": None, "plan_type": None, "source": "unavailable"})
        self.check.assert_not_awaited()
    def test_cached_claude_fields_are_allowlisted_and_env_identity_is_not_misattributed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".claude").mkdir()
            (root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "fixture@example.test", "other": "private"}}))
            (root / ".claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": KEY, "subscriptionType": "max"}}))
            with patch.object(Path, "home", return_value=root), patch.object(connections.subprocess, "run") as process:
                result = connections.native_account_metadata("claude", env={})
                self.assertEqual(result, {"backend": "claude", "email": "fixture@example.test", "plan_type": "max", "source": "local_profile"})
                self.assertNotIn(KEY, json.dumps(result))
                self.assertIsNone(connections.native_account_metadata("claude", env={"ANTHROPIC_API_KEY": KEY})["email"])
                process.assert_not_called()

    def test_cursor_uses_only_status_and_about_and_does_not_guess_missing_plan(self):
        from unittest.mock import Mock
        command = Mock(side_effect=[subprocess.CompletedProcess([], 0, "Logged in as fixture@example.test", ""),
                                    subprocess.CompletedProcess([], 0, "Subscription Tier   Pro", "")])
        result = connections.native_account_metadata("cursor", env={}, cursor_executable="/fixture/agent", command=command)
        self.assertEqual(result, {"backend": "cursor", "email": "fixture@example.test", "plan_type": "Pro", "source": "cli"})
        self.assertEqual([item.args[0] for item in command.call_args_list], [["/fixture/agent", "status"], ["/fixture/agent", "about"]])

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
                if field == "model" and invalid is None:
                    continue
                result = self.client.put(PATH, headers=NATIVE, json={**INPUT, field: invalid})
                self.assertEqual(result.status_code, 400, (field, invalid, result.text))
                self.assertNotIn(KEY, result.text)
        for path in (PATH.replace("claude", "cursor"), PATH.replace("claude", "codex")):
            self.assertEqual(self.client.put(path, headers=NATIVE, json=INPUT).status_code, 400)
        self.check.assert_not_awaited()

    def test_optional_model_is_saved_as_null_for_read_only_verification(self):
        response = self.client.put(PATH, headers=NATIVE, json={**INPUT, "model": None})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["configuration"]["model"])
        self.assertIsNone(self.store.read("claude")["model"])

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
        self.public_status = 401
        case = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                case.calls.append((self.path, dict(self.headers), None))
                self.send_response(case.status if self.headers.get("Authorization") or self.headers.get("x-api-key") else case.public_status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                self.wfile.write(json.dumps(case.payload).encode())

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                case.calls.append((self.path, dict(self.headers), body))
                self.send_response(case.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Location", "/credential-leak")
                self.end_headers()
                payload = case.payload(body) if callable(case.payload) else case.payload
                self.wfile.write(json.dumps(payload).encode())
            def log_message(self, *args):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown(); self.thread.join(); self.server.server_close()

    async def test_two_step_setup_uses_real_http_without_inference_until_model_check(self):
        self.public_status = 200
        self.payload = {"data": [{"id": "fixture/model"}]}
        with tempfile.TemporaryDirectory() as directory:
            store = connections.ConnectionStore(Path(directory) / "private")
            fixture = auth_fixture.CodexAuthTests()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.ns["codex_provider"] = codex_provider
            app = FastAPI()
            app.middleware("http")(fixture.ns["require_agent_token"])
            app.include_router(connections.create_router(authorize=fixture.ns["require_native_admin_control"], store=store))
            with TestClient(app) as client:
                route = "/api/admin/provider-connections/opencode"
                models = "/api/admin/provider-models/opencode"
                response = client.put(route, headers=NATIVE, json={**INPUT, "base_url": self.base, "model": None})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["ok"])
                self.assertEqual(response.json()["status"], "model_required")
                self.assertTrue(response.json()["configuration"]["configured"])
                listed = client.get(models, headers=NATIVE)
                self.assertEqual(listed.json()["models"], [{"value": "fixture/model", "label": "fixture/model"}])
                self.assertIsNone(listed.json()["default_model"])
                self.assertTrue(all(call[2] is None for call in self.calls))
                self.assertFalse(store.catalog("opencode")["available"])
                chosen = client.put(models, headers=NATIVE, json={"model": "fixture/model", "expected_revision": 1})
                self.assertEqual(chosen.status_code, 200, chosen.text)
                self.assertTrue(all(call[2] is None for call in self.calls))
                self.payload = {"type": "message", "content": [{"type": "text", "text": "OK"}]}
                verified = client.post(route + "/check", headers=NATIVE, json={"expected_revision": 2})
                self.assertTrue(verified.json()["ok"])
                posts = [call for call in self.calls if call[2] is not None]
                self.assertEqual(len(posts), 1)
                self.assertEqual(posts[0][2]["model"], "fixture/model")
                self.assertEqual(posts[0][2]["max_tokens"], 256)
                self.assertNotIn(KEY, verified.text)
                self.assertEqual(store.bind({"backend": "opencode", "provider_connection": "custom"})["model"], "fixture/model")

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

    async def test_model_check_leaves_a_bounded_reasoning_budget_without_retrying(self):
        # A live reasoning model used 76 output tokens to answer "OK". At 32
        # it returned HTTP 200, empty text and finish_reason=length instead.
        for protocol in ("anthropic", "responses", "chat_completions"):
            with self.subTest(protocol=protocol):
                def reply(body):
                    enough = body.get("max_output_tokens", body.get("max_tokens", 0)) >= 76
                    if protocol == "anthropic":
                        return {"type": "message", "content": [{"type": "text", "text": "OK"}] if enough else [], "stop_reason": "end_turn" if enough else "max_tokens"}
                    if protocol == "responses":
                        return {"status": "completed" if enough else "incomplete", "output": [{"content": [{"type": "output_text", "text": "OK"}]}] if enough else []}
                    return {"choices": [{"finish_reason": "stop" if enough else "length", "message": {"content": "OK" if enough else "", "reasoning_content": "bounded reasoning"}}]}
                self.payload = reply
                before = len(self.calls)
                selected = {**INPUT, "base_url": self.base, "protocol": protocol}
                self.assertEqual(await connections.probe(selected), "verified")
                self.assertEqual(len(self.calls), before + 1)
                body = self.calls[-1][2]
                self.assertEqual(body.get("max_output_tokens", body.get("max_tokens")), 256)
                self.assertNotIn("tools", body)

    async def test_reasoning_only_or_empty_output_still_cannot_verify(self):
        cases = [
            ("chat_completions", {"choices": [{"finish_reason": "length", "message": {"content": "", "reasoning_content": "not an answer"}}]}),
            ("chat_completions", {"choices": [{"message": {"content": None}}]}),
            ("anthropic", {"type": "message", "content": [{"type": "thinking", "thinking": "not an answer"}]}),
            ("responses", {"status": "incomplete", "output": []}),
        ]
        for protocol, payload in cases:
            with self.subTest(protocol=protocol, payload=payload):
                self.payload = payload
                before = len(self.calls)
                self.assertEqual(await connections.probe({**INPUT, "base_url": self.base, "protocol": protocol}), "invalid_response")
                self.assertEqual(len(self.calls), before + 1)

    async def test_anthropic_check_catalog_and_opencode_runtime_share_api_prefix(self):
        for suffix in ("", "/v1", "/proxy/anthropic", "/proxy/anthropic/v1"):
            for auth in ("bearer", "x-api-key"):
                with self.subTest(suffix=suffix, auth=auth), tempfile.TemporaryDirectory() as directory:
                    selected = {**INPUT, "base_url": self.base + suffix, "auth_header": auth}
                    expected_prefix = suffix if suffix.endswith("/v1") else suffix + "/v1"
                    self.payload = {"type": "message", "content": [{"type": "text", "text": "OK"}]}
                    self.assertEqual(await connections.probe(selected), "verified")
                    checked_path = self.calls[-1][0]
                    self.assertEqual(checked_path, expected_prefix + "/messages")
                    store = connections.ConnectionStore(Path(directory) / "private")
                    store.write("opencode", 0, selected, "verified")
                    chat = {"backend": "opencode", "provider_connection": "custom"}
                    chat["provider_connection_revision"] = store.bind(chat)["credential_id"]
                    original = store.for_session(chat)
                    env = store.opencode_overrides(chat, {})
                    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
                    runtime_base = config["provider"]["agentsdock_custom"]["options"]["baseURL"]
                    self.assertEqual(runtime_base + "/messages", self.base + checked_path)
                    self.assertEqual(store.for_session(chat), original)
                    self.payload = {"data": [{"id": "test/model"}]}
                    self.assertEqual(await connections.probe_credentials(selected), "verified")
                    self.assertEqual(self.calls[-1][0], expected_prefix + "/models")
                    self.assertTrue(connections.discover_models(selected))
                    self.assertEqual(self.calls[-1][0].split("?")[0], expected_prefix + "/models")

    async def test_read_only_key_check_requires_a_protected_endpoint(self):
        self.payload = {"data": [{"id": "fixture/model"}]}
        for protocol, header, path in [("responses", "bearer", "/models"), ("anthropic", "x-api-key", "/v1/models")]:
            selected = {**INPUT, "base_url": self.base, "model": None, "protocol": protocol, "auth_header": header}
            self.public_status = 401
            self.assertEqual(await connections.probe(selected), "verified")
            self.assertEqual(self.calls[-2][0], path)
            self.assertEqual(self.calls[-1][0], path)
            self.assertNotIn("Authorization", self.calls[-1][1])
            self.assertNotIn("x-api-key", self.calls[-1][1])
            self.public_status = 200
            self.assertEqual(await connections.probe(selected), "model_required")
        self.assertTrue(all(body is None for _, _, body in self.calls))  # no inference or tools

    async def test_read_only_auth_errors_redirect_and_missing_routes_never_verify(self):
        for status, expected in [(401, "authentication_failed"), (429, "rate_limited"), (302, "connection_failed"), (404, "model_required")]:
            self.status = status
            self.assertEqual(await connections.probe({**INPUT, "base_url": self.base, "model": None}), expected)
        self.assertEqual(len(self.calls), 4)

    async def test_openrouter_uses_private_key_status_not_public_model_list(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": {"label": "private-key-label", "limit": 100}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(connections.httpx, "AsyncClient", return_value=client):
            self.assertEqual(await connections.probe_credentials({**INPUT, "base_url": "https://openrouter.ai/api"}), "verified")
        self.assertEqual(seen, ["https://openrouter.ai/api/v1/key"])

    async def test_lookalike_router_is_not_treated_as_private_key_endpoint(self):
        seen = []
        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, json={"data": []})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(connections.httpx, "AsyncClient", return_value=client):
            self.assertEqual(await connections.probe_credentials({**INPUT, "base_url": "https://openrouter.ai.example/api/v1"}), "model_required")
        self.assertEqual(seen, ["https://openrouter.ai.example/api/v1/models"] * 2)


if __name__ == "__main__":
    unittest.main()
