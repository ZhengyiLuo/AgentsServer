"""Async message navigation and paging: source AST, isolated event files only."""
from __future__ import annotations

import ast
from collections import OrderedDict, deque
from contextlib import suppress
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest


FUNCTIONS = {
    "_build_timeline_index_locked", "timeline_index_async_cross_chat_key",
    "update_async_cross_chat_timeline_record", "timeline_index_cross_chat_key",
    "compact_timeline_index_text", "timeline_index_event_text", "timeline_index_event_is_hidden",
    "timeline_index_is_error", "timeline_index_is_native_steer_transition_stop",
    "timeline_index_retire_native_steer_turn", "timeline_index_codex_lifecycle_key",
    "scheduled_job_run_status", "scheduled_job_status_should_replace", "scheduled_job_output_rank",
    "scheduled_job_occurrence_key", "job_run_history_event_snapshot", "bounded_job_history_text",
    "is_client_visible_event", "collect_semantic_timeline_events", "read_semantic_timeline_page",
    "semantic_timeline_landmark_anchor", "semantic_timeline_ordinary_candidates",
    "semantic_timeline_event_identity", "semantic_timeline_event_is_display",
    "semantic_timeline_event_is_completed_commentary", "semantic_timeline_event_is_trace_anchor",
    "is_native_goal_steer_event",
}
CONSTANTS = {
    "TIMELINE_INDEX_PROJECTION_VERSION", "TIMELINE_INDEX_HIDDEN_TYPES", "TIMELINE_INDEX_JOB_TYPES",
    "TIMELINE_INDEX_CODEX_GOAL_TYPES", "TIMELINE_INDEX_CODEX_COMPACTION_TYPES", "TIMELINE_INDEX_TRACE_TYPES",
    "CROSS_CHAT_CLIENT_INTERNAL_EVENT_TYPES", "SEMANTIC_TIMELINE_EVENT_BUDGET_PER_ITEM",
    "SEMANTIC_TIMELINE_ESSENTIAL_DETAIL_TYPES", "SEMANTIC_TIMELINE_ESSENTIAL_LIMIT_PER_ITEM",
    "SEMANTIC_TIMELINE_ESSENTIAL_PAGE_OVERFLOW_LIMIT", "SEMANTIC_TIMELINE_TRACE_ANCHOR_PAGE_OVERFLOW_LIMIT",
    "SEMANTIC_TIMELINE_JOB_RUN_LIMIT", "RUN_TRACE_EVENT_TYPES",
}


def load_index(path: Path):
    source = (Path(__file__).resolve().parents[1] / "agent_server.py")
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    assert {node.name for node in selected} == FUNCTIONS
    constants = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in CONSTANTS:
                    constants[target.id] = ast.literal_eval(node.value)
    assert set(constants) == CONSTANTS
    namespace = {
        **constants, "re": re, "json": json, "Path": Path, "deque": deque, "suppress": suppress,
        "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude", "DEFAULT_BACKEND": "codex",
        "FORK_INTERNAL_PURPOSES": set(), "TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD": "_agentsdock_imported_prompt_hidden",
        "MAX_EVENT_RESPONSE_LIMIT": 1000, "JOB_RUN_HISTORY_TEXT_LIMIT": 10_000,
        "JOB_RUN_HISTORY_EVENT_FIELDS": set(),
        "STORE": SimpleNamespace(sessions={"recipient": {"backend": "codex"}, "sender": {"backend": "claude"}}),
        "events_path": lambda _session_id: path,
        "prepare_provider_history_metadata_repair": lambda _session_id: None,
        "prepare_claude_history_metadata_repair": lambda _session_id, **_kwargs: None,
        "CLAUDE_METADATA_REPAIR_CACHE": SimpleNamespace(signature=lambda _session_id: ()),
        "CODEX_GOAL_HISTORY_REPAIR_CACHE": SimpleNamespace(signature=lambda _session_id: ()),
        "CODEX_NATIVE_HISTORY_REPAIR_CACHE": SimpleNamespace(signature=lambda _session_id: ()),
        "session_codex_thread_id": lambda _session: "",
        "session_codex_subagent_states": lambda _session_id: [],
        "codex_subagent_ownership_snapshot": lambda: {},
        "TIMELINE_INDEX_CACHE": OrderedDict(), "TIMELINE_INDEX_CACHE_LOCK": threading.RLock(),
        "timeline_index_session_lock": lambda _session_id: threading.RLock(),
        "evict_timeline_index_cache_locked": lambda: None,
        "event_files_belong_to_session": lambda _event, _session_id: True,
        "project_legacy_imported_provider_event": lambda event, _session_id: event,
        "is_fork_internal_event": lambda _event, _run_ids: False,
        "client_safe_event": lambda event: event,
        "now_iso": lambda: "2026-09-10T00:00:00Z",
    }
    namespace["timeline_index_cached_entry"] = lambda session_id, **_kwargs: namespace["TIMELINE_INDEX_CACHE"].get(session_id)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    exec(compile(module, "<isolated-async-timeline-index>", "exec"), namespace)
    return namespace


