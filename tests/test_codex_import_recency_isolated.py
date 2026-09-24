"""Source-proven Codex import recency: AST helpers and synthetic state only."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests.test_codex_goal_history_isolated import load_projection


class CodexImportRecencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_projection()
        common = {"session_id": "synthetic-chat", "backend": "codex", "imported": True,
            "metadata_only": True, "run_id": "import_synthetic"}
        self.batch = [
            {**common, "id": "marker", "seq": 101, "type": "history_imported",
                "ts": "2026-09-14T12:00:00Z", "message": "Imported 1 rough messages from codex history."},
            {**common, "id": "input", "seq": 102, "type": "turn_started", "prompt": "",
                "ts": "2026-09-11T12:00:00Z", "provider_user_authored": True,
                "provider_history_repair": "source_proven_native_replay", "provider_origin": {
                    "provider": "codex", "kind": "user", "session_id": "synthetic-provider", "turn_id": "synthetic-turn",
                    "event_id": "synthetic-source-input", "native_event_id": "synthetic-native-start",
                    "timestamp": "2026-09-11T12:00:00Z", "source_text_sha256": "a" * 64}},
            {**common, "id": "terminal", "seq": 103, "type": "turn_finished", "result_text": "",
                "ts": "2026-09-11T12:00:00Z", "message": "Imported history replay finished."},
        ]

    def test_exact_proven_batch_does_not_bump_recency_or_visible_activity(self):
        before = copy.deepcopy(self.batch)
        for event in self.batch:
            with self.subTest(kind=event["type"]):
                self.assertFalse(self.ns["should_bump_session_updated_at"](event["type"], event))
                self.assertFalse(self.ns["is_agent_visible_event"](event["type"], event))
        self.assertEqual(self.batch, before)

    async def test_metadata_update_preserves_recency_unread_and_live_owner_for_every_prefix(self):
        session = {"id": "synthetic-chat", "updated_at": "2026-09-13T12:00:00Z",
            "latest_agent_event_seq": 90, "latest_agent_event_at": "2026-09-13T12:00:00Z",
            "latest_agent_event_type": "assistant_text", "last_read_agent_event_seq": 90,
            "active_run": {"run_id": "live-native-owner", "backend": "codex"}}
        before = copy.deepcopy(session)
        store = SimpleNamespace(sessions={session["id"]: session}, save=AsyncMock())
        self.ns["STORE"] = store
        for event in self.batch:
            await self.ns["update_session_event_metadata"](session["id"], event)
            for key, value in before.items():
                self.assertEqual(session[key], value, key)
            self.assertEqual(session["latest_event_seq"], event["seq"])
            store.save.assert_not_awaited()

    def test_missing_malformed_or_nonempty_inputs_keep_existing_recency_behavior(self):
        start = self.batch[1]
        variants = [
            {"metadata_only": False}, {"imported": False}, {"backend": "claude"}, {"run_id": "native-run"},
            {"provider_history_repair": None}, {"prompt": "A genuine same-words human quotation."},
            {"provider_origin": None}, {"prompt": {"unexpected": "content"}}, {"file_ids": ["synthetic-file"]},
        ]
        for key, value in (("kind", "assistant"), ("provider", "unknown"), ("event_id", ""),
                           ("native_event_id", ""), ("timestamp", "invalid"), ("source_text_sha256", "invalid")):
            variants.append({"provider_origin": {**start["provider_origin"], key: value}})
        for patch in variants:
            with self.subTest(patch=patch):
                self.assertTrue(self.ns["should_bump_session_updated_at"]("turn_started", {**start, **patch}))
        for patch in ({"prompt": "A real instruction"}, {"text": "Actual content"}, {"file_ids": ["synthetic-file"]},
                      {"metadata_only": False}, {"run_id": 42}, {"error": "An actual failure"}, {"is_error": "malformed"}):
            with self.subTest(marker=patch):
                self.assertTrue(self.ns["should_bump_session_updated_at"]("history_imported", {**self.batch[0], **patch}))

    def test_real_output_and_native_activity_still_bump_recency(self):
        for kind, payload in (("turn_started", {"prompt": "Real user"}), ("assistant_text", {"text": "Real answer"}),
                              ("turn_finished", {**self.batch[2], "result_text": "Unmatched answer"}),
                              ("chat_conversation_message_received", {}), ("job_ran", {}), ("file_uploaded", {})):
            with self.subTest(kind=kind):
                self.assertTrue(self.ns["should_bump_session_updated_at"](kind, payload))

    def native_child_notice(self, *, human=False):
        return {"timestamp": "2026-09-14T12:00:00Z", "ordinal": 23,
            "type": "response_item", "payload": {"type": "message", "id": "native-notice",
            "role": "user", "content": [{"type": "input_text", "text":
                '<subagent_notification>\n{"agent_path":"child","status":{"completed":"CHILD_RESULT"}}\n</subagent_notification>'}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "native-turn",
                "create_time": 1789387200,
                "content_item_kinds": ["user.text" if human else "multi_agent.subagent_notification"]}}}

    async def test_native_child_notice_import_is_silent_and_keeps_recency(self):
        ns = self.ns
        source = self.native_child_notice()
        before = copy.deepcopy(source)
        item = ns["codex_history_event_item"](source)
        self.assertEqual(item["provider_runtime_context"], "subagent_notification")
        ns["append_durable_event_batch"] = AsyncMock(side_effect=lambda _sid, events: [
            {"seq": index} for index, _event in enumerate(events, 1)])
        ns["append_imported_events"] = AsyncMock(side_effect=lambda _sid, events: len(events))
        for name, sink in (("append_imported_history", "append_durable_event_batch"),
                           ("append_staged_imported_history", "append_imported_events")):
            session = {"id": "synthetic-chat", "backend": "codex", "codex_thread_id": "synthetic-root",
                "updated_at": "2026-09-13T12:00:00Z", "latest_agent_event_seq": 7,
                "active_run": {"run_id": "current-owner", "backend": "codex"}}
            previous = copy.deepcopy(session)
            ns["STORE"] = SimpleNamespace(sessions={session["id"]: session}, save=AsyncMock())
            await ns[name](session, Path("unused"), [item])
            rows = ns[sink].await_args.args[1]
            for index, (kind, payload) in enumerate(rows, 10):
                self.assertFalse(ns["should_bump_session_updated_at"](kind, payload), (name, kind))
                self.assertFalse(ns["is_agent_visible_event"](kind, payload), (name, kind))
                await ns["update_session_event_metadata"](session["id"], {
                    "id": f"event-{index}", "seq": index, "session_id": session["id"],
                    "ts": "2026-09-14T15:00:00Z", "type": kind, **payload})
            for key in ("updated_at", "latest_agent_event_seq", "active_run"):
                self.assertEqual(session[key], previous[key], (name, key))
            starts = [payload for kind, payload in rows if kind == "turn_started"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0]["prompt"], "")
            self.assertTrue(starts[0]["metadata_only"])
            ns["STORE"].save.assert_not_awaited()
            for patch in ({"provider_user_authored": True}, {"prompt": "Real content"},
                          {"provider_runtime_context": "unknown"},
                          {"provider_origin": {**starts[0]["provider_origin"], "source_text_sha256": "bad"}}):
                self.assertTrue(ns["should_bump_session_updated_at"]("turn_started", {**starts[0], **patch}))
        self.assertEqual(source, before)

    async def test_same_native_notice_quoted_by_human_stays_visible(self):
        item = self.ns["codex_history_event_item"](self.native_child_notice(human=True))
        self.assertTrue(item["provider_user_authored"])
        self.assertNotIn("provider_runtime_context", item)
        self.ns["append_durable_event_batch"] = AsyncMock(side_effect=lambda _sid, events: [
            {"seq": index} for index, _event in enumerate(events, 1)])
        await self.ns["append_imported_history"]({"id": "synthetic-chat", "backend": "codex",
            "codex_thread_id": "synthetic-root"}, Path("unused"), [item])
        rows = self.ns["append_durable_event_batch"].await_args.args[1]
        self.assertNotIn("metadata_only", rows[0][1])
        start = next(payload for kind, payload in rows if kind == "turn_started")
        self.assertIn("CHILD_RESULT", start["prompt"])
        self.assertTrue(self.ns["should_bump_session_updated_at"]("turn_started", start))

    def test_v2_native_agent_message_is_not_reimported_as_user_or_assistant(self):
        source = {"type": "response_item", "timestamp": "2026-09-14T12:00:00Z", "payload": {
            "type": "agent_message", "id": "typed-child-result", "author": "/root/child",
            "recipient": "/root", "content": [{"type": "input_text", "text": "CHILD_RESULT"}]}}
        before = copy.deepcopy(source)
        self.assertIsNone(self.ns["codex_history_event_item"](source))
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
