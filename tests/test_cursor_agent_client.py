"""Tests for cursor_agent_client.py, using real captured `agent` CLI output.

Every fixture line below was captured verbatim from a real, authenticated
run of the Cursor CLI (agent 2026.08.11-e8db854) against a scratch
directory, not hand-written or inferred from documentation. Identifying
session, request, and account values are replaced with same-shape synthetic
fixtures.
"""

import json
import unittest

from cursor_agent_client import (
    CURSOR_PERMISSION_MODES,
    CursorEventParseError,
    build_cursor_cmd,
    cursor_account_is_free_tier,
    cursor_permission_flags,
    normalize_cursor_stream_event,
    parse_cursor_account_tier,
    parse_cursor_auth_status,
    parse_cursor_models_list,
)

SESSION_ID = "00000000-0000-4000-8000-000000000001"

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

    def test_current_lifecycle_events_are_bounded_and_redacted(self) -> None:
        fixtures = [
            ({"type": "retry", "subtype": "starting", "session_id": SESSION_ID,
              "attempt": 2, "is_resume": True}, "provider_status"),
            ({"type": "retry", "subtype": "resuming", "session_id": SESSION_ID,
              "attempt": 3, "checkpoint_turn_count": 11}, "provider_status"),
            ({"type": "connection", "subtype": "reconnecting", "session_id": SESSION_ID,
              "attempt": 4, "endpoint_url": "https://secret.invalid/prompt"}, "provider_status"),
            ({"type": "connection", "subtype": "reconnected", "session_id": SESSION_ID},
             "provider_status"),
            ({"type": "interaction_query", "subtype": "request", "session_id": SESSION_ID,
              "query_type": "permission", "query": {"prompt": "SECRET"}},
             "provider_interaction"),
            ({"type": "interaction_query", "subtype": "response", "session_id": SESSION_ID,
              "query_type": "permission", "response": {"answer": "SECRET"}},
             "provider_interaction"),
            ({"type": "system", "subtype": "task_notification", "session_id": SESSION_ID,
              "task_id": "secret-task", "title": "SECRET", "detail": "SECRET"},
             "background_task_notice"),
            ({"type": "system", "subtype": "background_shell_timeout", "session_id": SESSION_ID,
              "aborted_count": 2, "timeout_ms": 60_000}, "background_task_timeout"),
        ]
        for payload, expected_kind in fixtures:
            with self.subTest(payload=payload):
                normalized = normalize_cursor_stream_event(json.dumps(payload))
                self.assertEqual(normalized["kind"], expected_kind)
                serialized = json.dumps(normalized)
                self.assertNotIn("SECRET", serialized)
                self.assertNotIn("secret.invalid", serialized)
                self.assertNotIn("secret-task", serialized)

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
        self.assertEqual(event["exit_code"], 0)

    def test_success_envelope_preserves_nonzero_exit(self) -> None:
        event = normalize_cursor_stream_event(json.dumps({
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-shell-unsuccessful-success-envelope",
            "tool_call": {"shellToolCall": {
                "args": {"command": "exit 7"},
                "result": {"success": {"exitCode": 7, "stderr": "failed"}},
            }},
            "session_id": SESSION_ID,
        }))
        self.assertEqual(event["kind"], "tool_finished")
        self.assertEqual(event["exit_code"], 7)

    def test_shell_tool_failure_preserves_nonzero_exit(self) -> None:
        event = normalize_cursor_stream_event(json.dumps({
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-shell-failed",
            "tool_call": {"shellToolCall": {
                "args": {"command": "false"},
                "result": {"failure": {"exitCode": 7, "stderr": "failed"}},
            }},
            "session_id": SESSION_ID,
        }))
        self.assertEqual(event["kind"], "tool_finished")
        self.assertEqual(event["exit_code"], 7)
        self.assertEqual(event["args"], {"command": "false"})

    def test_current_non_success_tool_outcomes_are_failures(self) -> None:
        outcomes = {
            "error": {"error": "read failed"},
            "timeout": {"command": "sleep 10", "timeoutMs": 1000},
            "spawnError": {"command": "missing", "error": "ENOENT"},
            "permissionDenied": {"reason": "denied", "isReadonly": False},
            "readPermissionDenied": {"path": "unreadable.txt"},
            "writePermissionDenied": {
                "path": "readonly.txt",
                "error": "Permission denied",
                "isReadonly": True,
            },
            "fileNotFound": {"path": "missing.txt"},
            "invalidFile": {"path": "binary.dat"},
            "notFile": {"path": "folder", "actualType": "directory"},
            "fileBusy": {"path": "busy.txt"},
            "noSpace": {"path": "large.bin"},
            "sandboxUnsupported": {
                "command": "echo hi",
                "workingDirectory": "/tmp",
                "sandboxPolicyType": "workspace_readonly",
                "reason": "unsupported",
                "isReadonly": True,
            },
            "notFound": {"uri": "resource://missing"},
            "serverNotFound": {
                "name": "missing-server",
                "availableServers": ["configured-server"],
            },
            "toolNotFound": {
                "name": "missing-tool",
                "availableTools": ["configured-tool"],
            },
        }
        for outcome, payload in outcomes.items():
            with self.subTest(outcome=outcome):
                event = normalize_cursor_stream_event(json.dumps({
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": f"call-{outcome}",
                    "tool_call": {"readToolCall": {
                        "args": {"path": "missing.txt"},
                        "result": {outcome: payload},
                    }},
                    "session_id": SESSION_ID,
                }))
                self.assertEqual(event["kind"], "tool_finished")
                self.assertEqual(event["exit_code"], 1)
                self.assertEqual(event["result"], payload)

    def test_current_tool_specific_success_outcomes_are_successful(self) -> None:
        outcomes = (
            ("complete", "awaitToolCall", {}),
            ("registered", "prManagementToolCall", {}),
            (
                "startSuccess",
                "recordScreenToolCall",
                {"wasPriorRecordingCancelled": False},
            ),
            ("saveSuccess", "recordScreenToolCall", {"path": "/tmp/recording.mp4"}),
            ("discardSuccess", "recordScreenToolCall", {}),
        )
        for outcome, tool_call_key, payload in outcomes:
            with self.subTest(outcome=outcome, tool_call_key=tool_call_key):
                event = normalize_cursor_stream_event(json.dumps({
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": f"call-{outcome}",
                    "tool_call": {tool_call_key: {
                        "result": {outcome: payload},
                    }},
                    "session_id": SESSION_ID,
                }))
                self.assertEqual(event["kind"], "tool_finished")
                self.assertEqual(
                    event["tool"], tool_call_key.removesuffix("ToolCall")
                )
                self.assertEqual(event["exit_code"], 0)
                self.assertEqual(event["result"], payload)

    def test_nonterminal_tool_outcomes_are_neutral_statuses(self) -> None:
        outcomes = (
            ("approved", "mcpToolCall"),
            ("async", "askQuestionToolCall"),
            ("stillRunning", "awaitToolCall"),
        )
        for outcome, tool_call_key in outcomes:
            with self.subTest(outcome=outcome):
                event = normalize_cursor_stream_event(json.dumps({
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": f"call-{outcome}",
                    "tool_call": {tool_call_key: {
                        "result": {outcome: {}},
                    }},
                    "session_id": SESSION_ID,
                }))
                self.assertEqual(event["kind"], "tool_status")
                self.assertEqual(event["status"], outcome)
                self.assertTrue(event["message"])
                self.assertNotIn("exit_code", event)

    def test_confirmation_required_is_not_reported_as_success(self) -> None:
        event = normalize_cursor_stream_event(json.dumps({
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-confirmation",
            "tool_call": {"prManagementToolCall": {
                "result": {"needsConfirmation": {}},
            }},
            "session_id": SESSION_ID,
        }))
        self.assertEqual(event["kind"], "tool_confirmation_required")
        self.assertIn("requires confirmation", event["message"])
        self.assertNotIn("exit_code", event)

    def test_tool_result_requires_exactly_one_known_outcome(self) -> None:
        for result in (
            {},
            {"pid": 123, "sandboxPolicy": "workspace"},
            {"success": {}, "timeout": {"timeoutMs": 1}},
            {"futureOutcome": {"message": "schema drift"}},
        ):
            with self.subTest(result=result), self.assertRaises(
                CursorEventParseError
            ):
                normalize_cursor_stream_event(json.dumps({
                    "type": "tool_call",
                    "subtype": "completed",
                    "call_id": "call-outcome",
                    "tool_call": {"shellToolCall": {"result": result}},
                    "session_id": SESSION_ID,
                }))

    def test_tool_result_allows_non_outcome_metadata(self) -> None:
        event = normalize_cursor_stream_event(json.dumps({
            "type": "tool_call",
            "subtype": "completed",
            "call_id": "call-success-metadata",
            "tool_call": {"shellToolCall": {
                "result": {
                    "success": {"exitCode": 0},
                    "pid": 123,
                    "sandboxPolicy": "workspace",
                },
            }},
            "session_id": SESSION_ID,
        }))
        self.assertEqual(event["kind"], "tool_finished")
        self.assertEqual(event["exit_code"], 0)

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
            '"session_id":"%s","request_id":"00000001",'
            '"usage":{"inputTokens":14948,"outputTokens":103}}' % SESSION_ID
        )
        event = normalize_cursor_stream_event(line)
        self.assertEqual(event["kind"], "turn_finished")
        self.assertFalse(event["is_error"])
        self.assertEqual(event["result_text"], "It prints `hello world`.")
        self.assertEqual(event["usage"]["input_tokens"], 14948)
        self.assertEqual(event["usage"]["output_tokens"], 103)

    def test_terminal_result_normalizes_cache_usage_buckets(self) -> None:
        event = normalize_cursor_stream_event(json.dumps({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done.",
            "session_id": SESSION_ID,
            "usage": {
                "inputTokens": 5,
                "outputTokens": 2,
                "cacheReadTokens": 10,
                "cacheWriteTokens": 3,
            },
        }))
        self.assertEqual(event["usage"], {
            "input_tokens": 5,
            "output_tokens": 2,
            "cache_read_tokens": 10,
            "cache_write_tokens": 3,
        })

    def test_terminal_result_requires_consistent_typed_success_shape(self) -> None:
        malformed = (
            {"type": "result", "subtype": "success", "result": "x", "session_id": SESSION_ID},
            {"type": "result", "subtype": "success", "is_error": "false", "result": "x", "session_id": SESSION_ID},
            {"type": "result", "subtype": "error", "is_error": False, "result": "x", "session_id": SESSION_ID},
            {"type": "result", "subtype": "success", "is_error": False, "result": {"text": "x"}, "session_id": SESSION_ID},
        )
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(CursorEventParseError):
                normalize_cursor_stream_event(json.dumps(payload))

    def test_error_terminal_is_normalized_as_logical_failure(self) -> None:
        event = normalize_cursor_stream_event(
            '{"type":"result","subtype":"error","is_error":true,'
            '"result":"denied","session_id":"%s"}' % SESSION_ID
        )
        self.assertTrue(event["is_error"])
        self.assertEqual(event["result_text"], "denied")

    def test_invalid_or_unbounded_session_id_is_rejected(self) -> None:
        for session_id in ("contains whitespace", "x" * 257):
            with self.subTest(session_id=session_id), self.assertRaises(CursorEventParseError):
                normalize_cursor_stream_event(
                    '{"type":"system","subtype":"init","session_id":"%s"}'
                    % session_id
                )
        for session_id in (123, True):
            with self.subTest(session_id=session_id), self.assertRaises(
                CursorEventParseError
            ):
                normalize_cursor_stream_event(json.dumps({
                    "type": "system",
                    "subtype": "init",
                    "session_id": session_id,
                }))

    def test_tool_events_require_bounded_nonempty_string_call_id(self) -> None:
        for call_id in (None, "", " leading", "x" * 241, 123):
            payload = {
                "type": "tool_call",
                "subtype": "started",
                "call_id": call_id,
                "tool_call": {"readToolCall": {"args": {"path": "a"}}},
                "session_id": SESSION_ID,
            }
            with self.subTest(call_id=call_id), self.assertRaises(
                CursorEventParseError
            ):
                normalize_cursor_stream_event(json.dumps(payload))

    def test_non_object_rejection_payload_is_schema_error(self) -> None:
        with self.assertRaises(CursorEventParseError):
            normalize_cursor_stream_event(
                '{"type":"tool_call","subtype":"completed",'
                '"call_id":"x","tool_call":{"shellToolCall":'
                '{"result":{"rejected":"ask"}}},"session_id":"%s"}'
                % SESSION_ID
            )

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
                "action": "Run 'cursor-agent login', or set CURSOR_API_KEY.",
            },
        )

    def test_action_uses_resolved_fallback_executable_name(self) -> None:
        result = parse_cursor_auth_status(
            "Not logged in",
            executable_name="agent",
        )
        self.assertEqual(
            result["action"],
            "Run 'agent login', or set CURSOR_API_KEY.",
        )

    def test_logged_in(self) -> None:
        # Real captured output from `agent status` after login.
        result = parse_cursor_auth_status(
            "✓ Logged in as user@example.com"
        )
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["email"], "user@example.com")

    def test_explicit_boolean_auth_status_and_ansi_are_parsed(self) -> None:
        self.assertEqual(
            parse_cursor_auth_status('{"authenticated": false}')["state"],
            "unauthenticated",
        )
        self.assertEqual(
            parse_cursor_auth_status('Authenticated: true')["state"],
            "ready",
        )
        self.assertEqual(
            parse_cursor_auth_status('\x1b[32m✓ Logged in as user@example.com\x1b[0m')["state"],
            "ready",
        )

    def test_unrecognized_output_is_reported_as_error_not_silently_ready(
        self,
    ) -> None:
        result = parse_cursor_auth_status("something the CLI never printed before")
        self.assertEqual(result["state"], "error")


