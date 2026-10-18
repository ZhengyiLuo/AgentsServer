"""Automatic placeholder cleanup is conditional, unlike explicit Delete."""
from contextlib import ExitStack
import unittest
import io
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
import agent_server as server


class DiscardEmptySessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.session = {"id": "empty", "title": "New chat", "updated_at": "v1"}
        self.stack.enter_context(patch.object(server.STORE, "sessions", {"empty": self.session}))
        self.stack.enter_context(patch.object(server.JOBS, "jobs", {}))
        self.stack.enter_context(patch.object(server.PORT_TUNNELS, "_sockets", {}))
        for name in ("ACTIVE", "CURRENT_TURNS", "QUEUED_TURNS", "RUN_NOW_TURNS", "RUN_NOW_REQUESTS", "SESSION_TURN_TASKS"):
            self.stack.enter_context(patch.object(server, name, {}))
        for name in ("BUSY_SESSIONS", "DELETING_SESSIONS", "DELETED_SESSION_TOMBSTONES"):
            self.stack.enter_context(patch.object(server, name, set()))
        path = MagicMock()
        self.history = path.open
        self.history.side_effect = lambda *_: io.BytesIO(b'{"type":"session_created","seq":1}\n')
        self.stack.enter_context(patch.object(server, "events_path", return_value=path))
        self.files = self.stack.enter_context(patch.object(server, "list_session_file_records", return_value=[]))
        self.terminal = self.stack.enter_context(patch.object(server, "terminal_windows_snapshot", return_value={"exists": False}))

    async def test_accepts_only_unused_creation_record(self):
        await server.ensure_discardable_empty_session("empty", "v1")

    async def test_rejects_started_renamed_pinned_archived_and_provider_bound_sessions(self):
        for key, value in (("title", "Keep me"), ("backend_locked", True), ("pinned", True),
                           ("archived", True), ("codex_thread_id", "native"),
                           ("claude_session_id", "native"), ("cursor_session_id", "native"),
                           ("opencode_session_id", "native"), ("updated_at", "v2")):
            with self.subTest(key=key), patch.dict(self.session, {key: value}):
                with self.assertRaises(HTTPException) as raised:
                    await server.ensure_discardable_empty_session("empty", "v1")
                self.assertEqual(raised.exception.status_code, 409)

    async def test_preserves_queue_jobs_terminals_uploads_and_history(self):
        for target, attribute, value in ((server, "QUEUED_TURNS", {"empty": ["queued"]}),
                                         (server.JOBS, "jobs", {"j": {"session_id": "empty"}}),
                                         (server.PORT_TUNNELS, "_sockets", {"empty": {"socket": None}})):
            with patch.object(target, attribute, value), self.assertRaises(HTTPException):
                await server.ensure_discardable_empty_session("empty", "v1")
        self.files.return_value = [{"id": "upload"}]
        with self.assertRaises(HTTPException):
            await server.ensure_discardable_empty_session("empty", "v1")
        self.files.return_value = []
        self.terminal.return_value = {"exists": True}
        with self.assertRaises(HTTPException):
            await server.ensure_discardable_empty_session("empty", "v1")
        self.terminal.return_value = {"exists": False}
        self.history.side_effect = lambda *_: io.BytesIO(b'{"type":"session_created","seq":1}\n{"type":"turn_started"}\n')
        with self.assertRaises(HTTPException):
            await server.ensure_discardable_empty_session("empty", "v1")

    async def test_malformed_or_extra_history_is_never_discarded(self):
        for content in (b'', b'broken', b'{"type":"session_created","seq":1}\nmalformed', b'x' * 65537):
            with self.subTest(content=content[:40]):
                self.history.side_effect = lambda *_, data=content: io.BytesIO(data)
                with self.assertRaises(HTTPException):
                    await server.ensure_discardable_empty_session("empty", "v1")

    async def test_rechecks_after_async_reads_and_rejects_without_cancelling_work(self):
        # Use the actual read boundary; a writer that wins before the fence must
        # preserve the chat even if an earlier observation said it was empty.
        original = server.asyncio.to_thread
        async def mutate(func, *args):
            result = await original(func, *args)
            self.session["backend_locked"] = True
            return result
        with patch.object(server.asyncio, "to_thread", side_effect=mutate), self.assertRaises(HTTPException):
            await server.ensure_discardable_empty_session("empty", "v1")
        with patch.object(server, "ensure_session_not_initializing"), \
             patch.object(server, "stop_cleanup_in_progress", return_value=False), \
             patch.object(server, "cancel_generated_session_title") as cancel, \
             patch.object(server.STORE, "delete", new_callable=AsyncMock) as delete:
            with self.assertRaises(HTTPException):
                await server.delete_session("empty", discard_updated_at="v1")
            cancel.assert_not_called()
            delete.assert_not_awaited()
            self.assertNotIn("empty", server.DELETING_SESSIONS)

    async def test_separate_route_waits_for_queue_recovery(self):
        with patch.object(server, "wait_for_queue_recovery_admission", new_callable=AsyncMock) as recovery, \
             patch.object(server, "delete_session", new_callable=AsyncMock, return_value={"deleted": True}) as delete:
            self.assertEqual(await server.discard_empty_session("empty", "v1"), {"deleted": True})
            recovery.assert_awaited_once()
            delete.assert_awaited_once_with("empty", discard_updated_at="v1")

    async def test_recoverable_dismissal_adopts_legacy_blank_and_is_desktop_scoped(self):
        import json
        with patch.object(server, "wait_for_queue_recovery_admission", new_callable=AsyncMock), \
             patch.object(server.STORE, "save", new_callable=AsyncMock) as save, \
             patch.object(server.STORE, "delete", new_callable=AsyncMock) as delete, \
             patch.object(server.STORE, "ensure_sort_orders", new_callable=AsyncMock):
            self.assertEqual(await server.dismiss_empty_session("empty", "v1", "desktop-client-one"),
                             {"ok": True, "dismissed": True})
            delete.assert_not_awaited()
            save.assert_awaited_once()
            self.assertIn("empty", server.STORE.sessions)
            self.assertNotIn("empty", server.DELETING_SESSIONS)
            self.assertEqual((await server.list_sessions(placeholder_client_id="desktop-client-one"))["sessions"], [])
            self.assertEqual(len((await server.list_sessions(placeholder_client_id="desktop-client-two"))["sessions"]), 1)
            restored = json.loads(json.dumps(self.session))
            self.assertTrue(server.empty_chat_dismissed_for(restored, "desktop-client-one"))
            self.assertFalse(server.empty_chat_dismissed_for(restored, ""))
            restored.update(sort_order=42, last_read_agent_event_seq=0,
                            codex_thread_status={"type": "notLoaded"},
                            claude_permission_mode="default")
            self.assertTrue(server.empty_chat_dismissed_for(restored, "desktop-client-one"))
            for key, value in (("model", "another-model"), ("backend_locked", True), ("latest_event_seq", 2)):
                with patch.dict(restored, {key: value}):
                    self.assertFalse(server.empty_chat_dismissed_for(restored, "desktop-client-one"))

    async def test_recoverable_dismissal_preserves_resources_and_rolls_back_save_failure(self):
        with patch.object(server, "wait_for_queue_recovery_admission", new_callable=AsyncMock), \
             patch.object(server.STORE, "save", new_callable=AsyncMock, side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                await server.dismiss_empty_session("empty", "v1", "desktop-client-one")
            self.assertNotIn("_empty_chat_cleanup", self.session)
            self.assertNotIn("empty", server.DELETING_SESSIONS)
            self.files.return_value = [{"id": "uploaded"}]
            with self.assertRaises(HTTPException):
                await server.dismiss_empty_session("empty", "v1", "desktop-client-one")
            self.assertNotIn("empty", server.DELETING_SESSIONS)

    async def test_recoverable_dismissal_rejects_invalid_identity_and_stale_revision(self):
        with self.assertRaises(HTTPException) as raised:
            await server.dismiss_empty_session("empty", "v1", "")
        self.assertEqual(raised.exception.status_code, 400)
        with patch.object(server, "wait_for_queue_recovery_admission", new_callable=AsyncMock):
            with self.assertRaises(HTTPException):
                await server.dismiss_empty_session("empty", "old", "desktop-client-one")
        self.assertNotIn("_empty_chat_cleanup", self.session)
