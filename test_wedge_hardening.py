"""Regression tests for the 2026-09-04 wedge: fenced queues, hung Stop, retry
storms, health-poll reconcile spam, and update-when-idle lockout."""

import asyncio
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

import agent_server
from test_server_restart_endpoints import (
    http_request,
    restart_body,
    restart_environment,
)


def utc(offset_seconds: float = 0.0) -> str:
    moment = datetime.now(timezone.utc) - timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class QueueFenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reconcile_skips_and_names_explicit_stop_fence(self):
        never = asyncio.get_running_loop().create_future()
        stop_operation = asyncio.ensure_future(never)
        try:
            with patch.object(
                agent_server,
                "EXPLICIT_STOP_OPERATIONS",
                {"chat": stop_operation},
            ), patch.object(
                agent_server,
                "QUEUED_TURNS",
                {"chat": agent_server.deque([{"queued_id": "q1", "_durable": True}])},
            ), patch.object(agent_server, "QUEUE_FENCE_LOGGED_AT", {}), \
                 patch.object(
                     agent_server,
                     "schedule_next_queued_turn",
                 ) as schedule, \
                 self.assertLogs(agent_server.logger, level="WARNING") as logs:
                repaired = await agent_server.reconcile_idle_queue_session(
                    "chat",
                    schedule=True,
                    reason="health_poll",
                )
                # A second reconcile within the log interval stays quiet.
                await agent_server.reconcile_idle_queue_session(
                    "chat",
                    schedule=True,
                    reason="health_poll",
                )
        finally:
            never.cancel()
        self.assertFalse(repaired)
        schedule.assert_not_called()
        fence_lines = [
            line for line in logs.output if "queue promotion fenced" in line
        ]
        self.assertEqual(len(fence_lines), 1)
        self.assertIn("fence=explicit_stop", fence_lines[0])
        self.assertIn("session=chat", fence_lines[0])

    async def test_reconcile_names_server_restart_fence(self):
        with patch.object(
            agent_server,
            "managed_server_update_blocker",
            return_value=agent_server.MANAGED_SERVER_RESTART_ACTIVE_DETAIL,
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}):
            self.assertEqual(
                agent_server.queue_promotion_fence("chat"),
                "server_restart",
            )
        with patch.object(
            agent_server,
            "managed_server_update_blocker",
            return_value=agent_server.MANAGED_SERVER_UPDATE_ACTIVE_DETAIL,
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}):
            self.assertEqual(
                agent_server.queue_promotion_fence("chat"),
                "server_update",
            )
        with patch.object(
            agent_server,
            "managed_server_update_blocker",
            return_value=None,
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}):
            self.assertIsNone(agent_server.queue_promotion_fence("chat"))

    async def test_routine_idle_reconcile_logs_at_debug_only(self):
        with patch.object(
            agent_server,
            "QUEUED_TURNS",
            {"chat": agent_server.deque([{"queued_id": "q1", "_durable": True}])},
        ), patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}), \
             patch.object(agent_server, "RUN_NOW_REQUESTS", {}), \
             patch.object(agent_server, "QUEUE_START_TASKS", {}), \
             patch.object(agent_server, "STEERING_WAIT_TASKS", {}), \
             patch.object(agent_server, "STEERING_SESSIONS", set()), \
             patch.object(agent_server, "RUN_NOW_TURNS", {}), \
             patch.object(agent_server, "ACTIVE", {}), \
             patch.object(agent_server, "BUSY_SESSIONS", set()), \
             patch.object(agent_server, "CURRENT_TURNS", {}), \
             patch.object(
                 agent_server,
                 "managed_server_update_blocker",
                 return_value=None,
             ), \
             patch.object(agent_server, "schedule_next_queued_turn") as schedule, \
             self.assertNoLogs(agent_server.logger, level="INFO"):
            repaired = await agent_server.reconcile_idle_queue_session(
                "chat",
                schedule=True,
                reason="health_poll",
            )
        self.assertTrue(repaired)
        schedule.assert_called_once_with("chat")


class RetryTimerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_retry_timer_is_armed_per_chat(self):
        with patch.object(agent_server, "QUEUE_RETRY_TASKS", {}) as timers, \
             patch.object(
                 agent_server,
                 "start_next_queued_turn",
                 new=AsyncMock(),
             ) as start:
            self.assertTrue(agent_server.schedule_queued_turn_retry("chat", 5))
            self.assertFalse(agent_server.schedule_queued_turn_retry("chat", 5))
            self.assertFalse(agent_server.schedule_queued_turn_retry("chat", 5))
            self.assertEqual(len(timers), 1)
            timers["chat"].cancel()
            await asyncio.gather(timers["chat"], return_exceptions=True)
            await asyncio.sleep(0)
            self.assertNotIn("chat", timers)
            start.assert_not_called()
            # Once the previous timer settled a new one may be armed.
            self.assertTrue(agent_server.schedule_queued_turn_retry("chat", 5))
            timers["chat"].cancel()
            await asyncio.gather(timers["chat"], return_exceptions=True)


class HealthReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_poll_reconcile_is_rate_limited(self):
        with patch.object(
            agent_server,
            "HEALTH_QUEUE_RECONCILE_STATE",
            {"last_at": float("-inf")},
        ) as state, patch.object(
            agent_server,
            "reconcile_idle_queued_turns",
            new=AsyncMock(return_value=0),
        ) as reconcile, patch.object(
            agent_server,
            "HEALTH_QUEUE_RECONCILE_INTERVAL_SECONDS",
            3600.0,
        ):
            self.assertTrue(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            self.assertFalse(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            self.assertFalse(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            reconcile.assert_awaited_once_with(reason="health_poll")
            state["last_at"] = float("-inf")
            self.assertTrue(
                await agent_server.reconcile_idle_queued_turns_from_health_poll()
            )
            self.assertEqual(reconcile.await_count, 2)


class ExplicitStopDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_hung_stop_releases_fence_and_reports_timeout(self):
        release = asyncio.Event()

        async def hung_stop(session_id, *, _admission_ready=None):
            if _admission_ready is not None:
                _admission_ready.set()
            await release.wait()
            return {"ok": True, "stopped": True, "late": True}

        events: list[tuple[str, dict]] = []

        async def record_event(session_id, event_type, payload):
            events.append((event_type, payload))

        with patch.object(agent_server, "stop_turn", new=hung_stop), \
             patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}) as fences, \
             patch.object(agent_server, "EXPLICIT_STOP_OPERATION_TIMEOUT_SECONDS", 1.0), \
             patch.object(agent_server, "DETACHED_STOP_TASKS", set()) as detached, \
             patch.object(agent_server, "SESSION_LIFECYCLE_LOCKS", {}), \
             patch.object(agent_server, "append_event", new=record_event), \
             patch.object(agent_server, "schedule_next_queued_turn") as schedule, \
             patch.object(
                 agent_server,
                 "managed_server_update_blocker",
                 return_value=None,
             ), \
             self.assertLogs(agent_server.logger, level="ERROR") as logs:
            result = await asyncio.wait_for(
                agent_server.stop_turn_endpoint("chat"),
                timeout=5,
            )
            self.assertFalse(agent_server.explicit_stop_in_progress("chat"))
            self.assertNotIn("chat", fences)
            # Teardown keeps running detached until the provider settles.
            self.assertEqual(len(detached), 1)
            release.set()
            await asyncio.gather(*detached, return_exceptions=True)
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["stopped"])
        self.assertTrue(result["pending"])
        self.assertEqual([kind for kind, _ in events], ["error"])
        self.assertTrue(events[0][1]["stop_timeout"])
        schedule.assert_called_with("chat")
        self.assertTrue(any("did not finish" in line for line in logs.output))

    async def test_prompt_stop_returns_its_result_and_clears_fence(self):
        async def quick_stop(session_id, *, _admission_ready=None):
            if _admission_ready is not None:
                _admission_ready.set()
            return {"ok": True, "stopped": True}

        with patch.object(agent_server, "stop_turn", new=quick_stop), \
             patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}) as fences, \
             patch.object(agent_server, "SESSION_LIFECYCLE_LOCKS", {}), \
             patch.object(agent_server, "schedule_next_queued_turn"), \
             patch.object(
                 agent_server,
                 "managed_server_update_blocker",
                 return_value=None,
             ):
            result = await agent_server.stop_turn_endpoint("chat")
        self.assertEqual(result, {"ok": True, "stopped": True})
        self.assertEqual(fences, {})


