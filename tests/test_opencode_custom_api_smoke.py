"""Opt-in real CLI/runner test against a synthetic loopback model API.

Set OPENCODE_TEST_EXECUTABLE to an installed OpenCode executable to run.
Uses a disposable HOME/workspace and synthetic key: no native credentials,
real inference, existing chats or external gateway are involved. This tests
the actual adapter wire behavior, not live-model or desktop UI acceptance.
"""
import asyncio
import json
import os
from pathlib import Path
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import agent_server
import provider_connections as connections
from tests import test_run_opencode as runner_fixture


EXECUTABLE = os.environ.get("OPENCODE_TEST_EXECUTABLE", "")


@unittest.skipUnless(EXECUTABLE, "Set OPENCODE_TEST_EXECUTABLE for real CLI smoke tests")
class OpenCodeCustomAPISmokeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = runner_fixture.RunOpenCodeTests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.root = Path(self.fixture.tempdir.name)
        self.store = connections.ConnectionStore(self.root / "private")
        self.calls = []
        self.mode = "ok"
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                case.calls.append((self.path, bool(body.get("stream"))))
                code, content_type = 200, "application/json"
                if self.path != "/v1/messages" or (case.mode == "auth" and body.get("stream")):
                    code = 401
                    payload = json.dumps({"type": "error", "error": {
                        "type": "authentication_error", "message": "Synthetic rejection"}}).encode()
                elif not body.get("stream"):
                    payload = json.dumps({"type": "message", "content": [{"type": "text", "text": "OK"}]}).encode()
                elif case.mode == "html":
                    content_type, payload = "text/html", b"<html>Gateway homepage</html>"
                else:
                    content_type = "text/event-stream"
                    events = [
                        {"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant",
                         "model": body["model"], "content": [], "stop_reason": None, "stop_sequence": None,
                         "usage": {"input_tokens": 1, "output_tokens": 0}}},
                        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello from the test gateway."}},
                        {"type": "content_block_stop", "index": 0},
                        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 8}},
                        {"type": "message_stop"},
                    ]
                    payload = "".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode()
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_http)

    def close_http(self):
        self.http.shutdown()
        self.thread.join()
        self.http.server_close()

    async def run_case(self, suffix=""):
        value = {"base_url": f"http://127.0.0.1:{self.http.server_port}" + suffix,
                 "api_key": "synthetic-smoke-key", "model": "test-model", "protocol": "anthropic",
                 "auth_header": "x-api-key", "expected_revision": 0}
        self.assertEqual(await connections.probe(value), "verified")
        self.store.write("opencode", 0, value, "verified")
        chat = {"backend": "opencode", "provider_connection": "custom", "model": "test-model"}
        chat["provider_connection_revision"] = self.store.bind(chat)["credential_id"]
        self.fixture.session.update(chat)
        home = self.root / "home"
        home.mkdir()
        environment = {"PATH": os.environ["PATH"], "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
            "XDG_CACHE_HOME": str(home / "cache"), "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1", "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
            "OPENCODE_CONFIG_CONTENT": '{"permission":{"*":"deny"},"autoupdate":false}'}
        with patch.object(agent_server, "PROVIDER_CONNECTION_STORE", self.store), \
             patch.object(agent_server, "agent_runner_env", return_value=environment), \
             patch.object(runner_fixture, "_write_fake_cli", return_value=Path(EXECUTABLE)):
            events = await asyncio.wait_for(self.fixture._run("", prompt="Say hello. Do not use tools."), 40)
        self.assertTrue(any(stream for _, stream in self.calls))
        self.assertEqual({path for path, _ in self.calls}, {"/v1/messages"})
        self.assertNotIn(self.fixture.session_id, agent_server.ACTIVE)
        self.assertNotIn(self.fixture.session_id, agent_server.BUSY_SESSIONS)
        return events

    async def test_root_url_round_trip(self):
        events = await self.run_case()
        terminal = [e for e in events if e["type"] == "turn_finished"][-1]
        self.assertFalse(terminal["is_error"])
        self.assertIn("Hello from the test gateway.", terminal["result_text"])

    async def test_explicit_v1_round_trip(self):
        events = await self.run_case("/v1")
        self.assertFalse([e for e in events if e["type"] == "turn_finished"][-1]["is_error"])

    async def test_html_200_stops_with_actionable_error(self):
        self.mode = "html"
        events = await self.run_case()
        error = [e for e in events if e["type"] == "error"][-1]
        self.assertIn("valid completion", error["message"])
        self.assertNotIn("did not exit cleanly", error["message"])
        self.assertTrue([e for e in events if e["type"] == "turn_finished"][-1]["is_error"])

    async def test_auth_failure_preserves_http_status(self):
        self.mode = "auth"
        events = await self.run_case()
        self.assertIn("HTTP 401", [e for e in events if e["type"] == "error"][-1]["message"])
