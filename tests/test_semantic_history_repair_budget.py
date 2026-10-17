"""Repair receipts must not displace human messages in semantic history pages."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_server as server


class SemanticHistoryRepairBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(server, "STATE_DIR", Path(temporary.name)))
        self.sid = "repair-budget-chat"
        self.path = server.events_path(self.sid)
        self.path.parent.mkdir(parents=True)
        server.TIMELINE_INDEX_CACHE.clear()
        server.TIMELINE_INDEX_LOCKS.clear()
        self.addCleanup(server.TIMELINE_INDEX_CACHE.clear)
        self.addCleanup(server.TIMELINE_INDEX_LOCKS.clear)
        self.events = []
        self.repairs = set()
        original = server.project_legacy_imported_provider_event

        def project(event, session_id):
            # Ownership/provenance proof has separate real-transcript tests.
            # Model only its proven, same-ID correction at the paging boundary.
            if event["id"] in self.repairs:
                return {**event, "prompt": "",
                        "provider_history_repair": "source_proven_native_replay",
                        server.TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD: True}
            return original(event, session_id)

        self.enterContext(patch.object(server, "project_legacy_imported_provider_event", project))

    def add(self, kind, **fields):
        seq = len(self.events) + 1
        event = {"id": f"event-{seq}", "seq": seq, "type": kind,
                 "session_id": self.sid, "ts": "2026-10-17T12:00:00Z", **fields}
        self.events.append(event)
        return event

    def write(self):
        self.path.write_text("".join(json.dumps(event) + "\n" for event in self.events))

    def fixture(self, *, turns=48, repairs=169, last_state="completed"):
        for index in range(turns):
            run = f"run-{index}"
            self.add("turn_started", run_id=run, prompt=f"Question {index}")
            self.add("reasoning_summary", run_id=run, phase="commentary", text=f"Progress {index}")
            if index != turns - 1 or last_state != "active":
                self.add("assistant_text", run_id=run, text=f"Answer {index}")
                if index == turns - 1 and last_state == "stopped":
                    self.add("turn_stopped", run_id=run, stopped=True)
                else:
                    self.add("turn_finished", run_id=run, result_text=f"Answer {index}")
            # Repeated imported boundaries can outnumber semantic items.
            if index == 0:
                for _ in range(repairs):
                    event = self.add("turn_started", run_id="import_replays", imported=True,
                                     backend="codex", prompt="Already displayed native prompt")
                    self.repairs.add(event["id"])
        self.write()

    def assert_turn(self, page, index, *, answer=True):
        events = [event for event in page["events"] if event.get("run_id") == f"run-{index}"]
        self.assertTrue(any(event.get("prompt") == f"Question {index}" for event in events), index)
        if answer:
            self.assertTrue(any(event.get("text") == f"Answer {index}"
                                or event.get("result_text") == f"Answer {index}"
                                for event in events), index)

    def test_default_page_preserves_messages_and_all_normal_repair_receipts(self):
        self.fixture()
        before = self.path.read_bytes()
        page = server.read_semantic_timeline_page(self.sid, limit=48)
        self.assertEqual(page["semantic_item_count"], 48)
        for index in range(48):
            self.assert_turn(page, index)
        repaired = [event for event in page["events"] if event["id"] in self.repairs]
        self.assertEqual(len(repaired), 169)
        self.assertTrue(all(event["prompt"] == "" for event in repaired))
        self.assertEqual(len({event["id"] for event in page["events"]}), len(page["events"]))
        self.assertEqual(self.path.read_bytes(), before)

    def test_page_sizes_and_older_cursor_keep_both_sides(self):
        self.fixture()
        for limit in (1, 3, 30, 48, 60, 120):
            with self.subTest(limit=limit):
                page = server.read_semantic_timeline_page(self.sid, limit=limit)
                for index in range(48 - min(limit, 48), 48):
                    self.assert_turn(page, index)
                if limit < 48:
                    before = page["next_semantic_before"]
                    older = server.read_semantic_timeline_page(self.sid, semantic_before=before, limit=48)
                    for index in range(48 - limit):
                        self.assert_turn(older, index)
                    self.assertIsNone(older["next_semantic_before"])

    def test_corrections_do_not_crowd_out_active_or_stopped_turn(self):
        for state in ("active", "stopped"):
            with self.subTest(state=state):
                self.events.clear()
                self.repairs.clear()
                server.TIMELINE_INDEX_CACHE.clear()
                self.fixture(last_state=state)
                page = server.read_semantic_timeline_page(self.sid, limit=48)
                self.assert_turn(page, 47, answer=state != "active")
                self.assertTrue(any(event.get("run_id") == "run-47"
                                    and event["type"] == "reasoning_summary" for event in page["events"]))

    def test_repair_overflow_is_bounded_without_starving_normal_content(self):
        self.fixture(repairs=2000)
        page = server.read_semantic_timeline_page(self.sid, limit=48)
        for index in range(48):
            self.assert_turn(page, index)
        self.assertLessEqual(len(page["events"]), server.MAX_EVENT_RESPONSE_LIMIT)
        self.assertLessEqual(sum(event["id"] in self.repairs for event in page["events"]),
                             server.SEMANTIC_TIMELINE_REPAIR_PAGE_OVERFLOW_LIMIT)
        self.assertTrue(any(event["id"] in self.repairs for event in page["events"]))
        self.assertTrue(all(not event.get("prompt") for event in page["events"]
                            if event["id"] in self.repairs))

    def test_absolute_cap_reserves_messages_before_repairs(self):
        self.fixture()
        with patch.object(server, "MAX_EVENT_RESPONSE_LIMIT", 192):
            page = server.read_semantic_timeline_page(self.sid, limit=48)
        for index in range(48):
            self.assert_turn(page, index)
        self.assertLessEqual(len(page["events"]), 192)


if __name__ == "__main__":
    unittest.main()
