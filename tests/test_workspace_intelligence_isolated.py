"""Disposable LSP bridge tests; never import the server or open installed state."""
from __future__ import annotations

import ast
import asyncio
import base64
from contextlib import suppress
import hmac
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.testclient import TestClient

import workspace_intelligence as intelligence


TOKEN = "disposable-intelligence-test-token"
TOKEN_PROTOCOL = "agentsdock-token." + base64.urlsafe_b64encode(TOKEN.encode()).decode().rstrip("=")
PROTOCOLS = [intelligence.WEBSOCKET_PROTOCOL, TOKEN_PROTOCOL]


def auth_helpers():
    # Exercise the existing native boundary without importing live state,
    # starting provider managers, or replacing authentication with a mock.
    selected = {
        "decoded_exact_header_secret", "request_exact_native_token_header_authorized",
        "privileged_native_browser_request_forbidden", "require_native_admin_control",
        "port_tunnel_requested_protocols", "websocket_requested_protocols",
        "port_tunnel_websocket_authorized",
    }
    source = Path(__file__).resolve().parents[1] / "agent_server.py"
    tree = ast.parse(source.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in selected]
    namespace = {"Request": Request, "WebSocket": WebSocket, "HTTPException": HTTPException,
                 "AGENT_TOKEN": TOKEN, "PORT_TUNNEL_TOKEN_PROTOCOL_PREFIX": "agentsdock-token.",
                 "base64": base64, "hmac": hmac, "re": re,
                 "token_matches": lambda value: isinstance(value, str) and hmac.compare_digest(value, TOKEN)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    return namespace


AUTH = auth_helpers()


class MemorySocket:
    def __init__(self):
        self.query_params = {}
        self.incoming = asyncio.Queue()
        self.outgoing = asyncio.Queue()
        self.accepted = None
        self.closed = None

    async def accept(self, *, subprotocol=None):
        self.accepted = subprotocol

    async def close(self, *, code):
        self.closed = code
        await self.outgoing.put({"closed": code})

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, value):
        await self.outgoing.put(value)

    async def send(self, value):
        await self.incoming.put({"type": "websocket.receive", "text": json.dumps(value)})

    async def receive_json(self):
        return await asyncio.wait_for(self.outgoing.get(), timeout=30)


FAKE_SERVER = r'''
import json, os, sys, time
buffer = ""
def send(value):
    data = json.dumps(value).encode()
    sys.stdout.buffer.write(b"Content-Length: " + str(len(data)).encode() + b"\r\n\r\n" + data)
    sys.stdout.buffer.flush()
while True:
    header = sys.stdin.buffer.readline()
    if not header:
        break
    size = int(header.split(b":", 1)[1])
    while sys.stdin.buffer.readline() != b"\r\n":
        pass
    message = json.loads(sys.stdin.buffer.read(size))
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"params": message["params"], "pid": os.getpid(), "cwd": os.getcwd()}})
        send({"jsonrpc": "2.0", "id": "server-config", "method": "workspace/configuration", "params": {"items": []}})
    elif method == "textDocument/didOpen":
        buffer = message["params"]["textDocument"]["text"]
    elif method == "test/state":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"pid": os.getpid(), "buffer": buffer}})
    elif method == "test/fail":
        sys.exit(3)
    elif method == "test/delayed":
        send({"jsonrpc": "2.0", "method": "test/started"})
        while not os.path.exists("release-delayed"):
            time.sleep(0.005)
        send({"jsonrpc": "2.0", "id": message["id"], "result": "stale workspace result"})
    elif "id" in message:
        send({"jsonrpc": "2.0", "method": "test/response", "params": message})
'''


def executable(path: Path, content: str = FAKE_SERVER):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{content}")
    path.chmod(0o755)


