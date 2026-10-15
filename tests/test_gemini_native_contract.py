"""Opt-in REAL CLI / loopback fake API contract, not Google account acceptance.

Run with AGENTSDOCK_TEST_GEMINI_BIN pointing at a pinned disposable CLI.
No real credentials or user history. Network requests terminate on loopback.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from gemini_acp_client import GeminiACPClient, session_models
from gemini_runtime import build_environment
from gemini_history import replay_snapshot


class API(BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *_args): pass
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 4 * 1024 * 1024:
            self.send_error(413); return
        self.rfile.read(length)
        self.calls.append(self.path)
        result = {"candidates": [{"content": {"role": "model", "parts": [{"text": "Gemini local contract OK"}]},
                    "finishReason": "STOP", "index": 0}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 4, "totalTokenCount": 5}}
        stream = "streamGenerateContent" in self.path
        raw = ("data: " + json.dumps(result) + "\n\n").encode() if stream else json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)


@unittest.skipUnless(os.environ.get("AGENTSDOCK_TEST_GEMINI_BIN"), "optional pinned native CLI contract")
class NativeContract(unittest.IsolatedAsyncioTestCase):
    async def test_real_cli_new_prompt_resume_and_cleanup(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), API)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); cwd = root / "project"; cwd.mkdir()
                base = {"PATH": os.environ["PATH"], "HOME": str(root / "native"), "NO_PROXY": "*"}
                env = build_environment(base, cwd=str(cwd), api_home=root / "api",
                    api_key="local-fixture-not-a-real-key", base_url=f"http://127.0.0.1:{server.server_port}")
                events = []
                async def update(value): events.append(value)
                provider_id = None
                for _ in range(2):
                    client = GeminiACPClient(on_update=update)
                    try:
                        hello = await client.start([os.environ["AGENTSDOCK_TEST_GEMINI_BIN"], "--acp", "--model", "gemini-2.5-flash"], cwd=str(cwd), env=env)
                        self.assertEqual(hello["protocolVersion"], 1)
                        previous = len(events)
                        replay = replay_snapshot(env, str(cwd), provider_id) if provider_id else None
                        opened = await client.open_session(cwd=str(cwd), session_id=provider_id, replay=replay)
                        self.assertEqual(len(events), previous, "history replay must not create live user/assistant turns")
                        self.assertTrue(session_models(opened)["models"])
                        provider_id = client.session_id
                        result = await client.prompt([{"type": "text", "text": "Reply OK; use no tools."}], timeout=30)
                        self.assertEqual(result["stopReason"], "end_turn")
                        self.assertEqual([e["text"] for e in events[previous:] if e["type"] == "text"], ["Gemini local contract OK"])
                    finally:
                        await client.close()
                        self.assertIsNotNone(client.process.returncode)
                self.assertTrue(list((root / "api" / ".gemini" / "tmp").rglob("session-*")))
                self.assertTrue(API.calls)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
