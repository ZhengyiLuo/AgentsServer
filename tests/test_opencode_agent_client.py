"""Tests for opencode_agent_client.py against real captured CLI output.

Every stream fixture below was captured verbatim from a real
`opencode run --format json` invocation (opencode 1.18.29) against a scratch
directory, not hand-written from documentation. Session and call ids are the
real values from that run.
"""

import json
import unittest

from opencode_agent_client import (
    merge_opencode_usage,
    opencode_resume_failure,
    opencode_stderr_diagnostic,
    OPENCODE_DEFAULT_PERMISSION_MODE,
    OPENCODE_MUTATING_TOOLS,
    OPENCODE_PERMISSION_MODES,
    OpenCodeEventParseError,
    build_opencode_cmd,
    build_opencode_env_overrides,
    new_opencode_enforced_agent_name,
    normalize_opencode_stream,
    normalize_opencode_stream_event,
    opencode_permission_config,
    parse_opencode_models_list,
)

SESSION_ID = "ses_f86f2df67ffep5CKpMOXIyR44R"

STEP_START = (
    '{"type":"step_start","timestamp":1788737301240,"sessionID":"%s",'
    '"part":{"id":"prt_a","messageID":"msg_a","sessionID":"%s",'
    '"type":"step-start"}}' % (SESSION_ID, SESSION_ID)
)

TOOL_COMPLETED = (
    '{"type":"tool_use","timestamp":1788737301288,"sessionID":"%s","part":'
    '{"type":"tool","tool":"glob","callID":"call-727656b4-85db-42d7-a1dc-87b2da4b6888",'
    '"state":{"status":"completed","input":{"pattern":"**/a.py"},'
    '"output":"/private/tmp/oc-fix/a.py","metadata":{"count":1,"truncated":false},'
    '"title":"private/tmp/oc-fix","time":{"start":1788737301270,"end":1788737301287}},'
    '"id":"prt_0790d2b0f001uWx8VzTdpmecnD","sessionID":"%s",'
    '"messageID":"msg_0790d214c001SdrDJ388c531wr"}}' % (SESSION_ID, SESSION_ID)
)

TEXT_EVENT = (
    '{"type":"text","timestamp":1788737304682,"sessionID":"%s","part":'
    '{"id":"prt_0790d36bb0010Z8Zs85Pf16uU2","messageID":"msg_0790d3239001s1r47vuU4esHAA",'
    '"sessionID":"%s","type":"text",'
    '"text":"a.py contains a single line that prints \\"hi\\" when executed.",'
    '"time":{"start":1788737304251,"end":1788737304664}}}' % (SESSION_ID, SESSION_ID)
)

STEP_FINISH = (
    '{"type":"step_finish","timestamp":1788737301324,"sessionID":"%s","part":'
    '{"id":"prt_0790d2b44001LD4We60epZ4fgw","reason":"tool-calls",'
    '"messageID":"msg_0790d214c001SdrDJ388c531wr","sessionID":"%s",'
    '"type":"step-finish","tokens":{"total":9485,"input":9429,"output":27,'
    '"reasoning":29,"cache":{"write":0,"read":0}},"cost":0}}'
    % (SESSION_ID, SESSION_ID)
)

# Real `opencode models` output, truncated to a representative slice.
REAL_MODELS_OUTPUT = """opencode/big-pickle
opencode/ling-3.0-flash-fin-free
opencode/mimo-v2.5-free
opencode/nemotron-3.5-lightning-free
"""


# Captured from a run where the project config denied `bash` and the model
# called it anyway: the denial arrives as a synthetic tool named "invalid"
# whose status is "completed", not as the denied tool with a failing status.
UNAVAILABLE_TOOL = (
    '{"type":"tool_use","timestamp":1788739077000,"sessionID":"%s","part":'
    '{"type":"tool","tool":"invalid","callID":"call_unavailable_1",'
    '"state":{"status":"completed","input":{},'
    '"output":"The arguments provided to the tool are invalid: '
    'Model tried to call unavailable tool \'bash\'.",'
    '"time":{"start":1788739077000,"end":1788739077001}},'
    '"id":"prt_inv","sessionID":"%s","messageID":"msg_inv"}}' % (SESSION_ID, SESSION_ID)
)


