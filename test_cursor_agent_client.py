"""Tests for cursor_agent_client.py, using real captured `agent` CLI output.

Every fixture line below was captured verbatim from a real, authenticated
run of the Cursor CLI (agent 2026.08.11-e8db854) against a scratch
directory, not hand-written or inferred from documentation. Session ids and
call ids are real values from that run.
"""

import unittest

from cursor_agent_client import (
    CursorEventParseError,
    build_cursor_cmd,
    cursor_permission_flags,
    normalize_cursor_stream_event,
    parse_cursor_auth_status,
    parse_cursor_models_list,
)

SESSION_ID = "51cc22bb-e694-4f7c-807b-961b5b41810c"

# Real captured output from `agent --list-models` (2026.08.11-e8db854),
# truncated to a representative slice - the full list is 200+ lines.
# Captured on a free-plan account; the CLI used "(current, default)" then.
REAL_MODELS_LIST_OUTPUT = """Available models

auto - Auto (current, default)
gpt-5.3-codex-low - Codex 5.3 Low
claude-sonnet-5-thinking-high - Claude Sonnet 5 1M Thinking
gpt-5.4-nano-none - GPT-5.4 Nano None

Tip: use --model <id> (or /model <id> in interactive mode) to switch. Parameterized models also accept quoted overrides, e.g. --model 'claude-opus-4-8[context=1m,effort=high,fast=false]'.
"""

# Re-captured later against the same CLI version after upgrading to a paid
# plan: the default marker changed to plain "(default)" with no "current,"
# prefix - real evidence the label format isn't stable across accounts/time,
# which is exactly what broke the naive substring match this fixture guards.
REAL_MODELS_LIST_OUTPUT_PAID_PLAN = """Available models

auto - Auto (default)
gpt-5.3-codex-low - Codex 5.3 Low
gpt-5.3-codex-low-fast - Codex 5.3 Low Fast
claude-sonnet-5-thinking-high - Claude Sonnet 5 1M Thinking

Tip: use --model <id> (or /model <id> in interactive mode) to switch. Parameterized models also accept quoted overrides, e.g. --model 'claude-opus-4-8[context=1m,effort=high,fast=false]'.
"""


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


class ParseCursorModelsListTests(unittest.TestCase):
    def test_parses_real_captured_output(self) -> None:
        models = parse_cursor_models_list(REAL_MODELS_LIST_OUTPUT)
        by_id = {model["id"]: model for model in models}
        self.assertEqual(len(models), 4)
        self.assertTrue(by_id["auto"]["is_router"])
        self.assertTrue(by_id["auto"]["is_default"])
        self.assertEqual(by_id["auto"]["label"], "Auto")
        self.assertFalse(by_id["claude-sonnet-5-thinking-high"]["is_router"])
        self.assertFalse(by_id["claude-sonnet-5-thinking-high"]["is_default"])
        self.assertEqual(
            by_id["claude-sonnet-5-thinking-high"]["label"],
            "Claude Sonnet 5 1M Thinking",
        )

    def test_parses_real_captured_output_paid_plan_default_format(self) -> None:
        # Guards the "(current, default)" vs "(default)" format drift found
        # live: the naive substring match silently made is_default always
        # False against this real later-captured output.
        models = parse_cursor_models_list(REAL_MODELS_LIST_OUTPUT_PAID_PLAN)
        by_id = {model["id"]: model for model in models}
        self.assertEqual(len(models), 4)
        self.assertTrue(by_id["auto"]["is_default"])
        self.assertEqual(by_id["auto"]["label"], "Auto")
        self.assertFalse(by_id["gpt-5.3-codex-low-fast"]["is_default"])

    def test_empty_output_yields_no_models(self) -> None:
        self.assertEqual(parse_cursor_models_list(""), [])


class ParseCursorAuthStatusTests(unittest.TestCase):
    def test_not_logged_in(self) -> None:
        # Real captured output from `agent status` before login.
        self.assertEqual(
            parse_cursor_auth_status("Not logged in"),
            {
                "state": "unauthenticated",
                "action": "Run 'agent login', or set CURSOR_API_KEY.",
            },
        )

    def test_logged_in(self) -> None:
        # Real captured output from `agent status` after login.
        result = parse_cursor_auth_status(
            "✓ Logged in as georgialin1999@gmail.com"
        )
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["email"], "georgialin1999@gmail.com")

    def test_unrecognized_output_is_reported_as_error_not_silently_ready(
        self,
    ) -> None:
        result = parse_cursor_auth_status("something the CLI never printed before")
        self.assertEqual(result["state"], "error")


class CursorPermissionFlagsTests(unittest.TestCase):
    def test_default_is_trust_only_no_shell_auto_approval(self) -> None:
        # Real behavior confirmed live: --trust alone lets file edits through
        # but shell calls come back {"result": {"rejected": ...}}.
        self.assertEqual(cursor_permission_flags("default"), ["--trust"])

    def test_unrecognized_mode_fails_safe_to_trust_only(self) -> None:
        self.assertEqual(cursor_permission_flags("not_a_real_mode"), ["--trust"])

    def test_auto_review_adds_smart_classifier_flag(self) -> None:
        self.assertEqual(
            cursor_permission_flags("auto_review"), ["--trust", "--auto-review"]
        )

    def test_full_access_adds_force(self) -> None:
        self.assertEqual(
            cursor_permission_flags("full_access"), ["--trust", "--force"]
        )

    def test_plan_mode_is_read_only_and_skips_trust(self) -> None:
        self.assertEqual(cursor_permission_flags("plan"), ["--mode", "plan"])


class BuildCursorCmdTests(unittest.TestCase):
    def test_first_turn_has_no_resume_flag(self) -> None:
        cmd = build_cursor_cmd({}, "hello", cursor_bin="agent")
        self.assertEqual(
            cmd,
            ["agent", "-p", "hello", "--output-format", "stream-json", "--trust"],
        )

    def test_resume_uses_real_session_id_shape(self) -> None:
        # Real captured behavior: `agent -p "..." --output-format json
        # --trust --resume <session_id>` correctly resumed conversation
        # context from a prior real turn against the actual CLI.
        sess = {"cursor_session_id": "11784a88-5107-44b3-9e2f-d5b4897dd94d"}
        cmd = build_cursor_cmd(sess, "follow up", cursor_bin="agent")
        self.assertEqual(
            cmd,
            [
                "agent", "-p", "follow up", "--output-format", "stream-json",
                "--resume", "11784a88-5107-44b3-9e2f-d5b4897dd94d",
                "--trust",
            ],
        )

    def test_model_and_permission_mode_are_threaded_through(self) -> None:
        sess = {"model": "sonnet-5-thinking", "cursor_permission_mode": "full_access"}
        cmd = build_cursor_cmd(sess, "hi", cursor_bin="agent")
        self.assertEqual(
            cmd,
            [
                "agent", "-p", "hi", "--output-format", "stream-json",
                "--model", "sonnet-5-thinking",
                "--trust", "--force",
            ],
        )

    def test_falsy_resume_id_is_treated_as_first_turn(self) -> None:
        cmd = build_cursor_cmd({"cursor_session_id": ""}, "hi", cursor_bin="agent")
        self.assertNotIn("--resume", cmd)


if __name__ == "__main__":
    unittest.main()
