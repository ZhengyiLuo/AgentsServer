"""Real HTTP/WebSocket + pinned CLI, synthetic loopback model, disposable state."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from uuid import uuid4

import httpx
from websockets.sync.client import connect


class Fixture(BaseHTTPRequestHandler):
    mode = "normal"
    entered = threading.Event()
    release = threading.Event()
    bodies = []
    project = None

    def log_message(self, *_): pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cls = type(self); cls.bodies.append(body)
        if cls.mode == "error":
            raw = b'{"error":{"code":400,"message":"Synthetic rejected request","status":"INVALID_ARGUMENT"}}'
            self.send_response(400); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw); return
        responses = [p for c in body.get("contents", []) for p in c.get("parts", []) if "functionResponse" in p]
        parts = [{"text": "Fixture complete."}]
        if cls.mode == "mail":
            text = "\n".join(p.get("text", "") for c in body.get("contents", []) for p in c.get("parts", []))
            names = [f["name"] for tool in body.get("tools", []) for f in tool.get("functionDeclarations", [])]
            tool_name = next((name for name in names if name == "run" or ("agentsdock_" in name and name.endswith("run"))), None)
            def objects(value):
                if isinstance(value, dict):
                    yield value
                    for v in value.values(): yield from objects(v)
                elif isinstance(value, list):
                    for v in value: yield from objects(v)
                elif isinstance(value, str):
                    if value.startswith("<untrusted_context>\n") and value.endswith("\n</untrusted_context>"):
                        value = value.removeprefix("<untrusted_context>\n").removesuffix("\n</untrusted_context>")
                    try: parsed = json.loads(value)
                    except ValueError: return
                    if not isinstance(parsed, str): yield from objects(parsed)
            argument = None
            if "MAIL_SEND" in text:
                if not responses:
                    argument = {"helper": "chats", "arguments": ["list"]}
                elif not any(row.get("accepted") is True for row in objects(responses)):
                    route = next((row for row in objects(responses) if row.get("route_id") and row.get("allowed_actions")), None)
                    if route:
                        argument = {"helper": "chats", "arguments": ["send", "--route", route["route_id"], "--message", "MAIL_RECEIVE: reply MAIL_ACK", "--idempotency-key", "gemini-mail-fixture"]}
            elif "Unread peer mail" in text:
                if not responses:
                    argument = {"helper": "chats", "arguments": ["inbox"]}
                elif not any(row.get("accepted") is True for row in objects(responses)):
                    message = next((row for row in objects(responses) if row.get("source_session_id") and row.get("message_id")), None)
                    sender = next((row for row in objects(responses) if row.get("source_session_id")), None)
                    reply_route = next((row for row in objects(responses) if row.get("route_id") and row.get("available")), None)
                    if message and reply_route:
                        argument = {"helper": "chats", "arguments": ["send", "--route", reply_route["route_id"], "--reply-to", message["message_id"], "--message", "MAIL_ACK"]}
                    elif sender:
                        argument = {"helper": "chats", "arguments": ["read", "--sender", sender["source_session_id"], "--request-id", "gemini-mail-read"]}
            if argument and tool_name:
                parts = [{"functionCall": {"name": tool_name, "args": argument}}]
        if cls.mode == "tool" and not responses:
            parts = [{"functionCall": {"name": "write_file", "args": {"file_path": str(cls.project / "must-not-exist.txt"), "content": "test"}}}]
        stream = "streamGenerateContent" in self.path
        def record(parts, final=True):
            value = {"candidates": [{"index": 0, "content": {"role": "model", "parts": parts}, **({"finishReason": "STOP"} if final else {})}],
                     "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4, "totalTokenCount": 14}}
            return (("data: " + json.dumps(value) + "\n\n") if stream else json.dumps(value)).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream" if stream else "application/json"); self.end_headers()
        try:
            if cls.mode in {"stream", "thought_stream"}:
                self.wfile.write(record([{"text": "VISIBLE_FIRST_CHUNK ", **({"thought": True} if cls.mode == "thought_stream" else {})}], False)); self.wfile.flush()
                cls.entered.set(); cls.release.wait(25)
                self.wfile.write(record([{"text": "VISIBLE_LAST_CHUNK"}])); self.wfile.flush()
            else:
                self.wfile.write(record(parts)); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError): pass


@unittest.skipUnless(os.environ.get("AGENTSDOCK_TEST_GEMINI_BIN"), "optional native lifecycle contract")
class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.project = self.root / "project"; self.project.mkdir(); self.state = self.root / "state"
        self.fixture_type = type("LocalAPI", (Fixture,), {"mode": "normal", "entered": threading.Event(), "release": threading.Event(), "bodies": [], "project": self.project})
        self.fixture = ThreadingHTTPServer(("127.0.0.1", 0), self.fixture_type)
        self.thread = threading.Thread(target=self.fixture.serve_forever, daemon=True); self.thread.start()
        with socket.socket() as sock: sock.bind(("127.0.0.1", 0)); self.port = sock.getsockname()[1]
        self.token = secrets.token_hex(32)
        self.env = {"PATH": os.environ["PATH"], "HOME": str(self.root), "LANG": "en_US.UTF-8",
            "AGENTSDOCK_STATE_DIR": str(self.state), "AGENTSDOCK_AGENT_TOKEN": self.token, "AGENTSDOCK_AGENT_PORT": str(self.port),
            "AGENTSDOCK_AGENT_CWD": str(self.project), "AGENTSDOCK_GEMINI_PREVIEW": "1",
            "AGENTSDOCK_GEMINI_BIN": os.environ["AGENTSDOCK_TEST_GEMINI_BIN"], "AGENTSDOCK_CODEX_TRANSPORT": "exec", "AGENTSDOCK_CLAUDE_TRANSPORT": "print"}
        self.log = (self.root / "server.log").open("w")
        self.start_server()
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self.port}", headers={"X-AgentsDock-Token": self.token}, timeout=30, trust_env=False)
        self.addCleanup(self.cleanup)
        def healthy():
            try: return self.client.get("/api/health").status_code == 200
            except httpx.HTTPError: return False
        self.wait(healthy)
        response = self.client.put("/api/admin/provider-connections/gemini", json={"base_url": f"http://127.0.0.1:{self.fixture.server_port}",
            "api_key": "synthetic-fixture-key", "model": "gemini-2.5-flash", "protocol": "gemini", "auth_header": "x-goog-api-key", "expected_revision": 0})
        self.assertEqual(response.status_code, 200); self.assertTrue(response.json()["ok"])

    def start_server(self):
        self.server = subprocess.Popen([sys.executable, "-m", "uvicorn", "agent_server:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=Path(__file__).resolve().parents[1], env=self.env, stdout=self.log, stderr=self.log)

    def ready(self):
        try: return self.client.get("/api/health").status_code == 200
        except httpx.HTTPError: return False

    def cleanup(self):
        self.fixture_type.release.set(); self.client.close(); self.server.terminate()
        try: self.server.wait(timeout=15)
        except subprocess.TimeoutExpired: self.server.kill(); self.server.wait(timeout=5)
        self.fixture.shutdown(); self.fixture.server_close(); self.thread.join(timeout=2); self.log.close(); self.temp.cleanup()

    def wait(self, check, timeout=35):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            result = check()
            if result: return result
            time.sleep(.05)
        self.fail("Native lifecycle check timed out")

    def new(self):
        response = self.client.post("/api/sessions", json={"backend": "gemini", "provider_connection": "custom", "cwd": str(self.project)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["session"]["id"]

    def events(self, sid):
        path = self.state / "sessions" / sid / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def send(self, sid, **kwargs):
        response = self.client.post(f"/api/sessions/{sid}/turns", json={"prompt": "Disposable native contract.", "client_capabilities": ["gemini_acp_v1"], **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def finished(self, sid, count=1):
        return self.wait(lambda: (turns if len(turns := [e for e in self.events(sid) if e["type"] == "turn_finished"]) >= count else None))

    def test_first_failure_retry_stop_retry_and_delete_isolation(self):
        sid = self.new(); self.fixture_type.mode = "error"; self.send(sid)
        self.assertTrue(self.finished(sid)[-1]["is_error"])
        self.assertFalse(json.loads((self.state / "sessions.json").read_text())[sid].get("gemini_session_id"))
        self.fixture_type.mode = "normal"; self.send(sid)
        self.assertFalse(self.finished(sid, 2)[-1]["is_error"])
        neighbor = sid; before = self.events(neighbor)
        sid = self.new(); self.fixture_type.mode = "tool"; self.send(sid)
        route = f"/api/sessions/{sid}"
        pending = self.wait(lambda: self.client.get(route + "/gemini/runtime").json()["pending_interactions"])
        self.assertEqual(self.client.post(route + "/stop").status_code, 200)
        self.assertTrue(self.finished(sid)[-1]["stopped"])
        self.assertEqual(self.client.post(route + "/gemini/interactions/" + pending[0]["id"] + "/resolve", json={"response": {"decision": "accept"}}).status_code, 409)
        self.assertFalse((self.project / "must-not-exist.txt").exists())
        self.fixture_type.mode = "normal"; self.send(sid)
        self.assertFalse(self.finished(sid, 2)[-1]["is_error"])
        native_id = json.loads((self.state / "sessions.json").read_text())[sid]["gemini_session_id"]
        self.fixture_type.mode = "tool"; self.send(sid)
        self.wait(lambda: self.client.get(route + "/gemini/runtime").json()["pending_interactions"])
        histories = list((self.state / "admin/provider-connections").rglob(f"*{native_id[:8]}*"))
        self.assertTrue(histories)
        self.assertEqual(self.client.delete(route).status_code, 200)
        self.assertTrue(all(path.exists() for path in histories))
        self.assertEqual(self.events(neighbor), before)

    def test_live_answer_reconnect_snapshot_and_exact_final_usage(self):
        sid = self.new(); self.fixture_type.mode = "stream"; self.send(sid)
        self.assertTrue(self.fixture_type.entered.wait(20))
        for _ in range(2):
            with connect(f"ws://127.0.0.1:{self.port}/api/sessions/{sid}/events?reasoning_stream=true",
                         additional_headers={"X-AgentsDock-Token": self.token}, proxy=None) as ws:
                for _ in range(25):
                    packet = json.loads(ws.recv(timeout=5))
                    if packet.get("type") == "reasoning_summary_stream" and packet["items"]:
                        self.assertEqual(packet["items"][0]["phase"], "assistant")
                        self.assertEqual(packet["items"][0]["text"], "VISIBLE_FIRST_CHUNK ")
                        break
                else: self.fail("Live answer not delivered on reconnect")
        self.assertFalse(any(e["type"] == "turn_finished" for e in self.events(sid)))
        self.fixture_type.release.set(); terminal = self.finished(sid)[-1]
        self.assertFalse(terminal["is_error"])
        self.assertEqual(terminal["input_tokens"], 10)
        self.assertEqual(terminal["output_tokens"], 4)
        self.assertEqual([e["text"] for e in self.events(sid) if e["type"] == "assistant_text"], ["VISIBLE_FIRST_CHUNK VISIBLE_LAST_CHUNK"])

    def test_initial_native_import_titles_exclusion_and_idempotency(self):
        native_id = str(uuid4()); chats = self.root / ".gemini/tmp/native/chats"; chats.mkdir(parents=True)
        (chats.parent / ".project_root").write_text(str(self.project))
        path = chats / f"session-fixture-{native_id[:8]}.json"
        path.write_text(json.dumps({"sessionId": native_id, "kind": "main", "projectHash": hashlib.sha256(str(self.project).encode()).hexdigest(),
            "summary": "Original Gemini title", "messages": [{"id": "u", "type": "user", "content": "Original user"}, {"id": "a", "type": "gemini", "content": "Original answer"}]}))
        original = path.read_bytes()
        self.assertFalse(any(row["backend"] == "gemini" for row in self.client.get("/api/local-sessions").json()["sessions"]))
        rows = self.client.get("/api/local-sessions?include_gemini=true").json()["sessions"]
        self.assertEqual(rows[0]["label"], "Original Gemini title")
        request = {"items": [{"backend": "gemini", "provider_session_id": native_id, "cwd": str(self.project)}]}
        response = self.client.post("/api/sessions/bulk-import", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()["results"][0]; self.assertTrue(result["ok"], result)
        self.assertEqual(result["imported"], 2)
        sid = result["session_id"]
        self.assertEqual([e["text"] for e in self.events(sid) if e["type"] == "assistant_text"], ["Original answer"])
        self.assertFalse(self.client.get("/api/local-sessions?include_gemini=true").json()["sessions"])
        self.assertFalse(self.client.post("/api/sessions/bulk-import", json=request).json()["results"][0]["ok"])
        self.assertEqual(path.read_bytes(), original)

    def test_real_native_mcp_cross_chat_send_and_reply(self):
        # Trust only this disposable fixture, never the operator's workspace.
        trust = self.root / ".gemini/trustedFolders.json"; trust.parent.mkdir(exist_ok=True)
        trust.write_text(json.dumps({str(self.project): "TRUST_FOLDER"}))
        self.assertEqual(self.client.get("/api/runtime/catalog").status_code, 200)
        sender, receiver = self.new(), self.new()
        self.fixture_type.mode = "mail"
        prompt = "@Receiver MAIL_SEND: ask the receiver to reply using AgentsDock."
        self.send(sender, prompt=prompt, client_capabilities=["gemini_acp_v1", "chat_conversation_async_route_v1", "agent_cross_chat_routes_v2", "cross_chat_handoffs_v2"],
                  chat_references=[{"session_id": receiver, "display_title_snapshot": "Receiver", "source_text_start": 0,
                                    "source_text_end": 9, "action": "route", "grant_intent": True}])
        handled = set()
        def received_reply():
            for sid in (sender, receiver):
                pending = self.client.get(f"/api/sessions/{sid}/gemini/runtime").json()["pending_interactions"]
                for item in pending:
                    if item["id"] in handled: continue
                    handled.add(item["id"])
                    result = self.client.post(f"/api/sessions/{sid}/gemini/interactions/{item['id']}/resolve", json={"response": {"decision": "accept"}})
                    self.assertEqual(result.status_code, 200)
            return any(e.get("handoff_preview") == "MAIL_ACK" and e.get("source_session_id") == receiver for e in self.events(sender))
        try:
            self.wait(received_reply, timeout=40)
        except AssertionError:
            # Synthetic fixture diagnostics only: never dump production model requests.
            tools = [e.get("tool") for sid in (sender, receiver) for e in self.events(sid) if e["type"] in {"tool_started", "tool_finished"}]
            names = [[f["name"] for tool in body.get("tools", []) for f in tool.get("functionDeclarations", [])] for body in self.fixture_type.bodies]
            errors = [e.get("message") for sid in (sender, receiver) for e in self.events(sid) if e["type"] == "error"]
            responses = [p for body in self.fixture_type.bodies for content in body.get("contents", []) for p in content.get("parts", []) if "functionResponse" in p]
            self.fail("MCP fixture did not reply: " + repr({"tools": tools, "names": names, "errors": errors, "responses": responses})[-8000:])
        self.assertGreaterEqual(len(handled), 3, "Native MCP tool calls must require explicit approvals")
        self.assertFalse(any(e["type"] == "turn_started" and "MAIL_RECEIVE" in str(e.get("prompt", "")) and not e.get("purpose") for e in self.events(receiver)))
        self.assertTrue(any("user_delegation" in json.dumps(body) and prompt in json.dumps(body)
                            for body in self.fixture_type.bodies), "Native recipient must receive server-attested delegation")

    def test_untrusted_workspace_does_not_silently_grant_mcp_trust(self):
        self.assertEqual(self.client.get("/api/runtime/catalog").status_code, 200)
        sender, receiver = self.new(), self.new()
        before = len(self.fixture_type.bodies)
        self.send(sender, prompt="@Receiver send a greeting", client_capabilities=["gemini_acp_v1", "chat_conversation_async_route_v1", "agent_cross_chat_routes_v2", "cross_chat_handoffs_v2"],
                  chat_references=[{"session_id": receiver, "display_title_snapshot": "Receiver", "source_text_start": 0,
                                    "source_text_end": 9, "action": "route", "grant_intent": True}])
        self.assertTrue(self.finished(sender)[-1]["is_error"])
        errors = [e["message"] for e in self.events(sender) if e["type"] == "error"]
        self.assertTrue(any("folder trust and MCP settings" in message for message in errors), errors)
        self.assertFalse((self.root / ".gemini/trustedFolders.json").exists())
        self.assertEqual(len(self.fixture_type.bodies), before, "A blocked explicit handoff must not start model work")
        self.assertFalse(any(e["type"] == "turn_started" for e in self.events(receiver)))

    def test_mixed_attachments_reach_native_api_and_can_resume(self):
        import base64
        sid = self.new(); ids = []
        image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a5WQAAAAASUVORK5CYII=")
        for name, data, mime in (("中文 空格.txt", b"ATTACHED_UTF8", "text/plain"), ("pixel.png", image, "image/png")):
            response = self.client.post(f"/api/sessions/{sid}/files", files={"file": (name, data, mime)})
            self.assertEqual(response.status_code, 200, response.text)
            ids.append(response.json()["file"]["id"])
        self.send(sid, file_ids=ids)
        self.assertFalse(self.finished(sid)[-1]["is_error"])
        parts = [part for body in self.fixture_type.bodies for content in body.get("contents", []) for part in content.get("parts", [])]
        self.assertTrue(any("ATTACHED_UTF8" in part.get("text", "") for part in parts))
        self.assertTrue(any(part.get("inlineData", {}).get("data") == base64.b64encode(image).decode() for part in parts))
        self.send(sid)
        self.assertFalse(self.finished(sid, 2)[-1]["is_error"])
        other = self.new()
        response = self.client.post(f"/api/sessions/{other}/turns", json={"prompt": "Other chat", "file_ids": ids, "client_capabilities": ["gemini_acp_v1"]})
        self.assertEqual(response.status_code, 404)

    def test_queue_restart_and_native_continuation_after_delete(self):
        sid = self.new(); self.fixture_type.mode = "stream"; self.send(sid)
        self.assertTrue(self.fixture_type.entered.wait(20))
        queued = self.send(sid, prompt="Queued follow-up.")
        self.assertTrue(queued["queued"])
        self.fixture_type.mode = "normal"; self.fixture_type.release.set()
        self.assertFalse(self.finished(sid, 2)[-1]["is_error"])
        saved = json.loads((self.state / "sessions.json").read_text())[sid]
        native_id = saved["gemini_session_id"]
        self.server.terminate(); self.server.wait(timeout=15); self.start_server(); self.wait(self.ready)
        self.send(sid, prompt="After server restart.")
        self.assertFalse(self.finished(sid, 3)[-1]["is_error"])
        self.assertEqual(json.loads((self.state / "sessions.json").read_text())[sid]["gemini_session_id"], native_id)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/fork", json={}).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/sessions/{sid}").status_code, 200)
        # Continue the same provider UUID directly through native ACP, outside
        # AgentsServer, using this fixture's existing private API profile.
        import asyncio
        from gemini_acp_client import GeminiACPClient
        from gemini_history import replay_snapshot
        from gemini_runtime import build_environment
        profile = next((self.state / "admin/provider-connections").glob("gemini-runtime-*"))
        env = build_environment(self.env, cwd=str(self.project), api_home=profile,
            api_key="synthetic-fixture-key", base_url=f"http://127.0.0.1:{self.fixture.server_port}")
        async def resume():
            async def update(_event): pass
            client = GeminiACPClient(on_update=update)
            try:
                await client.start([os.environ["AGENTSDOCK_TEST_GEMINI_BIN"], "--acp", "--model", "gemini-2.5-flash"], cwd=str(self.project), env=env)
                await client.open_session(cwd=str(self.project), session_id=native_id, replay=replay_snapshot(env, str(self.project), native_id))
                result = await client.prompt([{"type": "text", "text": "Native continuation after Delete."}], timeout=20)
                self.assertEqual(result["stopReason"], "end_turn")
            finally:
                await client.close()
                self.assertIsNotNone(client.process.returncode)
        asyncio.run(resume())

    def test_invalid_native_import_is_rejected_without_creating_a_chat(self):
        before = json.loads((self.state / "sessions.json").read_text()) if (self.state / "sessions.json").exists() else {}
        for identity in ("not-a-native-uuid", str(uuid4())):
            response = self.client.post("/api/sessions", json={"backend": "gemini", "provider_session_id": identity, "cwd": str(self.project), "import_history": True})
            self.assertEqual(response.status_code, 400, response.text)
        after = json.loads((self.state / "sessions.json").read_text()) if (self.state / "sessions.json").exists() else {}
        self.assertEqual(before, after)

    def test_native_summary_is_adopted_but_manual_title_wins(self):
        sid = self.new(); self.send(sid); self.finished(sid)
        saved = json.loads((self.state / "sessions.json").read_text())[sid]
        native_id = saved["gemini_session_id"]
        transcript = next(path for path in (self.state / "admin/provider-connections").rglob(f"session-*{native_id[:8]}.*") if path.suffix in {".json", ".jsonl"})
        def summary(title):
            if transcript.suffix == ".json":
                value = json.loads(transcript.read_text()); value["summary"] = title; transcript.write_text(json.dumps(value))
            else:
                with transcript.open("a") as output: output.write(json.dumps({"$set": {"summary": title}}) + "\n")
        summary("Native generated summary")
        self.send(sid); self.finished(sid, 2)
        self.assertEqual(json.loads((self.state / "sessions.json").read_text())[sid]["title"], "Native generated summary")
        self.assertEqual(self.client.patch(f"/api/sessions/{sid}", json={"title": "My manual title"}).status_code, 200)
        summary("Updated native summary")
        self.send(sid); self.finished(sid, 3)
        self.assertEqual(json.loads((self.state / "sessions.json").read_text())[sid]["title"], "My manual title")

    def test_stop_retains_exposed_thought_and_denied_write_stays_denied(self):
        sid = self.new(); self.fixture_type.mode = "thought_stream"; self.send(sid)
        self.assertTrue(self.fixture_type.entered.wait(20))
        time.sleep(.2)
        self.assertEqual(self.client.post(f"/api/sessions/{sid}/stop").status_code, 200)
        self.assertTrue(self.finished(sid)[-1]["stopped"])
        # This exact heading/whitespace is supplied by the pinned ACP runtime,
        # not reconstructed from a private chain of thought by AgentsDock.
        self.assertEqual([e["text"] for e in self.events(sid) if e["type"] == "reasoning_summary"], ["****\nVISIBLE_FIRST_CHUNK"])
        self.fixture_type.release.set(); self.fixture_type.mode = "normal"
        self.send(sid); self.assertFalse(self.finished(sid, 2)[-1]["is_error"])
        self.fixture_type.mode = "tool"
        other = self.new(); self.send(other)
        pending = self.wait(lambda: self.client.get(f"/api/sessions/{other}/gemini/runtime").json()["pending_interactions"])
        result = self.client.post(f"/api/sessions/{other}/gemini/interactions/{pending[0]['id']}/resolve", json={"response": {"decision": "decline"}})
        self.assertEqual(result.status_code, 200)
        self.finished(other)
        self.assertFalse((self.project / "must-not-exist.txt").exists())
        self.assertTrue(any(e["type"] == "tool_finished" and e["tool"].get("is_error") for e in self.events(other)))
