"""Older import pages use indexed bounds and return cache-replacing repairs.

Only source-extracted timeline functions run here; no server is imported.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from test_async_chat_timeline_index_isolated import load_index


class HistoricalCronPageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "events.jsonl"
        self.ns = load_index(self.path)
        self.ns["STORE"].sessions["sender"] = {"backend": "claude"}
        common = {"session_id": "sender", "backend": "claude", "ts": "2026-09-10T12:00:00Z"}
        raw = [
            {"type": "history_imported", "run_id": "import_old"},
            {"type": "turn_started", "run_id": "import_old", "imported": True, "prompt": "Old scheduled input"},
            {"type": "assistant_text", "run_id": "import_old", "imported": True, "text": "Old scheduled report"},
            {"type": "turn_started", "run_id": "import_old", "imported": True, "prompt": "Genuine question"},
            {"type": "assistant_text", "run_id": "import_old", "imported": True, "text": "Genuine answer"},
            {"type": "turn_finished", "run_id": "import_old", "imported": True},
            {"type": "turn_started", "run_id": "native_now", "prompt": "Current question"},
            {"type": "assistant_text", "run_id": "native_now", "text": "Current answer"},
            {"type": "turn_finished", "run_id": "native_now"},
        ]
        self.events = [{**common, **event, "seq": i, "id": f"e{i}"} for i, event in enumerate(raw, 1)]
        self.lines = [(json.dumps(event) + "\n").encode() for event in self.events]
        self.path.write_bytes(b"".join(self.lines))
        self.calls = []

        class Window:
            def is_hidden(inner, event):
                return event.get("seq") == 2

            def project_event(inner, event):
                if event.get("seq") == 3:
                    return {**event, "text": "", "metadata_only": True,
                            "provider_history_repair": "source_proven_assistant_replay"}
                return None

        def prepare(session, *, event_window_end):
            self.calls.append((session, event_window_end))
            return Window()

        self.ns["prepare_claude_history_metadata_repair"] = prepare

    def test_older_page_uses_complete_import_end_and_returns_same_id_tombstones(self):
        original = self.path.read_bytes()
        self.ns["_build_timeline_index_locked"]("sender")
        cached = self.ns["TIMELINE_INDEX_CACHE"]["sender"]
        records_before = deepcopy(cached["by_key"])
        result = self.ns["read_semantic_timeline_page"]("sender", limit=3, tail=True)
        self.assertEqual(self.calls, [("sender", sum(map(len, self.lines[:6])))])
        events = {event["id"]: event for event in result["events"]}
        self.assertEqual(events["e2"]["prompt"], "")
        self.assertEqual(events["e2"]["provider_history_repair"], "source_proven_import")
        self.assertEqual(events["e3"]["text"], "")
        self.assertTrue(events["e3"]["metadata_only"])
        self.assertEqual(events["e4"]["prompt"], "Genuine question")
        self.assertEqual(events["e5"]["text"], "Genuine answer")
        self.assertEqual(events["e7"]["prompt"], "Current question")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIs(self.ns["TIMELINE_INDEX_CACHE"]["sender"], cached)
        self.assertEqual(cached["by_key"], records_before)

    def test_current_page_without_import_does_not_prepare_history(self):
        result = self.ns["read_semantic_timeline_page"]("sender", limit=1, tail=True)
        self.assertEqual(self.calls, [])
        self.assertIn("Current question", [event.get("prompt") for event in result["events"]])

    def test_missing_window_proof_leaves_original_messages_visible(self):
        self.ns["prepare_claude_history_metadata_repair"] = lambda *args, **kwargs: None
        result = self.ns["read_semantic_timeline_page"]("sender", limit=3, tail=True)
        events = {event["id"]: event for event in result["events"]}
        self.assertEqual(events["e2"]["prompt"], "Old scheduled input")
        self.assertEqual(events["e3"]["text"], "Old scheduled report")


if __name__ == "__main__":
    unittest.main()
