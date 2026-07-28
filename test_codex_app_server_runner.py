import asyncio
import time
import unittest
from collections import deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import agent_server
from codex_app_server import (
    CodexAppServerDisconnected,
    CodexAppServerRequestError,
)


class FakeTurn:
    def __init__(
        self,
        notifications: list[dict[str, object]] | None = None,
        *,
        turn_id: str = "turn-native",
        steer_error: BaseException | None = None,
    ) -> None:
        self.turn_id = turn_id
        self.steer_error = steer_error
        self.notifications: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        for notification in notifications or []:
            self.notifications.put_nowait(notification)
        self.steer_calls: list[tuple[list[dict[str, object]], str | None]] = []
        self.interrupt_calls = 0
        self.close_calls = 0

    async def next_notification(
        self,
        timeout: float | None = None,
    ) -> dict[str, object]:
        waiter = self.notifications.get()
        value = (
            await waiter
            if timeout is None
            else await asyncio.wait_for(waiter, timeout)
        )
        if isinstance(value, BaseException):
            raise value
        return value

    async def steer(
        self,
        input_items: list[dict[str, object]],
        *,
        client_user_message_id: str | None = None,
    ) -> str:
        self.steer_calls.append((input_items, client_user_message_id))
        if self.steer_error is not None:
            raise self.steer_error
        return self.turn_id

    async def interrupt(self) -> None:
        self.interrupt_calls += 1

    async def close(self) -> None:
        self.close_calls += 1

    def adopt_turn_id(self, turn_id: str) -> None:
        self.turn_id = turn_id

    def feed(self, notification: dict[str, object]) -> None:
        self.notifications.put_nowait(notification)


class FakeManager:
    def __init__(
        self,
        turn: FakeTurn | None = None,
        *,
        start_turn_error: BaseException | None = None,
        turns: list[FakeTurn] | None = None,
        read_thread_result: dict[str, object] | None = None,
    ) -> None:
        self.turn = turn or FakeTurn()
        self.turns = list(turns or [])
        self.start_turn_error = start_turn_error
        self.read_thread_result = read_thread_result
        self.start_calls = 0
        self.read_thread_calls: list[tuple[str, bool]] = []
        self.list_turns_calls: list[tuple[str, int, str, str]] = []
        self.turn_calls: list[
            tuple[str, list[dict[str, object]], dict[str, object]]
        ] = []

    async def start(self) -> None:
        self.start_calls += 1

    async def start_turn(
        self,
        thread_id: str,
        input_items: list[dict[str, object]],
        *,
        overrides: dict[str, object] | None = None,
    ) -> FakeTurn:
        self.turn_calls.append((thread_id, input_items, dict(overrides or {})))
        if self.start_turn_error is not None:
            raise self.start_turn_error
        if self.turns:
            return self.turns.pop(0)
        return self.turn

    async def read_thread(
        self,
        thread_id: str,
        *,
        include_turns: bool = False,
    ) -> dict[str, object]:
        self.read_thread_calls.append((thread_id, include_turns))
        if self.read_thread_result is not None:
            return self.read_thread_result
        return {"id": thread_id, "turns": []}

    async def list_turns(
        self,
        thread_id: str,
        *,
        limit: int = 4,
        items_view: str = "full",
        sort_direction: str = "desc",
    ) -> list[dict[str, object]]:
        self.list_turns_calls.append(
            (thread_id, limit, items_view, sort_direction)
        )
        if self.read_thread_result is not None:
            turns = self.read_thread_result.get("turns")
            if isinstance(turns, list):
                return [
                    turn for turn in turns
                    if isinstance(turn, dict)
                ]
        return []


def completed_notification(status: str = "completed") -> dict[str, object]:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "thread-native",
            "turnId": "turn-native",
            "turn": {"id": "turn-native", "status": status},
        },
    }


def agent_message(
    item_id: str,
    text: str,
    phase: str,
) -> dict[str, object]:
    return {
        "method": "item/completed",
        "params": {
            "threadId": "thread-native",
            "turnId": "turn-native",
            "item": {
                "id": item_id,
                "type": "agentMessage",
                "text": text,
                "phase": phase,
            },
        },
    }


class CodexAppServerRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_stop_requests = agent_server.STOP_REQUESTS
        self.previous_stopped_runs = agent_server.STOPPED_RUNS
        self.previous_queued = agent_server.QUEUED_TURNS
        self.previous_run_now = agent_server.RUN_NOW_TURNS
        self.previous_steering = agent_server.STEERING_SESSIONS
        self.previous_run_metadata = agent_server.RUN_METADATA

        self.cwd = str(Path(__file__).resolve().parent.parent)
        self.session = {
            "id": "chat-native",
            "backend": agent_server.BACKEND_CODEX,
            "cwd": self.cwd,
            "session_id": "thread-native",
            "codex_thread_id": "thread-native",
        }
        agent_server.STORE.sessions = {"chat-native": self.session}
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = {"chat-native"}
        agent_server.CURRENT_TURNS = {
            "chat-native": {
                "run_id": "run-original",
                "prompt": "Original request",
                "file_ids": [],
                "backend": agent_server.BACKEND_CODEX,
            }
        }
        agent_server.STOP_REQUESTS = set()
        agent_server.STOPPED_RUNS = set()
        agent_server.QUEUED_TURNS = {}
        agent_server.RUN_NOW_TURNS = {}
        agent_server.STEERING_SESSIONS = set()
        agent_server.RUN_METADATA = {}

    async def asyncTearDown(self) -> None:
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.STOP_REQUESTS = self.previous_stop_requests
        agent_server.STOPPED_RUNS = self.previous_stopped_runs
        agent_server.QUEUED_TURNS = self.previous_queued
        agent_server.RUN_NOW_TURNS = self.previous_run_now
        agent_server.STEERING_SESSIONS = self.previous_steering
        agent_server.RUN_METADATA = self.previous_run_metadata

    def runner_patches(
        self,
        manager: FakeManager,
    ) -> tuple[ExitStack, AsyncMock, AsyncMock, AsyncMock]:
        events = AsyncMock(return_value={})
        finished = AsyncMock(return_value={})
        exec_fallback = AsyncMock()

        async def wait_for_cancel(*_args: object, **_kwargs: object) -> None:
            await asyncio.Event().wait()

        stack = ExitStack()
        stack.enter_context(
            patch.object(
                agent_server,
                "codex_app_server_manager",
                AsyncMock(return_value=manager),
            )
        )
        stack.enter_context(
            patch.object(
                agent_server,
                "ensure_codex_app_server_thread",
                AsyncMock(return_value=("thread-native", "policy-hash")),
            )
        )
        stack.enter_context(
            patch.object(
                agent_server,
                "capture_git_baseline",
                AsyncMock(return_value={"head": "baseline"}),
            )
        )
        stack.enter_context(
            patch.object(agent_server, "watch_manifest_artifacts", wait_for_cancel)
        )
        stack.enter_context(patch.object(agent_server, "append_event", events))
        stack.enter_context(
            patch.object(agent_server, "append_turn_finished_event", finished)
        )
        stack.enter_context(
            patch.object(agent_server, "collect_manifest", AsyncMock())
        )
        stack.enter_context(
            patch.object(
                agent_server,
                "collect_recent_leftover_manifests",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch.object(agent_server, "publish_turn_code_diff", AsyncMock())
        )
        stack.enter_context(
            patch.object(agent_server, "release_turn_slot", AsyncMock())
        )
        stack.enter_context(
            patch.object(
                agent_server,
                "touch_codex_app_server_thread",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch.object(
                agent_server,
                "unpin_codex_app_server_thread",
                AsyncMock(),
            )
        )
        stack.enter_context(
            patch.object(agent_server, "run_codex_exec", exec_fallback)
        )
        stack.enter_context(
            patch.object(
                agent_server,
                "record_runtime_failure",
                runtime_failure := Mock(),
            )
        )
        self.runtime_failure = runtime_failure
        stack.enter_context(
            patch.object(agent_server, "record_runtime_success", Mock())
        )
        stack.enter_context(
            patch.object(
                agent_server,
                "should_schedule_queue_after_finish",
                return_value=False,
            )
        )
        return stack, events, finished, exec_fallback

    async def test_dispatcher_uses_app_server_and_only_auto_enables_fallback(
        self,
    ) -> None:
        app_server = AsyncMock()
        exec_runner = AsyncMock()
        manifest = Path(self.cwd) / ".runner-test-manifest.json"
        with patch.object(
            agent_server,
            "run_codex_app_server",
            app_server,
        ), patch.object(
            agent_server,
            "run_codex_exec",
            exec_runner,
        ), patch.object(
            agent_server,
            "CODEX_TRANSPORT",
            agent_server.CODEX_TRANSPORT_AUTO,
        ):
            await agent_server.run_codex(
                "chat-native",
                "run-original",
                "Current text",
                dict(self.session),
                manifest,
            )

        app_server.assert_awaited_once_with(
            "chat-native",
            "run-original",
            "Current text",
            self.session,
            manifest,
            allow_exec_fallback=True,
            interactive_app_server=False,
        )
        exec_runner.assert_not_awaited()

        app_server.reset_mock()
        with patch.object(
            agent_server,
            "run_codex_app_server",
            app_server,
        ), patch.object(
            agent_server,
            "run_codex_exec",
            exec_runner,
        ), patch.object(
            agent_server,
            "CODEX_TRANSPORT",
            agent_server.CODEX_TRANSPORT_APP_SERVER,
        ):
            await agent_server.run_codex(
                "chat-native",
                "run-original",
                "Current text",
                dict(self.session),
                manifest,
            )

        self.assertFalse(app_server.await_args.kwargs["allow_exec_fallback"])

    async def test_dispatcher_preserves_explicit_exec_compatibility(self) -> None:
        app_server = AsyncMock()
        exec_runner = AsyncMock()
        manifest = Path(self.cwd) / ".runner-test-manifest.json"
        with patch.object(
            agent_server,
            "run_codex_app_server",
            app_server,
        ), patch.object(
            agent_server,
            "run_codex_exec",
            exec_runner,
        ), patch.object(
            agent_server,
            "CODEX_TRANSPORT",
            agent_server.CODEX_TRANSPORT_EXEC,
        ):
            await agent_server.run_codex(
                "chat-native",
                "run-original",
                "Current text",
                dict(self.session),
                manifest,
            )

        exec_runner.assert_awaited_once_with(
            "chat-native",
            "run-original",
            "Current text",
            self.session,
            manifest,
        )
        app_server.assert_not_awaited()

    async def test_turn_start_gets_only_current_text_and_maps_message_phases(self) -> None:
        turn = FakeTurn(
            [
                agent_message("msg-commentary", "Working through it.", "commentary"),
                agent_message("msg-final", "Completed result.", "final_answer"),
                completed_notification(),
            ]
        )
        manager = FakeManager(turn)
        stack, events, finished, exec_fallback = self.runner_patches(manager)
        with stack:
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Only the current user message",
                dict(self.session),
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=True,
            )

        self.assertEqual(len(manager.turn_calls), 1)
        thread_id, input_items, overrides = manager.turn_calls[0]
        self.assertEqual(thread_id, "thread-native")
        self.assertEqual(
            input_items,
            [
                {
                    "type": "text",
                    "text": "Only the current user message",
                    "text_elements": [],
                }
            ],
        )
        self.assertNotIn("[AgentsDock context]", str(input_items))
        self.assertNotIn("developerInstructions", overrides)
        exec_fallback.assert_not_awaited()

        event_pairs = [
            (call.args[1], call.args[2])
            for call in events.await_args_list
            if len(call.args) >= 3
        ]
        reasoning = [
            payload for event_type, payload in event_pairs
            if event_type == "reasoning_summary"
        ]
        assistant = [
            payload for event_type, payload in event_pairs
            if event_type == "assistant_text"
        ]
        self.assertEqual(
            [payload["text"] for payload in reasoning],
            ["Working through it."],
        )
        self.assertEqual(reasoning[0]["phase"], "commentary")
        self.assertEqual(
            [payload["text"] for payload in assistant],
            ["Completed result."],
        )
        self.assertEqual(
            finished.await_args.args[1]["result_text"],
            "Completed result.",
        )

    async def test_interactive_turn_applies_saved_security_controls(self) -> None:
        turn = FakeTurn(
            [
                agent_message("interactive-final", "Done.", "final_answer"),
                completed_notification(),
            ]
        )
        manager = FakeManager(turn)
        session = {
            **self.session,
            "codex_approval_policy": "on-request",
            "codex_sandbox_mode": "workspace-write",
            "codex_approvals_reviewer": "user",
        }
        stack, _events, _finished, exec_fallback = self.runner_patches(manager)
        with stack:
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Interactive request",
                session,
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=False,
                interactive_app_server=True,
            )

        overrides = manager.turn_calls[0][2]
        self.assertEqual(overrides["approvalPolicy"], "on-request")
        self.assertEqual(
            overrides["sandboxPolicy"],
            {"type": "workspaceWrite"},
        )
        self.assertEqual(overrides["approvalsReviewer"], "user")
        exec_fallback.assert_not_awaited()

    async def test_permission_profile_never_combines_with_sandbox_policy(self) -> None:
        turn = FakeTurn(
            [
                agent_message("profile-final", "Done.", "final_answer"),
                completed_notification(),
            ]
        )
        manager = FakeManager(turn)
        session = {
            **self.session,
            "codex_permission_profile": ":read-only",
            "codex_approval_policy": "on-request",
            "codex_sandbox_mode": "workspace-write",
        }
        stack, _events, _finished, _exec_fallback = self.runner_patches(manager)
        with stack:
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Profile request",
                session,
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=False,
                interactive_app_server=True,
            )

        overrides = manager.turn_calls[0][2]
        self.assertEqual(overrides["permissions"], ":read-only")
        self.assertNotIn("sandboxPolicy", overrides)
        self.assertEqual(overrides["approvalPolicy"], "on-request")

    async def test_explicit_turn_start_rejection_uses_exec_fallback(self) -> None:
        rejection = CodexAppServerRequestError(
            "turn/start",
            {"code": -32602, "message": "invalid turn"},
        )
        manager = FakeManager(start_turn_error=rejection)
        stack, events, _finished, exec_fallback = self.runner_patches(manager)
        with stack:
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Safe to retry exactly once",
                dict(self.session),
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=True,
            )

        exec_fallback.assert_awaited_once()
        self.assertEqual(exec_fallback.await_args.args[2], "Safe to retry exactly once")
        fallback_events = [
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "codex_transport_fallback"
        ]
        self.assertEqual(len(fallback_events), 1)
        self.assertEqual(
            fallback_events[0]["from"],
            agent_server.CODEX_TRANSPORT_APP_SERVER,
        )

    async def test_ambiguous_post_send_failure_never_replays_through_exec(self) -> None:
        pending_turn = FakeTurn(
            [
                agent_message(
                    "msg-final",
                    "Observed the accepted turn.",
                    "final_answer",
                ),
                completed_notification(),
            ]
        )
        disconnected = CodexAppServerDisconnected(
            "connection closed after write",
            request_sent=True,
            safe_to_retry=False,
        )
        disconnected.pending_turn = pending_turn  # type: ignore[assignment]
        manager = FakeManager(start_turn_error=disconnected)
        stack, events, finished, exec_fallback = self.runner_patches(manager)
        with stack:
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Do not duplicate this message",
                dict(self.session),
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=True,
            )

        exec_fallback.assert_not_awaited()
        self.assertFalse(
            any(
                call.args[1] == "codex_transport_fallback"
                for call in events.await_args_list
            )
        )
        self.assertEqual(finished.await_args.args[1]["exit_code"], 0)
        self.assertEqual(
            finished.await_args.args[1]["result_text"],
            "Observed the accepted turn.",
        )

    async def test_stop_interrupts_native_turn_without_killing_shared_process(self) -> None:
        turn = FakeTurn()
        shared_process = object()
        agent_server.ACTIVE["chat-native"] = {
            "run_id": "run-original",
            "backend": agent_server.BACKEND_CODEX,
            "transport": agent_server.CODEX_TRANSPORT_APP_SERVER,
            "proc": shared_process,
            "provider_turn_ready": True,
            "provider_session_id": "thread-native",
            "codex_app_server_turn": turn,
        }

        with patch.object(
            agent_server,
            "terminate_process_tree",
            AsyncMock(),
        ) as terminate, patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ):
            result = await agent_server.stop_turn("chat-native")

        self.assertTrue(result["native_interrupt"])
        self.assertEqual(turn.interrupt_calls, 1)
        terminate.assert_not_awaited()

    async def test_native_run_now_steers_runner_and_emits_queued_id(self) -> None:
        turn = FakeTurn()
        manager = FakeManager(turn)
        agent_server.QUEUED_TURNS["chat-native"] = deque(
            [
                {
                    "queued_id": "queued-steer",
                    "prompt": "Steering message only",
                    "display_prompt": "Steering message only",
                    "file_ids": [],
                    "display_file_ids": [],
                    "backend": agent_server.BACKEND_CODEX,
                }
            ]
        )
        stack, events, finished, exec_fallback = self.runner_patches(manager)
        with stack:
            runner = asyncio.create_task(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Original request",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                )
            )
            for _ in range(100):
                active = agent_server.ACTIVE.get("chat-native") or {}
                if active.get("provider_turn_ready"):
                    break
                await asyncio.sleep(0)
            else:
                self.fail("native provider turn never became ready")

            run_now = await asyncio.wait_for(
                agent_server.run_queued_turn_now(
                    "chat-native",
                    "queued-steer",
                ),
                timeout=2,
            )
            turn.feed(
                agent_message(
                    "msg-final",
                    "Finished after steering.",
                    "final_answer",
                )
            )
            turn.feed(completed_notification())
            await asyncio.wait_for(runner, timeout=2)

        self.assertTrue(run_now["native_steer"])
        self.assertEqual(run_now["queued_id"], "queued-steer")
        self.assertNotIn("chat-native", agent_server.QUEUED_TURNS)
        self.assertEqual(len(turn.steer_calls), 1)
        steer_input, steer_message_id = turn.steer_calls[0]
        self.assertEqual(
            steer_input,
            [
                {
                    "type": "text",
                    "text": "Steering message only",
                    "text_elements": [],
                }
            ],
        )
        self.assertEqual(steer_message_id, run_now["run_id"])
        self.assertNotIn("[Interrupted message]", str(steer_input))
        exec_fallback.assert_not_awaited()

        turn_started = [
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "turn_started"
        ]
        run_now_events = [
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "turn_queue_run_now"
        ]
        self.assertEqual(len(turn_started), 1)
        self.assertEqual(turn_started[0]["queued_id"], "queued-steer")
        self.assertEqual(turn_started[0]["run_id"], run_now["run_id"])
        self.assertEqual(run_now_events[0]["queued_id"], "queued-steer")
        self.assertFalse(run_now_events[0]["replays_interrupted_message"])
        self.assertEqual(
            finished.await_args.args[1]["run_id"],
            run_now["run_id"],
        )

    async def test_simultaneous_completion_and_steer_settles_force_send(self) -> None:
        turn = FakeTurn([completed_notification()])

        class GatedManager(FakeManager):
            def __init__(self) -> None:
                super().__init__(turn)
                self.turn_start_called = asyncio.Event()
                self.release_turn_start = asyncio.Event()

            async def start_turn(
                self,
                thread_id: str,
                input_items: list[dict[str, object]],
                *,
                overrides: dict[str, object] | None = None,
            ) -> FakeTurn:
                self.turn_calls.append(
                    (thread_id, input_items, dict(overrides or {}))
                )
                self.turn_start_called.set()
                await self.release_turn_start.wait()
                return turn

        manager = GatedManager()
        agent_server.QUEUED_TURNS["chat-native"] = deque(
            [
                {
                    "queued_id": "queued-at-completion",
                    "prompt": "Too late to steer",
                    "file_ids": [],
                    "backend": agent_server.BACKEND_CODEX,
                }
            ]
        )
        stack, _events, _finished, _exec_fallback = self.runner_patches(manager)
        with stack:
            runner = asyncio.create_task(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Original request",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                    allow_resume_rollover=False,
                )
            )
            await asyncio.wait_for(manager.turn_start_called.wait(), timeout=1)

            active = agent_server.ACTIVE["chat-native"]
            active["provider_turn_ready"] = True
            active["provider_session_id"] = "thread-native"
            force_send = asyncio.create_task(
                agent_server.run_queued_turn_now(
                    "chat-native",
                    "queued-at-completion",
                )
            )
            native_queue = active["native_steer_queue"]
            for _ in range(100):
                if native_queue.qsize() == 1:
                    break
                await asyncio.sleep(0)
            else:
                self.fail("Force Send never reached the native runner queue")

            manager.release_turn_start.set()
            await asyncio.wait_for(runner, timeout=2)
            with self.assertRaises(agent_server.CodexAppServerError):
                await asyncio.wait_for(force_send, timeout=2)

        self.assertTrue(force_send.done())

    async def test_ambiguous_steer_error_is_not_requeued(self) -> None:
        uncertain = CodexAppServerDisconnected(
            "connection closed after steering write",
            request_sent=True,
            safe_to_retry=False,
        )
        turn = FakeTurn(steer_error=uncertain)
        manager = FakeManager(turn)
        agent_server.QUEUED_TURNS["chat-native"] = deque(
            [
                {
                    "queued_id": "queued-uncertain",
                    "prompt": "Do not replay this steer",
                    "file_ids": [],
                    "backend": agent_server.BACKEND_CODEX,
                }
            ]
        )
        stack, events, _finished, exec_fallback = self.runner_patches(manager)
        with stack:
            runner = asyncio.create_task(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Original request",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                    allow_resume_rollover=False,
                )
            )
            for _ in range(100):
                if (
                    agent_server.ACTIVE.get("chat-native") or {}
                ).get("provider_turn_ready"):
                    break
                await asyncio.sleep(0)
            else:
                self.fail("native provider turn never became ready")

            force_send = asyncio.create_task(
                agent_server.run_queued_turn_now(
                    "chat-native",
                    "queued-uncertain",
                )
            )
            done, _pending = await asyncio.wait({force_send}, timeout=2)
            self.assertIn(force_send, done)
            turn.feed(completed_notification("interrupted"))
            await asyncio.wait_for(runner, timeout=2)
            with self.assertRaises(agent_server.NativeSteerHandoffError) as raised:
                await force_send

        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertFalse(raised.exception.safe_to_requeue)
        self.assertNotIn("chat-native", agent_server.QUEUED_TURNS)
        self.assertEqual(turn.interrupt_calls, 1)
        exec_fallback.assert_not_awaited()
        uncertain_errors = [
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "error"
            and call.args[2].get("delivery_unknown")
        ]
        self.assertEqual(len(uncertain_errors), 1)

    async def test_explicit_steer_rejection_is_requeued(self) -> None:
        rejection = CodexAppServerRequestError(
            "turn/steer",
            {"code": -32602, "message": "turn is no longer steerable"},
        )
        turn = FakeTurn(steer_error=rejection)
        manager = FakeManager(turn)
        selected = {
            "queued_id": "queued-rejected",
            "prompt": "Retryable steering message",
            "file_ids": [],
            "backend": agent_server.BACKEND_CODEX,
        }
        agent_server.QUEUED_TURNS["chat-native"] = deque([selected])
        stack, _events, _finished, _exec_fallback = self.runner_patches(manager)
        with stack:
            runner = asyncio.create_task(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Original request",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                    allow_resume_rollover=False,
                )
            )
            for _ in range(100):
                if (
                    agent_server.ACTIVE.get("chat-native") or {}
                ).get("provider_turn_ready"):
                    break
                await asyncio.sleep(0)
            else:
                self.fail("native provider turn never became ready")

            force_send = asyncio.create_task(
                agent_server.run_queued_turn_now(
                    "chat-native",
                    "queued-rejected",
                )
            )
            done, _pending = await asyncio.wait({force_send}, timeout=2)
            self.assertIn(force_send, done)
            turn.feed(completed_notification())
            await asyncio.wait_for(runner, timeout=2)
            with self.assertRaises(agent_server.NativeSteerHandoffError) as raised:
                await force_send

        self.assertTrue(raised.exception.safe_to_requeue)
        self.assertEqual(
            [
                item["queued_id"]
                for item in agent_server.QUEUED_TURNS["chat-native"]
            ],
            ["queued-rejected"],
        )
        self.assertEqual(turn.interrupt_calls, 0)

    async def test_pending_stop_interrupts_after_provisional_turn_binds_and_hides_output(
        self,
    ) -> None:
        pending_turn = FakeTurn(turn_id="")
        disconnected = CodexAppServerDisconnected(
            "turn/start acknowledgement lost",
            request_sent=True,
            safe_to_retry=False,
        )
        disconnected.pending_turn = pending_turn  # type: ignore[assignment]
        manager = FakeManager(start_turn_error=disconnected)
        stack, events, finished, exec_fallback = self.runner_patches(manager)
        with stack:
            runner = asyncio.create_task(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Stop this provisional turn",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                    allow_resume_rollover=False,
                )
            )
            for _ in range(100):
                active = agent_server.ACTIVE.get("chat-native") or {}
                if active.get("codex_app_server_turn") is pending_turn:
                    break
                await asyncio.sleep(0)
            else:
                self.fail("provisional native turn was never installed")

            stop_result = await agent_server.stop_turn("chat-native")
            self.assertFalse(stop_result["native_interrupt"])
            pending_turn.adopt_turn_id("turn-native")
            pending_turn.feed(
                agent_message(
                    "msg-after-stop",
                    "This output must stay hidden.",
                    "final_answer",
                )
            )
            pending_turn.feed(completed_notification("interrupted"))
            await asyncio.wait_for(runner, timeout=2)

        self.assertEqual(pending_turn.interrupt_calls, 1)
        self.assertFalse(
            any(
                call.args[1] == "assistant_text"
                for call in events.await_args_list
            )
        )
        self.assertTrue(finished.await_args.args[1]["stopped"])
        self.assertEqual(finished.await_args.args[1]["result_text"], "")
        exec_fallback.assert_not_awaited()

    async def test_unresolved_ambiguous_start_terminalizes_without_replay(self) -> None:
        pending_turn = FakeTurn(turn_id="")
        disconnected = CodexAppServerDisconnected(
            "turn/start acknowledgement lost",
            request_sent=True,
            safe_to_retry=False,
        )
        disconnected.pending_turn = pending_turn  # type: ignore[assignment]
        manager = FakeManager(start_turn_error=disconnected)
        stack, events, finished, exec_fallback = self.runner_patches(manager)
        started = time.monotonic()
        with stack, patch.object(
            agent_server,
            "CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS",
            0.05,
        ):
            await asyncio.wait_for(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Never replay this unresolved message",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                    allow_resume_rollover=True,
                ),
                timeout=0.5,
            )

        self.assertLess(time.monotonic() - started, 0.5)
        exec_fallback.assert_not_awaited()
        self.assertEqual(
            manager.list_turns_calls,
            [("thread-native", 4, "full", "desc")],
        )
        self.assertEqual(finished.await_args.args[1]["exit_code"], 1)
        delivery_errors = [
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "error"
            and call.args[2].get("delivery_unknown")
        ]
        self.assertEqual(len(delivery_errors), 1)

    async def test_thread_read_recovers_and_binds_ambiguous_start(self) -> None:
        pending_turn = FakeTurn(turn_id="")
        disconnected = CodexAppServerDisconnected(
            "turn/start acknowledgement lost",
            request_sent=True,
            safe_to_retry=False,
        )
        disconnected.pending_turn = pending_turn  # type: ignore[assignment]
        manager = FakeManager(
            start_turn_error=disconnected,
            read_thread_result={
                "id": "thread-native",
                "turns": [
                    {
                        "id": "turn-recovered",
                        "status": "completed",
                        "startedAt": time.time() + 1,
                        "items": [
                            {
                                "id": "item-current",
                                "type": "userMessage",
                                "clientId": "run-original",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "Recover this exact message",
                                    }
                                ],
                            },
                            {
                                "id": "msg-recovered",
                                "type": "agentMessage",
                                "text": "Recovered without replay.",
                                "phase": "final_answer",
                            }
                        ],
                    }
                ],
            },
        )
        stack, _events, finished, exec_fallback = self.runner_patches(manager)
        with stack:
            await asyncio.wait_for(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Recover this exact message",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                    allow_resume_rollover=True,
                ),
                timeout=2,
            )

        self.assertEqual(pending_turn.turn_id, "turn-recovered")
        self.assertEqual(
            manager.list_turns_calls,
            [("thread-native", 4, "full", "desc")],
        )
        exec_fallback.assert_not_awaited()
        self.assertEqual(finished.await_args.args[1]["exit_code"], 0)
        self.assertEqual(
            finished.await_args.args[1]["result_text"],
            "Recovered without replay.",
        )

    async def test_thread_read_does_not_adopt_a_recent_prior_turn(self) -> None:
        pending_turn = FakeTurn(turn_id="")
        disconnected = CodexAppServerDisconnected(
            "turn/start acknowledgement lost",
            request_sent=True,
            safe_to_retry=False,
        )
        disconnected.pending_turn = pending_turn  # type: ignore[assignment]
        manager = FakeManager(
            start_turn_error=disconnected,
            read_thread_result={
                "id": "thread-native",
                "turns": [
                    {
                        "id": "turn-prior",
                        "status": "completed",
                        "startedAt": time.time() + 1,
                        "items": [
                            {
                                "id": "item-prior",
                                "type": "userMessage",
                                "clientId": "run-prior",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "The previous request",
                                    }
                                ],
                            },
                            {
                                "id": "msg-prior",
                                "type": "agentMessage",
                                "text": "Previous answer",
                                "phase": "final_answer",
                            },
                        ],
                    }
                ],
            },
        )
        stack, events, finished, exec_fallback = self.runner_patches(manager)
        with stack, patch.object(
            agent_server,
            "CODEX_APP_SERVER_AMBIGUOUS_ACCEPT_SECONDS",
            0.05,
        ):
            await asyncio.wait_for(
                agent_server.run_codex_app_server(
                    "chat-native",
                    "run-original",
                    "Do not confuse this with the prior request",
                    dict(self.session),
                    Path(self.cwd) / ".runner-test-manifest.json",
                    allow_exec_fallback=True,
                    allow_resume_rollover=True,
                ),
                timeout=0.5,
            )

        self.assertEqual(pending_turn.turn_id, "")
        exec_fallback.assert_not_awaited()
        self.assertEqual(finished.await_args.args[1]["exit_code"], 1)
        self.assertEqual(finished.await_args.args[1]["result_text"], "")
        delivery_errors = [
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "error"
            and call.args[2].get("delivery_unknown")
        ]
        self.assertEqual(len(delivery_errors), 1)

    async def test_runtime_changing_force_send_uses_restart_not_native_steer(
        self,
    ) -> None:
        model, effort, service_tier = agent_server.codex_runtime_settings(
            self.session
        )
        native_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        agent_server.ACTIVE["chat-native"] = {
            "run_id": "run-original",
            "backend": agent_server.BACKEND_CODEX,
            "transport": agent_server.CODEX_TRANSPORT_APP_SERVER,
            "provider_turn_ready": True,
            "provider_session_id": "thread-native",
            "provider_model": model,
            "provider_effort": effort,
            "provider_service_tier": service_tier,
            "native_steer_queue": native_queue,
        }
        agent_server.QUEUED_TURNS["chat-native"] = deque(
            [
                {
                    "queued_id": "queued-new-runtime",
                    "prompt": "Use a different runtime",
                    "file_ids": [],
                    "backend": agent_server.BACKEND_CODEX,
                    "model": f"{model}-different",
                }
            ]
        )

        with patch.object(
            agent_server,
            "stop_turn",
            AsyncMock(return_value={"stopped": True}),
        ) as stop, patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "wait_for_steered_turn_slot",
            AsyncMock(),
        ):
            result = await agent_server.run_queued_turn_now(
                "chat-native",
                "queued-new-runtime",
            )
            await asyncio.sleep(0)

        self.assertFalse(result.get("native_steer", False))
        self.assertFalse(result["replays_interrupted_message"])
        self.assertTrue(native_queue.empty())
        stop.assert_awaited_once()
        self.assertEqual(
            agent_server.RUN_NOW_TURNS["chat-native"]["model"],
            f"{model}-different",
        )

    def test_process_snapshot_reports_pidless_app_server_as_active(self) -> None:
        snapshot = agent_server.active_process_snapshot(
            "chat-native",
            {
                "run_id": "run-original",
                "backend": agent_server.BACKEND_CODEX,
                "transport": agent_server.CODEX_TRANSPORT_APP_SERVER,
                "proc": None,
                "pid": None,
                "cwd": self.cwd,
                "argv": ["codex", "app-server", "--listen", "stdio://"],
                "started_at": time.time() - 3,
                "started_at_iso": "2026-07-27T00:00:00Z",
                "stdout_tail": deque(["working"]),
                "stdout_total_lines": 1,
            },
        )

        self.assertTrue(snapshot["active"])
        self.assertEqual(
            snapshot["transport"],
            agent_server.CODEX_TRANSPORT_APP_SERVER,
        )
        self.assertIsNone(snapshot["pid"])
        self.assertEqual(snapshot["processes"], [])
        self.assertEqual(snapshot["stdout_tail"]["text"], "working")

    async def test_failed_terminal_without_body_records_runtime_failure_and_error(
        self,
    ) -> None:
        turn = FakeTurn([completed_notification("failed")])
        manager = FakeManager(turn)
        stack, events, finished, exec_fallback = self.runner_patches(manager)
        with stack:
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Fail without an error body",
                dict(self.session),
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=True,
                allow_resume_rollover=False,
            )

        self.runtime_failure.assert_called_once_with(
            agent_server.BACKEND_CODEX,
            "Codex app-server turn failed.",
        )
        errors = [
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "error"
        ]
        self.assertEqual(
            [event["message"] for event in errors],
            ["Codex app-server turn failed."],
        )
        self.assertEqual(finished.await_args.args[1]["exit_code"], 1)
        exec_fallback.assert_not_awaited()

    async def test_notification_backlog_failure_interrupts_the_provider_turn(
        self,
    ) -> None:
        turn = FakeTurn(
            [
                CodexAppServerDisconnected(
                    "app-server notification backlog exceeded its safety limit",
                    request_sent=True,
                    safe_to_retry=False,
                )
            ]
        )
        manager = FakeManager(turn)
        stack, _events, finished, exec_fallback = self.runner_patches(manager)
        with stack:
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Produce enough output to exercise backlog cleanup",
                dict(self.session),
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=True,
                allow_resume_rollover=False,
            )

        self.assertEqual(turn.interrupt_calls, 1)
        exec_fallback.assert_not_awaited()
        self.assertEqual(finished.await_args.args[1]["exit_code"], 1)

    async def test_tool_output_is_tail_bounded(self) -> None:
        turn = FakeTurn(
            [
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread-native",
                        "turnId": "turn-native",
                        "itemId": "tool-large",
                        "delta": "A" * 40,
                    },
                },
                {
                    "method": "item/commandExecution/outputDelta",
                    "params": {
                        "threadId": "thread-native",
                        "turnId": "turn-native",
                        "itemId": "tool-large",
                        "delta": "B" * 40,
                    },
                },
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-native",
                        "turnId": "turn-native",
                        "item": {
                            "id": "tool-large",
                            "type": "commandExecution",
                            "command": "large-output",
                            "status": "completed",
                            "exitCode": 0,
                        },
                    },
                },
                completed_notification(),
            ]
        )
        manager = FakeManager(turn)
        stack, events, _finished, _exec_fallback = self.runner_patches(manager)
        with stack, patch.object(
            agent_server,
            "CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS",
            32,
        ):
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Bound tool output",
                dict(self.session),
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=True,
                allow_resume_rollover=False,
            )

        tool_finished = next(
            call.args[2]
            for call in events.await_args_list
            if call.args[1] == "tool_finished"
        )
        self.assertTrue(
            tool_finished["output"].startswith(
                "[Earlier tool output truncated by AgentsServer]"
            )
        )
        self.assertTrue(tool_finished["output"].endswith("B" * 32))

    async def test_slow_websocket_does_not_block_other_subscribers(self) -> None:
        class FastWebSocket:
            def __init__(self) -> None:
                self.events: list[dict[str, object]] = []

            async def send_json(self, event: dict[str, object]) -> None:
                self.events.append(event)

        class SlowWebSocket:
            async def send_json(self, _event: dict[str, object]) -> None:
                await asyncio.Event().wait()

        hub = agent_server.SubscriberHub()
        fast = FastWebSocket()
        slow = SlowWebSocket()
        hub._subscribers["chat-native"] = {fast, slow}  # type: ignore[assignment]
        event = {"type": "assistant_text", "text": "ready"}
        with patch.object(
            agent_server,
            "WEBSOCKET_SEND_TIMEOUT_SECONDS",
            0.01,
        ):
            await asyncio.wait_for(
                hub.broadcast("chat-native", event),
                timeout=0.2,
            )

        self.assertEqual(fast.events, [event])
        self.assertEqual(hub._subscribers["chat-native"], {fast})

    async def test_silent_resumed_thread_rolls_over_once_with_app_server(self) -> None:
        first_turn = FakeTurn([completed_notification()])
        second_turn = FakeTurn(
            [
                agent_message(
                    "msg-after-rollover",
                    "Fresh app-server thread completed.",
                    "final_answer",
                ),
                completed_notification(),
            ]
        )
        manager = FakeManager(turns=[first_turn, second_turn])
        fresh_session = {
            "id": "chat-native",
            "backend": agent_server.BACKEND_CODEX,
            "cwd": self.cwd,
            "memory_seed": "bounded context",
            "memory_seed_used": False,
        }
        rollover = AsyncMock(return_value=(fresh_session, "bounded context"))
        ensure = AsyncMock(
            side_effect=[
                ("thread-native", "old-policy"),
                ("thread-fresh", "fresh-policy"),
            ]
        )
        stack, _events, finished, exec_fallback = self.runner_patches(manager)
        with stack, patch.object(
            agent_server,
            "ensure_codex_app_server_thread",
            ensure,
        ), patch.object(
            agent_server,
            "rollover_codex_provider_session",
            rollover,
        ):
            await agent_server.run_codex_app_server(
                "chat-native",
                "run-original",
                "Continue on a healthy thread",
                dict(self.session),
                Path(self.cwd) / ".runner-test-manifest.json",
                allow_exec_fallback=True,
            )

        self.assertEqual(len(manager.turn_calls), 2)
        self.assertEqual(
            [call[0] for call in manager.turn_calls],
            ["thread-native", "thread-fresh"],
        )
        rollover.assert_awaited_once()
        self.assertFalse(
            rollover.await_args.kwargs["memory_seed_used"],
        )
        exec_fallback.assert_not_awaited()
        finished.assert_awaited_once()
        self.assertEqual(
            finished.await_args.args[1]["result_text"],
            "Fresh app-server thread completed.",
        )


if __name__ == "__main__":
    unittest.main()