# Captured verbatim from a run with an unknown model id. Note there is no
# `part` key at all - the payload hangs off `error`.
ERROR_EVENT = (
    '{"type":"error","timestamp":1788739634702,"sessionID":"%s",'
    '"error":{"name":"UnknownError","data":{"message":"Unexpected server '
    'error. Check server logs for details.","ref":"err_46498456"}}}' % SESSION_ID
)


class NormalizeStreamEventTests(unittest.TestCase):
    def test_error_event_has_no_part_key_and_still_parses(self) -> None:
        # Requiring `part` turned every provider failure into a parse failure,
        # which reported the wrong cause for the turn.
        event = normalize_opencode_stream_event(ERROR_EVENT)
        self.assertEqual(event["kind"], "turn_error")
        self.assertIn("Unexpected server error", event["message"])
        self.assertIn("UnknownError", event["message"])

    def test_error_without_a_message_still_names_the_failure(self) -> None:
        line = json.dumps({
            "type": "error", "sessionID": SESSION_ID,
            "error": {"name": "ProviderAuthError"},
        })
        self.assertEqual(
            normalize_opencode_stream_event(line)["message"], "ProviderAuthError"
        )

    def test_withheld_tool_is_rejected_despite_completed_status(self) -> None:
        # The status field says "completed", so keying only off status would
        # report a blocked action as one that successfully ran.
        event = normalize_opencode_stream_event(UNAVAILABLE_TOOL)
        self.assertEqual(event["kind"], "tool_rejected")
        self.assertIn("unavailable tool", event["reason"])

    def test_blank_line_is_ignored(self) -> None:
        self.assertIsNone(normalize_opencode_stream_event("   "))

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(OpenCodeEventParseError):
            normalize_opencode_stream_event("{not json")

    def test_step_start_has_no_projection(self) -> None:
        self.assertIsNone(normalize_opencode_stream_event(STEP_START))

    def test_assistant_text(self) -> None:
        self.assertEqual(
            normalize_opencode_stream_event(TEXT_EVENT),
            {
                "kind": "assistant_text",
                "session_id": SESSION_ID,
                "text": 'a.py contains a single line that prints "hi" when executed.',
            },
        )

    def test_completed_tool_carries_name_args_and_output(self) -> None:
        event = normalize_opencode_stream_event(TOOL_COMPLETED)
        self.assertEqual(event["kind"], "tool_finished")
        self.assertEqual(event["tool"], "glob")
        self.assertEqual(event["call_id"], "call-727656b4-85db-42d7-a1dc-87b2da4b6888")
        self.assertEqual(event["args"], {"pattern": "**/a.py"})
        self.assertEqual(event["result"], "/private/tmp/oc-fix/a.py")

    def test_step_finish_projects_usage_and_cost(self) -> None:
        event = normalize_opencode_stream_event(STEP_FINISH)
        self.assertEqual(event["kind"], "step_finished")
        self.assertEqual(event["reason"], "tool-calls")
        self.assertEqual(event["usage"]["input_tokens"], 9429)
        self.assertEqual(event["usage"]["output_tokens"], 27)
        self.assertEqual(event["usage"]["reasoning_tokens"], 29)
        self.assertEqual(event["usage"]["cost"], 0.0)

    def test_denied_tool_is_rejected_not_failed(self) -> None:
        # OpenCode reports a config-denied tool as `invalid` rather than
        # failing the turn, so it has to stay distinguishable from a tool
        # that actually ran and errored.
        line = json.dumps({
            "type": "tool_use", "sessionID": SESSION_ID,
            "part": {
                "type": "tool", "tool": "bash", "callID": "call-denied-1",
                "state": {"status": "invalid", "input": {"command": "rm -rf /"}},
            },
        })
        event = normalize_opencode_stream_event(line)
        self.assertEqual(event["kind"], "tool_rejected")
        self.assertEqual(event["tool"], "bash")

    def test_errored_tool_is_distinct_from_denied(self) -> None:
        line = json.dumps({
            "type": "tool_use", "sessionID": SESSION_ID,
            "part": {
                "type": "tool", "tool": "read", "callID": "call-err-1",
                "state": {"status": "error", "error": "file not found"},
            },
        })
        event = normalize_opencode_stream_event(line)
        self.assertEqual(event["kind"], "tool_failed")
        self.assertEqual(event["reason"], "file not found")

    def test_unrecognized_event_type_raises_rather_than_dropping(self) -> None:
        # A schema change in a future release must be loud, not silently
        # swallowed as if nothing happened during the turn.
        line = json.dumps({
            "type": "something_new", "sessionID": SESSION_ID, "part": {"type": "x"},
        })
        with self.assertRaises(OpenCodeEventParseError):
            normalize_opencode_stream_event(line)

    def test_malformed_session_id_is_rejected(self) -> None:
        line = json.dumps({
            "type": "text", "sessionID": "bad id with spaces",
            "part": {"type": "text", "text": "hi"},
        })
        with self.assertRaises(OpenCodeEventParseError):
            normalize_opencode_stream_event(line)

    def test_mismatched_part_session_id_is_rejected(self) -> None:
        line = json.dumps({
            "type": "text", "sessionID": SESSION_ID,
            "part": {
                "type": "text", "sessionID": "ses_different", "text": "hi",
            },
        })
        with self.assertRaisesRegex(OpenCodeEventParseError, "different sessionIDs"):
            normalize_opencode_stream_event(line)

    def test_part_session_id_is_used_when_top_level_id_is_omitted(self) -> None:
        line = json.dumps({
            "type": "text",
            "part": {"type": "text", "sessionID": SESSION_ID, "text": "hi"},
        })
        self.assertEqual(
            normalize_opencode_stream_event(line)["session_id"], SESSION_ID
        )

    def test_explicit_mismatched_part_type_is_rejected(self) -> None:
        line = json.dumps({
            "type": "text", "sessionID": SESSION_ID,
            "part": {"type": "tool", "text": "hi"},
        })
        with self.assertRaisesRegex(OpenCodeEventParseError, "part payload"):
            normalize_opencode_stream_event(line)

    def test_omitted_part_type_remains_compatible(self) -> None:
        line = json.dumps({
            "type": "text", "sessionID": SESSION_ID,
            "part": {"text": "hi"},
        })
        self.assertEqual(normalize_opencode_stream_event(line)["text"], "hi")

    def test_non_string_text_is_rejected_instead_of_stringified(self) -> None:
        line = json.dumps({
            "type": "text", "sessionID": SESSION_ID,
            "part": {"type": "text", "text": {"unexpected": "shape"}},
        })
        with self.assertRaisesRegex(OpenCodeEventParseError, "must be a string"):
            normalize_opencode_stream_event(line)

    def test_empty_step_finish_reason_is_rejected(self) -> None:
        line = json.dumps({
            "type": "step_finish", "sessionID": SESSION_ID,
            "part": {"type": "step-finish", "reason": ""},
        })
        with self.assertRaisesRegex(OpenCodeEventParseError, "nonempty string"):
            normalize_opencode_stream_event(line)

    def test_full_captured_turn_projects_in_order(self) -> None:
        events = list(normalize_opencode_stream(
            [STEP_START, TOOL_COMPLETED, STEP_FINISH, TEXT_EVENT]
        ))
        self.assertEqual(
            [event["kind"] for event in events],
            ["tool_finished", "step_finished", "assistant_text"],
        )


