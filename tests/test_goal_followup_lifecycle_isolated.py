"""Real ordinary Codex runner/admission AST with in-memory provider transport.

Run with the guarded temporary-state runner. Never import agent_server.
"""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import codex_provider
from codex_app_server import CodexAppServerDisconnected
from tests.test_goal_followup_admission_isolated import AdmissionHTTPException, saved_route_snapshots
from tests import test_goal_native_steer_isolated as native_goal_fixture


SOURCE = (Path(__file__).resolve().parents[1] / "agent_server.py")
NAMES = {
    "run_codex_app_server", "retain_codex_goal_run_owner", "bind_active_turn",
    "turn_slot_is_owned", "codex_goal_followup_requires_native",
    "_run_queued_turn_now_once", "NonNativeForceSendRequiresLifecycleLock",
    "async_route_queue_fields", "await_native_steer_result",
    "withdraw_unaccepted_native_steer",
}
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
nodes = [node for node in tree.body if isinstance(node, (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
)) and node.name in NAMES]
assert {node.name for node in nodes} == NAMES
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
    module="__future__", names=[ast.alias(name="annotations")], level=0,
), *nodes], type_ignores=[])), str(SOURCE), "exec")
del tree, nodes


class MemorySubscription:
    def __init__(self):
        self.turn_id = "turn-1"
        self.transport_generation = 1
        self._last_enqueued_sequence = 0
        self._closed = False
        self.queue = asyncio.Queue()
        self.interrupt = AsyncMock()
        self.close_calls = 0

    def push(self, method, *, provider_turn="turn-1", **params):
        self._last_enqueued_sequence += 1
        self.queue.put_nowait((self._last_enqueued_sequence, {
            "method": method, "params": {"threadId": "thread", "turnId": provider_turn, **params},
        }))

    async def next_notification_with_sequence(self, timeout=None):
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)

    def close(self):
        self._closed = True
        self.close_calls += 1


class MemoryTurn:
    def __init__(self):
        self.thread_id = "thread"
        self.turn_id = "turn-1"
        self.transport_generation = 1
        self._subscription = MemorySubscription()
        self._closed = False
        self._completed = False
        self.interrupt = AsyncMock()
        self.close_calls = 0

    def push(self, *args, **kwargs):
        self._subscription.push(*args, **kwargs)

    async def next_notification_with_sequence(self, timeout=None):
        return await self._subscription.next_notification_with_sequence(timeout)

    async def close(self):
        self._closed = True
        self.close_calls += 1
        self._subscription.close()


class GoalFollowupLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse the existing isolated native-consumer globals and actual
        # send/commit/fence/requeue helpers, never its production-import suite.
        native_goal_fixture.NativeGoalSteerTests.setUp(self)
        exec(CODE, self.ns)
        temporary = tempfile.TemporaryDirectory(prefix="goal-followup-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session = self.ns["STORE"].sessions["chat"]
        self.session.update(cwd=str(self.root), codex_thread_id="thread", session_id="thread")
        self.current.clear()
        self.current.update(run_id="operation", prompt="Original goal request", file_ids=[], backend="codex")
        self.ns["ACTIVE"].clear()
        self.turn = MemoryTurn()
        self.subscription = self.turn._subscription
        self.manager.start = AsyncMock()
        self.manager.start_turn = AsyncMock(return_value=self.turn)
        self.manager.client = SimpleNamespace(unmatched_notifications=[])
        self.manager.retire_generation = AsyncMock()
        self.authority = object()
        self.tasks = []
        self.requests = []
        self.before_wait = None

        async def watch(*_args, **_kwargs):
            await asyncio.Event().wait()

        async def append_event(chat, kind, payload):
            self.events.append((kind, deepcopy(payload)))
            return {"seq": len(self.events)}

        async def append_batch(chat, specs):
            return [await append_event(chat, kind, payload) for kind, payload in specs]

        real_wait = self.ns["await_native_steer_result"]
        async def observe_wait(*args, **kwargs):
            self.requests.append(kwargs["request"])
            if self.before_wait is not None:
                self.before_wait(kwargs["request"])
            return await real_wait(*args, **kwargs)

        self.ns.update({
            "codex_provider": codex_provider,
            "CODEX_PROVIDER_STORE": SimpleNamespace(for_session=lambda session: None),
            "Path": Path, "CodexAppServerDisconnected": CodexAppServerDisconnected,
            "HTTPException": AdmissionHTTPException, "BACKEND_CLAUDE": "claude",
            "DEFAULT_BACKEND": "claude", "CODEX_TRANSPORT_APP_SERVER": "app_server",
            "CLAUDE_TRANSPORT_AGENT_SDK": "agent_sdk", "CODEX_BIN": "synthetic-provider",
            "CODEX_DEFAULT_SANDBOX_MODE": "workspace-write",
            "CODEX_SANDBOX_POLICY_TYPES": {"workspace-write": "workspaceWrite"},
            "CODEX_NONINTERACTIVE_APPROVAL_POLICY": "never",
            "CODEX_DEFAULT_APPROVAL_POLICY": "never", "CODEX_APPROVAL_POLICIES": {"never"},
            "LIVE_STDOUT_MAX_LINES": 10, "CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS": 4096,
            "CODEX_APP_SERVER_FIRST_ACTIVITY_TIMEOUT_SECONDS": 30,
            "RUN_NOW_PROVIDER_HANDOFF_TIMEOUT_SECONDS": 5,
            "CROSS_CHAT_DELIVERY_PURPOSES": {"local_delivery", "peer_delivery"},
            "LOCAL_CROSS_CHAT_DELIVERY_PURPOSE": "local_delivery",
            "CROSS_CHAT_CAPABILITIES": {"operation": self.authority},
            "RUN_METADATA": {"operation": {"synthetic_authority": "original"}},
            "RUN_NOW_TURNS": {}, "STEERING_SESSIONS": set(), "STEERING_WAIT_TASKS": {},
            "validate_provider_runtime_env": lambda value: dict(value or {}),
            "existing_cwd": lambda value: value, "session_provider_id": lambda value: value.get("codex_thread_id"),
            "codex_runtime_settings": lambda value: (None, None, None),
            "now_iso": lambda: "2026-09-13T10:00:00Z",
            "run_event_metadata": lambda run: self.ns["RUN_METADATA"].get(run, {}),
            "codex_app_server_manager": AsyncMock(return_value=self.manager),
            "existing_codex_app_server_manager": lambda session=None: self.manager,
            "existing_codex_app_server_manager_for_thread": lambda thread: self.manager,
            "codex_app_server_managers": lambda: (self.manager,),
            "acquire_codex_run_thread": AsyncMock(return_value="thread"),
            "persist_run_provider_session": AsyncMock(return_value=True),
            "codex_provider_mcp_run_proof": Mock(return_value="synthetic-original-proof"),
            "codex_provider_command_turn_input": lambda prompt, command: [{"type": "text", "text": prompt}],
            "watch_manifest_artifacts": watch, "append_event": append_event,
            "append_durable_event": append_event, "append_durable_event_batch": append_batch,
            "await_native_steer_result": observe_wait,
            "managed_server_update_admission_blocker": Mock(return_value=None),
            "stop_cleanup_in_progress": Mock(return_value=False),
            "force_send_conflict_detail": Mock(side_effect=lambda *_args, **kwargs: kwargs),
            "queued_claude_runtime_matches_active": Mock(return_value=False),
            "queued_codex_runtime_matches_active": Mock(return_value=True),
            "build_user_provider_prompt": Mock(side_effect=lambda chat, prompt, files: prompt + (" [synthetic attachment context]" if files else "")),
            "should_recover_codex_resume": Mock(return_value=False),
            "collect_manifest": AsyncMock(), "collect_recent_leftover_manifests": AsyncMock(),
            "publish_turn_code_diff": AsyncMock(), "record_runtime_success": Mock(),
            "record_runtime_failure": Mock(), "finalize_owned_turn_finished": AsyncMock(return_value=True),
            "unpin_codex_app_server_thread": AsyncMock(), "touch_codex_app_server_thread": AsyncMock(),
            "reconcile_provider_task_exit": AsyncMock(), "register_codex_native_action": Mock(),
            "schedule_codex_subagent_finalization": Mock(), "compact_memory_text": lambda value, limit: value[:limit],
            "maybe_notify_chat_mailbox_codex": AsyncMock(),
            "logger": Mock(),
        })
        self.forbidden = {}
        for name in ("stop_turn", "pause_active_codex_goal_for_stop", "issue_cross_chat_capability",
                     "revoke_cross_chat_capability", "run_codex_exec", "capture_git_baseline"):
            self.ns[name] = self.forbidden[name] = AsyncMock(side_effect=AssertionError(name))
        for name in ("schedule_next_queued_turn", "prepare_steered_turn", "schedule_steered_turn_slot_waiter"):
            self.ns[name] = self.forbidden[name] = Mock(side_effect=AssertionError(name))

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for request in self.requests:
            future = request["future"]
            if future.done() and not future.cancelled():
                future.exception()

    async def wait(self, predicate):
        async def poll():
            while not predicate():
                if self.tasks and self.tasks[0].done():
                    await self.tasks[0]
                    self.fail(f"Runner exited before condition: {self.events!r}")
                await asyncio.sleep(0)
        await asyncio.wait_for(poll(), 5)

    async def start(self, *, runtime_authority=True):
        runner = asyncio.create_task(self.ns["run_codex_app_server"](
            "chat", "operation", "Original goal request", self.session,
            self.root / "manifest.json", allow_exec_fallback=False,
            diff_baseline={"head": "synthetic"},
            provider_runtime_env={"AGENTSDOCK_PROVIDER_RUN_ID": "operation"} if runtime_authority else None,
        ))
        self.tasks.append(runner)
        await self.wait(lambda: (self.ns["ACTIVE"].get("chat") or {}).get("provider_turn_ready"))
        self.active = self.ns["ACTIVE"]["chat"]
        if runtime_authority:
            self.assertIsNone(self.active["native_steer_queue"])
        else:
            self.assertIsInstance(self.active["native_steer_queue"], asyncio.Queue)
        self.assertIsInstance(self.active["codex_goal_steer_queue"], asyncio.Queue)
        return runner

    def followup(self, **extra):
        selected = {"queued_id": f"queued-{len(self.requests) + 1}", "backend": "codex",
            "prompt": "Please check this too", "file_ids": [],
            "client_capabilities": ["codex_goal_steer_v1"], **extra}
        self.ns["QUEUED_TURNS"]["chat"] = deque([selected])
        task = asyncio.create_task(self.ns["_run_queued_turn_now_once"]("chat", selected["queued_id"], require_native=True))
        self.tasks.append(task)
        return task, selected

    def assert_original_owner(self):
        self.assertIs(self.ns["ACTIVE"]["chat"], self.active)
        self.assertIs(self.ns["CURRENT_TURNS"]["chat"], self.current)
        self.assertEqual(self.active["run_id"], "operation")
        self.assertEqual(self.current["run_id"], "operation")
        self.assertEqual(self.current["prompt"], "Original goal request")
        self.assertEqual(self.ns["RUN_METADATA"], {"operation": {"synthetic_authority": "original"}})
        self.assertIs(self.ns["CROSS_CHAT_CAPABILITIES"]["operation"], self.authority)
        self.assertEqual(self.goal["status"], "active")
        self.assertEqual(self.manager.start_turn.await_count, 1)
        for call in self.forbidden.values():
            call.assert_not_called()
        self.turn.interrupt.assert_not_awaited()
        self.manager.request.assert_not_awaited()
        self.ns["stop_codex_goal_resume"].assert_not_awaited()
        self.assertFalse({kind for kind, _ in self.events} & {"turn_started", "turn_stopped", "turn_finished"})

    async def finish_runner(self, runner, *, turn="turn-1"):
        self.goal["status"] = "complete"
        self.turn.push("turn/completed", provider_turn=turn, turn={"id": turn, "status": "completed"})
        await asyncio.wait_for(runner, 5)

    async def test_initial_goal_turn_with_runtime_authority_steers_same_run_once(self):
        runner = await self.start()
        self.turn.push("item/completed", item={"id": "before", "type": "agentMessage", "phase": "commentary", "text": "Before follow-up"})
        self.after_ack = True
        task, _ = self.followup()
        result = await asyncio.wait_for(task, 5)
        await self.wait(lambda: any(payload.get("text") == "after" for _, payload in self.events))
        self.assert_original_owner()
        self.assertEqual((result["run_id"], result["interrupted"], result["native_goal_steer"]), ("operation", False, True))
        visible = [(kind, row.get("text") or row.get("prompt")) for kind, row in self.events if kind in {"reasoning_summary", "turn_steered"}]
        self.assertEqual(visible, [("reasoning_summary", "Before follow-up"), ("turn_steered", "Please check this too"), ("reasoning_summary", "after")])
        self.assertEqual(len(self.calls), 1)
        self.assertIs(self.calls[0][3]["notification_subscription"], self.turn._subscription)
        await self.finish_runner(runner)

    async def test_file_followup_uses_context_but_records_only_display_files(self):
        runner = await self.start()
        task, _ = self.followup(file_ids=["synthetic-private-file"], display_file_ids=["synthetic-visible-file"])
        await asyncio.wait_for(task, 5)
        self.ns["build_user_provider_prompt"].assert_called_once_with("chat", "Please check this too", ["synthetic-private-file"])
        self.assertIn("synthetic attachment context", self.calls[0][2][0]["text"])
        event = next(row for kind, row in self.events if kind == "turn_steered")
        self.assertEqual(event["file_ids"], ["synthetic-visible-file"])
        self.assert_original_owner()
        await self.finish_runner(runner)

    async def test_scheduled_goal_and_continuation_ignore_automatic_saved_routes(self):
        self.current["purpose"] = "scheduled_job"
        owner_routes = self.current["provider_cross_chat_route_snapshot"] = saved_route_snapshots()[:1]
        runner = await self.start()
        queued_routes = saved_route_snapshots()
        task, _ = self.followup(
            prompt="Status report!", provider_cross_chat_route_snapshot=queued_routes,
            provider_team_mail_route_snapshot=[{"route_id": "mail_" + "1" * 32, "revision": "rev_" + "2" * 32}],
        )
        result = await asyncio.wait_for(task, 5)
        self.assert_original_owner()
        self.assertEqual((result["run_id"], result["interrupted"]), ("operation", False))
        self.assertEqual(self.current["purpose"], "scheduled_job")
        self.assertIs(self.current["provider_cross_chat_route_snapshot"], owner_routes)
        self.assertEqual(self.calls[0][2][0]["text"], "Status report!")

        self.turn.push("turn/completed", turn={"id": "turn-1", "status": "completed"})
        await self.wait(lambda: self.active.get("codex_native_operation_kind") == "goal_resume")
        self.turn.push("turn/started", provider_turn="turn-2", turn={"id": "turn-2"})
        await self.wait(lambda: self.active.get("provider_turn_id") == "turn-2")
        task, _ = self.followup(
            prompt="Review this attachment", file_ids=["synthetic-private-file"],
            display_file_ids=["synthetic-visible-file"],
            provider_cross_chat_route_snapshot=queued_routes,
        )
        await asyncio.wait_for(task, 5)
        self.assert_original_owner()
        self.assertIs(self.current["provider_cross_chat_route_snapshot"], owner_routes)
        self.assertEqual([(row[0], row[1]) for row in self.calls], [("thread", "turn-1"), ("thread", "turn-2")])
        steers = [row for kind, row in self.events if kind == "turn_steered"]
        self.assertEqual([row["prompt"] for row in steers], ["Status report!", "Review this attachment"])
        self.assertEqual(steers[1]["file_ids"], ["synthetic-visible-file"])
        self.assertNotIn("provider_cross_chat_route_snapshot", steers[1])
        await self.finish_runner(runner, turn="turn-2")

    async def test_goal_created_after_admission_does_not_rotate_logical_authority(self):
        self.goal["status"] = "paused"
        runner = await self.start(runtime_authority=False)
        def activate_after_admission(request):
            self.assertIsNone(request["goal_identity"])
            self.goal["status"] = "active"
        self.before_wait = activate_after_admission
        task, _ = self.followup()
        result = await asyncio.wait_for(task, 5)
        self.assert_original_owner()
        self.assertEqual(result["run_id"], "operation")
        self.assertTrue(result["native_goal_steer"])
        self.assertFalse(result["interrupted"])
        self.assertEqual(self.requests[0]["goal_identity"], ("goal", "Complete the work"))
        self.assertEqual(len(self.calls), 1)
        await self.finish_runner(runner)

    async def test_native_continuation_retains_initial_authority_and_steers_once(self):
        runner = await self.start()
        self.turn.push("turn/completed", turn={"id": "turn-1", "status": "completed"})
        await self.wait(lambda: self.active.get("codex_native_operation_kind") == "goal_resume")
        self.turn.push("turn/started", provider_turn="turn-2", turn={"id": "turn-2"})
        await self.wait(lambda: self.active.get("provider_turn_id") == "turn-2")
        task, _ = self.followup()
        result = await asyncio.wait_for(task, 5)
        self.assert_original_owner()
        self.assertEqual(result["run_id"], "operation")
        self.assertEqual([(row[0], row[1]) for row in self.calls], [("thread", "turn-2")])
        self.assertEqual(sum(kind == "turn_steered" for kind, _ in self.events), 1)
        self.ns["register_codex_native_action"].assert_called_once_with("chat", "operation", runner)
        await self.finish_runner(runner, turn="turn-2")

    async def enter_native_gap_with_followup(self):
        runner = await self.start()
        self.turn.push("turn/completed", turn={"id": "turn-1", "status": "completed"})
        await self.wait(lambda: self.active.get("codex_native_operation_kind") == "goal_resume")
        self.assertFalse(self.active["provider_turn_ready"])
        self.assertIsNone(self.active["provider_turn_id"])
        task, selected = self.followup()
        await self.wait(lambda: bool(self.requests) and self.requests[0]["phase"] == "accepted")
        self.assertFalse(task.done())
        self.assertFalse(self.requests[0]["future"].done())
        self.assertEqual(self.requests[0]["expected_provider_turn_id"], "")
        self.assertEqual(self.calls, [])
        self.assert_original_owner()
        return runner, task, selected

    async def test_gap_followup_waits_on_existing_stream_then_steers_next_turn_once(self):
        runner, task, _ = await self.enter_native_gap_with_followup()
        self.turn.push("turn/started", provider_turn="turn-2", turn={"id": "turn-2"})
        result = await asyncio.wait_for(task, 5)
        self.assert_original_owner()
        self.assertEqual(self.requests[0]["expected_provider_turn_id"], "turn-2")
        self.assertEqual([(row[0], row[1]) for row in self.calls], [("thread", "turn-2")])
        self.assertEqual(result["run_id"], "operation")
        self.assertFalse(result["interrupted"])
        self.assertEqual(sum(kind == "turn_steered" for kind, _ in self.events), 1)
        self.assertIs(self.calls[0][3]["notification_subscription"], self.subscription)
        await self.finish_runner(runner, turn="turn-2")

    async def test_stop_fence_in_native_gap_settles_held_followup_without_send(self):
        runner, task, selected = await self.enter_native_gap_with_followup()
        # Model the existing explicit Stop's committed fence, not another
        # Stop request manufactured by admission or the follow-up consumer.
        self.active["stop_requested"] = True
        self.ns["STOPPED_RUNS"].add("operation")
        self.goal["status"] = "paused"
        with self.assertRaises(self.ns["NativeSteerHandoffError"]) as error:
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(runner, 5)
        self.assertTrue(error.exception.safe_to_requeue)
        self.assertFalse(error.exception.delivery_uncertain)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.requests[0]["expected_provider_turn_id"], "")
        self.assertEqual(sum(kind == "turn_steered" for kind, _ in self.events), 0)
        self.assertIs(self.ns["QUEUED_TURNS"]["chat"][0], selected)
        self.assertTrue(selected["_paused_after_stop"])
        self.assertNotIn("_native_delivery_fenced", selected)
        self.assertEqual(self.goal["status"], "paused")
        self.forbidden["stop_turn"].assert_not_awaited()
        self.forbidden["pause_active_codex_goal_for_stop"].assert_not_awaited()
        self.forbidden["schedule_next_queued_turn"].assert_not_called()
        self.assertIsNone(self.active["native_steer_queue"])

    async def test_writer_owner_change_requeues_held_without_sending_or_replacing_owner(self):
        runner = await self.start()
        async def change_owner():
            self.current["run_id"] = "successor"
        self.before_rpc = change_owner
        task, selected = self.followup()
        with self.assertRaises(self.ns["NativeSteerHandoffError"]) as error:
            await asyncio.wait_for(task, 5)
        self.assertTrue(error.exception.safe_to_requeue)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.current["run_id"], "successor")
        self.assertTrue(selected["_paused_after_stop"])
        self.assertIs(self.ns["QUEUED_TURNS"]["chat"][0], selected)
        self.turn.interrupt.assert_not_awaited()
        self.forbidden["revoke_cross_chat_capability"].assert_not_awaited()
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

    async def test_cancellation_after_delivery_starts_settles_once_without_successor_cleanup(self):
        runner = await self.start()
        entered = asyncio.Event()
        async def hold_writer():
            entered.set()
            await asyncio.Event().wait()
        self.before_rpc = hold_writer
        task, selected = self.followup()
        await asyncio.wait_for(entered.wait(), 5)
        successor_queue = asyncio.Queue()
        successor = {"run_id": "successor", "provider_turn_ready": True, "codex_goal_steer_queue": successor_queue}
        self.ns["ACTIVE"]["chat"] = successor
        self.ns["CURRENT_TURNS"]["chat"] = {"run_id": "successor"}
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        with self.assertRaises(self.ns["NativeSteerHandoffError"]):
            await asyncio.wait_for(task, 5)
        self.assertTrue(self.requests[0]["future"].done())
        self.assertIs(self.ns["ACTIVE"]["chat"], successor)
        self.assertIs(successor["codex_goal_steer_queue"], successor_queue)
        self.assertTrue(successor["provider_turn_ready"])
        self.assertEqual(self.calls, [])
        self.assertEqual(sum(kind == "turn_steered" for kind, _ in self.events), 0)
        self.ns["reconcile_provider_task_exit"].assert_awaited_once_with("chat", "operation", "codex")

    def ack_after_rollover(self, *, status="completed"):
        async def accepted_with_next_turn(thread, turn, items, **kwargs):
            self.assertTrue(kwargs["before_send"]())
            self.calls.append((thread, turn, items, kwargs))
            # These arrive after request bytes were written but before the
            # acknowledgement: the thread subscription already owns them.
            self.turn.push("turn/completed", turn={"id": "turn-1", "status": status})
            self.turn._completed = True
            self.turn.push("turn/started", provider_turn="turn-2", turn={"id": "turn-2"})
            self.turn.push("item/completed", provider_turn="turn-2", item={
                "id": "next-progress", "type": "agentMessage", "phase": "commentary",
                "text": "Native continuation already progressed",
            })
            return turn, self.subscription._last_enqueued_sequence
        self.manager.steer_turn_with_notification_watermark = accepted_with_next_turn

    async def test_ack_boundary_retains_already_started_next_turn_without_losing_readiness(self):
        runner = await self.start()
        self.ack_after_rollover()
        task, _ = self.followup()
        result = await asyncio.wait_for(task, 5)
        await self.wait(lambda: self.active.get("codex_native_operation_kind") == "goal_resume")
        await self.wait(lambda: any(row.get("item_id") == "next-progress" for _, row in self.events))
        self.assertTrue(self.active["provider_turn_ready"])
        self.assertEqual(self.active["provider_turn_id"], "turn-2")
        progress = next(row for kind, row in self.events if row.get("item_id") == "next-progress")
        self.assertEqual(progress["provider_turn_id"], "turn-2")
        self.assertEqual(progress["run_id"], "operation")
        self.assertEqual(result["run_id"], "operation")
        self.assertEqual(sum(kind == "turn_steered" for kind, _ in self.events), 1)
        self.assert_original_owner()
        await self.finish_runner(runner, turn="turn-2")
        self.forbidden["revoke_cross_chat_capability"].assert_not_awaited()

    async def test_cancel_during_accepted_handoff_settles_uncertain_without_replay(self):
        runner = await self.start()
        self.ack_after_rollover()
        handoff_entered = asyncio.Event()
        calls = 0
        async def hold_handoff(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                handoff_entered.set()
                await asyncio.Event().wait()
        self.manager.wait_for_notification_handler = AsyncMock(side_effect=hold_handoff)
        task, selected = self.followup()
        await asyncio.wait_for(handoff_entered.wait(), 5)
        self.assertFalse(task.done())
        runner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(runner, 5)
        with self.assertRaises(self.ns["NativeSteerHandoffError"]) as error:
            await asyncio.wait_for(task, 5)
        self.assertFalse(error.exception.safe_to_requeue)
        self.assertTrue(error.exception.delivery_uncertain)
        self.assertTrue(selected["_native_delivery_fenced"])
        self.assertFalse(self.ns["QUEUED_TURNS"].get("chat"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(kind == "turn_steered" for kind, _ in self.events), 0)
        self.assertTrue(self.turn._closed)
        self.ns["reconcile_provider_task_exit"].assert_awaited_once_with("chat", "operation", "codex")

    async def test_failed_terminal_at_ack_cannot_strand_or_replay_pending_followup(self):
        runner = await self.start()
        self.ns["should_recover_codex_resume"].side_effect = AssertionError("cannot replay the original prompt after accepted input")
        self.ack_after_rollover(status="failed")
        task, selected = self.followup()
        with self.assertRaises(self.ns["NativeSteerHandoffError"]) as error:
            await asyncio.wait_for(task, 5)
        await asyncio.wait_for(runner, 5)
        self.assertFalse(error.exception.safe_to_requeue)
        self.assertTrue(error.exception.delivery_uncertain)
        self.assertTrue(selected["_native_delivery_fenced"])
        self.assertFalse(self.ns["QUEUED_TURNS"].get("chat"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(kind == "turn_steered" for kind, _ in self.events), 0)
        self.assertTrue(self.turn._closed)
        self.ns["finalize_owned_turn_finished"].assert_awaited_once()
        self.assertEqual(self.ns["finalize_owned_turn_finished"].await_args.args[:2], ("chat", "operation"))
        self.ns["should_recover_codex_resume"].assert_not_called()

    async def test_uncertain_goal_steer_preserves_progress_but_cannot_replay_original_prompt(self):
        runner = await self.start()
        self.rpc_error = self.ns["CodexAppServerProtocolError"](
            "steer acknowledgement lost", request_sent=True, safe_to_retry=False,
        )
        self.ns["should_recover_codex_resume"].side_effect = AssertionError("cannot replay uncertain input")
        task, selected = self.followup()
        with self.assertRaises(self.ns["NativeSteerHandoffError"]) as error:
            await asyncio.wait_for(task, 5)
        self.assertTrue(error.exception.delivery_uncertain)
        self.assertTrue(selected["_native_delivery_fenced"])
        self.turn.push("item/completed", item={
            "id": "still-working", "type": "agentMessage", "phase": "commentary", "text": "Still working",
        })
        await self.wait(lambda: any(row.get("text") == "Still working" for _, row in self.events))
        self.assert_original_owner()
        await self.finish_runner(runner)
        self.ns["should_recover_codex_resume"].assert_not_called()
        self.assertEqual(len(self.calls), 1)

    async def test_repeated_cancel_detaches_goal_only_admission_and_settles_waiter(self):
        runner = await self.start()
        entered = asyncio.Event()
        async def hold_writer():
            entered.set()
            await asyncio.Event().wait()
        self.before_rpc = hold_writer
        task, _ = self.followup()
        await asyncio.wait_for(entered.wait(), 5)
        await self.ns["ACTIVE_LOCK"].acquire()
        try:
            runner.cancel()
            await asyncio.sleep(0)
            runner.cancel()
            await asyncio.sleep(0)
        finally:
            self.ns["ACTIVE_LOCK"].release()
        await asyncio.wait_for(asyncio.gather(runner, return_exceptions=True), 5)
        with self.assertRaises(self.ns["NativeSteerHandoffError"]):
            await asyncio.wait_for(task, 5)
        self.assertIsNone(self.active["codex_goal_steer_queue"])
        self.assertFalse(self.active["provider_turn_ready"])
        self.assertTrue(self.requests[0]["future"].done())
        self.assertEqual(self.calls, [])
        self.assertTrue(self.turn._closed)
        self.ns["reconcile_provider_task_exit"].assert_awaited_once_with("chat", "operation", "codex")


if __name__ == "__main__":
    unittest.main()
