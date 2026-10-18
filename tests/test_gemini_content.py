from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import HTTPException
from gemini_acp_client import GeminiProtocolError
from gemini_content import attachments, plan_entries, usage, tool_output
from gemini_history import replay_text


class Content(unittest.TestCase):
    def test_native_tool_text_and_diff_are_both_retained(self):
        diff = {"type": "diff", "path": "sample.txt", "oldText": "old", "newText": "new"}
        result = tool_output([{"type": "content", "content": {"type": "text", "text": "Done"}}, diff])
        self.assertTrue(result.startswith("Done\n"))
        self.assertIn('"oldText": "old"', result)
        self.assertIn('"newText": "new"', result)
    def test_usage_is_provider_reported_not_guessed(self):
        self.assertEqual(usage({"inputTokens": 12, "outputTokens": 3, "thoughtTokens": 2}),
                         {"input_tokens": 12, "output_tokens": 3, "reasoning_tokens": 2})
        self.assertEqual(usage(None), {})
        for value in (-1, True, "2", 2**54):
            with self.assertRaises(GeminiProtocolError): usage({"inputTokens": value})

    def test_plans_are_bounded_native_entries(self):
        rows = [{"content": "Read files", "status": "in_progress", "priority": "high"}]
        self.assertEqual(plan_entries(rows), rows)
        for value in ({}, rows * 129, [{**rows[0], "status": "invented"}], [{**rows[0], "content": "a" * 2049}]):
            with self.assertRaises(GeminiProtocolError): plan_entries(value)

    def test_registered_text_and_media_are_embedded_no_arbitrary_file_links(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文 空格.txt"; path.write_text("Attached text")
            record = {"content_type": "text/plain"}
            api = SimpleNamespace(validate_session_file_ids=lambda sid, ids: ids,
                                  load_file_meta=lambda fid: record, fork_file_source_path=lambda fid, rec: path)
            self.assertEqual(attachments(api, "chat", ["file_test"])[0]["type"], "text")
            self.assertIn("Attached text", attachments(api, "chat", ["file_test"])[0]["text"])
            record["content_type"] = "image/png"; path.write_bytes(b"\x89PNG\r\n\x1a\n")
            media = attachments(api, "chat", ["file_test"])[0]
            self.assertEqual(media["type"], "image")
            self.assertEqual(replay_text({"inlineData": {"mimeType": media["mimeType"], "data": media["data"]}}), "[Image: image/png, 0.0 KB]")
            record["content_type"] = "application/octet-stream"
            with self.assertRaises(HTTPException): attachments(api, "chat", ["file_test"])
            path.write_bytes(b"a" * (512 * 1024 + 1))
            with self.assertRaises(HTTPException): attachments(api, "chat", ["file_test"])
            path.unlink(); path.symlink_to(Path(directory) / "absent")
            with self.assertRaises(HTTPException): attachments(api, "chat", ["file_test"])
            api.fork_file_source_path = lambda fid, rec: None
            with self.assertRaises(HTTPException): attachments(api, "chat", ["file_test"])
