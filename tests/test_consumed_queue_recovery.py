import asyncio
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class ConsumedGoalQueueRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.scope = ExitStack()
        self.addCleanup(self.scope.close)
        self.root = Path(self.scope.enter_context(tempfile.TemporaryDirectory()))
        self.path = self.root / "events.jsonl"
        self.session_id = "queue-recovery-chat"
        self.queued_id = "queued-accepted-followup"
        self.launch = AsyncMock(return_value={"run_id": "new-run", "queued": False})
        self.tasks = []
        self.scope.enter_context(patch.object(agent_server.STORE, "sessions", {
            self.session_id: {"id": self.session_id, "backend": "codex"},
        }))
        self.scope.enter_context(patch.multiple(
            agent_server,
            QUEUED_TURNS={}, RUN_NOW_TURNS={}, ACTIVE={}, CURRENT_TURNS={},
            BUSY_SESSIONS=set(), STEERING_SESSIONS=set(), STEERING_WAIT_TASKS={},
            RUN_NOW_REQUESTS={}, QUEUE_START_TASKS={}, SESSION_TURN_TASKS={},
            SESSION_LIFECYCLE_LOCKS={}, QUEUE_RECOVERY_TASK=None,
            RUN_NOW_IDLE_OWNER_TIMEOUT_SECONDS=1,
            ACTIVE_LOCK=asyncio.Lock(), QUEUE_LOCK=asyncio.Lock(),
            RUN_NOW_REQUEST_LOCK=asyncio.Lock(),
            events_path=lambda _session_id: self.path,
            managed_server_update_blocker=lambda: None,
            _start_turn_locked=self.launch,
            maybe_start_chat_mailbox_locked=AsyncMock(),
            schedule_next_queued_turn=self.schedule,
        ))
        self.scope.enter_context(patch.object(
            agent_server.CROSS_CHAT, "pending_mailbox_migration_candidates",
            AsyncMock(return_value=[]),
        ))

    def schedule(self, session_id: str) -> None:
        # Run the actual promotion and reconciliation code, retaining task
        # handles so assertions cannot race detached queue scheduling.
        self.tasks.append(asyncio.create_task(agent_server.start_next_queued_turn(session_id)))

    async def settle(self) -> None:
        while self.tasks:
            tasks, self.tasks = self.tasks, []
            await asyncio.gather(*tasks)

    async def asyncTearDown(self) -> None:
        await self.settle()

    def incident_events(self) -> list[dict]:
        queued = {"queued_id": self.queued_id, "prompt": "Already delivered follow-up",
                  "request_prompt": "Already delivered follow-up", "file_ids": [], "backend": "codex"}
        return [
            {**queued, "type": "turn_queued"},
            {"type": "turn_unqueued", "queued_id": self.queued_id, "reason": "native_delivery_fence"},
            {**queued, "type": "turn_queue_delivery_fenced"},
            {**queued, "type": "turn_queue_run_now", "run_id": "original-goal-run",
             "native_steer": True, "native_goal_steer": True, "superseded_queued_ids": []},
            {"type": "turn_steered", "queued_id": self.queued_id,
             "run_id": "original-goal-run", "provider_turn_id": "original-provider-turn",
             "backend": "codex", "purpose": "codex_goal_resume", "prompt": queued["prompt"],
             "native_steer": True, "native_goal_steer": True, "provider_user_authored": True},
        ]

    def write_events(self, events: list[dict]) -> None:
        self.path.write_text("".join(json.dumps({
            "id": f"event-{seq}", "seq": seq, "session_id": self.session_id,
            "ts": "2026-09-21T23:57:15Z", **event,
        }) + "\n" for seq, event in enumerate(events, 1)))

    async def test_startup_recovery_does_not_redeliver_acknowledged_goal_followup(self) -> None:
        self.write_events(self.incident_events())
        recovery = await agent_server.recover_queued_turns_after_start()
        await self.settle()
        await agent_server.reconcile_idle_queue_session(self.session_id, schedule=True, reason="session_snapshot")
        await self.settle()
        self.launch.assert_not_awaited()
        self.assertEqual(recovery, (0, 0))
        self.assertEqual(await agent_server.queued_turns_snapshot(self.session_id), [])

    async def test_stale_force_send_owner_cannot_restore_consumed_goal_followup(self) -> None:
        self.write_events(self.incident_events())

        async def abandoned_owner():
            await asyncio.Event().wait()

        owner = asyncio.create_task(abandoned_owner())
        owner._agentsdock_force_send_started_at = time.monotonic() - 1000
        agent_server.RUN_NOW_REQUESTS[self.session_id] = (self.queued_id, owner)
        agent_server.STEERING_SESSIONS.add(self.session_id)
        try:
            repaired = await agent_server.reconcile_idle_queue_session(
                self.session_id, schedule=True, reason="health_poll",
            )
            await self.settle()
            self.launch.assert_not_awaited()
            self.assertTrue(repaired)
            self.assertTrue(owner.cancelled())
            self.assertFalse(await agent_server.restore_missing_durable_force_send_row(self.session_id, self.queued_id))
            self.assertEqual(await agent_server.queued_turns_snapshot(self.session_id), [])
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)

    async def test_startup_preserves_and_runs_ordinary_successor_once(self) -> None:
        self.write_events([*self.incident_events(), {
            "type": "turn_queued", "queued_id": "queued-new-work",
            "prompt": "Genuinely waiting work", "backend": "codex", "file_ids": [],
        }])
        recovery = await agent_server.recover_queued_turns_after_start()
        await self.settle()
        self.launch.assert_awaited_once()
        self.assertEqual(self.launch.await_args.kwargs["queued_id"], "queued-new-work")
        self.assertEqual(self.launch.await_args.args[1].prompt, "Genuinely waiting work")
        self.assertEqual(recovery, (1, 1))

    async def test_missing_or_non_authoritative_acknowledgement_keeps_delivery_paused(self) -> None:
        # A delivery fence without an acknowledgement, including a truncated
        # native-goal commit batch, must remain visible but never runnable.
        cases = [self.incident_events()[:3], self.incident_events()[:4]]
        for field, value in [
            ("native_steer", False), ("native_goal_steer", False),
            ("provider_user_authored", False), ("backend", "claude"),
            ("purpose", None), ("run_id", ""), ("queued_id", "different-row"),
        ]:
            events = self.incident_events()
            events[-1] = {**events[-1], field: value}
            cases.append(events)
        for index, events in enumerate(cases):
            with self.subTest(case=index):
                agent_server.QUEUED_TURNS.clear()
                self.launch.reset_mock()
                self.write_events(events)
                recovery = await agent_server.recover_queued_turns_after_start()
                await self.settle()
                await agent_server.start_next_queued_turn(self.session_id)
                self.launch.assert_not_awaited()
                self.assertEqual(recovery, (1, 0))
                queued = await agent_server.queued_turns_snapshot(self.session_id)
                self.assertEqual(len(queued), 1)
                self.assertEqual(queued[0]["queued_id"], self.queued_id)
                self.assertTrue(queued[0]["paused"])
                self.assertEqual(queued[0]["pause_reason"], "delivery_uncertain")

    async def test_ordinary_steer_event_does_not_consume_a_waiting_queue_row(self) -> None:
        events = self.incident_events()
        ordinary_steer = {**events[-1], "purpose": None, "native_goal_steer": False}
        self.write_events([events[0], ordinary_steer])
        self.assertEqual(await agent_server.recover_queued_turns_after_start(), (1, 1))
        await self.settle()
        self.launch.assert_awaited_once()
        self.assertEqual(self.launch.await_args.kwargs["queued_id"], self.queued_id)


if __name__ == "__main__":
    unittest.main()
