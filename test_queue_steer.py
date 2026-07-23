import asyncio
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server
from agent_server import (
    build_turn_provider_prompt,
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
    def test_persists_raw_messages_and_generates_scoped_provider_prompt_at_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files_root = Path(tmp)
            for file_id in ("original", "shared", "new"):
                file_dir = files_root / file_id
                file_dir.mkdir()
                (file_dir / "meta.json").write_text(json.dumps({
                    "path": f"/uploads/{file_id}.png",
                    "filename": f"{file_id}.png",
                    "content_type": "image/png",
                }))
            with patch.object(agent_server, "FILES_ROOT", files_root):
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
                provider_prompt = build_turn_provider_prompt(
                    turn["prompt"],
                    turn["file_ids"],
                    turn["steering_lineage"],
                )

        self.assertEqual(turn["prompt"], "Use the smaller batch instead.")
        self.assertEqual(turn["steering_lineage"], [
            {
                "prompt": "Launch the complete training sweep.",
                "file_ids": ["original", "shared"],
            },
            {
                "prompt": "Use the smaller batch instead.",
                "file_ids": ["new", "shared"],
            },
        ])
        self.assertNotIn("[Interrupted message]", json.dumps(turn))
        self.assertNotIn("/uploads/", json.dumps(turn))
        self.assertNotIn("Launch the complete training sweep.", provider_prompt)
        self.assertNotIn("[Interrupted message]", provider_prompt)
        self.assertNotIn("/uploads/original.png", provider_prompt)
        self.assertIn("Use the smaller batch instead.", provider_prompt)
        self.assertIn("/uploads/new.png", provider_prompt)
        self.assertEqual(turn["display_prompt"], "Use the smaller batch instead.")
        self.assertEqual(turn["file_ids"], ["new", "shared"])
        self.assertEqual(turn["display_file_ids"], ["new", "shared"])
        self.assertEqual(turn["steer_interrupted_run_id"], "run-original")
        self.assertTrue(turn["replays_interrupted_message"])

    def test_text_only_steer_keeps_the_interrupted_image_scoped_to_old_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files_root = Path(tmp)
            file_dir = files_root / "original-image"
            file_dir.mkdir()
            (file_dir / "meta.json").write_text(json.dumps({
                "path": "/uploads/original.png",
                "filename": "original.png",
                "content_type": "image/png",
            }))
            with patch.object(agent_server, "FILES_ROOT", files_root):
                turn = prepare_steered_turn(
                    {"queued_id": "queued-steer", "prompt": "Look at the warning instead.", "file_ids": []},
                    {"run_id": "run-original", "prompt": "What is this?", "file_ids": ["original-image"]},
                )
                provider_prompt = build_turn_provider_prompt(
                    turn["prompt"],
                    turn["file_ids"],
                    turn["steering_lineage"],
                )

        self.assertNotIn("/uploads/original.png", provider_prompt)
        self.assertEqual(provider_prompt, "Look at the warning instead.")
        self.assertEqual(turn["prompt"], "Look at the warning instead.")
        self.assertEqual(turn["file_ids"], [])
        self.assertEqual(turn["display_file_ids"], [])

    def test_image_only_messages_remain_distinct_during_steering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files_root = Path(tmp)
            for file_id in ("original-image", "new-image"):
                file_dir = files_root / file_id
                file_dir.mkdir()
                filename = "original.png" if file_id == "original-image" else "new.png"
                (file_dir / "meta.json").write_text(json.dumps({
                    "path": f"/uploads/{filename}",
                    "filename": filename,
                    "content_type": "image/png",
                }))
            with patch.object(agent_server, "FILES_ROOT", files_root):
                turn = prepare_steered_turn(
                    {"queued_id": "queued-steer", "prompt": "", "file_ids": ["new-image"]},
                    {"run_id": "run-original", "prompt": "", "file_ids": ["original-image"]},
                )
                provider_prompt = build_turn_provider_prompt(
                    turn["prompt"],
                    turn["file_ids"],
                    turn["steering_lineage"],
                )

        self.assertEqual(turn["prompt"], "")
        self.assertIn("/uploads/new.png", provider_prompt)
        self.assertNotIn("/uploads/original.png", provider_prompt)
        self.assertEqual(turn["display_prompt"], "")
        self.assertEqual(turn["file_ids"], ["new-image"])
        self.assertEqual(turn["display_file_ids"], ["new-image"])

    def test_plain_promotion_stays_unchanged_without_an_interrupted_turn(self) -> None:
        selected = {"queued_id": "queued-steer", "prompt": "Run this now.", "file_ids": []}
        self.assertEqual(prepare_steered_turn(selected, None), selected)

    def test_repeated_steering_stays_flat_instead_of_nesting_generated_wrappers(self) -> None:
        first = prepare_steered_turn(
            {"prompt": "First steering instruction.", "file_ids": []},
            {"run_id": "run-original", "prompt": "Original request.", "file_ids": []},
        )
        second = prepare_steered_turn(
            {"prompt": "Second steering instruction.", "file_ids": []},
            {
                "run_id": "run-first-steer",
                "prompt": first["prompt"],
                "file_ids": first["file_ids"],
                "steering_lineage": first["steering_lineage"],
            },
        )
        provider_prompt = build_turn_provider_prompt(
            second["prompt"],
            second["file_ids"],
            second["steering_lineage"],
        )

        self.assertEqual(second["prompt"], "Second steering instruction.")
        self.assertEqual(
            [item["prompt"] for item in second["steering_lineage"]],
            ["Original request.", "First steering instruction.", "Second steering instruction."],
        )
        self.assertEqual(provider_prompt, "Second steering instruction.")
        self.assertNotIn("[AgentsDock steering context]", provider_prompt)
        self.assertNotIn("[Interrupted user message]", provider_prompt)
        self.assertNotIn("Original request.", provider_prompt)
        self.assertNotIn("First steering instruction.", provider_prompt)

    def test_nested_legacy_steering_envelopes_restore_flat_lineage(self) -> None:
        first = (
            agent_server.LEGACY_STEERING_PREFIX
            + "[Interrupted message]\nOriginal request.\n"
            "[End interrupted message]\n\n"
            "[Steering message]\nFirst steering instruction.\n"
            "[End steering message]"
        )
        second = (
            agent_server.LEGACY_STEERING_PREFIX
            + f"[Interrupted message]\n{first}\n"
            "[End interrupted message]\n\n"
            "[Steering message]\nSecond steering instruction.\n"
            "[End steering message]"
        )

        lineage = agent_server.parse_legacy_steering_lineage(second)

        self.assertEqual(
            [message["prompt"] for message in lineage],
            [
                "Original request.",
                "First steering instruction.",
                "Second steering instruction.",
            ],
        )


class StopTurnProviderReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_pre_spawn_force_send_defers_without_cancelling_the_original_turn(self) -> None:
        stop_requests: set[str] = set()
        with patch.object(agent_server, "ACTIVE", {}), \
                patch.object(agent_server, "BUSY_SESSIONS", {"chat-1"}), \
                patch.object(agent_server, "STOP_REQUESTS", stop_requests), \
                patch.object(agent_server, "STOPPED_RUNS", set()):
            result = await agent_server.stop_turn(
                "chat-1",
                emit_event=False,
                schedule_queue=False,
                require_provider_turn_ready=True,
            )

        self.assertFalse(result["stopped"])
        self.assertTrue(result["deferred"])
        self.assertNotIn("chat-1", stop_requests)


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

        with patch.object(
                agent_server,
                "stop_turn",
                new_callable=AsyncMock,
                return_value={"stopped": True},
        ) as stop_turn, \
                patch.object(agent_server, "append_event", append_event), \
                patch.object(agent_server, "wait_for_steered_turn_slot", completed_wait):
            result = await run_queued_turn_now("chat-1", "queued-steer")
            await asyncio.sleep(0)

        self.assertTrue(stop_turn.await_args.kwargs["require_provider_turn_ready"])
        promoted = agent_server.RUN_NOW_TURNS["chat-1"]
        self.assertTrue(result["replays_interrupted_message"])
        self.assertEqual(promoted["prompt"], "Change course now.")
        self.assertEqual(
            [item["prompt"] for item in promoted["steering_lineage"]],
            ["Finish the original investigation.", "Change course now."],
        )
        self.assertEqual(promoted["display_prompt"], "Change course now.")
        self.assertEqual(promoted["file_ids"], ["new-file"])
        self.assertEqual(promoted["display_file_ids"], ["new-file"])
        event_payload = append_event.await_args.args[2]
        self.assertEqual(event_payload["prompt"], "Change course now.")
        self.assertEqual(event_payload["request_prompt"], "Change course now.")
        self.assertNotIn("[Interrupted message]", event_payload["request_prompt"])
        self.assertEqual(event_payload["steering_lineage"], promoted["steering_lineage"])
        self.assertEqual(event_payload["file_ids"], ["new-file"])
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

    async def test_unready_provider_leaves_force_send_message_in_the_queue(self) -> None:
        append_event = AsyncMock(return_value={})
        with patch.object(
                agent_server,
                "stop_turn",
                new_callable=AsyncMock,
                return_value={"stopped": False, "deferred": True},
        ) as stop_turn, \
                patch.object(agent_server, "append_event", append_event), \
                patch.object(agent_server, "BUSY_SESSIONS", {"chat-1"}), \
                patch.object(agent_server, "schedule_next_queued_turn") as schedule_next:
            result = await run_queued_turn_now("chat-1", "queued-steer")

        self.assertFalse(result["ok"])
        self.assertTrue(result["deferred"])
        self.assertFalse(result["interrupted"])
        self.assertTrue(stop_turn.await_args.kwargs["require_provider_turn_ready"])
        self.assertNotIn("chat-1", agent_server.RUN_NOW_TURNS)
        self.assertNotIn("chat-1", agent_server.STEERING_SESSIONS)
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["queued-steer"],
        )
        self.assertEqual(append_event.await_args.args[1], "turn_deferred")
        schedule_next.assert_not_called()

    async def test_later_steer_runs_first_then_keeps_other_messages_in_original_order(self) -> None:
        agent_server.QUEUED_TURNS["chat-1"] = deque([
            {
                "queued_id": "queued-first",
                "prompt": "First queued message.",
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

        self.assertEqual(result["superseded_queued_ids"], [])
        self.assertEqual(agent_server.RUN_NOW_TURNS["chat-1"]["queued_id"], "queued-steer")
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["queued-first", "queued-later"],
        )
        event_types = [call.args[1] for call in append_event.await_args_list]
        self.assertEqual(event_types, ["turn_queue_run_now"])
        run_now_payload = append_event.await_args_list[0].args[2]
        self.assertEqual(run_now_payload["remaining"], 2)
        self.assertEqual(run_now_payload["superseded_queued_ids"], [])

        agent_server.STEERING_SESSIONS.discard("chat-1")
        with patch.object(agent_server, "start_turn", new_callable=AsyncMock) as start_turn:
            await start_next_queued_turn("chat-1")
            await start_next_queued_turn("chat-1")
            await start_next_queued_turn("chat-1")

        self.assertEqual(
            [call.kwargs["queued_id"] for call in start_turn.await_args_list],
            ["queued-steer", "queued-first", "queued-later"],
        )

    async def test_later_steer_preserves_user_and_internal_work_in_original_order(self) -> None:
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
            ["queued-first", "queued-digest"],
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
        lineage = [
            {"prompt": "Continue the original request.", "file_ids": []},
            {"prompt": "Use the smaller batch.", "file_ids": []},
        ]
        promoted = {
            "queued_id": "queued-steer",
            "prompt": "Use the smaller batch.",
            "display_prompt": "Use the smaller batch.",
            "file_ids": [],
            "display_file_ids": [],
            "backend": "codex",
            "steering_lineage": lineage,
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
        self.assertEqual(start_turn.await_args.kwargs["steering_lineage"], lineage)
        self.assertNotIn("chat-1", agent_server.RUN_NOW_TURNS)

    def test_recovered_legacy_run_now_turn_restores_raw_lineage(self) -> None:
        item = queued_turn_from_event(
            {
                "type": "turn_queue_run_now",
                "queued_id": "queued-steer",
                "request_prompt": (
                    agent_server.LEGACY_STEERING_PREFIX
                    + "[Interrupted message]\nOld request\n\n"
                    "[Interrupted message attachments]\n"
                    "- /uploads/old.png (old.png, image/png)\n"
                    "[End interrupted message attachments]\n"
                    "[End interrupted message]\n\n"
                    "[Steering message]\nNew steering text\n[End steering message]"
                ),
                "prompt": "New steering text",
                "file_ids": ["new-image"],
                "display_file_ids": ["new-image"],
                "replays_interrupted_message": True,
            },
            agent_server.STORE.sessions["chat-1"],
            1,
        )

        self.assertEqual(item["prompt"], "New steering text")
        self.assertNotIn("/uploads/old.png", item["prompt"])
        self.assertEqual(
            [message["prompt"] for message in item["steering_lineage"]],
            ["Old request", "New steering text"],
        )
        self.assertEqual(item["file_ids"], ["new-image"])
        self.assertEqual(item["display_file_ids"], ["new-image"])

    def test_recovered_run_now_turn_keeps_structured_lineage(self) -> None:
        lineage = [
            {"prompt": "Old request", "file_ids": ["old-image"]},
            {"prompt": "New steering text", "file_ids": ["new-image"]},
        ]
        item = queued_turn_from_event(
            {
                "type": "turn_queue_run_now",
                "queued_id": "queued-steer",
                "request_prompt": "New steering text",
                "prompt": "New steering text",
                "file_ids": ["new-image"],
                "display_file_ids": ["new-image"],
                "replays_interrupted_message": True,
                "steering_lineage": lineage,
            },
            agent_server.STORE.sessions["chat-1"],
            1,
        )

        self.assertEqual(item["prompt"], "New steering text")
        self.assertEqual(item["steering_lineage"], lineage)

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

    def test_recovery_keeps_unselected_turns_in_original_order_after_run_now_starts(self) -> None:
        events = [
            {
                "seq": 1,
                "type": "turn_queued",
                "queued_id": "queued-first",
                "prompt": "First queued message.",
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
                "superseded_queued_ids": [],
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

        self.assertEqual(rebuilt, 2)
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["queued-first", "queued-later"],
        )

    def test_recovery_honors_legacy_run_now_supersession_records(self) -> None:
        agent_server.QUEUED_TURNS.pop("chat-1", None)
        events = [
            {
                "seq": 1,
                "type": "turn_queued",
                "queued_id": "queued-first",
                "prompt": "Legacy superseded message.",
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
                "type": "turn_queue_run_now",
                "queued_id": "queued-steer",
                "superseded_queued_ids": ["queued-first"],
            },
            {
                "seq": 4,
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

        self.assertEqual(rebuilt, 0)
        self.assertNotIn("chat-1", agent_server.QUEUED_TURNS)

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

    async def test_append_failure_keeps_unselected_messages_in_original_order(self) -> None:
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

    def test_aborted_streaming_interruption_with_null_stop_reason_is_recognized(self) -> None:
        event = self.diagnostic_event([
            "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null",
        ])
        event["terminal_reason"] = "aborted_streaming"
        event["stop_reason"] = None

        self.assertTrue(is_expected_claude_interruption_result(event))
        self.assertEqual(claude_result_error(event), "Claude stopped before completing the turn.")

    def test_real_error_survives_alongside_internal_diagnostic(self) -> None:
        event = self.diagnostic_event([
            "[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=null",
            "Provider connection failed",
        ])
        event["terminal_reason"] = "aborted_streaming"
        event["stop_reason"] = None
        self.assertFalse(is_expected_claude_interruption_result(event))
        self.assertEqual(claude_result_error(event), "Provider connection failed")

    def test_ordinary_claude_error_is_unchanged(self) -> None:
        event = {"type": "result", "subtype": "error_during_execution", "errors": ["Authentication failed"]}
        self.assertEqual(claude_result_error(event), "Authentication failed")


if __name__ == "__main__":
    unittest.main()
