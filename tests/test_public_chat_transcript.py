"""Pure bounded transcript projection tests; never import the live server."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import public_chat_transcript as transcript


def expected_digest(content, messages):
    projected = json.dumps(messages, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(b"agentsdock-public-chat-preview-v2\x00" + hashlib.sha256(content).digest() + b"\x00" + hashlib.sha256(projected).digest()).hexdigest()


class PublicChatTranscriptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "events.jsonl"

    def write_events(self, *events):
        content = b"".join(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events)
        self.path.write_bytes(content)
        return content

    def read(self, **kwargs):
        return transcript.read_public_transcript(self.path, lambda event: event, **kwargs)

    def test_prefix_snapshot_and_digest_do_not_include_later_messages(self):
        content = self.write_events({"type": "turn_started", "run_id": "one", "prompt": "Reviewed prompt"})
        preview = self.read()
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"assistant_text","run_id":"one","text":"Not reviewed"}\n')
        snapshot = self.read(through_bytes=preview["through_bytes"])
        self.assertEqual(snapshot, preview)
        self.assertEqual(preview["digest"], expected_digest(content, preview["messages"]))
        self.assertEqual(preview["through_bytes"], len(content))
        self.assertEqual(len(self.read()["messages"]), 2)

    def test_digest_binds_excluded_source_bytes_too(self):
        self.write_events({"type": "turn_started", "prompt": "Public"}, {"type": "tool_finished", "text": "private-a"})
        preview = self.read()
        self.path.write_bytes(self.path.read_bytes().replace(b"private-a", b"private-b"))
        changed = self.read(through_bytes=preview["through_bytes"])
        self.assertEqual(changed["messages"], preview["messages"])
        self.assertNotEqual(changed["digest"], preview["digest"])

    def test_unfinished_final_record_is_not_in_preview_or_boundary(self):
        content = self.write_events({"type": "turn_started", "prompt": "Complete"})
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"assistant_text","text":"unfinished')
        self.assertEqual(self.read()["through_bytes"], len(content))
        with self.assertRaisesRegex(transcript.PublicTranscriptError, "preview it again"):
            self.read(through_bytes=self.path.stat().st_size)

    def test_codex_commentary_is_public_but_hidden_reasoning_and_tools_are_not_projected(self):
        self.write_events(
            {"type": "turn_started", "run_id": "codex", "prompt": "Question", "ts": 1},
            {"type": "reasoning_summary", "run_id": "codex", "phase": "analysis", "text": "Hidden reasoning"},
            {"type": "reasoning_summary", "run_id": "codex", "text": "Hidden unspecified reasoning"},
            {"type": "reasoning_summary", "run_id": "codex", "phase": "commentary", "text": "Visible progress", "ts": 2},
            {"type": "tool_finished", "text": "Private tool result"},
            {"type": "system", "text": "Private instructions"},
            {"type": "artifact_created", "path": "/private/file"},
            {"type": "assistant_text", "run_id": "codex", "text": "Answer", "ts": 3},
            {"type": "turn_finished", "run_id": "codex", "result_text": "Answer"},
        )
        projector = mock.Mock(side_effect=lambda event: event)
        result = transcript.read_public_transcript(self.path, projector)
        self.assertEqual([message["text"] for message in result["messages"]], ["Question", "Visible progress", "Answer"])
        self.assertEqual([call.args[0]["type"] for call in projector.call_args_list],
                         ["turn_started", "reasoning_summary", "artifact_created", "assistant_text", "turn_finished"])
        self.assertEqual(result["messages"][1], {"role": "assistant", "text": "Visible progress", "timestamp": 2})

    def test_only_projected_user_attachments_and_committed_artifacts_publish_videos(self):
        video = {"id": "video_ZmlsZQ." + "a" * 64, "filename": "demo.mp4",
                 "content_type": "video/mp4", "size": 10}
        content = self.write_events(
            {"type": "turn_started", "prompt": "", "shared_videos": [video], "ts": 1},
            {"type": "file_uploaded", "shared_videos": [video]},
            {"type": "tool_finished", "shared_videos": [video]},
            {"type": "artifact_created", "artifact": {"path": "/private/source.mp4"},
             "text": "Private artifact metadata", "shared_videos": [video], "ts": 2},
            {"type": "assistant_text", "run_id": "one", "text": "Published", "shared_videos": [video]},
            {"type": "turn_finished", "run_id": "one", "result_text": "Published", "shared_videos": [video]},
        )
        result = self.read()
        self.assertEqual(result["messages"], [
            {"role": "user", "text": "", "timestamp": 1, "videos": [video]},
            {"role": "assistant", "text": "", "timestamp": 2, "videos": [video]},
            {"role": "assistant", "text": "Published"},
        ])
        self.assertEqual(result["digest"], expected_digest(content, result["messages"]))
        self.assertNotIn("private", json.dumps(result["messages"]).lower())
        streamed = []
        reread = self.read(message_sink=streamed.append)
        self.assertEqual(streamed, result["messages"])
        self.assertEqual(reread["digest"], result["digest"])

    def test_video_projection_change_invalidates_preview_and_later_artifacts_are_excluded(self):
        video = {"id": "video_ZmlsZQ." + "a" * 64, "filename": "demo.mp4",
                 "content_type": "video/mp4", "size": 10}
        self.write_events({"type": "artifact_created", "shared_videos": [video]})
        first = self.read()
        changed = transcript.read_public_transcript(self.path,
            lambda event: {**event, "shared_videos": [{**video, "id": "video_ZmlsZQ." + "b" * 64}]})
        self.assertNotEqual(first["digest"], changed["digest"])
        with self.path.open("ab") as stream:
            stream.write(json.dumps({"type": "artifact_created", "shared_videos": [video]}).encode() + b"\n")
        self.assertEqual(self.read(through_bytes=first["through_bytes"]), first)
        self.assertEqual(len(self.read()["messages"]), 2)

    def test_invalid_video_metadata_fails_explicitly_instead_of_partial_snapshot(self):
        self.write_events({"type": "turn_started", "prompt": "Public"},
                          {"type": "artifact_created", "shared_videos": [{"id": "/private/file.mp4"}]})
        with self.assertRaisesRegex(transcript.PublicTranscriptError, "video metadata"):
            self.read()

    def test_claude_full_text_and_aggregate_result_are_not_duplicated(self):
        first = "Full first paragraph.\n\nUnicode 中文 remains literal."
        second = "Full second paragraph with **Markdown** and `code`."
        self.write_events(
            {"type": "turn_started", "run_id": "claude", "prompt": "Please answer"},
            {"type": "assistant_text", "run_id": "claude", "text": first},
            {"type": "assistant_text", "run_id": "claude", "text": second},
            {"type": "turn_finished", "run_id": "claude", "result_text": first + "\n\n" + second},
        )
        self.assertEqual([message["text"] for message in self.read()["messages"]], ["Please answer", first, second])

    def test_result_only_and_distinct_results_are_kept_and_run_dedup_is_scoped(self):
        self.write_events(
            {"type": "turn_started", "run_id": "one", "prompt": "One"},
            {"type": "assistant_text", "run_id": "one", "text": "Repeated"},
            {"type": "turn_finished", "run_id": "one", "result_text": "Different final answer"},
            {"type": "turn_started", "run_id": "two", "prompt": "Two"},
            {"type": "turn_finished", "run_id": "two", "result_text": "Repeated"},
        )
        self.assertEqual([message["text"] for message in self.read()["messages"]],
                         ["One", "Repeated", "Different final answer", "Two", "Repeated"])

    def test_projection_controls_visible_text_and_metadata_never_spills(self):
        self.write_events(
            {"type": "turn_started", "prompt": "Internal wrapped prompt", "ts": True, "secret": "private"},
            {"type": "assistant_text", "text": "Suppressed"},
            {"type": "turn_finished", "result_text": "Visible result", "ts": float("inf")},
        )
        def project(event):
            if event["type"] == "assistant_text":
                return None
            return {**event, **({"prompt": "Original user prompt"} if event["type"] == "turn_started" else {})}
        result = transcript.read_public_transcript(self.path, project)
        self.assertEqual(result["messages"], [
            {"role": "user", "text": "Original user prompt"},
            {"role": "assistant", "text": "Visible result"},
        ])

    def test_malformed_complete_records_fail_instead_of_publishing_partial_text(self):
        for malformed in (b"not-json\n", b"[]\n", b"null\n", b"\xff\n"):
            with self.subTest(record=malformed):
                content = self.write_events({"type": "turn_started", "prompt": "Valid first message"})
                self.path.write_bytes(content + malformed)
                with self.assertRaises(transcript.PublicTranscriptError):
                    self.read()

    def test_invalid_unicode_text_fails_with_a_transcript_error(self):
        self.path.write_bytes(b'{"type":"turn_started","prompt":"\\ud800"}\n')
        with self.assertRaises(transcript.PublicTranscriptError):
            self.read()

    def test_excessively_nested_record_fails_with_a_transcript_error(self):
        self.path.write_bytes(b'{"type":"turn_started","prompt":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}\n')
        with self.assertRaises(transcript.PublicTranscriptError):
            self.read()

    def test_record_message_output_and_work_limits_fail_explicitly(self):
        self.write_events({"type": "turn_started", "prompt": "中文"}, {"type": "assistant_text", "text": "answer"})
        for constant, limit in (("MAX_LINE_BYTES", 8), ("MAX_SCAN_SECONDS", 0),
                                ("MAX_TEXT_BYTES", 6), ("MAX_MESSAGE_BYTES", 5)):
            with self.subTest(limit=constant), mock.patch.object(transcript, constant, limit):
                with self.assertRaises(transcript.PublicTranscriptError):
                    self.read()

    def test_invalid_boundaries_and_truncated_prefixes_are_rejected(self):
        content = self.write_events({"type": "turn_started", "prompt": "Public"})
        for boundary in (True, 0, -1, 1.5, "1", transcript.MAX_SNAPSHOT_BOUNDARY + 1, len(content) + 1):
            with self.subTest(boundary=boundary), self.assertRaises(transcript.PublicTranscriptError):
                self.read(through_bytes=boundary)

    def test_large_raw_noise_and_many_readable_messages_keep_the_complete_prefix(self):
        expected = [{"role": "user" if index % 2 == 0 else "assistant",
                     "text": f"Synthetic message {index}: " + "readable text " * 20} for index in range(2504)]
        noise = json.dumps({"type": "tool_finished", "text": "x" * (128 * 1024)}).encode() + b"\n"
        with self.path.open("wb") as stream:
            for index, message in enumerate(expected):
                if index == 1252:
                    for _ in range(513):
                        stream.write(noise)
                field = "prompt" if message["role"] == "user" else "text"
                kind = "turn_started" if message["role"] == "user" else "assistant_text"
                stream.write(json.dumps({"type": kind, "run_id": str(index // 2), field: message["text"]}).encode() + b"\n")
        boundary = self.path.stat().st_size
        self.assertGreater(boundary, 64 * 1024 * 1024)
        preview = self.read()
        self.assertEqual(preview["messages"], expected)
        self.assertEqual(preview["through_bytes"], boundary)
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"assistant_text","text":"Not part of the reviewed prefix"}\n')
        self.assertEqual(self.read(through_bytes=boundary), preview)

    def test_actual_json_escaping_and_metadata_count_toward_output_budget(self):
        self.write_events({"type": "turn_started", "prompt": "\x01" * 20})
        with mock.patch.object(transcript, "MAX_TEXT_BYTES", 100):
            with self.assertRaisesRegex(transcript.PublicTranscriptError, "2 MiB"):
                self.read()

    def test_streamed_share_has_no_whole_chat_two_mib_limit(self):
        count = 24
        content = self.write_events(*[
            {"type": "assistant_text", "run_id": str(index), "text": f"Message {index}: " + "x" * (180 * 1024)}
            for index in range(count)
        ])
        seen = []
        result = self.read(message_sink=lambda message: seen.append((message["role"], message["text"][:20])))
        self.assertGreater(len(content), 4 * 1024 * 1024)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["message_count"], count)
        self.assertEqual(result["through_bytes"], len(content))
        self.assertEqual(len(seen), count)
        self.assertTrue(seen[0][1].startswith("Message 0:"))
        self.assertTrue(seen[-1][1].startswith("Message 23:"))

    def test_stream_and_preview_prove_the_same_exact_messages_and_boundary(self):
        self.write_events({"type": "turn_started", "run_id": "one", "prompt": "Exact 中文\n text", "ts": 1},
                          {"type": "assistant_text", "run_id": "one", "text": "First"},
                          {"type": "assistant_text", "run_id": "one", "text": "Second"},
                          {"type": "turn_finished", "run_id": "one", "result_text": "First Second"})
        preview = self.read()
        streamed = []
        result = self.read(through_bytes=preview["through_bytes"], message_sink=streamed.append)
        self.assertEqual(streamed, preview["messages"])
        self.assertEqual(result["digest"], preview["digest"])
        self.assertEqual(result["through_bytes"], preview["through_bytes"])
        self.assertEqual(result["message_count"], 3)

    def test_empty_or_private_only_chat_is_not_publishable(self):
        for events in ([], [{"type": "tool_finished", "text": "Private"}], [{"type": "assistant_text", "text": "  "}]):
            with self.subTest(events=events):
                self.write_events(*events)
                with self.assertRaisesRegex(transcript.PublicTranscriptError, "no shareable"):
                    self.read()

    def test_missing_and_linked_logs_are_unavailable(self):
        with self.assertRaisesRegex(transcript.PublicTranscriptError, "unavailable"):
            self.read()
        target = self.root / "other.jsonl"
        target.write_text('{"type":"turn_started","prompt":"Other chat"}\n')
        self.path.symlink_to(target)
        with self.assertRaisesRegex(transcript.PublicTranscriptError, "unavailable"):
            self.read()

    def test_append_during_projection_keeps_the_initial_prefix(self):
        content = self.write_events({"type": "turn_started", "prompt": "Reviewed"})
        def append(event):
            with self.path.open("ab") as stream:
                stream.write(b'{"type":"assistant_text","text":"Later"}\n')
            return event
        result = transcript.read_public_transcript(self.path, append)
        self.assertEqual(result["messages"], [{"role": "user", "text": "Reviewed"}])
        self.assertEqual(result["through_bytes"], len(content))
        self.assertEqual(result["digest"], expected_digest(content, result["messages"]))

    def test_truncation_during_projection_is_detected(self):
        self.write_events({"type": "turn_started", "prompt": "Reviewed"})
        def truncate(event):
            self.path.write_bytes(b"")
            return event
        with self.assertRaisesRegex(transcript.PublicTranscriptError, "preview it again"):
            transcript.read_public_transcript(self.path, truncate)

    def test_nonstring_event_type_fails_with_a_transcript_error(self):
        for kind in (None, [], {}, 42):
            with self.subTest(kind=kind):
                self.write_events({"type": kind, "prompt": "Public"})
                with self.assertRaises(transcript.PublicTranscriptError):
                    self.read()

    def test_recursive_decoder_error_is_normalized(self):
        self.write_events({"type": "turn_started", "prompt": "Public"})
        with mock.patch.object(transcript.json, "loads", side_effect=RecursionError):
            with self.assertRaises(transcript.PublicTranscriptError):
                self.read()

    def test_plaintext_whitespace_is_preserved_and_invalid_timestamps_omitted(self):
        for timestamp in (-1, 253402300800, 10 ** 500):
            with self.subTest(timestamp=timestamp):
                self.write_events({"type": "turn_started", "prompt": "  indented\n\n", "ts": timestamp})
                self.assertEqual(self.read()["messages"], [{"role": "user", "text": "  indented\n\n"}])

    def test_projector_hides_internal_runs_and_imported_delivery_segments_only(self):
        envelope = (
            "[AgentsDock delivery kind=status leg=0/2 origin=user from=Peer]\n"
            "[Source user instruction — verbatim, user-authored]\nAsk the peer\n[End source user instruction]\n"
            "[Server-generated exchange status]\ninternal status\n[End server-generated exchange status]\n"
            "reply: none (terminal status notice; do not respond to the exchange)\n[End delivery]"
        )
        self.write_events(
            {"type": "turn_started", "session_id": "chat", "run_id": "digest", "purpose": "handoff_digest", "prompt": "Private digest prompt"},
            {"type": "assistant_text", "run_id": "digest", "text": "Private digest result"},
            {"type": "turn_started", "run_id": "status", "cross_chat_exchange_status": True, "prompt": "Private status prompt"},
            {"type": "assistant_text", "run_id": "status", "text": "Private status result"},
            {"type": "turn_started", "run_id": "import_one", "backend": "claude", "imported": True, "prompt": envelope},
            {"type": "assistant_text", "run_id": "import_one", "text": "Private imported status response"},
            {"type": "turn_started", "run_id": "import_one", "backend": "claude", "imported": True, "prompt": "Next real imported user"},
            {"type": "assistant_text", "run_id": "import_one", "text": "Real imported answer"},
            {"type": "turn_started", "run_id": "native", "prompt": envelope},
            {"type": "assistant_text", "run_id": "native", "text": "Explanation of user's quoted envelope"},
        )
        strip = mock.Mock(side_effect=lambda text, **kwargs: text)
        projector = transcript.make_public_event_projector(
            "chat", event_is_visible=lambda event: True, event_files_belong=lambda event, session: True,
            project_provider_event=lambda event, session: event, strip_user_context=strip,
            fork_internal_purposes={"handoff_digest"},
        )
        result = transcript.read_public_transcript(self.path, projector)
        self.assertEqual([message["text"] for message in result["messages"]], [
            "Next real imported user", "Real imported answer", envelope, "Explanation of user's quoted envelope",
        ])
        self.assertTrue(any(call.kwargs["provider_history"] is True for call in strip.call_args_list))

    def test_projector_preserves_incomplete_imported_delivery_quotes(self):
        for prompt in (
            "A quote: [AgentsDock delivery kind=status leg=0/2 origin=user]\nbody\n[End delivery]",
            "[AgentsDock delivery kind=status leg=0/2 origin=user]\nno closing marker",
            "[AgentsDock delivery kind=status leg=4/2 origin=user]\ninvalid leg\n[End delivery]",
            "[AgentsDock delivery kind=status leg=0/2 origin=user]\nquoted outer markers only\n[End delivery]",
        ):
            with self.subTest(prompt=prompt):
                projector = transcript.make_public_event_projector(
                    "chat", event_is_visible=lambda event: True, event_files_belong=lambda event, session: True,
                    project_provider_event=lambda event, session: event, strip_user_context=lambda text, **kwargs: text,
                    fork_internal_purposes=set(),
                )
                event = {"type": "turn_started", "run_id": "import_x", "imported": True, "backend": "claude", "prompt": prompt}
                self.assertEqual(projector(event)["prompt"], prompt)

    def test_projector_rejects_other_session_ownership_and_invalid_run_identity(self):
        projector = transcript.make_public_event_projector(
            "chat", event_is_visible=lambda event: True, event_files_belong=lambda event, session: True,
            project_provider_event=lambda event, session: event, strip_user_context=lambda text, **kwargs: text,
            fork_internal_purposes=set(),
        )
        for additional in ({"session_id": "other"}, {"session_id": {}}, {"run_id": [1]}):
            with self.subTest(additional=additional), self.assertRaises(transcript.PublicTranscriptError):
                projector({"type": "turn_started", "prompt": "private", **additional})

    def test_confirmation_digest_binds_projected_text_even_when_source_is_unchanged(self):
        self.write_events({"type": "turn_started", "prompt": "Original private wrapper"})
        preview = transcript.read_public_transcript(self.path, lambda event: {**event, "prompt": "Reviewed text"})
        changed = transcript.read_public_transcript(self.path, lambda event: {**event, "prompt": "New unreviewed text"}, through_bytes=preview["through_bytes"])
        self.assertEqual(preview["through_bytes"], changed["through_bytes"])
        self.assertNotEqual(preview["digest"], changed["digest"])

    def test_actual_aware_iso_event_timestamps_are_preserved_as_unix_seconds(self):
        expected = datetime(2026, 9, 9, 10, 11, 12, 345000, tzinfo=timezone.utc).timestamp()
        for timestamp in ("2026-09-09T10:11:12.345Z", "2026-09-09T12:11:12.345+02:00"):
            with self.subTest(timestamp=timestamp):
                self.write_events({"type": "turn_started", "prompt": "Actual durable timestamp", "ts": timestamp})
                self.assertEqual(self.read()["messages"][0]["timestamp"], expected)
        for timestamp in ("2026-09-09T10:11:12", "2026-09-09", "invalid", "0" * 65):
            with self.subTest(timestamp=timestamp):
                self.write_events({"type": "turn_started", "prompt": "Invalid timestamp omitted", "ts": timestamp})
                self.assertNotIn("timestamp", self.read()["messages"][0])


if __name__ == "__main__":
    unittest.main()
