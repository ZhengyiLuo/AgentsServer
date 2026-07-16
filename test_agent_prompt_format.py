import unittest

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
        )

        for prompt in (claude_prompt, codex_prompt):
            self.assertIn("inline LaTeX as `$...$`", prompt)
            self.assertIn("display equations as `$$...$$`", prompt)


if __name__ == "__main__":
    unittest.main()
