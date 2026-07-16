import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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

    def test_per_chat_system_prompt_reaches_both_backends(self) -> None:
        session = {
            "id": "sess-123",
            "system_prompt": "Always verify the deployment target before editing.",
        }
        claude_command = agent_server.build_claude_cmd(
            "sess-123",
            {**session, "backend": "claude"},
            Path("/tmp/manifest.json"),
        )
        claude_prompt = claude_command[claude_command.index("--append-system-prompt") + 1]
        codex_prompt = agent_server.build_codex_cmd(
            "sess-123",
            {**session, "backend": "codex"},
            "Check the current status.",
            Path("/tmp/manifest.json"),
        )[-1]

        for prompt in (claude_prompt, codex_prompt):
            self.assertIn("[Per-chat system instructions]", prompt)
            self.assertIn("Always verify the deployment target before editing.", prompt)
        self.assertTrue(codex_prompt.endswith("Check the current status."))

    def test_public_session_exposes_per_chat_system_prompt(self) -> None:
        public = agent_server.public_session({
            "id": "sess-123",
            "title": "Deployment",
            "system_prompt": "Use the staging cluster.",
        })

        self.assertEqual(public["system_prompt"], "Use the staging cluster.")


class SessionSystemPromptPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_store_updates_and_clears_system_prompt(self) -> None:
        store = agent_server.SessionStore()
        store.sessions["sess-123"] = {
            "id": "sess-123",
            "title": "Deployment",
            "folder": "General",
            "backend": "codex",
            "system_prompt": None,
        }

        with patch.object(store, "save", AsyncMock()):
            updated = await store.update("sess-123", {"system_prompt": "  Use staging.  "})
            self.assertEqual(updated["system_prompt"], "Use staging.")
            cleared = await store.update("sess-123", {"system_prompt": None})

        self.assertIsNone(cleared["system_prompt"])


if __name__ == "__main__":
    unittest.main()
