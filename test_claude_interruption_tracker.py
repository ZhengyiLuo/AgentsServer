"""Pure source-lineage tests: no server import, provider data, or runtime state."""
from copy import deepcopy
import unittest

from claude_history_provenance import ClaudeInterruptionTracker, normalize_claude_interruption_context


SESSION = "89b7a49a-4d4b-4f25-85c0-7ad297b0ea6b"
PROMPT = "addba5a5-803b-4b19-9e34-4b0e0ca9e2d1"
TIME = "2026-09-09T20:16:54.515Z"
MARKER = "[Request interrupted by user]"


def raw(number, kind="user", text="Genuine request", **kwargs):
    return {"uuid": f"12345678-1234-4234-8234-{number:012d}", "type": kind,
            "sessionId": SESSION, "promptId": PROMPT, "timestamp": TIME,
            "isSidechain": False,
            "message": {"role": kind, "content": [{"type": "text", "text": text}]}, **kwargs}


def lineage():
    prompt = raw(1)
    assistant = raw(2, "assistant", parentUuid=prompt["uuid"])
    result = raw(3, parentUuid=assistant["uuid"], message={"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tool", "content": "done"}]})
    attachment = raw(4, "attachment", parentUuid=result["uuid"], message=None)
    marker = raw(5, text=MARKER, parentUuid=attachment["uuid"])
    return [prompt, assistant, result, attachment, marker]


class ClaudeInterruptionTrackerTests(unittest.TestCase):
    def classify(self, rows, context=None):
        tracker = ClaudeInterruptionTracker(context)
        output = [tracker.consume(row) for row in rows]
        return output, tracker.export_context()

    def test_actual_sdk_shape_retains_original_identity_and_unknown_cause(self):
        rows = lineage()
        before = deepcopy(rows)
        output, context = self.classify(rows)
        self.assertEqual(output[:-1], [None] * 4)
        self.assertEqual(output[-1], {"provider": "claude", "kind": "interruption", "cause": "unknown",
            "event_id": rows[-1]["uuid"], "session_id": SESSION, "timestamp": TIME,
            "parent_event_id": rows[-2]["uuid"], "prompt_id": PROMPT})
        self.assertEqual(context["anchor_event_id"], rows[0]["uuid"])
        self.assertEqual(rows, before)

    def test_unanchored_literal_and_new_prompt_quote_stay_user_authored(self):
        marker = lineage()[-1]
        self.assertIsNone(self.classify([marker])[0][-1])
        rows = lineage()
        rows[-1]["promptId"] = "a13a89e4-1234-4234-8234-123456789abc"
        self.assertIsNone(self.classify(rows)[0][-1])
        for content in (MARKER, [{"type": "text", "text": "Quote: " + MARKER}],
                        [{"type": "text", "text": MARKER}, {"type": "text", "text": "my comment"}]):
            rows = lineage()
            rows[-1]["message"]["content"] = content
            self.assertIsNone(self.classify(rows)[0][-1])

    def test_missing_broken_or_cross_session_lineage_does_not_guess(self):
        for change in ({"parentUuid": None}, {"promptId": None}, {"timestamp": "now"},
                       {"sessionId": "a13a89e4-1234-4234-8234-123456789abc"},
                       {"isSidechain": True}, {"uuid": "bogus"}):
            rows = lineage()
            rows[-1].update(change)
            with self.subTest(change=change):
                self.assertIsNone(self.classify(rows)[0][-1])
        rows = lineage()
        rows[2]["parentUuid"] = None
        self.assertIsNone(self.classify(rows)[0][-1])

    def test_queue_bookkeeping_and_sidechains_do_not_steal_mainline_anchor(self):
        rows = lineage()
        rows.insert(3, {"type": "queue-operation", "operation": "dequeue"})
        rows.insert(4, raw(42, isSidechain=True))
        self.assertIsNotNone(self.classify(rows)[0][-1])

    def test_explicit_provider_metadata_still_requires_valid_identity_and_time(self):
        marker = raw(2, text=MARKER, isMeta=True)
        self.assertIsNotNone(self.classify([marker])[0][-1])
        for key, value in (("timestamp", "2026-09-09T20:16:54"), ("sessionId", "bad"),
                           ("timestamp", "2026-09-09T20:16:54+24:00")):
            self.assertIsNone(self.classify([{**marker, key: value}])[0][-1])

    def test_tool_use_variant_has_same_strict_classification(self):
        rows = lineage()
        rows[-1]["message"]["content"][0]["text"] = "[Request interrupted by user for tool use]"
        self.assertEqual(self.classify(rows)[0][-1]["cause"], "unknown")

    def test_context_carries_across_cursor_without_content_or_extra_fields(self):
        rows = lineage()
        _, context = self.classify(rows[:-1])
        origin = self.classify(rows[-1:], context)[0][-1]
        self.assertIsNotNone(origin)
        restored = normalize_claude_interruption_context({**context, "text": "private", "grants": ["private"]}, SESSION)
        self.assertEqual(restored, context)
        self.assertEqual(normalize_claude_interruption_context(context, "another-session"), {"version": 1})
        exported = ClaudeInterruptionTracker(context).export_context()
        exported["last_event_id"] = "changed"
        self.assertEqual(context["last_event_id"], rows[-2]["uuid"])

    def test_unknown_records_with_message_uuid_break_lineage_conservatively(self):
        rows = lineage()
        rows.insert(-1, raw(77, "assistant", parentUuid=None))
        self.assertIsNone(self.classify(rows)[0][-1])

    def test_malformed_record_gap_resets_proof_without_aborting_import(self):
        for malformed in (None, [], 42, {"type": []}, {"type": {}}):
            rows = lineage()
            rows.insert(-1, malformed)
            with self.subTest(malformed=malformed):
                self.assertIsNone(self.classify(rows)[0][-1])
