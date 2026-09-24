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


def cursor_provider_envelope(prompt, *, memory=None):
    session = {"backend": "cursor", "cwd": "/tmp", "memory_seed": memory or ""}
    return server.build_cursor_provider_prompt(
        "sess_preview_fixture", session, prompt, Path("/tmp/preview-fixture-manifest.json"),
    )[0]


def native_tool_binding():
    name = "plugin-agentsdock-" + "a" * 24 + "-native"
    return (
        "\n\n[AgentsDock tool binding]\n"
        f"For this turn only, use MCP server `{name}`, tool `run` "
        f"(Cursor tool `{name}-run`) for AgentsDock helpers. "
        "This supersedes older helper commands and earlier tool bindings. "
        "No shell fallback, credential lookup, or full-access permission is needed. "
        "Only this top-level agent may call it.\n[End AgentsDock tool binding]"
    )


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
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "Original question")

    def test_native_title_wins_over_sidecar_and_prompt(self):
        self.fixture(title="Original Cursor name", sidecar={"title": "Stale sidecar name"})
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "Original Cursor name")

    def test_sanitize_before_falling_back_to_sidecar(self):
        for title in (None, 42, "\x00\u200b", "New\u200b Agent", "x" * 4097):
            with self.subTest(title_type=type(title).__name__):
                self.fixture(title=title, sidecar={"title": "Native\x00 sidecar\n title"})
                self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"],
                                 "Native sidecar title")

    def test_cursor_names_are_not_agentsdock_placeholders(self):
        for title in ("New chat", "Untitled", "New session - 2026-09-21"):
            with self.subTest(title=title):
                self.fixture(title=title)
                self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], title)
                self.assertEqual(server.cursor_native_session_title("native-cursor", str(self.cwd)), title)

    def test_fallback_uses_first_human_message_not_internal_or_latest_text(self):
        self.fixture(title="New Agent", events=[
            text_event("user", "Internal context, not a human message"),
            {"role": "user", "message": {"content": [{"type": "tool_result", "content": "Tool result"}]}},
            text_event("assistant", "Earlier assistant content"),
            text_event("user", "<user_query>" + "\n " * 100 + "First request\n continued</user_query>"),
            text_event("assistant", "An answer"),
            text_event("user", "<user_query>Latest request</user_query>"),
        ])
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"],
                         "First request continued")

    def test_preview_is_message_text_not_a_placeholder_title(self):
        for message, expected in (("New chat", "New chat"), ("New Agent", "New Agent"),
                                  ("Fix\x00 the\n issue\u202e", "Fix the issue")):
            with self.subTest(message=message):
                self.fixture(title="New Agent", events=[text_event("user", f"<user_query>{message}</user_query>")])
                candidate = server.local_cursor_session_candidates(set())[0]
                self.assertEqual(candidate["label"], expected)
                self.assertEqual(candidate["cwd"], str(self.cwd))

    def test_no_native_title_or_user_text_uses_identity_not_project(self):
        self.fixture(title="New Agent", events=[text_event("assistant", "Assistant-only export")])
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "Cursor chat native-c")

    def test_identical_previews_in_different_projects_stay_distinct(self):
        other = self.root / "another-project"
        other.mkdir()
        self.fixture("first", title="New Agent")
        self.fixture("second", title="New Agent", cwd=other)
        rows = server.local_cursor_session_candidates(set())
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["label"] for row in rows}, {"Original question"})
        self.assertEqual({row["cwd"] for row in rows}, {str(self.cwd), str(other)})

    def test_actual_server_envelope_is_not_the_first_message_preview(self):
        wrapped = cursor_provider_envelope("Fix the real issue")
        event = text_event("user", f"<user_query>\n{wrapped}\n</user_query>")
        directory, transcript = self.fixture(title="New Agent", events=[event])
        paths = [directory / "store.db", directory / "meta.json", transcript]
        before = [path.read_bytes() for path in paths]
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "Fix the real issue")
        self.assertEqual(server.cursor_history_event_item(event, for_preview=True)["text"], "Fix the real issue")
        # This change is display-only; do not silently alter history projection.
        self.assertTrue(server.cursor_history_event_item(event)["text"].startswith("[AgentsDock provider instructions]"))
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_preview_uses_real_prompt_after_length_delimited_memory(self):
        prompt = "[Current user prompt]\nThis marker is part of my example"
        wrapped = cursor_provider_envelope(prompt, memory="Context with [Current user prompt] inside it")
        self.assertEqual(server.cursor_import_user_preview(wrapped), prompt)

    def test_resumed_current_prompt_requires_exact_tool_binding(self):
        wrapped = "[Current user prompt]\nActual request" + native_tool_binding()
        self.assertEqual(server.cursor_import_user_preview(wrapped), "Actual request")
        for malformed in (wrapped.replace("plugin-agentsdock-", "plugin-other-", 1),
                          wrapped.removesuffix("[End AgentsDock tool binding]")):
            self.assertEqual(server.cursor_import_user_preview(malformed), malformed)

    def test_current_prompt_with_generated_authority_suffix(self):
        authority = server.cross_chat_provider_authority_block(
            [], server.cross_chat_authority_path("run_preview_fixture", "a" * 32),
            "sess_preview_fixture", {"publish"}, "blocked", compact=True,
        )
        wrapped = "[Current user prompt]\nActual request" + authority
        self.assertEqual(server.cursor_import_user_preview(wrapped), "Actual request")

    def test_quoted_and_incomplete_envelopes_are_preserved(self):
        valid = cursor_provider_envelope("Actual request")
        for text in (
            "[Current user prompt]\nA native CLI user quotation",
            "Please explain this:\n" + valid,
            "```text\n" + valid + "\n```",
            valid.replace("[End AgentsDock provider instructions]", "[Other footer]"),
            valid.replace("You are operating through AgentsDock, backed by AgentsServer.", "Quoted instructions"),
            cursor_provider_envelope("Actual request", memory="abc").replace("chars=3", "chars=99"),
        ):
            with self.subTest(prefix=text[:35]):
                self.assertEqual(server.cursor_import_user_preview(text), text)

    def test_native_title_wins_even_when_first_message_has_policy(self):
        self.fixture(title="Original name", events=[text_event("user", "<user_query>" + cursor_provider_envelope("Actual request") + "</user_query>")])
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "Original name")

    def test_empty_generated_prompt_does_not_hide_the_first_real_message(self):
        self.fixture(title="New Agent", events=[
            text_event("user", "<user_query>" + cursor_provider_envelope("") + "</user_query>"),
            text_event("user", "<user_query>First real request</user_query>"),
        ])
        self.assertEqual(server.local_cursor_session_candidates(set())[0]["label"], "First real request")

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

    async def test_bulk_import_keeps_unwrapped_picker_title(self):
        self.fixture(title="New Agent", events=[
            text_event("user", "<user_query>" + cursor_provider_envelope("The first actual question") + "</user_query>"),
            text_event("assistant", "An answer"),
        ])
        picker = await server.get_local_sessions(limit=100, include_cursor=True)
        self.assertEqual(picker["sessions"][0]["label"], "The first actual question")
        result = (await server.bulk_import_sessions_guarded(self.request(), set()))["results"][0]
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.store.sessions[result["session_id"]]["title"], "The first actual question")

    async def test_picker_name_is_preserved_on_import_without_generation(self):
        cases = [
            ("Original Cursor name", "Original Cursor name"),
            ("New Agent", "Original question"),
            ("New chat", "New chat"),
            ("Untitled", "Untitled"),
        ]
        with patch.object(server, "generate_session_title", new_callable=AsyncMock) as generate:
            for index, (native_title, expected) in enumerate(cases):
                with self.subTest(title=native_title):
                    provider_id = f"named-cursor-{index}"
                    self.fixture(provider_id, title=native_title)
                    picker = await server.get_local_sessions(limit=100, include_cursor=True)
                    candidate = next(row for row in picker["sessions"] if row["provider_session_id"] == provider_id)
                    self.assertEqual(candidate["label"], expected)
                    request = server.BulkImportSessionsRequest(items=[{
                        "provider_session_id": provider_id, "backend": "cursor", "cwd": str(self.cwd),
                    }])
                    result = (await server.bulk_import_sessions_guarded(request, set()))["results"][0]
                    self.assertTrue(result["ok"], result)
                    session = self.store.sessions[result["session_id"]]
                    self.assertEqual(session["title"], expected)
                    self.assertEqual(session["_title_source"], "manual")
                    self.assertFalse(server.generated_title_eligible(session))
                    persisted = json.loads(server.SESSIONS_FILE.read_text())
                    self.assertEqual(persisted[session["id"]]["title"], expected)
            generate.assert_not_called()

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
