import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


def write_events(path: Path, events: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(event) + "\n" for event in events),
        encoding="utf-8",
    )


class AbandonedTurnDetectionTests(unittest.TestCase):
    def test_detects_latest_unfinished_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            write_events(path, [
                {
                    "seq": 10,
                    "type": "turn_started",
                    "run_id": "run-open",
                    "backend": "codex",
                    "purpose": "scheduled_job",
                    "job_id": "job-status",
                },
                {
                    "seq": 11,
                    "type": "reasoning_summary",
                    "run_id": "run-open",
                    "text": "Still working",
                },
            ])
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.abandoned_turn_after_restart(
                    "chat-1",
                    {"latest_event_type": "reasoning_summary"},
                )

        self.assertIsNotNone(result)
        self.assertEqual(result["run_id"], "run-open")
        self.assertEqual(result["purpose"], "scheduled_job")
        self.assertEqual(result["job_id"], "job-status")

    def test_terminal_event_closes_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            write_events(path, [
                {"seq": 1, "type": "turn_started", "run_id": "run-done"},
                {"seq": 2, "type": "reasoning_summary", "run_id": "run-done"},
                {"seq": 3, "type": "turn_finished", "run_id": "run-done"},
            ])
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.abandoned_turn_after_restart(
                    "chat-1",
                    {"latest_event_type": "turn_finished"},
                )

        self.assertIsNone(result)

    def test_native_steer_recovers_only_new_logical_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            write_events(path, [
                {"seq": 1, "type": "turn_started", "run_id": "run-old"},
                {"seq": 2, "type": "reasoning_summary", "run_id": "run-old"},
                {
                    "seq": 3,
                    "type": "turn_stopped",
                    "run_id": "run-old",
                    "native_steer": True,
                },
                {"seq": 4, "type": "turn_started", "run_id": "run-new"},
                {"seq": 5, "type": "tool_finished", "run_id": "run-new"},
            ])
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.abandoned_turn_after_restart(
                    "chat-1",
                    {"latest_event_type": "tool_finished"},
                )

        self.assertIsNotNone(result)
        self.assertEqual(result["run_id"], "run-new")

    def test_delayed_activity_does_not_reopen_finished_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            write_events(path, [
                {"seq": 1, "type": "turn_started", "run_id": "run-failed"},
                {"seq": 2, "type": "turn_finished", "run_id": "run-failed"},
                {"seq": 3, "type": "tool_finished", "run_id": "run-failed"},
            ])
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.abandoned_turn_after_restart(
                    "chat-1",
                    {"latest_event_type": "tool_finished"},
                )

        self.assertIsNone(result)

    def test_error_without_finished_marker_is_recovered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            write_events(path, [
                {"seq": 1, "type": "turn_started", "run_id": "run-failed"},
                {"seq": 2, "type": "error", "run_id": "run-failed"},
            ])
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.abandoned_turn_after_restart(
                    "chat-1",
                    {"latest_event_type": "error"},
                )

        self.assertIsNotNone(result)
        self.assertEqual(result["run_id"], "run-failed")

    def test_persisted_active_run_survives_latest_codex_control_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            write_events(path, [
                {"seq": 1, "type": "turn_started", "run_id": "run-open"},
                {
                    "seq": 2,
                    "type": "codex_thread_status",
                    "status": {"type": "idle"},
                },
            ])
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.abandoned_turn_after_restart(
                    "chat-1",
                    {
                        "latest_event_type": "codex_thread_status",
                        "active_run": {
                            "run_id": "run-open",
                            "backend": "codex",
                        },
                    },
                )

        self.assertIsNotNone(result)
        self.assertEqual(result["run_id"], "run-open")

    def test_terminal_event_clears_stale_persisted_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            write_events(path, [
                {"seq": 1, "type": "turn_started", "run_id": "run-done"},
                {"seq": 2, "type": "turn_finished", "run_id": "run-done"},
            ])
            with patch.object(agent_server, "events_path", return_value=path):
                result = agent_server.abandoned_turn_after_restart(
                    "chat-1",
                    {
                        "latest_event_type": "codex_thread_status",
                        "active_run": {"run_id": "run-done"},
                    },
                )

        self.assertIsNone(result)


class AbandonedTurnRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_metadata_persists_active_run_until_terminal(self) -> None:
        sessions = {"chat-1": {"id": "chat-1", "backend": "codex"}}
        save = AsyncMock()
        with patch.object(agent_server.STORE, "sessions", sessions), patch.object(
            agent_server.STORE,
            "save",
            save,
        ):
            await agent_server.update_session_event_metadata("chat-1", {
                "seq": 1,
                "ts": "2026-08-01T00:00:00Z",
                "type": "turn_started",
                "run_id": "run-open",
                "backend": "codex",
            })
            self.assertEqual(
                sessions["chat-1"]["active_run"]["run_id"],
                "run-open",
            )
            await agent_server.update_session_event_metadata("chat-1", {
                "seq": 2,
                "ts": "2026-08-01T00:00:01Z",
                "type": "codex_thread_status",
                "status": {"type": "idle"},
            })
            self.assertIn("active_run", sessions["chat-1"])
            await agent_server.update_session_event_metadata("chat-1", {
                "seq": 3,
                "ts": "2026-08-01T00:00:02Z",
                "type": "turn_finished",
                "run_id": "run-open",
            })

        self.assertNotIn("active_run", sessions["chat-1"])
        self.assertEqual(save.await_count, 2)

    async def test_persists_stopped_event_without_replaying_prompt(self) -> None:
        append_event = AsyncMock(return_value={})
        with patch.object(
            agent_server.STORE,
            "sessions",
            {
                "chat-1": {
                    "id": "chat-1",
                    "backend": "codex",
                    "latest_event_type": "reasoning_summary",
                }
            },
        ), patch.object(
            agent_server,
            "abandoned_turn_after_restart",
            return_value={
                "run_id": "run-orphan",
                "backend": "codex",
                "purpose": "scheduled_job",
                "job_id": "job-1",
            },
        ), patch.object(agent_server, "append_event", append_event):
            recovered = await agent_server.recover_abandoned_turns_after_start()

        self.assertEqual(recovered, 1)
        append_event.assert_awaited_once()
        session_id, event_type, payload = append_event.await_args.args
        self.assertEqual(session_id, "chat-1")
        self.assertEqual(event_type, "turn_stopped")
        self.assertEqual(payload["run_id"], "run-orphan")
        self.assertEqual(payload["job_id"], "job-1")
        self.assertTrue(payload["recovered_after_restart"])
        self.assertNotIn("prompt", payload)


if __name__ == "__main__":
    unittest.main()
