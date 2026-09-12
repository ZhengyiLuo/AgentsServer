"""Synthetic local files only; no server imports or provider activity."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import interactive_chat_projection as projection
from public_chat_transcript import make_public_event_projector, read_public_transcript, PublicTranscriptError


def projector():
    return make_public_event_projector(
        "chat", event_is_visible=lambda event: not event.get("private"),
        event_files_belong=lambda event, session: True,
        project_provider_event=lambda event, session: event,
        strip_user_context=lambda text, **kwargs: text,
        fork_internal_purposes=frozenset({"fork_context"}),
    )


class InteractiveChatProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "events.jsonl"

    def append(self, *events):
        with self.path.open("ab") as stream:
            for event in events:
                stream.write((json.dumps({"session_id": "chat", "run_id": "run", **event}) + "\n").encode())

    def test_matches_public_text_projection_and_keeps_result_dedup_across_loads(self):
        self.append({"type": "turn_started", "prompt": "Question", "ts": "2026-09-12T00:00:00Z"},
                    {"type": "reasoning_summary", "text": "Private summary"},
                    {"type": "reasoning_summary", "phase": "commentary", "text": "Public progress"},
                    {"type": "tool_finished", "output": "Private result"},
                    {"type": "assistant_text", "text": "First answer."})
        reader = projection.IncrementalChatTranscript(self.path, projector())
        first = reader.load()
        self.assertEqual(first["messages"], read_public_transcript(self.path, projector())["messages"])
        first["messages"][0]["text"] = "Caller mutation"
        self.append({"type": "assistant_text", "text": "Second answer."},
                    {"type": "turn_finished", "result_text": "First answer. Second answer."},
                    {"type": "turn_started", "run_id": "private", "purpose": "handoff_digest", "prompt": "Private input"},
                    {"type": "assistant_text", "run_id": "private", "text": "Private answer"},
                    {"type": "turn_started", "run_id": "next", "prompt": "A genuine <turn_aborted> quotation"})
        result = reader.load()
        self.assertEqual(result["messages"], read_public_transcript(self.path, projector())["messages"])
        self.assertEqual([row["text"] for row in result["messages"]],
                         ["Question", "Public progress", "First answer.", "Second answer.", "A genuine <turn_aborted> quotation"])
        self.assertGreater(int(result["revision"]), int(first["revision"]))

    def test_empty_and_tool_only_appends_do_not_change_visible_revision_or_reproject_old_rows(self):
        seen = []
        actual = projector()
        reader = projection.IncrementalChatTranscript(self.path, lambda event: seen.append(event) or actual(event))
        self.assertEqual(reader.load(), {"messages": [], "through_bytes": 0, "revision": "0"})
        self.append({"type": "turn_started", "prompt": "Question"})
        first = reader.load()
        self.append({"type": "tool_started", "tool": {"input": "private"}},
                    {"type": "file_uploaded", "file": {"path": "/not-public"}},
                    {"type": "reasoning_summary", "text": "Not public"})
        second = reader.load()
        self.assertEqual(second["messages"], first["messages"])
        self.assertEqual(second["revision"], first["revision"])
        self.assertGreater(second["through_bytes"], first["through_bytes"])
        self.assertEqual(len(seen), 1)
        with patch.object(projection.json, "loads", side_effect=AssertionError("unchanged source must not be parsed")):
            self.assertEqual(reader.load(), second)

    def test_partial_tail_waits_for_newline_and_only_reads_from_committed_offset(self):
        self.append({"type": "turn_started", "prompt": "Question"})
        reader = projection.IncrementalChatTranscript(self.path, projector())
        first = reader.load()
        body = json.dumps({"type": "assistant_text", "run_id": "run", "text": "Answer"}).encode()
        with self.path.open("ab") as stream:
            stream.write(body)
        self.assertEqual(reader.load(), first)
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        offsets = []
        original = os.fdopen
        class ObservedStream:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): return self.stream.__exit__(*args)
            def __getattr__(self, name): return getattr(self.stream, name)
            def readline(self, size):
                offsets.append(self.stream.tell())
                return self.stream.readline(size)
        with patch.object(projection.os, "fdopen", side_effect=lambda *args: ObservedStream(original(*args))):
            result = reader.load()
        self.assertEqual(offsets, [first["through_bytes"]])
        self.assertEqual(result["messages"][-1]["text"], "Answer")

    def test_changed_logs_fail_closed_and_require_new_projector(self):
        for change in ("replace", "truncate", "rewrite", "symlink"):
            with self.subTest(change=change):
                self.path.unlink(missing_ok=True)
                self.append({"type": "turn_started", "prompt": "Question"})
                reader = projection.IncrementalChatTranscript(self.path, projector())
                reader.load()
                if change == "replace":
                    replacement = self.path.with_suffix(".new")
                    replacement.write_bytes(self.path.read_bytes()); replacement.replace(self.path)
                elif change == "truncate":
                    self.path.write_bytes(b"")
                elif change == "rewrite":
                    self.path.write_bytes(self.path.read_bytes().replace(b"Question", b"Modified"))
                else:
                    target = self.path.with_suffix(".target")
                    self.path.replace(target); self.path.symlink_to(target)
                with self.assertRaises(PublicTranscriptError): reader.load()
                with self.assertRaises(PublicTranscriptError): reader.load()

    def test_limits_invalid_rows_and_unicode_are_explicit_errors(self):
        cases = [
            ("MAX_LOG_BYTES", 1, {"type": "turn_started", "prompt": "Text"}),
            ("MAX_LINE_BYTES", 1, {"type": "turn_started", "prompt": "Text"}),
            ("MAX_RECORDS", 0, {"type": "tool_started"}),
            ("MAX_MESSAGES", 0, {"type": "turn_started", "prompt": "Text"}),
            ("MAX_MESSAGE_BYTES", 1, {"type": "turn_started", "prompt": "Text"}),
            ("MAX_TEXT_BYTES", 1, {"type": "turn_started", "prompt": "Text"}),
        ]
        for name, limit, event in cases:
            with self.subTest(limit=name):
                self.path.unlink(missing_ok=True); self.append(event)
                with patch.object(projection, name, limit), self.assertRaises(PublicTranscriptError):
                    projection.IncrementalChatTranscript(self.path, projector()).load()
        for body in (b"not-json\n", b"[]\n", b'{}\n', b'{"type":"turn_started","prompt":"\\ud800"}\n'):
            self.path.write_bytes(body)
            with self.assertRaises(PublicTranscriptError):
                projection.IncrementalChatTranscript(self.path, projector()).load()


if __name__ == "__main__":
    unittest.main()
