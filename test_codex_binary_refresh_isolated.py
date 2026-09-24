"""CLI replacement exercises actual server routing without listeners or auth."""
from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, Mock
import weakref

from fastapi import HTTPException
from codex_app_server import CodexAppServerClient, CodexAppServerRequestError
import codex_provider


class Manager:
    def __init__(self, *_args, **options):
        self.options = options
        self.client = SimpleNamespace(_loaded_threads=set(), _pending={},
            _server_request_tasks={}, _callback_tasks=set(), _turns_by_thread={})
        self.generation = 1
        self.ready = True
        self.handlers = []
        self.closed = False
        self.goal = None
        self.terminals = []
        self.get_thread_goal = AsyncMock(side_effect=lambda _thread: self.goal)
        self.list_background_terminals = AsyncMock(side_effect=lambda _thread: self.terminals)

    def add_notification_handler(self, handler):
        self.handlers.append(handler)

    def is_thread_loaded(self, thread):
        return thread in self.client._loaded_threads

    def active_turn(self, thread):
        return self.client._turns_by_thread.get(thread)

    async def close(self):
        self.closed = True


class BinaryRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        source = Path(__file__).with_name("agent_server.py")
        names = {"codex_app_server_managers", "retain_codex_manager_caller",
            "codex_manager_has_callers", "codex_manager_has_callbacks", "codex_manager_owns_notification",
            "refresh_codex_app_server_binary", "codex_manager_session_busy",
            "prepare_codex_app_server_process",
            "drain_retired_codex_managers", "existing_codex_app_server_manager",
            "existing_codex_app_server_manager_for_thread", "codex_app_server_manager",
            "close_codex_app_server_manager", "session_registry_has_live_tasks"}
        names.update({"handle_codex_server_request", "cache_codex_approval_item", "schedule_codex_manager_drain"})
        nodes = [node for node in ast.parse(source.read_text()).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.identity = ("/fixture/codex", 1, 2, 3, 4, "codex-cli 0.153.4")
        self.locks = {}
        self.ns = ns = {"asyncio": asyncio, "time": time, "weakref": weakref,
            "HTTPException": HTTPException, "CodexAppServerRequestError": CodexAppServerRequestError,
            "logger": logging.getLogger(__name__), "codex_provider": codex_provider,
            "CODEX_APP_SERVER_MANAGER": None, "CODEX_CUSTOM_APP_SERVER_MANAGERS": {},
            "CODEX_RETIRED_APP_SERVER_MANAGERS": [], "CODEX_SESSION_APP_SERVER_MANAGERS": {},
            "CODEX_BINARY_IDENTITY": None, "CODEX_BINARY_CHECKED_AT": 0,
            "CODEX_APP_SERVER_MANAGER_EPOCH": 0, "CODEX_APP_SERVER_MANAGER_CLEANUP_EPOCH": None,
            "CODEX_MANAGER_DRAIN_TASK": None, "CODEX_MANAGER_DRAIN_REQUESTED": False,
            "CODEX_MANAGER_CLOSING": False,
            "CODEX_TRANSPORT": "app-server", "CODEX_TRANSPORT_EXEC": "exec",
            "CODEX_GOALS_ENABLED": True, "CODEX_GOALS_RECONFIGURING": False,
            "CODEX_BIN": "/fixture/codex", "DEFAULT_CWD": "/fixture", "SERVER_VERSION": "test",
            "CodexAppServerManager": Manager, "existing_cwd": lambda value: value,
            "codex_app_server_env": lambda *_args: {}, "codex_binary_identity": lambda: self.identity,
            "ensure_provider_manager_factory_admission": lambda **_kwargs: None,
            "STORE": SimpleNamespace(sessions={}), "CODEX_PROVIDER_STORE": SimpleNamespace(
                for_session=lambda session, **_kwargs: None, for_thread=lambda thread: None),
            "session_provider_id": lambda session: session.get("codex_thread_id"),
            "session_lifecycle_lock": lambda session: self.locks.setdefault(session, asyncio.Lock()),
            "codex_session_has_live_subagents": lambda session: False,
            "schedule_codex_manager_drain": Mock(),
            "cancel_codex_interactions": AsyncMock(), "cancel_codex_native_actions": AsyncMock(),
            "decline_server_request": AsyncMock(return_value={"decision": "decline"}),
            "CODEX_INTERACTION_METHODS": {"item/commandExecution/requestApproval"},
            "codex_request_is_interactive": lambda *_args: True,
            "reset_codex_ephemeral_runtime_metadata": AsyncMock(),
            "CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS": .1}
        for name in ("CODEX_BINARY_PROBE_LOCK", "CODEX_GOALS_CONFIG_LOCK", "CODEX_AUTH_LOCK",
                     "CODEX_APP_SERVER_MANAGER_LOCK", "CODEX_APP_SERVER_THREAD_LRU_LOCK"):
            ns[name] = asyncio.Lock()
        for name in ("ACTIVE", "CURRENT_TURNS", "SESSION_TURN_TASKS", "CODEX_NATIVE_ACTION_TASKS",
                     "CODEX_INTERACTION_HANDLER_TASKS", "CLAUDE_INTERACTION_HANDLER_TASKS",
                     "CODEX_PENDING_INTERACTIONS", "CODEX_GOAL_SYNC_GENERATIONS",
                     "CODEX_APP_SERVER_EVICTING_THREADS", "CODEX_APP_SERVER_THREAD_LRU",
                     "CODEX_APP_SERVER_THREAD_PIN_COUNTS", "CODEX_APPROVAL_ITEM_CACHE",
                     "CODEX_INTERACTIVE_CONTROL_THREAD_COUNTS", "CODEX_PERMISSION_PROFILES_CACHE",
                     "CODEX_QUARANTINED_GOAL_THREADS"):
            ns[name] = {}
        for name in ("BUSY_SESSIONS", "SERVER_MAINTENANCE_SESSIONS", "CODEX_APP_SERVER_PINNED_THREADS",
                     "CODEX_INTERACTIVE_CONTROL_THREADS", "CODEX_APP_SERVER_INVALIDATED_THREADS"):
            ns[name] = set()
        for name in ("CODEX_APP_SERVER_TIMEOUT_SECONDS", "CODEX_APP_SERVER_LIFECYCLE_TIMEOUT_SECONDS",
                     "CODEX_APP_SERVER_JSONL_LIMIT_BYTES", "CODEX_APP_SERVER_NOTIFICATION_QUEUE_LIMIT"):
            ns[name] = 10
        for name in ("handle_codex_server_request", "register_codex_app_server_child",
                     "unregister_codex_app_server_child", "project_codex_notification", "cache_codex_approval_item"):
            ns[name] = Mock()
        ns["codex_session_id_for_thread"] = lambda thread: next((sid for sid, session in ns["STORE"].sessions.items()
            if session.get("codex_thread_id") == thread), None)
        async def evict(manager, thread, **_kwargs):
            manager.client._loaded_threads.discard(thread)
            return True
        ns["evict_codex_app_server_thread"] = AsyncMock(side_effect=evict)
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), ns)
        self.real_schedule_drain = ns["schedule_codex_manager_drain"]
        ns["schedule_codex_manager_drain"] = Mock()

    async def manager(self, session_id):
        session = self.ns["STORE"].sessions.setdefault(session_id, {"id": session_id, "backend": "codex"})
        return await asyncio.create_task(self.ns["codex_app_server_manager"](session))

    async def refresh(self):
        await self.ns["refresh_codex_app_server_binary"](force=True)

    async def drain(self):
        self.ns["CODEX_MANAGER_DRAIN_REQUESTED"] = True
        task = asyncio.create_task(self.ns["drain_retired_codex_managers"]())
        self.ns["CODEX_MANAGER_DRAIN_TASK"] = task
        await task

    def load(self, session_id, manager):
        thread = "thread-" + session_id
        self.ns["STORE"].sessions[session_id]["codex_thread_id"] = thread
        manager.client._loaded_threads.add(thread)
        return thread

    async def upgrade(self):
        self.identity = (*self.identity[:-1], "codex-cli 0.156.1")
        await self.refresh()

    async def test_new_chat_uses_upgrade_while_old_turn_and_approval_keep_owner(self):
        old = await self.manager("old")
        thread = self.load("old", old)
        self.ns["BUSY_SESSIONS"].add("old")
        old.client._turns_by_thread[thread] = SimpleNamespace(_completed=False)
        await self.upgrade()
        new = await self.manager("new")
        self.assertIsNot(new, old)
        self.assertIs(await self.manager("old"), old)
        self.assertIs(self.ns["existing_codex_app_server_manager_for_thread"](thread), old)
        self.assertEqual(self.ns["codex_app_server_managers"](), (new, old))
        await self.drain()
        self.assertFalse(old.closed)
        self.assertFalse(new.closed)

    async def test_same_identity_and_failed_probe_do_not_churn(self):
        old = await self.manager("old")
        await self.refresh()
        self.assertIs(await self.manager("new"), old)
        self.identity = None
        await self.refresh()
        self.assertIs(await self.manager("third"), old)
        self.assertEqual(self.ns["CODEX_RETIRED_APP_SERVER_MANAGERS"], [])

    async def test_first_refresh_compares_process_started_during_auth_to_installed_cli(self):
        async with self.ns["CODEX_AUTH_LOCK"]:
            old = await self.manager("old")
            await old.options["before_start"]()
        self.assertIsNone(self.ns["CODEX_BINARY_IDENTITY"])
        await self.upgrade()
        self.assertIsNot(await self.manager("new"), old)
        self.assertIn(old, self.ns["CODEX_RETIRED_APP_SERVER_MANAGERS"])

    async def test_idle_thread_migrates_without_changing_native_thread_id(self):
        old = await self.manager("old")
        thread = self.load("old", old)
        self.ns["CODEX_GOAL_SYNC_GENERATIONS"]["old"] = (id(old), 1, thread)
        await self.upgrade()
        await self.drain()
        self.assertTrue(old.closed)
        self.assertEqual(self.ns["STORE"].sessions["old"]["codex_thread_id"], thread)
        self.assertNotIn("old", self.ns["CODEX_GOAL_SYNC_GENERATIONS"])
        self.assertIsNot(await self.manager("old"), old)

    async def test_native_goal_background_terminal_and_pending_approval_preserve_owner(self):
        old = await self.manager("old")
        self.load("old", old)
        await self.upgrade()
        old.goal = {"status": "active"}
        await self.drain()
        self.assertFalse(old.closed)
        old.goal = None
        old.terminals = [{"id": "background"}]
        await self.drain()
        self.assertFalse(old.closed)
        old.terminals = []
        self.ns["CODEX_PENDING_INTERACTIONS"]["approval"] = {"session_id": "old"}
        await self.drain()
        self.assertFalse(old.closed)
        self.ns["CODEX_PENDING_INTERACTIONS"].clear()
        await self.drain()
        self.assertTrue(old.closed)

    async def test_slow_drain_does_not_hold_global_admission_and_late_borrow_retains_owner(self):
        old = await self.manager("old")
        self.load("old", old)
        await self.upgrade()
        entered, release = asyncio.Event(), asyncio.Event()
        async def goal(_thread):
            entered.set()
            await release.wait()
            return None
        old.get_thread_goal.side_effect = goal
        draining = asyncio.create_task(self.drain())
        await entered.wait()
        try:
            new = await asyncio.wait_for(self.manager("new"), timeout=.5)
            self.assertIsNot(new, old)
            borrowed = await self.ns["codex_app_server_manager"](self.ns["STORE"].sessions["old"])
            self.assertIs(borrowed, old)
        finally:
            release.set()
            await draining
        self.assertFalse(old.closed)
        self.assertIs(self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"]["old"], old)

    async def test_long_lived_read_only_inspector_does_not_retain_idle_process(self):
        old = await self.manager("old")
        thread = self.load("old", old)
        entered, release = asyncio.Event(), asyncio.Event()
        async def inspect():
            self.assertIs(self.ns["existing_codex_app_server_manager"](self.ns["STORE"].sessions["old"]), old)
            self.assertIs(self.ns["existing_codex_app_server_manager_for_thread"](thread), old)
            entered.set()
            await release.wait()
        inspector = asyncio.create_task(inspect())
        await entered.wait()
        try:
            await self.upgrade()
            await self.drain()
            self.assertTrue(old.closed)
            self.assertFalse(inspector.done())
        finally:
            release.set()
            await inspector

    async def test_terminal_projection_finishes_before_owner_migration(self):
        old = await self.manager("old")
        self.load("old", old)
        await self.upgrade()
        callback = asyncio.create_task(asyncio.Event().wait())
        old.client._callback_tasks.add(callback)
        try:
            await self.drain()
            self.assertIs(self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"]["old"], old)
            self.assertFalse(old.closed)
        finally:
            callback.cancel()
            await asyncio.gather(callback, return_exceptions=True)
        await self.drain()
        self.assertTrue(old.closed)

    async def test_old_notifications_cannot_target_resumed_thread_or_lease_reader(self):
        old = await self.manager("old")
        thread = self.load("old", old)
        self.assertIs(await self.manager("busy"), old)
        self.ns["BUSY_SESSIONS"].add("busy")
        await self.upgrade()
        await self.drain()
        self.assertFalse(old.closed)
        new = await self.manager("old")
        new.client._loaded_threads.add(thread)
        for method in ("thread/goal/cleared", "thread/status/changed", "thread/closed", "item/started"):
            notification = {"method": method, "params": {"threadId": thread}}
            self.assertFalse(self.ns["codex_manager_owns_notification"](old, notification))
            self.assertTrue(self.ns["codex_manager_owns_notification"](new, notification))
        self.assertNotIn(asyncio.current_task(), new._agentsdock_callers)
        self.ns["CODEX_APPROVAL_ITEM_CACHE"][(thread, "new-item")] = {"current": True}
        client = CodexAppServerClient("unused", cwd="/tmp", env_factory=lambda: {},
            notification_guard=old.options["notification_guard"])
        client.add_notification_handler(self.ns["cache_codex_approval_item"])
        projected = AsyncMock()
        client.add_notification_handler(projected)
        client._route_notification({"method": "thread/closed", "params": {"threadId": thread}})
        client._route_notification({"method": "thread/goal/cleared", "params": {"threadId": thread}})
        self.assertIn((thread, "new-item"), self.ns["CODEX_APPROVAL_ITEM_CACHE"])
        projected.assert_not_called()
        result = await old.options["server_request_handler"]("old-request", "item/commandExecution/requestApproval", {"threadId": thread})
        self.assertEqual(result, {"decision": "decline"})
        self.assertFalse(self.ns["CODEX_PENDING_INTERACTIONS"])

    async def test_notification_guard_rechecks_queued_handler_after_ownership_changes(self):
        allowed = True
        entered, release = asyncio.Event(), asyncio.Event()
        seen = []
        async def project(notification):
            if notification["params"]["index"] == 1:
                entered.set()
                await release.wait()
            seen.append(notification["params"]["index"])
        client = CodexAppServerClient("unused", cwd="/tmp", env_factory=lambda: {},
            notification_guard=lambda _notification: allowed)
        client.add_notification_handler(project)
        for index in (1, 2):
            client._route_notification({"method": "thread/goal/updated", "params": {"threadId": "thread", "index": index}})
        await entered.wait()
        allowed = False
        release.set()
        await asyncio.gather(*client._callback_tasks)
        self.assertEqual(seen, [1])

    async def test_shutdown_suppresses_drain_spawned_by_cancelled_caller(self):
        old = await self.manager("old")
        await self.upgrade()
        async def cancellation():
            self.assertTrue(self.ns["CODEX_MANAGER_CLOSING"])
            self.real_schedule_drain()
            self.assertIsNone(self.ns["CODEX_MANAGER_DRAIN_TASK"])
            await asyncio.sleep(0)
        self.ns["cancel_codex_native_actions"].side_effect = cancellation
        await self.ns["close_codex_app_server_manager"]()
        self.assertTrue(old.closed)
        self.assertFalse(self.ns["CODEX_MANAGER_CLOSING"])

    async def test_shutdown_closes_current_and_retained_managers(self):
        old = await self.manager("old")
        await self.upgrade()
        new = await self.manager("new")
        await self.ns["close_codex_app_server_manager"]()
        self.assertTrue(old.closed)
        self.assertTrue(new.closed)
        self.assertEqual(self.ns["codex_app_server_managers"](), ())
        self.assertFalse(self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"])


if __name__ == "__main__":
    unittest.main()
