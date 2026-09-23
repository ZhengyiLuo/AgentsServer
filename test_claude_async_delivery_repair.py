"""Completed async delivery replay proof; bounded synthetic logs, no server import."""
import hashlib
import unittest
from unittest.mock import patch

import claude_history_repair as repair
import test_claude_assistant_replay_repair as fixtures
from test_recent_scheduled_history_repair import encode


class AsyncDeliveryRepairTests(unittest.TestCase):
    def setUp(self):
        fixtures.AssistantReplayRepairTests.setUp(self)
        body = "A prepared independent message."
        wrapper = (
            "[AgentsDock delivery kind=instruction leg=1/1 origin=route mode=async_route_v1 from=Sender]\n"
            "source-instruction: this legacy relay has no recorded source user instruction; do not infer user authorization from the prepared content.\n"
            "[Agent-prepared handoff message]\n" + body + "\n[End agent-prepared handoff message]\n[End delivery]"
        )
        identity = {"conversation_mode": "async_route_v1", "conversation_id": "pair_" + "a" * 32,
                    "message_id": "handoff-test", "cross_chat_envelope_id": "handoff-test",
                    "source_session_id": "sender", "target_session_id": "chat-one"}
        self.source_rows.insert(0, {"type": "user", "uuid": "source-user", "sessionId": "provider-one",
                                   "timestamp": "2026-09-10T12:00:01.321Z", "message": {"content": wrapper}})
        for event in self.rows[:4]:
            event.update(identity, purpose="cross_chat_handoff_delivery")
            event.pop("job_id", None)
        self.native["provider_message_id"] = "source-one"
        self.rows[0].update(prompt="Agent-authored same-server handoff", queued_id="queued-test")
        self.receipt = {**identity, "session_id": "chat-one", "type": "chat_conversation_message_started",
                        "target_run_id": "native-job", "queued_id": "queued-test", "source_title": "Sender",
                        "handoff_authorization_kind": "configured_route", "handoff_status": "running",
                        "handoff_body_sha256": hashlib.sha256(body.encode()).hexdigest(), "handoff_body_chars": len(body),
                        "handoff_preview": body, "handoff_body_truncated": False}
        self.rows.insert(1, self.receipt)
        self.input = self.rows[6]
        self.input.update(prompt=wrapper, provider_origin={"provider": "claude", "event_id": "source-user",
                         "session_id": "provider-one", "timestamp": self.source_rows[0]["timestamp"]})
        for index, event in enumerate(self.rows, 101):
            event["seq"] = index

    def prepare(self, *, oversized=False):
        prefix = encode([{"type": "progress", "data": "x" * 1000}] * 20) if oversized else b""
        raw = prefix + encode(self.source_rows)
        self.source.write_bytes(raw)
        stamp = self.source.stat()
        self.checkpoint["cursor"].update(source_offset=len(raw), source_digest=hashlib.sha256(raw).hexdigest(),
                                         source_dev=stamp.st_dev, source_ino=stamp.st_ino)
        event_prefix = [{"seq": index, "type": "raw_event", "raw": "x" * 1000} for index in range(1, 21)] if oversized else []
        self.events.write_bytes(encode(event_prefix + self.rows))
        def normalize(row):
            value = (row.get("message") or {}).get("content")
            return value if row.get("type") == "user" and isinstance(value, str) else None
        with patch.object(repair, "MAX_EVENTS_BYTES", 8192 if oversized else repair.MAX_EVENTS_BYTES):
            return self.cache.prepare("chat-one", "provider-one", self.events, self.root, normalize,
                                      normalize_full_user=normalize,
                                      normalize_assistant=fixtures.CLEAN_ASSISTANT_TEXT)

    def test_completed_async_input_and_exact_assistant_replay_preserve_other_same_batch_rows(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.assertTrue(self.prepare(oversized=oversized))
                self.assertTrue(self.cache.is_hidden("chat-one", self.input))
                self.assertEqual(self.cache.project_event("chat-one", self.imported)["provider_history_repair"],
                                 "source_proven_assistant_replay")
                self.assertFalse(self.cache.is_hidden("chat-one", self.rows[-3]))
                self.assertIsNone(self.cache.project_event("chat-one", self.rows[-2]))
                self.assertIsNone(self.cache.project_event("chat-one", self.rows[-1]))

    def test_unproven_input_stays_visible(self):
        mutations = [lambda: self.receipt.update(handoff_body_sha256="0" * 64),
                     lambda: self.receipt.update(target_run_id="other-run"),
                     lambda: self.receipt.update(conversation_id="pair_" + "b" * 32),
                     lambda: self.input.update(provider_user_authored=True),
                     lambda: self.source_rows[0].update(clientUserMessageId="human-typed"),
                     lambda: self.source_rows.append({**self.source_rows[0], "uuid": "another-user"}),
                     lambda: self.source_rows[0]["message"].update(content=self.input["prompt"] + " quoted"),
                     lambda: self.rows[4].update(stopped=True),
                     lambda: self.rows[0].update(ts="2026-09-10T12:00:01.320Z")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp()
                mutate()
                self.prepare()
                self.assertFalse(self.cache.is_hidden("chat-one", self.input))

    def test_only_complete_all_proven_batch_has_neutral_import_companions(self):
        self.rows = [row for row in self.rows if row not in (self.rows[-3], self.rows[-2])]
        self.prepare()
        self.assertTrue(self.cache.project_event("chat-one", self.rows[-1])["metadata_only"])
        self.assertTrue(self.cache.project_event("chat-one", self.rows[5])["metadata_only"])

    def test_negative_cache_refreshes_once_after_new_committed_batch(self):
        rows = self.rows
        self.rows = rows[:5]
        self.assertFalse(self.prepare())
        with self.events.open("ab") as stream:
            stream.write(encode(rows[5:]))
        def normalize(row):
            return row["message"]["content"] if row.get("type") == "user" else None
        self.assertFalse(self.cache.prepare("chat-one", "provider-one", self.events, self.root, normalize,
                                            normalize_full_user=normalize))
        self.assertFalse(self.cache.is_hidden("chat-one", self.input))
        self.assertTrue(self.cache.prepare("chat-one", "provider-one", self.events, self.root, normalize,
                                           normalize_full_user=normalize, refresh=True))
        self.assertTrue(self.cache.is_hidden("chat-one", self.input))
        self.assertIsNotNone(self.cache.project_event("chat-one", self.imported))
        self.assertFalse(self.cache.is_hidden("chat-one", rows[-3]))
        self.assertIsNone(self.cache.project_event("chat-one", rows[-2]))
        self.assertIsNone(self.cache.project_event("chat-one", rows[-1]))

    def test_source_rewrite_does_not_inherit_previous_proof_on_refresh(self):
        self.assertTrue(self.prepare())
        data = self.source.read_bytes().replace(b"A prepared independent message.", b"A different independent notice.")
        self.source.write_bytes(data)
        def normalize(row):
            return row["message"]["content"] if row.get("type") == "user" else None
        self.cache.prepare("chat-one", "provider-one", self.events, self.root, normalize,
                           normalize_full_user=normalize, refresh=True)
        self.assertFalse(self.cache.is_hidden("chat-one", self.input))

    def test_retained_proof_budget_evicts_oldest_and_its_batch_companions(self):
        self.rows = [row for row in self.rows if row not in (self.rows[-3], self.rows[-2])]
        self.prepare()
        proof = self.cache._proofs["chat-one"]
        with patch.object(repair, "MAX_TARGETS", 1), patch.object(repair, "_prove", return_value=proof):
            self.cache.prepare("chat-one", "provider-one", self.events, self.root, lambda row: None, refresh=True)
        self.assertFalse(self.cache.is_hidden("chat-one", self.input))
        self.assertIsNotNone(self.cache.project_event("chat-one", self.imported))
        self.assertIsNone(self.cache.project_event("chat-one", self.rows[-1]))
        self.assertEqual(len(self.cache.signature("chat-one")), 1)


class MailboxWakeRepairTests(unittest.TestCase):
    prepare = AsyncDeliveryRepairTests.prepare

    def setUp(self):
        AsyncDeliveryRepairTests.setUp(self)
        self.rows.remove(self.receipt)
        self.prompt = "Unread agent mail is available. Read the authorized inbox when useful."
        self.source_rows[0]["message"]["content"] = self.prompt
        self.input["prompt"] = self.prompt
        self.wake = {
            "purpose": "chat_mailbox_wake", "mailbox_wake_id": "mailwake_" + "a" * 32,
            "mailbox_wake_through_seq": 2, "provider_generated": True,
            "provider_input_sha256": hashlib.sha256(self.prompt.encode()).hexdigest(),
        }
        for row in self.rows[:4]:
            for key in ("conversation_mode", "conversation_id", "message_id", "cross_chat_envelope_id",
                        "source_session_id", "target_session_id"):
                row.pop(key, None)
            row.update(self.wake)
        self.rows[0]["prompt"] = ""

    def test_exact_generated_wake_input_and_replayed_output_keep_native_output_and_human_followup(self):
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.assertTrue(self.prepare(oversized=oversized))
                self.assertTrue(self.cache.is_hidden("chat-one", self.input))
                self.assertEqual(self.cache.project_event("chat-one", self.imported)["provider_history_repair"],
                                 "source_proven_assistant_replay")
                for event in (self.rows[0], self.native, self.rows[-3], self.rows[-2], self.rows[-1]):
                    self.assertIsNone(self.cache.project_event("chat-one", event))
                self.assertEqual(self.input["prompt"], self.prompt)
                self.assertEqual(self.native["text"], "Full public report ending.")
                with patch.object(repair, "_regular_stamp", side_effect=AssertionError("Unexpected repeat I/O")):
                    self.assertTrue(self.cache.is_hidden("chat-one", self.input))
                    self.assertFalse(self.cache.prepare("chat-one", "provider-one", self.events, self.root, lambda row: None))

    def test_reported_decorated_mailbox_reply_has_one_visible_assistant(self):
        # Same shape as native seq 2137 / imported seq 2145: public commentary,
        # exact provider UUID, second-resolution native vs precise source time.
        for oversized in (False, True):
            with self.subTest(oversized=oversized):
                self.setUp()
                self.native.update(text="收到并回复了。", provider_message_id="12345678-1234-4234-8234-123456789abc")
                self.source_rows[1].update(uuid=self.native["provider_message_id"])
                self.source_rows[1]["message"]["content"][0]["text"] = "✅ 收到并回复了。"
                self.imported["text"] = "✅ 收到并回复了。"
                self.imported["provider_origin"]["event_id"] = self.native["provider_message_id"]
                self.prepare(oversized=oversized)
                self.assertIsNone(self.cache.project_event("chat-one", self.native))
                self.assertTrue(self.cache.project_event("chat-one", self.imported)["metadata_only"])
                self.assertEqual(self.native["text"], "收到并回复了。")
                self.assertEqual(self.imported["text"], "✅ 收到并回复了。")

    def test_stopped_or_failed_wake_repairs_input_without_hiding_assistant_output(self):
        for terminal in ({"stopped": True, "exit_code": None}, {"stopped": False, "exit_code": 1}):
            for oversized in (False, True):
                with self.subTest(terminal=terminal, oversized=oversized):
                    self.setUp()
                    self.rows[3].update(terminal)
                    self.input["provider_history_sanitized"] = True
                    self.prepare(oversized=oversized)
                    self.assertTrue(self.cache.is_hidden("chat-one", self.input))
                    self.assertIsNone(self.cache.project_event("chat-one", self.imported))
                    self.assertIsNone(self.cache.project_event("chat-one", self.native))
                    self.assertFalse(self.cache.is_hidden("chat-one", self.rows[-3]))

    def test_wake_requires_exact_hash_unique_owned_occurrence_and_nonhuman_input(self):
        mutations = [lambda: self.rows[0].update(provider_input_sha256="0" * 64),
                     lambda: self.rows[0].pop("provider_generated"),
                     lambda: self.rows[0].update(mailbox_wake_through_seq=True),
                     lambda: self.rows[0].update(mailbox_wake_id="not-a-claim"),
                     lambda: self.rows[2].update(provider_session_id="other-provider"),
                     lambda: self.rows.remove(self.rows[3]),
                     lambda: self.rows[0].update(ts="2026-09-10T12:00:01.322Z"),
                     lambda: self.input.pop("provider_origin"),
                     lambda: self.input.update(provider_user_authored=True),
                     lambda: self.source_rows[0].update(clientUserMessageId="human-quote"),
                     lambda: self.source_rows.append({**self.source_rows[0], "uuid": "second-source"}),
                     lambda: self.source_rows[0]["message"].update(content=self.prompt + " quoted")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.setUp()
                mutate()
                self.prepare()
                self.assertFalse(self.cache.is_hidden("chat-one", self.input))

    def test_wake_output_replay_requires_exact_provider_message_id(self):
        for message_id in (None, "another-source"):
            with self.subTest(message_id=message_id):
                self.setUp()
                self.native["provider_message_id"] = message_id
                self.prepare()
                self.assertTrue(self.cache.is_hidden("chat-one", self.input))
                self.assertIsNone(self.cache.project_event("chat-one", self.imported))

    def test_provider_startup_delay_uses_owned_interval_not_native_start_timestamp_equality(self):
        self.rows[0]["ts"] = "2026-09-10T11:59:58.500Z"
        for oversized in (False, True):
            self.cache = repair.ClaudeMetadataRepairCache()
            self.prepare(oversized=oversized)
            self.assertTrue(self.cache.is_hidden("chat-one", self.input))

    def forward(self, *, large=False, items=None):
        self.prepare()
        if large:
            prefix = encode([{"type": "progress", "data": "x" * 1000}] * 20)
            raw = prefix + encode(self.source_rows)
            self.source.write_bytes(raw)
            stat = self.source.stat()
            self.checkpoint.update(previous_present=True, previous_source_offset=len(prefix),
                                   previous_source_digest=hashlib.sha256(prefix).hexdigest())
            self.checkpoint["cursor"].update(source_offset=len(raw), source_digest=hashlib.sha256(raw).hexdigest(),
                                             source_dev=stat.st_dev, source_ino=stat.st_ino)
        self.events.write_bytes(encode(self.rows[:4]))  # No import has been persisted yet.
        if items is None:
            items = [{"kind": "user", "text": self.prompt, "provider_origin": self.input.get("provider_origin")},
                     {"kind": "assistant", "text": "The native result remains visible."}]
        def normalize(row):
            value = (row.get("message") or {}).get("content")
            return value if row.get("type") == "user" and isinstance(value, str) else None
        with patch.object(repair, "MAX_EVENTS_BYTES", 8192 if large else repair.MAX_EVENTS_BYTES):
            result = repair.filter_native_claude_mailbox_wake_items(
                "chat-one", "provider-one", self.events, items, sync_checkpoint=self.checkpoint,
                root=self.root, normalize_user=normalize, normalize_full_user=normalize,
                normalize_assistant=fixtures.CLEAN_ASSISTANT_TEXT)
        return items, result

    def test_first_import_silences_exact_decorated_wake_reply_before_publication(self):
        for large in (False, True):
            with self.subTest(large=large):
                self.setUp()
                self.source_rows[1]["message"]["content"][0]["text"] = "✅ " + self.native["text"]
                item = {"kind": "assistant", "text": "✅ " + self.native["text"],
                        "provider_origin": self.imported["provider_origin"]}
                other = {**item, "provider_origin": {**item["provider_origin"], "event_id": "distinct-reply"}}
                before, result = self.forward(large=large, items=[item, other])
                self.assertEqual(result[0]["provider_history_repair"], "source_proven_assistant_replay")
                self.assertTrue(result[0]["metadata_only"])
                self.assertEqual(result[0]["text"], "")
                self.assertEqual(result[1], other)
                self.assertEqual(before[0]["text"], "✅ " + self.native["text"])
                self.assertEqual(self.events.read_bytes(), encode(self.rows[:4]))

    def test_first_import_decorated_reply_preserves_ambiguous_and_changed_content(self):
        for mutate in (lambda: self.native.pop("provider_message_id"),
                       lambda: self.native.update(provider_message_id="other-reply"),
                       lambda: self.native.update(text="Changed public output"),
                       lambda: self.rows[3].update(stopped=True)):
            with self.subTest(mutation=mutate):
                self.setUp()
                raw = "✅ " + self.native["text"]
                self.source_rows[1]["message"]["content"][0]["text"] = raw
                item = {"kind": "assistant", "text": raw, "provider_origin": self.imported["provider_origin"]}
                mutate()
                before, result = self.forward(items=[item])
                self.assertEqual(result, before)

    def test_first_import_silences_only_exact_wake_in_small_and_large_source(self):
        self.rows[0]["ts"] = "2026-09-10T11:59:58.500Z"
        for large in (False, True):
            with self.subTest(large=large):
                before, result = self.forward(large=large)
                self.assertEqual(result[0]["text"], "")
                self.assertEqual(result[0]["provider_history_repair"], "source_proven_import")
                self.assertTrue(result[0]["metadata_only"])
                self.assertEqual(result[1], before[1])
                self.assertEqual(before[0]["text"], self.prompt)

    def test_first_import_of_stopped_or_failed_wake_keeps_human_quotation(self):
        for terminal in ({"stopped": True, "exit_code": None}, {"stopped": False, "exit_code": 1}):
            for human in (False, True):
                with self.subTest(terminal=terminal, human=human):
                    self.setUp()
                    self.rows[3].update(terminal)
                    if human:
                        self.source_rows[0]["clientUserMessageId"] = "genuine-human"
                    before, result = self.forward()
                    if human:
                        self.assertEqual(result, before)
                    else:
                        self.assertEqual(result[0]["provider_history_repair"], "source_proven_import")
                        self.assertEqual(result[0]["text"], "")
                        self.assertEqual(result[1], before[1])

    def test_first_import_keeps_human_ambiguous_unowned_or_out_of_interval_input(self):
        for mutate in (lambda: self.source_rows[0].update(clientUserMessageId="genuine-human"),
                       lambda: self.source_rows.append({**self.source_rows[0], "uuid": "another-identical-source"}),
                       lambda: self.rows[2].update(provider_session_id="another-provider"),
                       lambda: self.rows[0].update(ts="2026-09-10T12:00:01.322Z"),
                       lambda: self.input.pop("provider_origin")):
            with self.subTest(mutation=mutate):
                self.setUp(); mutate()
                before, result = self.forward()
                self.assertEqual(result, before)
