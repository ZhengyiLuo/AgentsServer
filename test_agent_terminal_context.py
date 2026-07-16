import unittest
from pathlib import Path

import agent_server


class AgentTerminalContextTests(unittest.TestCase):
    def test_agent_environment_identifies_chat_terminal(self) -> None:
        env = agent_server.agent_runner_env("sess-ab/cd")

        self.assertEqual(env["AGENTSDOCK_CHAT_ID"], "sess-ab/cd")
        self.assertEqual(env["AGENTSDOCK_TMUX_SESSION"], "zd_sess_ab_cd")

    def test_claude_prompt_names_terminal_and_keeps_it_read_only(self) -> None:
        command = agent_server.build_claude_cmd(
            "sess-123",
            {"id": "sess-123", "backend": "claude"},
            Path("/tmp/manifest.json"),
        )
        prompt = command[command.index("--append-system-prompt") + 1]

        self.assertIn("`zd_sess_123`", prompt)
        self.assertIn("AGENTSDOCK_TMUX_SESSION", prompt)
        self.assertIn("Do not send keys", prompt)

    def test_codex_prompt_names_terminal_and_preserves_user_prompt(self) -> None:
        command = agent_server.build_codex_cmd(
            "sess-123",
            {"id": "sess-123", "backend": "codex"},
            "Inspect the terminal state.",
            Path("/tmp/manifest.json"),
        )
        prompt = command[-1]

        self.assertIn("`zd_sess_123`", prompt)
        self.assertIn("AGENTSDOCK_TMUX_SESSION", prompt)
        self.assertTrue(prompt.endswith("Inspect the terminal state."))


if __name__ == "__main__":
    unittest.main()
