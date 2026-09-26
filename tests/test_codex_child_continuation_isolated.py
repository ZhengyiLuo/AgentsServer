"""Ordinary parent continuation regressions with a no-process native router.

Only the existing AST allowlist runner is evaluated. Never import agent_server,
start a provider, or read its authenticated runtime state.
"""
from __future__ import annotations

import asyncio
import ast
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from codex_app_server import CodexAppServerClient, CodexAppServerTurn, CodexAppServerTimeout
from tests import test_goal_followup_lifecycle_isolated as ordinary_fixture

NAMES = {"codex_collaboration_states", "codex_collaboration_thread_ids",
         "codex_child_status_from_thread", "normalize_subagent_status",
         "codex_subagent_thread_identity", "useful_subagent_identity_text",
         "compact_subagent_text", "project_codex_notification", "stop_turn", "active_snapshot_input"}
nodes = [node for node in ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text()).body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
assert {node.name for node in nodes} == NAMES
PROJECTION_CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
    module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
    type_ignores=[])), "native-child-projection-isolated", "exec")
del nodes


class ControlledContinuationTurn(CodexAppServerTurn):
    """Keep transport routing real while controlling only native acceptance."""


class CodexChildContinuationTests(unittest.IsolatedAsyncioTestCase):
    setUp = ordinary_fixture.GoalFollowupLifecycleTests.setUp
    asyncTearDown = ordinary_fixture.GoalFollowupLifecycleTests.asyncTearDown
    wait = ordinary_fixture.GoalFollowupLifecycleTests.wait

    async def start_ordinary(self):
        self.session.pop("codex_goal", None)
        self.client = CodexAppServerClient(
            "must-never-execute", cwd=str(self.root), env_factory=lambda: {},
            process_factory=AsyncMock(side_effect=AssertionError("provider startup forbidden")),
        )
        self.client._generation = 1
        self.client.interrupt_turn = AsyncMock()
        self.subscription = self.client.subscribe_thread("thread")
        self.turn = ControlledContinuationTurn(
            self.client, "thread", "turn-1", self.subscription,
            transport_generation=1, _retain_thread_stream=True,
        )
        self.client._turns_by_thread["thread"] = self.turn
        self.continuation_calls = []
        async def continue_native(*, before_send, **kwargs):
            if not before_send() or not self.turn._completed:
                return None
            self.continuation_calls.append(kwargs)
            # Acceptance and notifications are independently controlled. This
            # is the same race as real app-server responding before turn/started.
            self.turn.turn_id = f"turn-{len(self.continuation_calls) + 1}"
            self.turn._completed = False
            return self.turn.turn_id
        self.turn.continue_after_subagents = AsyncMock(side_effect=continue_native)
        self.manager.client = self.client
        self.manager.start_turn = AsyncMock(return_value=self.turn)
        self.manager.add_notification_handler = self.client.add_notification_handler
        self.manager.remove_notification_handler = self.client.remove_notification_handler
        self.manager.wait_for_notification_handler = self.client.wait_for_notification_handler
        self.manager.list_turns = AsyncMock(return_value=[])
        self.manager.retire_generation = AsyncMock()
        self.ns.update({
            "CODEX_SUBAGENT_STATE": {}, "CODEX_SUBAGENT_SESSION_INDEX": {},
            "CODEX_SUBAGENT_LIVE_GENERATIONS": {},
            "CODEX_SUBAGENT_LIVE_MANAGERS": {},
            "CODEX_SUBAGENT_INDEX_LOCK": threading.RLock(),
            "IDLE_WARN_SECONDS": 10_000, "IDLE_KILL_SECONDS": 20_000,
            "codex_app_server_changed_paths": lambda item: set(),
            "CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS": 0.2,
            "CODEX_APP_SERVER_TIMEOUT_SECONDS": 0.1,
            "codex_turn_matches_client_user_message": lambda value, run: value.get("clientUserMessageId") == run,
        })
        self.ns["SUBAGENT_SNAPSHOT_TEXT_LIMIT"] = 200
        exec(PROJECTION_CODE, self.ns)
        self.ns["stop_turn"] = AsyncMock(side_effect=AssertionError("unexpected stop call"))
        runner = asyncio.create_task(self.ns["run_codex_app_server"](
            "chat", "operation", "Inspect this with a subagent", self.session,
            self.root / "manifest.json", allow_exec_fallback=False,
            diff_baseline={"head": "synthetic"},
            provider_runtime_env={"AGENTSDOCK_PROVIDER_RUN_ID": "operation"},
        ))
        self.tasks.append(runner)
        await self.wait(lambda: (self.ns["ACTIVE"].get("chat") or {}).get("provider_turn_ready"))
        self.active = self.ns["ACTIVE"]["chat"]
        self.route("turn/started", turn={"id": "turn-1", "status": "inProgress"})
        return runner

    def route(self, method, *, thread="thread", turn_id="turn-1", **params):
        self.client._route_notification({"method": method,
            "params": {"threadId": thread, "turnId": turn_id, **params}})

    def spawn(self, child="child-1", *, turn_id="turn-1"):
        # This metadata is the independent card projection. The consumer must
        # still observe the actual native spawn notification, not a fake prompt.
        self.ns["CODEX_SUBAGENT_SESSION_INDEX"][child] = "chat"
        self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"][child] = 1
        self.ns["CODEX_SUBAGENT_LIVE_MANAGERS"][child] = self.manager
        self.ns["CODEX_SUBAGENT_STATE"][child] = {
            "session_id": "chat", "run_id": "operation", "subagent_id": child,
            "subagent_parent_thread_id": "thread", "subagent_status": "running",
        }
        self.route("item/completed", turn_id=turn_id, item={
            "id": f"spawn-{child}-{turn_id}", "type": "collabAgentToolCall", "tool": "spawnAgent",
            "senderThreadId": "thread", "receiverThreadIds": [child],
            "status": "completed", "agentsStates": {child: {"status": "running"}},
        })

    def child_completed(self, child="child-1"):
        self.ns["CODEX_SUBAGENT_STATE"][child]["subagent_status"] = "completed"
        self.ns["CODEX_SUBAGENT_LIVE_GENERATIONS"].pop(child, None)
        self.ns["CODEX_SUBAGENT_LIVE_MANAGERS"].pop(child, None)
        self.route("turn/completed", thread=child, turn_id=f"{child}-turn",
                   turn={"id": f"{child}-turn", "status": "completed"})

    def answer(self, text, *, turn_id="turn-1"):
        self.route("item/completed", turn_id=turn_id, item={
            "id": f"answer-{turn_id}", "type": "agentMessage",
            "phase": "final_answer", "text": text,
        })

    def completed(self, *, turn_id="turn-1", status="completed"):
        self.route("turn/completed", turn_id=turn_id,
                   turn={"id": turn_id, "status": status})

    async def settle_callbacks(self):
        # Explicit event-loop checkpoints, not elapsed-time assumptions about
        # provider wake latency. No provider/network calls occur in this fixture.
        for _ in range(30):
            await asyncio.sleep(0)

    async def test_buffered_followup_is_consumed_before_owner_is_released(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.answer("Initial answer; delegated work is still running")
        self.completed()
        self.child_completed()
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Consolidated child result", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        answers = [row["text"] for kind, row in self.events if kind == "assistant_text"]
        self.assertEqual(answers, ["Initial answer; delegated work is still running", "Consolidated child result"])
        self.manager.start_turn.assert_awaited_once()
        self.ns["finalize_owned_turn_finished"].assert_awaited_once()
        self.assertEqual(self.ns["finalize_owned_turn_finished"].await_args.kwargs["payload"]["result_text"],
                         "Consolidated child result")
        self.assertTrue(self.turn._closed)
        self.assertEqual(self.client._subscriptions, set())

    async def test_child_terminal_does_not_release_owner_before_native_followup(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.answer("Initial answer")
        self.completed()
        await self.settle_callbacks()
        self.assertFalse(runner.done(), "ordinary parent owner was dropped while its child was running")
        self.assertFalse(self.turn._closed)
        self.child_completed()
        await self.settle_callbacks()
        self.assertFalse(runner.done(), "child terminal is not proof the parent consumed its result")
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Collected child result", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        self.assertEqual([row["text"] for kind, row in self.events if kind == "assistant_text"],
                         ["Initial answer", "Collected child result"])
        self.manager.start_turn.assert_awaited_once()
        self.ns["finalize_owned_turn_finished"].assert_awaited_once()

    async def test_multiple_child_drain_turns_preserve_authority_and_latest_final(self):
        runner = await self.start_ordinary()
        self.spawn("child-1")
        self.spawn("child-2")
        self.answer("Initial")
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        for number in (1, 2):
            self.child_completed(f"child-{number}")
            turn_id = f"turn-{number + 1}"
            self.route("turn/started", turn_id=turn_id,
                       turn={"id": turn_id, "status": "inProgress"})
            self.answer(f"Collected {number}", turn_id=turn_id)
            self.completed(turn_id=turn_id)
            if number == 1:
                await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
                self.assertFalse(runner.done())
        await asyncio.wait_for(runner, 5)
        self.assertEqual([row["text"] for kind, row in self.events if kind == "assistant_text"],
                         ["Initial", "Collected 1", "Collected 2"])
        result = self.ns["finalize_owned_turn_finished"].await_args
        self.assertEqual(result.args[:2], ("chat", "operation"))
        self.assertEqual(result.kwargs["payload"]["result_text"], "Collected 2")
        self.assertIs(self.ns["CROSS_CHAT_CAPABILITIES"]["operation"], self.authority)
        self.assertEqual(self.ns["RUN_METADATA"], {"operation": {"synthetic_authority": "original"}})
        self.manager.request.assert_not_awaited()
        self.ns["stop_codex_goal_resume"].assert_not_awaited()
        self.assertFalse({kind for kind, _ in self.events} & {"turn_started", "turn_steered", "turn_stopped"})

    async def test_child_finished_during_parent_turn_does_not_require_an_extra_turn(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.child_completed()
        self.answer("Already consolidated")
        self.completed()
        await asyncio.wait_for(runner, 5)
        self.assertFalse(self.active.get("codex_child_continuation_waiting", False))
        self.manager.start_turn.assert_awaited_once()

    async def test_old_child_cards_and_other_threads_cannot_keep_parent_alive(self):
        runner = await self.start_ordinary()
        self.ns["CODEX_SUBAGENT_STATE"]["historic"] = {
            "session_id": "chat", "run_id": "past-run", "subagent_status": "running",
        }
        self.route("turn/started", thread="foreign", turn_id="foreign-turn")
        self.route("turn/completed", thread="foreign", turn_id="foreign-turn",
                   turn={"id": "foreign-turn", "status": "completed"})
        self.answer("No delegated work")
        self.completed()
        await asyncio.wait_for(runner, 5)
        self.assertEqual([row["text"] for kind, row in self.events if kind == "assistant_text"],
                         ["No delegated work"])

    async def test_replaced_owner_cannot_receive_old_continuation_output(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        successor = {**self.active, "run_id": "successor"}
        self.ns["ACTIVE"]["chat"] = successor
        self.current["run_id"] = "successor"
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Wrong owner", turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        self.assertFalse(any(kind == "assistant_text" for kind, _ in self.events))
        self.assertIs(self.ns["ACTIVE"]["chat"], successor)
        self.assertFalse(successor.get("stop_requested", False))
        self.client.interrupt_turn.assert_not_awaited()

    async def test_replaced_generation_cannot_continue_original_authority(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        self.manager.generation = 2
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Wrong generation", turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        self.assertFalse(any(kind == "assistant_text" for kind, _ in self.events))
        self.client.interrupt_turn.assert_not_awaited()

    async def test_explicit_stop_event_finishes_wait_without_provider_packets(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        self.active["stop_requested"] = True
        self.ns["STOPPED_RUNS"].add("operation")
        self.active["codex_child_continuation_stop"].set()
        await asyncio.wait_for(runner, 5)
        self.assertTrue(self.ns["finalize_owned_turn_finished"].await_args.kwargs["stopped"])
        self.assertTrue(self.turn._closed)
        self.assertEqual(self.client._notification_handlers, set())

    async def test_native_turn_start_at_stop_wake_is_interrupted_before_release(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        self.active["stop_requested"] = True
        self.ns["STOPPED_RUNS"].add("operation")
        self.active["codex_child_continuation_stop"].set()
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        async def interrupted(thread, turn_id):
            self.completed(turn_id=turn_id, status="interrupted")
        self.client.interrupt_turn.side_effect = interrupted
        await asyncio.wait_for(runner, 5)
        self.client.interrupt_turn.assert_awaited_once_with("thread", "turn-2")
        self.assertTrue(self.turn._closed)
        self.assertTrue(self.ns["finalize_owned_turn_finished"].await_args.kwargs["stopped"])

    async def test_last_child_drain_requests_one_native_continuation_without_user_input(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        self.child_completed()
        await self.wait(lambda: len(self.continuation_calls) == 1)
        self.assertEqual(self.active["run_id"], "operation")
        self.child_completed()  # Duplicate terminal cannot start another turn.
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Native notification consumed", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        self.assertEqual(len(self.continuation_calls), 1)
        self.assertEqual(self.continuation_calls[0], {
            "client_user_message_id": "operation", "responsesapi_client_metadata": {
                "agentsdock_run_id": "operation", "agentsdock_run_proof": "synthetic-original-proof",
            },
        })
        self.manager.start_turn.assert_awaited_once()
        self.assertFalse(any(kind == "turn_started" for kind, _ in self.events))
        self.assertEqual(self.ns["finalize_owned_turn_finished"].await_args.kwargs["payload"]["result_text"],
                         "Native notification consumed")

    async def test_spontaneous_native_followup_wins_before_empty_continuation(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        self.child_completed()
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Provider already continued", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        self.assertEqual(self.continuation_calls, [])

    async def test_completed_successor_cannot_continue_under_predecessor_wait(self):
        runner = await self.start_ordinary()
        next_native = self.turn.next_notification_with_sequence
        successor_read = asyncio.Event()
        release_successor = asyncio.Event()

        async def gated_next(*args, **kwargs):
            sequence, packet = await next_native(*args, **kwargs)
            if (
                packet.get("method") == "turn/started"
                and packet.get("params", {}).get("turnId") == "turn-2"
            ):
                successor_read.set()
                await release_successor.wait()
            return sequence, packet

        self.turn.next_notification_with_sequence = gated_next
        self.spawn()
        self.completed()  # A finishes with an active child.
        self.child_completed()
        self.route("turn/started", turn_id="turn-2", turn={"id": "turn-2", "status": "inProgress"})
        self.answer("B already collected the child", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(successor_read.wait(), 5)
        await self.settle_callbacks()
        # The native handle already represents completed B, but the consumer
        # is still waiting on A. A's child-drain signal cannot authorize C.
        self.turn.continue_after_subagents.assert_not_awaited()
        self.assertFalse(runner.done())
        release_successor.set()
        await asyncio.wait_for(runner, 5)
        self.turn.continue_after_subagents.assert_not_awaited()
        self.assertEqual(self.ns["finalize_owned_turn_finished"].await_args.kwargs["payload"]["result_text"],
                         "B already collected the child")

    async def wait_with_child(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        return runner

    async def finish_second_turn(self, runner):
        self.route("turn/started", turn_id="turn-2", turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Consolidated safely", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(runner, 5)

    async def test_new_child_before_wire_send_allows_retry_only_after_its_drain(self):
        runner = await self.wait_with_child()
        entered, release = asyncio.Event(), asyncio.Event()
        accept = self.turn.continue_after_subagents.side_effect
        async def blocked(**kwargs):
            entered.set()
            await release.wait()
            return await accept(**kwargs)
        self.turn.continue_after_subagents.side_effect = blocked
        self.child_completed()
        await asyncio.wait_for(entered.wait(), 5)
        self.spawn("child-2")
        release.set()
        await self.settle_callbacks()
        self.assertEqual(self.continuation_calls, [])
        self.child_completed("child-2")
        await self.wait(lambda: len(self.continuation_calls) == 1)
        await self.finish_second_turn(runner)
        self.assertEqual(self.turn.continue_after_subagents.await_count, 2)

    async def test_stop_before_wire_send_never_continues_or_retires_generation(self):
        runner = await self.wait_with_child()
        accept = self.turn.continue_after_subagents.side_effect
        async def stopped(**kwargs):
            self.active["stop_requested"] = True
            self.ns["STOPPED_RUNS"].add("operation")
            self.active["codex_child_continuation_stop"].set()
            return await accept(**kwargs)
        self.turn.continue_after_subagents.side_effect = stopped
        self.child_completed()
        await asyncio.wait_for(runner, 5)
        self.assertEqual(self.continuation_calls, [])
        self.manager.retire_generation.assert_not_awaited()
        self.client.interrupt_turn.assert_not_awaited()

    async def test_goal_activation_at_wire_guard_does_not_start_ordinary_continuation(self):
        runner = await self.wait_with_child()
        accept = self.turn.continue_after_subagents.side_effect
        async def activated(**kwargs):
            self.session["codex_goal"] = {"id": "goal", "status": "active"}
            result = await accept(**kwargs)
            self.assertIsNone(result)
            self.active["stop_requested"] = True
            self.ns["STOPPED_RUNS"].add("operation")
            self.active["codex_child_continuation_stop"].set()
            return result
        self.turn.continue_after_subagents.side_effect = activated
        self.child_completed()
        await asyncio.wait_for(runner, 5)
        self.assertEqual(self.continuation_calls, [])

    async def test_lost_ack_with_already_routed_new_turn_never_retires_or_replays(self):
        runner = await self.wait_with_child()
        async def lost_ack(**kwargs):
            self.route("turn/started", turn_id="turn-2", turn={"id": "turn-2", "status": "inProgress"})
            raise CodexAppServerTimeout("turn/start", 1, request_sent=True)
        self.turn.continue_after_subagents.side_effect = lost_ack
        self.child_completed()
        await self.wait(lambda: self.active.get("provider_turn_id") == "turn-2")
        await self.finish_second_turn(runner)
        self.manager.list_turns.assert_not_awaited()
        self.manager.retire_generation.assert_not_awaited()
        self.client.interrupt_turn.assert_not_awaited()
        self.turn.continue_after_subagents.assert_awaited_once()

    async def test_lost_ack_waits_for_late_native_turn_without_replay(self):
        runner = await self.wait_with_child()
        self.turn.continue_after_subagents.side_effect = CodexAppServerTimeout("turn/start", 1, request_sent=True)
        self.child_completed()
        await self.wait(lambda: self.turn.continue_after_subagents.await_count == 1)
        await self.settle_callbacks()
        self.assertFalse(runner.done())
        await self.finish_second_turn(runner)
        self.manager.list_turns.assert_not_awaited()
        self.manager.retire_generation.assert_not_awaited()
        self.client.interrupt_turn.assert_not_awaited()
        self.turn.continue_after_subagents.assert_awaited_once()

    async def test_ambiguous_source_cannot_misbind_old_parent_with_same_run_metadata(self):
        runner = await self.wait_with_child()
        self.ns["CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS"] = 0.01
        self.manager.list_turns.return_value = [{
            "id": "turn-1", "status": "completed", "clientUserMessageId": "operation",
            "startedAt": time.time(), "items": [],
        }]
        self.turn.continue_after_subagents.side_effect = CodexAppServerTimeout("turn/start", 1, request_sent=True)
        self.child_completed()
        await asyncio.wait_for(runner, 5)
        self.manager.list_turns.assert_not_awaited()
        self.manager.retire_generation.assert_awaited_once_with(1)
        self.client.interrupt_turn.assert_not_awaited()
        self.turn.continue_after_subagents.assert_awaited_once()
        self.assertEqual(self.ns["finalize_owned_turn_finished"].await_args.kwargs["payload"]["exit_code"], 1)

    async def test_cancelled_sent_continuation_retains_stream_until_late_turn_is_interrupted(self):
        runner = await self.wait_with_child()
        entered = asyncio.Event()
        async def cancel_after_sent(**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                exc.request_sent = True
                raise
        self.turn.continue_after_subagents.side_effect = cancel_after_sent
        self.child_completed()
        await asyncio.wait_for(entered.wait(), 5)
        runner.cancel()
        await self.settle_callbacks()
        self.assertFalse(self.turn._closed)
        self.route("turn/started", turn_id="turn-2", turn={"id": "turn-2", "status": "inProgress"})
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.client.interrupt_turn.assert_awaited_once_with("thread", "turn-2")
        self.manager.retire_generation.assert_not_awaited()
        self.assertTrue(self.turn._closed)

    async def test_cancelled_sent_native_turn_already_completed_does_not_interrupt_or_retire(self):
        runner = await self.wait_with_child()
        entered = asyncio.Event()
        async def cancel_after_sent(**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                exc.request_sent = True
                raise
        self.turn.continue_after_subagents.side_effect = cancel_after_sent
        self.child_completed()
        await asyncio.wait_for(entered.wait(), 5)
        self.route("turn/started", turn_id="turn-2", turn={"id": "turn-2", "status": "inProgress"})
        self.completed(turn_id="turn-2")
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.client.interrupt_turn.assert_not_awaited()
        self.manager.retire_generation.assert_not_awaited()
        self.assertTrue(self.turn._closed)
        self.assertTrue(self.turn._completed)

    async def test_lost_ack_already_buffered_terminal_preserves_native_answer_and_finishes(self):
        runner = await self.wait_with_child()
        async def lost_ack(**kwargs):
            self.route("turn/started", turn_id="turn-2", turn={"id": "turn-2", "status": "inProgress"})
            self.answer("Native consolidation", turn_id="turn-2")
            self.completed(turn_id="turn-2")
            raise CodexAppServerTimeout("turn/start", 1, request_sent=True)
        self.turn.continue_after_subagents.side_effect = lost_ack
        self.child_completed()
        await asyncio.wait_for(runner, 5)
        self.manager.retire_generation.assert_not_awaited()
        self.client.interrupt_turn.assert_not_awaited()
        self.assertEqual(self.ns["finalize_owned_turn_finished"].await_args.kwargs["payload"]["result_text"],
                         "Native consolidation")
        self.assertEqual([row["item_id"] for kind, row in self.events if kind == "assistant_text"],
                         ["answer-turn-2"])

    async def test_zero_byte_cancel_does_not_reconcile_or_retire(self):
        runner = await self.wait_with_child()
        entered = asyncio.Event()
        async def cancel_before_sent(**kwargs):
            entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError as exc:
                exc.request_sent = False
                raise
        self.turn.continue_after_subagents.side_effect = cancel_before_sent
        self.child_completed()
        await asyncio.wait_for(entered.wait(), 5)
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await runner
        self.manager.list_turns.assert_not_awaited()
        self.manager.retire_generation.assert_not_awaited()
        self.client.interrupt_turn.assert_not_awaited()

    async def test_ambiguous_continuation_cannot_retire_or_interrupt_successor(self):
        runner = await self.wait_with_child()
        successor = {**self.active, "run_id": "successor"}
        self.turn.continue_after_subagents.side_effect = CodexAppServerTimeout("turn/start", 1, request_sent=True)
        self.child_completed()
        await self.wait(lambda: self.turn.continue_after_subagents.await_count == 1)
        self.ns["ACTIVE"]["chat"] = successor
        self.current["run_id"] = "successor"
        self.route("turn/started", turn_id="turn-3", turn={"id": "turn-3", "status": "inProgress"})
        await asyncio.wait_for(runner, 5)
        self.assertIs(self.ns["ACTIVE"]["chat"], successor)
        self.manager.retire_generation.assert_not_awaited()
        self.client.interrupt_turn.assert_not_awaited()

    async def test_v2_completed_activity_does_not_revive_finished_child(self):
        runner = await self.start_ordinary()
        item = {"id": "v2-spawn", "type": "subAgentActivity", "kind": "started", "agentThreadId": "child-1"}
        self.route("item/started", item=item)
        self.route("turn/started", thread="child-1", turn_id="child-turn")
        self.route("turn/completed", thread="child-1", turn_id="child-turn",
                   turn={"id": "child-turn", "status": "completed"})
        self.route("item/completed", item=item)
        self.route("item/completed", item={**item, "id": "v2-message", "kind": "interacted"})
        self.answer("Finished")
        self.completed()
        await asyncio.wait_for(runner, 5)
        self.assertEqual(self.continuation_calls, [])

    async def test_late_duplicate_terminal_cannot_finish_followup(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.completed()
        await self.wait(lambda: self.active.get("codex_child_continuation_waiting"))
        self.child_completed()
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.completed()  # Late duplicate of turn-1, while turn-2 runs.
        self.route("turn/started", turn_id="turn-1", turn={"id": "turn-1", "status": "inProgress"})
        self.answer("Second turn still owns the result", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        self.assertEqual(self.ns["finalize_owned_turn_finished"].await_args.kwargs["payload"]["provider_turn_id"],
                         "turn-2")

    def prepare_stopped_projection(self):
        self.active["stop_requested"] = True
        self.ns["STOPPED_RUNS"].add("operation")
        self.ns.update({
            "codex_session_id_for_thread": lambda thread: "chat",
            "session_codex_thread_id": lambda session: "thread",
            "CODEX_QUARANTINED_GOAL_THREADS": {}, "CODEX_APP_SERVER_MANAGER": self.manager,
            "CODEX_GOAL_CONTROL_TIMEOUT_SECONDS": 1,
        })
        self.interrupt_writes = []
        async def guarded_request(method, params, **kwargs):
            if kwargs["before_send"]():
                self.interrupt_writes.append((method, params))
        self.client._request_connected = AsyncMock(side_effect=guarded_request)

    async def test_stopped_projection_ignores_stale_B_after_live_C_is_bound(self):
        runner = await self.wait_with_child()
        self.prepare_stopped_projection()
        self.route("turn/started", turn_id="turn-3", turn={"id": "turn-3", "status": "inProgress"})
        self.active["provider_turn_id"] = "turn-3"
        self.active["native_interrupt_sent"] = False
        await self.ns["project_codex_notification"]({"method": "turn/started", "params": {
            "threadId": "thread", "turnId": "turn-2", "turn": {"id": "turn-2"},
        }})
        self.assertEqual(self.active["provider_turn_id"], "turn-3")
        self.assertFalse(self.active["native_interrupt_sent"])
        self.assertEqual(self.interrupt_writes, [])
        self.completed(turn_id="turn-3", status="interrupted")
        await asyncio.wait_for(runner, 5)

    async def test_stopped_projection_interrupts_exact_live_turn_and_checks_write_boundary(self):
        runner = await self.wait_with_child()
        self.prepare_stopped_projection()
        self.route("turn/started", turn_id="turn-2", turn={"id": "turn-2", "status": "inProgress"})
        await self.ns["project_codex_notification"]({"method": "turn/started", "params": {
            "threadId": "thread", "turnId": "turn-2", "turn": {"id": "turn-2"},
        }})
        self.assertEqual(self.interrupt_writes, [("turn/interrupt", {"threadId": "thread", "turnId": "turn-2"})])
        self.completed(turn_id="turn-2", status="interrupted")
        await asyncio.wait_for(runner, 5)

    async def test_stopped_projection_keeps_closed_handle_cleanup_gap_fenced(self):
        runner = await self.wait_with_child()
        self.prepare_stopped_projection()
        await self.turn.close()
        await self.ns["project_codex_notification"]({"method": "turn/started", "params": {
            "threadId": "thread", "turnId": "turn-2", "turn": {"id": "turn-2"},
        }})
        self.assertEqual(self.interrupt_writes, [("turn/interrupt", {"threadId": "thread", "turnId": "turn-2"})])
        self.active["codex_child_continuation_stop"].set()
        await asyncio.gather(runner, return_exceptions=True)

    async def test_actual_stop_admission_accepts_between_turn_wait_but_not_startup(self):
        runner = await self.wait_with_child()
        class AdmissionChecked(Exception):
            pass
        namespace = dict(self.ns)
        exec(PROJECTION_CODE, namespace)
        namespace.update({
            "SESSION_TURN_TASKS": {}, "CODEX_NATIVE_ACTION_TASKS": {},
            "empty_subagent_stop_result": Mock(side_effect=AdmissionChecked),
        })
        with self.assertRaises(AdmissionChecked):
            await namespace["stop_turn"]("chat", expected_run_id="operation",
                require_provider_turn_ready=True, cascade_codex_subagents=False)
        self.assertTrue(self.active["stop_requested"])
        self.assertTrue(self.active["codex_child_continuation_stop"].is_set())
        self.assertIn("operation", self.ns["STOPPED_RUNS"])
        self.assertFalse(self.active.get("native_interrupt_sent", False))
        await asyncio.wait_for(runner, 5)

        startup = dict(self.active)
        startup.pop("codex_child_continuation_waiting", None)
        startup.pop("codex_child_continuation_stop", None)
        startup.pop("stop_requested", None)
        startup["provider_turn_ready"] = False
        self.ns["ACTIVE"]["chat"] = startup
        with self.assertRaises(AdmissionChecked):
            await namespace["stop_turn"]("chat", expected_run_id="operation", require_provider_turn_ready=True)
        self.assertNotIn("stop_requested", startup)

    async def test_stop_skips_native_interrupt_only_for_explicit_completed_true(self):
        class AdmissionChecked(Exception):
            pass

        namespace = dict(self.ns)
        exec(PROJECTION_CODE, namespace)
        namespace.update({
            "SESSION_TURN_TASKS": {}, "CODEX_NATIVE_ACTION_TASKS": {},
            "empty_subagent_stop_result": Mock(side_effect=AdmissionChecked),
        })
        for label, native_turn, should_interrupt in (
            ("missing", SimpleNamespace(turn_id="turn-1"), True),
            ("false", SimpleNamespace(turn_id="turn-1", _completed=False), True),
            ("true", SimpleNamespace(turn_id="turn-1", _completed=True), False),
            ("unset mock", Mock(turn_id="turn-1"), True),
        ):
            with self.subTest(completion=label):
                active = {
                    "run_id": "operation", "backend": "codex", "transport": "app_server",
                    "provider_turn_ready": True, "provider_session_id": "thread",
                    "codex_app_server_turn": native_turn,
                }
                namespace["ACTIVE"]["chat"] = active
                with self.assertRaises(AdmissionChecked):
                    await namespace["stop_turn"]("chat", expected_run_id="operation")
                self.assertEqual(active.get("native_interrupt_sent", False), should_interrupt)

    async def test_snapshot_does_not_serialize_private_continuation_event(self):
        runner = await self.wait_with_child()
        snapshot = self.ns["active_snapshot_input"](self.active)
        self.assertNotIn("codex_child_continuation_stop", snapshot)
        self.active["stop_requested"] = True
        self.active["codex_child_continuation_stop"].set()
        await asyncio.wait_for(runner, 5)

    async def test_stop_fence_wins_over_pending_child_and_buffered_parent_start(self):
        runner = await self.start_ordinary()
        self.spawn()
        self.active["stop_requested"] = True
        self.ns["STOPPED_RUNS"].add("operation")
        self.completed(status="interrupted")
        self.child_completed()
        self.route("turn/started", turn_id="turn-2",
                   turn={"id": "turn-2", "status": "inProgress"})
        self.answer("Must not revive stopped work", turn_id="turn-2")
        self.completed(turn_id="turn-2")
        await asyncio.wait_for(runner, 5)
        self.assertFalse(any(kind == "assistant_text" for kind, _ in self.events))
        self.ns["finalize_owned_turn_finished"].assert_awaited_once()
        self.assertTrue(self.turn._closed)
        self.manager.start_turn.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
