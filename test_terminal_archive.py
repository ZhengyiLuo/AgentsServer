import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

import agent_server


class TerminalArchiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_archiving_pauses_the_chats_scheduled_jobs(self) -> None:
        session_id = "archive-pauses-jobs"
        archived = {
            "id": session_id,
            "title": "Archived",
            "backend": "codex",
            "archived": True,
        }
        active = {**archived, "archived": False}
        update = AsyncMock(side_effect=[archived, active, active])
        pause_for_session = AsyncMock(return_value=2)

        with (
            patch.object(agent_server.STORE, "sessions", {
                session_id: active,
            }),
            patch.object(agent_server.STORE, "update", update),
            patch.object(
                agent_server.JOBS,
                "pause_for_session",
                pause_for_session,
            ),
            patch.object(
                agent_server,
                "terminalize_archived_cross_chat_session",
                new_callable=AsyncMock,
            ),
            patch.object(agent_server.asyncio, "to_thread", new_callable=AsyncMock),
        ):
            archive_response = await agent_server.update_session(
                session_id,
                agent_server.UpdateSessionRequest(archived=True),
            )
            unarchive_response = await agent_server.update_session(
                session_id,
                agent_server.UpdateSessionRequest(archived=False),
            )
            rename_response = await agent_server.update_session(
                session_id,
                agent_server.UpdateSessionRequest(title="Renamed"),
            )

        self.assertTrue(archive_response["session"]["archived"])
        self.assertFalse(unarchive_response["session"]["archived"])
        self.assertFalse(rename_response["session"]["archived"])
        pause_for_session.assert_awaited_once_with(session_id)

    async def test_archiving_stays_successful_when_job_pause_persistence_fails(
        self,
    ) -> None:
        session_id = "archive-job-pause-failure"
        session = {
            "id": session_id,
            "title": "Archived",
            "backend": "codex",
            "archived": True,
        }

        async def update(_session_id: str, values: dict) -> dict:
            session.update(values)
            return dict(session)

        pause_for_session = AsyncMock(
            side_effect=[OSError("disk full"), OSError("disk full"), 0],
        )
        with (
            patch.object(agent_server.STORE, "sessions", {session_id: session}),
            patch.object(
                agent_server.STORE,
                "update",
                side_effect=update,
            ),
            patch.object(
                agent_server.JOBS,
                "pause_for_session",
                pause_for_session,
            ),
            patch.object(
                agent_server,
                "terminalize_archived_cross_chat_session",
                new_callable=AsyncMock,
            ),
            patch.object(agent_server.asyncio, "to_thread", new_callable=AsyncMock),
            patch.object(agent_server.logger, "warning") as warning,
        ):
            response = await agent_server.update_session(
                session_id,
                agent_server.UpdateSessionRequest(archived=True),
            )
            with self.assertRaises(HTTPException) as blocked:
                await agent_server.update_session(
                    session_id,
                    agent_server.UpdateSessionRequest(archived=False),
                )
            unarchived = await agent_server.update_session(
                session_id,
                agent_server.UpdateSessionRequest(archived=False),
            )

        self.assertTrue(response["session"]["archived"])
        self.assertEqual(blocked.exception.status_code, 503)
        self.assertFalse(unarchived["session"]["archived"])
        self.assertEqual(
            pause_for_session.await_args_list,
            [
                unittest.mock.call(session_id),
                unittest.mock.call(session_id, persist_unchanged=True),
                unittest.mock.call(session_id, persist_unchanged=True),
            ],
        )
        warning.assert_called_once()
        self.assertIn(session_id, warning.call_args.args)

    async def test_archiving_a_chat_kills_its_terminal_session(self) -> None:
        session = {"id": "archive-test", "title": "Archive test", "backend": "codex", "archived": True}
        update = AsyncMock(return_value=session)
        def threaded(callback, *args):
            if callback is agent_server.kill_terminal_session:
                return {"killed": True}
            return callback(*args)
        to_thread = AsyncMock(side_effect=threaded)

        with patch.dict(agent_server.STORE.sessions, {"archive-test": session}), \
             patch.object(agent_server.STORE, "update", update), \
             patch.object(agent_server.asyncio, "to_thread", to_thread):
            response = await agent_server.update_session(
                "archive-test",
                agent_server.UpdateSessionRequest(archived=True),
            )

        self.assertTrue(response["session"]["archived"])
        self.assertIn(
            unittest.mock.call(agent_server.kill_terminal_session, "archive-test"),
            to_thread.await_args_list,
        )

    async def test_archiving_succeeds_when_tmux_is_not_installed(self) -> None:
        session_id = "archive-without-tmux"
        session = {"id": session_id, "title": "Archive test", "backend": "codex", "archived": True}
        update = AsyncMock(return_value=session)

        with patch.dict(agent_server.STORE.sessions, {session_id: session}), \
             patch.object(agent_server.STORE, "update", update), \
             patch.object(agent_server.shutil, "which", return_value=None), \
             patch.object(agent_server, "run_tmux") as run_tmux:
            response = await agent_server.update_session(
                session_id,
                agent_server.UpdateSessionRequest(archived=True),
            )

        self.assertTrue(response["session"]["archived"])
        run_tmux.assert_not_called()

    async def test_archiving_remains_successful_when_terminal_cleanup_fails(self) -> None:
        session_id = "archive-cleanup-failure"
        session = {"id": session_id, "title": "Archive test", "backend": "codex", "archived": True}
        update = AsyncMock(return_value=session)
        cleanup_error = HTTPException(status_code=500, detail="tmux server failed")
        def threaded(callback, *args):
            if callback is agent_server.kill_terminal_session:
                raise cleanup_error
            return callback(*args)
        to_thread = AsyncMock(side_effect=threaded)

        with patch.dict(agent_server.STORE.sessions, {session_id: session}), \
             patch.object(agent_server.STORE, "update", update), \
             patch.object(agent_server.asyncio, "to_thread", to_thread), \
             patch.object(agent_server.logger, "warning") as warning:
            response = await agent_server.update_session(
                session_id,
                agent_server.UpdateSessionRequest(archived=True),
            )

        self.assertTrue(response["session"]["archived"])
        update.assert_awaited_once()
        self.assertIn(
            unittest.mock.call(agent_server.kill_terminal_session, session_id),
            to_thread.await_args_list,
        )
        warning.assert_called_once()
        self.assertIn(session_id, warning.call_args.args)

    def test_terminal_kill_does_not_mask_other_tmux_errors(self) -> None:
        session_id = "archive-tmux-error"
        session = {"id": session_id, "title": "Archive test", "backend": "codex"}
        error = HTTPException(status_code=500, detail="tmux server failed")

        with patch.dict(agent_server.STORE.sessions, {session_id: session}), \
             patch.object(agent_server.shutil, "which", return_value="/usr/bin/tmux"), \
             patch.object(agent_server, "tmux_session_exists", side_effect=error):
            with self.assertRaises(HTTPException) as raised:
                agent_server.kill_terminal_session(session_id)

        self.assertIs(raised.exception, error)

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