class PermissionConfigTests(unittest.TestCase):
    AGENT = "agentsdock-turn-" + "a" * 64

    def test_default_defers_entirely_to_opencode(self) -> None:
        # Product decision: the default mode matches OpenCode's own behaviour,
        # which allows every tool including bash with no prompt (verified
        # live). That is deliberately broader than the Cursor default.
        self.assertIsNone(opencode_permission_config("default"))

    def test_default_injects_no_env_var_so_operator_config_survives(self) -> None:
        # Setting OPENCODE_CONFIG_CONTENT at all would shadow the operator's
        # own opencode.json, so "match OpenCode" has to mean leaving the
        # variable unset, not sending an empty or permissive config.
        self.assertEqual(build_opencode_env_overrides("default"), {})

    def test_plan_denies_every_mutating_tool(self) -> None:
        # Denying only bash is not a restriction: a model blocked from bash
        # reached for `write` and produced the same file.
        config = opencode_permission_config("plan")
        for tool in OPENCODE_MUTATING_TOOLS:
            self.assertEqual(config["permission"][tool], "deny", tool)
        self.assertEqual(config["permission"]["task"], "deny")

    def test_full_access_allows_every_mutating_tool(self) -> None:
        # Distinct from the default even though OpenCode already allows these:
        # this one is an explicit override that still wins when the operator's
        # own config denies a tool.
        config = opencode_permission_config("full_access")
        for tool in OPENCODE_MUTATING_TOOLS:
            self.assertEqual(config["permission"][tool], "allow", tool)

    def test_unknown_mode_falls_back_to_the_default(self) -> None:
        self.assertIsNone(opencode_permission_config("not_a_mode"))
        self.assertEqual(build_opencode_env_overrides("not_a_mode"), {})

    def test_every_restricting_mode_produces_a_config(self) -> None:
        for mode in OPENCODE_PERMISSION_MODES:
            config = opencode_permission_config(mode)
            if mode == OPENCODE_DEFAULT_PERMISSION_MODE:
                self.assertIsNone(config)
            else:
                self.assertIn("permission", config)

    def test_env_override_is_inline_json_the_cli_accepts(self) -> None:
        env = build_opencode_env_overrides(
            "plan", enforced_agent_name=self.AGENT
        )
        self.assertIn("OPENCODE_CONFIG_CONTENT", env)
        self.assertNotIn("OPENCODE_PERMISSION", env)
        parsed = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        agent = parsed["agent"][self.AGENT]
        self.assertEqual(agent["mode"], "primary")
        self.assertEqual(
            agent["permission"], opencode_permission_config("plan")["permission"]
        )

    def test_random_primary_agent_survives_global_and_named_agent_wildcards(self) -> None:
        existing = json.dumps({
            "model": "operator/custom-model",
            "provider": {"operator": {"api": "https://example.invalid"}},
            "permission": {"*": "allow", "bash": "allow"},
            "agent": {"build": {"permission": {"*": "allow"}}},
        })
        env = build_opencode_env_overrides(
            "plan", existing, enforced_agent_name=self.AGENT
        )
        parsed = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(parsed["model"], "operator/custom-model")
        self.assertEqual(
            parsed["provider"],
            {"operator": {"api": "https://example.invalid"}},
        )
        self.assertEqual(parsed["permission"], {"*": "allow", "bash": "allow"})
        self.assertEqual(
            parsed["agent"]["build"], {"permission": {"*": "allow"}}
        )
        permissions = parsed["agent"][self.AGENT]["permission"]
        for tool in OPENCODE_MUTATING_TOOLS:
            self.assertEqual(permissions[tool], "deny", tool)

    def test_enforced_modes_require_the_random_agent_boundary(self) -> None:
        with self.assertRaisesRegex(ValueError, "one-turn agent"):
            build_opencode_env_overrides("plan")
        with self.assertRaisesRegex(ValueError, "one-turn agent"):
            build_opencode_env_overrides("full_access", enforced_agent_name="build")

    def test_malformed_existing_inline_config_fails_without_leaking_it(self) -> None:
        secret = "not-json-SENSITIVE-CONFIG-CONTENT"
        with self.assertRaises(ValueError) as caught:
            build_opencode_env_overrides(
                "plan", secret, enforced_agent_name=self.AGENT
            )
        self.assertNotIn(secret, str(caught.exception))

    def test_global_permission_shorthand_is_preserved_unchanged(self) -> None:
        env = build_opencode_env_overrides(
            "full_access",
            json.dumps({"permission": "ask"}),
            enforced_agent_name=self.AGENT,
        )
        parsed = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(parsed["permission"], "ask")
        permissions = parsed["agent"][self.AGENT]["permission"]
        for tool in OPENCODE_MUTATING_TOOLS:
            self.assertEqual(permissions[tool], "allow")

    def test_unknown_non_object_existing_agents_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "agents must be a JSON object"):
            build_opencode_env_overrides(
                "full_access",
                json.dumps({"agent": ["build"]}),
                enforced_agent_name=self.AGENT,
            )

    def test_selected_skill_adds_private_instruction_without_new_read_access(self) -> None:
        env = build_opencode_env_overrides(
            "plan",
            json.dumps({"instructions": ["operator.md"]}),
            instruction_paths=["/private/tmp/selected-skill.md"],
            deny_skill_tool=True,
            enforced_agent_name=self.AGENT,
        )
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(
            config["instructions"],
            ["operator.md", "/private/tmp/selected-skill.md"],
        )
        permissions = config["agent"][self.AGENT]["permission"]
        self.assertEqual(permissions["skill"], "deny")
        self.assertEqual(permissions["task"], "deny")
        for tool in OPENCODE_MUTATING_TOOLS:
            self.assertEqual(permissions[tool], "deny", tool)
        self.assertNotIn("read", permissions)
        self.assertNotIn("external_directory", permissions)

    def test_selected_skill_rejects_non_array_operator_instructions(self) -> None:
        with self.assertRaisesRegex(ValueError, "instructions must be a string array"):
            build_opencode_env_overrides(
                "default",
                json.dumps({"instructions": "private.md"}),
                instruction_paths=["/private/tmp/selected-skill.md"],
                deny_skill_tool=True,
                enforced_agent_name=self.AGENT,
            )

    def test_generated_agents_are_cryptographically_unique(self) -> None:
        first = new_opencode_enforced_agent_name()
        second = new_opencode_enforced_agent_name()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^agentsdock-turn-[0-9a-f]{64}$")


