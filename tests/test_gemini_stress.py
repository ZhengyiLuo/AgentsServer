"""Opt-in bounded stress: real CLI + HTTP/WebSocket, synthetic model and state."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import time
import unittest

from websockets.sync.client import connect
from tests import test_gemini_lifecycle as lifecycle


LONG_TEXT = "".join(
    f"长消息 row {index}: {hashlib.sha256(str(index).encode()).hexdigest()}\n"
    for index in range(1600)
)


def streaming_reply(handler):
    handler.rfile.read(int(handler.headers["Content-Length"]))
    stream = "streamGenerateContent" in handler.path
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream" if stream else "application/json")
    handler.end_headers()
    chunks = [LONG_TEXT[i:i + 2500] for i in range(0, len(LONG_TEXT), 2500)] if stream else [LONG_TEXT]
    for index, chunk in enumerate(chunks):
        packet = {"candidates": [{"index": 0, "content": {"role": "model", "parts": [{"text": chunk}]},
            **({"finishReason": "STOP"} if index == len(chunks) - 1 else {})}],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 100, "totalTokenCount": 200}}
        raw = "data: " + json.dumps(packet) + "\n\n" if stream else json.dumps(packet)
        try:
            handler.wfile.write(raw.encode())
            handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        if stream:
            time.sleep(.02)


@unittest.skipUnless(os.environ.get("AGENTSDOCK_TEST_GEMINI_BIN")
                     and os.environ.get("AGENTSDOCK_TEST_GEMINI_STRESS") == "1",
                     "optional native CLI stress, about 80 seconds")
class NativeStress(unittest.TestCase):
    for _name in ("setUp", "start_server", "ready", "cleanup", "wait", "new", "events", "send", "finished"):
        locals()[_name] = getattr(lifecycle.Lifecycle, _name)

    def test_four_chats_ten_long_turns_with_reconnect_and_native_resume(self):
        self.fixture_type.do_POST = streaming_reply
        chats = [self.new() for _ in range(4)]
        expected_ids = None
        for turn in range(1, 11):
            def one(sid):
                self.send(sid, prompt=f"Isolated stress chat {sid}, round {turn}; reply once.")
                if turn % 2:
                    for _ in range(2):
                        with connect(f"ws://127.0.0.1:{self.port}/api/sessions/{sid}/events?after=0&liveness=true&visible=true",
                                     additional_headers={"X-AgentsDock-Token": self.token}, proxy=None) as ws:
                            last = 0
                            for _ in range(2000):
                                packet = json.loads(ws.recv(timeout=10))
                                if packet.get("type") == "event_stream_status" and packet.get("phase") == "ready":
                                    break
                                if "seq" in packet:
                                    self.assertGreater(packet["seq"], last)
                                    last = packet["seq"]
                                    self.assertEqual(packet["session_id"], sid)
                            else:
                                self.fail("Missing catch-up boundary")
                self.assertFalse(self.finished(sid, turn)[-1]["is_error"])
                events = self.events(sid)
                self.assertEqual(sum(e["type"] == "turn_started" for e in events), turn)
                self.assertEqual([e["text"] for e in events if e["type"] == "assistant_text"], [LONG_TEXT] * turn)
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(one, chats))
            records = json.loads((self.state / "sessions.json").read_text())
            ids = {sid: records[sid]["gemini_session_id"] for sid in chats}
            self.assertEqual(len(set(ids.values())), 4)
            if expected_ids:
                self.assertEqual(ids, expected_ids)
            expected_ids = ids
        histories = [p for sid in chats for p in (self.state / "admin/provider-connections").rglob(
            "*" + expected_ids[sid][:8] + "*") if p.is_file()]
        self.assertTrue(histories)
        for sid in chats:
            self.assertEqual(self.client.delete("/api/sessions/" + sid).status_code, 200)
        self.assertTrue(all(p.exists() for p in histories))
