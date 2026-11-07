"""Claude timeout precedence without starting the server or touching its state."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "agent_server.py"


def compile_settings():
    functions = {"env_setting", "agentsdock_setting"}
    settings = {"IDLE_KILL_SECONDS", "CLAUDE_SDK_IDLE_TIMEOUT_SECONDS"}
    selected = [node for node in ast.parse(SOURCE.read_text()).body if (
        isinstance(node, ast.FunctionDef) and node.name in functions
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id in settings for target in node.targets)
    )]
    assert len(selected) == len(functions) + len(settings)
    return compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec")


SETTINGS_CODE = compile_settings()


def effective_idle_timeout(env):
    namespace = {"os": SimpleNamespace(environ=env)}
    exec(SETTINGS_CODE, namespace)
    return namespace["CLAUDE_SDK_IDLE_TIMEOUT_SECONDS"]


class ClaudeTimeoutConfigurationTests(unittest.TestCase):
    def test_default_disables_idle_deadline(self):
        self.assertEqual(effective_idle_timeout({}), 0)

    def test_explicit_global_idle_deadline_is_inherited(self):
        for prefix in ("AGENTSDOCK", "ZENITHBOT"):
            with self.subTest(prefix=prefix):
                self.assertEqual(effective_idle_timeout({f"{prefix}_IDLE_KILL_SECONDS": "3600"}), 3600)

    def test_explicit_claude_override_including_zero_wins(self):
        for prefix in ("AGENTSDOCK", "ZENITHBOT"):
            for override in (0, 120):
                with self.subTest(prefix=prefix, override=override):
                    self.assertEqual(effective_idle_timeout({
                        "AGENTSDOCK_IDLE_KILL_SECONDS": "3600",
                        f"{prefix}_CLAUDE_SDK_IDLE_TIMEOUT_SECONDS": str(override),
                    }), override)
        self.assertEqual(effective_idle_timeout({
            "AGENTSDOCK_IDLE_KILL_SECONDS": "3600",
            "AGENTSDOCK_CLAUDE_SDK_IDLE_TIMEOUT_SECONDS": "0",
            "ZENITHBOT_CLAUDE_SDK_IDLE_TIMEOUT_SECONDS": "120",
        }), 0)


if __name__ == "__main__":
    unittest.main()