def process_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class DiscoveryAndAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="intelligence-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "project space"
        self.root.mkdir()
        self.runtime = intelligence.WorkspaceIntelligence(
            workspace_root=lambda session: ({}, self.root), environment=lambda: {"PATH": ""})

    def test_status_is_launch_free_and_project_installation_wins(self):
        project = self.root / ".venv/bin/pyright-langserver"
        executable(project)
        with patch.object(intelligence.asyncio, "create_subprocess_exec") as spawn:
            result = self.runtime.status("session")
        spawn.assert_not_called()
        self.assertEqual(result["root"], str(self.root))
        self.assertEqual(result["root_uri"], self.root.as_uri())
        self.assertIn("%20", result["root_uri"])
        self.assertEqual(result["languages"][0], {"id": "python", "available": True})
        self.assertFalse(result["languages"][1]["available"])
        self.assertIn("install_hint", result["languages"][1])
        self.assertEqual(intelligence.language_command(self.root, "python", {"PATH": "/bin"}), (str(project), "--stdio"))

    def test_root_changed_during_discovery_is_rejected(self):
        alternative = self.root.parent
        with patch.object(self.runtime, "workspace_root", side_effect=[({}, self.root), ({}, alternative)]):
            with self.assertRaises(HTTPException) as raised:
                self.runtime.status("session")
        self.assertEqual(raised.exception.status_code, 409)

    def app(self):
        app = FastAPI()
        intelligence.register_workspace_intelligence_routes(
            app, authorize=AUTH["require_native_admin_control"],
            authorize_websocket=lambda ws: not AUTH["privileged_native_browser_request_forbidden"](ws)
            and AUTH["port_tunnel_websocket_authorized"](ws),
            protocols=AUTH["port_tunnel_requested_protocols"],
            workspace_root=lambda session: ({}, self.root), environment=lambda: {"PATH": ""})
        return app

    def test_http_status_requires_exact_native_header(self):
        with TestClient(self.app()) as client:
            path = "/api/sessions/chat/workspace/intelligence"
            self.assertEqual(client.get(path, headers={"x-agentsdock-token": TOKEN}).status_code, 200)
            for headers, expected in (({}, 401), ({"authorization": f"Bearer {TOKEN}"}, 401),
                                      ({"x-agentsdock-token": TOKEN, "origin": "https://example.invalid"}, 403),
                                      ({"x-agentsdock-token": TOKEN, "sec-fetch-mode": "cors"}, 403),
                                      ({"x-agentsdock-token": TOKEN, "x-zenithdock-token": TOKEN}, 401)):
                with self.subTest(headers=list(headers)):
                    self.assertEqual(client.get(path, headers=headers).status_code, expected)

    def test_websocket_rejects_browser_ambiguous_tokens_and_client_launch_options(self):
        path = "/api/sessions/chat/workspace/intelligence/python"
        cases = [([], {}, "", 4401), (PROTOCOLS, {"origin": "https://example.invalid"}, "", 4401),
                 (PROTOCOLS, {"authorization": f"Bearer {TOKEN}"}, "", 4401),
                 (PROTOCOLS + [TOKEN_PROTOCOL], {}, "", 4401),
                 (PROTOCOLS, {}, "?token=" + TOKEN, 4401),
                 ([TOKEN_PROTOCOL], {}, "", 4406), (PROTOCOLS, {}, "?command=arbitrary", 4400)]
        with TestClient(self.app()) as client, patch.object(intelligence.asyncio, "create_subprocess_exec") as spawn:
            for protocols, headers, query, expected in cases:
                with self.subTest(protocols=len(protocols), headers=list(headers), query=bool(query)):
                    with client.websocket_connect(path + query, subprotocols=protocols, headers=headers) as ws:
                        self.assertEqual(ws.receive()["code"], expected)
            with client.websocket_connect(path, subprotocols=PROTOCOLS) as ws:
                self.assertEqual(ws.receive_json()["type"], "intelligence_error")
                self.assertEqual(ws.receive()["code"], 1011)
            with client.websocket_connect(path.replace("python", "arbitrary"), subprotocols=PROTOCOLS) as ws:
                self.assertEqual(ws.receive_json()["type"], "intelligence_error")
                self.assertEqual(ws.receive()["code"], 4404)
        spawn.assert_not_called()


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="intelligence-bridge-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        executable(self.root / ".venv/bin/pyright-langserver")
        self.runtime = intelligence.WorkspaceIntelligence(
            workspace_root=lambda session: ({}, self.root), environment=lambda: {**os.environ, "HOME": str(self.root)})
        self.addAsyncCleanup(self.runtime.shutdown)

    async def connect(self, language="python"):
        ws = MemorySocket()
        task = asyncio.create_task(self.runtime.serve("chat", language, ws,
                                   authorize=lambda ws: True, protocols=lambda ws: PROTOCOLS))
        return ws, task

    async def test_framing_uses_utf8_bytes_and_handles_large_split_payload(self):
        message = {"jsonrpc": "2.0", "method": "example", "params": "é" * (1024 * 1024 + 1)}
        data = json.dumps(message, ensure_ascii=False).encode()
        stream = asyncio.StreamReader()
        encoded = f"Content-Length: {len(data)}\r\nContent-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n".encode() + data
        task = asyncio.create_task(intelligence.read_message(stream))
        for start in range(0, len(encoded), 731):
            stream.feed_data(encoded[start:start + 731])
            await asyncio.sleep(0)
        self.assertEqual(await task, message)
        stream.feed_eof()
        self.assertIsNone(await intelligence.read_message(stream))
        malformed = asyncio.StreamReader()
        malformed.feed_data(b"Content-Length: 2\r\nContent-Length: 2\r\n\r\n{}")
        with self.assertRaises(intelligence.IntelligenceError):
            await intelligence.read_message(malformed)

    async def initialize(self, ws):
        await ws.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "rootUri": "file:///wrong", "processId": 123, "initializationOptions": {"tsserver": {"path": "arbitrary"}}}})
        result = (await ws.receive_json())["result"]
        self.assertEqual(result["cwd"], str(self.root))
        self.assertEqual(result["params"]["rootUri"], self.root.as_uri())
        self.assertEqual(result["params"]["workspaceFolders"][0]["uri"], self.root.as_uri())
        self.assertIsNone(result["params"]["processId"])
        self.assertNotIn("initializationOptions", result["params"])
        request = await ws.receive_json()
        self.assertEqual(request["method"], "workspace/configuration")
        await ws.send({"jsonrpc": "2.0", "id": request["id"], "result": []})
        self.assertEqual((await ws.receive_json())["params"]["result"], [])
        return result["pid"]

    async def test_editor_processes_isolate_unsaved_buffers_and_close_independently(self):
        first, first_owner = await self.connect()
        first_pid = await self.initialize(first)
        second, second_owner = await self.connect()
        second_pid = await self.initialize(second)
        self.assertNotEqual(first_pid, second_pid)
        for ws, text in ((first, "unsaved first"), (second, "unsaved second")):
            await ws.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {"textDocument": {"text": text}}})
        await first.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(first_owner, 5)
        self.assertFalse(process_exists(first_pid))
        self.assertTrue(process_exists(second_pid))
        await second.send({"jsonrpc": "2.0", "id": 2, "method": "test/state"})
        self.assertEqual((await second.receive_json())["result"]["buffer"], "unsaved second")
        await self.runtime.shutdown()
        self.assertFalse(process_exists(second_pid))
        self.assertTrue(second_owner.done())
        self.assertEqual(self.runtime.connections, set())

    async def test_language_server_failure_reports_error_and_reaps_owned_process(self):
        ws, owner = await self.connect()
        pid = await self.initialize(ws)
        await ws.send({"jsonrpc": "2.0", "method": "test/fail"})
        self.assertIn("status 3", (await ws.receive_json())["message"])
        await asyncio.wait_for(owner, 5)
        self.assertEqual(ws.closed, 1011)
        self.assertFalse(process_exists(pid))

    async def test_typescript_semantic_option_does_not_allow_executable_paths(self):
        executable(self.root / "node_modules/.bin/typescript-language-server")
        ws, owner = await self.connect("typescript")
        await ws.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "initializationOptions": {"tsserver": {"useSyntaxServer": "never", "path": "/arbitrary"},
                                      "npmLocation": "/arbitrary", "plugins": [{"location": "/arbitrary"}]}}})
        result = (await ws.receive_json())["result"]
        self.assertEqual(result["params"]["initializationOptions"], {"tsserver": {"useSyntaxServer": "never"}})
        await ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(owner, 5)
        self.assertFalse(process_exists(result["pid"]))

    async def test_changed_workspace_closes_only_its_editor(self):
        ws, owner = await self.connect()
        pid = await self.initialize(ws)
        with patch.object(self.runtime, "workspace_root", return_value=({}, self.root.parent)):
            await ws.send({"jsonrpc": "2.0", "id": 2, "method": "test/state"})
            self.assertIn("working directory changed", (await ws.receive_json())["message"])
            await asyncio.wait_for(owner, 5)
        self.assertEqual(ws.closed, 4409)
        self.assertFalse(process_exists(pid))

    async def test_changed_workspace_discards_pending_server_result_and_reaps_process(self):
        ws, owner = await self.connect()
        pid = await self.initialize(ws)
        await ws.send({"jsonrpc": "2.0", "id": 2, "method": "test/delayed"})
        self.assertEqual((await ws.receive_json())["method"], "test/started")
        with patch.object(self.runtime, "workspace_root", return_value=({}, self.root.parent)):
            # Release a result already being computed by the old workspace,
            # without sending another client frame that could catch the change.
            (self.root / "release-delayed").touch()
            error = await ws.receive_json()
            self.assertEqual(error["type"], "intelligence_error")
            self.assertIn("working directory changed", error["message"])
            await asyncio.wait_for(owner, 5)
        self.assertEqual(await ws.receive_json(), {"closed": 4409})
        self.assertTrue(ws.outgoing.empty())
        self.assertFalse(process_exists(pid))

    async def test_cancel_during_spawn_and_repeated_cancel_still_reaps(self):
        real_spawn = asyncio.create_subprocess_exec
        created = asyncio.Event()
        release = asyncio.Event()
        children = []

        async def delayed_spawn(*args, **kwargs):
            child = await real_spawn(*args, **kwargs)
            children.append(child)
            created.set()
            await release.wait()
            return child

        with patch.object(intelligence.asyncio, "create_subprocess_exec", side_effect=delayed_spawn):
            _, owner = await self.connect()
            await asyncio.wait_for(created.wait(), 5)
            owner.cancel()
            await asyncio.sleep(0)
            owner.cancel()
            release.set()
            await asyncio.wait_for(asyncio.gather(owner, return_exceptions=True), 5)
        self.assertFalse(process_exists(children[0].pid))
        self.assertEqual(self.runtime.connections, set())

    async def test_cleanup_drains_full_pipes_and_reaps_only_the_owned_process_group(self):
        # A language server can be blocked writing while an editor disconnects.
        # Its full stdout/stderr must not keep subprocess transport cleanup alive.
        script = "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); os.write(1,b'x'*4000000); os.write(2,b'y'*4000000); time.sleep(60)"
        owned = await asyncio.create_subprocess_exec(
            sys.executable, "-c", script, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        unrelated = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True)
        try:
            assert owned.stdout is not None
            self.assertEqual(await owned.stdout.readexactly(1), b"x")
            await asyncio.wait_for(intelligence.stop_process(owned), 5)
            self.assertFalse(process_exists(owned.pid))
            self.assertTrue(process_exists(unrelated.pid))
        finally:
            if unrelated.returncode is None:
                unrelated.kill()
            await unrelated.wait()
            if owned.returncode is None:
                owned.kill()
                await owned.wait()


