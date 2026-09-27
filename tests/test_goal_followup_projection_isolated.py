"""Accepted goal follow-ups remain human history without owning new runs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_async_chat_timeline_index_isolated import load_index
from tests.test_codex_history_metadata_isolated import projection
from public_chat_transcript import read_public_transcript


def event(seq, kind, **fields):
    return {
        "id": f"event-{seq}", "seq": seq, "session_id": "chat", "run_id": "goal-owner",
        "backend": "codex", "purpose": "codex_goal_resume", "type": kind,
        "ts": f"2026-09-10T10:00:{seq:02d}Z", **fields,
    }


def followup(seq, text="Keep working, including the new failure.", **fields):
    return event(seq, "turn_steered", native_goal_steer=True, native_steer=True,
                 provider_user_authored=True, prompt=text, queued_id=f"queued-{seq}", **fields)


class GoalFollowupProjectionTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="goal-followup-projection-")
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "events.jsonl"
        self.events = [
            event(1, "turn_started", prompt="Original goal request"),
            event(2, "assistant_text", text="Earlier answer"),
            followup(3),
            event(4, "reasoning_summary", phase="commentary", text="Continuing after your follow-up"),
            event(5, "assistant_text", text="Updated answer"),
            followup(6, "One more clarification"),
            event(7, "assistant_text", text="Latest answer"),
            event(8, "turn_finished", result_text="Latest answer"),
        ]
        self.write(self.events)

    def write(self, events):
        self.path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

    def test_minimap_keys_and_tail_pages_keep_followups_and_their_own_output(self):
        ns = load_index(self.path)
        index = ns["_build_timeline_index_locked"]("chat")
        self.assertEqual([item["key"] for item in index["landmarks"]], [
            "turn:goal-owner", "turn:goal-owner:start-3", "turn:goal-owner:start-6",
        ])
        self.assertEqual([item["kind"] for item in index["landmarks"]], ["user"] * 3)
        tail = ns["read_semantic_timeline_page"]("chat", limit=1)
        self.assertEqual([item["seq"] for item in tail["events"]], [6, 7, 8])
        ns["TIMELINE_INDEX_CACHE"].clear()
        self.assertEqual(ns["_build_timeline_index_locked"]("chat"), index)
        self.assertEqual(ns["read_semantic_timeline_page"]("chat", limit=1), tail)
        # A partial ledger starts with this accepted follow-up, not goal start.
        self.write(self.events[2:])
        ns["TIMELINE_INDEX_CACHE"].clear()
        self.assertEqual(ns["_build_timeline_index_locked"]("chat")["landmarks"][0]["key"],
                         "turn:goal-owner:start-3")

    def test_history_reconcile_credits_prevent_reimporting_the_followup(self):
        ns = projection()
        ns["events_path"] = lambda _: self.path
        keys, present, truncated = ns["history_timeline_message_keys"](
            "chat", timeline_after_seq=2, timeline_through_seq=8,
            tail=False, include_imported=False,
        )
        self.assertTrue(present)
        self.assertFalse(truncated)
        self.assertEqual([seq for seq, (role, _) in keys if role == "user"], [3, 6])
        followup_texts = ["Keep working, including the new failure.", "One more clarification"]
        self.assertEqual([digest for _, (role, digest) in keys if role == "user"], [
            hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
            for text in followup_texts
        ])
        provider_items = [
            {"kind": "user", "text": followup_texts[0]},
            {"kind": "assistant", "text": "Continuing after your follow-up"},
            {"kind": "assistant", "text": "Updated answer"},
            {"kind": "user", "text": followup_texts[1]},
            {"kind": "assistant", "text": "Latest answer"},
            # The same text sent again is a new occurrence, not another credit.
            {"kind": "user", "text": followup_texts[0]},
        ]
        fresh, consumed_seq = ns["reconcile_cursor_history_items"](
            "chat", provider_items, timeline_after_seq=2, timeline_through_seq=8,
        )
        self.assertEqual(fresh, provider_items[-1:])
        self.assertEqual(consumed_seq, 8)

    def test_append_to_warm_index_matches_cold_reopen(self):
        self.write(self.events[:2])
        ns = load_index(self.path)
        initial = ns["_build_timeline_index_locked"]("chat")
        self.assertEqual(len(initial["landmarks"]), 1)
        with self.path.open("a", encoding="utf-8") as target:
            for item in self.events[2:]:
                target.write(json.dumps(item) + "\n")
        warm = deepcopy(ns["_build_timeline_index_locked"]("chat"))
        ns["TIMELINE_INDEX_CACHE"].clear()
        self.assertEqual(ns["_build_timeline_index_locked"]("chat"), warm)
        self.assertEqual(len(warm["landmarks"]), 3)

    def inverted_history(self):
        self.write([
            event(1, "provider_session", provider_session_id="thread-native"),
            followup(2, "First follow-up", provider_turn_id="turn-native"),
            event(3, "reasoning_summary", text="Earlier public update", phase="commentary",
                  item_id="assistant-native", provider_turn_id="turn-native"),
            followup(4, "Next follow-up", provider_turn_id="turn-native"),
        ])
        ns = projection()
        ns["events_path"] = lambda _: self.path
        def source(kind, text, item_id, *, turn_id="turn-native", thread_id="thread-native"):
            return {"kind": kind, "text": text, "provider_user_authored": kind == "user",
                    "provider_origin": {"provider": "codex", "kind": kind,
                        "event_id": item_id, "turn_id": turn_id, "session_id": thread_id}}
        return ns, source, [
            source("assistant", "Earlier public update", "assistant-native"),
            source("user", "First follow-up", "user-first"),
            source("user", "Next follow-up", "user-next"),
        ]

    def test_native_identity_reconciles_inverted_goal_receipt_order(self):
        ns, _, items = self.inverted_history()
        fresh, consumed = ns["reconcile_cursor_history_items"](
            "chat", items, timeline_after_seq=1, timeline_through_seq=4)
        self.assertEqual(fresh, [])
        self.assertEqual(consumed, 4)

    def test_inverted_receipt_does_not_consume_repeated_text_or_other_native_identity(self):
        ns, source, items = self.inverted_history()
        repeated = source("user", "Next follow-up", "user-repeated")
        other_turn = source("user", "Next follow-up", "user-other", turn_id="other-turn")
        other_thread = source("assistant", "Earlier public update", "assistant-native", thread_id="other-thread")
        other_item = source("assistant", "Earlier public update", "other-assistant")
        unknown = {"kind": "user", "text": "Next follow-up"}
        # The native identities must not consume an unrelated item before the
        # real source occurrence, even if its text and native turn are equal.
        extras = [other_turn, other_thread, other_item, unknown]
        fresh, consumed = ns["reconcile_cursor_history_items"](
            "chat", [*extras, *items, repeated], timeline_after_seq=1, timeline_through_seq=4)
        self.assertEqual(fresh, [*extras, repeated])
        self.assertEqual(consumed, 4)

    def test_inverted_receipts_split_across_sync_preserve_contiguous_watermark(self):
        ns, _, items = self.inverted_history()
        ns["HISTORY_SYNC_EVENT_SCAN_LIMIT"] = 2
        consumed_receipts = []
        fresh, consumed = ns["reconcile_cursor_history_items"](
            "chat", items[:1], timeline_after_seq=1, timeline_through_seq=4,
            consumed_receipts=consumed_receipts)
        self.assertEqual(fresh, [])
        self.assertEqual(consumed, 1, "An assistant receipt must not skip the preceding user credit")
        self.assertEqual(consumed_receipts, [3])
        # The next bounded source delta contains no second copy of the earlier
        # assistant. Its outstanding credit cannot block later exact inputs.
        fresh, consumed = ns["reconcile_cursor_history_items"](
            "chat", items[1:2], timeline_after_seq=consumed, timeline_through_seq=4,
            consumed_receipts=consumed_receipts)
        self.assertEqual(fresh, [])
        self.assertEqual(consumed, 3)
        self.assertEqual(consumed_receipts, [])
        fresh, consumed = ns["reconcile_cursor_history_items"](
            "chat", items[2:], timeline_after_seq=consumed, timeline_through_seq=4,
            consumed_receipts=consumed_receipts)
        self.assertEqual((fresh, consumed, consumed_receipts), ([], 4, []))
        repeated = {**items[2], "provider_origin": {**items[2]["provider_origin"], "event_id": "new-repeat"}}
        fresh, consumed = ns["reconcile_cursor_history_items"](
            "chat", [repeated], timeline_after_seq=consumed, timeline_through_seq=4,
            consumed_receipts=consumed_receipts)
        self.assertEqual(fresh, [repeated])

    def test_unproven_steer_cannot_match_out_of_order_native_input(self):
        ns, _, items = self.inverted_history()
        events = [json.loads(line) for line in self.path.read_text().splitlines()]
        events[-1]["provider_user_authored"] = False
        self.write(events)
        fresh, _ = ns["reconcile_cursor_history_items"](
            "chat", items, timeline_after_seq=1, timeline_through_seq=4)
        self.assertEqual(fresh, items[-1:])

    def test_inherited_fork_receipts_keep_first_sync_tail_anchor(self):
        ns, source, items = self.inverted_history()
        events = [json.loads(line) for line in self.path.read_text().splitlines()]
        for row in events:
            row.update(forked=True, original_session_id="parent-chat")
        self.write(events)
        child_items = [{**item, "provider_origin": {**item["provider_origin"], "session_id": "child-thread"}}
                       for item in [items[1], items[0], items[2]]]
        new_item = source("user", "New child question", "child-message", turn_id="child-turn", thread_id="child-thread")
        self.assertEqual(ns["unsynced_history_items"](
            "chat", [*child_items, new_item], timeline_through_seq=4), [new_item])

    def test_public_transcript_keeps_user_followup_not_raw_control_payload(self):
        result = read_public_transcript(self.path, lambda item: item)
        self.assertEqual([(item["role"], item["text"]) for item in result["messages"]], [
            ("user", "Original goal request"), ("assistant", "Earlier answer"),
            ("user", "Keep working, including the new failure."),
            ("assistant", "Continuing after your follow-up"), ("assistant", "Updated answer"),
            ("user", "One more clarification"), ("assistant", "Latest answer"),
        ])
        self.assertNotIn("queued_id", json.dumps(result["messages"]))

    def test_unproven_steer_is_not_human_history_or_shareable_input(self):
        for field, value in (("native_goal_steer", False), ("native_steer", False),
                             ("provider_user_authored", False), ("purpose", "scheduled_job"),
                             ("backend", "claude"), ("run_id", "")):
            invalid = deepcopy(self.events[2])
            invalid[field] = value
            ns = projection()
            self.assertFalse(ns["is_native_goal_steer_event"](invalid))
            self.write([self.events[0], invalid])
            result = read_public_transcript(self.path, lambda item: item)
            self.assertEqual([item["text"] for item in result["messages"]], ["Original goal request"])
