"""Accepted goal follow-ups remain human history without owning new runs."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_async_chat_timeline_index_isolated import load_index
from tests.test_codex_history_metadata_isolated import projection
from public_chat_transcript import make_public_event_projector, read_public_transcript


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


class ClaudeFollowupProjectionTests(GoalFollowupProjectionTests):
    def setUp(self):
        super().setUp()
        for item in self.events:
            item.update(backend="claude", purpose=None)
            if item["type"] == "turn_steered":
                item.pop("native_goal_steer")
                item.update(native_followup=True, provider_message_id=f"claude-message-{item['seq']}")
        self.write(self.events)

    def test_unproven_steer_is_not_human_history_or_shareable_input(self):
        for field, value in (
            ("native_followup", False), ("native_steer", False),
            ("provider_user_authored", False), ("provider_generated", True),
            ("purpose", "scheduled_job"), ("backend", "codex"),
            ("run_id", ""), ("provider_message_id", None),
            ("provider_message_id", " "), ("provider_message_id", 42),
        ):
            with self.subTest(field=field, value=value):
                invalid = {**self.events[2], field: value}
                self.assertFalse(projection()["is_native_goal_steer_event"](invalid))
                self.write([self.events[0], invalid])
                result = read_public_transcript(self.path, lambda item: item)
                self.assertEqual([item["text"] for item in result["messages"]], ["Original goal request"])

    def test_scheduled_owner_keeps_user_followups_separate_on_append_and_reopen(self):
        scheduled = [
            item if item["type"] == "turn_steered"
            else {**item, "purpose": "scheduled_job", "job_id": "job-one"}
            for item in self.events
        ]
        self.write(scheduled[:3])
        ns = load_index(self.path)
        initial = ns["_build_timeline_index_locked"]("chat")
        self.assertEqual([item["key"] for item in initial["landmarks"]], [
            "job:job-one", "turn:goal-owner:start-3",
        ])
        with self.path.open("a", encoding="utf-8") as target:
            for item in scheduled[3:]:
                target.write(json.dumps(item) + "\n")
        warm = deepcopy(ns["_build_timeline_index_locked"]("chat"))
        self.assertEqual([(item["key"], item["end_seq"]) for item in warm["landmarks"]], [
            ("job:job-one", 8), ("turn:goal-owner:start-3", 3), ("turn:goal-owner:start-6", 6),
        ])
        self.assertEqual([item["seq"] for item in ns["read_semantic_timeline_page"]("chat", limit=1)["events"]], [6])
        self.assertEqual([item["seq"] for item in ns["read_semantic_timeline_page"](
            "chat", limit=1, semantic_before=6,
        )["events"]], [3])
        self.assertEqual(ns["TIMELINE_INDEX_CACHE"]["chat"]["by_key"]["job:job-one"]["event_count"], 6)
        ns["TIMELINE_INDEX_CACHE"].clear()
        self.assertEqual(ns["_build_timeline_index_locked"]("chat"), warm)
        self.assertEqual([item["seq"] for item in ns["read_semantic_timeline_page"]("chat", limit=1)["events"]], [6])

    def test_shared_followup_preserves_authored_text_and_attachment(self):
        video = {"id": "video_ZmlsZQ." + "a" * 64, "filename": "demo.mp4",
                 "content_type": "video/mp4", "size": 10}
        accepted = {**self.events[2], "prompt": "Exact human instruction", "shared_videos": [video]}
        self.write([accepted])
        ns = load_index(self.path)
        projector = make_public_event_projector(
            "chat", event_is_visible=ns["is_client_visible_event"],
            event_files_belong=lambda event, chat: event.get("session_id") == chat,
            project_provider_event=lambda event, _chat: event,
            strip_user_context=lambda prompt, **_kwargs: prompt,
            fork_internal_purposes=set(),
        )
        result = read_public_transcript(self.path, projector)
        self.assertEqual([(item["role"], item["text"], item.get("videos")) for item in result["messages"]], [
            ("user", "Exact human instruction", [video]),
        ])
        self.assertNotIn("provider_message_id", json.dumps(result["messages"]))
