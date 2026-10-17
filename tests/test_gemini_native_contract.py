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

from gemini_acp_client import GeminiACPClient, GeminiRPCError, session_models
from gemini_runtime import build_environment
from gemini_history import replay_snapshot


class API(BaseHTTPRequestHandler):
    calls = []
    def log_message(self, *_args): pass
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 4 * 1024 * 1024:
            self.send_error(413); return
        body = json.loads(self.rfile.read(length))
        self.calls.append(self.path)
        parts = [p for c in body.get("contents", []) for p in c.get("parts", [])]
        activate = any("FIXTURE_SKILL" in p.get("text", "") for p in parts) and not any("functionResponse" in p for p in parts)
        answer = [{"functionCall": {"name": "activate_skill", "args": {"name": "fixture-skill"}}}] if activate else [{"text": "Gemini local contract OK"}]
        # Auto invokes the native routing classifier before the visible reply.
        # Return its requested structured shape, not the ordinary chat fixture.
        generation = body.get("generationConfig", {})
        schema = generation.get("responseJsonSchema") or generation.get("responseSchema") or {}
        properties = schema.get("properties", {})
        if "model_choice" in properties:
            answer = [{"text": json.dumps({"reasoning": "Simple test", "model_choice": "flash"})}]
        elif "complexity_score" in properties:
            answer = [{"text": json.dumps({"complexity_reasoning": "Simple test", "complexity_score": 10})}]
        result = {"candidates": [{"content": {"role": "model", "parts": answer},
                    "finishReason": "STOP", "index": 0}], "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 4, "totalTokenCount": 5}}
        stream = "streamGenerateContent" in self.path
        raw = ("data: " + json.dumps(result) + "\n\n").encode() if stream else json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)


@unittest.skipUnless(os.environ.get("AGENTSDOCK_TEST_GEMINI_BIN"), "optional pinned native CLI contract")
class NativeContract(unittest.IsolatedAsyncioTestCase):
    async def test_native_auto_resolves_before_developer_api_request(self):
        class AutoAPI(API):
            calls = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), AutoAPI)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary); cwd = root / "project"; cwd.mkdir()
                env = build_environment({"PATH": os.environ["PATH"], "HOME": str(root / "native")},
                    cwd=str(cwd), api_home=root / "api", api_key="local-fixture-key",
                    base_url=f"http://127.0.0.1:{server.server_port}")
                events = []
                async def update(value): events.append(value)
                client = GeminiACPClient(on_update=update)
                try:
                    await client.start([os.environ["AGENTSDOCK_TEST_GEMINI_BIN"], "--acp", "--model", "auto"], cwd=str(cwd), env=env)
                    await client.open_session(cwd=str(cwd))
                    result = await client.prompt([{"type": "text", "text": "Reply OK only. No tools."}], timeout=45)
                    self.assertEqual(result["stopReason"], "end_turn")
                    self.assertEqual([e["text"] for e in events if e["type"] == "text"], ["Gemini local contract OK"])
                    self.assertTrue(any("streamGenerateContent" in path for path in AutoAPI.calls))
                    self.assertFalse(any("/models/auto" in path for path in AutoAPI.calls), AutoAPI.calls)
                finally:
                    await client.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

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
                        if previous == 0:
                            # Probe actual protocol support, not an SDK method
                            # declaration or a copied-history substitute.
                            modes = {row["id"] for row in opened["modes"]["availableModes"]}
                            self.assertIn("default", modes)
                            await client.request("session/set_mode", {"sessionId": provider_id, "modeId": "default"})
                            with self.assertRaises(GeminiRPCError) as unsupported:
                                await client.request("session/fork", {"sessionId": provider_id, "cwd": str(cwd), "mcpServers": []})
                            self.assertEqual(unsupported.exception.code, -32601)
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
