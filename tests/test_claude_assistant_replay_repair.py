"""Exact scheduled public-output repairs; synthetic bounded files, no server import."""
import hashlib
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import claude_history_repair as repair
from tests.test_claude_history_provenance_isolated import load_projection
from tests.test_recent_scheduled_history_repair import encode

CLEAN_ASSISTANT_TEXT = load_projection()["clean_assistant_text"]


class AssistantReplayRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "provider-one.jsonl"
        self.events = self.root / "events.jsonl"
        self.cache = repair.ClaudeMetadataRepairCache()
        common = {"session_id": "chat-one", "backend": "claude"}
        native = {**common, "run_id": "native-job", "purpose": "scheduled_job", "job_id": "job-one"}
        self.source_rows = [{"type": "assistant", "uuid": "source-one", "sessionId": "provider-one",
                             "timestamp": "2026-09-10T12:00:02.321Z",
                             "message": {"content": [{"type": "text", "text": "Full public report ending."}]}}]
        self.native = {**native, "seq": 102, "type": "reasoning_summary", "phase": "commentary",
                       "text": "Full public report ending.", "ts": "2026-09-10T12:00:02Z"}
        self.imported = {**common, "seq": 107, "run_id": "import_shared", "type": "assistant_text",
                         "imported": True, "text": self.native["text"], "ts": self.source_rows[0]["timestamp"],
                         "provider_origin": {"provider": "claude", "event_id": "source-one",
                                             "session_id": "provider-one", "timestamp": self.source_rows[0]["timestamp"]}}
        self.checkpoint = {"version": 1, "previous_present": False, "previous_source_offset": 0,
                           "previous_source_digest": "", "cursor": {"version": 1, "backend": "claude",
                           "provider_session_id": "provider-one", "source_path": str(self.source)}}
        self.rows = [
            {**native, "seq": 101, "type": "turn_started", "prompt": "Scheduled request", "ts": "2026-09-10T12:00:01Z"},
            self.native,
            {**native, "seq": 103, "type": "provider_session", "provider_session_id": "provider-one", "ts": "2026-09-10T12:00:03Z"},
            {**native, "seq": 104, "type": "turn_finished", "exit_code": 0, "ts": "2026-09-10T12:00:03Z"},
            {**common, "seq": 105, "run_id": "import_shared", "type": "history_imported", "provider_session_id": "provider-one",
             "source_path": str(self.source), "_history_sync_checkpoint": self.checkpoint},
            {**common, "seq": 106, "run_id": "import_shared", "type": "turn_started", "imported": True, "prompt": "Unproven input"},
            self.imported,
            {**common, "seq": 108, "run_id": "import_shared", "type": "turn_started", "imported": True, "prompt": "Genuine follow-up"},
            {**common, "seq": 109, "run_id": "import_shared", "type": "assistant_text", "imported": True, "text": "Unrelated answer"},
            {**common, "seq": 110, "run_id": "import_shared", "type": "turn_finished", "imported": True},
        ]

    def prepare(self, *, oversized=False, normalize_assistant=None):
        prefix = encode([{"type": "progress", "data": "x" * 1000}] * 20) if oversized else b""
        raw = prefix + encode(self.source_rows)
        self.source.write_bytes(raw)
        stamp = self.source.stat()
        self.checkpoint["cursor"].update(source_offset=len(raw), source_digest=hashlib.sha256(raw).hexdigest(),
                                          source_dev=stamp.st_dev, source_ino=stamp.st_ino)
        event_prefix = [{"seq": index, "type": "raw_event", "raw": "x" * 1000} for index in range(1, 21)] if oversized else []
        self.events.write_bytes(encode(event_prefix + self.rows))
        with patch.object(repair, "MAX_EVENTS_BYTES", 8192 if oversized else repair.MAX_EVENTS_BYTES):
            return self.cache.prepare("chat-one", "provider-one", self.events, self.root, lambda row: None,
                                      normalize_full_user=lambda row: None, normalize_assistant=normalize_assistant)

    def decorate(self, prefix="✅ "):
        self.native["provider_message_id"] = "source-one"
        text = prefix + self.native["text"]
        self.source_rows[0]["message"]["content"][0]["text"] = text
        self.imported["text"] = text

    def test_decorated_exact_uuid_repairs_small_recent_and_historical_page_without_writes(self):
        for prefix in ("✅ ", "🎉 ", "👉 ", ":white_check_mark: "):
            for oversized in (False, True):
                with self.subTest(prefix=prefix, oversized=oversized):
                    self.setUp()
                    self.decorate(prefix)
                    self.assertTrue(self.prepare(oversized=oversized, normalize_assistant=CLEAN_ASSISTANT_TEXT))
                    before = (self.events.read_bytes(), self.source.read_bytes())
                    projected = self.cache.project_event("chat-one", self.imported)
                    self.assertTrue(projected["metadata_only"])
                    self.assertEqual(projected["text"], "")
                    self.assertIsNone(self.cache.project_event("chat-one", self.native))
                    signature = self.cache.signature("chat-one")
                    window = self.cache.prepare_window(
                        "chat-one", "provider-one", self.events, self.root, lambda row: None,
                        event_window_end=self.events.stat().st_size, normalize_full_user=lambda row: None,
                        normalize_assistant=CLEAN_ASSISTANT_TEXT)
                    self.assertTrue(window.project_event(self.imported)["metadata_only"])
                    self.assertEqual(self.cache.signature("chat-one"), signature)
                    self.assertEqual((self.events.read_bytes(), self.source.read_bytes()), before)

    def test_decorated_repair_fails_visible_without_complete_unambiguous_identity_and_text(self):
        mutations = [lambda: self.native.pop("provider_message_id"),
                     lambda: self.native.update(provider_message_id="different-native"),
                     lambda: self.imported["provider_origin"].update(session_id="other-provider"),
                     lambda: self.imported["provider_origin"].update(timestamp="2026-09-10T12:00:02.322Z"),
                     lambda: self.imported.update(text="✅ Different public reply"),
                     lambda: self.native.update(text="Full public report different ending."),
                     lambda: self.rows.insert(2, {**self.native, "text": "🎉 " + self.native["text"]}),
                     lambda: self.source_rows.append({**self.source_rows[0], "timestamp": "2026-09-10T12:00:02.322Z"}),
                     lambda: self.rows[3].update(exit_code=1),
                     lambda: self.imported.update(provider_user_authored=True)]
        for oversized in (False, True):
            for mutate in mutations:
                with self.subTest(oversized=oversized, mutation=mutate):
                    self.setUp()
                    self.decorate()
                    mutate()
                    self.prepare(oversized=oversized, normalize_assistant=CLEAN_ASSISTANT_TEXT)
                    self.assertIsNone(self.cache.project_event("chat-one", self.imported))

    def test_distinct_uuid_with_same_cleaned_text_is_still_a_separate_reply(self):
        self.decorate()
        other_source = {**self.source_rows[0], "uuid": "source-two"}
        other_import = {**self.imported, "seq": 109,
                        "provider_origin": {**self.imported["provider_origin"], "event_id": "source-two"}}
        self.source_rows.append(other_source)
        self.rows[-2] = other_import
        self.prepare(normalize_assistant=CLEAN_ASSISTANT_TEXT)
        self.assertIsNotNone(self.cache.project_event("chat-one", self.imported))
        self.assertIsNone(self.cache.project_event("chat-one", other_import))

    def test_normalization_is_opt_in_nonempty_and_cannot_repair_modified_target(self):
        self.decorate()
        self.prepare()
        self.assertIsNone(self.cache.project_event("chat-one", self.imported))
        self.cache.forget("chat-one")
        self.prepare(normalize_assistant=lambda text: "")
        self.assertIsNone(self.cache.project_event("chat-one", self.imported))
        self.cache.forget("chat-one")
        self.prepare(normalize_assistant=CLEAN_ASSISTANT_TEXT)
        self.assertIsNotNone(self.cache.project_event("chat-one", self.imported))
        self.assertIsNone(self.cache.project_event("chat-one", {**self.imported, "text": "🎉 Full public report ending."}))

    def test_small_and_recent_exact_public_source_repairs_only_one_event(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.assertTrue(self.prepare(oversized=oversized))
                result = self.cache.project_event("chat-one", self.imported)
                self.assertEqual(result["provider_history_repair"], "source_proven_assistant_replay")
                self.assertEqual(result["text"], "")
                self.assertTrue(result["metadata_only"])
                self.assertEqual(self.imported["text"], "Full public report ending.")
                self.assertIsNone(self.cache.project_event("another-chat", self.imported))
                for row in self.rows:
                    if row is not self.imported:
                        self.assertIsNone(self.cache.project_event("chat-one", row))
                with patch.object(repair, "_regular_stamp", side_effect=AssertionError("Per-event I/O")):
                    self.assertIsNotNone(self.cache.project_event("chat-one", self.imported))
                    self.assertFalse(self.cache.prepare("chat-one", "provider-one", self.events, self.root, lambda row: None))

    def test_conflicting_private_unowned_or_full_text_changed_output_stays_visible(self):
        mutations = [lambda: self.native.pop("phase"), lambda: self.native.update(text="Full public report different ending."),
                     lambda: self.rows[2].update(provider_session_id="other-provider"),
                     lambda: self.rows[3].update(stopped=True), lambda: self.imported.update(phase="final_answer"),
                     lambda: self.imported.update(provider_user_authored=True),
                     lambda: self.source_rows[0].update(isSidechain=True),
                     lambda: self.imported["provider_origin"].update(event_id="different-source"),
                     lambda: self.native.update(ts="2026-09-10T12:00:02.320Z")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp()
                mutate()
                self.prepare()
                self.assertIsNone(self.cache.project_event("chat-one", self.imported))

    def test_distinct_source_occurrences_cannot_share_one_native_credit(self):
        self.source_rows.append({**self.source_rows[0], "uuid": "different-source"})
        self.prepare()
        self.assertIsNone(self.cache.project_event("chat-one", self.imported))
        self.native["provider_message_id"] = "source-one"
        self.cache.forget("chat-one")
        self.assertTrue(self.prepare())
        self.assertIsNotNone(self.cache.project_event("chat-one", self.imported))

    def test_duplicate_native_credit_or_import_origin_is_ambiguous(self):
        for native in (True, False):
            with self.subTest(native=native):
                self.setUp()
                if native:
                    self.rows.insert(2, {**self.native, "seq": 102})
                else:
                    self.rows.insert(-1, {**self.imported, "seq": 109})
                self.prepare()
                self.assertIsNone(self.cache.project_event("chat-one", self.imported))

    def test_explicit_final_and_matching_terminal_result_are_public(self):
        self.native.update(type="assistant_text", phase="final_answer")
        self.assertTrue(self.prepare())
        self.assertIsNotNone(self.cache.project_event("chat-one", self.imported))
        self.native.pop("phase")
        self.rows[3]["result_text"] = self.native["text"]
        self.cache.forget("chat-one")
        self.assertTrue(self.prepare())

    def test_historical_page_repairs_assistant_without_changing_global_signature(self):
        self.prepare(oversized=True)
        end = self.events.stat().st_size
        with self.events.open("ab") as stream:
            stream.write(encode([{"seq": 200 + index, "type": "raw_event", "raw": "x" * 1000}
                                 for index in range(24)]))
        self.cache.forget("chat-one")
        with patch.object(repair, "MAX_EVENTS_BYTES", 8192):
            window = self.cache.prepare_window("chat-one", "provider-one", self.events, self.root,
                                                lambda row: None, event_window_end=end,
                                                normalize_full_user=lambda row: None)
        result = window.project_event(self.imported)
        self.assertEqual(result["provider_history_repair"], "source_proven_assistant_replay")
        self.assertEqual(result["text"], "")
        self.assertEqual(self.imported["text"], "Full public report ending.")
        self.assertEqual(self.cache.signature("chat-one"), frozenset())
        for row in self.rows:
            if row is not self.imported:
                self.assertIsNone(window.project_event(row))
        self.assertIsNone(window.project_event({**self.imported, "session_id": "other-chat"}))


if __name__ == "__main__":
    unittest.main()
