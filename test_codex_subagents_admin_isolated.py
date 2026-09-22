"""Real settings/auth functions with synthetic state, never the server runtime."""
from __future__ import annotations

import ast
import asyncio
import copy
import errno
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError

from tests.test_codex_subagent_config_isolated import code as config_code


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "read_codex_admin_settings", "codex_subagents_admin_status",
    "get_codex_subagents_admin", "put_codex_subagents_admin",
    "get_codex_subagents_admin_endpoint", "put_codex_subagents_admin_endpoint",
    "put_codex_goals_admin", "codex_goals_admin_status",
    "require_native_admin_control", "token_matches", "decoded_exact_header_secret",
    "request_exact_native_token_header_authorized", "privileged_native_browser_request_forbidden",
    "privileged_native_json_transport", "bounded_request_content_length",
    "prebuffer_bounded_request_body", "require_agent_token",
}
MODELS = {"CodexSubagentsAdminRequest", "CodexGoalsAdminRequest"}
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
nodes = [copy.deepcopy(node) for node in tree.body if (
    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS
) or (isinstance(node, ast.ClassDef) and node.name in MODELS)]
assert {node.name for node in nodes} == FUNCTIONS | MODELS
for node in nodes:
    node.decorator_list = []
admin_code = compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(SOURCE), "exec")
atomic_tree = ast.parse(SOURCE.with_name("update_runner.py").read_text(encoding="utf-8"))
atomic_code = compile(ast.Module(body=[next(node for node in atomic_tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "atomic_json")], type_ignores=[]),
    "update_runner.py", "exec")


class CodexSubagentsAdminTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="subagents-admin-")
        self.addCleanup(temporary.cleanup)
        self.settings = Path(temporary.name) / "admin" / "codex-settings.json"
        self.ns = {
            "Any": object, "Path": Path, "json": json, "os": os, "asyncio": asyncio,
            "hmac": hmac, "re": re, "logger": Mock(), "BaseModel": BaseModel,
            "Field": Field, "Request": Request, "HTTPException": HTTPException,
            "JSONResponse": JSONResponse, "CODEX_SETTINGS_FILE": self.settings,
            "CODEX_TRANSPORT": "app-server", "CODEX_TRANSPORT_EXEC": "exec",
            "CODEX_GOALS_CONFIG_LOCK": asyncio.Lock(), "CODEX_GOALS_ENABLED": True,
            "CODEX_GOALS_DEFAULT_ENABLED": True, "CODEX_GOALS_RECONFIGURING": False,
            "update_utc_now": lambda: "2030-01-01T00:00:00Z",
            "reserve_codex_goals_reconfiguration": AsyncMock(),
            "release_codex_goals_reconfiguration": AsyncMock(),
            "pause_idle_codex_goals_before_disable": AsyncMock(return_value={}),
            "close_codex_app_server_manager": AsyncMock(),
            "AGENT_TOKEN": "synthetic-native-token",
            "TEAM_HUB_MOUNT_PATH": "/api/team-hub",
            "TEAM_HUB_SERVER_SESSION_MOUNT_PATH": "/api/team-hub-server-session",
            "TEAM_HUB_SERVER_SESSION_ATTACHMENT_CONTENT_PATH_RE": re.compile(r"^never$"),
            "CODEX_PROVIDER_MCP_PATH": "/api/provider-mcp",
            "CODEX_GOALS_ADMIN_MAX_BODY_BYTES": 256,
            "secure_peer_attachment_content_match": lambda path: None,
            "is_agent_helper_route": lambda method, path: False,
            "UNSAFE_HTTP_MUTATION_METHODS": {"POST", "PUT", "PATCH", "DELETE"},
        }
        exec(config_code, self.ns)
        exec(atomic_code, self.ns)
        self.ns["atomic_update_json"] = self.ns["atomic_json"]
        exec(admin_code, self.ns)
        self.ns["request_authorized"] = self.ns["request_exact_native_token_header_authorized"]

    def write(self, value):
        self.settings.parent.mkdir(parents=True, exist_ok=True)
        self.settings.write_text(json.dumps(value), encoding="utf-8")

    async def put(self, value):
        req = self.ns["CodexSubagentsAdminRequest"](max_concurrent_threads_per_session=value)
        return await self.ns["put_codex_subagents_admin"](req)

    def request(self, method="GET", headers=None, query=b""):
        return Request({"type": "http", "method": method,
            "path": "/api/admin/codex/subagents", "scheme": "http",
            "server": ("127.0.0.1", 1), "client": ("127.0.0.1", 2),
            "headers": headers if headers is not None else [(b"x-agentsdock-token", b"synthetic-native-token")],
            "query_string": query})

    async def test_default_has_no_hidden_cap_and_does_not_write_or_probe_provider(self):
        result = await self.ns["get_codex_subagents_admin"]()
        self.assertIsNone(result["max_concurrent_threads_per_session"])
        self.assertTrue(result["configurable"])
        self.assertEqual(result["applies_to"], "new_or_reloaded_threads")
        self.assertEqual(result["provider_config_key"], "agents.max_concurrent_threads_per_session")
        self.assertFalse(self.settings.exists())
        self.ns["close_codex_app_server_manager"].assert_not_called()

    async def test_native_endpoint_model_and_actual_provider_config_roundtrip(self):
        app = FastAPI()
        app.get("/api/admin/codex/subagents")(self.ns["get_codex_subagents_admin_endpoint"])
        app.put("/api/admin/codex/subagents")(self.ns["put_codex_subagents_admin_endpoint"])
        client = TestClient(app)
        headers = {"X-AgentsDock-Token": "synthetic-native-token"}
        for value in (12, 9999, 9007199254740991, None):
            response = client.put("/api/admin/codex/subagents", headers=headers,
                json={"max_concurrent_threads_per_session": value})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["max_concurrent_threads_per_session"], value)
            self.assertEqual(client.get("/api/admin/codex/subagents", headers=headers).json(), response.json())
            config = self.ns["codex_effective_thread_config"]({"id": "synthetic"})
            self.assertEqual(config.get("agents", {}).get("max_concurrent_threads_per_session"), value)
        self.ns["close_codex_app_server_manager"].assert_not_called()
        self.ns["reserve_codex_goals_reconfiguration"].assert_not_called()
        self.ns["pause_idle_codex_goals_before_disable"].assert_not_called()

    async def test_preserves_other_settings_roles_and_chat_override(self):
        original = {"goals_enabled": False, "future": {"keep": True}, "thread_config": {
            "other": "retained", "agents": {"enabled": True, "max_threads": 4,
                "default_subagent_model": "fixture-model", "reviewer": {"description": "keep"}}}}
        self.write(original)
        await self.put(18)
        stored = json.loads(self.settings.read_text())
        expected = copy.deepcopy(original)
        expected["updated_at"] = "2030-01-01T00:00:00Z"
        expected["thread_config"]["agents"].pop("max_threads")
        expected["thread_config"]["agents"]["max_concurrent_threads_per_session"] = 18
        self.assertEqual(stored, expected)
        configured = self.ns["codex_effective_thread_config"]({"id": "synthetic",
            "codex_config_overrides": {"agents": {"max_threads": 30}}})
        self.assertEqual(configured["agents"]["max_concurrent_threads_per_session"], 30)

    async def test_clear_removes_legacy_and_canonical_without_erasing_other_settings(self):
        self.write({"goals_enabled": True, "thread_config": {"agents": {
            "max_threads": 4, "max_concurrent_threads_per_session": 16, "enabled": False}}})
        self.assertIsNone((await self.put(None))["max_concurrent_threads_per_session"])
        self.assertEqual(json.loads(self.settings.read_text())["thread_config"], {"agents": {"enabled": False}})

    async def test_goals_writer_preserves_subagent_and_other_settings(self):
        await self.put(24)
        goal_req = self.ns["CodexGoalsAdminRequest"](enabled=False)
        await self.ns["put_codex_goals_admin"](goal_req)
        stored = json.loads(self.settings.read_text())
        self.assertFalse(stored["goals_enabled"])
        self.assertEqual(stored["thread_config"]["agents"]["max_concurrent_threads_per_session"], 24)
        await self.put(32)
        self.assertFalse(json.loads(self.settings.read_text())["goals_enabled"])

    async def test_settings_write_waits_on_shared_goals_lock(self):
        lock = self.ns["CODEX_GOALS_CONFIG_LOCK"]
        async with lock:
            pending = asyncio.create_task(self.put(8))
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            self.assertFalse(self.settings.exists())
        self.assertEqual((await pending)["max_concurrent_threads_per_session"], 8)

    async def test_malformed_settings_and_disk_full_fail_without_erasing_data(self):
        for value in ([1], {"thread_config": []}, {"thread_config": {"agents": 4}}):
            self.write(value)
            before = self.settings.read_bytes()
            with self.assertRaises(HTTPException) as read_failure:
                await self.ns["get_codex_subagents_admin"]()
            self.assertEqual(read_failure.exception.status_code, 503)
            with self.assertRaises(HTTPException) as raised:
                await self.put(12)
            self.assertEqual(raised.exception.status_code, 503)
            self.assertEqual(self.settings.read_bytes(), before)
        self.write({"goals_enabled": True})
        before = self.settings.read_bytes()
        self.ns["atomic_update_json"] = Mock(side_effect=OSError(errno.ENOSPC, "synthetic full disk"))
        with self.assertRaises(HTTPException) as raised:
            await self.put(12)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.settings.read_bytes(), before)

    async def test_get_never_reports_unreadable_or_invalid_json_as_provider_default(self):
        self.write({})
        self.settings.write_text("{broken", encoding="utf-8")
        with self.assertRaises(HTTPException) as raised:
            await self.ns["get_codex_subagents_admin"]()
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.settings.read_text(), "{broken")
        self.ns["CODEX_SETTINGS_FILE"] = Mock()
        self.ns["CODEX_SETTINGS_FILE"].read_text.side_effect = PermissionError("synthetic unreadable")
        with self.assertRaises(HTTPException) as raised:
            await self.ns["get_codex_subagents_admin"]()
        self.assertEqual(raised.exception.status_code, 503)

    def test_limits_are_nullable_strict_positive_safe_integers_not_coerced(self):
        model = self.ns["CodexSubagentsAdminRequest"]
        for value in (0, -1, True, False, "8", 1.5, 8.0, [], {}, 9007199254740992):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                model(max_concurrent_threads_per_session=value)
        with self.assertRaises(ValidationError):
            model()
        with self.assertRaises(ValidationError):
            model(max_concurrent_threads_per_session=8, extra="unsupported")

    async def test_exec_transport_cannot_persist_placebo_setting(self):
        self.ns["CODEX_TRANSPORT"] = "exec"
        result = await self.ns["get_codex_subagents_admin"]()
        self.assertFalse(result["configurable"])
        self.assertEqual(result["reason"], "unsupported_transport")
        with self.assertRaises(HTTPException) as raised:
            await self.put(8)
        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(self.settings.exists())

    async def test_direct_endpoints_recheck_native_auth(self):
        req = self.ns["CodexSubagentsAdminRequest"](max_concurrent_threads_per_session=8)
        for fn, args in (("get_codex_subagents_admin_endpoint", ()),
                         ("put_codex_subagents_admin_endpoint", (req,))):
            with self.assertRaises(HTTPException) as raised:
                await self.ns[fn](*args, self.request(headers=[]))
            self.assertEqual(raised.exception.status_code, 401)
        self.assertFalse(self.settings.exists())

    async def test_real_server_middleware_rejects_browser_ambiguous_and_bad_transport_before_body(self):
        native = (b"x-agentsdock-token", b"synthetic-native-token")
        cases = [
            ("GET", [], b"", 401),
            ("GET", [], b"token=synthetic-native-token", 401),
            ("GET", [native, native], b"", 401),
            ("GET", [native, (b"origin", b"https://bad.invalid")], b"", 403),
            ("GET", [native, (b"cookie", b"ambient=1")], b"", 403),
            ("GET", [native, (b"sec-fetch-mode", b"cors")], b"", 403),
            ("OPTIONS", [native], b"", 403),
            ("PUT", [native, (b"content-type", b"text/plain")], b"", 415),
            ("PUT", [native, (b"content-type", b"application/json")], b"", 411),
            ("PUT", [native, (b"content-type", b"application/json"), (b"content-length", b"257")], b"", 413),
        ]
        for method, headers, query, expected in cases:
            with self.subTest(method=method, headers=headers):
                next_handler = AsyncMock()
                response = await self.ns["require_agent_token"](self.request(method, headers, query), next_handler)
                self.assertEqual(response.status_code, expected)
                next_handler.assert_not_called()
        next_handler = AsyncMock(return_value="native endpoint")
        self.assertEqual(await self.ns["require_agent_token"](self.request(), next_handler), "native endpoint")


if __name__ == "__main__":
    unittest.main()
