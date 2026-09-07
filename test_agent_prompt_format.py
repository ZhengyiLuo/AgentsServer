import unittest
from pathlib import Path

import agent_server


class AgentPromptFormatTests(unittest.TestCase):
    def test_both_agent_prompts_explain_renderable_latex_delimiters(self) -> None:
        claude_prompt = agent_server.SYSTEM_PROMPT.format(
            manifest_path="/tmp/manifest.json",
            terminal_session="zd_sess_123",
        )
        codex_prompt = agent_server.CODEX_PROMPT_PRELUDE.format(
            manifest_path="/tmp/manifest.json",
            terminal_session="zd_sess_123",
            chat_id="sess_123",
        )

        for prompt in (claude_prompt, codex_prompt):
            self.assertIn("`$...$`", prompt)
            self.assertIn("`$$...$$`", prompt)

    def test_all_provider_prompts_require_separate_resumable_wait_slices(self) -> None:
        claude_prompt = agent_server.CLAUDE_PROMPT_PRELUDE.format()
        codex_prompt = agent_server.CODEX_PROMPT_PRELUDE.format(
            manifest_path="/tmp/manifest.json",
            terminal_session="zd_sess_123",
            chat_id="sess_123",
        )
        cursor_prompt = agent_server.cross_chat_provider_authority_block(
            [],
            Path("/tmp/provider-authority.json"),
            "sess_123",
            {"agent_cross_chat_routes"},
            provider_route_snapshot=[{
                "route_id": "route_" + "a" * 32,
                "target_session_id": "sess_target",
                "alias": "review",
                "actions": ["request_reply"],
                "enabled": True,
            }],
        )

        for prompt in (claude_prompt, codex_prompt):
            compact = " ".join(prompt.split())
            self.assertIn("pending=true", compact)
            self.assertIn("call Chats `wait`", compact)
            self.assertIn("exchange/inbound-leg/lease values", compact)
            self.assertIn("one foreground call at a time", compact)
            self.assertIn("Never finish", compact)
            self.assertIn("never loop", compact)

        cursor_compact = " ".join(cursor_prompt.split())
        self.assertIn("pending=true", cursor_compact)
        self.assertIn("wait --exchange EXCHANGE_ID", cursor_compact)
        self.assertIn("--lease LIVE_RESPONSE_LEASE_ID", cursor_compact)
        self.assertIn("foreground `wait` tool call", cursor_compact)
        self.assertIn("Never finish", cursor_compact)
        self.assertIn("shell loop", cursor_compact)


if __name__ == "__main__":
    unittest.main()
