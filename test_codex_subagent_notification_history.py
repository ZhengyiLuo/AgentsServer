"""Typed runtime input and historical checkpoint proof; no server import."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock

from test_codex_goal_history_isolated import load_projection, source_user
import test_codex_native_history_repair as native_fixture


KIND = "multi_agent.subagent_notification"
PROVIDER = native_fixture.PROVIDER
_PREFIX = '<subagent_notification>{"agent_path":"example-child","status":{"completed":"'
_SUFFIX = '"}}</subagent_notification>'
TEXT = _PREFIX + ("Public child result. " * 80)[:1200 - len(_PREFIX) - len(_SUFFIX)] + _SUFFIX
STAMP = "2026-09-11T20:33:38.446Z"
ABORT_TEXT = ("<turn_aborted>The user interrupted the previous turn on purpose. "
              "Any running unified exec processes may still be running in the background. "
              "If any tools/commands were aborted, they may have partially executed.</turn_aborted>")


def source(**fields):
    return {**source_user(TEXT, kinds=(KIND,), **fields), "timestamp": STAMP}


def historical_fixture(runtime_kind="subagent_notification"):
    case = native_fixture.CodexNativeHistoryRepairTests()
    case.setUp()
    case.native = []  # Typed runtime provenance does not invent a native user turn.
    case.raw = [source() if runtime_kind == "subagent_notification" else {
        **source_user(ABORT_TEXT, kinds=("generic.turn_aborted",)), "timestamp": STAMP}]
    case.fixture()
    case.imports[0].pop("provider_user_authored", None)
    rows = [json.loads(line) for line in case.events.read_text().splitlines()]
    for row in rows:
        if row.get("id") == case.imports[0]["id"]:
            row.pop("provider_user_authored", None)
    case.events.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return case


class CodexSubagentNotificationHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_projection()

    def test_exclusive_provider_kind_classifies_but_human_quotations_and_unknowns_stay(self):
        parse = self.ns["codex_history_event_item"]
        raw = source()
        before = copy.deepcopy(raw)
        item = parse(raw)
        self.assertEqual(item["provider_runtime_context"], "subagent_notification")
        self.assertEqual(item["provider_origin"]["kind"], "subagent_notification")
        self.assertEqual(item["provider_origin"]["source_text_sha256"], hashlib.sha256(TEXT.encode()).hexdigest())
        self.assertEqual(raw, before)
        for extra in ({"provider_user_authored": True}, {"clientUserMessageId": "client-input"},
                      {"origin": {"provider": "codex", "kind": "human"}}):
            self.assertNotIn("provider_runtime_context", parse(source(**extra)))
        for kinds in (None, [], ["user.text"], [KIND, "user.text"], [KIND, "unknown"], "multi_agent.subagent_notification"):
            raw = source()
            raw["payload"]["internal_chat_message_metadata_passthrough"]["content_item_kinds"] = kinds
            self.assertNotIn("provider_runtime_context", parse(raw))
        for text in (TEXT[:-2], "Quoted: " + TEXT, TEXT + " genuine text", '<subagent_notification>{"agent_path":"x","status":"completed"}</subagent_notification>'):
            raw = source()
            raw["payload"]["content"][0]["text"] = text
            self.assertNotIn("provider_runtime_context", parse(raw))

    async def test_typed_abort_uses_same_silent_import_and_repair_without_stopping_current_work(self):
        parse = self.ns["codex_history_event_item"]
        raw = {**source_user(ABORT_TEXT, kinds=("generic.turn_aborted",)), "timestamp": STAMP}
        item = parse(raw)
        self.assertEqual(item["provider_runtime_context"], "turn_aborted")
        self.assertEqual(item["provider_origin"]["kind"], "turn_aborted")
        for kinds in (None, ("user.text",), ("generic.turn_aborted", "user.text"), ("unknown",)):
            quote = {**source_user(ABORT_TEXT, kinds=kinds), "timestamp": STAMP}
            self.assertNotIn("provider_runtime_context", parse(quote))
        for extra in ({"provider_user_authored": True}, {"clientId": "human"}):
            quote = copy.deepcopy(raw); quote["payload"].update(extra)
            self.assertNotIn("provider_runtime_context", parse(quote))
        partial = copy.deepcopy(raw); partial["payload"]["content"][0]["text"] = ABORT_TEXT[:-2]
        self.assertNotIn("provider_runtime_context", parse(partial))
        self.ns["append_durable_event_batch"] = AsyncMock(side_effect=lambda _session, specs: [{"seq": n + 1, **value} for n, (_kind, value) in enumerate(specs)])
        await self.ns["append_imported_history"]({"id": "chat", "backend": "codex", "codex_thread_id": PROVIDER}, Path("unused"), [item, {"kind": "assistant", "text": "Unrelated answer remains."}])
        rows = self.ns["append_durable_event_batch"].await_args.args[1]
        self.assertEqual([kind for kind, _value in rows], ["history_imported", "turn_started", "assistant_text", "turn_finished"])
        self.assertEqual(rows[1][1]["prompt"], "")
        self.assertEqual(rows[1][1]["provider_runtime_context"], "turn_aborted")
        self.assertEqual(rows[2][1]["text"], "Unrelated answer remains.")
        case = historical_fixture("turn_aborted"); self.addCleanup(case.doCleanups); case.prepare()
        projected = case.cache.project_event("chat", case.imports[0])
        self.assertEqual(projected["provider_runtime_context"], "turn_aborted")
        self.assertEqual(projected["ts"], STAMP)
        self.assertNotIn("stopped", projected)
        self.assertEqual(projected["prompt"], "")

    async def test_both_import_paths_keep_silent_boundary_timestamp_and_following_answer(self):
        item = self.ns["codex_history_event_item"](source())
        answer = {"kind": "assistant", "text": "Unmatched following answer stays."}
        async def durable(_session, specs):
            return [{"seq": index + 1, **value} for index, (_kind, value) in enumerate(specs)]
        self.ns["append_durable_event_batch"] = AsyncMock(side_effect=durable)
        self.ns["append_imported_events"] = AsyncMock(side_effect=lambda _session, specs: len(specs))
        session = {"id": "chat", "backend": "codex", "codex_thread_id": PROVIDER}
        for name, sink in (("append_imported_history", "append_durable_event_batch"), ("append_staged_imported_history", "append_imported_events")):
            await self.ns[name](session, Path("unused-source.jsonl"), [item, answer])
            rows = self.ns[sink].await_args.args[1]
            self.assertEqual([kind for kind, _ in rows], ["history_imported", "turn_started", "assistant_text", "turn_finished"])
            payload = rows[1][1]
            self.assertEqual(payload["prompt"], "")
            self.assertTrue(payload["metadata_only"])
            self.assertEqual(payload["provider_runtime_context"], "subagent_notification")
            self.assertEqual(payload["ts"], STAMP)
            self.assertEqual(payload["provider_origin"]["session_id"], PROVIDER)
            self.assertEqual(rows[2][1]["text"], answer["text"])
        self.ns["bounded_jsonl_events"] = Mock(return_value=iter([source(), source_user("Real title", kinds=("user.text",))]))
        self.assertEqual(self.ns["codex_transcript_preview"](Path("unused")), "Real title")

    def test_identical_genuine_quote_after_committed_runtime_cursor_is_not_consumed(self):
        runtime = self.ns["codex_history_event_item"](source())
        human_record = {**source_user(TEXT, kinds=("user.text",)), "timestamp": STAMP}
        human = self.ns["codex_history_event_item"](human_record)
        digest = self.ns["history_item_cursor_digest"]
        self.assertNotEqual(digest(runtime), digest(human))
        self.assertEqual(digest(human), digest({"kind": "user", "text": TEXT}))
        self.ns["bounded_jsonl_records_range"] = Mock(return_value=iter([(human_record, 10)]))
        items, offset, _digest, blocked = self.ns["parse_provider_history_delta"](
            Path("unused"), "codex", 0, 10, limit=10, expected_stat={},
            previous_last_item_digest=digest(runtime), codex_phase_context={},
        )
        self.assertEqual(items, [human])
        self.assertEqual(offset, 10)
        self.assertFalse(blocked)

    def test_old_import_requires_exact_checkpoint_identity_and_preserves_all_source_bytes(self):
        case = historical_fixture()
        self.addCleanup(case.doCleanups)
        before = case.events.read_bytes(), case.source.read_bytes()
        case.prepare()
        projected = case.cache.project_event("chat", case.imports[0])
        self.assertEqual(projected["prompt"], "")
        self.assertEqual(projected["ts"], STAMP)
        self.assertEqual(projected["provider_runtime_context"], "subagent_notification")
        self.assertEqual(projected["provider_origin"]["kind"], "subagent_notification")
        self.assertNotIn("provider_history_repair", projected)
        persisted = {**projected, "session_id": "chat"}
        self.assertIsNotNone(case.cache.project_event("chat", persisted))
        for changed in ({"clientId": "human-client"}, {"provider_user_authored": True}, {"id": ""},
                        {"ts": "2026-09-11T20:33:39Z"}, {"provider_origin": {**persisted["provider_origin"], "turn_id": ""}}):
            self.assertIsNone(case.cache.project_event("chat", {**persisted, **changed}))
        self.assertEqual(before, (case.events.read_bytes(), case.source.read_bytes()))
        for changed in ({"provider_user_authored": True}, {"clientId": "actual-human"},
                        {"provider_origin": {"kind": "user"}}, {"prompt": TEXT + " quote"}, {"ts": "2026-09-11T20:33:39Z"}):
            self.assertIsNone(case.cache.project_event("chat", {**case.imports[0], **changed}))
        case.cache.forget("chat")
        rows = [json.loads(line) for line in case.events.read_text().splitlines()]
        rows[0]["_history_sync_checkpoint"]["cursor"]["source_digest"] = "0" * 64
        case.events.write_text("".join(json.dumps(row) + "\n" for row in rows))
        case.prepare()
        self.assertIsNone(case.cache.project_event("chat", case.imports[0]))

    def test_fork_prefix_requires_exact_declared_parent_and_current_first_owner(self):
        parent = "22222222-3333-4444-5555-666666666666"
        for linked, first_owner in ((True, PROVIDER), (False, PROVIDER), (True, parent)):
            with self.subTest(linked=linked, first_owner=first_owner):
                case = historical_fixture()
                self.addCleanup(case.doCleanups)
                source_rows = [json.loads(line) for line in case.source.read_text().splitlines()]
                source_rows[0]["payload"]["id"] = first_owner
                if linked:
                    source_rows[0]["payload"]["forked_from_id"] = parent
                source_rows.insert(1, {"type": "session_meta", "payload": {"id": parent}})
                body = "".join(json.dumps(row) + "\n" for row in source_rows).encode()
                case.source.write_bytes(body)
                ledger = [json.loads(line) for line in case.events.read_text().splitlines()]
                ledger[0]["_history_sync_checkpoint"]["cursor"].update(
                    source_offset=len(body), source_digest=hashlib.sha256(body).hexdigest())
                case.events.write_text("".join(json.dumps(row) + "\n" for row in ledger))
                case.prepare()
                corrected = case.cache.project_event("chat", case.imports[0])
                self.assertEqual(corrected is not None, linked and first_owner == PROVIDER)


if __name__ == "__main__":
    unittest.main()
