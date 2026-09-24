"""Normal and custom readiness stay separate; never import/start the server."""
from __future__ import annotations
import ast
import asyncio
import json
from pathlib import Path
import re
import subprocess
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from fastapi import HTTPException
import codex_provider

SOURCE = (Path(__file__).resolve().parents[1] / "agent_server.py")
NAMES = {"probe_runtime", "runtime_diagnostic_payload", "safe_runtime_version", "auth_failure_text",
         "discover_runtime_catalog", "ensure_runtime_available"}
NODES = [node for node in ast.parse(SOURCE.read_text()).body
         if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES]
assert {node.name for node in NODES} == NAMES
CODE = compile(ast.Module(body=NODES, type_ignores=[]), str(SOURCE), "exec")


class CodexProviderReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.selected = {"base_url": "https://gateway.example/v1", "model": "openai/openai/gpt-6-astra"}
        self.store = SimpleNamespace(selection=Mock(side_effect=RuntimeError("must not read custom key for normal auth")),
            for_session=Mock(return_value=self.selected), catalog=Mock(return_value={
                **self.selected, "configured": True, "available": True}))
        self.command = Mock(return_value=subprocess.CompletedProcess([], 0, "Codex 0.153.3", ""))
        self.ns = {"Any": object, "Path": Path, "re": re, "time": time, "json": json, "subprocess": subprocess,
            "asyncio": asyncio, "HTTPException": HTTPException, "codex_provider": codex_provider,
            "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude", "BACKEND_CURSOR": "cursor",
            "CODEX_PROVIDER_STORE": self.store, "CODEX_TRANSPORT": "app-server", "CODEX_TRANSPORT_EXEC": "exec",
            "runtime_display_name": lambda backend: backend, "runtime_action": lambda *args, **kwargs: None,
            "now_iso": lambda: "2026-09-17T00:00:00Z", "runtime_executable": lambda backend: backend,
            "runner_env": lambda: {"PATH": "/synthetic"}, "shutil": SimpleNamespace(which=lambda *args, **kwargs: "/synthetic/codex"),
            "runtime_command": self.command, "logger": Mock(),
            "runtime_diagnostic": Mock(return_value={"status": "unauthenticated", "installed": True}),
            "public_runtime_diagnostic": lambda value: value,
            "runtime_option": lambda value, label: {"value": value, "label": label},
            "CURSOR_PERMISSION_MODES": [], "CURSOR_DEFAULT_PERMISSION_MODE": "default"}
        exec(CODE, self.ns)

    def test_normal_codex_uses_its_login_despite_saved_or_broken_optional_provider(self):
        result = self.ns["probe_runtime"]("codex")
        self.assertEqual(result["status"], "ready")
        self.command.assert_called_with(["/synthetic/codex", "login", "status"])
        self.store.selection.assert_not_called()

    def test_normal_codex_unauthenticated_state_is_not_masked_by_custom_key(self):
        self.command.side_effect = [subprocess.CompletedProcess([], 0, "Codex 0.153.3", ""),
                                   subprocess.CompletedProcess([], 1, "", "Not logged in")]
        self.assertEqual(self.ns["probe_runtime"]("codex")["status"], "unauthenticated")
        self.store.selection.assert_not_called()

    async def test_custom_admission_uses_own_key_independently_of_normal_login(self):
        result = await self.ns["ensure_runtime_available"]("codex", session={"codex_provider": "custom"})
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["authenticated"])
        with self.assertRaises(HTTPException):
            await self.ns["ensure_runtime_available"]("codex", session={"codex_provider": "default"})
        self.store.for_session.assert_called_once()
        self.command.assert_not_called()

    async def test_missing_or_unsupported_custom_runtime_fails_closed(self):
        self.ns["CODEX_TRANSPORT"] = "exec"
        with self.assertRaises(HTTPException):
            await self.ns["ensure_runtime_available"]("codex", session={"codex_provider": "custom"})
        self.ns["CODEX_TRANSPORT"] = "app-server"
        self.ns["runtime_diagnostic"].return_value = {"status": "missing", "installed": False}
        with self.assertRaises(HTTPException):
            await self.ns["ensure_runtime_available"]("codex", session={"codex_provider": "custom"})

    def test_catalog_adds_custom_metadata_without_replacing_normal_models_or_readiness(self):
        normal = {"models": [{"value": "ordinary-codex-model", "label": "Normal"}], "default_model": "ordinary-codex-model"}
        self.ns.update({"refresh_runtime_diagnostics": lambda **kwargs: {"codex": {"status": "unauthenticated", "installed": True}},
            "discover_codex_catalog": lambda: dict(normal), "parse_claude_help_catalog": lambda: {}})
        result = self.ns["discover_runtime_catalog"]()["backends"]["codex"]
        self.assertEqual(result["models"], normal["models"])
        self.assertEqual(result["default_model"], normal["default_model"])
        self.assertFalse(result["available"])
        self.assertTrue(result["custom_provider"]["available"])
        self.assertEqual(result["custom_provider"]["model"], self.selected["model"])
        self.store.catalog.assert_called_once_with(available=True)

    def test_claude_does_not_read_or_use_codex_provider_settings(self):
        self.command.side_effect = [subprocess.CompletedProcess([], 0, "Claude 1.0", ""),
                                   subprocess.CompletedProcess([], 0, '{"loggedIn":true}', "")]
        self.assertEqual(self.ns["probe_runtime"]("claude")["status"], "ready")
        self.store.selection.assert_not_called()
