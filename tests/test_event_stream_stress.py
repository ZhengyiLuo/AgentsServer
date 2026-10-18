"""Real loopback WebSockets, temporary history, no provider or installed service.

Exercises the production authenticated route and durable append path under
concurrent catch-up, client disconnects, filtered sequence gaps and live writes.
"""
import asyncio
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import uvicorn
from websockets.asyncio.client import connect

import agent_server


class EventStreamStressTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_clients_catch_up_reconnect_and_receive_live_tail(self):
        sid = "isolated-sync-stress"
        total = 6000
        clients = 6
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(agent_server.app, lifespan="off", log_level="critical"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            with path.open("w") as output:
                for seq in range(1, total + 1):
                    output.write(json.dumps({"id": f"e{seq}", "seq": seq, "session_id": sid,
                        "type": "raw_event" if seq % 7 == 0 else "assistant_text",
                        "ts": "2026-10-01T00:00:00Z", "text": f"Item {seq}: " + "x" * 200}) + "\n")
            with (
                patch.object(agent_server, "AGENT_TOKEN", "isolated-test-token"),
                patch.object(agent_server, "HUB", agent_server.SubscriberHub()),
                patch.dict(agent_server.STORE.sessions, {sid: {"id": sid}}, clear=True),
                patch.object(agent_server, "events_path", return_value=path),
                patch.object(agent_server, "ensure_dirs"),
                patch.object(agent_server, "prepare_provider_history_metadata_repair"),
                patch.object(agent_server, "fork_internal_run_ids", return_value=set()),
                patch.object(agent_server, "update_session_event_metadata", new=AsyncMock()),
                patch.dict(agent_server.EVENT_SEQ_CACHE, {sid: total}, clear=True),
                patch.object(agent_server, "EVENT_STREAM_HEARTBEAT_SECONDS", 0.1),
            ):
                task = asyncio.create_task(server.serve(sockets=[listener]))
                sockets = []
                try:
                    async with asyncio.timeout(10):
                        while not server.started:
                            await asyncio.sleep(0.01)
                    ready = [asyncio.Event() for _ in range(clients)]

                    async def consume(index):
                        received = []
                        last = 0
                        for attempt in range(4):
                            url = f"ws://127.0.0.1:{port}/api/sessions/{sid}/events?after={last}&visible=true&liveness=true"
                            async with connect(url, additional_headers={"X-AgentsDock-Token": "isolated-test-token"}, proxy=None) as ws:
                                sockets.append(ws)
                                count = 0
                                async for raw in ws:
                                    event = json.loads(raw)
                                    if event.get("type") == "event_stream_status":
                                        self.assertNotIn("seq", event)
                                        if event["phase"] == "ready":
                                            ready[index].set()
                                        continue
                                    seq = event["seq"]
                                    self.assertGreater(seq, last)
                                    self.assertEqual(event["session_id"], sid)
                                    received.append(seq)
                                    last = seq
                                    count += 1
                                    # Three forced network reconnects per client.
                                    if attempt < 3 and count >= 71 + index * 23:
                                        break
                                    if seq == total + 40:
                                        return received
                                    if count % 100 == 0:
                                        await asyncio.sleep(0.001 * index)
                        self.fail("stream ended before live tail")

                    async def append_tail():
                        await asyncio.gather(*(event.wait() for event in ready))
                        await agent_server.append_durable_event_batch(sid, [
                            ("assistant_text", {"text": f"Live tail {n}"}) for n in range(40)
                        ])

                    results = await asyncio.wait_for(asyncio.gather(
                        *(consume(index) for index in range(clients)), append_tail()
                    ), timeout=45)
                    expected = [seq for seq in range(1, total + 1) if seq % 7] + list(range(total + 1, total + 41))
                    for received in results[:-1]:
                        self.assertEqual(received, expected)
                    # Wait for endpoint finally blocks, including all 18 dropped connections.
                    async with asyncio.timeout(5):
                        while agent_server.HUB._subscribers or agent_server.HUB._reservations:
                            await asyncio.sleep(0.01)
                finally:
                    for ws in sockets:
                        await ws.close()
                    server.should_exit = True
                    await asyncio.wait_for(task, timeout=10)
                    listener.close()
