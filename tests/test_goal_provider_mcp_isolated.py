"""Native goal MCP ownership with absent SDK client metadata; no provider startup."""
from __future__ import annotations

import ast
import asyncio
import copy
import hmac
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException
from starlette.responses import JSONResponse, Response


SOURCE = Path(__file__).resolve().parents[1] / "agent_server.py"
NAMES = {"codex_provider_mcp", "codex_provider_mcp_jsonrpc_error", "provider_tool_active_matches",
         "codex_native_mailbox_owner_matches", "provider_capability_is_attached_to_live_run"}
tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name in NAMES]
assert {node.name for node in nodes} == NAMES
for node in nodes:
    node.decorator_list = []
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
    module="__future__", names=[ast.alias(name="annotations")], level=0,
), *nodes], type_ignores=[])), str(SOURCE), "exec")
del tree, nodes


class GoalProviderMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.active = {
            "run_id": "run_goal", "backend": "codex", "transport": "app_server",
            "provider_thread_id": "thread", "provider_turn_id": "turn", "provider_turn_ready": True,
            "codex_native_operation": True, "codex_native_operation_kind": "goal_resume",
            "codex_control_reservation_id": "reservation",
        }
        self.current = {"run_id": "run_goal", "codex_control_reservation_id": "reservation"}
        self.session = {"codex_thread_id": "thread", "codex_goal": {"status": "active"}}
        self.manager = SimpleNamespace(ready=True)
        self.meta = {"thread_id": "thread", "turn_id": "turn"}
        self.payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "run", "arguments": {"helper": "chats", "arguments": ["list"]},
            "_meta": {"callId": "call-goal", "x-codex-turn-metadata": self.meta},
        }}
        self.ns = {
            "asyncio": asyncio, "hmac": hmac, "re": re, "JSONResponse": JSONResponse,
            "Response": Response, "HTTPException": HTTPException,
            "ProviderToolError": type("ProviderToolError", (Exception,), {}),
            "BACKEND_CODEX": "codex", "BACKEND_CURSOR": "cursor", "BACKEND_OPENCODE": "opencode",
            "BACKEND_GEMINI": "gemini", "CODEX_TRANSPORT_APP_SERVER": "app_server",
            "ACTIVE": {"chat": self.active}, "CURRENT_TURNS": {"chat": self.current},
            "ACTIVE_LOCK": asyncio.Lock(), "BUSY_SESSIONS": {"chat"}, "STOPPED_RUNS": set(),
            "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
            "STORE": SimpleNamespace(sessions={"chat": self.session}),
            "existing_codex_app_server_manager": lambda _session: self.manager,
            "session_references_codex_thread": lambda session, thread: session.get("codex_thread_id") == thread,
            "codex_provider_mcp_run_proof": lambda *_args: "a" * 64,
            "execute_provider_tool_once": AsyncMock(return_value=("selected route", False)),
        }
        exec(CODE, self.ns)

    async def invoke(self, *, authenticated=True):
        request = SimpleNamespace(state=SimpleNamespace(codex_provider_mcp_authenticated=authenticated),
                                  json=AsyncMock(return_value=self.payload))
        response = await self.ns["codex_provider_mcp"](request)
        return json.loads(response.body)

    async def test_authenticated_exact_native_goal_can_use_missing_sdk_metadata(self):
        result = await self.invoke()
        self.assertEqual(result["result"]["content"][0]["text"], "selected route")
        self.ns["execute_provider_tool_once"].assert_awaited_once_with(
            "chat", "run_goal", self.payload["params"]["arguments"], replay_key="codex:call-goal",
            backend="codex", provider_thread_id="thread", provider_turn_id="turn")

    async def test_missing_metadata_never_grants_ordinary_stale_or_stopped_owner(self):
        for change in ("ordinary", "turn", "reservation", "current", "stop", "stopped_run", "idle",
                       "deleted", "paused_goal", "closed", "manager", "ambiguous", "no_call", "invalid_run"):
            with self.subTest(change=change):
                self.setUp()
                if change == "ordinary": self.active["codex_native_operation_kind"] = None
                elif change == "turn": self.meta["turn_id"] = "old-turn"
                elif change == "reservation": self.current["codex_control_reservation_id"] = "old-reservation"
                elif change == "current": self.current["run_id"] = "old-run"
                elif change == "stop": self.active["stop_requested"] = True
                elif change == "stopped_run": self.ns["STOPPED_RUNS"].add("run_goal")
                elif change == "idle": self.ns["BUSY_SESSIONS"].clear()
                elif change == "deleted": self.ns["DELETING_SESSIONS"].add("chat")
                elif change == "paused_goal": self.session["codex_goal"]["status"] = "paused"
                elif change == "closed": self.active["codex_goal_handoff_closed"] = True
                elif change == "manager": self.manager.ready = False
                elif change == "ambiguous": self.ns["STORE"].sessions["other"] = copy.deepcopy(self.session)
                elif change == "no_call": self.payload["params"]["_meta"].pop("callId")
                else: self.active["run_id"] = self.current["run_id"] = "codexgoal_old"
                self.assertIn("error", await self.invoke())
                self.ns["execute_provider_tool_once"].assert_not_awaited()

    async def test_supplied_invalid_partial_or_stale_proof_never_falls_back(self):
        for metadata in ({"agentsdock_run_id": "run_goal"}, {"agentsdock_run_proof": "a" * 64},
                         {"agentsdock_run_id": "", "agentsdock_run_proof": ""},
                         {"agentsdock_run_id": "run_old", "agentsdock_run_proof": "a" * 64},
                         {"agentsdock_run_id": "run_goal", "agentsdock_run_proof": "b" * 64},
                         {"agentsdock_run_id": "run_old", "responsesapi_client_metadata": {}},
                         {"responsesapi_client_metadata": {}, "responsesapiClientMetadata": {
                             "agentsdock_run_id": "run_old", "agentsdock_run_proof": "a" * 64}},
                         {"responsesapi_client_metadata": {}, "responsesapiClientMetadata": {
                             "agentsdock_run_proof": "invalid"}},
                         {"responsesapi_client_metadata": {}, "responsesapiClientMetadata": {
                             "agentsdock_run_id": "run_old"}}):
            with self.subTest(metadata=metadata):
                self.setUp()
                self.meta.update(metadata)
                self.assertIn("error", await self.invoke())
                self.ns["execute_provider_tool_once"].assert_not_awaited()

    async def test_subagent_and_unauthenticated_calls_remain_forbidden(self):
        self.meta["parent_thread_id"] = "parent"
        self.assertIn("error", await self.invoke())
        self.meta.pop("parent_thread_id")
        with self.assertRaises(HTTPException):
            await self.invoke(authenticated=False)
        self.ns["execute_provider_tool_once"].assert_not_awaited()

    async def test_existing_regular_run_proof_path_is_unchanged(self):
        self.active["codex_native_operation_kind"] = None
        self.meta.update(agentsdock_run_id="run_goal", agentsdock_run_proof="a" * 64)
        self.assertIn("result", await self.invoke())
        self.ns["execute_provider_tool_once"].assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