class BuildCmdTests(unittest.TestCase):
    AGENT = "agentsdock-turn-" + "b" * 64

    def test_first_turn_has_no_resume_flag(self) -> None:
        self.assertEqual(
            build_opencode_cmd({}, "hello", opencode_bin="opencode"),
            ["opencode", "run", "--format", "json", "hello"],
        )

    def test_workspace_is_passed_as_dir_not_left_to_process_cwd(self) -> None:
        # Keep workspace selection explicit instead of relying on CLI cwd and
        # configuration resolution details.
        cmd = build_opencode_cmd({"cwd": "/tmp/ws"}, "hi", opencode_bin="opencode")
        self.assertIn("--dir", cmd)
        self.assertEqual(cmd[cmd.index("--dir") + 1], "/tmp/ws")

    def test_explicit_workdir_overrides_the_session_field(self) -> None:
        cmd = build_opencode_cmd({"cwd": "/tmp/stale"}, "hi", workdir="/tmp/resolved")
        self.assertEqual(cmd[cmd.index("--dir") + 1], "/tmp/resolved")
        self.assertNotIn("/tmp/stale", cmd)

    def test_resume_uses_the_captured_session_id(self) -> None:
        cmd = build_opencode_cmd(
            {"opencode_session_id": SESSION_ID}, "follow up", opencode_bin="opencode"
        )
        self.assertEqual(
            cmd,
            ["opencode", "run", "--format", "json", "-s", SESSION_ID, "follow up"],
        )

    def test_model_and_variant_are_threaded_through(self) -> None:
        cmd = build_opencode_cmd(
            {"model": "opencode/mimo-v2.5-free", "effort": "high"},
            "hi",
            opencode_bin="opencode",
        )
        self.assertEqual(
            cmd,
            [
                "opencode", "run", "--format", "json",
                "-m", "opencode/mimo-v2.5-free", "--variant", "high", "hi",
            ],
        )

    def test_prompt_stays_last_so_it_cannot_be_read_as_a_flag(self) -> None:
        cmd = build_opencode_cmd(
            {"model": "opencode/mimo-v2.5-free"}, "--auto", opencode_bin="opencode"
        )
        self.assertEqual(cmd[-1], "--auto")

    def test_repeatable_files_and_enforced_agent_precede_prompt(self) -> None:
        cmd = build_opencode_cmd(
            {},
            "inspect both",
            opencode_bin="opencode",
            attachment_paths=["/private/tmp/a one.png", "/private/tmp/-two.pdf"],
            enforced_agent_name=self.AGENT,
        )
        self.assertEqual(cmd.count("--file"), 2)
        self.assertEqual(
            [cmd[index + 1] for index, value in enumerate(cmd) if value == "--file"],
            ["/private/tmp/a one.png", "/private/tmp/-two.pdf"],
        )
        self.assertEqual(cmd[cmd.index("--agent") + 1], self.AGENT)
        self.assertEqual(cmd[-2:], ["--", "inspect both"])
        self.assertEqual(cmd[-1], "inspect both")

    def test_empty_stdin_prompt_does_not_add_file_option_sentinel(self) -> None:
        cmd = build_opencode_cmd(
            {},
            "",
            attachment_paths=["/private/tmp/only.png"],
        )
        self.assertEqual(cmd[-2:], ["/private/tmp/only.png", ""])
        self.assertNotIn("--", cmd)

    def test_enforced_resume_forks_away_session_permissions(self) -> None:
        cmd = build_opencode_cmd(
            {"opencode_session_id": SESSION_ID},
            "continue safely",
            enforced_agent_name=self.AGENT,
        )
        self.assertEqual(cmd[cmd.index("-s") + 1], SESSION_ID)
        self.assertIn("--fork", cmd)

    def test_invalid_enforced_agent_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "one-turn agent"):
            build_opencode_cmd({}, "hi", enforced_agent_name="build")

    def test_falsy_resume_id_is_treated_as_a_first_turn(self) -> None:
        cmd = build_opencode_cmd({"opencode_session_id": ""}, "hi")
        self.assertNotIn("-s", cmd)