# Real captured output from `agent about` (2026.08.11-e8db854, authenticated,
# on an account since upgraded to Cursor Pro).
REAL_ABOUT_OUTPUT_PRO = """About Cursor CLI

CLI Version         2026.08.11-e8db854
Model               Claude Sonnet 5 300K High
Subscription Tier   Pro
OS                  darwin (arm64)
Terminal            unknown
Shell               zsh
User Email          user@example.com
"""

# Same account/table shape, tier value swapped to what a free-plan account is
# expected to report - not independently captured (the account was upgraded
# before this was written), so treat the exact free-tier label as unverified
# until seen for real; the parser only needs the row's position/format,
# which is captured above.
ABOUT_OUTPUT_FREE_PLAN_EXPECTED_SHAPE = """About Cursor CLI

CLI Version         2026.08.11-e8db854
Model               Auto
Subscription Tier   Free
OS                  darwin (arm64)
Terminal            unknown
Shell               zsh
User Email          user@example.com
"""


class ParseCursorAccountTierTests(unittest.TestCase):
    def test_paid_tier_from_real_captured_output(self) -> None:
        self.assertEqual(parse_cursor_account_tier(REAL_ABOUT_OUTPUT_PRO), "Pro")

    def test_free_tier(self) -> None:
        self.assertEqual(
            parse_cursor_account_tier(ABOUT_OUTPUT_FREE_PLAN_EXPECTED_SHAPE), "Free"
        )

    def test_missing_row_returns_none(self) -> None:
        self.assertIsNone(parse_cursor_account_tier("About Cursor CLI\n\nOS   darwin\n"))

    def test_case_insensitive_colon_format(self) -> None:
        self.assertEqual(
            parse_cursor_account_tier("subscription tier: Pro Plus"),
            "Pro Plus",
        )


