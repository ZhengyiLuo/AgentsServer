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


if __name__ == "__main__":
    unittest.main()
