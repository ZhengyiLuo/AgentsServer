"""Synthetic native DTOs only; no server import, provider, or filesystem reads."""
from copy import deepcopy
import math
import unittest

from interactive_chat_native import shared_events, shared_native_value, shared_session


class InteractiveChatNativeTests(unittest.TestCase):
    def test_native_message_activity_and_runtime_marker_keep_exact_chronology(self):
        common = {"session_id": "chat-one", "run_id": "native-run", "backend": "codex"}
        rows = [
            {**common, "id": "user-10", "seq": 10, "ts": "2026-09-12T00:00:00.123Z", "type": "turn_started", "prompt": "My genuine quote contains token, authority, and /example/path."},
            {**common, "id": "progress-11", "seq": 11, "ts": "2026-09-12T00:00:01.456Z", "type": "reasoning_summary", "phase": "commentary", "text": "Public progress"},
            {**common, "id": "tool-12", "seq": 12, "ts": "2026-09-12T00:00:02.789Z", "type": "tool_started", "tool": {"id": "tool-one", "name": "synthetic", "input": {"query": "A deliberate user query"}}},
            {**common, "id": "output-13", "seq": 13, "ts": "2026-09-12T00:00:03.000Z", "type": "tool_finished", "tool_id": "tool-one", "output": "Output already in the chat."},
            {**common, "id": "answer-14", "seq": 14, "ts": "2026-09-12T00:00:04.000Z", "type": "assistant_text", "text": "Final answer"},
            {**common, "id": "import-15", "seq": 15, "ts": "2026-09-12T00:00:02.789Z", "type": "turn_started", "run_id": "import-one", "imported": True, "metadata_only": True, "prompt": "", "provider_runtime_context": "turn_aborted", "provider_origin": {"provider": "codex", "kind": "turn_aborted", "session_id": "provider-thread", "event_id": "provider-item", "turn_id": "provider-turn", "timestamp": "2026-09-12T00:00:02.789Z", "source_text_sha256": "a" * 64}},
        ]
        self.assertEqual(shared_events(rows, "chat-one"), rows)

    def test_known_private_transport_file_aliases_are_removed_recursively_without_mutation(self):
        private = {key: "synthetic-private-value" for key in (
            "cwd", "path", "source_path", "root", "file", "files", "artifact", "artifacts",
            "file_ids", "display_file_ids", "file_path", "notebook_path", "old_path", "new_path",
            "attachments", "env", "environment", "runtime_env", "runtime_context",
            "authority", "authority_file", "authority_path", "authorization", "headers",
            "token", "access_token", "refresh_token", "api_key", "password", "secret",
            "request_prompt", "provider_cross_chat_route_snapshot", "secure_peer_route_snapshots",
            "chat_references", "team_references", "_history_sync_checkpoint")}
        original = {"id": "row", "prompt": "Keep literal API_KEY and /example/path as authored text.",
                    "payload": [{**private, "TOKEN": "uppercase-private", "text": "Keep this text"}], **private}
        before = deepcopy(original)
        result = shared_native_value(original)
        self.assertEqual(result, {"id": "row", "prompt": original["prompt"], "payload": [{"text": "Keep this text"}]})
        self.assertEqual(original, before)

    def test_file_workspace_terminal_events_are_omitted_not_plaintext_mentions(self):
        rows = [{"session_id": "chat-one", "type": kind, "id": kind} for kind in
                ("file_uploaded", "artifact_published", "workspace_changed", "terminal_output")]
        rows.append({"session_id": "chat-one", "type": "assistant_text", "text": "The file_uploaded event is a quoted example."})
        self.assertEqual(shared_events(rows, "chat-one"), rows[-1:])

    def test_foreign_chat_owner_rejected_before_any_partial_result_is_returned(self):
        for row in ({"type": "assistant_text", "session_id": "another-chat", "text": "Not shared"},
                    {"type": "file_uploaded", "session_id": "another-chat"}, None,
                    {"type": "assistant_text", "session_id": {"id": "chat-one"}}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                shared_events([{"type": "assistant_text", "session_id": "chat-one", "text": "Allowed"}, row], "chat-one")
        self.assertEqual(shared_events([{"type": "job_summary", "job_id": "owned-job"}], "chat-one"),
                         [{"type": "job_summary", "job_id": "owned-job"}])

    def test_session_allowlist_preserves_controls_but_not_owner_files_or_provider_identity(self):
        source = {"id": "chat-one", "title": "Shared", "backend": "codex", "system_prompt": "Authorized instruction",
                  "codex_goal": {"objective": "Goal", "status": "active", "token": "private"},
                  "latest_event_seq": 10, "cwd": "/private/workspace", "codex_thread_id": "native-provider-id",
                  "unknown_server_setting": True, "sessions": [{"id": "other-chat"}]}
        self.assertEqual(shared_session(source), {"id": "chat-one", "title": "Shared", "backend": "codex",
            "system_prompt": "Authorized instruction", "codex_goal": {"objective": "Goal", "status": "active"}, "latest_event_seq": 10})

    def test_non_json_numbers_objects_and_excessive_depth_fail_closed(self):
        for value in (math.nan, math.inf, -math.inf, object()):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                shared_native_value({"metric": value})
        nested = {}
        for _ in range(22): nested = {"child": nested}
        with self.assertRaises(ValueError): shared_native_value(nested)


if __name__ == "__main__":
    unittest.main()