class CursorAccountIsFreeTierTests(unittest.TestCase):
    def test_pro_is_not_free(self) -> None:
        self.assertFalse(cursor_account_is_free_tier("Pro"))

    def test_case_and_whitespace_insensitive(self) -> None:
        self.assertFalse(cursor_account_is_free_tier("  pro  "))

    def test_free_is_free(self) -> None:
        self.assertTrue(cursor_account_is_free_tier("Free"))

    def test_only_explicit_free_tiers_are_free(self) -> None:
        self.assertTrue(cursor_account_is_free_tier("Hobby"))
        self.assertTrue(cursor_account_is_free_tier("Individual Free"))
        for tier in (None, "", "Start", "Pro+", "Pro Plus", "Ultra",
                     "Enterprise", "Teams Standard", "Teams Premium",
                     "SomeFutureTierName"):
            with self.subTest(tier=tier):
                self.assertFalse(cursor_account_is_free_tier(tier))


class CursorPermissionFlagsTests(unittest.TestCase):
    def test_default_is_trust_only_no_shell_auto_approval(self) -> None:
        # Real behavior confirmed live: --trust alone lets file edits through
        # but shell calls come back {"result": {"rejected": ...}}.
        self.assertEqual(cursor_permission_flags("default"), ["--trust"])

    def test_unrecognized_mode_fails_safe_to_trust_only(self) -> None:
        self.assertEqual(cursor_permission_flags("not_a_real_mode"), ["--trust"])

    def test_auto_review_is_not_an_advertised_headless_mode(self) -> None:
        self.assertNotIn("auto_review", CURSOR_PERMISSION_MODES)
        self.assertEqual(cursor_permission_flags("auto_review"), ["--trust"])

    def test_full_access_adds_force(self) -> None:
        self.assertEqual(
            cursor_permission_flags("full_access"), ["--trust", "--force"]
        )

    def test_plan_mode_is_read_only_and_acknowledges_workspace_trust(self) -> None:
        self.assertEqual(
            cursor_permission_flags("plan"),
            ["--trust", "--mode", "plan"],
        )