def message(seq: int, phase: str, **patch):
    return {
        "id": f"event-{seq}", "seq": seq, "session_id": "recipient", "ts": f"2026-09-10T10:00:{seq:02d}Z",
        "type": f"chat_conversation_message_{phase}", "conversation_mode": "async_route_v1",
        "message_id": "handoff_one", "handoff_id": "handoff_one", "cross_chat_envelope_id": "handoff_one",
        "conversation_id": "pair_one", "source_session_id": "sender", "target_session_id": "recipient",
        "source_title": "Research agent", "target_title": "Desktop agent", "handoff_status": phase,
        "handoff_preview": "Prepared message body", "handoff_action": "instruction", **patch,
    }


class AsyncChatTimelineIndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="async-message-index-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "events.jsonl"
        self.path.write_text("", encoding="utf-8")
        self.ns = load_index(self.path)

    def append(self, *events):
        with self.path.open("a", encoding="utf-8") as target:
            for event in events:
                target.write(json.dumps(event) + "\n")

    def index(self, session="recipient"):
        return self.ns["_build_timeline_index_locked"](session)

    def page(self, session="recipient", **kwargs):
        return self.ns["read_semantic_timeline_page"](session, **kwargs)

    def test_pending_and_cancelled_recipient_never_enter_index_or_consume_pages(self):
        self.append(message(1, "received"), message(2, "queued"))
        self.assertEqual(self.index()["landmarks"], [])
        self.assertEqual(self.page()["semantic_total"], 0)
        self.append(message(3, "cancelled"))
        self.assertEqual(self.index()["landmarks"], [])
        self.assertEqual(self.page()["events"], [])

    def test_start_anchor_sender_body_and_cache_match_a_cold_reopen(self):
        self.append(message(1, "received"), message(2, "queued"))
        self.assertEqual(self.index()["landmarks"], [])
        self.append(message(8, "started"))
        started = self.index()["landmarks"]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0], {
            "key": "cross-chat:handoff:handoff_one", "kind": "system", "start_seq": 8, "end_seq": 8,
            "title": "Message from Research agent", "preview": "Prepared message body", "meta": "",
            "timestamp": "2026-09-10T10:00:08Z",
        })
        self.append(message(9, "delivered"))
        warm = deepcopy(self.index())
        self.ns["TIMELINE_INDEX_CACHE"].clear()
        self.assertEqual(self.index(), warm)
        selected = self.page()
        self.assertEqual([event["seq"] for event in selected["events"]], [8, 9])
        self.assertEqual(selected["semantic_total"], 1)
        self.assertEqual(self.page(semantic_before=8)["events"], [])

    def test_fast_delivery_without_started_uses_delivered_anchor(self):
        self.append(message(1, "received"), message(5, "delivered"))
        self.assertEqual(self.index()["landmarks"][0]["start_seq"], 5)
        self.assertEqual([event["seq"] for event in self.page()["events"]], [5])

    def test_busy_recipient_history_pages_follow_execution_not_queue_receipt(self):
        self.append({
            "id": "busy-start", "seq": 1, "session_id": "recipient", "type": "turn_started",
            "ts": "2026-09-10T10:00:01Z", "run_id": "busy-run", "prompt": "Finish existing work",
        }, message(2, "received"), message(3, "queued"))
        self.assertEqual([item["key"] for item in self.index()["landmarks"]], ["turn:busy-run"])
        self.append({
            "id": "busy-finish", "seq": 5, "session_id": "recipient", "type": "turn_finished",
            "ts": "2026-09-10T10:00:05Z", "run_id": "busy-run", "result_text": "Existing result",
        }, message(6, "started"), message(9, "delivered"))
        self.assertEqual([item["start_seq"] for item in self.index()["landmarks"]], [1, 6])
        tail = self.page(limit=1)
        self.assertEqual([event["seq"] for event in tail["events"]], [6, 9])
        self.assertEqual(tail["next_semantic_before"], 6)
        earlier = self.page(limit=1, semantic_before=6)
        self.assertEqual([event["seq"] for event in earlier["events"]], [1, 5])
        self.assertEqual(earlier["semantic_total"], 2)
        warm = deepcopy(self.index())
        self.ns["TIMELINE_INDEX_CACHE"].clear()
        self.assertEqual(self.index(), warm)

    def test_sent_message_stays_at_acceptance_with_independent_reply(self):
        self.append(message(1, "registered", session_id="sender"), message(3, "queued", session_id="sender"))
        sent = self.index("sender")["landmarks"][0]
        self.assertEqual((sent["start_seq"], sent["title"]), (1, "Sent to Desktop agent"))
        self.append(message(6, "started", session_id="sender", source_session_id="recipient", target_session_id="sender",
                            source_title="Desktop agent", target_title="Research agent",
                            handoff_id="handoff_reply", cross_chat_envelope_id="handoff_reply", message_id="handoff_reply"))
        self.assertEqual([item["key"] for item in self.index("sender")["landmarks"]], [
            "cross-chat:handoff:handoff_one", "cross-chat:handoff:handoff_reply",
        ])

    def test_delivery_output_does_not_replace_message_sender_or_body(self):
        self.append(message(1, "received"), message(5, "started"), message(6, "started",
            type="assistant_text", purpose="cross_chat_handoff_delivery", run_id="delivery-run", text="Agent answer"))
        landmark = self.index()["landmarks"][0]
        self.assertEqual((landmark["start_seq"], landmark["title"], landmark["preview"]),
                         (5, "Message from Research agent", "Prepared message body"))
        page = self.page()
        self.assertTrue(any(event.get("text") == "Agent answer" for event in page["events"]))

    def test_completed_mailbox_turn_keeps_commentary_between_cards_on_reopen(self):
        events = []

        def emit(event_type, **fields):
            seq = len(events) + 1
            event = {
                "id": f"event-{seq}", "seq": seq, "session_id": "recipient",
                "ts": "2026-09-10T10:00:00Z", "type": event_type,
                "run_id": "ordinary-run", **fields,
            }
            events.append(event)
            return event

        emit("turn_started", prompt="Work with the other chats")
        commentary = []
        cards = []
        for section in range(6):
            commentary.append(emit(
                "reasoning_summary", phase="commentary",
                text=f"Public progress paragraph {section}",
                item_id=f"commentary-{section}",
            ))
            if section == 5:
                continue
            for offset in range(5):
                number = section * 5 + offset
                for tool in range(4):
                    tool_id = f"tool-{number}-{tool}"
                    emit("tool_started", tool_id=tool_id, tool_name="exec_command")
                    emit("tool_finished", tool_id=tool_id, tool_name="exec_command")
                incoming = number % 2 == 0
                cards.append(emit(
                    "chat_conversation_message_received" if incoming else "chat_conversation_message_registered",
                    conversation_mode="async_route_v1", delivery_mode="mailbox",
                    handoff_id=f"mail-{number}", cross_chat_envelope_id=f"mail-{number}",
                    message_id=f"mail-{number}", conversation_id="pair_one",
                    source_session_id="sender" if incoming else "recipient",
                    target_session_id="recipient" if incoming else "sender",
                    source_title="Research agent", target_title="Desktop agent",
                    handoff_preview=f"Message {number}",
                    run_id=None if incoming else "ordinary-run",
                ))
        final = emit("assistant_text", text="Final answer after all messages")
        emit("turn_finished", result_text=final["text"])
        self.append(*events)
        original_anchors = [
            item["start_seq"] for item in self.index()["landmarks"]
            if item["key"].startswith("cross-chat:")
        ]
        first_read = len(events)
        for card in cards:
            emit("chat_conversation_message_read", **{
                key: value for key, value in card.items()
                if key not in {"id", "seq", "ts", "type", "run_id"}
            }, run_id=None)
        self.append(*events[first_read:])

        expected = [event["seq"] for event in events if event in commentary or event in cards or event is final]
        page = self.page(limit=100)
        actual = [event["seq"] for event in page["events"] if (
            event.get("phase") == "commentary"
            or event["type"] in {"chat_conversation_message_received", "chat_conversation_message_registered", "assistant_text"}
        )]
        self.assertEqual(actual, expected)
        self.assertLess(
            sum(event["type"].startswith("tool_") for event in page["events"]),
            sum(event["type"].startswith("tool_") for event in events),
        )
        self.assertEqual(
            [item["start_seq"] for item in self.index()["landmarks"] if item["key"].startswith("cross-chat:")],
            original_anchors,
        )
        self.assertEqual(original_anchors, [card["seq"] for card in cards])
        self.ns["TIMELINE_INDEX_CACHE"].clear()
        self.assertEqual(self.page(limit=100), page)

        # The turn can be selected independently of the newer message cards.
        # Incoming mail has no run_id, but still separates its public progress.
        older = self.page(limit=1, semantic_before=cards[0]["seq"])
        self.assertEqual(
            [event["seq"] for event in older["events"] if event.get("phase") == "commentary"],
            [event["seq"] for event in commentary],
        )

    def test_mail_read_update_does_not_expand_an_unrelated_completed_turn(self):
        self.append(message(1, "received", delivery_mode="mailbox"), {
            "id": "start", "seq": 2, "type": "turn_started",
            "session_id": "recipient", "run_id": "later-run", "prompt": "Unrelated work",
        }, {
            "id": "commentary", "seq": 3, "type": "reasoning_summary", "phase": "commentary",
            "session_id": "recipient", "run_id": "later-run", "text": "Earlier progress",
        }, *({
            "id": f"tool-{seq}", "seq": seq, "type": "tool_finished",
            "session_id": "recipient", "run_id": "later-run", "tool_name": "exec_command",
        } for seq in range(4, 44)), message(44, "read", delivery_mode="mailbox"), {
            "id": "final", "seq": 45, "type": "assistant_text",
            "session_id": "recipient", "run_id": "later-run", "text": "Final answer",
        }, {
            "id": "finished", "seq": 46, "type": "turn_finished",
            "session_id": "recipient", "run_id": "later-run", "result_text": "Final answer",
        })
        page = self.page()
        self.assertNotIn(3, [event["seq"] for event in page["events"]])
        self.assertIn(45, [event["seq"] for event in page["events"]])
        self.assertLess(len(page["events"]), 20)

    def test_legacy_handoff_and_exchange_keys_and_visibility_are_unchanged(self):
        self.append(message(1, "received", type="cross_chat_handoff_received", conversation_mode=None))
        self.assertEqual(self.index()["landmarks"][0]["key"], "cross_chat:handoff_one")
        self.assertEqual(self.index()["landmarks"][0]["kind"], "cross_chat")
        self.assertEqual(self.ns["timeline_index_cross_chat_key"]({
            "type": "cross_chat_exchange_leg_started", "exchange_id": "exchange_one", "exchange_leg_id": "leg_one",
        }), "cross_chat_exchange:exchange_one:leg:leg_one")


if __name__ == "__main__":
    unittest.main()
