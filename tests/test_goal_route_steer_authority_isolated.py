"""Lazy native goal authority ownership and cleanup; no provider or server startup."""
from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from tests import test_goal_route_steer_isolated as route_fixture


SOURCE = route_fixture.SOURCE
tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
         and node.name == "issue_codex_goal_route_steer_authority"]
assert len(nodes) == 1
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
    module="__future__", names=[ast.alias(name="annotations")], level=0,
), *nodes], type_ignores=[])), str(SOURCE), "exec")
del tree, nodes


class GoalRouteSteerAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        route_fixture.GoalRouteSteerTests.setUp(self)
        temporary = tempfile.TemporaryDirectory(prefix="goal-route-authority-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "authority.json"
        self.ns["CROSS_CHAT_CAPABILITIES"].clear()
        self.active["run_id"] = self.current["run_id"] = "run_codexgoal_1234567890abcdef"
        self.ns.update(suppress=suppress, logger=SimpleNamespace(warning=Mock()),
                       concise_error_message=str, provider_authority_runtime_env=AsyncMock())
        exec(CODE, self.ns)

        async def issue(chat, run, references, **kwargs):
            self.assertEqual((chat, run, references), ("chat", self.active["run_id"], []))
            self.assertEqual(kwargs["actions"], {"agent_cross_chat_routes"})
            self.assertTrue(kwargs["async_route_v1"])
            self.assertEqual(kwargs["source_user_instruction"], "Ask @Selected to review")
            self.assertEqual(len(kwargs["provider_route_snapshot"]), 1)
            self.assertEqual(kwargs["provider_route_snapshot"][0]["target_session_id"], "Selected")
            self.path.write_text("synthetic authority", encoding="utf-8")
            self.ns["CROSS_CHAT_CAPABILITIES"]["lazy-token"] = {
                **self.capability, "source_run_id": run, "authority_path": str(self.path),
                "provider_route_grants": {}, "user_delegation_grants": set(),
                "source_user_instruction": kwargs["source_user_instruction"],
            }
            return self.path
        self.ns["issue_cross_chat_capability"] = AsyncMock(side_effect=issue)

    async def prepare(self):
        return await self.ns["prepare_codex_goal_route_steer"](
            "chat", self.active["run_id"], route_fixture.GoalRouteSteerTests.selection(self))

    async def issue(self, prepared):
        return await self.ns["issue_codex_goal_route_steer_authority"](
            "chat", self.active["run_id"], prepared)

    async def test_canonical_resumed_owner_gets_one_exact_selected_authority(self):
        prepared = await self.prepare()
        self.assertIsNone(prepared["capability"])
        self.assertEqual(self.ns["CROSS_CHAT_CAPABILITIES"], {})
        self.ns["issue_cross_chat_capability"].assert_not_awaited()
        self.assertTrue(await self.issue(prepared))
        self.assertIs(prepared["capability"], self.ns["CROSS_CHAT_CAPABILITIES"]["lazy-token"])
        self.assertTrue(await self.ns["commit_codex_goal_route_steer"]("chat", self.active["run_id"], prepared))
        self.assertEqual(set(prepared["capability"]["provider_route_grants"]), {self.routes[1]["route_id"]})
        self.ns["provider_authority_runtime_env"].assert_awaited_once_with(
            self.active["run_id"], self.path, "chat", [])
        self.assertTrue(self.path.exists())

    async def test_existing_authority_is_never_rotated(self):
        prepared = await self.prepare()
        self.ns["CROSS_CHAT_CAPABILITIES"]["existing"] = {
            "source_run_id": self.active["run_id"], "authority_path": "existing-path"}
        self.assertFalse(await self.issue(prepared))
        self.ns["issue_cross_chat_capability"].assert_not_awaited()
        self.assertEqual(set(self.ns["CROSS_CHAT_CAPABILITIES"]), {"existing"})

    async def test_normal_stopped_or_revoked_owner_cannot_issue_new_authority(self):
        for change in ("ordinary", "stop", "route"):
            with self.subTest(change=change):
                self.setUp()
                prepared = await self.prepare()
                if change == "ordinary": self.active["codex_native_operation_kind"] = None
                elif change == "stop": self.active["stop_requested"] = True
                else: self.sessions["chat"]["_revoked_provider_cross_chat_route_ids"] = [self.routes[1]["route_id"]]
                self.assertFalse(await self.issue(prepared))
                self.ns["issue_cross_chat_capability"].assert_not_awaited()
                self.assertFalse(self.path.exists())

    async def test_binding_failure_cleans_only_new_record_and_file(self):
        prepared = await self.prepare()
        neighbor = {"source_run_id": "run_neighbor", "authority_path": "neighbor-path"}
        self.ns["CROSS_CHAT_CAPABILITIES"]["neighbor"] = neighbor
        self.ns["provider_authority_runtime_env"].side_effect = OSError("synthetic binding failure")
        self.assertFalse(await self.issue(prepared))
        self.assertEqual(self.ns["CROSS_CHAT_CAPABILITIES"], {"neighbor": neighbor})
        self.assertFalse(self.path.exists())
        self.assertIsNone(prepared["capability"])

    async def test_stop_during_runtime_binding_removes_new_authority(self):
        prepared = await self.prepare()
        async def stop(*_args):
            self.active["stop_requested"] = True
        self.ns["provider_authority_runtime_env"].side_effect = stop
        self.assertFalse(await self.issue(prepared))
        self.assertEqual(self.ns["CROSS_CHAT_CAPABILITIES"], {})
        self.assertFalse(self.path.exists())

    async def test_cancellation_during_binding_cleans_owned_authority(self):
        prepared = await self.prepare()
        self.ns["provider_authority_runtime_env"].side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.issue(prepared)
        self.assertEqual(self.ns["CROSS_CHAT_CAPABILITIES"], {})
        self.assertFalse(self.path.exists())

    async def test_old_noncanonical_goal_run_is_rejected_before_issue(self):
        self.active["run_id"] = self.current["run_id"] = "codexgoal_legacy"
        with self.assertRaises(self.ns["NativeSteerHandoffError"]):
            await self.prepare()
        self.ns["issue_cross_chat_capability"].assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
