"""Cursor CLI import: public exports only, exact native identity, isolated state."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import agent_server as server
import cursor_history
from cursor_agent_client import build_cursor_cmd


def text_event(role, text):
    return {"role": role, "message": {"content": [{"type": "text", "text": text}]}}


class CursorFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cursor-import-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / ".cursor"
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()
        self.enterContext(patch.object(Path, "home", return_value=self.root))
        self.enterContext(patch.dict(os.environ, {"CURSOR_CONFIG_DIR": str(self.config), "CURSOR_DATA_DIR": ""}))
        self.enterContext(patch.object(server, "local_claude_session_candidates", return_value=[]))
        self.enterContext(patch.object(server, "local_codex_session_candidates", return_value=[]))
        self.enterContext(patch.object(server, "other_local_instance_provider_keys", return_value=set()))

    def fixture(self, provider_id="native-cursor", *, cwd=None, title="Cursor native title", sidecar=None, metadata=None, events=None):
        cwd = str(cwd or self.cwd)
        bucket = hashlib.md5(cwd.encode(), usedforsecurity=False).hexdigest()
        directory = self.config / "chats" / bucket / provider_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "meta.json").write_text(json.dumps({
            "schemaVersion": 1, "hasConversation": True, "cwd": cwd,
            "updatedAtMs": 1_790_000_000_000, **(sidecar or {}),
        }))
        with sqlite3.connect(directory / "store.db") as db:
            db.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
            db.execute("INSERT OR REPLACE INTO meta VALUES ('0', ?)", (json.dumps({
                "agentId": provider_id, "name": title, **(metadata or {}),
            }).encode().hex(),))
        transcript = self.config / "projects" / cursor_history.project_slug(cwd) / "agent-transcripts" / provider_id / f"{provider_id}.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        records = events if events is not None else [
            text_event("user", "<timestamp>2026-09-20T00:00:00Z</timestamp>\n<user_query>Original question</user_query>"),
            text_event("assistant", "Original answer"),
        ]
        transcript.write_text("".join(json.dumps(event) + "\n" for event in records))
        return directory, transcript


class CursorDiscoveryTests(CursorFixture, unittest.TestCase):
    def test_native_title_and_exact_identity_are_read_only(self):
        directory, transcript = self.fixture()
        paths = [directory / "meta.json", directory / "store.db", transcript]
        before = [path.read_bytes() for path in paths]
        found = server.local_cursor_session_candidates(set())
        self.assertEqual([(row["backend"], row["provider_session_id"], row["label"], row["cwd"]) for row in found],
                         [("cursor", "native-cursor", "Cursor native title", str(self.cwd))])
        self.assertEqual(before, [path.read_bytes() for path in paths])
        self.assertEqual(server.local_cursor_session_candidates({"native-cursor"}), [])

    def test_placeholder_falls_back_to_sidecar_then_human_prompt(self):
        self.fixture(title=" New Agent ", sidecar={"title": "Sidecar title"})
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "Sidecar title")
        self.fixture(title="New Agent")
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "workspace: Original question")

    def test_invalid_entries_do_not_hide_a_valid_main_session(self):
        self.fixture("valid")
        for index, values in enumerate([
            {"sidecar": {"hasConversation": False}}, {"sidecar": {"isSubagent": True}},
            {"sidecar": {"schemaVersion": 2}}, {"metadata": {"subagentInfo": {"parentId": "parent"}}},
            {"metadata": {"agentId": "foreign"}}, {"sidecar": {"cwd": "/wrong-workspace"}},
            {"sidecar": {"cwd": "relative"}}, {"sidecar": {"cwd": "\ud800"}},
            {"cwd": self.root / "deleted-project"},
        ]):
            self.fixture(f"invalid-{index}", **values)
        self.assertEqual([row.provider_id for row in cursor_history.local_sessions()], ["valid"])

    def test_missing_or_corrupt_sources_are_skipped(self):
        for file in ("meta.json", "store.db", "transcript"):
            with self.subTest(file=file):
                directory, transcript = self.fixture()
                target = transcript if file == "transcript" else directory / file
                saved = target.read_bytes()
                target.unlink()
                self.assertEqual(list(cursor_history.local_sessions()), [])
                target.write_bytes(saved)
        (directory / "meta.json").write_text("{" + "x" * cursor_history.MAX_METADATA_BYTES)
        self.assertEqual(list(cursor_history.local_sessions()), [])

    def test_symlinked_transcript_is_not_followed(self):
        _, transcript = self.fixture()
        outside = self.root / "unrelated.jsonl"
        transcript.rename(outside)
        transcript.symlink_to(outside)
        self.assertEqual(list(cursor_history.local_sessions()), [])

    def test_repeated_titles_are_not_identity_and_copied_ids_are_ambiguous(self):
        self.fixture("first")
        self.fixture("second")
        self.assertEqual(len(server.local_cursor_session_candidates(set())), 2)
        another = self.root / "another-workspace"
        another.mkdir()
        self.fixture("first", cwd=another)
        self.assertEqual([row["provider_session_id"] for row in server.local_cursor_session_candidates(set())], ["second"])

    def test_known_and_other_instance_ids_are_removed_before_limit(self):
        self.fixture("owned")
        self.fixture("available", sidecar={"updatedAtMs": 1_780_000_000_000})
        with patch.object(server, "other_local_instance_provider_keys", return_value={("cursor", "owned")}):
            found = server.local_session_candidates(1, set(), include_cursor=True)
        self.assertEqual([row["provider_session_id"] for row in found], ["available"])
        self.assertEqual(server.local_session_candidates(100, set()), [])
        self.assertEqual(server.local_session_candidates(100, {("cursor", "owned"), ("cursor", "available")}, include_cursor=True), [])

    def test_custom_root_keeps_native_home_export_location(self):
        directory, _ = self.fixture()
        custom = self.root / "custom-config"
        custom.mkdir()
        (self.config / "chats").rename(custom / "chats")
        with patch.dict(os.environ, {"CURSOR_CONFIG_DIR": str(custom)}):
            self.assertEqual(len(list(cursor_history.local_sessions())), 1)
            self.assertIsNotNone(cursor_history.find_session(directory.name, str(self.cwd)))

    def test_resume_rejects_wrong_workspace_and_invalid_ids(self):
        self.fixture()
        for provider_id, cwd in (("../native-cursor", str(self.cwd)), ("native-cursor", str(self.root)), ("native-cursor", "\ud800")):
            self.assertIsNone(cursor_history.find_session(provider_id, cwd))

    def test_custom_data_root_is_independent_from_configuration(self):
        self.fixture()
        data = self.root / "native-data"
        data.mkdir()
        (self.config / "projects").rename(data / "projects")
        with patch.dict(os.environ, {"CURSOR_DATA_DIR": str(data)}):
            self.assertEqual(len(list(cursor_history.local_sessions())), 1)
            self.assertEqual(len(server.cursor_initial_history("native-cursor", str(self.cwd))[1]), 2)

    def test_public_projection_preserves_repeats_and_omits_internal_content(self):
        events = [
            text_event("user", "unwrapped runtime context"),
            {"role": "user", "message": {"content": [{"type": "tool_result", "content": "not a user"}]}},
            text_event("user", "<user_query>Quote </user_query> literally</user_query>"),
            text_event("assistant", "Same reply"), text_event("assistant", "Same reply"),
            {"role": "assistant", "message": {"content": [{"type": "thinking", "text": "private"}, {"type": "tool_use", "text": "tool"}]}},
        ]
        self.fixture(events=events)
        _, items = server.cursor_initial_history("native-cursor", str(self.cwd))
        self.assertEqual([(item["kind"], item["text"]) for item in items], [
            ("user", "Quote </user_query> literally"), ("assistant", "Same reply"), ("assistant", "Same reply"),
        ])

    def test_initial_snapshot_ignores_partial_record_and_detects_changes(self):
        _, transcript = self.fixture()
        with transcript.open("ab") as output:
            output.write(b'{"role":"assistant"')
        self.assertEqual(len(server.cursor_initial_history("native-cursor", str(self.cwd))[1]), 2)
        original = server.provider_history_source_snapshot
        def changed(*args):
            result = original(*args)
            with transcript.open("ab") as output:
                output.write(b'changed')
            return result
        with patch.object(server, "provider_history_source_snapshot", side_effect=changed):
            with self.assertRaises(ValueError):
                server.cursor_initial_history("native-cursor", str(self.cwd))


class CursorImportTests(CursorFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        state = self.root / "server-state"
        state.mkdir()
        self.enterContext(patch.object(server, "STATE_DIR", state))
        self.enterContext(patch.object(server, "SESSIONS_FILE", state / "sessions.json"))
        self.store = server.SessionStore()
        self.enterContext(patch.object(server, "STORE", self.store))
        self.enterContext(patch.object(server, "ensure_dirs", side_effect=lambda session_id=None:
            (server.session_dir(session_id) if session_id else state).mkdir(parents=True, exist_ok=True)))
        self.enterContext(patch.object(server, "ensure_codex_thread_not_pending_fork_cleanup"))
        self.fixture()

    def request(self, **kwargs):
        return server.BulkImportSessionsRequest(items=[{
            "provider_session_id": "native-cursor", "backend": "cursor", "cwd": str(self.cwd), **kwargs,
        }])

    async def test_legacy_and_opt_in_picker(self):
        self.assertEqual((await server.get_local_sessions(limit=100))["sessions"], [])
        self.assertEqual((await server.get_local_sessions(limit=100, include_cursor=True))["sessions"][0]["backend"], "cursor")

    async def test_bulk_import_persists_history_and_native_resume_identity(self):
        results = (await server.bulk_import_sessions_guarded(self.request(), set()))["results"]
        self.assertTrue(results[0]["ok"], results)
        self.assertEqual(results[0]["imported"], 2)
        session = self.store.sessions[results[0]["session_id"]]
        self.assertEqual(session["cursor_session_id"], "native-cursor")
        self.assertEqual(session["cwd"], str(self.cwd))
        self.assertEqual(session["title"], "Cursor native title")
        self.assertNotIn("_history_import_initializing", session)
        command = build_cursor_cmd(session)
        self.assertEqual(command[command.index("--resume") + 1], "native-cursor")
        events = server.read_events(session["id"])
        self.assertEqual([event["prompt"] for event in events if event["type"] == "turn_started"], ["Original question"])
        self.assertEqual([event["text"] for event in events if event["type"] == "assistant_text"], ["Original answer"])
        self.assertEqual(events[-1]["type"], "turn_finished")
        self.assertTrue(events[-1]["imported"])
        self.assertEqual((await server.import_session_history(session, force=True))["imported"], 0)
        again = (await server.bulk_import_sessions_guarded(self.request(), set()))["results"][0]
        self.assertEqual(again["code"], "already_imported")
        self.assertEqual(len(self.store.sessions), 1)
        persisted = json.loads(server.SESSIONS_FILE.read_text())
        self.assertEqual(persisted[session["id"]]["cursor_session_id"], "native-cursor")

    async def test_manual_resume_import_and_explicit_name(self):
        session = await self.store.create(server.CreateSessionRequest(
            backend="cursor", provider_session_id="native-cursor", cwd=str(self.cwd), title="My name",
        ))
        self.assertEqual((await server.import_session_history(session))["imported"], 2)
        self.assertEqual(session["title"], "My name")
        self.assertEqual((await server.import_session_history(session, force=True))["imported"], 0)

    async def test_foreign_claim_and_changed_workspace_do_not_create_sessions(self):
        result = (await server.bulk_import_sessions_guarded(self.request(), {("cursor", "native-cursor")}))["results"][0]
        self.assertEqual(result["code"], "already_imported")
        result = (await server.bulk_import_sessions_guarded(self.request(cwd=str(self.root)), set()))["results"][0]
        self.assertEqual(result["code"], "candidate_changed")
        self.assertEqual(self.store.sessions, {})

    async def test_failed_staging_rolls_back_only_agentsdock_entry(self):
        with patch.object(server, "append_staged_imported_history", side_effect=OSError("test write failure")):
            result = (await server.bulk_import_sessions_guarded(self.request(), set()))["results"][0]
        self.assertEqual(result["code"], "import_failed")
        self.assertEqual(self.store.sessions, {})
        self.assertIsNotNone(cursor_history.find_session("native-cursor", str(self.cwd)))

    async def test_empty_export_creates_no_chat(self):
        self.fixture(events=[])
        result = (await server.bulk_import_sessions_guarded(self.request(), set()))["results"][0]
        self.assertEqual(result["code"], "no_messages")
        self.assertEqual(self.store.sessions, {})
