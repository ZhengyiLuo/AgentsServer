import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class FakeWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.accepted = False

    async def send_json(self, event: dict[str, object]) -> None:
        self.events.append(event)

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        raise agent_server.WebSocketDisconnect()


class EventWebSocketCatchupTests(unittest.IsolatedAsyncioTestCase):
    async def test_catchup_projects_completed_commentary_into_chat_body(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            path.write_text(
                json.dumps({
                    "seq": 1,
                    "id": "commentary-1",
                    "session_id": "chat",
                    "type": "reasoning_summary",
                    "phase": "commentary",
                    "ts": "2026-07-28T00:00:00Z",
                    "text": "Still validating step 25.",
                })
                + "\n",
                encoding="utf-8",
            )

            socket = FakeWebSocket()
            with (
                patch.object(agent_server, "events_path", return_value=path),
                patch.object(agent_server, "fork_internal_run_ids", return_value=set()),
            ):
                cursor = await agent_server.send_event_catchup(
                    "chat",
                    socket,  # type: ignore[arg-type]
                    after=0,
                    through=1,
                    visible=True,
                )

        self.assertEqual(cursor, 1)
        self.assertEqual(socket.events[0]["type"], "assistant_text")
        self.assertEqual(socket.events[0]["phase"], "commentary")

    async def test_catchup_drains_more_than_one_page_without_raw_events(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with path.open("w", encoding="utf-8") as output:
                for seq in range(1, 1606):
                    event_type = "raw_event" if seq % 4 == 0 else "reasoning_summary"
                    output.write(json.dumps({
                        "seq": seq,
                        "id": f"event-{seq}",
                        "session_id": "chat",
                        "type": event_type,
                        "ts": "2026-07-28T00:00:00Z",
                        "text": f"event {seq}",
                    }) + "\n")

            socket = FakeWebSocket()
            with (
                patch.object(agent_server, "events_path", return_value=path),
                patch.object(agent_server, "fork_internal_run_ids", return_value=set()),
            ):
                cursor = await agent_server.send_event_catchup(
                    "chat",
                    socket,  # type: ignore[arg-type]
                    after=0,
                    through=1605,
                    visible=True,
                )

        self.assertEqual(cursor, 1605)
        self.assertEqual(len(socket.events), 1204)
        self.assertEqual(socket.events[0]["seq"], 1)
        self.assertEqual(socket.events[-1]["seq"], 1605)
        self.assertNotIn("raw_event", {event["type"] for event in socket.events})

    async def test_omitted_visible_query_preserves_bounded_legacy_handshake(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            with path.open("w", encoding="utf-8") as output:
                for seq in range(1, 706):
                    output.write(json.dumps({
                        "seq": seq,
                        "id": f"event-{seq}",
                        "session_id": "legacy-chat",
                        "type": "raw_event" if seq % 2 == 0 else "reasoning_summary",
                        "ts": "2026-07-28T00:00:00Z",
                        "text": f"event {seq}",
                    }) + "\n")

            socket = FakeWebSocket()
            agent_server.EVENT_DELIVERY_LOCKS.pop("legacy-chat", None)
            with (
                patch.dict(
                    agent_server.STORE.sessions,
                    {"legacy-chat": {"id": "legacy-chat"}},
                ),
                patch.object(agent_server, "events_path", return_value=path),
                patch.object(
                    agent_server,
                    "fork_internal_run_ids",
                    return_value=set(),
                ),
                patch.object(
                    agent_server,
                    "websocket_authorized",
                    return_value=True,
                ),
            ):
                await agent_server.session_events(
                    "legacy-chat",
                    socket,  # type: ignore[arg-type]
                    after=0,
                    visible=None,
                )

        self.assertTrue(socket.accepted)
        self.assertEqual(len(socket.events), 500)
        self.assertEqual(socket.events[0]["seq"], 1)
        self.assertEqual(socket.events[-1]["seq"], 500)
        self.assertIn("raw_event", {event["type"] for event in socket.events})

    async def test_opted_in_handshake_delivers_racing_and_live_events_once(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            path.write_text(json.dumps({
                "seq": 1,
                "id": "event-1",
                "session_id": "race-chat",
                "type": "reasoning_summary",
                "ts": "2026-07-28T00:00:00Z",
                "text": "first",
            }) + "\n", encoding="utf-8")

            class RacingWebSocket(FakeWebSocket):
                injected = False

                async def send_json(self, event: dict[str, object]) -> None:
                    await super().send_json(event)
                    if event["seq"] == 1 and not self.injected:
                        self.injected = True
                        await agent_server.append_event(
                            "race-chat",
                            "reasoning_summary",
                            {"text": "raced"},
                        )

                async def receive_text(self) -> str:
                    await agent_server.append_event(
                        "race-chat",
                        "reasoning_summary",
                        {"text": "live"},
                    )
                    raise agent_server.WebSocketDisconnect()

            socket = RacingWebSocket()
            agent_server.EVENT_SEQ_CACHE.pop("race-chat", None)
            agent_server.EVENT_DELIVERY_LOCKS.pop("race-chat", None)
            with (
                patch.dict(
                    agent_server.STORE.sessions,
                    {"race-chat": {"id": "race-chat"}},
                ),
                patch.object(agent_server, "ensure_dirs"),
                patch.object(agent_server, "events_path", return_value=path),
                patch.object(
                    agent_server,
                    "fork_internal_run_ids",
                    return_value=set(),
                ),
                patch.object(
                    agent_server,
                    "websocket_authorized",
                    return_value=True,
                ),
                patch.object(
                    agent_server,
                    "update_session_event_metadata",
                    new=AsyncMock(),
                ),
                patch.object(
                    agent_server,
                    "event_files_belong_to_session",
                    return_value=True,
                ),
            ):
                await agent_server.session_events(
                    "race-chat",
                    socket,  # type: ignore[arg-type]
                    after=0,
                    visible=True,
                )

        self.assertEqual(
            [event["seq"] for event in socket.events],
            [1, 2, 3],
        )

    async def test_append_event_preserves_live_sequence_order(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            first_metadata_started = asyncio.Event()
            release_first_metadata = asyncio.Event()
            delivered: list[int] = []

            async def update_metadata(_session_id: str, event: dict[str, object]) -> None:
                if event["seq"] == 1:
                    first_metadata_started.set()
                    await release_first_metadata.wait()

            async def broadcast(_session_id: str, event: dict[str, object]) -> None:
                delivered.append(int(event["seq"]))

            agent_server.EVENT_SEQ_CACHE.pop("ordered-chat", None)
            agent_server.EVENT_DELIVERY_LOCKS.pop("ordered-chat", None)
            with (
                patch.object(agent_server, "ensure_dirs"),
                patch.object(agent_server, "events_path", return_value=path),
                patch.object(
                    agent_server,
                    "update_session_event_metadata",
                    side_effect=update_metadata,
                ),
                patch.object(
                    agent_server,
                    "event_files_belong_to_session",
                    return_value=True,
                ),
                patch.object(
                    agent_server.HUB,
                    "broadcast",
                    side_effect=broadcast,
                ),
            ):
                first = asyncio.create_task(
                    agent_server.append_event(
                        "ordered-chat",
                        "reasoning_summary",
                        {"text": "first"},
                    )
                )
                await first_metadata_started.wait()
                second = asyncio.create_task(
                    agent_server.append_event(
                        "ordered-chat",
                        "reasoning_summary",
                        {"text": "second"},
                    )
                )
                await asyncio.sleep(0)
                self.assertEqual(delivered, [])
                release_first_metadata.set()
                await asyncio.gather(first, second)

            persisted = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual([event["seq"] for event in persisted], [1, 2])
        self.assertEqual(delivered, [1, 2])

    async def test_append_event_bounds_tool_output_once_with_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "events.jsonl"
            agent_server.EVENT_SEQ_CACHE.pop("bounded-chat", None)
            agent_server.EVENT_DELIVERY_LOCKS.pop("bounded-chat", None)
            with (
                patch.object(agent_server, "ensure_dirs"),
                patch.object(agent_server, "events_path", return_value=path),
                patch.object(
                    agent_server,
                    "update_session_event_metadata",
                    new=AsyncMock(),
                ),
                patch.object(
                    agent_server,
                    "event_files_belong_to_session",
                    return_value=False,
                ),
                patch.object(
                    agent_server,
                    "CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS",
                    80,
                ),
            ):
                event = await agent_server.append_event(
                    "bounded-chat",
                    "tool_finished",
                    {"output": "x" * 200},
                )

            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(event["output_chars"], 200)
        self.assertTrue(event["output_truncated"])
        self.assertLessEqual(len(event["output"]), 80)
        self.assertEqual(persisted["output"], event["output"])
        self.assertTrue(
            event["output"].startswith(
                "[Earlier tool output truncated by AgentsServer]\n"
            )
        )


if __name__ == "__main__":
    unittest.main()
