import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server


class TerminalArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_archiving_a_chat_kills_its_terminal_session(self) -> None:
        session = {"id": "archive-test", "title": "Archive test", "backend": "codex", "archived": True}
        update = AsyncMock(return_value=session)
        to_thread = AsyncMock(return_value={"killed": True})

        with patch.object(agent_server.STORE, "update", update), patch.object(agent_server.asyncio, "to_thread", to_thread):
            response = await agent_server.update_session(
                "archive-test",
                agent_server.UpdateSessionRequest(archived=True),
            )

        self.assertTrue(response["session"]["archived"])
        to_thread.assert_awaited_once_with(agent_server.kill_terminal_session, "archive-test")

    async def test_non_archive_updates_leave_terminal_session_running(self) -> None:
        session = {"id": "archive-test", "title": "Renamed", "backend": "codex", "archived": False}
        update = AsyncMock(return_value=session)
        to_thread = AsyncMock()

        with patch.object(agent_server.STORE, "update", update), patch.object(agent_server.asyncio, "to_thread", to_thread):
            await agent_server.update_session(
                "archive-test",
                agent_server.UpdateSessionRequest(title="Renamed"),
            )

        to_thread.assert_not_awaited()

    async def test_archived_chat_cannot_recreate_a_terminal_session(self) -> None:
        session_id = "archived-terminal-test"
        with patch.dict(agent_server.STORE.sessions, {
            session_id: {"id": session_id, "title": "Archived", "backend": "codex", "archived": True}
        }):
            with self.assertRaises(HTTPException) as raised:
                agent_server.ensure_terminal_session(session_id)

        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
