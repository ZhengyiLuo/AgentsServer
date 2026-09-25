"""Real usage HTTP authorization/projection with inert native provider clients."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import provider_usage
import test_codex_subagents_admin_isolated as admin_fixture
from test_provider_usage import FakeManager


SOURCE = Path(__file__).with_name("agent_server.py")
NAMES = {"runtime_provider_usage", "observe_claude_provider_usage",
         "project_codex_usage_notification", "broadcast_codex_usage_changed",
         "broadcast_provider_usage_changed"}
nodes = [node for node in ast.parse(SOURCE.read_text()).body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
assert {node.name for node in nodes} == NAMES
for node in nodes:
    node.decorator_list = []
CODE = compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec")
HEADERS = {"X-AgentsDock-Token": "synthetic-native-token"}


class UsageRoutesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixture = admin_fixture.CodexSubagentsAdminTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ns = fixture.ns
        self.manager = FakeManager()
        self.claude = SimpleNamespace(usage_generation=Mock(return_value="claude-owner:1"))
        self.ns.update({"Any": object, "CodexAppServerManager": object,
            "provider_usage": provider_usage, "PROVIDER_USAGE": provider_usage.ProviderUsage(),
            "STORE": SimpleNamespace(sessions={
                "native": {"id": "native", "backend": "codex"},
                "custom": {"id": "custom", "backend": "codex", "codex_provider": "custom"},
                "claude": {"id": "claude", "backend": "claude"}}),
            "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude", "DEFAULT_BACKEND": "codex",
            "CLAUDE_SDK_MANAGER": self.claude,
            "codex_provider": SimpleNamespace(session_choice=lambda value: value or "default"),
            "codex_app_server_manager": AsyncMock(return_value=self.manager),
            "existing_codex_app_server_manager": lambda session: self.manager,
            "append_event": AsyncMock(), "broadcast_provider_runtime_changed": AsyncMock()})
        exec(CODE, self.ns)
        app = FastAPI()
        app.get("/api/runtime/usage")(self.ns["runtime_provider_usage"])
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def read(self, query="backend=codex&session_id=native", headers=None):
        return self.client.get("/api/runtime/usage?" + query, headers=HEADERS if headers is None else headers)

    def test_native_http_read_and_refresh(self):
        first = self.read()
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(first.json()["windows"][0]["used_percent"], 25)
        self.assertEqual(self.read().json(), first.json())
        self.assertEqual(len(self.manager.calls), 2)
        self.read("backend=codex&session_id=native&refresh=true")
        self.assertEqual(len(self.manager.calls), 4)

    def test_auth_and_browser_guard_run_before_provider_access(self):
        for headers, expected in (({}, 401), ({"X-AgentsDock-Token": "wrong"}, 401),
                                  ({**HEADERS, "Sec-Fetch-Mode": "cors"}, 403)):
            self.assertEqual(self.read(headers=headers).status_code, expected)
        self.ns["codex_app_server_manager"].assert_not_awaited()

    def test_custom_session_and_draft_never_fall_back_to_native_account(self):
        for query in ("backend=codex&session_id=custom", "backend=codex&codex_provider=custom"):
            result = self.read(query)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["account_kind"], "custom")
        self.ns["codex_app_server_manager"].assert_not_awaited()
        self.assertEqual(self.manager.calls, [])

    def test_wrong_backend_or_unknown_session_never_reads_other_account(self):
        self.assertEqual(self.read("backend=codex&session_id=claude").json()["reason"], "selection_changed")
        self.assertEqual(self.read("backend=codex&session_id=missing").status_code, 404)
        self.ns["codex_app_server_manager"].assert_not_awaited()

    def test_claude_without_event_is_unavailable_and_never_starts_provider(self):
        result = self.read("backend=claude&session_id=claude&refresh=true")
        self.assertEqual(result.json()["status"], "unavailable")
        self.ns["codex_app_server_manager"].assert_not_awaited()
        self.claude.usage_generation.assert_called_once_with("claude")

    async def test_typed_claude_event_is_metadata_only_and_visible_to_http_read(self):
        RateLimitEvent = type("RateLimitEvent", (), {})
        message = RateLimitEvent()
        message.rate_limit_info = SimpleNamespace(rate_limit_type="five_hour", utilization=.3, status="allowed", resets_at=2000000000)
        await self.ns["observe_claude_provider_usage"]("claude", "claude-owner:1", message)
        self.ns["append_event"].assert_not_awaited()
        self.ns["broadcast_provider_runtime_changed"].assert_awaited_once_with("claude", {
            "type": "provider_usage_changed", "backend": "claude", "ephemeral": True})
        result = self.read("backend=claude&session_id=claude").json()
        self.assertEqual(result["windows"][0]["used_percent"], 30)
        self.assertEqual(result["source"], "claude-events")

    async def test_claude_event_from_retired_owner_does_not_populate_usage(self):
        self.claude.usage_generation.return_value = None
        await self.ns["observe_claude_provider_usage"]("claude", "retired-owner:1", {
            "type": "rate_limit_event", "rate_limit_info": {"rate_limit_type": "five_hour", "utilization": .9}})
        self.ns["broadcast_provider_runtime_changed"].assert_not_awaited()
        self.assertEqual(self.read("backend=claude&session_id=claude").json()["status"], "unavailable")

    async def test_codex_event_updates_cache_and_only_invalidates_matching_native_chats(self):
        await self.ns["PROVIDER_USAGE"].read_codex(self.manager)
        await self.ns["project_codex_usage_notification"](self.manager, {"method": "account/rateLimits/updated", "params": {
            "rateLimits": {"primary": {"usedPercent": 55}}}})
        self.ns["broadcast_provider_runtime_changed"].assert_awaited_once_with("native", {
            "type": "provider_usage_changed", "backend": "codex", "ephemeral": True})
        self.assertEqual(self.read().json()["windows"][0]["used_percent"], 55)
        self.assertEqual(len(self.manager.calls), 2)


if __name__ == "__main__":
    unittest.main()
