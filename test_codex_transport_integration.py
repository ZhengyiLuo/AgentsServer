import asyncio
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import agent_server


class SessionCodexTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_load_migrates_missing_and_invalid_transport_to_auto(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            sessions_file = Path(temporary) / "sessions.json"
            sessions_file.write_text(json.dumps({
                "missing": {
                    "id": "missing",
                    "backend": "codex",
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
                "invalid": {
                    "id": "invalid",
                    "backend": "codex",
                    "codex_transport": "old-mode",
                    "created_at": "2026-01-02T00:00:00Z",
                    "updated_at": "2026-01-02T00:00:00Z",
                },
            }))
            store = agent_server.SessionStore()
            with patch.object(agent_server, "SESSIONS_FILE", sessions_file), \
                    patch.object(agent_server, "ensure_dirs"):
                await store.load()

            self.assertEqual(store.sessions["missing"]["codex_transport"], "auto")
            self.assertEqual(store.sessions["invalid"]["codex_transport"], "auto")
            persisted = json.loads(sessions_file.read_text())
            self.assertEqual(persisted["missing"]["codex_transport"], "auto")
            self.assertEqual(persisted["invalid"]["codex_transport"], "auto")

    async def test_create_update_and_public_snapshot_keep_transport(self) -> None:
        store = agent_server.SessionStore()
        with patch.object(agent_server, "ensure_dirs"), \
                patch.object(store, "save", new_callable=AsyncMock), \
                patch.object(agent_server, "append_event", new_callable=AsyncMock):
            session = await store.create(agent_server.CreateSessionRequest(
                backend="codex",
                codex_transport="app-server",
                title="Transport test",
            ))
            self.assertEqual(session["codex_transport"], "app_server")

            # Transport remains switchable after a native provider thread exists.
            session["codex_thread_id"] = "thread-existing"
            session["session_id"] = "thread-existing"
            updated = await store.update(session["id"], {"codex_transport": "exec"})

        self.assertEqual(updated["codex_transport"], "exec")
        self.assertEqual(agent_server.public_session(updated)["codex_transport"], "exec")

    def test_queued_event_recovery_snapshots_transport(self) -> None:
        session = {"id": "chat-1", "backend": "codex", "codex_transport": "exec"}
        item = agent_server.queued_turn_from_event(
            {
                "queued_id": "queued-1",
                "prompt": "queued",
                "codex_transport": "app_server",
            },
            session,
            1,
        )
        session["codex_transport"] = "auto"
        self.assertEqual(item["codex_transport"], "app_server")
        self.assertEqual(
            agent_server.public_queued_turn("chat-1", item, 1)["codex_transport"],
            "app_server",
        )


class StartTurnCodexTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_queued = agent_server.QUEUED_TURNS
        self.previous_run_now = agent_server.RUN_NOW_TURNS
        self.previous_steering = agent_server.STEERING_SESSIONS
        self.previous_metadata = agent_server.RUN_METADATA
        agent_server.STORE.sessions = {
            "chat-1": {
                "id": "chat-1",
                "title": "Existing chat",
                "folder": "General",
                "cwd": "/tmp",
                "backend": "codex",
                "codex_transport": "exec",
            },
        }
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = set()
        agent_server.CURRENT_TURNS = {}
        agent_server.QUEUED_TURNS = {}
        agent_server.RUN_NOW_TURNS = {}
        agent_server.STEERING_SESSIONS = set()
        agent_server.RUN_METADATA = {}

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.QUEUED_TURNS = self.previous_queued
        agent_server.RUN_NOW_TURNS = self.previous_run_now
        agent_server.STEERING_SESSIONS = self.previous_steering
        agent_server.RUN_METADATA = self.previous_metadata

    async def test_immediate_turn_snapshots_request_override_without_persisting_it(self) -> None:
        session = agent_server.STORE.sessions["chat-1"]

        async def update(_session_id: str, values: dict[str, object]) -> dict[str, object]:
            session.update({key: value for key, value in values.items() if value is not None})
            return session

        run_selected = AsyncMock()
        append_event = AsyncMock(return_value={"type": "turn_started"})
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(agent_server, "turn_start_blocker", new_callable=AsyncMock, return_value=None), \
                patch.object(agent_server, "ensure_runtime_available", new_callable=AsyncMock), \
                patch.object(agent_server, "manifests_dir", return_value=Path(temporary)), \
                patch.object(agent_server.STORE, "update", side_effect=update), \
                patch.object(agent_server, "append_event", append_event), \
                patch.object(agent_server, "run_codex_selected", run_selected):
            result = await agent_server.start_turn(
                "chat-1",
                agent_server.TurnRequest(prompt="use native", codex_transport="app_server"),
            )
            await asyncio.sleep(0)

        self.assertFalse(result["queued"])
        self.assertEqual(session["codex_transport"], "exec")
        self.assertEqual(agent_server.CURRENT_TURNS["chat-1"]["codex_transport"], "app_server")
        started_payload = append_event.await_args.args[2]
        self.assertEqual(started_payload["codex_transport"], "app_server")
        run_session = run_selected.await_args.args[3]
        self.assertEqual(run_session["codex_transport"], "app_server")

    async def test_queued_turn_snapshots_transport_before_session_changes(self) -> None:
        agent_server.BUSY_SESSIONS.add("chat-1")
        with patch.object(agent_server, "append_event", new_callable=AsyncMock, return_value={}):
            result = await agent_server.start_turn(
                "chat-1",
                agent_server.TurnRequest(prompt="queued native", codex_transport="app_server"),
            )
        agent_server.STORE.sessions["chat-1"]["codex_transport"] = "auto"

        self.assertTrue(result["queued"])
        queued = agent_server.QUEUED_TURNS["chat-1"][0]
        self.assertEqual(queued["codex_transport"], "app_server")
        snapshot = await agent_server.queued_turns_snapshot("chat-1")
        self.assertEqual(snapshot[0]["codex_transport"], "app_server")


class CodexRunnerSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exec_transport_uses_exec_runner(self) -> None:
        run_exec = AsyncMock()
        run_app_server = AsyncMock()
        with patch.object(agent_server, "run_codex", run_exec), \
                patch.object(agent_server, "run_codex_app_server", run_app_server):
            await agent_server.run_codex_selected(
                "chat-1",
                "run-1",
                "prompt",
                {"codex_transport": "exec"},
                Path("/tmp/manifest.json"),
            )

        run_exec.assert_awaited_once()
        run_app_server.assert_not_awaited()

    async def test_auto_transport_uses_app_server_with_safe_exec_fallback(self) -> None:
        run_exec = AsyncMock()
        run_app_server = AsyncMock()
        with patch.object(agent_server, "run_codex", run_exec), \
                patch.object(agent_server, "run_codex_app_server", run_app_server):
            await agent_server.run_codex_selected(
                "chat-1",
                "run-1",
                "prompt",
                {"codex_transport": "auto"},
                Path("/tmp/manifest.json"),
            )

        run_exec.assert_not_awaited()
        run_app_server.assert_awaited_once()
        self.assertTrue(run_app_server.await_args.kwargs["allow_exec_fallback"])

    async def test_explicit_app_server_disables_exec_fallback(self) -> None:
        run_app_server = AsyncMock()
        with patch.object(agent_server, "run_codex_app_server", run_app_server):
            await agent_server.run_codex_selected(
                "chat-1",
                "run-1",
                "prompt",
                {"codex_transport": "app_server"},
                Path("/tmp/manifest.json"),
            )

        self.assertFalse(run_app_server.await_args.kwargs["allow_exec_fallback"])


class NativeCodexControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_queued = agent_server.QUEUED_TURNS
        self.previous_run_now = agent_server.RUN_NOW_TURNS
        self.previous_steering = agent_server.STEERING_SESSIONS
        self.previous_stopped = agent_server.STOPPED_RUNS
        self.previous_stop_requests = agent_server.STOP_REQUESTS
        self.native_turn = SimpleNamespace(
            steer=AsyncMock(return_value="turn-1"),
            interrupt=AsyncMock(return_value=None),
        )
        agent_server.STORE.sessions = {
            "chat-1": {
                "id": "chat-1",
                "backend": "codex",
                "codex_transport": "app_server",
            },
        }
        agent_server.ACTIVE = {
            "chat-1": {
                "run_id": "run-active",
                "backend": "codex",
                "transport": "app_server",
                "proc": SimpleNamespace(returncode=None),
                "codex_app_server_turn": self.native_turn,
            },
        }
        agent_server.BUSY_SESSIONS = {"chat-1"}
        agent_server.CURRENT_TURNS = {
            "chat-1": {
                "run_id": "run-active",
                "prompt": "Active request",
                "file_ids": ["active-image"],
                "backend": "codex",
                "codex_transport": "app_server",
            },
        }
        agent_server.QUEUED_TURNS = {
            "chat-1": deque([
                {
                    "queued_id": "A",
                    "prompt": "First",
                    "file_ids": [],
                    "backend": "codex",
                    "codex_transport": "app_server",
                },
                {
                    "queued_id": "B",
                    "prompt": "Steer with B",
                    "file_ids": ["steer-image"],
                    "backend": "codex",
                    "codex_transport": "app_server",
                },
                {
                    "queued_id": "C",
                    "prompt": "Third",
                    "file_ids": [],
                    "backend": "codex",
                    "codex_transport": "app_server",
                },
            ]),
        }
        agent_server.RUN_NOW_TURNS = {}
        agent_server.STEERING_SESSIONS = set()
        agent_server.STOPPED_RUNS = set()
        agent_server.STOP_REQUESTS = set()

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.QUEUED_TURNS = self.previous_queued
        agent_server.RUN_NOW_TURNS = self.previous_run_now
        agent_server.STEERING_SESSIONS = self.previous_steering
        agent_server.STOPPED_RUNS = self.previous_stopped
        agent_server.STOP_REQUESTS = self.previous_stop_requests

    @staticmethod
    def write_file_record(root: Path, file_id: str) -> None:
        record_dir = root / file_id
        record_dir.mkdir()
        (record_dir / "meta.json").write_text(json.dumps({
            "path": f"/uploads/{file_id}.png",
            "filename": f"{file_id}.png",
            "content_type": "image/png",
        }))

    async def test_native_steer_runs_b_now_then_leaves_a_c_without_active_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            files_root = Path(temporary)
            self.write_file_record(files_root, "active-image")
            self.write_file_record(files_root, "steer-image")
            append_event = AsyncMock(return_value={})
            with patch.object(agent_server, "FILES_ROOT", files_root), \
                    patch.object(agent_server, "append_event", append_event):
                result = await agent_server.run_queued_turn_now("chat-1", "B")

        self.assertTrue(result["native_steer"])
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["A", "C"],
        )
        self.assertNotIn("chat-1", agent_server.RUN_NOW_TURNS)
        self.assertNotIn("chat-1", agent_server.STEERING_SESSIONS)
        input_items = self.native_turn.steer.await_args.args[0]
        request_text = input_items[0]["text"]
        self.assertIn("Steer with B", request_text)
        self.assertIn("/uploads/steer-image.png", request_text)
        self.assertNotIn("Active request", request_text)
        self.assertNotIn("/uploads/active-image.png", request_text)
        event_payload = append_event.await_args.args[2]
        self.assertTrue(event_payload["native_steer"])
        self.assertFalse(event_payload["replays_interrupted_message"])
        self.assertEqual(event_payload["remaining"], 2)

    async def test_native_steer_failure_restores_selected_at_original_position(self) -> None:
        self.native_turn.steer.side_effect = RuntimeError("steer failed")
        with patch.object(agent_server, "append_event", new_callable=AsyncMock) as append_event:
            with self.assertRaisesRegex(RuntimeError, "steer failed"):
                await agent_server.run_queued_turn_now("chat-1", "B")

        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["A", "B", "C"],
        )
        self.assertNotIn("chat-1", agent_server.RUN_NOW_TURNS)
        self.assertNotIn("chat-1", agent_server.STEERING_SESSIONS)
        append_event.assert_not_awaited()

    async def test_native_steer_success_schedules_once_if_turn_finishes_during_steer(self) -> None:
        async def finish_during_steer(_input_items: list[dict[str, object]]) -> str:
            agent_server.ACTIVE.pop("chat-1", None)
            agent_server.BUSY_SESSIONS.discard("chat-1")
            agent_server.CURRENT_TURNS.pop("chat-1", None)
            return "turn-1"

        self.native_turn.steer.side_effect = finish_during_steer
        with patch.object(agent_server, "append_event", new_callable=AsyncMock, return_value={}), \
                patch.object(agent_server, "schedule_next_queued_turn") as schedule:
            result = await agent_server.run_queued_turn_now("chat-1", "B")

        self.assertTrue(result["native_steer"])
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["A", "C"],
        )
        schedule.assert_called_once_with("chat-1")

    async def test_native_steer_failure_schedules_once_if_turn_finishes_during_steer(self) -> None:
        async def finish_during_steer(_input_items: list[dict[str, object]]) -> str:
            agent_server.ACTIVE.pop("chat-1", None)
            agent_server.BUSY_SESSIONS.discard("chat-1")
            agent_server.CURRENT_TURNS.pop("chat-1", None)
            raise RuntimeError("turn already completed")

        self.native_turn.steer.side_effect = finish_during_steer
        with patch.object(agent_server, "append_event", new_callable=AsyncMock), \
                patch.object(agent_server, "schedule_next_queued_turn") as schedule:
            with self.assertRaisesRegex(RuntimeError, "turn already completed"):
                await agent_server.run_queued_turn_now("chat-1", "B")

        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["chat-1"]],
            ["A", "B", "C"],
        )
        schedule.assert_called_once_with("chat-1")

    async def test_stop_turn_uses_native_interrupt_without_killing_process(self) -> None:
        with patch.object(agent_server, "terminate_process_tree", new_callable=AsyncMock) as terminate:
            result = await agent_server.stop_turn(
                "chat-1",
                emit_event=False,
                schedule_queue=False,
            )

        self.native_turn.interrupt.assert_awaited_once_with()
        terminate.assert_not_awaited()
        self.assertTrue(agent_server.ACTIVE["chat-1"]["stop_requested"])
        self.assertIn("run-active", agent_server.STOPPED_RUNS)
        self.assertTrue(result["native_interrupt"])

    async def test_failed_native_interrupt_falls_back_to_process_termination(self) -> None:
        self.native_turn.interrupt.side_effect = RuntimeError("interrupt failed")
        with patch.object(agent_server, "terminate_process_tree", new_callable=AsyncMock) as terminate:
            result = await agent_server.stop_turn(
                "chat-1",
                emit_event=False,
                schedule_queue=False,
            )

        terminate.assert_awaited_once_with(agent_server.ACTIVE["chat-1"]["proc"])
        self.assertFalse(result["native_interrupt"])


if __name__ == "__main__":
    unittest.main()
