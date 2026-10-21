"""Accepted goal follow-ups remain human history without owning new runs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_async_chat_timeline_index_isolated import load_index
from tests.test_codex_history_metadata_isolated import projection
from codex_history_repair import filter_native_codex_history_items
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
        provider = "11111111-2222-3333-4444-555555555555"
        # A dedicated resume records a promptless lifecycle start. Public
        # output carries the exact thread/turn/item receipt from its consumer.
        self.events[0] = event(1, "turn_started", thread_id=provider)
        for item in self.events[1:7]:
            item["provider_turn_id"] = "goal-turn-1" if item["seq"] == 2 else (
                "goal-turn-2" if item["seq"] <= 5 else "goal-turn-3"
            )
            if item["type"] != "turn_steered":
                item.update(provider_thread_id=provider, item_id=f"provider-item-{item['seq']}")
        self.events[7] = event(8, "turn_finished", status="completed", turn_id="goal-turn-3")
        self.write(self.events)
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
        for item, native in zip(provider_items[:-1], self.events[2:7]):
            item["provider_origin"] = {
                "provider": "codex", "session_id": provider, "kind": item["kind"],
                "event_id": f"provider-item-{native['seq']}",
                "turn_id": native["provider_turn_id"], "timestamp": native["ts"],
            }
        fresh, consumed_seq = ns["reconcile_cursor_history_items"](
            "chat", provider_items, timeline_after_seq=2, timeline_through_seq=8,
        )
        # Human follow-ups still consume one credit each; goal replies reach
        # the second stage so another same-text native item cannot consume them.
        self.assertEqual(fresh, [provider_items[index] for index in (1, 2, 4, 5)])
        self.assertEqual(consumed_seq, 8)
        self.assertEqual(filter_native_codex_history_items("chat", provider, self.path, fresh),
                         provider_items[-1:])

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
