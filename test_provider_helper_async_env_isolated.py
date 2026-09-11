"""Run actual Chats parsing/negotiation through the extracted helper launch seam."""
from __future__ import annotations

import ast
import asyncio
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

import agentsdock_chats


TREE = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
ROUTE = "route_" + "a" * 32
FUNCTIONS = {
    "execute_provider_tool", "agent_runner_env", "is_provider_runtime_env_name",
    "scrub_provider_runtime_environment", "validate_provider_runtime_env",
    "resolve_provider_tool_arguments", "provider_tool_argument_value",
}
CONSTANTS = {
    "PROVIDER_SECRET_ENV_NAMES", "PROVIDER_RUNTIME_ENV_EXACT_NAMES", "PROVIDER_RUNTIME_ENV_PREFIXES",
    "MAX_PROVIDER_RUNTIME_ENV_VARS", "MAX_PROVIDER_RUNTIME_ENV_KEY_CHARS",
    "MAX_PROVIDER_RUNTIME_ENV_VALUE_CHARS", "MAX_PROVIDER_RUNTIME_ENV_BYTES",
    "PROVIDER_TOOL_MAX_OUTPUT_BYTES", "PROVIDER_TOOL_TIMEOUT_SECONDS", "PROVIDER_CROSS_CHAT_ROUTE_ID_RE",
}


class ProviderHelperAsyncEnvTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in TREE.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in CONSTANTS for target in node.targets):
                nodes.append(node)
        self.namespace = {
            "Any": Any, "Path": Path, "sys": sys, "re": re, "json": json,
            "ProviderToolError": RuntimeError, "ProviderRuntimeContextError": ValueError,
            "SERVER_ROOT": Path("/qa/source"),
            "validate_provider_tool_input": lambda value: (value["helper"], value["arguments"], value.get("stdin", "")),
            "redact_provider_tool_output": lambda value, _path: value,
            "terminal_session_name": lambda _session: "qa-terminal",
            "codex_manifest_path": lambda _session: Path("/qa/manifest"),
            "provider_helper_server_origin": lambda: "http://127.0.0.1:1",
            "codex_provider_mcp_connection_host": lambda: "127.0.0.1",
            "add_provider_no_proxy_environment": lambda *_args: None,
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "<isolated-helper-env>", "exec"), self.namespace)
        def runner_env():
            environment = {"PATH": "/qa/bin", "AGENTSDOCK_CROSS_CHAT_MODE": "stale-inherited-mode"}
            self.namespace["scrub_provider_runtime_environment"](environment)
            return environment
        self.namespace["runner_env"] = runner_env
        self.runtime = {"AGENTSDOCK_CROSS_CHAT_MODE": "async_route_v1"}
        self.namespace["provider_tool_capability_snapshot"] = AsyncMock(
            side_effect=lambda *_args, **_kwargs: (Path("/qa/synthetic-authority"), dict(self.runtime)))
        self.environments = []
        self.payloads = []
        self.route_reads = []

        async def create_subprocess(*command, **options):
            self.environments.append(dict(options["env"]))
            def get_json(path, _authority):
                self.route_reads.append(path)
                return {"routes": [{"route_id": ROUTE, "available": True, "mode": "async_route_v1"}]}
            def post_json(path, payload, _authority):
                self.payloads.append(payload)
                self.assertEqual(payload.get("mode"), "async_route_v1")
                self.assertEqual(payload["action"], "instruction")
                self.assertNotIn("wait_for_response", payload)
                return {"ok": True, "route_id": ROUTE, "action": "instruction", "accepted": True,
                        "mode": "async_route_v1", "message_id": "handoff_" + "b" * 32, "duplicate": False}
            # No process, network request, authority-file read or provider call.
            # The real helper parser/handler receives exactly the launch env.
            with patch.dict(os.environ, options["env"], clear=True), \
                    patch.object(agentsdock_chats, "authority", return_value="synthetic-authority"), \
                    patch.object(agentsdock_chats, "get_json", side_effect=get_json), \
                    patch.object(agentsdock_chats, "post_json", side_effect=post_json):
                parsed = agentsdock_chats.parser().parse_args(list(command[2:]))
                result = parsed.handler(parsed)
            stdout, stderr = asyncio.StreamReader(), asyncio.StreamReader()
            stdout.feed_data(json.dumps(result).encode())
            stdout.feed_eof()
            stderr.feed_eof()
            return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=0,
                stdin=SimpleNamespace(write=lambda _data: None, drain=AsyncMock(), close=lambda: None),
                wait=AsyncMock(return_value=0))
        self.namespace["asyncio"] = SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": create_subprocess})

    async def invoke(self, arguments):
        result, failed = await self.namespace["execute_provider_tool"](
            "qa-chat", "qa-run", {"helper": "chats", "arguments": arguments}, backend="claude")
        self.assertFalse(failed)
        self.assertEqual(json.loads(result)["mode"], "async_route_v1")
        self.assertEqual(self.environments[-1]["AGENTSDOCK_CROSS_CHAT_MODE"], "async_route_v1")
        self.assertEqual(len(self.route_reads), 1)

    async def test_send_and_ask_negotiate_independent_messages(self):
        for command in ("send", "ask"):
            with self.subTest(command=command):
                self.route_reads.clear()
                await self.invoke([command, "--route", ROUTE, "--message", "Synthetic question"])

    async def test_respond_current_uses_the_fresh_inbound_pair_route(self):
        self.runtime.update({"AGENTSDOCK_CROSS_CHAT_RESPONSE_MODE": "async_route_v1",
                             "AGENTSDOCK_CROSS_CHAT_RESPONSE_ROUTE_ID": ROUTE})
        await self.invoke(["respond-current", "--message", "Synthetic response"])

    async def test_projection_is_confined_to_trusted_helper_not_ordinary_provider(self):
        ordinary = self.namespace["agent_runner_env"]("qa-chat")
        self.assertNotIn("AGENTSDOCK_CROSS_CHAT_MODE", ordinary)
        self.assertNotIn("AGENTSDOCK_CROSS_CHAT_RESPONSE_MODE", ordinary)
        sdk_builder = next(node for node in TREE.body if isinstance(node, ast.FunctionDef)
                           and node.name == "build_claude_sdk_options")
        calls = [node for node in ast.walk(sdk_builder) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "agent_runner_env"]
        self.assertEqual([len(node.args) for node in calls], [1])
        await self.invoke(["send", "--route", ROUTE, "--message", "Synthetic message"])
        self.assertNotIn("AGENTSDOCK_CROSS_CHAT_MODE", self.namespace["agent_runner_env"]("qa-chat"))


if __name__ == "__main__":
    unittest.main()
