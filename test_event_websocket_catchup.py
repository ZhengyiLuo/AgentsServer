import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


class FakeWebSocket:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    async def send_json(self, event: dict[str, object]) -> None:
        self.events.append(event)


class EventWebSocketCatchupTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
