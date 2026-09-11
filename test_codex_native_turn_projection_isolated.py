"""Exercise the native projection AST without importing or starting the server."""

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


_SOURCE = Path(__file__).with_name("agent_server.py")
_FUNCTIONS = {
    "consume_codex_native_turn",
    "concise_error_message",
    "is_codex_reconnect_notice",
    "is_codex_app_server_retry_notice",
    "codex_reasoning_text",
    "codex_app_server_reasoning_summary",
    "session_lifecycle_lock",
    "maybe_notify_chat_mailbox_codex",
}
_TREE = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
_SELECTED = [node for node in _TREE.body if isinstance(
    node, (ast.FunctionDef, ast.AsyncFunctionDef)
) and node.name in _FUNCTIONS]
assert {node.name for node in _SELECTED} == _FUNCTIONS
_CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
    module="__future__", names=[ast.alias(name="annotations")], level=0,
    lineno=1, col_offset=0,
), *_SELECTED], type_ignores=[])), str(_SOURCE), "exec")


class SubscriptionClosed(Exception):
    pass


def notice(method, *, turn="turn-1", **params):
    return {"method": method, "params": {"turnId": turn, **params}}


def started(turn="turn-1"):
    return notice("turn/started", turn=turn)


def completed(turn="turn-1", status="completed", error=None):
    return {"method": "turn/completed", "params": {
        "turnId": turn, "turn": {"id": turn, "status": status, "error": error},
    }}


def item(item_id, kind, turn="turn-1", **fields):
    return notice("item/completed", turn=turn, item={
        "id": item_id, "type": kind, **fields,
    })


class Subscription:
    def __init__(self, notifications, store, goal):
        self.notifications = deque(notifications)
        self.store = store
        self.goal = goal
        self.closed = 0
        self.calls = 0

    async def next_notification(self, *, timeout):
        self.calls += 1
        if self.calls > 40:
            raise AssertionError("consumer failed to stop after the fixture")
        if not self.notifications:
            if self.goal:
                self.store.sessions["chat"]["codex_goal"]["status"] = "complete"
            raise asyncio.TimeoutError("provider idle timeout")
        value = self.notifications.popleft()
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed += 1


class NativeTurnProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def run_projection(self, notifications, *, goal=False, claimed=True,
                             interrupted_before_start=False, stopped=False):
        store = SimpleNamespace(sessions={"chat": {
            "codex_goal": {"status": "active", "id": "goal"},
        }})
        namespace = {
            "asyncio": asyncio, "time": time, "json": json, "re": re,
            "suppress": suppress,
            "STORE": store, "ACTIVE_LOCK": asyncio.Lock(),
            "SESSION_LIFECYCLE_LOCKS": {},
            "CHAT_MAILBOX_PENDING": set(),
            "ACTIVE": {"chat": {"run_id": "operation"}},
            "STOPPED_RUNS": {"unrelated"} | ({"operation"} if stopped or interrupted_before_start else set()),
            "BACKEND_CODEX": "codex",
            "CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS": 30,
            "IDLE_KILL_SECONDS": 30,
            "CodexAppServerSubscriptionClosed": SubscriptionClosed,
            "codex_goal_time_budget_remaining": lambda session: None,
            "clean_assistant_text": lambda text: text.strip(),
            "compact_memory_text": lambda text, limit: text[:limit],
            "codex_app_server_tool": lambda value: (
                {"id": value["id"], "name": "shell", "input": "fixture"}
                if value.get("type") == "commandExecution" else None
            ),
            "codex_app_server_tool_output": lambda value: ("safe output", 0, False),
            "project_codex_notification": Mock(),
            "append_event": AsyncMock(),
            "claim_codex_control_terminal_publication": AsyncMock(return_value=claimed),
            "stop_codex_goal_resume": AsyncMock(),
            "finish_codex_control_terminal_publication": AsyncMock(),
            "release_codex_control_thread": AsyncMock(),
            "release_codex_interactive_control_lease": Mock(),
            "logger": SimpleNamespace(warning=Mock()),
        }
        exec(_CODE, namespace)
        manager = SimpleNamespace(
            request=AsyncMock(), wait_for_notification_handler=AsyncMock(),
        )
        subscription = Subscription(notifications, store, goal)
        self.namespace = namespace
        self.manager = manager
        self.subscription = subscription
        await namespace["consume_codex_native_turn"](
            "chat", "operation", "goal_resume" if goal else "review",
            manager, "thread", "reservation", subscription,
            interrupted_before_start=interrupted_before_start,
        )
        return self.events()

    def events(self, kind=None):
        events = [(call.args[1], call.args[2])
                  for call in self.namespace["append_event"].await_args_list]
        return events if kind is None else [data for name, data in events if name == kind]

    def assert_clean_release(self, *, schedule_queue=True):
        self.assertEqual(self.subscription.closed, 1)
        self.namespace["claim_codex_control_terminal_publication"].assert_awaited_once_with(
            "chat", "operation", "thread", "reservation",
        )
        self.namespace["finish_codex_control_terminal_publication"].assert_awaited_once_with(
            "chat", reservation_id="reservation", schedule_queue=schedule_queue,
        )
        self.namespace["release_codex_interactive_control_lease"].assert_called_once_with("thread")
        self.namespace["release_codex_control_thread"].assert_awaited_once_with(
            "chat", self.manager, "thread", reserved_session=True,
            reservation_id="reservation", lease_already_released=True,
            schedule_queue=False,
        )
        self.assertEqual(self.namespace["STOPPED_RUNS"], {"unrelated"})
        self.manager.request.assert_not_awaited()

    async def test_public_commentary_final_and_tools_keep_exact_native_identity(self):
        events = await self.run_projection([
            started(),
            item("progress", "agentMessage", text="Checking the result.", phase="commentary"),
            notice("item/started", item={"id": "tool", "type": "commandExecution"}),
            item("tool", "commandExecution"),
            item("answer", "agentMessage", text="Done.", phase="final_answer"),
            completed(),
        ])
        self.assertEqual([name for name, _ in events], [
            "reasoning_summary", "tool_started", "tool_finished",
            "assistant_text", "codex_review_finished",
        ])
        self.assertEqual(events[0][1]["phase"], "commentary")
        self.assertEqual(events[3][1]["phase"], "final_answer")
        for (_, data), item_id in zip(events[:4], ["progress", "tool", "tool", "answer"]):
            self.assertEqual((data["run_id"], data["provider_turn_id"], data["item_id"]),
                             ("operation", "turn-1", item_id))
            self.assertEqual(data["purpose"], "codex_review")
        self.assert_clean_release()

    async def test_goal_runtime_user_items_do_not_publish_human_turns(self):
        from test_codex_goal_history_isolated import GOAL

        runtime = {"id": "goal-input", "type": "userMessage", "content": [
            {"type": "text", "text": GOAL},
        ], "internal_chat_message_metadata_passthrough": {
            "content_item_kinds": ["goal.internal_context"],
        }}
        events = await self.run_projection([
            started(), notice("item/started", item=runtime),
            notice("item/completed", item=runtime),
            item("answer", "agentMessage", text="Actual answer.", phase="final_answer"),
            completed(),
        ], goal=True)
        self.assertNotIn("codex_internal_context", json.dumps(events))
        self.assertEqual(self.events("turn_started"), [])
        self.assertEqual([event["text"] for event in self.events("assistant_text")], ["Actual answer."])

    async def test_reasoning_uses_only_public_summary_and_plan_has_separate_buffer(self):
        class RawReasoning(dict):
            def get(self, key, default=None):
                if key == "text":
                    raise AssertionError("raw reasoning text must not be read")
                return super().get(key, default)

        raw = item("summary", "reasoning", summary=[{"text": "Public summary"}])
        raw["params"]["item"] = RawReasoning(raw["params"]["item"])
        events = await self.run_projection([
            started(),
            notice("item/reasoning/textDelta", itemId="summary", delta="RAW PRIVATE DELTA"),
            raw,
            notice("item/reasoning/textDelta", itemId="raw-only", delta="RAW PRIVATE DELTA"),
            item("raw-only", "reasoning", text="RAW PRIVATE ITEM"),
            notice("item/reasoning/summaryTextDelta", itemId="buffered", delta="Safe buffered summary"),
            item("buffered", "reasoning", text="RAW PRIVATE ITEM"),
            notice("item/plan/delta", itemId="plan", delta="Public plan"),
            item("plan", "plan"),
            item("plan-text", "plan", text="Completed plan"),
            completed(),
        ])
        self.assertEqual([(data["text"], data["phase"]) for data in self.events("reasoning_summary")], [
            ("Public summary", "summary"), ("Safe buffered summary", "summary"),
            ("Public plan", "plan"), ("Completed plan", "plan"),
        ])
        self.assertNotIn("RAW PRIVATE", json.dumps(events))
        self.assert_clean_release()

    async def test_completed_summary_does_not_mix_plan_or_raw_deltas(self):
        await self.run_projection([
            started(),
            notice("item/plan/delta", itemId="same", delta="Wrong plan buffer"),
            notice("item/reasoning/textDelta", itemId="same", delta="RAW PRIVATE"),
            notice("item/reasoning/summaryTextDelta", itemId="same", delta="Old public delta"),
            item("same", "reasoning", summary=["Authoritative public summary"]),
            completed(),
        ])
        self.assertEqual([data["text"] for data in self.events("reasoning_summary")],
                         ["Authoritative public summary"])

    async def test_retry_notices_never_turn_failed_completion_into_a_false_error_row(self):
        for params in [
            {"error": {"message": "Temporary transport unavailable"}, "willRetry": True},
            {"error": {"message": "Reconnecting... 2/2"}},
            {"message": "Reconnecting... 1/2", "willRetry": True},
        ]:
            with self.subTest(params=params):
                await self.run_projection([
                    started(), notice("error", **params), completed(status="failed"),
                ])
                self.assertEqual(self.events("error"), [])
                terminal = self.events("codex_review_finished")[0]
                self.assertEqual(terminal["status"], "failed")
                self.assertIsNone(terminal["error"])
                self.assert_clean_release()

    async def test_explicit_nonretry_and_real_failures_remain_visible(self):
        for message, flags in [
            ("Reconnecting... 2/2", {"willRetry": False}),
            ("Credentials rejected", {}),
            ("Disconnected permanently", {"willRetry": False}),
        ]:
            with self.subTest(message=message):
                await self.run_projection([
                    started(), notice("error", message=message, **flags),
                    completed(status="failed"),
                ])
                self.assertEqual(self.events("error")[0]["message"], message)
                self.assertEqual(self.events("codex_review_finished")[0]["error"], message)
                self.assert_clean_release()

    async def test_successful_terminal_packet_clears_prior_error_even_without_retry_flag(self):
        await self.run_projection([
            started(), notice("error", message="Earlier transport failure", willRetry=False),
            item("answer", "agentMessage", text="Recovered answer."), completed(),
        ])
        self.assertEqual(self.events("error"), [])
        terminal = self.events("codex_review_finished")[0]
        self.assertEqual(terminal["status"], "completed")
        self.assertIsNone(terminal["error"])
        self.assert_clean_release()

    async def test_actual_completed_error_is_not_cleared_by_retry_notice(self):
        await self.run_projection([
            started(), notice("error", message="Reconnecting... 2/2", willRetry=True),
            completed(status="failed", error={"message": "Connection could not be restored"}),
        ])
        self.assertEqual(self.events("error")[0]["message"], "Connection could not be restored")
        self.assertEqual(self.events("codex_review_finished")[0]["status"], "failed")

    async def test_goal_continuations_share_operation_but_keep_provider_turn_identity(self):
        await self.run_projection([
            started(),
            notice("item/agentMessage/delta", itemId="unfinished", delta="Old partial answer"),
            notice("error", message="Reconnecting... 2/2"),
            item("same", "agentMessage", text="First continuation.", phase="commentary"),
            completed(),
            notice("error", turn="", message="Between-turn stale notice"),
            started("turn-2"),
            item("unfinished", "agentMessage", turn="turn-2"),
            item("same", "agentMessage", turn="turn-2", text="Second continuation.", phase="commentary"),
            item("old", "agentMessage", turn="turn-1", text="Stale output"),
            completed("turn-2"),
        ], goal=True)
        rows = self.events("reasoning_summary")
        self.assertEqual([(data["provider_turn_id"], data["text"]) for data in rows], [
            ("turn-1", "First continuation."), ("turn-2", "Second continuation."),
        ])
        self.assertTrue(all(data["run_id"] == "operation" and
                            data["purpose"] == "codex_goal_resume" for data in rows))
        self.assertEqual(self.events("assistant_text"), [])
        self.assertEqual(self.events("error"), [])
        self.assertEqual(self.events("turn_finished")[0]["status"], "completed")
        self.assertTrue(self.namespace["ACTIVE"]["chat"]["codex_goal_handoff_closed"])
        self.namespace["stop_codex_goal_resume"].assert_not_awaited()
        self.assert_clean_release()

    async def test_new_goal_turn_resets_error_before_a_real_failed_terminal(self):
        await self.run_projection([
            notice("error", turn="", message="Stale pre-turn error"),
            started("turn-2"), completed("turn-2", status="failed"),
        ], goal=True)
        self.assertEqual(self.events("error"), [])
        self.assertEqual(self.events("turn_finished")[0]["status"], "failed")
        self.namespace["stop_codex_goal_resume"].assert_awaited_once()
        self.assert_clean_release()

    async def test_duplicate_start_does_not_erase_same_turn_error(self):
        await self.run_projection([
            started(), notice("error", message="Actual failure", willRetry=False),
            started(), completed(status="failed"),
        ], goal=True)
        self.assertEqual(self.events("error")[0]["message"], "Actual failure")
        self.assert_clean_release()

    async def test_timeout_and_closed_subscription_remain_terminal_errors(self):
        for error in [asyncio.TimeoutError("provider deadline reached"),
                      SubscriptionClosed("provider connection closed")]:
            with self.subTest(error=type(error).__name__):
                await self.run_projection([started(), error])
                self.assertEqual(self.events("error")[0]["message"], str(error))
                self.assertEqual(self.events("codex_review_finished")[0]["status"], "failed")
                self.assert_clean_release()

    async def test_cancellation_preserves_interrupted_cleanup_and_never_sends(self):
        with self.assertRaises(asyncio.CancelledError):
            await self.run_projection([started(), asyncio.CancelledError()], goal=True, stopped=True)
        self.assertEqual(self.events("error"), [])
        self.assertEqual(self.events("turn_finished")[0]["status"], "interrupted")
        self.namespace["stop_codex_goal_resume"].assert_awaited_once()
        self.assert_clean_release(schedule_queue=False)

    async def test_lost_terminal_claim_does_not_publish_or_clean_other_owner(self):
        await self.run_projection([started(), completed()], claimed=False)
        self.assertEqual(self.events(), [])
        self.namespace["finish_codex_control_terminal_publication"].assert_not_awaited()
        self.namespace["release_codex_interactive_control_lease"].assert_not_called()
        self.namespace["release_codex_control_thread"].assert_awaited_once_with(
            "chat", self.manager, "thread", reserved_session=True,
            reservation_id="reservation", schedule_queue=True,
        )
        self.assertEqual(self.subscription.closed, 1)


if __name__ == "__main__":
    unittest.main()
