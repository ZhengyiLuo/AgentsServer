import asyncio
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server
from agent_server import (
    claude_result_error,
    is_expected_claude_interruption_result,
    prepare_steered_turn,
    queued_turn_from_event,
    rebuild_queued_turns_from_events,
    run_queued_turn_now,
    should_schedule_queue_after_finish,
    start_next_queued_turn,
)


class PrepareSteeredTurnTests(unittest.TestCase):
    def test_replays_both_messages_and_merges_attachments(self) -> None:
        turn = prepare_steered_turn(
            {
                "queued_id": "queued-steer",
                "prompt": "Use the smaller batch instead.",
                "file_ids": ["new", "shared"],
            },
            {
                "run_id": "run-original",
                "prompt": "Launch the complete training sweep.",
                "file_ids": ["original", "shared"],
            },
        )

        self.assertIn("Launch the complete training sweep.", turn["prompt"])
        self.assertIn("Use the smaller batch instead.", turn["prompt"])
        self.assertEqual(turn["display_prompt"], "Use the smaller batch instead.")
        self.assertEqual(turn["file_ids"], ["original", "shared", "new"])
        self.assertEqual(turn["display_file_ids"], ["new", "shared"])
        self.assertEqual(turn["steer_interrupted_run_id"], "run-original")
        self.assertTrue(turn["replays_interrupted_message"])

    def test_text_only_steer_replays_but_does_not_claim_the_interrupted_image(self) -> None:
        turn = prepare_steered_turn(
            {"queued_id": "queued-steer", "prompt": "Look at the warning instead.", "file_ids": []},
            {"run_id": "run-original", "prompt": "What is this?", "file_ids": ["original-image"]},
        )

        self.assertEqual(turn["file_ids"], ["original-image"])
        self.assertEqual(turn["display_file_ids"], [])

    def test_image_only_messages_remain_distinct_during_steering(self) -> None:
        turn = prepare_steered_turn(
            {"queued_id": "queued-steer", "prompt": "", "file_ids": ["new-image"]},
            {"run_id": "run-original", "prompt": "", "file_ids": ["original-image"]},
        )

        self.assertIn("[Attachment-only message]", turn["prompt"])
        self.assertIn("[Attachment-only steering message]", turn["prompt"])
        self.assertEqual(turn["display_prompt"], "")
        self.assertEqual(turn["file_ids"], ["original-image", "new-image"])
        self.assertEqual(turn["display_file_ids"], ["new-image"])

    def test_plain_promotion_stays_unchanged_without_an_interrupted_turn(self) -> None:
        selected = {"queued_id": "queued-steer", "prompt": "Run this now.", "file_ids": []}
        self.assertEqual(prepare_steered_turn(selected, None), selected)


class RunQueuedTurnNowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_queued = agent_server.QUEUED_TURNS
        self.previous_run_now = agent_server.RUN_NOW_TURNS
        self.previous_steering = agent_server.STEERING_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        agent_server.STORE.sessions = {
            "chat-1": {"id": "chat-1", "title": "Chat", "backend": "codex"},
        }
        agent_server.QUEUED_TURNS = {
            "chat-1": deque([{
                "queued_id": "queued-steer",
                "prompt": "Change course now.",
                "file_ids": ["new-file"],
                "backend": "codex",
            }]),
        }
        agent_server.RUN_NOW_TURNS = {}
        agent_server.STEERING_SESSIONS = set()
        agent_server.CURRENT_TURNS = {
            "chat-1": {
                "run_id": "run-original",
                "prompt": "Finish the original investigation.",
                "file_ids": ["original-file"],
                "backend": "codex",
            },
        }

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.QUEUED_TURNS = self.previous_queued
        agent_server.RUN_NOW_TURNS = self.previous_run_now
        agent_server.STEERING_SESSIONS = self.previous_steering
        agent_server.CURRENT_TURNS = self.previous_current

    async def test_interrupted_turn_is_replayed_with_exact_steering_message(self) -> None:
        append_event = AsyncMock(return_value={})

        async def completed_wait(_session_id: str) -> None:
            return None

        with patch.object(agent_server, "stop_turn", new_callable=AsyncMock, return_value={"stopped": True}), \
                patch.object(agent_server, "append_event", append_event), \
                patch.object(agent_server, "wait_for_steered_turn_slot", completed_wait):
            result = await run_queued_turn_now("chat-1", "queued-steer")
            await asyncio.sleep(0)

        promoted = agent_server.RUN_NOW_TURNS["chat-1"]
        self.assertTrue(result["replays_interrupted_message"])
        self.assertIn("Finish the original investigation.", promoted["prompt"])
        self.assertIn("Change course now.", promoted["prompt"])
        self.assertEqual(promoted["display_prompt"], "Change course now.")
        self.assertEqual(promoted["file_ids"], ["original-file", "new-file"])
        self.assertEqual(promoted["display_file_ids"], ["new-file"])
        event_payload = append_event.await_args.args[2]
        self.assertEqual(event_payload["prompt"], "Change course now.")
        self.assertIn("Finish the original investigation.", event_payload["request_prompt"])
        self.assertEqual(event_payload["file_ids"], ["original-file", "new-file"])
        self.assertEqual(event_payload["display_file_ids"], ["new-file"])
        self.assertTrue(event_payload["replays_interrupted_message"])

    async def test_no_active_turn_promotes_without_replaying_old_text(self) -> None:
        append_event = AsyncMock(return_value={})

        async def completed_wait(_session_id: str) -> None:
            return None

        with patch.object(agent_server, "stop_turn", new_callable=AsyncMock, return_value={"stopped": False}), \
                patch.object(agent_server, "append_event", append_event), \
                patch.object(agent_server, "wait_for_steered_turn_slot", completed_wait):
            result = await run_queued_turn_now("chat-1", "queued-steer")
            await asyncio.sleep(0)

        promoted = agent_server.RUN_NOW_TURNS["chat-1"]
        self.assertFalse(result["replays_interrupted_message"])
        self.assertEqual(promoted["prompt"], "Change course now.")

    async def test_later_steer_supersedes_earlier_user_turn_but_keeps_later_work(self) -> None:
        agent_server.QUEUED_TURNS["chat-1"] = deque([
            {
                "queued_id": "queued-first",
                "prompt": "Stale first message.",
                "file_ids": [],
                "backend": "codex",
            },
            {
                "queued_id": "queued-steer",
                "prompt": "Change course now.",
                "file_ids": ["new-file"],
                "backend": "codex",
            },
            {
                "queued_id": "queued-later",
                "prompt": "Keep this for afterward.",
                "file_ids": [],
                "backend": "codex",
            },
        ])
        append_event = AsyncMock(return_value={})

        async def completed_wait(_session_id: str) -> None:
            return None

        with patch.object(agent_server, "stop_turn", new_callable=AsyncMock, return_value={"stopped": True}), \
                patch.object(agent_server, "append_event", append_event), \
                patch.object(agent_server, "wait_for_steered_turn_slot", completed_wait):
            result = await run_queued_turn_now("chat-1", "queued-steer")
            await asyncio.sleep(0)

        self.assertEqual(result["superseded_queued_ids"], ["queued-first"])
        self.assertEqual(agent_server.RUN_NOW_TURNS["chat-1"]["queued_id"], "queued-steer")
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["queued-later"],
        )
        event_types = [call.args[1] for call in append_event.await_args_list]
        self.assertEqual(event_types, ["turn_queue_run_now", "turn_unqueued"])
        run_now_payload = append_event.await_args_list[0].args[2]
        self.assertEqual(run_now_payload["remaining"], 1)
        self.assertEqual(run_now_payload["superseded_queued_ids"], ["queued-first"])
        unqueued_payload = append_event.await_args_list[1].args[2]
        self.assertEqual(unqueued_payload["queued_id"], "queued-first")
        self.assertEqual(unqueued_payload["superseded_by_queued_id"], "queued-steer")

        agent_server.STEERING_SESSIONS.discard("chat-1")
        with patch.object(agent_server, "start_turn", new_callable=AsyncMock) as start_turn:
            await start_next_queued_turn("chat-1")
            await start_next_queued_turn("chat-1")

        self.assertEqual(
            [call.kwargs["queued_id"] for call in start_turn.await_args_list],
            ["queued-steer", "queued-later"],
        )

    async def test_later_steer_preserves_purpose_bearing_internal_work(self) -> None:
        agent_server.QUEUED_TURNS["chat-1"] = deque([
            {
                "queued_id": "queued-first",
                "prompt": "Stale first message.",
                "file_ids": [],
                "backend": "codex",
            },
            {
                "queued_id": "queued-digest",
                "prompt": "Internal digest.",
                "file_ids": [],
                "backend": "codex",
                "purpose": "handoff_digest",
            },
            {
                "queued_id": "queued-steer",
                "prompt": "Change course now.",
                "file_ids": [],
                "backend": "codex",
            },
        ])

        async def completed_wait(_session_id: str) -> None:
            return None

        with patch.object(agent_server, "stop_turn", new_callable=AsyncMock, return_value={"stopped": True}), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server, "wait_for_steered_turn_slot", completed_wait):
            await run_queued_turn_now("chat-1", "queued-steer")
            await asyncio.sleep(0)

        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["queued-digest"],
        )

    async def test_second_steer_cannot_overwrite_the_first_handoff(self) -> None:
        agent_server.RUN_NOW_TURNS["chat-1"] = {
            "queued_id": "already-steering",
            "prompt": "First steer",
        }
        with self.assertRaisesRegex(agent_server.HTTPException, "already in progress"):
            await run_queued_turn_now("chat-1", "queued-steer")
        self.assertEqual(agent_server.RUN_NOW_TURNS["chat-1"]["queued_id"], "already-steering")
        self.assertEqual(len(agent_server.QUEUED_TURNS["chat-1"]), 1)

    async def test_handoff_barrier_keeps_the_promoted_turn_reserved(self) -> None:
        promoted = {
            "queued_id": "queued-steer",
            "prompt": "Continue the original request, but use the smaller batch.",
            "display_prompt": "Use the smaller batch.",
            "file_ids": [],
            "display_file_ids": [],
            "backend": "codex",
        }
        agent_server.RUN_NOW_TURNS["chat-1"] = promoted
        agent_server.STEERING_SESSIONS.add("chat-1")

        with patch.object(agent_server, "start_turn", new_callable=AsyncMock) as start_turn:
            await start_next_queued_turn("chat-1")
            start_turn.assert_not_awaited()
            self.assertIs(agent_server.RUN_NOW_TURNS["chat-1"], promoted)

            agent_server.STEERING_SESSIONS.discard("chat-1")
            await start_next_queued_turn("chat-1")

        start_turn.assert_awaited_once()
        request = start_turn.await_args.args[1]
        self.assertEqual(request.prompt, promoted["prompt"])
        self.assertEqual(request.display_prompt, promoted["display_prompt"])
        self.assertEqual(start_turn.await_args.kwargs["display_file_ids"], [])
        self.assertNotIn("chat-1", agent_server.RUN_NOW_TURNS)

    def test_recovered_run_now_turn_keeps_provider_and_display_attachments_separate(self) -> None:
        item = queued_turn_from_event(
            {
                "queued_id": "queued-steer",
                "request_prompt": "Combined provider replay",
                "prompt": "New steering text",
                "file_ids": ["old-image", "new-image"],
                "display_file_ids": ["new-image"],
            },
            agent_server.STORE.sessions["chat-1"],
            1,
        )

        self.assertEqual(item["file_ids"], ["old-image", "new-image"])
        self.assertEqual(item["display_file_ids"], ["new-image"])

    def test_recovery_keeps_an_image_only_queued_turn(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text(json.dumps({
                "id": "event-1",
                "seq": 1,
                "session_id": "chat-1",
                "type": "turn_queued",
                "ts": "2026-07-20T23:00:00Z",
                "queued_id": "queued-image",
                "prompt": "",
                "request_prompt": "",
                "file_ids": ["image-only"],
            }) + "\n")
            with patch.object(agent_server, "events_path", return_value=path):
                rebuilt = rebuild_queued_turns_from_events()

        self.assertEqual(rebuilt, 1)
        self.assertEqual(agent_server.QUEUED_TURNS["chat-1"][0]["prompt"], "")
        self.assertEqual(agent_server.QUEUED_TURNS["chat-1"][0]["file_ids"], ["image-only"])

    def test_recovery_does_not_resurrect_turns_superseded_by_run_now(self) -> None:
        events = [
            {
                "seq": 1,
                "type": "turn_queued",
                "queued_id": "queued-first",
                "prompt": "Stale first message.",
                "file_ids": [],
            },
            {
                "seq": 2,
                "type": "turn_queued",
                "queued_id": "queued-steer",
                "prompt": "Run this now.",
                "file_ids": [],
            },
            {
                "seq": 3,
                "type": "turn_queued",
                "queued_id": "queued-later",
                "prompt": "Keep this for afterward.",
                "file_ids": [],
            },
            {
                "seq": 4,
                "type": "turn_queue_run_now",
                "queued_id": "queued-steer",
                "superseded_queued_ids": ["queued-first"],
            },
            {
                "seq": 5,
                "type": "turn_started",
                "queued_id": "queued-steer",
            },
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("".join(json.dumps({
                "id": f"event-{event['seq']}",
                "session_id": "chat-1",
                "ts": "2026-07-21T00:00:00Z",
                **event,
            }) + "\n" for event in events))
            with patch.object(agent_server, "events_path", return_value=path):
                rebuilt = rebuild_queued_turns_from_events()

        self.assertEqual(rebuilt, 1)
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["queued-later"],
        )

    def test_stopped_runner_does_not_schedule_a_second_steering_drain(self) -> None:
        agent_server.RUN_NOW_TURNS["chat-1"] = {
            "queued_id": "queued-steer",
            "prompt": "Run this next.",
        }

        self.assertFalse(should_schedule_queue_after_finish("chat-1", stopped=True))
        self.assertFalse(should_schedule_queue_after_finish("chat-1", stopped=False))
        agent_server.RUN_NOW_TURNS.clear()
        agent_server.STEERING_SESSIONS.add("chat-1")
        self.assertFalse(should_schedule_queue_after_finish("chat-1", stopped=False))
        agent_server.STEERING_SESSIONS.clear()
        self.assertTrue(should_schedule_queue_after_finish("chat-1", stopped=False))

    async def test_append_failure_restores_superseded_predecessor(self) -> None:
        agent_server.QUEUED_TURNS["chat-1"] = deque([
            {
                "queued_id": "queued-first",
                "prompt": "Do not lose this message.",
                "file_ids": [],
                "backend": "codex",
            },
            {
                "queued_id": "queued-internal",
                "prompt": "Internal work.",
                "file_ids": [],
                "backend": "codex",
                "purpose": "scheduled_job",
            },
            {
                "queued_id": "queued-second",
                "prompt": "Do not lose this one either.",
                "file_ids": [],
                "backend": "codex",
            },
            {
                "queued_id": "queued-steer",
                "prompt": "Run this now.",
                "file_ids": [],
                "backend": "codex",
            },
        ])

        with patch.object(agent_server, "stop_turn", new_callable=AsyncMock, return_value={"stopped": True}), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock, side_effect=OSError("disk full")), \
                patch.object(agent_server, "schedule_next_queued_turn") as schedule_next:
            with self.assertRaisesRegex(OSError, "disk full"):
                await run_queued_turn_now("chat-1", "queued-steer")

        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["queued-first", "queued-internal", "queued-second"],
        )
        self.assertNotIn("chat-1", agent_server.STEERING_SESSIONS)
        schedule_next.assert_called_once_with("chat-1")


class ClaudeResultDiagnosticTests(unittest.TestCase):
    def diagnostic_event(self, errors: list[str] | None = None) -> dict[str, object]:
        return {
            "type": "result",
            "subtype": "error_during_execution",
            "terminal_reason": "aborted_tools",
            "stop_reason": "tool_use",
            "errors": errors or ["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"],
        }

    def test_expected_interruption_is_recognized_and_sanitized(self) -> None:
        event = self.diagnostic_event()
        self.assertTrue(is_expected_claude_interruption_result(event))
        self.assertEqual(claude_result_error(event), "Claude stopped before completing the turn.")

    def test_real_error_survives_alongside_internal_diagnostic(self) -> None:
        event = self.diagnostic_event([
            "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use",
            "Provider connection failed",
        ])
        self.assertFalse(is_expected_claude_interruption_result(event))
        self.assertEqual(claude_result_error(event), "Provider connection failed")

    def test_ordinary_claude_error_is_unchanged(self) -> None:
        event = {"type": "result", "subtype": "error_during_execution", "errors": ["Authentication failed"]}
        self.assertEqual(claude_result_error(event), "Authentication failed")


if __name__ == "__main__":
    unittest.main()