# Captured verbatim from `opencode run -s ses_doesnotexist`, colour codes and
# all. stdout was completely empty; this was the only signal.
REAL_RESUME_FAILURE_STDERR = (
    "\x1b[91m\x1b[1mError: \x1b[0mSession not found\n"
)


class StderrDiagnosticTests(unittest.TestCase):
    def test_resume_failure_is_recognized_through_ansi_colour_codes(self) -> None:
        # The marker is wrapped in colour escapes even on a non-tty stderr, so
        # a plain substring check against the raw bytes misses it.
        self.assertTrue(opencode_resume_failure(REAL_RESUME_FAILURE_STDERR))
        self.assertTrue(
            opencode_resume_failure(REAL_RESUME_FAILURE_STDERR.encode())
        )

    def test_ordinary_failure_is_not_read_as_a_resume_failure(self) -> None:
        self.assertFalse(opencode_resume_failure("Error: network unreachable"))

    def test_diagnostic_strips_colour_and_keeps_the_message(self) -> None:
        diagnostic = opencode_stderr_diagnostic(REAL_RESUME_FAILURE_STDERR)
        self.assertEqual(diagnostic, "Error: Session not found")
        self.assertNotIn("\x1b", diagnostic)

    def test_empty_stderr_yields_no_diagnostic(self) -> None:
        self.assertEqual(opencode_stderr_diagnostic(b""), "")


