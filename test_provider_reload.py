import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server


class FakeCodexManager:
    def __init__(self, *, loaded: bool = True) -> None:
        self.loaded = loaded
        self.ready = True
        self.generation = 7
        self.unsubscribe_calls: list[str] = []
        self.background_terminals: list[dict[str, object]] = []

    def is_thread_loaded(self, thread_id: str) -> bool:
        return self.loaded and thread_id == "codex-thread"

    def active_turn(self, _thread_id: str) -> None:
        return None

    async def list_background_terminals(
        self,
        _thread_id: str,
    ) -> list[dict[str, object]]:
        return list(self.background_terminals)

    async def unsubscribe_thread(self, thread_id: str) -> str:
        self.unsubscribe_calls.append(thread_id)
        self.loaded = False
        return "unsubscribed"


class FakeClaudeManager:
    def __init__(self, *, loaded: bool = True) -> None:
        self.loaded = loaded
        self.evict_calls: list[tuple[str, bool]] = []

    def is_loaded(self, chat_id: str) -> bool:
        return self.loaded and chat_id == "chat"

    async def evict(self, chat_id: str, *, force: bool = False) -> bool:
        self.evict_calls.append((chat_id, force))
        if not self.loaded:
            return False
        if force:
            raise AssertionError("provider reload must never force eviction")
        self.loaded = False
        return True


class ProviderReloadEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        names = (
            "ACTIVE",
            "BUSY_SESSIONS",
            "CURRENT_TURNS",
            "SERVER_MAINTENANCE_SESSIONS",
            "SESSION_TURN_TASKS",
            "CODEX_NATIVE_ACTION_TASKS",
            "CODEX_INTERACTION_HANDLER_TASKS",
            "CLAUDE_INTERACTION_HANDLER_TASKS",
            "CODEX_PENDING_INTERACTIONS",
            "CLAUDE_PENDING_INTERACTIONS",
            "CODEX_APP_SERVER_MANAGER",
            "CLAUDE_SDK_MANAGER",
            "CODEX_APP_SERVER_THREAD_LRU",
            "CODEX_APP_SERVER_EVICTING_THREADS",
            "CODEX_APP_SERVER_THREAD_PIN_COUNTS",
            "CODEX_APP_SERVER_PINNED_THREADS",
            "CODEX_SUBAGENT_STATE",
            "CODEX_SUBAGENT_SESSION_INDEX",
            "CODEX_SUBAGENT_LIVE_GENERATIONS",
            "SESSION_LIFECYCLE_LOCKS",
            "DELETING_SESSIONS",
            "DELETED_SESSION_TOMBSTONES",
        )
        self.previous = {
            name: getattr(agent_server, name)
            for name in names
        }
        self.previous_sessions = agent_server.STORE.sessions
        agent_server.STORE.sessions = {}
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = set()
        agent_server.CURRENT_TURNS = {}
        agent_server.SERVER_MAINTENANCE_SESSIONS = set()
        agent_server.SESSION_TURN_TASKS = {}
        agent_server.CODEX_NATIVE_ACTION_TASKS = {}
        agent_server.CODEX_INTERACTION_HANDLER_TASKS = {}
        agent_server.CLAUDE_INTERACTION_HANDLER_TASKS = {}
        agent_server.CODEX_PENDING_INTERACTIONS = {}
        agent_server.CLAUDE_PENDING_INTERACTIONS = {}
        agent_server.CODEX_APP_SERVER_MANAGER = None
        agent_server.CLAUDE_SDK_MANAGER = None
        agent_server.CODEX_APP_SERVER_THREAD_LRU = agent_server.OrderedDict()
        agent_server.CODEX_APP_SERVER_EVICTING_THREADS = {}
        agent_server.CODEX_APP_SERVER_THREAD_PIN_COUNTS = {}
        agent_server.CODEX_APP_SERVER_PINNED_THREADS = set()
        agent_server.CODEX_SUBAGENT_STATE = {}
        agent_server.CODEX_SUBAGENT_SESSION_INDEX = {}
        agent_server.CODEX_SUBAGENT_LIVE_GENERATIONS = {}
        agent_server.SESSION_LIFECYCLE_LOCKS = {}
        agent_server.DELETING_SESSIONS = set()
        agent_server.DELETED_SESSION_TOMBSTONES = set()

    async def asyncTearDown(self) -> None:
        for registry_name in (
            "SESSION_TURN_TASKS",
            "CODEX_NATIVE_ACTION_TASKS",
            "CODEX_INTERACTION_HANDLER_TASKS",
            "CLAUDE_INTERACTION_HANDLER_TASKS",
        ):
            registry = getattr(agent_server, registry_name)
            for value in tuple(registry.values()):
                tasks = value if isinstance(value, set) else {value}
                for task in tuple(tasks):
                    if isinstance(task, asyncio.Task) and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
        agent_server.STORE.sessions = self.previous_sessions
        for name, value in self.previous.items():
            setattr(agent_server, name, value)

    def install_codex(self, manager: FakeCodexManager) -> dict[str, object]:
        session: dict[str, object] = {
            "id": "chat",
            "backend": agent_server.BACKEND_CODEX,
            "cwd": str(agent_server.DEFAULT_CWD),
            "codex_thread_id": "codex-thread",
        }
        agent_server.STORE.sessions = {"chat": session}
        agent_server.CODEX_APP_SERVER_MANAGER = manager
        return session

    def install_claude(self, manager: FakeClaudeManager) -> dict[str, object]:
        session: dict[str, object] = {
            "id": "chat",
            "backend": agent_server.BACKEND_CLAUDE,
            "cwd": str(agent_server.DEFAULT_CWD),
            "claude_session_id": "claude-session",
        }
        agent_server.STORE.sessions = {"chat": session}
        agent_server.CLAUDE_SDK_MANAGER = manager
        return session

    async def test_codex_reload_unsubscribes_only_selected_thread(self) -> None:
        manager = FakeCodexManager()
        session = self.install_codex(manager)
        with patch.object(
            agent_server,
            "CODEX_TRANSPORT",
            agent_server.CODEX_TRANSPORT_APP_SERVER,
        ):
            result = await agent_server.post_session_provider_reload("chat")

        self.assertEqual(manager.unsubscribe_calls, ["codex-thread"])
        self.assertEqual(session["codex_thread_id"], "codex-thread")
        self.assertEqual(result["action"], "unloaded")
        self.assertEqual(result["provider_id"], "codex-thread")
        self.assertFalse(result["runtime"]["thread_loaded"])
        self.assertEqual(result["runtime"]["status"], {"type": "notLoaded"})
        self.assertIs(agent_server.CODEX_APP_SERVER_MANAGER, manager)
        self.assertFalse(agent_server.SERVER_MAINTENANCE_SESSIONS)

    async def test_claude_reload_evicts_idle_chat_without_force(self) -> None:
        manager = FakeClaudeManager()
        session = self.install_claude(manager)
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ), patch.object(
            agent_server,
            "claude_sdk_dependency_available",
            return_value=True,
        ), patch.object(
            agent_server,
            "build_claude_subagent_snapshot",
            return_value={"active_count": 0},
        ):
            result = await agent_server.post_session_provider_reload("chat")

        self.assertEqual(manager.evict_calls, [("chat", False)])
        self.assertEqual(session["claude_session_id"], "claude-session")
        self.assertEqual(result["action"], "unloaded")
        self.assertEqual(result["provider_id"], "claude-session")
        self.assertFalse(result["runtime"]["session_loaded"])
        self.assertEqual(result["runtime"]["status"], {"type": "notLoaded"})
        self.assertIs(agent_server.CLAUDE_SDK_MANAGER, manager)
        self.assertFalse(agent_server.SERVER_MAINTENANCE_SESSIONS)

    async def test_reload_is_idempotent_when_provider_is_unloaded(self) -> None:
        manager = FakeClaudeManager(loaded=False)
        self.install_claude(manager)
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ), patch.object(
            agent_server,
            "claude_sdk_dependency_available",
            return_value=True,
        ):
            result = await agent_server.post_session_provider_reload("chat")

        self.assertEqual(manager.evict_calls, [])
        self.assertEqual(result["action"], "already_unloaded")
        self.assertFalse(result["was_loaded"])

    async def test_reload_rejects_active_turn_without_evicting(self) -> None:
        manager = FakeClaudeManager()
        self.install_claude(manager)
        agent_server.BUSY_SESSIONS.add("chat")
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.post_session_provider_reload("chat")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(manager.evict_calls, [])

    async def test_reload_rejects_live_interaction_handler(self) -> None:
        manager = FakeClaudeManager()
        self.install_claude(manager)
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        handler = asyncio.create_task(asyncio.Event().wait())
        agent_server.CLAUDE_PENDING_INTERACTIONS["approval"] = {
            "id": "approval",
            "session_id": "chat",
            "future": future,
            "responded": False,
        }
        agent_server.CLAUDE_INTERACTION_HANDLER_TASKS["chat"] = {handler}
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.post_session_provider_reload("chat")

        self.assertEqual(raised.exception.status_code, 409)
        self.assertFalse(future.done())
        self.assertEqual(manager.evict_calls, [])
        self.assertFalse(agent_server.SERVER_MAINTENANCE_SESSIONS)

    async def test_reload_clears_orphaned_interaction(self) -> None:
        manager = FakeClaudeManager(loaded=False)
        session = self.install_claude(manager)
        future: asyncio.Future[dict[str, object]] = (
            asyncio.get_running_loop().create_future()
        )
        agent_server.CLAUDE_PENDING_INTERACTIONS["stale"] = {
            "id": "stale",
            "session_id": "chat",
            "future": future,
            "responded": False,
        }
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ), patch.object(
            agent_server,
            "claude_sdk_dependency_available",
            return_value=True,
        ), patch.object(agent_server.STORE, "save", AsyncMock()):
            result = await agent_server.post_session_provider_reload("chat")

        self.assertEqual(result["cleared_stale_interactions"], 1)
        self.assertEqual(await future, {"decision": "cancel"})
        self.assertFalse(agent_server.CLAUDE_PENDING_INTERACTIONS)
        self.assertEqual(session["claude_pending_interaction_count"], 0)
        self.assertFalse(session["claude_needs_user_action"])

    async def test_reload_rejects_live_background_work(self) -> None:
        codex = FakeCodexManager()
        codex.background_terminals = [{"processId": "process-1"}]
        self.install_codex(codex)
        with patch.object(
            agent_server,
            "CODEX_TRANSPORT",
            agent_server.CODEX_TRANSPORT_APP_SERVER,
        ):
            with self.assertRaises(HTTPException) as terminal_error:
                await agent_server.post_session_provider_reload("chat")
        self.assertEqual(terminal_error.exception.status_code, 409)
        self.assertEqual(codex.unsubscribe_calls, [])
        self.assertFalse(agent_server.SERVER_MAINTENANCE_SESSIONS)

        claude = FakeClaudeManager()
        self.install_claude(claude)
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ), patch.object(
            agent_server,
            "build_claude_subagent_snapshot",
            return_value={"active_count": 1},
        ):
            with self.assertRaises(HTTPException) as subagent_error:
                await agent_server.post_session_provider_reload("chat")
        self.assertEqual(subagent_error.exception.status_code, 409)
        self.assertEqual(claude.evict_calls, [])
        self.assertFalse(agent_server.SERVER_MAINTENANCE_SESSIONS)

    def test_route_is_additive(self) -> None:
        routes = {
            (route.path, method)
            for route in agent_server.app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertIn(
            ("/api/sessions/{session_id}/provider/reload", "POST"),
            routes,
        )


if __name__ == "__main__":
    unittest.main()
