"""Native shell intents keep the existing consumer through terminal races."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import agent_server as server


class Subscription:
    def __init__(self, packets):
        self.packets = asyncio.Queue()
        for index, packet in enumerate(packets, 1):
            self.packets.put_nowait((index, packet))
        self._last_enqueued_sequence = len(packets)
        self.closed = False

    async def next_notification_with_sequence(self, timeout=None):
        return await asyncio.wait_for(self.packets.get(), timeout)

    def close(self):
        self.closed = True


def turn(method, turn_id, status="completed"):
    return {"method": method, "params": {"threadId": "thread", "turn": {"id": turn_id, "status": status}}}


def item(method, turn_id):
    return {"method": method, "params": {"threadId": "thread", "turnId": turn_id, "item": {
        "id": "shell-item", "type": "commandExecution", "source": "userShell",
        "command": "wrapped echo ok", "commandActions": [{"type": "unknown", "command": "echo ok"}],
        "status": "completed" if method == "item/completed" else "inProgress", "exitCode": 0,
    }}}


class NativeShellContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def scenario(self, operation, *, stopped=False, admitted_during_projection=False):
        intent = {"operation_id": "manual-shell", "command": "echo ok", "after_sequence": 0,
                  "item_id": "", "turn_id": "", "item_completed": False, "turn_completed": False}
        owner = {"run_id": "owner", "provider_thread_id": "thread", "provider_turn_id": "original",
                 "provider_turn_ready": True, "codex_control_reservation_id": "reservation",
                 "codex_shell_intents": [intent], "stop_requested": stopped,
                 "native_interrupt_sent": stopped}
        packets = [turn("turn/completed", "original", "interrupted" if stopped else "completed"),
                   turn("turn/started", "late-shell", "inProgress"), item("item/started", "late-shell"),
                   item("item/completed", "late-shell"), turn("turn/completed", "late-shell", "interrupted" if stopped else "completed")]
        if admitted_during_projection:
            packets.extend([turn("turn/started", "next-shell", "inProgress"), item("item/started", "next-shell"),
                            item("item/completed", "next-shell"), turn("turn/completed", "next-shell")])
        subscription = Subscription(packets)
        manager = AsyncMock()
        admitted = False
        async def projected(*_args):
            nonlocal admitted
            if admitted_during_projection and not admitted and not owner["codex_shell_intents"]:
                admitted = True
                owner["codex_shell_intents"].append({**intent, "operation_id": "next-manual-shell",
                    "after_sequence": 5, "item_id": "", "turn_id": "", "item_completed": False,
                    "turn_completed": False})
        manager.wait_for_notification_handler.side_effect = projected
        events = AsyncMock(return_value={})
        with (
            patch.object(server, "ACTIVE", {"chat": owner}),
            patch.object(server, "CURRENT_TURNS", {"chat": {"run_id": "owner"}}),
            patch.object(server, "STOPPED_RUNS", {"owner"} if stopped else set()),
            patch.object(server.STORE, "sessions", {"chat": {"id": "chat", "codex_goal": {"status": "complete"}}}),
            patch.object(server, "append_event", events),
            patch.object(server, "finish_reasoning_summary_stream", AsyncMock()),
            patch.object(server, "maybe_notify_chat_mailbox_codex", AsyncMock()),
            patch.object(server, "stop_codex_goal_resume", AsyncMock()),
        ):
            result = await asyncio.wait_for(server.consume_codex_native_turn(
                "chat", "owner", operation, manager, "thread", "reservation", subscription,
                turn_id="original", finalize_operation=False), 2)
            self.assertIs(server.ACTIVE["chat"], owner)
            self.assertEqual(owner["run_id"], "owner")
            self.assertEqual(owner["codex_shell_intents"], [])
            self.assertNotIn("codex_shell_subscription", owner)
            self.assertTrue(owner["codex_shell_handoff_closed"])
        event_types = [call.args[1] for call in events.await_args_list]
        expected = 2 if admitted_during_projection else 1
        self.assertEqual(event_types.count("tool_started"), expected)
        self.assertEqual(event_types.count("tool_finished"), expected)
        self.assertEqual(event_types.count("codex_shell_finished"), expected)
        self.assertNotIn("turn_finished", event_types)
        self.assertEqual(result["status"], "interrupted" if stopped else "completed")
        self.assertTrue(subscription.closed)
        if stopped:
            manager.request.assert_awaited_once_with("turn/interrupt", {"threadId": "thread", "turnId": "late-shell"})
        else:
            manager.request.assert_not_awaited()

    async def test_goal_owner_retains_late_shell_after_goal_completion(self):
        await self.scenario("goal_resume")

    async def test_review_owner_retains_late_shell_after_review_completion(self):
        await self.scenario("review")

    async def test_ordinary_shell_handoff_has_no_goal_restart_or_extra_terminal(self):
        await self.scenario("shell_resume")

    async def test_stop_keeps_receiver_for_late_shell_and_interrupts_exact_turn(self):
        await self.scenario("shell_resume", stopped=True)

    async def test_shell_admitted_during_terminal_projection_retains_native_owner(self):
        await self.scenario("review", admitted_during_projection=True)

    async def test_goal_activated_during_shell_tail_keeps_same_supervisor(self):
        queue = asyncio.Queue(maxsize=1)
        owner = {"run_id": "owner", "provider_thread_id": "thread",
                 "provider_turn_id": "shell", "provider_turn_ready": True,
                 "codex_control_reservation_id": "reservation",
                 "codex_native_operation_kind": "shell_resume", "native_steer_queue": queue,
                 "codex_shell_intents": [{"operation_id": "manual-shell", "after_sequence": 0,
                    "item_id": "", "turn_id": "", "item_completed": False, "turn_completed": False}]}
        subscription = Subscription([item("item/started", "shell"), item("item/completed", "shell"),
                                     turn("turn/completed", "shell")])
        session = {"id": "chat", "codex_goal": None}
        activated = asyncio.Event()
        manager = AsyncMock()
        async def projected(*_args):
            if not activated.is_set():
                session["codex_goal"] = {"id": "new-goal", "status": "active", "objective": "Finish goal"}
                activated.set()
            else:
                session["codex_goal"]["status"] = "complete"
        manager.wait_for_notification_handler.side_effect = projected
        events = AsyncMock(return_value={})
        with (
            patch.object(server, "ACTIVE", {"chat": owner}),
            patch.object(server, "CURRENT_TURNS", {"chat": {"run_id": "owner", "purpose": "user"}}),
            patch.object(server, "STOPPED_RUNS", set()),
            patch.object(server, "CODEX_GOALS_ENABLED", True),
            patch.object(server.STORE, "sessions", {"chat": session}),
            patch.object(server, "append_event", events),
            patch.object(server, "finish_reasoning_summary_stream", AsyncMock()),
            patch.object(server, "maybe_notify_chat_mailbox_codex", AsyncMock()),
        ):
            task = asyncio.create_task(server.consume_codex_native_turn(
                "chat", "owner", "shell_resume", manager, "thread", "reservation", subscription,
                turn_id="shell", finalize_operation=False))
            try:
                await asyncio.wait_for(activated.wait(), 1)
                await asyncio.sleep(.65)
                self.assertFalse(task.done(), "Active native goal lost its retained shell supervisor")
                self.assertIs(server.ACTIVE["chat"], owner)
                self.assertIs(owner["native_steer_queue"], queue)
                self.assertIs(owner["codex_shell_subscription"], subscription)
                self.assertEqual(owner["codex_native_operation_kind"], "goal_resume")
                for packet in [turn("turn/started", "goal", "inProgress"),
                    {"method": "item/completed", "params": {"threadId": "thread", "turnId": "goal",
                     "item": {"id": "goal-answer", "type": "agentMessage", "text": "GOAL_DONE"}}},
                    turn("turn/completed", "goal")]:
                    subscription._last_enqueued_sequence += 1
                    subscription.packets.put_nowait((subscription._last_enqueued_sequence, packet))
                result = await asyncio.wait_for(task, 2)
                self.assertEqual(result["result_text"], "GOAL_DONE")
                self.assertEqual(server.CURRENT_TURNS["chat"]["purpose"], "user")
                manager.request.assert_not_awaited()
                self.assertNotIn("turn_started", [call.args[1] for call in events.await_args_list])
                self.assertTrue(owner["codex_goal_handoff_closed"])
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
