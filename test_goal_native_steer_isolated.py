"""Native goal steering AST and in-memory protocol fakes; no server startup."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from contextlib import suppress
import json
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from codex_app_server import CodexAppServerError, CodexAppServerProtocolError, CodexAppServerRequestError


SOURCE = Path(__file__).with_name("agent_server.py")
NAMES = {
    "NativeSteerHandoffError", "send_codex_goal_steer", "commit_codex_goal_steer",
    "consume_codex_native_turn", "codex_goal_steer_selection_is_plain",
    "mark_native_steer_accepted", "fence_native_steer_delivery",
    "requeue_native_steer_after_safe_rejection", "native_steer_requeue_event_payload",
    "join_task_despite_caller_cancellation", "concise_error_message",
    "is_codex_reconnect_notice", "is_codex_app_server_retry_notice",
    "codex_reasoning_text", "codex_app_server_reasoning_summary",
}
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
NODES = [node for node in TREE.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
assert {node.name for node in NODES} == NAMES
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
    module="__future__", names=[ast.alias(name="annotations")], level=0,
), *NODES], type_ignores=[])), str(SOURCE), "exec")


class Subscription:
    def __init__(self):
        self.queue = asyncio.Queue()
        self._last_enqueued_sequence = 0
        self._closed = False

    def push(self, method, **params):
        self._last_enqueued_sequence += 1
        self.queue.put_nowait((self._last_enqueued_sequence, {"method": method, "params": {"turnId": "turn-1", **params}}))

    async def next_notification_with_sequence(self, timeout=None):
        return await asyncio.wait_for(self.queue.get(), timeout=min(timeout or 1, .02))

    def close(self):
        self._closed = True


class NativeGoalSteerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.queue = asyncio.Queue(maxsize=1)
        self.goal = {"id": "goal", "objective": "Complete the work", "status": "active"}
        self.active = {
            "run_id": "operation", "codex_control_reservation_id": "reservation",
            "codex_native_operation_kind": "goal_resume", "provider_thread_id": "thread",
            "provider_turn_id": "turn-1", "provider_turn_ready": True,
            "native_steer_queue": self.queue,
        }
        self.current = {"run_id": "operation", "codex_control_reservation_id": "reservation", "purpose": "codex_goal_resume"}
        self.events = []
        self.calls = []
        self.after_seen = asyncio.Event()

        async def append_event(chat, kind, payload):
            self.events.append((kind, dict(payload)))
            if payload.get("text") == "after":
                self.after_seen.set()

        async def append_batch(chat, specs):
            self.events.extend((kind, dict(payload)) for kind, payload in specs)
            return [{"seq": index + 1} for index in range(len(specs))]

        self.ns = {
            "asyncio": asyncio, "deque": deque, "suppress": suppress, "json": json,
            "re": re, "time": time, "CodexAppServerError": CodexAppServerError,
            "CodexAppServerProtocolError": CodexAppServerProtocolError,
            "CodexAppServerRequestError": CodexAppServerRequestError,
            "CodexAppServerSubscriptionClosed": type("SubscriptionClosed", (Exception,), {}),
            "CODEX_GOALS_ENABLED": True, "CODEX_GOAL_STEER_CLIENT_CAPABILITY": "codex_goal_steer_v1",
            "BACKEND_CODEX": "codex", "DEFAULT_BACKEND": "codex",
            "CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS": 2, "IDLE_KILL_SECONDS": 2,
            "ACTIVE": {"chat": self.active}, "CURRENT_TURNS": {"chat": self.current},
            "ACTIVE_LOCK": asyncio.Lock(), "QUEUE_LOCK": asyncio.Lock(),
            "QUEUED_TURNS": {}, "BUSY_SESSIONS": {"chat"}, "STOPPED_RUNS": set(),
            "STOP_REQUESTS": set(), "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
            "SERVER_MAINTENANCE_SESSIONS": set(),
            "STORE": SimpleNamespace(sessions={"chat": {"backend": "codex", "codex_goal": self.goal}}),
            "codex_goal_time_budget_is_exhausted": lambda session: bool(session.get("codex_goal_time_budget_exhausted")),
            "codex_goal_time_budget_remaining": lambda session: None,
            "provider_route_snapshot_allows_native_steer": lambda value: not value,
            "normalized_provider_cross_chat_route_snapshot": lambda value: value or [],
            "queued_codex_runtime_matches_active": lambda *args: True,
            "build_user_provider_prompt": lambda chat, prompt, files: prompt,
            "clean_assistant_text": lambda text: text.strip(),
            "codex_app_server_tool": lambda item: None,
            "project_codex_notification": Mock(),
            "append_event": append_event, "append_durable_event_batch": append_batch,
            "claim_codex_control_terminal_publication": AsyncMock(return_value=False),
            "stop_codex_goal_resume": AsyncMock(), "release_codex_control_thread": AsyncMock(),
            "finish_codex_control_terminal_publication": AsyncMock(),
            "release_codex_interactive_control_lease": Mock(), "logger": SimpleNamespace(warning=Mock()),
        }
        exec(CODE, self.ns)
        self.subscription = Subscription()
        self.manager = SimpleNamespace(generation=1, request=AsyncMock(), wait_for_notification_handler=AsyncMock())
        self.before_rpc = None
        self.rpc_error = None
        self.after_ack = False

        async def steer(thread, turn, items, **kwargs):
            if self.before_rpc is not None:
                await self.before_rpc()
            if not kwargs["before_send"]():
                raise CodexAppServerProtocolError("dispatch fenced", request_sent=False, safe_to_retry=True)
            self.calls.append((thread, turn, items, kwargs))
            if self.rpc_error is not None:
                raise self.rpc_error
            watermark = self.subscription._last_enqueued_sequence
            if self.after_ack:
                self.subscription.push("item/completed", item={"id": "after", "type": "agentMessage", "phase": "commentary", "text": "after"})
            return turn, watermark

        self.manager.steer_turn_with_notification_watermark = steer

    def request(self):
        return {"selected": {"queued_id": "queued-1", "prompt": "Please check this too", "client_capabilities": ["codex_goal_steer_v1"]},
                "future": asyncio.get_running_loop().create_future(), "phase": "queued",
                "accepted_event": asyncio.Event(), "remaining": 0,
                "expected_provider_turn_id": "turn-1", "goal_identity": ("goal", "Complete the work")}

    async def send(self, request):
        return await self.ns["send_codex_goal_steer"]("chat", "operation", self.manager, "thread", "reservation", self.subscription, self.queue, request)

    def consumer(self):
        return asyncio.create_task(self.ns["consume_codex_native_turn"](
            "chat", "operation", "goal_resume", self.manager, "thread", "reservation", self.subscription, turn_id="turn-1",
        ))

    async def finish(self, task):
        self.goal["status"] = "complete"
        self.subscription.push("turn/completed", turn={"id": "turn-1", "status": "completed"})
        await asyncio.wait_for(task, 1)

    async def test_exact_turn_fence_and_public_input_keep_original_goal_owner(self):
        request = self.request()
        pending = await self.send(request)
        result = await self.ns["commit_codex_goal_steer"]("chat", "operation", "thread", "reservation", pending)
        self.assertEqual(self.calls[0][:3], ("thread", "turn-1", [{"type": "text", "text": "Please check this too", "text_elements": []}]))
        self.assertIs(self.calls[0][3]["notification_subscription"], self.subscription)
        self.assertEqual([kind for kind, _ in self.events], ["turn_unqueued", "turn_queue_delivery_fenced", "turn_queue_run_now", "turn_steered"])
        event = self.events[-1][1]
        self.assertEqual((event["run_id"], event["provider_turn_id"], event["prompt"]), ("operation", "turn-1", "Please check this too"))
        self.assertTrue(event["provider_user_authored"] and event["native_steer"] and event["native_goal_steer"])
        self.assertFalse(result["interrupted"])
        self.assertEqual(self.goal["status"], "active")
        self.assertIs(self.ns["ACTIVE"]["chat"], self.active)
        self.assertEqual(self.current["run_id"], "operation")
        self.manager.request.assert_not_awaited()

    async def test_final_write_guard_rechecks_pause_stop_owner_turn_budget_generation(self):
        changes = [lambda: self.goal.update(status="paused"), lambda: self.goal.update(status="blocked"),
                   lambda: self.active.update(stop_requested=True), lambda: self.ns["SERVER_MAINTENANCE_SESSIONS"].add("chat"),
                   lambda: self.active.update(provider_turn_id="turn-2"), lambda: self.current.update(run_id="successor"),
                   lambda: self.current.update(codex_control_reservation_id="successor"),
                   lambda: self.goal.update(objective="Different goal"), lambda: setattr(self.manager, "generation", 2),
                   lambda: self.ns["DELETING_SESSIONS"].add("chat"), lambda: self.subscription.close(),
                   lambda: self.ns["STORE"].sessions["chat"].update(codex_goal_time_budget_exhausted=True)]
        for change in changes:
            self.setUp()
            request = self.request()
            async def mutate():
                change()
            self.before_rpc = mutate
            with self.assertRaises(self.ns["NativeSteerHandoffError"]) as caught:
                await self.send(request)
            self.assertTrue(caught.exception.safe_to_requeue)
            self.assertEqual(self.calls, [])
            self.assertTrue(request["selected"]["_native_delivery_fenced"])

    async def test_known_provider_rejection_is_retry_safe_unknown_outcome_is_fenced_once(self):
        for error, safe in ((CodexAppServerRequestError("turn/steer", {"message": "turn changed"}), True),
                            (CodexAppServerProtocolError("wrong acknowledgement", request_sent=True, safe_to_retry=False), False)):
            self.setUp()
            self.rpc_error = error
            request = self.request()
            with self.assertRaises(self.ns["NativeSteerHandoffError"]) as caught:
                await self.send(request)
            self.assertEqual(caught.exception.safe_to_requeue, safe)
            self.assertEqual(caught.exception.delivery_uncertain, not safe)
            self.assertEqual(len(self.calls), 1)
            self.assertTrue(request["selected"]["_native_delivery_fenced"])
            self.assertEqual(self.goal["status"], "active")

    async def test_consumer_drains_pre_ack_then_user_then_continuation_without_restart(self):
        self.subscription.push("item/completed", item={"id": "before", "type": "agentMessage", "phase": "commentary", "text": "before"})
        self.after_ack = True
        request = self.request()
        self.queue.put_nowait(request)
        task = self.consumer()
        result = await asyncio.wait_for(asyncio.shield(request["future"]), 1)
        await asyncio.wait_for(self.after_seen.wait(), 1)
        visible = [(kind, event.get("text") or event.get("prompt")) for kind, event in self.events if kind in {"reasoning_summary", "turn_steered"}]
        self.assertEqual(visible, [("reasoning_summary", "before"), ("turn_steered", "Please check this too"), ("reasoning_summary", "after")])
        self.assertFalse(result["interrupted"])
        self.assertEqual(self.goal["status"], "active")
        self.assertTrue(self.active["provider_turn_ready"])
        await self.finish(task)
        self.ns["stop_codex_goal_resume"].assert_not_awaited()

    async def test_ready_steer_is_not_starved_by_notification_backlog(self):
        for index in range(60):
            self.subscription.push("item/completed", item={"id": f"p{index}", "type": "agentMessage", "phase": "commentary", "text": str(index)})
        observed = []
        async def inspect():
            observed.append(sum(kind == "reasoning_summary" for kind, _ in self.events))
        self.before_rpc = inspect
        request = self.request()
        self.queue.put_nowait(request)
        task = self.consumer()
        await asyncio.wait_for(asyncio.shield(request["future"]), 1)
        self.assertLess(observed[0], 60)
        self.assertEqual(sum(kind == "reasoning_summary" for kind, _ in self.events), 60)
        await self.finish(task)

    async def test_repeated_cancel_joins_steering_cleanup_and_settles_waiter(self):
        fenced = asyncio.Event()
        blocker = asyncio.Event()
        original = self.ns["fence_native_steer_delivery"]
        async def hold_fence(*args, **kwargs):
            await original(*args, **kwargs)
            fenced.set()
            await blocker.wait()
        self.ns["fence_native_steer_delivery"] = hold_fence
        request = self.request()
        self.queue.put_nowait(request)
        task = self.consumer()
        await asyncio.wait_for(fenced.wait(), 1)
        await self.ns["ACTIVE_LOCK"].acquire()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.ns["ACTIVE_LOCK"].release()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        with self.assertRaises(self.ns["NativeSteerHandoffError"]) as caught:
            await asyncio.wait_for(asyncio.shield(request["future"]), 1)
        self.assertTrue(caught.exception.safe_to_requeue)
        self.assertIsNone(self.active["native_steer_queue"])
        self.assertTrue(self.subscription._closed)
        self.assertEqual(self.calls, [])

    async def test_safe_goal_rollback_is_durably_held_not_automatically_restarted(self):
        selected = self.request()["selected"]
        selected.update(_native_delivery_fenced=True, _paused_after_stop=True)
        await self.ns["requeue_native_steer_after_safe_rejection"](
            "chat", selected, selected_index=0, selected_predecessor_id=None,
            selected_successor_id=None, preserve_pause=True,
        )
        self.assertEqual([kind for kind, _ in self.events], ["turn_queued", "turn_queue_reordered", "turn_queue_paused"])
        self.assertEqual(self.events[-1][1]["queued_ids"], ["queued-1"])
        self.assertTrue(self.ns["QUEUED_TURNS"]["chat"][0]["_paused_after_stop"])
        self.assertNotIn("_native_delivery_fenced", selected)


if __name__ == "__main__":
    unittest.main()
