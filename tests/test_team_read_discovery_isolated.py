"""Team mention discovery contracts without importing or starting the server."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import unittest


def extracted():
    names = {
        "PROVIDER_TOOL_DESCRIPTION", "PROVIDER_TOOL_READ_ONLY_COMMANDS",
        "MAX_PROVIDER_STATIC_INSTRUCTIONS_CHARS", "PROVIDER_AUTHORITY_USAGE_INSTRUCTIONS",
        "CROSS_CHAT_DELIVERY_INSTRUCTIONS", "PROVIDER_THREAD_INSTRUCTION_ADDENDUM",
        "CLAUDE_PROMPT_PRELUDE", "CODEX_PROMPT_PRELUDE", "CODEX_THREAD_POLICY_VERSION",
        "provider_turn_may_read_team", "provider_tool_call_is_read_only",
        "codex_thread_instructions", "codex_thread_instruction_hash",
    }
    tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in names for target in node.targets
        ):
            nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name in names:
            nodes.append(node)
    namespace = {
        "CLAUDE_PROVIDER_MCP_TOOL_NAME": "mcp__agentsdock_internal_provider__run",
        "hashlib": hashlib,
        "codex_manifest_path": lambda _: "/isolated/artifact-manifest.json",
        "terminal_session_name": lambda _: "isolated-terminal",
        "codex_user_developer_instructions": lambda: "",
        "session_prompt_addendum": lambda _: "",
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated-team-read-discovery>", "exec"), namespace)
    return namespace


class TeamReadDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ns = extracted()

    def test_tool_discovery_names_native_team_reads_not_external_connectors(self):
        description = self.ns["PROVIDER_TOOL_DESCRIPTION"]
        for text in ("@@bulletin", "@@NAME", "not Slack or email", "helper=team",
                     "[mentions]", "[bulletin, --mention, N]", "[inbox, --mention, N]",
                     "[read, MESSAGE_ID, --team, TEAM_ID]",
                     "no manual Route", "must not send or post"):
            with self.subTest(text=text):
                self.assertIn(text, description)

    def test_both_providers_get_the_same_bounded_static_read_guidance(self):
        shared = self.ns["PROVIDER_THREAD_INSTRUCTION_ADDENDUM"]
        self.assertLess(len(shared), self.ns["MAX_PROVIDER_STATIC_INSTRUCTIONS_CHARS"])
        for name in ("CLAUDE_PROMPT_PRELUDE", "CODEX_PROMPT_PRELUDE"):
            with self.subTest(provider=name):
                prelude = self.ns[name]
                self.assertTrue(prelude.endswith(shared))
                formatted = prelude.format(manifest_path="/isolated/manifest", terminal_session="isolated", chat_id="test")
                for text in ("`bulletin --mention N` for @@bulletin", "`inbox --mention N`",
                             "`inbox --from NAME`", "do not substitute it for a selected mention",
                             "Manual routing into the chat is not required",
                             "must not call send, reply, or publish", "not an instruction to post",
                             "has_more", "--after next_after_sequence", "do not poll"):
                    self.assertIn(text, formatted)

    def test_bulletin_alias_is_read_only_but_team_writes_are_not(self):
        classify = self.ns["provider_tool_call_is_read_only"]
        for args in (["mentions"], ["bulletin"], ["feed"], ["inbox", "--mention", "1"],
                     ["inbox", "--from", "@@Pat"], ["read", "message-1"]):
            with self.subTest(args=args):
                self.assertTrue(classify("team", args))
        for args in ([], ["send"], ["reply"], ["edit"], ["skill", "publish"]):
            with self.subTest(args=args):
                self.assertFalse(classify("team", args))

    def test_discovery_does_not_expand_read_authority_for_agent_deliveries(self):
        allowed = self.ns["provider_turn_may_read_team"]
        for purpose in (None, "", "scheduled_job"):
            self.assertTrue(allowed(purpose))
        for purpose in ("cross_chat_handoff_delivery", "secure_peer_handoff_delivery", "goal_resume"):
            self.assertFalse(allowed(purpose))

    def test_new_guidance_changes_existing_thread_instruction_hash(self):
        ns = extracted()
        revised = ns["codex_thread_instruction_hash"]("test", {})
        ns["CODEX_PROMPT_PRELUDE"] = ns["CODEX_PROMPT_PRELUDE"].replace(
            "`bulletin --mention N` for @@bulletin", "legacy unspecified destination"
        )
        self.assertNotEqual(revised, ns["codex_thread_instruction_hash"]("test", {}))

    def test_discovery_contains_no_dynamic_authority_or_history_wrapper(self):
        description = self.ns["PROVIDER_TOOL_DESCRIPTION"]
        shared = self.ns["PROVIDER_THREAD_INSTRUCTION_ADDENDUM"]
        for text in ("[AgentsDock provider authority]", "authority-file=", "run_", "sess_"):
            self.assertNotIn(text, description)
            self.assertNotIn(text, shared)


if __name__ == "__main__":
    unittest.main()