class RealLanguageServerTests(unittest.IsolatedAsyncioTestCase):
    """Set INTELLIGENCE_TEST_BIN_DIR to an isolated npm .bin directory to opt in."""
    connect = BridgeTests.connect

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="intelligence-real-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        path = os.environ.get("INTELLIGENCE_TEST_BIN_DIR", "")
        self.environment = {**os.environ, "HOME": str(self.root),
                            "PATH": path + os.pathsep + os.environ.get("PATH", "")}
        self.runtime = intelligence.WorkspaceIntelligence(
            workspace_root=lambda session: ({}, self.root), environment=lambda: self.environment)
        self.addAsyncCleanup(self.runtime.shutdown)
        self.messages = []

    async def response(self, ws, identity):
        while True:
            message = await ws.receive_json()
            self.messages.append(message)
            self.assertNotIn("closed", message)
            self.assertNotEqual(message.get("type"), "intelligence_error", str(message))
            if "method" in message and "id" in message:
                items = message.get("params", {}).get("items", [])
                result = [{"analysis": {"diagnosticMode": "workspace"}} if item.get("section") == "python" else {}
                          for item in items] if message["method"] == "workspace/configuration" else None
                await ws.send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            elif message.get("id") == identity:
                self.assertNotIn("error", message, str(message))
                return message["result"]

    async def exercise_language(self, language, suffix, helper, source):
        if intelligence.language_command(self.root, language, self.environment) is None:
            self.skipTest("Language server not installed; use an isolated INTELLIGENCE_TEST_BIN_DIR.")
        (self.root / f"helpers.{suffix}").write_text(helper)
        independent = self.root / f"independent.{suffix}"
        independent.write_text('def independentFunction():\n    return 7\n' if language == "python"
                               else 'export function independentFunction() { return 7; }\n')
        # The complete editor document intentionally differs from disk.
        (self.root / f"main.{suffix}").write_text("# stale file\n" if language == "python" else "// stale file\n")
        uri = (self.root / f"main.{suffix}").as_uri()
        ws, owner = await self.connect(language)
        await ws.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "capabilities": {"workspace": {"configuration": True, "workspaceFolders": True},
                             "textDocument": {"publishDiagnostics": {"relatedInformation": True},
                                              "completion": {"completionItem": {"snippetSupport": True}}}}}})
        self.assertIn("capabilities", await self.response(ws, 1))
        await ws.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        await ws.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": uri, "languageId": language, "version": 1, "text": source}}})
        await ws.send({"jsonrpc": "2.0", "id": 2, "method": "textDocument/completion", "params": {
            "textDocument": {"uri": uri}, "position": {"line": 3, "character": 3}}})
        completions = await self.response(ws, 2)
        items = completions.get("items", []) if isinstance(completions, dict) else completions
        self.assertTrue(any(item["label"] == "greet" for item in items), str(items)[:500])
        await ws.send({"jsonrpc": "2.0", "id": 3, "method": "textDocument/hover", "params": {
            "textDocument": {"uri": uri}, "position": {"line": 2, "character": 2}}})
        self.assertIn("greet", str(await self.response(ws, 3)))
        await ws.send({"jsonrpc": "2.0", "id": 4, "method": "textDocument/definition", "params": {
            "textDocument": {"uri": uri}, "position": {"line": 2, "character": 2}}})
        self.assertIn(f"helpers.{suffix}", str(await self.response(ws, 4)))
        def has_diagnostic():
            return any(message.get("method") == "textDocument/publishDiagnostics"
                       and message.get("params", {}).get("uri") == uri
                       and any(item["range"]["start"]["line"] == 1
                               for item in message["params"].get("diagnostics", []))
                       for message in self.messages)
        while not has_diagnostic():
            message = await ws.receive_json()
            self.messages.append(message)
            self.assertNotIn("closed", message)
        if language == "python":
            # Project discovery/analysis is asynchronous. Its own diagnostic
            # event proves this unopened file is indexed, without polling.
            while not any(message.get("method") == "textDocument/publishDiagnostics"
                          and message.get("params", {}).get("uri") == independent.as_uri()
                          for message in self.messages):
                self.messages.append(await ws.receive_json())
        else:
            # tsserver creates a project lazily; this request is a barrier for
            # its initial seed's project loading, not a delay or retry loop.
            await ws.send({"jsonrpc": "2.0", "id": 5, "method": "textDocument/documentSymbol",
                           "params": {"textDocument": {"uri": uri}}})
            await self.response(ws, 5)
        await ws.send({"jsonrpc": "2.0", "id": 6, "method": "workspace/symbol", "params": {"query": "independentFunction"}})
        symbols = await self.response(ws, 6)
        self.assertTrue(any(symbol.get("name") == "independentFunction"
                            and symbol.get("location", {}).get("uri") == independent.as_uri()
                            for symbol in symbols), str(symbols))
        await ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(owner, 5)
        self.assertEqual(self.runtime.connections, set())

    async def test_real_python_project_completions_hover_definition_and_unsaved_diagnostics(self):
        (self.root / "pyrightconfig.json").write_text(json.dumps({"typeCheckingMode": "basic", "include": ["."]}))
        await self.exercise_language("python", "py", 'def greet(name: str) -> str:\n    return "Hello " + name\n',
                                     'from helpers import greet\nvalue: int = "bad"\ngreet("world")\ngre\n')

    async def test_real_typescript_project_completions_hover_definition_and_unsaved_diagnostics(self):
        (self.root / "tsconfig.json").write_text(json.dumps({"compilerOptions": {"strict": True}, "include": ["*.ts"]}))
        await self.exercise_language("typescript", "ts", 'export function greet(name: string): string { return "Hello " + name; }\n',
                                     'import { greet } from "./helpers";\nconst value: number = "bad";\ngreet("world");\ngre\n')

    async def test_real_typescript_fresh_symbols_include_unopened_independent_file(self):
        if intelligence.language_command(self.root, "typescript", self.environment) is None:
            self.skipTest("Language server not installed; use an isolated INTELLIGENCE_TEST_BIN_DIR.")
        (self.root / "tsconfig.json").write_text('{"include":["*.ts"]}')
        seed = self.root / "main.ts"
        seed.write_text('export function mainFunction() { return 1; }\n')
        independent = self.root / "independent.ts"
        independent.write_text('export function independentFunction() { return 2; }\n')
        ws, owner = await self.connect("typescript")
        await ws.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "capabilities": {}, "initializationOptions": {"tsserver": {"useSyntaxServer": "never"}}}})
        await self.response(ws, 1)
        await ws.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        await ws.send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": seed.as_uri(), "languageId": "typescript", "version": 0, "text": seed.read_text()}}})
        # The first query must use the semantic project. The default syntax
        # server can return an empty list while that project is still loading.
        await ws.send({"jsonrpc": "2.0", "id": 2, "method": "textDocument/documentSymbol",
                       "params": {"textDocument": {"uri": seed.as_uri()}}})
        await self.response(ws, 2)
        await ws.send({"jsonrpc": "2.0", "id": 3, "method": "workspace/symbol", "params": {"query": "independentFunction"}})
        symbols = await self.response(ws, 3)
        self.assertTrue(any(symbol.get("name") == "independentFunction"
                            and symbol.get("location", {}).get("uri") == independent.as_uri()
                            for symbol in symbols), str(symbols))
        await ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(owner, 5)


if __name__ == "__main__":
    unittest.main()
