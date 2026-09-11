"""Bounded recent scheduled repair; synthetic logs only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import claude_history_repair as repair


def encode(rows):
    return b"".join(json.dumps(row).encode() + b"\n" for row in rows)


class RecentScheduledRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "provider-one.jsonl"
        self.events = self.root / "events.jsonl"
        self.cache = repair.ClaudeMetadataRepairCache()
        self.full_text = "Shared scheduled prompt " * 40 + "scheduled ending"
        self.addCleanup(patch.stopall)
        patch.object(repair, "MAX_EVENTS_BYTES", 8192).start()

    def fixture(self, *, human=False, extra_row=False, source_prefix=True):
        prefix = encode([{"type": "progress", "data": "x" * 1000}] * 24) if source_prefix else b""
        source_rows = [{
            "type": "user", "uuid": "scheduled-source", "sessionId": "provider-one",
            "timestamp": "2026-09-10T12:00:02Z", "text": self.full_text,
        }]
        if human:
            source_rows.append({**source_rows[0], "uuid": "human-source",
                                "text": "Shared scheduled prompt " * 40 + "different genuine question"})
        raw = prefix + encode(source_rows)
        self.source.write_bytes(raw)
        identity = self.source.stat()
        checkpoint = {
            "version": 1, "previous_present": bool(prefix),
            "previous_source_offset": len(prefix),
            "previous_source_digest": hashlib.sha256(prefix).hexdigest() if prefix else "",
            "cursor": {
                "version": 1, "backend": "claude", "provider_session_id": "provider-one",
                "source_path": str(self.source), "source_dev": identity.st_dev, "source_ino": identity.st_ino,
                "source_offset": len(raw), "source_digest": hashlib.sha256(raw).hexdigest(),
            },
        }
        common = {"session_id": "chat-one", "backend": "claude"}
        self.rows = [{"seq": seq, "type": "raw_event", "raw": "x" * 1000}
                     for seq in range(1, 25)]
        self.rows.extend([
            {**common, "seq": 25, "type": "turn_started", "run_id": "scheduled-native",
             "purpose": "scheduled_job", "job_id": "job-one", "prompt": self.full_text,
             "ts": "2026-09-10T12:00:01Z"},
            {**common, "seq": 26, "type": "turn_finished", "run_id": "scheduled-native",
             "ts": "2026-09-10T12:00:03Z"},
            {**common, "seq": 27, "type": "history_imported", "run_id": "import_recent",
             "provider_session_id": "provider-one", "source_path": str(self.source),
             "_history_sync_checkpoint": checkpoint},
        ])
        self.imported = []
        for source_row in source_rows:
            imported = {**common, "seq": len(self.rows) + 1, "type": "turn_started",
                        "run_id": "import_recent", "imported": True, "prompt": source_row["text"][:48],
                        "provider_origin": {"provider": "claude", "kind": "user",
                                            "event_id": source_row["uuid"], "session_id": "provider-one",
                                            "timestamp": source_row["timestamp"]}}
            self.imported.append(imported)
            self.rows.append(imported)
        if extra_row:
            self.rows.append({**common, "seq": len(self.rows) + 1, "type": "assistant_text",
                              "run_id": "import_recent", "imported": True, "text": "A real answer"})
        self.terminal = {**common, "seq": len(self.rows) + 1, "type": "turn_finished",
                         "run_id": "import_recent", "imported": True}
        self.rows.append(self.terminal)
        self.events.write_bytes(encode(self.rows))
        return source_rows, checkpoint

    def prepare(self, full=None):
        return self.cache.prepare("chat-one", "provider-one", self.events, self.root,
                                  lambda row: row.get("text", "")[:48],
                                  normalize_full_user=full or (lambda row: row.get("text")))

    def test_recent_exact_occurrence_hides_duplicate_and_only_complete_batch_companions(self):
        self.fixture()
        with self.source.open("ab") as stream:
            stream.write(encode([{"type": "user", "text": "New source append after checkpoint"}]))
        self.assertTrue(self.prepare())
        self.assertTrue(self.cache.is_hidden("chat-one", self.imported[0]))
        self.assertTrue(self.cache.project_event("chat-one", self.rows[26])["metadata_only"])
        self.assertTrue(self.cache.project_event("chat-one", self.terminal)["metadata_only"])
        with patch.object(repair, "_regular_stamp", side_effect=AssertionError("Unexpected reread")):
            self.assertFalse(self.prepare())
            self.assertTrue(self.cache.is_hidden("chat-one", self.imported[0]))

    def test_same_display_prefix_genuine_input_remains_visible(self):
        self.fixture(human=True)
        self.assertEqual(self.imported[0]["prompt"], self.imported[1]["prompt"])
        self.assertTrue(self.prepare())
        self.assertTrue(self.cache.is_hidden("chat-one", self.imported[0]))
        self.assertFalse(self.cache.is_hidden("chat-one", self.imported[1]))
        self.assertIsNone(self.cache.project_event("chat-one", self.terminal))
        self.assertIsNone(self.cache.project_event("chat-one", self.rows[26]))

    def test_recent_compact_summary_uses_exact_source_flag_and_origin_without_native_job(self):
        for flag in (True, False, "true", 1):
            with self.subTest(flag=flag):
                self.cache = repair.ClaudeMetadataRepairCache()
                source_rows, checkpoint = self.fixture(human=True)
                source_rows[0]["isCompactSummary"] = flag
                prefix = self.source.read_bytes()[:checkpoint["previous_source_offset"]]
                raw = prefix + encode(source_rows)
                self.source.write_bytes(raw)
                checkpoint["cursor"]["source_offset"] = len(raw)
                self.rows[24]["purpose"] = "normal_chat"
                self.events.write_bytes(encode(self.rows))
                self.prepare()
                self.assertEqual(self.cache.is_hidden("chat-one", self.imported[0]), flag is True)
                self.assertFalse(self.cache.is_hidden("chat-one", self.imported[1]))
                self.assertIsNone(self.cache.project_event("chat-one", self.terminal))

    def test_partial_batch_preserves_companions(self):
        self.fixture(extra_row=True)
        self.assertTrue(self.prepare())
        self.assertTrue(self.cache.is_hidden("chat-one", self.imported[0]))
        self.assertIsNone(self.cache.project_event("chat-one", self.terminal))

    def test_metadata_and_human_with_duplicate_source_identity_fail_visible(self):
        source_rows, checkpoint = self.fixture(human=True)
        source_rows[0]["isCompactSummary"] = True
        source_rows[1]["uuid"] = source_rows[0]["uuid"]
        raw = self.source.read_bytes()[:checkpoint["previous_source_offset"]] + encode(source_rows)
        self.source.write_bytes(raw)
        checkpoint["cursor"]["source_offset"] = len(raw)
        self.rows[24]["purpose"] = "normal_chat"
        self.events.write_bytes(encode(self.rows))
        self.prepare()
        for row in self.imported:
            self.assertFalse(self.cache.is_hidden("chat-one", row))

    def test_full_text_mismatch_cannot_be_proven_by_display_prefix(self):
        source_rows, checkpoint = self.fixture()
        source_rows[0]["text"] = self.full_text[:-len("scheduled ending")] + "different ending"
        raw = self.source.read_bytes()[:checkpoint["previous_source_offset"]] + encode(source_rows)
        self.source.write_bytes(raw)
        checkpoint["cursor"]["source_offset"] = len(raw)
        self.events.write_bytes(encode(self.rows))
        self.assertFalse(self.prepare())
        self.assertFalse(self.cache.is_hidden("chat-one", self.imported[0]))

    def test_wrong_source_identity_and_ambiguous_native_occurrence_fail_visible(self):
        for mutate in (lambda: self.imported[0]["provider_origin"].update(event_id="other"),
                       lambda: self.imported[0]["provider_origin"].update(timestamp="2026-09-10T12:00:02.001Z"),
                       lambda: self.rows[25].update(ts="2026-09-10T12:00:01Z"),
                       lambda: self.rows[26]["_history_sync_checkpoint"]["cursor"].update(source_ino=-1)):
            with self.subTest(mutation=mutate):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.fixture()
                mutate()
                self.events.write_bytes(encode(self.rows))
                self.assertFalse(self.prepare())
                self.assertFalse(self.cache.is_hidden("chat-one", self.imported[0]))

    def test_duplicate_origin_in_batch_is_ambiguous(self):
        self.fixture(human=True)
        self.imported[1]["provider_origin"] = self.imported[0]["provider_origin"]
        self.events.write_bytes(encode(self.rows))
        self.assertFalse(self.prepare())

    def test_missing_native_start_outside_tail_and_truncated_source_fail_visible(self):
        self.fixture()
        with patch.object(repair, "MAX_EVENTS_BYTES", 1700):
            self.assertFalse(self.prepare())
        self.cache = repair.ClaudeMetadataRepairCache()
        self.fixture()
        with self.source.open("r+b") as stream:
            stream.truncate(self.source.stat().st_size - 1)
        self.assertFalse(self.prepare())

    def test_source_change_during_window_read_fails_visible(self):
        self.fixture()
        def changed(row):
            with self.source.open("ab") as stream:
                stream.write(encode([{"type": "progress", "data": "changed"}]))
            return row.get("text")
        self.assertFalse(self.prepare(changed))

    def test_oversized_source_alone_uses_recent_proof(self):
        self.fixture()
        self.rows = self.rows[24:]
        self.events.write_bytes(encode(self.rows))
        with patch.object(repair, "MAX_BYTES", 8192):
            self.assertTrue(self.prepare())
            self.assertTrue(self.cache.is_hidden("chat-one", self.imported[0]))


if __name__ == "__main__":
    unittest.main()
