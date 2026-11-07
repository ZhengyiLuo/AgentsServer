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

    def fixture(self, *, human=False, extra_row=False, source_prefix=True, padding=b""):
        prefix = encode([{"type": "progress", "data": "x" * 1000}] * 24) if source_prefix else b""
        prefix += padding
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

    def first_import(self, source_rows, checkpoint, *, candidate_fields=None):
        raw = self.source.read_bytes()[:checkpoint["previous_source_offset"]] + encode(source_rows)
        self.source.write_bytes(raw)
        checkpoint["cursor"].update(source_offset=len(raw), source_digest=hashlib.sha256(raw).hexdigest())
        native = [{"provider_session_id": "provider-one", **row} for row in self.rows
                  if not str(row.get("run_id", "")).startswith("import_")]
        self.events.write_bytes(encode(native))
        items = [{"kind": "user", "text": row["prompt"], "provider_origin": row["provider_origin"],
                  "source_text_sha256": hashlib.sha256(" ".join(source["text"].split()).encode()).hexdigest()}
                 for row, source in zip(self.imported, source_rows)]
        if candidate_fields:
            items[0].update(candidate_fields)
        before_events, before_source = self.events.read_bytes(), self.source.read_bytes()
        filtered = repair.filter_native_claude_mailbox_wake_items(
            "chat-one", "provider-one", self.events, items, source_path=self.source, root=self.root,
            sync_checkpoint=checkpoint, normalize_user=lambda row: row.get("text", "")[:48],
            normalize_full_user=lambda row: row.get("text"))
        self.assertEqual(self.events.read_bytes(), before_events)
        self.assertEqual(self.source.read_bytes(), before_source)
        return items, filtered

    def test_first_import_hides_exact_scheduled_input_and_keeps_same_prefix_human(self):
        source_rows, checkpoint = self.fixture(human=True)
        items, filtered = self.first_import(source_rows, checkpoint)
        self.assertTrue(filtered[0].get("metadata_only"))
        self.assertEqual(filtered[0]["text"], "")
        self.assertEqual(filtered[0]["provider_history_repair"], "source_proven_import")
        self.assertEqual(filtered[1], items[1])
        self.assertTrue(all("metadata_only" not in item for item in items))

    def test_first_import_keeps_unproven_or_explicitly_authored_scheduled_text(self):
        for change in ("different_text", "human", "duplicate", "incomplete", "wrong_provider", "mixed_owner", "wrong_origin", "candidate_human"):
            with self.subTest(change=change):
                source_rows, checkpoint = self.fixture()
                if change == "different_text":
                    source_rows[0]["text"] = self.full_text[:-len("scheduled ending")] + "different ending"
                elif change == "human":
                    source_rows[0]["clientUserMessageId"] = "human-message"
                elif change == "duplicate":
                    source_rows.append(dict(source_rows[0], uuid="another-source"))
                elif change == "incomplete":
                    self.rows[25]["type"] = "raw_event"
                elif change == "wrong_provider":
                    source_rows[0]["sessionId"] = "provider-other"
                elif change == "mixed_owner":
                    self.rows[25]["provider_session_id"] = "provider-other"
                elif change == "wrong_origin":
                    self.imported[0]["provider_origin"]["event_id"] = "different-source"
                items, filtered = self.first_import(source_rows, checkpoint,
                    candidate_fields={"client_user_message_id": "human-message"} if change == "candidate_human" else None)
                self.assertEqual(filtered, items)

    def test_repeated_schedule_input_is_owned_per_occurrence(self):
        source_rows, checkpoint = self.fixture(human=True)
        source_rows[1].update(text=self.full_text, timestamp="2026-09-10T12:05:02Z")
        self.imported[1]["provider_origin"]["timestamp"] = source_rows[1]["timestamp"]
        self.rows[26:26] = [
            {**self.rows[24], "seq": 27, "run_id": "scheduled-next", "ts": "2026-09-10T12:05:01Z"},
            {**self.rows[25], "seq": 28, "run_id": "scheduled-next", "ts": "2026-09-10T12:05:03Z"},
        ]
        _items, filtered = self.first_import(source_rows, checkpoint)
        self.assertTrue(all(item.get("metadata_only") for item in filtered))

    def test_identical_human_input_after_scheduled_occurrence_stays_visible(self):
        source_rows, checkpoint = self.fixture(human=True)
        source_rows[1].update(text=self.full_text, timestamp="2026-09-10T12:05:02Z")
        self.imported[1]["provider_origin"]["timestamp"] = source_rows[1]["timestamp"]
        items, filtered = self.first_import(source_rows, checkpoint)
        self.assertTrue(filtered[0].get("metadata_only"))
        self.assertEqual(filtered[1], items[1])

    def test_nul_padding_before_checkpoint_preserves_first_import_and_historical_proof(self):
        source_rows, checkpoint = self.fixture(human=True, padding=b"\0" * 3377 + b"\n")
        self.assertTrue(self.prepare())
        self.assertTrue(self.cache.is_hidden("chat-one", self.imported[0]))
        self.assertFalse(self.cache.is_hidden("chat-one", self.imported[1]))
        window = self.prepare_window(self.events.stat().st_size)
        self.assertTrue(window.is_hidden(self.imported[0]))
        self.assertFalse(window.is_hidden(self.imported[1]))
        # Exercise the full proof as well as the bounded historical window.
        self.cache = repair.ClaudeMetadataRepairCache()
        with patch.object(repair, "MAX_EVENTS_BYTES", 1024 * 1024):
            self.assertTrue(self.prepare())
            self.assertTrue(self.cache.is_hidden("chat-one", self.imported[0]))
        items, filtered = self.first_import(source_rows, checkpoint)
        self.assertTrue(filtered[0].get("metadata_only"))
        self.assertEqual(filtered[1], items[1])

    def test_mixed_nul_or_malformed_record_does_not_establish_proof(self):
        for padding in (b"\0" * 3377 + b"{}\n", b"{broken}\n"):
            with self.subTest(padding_size=len(padding)):
                self.cache = repair.ClaudeMetadataRepairCache()
                source_rows, checkpoint = self.fixture(padding=padding)
                self.assertFalse(self.prepare())
                self.assertFalse(self.cache.is_hidden("chat-one", self.imported[0]))
                items, filtered = self.first_import(source_rows, checkpoint)
                self.assertEqual(filtered, items)

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

    def prepare_window(self, end):
        return self.cache.prepare_window("chat-one", "provider-one", self.events, self.root,
                                         lambda row: row.get("text", "")[:48], event_window_end=end,
                                         normalize_full_user=lambda row: row.get("text"))

    def test_old_page_window_preserves_tail_cache_and_same_prefix_human(self):
        self.fixture(human=True, extra_row=True)
        end = self.events.stat().st_size
        with self.events.open("ab") as stream:
            stream.write(encode([{"seq": 100 + index, "type": "raw_event", "raw": "x" * 1000}
                                 for index in range(24)]))
        with self.source.open("ab") as stream:
            stream.write(encode([{"type": "progress", "data": "x" * 1000}] * 24))
        self.assertFalse(self.prepare())
        signature = self.cache.signature("chat-one")
        reads = []
        original = repair._bounded_records
        def records(path, stamp, start, stop, **kwargs):
            reads.append((path, start, stop))
            yield from original(path, stamp, start, stop, **kwargs)
        with patch.object(repair, "_bounded_records", side_effect=records):
            window = self.prepare_window(end)
        self.assertEqual(len(reads), 2)
        self.assertTrue(all(stop - start <= 8192 for _, start, stop in reads))
        self.assertEqual(reads[0][2], end)
        self.assertTrue(window.is_hidden(self.imported[0]))
        self.assertFalse(window.is_hidden(self.imported[1]))
        self.assertFalse(window.is_hidden({**self.imported[0], "session_id": "other-chat"}))
        self.assertIsNone(window.project_event(self.terminal))
        self.assertFalse(self.cache.is_hidden("chat-one", self.imported[0]))
        self.assertEqual(self.cache.signature("chat-one"), signature)
        with patch.object(repair, "_bounded_records", side_effect=AssertionError("Repeated page scan")):
            self.assertIs(self.prepare_window(end), window)
        with patch.object(repair, "_regular_stamp", side_effect=AssertionError("Per-event I/O")):
            self.assertTrue(window.is_hidden(self.imported[0]))
            self.assertIsNone(window.project_event(self.terminal))

    def test_page_window_negative_cache_forget_and_lru(self):
        self.fixture()
        self.imported[0].pop("provider_origin")
        self.events.write_bytes(encode(self.rows))
        end = self.events.stat().st_size
        missing = self.prepare_window(end)
        self.assertFalse(missing.is_hidden(self.imported[0]))
        with patch.object(repair, "_bounded_records", side_effect=AssertionError("Repeated negative scan")):
            self.assertIs(self.prepare_window(end), missing)
        with patch.object(repair, "MAX_WINDOWS", 2):
            self.prepare_window(end - 1)
            self.prepare_window(end - 2)
        self.assertEqual(len(self.cache._windows), 2)
        self.cache.forget("chat-one")
        self.assertEqual(len(self.cache._windows), 0)
        self.assertIsNot(self.prepare_window(end), missing)

    def test_page_window_reproves_growth_and_source_or_event_rewrites(self):
        self.fixture()
        end = self.events.stat().st_size
        first = self.prepare_window(end)
        self.assertTrue(first.is_hidden(self.imported[0]))
        with self.events.open("ab") as stream:
            stream.write(encode([{"seq": 100, "type": "raw_event"}]))
        grown = self.prepare_window(end)
        self.assertIsNot(grown, first)
        self.assertTrue(grown.is_hidden(self.imported[0]))
        with self.source.open("ab") as stream:
            stream.write(encode([{"type": "progress"}]))
        source_grown = self.prepare_window(end)
        self.assertIsNot(source_grown, grown)
        self.assertTrue(source_grown.is_hidden(self.imported[0]))
        original_source = self.source.read_bytes()
        self.source.write_bytes(original_source.replace(b"scheduled ending", b"different ending"))
        self.assertFalse(self.prepare_window(end).is_hidden(self.imported[0]))
        self.source.write_bytes(original_source)
        self.cache.forget("chat-one")
        self.assertTrue(self.prepare_window(end).is_hidden(self.imported[0]))
        raw = self.events.read_bytes().replace(b'"purpose": "scheduled_job"', b'"purpose": "ordinary_chat"')
        self.events.write_bytes(raw)
        self.assertFalse(self.prepare_window(end).is_hidden(self.imported[0]))

    def test_historical_window_requires_complete_boundaries(self):
        self.fixture()
        end = self.events.stat().st_size
        for boundary in (end - 1, end + 1, 0, True):
            self.assertFalse(self.prepare_window(boundary).is_hidden(self.imported[0]))
        with patch.object(repair, "MAX_EVENTS_BYTES", 1700):
            self.assertFalse(self.prepare_window(end).is_hidden(self.imported[0]))


if __name__ == "__main__":
    unittest.main()
