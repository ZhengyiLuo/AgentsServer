import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

import agent_server


class GoalManager:
    def __init__(self, goal: dict | None = None) -> None:
        self.generation = 1
        self.loaded: set[str] = set()
        self.goal = goal
        self.get_thread_goal = AsyncMock(side_effect=self._get_goal)
        self.set_thread_goal = AsyncMock(side_effect=self._set_goal)
        self.start_calls = 0
        self.resume_calls = 0

    async def _get_goal(self, _thread_id: str) -> dict | None:
        return dict(self.goal) if isinstance(self.goal, dict) else None

    async def _set_goal(self, thread_id: str, **values: object) -> dict:
        self.goal = {
            **(self.goal or {}),
            "threadId": thread_id,
            **values,
        }
        return dict(self.goal)

    def is_thread_loaded(self, thread_id: str) -> bool:
        return thread_id in self.loaded

    async def start_thread(self, _params: dict) -> str:
        self.start_calls += 1
        self.loaded.add("thread-new")
        return "thread-new"

    async def resume_thread(self, thread_id: str, _params: dict) -> str:
        self.resume_calls += 1
        self.loaded.add(thread_id)
        return thread_id

    async def inject_items(self, _thread_id: str, _items: list[dict]) -> None:
        return None


class CodexGoalReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_manager = agent_server.CODEX_APP_SERVER_MANAGER
        self.previous_index = agent_server.CODEX_THREAD_SESSION_INDEX
        self.previous_sync = agent_server.CODEX_GOAL_SYNC_GENERATIONS
        self.previous_quarantine = agent_server.CODEX_QUARANTINED_GOAL_THREADS
        agent_server.STORE.sessions = {}
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = set()
        agent_server.CODEX_APP_SERVER_MANAGER = None
        agent_server.CODEX_THREAD_SESSION_INDEX = {}
        agent_server.CODEX_GOAL_SYNC_GENERATIONS = {}
        agent_server.CODEX_QUARANTINED_GOAL_THREADS = {}

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CODEX_APP_SERVER_MANAGER = self.previous_manager
        agent_server.CODEX_THREAD_SESSION_INDEX = self.previous_index
        agent_server.CODEX_GOAL_SYNC_GENERATIONS = self.previous_sync
        agent_server.CODEX_QUARANTINED_GOAL_THREADS = self.previous_quarantine

    def session(self, **values: object) -> dict:
        session = {
            "id": "chat",
            "backend": agent_server.BACKEND_CODEX,
            "cwd": str(Path(__file__).resolve().parent.parent),
            "session_id": "thread-old",
            "codex_thread_id": "thread-old",
        }
        session.update(values)
        return session

    async def test_resume_reconciles_once_per_daemon_generation(self) -> None:
        native_goal = {
            "objective": "Finish safely",
            "status": "paused",
            "timeUsedSeconds": 4,
        }
        session = self.session(codex_instruction_hash="policy")
        agent_server.STORE.sessions["chat"] = session
        manager = GoalManager(native_goal)
        manager.loaded.add("thread-old")

        with (
            patch.object(
                agent_server,
                "codex_thread_instruction_hash",
                return_value="policy",
            ),
            patch.object(agent_server, "pin_codex_app_server_thread", AsyncMock()),
            patch.object(agent_server, "unpin_codex_app_server_thread", AsyncMock()),
            patch.object(agent_server, "touch_codex_app_server_thread", AsyncMock()),
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server.STORE, "save_provider_session", AsyncMock()),
        ):
            await agent_server.ensure_codex_app_server_thread(
                manager, "chat", session, str(session["cwd"])
            )
            await agent_server.ensure_codex_app_server_thread(
                manager, "chat", session, str(session["cwd"])
            )
            self.assertEqual(manager.get_thread_goal.await_count, 1)

            manager.generation = 2
            manager.loaded.clear()
            await agent_server.ensure_codex_app_server_thread(
                manager, "chat", session, str(session["cwd"])
            )

        self.assertEqual(manager.get_thread_goal.await_count, 2)
        self.assertEqual(manager.resume_calls, 1)
        self.assertEqual(session["codex_goal"], native_goal)

    async def test_missing_native_goal_clears_divergent_local_cache(self) -> None:
        session = self.session(
            codex_goal={"objective": "Ghost", "status": "paused"},
            codex_goal_time_budget_seconds=30,
            codex_goal_time_budget_exhausted=True,
        )
        agent_server.STORE.sessions["chat"] = session
        manager = GoalManager(None)
        with patch.object(agent_server.STORE, "save", AsyncMock()):
            goal = await agent_server.reconcile_codex_thread_goal(
                manager, "chat", "thread-old", force=True
            )

        self.assertIsNone(goal)
        self.assertIsNone(session["codex_goal"])
        self.assertIsNone(session["codex_goal_time_budget_seconds"])
        self.assertFalse(session["codex_goal_time_budget_exhausted"])

    async def test_rollover_migrates_active_goal_as_paused(self) -> None:
        session = self.session(
            codex_goal={
                "objective": "Finish safely",
                "status": "active",
                "tokenBudget": 2000,
            }
        )
        agent_server.STORE.sessions["chat"] = session
        with (
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server, "build_fork_memory", return_value="memory"),
            patch.object(agent_server, "append_event", AsyncMock(return_value={})),
        ):
            result = await agent_server.rollover_codex_provider_session(
                "chat", "run", "thread-old", "stale", message="rolled"
            )

        self.assertIsNotNone(result)
        self.assertEqual(session["codex_goal"]["status"], "paused")
        self.assertIsNone(session["codex_thread_id"])
        self.assertEqual(
            agent_server.CODEX_QUARANTINED_GOAL_THREADS["thread-old"],
            "chat",
        )

        manager = GoalManager(None)
        with (
            patch.object(agent_server, "pin_codex_app_server_thread", AsyncMock()),
            patch.object(agent_server, "touch_codex_app_server_thread", AsyncMock()),
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server.STORE, "save_provider_session", AsyncMock()),
        ):
            await agent_server.ensure_codex_app_server_thread(
                manager, "chat", session, str(session["cwd"])
            )

        manager.set_thread_goal.assert_awaited_once_with(
            "thread-new",
            objective="Finish safely",
            status="paused",
            token_budget=2000,
        )
        self.assertEqual(session["codex_goal"]["threadId"], "thread-new")
        self.assertEqual(session["codex_goal"]["status"], "paused")

    async def test_ownerless_native_continuation_is_interrupted(self) -> None:
        session = self.session(
            codex_goal={"objective": "Ghost", "status": "active"}
        )
        agent_server.STORE.sessions["chat"] = session
        agent_server.CODEX_THREAD_SESSION_INDEX["thread-old"] = "chat"
        manager = GoalManager(session["codex_goal"])
        manager.request = AsyncMock(return_value={})  # type: ignore[attr-defined]
        agent_server.CODEX_APP_SERVER_MANAGER = manager  # type: ignore[assignment]

        with patch.object(agent_server.STORE, "save", AsyncMock()):
            await agent_server.project_codex_notification({
                "method": "turn/started",
                "params": {
                    "threadId": "thread-old",
                    "turn": {"id": "turn-ghost"},
                },
            })

        manager.request.assert_awaited_once_with(  # type: ignore[attr-defined]
            "turn/interrupt",
            {"threadId": "thread-old", "turnId": "turn-ghost"},
            timeout=agent_server.CODEX_GOAL_CONTROL_TIMEOUT_SECONDS,
        )
        manager.set_thread_goal.assert_awaited_once_with(
            "thread-old", status="paused"
        )
        self.assertEqual(session["codex_goal"]["status"], "paused")
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)

    async def test_stale_thread_does_not_block_goal_clear(self) -> None:
        session = self.session(
            codex_goal={"objective": "Ghost", "status": "active"}
        )
        agent_server.STORE.sessions["chat"] = session
        stale = HTTPException(
            status_code=502,
            detail="Codex app-server control failed: thread not found",
        )
        with (
            patch.object(
                agent_server,
                "acquire_codex_control_thread",
                AsyncMock(side_effect=stale),
            ),
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server, "build_fork_memory", return_value="memory"),
            patch.object(agent_server, "append_event", AsyncMock(return_value={})),
        ):
            result = await agent_server._delete_codex_goal_locked("chat")

        self.assertIsNone(result["goal"])
        self.assertIsNone(session["codex_goal"])
        self.assertIsNone(session["codex_thread_id"])
        self.assertEqual(
            agent_server.CODEX_QUARANTINED_GOAL_THREADS["thread-old"],
            "chat",
        )

    async def test_goal_clear_timeout_detaches_thread_and_clears_local_goal(
        self,
    ) -> None:
        session = self.session(
            codex_goal={"objective": "Ghost", "status": "active"},
            codex_goal_time_budget_seconds=30,
            codex_goal_time_budget_exhausted=True,
        )
        agent_server.STORE.sessions["chat"] = session
        manager = GoalManager(session["codex_goal"])

        async def never_finishes(_thread_id: str) -> None:
            await asyncio.Event().wait()

        manager.clear_thread_goal = AsyncMock(side_effect=never_finishes)  # type: ignore[attr-defined]
        with (
            patch.object(
                agent_server,
                "acquire_codex_control_thread",
                AsyncMock(return_value=(manager, "thread-old", session)),
            ),
            patch.object(
                agent_server,
                "release_codex_control_thread",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "CODEX_GOAL_CONTROL_TIMEOUT_SECONDS",
                0.01,
            ),
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server, "build_fork_memory", return_value="memory"),
            patch.object(agent_server, "append_event", AsyncMock(return_value={})),
        ):
            result = await agent_server._delete_codex_goal_locked("chat")

        self.assertIsNone(result["goal"])
        self.assertIsNone(session["codex_goal"])
        self.assertIsNone(session["codex_goal_time_budget_seconds"])
        self.assertFalse(session["codex_goal_time_budget_exhausted"])
        self.assertIsNone(session["codex_thread_id"])
        self.assertEqual(
            agent_server.CODEX_QUARANTINED_GOAL_THREADS["thread-old"],
            "chat",
        )

    async def test_stop_terminalizes_when_native_goal_get_fails(self) -> None:
        session = self.session(
            codex_goal={"objective": "Ghost", "status": "active"}
        )
        agent_server.STORE.sessions["chat"] = session
        turn = Mock()
        turn.turn_id = "turn-live"
        turn.interrupt = AsyncMock()
        agent_server.ACTIVE["chat"] = {
            "run_id": "run-live",
            "backend": agent_server.BACKEND_CODEX,
            "transport": agent_server.CODEX_TRANSPORT_APP_SERVER,
            "provider_thread_id": "thread-old",
            "provider_session_id": "thread-old",
            "provider_turn_ready": True,
            "codex_app_server_turn": turn,
        }
        agent_server.BUSY_SESSIONS.add("chat")
        manager = GoalManager(session["codex_goal"])
        manager.get_thread_goal = AsyncMock(
            side_effect=RuntimeError("goal get unavailable")
        )
        agent_server.CODEX_APP_SERVER_MANAGER = manager  # type: ignore[assignment]
        with (
            patch.object(agent_server, "STOP_CONFIRM_TIMEOUT_SECONDS", 0.01),
            patch.object(agent_server.STORE, "save", AsyncMock()),
            patch.object(agent_server, "build_fork_memory", return_value="memory"),
            patch.object(agent_server, "append_event", AsyncMock(return_value={})),
        ):
            result = await agent_server.stop_turn("chat")

        self.assertTrue(result["stopped"])
        self.assertFalse(result["pending"])
        self.assertTrue(result["goal_fenced"])
        turn.interrupt.assert_awaited_once()
        self.assertNotIn("chat", agent_server.ACTIVE)
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertIsNone(session["codex_thread_id"])
        self.assertEqual(session["codex_goal"]["status"], "paused")

    async def test_restart_pauses_cached_goal_without_claiming_run_liveness(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions_file = Path(temp_dir) / "sessions.json"
            sessions_file.write_text(
                '{"chat":{"id":"chat","backend":"codex",'
                '"codex_thread_id":"thread-old",'
                '"codex_goal":{"objective":"Keep going","status":"active"}}}'
            )
            store = agent_server.SessionStore()
            with patch.object(agent_server, "SESSIONS_FILE", sessions_file):
                await store.load()

        self.assertEqual(store.sessions["chat"]["codex_goal"]["status"], "paused")
        self.assertIn(
            "_codex_goal_reconciliation_required",
            store.sessions["chat"],
        )
        self.assertNotIn("chat", agent_server.BUSY_SESSIONS)
        self.assertNotIn("chat", agent_server.ACTIVE)


if __name__ == "__main__":
    unittest.main()
