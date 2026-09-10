"""Exercise real config functions without importing the live server monolith."""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "_codex_config_positive_int", "_codex_config_non_empty_str",
    "_warn_codex_thread_config_once", "sanitize_codex_thread_config",
    "merge_codex_thread_config", "read_codex_thread_config_overrides",
    "codex_session_config_overrides", "codex_effective_thread_config",
    "codex_thread_params", "flatten_codex_config_overrides", "build_codex_cmd",
}
CONSTANTS = {
    "CODEX_THREAD_CONFIG_DEFAULTS", "CODEX_THREAD_CONFIG_LEGACY_MAX_THREADS_KEY",
    "CODEX_THREAD_CONFIG_AGENT_VALIDATORS", "CODEX_THREAD_CONFIG_TOP_LEVEL_VALIDATORS",
    "_CODEX_THREAD_CONFIG_WARNED_KEYS",
}
tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
selected = []
found = set()
for node in tree.body:
    if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
        selected.append(node)
        found.add(node.name)
    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {item.id for item in targets if isinstance(item, ast.Name)}
        if names & CONSTANTS:
            assert names <= CONSTANTS
            selected.append(node)
            found.update(names)
assert found == FUNCTIONS | CONSTANTS
code = compile(ast.fix_missing_locations(ast.Module(body=[
    ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
    *selected,
], type_ignores=[])), str(SOURCE), "exec")


class CodexSubagentConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Path(self.temporary.name) / "codex-settings.json"
        self.transport = {"mcp_servers.fixture.url": "http://127.0.0.1:1/fixture"}
        self.ns = {
            "json": json, "Path": Path, "logger": logging.getLogger(__name__),
            "CODEX_SETTINGS_FILE": self.settings,
            "CODEX_NONINTERACTIVE_APPROVAL_POLICY": "never",
            "CODEX_DEFAULT_SANDBOX_MODE": "workspace-write",
            "CODEX_PROVIDER_MCP_NAME": "fixture",
            "codex_runtime_settings": Mock(return_value=("", "", "")),
            "codex_provider_mcp_config": Mock(return_value=self.transport),
            "codex_app_server_service_tier": lambda value: value,
            "codex_thread_instructions": Mock(return_value="fixture instructions"),
            "codex_goals_cli_args": Mock(return_value=[]),
            "CODEX_BIN": "fixture-codex-never-launched", "BACKEND_CODEX": "codex",
        }
        exec(code, self.ns)

    def write_settings(self, value):
        self.settings.write_text(json.dumps(value), encoding="utf-8")

    def params(self, session=None):
        return self.ns["codex_thread_params"](session or {"id": "fixture"}, "/fixture")

    def test_unconfigured_thread_only_adds_transport_not_subagent_settings(self):
        self.assertEqual(self.params()["config"], self.transport)
        self.assertEqual(self.ns["CODEX_THREAD_CONFIG_DEFAULTS"], {})
        self.write_settings({"goals_enabled": True})
        self.assertEqual(self.params()["config"], self.transport)

    def test_explicit_operator_limit_and_chat_override_are_preserved(self):
        self.write_settings({"thread_config": {"agents": {
            "max_concurrent_threads_per_session": 12, "default_subagent_model": "fixture-model",
        }}})
        self.assertEqual(self.params()["config"]["agents.max_concurrent_threads_per_session"], 12)
        params = self.params({"id": "fixture", "codex_config_overrides": {"agents": {
            "max_concurrent_threads_per_session": 20,
        }}})
        self.assertEqual(params["config"], {**self.transport,
            "agents.max_concurrent_threads_per_session": 20,
            "agents.default_subagent_model": "fixture-model",
        })

    def test_legacy_alias_is_honored_but_explicit_canonical_wins(self):
        sanitize = self.ns["sanitize_codex_thread_config"]
        self.assertEqual(sanitize({"agents": {"max_threads": 17}}, source="fixture"),
                         {"agents": {"max_concurrent_threads_per_session": 17}})
        self.assertEqual(sanitize({"agents": {"max_threads": 17,
            "max_concurrent_threads_per_session": 23}}, source="fixture"),
            {"agents": {"max_concurrent_threads_per_session": 23}})

    def test_invalid_limits_do_not_restore_hidden_default(self):
        for invalid in (True, False, 0, -1, "8", None):
            with self.subTest(invalid=invalid):
                self.write_settings({"thread_config": {"agents": {
                    "max_concurrent_threads_per_session": invalid,
                }}})
                self.assertEqual(self.params()["config"], self.transport)

    def test_explicit_subagents_disabled_is_not_removed(self):
        self.write_settings({"thread_config": {"agents": {"enabled": False}}})
        self.assertEqual(self.params()["config"], {**self.transport, "agents.enabled": False})

    def test_malformed_settings_do_not_inject_limit_or_mutate_defaults(self):
        self.settings.write_text("{broken", encoding="utf-8")
        first = self.ns["read_codex_thread_config_overrides"]()
        first["agents"] = {"max_concurrent_threads_per_session": 99}
        self.assertEqual(self.ns["read_codex_thread_config_overrides"](), {})
        self.assertEqual(self.ns["CODEX_THREAD_CONFIG_DEFAULTS"], {})

    def test_legacy_exec_authority_isolation_is_still_explicit(self):
        build = self.ns["build_codex_cmd"]
        args = ("fixture", {"id": "fixture"}, "fixture prompt", Path("/fixture/manifest"))
        ordinary = build(*args)
        guarded = build(*args, disable_provider_subagents=True)
        self.assertFalse(any(value.startswith("agents.") for value in ordinary))
        self.assertIn("agents.enabled=false", guarded)
        self.assertIn("agents.max_concurrent_threads_per_session=1", guarded)


# Reuse the existing settings cases without importing their server-bound module.
settings_source = SOURCE.with_name("test_wedge_codex_config.py")
settings_tree = ast.parse(settings_source.read_text(encoding="utf-8"))
settings_class = next(node for node in settings_tree.body if isinstance(node, ast.ClassDef)
                      and node.name == "CodexThreadConfigSettingsTests")
settings_ns = {"unittest": unittest, "tempfile": tempfile, "Path": Path, "json": json,
               "DEFAULT_CONFIG": {}}
exec(code, settings_ns := {**settings_ns, "logger": logging.getLogger(__name__)})
settings_ns["agent_server"] = SimpleNamespace(**settings_ns)
exec(compile(ast.Module(body=[settings_class], type_ignores=[]), str(settings_source), "exec"), settings_ns)
CodexThreadConfigSettingsTests = settings_ns["CodexThreadConfigSettingsTests"]


if __name__ == "__main__":
    unittest.main()
