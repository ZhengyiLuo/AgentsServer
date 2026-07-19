import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class CompactTimelinePagingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_state_dir = agent_server.STATE_DIR
        agent_server.STATE_DIR = Path(self.temporary.name)
        self.session_id = "compact-history-chat"
        path = agent_server.events_path(self.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(self.event(seq, event_type)) + "\n" for seq, event_type in enumerate([
                "turn_started",
                "raw_event",
                "reasoning_summary",
                "assistant_text",
                "tool_started",
                "artifact_created",
                "tool_finished",
                "job_started",
                "process_started",
                "error",
                "provider_session",
                "turn_finished",
                "cwd_fallback",
                "file_uploaded",
                "history_imported",
                "handoff_digest_received",
                "backend_changed",
                "turn_stopped",
                "session_created",
                "job_finished",
                "code_diff",
                "queue_snapshot",
            ], start=1)),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        agent_server.STATE_DIR = self.previous_state_dir
        self.temporary.cleanup()

    def test_compact_filter_preserves_conversation_system_job_and_file_events(self) -> None:
        default_page = agent_server.read_visible_events_page(
            self.session_id,
            limit=100,
            tail=False,
        )
        compact_page = agent_server.read_visible_events_page(
            self.session_id,
            limit=100,
            tail=False,
            compact=True,
        )

        default_types = [event["type"] for event in default_page[0]]
        self.assertNotIn("raw_event", default_types)
        self.assertIn("reasoning_summary", default_types)
        self.assertIn("tool_started", default_types)
        self.assertIn("code_diff", default_types)

        compact_types = [event["type"] for event in compact_page[0]]
        self.assertEqual(compact_types, [
            "turn_started",
            "assistant_text",
            "artifact_created",
            "job_started",
            "error",
            "turn_finished",
            "file_uploaded",
            "handoff_digest_received",
            "turn_stopped",
            "job_finished",
            "queue_snapshot",
        ])
        self.assertEqual(compact_page[1:], (22, 11, 0, 0))

    def test_compact_before_and_after_pages_count_only_compact_events(self) -> None:
        before_page = agent_server.read_visible_events_page(
            self.session_id,
            before=18,
            limit=3,
            tail=True,
            compact=True,
        )
        self.assertEqual([event["seq"] for event in before_page[0]], [12, 14, 16])
        self.assertEqual(before_page[1:], (22, 8, 5, 0))

        after_page = agent_server.read_visible_events_after_page(
            self.session_id,
            after=10,
            limit=3,
            compact=True,
        )
        self.assertEqual([event["seq"] for event in after_page[0]], [12, 14, 16])
        self.assertEqual(after_page[1:], (22, 6, 0, 3))

    async def test_endpoint_keeps_default_payload_and_offloads_visible_scans(self) -> None:
        session = {
            "id": self.session_id,
            "title": "Compact history",
            "backend": "codex",
            "created_at": "2026-07-19T00:00:00Z",
            "updated_at": "2026-07-19T00:00:00Z",
        }
        original_to_thread = asyncio.to_thread
        offload = AsyncMock(side_effect=original_to_thread)
        with patch.dict(agent_server.STORE.sessions, {self.session_id: session}, clear=True), patch.object(
            agent_server.asyncio,
            "to_thread",
            new=offload,
        ):
            default_response = await agent_server.get_session(
                self.session_id,
                limit=100,
                tail=False,
            )
            visible_response = await agent_server.get_session(
                self.session_id,
                limit=100,
                tail=False,
                visible=True,
            )
            compact_response = await agent_server.get_session(
                self.session_id,
                after=10,
                limit=3,
                tail=False,
                compact=True,
            )

        self.assertIn("raw_event", [event["type"] for event in default_response["events"]])
        self.assertIn("reasoning_summary", [event["type"] for event in visible_response["events"]])
        self.assertNotIn("raw_event", [event["type"] for event in visible_response["events"]])
        self.assertEqual([event["seq"] for event in compact_response["events"]], [12, 14, 16])
        self.assertEqual(offload.await_count, 2)
        self.assertIs(offload.await_args_list[0].args[0], agent_server.read_visible_events_page)
        self.assertIs(offload.await_args_list[1].args[0], agent_server.read_visible_events_after_page)
        self.assertTrue(offload.await_args_list[1].kwargs["compact"])

    def event(self, seq: int, event_type: str) -> dict[str, object]:
        event: dict[str, object] = {
            "id": f"event-{seq}",
            "session_id": self.session_id,
            "seq": seq,
            "type": event_type,
            "ts": f"2026-07-19T00:00:{seq:02d}Z",
        }
        if event_type == "turn_started":
            event["prompt"] = "Start the conversation"
        elif event_type in {"assistant_text", "reasoning_summary"}:
            event["text"] = event_type
        elif event_type == "turn_finished":
            event["result_text"] = "Done"
        elif event_type in {"artifact_created", "file_uploaded"}:
            event["file"] = {"id": f"file-{seq}", "filename": f"file-{seq}.txt"}
        elif event_type.startswith("job_"):
            event["job_id"] = "job-1"
        elif event_type == "raw_event":
            event["raw"] = "provider packet"
        return event


if __name__ == "__main__":
    unittest.main()
