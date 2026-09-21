"""Exact-process child liveness and shared scan budget; no server/provider startup."""
from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


NAMES = {
    "normalize_subagent_status", "codex_child_status_from_turn",
    "codex_subagent_has_live_owner", "active_codex_work_labels", "active_generated_title_work_labels",
    "codex_subagent_native_turn_candidates", "codex_subagent_native_turn_scopes",
    "cached_codex_subagent_native_statuses", "cache_codex_subagent_native_statuses",
    "prepare_codex_subagent_terminal_snapshot", "provider_background_work_labels_from_snapshot",
}
TREE = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
NODES = [node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name in NAMES]
assert {node.name for node in NODES} == NAMES
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",
    names=[ast.alias(name="annotations")], level=0), *NODES], type_ignores=[])),
    "<isolated-provider-background>", "exec")


class ProviderBackgroundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.normal = self.manager()
        self.custom = self.manager()
        self.managers = [self.normal, self.custom]
        self.sessions = {
            "normal": {"id": "normal", "backend": "codex"},
            "custom": {"id": "custom", "backend": "codex", "codex_provider": "custom"},
        }
        self.ns = {
            "asyncio": asyncio, "suppress": suppress, "STORE": SimpleNamespace(sessions=self.sessions),
            "DEFAULT_BACKEND": "codex", "BACKEND_CODEX": "codex",
            "CODEX_APP_SERVER_MANAGER": self.normal, "CLAUDE_SDK_MANAGER": None,
            "codex_app_server_managers": lambda: tuple(self.managers),
            "existing_codex_app_server_manager": lambda session: self.custom
                if (session or {}).get("codex_provider") == "custom" else self.normal,
            "CODEX_SUBAGENT_INDEX_LOCK": threading.RLock(), "CODEX_SUBAGENT_STATE": {},
            "CODEX_SUBAGENT_SESSION_INDEX": {}, "CODEX_SUBAGENT_LIVE_GENERATIONS": {},
            "CODEX_SUBAGENT_LIVE_MANAGERS": {}, "CODEX_SUBAGENT_NATIVE_STATUS_CACHES": {},
            "CODEX_SUBAGENT_NATIVE_STATUS_CACHE_CLOCK": 0,
            "BUSY_SESSIONS": set(), "ACTIVE": {}, "CURRENT_TURNS": {},
            "SERVER_MAINTENANCE_SESSIONS": set(), "CODEX_NATIVE_ACTION_TASKS": {},
            "CODEX_PENDING_INTERACTIONS": {}, "SERVER_UPDATE_CODEX_SUBAGENT_SCAN_LIMIT": 2,
            "SERVER_UPDATE_CODEX_SUBAGENT_SCAN_TIMEOUT_SECONDS": 1,
            "SERVER_UPDATE_PROVIDER_WORK_LABEL_LIMIT": 32, "GENERATED_TITLE_TASKS": {},
            "durable_event_seq": lambda state: state.get("seq"),
            "loaded_claude_background_session_state": lambda manager: ((), ()),
            "claude_event_file_fingerprints": lambda sessions: (),
        }
        exec(CODE, self.ns)

    @staticmethod
    def manager():
        return SimpleNamespace(ready=True, generation=1,
            active_turn=lambda thread: None,
            list_turns=AsyncMock(return_value=[{"id": "latest", "status": "completed"}]))

    def child(self, owner, thread):
        manager = self.custom if owner == "custom" else self.normal
        self.ns["CODEX_SUBAGENT_STATE"][thread] = {
            "session_id": owner, "subagent_status": "running", "id": "event-" + thread, "seq": 1,
        }
        self.ns["CODEX_SUBAGENT_SESSION_INDEX"][thread] = owner
        self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"][thread] = manager.generation
        self.ns["CODEX_SUBAGENT_LIVE_MANAGERS"][thread] = manager

    def labels(self, snapshot):
        return self.ns["provider_background_work_labels_from_snapshot"]({
            "codex": snapshot, "manager": None, "session_ids": (), "unknown_labels": (),
            "fingerprints": (), "claude_labels": (), "consistent": True,
        })

    async def test_custom_children_are_checked_only_in_their_own_process(self):
        self.child("custom", "custom-child")
        self.assertEqual(self.ns["active_codex_work_labels"](), ["Codex subagent custom-child"])
        snapshot = await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.assertEqual(self.labels(snapshot), [])
        self.normal.list_turns.assert_not_awaited()
        self.custom.list_turns.assert_awaited_once_with(
            "custom-child", limit=1, items_view="summary", sort_direction="desc")

    async def test_loaded_manager_remains_visible_if_saved_credentials_become_unavailable(self):
        self.child("custom", "custom-child")
        self.ns["existing_codex_app_server_manager"] = lambda session: None
        self.custom.list_turns.return_value = [{"id": "latest", "status": "inProgress"}]
        snapshot = await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.assertEqual(self.labels(snapshot), ["Codex subagent custom-child"])
        self.custom.list_turns.assert_awaited_once()

    async def test_batch_limit_is_shared_and_both_managers_make_progress(self):
        for owner in ("normal", "custom"):
            for index in range(2):
                self.child(owner, f"{owner}-{index}")
        first = await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.assertEqual(self.normal.list_turns.await_count + self.custom.list_turns.await_count, 2)
        self.assertEqual(self.labels(first), ["Codex subagent custom-0", "Codex subagent custom-1"])
        second = await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.assertEqual(self.normal.list_turns.await_count + self.custom.list_turns.await_count, 4)
        self.assertEqual(self.labels(second), [])

    async def test_generation_collision_does_not_transfer_live_ownership(self):
        self.child("custom", "custom-child")
        self.ns["CODEX_SUBAGENT_LIVE_MANAGERS"]["custom-child"] = self.normal
        state = self.ns["CODEX_SUBAGENT_STATE"]["custom-child"]
        self.assertFalse(self.ns["codex_subagent_has_live_owner"]("custom-child", state))
        await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.custom.list_turns.assert_not_awaited()
        self.normal.list_turns.assert_not_awaited()

    async def test_replaced_manager_cannot_reuse_old_terminal_proof(self):
        self.child("custom", "custom-child")
        snapshot = await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.custom = self.manager()
        self.custom.active_turn = lambda thread: SimpleNamespace(turn_id="new-turn")
        self.custom.list_turns.return_value = [{"id": "new-turn", "status": "inProgress"}]
        self.managers[1] = self.custom
        self.assertEqual(self.labels(snapshot), ["Codex subagent custom-child"])
        current = await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.assertEqual(self.labels(current), ["Codex subagent custom-child"])

    async def test_failure_in_custom_process_keeps_its_child_blocking(self):
        self.child("normal", "normal-child")
        self.child("custom", "custom-child")
        self.custom.list_turns.side_effect = RuntimeError("synthetic process unavailable")
        snapshot = await self.ns["prepare_codex_subagent_terminal_snapshot"]()
        self.assertEqual(snapshot["error"], "scan_failed")
        self.assertEqual(self.labels(snapshot), ["Codex subagent custom-child"])

    async def test_caches_never_share_an_identical_candidate_between_managers(self):
        candidate = ("same-child", "chat", "run", "running", 1, "", "event", 1, "timestamp", 0)
        self.ns["cache_codex_subagent_native_statuses"](self.normal, 1, (candidate,), [(candidate, "completed")])
        self.assertEqual(self.ns["cached_codex_subagent_native_statuses"](self.custom, 1, (candidate,)), {})
        self.assertEqual(self.ns["cached_codex_subagent_native_statuses"](self.normal, 1, (candidate,))[candidate][0], "completed")


if __name__ == "__main__":
    unittest.main()
