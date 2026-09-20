"""Resume/import naming uses existing provider metadata, never a model call."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server as server
from test_local_sessions_import import write_claude_transcript, write_codex_transcript


class ImportTitleLabels(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.projects = self.root / "claude"
        self.transcript = self.projects / "project" / "native-chat.jsonl"
        write_claude_transcript(self.transcript, cwd="/work/project", first_user_text="Long original prompt")
        self.enterContext(patch.object(server, "CLAUDE_PROJECTS_ROOT", self.projects))

    def claude_label(self, events):
        with self.transcript.open("a") as stream:
            for event in events:
                stream.write(json.dumps({"sessionId": "native-chat", **event}) + "\n")
        before = self.transcript.read_bytes()
        candidates = server.local_claude_session_candidates(set())
        self.assertEqual(before, self.transcript.read_bytes())
        self.assertEqual(len(candidates), 1)
        return candidates[0]["label"]

    def test_claude_custom_title_wins_over_ai_and_first_message(self):
        with patch.object(server, "claude_transcript_preview", side_effect=AssertionError("unnecessary preview scan")):
            self.assertEqual(self.claude_label([
                {"type": "custom-title", "customTitle": "Hand-picked title"},
                {"type": "ai-title", "aiTitle": "Automatic title"},
            ]), "Hand-picked title")

    def test_claude_latest_ai_title_without_custom_name(self):
        self.assertEqual(self.claude_label([
            {"type": "ai-title", "aiTitle": "Old title"},
            {"type": "ai-title", "aiTitle": "New title"},
        ]), "New title")

    def test_claude_latest_manual_title_without_changing_timestamp(self):
        label = self.claude_label([
            {"type": "custom-title", "customTitle": "Old name"},
            {"type": "custom-title", "customTitle": "New name"},
        ])
        self.assertEqual(label, "New name")
        self.assertEqual(server.local_claude_session_candidates(set())[0]["updated_at"],
                         server.iso_from_timestamp(self.transcript.stat().st_mtime))

    def test_claude_foreign_sidechain_summary_and_bad_names_keep_preview(self):
        self.assertEqual(self.claude_label([
            {"type": "ai-title", "sessionId": "other", "aiTitle": "Wrong chat"},
            {"type": "custom-title", "isSidechain": True, "customTitle": "Child"},
            {"type": "summary", "summary": "Not a title"},
            {"type": "ai-title", "aiTitle": "New chat"},
            {"type": "custom-title", "customTitle": {"bad": "shape"}},
        ]), "project: Long original prompt")

    def test_claude_tail_title_after_large_middle(self):
        with self.transcript.open("a") as stream:
            stream.write(json.dumps({"padding": "x" * 3000}) + "\n")
        with patch.object(server, "CLAUDE_TRANSCRIPT_CWD_SCAN_BYTES", 512):
            self.assertEqual(self.claude_label([
                {"type": "ai-title", "aiTitle": "Saved at end"},
            ]), "Saved at end")

    def test_claude_malformed_metadata_does_not_break_list(self):
        with self.transcript.open("ab") as stream:
            stream.write(b'not json\n{"bad utf8":"\xff"}\n')
        self.assertEqual(self.claude_label([]), "project: Long original prompt")

    def test_deeply_nested_claude_and_codex_metadata_is_skipped(self):
        nested = "[" * 2000 + "0" + "]" * 2000
        with self.transcript.open("a") as stream:
            stream.write(nested + "\n")
        self.assertEqual(self.claude_label([
            {"type": "ai-title", "aiTitle": "Valid title"},
        ]), "Valid title")
        index = self.root / "session_index.jsonl"
        index.write_text(nested + "\n" + json.dumps({"id": "native-chat", "thread_name": "Valid title"}) + "\n")
        with patch.object(server, "CODEX_SESSION_INDEX_PATH", index):
            self.assertEqual(server.codex_session_index_thread_names(), {"native-chat": "Valid title"})

    def test_claude_native_names_are_safe_bounded_display_only(self):
        self.assertEqual(self.claude_label([
            {"type": "ai-title", "aiTitle": "  中文\n title\t\x00\u202e " + "x" * 200},
        ]), ("中文 title " + "x" * 200)[:120])

    def test_claude_known_provider_ids_stay_excluded(self):
        self.claude_label([{"type": "ai-title", "aiTitle": "Native title"}])
        self.assertEqual(server.local_claude_session_candidates({"native-chat"}), [])

    def test_codex_latest_valid_index_title_is_shared_with_resume_reader(self):
        index = self.root / "session_index.jsonl"
        index.write_text("\n".join(json.dumps(event) for event in [
            {"id": "native-chat", "thread_name": "Old title"},
            {"id": "native-chat", "thread_name": "  Native\n title\x00\u202e "},
            {"id": "native-chat", "thread_name": {"invalid": "shape"}},
            {"id": "other", "thread_name": "Other name"},
        ]) + "\n")
        transcripts = self.root / "codex"
        write_codex_transcript(transcripts / "rollout.jsonl", session_id="native-chat",
                               cwd="/work/project", first_user_text="Original Codex prompt")
        before = index.read_bytes()
        with patch.object(server, "CODEX_SESSION_INDEX_PATH", index), patch.object(
            server, "CODEX_SESSIONS_ROOT", transcripts,
        ), patch.object(server, "codex_transcript_preview", side_effect=AssertionError("unnecessary preview")):
            candidate = server.local_codex_session_candidates(set())[0]
            self.assertEqual(candidate["label"], "Native title")
            self.assertEqual(server.read_native_session_title({"backend": "codex", "session_id": "native-chat"}), candidate["label"])
            self.assertEqual(server.local_codex_session_candidates({"native-chat"}), [])
        self.assertEqual(index.read_bytes(), before)

    def test_codex_missing_invalid_and_placeholder_titles_keep_preview(self):
        index = self.root / "session_index.jsonl"
        transcripts = self.root / "codex"
        write_codex_transcript(transcripts / "rollout.jsonl", session_id="native-chat",
                               cwd="/work/project", first_user_text="Original Codex prompt")
        with patch.object(server, "CODEX_SESSION_INDEX_PATH", index), patch.object(server, "CODEX_SESSIONS_ROOT", transcripts):
            for value in (None, "", "New chat", "\x00\u202e", {}, "x" * 4097):
                with self.subTest(value=str(value)[:30]):
                    index.write_text(json.dumps({"id": "native-chat", "thread_name": value}) + "\n")
                    self.assertEqual(server.local_codex_session_candidates(set())[0]["label"],
                                     "project: Original Codex prompt")


class CursorNativeMetadata(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        self.cwd = str(self.root / "project")
        bucket = hashlib.md5(self.cwd.encode(), usedforsecurity=False).hexdigest()
        self.database = self.config / "chats" / bucket / "native-chat" / "store.db"
        self.enterContext(patch.dict(os.environ, {"CURSOR_CONFIG_DIR": str(self.config)}))
        self.sess = {"backend": "cursor", "session_id": "native-chat", "cwd": self.cwd}

    def write_metadata(self, metadata=None, *, raw=None):
        if metadata is None:
            metadata = {"agentId": "native-chat", "name": "Native Cursor title", "latestRootBlobId": "ab" * 32}
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT OR REPLACE INTO meta VALUES ('0', ?)",
                               (raw if raw is not None else json.dumps(metadata).encode().hex(),))
            connection.commit()
        finally:
            connection.close()

    def read(self, **changes):
        return server.read_native_session_title({**self.sess, **changes})

    def test_exact_name_without_reading_conversation_blobs_or_writing(self):
        self.write_metadata({"agentId": "native-chat", "name": "  修复\n Cursor\x00\u202e  "})
        before = self.database.read_bytes()
        # The fixture deliberately has no blobs table: naming never needs it.
        self.assertEqual(self.read(), "修复 Cursor")
        self.assertEqual(self.database.read_bytes(), before)

    def test_missing_store_and_directory_are_not_created(self):
        self.assertIsNone(self.read())
        self.assertFalse(self.config.exists())

    def test_wrong_workspace_and_other_session_never_supply_name(self):
        self.write_metadata()
        for changes in ({"cwd": str(self.root / "other")}, {"cwd": None}, {"cwd": "relative"},
                        {"session_id": "neighbor"}, {"session_id": "../../escape"}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.read(**changes))

    def test_identity_child_and_unknown_title_shapes_fail_closed(self):
        for metadata in (
            {"agentId": "other", "name": "Foreign"},
            {"name": "No identity"},
            {"agentId": "native-chat", "name": "Child", "subagentInfo": {}},
            {"agentId": "native-chat", "name": {"bad": "shape"}},
            {"agentId": "native-chat", "name": "New Agent"},
            {"agentId": "native-chat", "name": "x" * 4097},
            [],
        ):
            with self.subTest(metadata=str(metadata)[:80]):
                self.write_metadata(metadata)
                self.assertIsNone(self.read())

    def test_malformed_oversized_and_non_text_values_fail_closed(self):
        nested = ("[" * 2000 + "0" + "]" * 2000).encode().hex()
        for raw in ("not hex", "ff", "7b", "00" * 20000, b"abcd", "7b7d", nested):
            with self.subTest(raw=str(raw)[:20]):
                self.write_metadata(raw=raw)
                self.assertIsNone(self.read())

    def test_busy_store_keeps_fallback(self):
        self.write_metadata()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("BEGIN EXCLUSIVE")
            self.assertIsNone(self.read())
        finally:
            connection.rollback()
            connection.close()

    def test_missing_schema_or_corrupt_db_keeps_fallback(self):
        self.database.parent.mkdir(parents=True)
        for data in (b"", b"not a sqlite database"):
            self.database.write_bytes(data)
            self.assertIsNone(self.read())
            self.assertEqual(self.database.read_bytes(), data)

    def test_wal_metadata_is_visible_without_a_checkpoint(self):
        self.write_metadata()
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("UPDATE meta SET value=? WHERE key='0'", (
                json.dumps({"agentId": "native-chat", "name": "Newest WAL name"}).encode().hex(),
            ))
            connection.commit()
            before = self.database.read_bytes()
            wal = self.database.with_name("store.db-wal")
            before_wal = wal.read_bytes()
            self.assertEqual(self.read(), "Newest WAL name")
            self.assertEqual(self.database.read_bytes(), before)
            self.assertEqual(wal.read_bytes(), before_wal)
        finally:
            connection.close()

    def test_database_open_is_read_only(self):
        self.write_metadata()
        connect = sqlite3.connect
        with patch.object(server.sqlite3, "connect", wraps=connect) as spy:
            self.assertEqual(self.read(), "Native Cursor title")
        self.assertEqual(spy.call_args.args[0], self.database.resolve().as_uri() + "?mode=ro")
        self.assertTrue(spy.call_args.kwargs["uri"])

    def test_linked_database_or_workspace_is_not_followed(self):
        self.write_metadata()
        actual = self.database.with_name("actual.db")
        self.database.rename(actual)
        self.database.symlink_to(actual)
        self.assertIsNone(self.read())
        self.database.unlink()
        actual.rename(self.database)
        directory = self.database.parent
        moved = directory.with_name("moved")
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.assertIsNone(self.read())

    def test_cursor_config_root_precedence_matches_cli(self):
        self.write_metadata()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "different"), "CURSOR_DATA_DIR": str(self.root / "not-chats")}):
            self.assertEqual(self.read(), "Native Cursor title")

        xdg_config = self.root / "xdg" / "cursor"
        xdg_config.parent.mkdir()
        self.config.rename(xdg_config)
        with patch.dict(os.environ, {"CURSOR_CONFIG_DIR": "", "XDG_CONFIG_HOME": str(xdg_config.parent)}):
            self.assertEqual(self.read(), "Native Cursor title")
        default_config = self.root / ".cursor"
        xdg_config.rename(default_config)
        with patch.dict(os.environ, {"CURSOR_CONFIG_DIR": "", "XDG_CONFIG_HOME": ""}), patch.object(Path, "home", return_value=self.root):
            self.assertEqual(self.read(), "Native Cursor title")

    def test_relative_config_is_resolved_from_provider_working_directory(self):
        self.write_metadata()
        Path(self.cwd).mkdir()
        with patch.dict(os.environ, {"CURSOR_CONFIG_DIR": "../config"}):
            self.assertEqual(self.read(), "Native Cursor title")


class ResumeTitleCreation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = server.SessionStore()
        self.store.save = AsyncMock()
        self.enterContext(patch.object(server, "STORE", self.store))
        self.ensure_dirs = self.enterContext(patch.object(server, "ensure_dirs"))
        self.enterContext(patch.object(server, "append_event", new_callable=AsyncMock))
        self.enterContext(patch.object(server, "ensure_codex_thread_not_pending_fork_cleanup"))
        self.enterContext(patch.object(server.CODEX_PROVIDER_STORE, "require_thread"))
        self.native_reader = server.read_native_session_title
        self.reader = self.enterContext(patch.object(server, "read_native_session_title", return_value="Existing native title"))
        self.generator = self.enterContext(patch.object(server.title_generation, "generate_title", new_callable=AsyncMock))

    async def create(self, backend="cursor", **kwargs):
        return await self.store.create(server.CreateSessionRequest(
            backend=backend, provider_session_id="native-chat", cwd="/work", **kwargs,
        ))

    async def test_all_providers_use_native_title_before_returning_resume_response(self):
        for backend in ("claude", "codex", "cursor"):
            for title in (None, "New chat", f"Resumed {backend.title()} native-c"):
                with self.subTest(backend=backend, title=title):
                    created = await self.create(backend, title=title)
                    self.assertEqual(created["title"], "Existing native title")
                    self.assertEqual(created["_title_source"], "provider")
                    self.assertEqual(created["_title_auto_value"], created["title"])
                    self.assertEqual(created["session_id"], "native-chat")
        self.generator.assert_not_awaited()

    async def test_explicit_name_always_wins(self):
        for title in ("My manual name", "Resumed Cursor other-id", "Resumed chat"):
            created = await self.create(title=title)
            self.assertEqual(created["title"], title)
            self.assertEqual(created["_title_source"], "manual")
        self.reader.assert_not_called()

    async def test_missing_unreadable_or_slow_metadata_keeps_resume_working(self):
        for error in (None, OSError("unavailable"), sqlite3.DatabaseError("unknown schema"), TimeoutError()):
            self.reader.return_value = None
            self.reader.side_effect = error
            created = await self.create()
            self.assertEqual(created["title"], "Resumed Cursor native-c")
            self.assertEqual(created["_title_source"], "placeholder")

    async def test_new_chats_forks_and_staged_imports_do_not_probe_metadata(self):
        await self.store.create(server.CreateSessionRequest(backend="cursor"))
        for flags in ({"parent_id": "parent"}, {"initializing_fork": True}, {"initializing_import": True}):
            await self.store.create(server.CreateSessionRequest(backend="cursor", provider_session_id="native-chat"), **flags)
        self.reader.assert_not_called()

    async def test_placeholder_wording_is_manual_when_user_renames_existing_chat(self):
        created = await self.create()
        await self.store.update(created["id"], {"title": "Resumed Cursor native-c"})
        self.assertEqual(created["_title_source"], "manual")
        self.reader.reset_mock()
        await server.refresh_native_session_title(created["id"])
        self.reader.assert_not_called()

    async def test_resume_endpoint_returns_native_title_without_import_or_model_call(self):
        with patch.object(server, "import_session_history", new_callable=AsyncMock) as history:
            response = await server.create_session(server.CreateSessionRequest(
                backend="cursor", provider_session_id="native-chat", cwd="/work",
                title="Resumed Cursor native-c", import_history=False,
            ))
            self.assertEqual(response["session"]["title"], "Existing native title")
            history.assert_not_awaited()
        self.generator.assert_not_awaited()

    async def test_real_provider_metadata_flows_into_created_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.reader.side_effect = self.native_reader
            projects = root / "claude"
            transcript = projects / "project" / "native-chat.jsonl"
            write_claude_transcript(transcript, cwd="/work", first_user_text="First prompt")
            with transcript.open("a") as stream:
                stream.write(json.dumps({"type": "ai-title", "sessionId": "native-chat", "aiTitle": "Claude title"}) + "\n")
            index = root / "session_index.jsonl"
            index.write_text(json.dumps({"id": "native-chat", "thread_name": "Codex title"}) + "\n")
            bucket = hashlib.md5(b"/work", usedforsecurity=False).hexdigest()
            database = root / "cursor" / "chats" / bucket / "native-chat" / "store.db"
            database.parent.mkdir(parents=True)
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO meta VALUES ('0', ?)", (
                json.dumps({"agentId": "native-chat", "name": "Cursor title"}).encode().hex(),
            ))
            connection.commit()
            connection.close()
            with patch.object(server, "CLAUDE_PROJECTS_ROOT", projects), patch.object(server, "CODEX_SESSION_INDEX_PATH", index), patch.dict(
                os.environ, {"CURSOR_CONFIG_DIR": str(root / "cursor")},
            ):
                for backend in ("claude", "codex", "cursor"):
                    created = await self.create(backend, title=f"Resumed {backend.title()} native-c")
                    self.assertEqual(created["title"], f"{backend.title()} title")
        self.generator.assert_not_awaited()

    async def test_actual_deadline_does_not_block_resume(self):
        release = threading.Event()
        self.reader.side_effect = lambda _session: release.wait(3) and "Too late"
        try:
            created = await asyncio.wait_for(self.create(), timeout=2)
            self.assertEqual(created["title"], "Resumed Cursor native-c")
        finally:
            release.set()
        self.assertEqual(created["_title_source"], "placeholder")

    async def test_cancelled_resume_does_not_create_orphaned_session(self):
        entered, release = threading.Event(), threading.Event()
        def pending(_session):
            entered.set()
            release.wait(3)
            return "Late result"
        self.reader.side_effect = pending
        task = asyncio.create_task(self.create())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        self.assertEqual(self.store.sessions, {})
        self.ensure_dirs.assert_not_called()

    async def test_missing_title_can_be_adopted_later_without_running_provider(self):
        self.reader.return_value = None
        created = await self.create()
        self.reader.return_value = "Now available"
        await server.refresh_native_session_title(created["id"])
        self.assertEqual(created["title"], "Now available")
        self.assertEqual(created["_title_source"], "provider")
        self.generator.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
