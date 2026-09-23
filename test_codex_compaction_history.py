"""Compaction summary proof across checkpointed read repair and first import."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from codex_history_repair import (
    CodexNativeHistoryRepairCache, filter_native_codex_history_items,
)
from test_codex_goal_history_isolated import load_projection


PROVIDER = "11111111-2222-3333-4444-555555555555"
BODY = "Synthetic continuation notes"
PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process. "
    "You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. "
    "Here is the summary produced by the other language model, use the information in this summary "
    "to assist with your own analysis:\n"
)


def compaction_records():
    identity = {"thread_id": PROVIDER, "turn_id": "turn-1", "response_id": "resp-compact"}
    return [
        {"type": "response_item", "timestamp": "2026-09-11T00:18:27.218Z", "payload": {
            "type": "message", "role": "assistant", "phase": "final_answer", "id": "msg_summary",
            "content": [{"type": "output_text", "text": BODY}],
            "internal_chat_message_metadata_passthrough": {
                "turn_id": "turn-1", "content_item_kinds": ["unknown"]}}},
        {"type": "token_usage_record", "timestamp": "2026-09-11T00:18:27.243Z",
            "payload": dict(identity)},
        {"type": "event_msg", "timestamp": "2026-09-11T00:18:27.268Z",
            "payload": {"type": "token_count", "info": None}},
        {"type": "compacted", "timestamp": "2026-09-11T00:18:27.293Z", "payload": {
            "message": PREFIX + BODY, "compaction_response_id": "resp-compact",
            "latest_token_usage_record": dict(identity), "replacement_history": [{
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": PREFIX + BODY}],
                "internal_chat_message_metadata_passthrough": {
                    "turn_id": "turn-1", "content_item_kinds": ["compaction.summary"]}}]}},
    ]


class CodexCompactionHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / f"rollout-{PROVIDER}.jsonl"
        self.events = self.root / "events.jsonl"
        self.pending_events = self.root / "before-import.jsonl"
        self.pending_events.write_text(json.dumps({"seq": 1, "session_id": "chat", "type": "session_created"}) + "\n")
        self.ns = load_projection()
        self.parse = self.ns["codex_history_event_item"]
        self.raw = compaction_records()
        self.fixture()

    def fixture(self, *, tampered=False):
        self.cache = CodexNativeHistoryRepairCache()
        self.source_records = [{"type": "session_meta", "payload": {"id": PROVIDER}}, *self.raw]
        data = "".join(json.dumps(row) + "\n" for row in self.source_records).encode()
        self.source.write_bytes(data)
        stat = self.source.stat()
        self.checkpoint = {"version": 1, "previous_present": False,
            "previous_source_offset": 0, "previous_source_digest": "", "cursor": {
                "version": 1, "backend": "codex", "provider_session_id": PROVIDER,
                "source_path": str(self.source), "source_dev": stat.st_dev, "source_ino": stat.st_ino,
                "source_offset": len(data), "source_digest": "0" * 64 if tampered else hashlib.sha256(data).hexdigest()}}
        assistants = [row for row in self.raw if row.get("payload", {}).get("role") == "assistant"]
        self.imports = [{"seq": index + 101, "id": f"import-{index}", "session_id": "chat",
            "run_id": "import_fixture", "backend": "codex", "imported": True,
            "type": "assistant_text", "ts": row["timestamp"], "provider_history_sanitized": True,
            "text": row["payload"]["content"][0]["text"]} for index, row in enumerate(assistants)]
        rows = [{"seq": 100, "type": "history_imported", "run_id": "import_fixture", "backend": "codex",
            "provider_session_id": PROVIDER, "source_path": str(self.source),
            "_history_sync_checkpoint": self.checkpoint}, *self.imports,
            {"seq": 200, "type": "turn_finished", "run_id": "import_fixture", "backend": "codex", "imported": True}]
        self.events.write_text("".join(json.dumps({"session_id": "chat", **row}) + "\n" for row in rows))

    def prepare(self):
        self.cache.prepare("chat", PROVIDER, self.events, self.source, self.root, self.parse)

    def filter_pending(self, items):
        return filter_native_codex_history_items("chat", PROVIDER, self.pending_events, items,
            source_path=self.source, root=self.root, sync_checkpoint=self.checkpoint, parse_item=self.parse)

    def parse_full(self):
        return self.ns["parse_codex_history_events"](self.source_records, 20, expected_session_id=PROVIDER)

    def files(self):
        return tuple(path.read_bytes() for path in (self.events, self.pending_events, self.source))

    def test_exact_summary_is_hidden_on_read_and_before_first_import_without_native_owner(self):
        before = self.files()
        original = deepcopy(self.imports[0])
        self.prepare()
        projected = self.cache.project_event("chat", self.imports[0])
        self.assertEqual(projected["text"], "")
        self.assertTrue(projected["metadata_only"])
        self.assertEqual(projected["provider_history_repair"], "source_proven_compaction")
        self.assertEqual(projected["provider_origin"]["kind"], "compaction_summary")
        self.assertEqual(self.parse_full(), [])
        item = self.parse(self.raw[0])
        self.assertEqual(self.filter_pending([item]), [item])
        self.assertEqual(self.imports[0], original)
        self.assertEqual(self.files(), before)

    def test_same_text_outside_compaction_boundary_and_changed_text_remain_visible(self):
        genuine = deepcopy(self.raw[0])
        genuine["timestamp"] = "2026-09-11T00:18:28.218Z"
        genuine["payload"]["id"] = "msg_genuine"
        self.raw.append(genuine)
        self.fixture()
        before = self.files()
        self.prepare()
        self.assertIsNotNone(self.cache.project_event("chat", self.imports[0]))
        self.assertIsNone(self.cache.project_event("chat", self.imports[1]))
        self.assertIsNone(self.cache.project_event("chat", {**self.imports[0], "text": BODY + " changed"}))
        summary, answer = self.parse(self.raw[0]), self.parse(genuine)
        changed = {**summary, "text": BODY + " changed"}
        self.assertEqual(self.parse_full(), [answer])
        self.assertEqual(self.filter_pending([summary, answer, changed]), [summary, answer, changed])
        self.assertEqual(self.files(), before)

    def test_conflicting_or_untyped_compaction_does_not_hide_assistant(self):
        for label, mutate in (
            ("changed summary", lambda payload: payload.update(message=PREFIX + BODY + " changed")),
            ("wrong usage turn", lambda payload: payload["latest_token_usage_record"].update(turn_id="other-turn")),
            ("wrong response", lambda payload: payload.update(compaction_response_id="other-response")),
            ("wrong marker turn", lambda payload: payload["replacement_history"][0]["internal_chat_message_metadata_passthrough"].update(turn_id="other-turn")),
            ("wrong typed marker", lambda payload: payload["replacement_history"][0]["internal_chat_message_metadata_passthrough"].update(content_item_kinds=["user.text"])),
            ("untyped", lambda payload: payload["replacement_history"][0].pop("internal_chat_message_metadata_passthrough")),
        ):
            with self.subTest(label=label):
                self.raw = compaction_records()
                mutate(self.raw[-1]["payload"])
                self.fixture()
                before = self.files()
                self.prepare()
                self.assertIsNone(self.cache.project_event("chat", self.imports[0]))
                item = self.parse(self.raw[0])
                self.assertEqual(self.parse_full(), [item])
                self.assertEqual(self.filter_pending([item]), [item])
                self.assertEqual(self.files(), before)

    def test_tampered_checkpoint_cannot_hide_the_persisted_summary(self):
        self.fixture(tampered=True)
        before = self.files()
        self.prepare()
        self.assertIsNone(self.cache.project_event("chat", self.imports[0]))
        item = self.parse(self.raw[0])
        self.assertEqual(self.filter_pending([item]), [item])
        self.assertEqual(self.files(), before)

    def test_ordinary_assistant_import_never_scans_the_source_prefix(self):
        ordinary = deepcopy(self.raw[0])
        ordinary["payload"]["id"] = "msg_ordinary"
        ordinary["payload"]["content"][0]["text"] = "A genuine assistant answer"
        item = self.parse(ordinary)
        before = self.files()
        with mock.patch("codex_history_repair._prove_native_source",
                        side_effect=AssertionError("ordinary assistant triggered source scan")) as prove:
            self.assertEqual(self.filter_pending([item]), [item])
            prove.assert_not_called()
        self.assertEqual(self.files(), before)

    def test_delta_omits_only_summary_with_receipt_in_the_same_range(self):
        before = self.files()
        genuine = deepcopy(self.raw[0])
        genuine["timestamp"] = "2026-09-11T00:18:28.218Z"
        genuine["payload"]["id"] = "msg_genuine"
        for label, records, visible in (
            ("complete receipt", self.source_records, []),
            ("summary without receipt", self.source_records[:2], [self.parse(self.raw[0])]),
            ("same text after receipt", [*self.source_records, genuine], [self.parse(genuine)]),
        ):
            with self.subTest(label=label):
                self.ns["bounded_jsonl_records_range"] = mock.Mock(return_value=iter(
                    (record, index * 10) for index, record in enumerate(records, 1)))
                items, offset, digest, blocked = self.ns["parse_provider_history_delta"](
                    self.source, "codex", 0, len(records) * 10, limit=20, expected_stat={},
                    previous_last_item_digest="previous", expected_session_id=PROVIDER)
                self.assertEqual(items, visible)
                self.assertEqual(offset, len(records) * 10)
                self.assertFalse(blocked)
                self.assertEqual(digest, self.ns["history_item_cursor_digest"](visible[0]) if visible else "previous")
        self.assertEqual(self.files(), before)


if __name__ == "__main__":
    unittest.main()