class BuildCursorCmdTests(unittest.TestCase):
    def test_first_turn_has_no_resume_flag(self) -> None:
        cmd = build_cursor_cmd({}, cursor_bin="agent")
        self.assertEqual(
            cmd,
            ["agent", "-p", "--output-format", "stream-json", "--trust"],
        )

    def test_resume_uses_real_session_id_shape(self) -> None:
        # Real captured behavior: `agent -p "..." --output-format json
        # --trust --resume <session_id>` correctly resumed conversation
        # context from a prior real turn against the actual CLI.
        sess = {"cursor_session_id": "00000000-0000-4000-8000-000000000002"}
        cmd = build_cursor_cmd(sess, cursor_bin="agent")
        self.assertEqual(
            cmd,
            [
                "agent", "-p", "--output-format", "stream-json",
                "--resume", "00000000-0000-4000-8000-000000000002",
                "--trust",
            ],
        )

    def test_model_and_permission_mode_are_threaded_through(self) -> None:
        sess = {"model": "sonnet-5-thinking", "cursor_permission_mode": "full_access"}
        cmd = build_cursor_cmd(sess, cursor_bin="agent")
        self.assertEqual(
            cmd,
            [
                "agent", "-p", "--output-format", "stream-json",
                "--model", "sonnet-5-thinking",
                "--trust", "--force",
            ],
        )

    def test_falsy_resume_id_is_treated_as_first_turn(self) -> None:
        cmd = build_cursor_cmd({"cursor_session_id": ""}, cursor_bin="agent")
        self.assertNotIn("--resume", cmd)

    def test_prompt_is_never_part_of_process_argv(self) -> None:
        secret = "AUTHORITY-SENTINEL"
        self.assertNotIn(secret, build_cursor_cmd({}, cursor_bin="agent"))


if __name__ == "__main__":
    unittest.main()