class UsageAccumulationTests(unittest.TestCase):
    def test_multi_step_turn_sums_rather_than_overwrites(self) -> None:
        # OpenCode reports usage per step, not once per turn: keeping only the
        # last step would under-report every turn that used a tool.
        total: dict = {}
        total = merge_opencode_usage(total, {"input_tokens": 9429, "output_tokens": 27})
        total = merge_opencode_usage(total, {"input_tokens": 135, "output_tokens": 336})
        self.assertEqual(total["input_tokens"], 9564)
        self.assertEqual(total["output_tokens"], 363)

    def test_non_numeric_values_are_ignored(self) -> None:
        self.assertEqual(merge_opencode_usage({}, {"cost": "free", "x": True}), {})


class ParseModelsTests(unittest.TestCase):
    def test_parses_real_captured_output(self) -> None:
        models = parse_opencode_models_list(REAL_MODELS_OUTPUT)
        by_id = {model["id"]: model for model in models}
        self.assertEqual(len(models), 4)
        self.assertEqual(by_id["opencode/mimo-v2.5-free"]["provider"], "opencode")
        self.assertTrue(by_id["opencode/mimo-v2.5-free"]["is_free"])
        self.assertFalse(by_id["opencode/big-pickle"]["is_free"])

    def test_ignores_noise_and_duplicates(self) -> None:
        models = parse_opencode_models_list(
            "Available models\n"
            "opencode/a-free\n"
            "opencode/a-free\n"
            "\n"
            "not a model id\n"
        )
        self.assertEqual([model["id"] for model in models], ["opencode/a-free"])

    def test_empty_output_yields_no_models(self) -> None:
        self.assertEqual(parse_opencode_models_list(""), [])


if __name__ == "__main__":
    unittest.main()
