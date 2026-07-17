import asyncio
import unittest
from collections import deque
from unittest.mock import AsyncMock, patch

import agent_server
from agent_server import prepare_steered_turn, run_queued_turn_now, start_next_queued_turn


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
        self.assertEqual(turn["steer_interrupted_run_id"], "run-original")
        self.assertTrue(turn["replays_interrupted_message"])

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
        event_payload = append_event.await_args.args[2]
        self.assertEqual(event_payload["prompt"], "Change course now.")
        self.assertIn("Finish the original investigation.", event_payload["request_prompt"])
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
        self.assertNotIn("chat-1", agent_server.RUN_NOW_TURNS)


if __name__ == "__main__":
    unittest.main()
