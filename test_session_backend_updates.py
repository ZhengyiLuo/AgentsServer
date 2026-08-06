import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import agent_server


async def wait_forever() -> None:
    await asyncio.Event().wait()


class SessionBackendUpdateFenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session_id = "backend-fence-chat"
        self.session = {
            "id": self.session_id,
            "title": "New chat",
            "backend": agent_server.BACKEND_CLAUDE,
            "cwd": "/tmp",
        }
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_tasks = agent_server.SESSION_TURN_TASKS
        self.previous_lifecycle_locks = agent_server.SESSION_LIFECYCLE_LOCKS
        agent_server.STORE.sessions = {self.session_id: self.session}
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = set()
        agent_server.CURRENT_TURNS = {}
        agent_server.SESSION_TURN_TASKS = {}
        agent_server.SESSION_LIFECYCLE_LOCKS = {}

    async def asyncTearDown(self) -> None:
        pending = [
            task
            for tasks in agent_server.SESSION_TURN_TASKS.values()
            for task in tasks
            if not task.done()
        ]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.SESSION_TURN_TASKS = self.previous_tasks
        agent_server.SESSION_LIFECYCLE_LOCKS = self.previous_lifecycle_locks

    async def test_busy_first_turn_rejects_backend_change_without_provider_id(
        self,
    ) -> None:
        agent_server.BUSY_SESSIONS.add(self.session_id)
        agent_server.CURRENT_TURNS[self.session_id] = {
            "run_id": None,
            "backend": agent_server.BACKEND_CLAUDE,
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(agent_server.STORE, "update", update):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.update_session(
                    self.session_id,
                    agent_server.UpdateSessionRequest(
                        backend=agent_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("active turn", str(raised.exception.detail))
        update.assert_not_awaited()

    async def test_active_record_rejects_change_even_without_busy_reservation(
        self,
    ) -> None:
        agent_server.ACTIVE[self.session_id] = {
            "run_id": "run-active",
            "backend": agent_server.BACKEND_CLAUDE,
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(agent_server.STORE, "update", update):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.update_session(
                    self.session_id,
                    agent_server.UpdateSessionRequest(
                        backend=agent_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        update.assert_not_awaited()

    async def test_registered_provider_start_closes_pre_reservation_race(
        self,
    ) -> None:
        provider_start = asyncio.create_task(wait_forever())
        agent_server.SESSION_TURN_TASKS = {
            self.session_id: {provider_start},
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(agent_server.STORE, "update", update):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.update_session(
                    self.session_id,
                    agent_server.UpdateSessionRequest(
                        backend=agent_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        update.assert_not_awaited()

    async def test_noop_backend_updates_remain_allowed_while_starting(self) -> None:
        provider_start = asyncio.create_task(wait_forever())
        agent_server.SESSION_TURN_TASKS = {
            self.session_id: {provider_start},
        }
        update = AsyncMock(return_value=self.session)

        with patch.object(agent_server.STORE, "update", update):
            same_response = await agent_server.update_session(
                self.session_id,
                agent_server.UpdateSessionRequest(backend="CLAUDE"),
            )
            null_response = await agent_server.update_session(
                self.session_id,
                agent_server.UpdateSessionRequest(backend=None),
            )

        self.assertEqual(same_response["session"]["backend"], "claude")
        self.assertEqual(null_response["session"]["backend"], "claude")
        self.assertEqual(update.await_count, 2)

    async def test_completed_registry_entries_do_not_block_a_change(self) -> None:
        completed = asyncio.create_task(asyncio.sleep(0))
        await completed
        agent_server.SESSION_TURN_TASKS = {
            self.session_id: {completed},
        }
        changed = {**self.session, "backend": agent_server.BACKEND_CODEX}
        update = AsyncMock(return_value=changed)

        with patch.object(agent_server.STORE, "update", update):
            response = await agent_server.update_session(
                self.session_id,
                agent_server.UpdateSessionRequest(
                    backend=agent_server.BACKEND_CODEX,
                ),
            )

        self.assertEqual(response["session"]["backend"], "codex")
        update.assert_awaited_once_with(
            self.session_id,
            {"backend": agent_server.BACKEND_CODEX},
        )

    async def test_backend_patch_that_wins_invalidates_a_waiting_turn(self) -> None:
        update_entered = asyncio.Event()
        allow_update = asyncio.Event()

        async def gated_update(
            session_id: str,
            patch_payload: dict[str, object],
        ) -> dict[str, object]:
            self.assertEqual(session_id, self.session_id)
            update_entered.set()
            await allow_update.wait()
            self.session.update(patch_payload)
            return self.session

        with patch.object(
            agent_server.STORE,
            "update",
            AsyncMock(side_effect=gated_update),
        ):
            update_task = asyncio.create_task(
                agent_server.update_session(
                    self.session_id,
                    agent_server.UpdateSessionRequest(
                        backend=agent_server.BACKEND_CODEX,
                    ),
                )
            )
            await asyncio.wait_for(update_entered.wait(), timeout=1)

            turn_task = asyncio.create_task(
                agent_server.start_turn(
                    self.session_id,
                    agent_server.TurnRequest(
                        prompt="Use the backend selected when I pressed Send",
                        backend=agent_server.BACKEND_CLAUDE,
                    ),
                )
            )
            for _attempt in range(20):
                if agent_server.SESSION_TURN_TASKS.get(self.session_id):
                    break
                await asyncio.sleep(0)
            self.assertIn(
                turn_task,
                agent_server.SESSION_TURN_TASKS.get(self.session_id, set()),
            )

            allow_update.set()
            update_response = await asyncio.wait_for(update_task, timeout=1)
            with self.assertRaises(agent_server.HTTPException) as raised:
                await asyncio.wait_for(turn_task, timeout=1)

        self.assertEqual(update_response["session"]["backend"], "codex")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("backend changed", str(raised.exception.detail))
        self.assertNotIn(self.session_id, agent_server.BUSY_SESSIONS)
        self.assertNotIn(self.session_id, agent_server.CURRENT_TURNS)
        self.assertNotIn(self.session_id, agent_server.SESSION_TURN_TASKS)

    async def test_durable_first_turn_lock_blocks_backend_without_provider_id(
        self,
    ) -> None:
        self.session["backend_locked"] = True
        update = AsyncMock(return_value=self.session)

        self.assertTrue(
            agent_server.public_session(self.session, summary=True)["backend_locked"]
        )
        with patch.object(agent_server.STORE, "update", update):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.update_session(
                    self.session_id,
                    agent_server.UpdateSessionRequest(
                        backend=agent_server.BACKEND_CODEX,
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertIn("backend is locked", str(raised.exception.detail))
        update.assert_not_awaited()

    async def test_mark_backend_started_persists_summary_lock(self) -> None:
        save = AsyncMock()

        with patch.object(agent_server.STORE, "save", save):
            marked = await agent_server.STORE.mark_backend_started(
                self.session_id,
                agent_server.BACKEND_CLAUDE,
            )

        self.assertTrue(marked["backend_locked"])
        self.assertTrue(
            agent_server.public_session(marked, summary=True)["backend_locked"]
        )
        save.assert_awaited_once()

    async def test_chat_turn_marks_backend_before_returning_admission(self) -> None:
        self.session["title"] = "Existing chat"

        async def mark_started(
            session_id: str,
            backend: str,
        ) -> dict[str, object]:
            self.assertEqual(session_id, self.session_id)
            self.assertEqual(backend, agent_server.BACKEND_CLAUDE)
            self.session["backend_locked"] = True
            return self.session

        mark = AsyncMock(side_effect=mark_started)
        update = AsyncMock(return_value=self.session)
        append_event = AsyncMock(return_value={"type": "turn_started", "seq": 1})
        run_claude = AsyncMock()

        with (
            patch.object(agent_server.STORE, "mark_backend_started", mark),
            patch.object(agent_server.STORE, "update", update),
            patch.object(
                agent_server,
                "managed_server_update_blocker",
                return_value=None,
            ),
            patch.object(
                agent_server,
                "turn_start_blocker",
                AsyncMock(return_value=None),
            ),
            patch.object(
                agent_server,
                "ensure_runtime_available",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "build_turn_provider_prompt",
                return_value="provider prompt",
            ),
            patch.object(agent_server, "append_event", append_event),
            patch.object(agent_server, "run_claude", run_claude),
        ):
            result = await agent_server._start_turn_locked(
                self.session_id,
                agent_server.TurnRequest(prompt="Start"),
                admission_backend=agent_server.BACKEND_CLAUDE,
            )
            await asyncio.sleep(0)

        mark.assert_awaited_once_with(
            self.session_id,
            agent_server.BACKEND_CLAUDE,
        )
        self.assertTrue(result["session"]["backend_locked"])
        run_claude.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
