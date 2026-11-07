"""Goals arriving before SDK ownership use the ordinary durable admission queue."""
import asyncio
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server as server
from provider_commands import ProviderCommandInventory, ProviderCommandRecord


class ClaudeGoalQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="claude-goal-queue-")
        self.addCleanup(self.directory.cleanup)
        self.session = {"id": "goal-chat", "backend": "claude", "cwd": self.directory.name}
        # Admission is busy but has not acquired an SDK transport yet. Loaded
        # owners use native queue_goal instead, covered by goal route tests.
        self.owner = {"run_id": "original-run", "backend": "claude"}
        self.original_task = asyncio.create_task(asyncio.Event().wait())
        self.addAsyncCleanup(self.finish_original_task)
        self.old_goal = {"condition": "Existing native goal", "status": "active", "set_at": "earlier"}
        self.inventory = ProviderCommandInventory(backend="claude", revision="pcmdrev_" + "b" * 32, records=(
            ProviderCommandRecord(public={"id": "pcmd_" + "a" * 32, "invocation": "/goal"},
                                  native={"name": "goal", "control_generation": "native-generation"}),))
        self.events = AsyncMock(return_value={})
        values = {
            "ACTIVE": {"goal-chat": self.owner}, "ACTIVE_LOCK": asyncio.Lock(),
            "BUSY_SESSIONS": {"goal-chat"}, "CURRENT_TURNS": {"goal-chat": {"run_id": "original-run"}},
            "SESSION_TURN_TASKS": {"goal-chat": {self.original_task}},
            "QUEUED_TURNS": {}, "QUEUE_LOCK": asyncio.Lock(), "RUN_NOW_TURNS": {},
            "QUEUE_START_TASKS": {}, "STEERING_SESSIONS": set(), "CLAUDE_GOAL_PENDING": {},
            "CLAUDE_GOAL_PROJECTIONS": {}, "CLAUDE_PENDING_INTERACTIONS": {},
            "CLAUDE_PENDING_INTERACTIONS_LOCK": asyncio.Lock(), "CLAUDE_SDK_MANAGER": None,
            "CLAUDE_TRANSPORT": "agent-sdk", "claude_sdk_dependency_available": lambda: True,
            "wait_for_queue_recovery_admission": AsyncMock(),
            "reconcile_idle_queue_session": AsyncMock(),
            "refresh_claude_goal": AsyncMock(return_value=self.old_goal),
            "discover_session_provider_commands": AsyncMock(return_value=({"support": {"available": True}}, self.inventory)),
            "preview_session_runtime_update": lambda session, _patch: session,
            "managed_server_update_admission_blocker": lambda: None,
            "managed_server_update_blocker": lambda: None,
            "stop_cleanup_in_progress": lambda _sid: False,
            "append_durable_event": self.events,
            "append_event": AsyncMock(),
            "schedule_next_queued_turn": unittest.mock.Mock(),
        }
        for name, value in values.items():
            self.enterContext(patch.object(server, name, value))
        self.enterContext(patch.object(server.STORE, "sessions", {"goal-chat": self.session}))

    async def finish_original_task(self):
        self.original_task.cancel()
        await asyncio.gather(self.original_task, return_exceptions=True)

    async def queue_goal(self):
        return await server.put_claude_goal("goal-chat", server.ClaudeGoalRequest(condition="Finish the follow-up"))

    async def test_busy_admission_is_durable_cancellable_and_keeps_original_owner(self):
        result = await self.queue_goal()
        queued = result["queued_goal"]
        self.assertEqual(queued["condition"], "Finish the follow-up")
        self.assertEqual(result["goal"], self.old_goal)
        self.assertFalse(result["goal_starting"])
        self.assertIs(server.ACTIVE["goal-chat"], self.owner)
        self.assertFalse(self.original_task.done())
        row = server.QUEUED_TURNS["goal-chat"][0]
        self.assertTrue(row["_durable"])
        self.assertEqual(row["skill_selection"]["revision"], self.inventory.revision)
        self.assertEqual(row["client_capabilities"], [server.CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY])
        queued_event = next(call.args[2] for call in self.events.await_args_list if call.args[1] == "turn_queued")
        restored = server.queued_turn_from_event(queued_event, self.session, 1)
        self.assertEqual(restored["prompt"], "/goal Finish the follow-up")
        self.assertEqual(restored["skill_selection"], row["skill_selection"])
        self.assertEqual(restored["client_capabilities"], row["client_capabilities"])
        await server.unqueue_turn("goal-chat", queued["queued_id"])
        self.assertIsNone((await server.claude_runtime_snapshot("goal-chat"))["queued_goal"])
        self.assertIs(server.ACTIVE["goal-chat"], self.owner)
        self.assertFalse(self.original_task.done())

    async def test_drain_rebuilds_the_exact_selected_native_command(self):
        result = await self.queue_goal()
        server.BUSY_SESSIONS.clear()
        server.ACTIVE.clear()
        server.CURRENT_TURNS.clear()
        start = AsyncMock(return_value={"run_id": "goal-run"})
        with patch.object(server, "_start_turn_locked", start):
            await server._start_next_queued_turn_locked("goal-chat", admission_backend="claude")
        request = start.await_args.args[1]
        self.assertEqual(request.prompt, "/goal Finish the follow-up")
        self.assertEqual(request.skill_selection.id, self.inventory.records[0].public["id"])
        self.assertEqual(request.skill_selection.revision, self.inventory.revision)
        self.assertEqual(request.client_capabilities, [server.CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY])
        self.assertEqual(start.await_args.kwargs["queued_id"], result["queued_goal"]["queued_id"])
        self.assertFalse(start.await_args.kwargs["queue_if_busy"])
        self.assertIsNone(await server.queued_claude_goal("goal-chat"))

    async def test_unavailable_provider_command_never_creates_a_queued_goal(self):
        server.discover_session_provider_commands.return_value = ({"support": {"available": False}}, self.inventory)
        with self.assertRaises(HTTPException) as failure:
            await self.queue_goal()
        self.assertEqual(failure.exception.status_code, 409)
        self.assertEqual(server.QUEUED_TURNS, {})
        self.assertEqual(server.CLAUDE_GOAL_PENDING, {})
        self.assertIs(server.ACTIVE["goal-chat"], self.owner)
        self.events.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
