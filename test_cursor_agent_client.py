"""Tests for cursor_agent_client.py, using real captured `agent` CLI output.

Every fixture line below was captured verbatim from a real, authenticated
run of the Cursor CLI (agent 2026.08.11-e8db854) against a scratch
directory, not hand-written or inferred from documentation. Session ids and
call ids are real values from that run.
"""

import unittest

from cursor_agent_client import (
    CursorEventParseError,
    normalize_cursor_stream_event,
)

SESSION_ID = "51cc22bb-e694-4f7c-807b-961b5b41810c"


class NormalizeCursorStreamEventTests(unittest.TestCase):
    def test_blank_line_is_ignored(self) -> None:
        self.assertIsNone(normalize_cursor_stream_event("   "))

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(CursorEventParseError):
            normalize_cursor_stream_event("{not json")

    def test_session_init(self) -> None:
        line = (
            '{"type":"system","subtype":"init","apiKeySource":"login",'
            '"cwd":"/private/tmp/cursor-research","session_id":"%s",'
            '"model":"Auto","permissionMode":"default"}' % SESSION_ID
        )
        self.assertEqual(
            normalize_cursor_stream_event(line),
            {
                "kind": "session_started",
                "session_id": SESSION_ID,
                "cwd": "/private/tmp/cursor-research",
                "model": "Auto",
            },
        )

    def test_user_echo_is_dropped(self) -> None:
        line = (
            '{"type":"user","message":{"role":"user","content":'
            '[{"type":"text","text":"Read hello.py"}]},"session_id":"%s"}'
            % SESSION_ID
        )
        self.assertIsNone(normalize_cursor_stream_event(line))

    def test_thinking_delta_and_completed(self) -> None:
        delta = (
            '{"type":"thinking","subtype":"delta","text":"Reading hello.py to ",'
            '"session_id":"%s","timestamp_ms":1787521421085}' % SESSION_ID
        )
        completed = (
            '{"type":"thinking","subtype":"completed","session_id":"%s",'
            '"timestamp_ms":1787521421089}' % SESSION_ID
        )
        self.assertEqual(
            normalize_cursor_stream_event(delta),
            {
                "kind": "reasoning_delta",
                "session_id": SESSION_ID,
                "text": "Reading hello.py to ",
            },
        )
        self.assertEqual(
            normalize_cursor_stream_event(completed),
            {"kind": "reasoning_completed", "session_id": SESSION_ID},
        )

    def test_glob_tool_call_started_and_completed(self) -> None:
        started = (
            '{"type":"tool_call","subtype":"started",'
            '"call_id":"call-glob-0","tool_call":{"globToolCall":'
            '{"args":{"targetDirectory":"/private/tmp/cursor-research",'
            '"globPattern":"**/hello.py"}}},"session_id":"%s"}' % SESSION_ID
        )
        completed = (
            '{"type":"tool_call","subtype":"completed",'
            '"call_id":"call-glob-0","tool_call":{"globToolCall":'
            '{"args":{"targetDirectory":"/private/tmp/cursor-research",'
            '"globPattern":"**/hello.py"},"result":{"success":'
            '{"pattern":"","path":"/private/tmp/cursor-research",'
            '"files":["hello.py"],"totalFiles":1}}}},"session_id":"%s"}'
            % SESSION_ID
        )
        self.assertEqual(
            normalize_cursor_stream_event(started),
            {
                "kind": "tool_started",
                "session_id": SESSION_ID,
                "call_id": "call-glob-0",
                "tool": "glob",
                "args": {
                    "targetDirectory": "/private/tmp/cursor-research",
                    "globPattern": "**/hello.py",
                },
            },
        )
        finished = normalize_cursor_stream_event(completed)
        self.assertEqual(finished["kind"], "tool_finished")
        self.assertEqual(finished["tool"], "glob")
        self.assertEqual(finished["result"]["files"], ["hello.py"])

    def test_edit_tool_call_completed_carries_a_ready_made_diff(self) -> None:
        completed = (
            '{"type":"tool_call","subtype":"completed","call_id":"call-edit-2",'
            '"tool_call":{"editToolCall":{"args":{"path":'
            '"/private/tmp/cursor-research/hello.py",'
            '"streamContent":"# greeting\\nprint(\'hello world\')"},'
            '"result":{"success":{"path":"/private/tmp/cursor-research/hello.py",'
            '"linesAdded":1,"linesRemoved":0,'
            '"diffString":"--- a\\n+++ b\\n@@ -1 +1,2 @@\\n+# greeting\\n print(\'hello world\')",'
            '"beforeFullFileContent":"print(\'hello world\')\\n",'
            '"afterFullFileContent":"# greeting\\nprint(\'hello world\')\\n"}}}},'
            '"session_id":"%s"}' % SESSION_ID
        )
        finished = normalize_cursor_stream_event(completed)
        self.assertEqual(finished["kind"], "tool_finished")
        self.assertEqual(finished["tool"], "edit")
        self.assertEqual(finished["result"]["linesAdded"], 1)
        self.assertIn("diffString", finished["result"])

    def test_shell_tool_call_rejected_is_distinct_from_finished(self) -> None:
        # Real behavior found during testing: --trust alone permits file
        # reads/edits but shell calls come back rejected without --force.
        completed = (
            '{"type":"tool_call","subtype":"completed","call_id":"call-shell-3",'
            '"tool_call":{"shellToolCall":{"result":{"rejected":'
            '{"command":"python3 hello.py","workingDirectory":'
            '"/private/tmp/cursor-research","reason":"","isReadonly":false}}}},'
            '"session_id":"%s"}' % SESSION_ID
        )
        event = normalize_cursor_stream_event(completed)
        self.assertEqual(event["kind"], "tool_rejected")
        self.assertEqual(event["tool"], "shell")

    def test_shell_tool_call_completed_with_force(self) -> None:
        completed = (
            '{"type":"tool_call","subtype":"completed","call_id":"call-shell-7",'
            '"tool_call":{"shellToolCall":{"args":{"command":'
            '"python3 /private/tmp/cursor-research/hello.py"},'
            '"result":{"success":{"command":'
            '"python3 /private/tmp/cursor-research/hello.py",'
            '"exitCode":0,"stdout":"hello world\\n","stderr":""}}}},'
            '"session_id":"%s"}' % SESSION_ID
        )
        event = normalize_cursor_stream_event(completed)
        self.assertEqual(event["kind"], "tool_finished")
        self.assertEqual(event["tool"], "shell")
        self.assertEqual(event["result"]["exitCode"], 0)
        self.assertEqual(event["result"]["stdout"], "hello world\n")

    def test_assistant_text(self) -> None:
        line = (
            '{"type":"assistant","message":{"role":"assistant","content":'
            '[{"type":"text","text":"It prints `hello world`."}]},'
            '"session_id":"%s"}' % SESSION_ID
        )
        self.assertEqual(
            normalize_cursor_stream_event(line),
            {
                "kind": "assistant_text",
                "session_id": SESSION_ID,
                "text": "It prints `hello world`.",
            },
        )

    def test_terminal_result(self) -> None:
        line = (
            '{"type":"result","subtype":"success","duration_ms":4889,'
            '"is_error":false,"result":"It prints `hello world`.",'
            '"session_id":"%s","request_id":"edd8e841",'
            '"usage":{"inputTokens":14948,"outputTokens":103}}' % SESSION_ID
        )
        event = normalize_cursor_stream_event(line)
        self.assertEqual(event["kind"], "turn_finished")
        self.assertFalse(event["is_error"])
        self.assertEqual(event["result_text"], "It prints `hello world`.")
        self.assertEqual(event["usage"]["inputTokens"], 14948)

    def test_unrecognized_event_type_raises_rather_than_silently_dropping(
        self,
    ) -> None:
        # A schema change in a future Cursor CLI release should be loud,
        # not silently swallowed as if nothing happened during the turn.
        with self.assertRaises(CursorEventParseError):
            normalize_cursor_stream_event(
                '{"type":"something_new","session_id":"%s"}' % SESSION_ID
            )


if __name__ == "__main__":
    unittest.main()
