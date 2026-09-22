"""Verify sender guidance and verbatim bodies without loading server state."""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import patch

import agentsdock_chats


GUIDANCE = (
    "Preserve normal word spacing, punctuation, and paragraph breaks in message bodies; "
    "keep technical summaries concise without concatenating words or numbers."
)


class ChatMessageReadabilityTests(unittest.TestCase):
    def test_provider_tool_description_guides_message_authors(self):
        tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        description = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "PROVIDER_TOOL_DESCRIPTION"
                    for target in node.targets)
        )
        self.assertIn(GUIDANCE, description)

    def test_message_command_help_has_the_same_authoring_guidance(self):
        for command in ("send", "ask", "respond", "respond-current"):
            with self.subTest(command=command):
                output = io.StringIO()
                with redirect_stdout(output), self.assertRaises(SystemExit) as result:
                    agentsdock_chats.parser().parse_args([command, "--help"])
                self.assertEqual(result.exception.code, 0)
                self.assertIn(GUIDANCE, " ".join(output.getvalue().split()))

    def test_message_stdin_preserves_spacing_and_does_not_rewrite_joined_text(self):
        for body in ("Run on **20** episodes.\n\nOperator `body`: mean 0.06.\n",
                     "Run on20episodes.\nOperatorbody:mean.06."):
            with self.subTest(body=body), patch.object(agentsdock_chats.sys, "stdin", io.StringIO(body)):
                self.assertEqual(agentsdock_chats.read_message_stdin(), body)


if __name__ == "__main__":
    unittest.main()
