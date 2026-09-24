"""Native naming is optional metadata, never a model request or user rename."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import agent_server as server


def session(backend="claude", *, title="First request", source="prompt"):
    return {
        "id": "title-chat", "backend": backend, "session_id": "provider-title",
        "title": title, "_title_source": source, "_title_auto_value": title,
        "cwd": "/tmp", "folder": "General", "updated_at": "before",
    }


class NativeTitleReaders(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def claude_title(self, events):
        transcript = self.root / "provider-title.jsonl"
        transcript.write_text("\n".join(json.dumps(event) for event in events) + "\n")
        before = transcript.read_bytes()
        with patch.object(server, "claude_history_candidates", return_value=[transcript]):
            result = server.read_native_session_title(session())
        self.assertEqual(transcript.read_bytes(), before)
        return result

    def test_claude_reads_ai_title_not_prompt_or_summary(self):
        self.assertEqual(self.claude_title([
            {"type": "user", "sessionId": "provider-title", "message": "A long first line"},
            {"type": "summary", "summary": "Not a title"},
            {"type": "ai-title", "sessionId": "provider-title", "aiTitle": "Fix login"},
        ]), "Fix login")
        self.assertIsNone(self.claude_title([{"type": "summary", "summary": "Not a title"}]))

    def test_claude_custom_name_wins_and_other_sessions_are_ignored(self):
        self.assertEqual(self.claude_title([
            {"type": "custom-title", "sessionId": "provider-title", "customTitle": "My name"},
            {"type": "ai-title", "sessionId": "provider-title", "aiTitle": "Generated"},
            {"type": "custom-title", "sessionId": "neighbor", "customTitle": "Wrong chat"},
            {"type": "custom-title", "sessionId": "provider-title", "isSidechain": True, "customTitle": "Child"},
        ]), "My name")

    def test_claude_latest_title_wins(self):
        self.assertEqual(self.claude_title([
            {"type": "ai-title", "sessionId": "provider-title", "aiTitle": name}
            for name in ("Old", "New")
        ]), "New")

    def test_claude_ambiguous_identity_is_not_adopted(self):
        with patch.object(server, "claude_history_candidates", return_value=[Path("a"), Path("b")]):
            self.assertIsNone(server.read_native_session_title(session()))

    def test_codex_existing_index_name_no_inference(self):
        index = self.root / "session_index.jsonl"
        index.write_text(json.dumps({"id": "provider-title", "thread_name": "Refactor auth"}) + "\n")
        with patch.object(server, "CODEX_SESSION_INDEX_PATH", index):
            self.assertEqual(server.read_native_session_title(session("codex")), "Refactor auth")
            self.assertIsNone(server.read_native_session_title({**session("codex"), "session_id": "missing"}))

    def test_opencode_exact_main_session_read_only(self):
        database = self.root / "opencode" / "opencode.db"
        database.parent.mkdir()
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE session(id TEXT PRIMARY KEY, title TEXT, parent_id TEXT)")
            connection.executemany("INSERT INTO session VALUES (?, ?, ?)", [
                ("provider-title", "OpenCode native name", None),
                ("child", "Child name", "provider-title"),
                ("other", "Other name", None),
            ])
        connection.close()
        before = database.read_bytes()
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}):
            self.assertEqual(server.read_native_session_title(session("opencode")), "OpenCode native name")
            for provider_id in ("child", "unknown", "../../escape"):
                self.assertIsNone(server.read_native_session_title({**session("opencode"), "session_id": provider_id}))
        self.assertEqual(database.read_bytes(), before)

    def test_missing_opencode_db_is_not_created(self):
        with patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root)}):
            self.assertIsNone(server.read_native_session_title(session("opencode")))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cursor_keeps_fallback_without_decoding_private_store(self):
        self.assertIsNone(server.read_native_session_title(session("cursor")))

    def test_invalid_titles_and_provider_placeholders_are_not_used(self):
        for value in (None, {}, 17, "", "  ", "New chat", "Untitled", "New session", "New session - 2026-09-20T00:00:00Z", "x" * 4097):
            with self.subTest(value=str(value)[:50]):
                self.assertIsNone(server.native_session_title(value))

    def test_titles_are_bounded_single_line_unicode_display_text(self):
        self.assertEqual(server.native_session_title("  修复\n login\t\x00bug\u202e  "), "修复 login bug")
        self.assertEqual(len(server.native_session_title("x" * 200)), 120)


class NativeTitleOwnership(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = server.SessionStore()
        self.store.save = AsyncMock()
        self.sess = session()
        self.store.sessions = {"title-chat": self.sess}
        self.patches = [
            patch.object(server, "STORE", self.store),
            patch.object(server, "HISTORY_SEARCH_DIRTY", set()),
            patch.object(server, "DELETING_SESSIONS", set()),
            patch.object(server, "DELETED_SESSION_TOMBSTONES", set()),
            patch.object(server, "CODEX_APP_SERVER_MANAGER", None),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    async def adopt(self, title="Native name", **kwargs):
        return await self.store.adopt_auto_title(
            "title-chat", title, source="provider", backend="claude",
            provider_id="provider-title", **kwargs,
        )

    async def test_fallback_then_provider_name(self):
        self.sess.update(title="New chat", _title_source="placeholder", _title_auto_value="New chat")
        self.assertTrue(await self.store.adopt_auto_title("title-chat", "First request", source="prompt"))
        self.assertTrue(await self.adopt())
        self.assertEqual(self.sess["title"], "Native name")
        self.assertFalse(await self.store.adopt_auto_title("title-chat", "Second request", source="prompt"))

    async def test_attachment_only_first_turn_does_not_consume_placeholder(self):
        self.sess.update(title="New chat", _title_source="placeholder", _title_auto_value="New chat")
        self.assertFalse(await self.store.adopt_auto_title("title-chat", "New chat", source="prompt"))
        self.assertTrue(await self.store.adopt_auto_title("title-chat", "First real text", source="prompt"))

    async def test_user_rename_locks_even_same_text_or_new_chat(self):
        for title in ("Custom", "First request", "New chat", ""):
            self.sess.update(session())
            await self.store.update("title-chat", {"title": title})
            self.assertFalse(await self.adopt())
            self.assertEqual(self.sess["title"], title)
            self.assertEqual(self.sess["_title_source"], "manual")

    async def test_unknown_legacy_titles_are_preserved(self):
        self.sess.pop("_title_source")
        for title in ("First request", "New chat", "Custom"):
            self.sess["title"] = title
            self.assertFalse(await self.adopt())

    async def test_deleted_changed_provider_and_external_title_mutation_are_fenced(self):
        for field, value in (("session_id", "other"), ("backend", "codex"), ("title", "External"), ("_fork_initializing", True)):
            original = dict(self.sess)
            self.sess[field] = value
            self.assertFalse(await self.adopt())
            self.sess.clear()
            self.sess.update(original)
        server.DELETING_SESSIONS.add("title-chat")
        self.assertFalse(await self.adopt())
        server.DELETING_SESSIONS.clear()
        self.store.sessions.clear()
        self.assertFalse(await self.adopt())

    async def test_duplicate_and_stale_metadata_are_no_ops(self):
        await self.adopt()
        self.store.save.reset_mock()
        self.assertFalse(await self.adopt())
        self.assertFalse(await self.adopt("Stale title", expected_title="First request"))
        self.store.save.assert_not_awaited()

    async def test_failed_persistence_restores_previous_title_and_owner(self):
        original = dict(self.sess)
        self.store.save.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            await self.adopt()
        self.assertEqual(self.sess, original)

    async def test_title_rollback_does_not_remove_unrelated_live_metadata(self):
        async def fail_save():
            self.sess["latest_event_seq"] = 123
            raise OSError("disk full")

        self.store.save.side_effect = fail_save
        with self.assertRaises(OSError):
            await self.adopt()
        self.assertEqual(self.sess["title"], "First request")
        self.assertEqual(self.sess["latest_event_seq"], 123)

    async def test_cancelled_save_retains_its_committed_title(self):
        # SessionStore.save's awaited cancellation contract: write is committed
        # before CancelledError reaches its caller.
        self.store.save.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.adopt()
        self.assertEqual(self.sess["title"], "Native name")
        self.assertEqual(self.sess["_title_source"], "provider")

    async def test_manual_rename_wins_in_flight_metadata_read(self):
        ready, release = asyncio.Event(), asyncio.Event()

        async def read_in_thread(*args):
            ready.set()
            await release.wait()
            return "Generated title"

        with patch.object(server.asyncio, "to_thread", side_effect=read_in_thread):
            task = asyncio.create_task(server.refresh_native_session_title("title-chat"))
            await ready.wait()
            await self.store.update("title-chat", {"title": "My choice"})
            release.set()
            await task
        self.assertEqual(self.sess["title"], "My choice")

    async def test_read_failures_and_timeouts_leave_fallback(self):
        for error in (OSError("missing"), sqlite3.OperationalError("schema changed"), TimeoutError()):
            with patch.object(server, "read_native_session_title", side_effect=error):
                await server.refresh_native_session_title("title-chat")
            self.assertEqual(self.sess["title"], "First request")

    async def test_custom_codex_uses_its_own_manager_cache(self):
        self.sess.update(backend="codex", codex_thread_id="provider-title",
                         codex_provider="custom", codex_provider_revision="custom-revision")
        ordinary = SimpleNamespace(
            cached_thread_name=Mock(return_value="Wrong title"),
            is_thread_loaded=Mock(return_value=False),
        )
        custom = SimpleNamespace(
            cached_thread_name=Mock(return_value="Custom title"),
            is_thread_loaded=Mock(return_value=True),
            _agentsdock_provider_revision="custom-revision",
        )
        with patch.object(server, "CODEX_APP_SERVER_MANAGER", ordinary), patch.object(
            server, "CODEX_CUSTOM_APP_SERVER_MANAGERS", {"custom-revision": custom},
        ), patch.object(server, "read_native_session_title") as reader:
            self.assertIs(server.existing_codex_app_server_manager(self.sess), custom)
            await server.refresh_native_session_title("title-chat")
        self.assertEqual(self.sess["title"], "Custom title")
        custom.cached_thread_name.assert_called_once_with("provider-title")
        ordinary.cached_thread_name.assert_not_called()
        reader.assert_not_called()

    async def test_manual_titles_do_not_even_read_provider_store(self):
        self.sess["_title_source"] = "manual"
        with patch.object(server, "read_native_session_title") as reader:
            await server.refresh_native_session_title("title-chat")
        reader.assert_not_called()

    async def test_changed_provider_during_lookup_is_not_renamed(self):
        async def read_in_thread(*args):
            self.sess["session_id"] = "replacement"
            return "Old session title"

        with patch.object(server.asyncio, "to_thread", side_effect=read_in_thread):
            await server.refresh_native_session_title("title-chat")
        self.assertEqual(self.sess["title"], "First request")

    async def test_live_codex_metadata_needs_no_disk_or_provider_request(self):
        self.sess["backend"] = "codex"
        manager = SimpleNamespace(
            cached_thread_name=Mock(return_value="Cached title"),
            is_thread_loaded=Mock(return_value=True),
        )
        with patch.object(server, "CODEX_APP_SERVER_MANAGER", manager), patch.object(server, "read_native_session_title") as reader:
            self.assertIs(server.existing_codex_app_server_manager(self.sess), manager)
            await server.refresh_native_session_title("title-chat")
        self.assertEqual(self.sess["title"], "Cached title")
        manager.cached_thread_name.assert_called_once_with("provider-title")
        reader.assert_not_called()

    async def test_title_is_saved_before_terminal_event_refreshes_client(self):
        async def append(sid, kind, payload):
            self.assertEqual(self.sess["title"], "Native name")
            self.assertEqual(kind, "turn_finished")
            return {"session_id": sid, "type": kind, **payload}

        with patch.object(server, "read_native_session_title", return_value="Native name"), patch.object(
            server, "append_event", side_effect=append,
        ), patch.object(server, "finalize_cross_chat_terminal", new_callable=AsyncMock):
            await server.append_turn_finished_event("title-chat", {"run_id": "run-test"})

    async def test_imported_terminals_do_not_rescan_metadata_per_message(self):
        with patch.object(server, "refresh_native_session_title", new_callable=AsyncMock) as refresh, patch.object(
            server, "append_event", new_callable=AsyncMock, return_value={},
        ), patch.object(server, "finalize_cross_chat_terminal", new_callable=AsyncMock):
            await server.append_turn_finished_event("title-chat", {"run_id": "import_test"})
            await server.append_turn_finished_event("title-chat", {"imported": True})
        refresh.assert_not_awaited()

    async def test_late_provider_title_is_checked_again_when_chat_opens(self):
        with patch.object(server, "read_native_session_title", return_value="Late native title"), patch.object(
            server, "sync_provider_history", new_callable=AsyncMock, return_value={},
        ), patch.object(server, "ACTIVE", {}), patch.object(server, "BUSY_SESSIONS", set()), patch.object(
            server, "SERVER_MAINTENANCE_SESSIONS", set(),
        ), patch.object(server, "SESSION_TURN_TASKS", {}), patch.object(server, "SESSION_LIFECYCLE_LOCKS", {}):
            await server.run_provider_history_sync("title-chat")
        self.assertEqual(self.sess["title"], "Late native title")

    async def test_codex_name_notification_uses_exact_parent_thread(self):
        self.sess["backend"] = "codex"
        self.sess["codex_thread_id"] = "provider-title"
        with patch.object(server, "codex_session_id_for_thread", return_value="title-chat"), patch.object(
            server, "CODEX_SUBAGENT_SESSION_INDEX", {"child": "title-chat"},
        ), patch.object(server, "CODEX_SUBAGENT_STATE", {"child": {
            "session_id": "title-chat", "subagent_status": "running", "run_id": "child-run",
        }}), patch.object(server, "emit_codex_subagent_state", new_callable=AsyncMock) as emit_child:
            await server.project_codex_notification({"method": "thread/name/updated", "params": {"threadId": "child", "threadName": "Child name"}})
            self.assertEqual(self.sess["title"], "First request")
            emit_child.assert_awaited_once_with("title-chat", "child", "running", title="Child name",
                run_id="child-run", inherit_run_id=False, identity_only=True)
            await server.project_codex_notification({"method": "thread/name/updated", "params": {"threadId": "provider-title", "threadName": "Native parent title"}})
            self.assertEqual(self.sess["title"], "Native parent title")

    async def test_auto_title_survives_persisted_session_roundtrip_and_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions_file = Path(directory) / "sessions.json"
            self.store.save = server.SessionStore.save.__get__(self.store)
            with patch.object(server, "SESSIONS_FILE", sessions_file), patch.object(server, "ensure_dirs"):
                await self.adopt()
                await self.store.flush_pending_save()
            reloaded = json.loads(sessions_file.read_text())["title-chat"]
        self.assertTrue(server.session_accepts_auto_title(reloaded))
        self.assertNotIn("_title_source", server.public_session(reloaded))
        self.assertNotIn("_title_auto_value", server.public_session(reloaded, summary=True))

    async def test_create_classifies_client_placeholder_and_explicit_names(self):
        with patch.object(server, "ensure_dirs"), patch.object(server, "append_event", new_callable=AsyncMock):
            for title, expected in ((None, "placeholder"), ("New chat", "placeholder"), ("User title", "manual")):
                created = await self.store.create(server.CreateSessionRequest(title=title, backend="claude", cwd="/tmp"))
                self.assertEqual(created["_title_source"], expected)


if __name__ == "__main__":
    unittest.main()
