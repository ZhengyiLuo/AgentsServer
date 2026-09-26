"""Production goal routes with isolated store/transport; no live provider I/O."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import BaseModel, Field

from claude_goals import ClaudeGoalProjection
from claude_sdk_client import ClaudeSDKSupervisorError


def load_routes():
    names = {"refresh_claude_goal", "require_claude_goal_session",
             "start_claude_goal_command", "put_claude_goal", "delete_claude_goal"}
    source = (Path(__file__).resolve().parents[1] / "agent_server.py")
    tree = ast.parse(source.read_text(), filename=str(source))
    selected = [node for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
                or isinstance(node, ast.ClassDef) and node.name == "ClaudeGoalRequest"]
    ns = {
        "__name__": __name__, "app": FastAPI(), "Any": Any, "Path": Path,
        "asyncio": asyncio, "BaseModel": BaseModel, "Field": Field,
        "HTTPException": HTTPException, "ClaudeSDKSupervisorError": ClaudeSDKSupervisorError,
        "ClaudeGoalProjection": ClaudeGoalProjection,
        "BACKEND_CLAUDE": "claude", "DEFAULT_BACKEND": "claude",
        "CLAUDE_TRANSPORT": "agent-sdk", "CLAUDE_TRANSPORT_PRINT": "print",
        "CLAUDE_TRANSPORT_AGENT_SDK": "agent-sdk",
        "CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY": "claude-sdk-interactive-v1",
        "claude_sdk_dependency_available": lambda: True,
        "TurnRequest": lambda **kwargs: SimpleNamespace(**kwargs),
        "SkillSelection": lambda **kwargs: SimpleNamespace(**kwargs),
        "claude_provider_id_for_session": lambda session: session.get("claude_session_id"),
        "STORE": SimpleNamespace(sessions={"chat": {"backend": "claude"}}),
        "append_event": AsyncMock(),
        "CLAUDE_GOAL_PROJECTIONS": {}, "CLAUDE_GOAL_PATHS": {}, "CLAUDE_GOAL_LOCKS": {},
        "CLAUDE_GOAL_PENDING": {}, "ACTIVE": {}, "ACTIVE_LOCK": asyncio.Lock(),
        "ensure_session_not_deleting": lambda session_id: None,
        "start_turn": AsyncMock(return_value={"run_id": "new-run"}),
    }
    lifecycle_lock = asyncio.Lock()
    ns["session_lifecycle_lock"] = lambda session_id: lifecycle_lock
    inventory = SimpleNamespace(revision="inventory-revision", records=[
        SimpleNamespace(public={"invocation": "/goal", "id": "opaque-native-goal"})])
    ns["discover_session_provider_commands"] = AsyncMock(return_value=(
        {"support": {"available": True}}, inventory))
    ns["claude_runtime_snapshot"] = AsyncMock(return_value={"available": True, "goal": None})
    ns["claude_sdk_manager"] = AsyncMock(return_value=SimpleNamespace(clear_goal=AsyncMock()))
    exec(compile(ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[])), str(source), "exec"), ns)
    return ns


class ClaudeGoalRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = load_routes()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.ns["app"]), base_url="http://isolated")

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_set_dispatches_only_validated_native_goal_and_does_not_queue(self):
        response = await self.client.put("/api/sessions/chat/claude/goal", json={"condition": "  Reply DONE  "})
        self.assertEqual(response.status_code, 200, response.text)
        call = self.ns["start_turn"].await_args
        self.assertEqual(call.args[0], "chat")
        self.assertEqual(call.args[1].prompt, "/goal Reply DONE")
        self.assertEqual(call.args[1].skill_selection.id, "opaque-native-goal")
        self.assertEqual(call.args[1].skill_selection.revision, "inventory-revision")
        self.assertFalse(call.kwargs["queue_if_busy"])

    async def test_unavailable_goal_command_and_busy_start_leave_no_pending_goal(self):
        self.ns["discover_session_provider_commands"].return_value = (
            {"support": {"available": True}}, SimpleNamespace(records=[]))
        response = await self.client.put("/api/sessions/chat/claude/goal", json={"condition": "Reply DONE"})
        self.assertEqual(response.status_code, 409)
        self.ns["start_turn"].assert_not_awaited()
        self.assertEqual(self.ns["CLAUDE_GOAL_PENDING"], {})

    async def test_busy_start_clears_only_its_pending_operation(self):
        pending = {"run_id": "accepted-elsewhere", "previous_set_at": None}
        self.ns["CLAUDE_GOAL_PENDING"]["chat"] = pending
        self.ns["start_turn"].side_effect = HTTPException(409, "busy")
        response = await self.client.put("/api/sessions/chat/claude/goal", json={"condition": "Reply DONE"})
        self.assertEqual(response.status_code, 409)
        self.assertIs(self.ns["CLAUDE_GOAL_PENDING"]["chat"], pending)

    async def test_starting_status_belongs_to_admitted_run_only(self):
        async def accepted(*args, **kwargs):
            self.ns["ACTIVE"]["chat"] = {"run_id": "accepted-run"}
            return {"run_id": "accepted-run"}
        self.ns["start_turn"].side_effect = accepted
        response = await self.client.put("/api/sessions/chat/claude/goal", json={"condition": "Reply DONE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ns["CLAUDE_GOAL_PENDING"]["chat"]["run_id"], "accepted-run")

    async def test_incomplete_native_scan_never_exposes_stale_goal_then_notifies_when_known(self):
        provider = "8a865fcc-fc32-4373-9524-7cc8e62cfbc2"
        self.ns["STORE"].sessions["chat"]["claude_session_id"] = provider
        row = {"type": "attachment", "sessionId": provider, "isSidechain": False,
               "uuid": "f55e0276-7267-4a35-a728-c042e61c961d", "timestamp": "2026-09-22T23:45:34.051Z",
               "attachment": {"type": "goal_status", "sentinel": True, "met": False, "condition": "Finish"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native.jsonl"
            path.write_text(json.dumps(row) + "\n" + '{}\n' * 1_400_000)
            self.ns["find_claude_history"] = lambda sid: path
            first = await self.ns["refresh_claude_goal"]("chat")
            self.assertIsNone(first)
            self.assertFalse(self.ns["CLAUDE_GOAL_PROJECTIONS"]["chat"].caught_up)
            self.ns["append_event"].assert_not_awaited()
            goal = await self.ns["refresh_claude_goal"]("chat")
            self.assertEqual(goal["status"], "active")
            self.ns["append_event"].assert_awaited_once_with("chat", "claude_goal_changed", {})

    async def test_live_clear_targets_exact_active_sdk_run_without_new_turn(self):
        self.ns["ACTIVE"]["chat"] = {"transport": "agent-sdk", "run_id": "exact-active-run"}
        response = await self.client.delete("/api/sessions/chat/claude/goal")
        self.assertEqual(response.status_code, 200, response.text)
        manager = self.ns["claude_sdk_manager"].return_value
        manager.clear_goal.assert_awaited_once_with("chat", run_id="exact-active-run")
        self.ns["start_turn"].assert_not_awaited()

    async def test_idle_clear_uses_native_command_and_invalid_sessions_never_send(self):
        response = await self.client.delete("/api/sessions/chat/claude/goal")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.ns["start_turn"].await_args.args[1].prompt, "/goal clear")
        self.ns["start_turn"].reset_mock()
        self.ns["STORE"].sessions["chat"]["backend"] = "codex"
        response = await self.client.delete("/api/sessions/chat/claude/goal")
        self.assertEqual(response.status_code, 400)
        response = await self.client.put("/api/sessions/missing/claude/goal", json={"condition": "x"})
        self.assertEqual(response.status_code, 404)
        self.ns["start_turn"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
