import asyncio
import tempfile
import unittest
from collections import deque
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

import agent_server


class GoalSubscription:
    def __init__(self) -> None:
        self.notifications: asyncio.Queue[dict] = asyncio.Queue()
        self.closed = False
        self.read_calls = 0

    async def next_notification(self, timeout: float | None = None) -> dict:
        self.read_calls += 1
        return await asyncio.wait_for(self.notifications.get(), timeout=timeout)

    def close(self) -> None:
        self.closed = True


class GoalResumeManager:
    """A provider fake that can publish a turn before goal/set returns."""

    def __init__(self, goal: dict) -> None:
        self.generation = 1
        self.goal = dict(goal)
        self.subscriptions: list[GoalSubscription] = []
        self.resume_observations: list[dict] = []
        self.emit_start = True
        self.goal_error: Exception | None = None
        self.goal_error_after_start: Exception | None = None
        self.finish_before_response: str | None = None
        self.invalid_goal_response = False
        self.complete_on_interrupt = False
        self.interrupt_observations: list[dict] = []
        self.start = AsyncMock()
        self.request = AsyncMock(side_effect=self._request)
        self.wait_for_notification_handler = AsyncMock()
        self.get_thread_goal = AsyncMock(side_effect=lambda _thread_id: dict(self.goal))
        self.set_thread_goal = AsyncMock(side_effect=self._set_goal)

    def is_thread_loaded(self, thread_id: str) -> bool:
        return thread_id == "thread-goal"

    def subscribe_thread(self, thread_id: str) -> GoalSubscription:
        if thread_id != "thread-goal":
            raise AssertionError(f"unexpected provider thread: {thread_id}")
        subscription = GoalSubscription()
        self.subscriptions.append(subscription)
        return subscription

    async def publish(self, notification: dict) -> None:
        # Match the real transport: subscriptions receive the notification
        # before the server's asynchronous durable-state projector runs.
        for subscription in self.subscriptions:
            if not subscription.closed:
                subscription.notifications.put_nowait(notification)
        await agent_server.project_codex_notification(notification)

    async def _request(self, method: str, params: dict, **_options: object) -> dict:
        if method != "turn/interrupt":
            raise AssertionError(f"unexpected provider request: {method}")
        self.interrupt_observations.append({
            "params": dict(params),
            "goal_status": self.goal["status"],
            "busy": "chat" in agent_server.BUSY_SESSIONS,
        })
        if self.complete_on_interrupt:
            await self.publish({
                "method": "turn/completed",
                "params": {
                    "threadId": params["threadId"],
                    "turn": {"id": params["turnId"], "status": "interrupted"},
                },
            })
        return {}

    async def _set_goal(self, thread_id: str, **values: object) -> object:
        if values.get("status") == "active":
            active = agent_server.ACTIVE.get("chat")
            self.resume_observations.append({
                "busy": "chat" in agent_server.BUSY_SESSIONS,
                "owner": dict(active) if active else None,
                "subscribed": any(not item.closed for item in self.subscriptions),
            })
        if self.goal_error is not None:
            raise self.goal_error
        self.goal.update({"threadId": thread_id, **values})
        response_goal = dict(self.goal)
        if values.get("status") == "active" and self.emit_start:
            await self.publish({
                "method": "turn/started",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": "turn-resumed", "status": "inProgress"},
                },
            })
            if self.goal_error_after_start is not None:
                raise self.goal_error_after_start
            if self.invalid_goal_response:
                return []
            if self.finish_before_response is not None:
                self.goal["status"] = self.finish_before_response
                await self.publish({
                    "method": "thread/goal/updated",
                    "params": {"threadId": thread_id, "goal": dict(self.goal)},
                })
                await self.publish({
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": "turn-resumed",
                        "item": {
                            "id": "fast-answer",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "Output before the goal response.",
                        },
                    },
                })
                await self.publish({
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {"id": "turn-resumed", "status": "completed"},
                    },
                })
                return response_goal
        return dict(self.goal)


class CodexGoalResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.cwd = self.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="codex-goal-resume-")
        )
        self.session = {
            "id": "chat",
            "backend": agent_server.BACKEND_CODEX,
            "cwd": self.cwd,
            "session_id": "thread-goal",
            "codex_thread_id": "thread-goal",
            "codex_goal": {
                "threadId": "thread-goal",
                "objective": "Finish the existing task",
                "status": "paused",
                "tokenBudget": None,
                "tokensUsed": 10,
                "timeUsedSeconds": 4,
                "createdAt": 1,
                "updatedAt": 2,
            },
            "codex_goal_time_budget_seconds": None,
            "codex_goal_time_budget_exhausted": False,
        }
        self.manager = GoalResumeManager(self.session["codex_goal"])
        self.store = SimpleNamespace(
            sessions={"chat": self.session},
            _lock=asyncio.Lock(),
            save=AsyncMock(),
            save_provider_session=AsyncMock(),
        )
        self.events: list[tuple[str, dict]] = []

        async def append_event(_session_id: str, kind: str, data: dict) -> dict:
            self.events.append((kind, dict(data)))
            return {"type": kind, "data": dict(data)}

        replacements = {
            "STORE": self.store,
            "ACTIVE": {},
            "ACTIVE_LOCK": asyncio.Lock(),
            "BUSY_SESSIONS": set(),
            "CURRENT_TURNS": {},
            "QUEUE_LOCK": asyncio.Lock(),
            "QUEUED_TURNS": {},
            "RUN_NOW_TURNS": {},
            "STEERING_SESSIONS": set(),
            "QUEUE_START_TASKS": {},
            "STOP_REQUESTS": set(),
            "STOPPED_RUNS": set(),
            "RUN_METADATA": {},
            "SERVER_MAINTENANCE_SESSIONS": set(),
            "SESSION_LIFECYCLE_LOCKS": {},
            "SESSION_TURN_TASKS": {},
            "DELETING_SESSIONS": set(),
            "DELETED_SESSION_TOMBSTONES": set(),
            "CODEX_NATIVE_ACTION_TASKS": {},
            "CODEX_CONTROL_TERMINAL_FENCES": {},
            "CODEX_THREAD_SESSION_INDEX": {"thread-goal": "chat"},
            "CODEX_SUBAGENT_SESSION_INDEX": {},
            "CODEX_GOAL_SYNC_GENERATIONS": {},
            "CODEX_QUARANTINED_GOAL_THREADS": {},
            "CODEX_INTERACTIVE_CONTROL_THREADS": set(),
            "CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS": {},
            "CODEX_APP_SERVER_MANAGER": self.manager,
            "CODEX_GOALS_ENABLED": True,
            "CODEX_GOALS_RECONFIGURING": False,
            "CODEX_TRANSPORT": agent_server.CODEX_TRANSPORT_APP_SERVER,
            "CODEX_GOAL_CONTROL_TIMEOUT_SECONDS": 1,
            "STOP_CONFIRM_TIMEOUT_SECONDS": 1,
            "codex_app_server_manager": AsyncMock(return_value=self.manager),
            "ensure_codex_app_server_thread": AsyncMock(
                return_value=("thread-goal", "instructions")
            ),
            "pin_codex_app_server_thread": AsyncMock(),
            "unpin_codex_app_server_thread": AsyncMock(),
            "touch_codex_app_server_thread": AsyncMock(),
            "quarantine_codex_goal_thread": AsyncMock(),
            "managed_server_update_blocker": Mock(return_value=None),
            "managed_server_update_admission_blocker": Mock(return_value=None),
            "wait_for_queue_recovery_admission": AsyncMock(),
            "turn_start_blocker": AsyncMock(return_value=None),
            "revoke_cross_chat_capability": AsyncMock(),
            "cancel_codex_interactions": AsyncMock(),
            "cancel_claude_interactions": AsyncMock(),
            "schedule_next_queued_turn": Mock(),
            "append_event": AsyncMock(side_effect=append_event),
            "record_codex_token_usage": AsyncMock(),
        }
        for name, value in replacements.items():
            self.stack.enter_context(patch.object(agent_server, name, value))
        self.stack.enter_context(
            patch.object(agent_server.HUB, "broadcast", AsyncMock())
        )

    async def asyncTearDown(self) -> None:
        tasks = list(agent_server.CODEX_NATIVE_ACTION_TASKS.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def resume(self, **values: object) -> dict:
        return await agent_server.put_codex_goal(
            "chat", agent_server.CodexGoalRequest(status="active", **values)
        )

    async def wait_for_subscription_reads(self, minimum: int) -> None:
        async def wait() -> None:
            while self.manager.subscriptions[0].read_calls < minimum:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait(), timeout=1)

    async def complete_goal(self, turn_id: str = "turn-resumed") -> None:
        self.manager.goal["status"] = "complete"
        await self.manager.publish({
            "method": "thread/goal/updated",
            "params": {"threadId": "thread-goal", "goal": dict(self.manager.goal)},
        })
        await self.manager.publish({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-goal",
                "turn": {"id": turn_id, "status": "completed"},
            },
        })

    async def test_idle_resume_owns_and_subscribes_before_native_goal_update(
        self,
    ) -> None:
        result = await self.resume()

        # Before the fix, the projector interrupts this immediate native turn
        # and recursively pauses the goal because goal/set has no local owner.
        self.manager.request.assert_not_awaited()
        self.assertEqual(result["goal"]["status"], "active")
        self.assertEqual(self.manager.set_thread_goal.await_count, 1)
        observed = self.manager.resume_observations[0]
        self.assertTrue(observed["busy"])
        self.assertTrue(observed["subscribed"])
        self.assertEqual(observed["owner"]["provider_thread_id"], "thread-goal")
        self.assertTrue(observed["owner"]["codex_native_operation"])
        self.assertTrue(observed["owner"]["codex_control_reservation_id"])
        self.assertIn("chat", agent_server.BUSY_SESSIONS)
        self.assertEqual(len(agent_server.CODEX_NATIVE_ACTION_TASKS), 1)

    async def test_duplicate_resume_keeps_existing_native_consumer_and_owner(
        self,
    ) -> None:
        await self.resume()
        owner = agent_server.ACTIVE["chat"]
        tasks = dict(agent_server.CODEX_NATIVE_ACTION_TASKS)
        self.manager.emit_start = False

        result = await self.resume()

        self.assertEqual(result["goal"]["status"], "active")
        self.assertIs(agent_server.ACTIVE["chat"], owner)
        self.assertEqual(agent_server.CODEX_NATIVE_ACTION_TASKS, tasks)
        self.assertEqual(len(self.manager.subscriptions), 1)
        self.assertIn("chat", agent_server.BUSY_SESSIONS)
        self.assertNotIn("chat", agent_server.SERVER_MAINTENANCE_SESSIONS)
        self.manager.request.assert_not_awaited()

    async def test_active_goal_mutation_keeps_ordinary_running_turn_owner(
        self,
    ) -> None:
        owner = {
            "run_id": "existing-run",
            "backend": agent_server.BACKEND_CODEX,
            "transport": agent_server.CODEX_TRANSPORT_APP_SERVER,
            "provider_thread_id": "thread-goal",
            "provider_session_id": "thread-goal",
            "provider_turn_id": "existing-turn",
            "provider_turn_ready": True,
        }
        current = {"run_id": "existing-run", "backend": agent_server.BACKEND_CODEX}
        agent_server.ACTIVE["chat"] = owner
        agent_server.CURRENT_TURNS["chat"] = current
        agent_server.BUSY_SESSIONS.add("chat")
        self.manager.emit_start = False

        await self.resume()

        self.assertIs(agent_server.ACTIVE["chat"], owner)
        self.assertIs(agent_server.CURRENT_TURNS["chat"], current)
        self.assertEqual(owner["run_id"], "existing-run")
        self.assertEqual(owner["provider_turn_id"], "existing-turn")
        self.assertEqual(agent_server.CODEX_NATIVE_ACTION_TASKS, {})
        self.assertEqual(self.manager.subscriptions, [])
        self.assertIn("chat", agent_server.BUSY_SESSIONS)
        self.manager.request.assert_not_awaited()

    async def test_exhausted_time_budget_rejects_before_native_activation(
        self,
    ) -> None:
        self.session["codex_goal_time_budget_seconds"] = 4
        for exhausted_flag in (False, True):
            with self.subTest(exhausted_flag=exhausted_flag):
                self.session["codex_goal_time_budget_exhausted"] = exhausted_flag
                with self.assertRaises(HTTPException) as raised:
                    await self.resume()

                self.assertEqual(raised.exception.status_code, 409)
                self.assertIn("time", str(raised.exception.detail).lower())
                self.manager.set_thread_goal.assert_not_awaited()
                self.manager.request.assert_not_awaited()
                self.assertNotIn("chat", agent_server.ACTIVE)
                self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
                self.assertNotIn("chat", agent_server.SERVER_MAINTENANCE_SESSIONS)

    async def test_failed_goal_rpc_releases_idle_reservation(self) -> None:
        self.manager.goal_error = RuntimeError("goal control unavailable")

        with self.assertRaises(HTTPException) as raised:
            await self.resume()

        self.assertGreaterEqual(raised.exception.status_code, 500)
        self.assertIn("goal control unavailable", str(raised.exception.detail))
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertNotIn("chat", agent_server.CURRENT_TURNS)
        self.assertNotIn("chat", agent_server.SERVER_MAINTENANCE_SESSIONS)
        self.assertEqual(agent_server.CODEX_NATIVE_ACTION_TASKS, {})
        self.assertTrue(all(item.closed for item in self.manager.subscriptions))
        self.assertEqual(agent_server.CODEX_INTERACTIVE_CONTROL_THREADS, set())

    async def test_resumed_output_and_terminal_release_native_ownership(
        self,
    ) -> None:
        await self.resume()
        task = next(iter(agent_server.CODEX_NATIVE_ACTION_TASKS.values()))
        await self.manager.publish({
            "method": "item/completed",
            "params": {
                "threadId": "thread-goal",
                "turnId": "turn-resumed",
                "item": {
                    "id": "answer",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Completed the existing task.",
                },
            },
        })
        await self.complete_goal()
        await asyncio.wait_for(asyncio.shield(task), timeout=2)

        assistant_events = [data for kind, data in self.events if kind == "assistant_text"]
        self.assertEqual([item["text"] for item in assistant_events], [
            "Completed the existing task."
        ])
        self.assertEqual(assistant_events[0]["purpose"], "codex_goal_resume")
        self.assertEqual(len([kind for kind, _ in self.events if kind == "turn_finished"]), 1)
        self.assertFalse(any(kind == "user_message" for kind, _ in self.events))
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertNotIn("chat", agent_server.CURRENT_TURNS)
        self.assertNotIn("chat", agent_server.SERVER_MAINTENANCE_SESSIONS)
        self.assertTrue(self.manager.subscriptions[0].closed)
        agent_server.schedule_next_queued_turn.assert_called_once_with("chat")

    async def test_native_goal_retains_owner_across_completed_to_next_turn_gap(
        self,
    ) -> None:
        await self.resume()
        owner = agent_server.ACTIVE["chat"]
        task = next(iter(agent_server.CODEX_NATIVE_ACTION_TASKS.values()))
        await self.manager.publish({
            "method": "turn/completed",
            "params": {
                "threadId": "thread-goal",
                "turn": {"id": "turn-resumed", "status": "completed"},
            },
        })
        # The consumer has drained start and completion and now awaits a third
        # notification. Exercise a real gap before the native follow-up turn.
        await self.wait_for_subscription_reads(3)
        self.assertFalse(task.done())
        self.assertIs(agent_server.ACTIVE["chat"], owner)
        self.assertIn("chat", agent_server.BUSY_SESSIONS)
        self.assertFalse(self.manager.subscriptions[0].closed)
        self.assertFalse(any(kind == "turn_finished" for kind, _ in self.events))

        await self.manager.publish({
            "method": "turn/started",
            "params": {
                "threadId": "thread-goal",
                "turn": {"id": "turn-follow-up", "status": "inProgress"},
            },
        })
        await self.wait_for_subscription_reads(4)
        self.manager.request.assert_not_awaited()
        self.assertEqual(owner["provider_turn_id"], "turn-follow-up")
        self.assertEqual(len(agent_server.CODEX_NATIVE_ACTION_TASKS), 1)

        await self.complete_goal("turn-follow-up")
        await asyncio.wait_for(asyncio.shield(task), timeout=2)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertEqual(len([kind for kind, _ in self.events if kind == "turn_finished"]), 1)

    async def test_archived_chat_cannot_activate_native_goal(self) -> None:
        self.session["archived"] = True

        with self.assertRaises(HTTPException) as raised:
            await self.resume()

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("archived", str(raised.exception.detail))
        self.manager.set_thread_goal.assert_not_awaited()
        agent_server.codex_app_server_manager.assert_not_awaited()
        self.assertEqual(agent_server.ACTIVE, {})
        self.assertEqual(agent_server.BUSY_SESSIONS, set())

    async def test_prior_queued_work_cannot_be_overtaken_by_goal_resume(self) -> None:
        gates = [
            ("QUEUED_TURNS", {"chat": deque([{"run_id": "queued"}])}),
            ("RUN_NOW_TURNS", {"chat": {"run_id": "run-now"}}),
            ("STEERING_SESSIONS", {"chat"}),
            ("QUEUE_START_TASKS", {"chat": Mock(done=Mock(return_value=False))}),
        ]
        for name, pending in gates:
            with self.subTest(gate=name), patch.object(agent_server, name, pending):
                with self.assertRaises(HTTPException) as raised:
                    await self.resume()

                self.assertEqual(raised.exception.status_code, 409)
                self.assertIn("queued", str(raised.exception.detail))
                self.manager.set_thread_goal.assert_not_awaited()
                self.assertEqual(agent_server.ACTIVE, {})
                self.assertEqual(agent_server.BUSY_SESSIONS, set())
                self.assertEqual(agent_server.CURRENT_TURNS, {})
                self.assertEqual(agent_server.SERVER_MAINTENANCE_SESSIONS, set())

    async def test_launch_blocker_releases_reservation_before_native_activation(
        self,
    ) -> None:
        agent_server.turn_start_blocker.return_value = "provider host is unavailable"

        with self.assertRaises(HTTPException) as raised:
            await self.resume()

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("provider host is unavailable", str(raised.exception.detail))
        agent_server.turn_start_blocker.assert_awaited_once_with(ignore_session_id="chat")
        self.manager.set_thread_goal.assert_not_awaited()
        self.assertEqual(agent_server.ACTIVE, {})
        self.assertEqual(agent_server.BUSY_SESSIONS, set())
        self.assertEqual(agent_server.CURRENT_TURNS, {})
        self.assertEqual(agent_server.SERVER_MAINTENANCE_SESSIONS, set())

    async def test_stop_pauses_goal_before_interrupting_resumed_native_turn(
        self,
    ) -> None:
        self.manager.complete_on_interrupt = True
        await self.resume()
        consumer = next(iter(agent_server.CODEX_NATIVE_ACTION_TASKS.values()))
        await self.wait_for_subscription_reads(2)

        result = await asyncio.wait_for(agent_server.stop_turn(
            "chat",
            cascade_codex_subagents=False,
            cascade_claude_subagents=False,
            pause_queued_turns_on_stop=False,
            hard_terminalize_on_timeout=False,
        ), timeout=2)
        await asyncio.wait_for(asyncio.shield(consumer), timeout=2)

        self.assertTrue(result["stopped"])
        self.assertFalse(result["pending"])
        self.assertTrue(result["goal_paused"])
        self.assertEqual(self.session["codex_goal"]["status"], "paused")
        self.assertEqual(len(self.manager.interrupt_observations), 1)
        self.manager.request.assert_awaited_once()
        agent_server.quarantine_codex_goal_thread.assert_not_awaited()
        self.assertTrue(all(
            item["goal_status"] == "paused"
            for item in self.manager.interrupt_observations
        ))
        self.assertEqual(self.manager.interrupt_observations[0]["params"], {
            "threadId": "thread-goal", "turnId": "turn-resumed",
        })
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertTrue(self.manager.subscriptions[0].closed)

    async def test_goal_rpc_failure_after_native_start_pauses_and_interrupts(
        self,
    ) -> None:
        self.manager.goal_error_after_start = RuntimeError("goal acknowledgement lost")
        self.manager.complete_on_interrupt = True

        with self.assertRaises(HTTPException) as raised:
            await self.resume()

        self.assertIn("goal acknowledgement lost", str(raised.exception.detail))
        self.assertTrue(self.manager.resume_observations[0]["busy"])
        self.assertTrue(self.manager.resume_observations[0]["subscribed"])
        self.assertEqual(self.session["codex_goal"]["status"], "paused")
        self.assertEqual(self.manager.interrupt_observations[0], {
            "params": {"threadId": "thread-goal", "turnId": "turn-resumed"},
            "goal_status": "paused",
            "busy": True,
        })
        self.assertEqual(agent_server.ACTIVE, {})
        self.assertEqual(agent_server.BUSY_SESSIONS, set())
        self.assertEqual(agent_server.CURRENT_TURNS, {})
        self.assertEqual(agent_server.CODEX_NATIVE_ACTION_TASKS, {})
        self.assertTrue(self.manager.subscriptions[0].closed)

    async def test_saved_time_limit_stops_native_turn_without_usage_updates(
        self,
    ) -> None:
        self.session["codex_goal_time_budget_seconds"] = 1
        self.session["codex_goal"]["timeUsedSeconds"] = 0
        self.manager.goal["timeUsedSeconds"] = 0
        self.manager.complete_on_interrupt = True

        await self.resume()
        consumer = next(iter(agent_server.CODEX_NATIVE_ACTION_TASKS.values()))
        await asyncio.wait_for(asyncio.shield(consumer), timeout=3)

        self.assertTrue(self.session["codex_goal_time_budget_exhausted"])
        self.assertEqual(self.session["codex_goal"]["status"], "budgetLimited")
        self.assertTrue(any(
            kind == "codex_goal_budget_limited" for kind, _ in self.events
        ))
        self.assertEqual(len(self.manager.interrupt_observations), 1)
        self.manager.request.assert_awaited_once()
        agent_server.quarantine_codex_goal_thread.assert_not_awaited()
        self.assertEqual(self.manager.interrupt_observations[0]["goal_status"], "budgetLimited")
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertTrue(self.manager.subscriptions[0].closed)

    async def verify_native_terminal_before_stale_goal_response(self, status: str) -> None:
        self.manager.finish_before_response = status

        result = await self.resume()
        self.assertEqual(result["goal"]["status"], status)
        self.assertEqual(self.session["codex_goal"]["status"], status)
        consumer = next(iter(agent_server.CODEX_NATIVE_ACTION_TASKS.values()))
        await asyncio.wait_for(asyncio.shield(consumer), timeout=2)

        self.assertEqual([
            data["text"] for kind, data in self.events if kind == "assistant_text"
        ], ["Output before the goal response."])
        self.assertEqual(len([kind for kind, _ in self.events if kind == "turn_finished"]), 1)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertTrue(self.manager.subscriptions[0].closed)
        self.manager.request.assert_not_awaited()

    async def test_fast_native_goal_completion_wins_over_stale_active_rpc_response(
        self,
    ) -> None:
        await self.verify_native_terminal_before_stale_goal_response("complete")

    async def test_fast_native_goal_pause_wins_over_stale_active_rpc_response(
        self,
    ) -> None:
        await self.verify_native_terminal_before_stale_goal_response("paused")

    async def test_malformed_goal_response_after_native_start_stops_owned_work(
        self,
    ) -> None:
        self.manager.invalid_goal_response = True
        self.manager.complete_on_interrupt = True

        with self.assertRaises(HTTPException):
            await self.resume()

        self.assertEqual(self.session["codex_goal"]["status"], "paused")
        self.assertTrue(self.manager.interrupt_observations)
        self.assertEqual(self.manager.interrupt_observations[0], {
            "params": {"threadId": "thread-goal", "turnId": "turn-resumed"},
            "goal_status": "paused",
            "busy": True,
        })
        self.assertEqual(agent_server.ACTIVE, {})
        self.assertEqual(agent_server.BUSY_SESSIONS, set())
        self.assertEqual(agent_server.CURRENT_TURNS, {})
        self.assertEqual(agent_server.CODEX_NATIVE_ACTION_TASKS, {})
        self.assertTrue(self.manager.subscriptions[0].closed)

    async def test_consumer_cancelled_before_first_step_cleans_native_reservation(
        self,
    ) -> None:
        self.manager.complete_on_interrupt = True
        register = agent_server.register_codex_native_action
        consumers: list[asyncio.Task] = []

        def register_then_cancel(session_id: str, operation_id: str, task: asyncio.Task) -> None:
            register(session_id, operation_id, task)
            consumers.append(task)
            task.cancel()

        with patch.object(
            agent_server, "register_codex_native_action", side_effect=register_then_cancel,
        ):
            await self.resume()
        await asyncio.gather(*consumers, return_exceptions=True)

        async def wait_for_cleanup() -> None:
            while "chat" in agent_server.BUSY_SESSIONS:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_for_cleanup(), timeout=1)
        self.assertTrue(consumers)
        self.assertTrue(consumers[0].cancelled())
        self.assertEqual(self.session["codex_goal"]["status"], "paused")
        self.assertTrue(self.manager.interrupt_observations)
        self.assertEqual(self.manager.interrupt_observations[0]["goal_status"], "paused")
        self.assertEqual(agent_server.ACTIVE, {})
        self.assertEqual(agent_server.CURRENT_TURNS, {})
        self.assertTrue(self.manager.subscriptions[0].closed)

    async def test_pause_persistence_failure_cannot_strand_cancelled_consumer(
        self,
    ) -> None:
        self.manager.complete_on_interrupt = True
        await self.resume()
        consumer = next(iter(agent_server.CODEX_NATIVE_ACTION_TASKS.values()))
        await self.wait_for_subscription_reads(2)

        async def fail_paused_save() -> None:
            if self.session["codex_goal"]["status"] == "paused":
                raise OSError("mocked pause persistence failure")

        self.store.save.side_effect = fail_paused_save
        consumer.cancel()
        await asyncio.wait_for(
            asyncio.gather(consumer, return_exceptions=True), timeout=2,
        )

        self.assertEqual(self.manager.goal["status"], "paused")
        self.assertTrue(self.manager.interrupt_observations)
        self.assertEqual(self.manager.interrupt_observations[0]["goal_status"], "paused")
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertNotIn("chat", agent_server.CURRENT_TURNS)
        self.assertTrue(self.manager.subscriptions[0].closed)
