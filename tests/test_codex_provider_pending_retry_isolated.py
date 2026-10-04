"""A completed shared transport operation must wake a saved per-chat handoff.

The older refresh fixture replaces the release proof with an always-successful
mock. These tests execute that proof and the real client completion boundaries,
without launching a provider or changing any user sessions.
"""
from __future__ import annotations

import ast
import asyncio
import copy
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from codex_app_server import CodexAppServerClient, CodexAppServerRequestError
from tests.test_codex_provider_sessions_isolated import make_namespace

SOURCE = Path(__file__).resolve().parents[1] / "agent_server.py"
NAMES = {
    "codex_manager_work_belongs_to_session", "codex_manager_session_blocker", "codex_login_handoff_wait", "watch_codex_login_handoff_blockers",
    "schedule_codex_subagent_limit_application", "apply_pending_codex_subagent_limit",
    "codex_manager_has_callers", "codex_manager_has_callbacks", "codex_manager_session_busy",
    "codex_manager_work_belongs_to_session",
    "release_idle_codex_manager_session", "release_codex_provider_writers",
    "watch_codex_provider_handoff_blockers",
    "schedule_codex_manager_drain", "join_task_despite_caller_cancellation",
}


class PendingProviderCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="provider-pending-retry-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ns = make_namespace(self.root)
        tree = ast.parse(SOURCE.read_text())
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
        self.assertEqual({node.name for node in nodes}, NAMES)
        module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[]))
        self.ns.update(
            time=time, CODEX_SUBAGENT_LIMIT_TASKS={}, CODEX_SUBAGENT_LIMIT_REQUESTED=set(),
            CODEX_MANAGER_CLOSING=False, DELETING_SESSIONS=set(), CODEX_NATIVE_ACTION_TASKS={},
            CODEX_INTERACTION_HANDLER_TASKS={}, CODEX_PENDING_INTERACTIONS={},
            CODEX_APP_SERVER_PINNED_THREADS=set(), CODEX_INTERACTIVE_CONTROL_THREADS=set(),
            CODEX_RETIRED_APP_SERVER_MANAGERS=[], CODEX_MANAGER_DRAIN_TASK=None,
            CODEX_MANAGER_DRAIN_REQUESTED=False, CODEX_SESSION_APP_SERVER_MANAGERS={},
            existing_cwd=lambda path: path, retain_codex_manager_caller=Mock(),
            pin_codex_app_server_thread=AsyncMock(), unpin_codex_app_server_thread=AsyncMock(),
            apply_codex_subagent_limit_when_idle=AsyncMock(return_value=False),
            CodexAppServerRequestError=CodexAppServerRequestError,
        )
        exec(compile(module, str(SOURCE), "exec"), self.ns)
        self.ns["CODEX_PROVIDER_STORE"].save({"base_url": "https://synthetic.invalid/v1", "api_key": "synthetic-key", "model": "synthetic-model"})
        self.chat = await self.ns["STORE"].create(self.ns["CreateSessionRequest"](codex_provider="custom"))
        self.sid = self.chat["id"]
        self.goal = {"status": "blocked", "objective": "Keep the exact existing goal"}
        self.chat.update(codex_thread_id="target-thread", session_id="target-thread", codex_goal=copy.deepcopy(self.goal))
        self.ns["CODEX_PROVIDER_STORE"].record_thread("target-thread", self.ns["CODEX_PROVIDER_STORE"].for_session(self.chat))
        self.peer = await self.ns["STORE"].create(self.ns["CreateSessionRequest"](codex_provider="custom"))
        self.peer.update(codex_thread_id="peer-thread", session_id="peer-thread")
        self.peer_before = copy.deepcopy(self.peer)
        self.ns["BUSY_SESSIONS"].add(self.peer["id"])
        self.peer_turn = SimpleNamespace(_completed=False)
        self.client = CodexAppServerClient("unused", cwd=str(self.root), env_factory=lambda: {})
        # The incident's target is unsubscribed locally but its native writer
        # is still visible to thread/loaded/list. The peer continues running.
        self.client._loaded_threads = {"peer-thread"}
        self.client._turns_by_thread["peer-thread"] = self.peer_turn
        async def request(method, params):
            if method == "thread/loaded/list":
                return {"data": ["target-thread", "peer-thread"], "nextCursor": None}
            return {}
        self.manager = SimpleNamespace(
            client=self.client, generation=1, ready=True,
            _agentsdock_provider_revision=self.chat["codex_provider_revision"], _agentsdock_callers={},
            is_thread_loaded=lambda thread: thread in self.client._loaded_threads,
            active_turn=lambda thread: self.client._turns_by_thread.get(thread),
            request=AsyncMock(side_effect=request), get_thread_goal=AsyncMock(return_value=copy.deepcopy(self.goal)),
            list_descendant_threads=AsyncMock(return_value=[]),
            list_background_terminals=AsyncMock(return_value=[]), close=AsyncMock(),
        )
        self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"].update({self.sid: self.manager, self.peer["id"]: self.manager})
        self.ns["existing_codex_app_server_manager"] = lambda session: self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"].get(session["id"])
        self.ns["codex_session_id_for_thread"] = lambda thread: {"target-thread": self.sid, "peer-thread": self.peer["id"]}.get(thread)
        self.ns["evict_codex_app_server_thread"] = AsyncMock(side_effect=AssertionError("an unloaded target needs no eviction"))
        self.cleanup_tasks = []

    async def asyncTearDown(self):
        tasks = [*self.cleanup_tasks, *self.client._callback_tasks, *self.ns["CODEX_SUBAGENT_LIMIT_TASKS"].values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def settle(self):
        # Drain existing event-loop completion callbacks, not a retry timer or
        # an artificial second settings request. Completion must provide wakeup.
        for _ in range(8):
            await asyncio.sleep(0)
            tasks = tuple(self.ns["CODEX_SUBAGENT_LIMIT_TASKS"].values())
            if tasks:
                await asyncio.gather(*tasks)

    async def save_default(self):
        await self.ns["update_session"](self.sid, self.ns["UpdateSessionRequest"](codex_provider="default"))
        await self.settle()
        self.assertEqual(self.chat["codex_provider"], "custom")
        self.assertEqual(self.chat["_codex_provider_pending"]["codex_provider"], "default")
        self.assertFalse(self.manager.is_thread_loaded("target-thread"))

    def assert_handoff_preserved_ownership(self):
        self.assertEqual(self.chat["codex_provider"], "default", "completed shared transport work stranded the saved provider selection")
        self.assertNotIn("_codex_provider_pending", self.chat)
        self.assertEqual(self.chat["codex_thread_id"], "target-thread")
        self.assertEqual(self.chat["session_id"], "target-thread")
        self.assertEqual(self.chat["codex_goal"], self.goal)
        self.ns["CODEX_PROVIDER_STORE"].require_thread("target-thread", None)
        self.assertEqual(self.peer, self.peer_before)
        self.assertIs(self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"][self.peer["id"]], self.manager)
        self.assertIs(self.client._turns_by_thread["peer-thread"], self.peer_turn)
        self.assertFalse(self.peer_turn._completed)
        self.manager.close.assert_not_awaited()
        self.assertEqual([(call.args[0], call.args[1]["threadId"]) for call in self.manager.request.await_args_list if call.args[0] in {"thread/archive", "thread/unarchive"}], [("thread/archive", "target-thread"), ("thread/unarchive", "target-thread")])

    async def test_settled_rpc_registry_entry_does_not_swallow_completion_wakeup(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result({})
        # This is the real response/coroutine-finally ordering window: the
        # response has completed but request cleanup has not popped its entry.
        self.client._pending[7] = ("thread/read", future, None)
        await self.ns["update_session"](self.sid, self.ns["UpdateSessionRequest"](codex_provider="default"))
        await self.settle()
        self.assert_handoff_preserved_ownership()

    async def test_target_caller_completion_is_deduplicated_and_excludes_current_task(self):
        gate, peer_gate = asyncio.Event(), asyncio.Event()
        caller = asyncio.create_task(gate.wait())
        peer_caller = asyncio.create_task(peer_gate.wait())
        self.cleanup_tasks.extend([caller, peer_caller])
        current = asyncio.current_task()
        self.manager._agentsdock_callers.update({caller: self.sid, peer_caller: self.peer["id"]})
        await self.save_default()
        self.manager._agentsdock_callers[current] = self.sid
        watcher = self.ns["watch_codex_provider_handoff_blockers"]
        self.assertEqual(watcher(self.manager, self.sid, ignore_task=current), 0)
        self.assertNotIn(self.sid, getattr(current, "_agentsdock_provider_handoff_watch", set()))
        self.manager._agentsdock_callers.pop(current)
        schedule = Mock(wraps=self.ns["schedule_codex_subagent_limit_application"])
        self.ns["schedule_codex_subagent_limit_application"] = schedule
        peer_gate.set()
        await peer_caller
        await self.settle()
        schedule.assert_not_called()
        gate.set()
        await caller
        await self.settle()
        schedule.assert_called_once_with(self.sid)
        self.assert_handoff_preserved_ownership()

    async def test_native_proof_error_generated_callback_does_not_schedule_its_own_retry(self):
        gate = asyncio.Event()
        async def handler(notification):
            await gate.wait()
        async def failed_proof(method, params):
            self.client._dispatch_notification_handlers({"method": "thread/status/changed", "params": {"threadId": "target-thread"}}, (handler,))
            raise RuntimeError("synthetic native ownership unavailable")
        self.manager.request.side_effect = failed_proof
        self.chat["_codex_provider_pending"] = self.ns["codex_provider"].runtime_selection({**self.chat, "codex_provider": "default"})
        async with self.ns["session_lifecycle_lock"](self.sid):
            self.assertFalse(await self.ns["apply_codex_provider_when_idle"](self.sid, ignore_task=asyncio.current_task()))
        gate.set()
        await asyncio.gather(*tuple(self.client._callback_tasks))
        await self.settle()
        self.manager.request.assert_awaited_once()
        self.assertEqual(self.chat["codex_provider"], "custom")
        self.assertIn("_codex_provider_pending", self.chat)
        self.assertFalse(self.ns["CODEX_SUBAGENT_LIMIT_TASKS"])

    async def test_background_commands_explain_pending_selection_and_clear_after_handoff(self):
        self.manager.list_background_terminals.return_value = [{"processId": "native-command"}]
        await self.save_default()
        control = self.ns["CODEX_PROVIDER_STORE"].control(self.chat)
        self.assertTrue(control["pending"])
        self.assertEqual(control["pending_reason"], "background_terminals")
        self.assertEqual(control["active_provider"], "custom")
        self.assertEqual(control["requested_provider"], "default")
        self.manager.list_background_terminals.return_value = []
        self.assertTrue(await self.ns["apply_codex_provider_when_idle"](self.sid, ignore_task=asyncio.current_task()))
        control = self.ns["CODEX_PROVIDER_STORE"].control(self.chat)
        self.assertFalse(control["pending"])
        self.assertIsNone(control["pending_reason"])
        self.assertEqual(self.peer, self.peer_before)

    async def test_failed_native_ownership_proof_keeps_pending_without_touching_peer(self):
        self.manager.request.side_effect = RuntimeError("synthetic native ownership unavailable")
        await self.save_default()
        self.assertEqual(self.chat["codex_goal"], self.goal)
        self.assertEqual(self.peer, self.peer_before)
        self.assertEqual(self.chat["codex_thread_id"], "target-thread")
        self.manager.close.assert_not_awaited()
        self.assertFalse(self.ns["CODEX_SUBAGENT_LIMIT_TASKS"])
        self.assertFalse(self.ns["CODEX_SUBAGENT_LIMIT_REQUESTED"])
        self.ns["CODEX_PROVIDER_STORE"].require_thread("target-thread", self.ns["CODEX_PROVIDER_STORE"].for_session(self.chat))
        self.ns["apply_codex_subagent_limit_when_idle"].assert_not_awaited()

    async def test_existing_rejected_child_archive_recovers_and_applies_saved_parent_provider(self):
        child_id = "persisted-child"
        self.chat["_codex_provider_unarchive_pending"] = [child_id]
        selected = self.ns["CODEX_PROVIDER_STORE"].for_session(self.chat)
        self.ns["CODEX_PROVIDER_STORE"].record_thread(child_id, selected)
        rollout_root = self.root / "sessions"
        rollout_root.mkdir()
        path = rollout_root / "rollout-persisted-child.jsonl"
        path.write_text('synthetic child rollout\n')
        self.ns["CODEX_SESSIONS_ROOT"] = rollout_root
        self.manager.list_descendant_threads = AsyncMock(return_value=[{"id": child_id}])
        self.manager.read_thread = AsyncMock(return_value={"id": child_id, "path": str(path), "ephemeral": False})
        self.manager.list_turns = AsyncMock(return_value=[{"id": "child-turn", "status": "interrupted"}])
        native_request = self.manager.request.side_effect
        async def request(method, params):
            if method == "thread/unarchive" and params["threadId"] == child_id:
                raise CodexAppServerRequestError(method, {"code": -32000, "message": "no archived rollout found"})
            return await native_request(method, params)
        self.manager.request.side_effect = request
        await self.ns["update_session"](self.sid, self.ns["UpdateSessionRequest"](codex_provider="default"))
        await self.settle()
        self.assertEqual(self.chat["codex_provider"], "default")
        self.assertNotIn("_codex_provider_pending", self.chat)
        self.assertNotIn("_codex_provider_unarchive_pending", self.chat)
        self.ns["CODEX_PROVIDER_STORE"].require_thread("target-thread", None)
        self.ns["CODEX_PROVIDER_STORE"].require_thread(child_id, selected)
        self.assertEqual(path.read_text(), 'synthetic child rollout\n')
        self.assertEqual(self.peer, self.peer_before)
        self.assertIs(self.client._turns_by_thread["peer-thread"], self.peer_turn)
        self.manager.close.assert_not_awaited()
        self.assertEqual([(call.args[0], call.args[1]["threadId"]) for call in self.manager.request.await_args_list if call.args[0] in {"thread/archive", "thread/unarchive"}],
            [("thread/unarchive", child_id), ("thread/archive", "target-thread"), ("thread/unarchive", "target-thread"), ("thread/unarchive", child_id)])

    async def assert_notification_handoff(self, thread_id, *, deferred):
        gate = asyncio.Event()
        async def handler(notification):
            await gate.wait()
        self.client._dispatch_notification_handlers({"method": "thread/status/changed", "params": {"threadId": thread_id}}, (handler,))
        await asyncio.sleep(0)
        self.assertTrue(self.client._callback_tasks)
        callbacks = tuple(self.client._callback_tasks)
        if deferred:
            await self.save_default()
        else:
            await self.ns["update_session"](self.sid, self.ns["UpdateSessionRequest"](codex_provider="default"))
            await self.settle()
            self.assert_handoff_preserved_ownership()
        self.assertTrue(all(not task.done() for task in callbacks))
        gate.set()
        await asyncio.gather(*callbacks)
        await self.settle()
        self.assertFalse(self.client._callback_tasks)
        self.assert_handoff_preserved_ownership()

    async def test_peer_notification_does_not_delay_unloaded_chat_handoff(self):
        await self.assert_notification_handoff("peer-thread", deferred=False)

    async def test_target_notification_completion_retries_pending_unloaded_chat(self):
        await self.assert_notification_handoff("target-thread", deferred=True)

    async def test_unknown_notification_completion_retries_pending_unloaded_chat(self):
        await self.assert_notification_handoff("unknown-thread", deferred=True)

    async def assert_rpc_handoff(self, thread_id, *, deferred):
        self.client._proc = SimpleNamespace(returncode=None, stdin=object())
        self.client._send = AsyncMock()
        request = asyncio.create_task(self.client._request_connected("thread/read", {"threadId": thread_id}))
        self.cleanup_tasks.append(request)
        await asyncio.sleep(0)
        self.assertTrue(self.client._pending)
        future = next(iter(self.client._pending.values()))[1]
        if deferred:
            await self.save_default()
        else:
            await self.ns["update_session"](self.sid, self.ns["UpdateSessionRequest"](codex_provider="default"))
            await self.settle()
            self.assert_handoff_preserved_ownership()
        self.assertFalse(request.done())
        self.assertFalse(future.done())
        future.set_result({"thread": {"id": thread_id}})
        await request
        await self.settle()
        self.assertFalse(self.client._pending)
        self.assert_handoff_preserved_ownership()

    async def test_peer_rpc_does_not_delay_unloaded_chat_handoff(self):
        await self.assert_rpc_handoff("peer-thread", deferred=False)

    async def test_target_rpc_completion_retries_pending_unloaded_chat(self):
        await self.assert_rpc_handoff("target-thread", deferred=True)

    async def test_unknown_rpc_completion_retries_pending_unloaded_chat(self):
        await self.assert_rpc_handoff("unknown-thread", deferred=True)