class PendingUpdateGraceTests(unittest.TestCase):
    def pending_status(self, root: Path, *, age_seconds: float) -> None:
        agent_server.write_fresh_server_update_status(
            phase="pending",
            schedule_id="e" * 32,
            target_version="1.1.0",
            track="stable",
            when_idle=True,
            pending_at=utc(age_seconds),
        )

    def test_recent_reservation_still_parks_interactive_turns(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                agent_server,
                "SERVER_UPDATE_STATUS_FILE",
                root / "status.json",
            ), patch.object(
                agent_server,
                "PENDING_UPDATE_BYPASS_LOGGED_AT",
                {"at": float("-inf")},
            ):
                self.pending_status(root, age_seconds=5)
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(prompt="hi"),
                    )
                )

    def test_stale_reservation_admits_interactive_turns_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                agent_server,
                "SERVER_UPDATE_STATUS_FILE",
                root / "status.json",
            ), patch.object(
                agent_server,
                "PENDING_UPDATE_BYPASS_LOGGED_AT",
                {"at": float("-inf")},
            ), patch.object(
                agent_server,
                "SERVER_UPDATE_PENDING_INTERACTIVE_FENCE_SECONDS",
                120.0,
            ):
                self.pending_status(root, age_seconds=600)
                with self.assertLogs(agent_server.logger, level="WARNING") as logs:
                    self.assertTrue(
                        agent_server.interactive_turn_may_bypass_pending_update(
                            agent_server.TurnRequest(prompt="hi"),
                        )
                    )
                self.assertTrue(
                    any("parked chats" in line for line in logs.output)
                )
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(
                            prompt="job",
                            purpose="scheduled_job",
                        ),
                    )
                )
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(
                            prompt="delivery",
                            cross_chat_envelope_id="env-1",
                        ),
                    )
                )

    def test_no_reservation_means_no_bypass(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(
                agent_server,
                "SERVER_UPDATE_STATUS_FILE",
                root / "status.json",
            ):
                self.assertIsNone(agent_server.pending_server_update_age_seconds())
                self.assertFalse(
                    agent_server.interactive_turn_may_bypass_pending_update(
                        agent_server.TurnRequest(prompt="hi"),
                    )
                )


class RestartDenialLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_denied_restart_logs_code_and_blockers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), \
                 patch.object(agent_server, "BUSY_SESSIONS", {"chat"}), \
                 self.assertLogs(agent_server.logger, level="WARNING") as logs:
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.restart_server_endpoint(
                        restart_body(),
                        http_request(method="POST"),
                        BackgroundTasks(),
                    )
        self.assertEqual(raised.exception.detail["code"], "server_restart_busy")
        denial = [line for line in logs.output if "server restart denied" in line]
        self.assertEqual(len(denial), 1)
        self.assertIn("code=server_restart_busy", denial[0])
        self.assertIn("forced=False", denial[0])
        self.assertIn('"active_count": 1', denial[0])

    async def test_snapshot_advertises_force_availability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with restart_environment(root), \
                 patch.object(agent_server, "SERVER_MAINTENANCE_SESSIONS", {"chat"}):
                snapshot = agent_server.server_restart_blocker_snapshot_locked()
        self.assertTrue(snapshot["has_safety_blockers"])
        self.assertTrue(snapshot["force_restart_available"])


if __name__ == "__main__":
    unittest.main()
