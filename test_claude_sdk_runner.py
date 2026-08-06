import asyncio
import sys
import types
import unittest
from collections import deque
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import agent_server
from claude_sdk_client import ClaudeSDKQueryError, ClaudeSDKUnavailable


class FakeClaudeRun:
    def __init__(self, messages: list[object] | None = None) -> None:
        self.messages: asyncio.Queue[object] = asyncio.Queue()
        for message in messages or []:
            self.messages.put_nowait(message)
        self.interrupt_calls = 0

    async def __anext__(self) -> object:
        return await self.messages.get()

    async def wait_result(self) -> object:
        return {"type": "result", "result": "done", "session_id": "provider"}

    async def interrupt(self) -> bool:
        self.interrupt_calls += 1
        return True


class FailingClaudeRun(FakeClaudeRun):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self.error = error

    async def __anext__(self) -> object:
        raise self.error

    async def wait_result(self) -> object:
        raise self.error


class FakeClaudePrintStdin:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeClaudePrintStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = deque(chunks)

    async def readline(self) -> bytes:
        return self.chunks.popleft() if self.chunks else b""

    async def read(self) -> bytes:
        return b"".join(self.chunks)


class FakeClaudePrintProcess:
    def __init__(self, stdout_chunks: list[bytes]) -> None:
        self.pid = 4242
        self.returncode = 0
        self.stdin = FakeClaudePrintStdin()
        self.stdout = FakeClaudePrintStream(stdout_chunks)
        self.stderr = FakeClaudePrintStream([])


class FakeClaudeManager:
    def __init__(self, handle: FakeClaudeRun | None = None) -> None:
        self.handle = handle or FakeClaudeRun()
        self.start_calls: list[tuple[object, ...]] = []
        self.evict_calls: list[tuple[str, bool]] = []
        self.owner_token = "fake-claude-owner"
        self.active_run_id: str | None = None
        self.context_usage_response: tuple[dict[str, Any], int] | None = None
        self.context_usage_calls: list[tuple[str, str | None]] = []

    async def start_run(
        self,
        chat_id: str,
        prompt: str,
        *,
        run_id: str,
        options: object,
        configuration_key: str,
        query_session_id: str | None = None,
        on_supervisor_ready: object | None = None,
    ) -> FakeClaudeRun:
        self.start_calls.append(
            (
                chat_id,
                prompt,
                run_id,
                options,
                configuration_key,
                query_session_id,
            )
        )
        self.active_run_id = run_id
        if on_supervisor_ready is not None:
            await on_supervisor_ready(self.owner_token)  # type: ignore[operator]
        return self.handle

    async def evict(self, chat_id: str, *, force: bool = False) -> bool:
        self.evict_calls.append((chat_id, force))
        self.active_run_id = None
        return True

    def owns_active_run(
        self,
        chat_id: str,
        ownership_token: str,
        run_id: str,
    ) -> bool:
        return (
            chat_id == "chat-claude"
            and ownership_token == self.owner_token
            and run_id == self.active_run_id
        )

    async def get_context_usage(
        self,
        chat_id: str,
        *,
        ownership_token: str | None = None,
    ) -> tuple[dict[str, Any], int] | None:
        self.context_usage_calls.append((chat_id, ownership_token))
        return self.context_usage_response


class SequencedClaudeManager(FakeClaudeManager):
    def __init__(self, handles: list[FakeClaudeRun]) -> None:
        super().__init__(handles[0])
        self.handles = deque(handles)

    async def start_run(
        self,
        chat_id: str,
        prompt: str,
        *,
        run_id: str,
        options: object,
        configuration_key: str,
        query_session_id: str | None = None,
        on_supervisor_ready: object | None = None,
    ) -> FakeClaudeRun:
        self.start_calls.append(
            (
                chat_id,
                prompt,
                run_id,
                options,
                configuration_key,
                query_session_id,
            )
        )
        self.active_run_id = run_id
        if on_supervisor_ready is not None:
            await on_supervisor_ready(self.owner_token)  # type: ignore[operator]
        return self.handles.popleft()


class PermissionDuringStartManager(SequencedClaudeManager):
    """Model an SDK query that requests approval before start_run returns."""

    def __init__(
        self,
        handles: list[FakeClaudeRun],
        *,
        permission_on_calls: set[int],
    ) -> None:
        super().__init__(handles)
        self.permission_on_calls = set(permission_on_calls)
        self.permission_results: list[object] = []
        self.permission_requested = asyncio.Event()

    async def start_run(
        self,
        chat_id: str,
        prompt: str,
        *,
        run_id: str,
        options: object,
        configuration_key: str,
        query_session_id: str | None = None,
        on_supervisor_ready: object | None = None,
    ) -> FakeClaudeRun:
        call_number = len(self.start_calls) + 1
        self.start_calls.append(
            (
                chat_id,
                prompt,
                run_id,
                options,
                configuration_key,
                query_session_id,
            )
        )
        self.active_run_id = run_id
        if on_supervisor_ready is not None:
            await on_supervisor_ready(self.owner_token)  # type: ignore[operator]
        # A real supervisor sets _active_run immediately before client.query;
        # can_use_tool can then fire while query/start_run is still awaiting.
        if call_number in self.permission_on_calls:
            callback = (
                options.get("can_use_tool")
                if isinstance(options, dict)
                else getattr(options, "can_use_tool")
            )
            permission_task = asyncio.create_task(callback(
                "Bash",
                {"command": "pwd"},
                {"tool_use_id": f"permission-{call_number}"},
            ))
            self.permission_requested.set()
            self.permission_results.append(await permission_task)
        return self.handles.popleft()


class BlockingCandidateStartManager(SequencedClaudeManager):
    """Hold the second query at its delivery-uncertain cancellation boundary."""

    def __init__(self, handles: list[FakeClaudeRun]) -> None:
        super().__init__(handles)
        self.candidate_query_started = asyncio.Event()

    async def start_run(
        self,
        chat_id: str,
        prompt: str,
        *,
        run_id: str,
        options: object,
        configuration_key: str,
        query_session_id: str | None = None,
        on_supervisor_ready: object | None = None,
    ) -> FakeClaudeRun:
        call_number = len(self.start_calls) + 1
        self.start_calls.append(
            (
                chat_id,
                prompt,
                run_id,
                options,
                configuration_key,
                query_session_id,
            )
        )
        self.active_run_id = run_id
        if on_supervisor_ready is not None:
            await on_supervisor_ready(self.owner_token)  # type: ignore[operator]
        if call_number == 2:
            self.candidate_query_started.set()
            await asyncio.Event().wait()
        return self.handles.popleft()


class FakePermissionResultAllow:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


class FakePermissionResultDeny:
    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


def fake_claude_sdk_modules() -> dict[str, types.ModuleType]:
    package = types.ModuleType("claude_agent_sdk")
    package.__path__ = []  # type: ignore[attr-defined]
    sdk_types = types.ModuleType("claude_agent_sdk.types")
    sdk_types.PermissionResultAllow = FakePermissionResultAllow
    sdk_types.PermissionResultDeny = FakePermissionResultDeny
    return {
        "claude_agent_sdk": package,
        "claude_agent_sdk.types": sdk_types,
    }


async def wait_forever(*_args: object, **_kwargs: object) -> None:
    await asyncio.Event().wait()


class ClaudeSDKRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.previous_sessions = agent_server.STORE.sessions
        self.previous_active = agent_server.ACTIVE
        self.previous_busy = agent_server.BUSY_SESSIONS
        self.previous_current = agent_server.CURRENT_TURNS
        self.previous_stop_requests = agent_server.STOP_REQUESTS
        self.previous_stopped_runs = agent_server.STOPPED_RUNS
        self.previous_turn_tasks = agent_server.SESSION_TURN_TASKS
        self.previous_manager = agent_server.CLAUDE_SDK_MANAGER
        self.previous_run_metadata = agent_server.RUN_METADATA
        self.previous_queue = agent_server.QUEUED_TURNS
        self.previous_run_now = agent_server.RUN_NOW_TURNS
        self.previous_steering = agent_server.STEERING_SESSIONS
        self.previous_pending_interactions = agent_server.CLAUDE_PENDING_INTERACTIONS
        self.previous_interaction_tasks = agent_server.CLAUDE_INTERACTION_HANDLER_TASKS
        self.previous_lifecycle_locks = agent_server.SESSION_LIFECYCLE_LOCKS
        self.previous_deleting = agent_server.DELETING_SESSIONS
        self.previous_deleted = agent_server.DELETED_SESSION_TOMBSTONES

        self.cwd = str(Path(__file__).resolve().parent.parent)
        self.session = {
            "id": "chat-claude",
            "backend": agent_server.BACKEND_CLAUDE,
            "cwd": self.cwd,
            "model": "claude-opus",
            "effort": "high",
        }
        agent_server.STORE.sessions = {"chat-claude": self.session}
        agent_server.ACTIVE = {}
        agent_server.BUSY_SESSIONS = {"chat-claude"}
        agent_server.CURRENT_TURNS = {
            "chat-claude": {
                "run_id": "run-claude",
                "prompt": "Prompt",
                "file_ids": [],
                "backend": agent_server.BACKEND_CLAUDE,
            }
        }
        agent_server.STOP_REQUESTS = set()
        agent_server.STOPPED_RUNS = set()
        agent_server.SESSION_TURN_TASKS = {}
        agent_server.CLAUDE_SDK_MANAGER = None
        agent_server.RUN_METADATA = {}
        agent_server.QUEUED_TURNS = {}
        agent_server.RUN_NOW_TURNS = {}
        agent_server.STEERING_SESSIONS = set()
        agent_server.CLAUDE_PENDING_INTERACTIONS = {}
        agent_server.CLAUDE_INTERACTION_HANDLER_TASKS = {}
        agent_server.SESSION_LIFECYCLE_LOCKS = {}
        agent_server.DELETING_SESSIONS = set()
        agent_server.DELETED_SESSION_TOMBSTONES = set()

    async def asyncTearDown(self) -> None:
        for tasks in agent_server.SESSION_TURN_TASKS.values():
            for task in tasks:
                if not task.done():
                    task.cancel()
        pending = [
            task
            for tasks in agent_server.SESSION_TURN_TASKS.values()
            for task in tasks
            if not task.done()
        ]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        agent_server.STORE.sessions = self.previous_sessions
        agent_server.ACTIVE = self.previous_active
        agent_server.BUSY_SESSIONS = self.previous_busy
        agent_server.CURRENT_TURNS = self.previous_current
        agent_server.STOP_REQUESTS = self.previous_stop_requests
        agent_server.STOPPED_RUNS = self.previous_stopped_runs
        agent_server.SESSION_TURN_TASKS = self.previous_turn_tasks
        agent_server.CLAUDE_SDK_MANAGER = self.previous_manager
        agent_server.RUN_METADATA = self.previous_run_metadata
        agent_server.QUEUED_TURNS = self.previous_queue
        agent_server.RUN_NOW_TURNS = self.previous_run_now
        agent_server.STEERING_SESSIONS = self.previous_steering
        agent_server.CLAUDE_PENDING_INTERACTIONS = self.previous_pending_interactions
        agent_server.CLAUDE_INTERACTION_HANDLER_TASKS = self.previous_interaction_tasks
        agent_server.SESSION_LIFECYCLE_LOCKS = self.previous_lifecycle_locks
        agent_server.DELETING_SESSIONS = self.previous_deleting
        agent_server.DELETED_SESSION_TOMBSTONES = self.previous_deleted

    async def _run_sdk_terminal_case(
        self,
        messages: list[object],
    ) -> tuple[AsyncMock, AsyncMock, Mock, Mock]:
        manager = FakeClaudeManager(FakeClaudeRun(messages))
        append_event = AsyncMock(return_value={})
        append_finished = AsyncMock(return_value={})
        runtime_success = Mock()
        runtime_failure = Mock()
        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            append_event,
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            append_finished,
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "sample_claude_context_usage",
            AsyncMock(return_value=False),
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_success",
            runtime_success,
        ), patch.object(
            agent_server,
            "record_runtime_failure",
            runtime_failure,
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            await agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            )
        return append_event, append_finished, runtime_success, runtime_failure

    async def test_empty_sdk_result_without_tools_is_visible_failure(self) -> None:
        append_event, append_finished, runtime_success, runtime_failure = (
            await self._run_sdk_terminal_case([{
                "type": "result",
                "result": "",
                "session_id": "provider",
                "terminal_reason": "end_turn",
            }])
        )

        terminal = append_finished.await_args.args[1]
        self.assertEqual(terminal["exit_code"], 1)
        self.assertEqual(terminal["result_text"], "")
        self.assertTrue(any(
            call.args[1] == "error"
            and call.args[2]["message"]
            == agent_server.CLAUDE_EMPTY_TURN_ERROR
            for call in append_event.await_args_list
        ))
        runtime_success.assert_not_called()
        runtime_failure.assert_called_once_with(
            agent_server.BACKEND_CLAUDE,
            agent_server.CLAUDE_EMPTY_TURN_ERROR,
        )

    async def test_tool_only_empty_sdk_result_remains_success(self) -> None:
        append_event, append_finished, runtime_success, runtime_failure = (
            await self._run_sdk_terminal_case([
                {
                    "type": "AssistantMessage",
                    "content": [{
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "/tmp/example"},
                    }],
                    "session_id": "provider",
                },
                {
                    "type": "UserMessage",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "done",
                    }],
                },
                {
                    "type": "result",
                    "result": "",
                    "session_id": "provider",
                    "terminal_reason": "end_turn",
                },
            ])
        )

        terminal = append_finished.await_args.args[1]
        self.assertEqual(terminal["exit_code"], 0)
        self.assertEqual(terminal["result_text"], "")
        self.assertFalse(any(
            call.args[1] == "error"
            and call.args[2].get("message")
            == agent_server.CLAUDE_EMPTY_TURN_ERROR
            for call in append_event.await_args_list
        ))
        runtime_success.assert_called_once_with(agent_server.BACKEND_CLAUDE)
        runtime_failure.assert_not_called()

    async def test_empty_print_result_without_tools_is_visible_failure(self) -> None:
        process = FakeClaudePrintProcess([
            b'{"type":"result","result":"","session_id":"provider"}\n',
        ])
        append_event = AsyncMock(return_value={})
        append_finished = AsyncMock(return_value={})
        runtime_success = Mock()
        runtime_failure = Mock()
        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_cmd",
            return_value=["claude", "-p"],
        ), patch.object(
            agent_server.asyncio,
            "create_subprocess_exec",
            AsyncMock(return_value=process),
        ), patch.object(
            agent_server,
            "process_group_for_pid",
            return_value=4242,
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            append_event,
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            append_finished,
        ), patch.object(
            agent_server,
            "append_active_stdout",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "terminate_process_tree",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "clear_active_process",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_success",
            runtime_success,
        ), patch.object(
            agent_server,
            "record_runtime_failure",
            runtime_failure,
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            await agent_server.run_claude_print(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            )

        terminal = append_finished.await_args.args[1]
        self.assertEqual(terminal["exit_code"], 1)
        self.assertEqual(terminal["result_text"], "")
        self.assertTrue(any(
            call.args[1] == "error"
            and call.args[2].get("message")
            == agent_server.CLAUDE_EMPTY_TURN_ERROR
            for call in append_event.await_args_list
        ))
        runtime_success.assert_not_called()
        runtime_failure.assert_called_once_with(
            agent_server.BACKEND_CLAUDE,
            agent_server.CLAUDE_EMPTY_TURN_ERROR,
        )

    async def test_interactive_auto_never_downgrades_to_unattended_print(self) -> None:
        sdk = AsyncMock(side_effect=ClaudeSDKUnavailable("missing SDK"))
        print_runner = AsyncMock()
        terminal_failure = AsyncMock()
        with patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_AUTO), patch.object(
            agent_server,
            "run_claude_sdk",
            sdk,
        ), patch.object(
            agent_server,
            "run_claude_print",
            print_runner,
        ), patch.object(
            agent_server,
            "finish_claude_sdk_start_failure",
            terminal_failure,
        ):
            await agent_server.run_claude(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
                interactive_agent_sdk=True,
            )

        sdk.assert_awaited_once()
        print_runner.assert_not_awaited()
        terminal_failure.assert_awaited_once()

    async def test_terminal_context_usage_is_normalized_persisted_and_broadcast(self) -> None:
        manager = FakeClaudeManager()
        manager.context_usage_response = ({
            "totalTokens": 45_000,
            "maxTokens": 188_000,
            "rawMaxTokens": 200_000,
            "percentage": 23.94,
            "model": "claude-opus-4-1",
            "categories": {"ignored": "not persisted"},
        }, 9)
        self.session["claude_session_id"] = "provider-usage"
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_owner_token": manager.owner_token,
                "stop_requested": False,
            }
        }
        broadcast = AsyncMock()
        with patch.object(
            agent_server.STORE,
            "save",
            AsyncMock(),
        ), patch.object(
            agent_server.HUB,
            "broadcast",
            broadcast,
        ):
            stored = await agent_server.sample_claude_context_usage(
                "chat-claude",
                "run-claude",
                "provider-usage",
                manager,
            )

        self.assertTrue(stored)
        snapshot = self.session["claude_context_usage_snapshot"]
        self.assertEqual(snapshot["context_tokens"], 45_000)
        self.assertEqual(snapshot["totalTokens"], 45_000)
        self.assertEqual(snapshot["maxTokens"], 188_000)
        self.assertEqual(snapshot["rawMaxTokens"], 200_000)
        self.assertEqual(snapshot["effective_context_window"], 188_000)
        self.assertEqual(snapshot["raw_context_window"], 200_000)
        self.assertEqual(snapshot["provider_generation"], 9)
        self.assertEqual(snapshot["usage_generation"], 1)
        self.assertNotIn("categories", snapshot)
        runtime = await agent_server.claude_runtime_snapshot("chat-claude")
        self.assertEqual(runtime["context_usage_state"], "available")
        self.assertEqual(runtime["context_usage_snapshot"], snapshot)
        packet = broadcast.await_args.args[1]
        self.assertEqual(packet["type"], "provider_runtime_changed")
        self.assertEqual(packet["session_id"], "chat-claude")
        self.assertNotIn("seq", packet)

    async def test_context_usage_rejects_stale_provider_and_stop_owner(self) -> None:
        manager = FakeClaudeManager()
        manager.context_usage_response = ({
            "totalTokens": 10,
            "maxTokens": 100,
            "rawMaxTokens": 120,
            "percentage": 10,
            "model": "claude",
        }, 3)
        self.session["claude_session_id"] = "current-provider"
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_owner_token": manager.owner_token,
                "stop_requested": False,
            }
        }
        self.assertFalse(await agent_server.sample_claude_context_usage(
            "chat-claude",
            "run-claude",
            "stale-provider",
            manager,
        ))
        agent_server.ACTIVE["chat-claude"]["stop_requested"] = True
        self.assertFalse(await agent_server.sample_claude_context_usage(
            "chat-claude",
            "run-claude",
            "current-provider",
            manager,
        ))
        self.assertNotIn("context_usage_snapshot", self.session)

    async def test_context_usage_timeout_does_not_wait_for_slow_eviction(self) -> None:
        manager = FakeClaudeManager()
        manager.get_context_usage = AsyncMock(side_effect=wait_forever)  # type: ignore[method-assign]

        async def slow_evict(*_args: object, **_kwargs: object) -> bool:
            await asyncio.sleep(0.05)
            return True

        manager.evict = AsyncMock(side_effect=slow_evict)  # type: ignore[method-assign]
        self.session["claude_session_id"] = "provider-usage"
        stale_snapshot = {
            "provider_session_id": "provider-usage",
            "context_tokens": 50_000,
            "usage_generation": 4,
        }
        self.session.update({
            "_context_usage_generation": 4,
            "context_usage_state": "available",
            "context_usage_snapshot": stale_snapshot,
            "claude_context_usage_snapshot": stale_snapshot,
        })
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_owner_token": manager.owner_token,
                "stop_requested": False,
            }
        }
        broadcast = AsyncMock()
        with patch.object(
            agent_server,
            "CLAUDE_CONTEXT_USAGE_TIMEOUT_SECONDS",
            0.01,
        ), patch.object(
            agent_server.STORE,
            "save",
            AsyncMock(),
        ), patch.object(
            agent_server.HUB,
            "broadcast",
            broadcast,
        ):
            self.assertFalse(await asyncio.wait_for(
                agent_server.sample_claude_context_usage(
                    "chat-claude",
                    "run-claude",
                    "provider-usage",
                    manager,
                ),
                0.1,
            ))
        manager.evict.assert_awaited_once_with("chat-claude", force=True)
        self.assertEqual(self.session["context_usage_state"], "unavailable")
        self.assertNotIn("context_usage_snapshot", self.session)
        self.assertNotIn("claude_context_usage_snapshot", self.session)
        self.assertEqual(self.session["_context_usage_generation"], 5)
        packet = broadcast.await_args.args[1]
        self.assertEqual(packet["context_usage_state"], "unavailable")
        await asyncio.sleep(0.06)

    async def test_stop_before_sampling_invalidates_without_rpc(self) -> None:
        manager = FakeClaudeManager()
        manager.get_context_usage = AsyncMock()  # type: ignore[method-assign]
        self.session["claude_session_id"] = "provider-usage"
        stale_snapshot = {
            "provider_session_id": "provider-usage",
            "context_tokens": 22_000,
            "usage_generation": 2,
        }
        self.session.update({
            "_context_usage_generation": 2,
            "context_usage_state": "available",
            "context_usage_snapshot": stale_snapshot,
            "claude_context_usage_snapshot": stale_snapshot,
        })
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_owner_token": manager.owner_token,
                "stop_requested": True,
            }
        }
        broadcast = AsyncMock()
        with patch.object(
            agent_server.STORE,
            "save",
            AsyncMock(),
        ), patch.object(
            agent_server.HUB,
            "broadcast",
            broadcast,
        ):
            self.assertFalse(await agent_server.sample_claude_context_usage(
                "chat-claude",
                "run-claude",
                "provider-usage",
                manager,
            ))

        manager.get_context_usage.assert_not_awaited()
        self.assertEqual(self.session["context_usage_state"], "unavailable")
        self.assertNotIn("context_usage_snapshot", self.session)
        self.assertEqual(
            broadcast.await_args.args[1]["context_usage_state"],
            "unavailable",
        )

    async def test_stop_during_successful_rpc_invalidates_rejected_sample(self) -> None:
        manager = FakeClaudeManager()
        sampling_started = asyncio.Event()
        release_sampling = asyncio.Event()

        async def valid_after_stop(
            *_args: object,
            **_kwargs: object,
        ) -> tuple[dict[str, Any], int]:
            sampling_started.set()
            await release_sampling.wait()
            return ({
                "totalTokens": 25_000,
                "maxTokens": 188_000,
                "rawMaxTokens": 200_000,
                "percentage": 13.3,
                "model": "claude-opus",
            }, 5)

        manager.get_context_usage = AsyncMock(  # type: ignore[method-assign]
            side_effect=valid_after_stop
        )
        self.session["claude_session_id"] = "provider-usage"
        stale_snapshot = {
            "provider_session_id": "provider-usage",
            "context_tokens": 21_000,
            "usage_generation": 6,
        }
        self.session.update({
            "_context_usage_generation": 6,
            "context_usage_state": "available",
            "context_usage_snapshot": stale_snapshot,
            "claude_context_usage_snapshot": stale_snapshot,
        })
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_owner_token": manager.owner_token,
                "stop_requested": False,
            }
        }
        broadcast = AsyncMock()
        with patch.object(
            agent_server.STORE,
            "save",
            AsyncMock(),
        ), patch.object(
            agent_server.HUB,
            "broadcast",
            broadcast,
        ):
            sampling = asyncio.create_task(
                agent_server.sample_claude_context_usage(
                    "chat-claude",
                    "run-claude",
                    "provider-usage",
                    manager,
                )
            )
            await asyncio.wait_for(sampling_started.wait(), 0.2)
            agent_server.ACTIVE["chat-claude"]["stop_requested"] = True
            release_sampling.set()
            self.assertFalse(await asyncio.wait_for(sampling, 0.2))

        self.assertEqual(self.session["context_usage_state"], "unavailable")
        self.assertNotIn("context_usage_snapshot", self.session)
        self.assertEqual(
            broadcast.await_args.args[1]["context_usage_state"],
            "unavailable",
        )

    async def test_context_usage_rpc_and_invalid_results_clear_current_snapshot(self) -> None:
        self.session["claude_session_id"] = "provider-usage"
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_owner_token": "fake-claude-owner",
                "stop_requested": False,
            }
        }
        cases = (
            AsyncMock(side_effect=RuntimeError("usage RPC failed")),
            AsyncMock(return_value=({"unexpected": True}, 7)),
        )
        for index, getter in enumerate(cases, start=1):
            with self.subTest(case=index):
                manager = FakeClaudeManager()
                manager.get_context_usage = getter  # type: ignore[method-assign]
                stale_snapshot = {
                    "provider_session_id": "provider-usage",
                    "context_tokens": 40_000 + index,
                    "usage_generation": index * 10,
                }
                self.session.update({
                    "_context_usage_generation": index * 10,
                    "context_usage_state": "available",
                    "context_usage_snapshot": stale_snapshot,
                    "claude_context_usage_snapshot": stale_snapshot,
                })
                broadcast = AsyncMock()
                with patch.object(
                    agent_server.STORE,
                    "save",
                    AsyncMock(),
                ), patch.object(
                    agent_server.HUB,
                    "broadcast",
                    broadcast,
                ):
                    self.assertFalse(await agent_server.sample_claude_context_usage(
                        "chat-claude",
                        "run-claude",
                        "provider-usage",
                        manager,
                    ))
                self.assertEqual(
                    self.session["context_usage_state"],
                    "unavailable",
                )
                self.assertNotIn("context_usage_snapshot", self.session)
                self.assertEqual(
                    broadcast.await_args.args[1]["context_usage_state"],
                    "unavailable",
                )

    async def test_failed_sample_cannot_clear_newer_generation(self) -> None:
        manager = FakeClaudeManager()
        sampling_started = asyncio.Event()
        release_sampling = asyncio.Event()

        async def invalid_after_race(
            *_args: object,
            **_kwargs: object,
        ) -> tuple[dict[str, Any], int]:
            sampling_started.set()
            await release_sampling.wait()
            return {"invalid": True}, 4

        manager.get_context_usage = AsyncMock(  # type: ignore[method-assign]
            side_effect=invalid_after_race
        )
        self.session["claude_session_id"] = "provider-usage"
        old_snapshot = {
            "provider_session_id": "provider-usage",
            "context_tokens": 20_000,
            "usage_generation": 8,
        }
        self.session.update({
            "_context_usage_generation": 8,
            "context_usage_state": "available",
            "context_usage_snapshot": old_snapshot,
            "claude_context_usage_snapshot": old_snapshot,
        })
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_owner_token": manager.owner_token,
                "stop_requested": False,
            }
        }
        broadcast = AsyncMock()
        with patch.object(
            agent_server.STORE,
            "save",
            AsyncMock(),
        ), patch.object(
            agent_server.HUB,
            "broadcast",
            broadcast,
        ):
            sampling = asyncio.create_task(
                agent_server.sample_claude_context_usage(
                    "chat-claude",
                    "run-claude",
                    "provider-usage",
                    manager,
                )
            )
            await asyncio.wait_for(sampling_started.wait(), 0.2)
            newer_snapshot = {
                "provider_session_id": "provider-usage",
                "context_tokens": 30_000,
                "usage_generation": 9,
            }
            async with agent_server.STORE._lock:
                self.session.update({
                    "_context_usage_generation": 9,
                    "context_usage_state": "available",
                    "context_usage_snapshot": newer_snapshot,
                    "claude_context_usage_snapshot": newer_snapshot,
                })
            agent_server.ACTIVE["chat-claude"]["stop_requested"] = True
            release_sampling.set()
            self.assertFalse(await asyncio.wait_for(sampling, 0.2))

        self.assertIs(self.session["context_usage_snapshot"], newer_snapshot)
        self.assertEqual(self.session["context_usage_state"], "available")
        broadcast.assert_not_awaited()

    async def test_noninteractive_client_keeps_print_compatibility(self) -> None:
        sdk = AsyncMock()
        print_runner = AsyncMock()
        with patch.object(
            agent_server,
            "run_claude_sdk",
            sdk,
        ), patch.object(
            agent_server,
            "run_claude_print",
            print_runner,
        ):
            await agent_server.run_claude(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
                interactive_agent_sdk=False,
            )

        sdk.assert_not_awaited()
        print_runner.assert_awaited_once()

    async def test_print_fallback_retires_sdk_and_preserves_resume_identity(self) -> None:
        session = {
            **self.session,
            "claude_session_id": "provider-from-print",
            "claude_session_cwd": self.cwd,
        }
        order: list[str] = []

        async def evict(*_args: object, **_kwargs: object) -> bool:
            order.append("evict")
            return True

        async def run_print(
            _session_id: str,
            _run_id: str,
            _prompt: str,
            selected_session: dict[str, object],
            _manifest_path: Path,
            **_kwargs: object,
        ) -> None:
            order.append("print")
            self.assertEqual(
                selected_session.get("claude_session_id"),
                "provider-from-print",
            )

        with patch.object(
            agent_server,
            "evict_claude_sdk_chat",
            side_effect=evict,
        ), patch.object(
            agent_server,
            "run_claude_print",
            side_effect=run_print,
        ):
            await agent_server.run_claude(
                "chat-claude",
                "run-claude",
                "Prompt",
                session,
                Path(self.cwd) / ".manifest.json",
                interactive_agent_sdk=False,
            )

        self.assertEqual(order, ["evict", "print"])

        captured_options: dict[str, object] = {}

        def make_options(**kwargs: object) -> dict[str, object]:
            captured_options.update(kwargs)
            return kwargs

        with patch.object(
            agent_server,
            "claude_sdk_cli_path",
            return_value="/usr/bin/claude",
        ), patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=("provider-from-print", None),
        ), patch.object(
            agent_server,
            "create_claude_agent_options",
            side_effect=make_options,
        ):
            agent_server.build_claude_sdk_options(
                "chat-claude",
                session,
                self.cwd,
                Path(self.cwd) / ".manifest.json",
            )

        self.assertEqual(captured_options["resume"], "provider-from-print")
        self.assertEqual(
            captured_options["extra_args"],
            {
                "replay-user-messages": None,
                "allow-dangerously-skip-permissions": None,
            },
        )

    async def test_permission_mode_options_hooks_and_plan_tools_are_wired(self) -> None:
        session = {**self.session, "claude_permission_mode": "plan"}
        captured_options: dict[str, object] = {}
        permission = AsyncMock(return_value={"behavior": "allow"})

        def make_options(**kwargs: object) -> dict[str, object]:
            captured_options.update(kwargs)
            return kwargs

        with patch.object(
            agent_server,
            "claude_sdk_cli_path",
            return_value="/usr/bin/claude",
        ), patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "create_claude_agent_options",
            side_effect=make_options,
        ), patch.object(
            agent_server,
            "handle_claude_tool_permission",
            permission,
        ):
            options, config_key, _ = agent_server.build_claude_sdk_options(
                "chat-claude",
                session,
                self.cwd,
                Path(self.cwd) / ".manifest.json",
            )
            result = await options["can_use_tool"](
                "ExitPlanMode",
                {"plan": "Proceed carefully"},
                {"tool_use_id": "exit-plan"},
            )

        self.assertEqual(captured_options["permission_mode"], "plan")
        self.assertNotIn("disallowed_tools", captured_options)
        self.assertEqual(
            captured_options["extra_args"],
            {
                "replay-user-messages": None,
                "allow-dangerously-skip-permissions": None,
            },
        )
        hooks = captured_options["hooks"]
        self.assertEqual(hooks["PreToolUse"][0].matcher, "Bash")
        self.assertEqual(result, {"behavior": "allow"})
        permission.assert_awaited_once_with(
            "chat-claude",
            "ExitPlanMode",
            {"plan": "Proceed carefully"},
            {"tool_use_id": "exit-plan"},
            owner_token="",
        )
        default_key = agent_server.claude_sdk_configuration_key(
            {**session, "claude_permission_mode": "default"},
            self.cwd,
            "/usr/bin/claude",
            agent_server.session_system_prompt(
                "chat-claude",
                session,
                Path(self.cwd) / ".manifest.json",
            ),
        )
        self.assertNotEqual(config_key, default_key)

    async def test_permission_mode_runtime_contract_and_public_session(self) -> None:
        self.session["claude_permission_mode"] = "acceptEdits"
        with patch.object(
            agent_server,
            "claude_sdk_dependency_available",
            return_value=True,
        ), patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            runtime = await agent_server.claude_runtime_snapshot("chat-claude")

        self.assertEqual(runtime["policy"]["permission_mode"], "acceptEdits")
        self.assertEqual(
            runtime["permission_modes"],
            list(agent_server.CLAUDE_PERMISSION_MODE_OPTIONS),
        )
        self.assertTrue(runtime["features"]["permission_mode_control"])
        with patch.object(
            agent_server,
            "host_pressure_snapshot",
            return_value={},
        ), patch.object(
            agent_server,
            "tmux_capability",
            return_value={"available": False},
        ), patch.object(
            agent_server,
            "runtime_diagnostics_snapshot",
            return_value={},
        ), patch.object(
            agent_server,
            "claude_sdk_dependency_available",
            return_value=True,
        ), patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            health = await agent_server.health()
        capability = health["capabilities"]["claude_controls"]
        self.assertTrue(capability["features"]["permission_mode_control"])
        self.assertEqual(
            capability["permission_modes"],
            list(agent_server.CLAUDE_PERMISSION_MODE_OPTIONS),
        )
        self.assertEqual(
            agent_server.public_session(self.session)["claude_permission_mode"],
            "acceptEdits",
        )
        legacy = {"id": "legacy", "backend": agent_server.BACKEND_CLAUDE}
        self.assertEqual(
            agent_server.effective_claude_permission_mode(legacy),
            agent_server.CLAUDE_DEFAULT_PERMISSION_MODE,
        )

    async def test_permission_mode_change_is_fenced_but_noop_is_allowed(self) -> None:
        self.session["claude_permission_mode"] = "default"
        update = AsyncMock(return_value=self.session)
        with patch.object(agent_server.STORE, "update", update):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.update_session(
                    "chat-claude",
                    agent_server.UpdateSessionRequest(
                        claude_permission_mode="plan",
                    ),
                )
            self.assertEqual(raised.exception.status_code, 409)
            update.assert_not_awaited()

            result = await agent_server.update_session(
                "chat-claude",
                agent_server.UpdateSessionRequest(
                    claude_permission_mode="default",
                ),
            )

        self.assertEqual(result["session"]["claude_permission_mode"], "default")
        update.assert_awaited_once()

    async def test_permission_mode_store_persists_and_null_resets(self) -> None:
        self.session["claude_permission_mode"] = "default"
        with patch.object(agent_server.STORE, "save", AsyncMock()):
            updated = await agent_server.STORE.update(
                "chat-claude",
                {"claude_permission_mode": "dontAsk"},
            )
            persisted_mode = updated["claude_permission_mode"]
            reset = await agent_server.STORE.update(
                "chat-claude",
                {"claude_permission_mode": None},
            )

        self.assertEqual(persisted_mode, "dontAsk")
        self.assertEqual(reset["claude_permission_mode"], "default")

    async def test_print_fallback_keeps_legacy_permission_behavior(self) -> None:
        session = {**self.session, "claude_permission_mode": "plan"}
        command = agent_server.build_claude_cmd(
            "chat-claude",
            session,
            Path(self.cwd) / ".manifest.json",
        )

        self.assertIn("--dangerously-skip-permissions", command)
        self.assertNotIn("--permission-mode", command)
        disallowed_index = command.index("--disallowedTools")
        self.assertEqual(
            command[disallowed_index + 1:disallowed_index + 4],
            ["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"],
        )

    async def test_sdk_stop_timeout_retires_only_chat_and_terminalizes(self) -> None:
        handle = FakeClaudeRun()
        manager = FakeClaudeManager(handle)
        runner = asyncio.create_task(wait_forever())
        agent_server.CLAUDE_SDK_MANAGER = manager
        agent_server.ACTIVE = {
            "chat-claude": {
                "proc": None,
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_run": handle,
                "provider_turn_ready": True,
                "stop_requested": False,
            }
        }
        agent_server.SESSION_TURN_TASKS = {"chat-claude": {runner}}

        with patch.object(
            agent_server,
            "STOP_CONFIRM_TIMEOUT_SECONDS",
            0.01,
        ), patch.object(
            agent_server,
            "cancel_codex_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ):
            result = await agent_server.stop_turn("chat-claude")

        self.assertTrue(result["stopped"])
        self.assertTrue(result["hard_stop"])
        self.assertEqual(manager.evict_calls, [("chat-claude", True)])
        self.assertEqual(handle.interrupt_calls, 1)
        self.assertTrue(runner.cancelled() or runner.done())

    async def test_startup_query_can_request_permission_before_handle_returns(self) -> None:
        handle = FakeClaudeRun([{
            "type": "result",
            "result": "done",
            "session_id": "provider",
            "terminal_reason": "end_turn",
        }])
        manager = PermissionDuringStartManager(
            [handle],
            permission_on_calls={1},
        )
        agent_server.CLAUDE_SDK_MANAGER = manager

        async def permission_callback(
            tool_name: str,
            input_data: dict[str, Any],
            context: Any,
        ) -> object:
            return await agent_server.handle_claude_tool_permission(
                "chat-claude",
                tool_name,
                input_data,
                context,
                owner_token=manager.owner_token,
            )

        with patch.dict(sys.modules, fake_claude_sdk_modules()), patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(
                {"can_use_tool": permission_callback},
                "config",
                "/usr/bin/claude",
            ),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "update_claude_pending_session_metadata",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "project_claude_sdk_message",
            AsyncMock(return_value={
                "session_id": "provider",
                "result_text": "done",
                "terminal_reason": "end_turn",
                "is_error": False,
                "aborted": False,
                "error": "",
            }),
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_success",
            Mock(),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            runner = asyncio.create_task(agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            ))
            await asyncio.wait_for(manager.permission_requested.wait(), 0.5)
            for _ in range(100):
                if agent_server.CLAUDE_PENDING_INTERACTIONS:
                    break
                await asyncio.sleep(0)
            interaction_id, interaction = next(iter(
                agent_server.CLAUDE_PENDING_INTERACTIONS.items()
            ))
            self.assertEqual(interaction["turn_id"], "run-claude")
            self.assertTrue(
                agent_server.ACTIVE["chat-claude"]["claude_permissions_open"]
            )
            await agent_server.resolve_claude_interaction(
                "chat-claude",
                interaction_id,
                {"decision": "accept"},
            )
            await asyncio.wait_for(runner, 0.5)

        self.assertEqual(len(manager.permission_results), 1)
        self.assertIsInstance(
            manager.permission_results[0],
            FakePermissionResultAllow,
        )
        self.assertFalse(agent_server.CLAUDE_PENDING_INTERACTIONS)

    async def test_steered_query_routes_permission_to_candidate_before_handle_returns(self) -> None:
        first = FakeClaudeRun()
        second = FakeClaudeRun([{
            "type": "result",
            "result": "steered done",
            "session_id": "provider",
            "terminal_reason": "end_turn",
        }])
        manager = PermissionDuringStartManager(
            [first, second],
            permission_on_calls={2},
        )
        agent_server.CLAUDE_SDK_MANAGER = manager

        async def permission_callback(
            tool_name: str,
            input_data: dict[str, Any],
            context: Any,
        ) -> object:
            return await agent_server.handle_claude_tool_permission(
                "chat-claude",
                tool_name,
                input_data,
                context,
                owner_token=manager.owner_token,
            )

        async def project_message(
            _session_id: str,
            _run_id: str,
            message: object,
            **_kwargs: object,
        ) -> dict[str, object] | None:
            if isinstance(message, dict) and message.get("type") == "result":
                return {
                    "session_id": str(message.get("session_id") or ""),
                    "result_text": str(message.get("result") or ""),
                    "terminal_reason": str(
                        message.get("terminal_reason") or ""
                    ),
                    "is_error": False,
                    "aborted": (
                        message.get("terminal_reason") == "aborted_streaming"
                    ),
                    "error": "",
                }
            return None

        with patch.dict(sys.modules, fake_claude_sdk_modules()), patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(
                {"can_use_tool": permission_callback},
                "config",
                "/usr/bin/claude",
            ),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "update_claude_pending_session_metadata",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "project_claude_sdk_message",
            side_effect=project_message,
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "build_user_provider_prompt",
            return_value="Steered prompt",
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_success",
            Mock(),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            runner = asyncio.create_task(agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            ))
            for _ in range(100):
                active = agent_server.ACTIVE.get("chat-claude") or {}
                if active.get("native_steer_queue") is not None:
                    break
                await asyncio.sleep(0)
            steer_future = asyncio.get_running_loop().create_future()
            await agent_server.ACTIVE["chat-claude"]["native_steer_queue"].put({
                "selected": {
                    "queued_id": "queued-next",
                    "prompt": "Steer",
                    "file_ids": [],
                },
                "remaining": 0,
                "future": steer_future,
            })
            for _ in range(100):
                if first.interrupt_calls:
                    break
                await asyncio.sleep(0)
            await first.messages.put({
                "type": "result",
                "result": "interrupted",
                "session_id": "provider",
                "terminal_reason": "aborted_streaming",
            })
            await asyncio.wait_for(manager.permission_requested.wait(), 0.5)
            for _ in range(100):
                if agent_server.CLAUDE_PENDING_INTERACTIONS:
                    break
                await asyncio.sleep(0)
            interaction_id, interaction = next(iter(
                agent_server.CLAUDE_PENDING_INTERACTIONS.items()
            ))
            candidate_run_id = str(interaction["turn_id"])
            self.assertNotEqual(candidate_run_id, "run-claude")
            self.assertEqual(
                agent_server.ACTIVE["chat-claude"][
                    "claude_permission_run_id"
                ],
                candidate_run_id,
            )
            await agent_server.resolve_claude_interaction(
                "chat-claude",
                interaction_id,
                {"decision": "accept"},
            )
            steer_result = await asyncio.wait_for(steer_future, 0.5)
            await asyncio.wait_for(runner, 0.5)

        self.assertEqual(steer_result["run_id"], candidate_run_id)
        self.assertEqual(len(manager.permission_results), 1)
        self.assertIsInstance(
            manager.permission_results[0],
            FakePermissionResultAllow,
        )

    async def test_typed_tool_results_finish_from_assistant_and_user_messages(self) -> None:
        from claude_agent_sdk.types import (
            AssistantMessage,
            ServerToolResultBlock,
            ServerToolUseBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )

        current_tools: dict[str, dict[str, object]] = {}
        append_event = AsyncMock(return_value={})
        mark_ready = AsyncMock()

        assistant_message = AssistantMessage(
            content=[
                ToolUseBlock(
                    id="assistant-local",
                    name="Read",
                    input={"file_path": "/tmp/local.txt"},
                ),
                ToolResultBlock(
                    tool_use_id="assistant-local",
                    content="local result",
                ),
                ServerToolUseBlock(
                    id="assistant-server",
                    name="web_search",
                    input={"query": "AgentsDock"},
                ),
                ServerToolResultBlock(
                    tool_use_id="assistant-server",
                    content={"type": "web_search_result", "results": []},
                ),
            ],
            model="claude-test",
            session_id="provider-assistant",
        )
        current_tools.update({
            "user-local": {
                "id": "user-local",
                "name": "Write",
                "input": {},
            },
            "user-server": {
                "id": "user-server",
                "name": "web_fetch",
                "input": {},
            },
        })
        user_message = UserMessage(content=[
            ToolResultBlock(
                tool_use_id="user-local",
                content="user local result",
                is_error=True,
            ),
            ServerToolResultBlock(
                tool_use_id="user-server",
                content={"type": "web_fetch_result", "status": 200},
            ),
        ])

        with patch.object(
            agent_server,
            "append_event",
            append_event,
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            mark_ready,
        ):
            await agent_server.project_claude_sdk_message(
                "chat-claude",
                "run-claude",
                assistant_message,
                text_parts=[],
                current_tools=current_tools,
                changed_paths=set(),
            )
            await agent_server.project_claude_sdk_message(
                "chat-claude",
                "run-claude",
                user_message,
                text_parts=[],
                current_tools=current_tools,
                changed_paths=set(),
            )

        self.assertEqual(current_tools, {})
        mark_ready.assert_awaited_once_with(
            "chat-claude",
            "run-claude",
            "provider-assistant",
        )
        projected = [
            (call.args[1], call.args[2])
            for call in append_event.await_args_list
        ]
        self.assertEqual(
            [event_type for event_type, _payload in projected],
            [
                "tool_started",
                "tool_finished",
                "tool_started",
                "tool_finished",
                "tool_finished",
                "tool_finished",
            ],
        )
        finished = [
            payload
            for event_type, payload in projected
            if event_type == "tool_finished"
        ]
        self.assertEqual(
            [payload["tool_id"] for payload in finished],
            [
                "assistant-local",
                "assistant-server",
                "user-local",
                "user-server",
            ],
        )
        self.assertEqual(
            [payload["tool"]["name"] for payload in finished],
            ["Read", "web_search", "Write", "web_fetch"],
        )
        self.assertEqual(
            [payload["is_error"] for payload in finished],
            [False, False, True, False],
        )

    async def test_typed_task_lifecycle_projects_only_snapshot_fields(self) -> None:
        from claude_agent_sdk.types import (
            TaskNotificationMessage,
            TaskProgressMessage,
            TaskStartedMessage,
            TaskUpdatedMessage,
            ToolResultBlock,
            UserMessage,
        )

        messages = [
            TaskStartedMessage(
                subtype="task_started",
                data={
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": "task-1",
                    "tool_use_id": "agent-tool",
                    "task_type": "local_agent",
                    "description": "Review the server",
                    "prompt": "SECRET CHILD PROMPT",
                },
                task_id="task-1",
                description="Review the server",
                uuid="message-1",
                session_id="provider",
                tool_use_id="agent-tool",
                task_type="local_agent",
            ),
            TaskProgressMessage(
                subtype="task_progress",
                data={
                    "type": "system",
                    "subtype": "task_progress",
                    "task_id": "task-1",
                    "description": "Running tests",
                    "last_tool_name": "Bash",
                    "output": "SECRET CHILD OUTPUT",
                },
                task_id="task-1",
                description="Running tests",
                usage={"total_tokens": 10, "tool_uses": 1, "duration_ms": 25},
                uuid="message-2",
                session_id="provider",
                last_tool_name="Bash",
            ),
            TaskUpdatedMessage(
                subtype="task_updated",
                data={
                    "type": "system",
                    "subtype": "task_updated",
                    "task_id": "task-1",
                    "patch": {
                        "status": "completed",
                        "summary": "Review complete",
                        "result": "SECRET TASK RESULT",
                    },
                },
                task_id="task-1",
                patch={"status": "completed", "summary": "Review complete"},
                status="completed",
                session_id="provider",
            ),
            TaskNotificationMessage(
                subtype="task_notification",
                data={
                    "type": "system",
                    "subtype": "task_notification",
                    "task_id": "task-1",
                    "status": "completed",
                    "summary": "Review complete",
                    "output_file": "/private/secret-output.txt",
                },
                task_id="task-1",
                status="completed",
                output_file="/private/secret-output.txt",
                summary="Review complete",
                uuid="message-4",
                session_id="provider",
            ),
            UserMessage(
                content=[ToolResultBlock(
                    tool_use_id="agent-tool",
                    content="SECRET TOOL RESULT",
                )],
                tool_use_result={
                    "status": "async_launched",
                    "isAsync": True,
                    "agentId": "task-1",
                    "result": "SECRET ASYNC RESULT",
                },
            ),
        ]
        append_event = AsyncMock(return_value={})

        with patch.object(agent_server, "append_event", append_event), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ):
            for message in messages:
                await agent_server.project_claude_sdk_message(
                    "chat-claude",
                    "run-claude",
                    message,
                    text_parts=[],
                    current_tools={},
                    changed_paths=set(),
                )

        raw_payloads = [
            call.args[2]["raw"]
            for call in append_event.await_args_list
            if call.args[1] == "raw_event"
        ]
        self.assertEqual(len(raw_payloads), len(messages))
        serialized = "\n".join(raw_payloads)
        for secret in (
            "SECRET CHILD PROMPT",
            "SECRET CHILD OUTPUT",
            "SECRET TASK RESULT",
            "/private/secret-output.txt",
            "SECRET TOOL RESULT",
            "SECRET ASYNC RESULT",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn('"subtype":"task_started"', serialized)
        self.assertIn('"subtype":"task_progress"', serialized)
        self.assertIn('"status":"completed"', serialized)
        self.assertIn('"agentId":"task-1"', serialized)

    async def test_delivery_uncertain_stream_retires_without_empty_success(self) -> None:
        handle = FailingClaudeRun(ClaudeSDKQueryError("replay ACK missing"))
        manager = FakeClaudeManager(handle)
        append_event = AsyncMock(return_value={})
        append_finished = AsyncMock(return_value={})
        project = AsyncMock()

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            append_event,
        ), patch.object(
            agent_server,
            "project_claude_sdk_message",
            project,
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            append_finished,
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_failure",
            Mock(),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            await agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            )

        self.assertEqual(len(manager.start_calls), 1)
        self.assertEqual(manager.evict_calls, [("chat-claude", True)])
        project.assert_not_awaited()
        terminal = append_finished.await_args.args[1]
        self.assertEqual(terminal["exit_code"], 1)
        self.assertEqual(terminal["result_text"], "")
        self.assertTrue(any(
            call.args[1] == "error"
            and "replay ACK missing" in call.args[2]["message"]
            for call in append_event.await_args_list
        ))

    async def test_projection_failure_retires_provider_before_releasing_slot(self) -> None:
        handle = FakeClaudeRun([{"type": "assistant"}])
        manager = FakeClaudeManager(handle)
        release = AsyncMock(return_value=True)

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "project_claude_sdk_message",
            AsyncMock(side_effect=RuntimeError("timeline write failed")),
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            release,
        ), patch.object(
            agent_server,
            "record_runtime_failure",
            Mock(),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            await agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            )

        self.assertGreaterEqual(handle.interrupt_calls, 1)
        self.assertEqual(manager.evict_calls, [("chat-claude", True)])
        release.assert_awaited_once_with(
            "chat-claude",
            expected_run_id="run-claude",
        )

    async def test_natural_completion_wins_race_with_steering_interrupt(self) -> None:
        first = FakeClaudeRun()
        second = FakeClaudeRun()
        manager = SequencedClaudeManager([first, second])
        append_event = AsyncMock(return_value={})
        append_finished = AsyncMock(return_value={})

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            append_event,
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            append_finished,
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "build_user_provider_prompt",
            return_value="Steered prompt",
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_success",
            Mock(),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            runner = asyncio.create_task(agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            ))
            for _ in range(100):
                active = agent_server.ACTIVE.get("chat-claude") or {}
                if active.get("native_steer_queue") is not None:
                    break
                await asyncio.sleep(0)
            native_queue = agent_server.ACTIVE["chat-claude"][
                "native_steer_queue"
            ]
            steer_future = asyncio.get_running_loop().create_future()
            await native_queue.put({
                "selected": {
                    "queued_id": "queued-next",
                    "prompt": "Steer",
                    "file_ids": [],
                },
                "remaining": 0,
                "future": steer_future,
            })
            for _ in range(100):
                if first.interrupt_calls:
                    break
                await asyncio.sleep(0)
            self.assertEqual(first.interrupt_calls, 1)
            await first.messages.put({
                "type": "result",
                "result": "Natural result",
                "session_id": "provider-1",
                "terminal_reason": "end_turn",
            })
            steer_result = await asyncio.wait_for(steer_future, 0.5)
            await second.messages.put({
                "type": "result",
                "result": "Steered result",
                "session_id": "provider-2",
                "terminal_reason": "end_turn",
            })
            await asyncio.wait_for(runner, 0.5)

        self.assertFalse(steer_result["interrupted"])
        prior_finished = [
            call.args[1]
            for call in append_finished.await_args_list
            if call.args[1].get("run_id") == "run-claude"
        ]
        self.assertEqual(len(prior_finished), 1)
        self.assertEqual(prior_finished[0]["result_text"], "Natural result")
        prior_stopped = [
            call.args[2]
            for call in append_event.await_args_list
            if call.args[1] == "turn_stopped"
            and call.args[2].get("run_id") == "run-claude"
        ]
        self.assertEqual(prior_stopped, [])

    async def test_empty_natural_completion_during_steer_is_failed(self) -> None:
        first = FakeClaudeRun()
        second = FakeClaudeRun()
        manager = SequencedClaudeManager([first, second])
        append_event = AsyncMock(return_value={})
        append_finished = AsyncMock(return_value={})
        runtime_failure = Mock()

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            append_event,
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            append_finished,
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "build_user_provider_prompt",
            return_value="Steered prompt",
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "sample_claude_context_usage",
            AsyncMock(return_value=False),
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_success",
            Mock(),
        ), patch.object(
            agent_server,
            "record_runtime_failure",
            runtime_failure,
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            runner = asyncio.create_task(agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            ))
            for _ in range(100):
                active = agent_server.ACTIVE.get("chat-claude") or {}
                if active.get("native_steer_queue") is not None:
                    break
                await asyncio.sleep(0)
            steer_future = asyncio.get_running_loop().create_future()
            await agent_server.ACTIVE["chat-claude"][
                "native_steer_queue"
            ].put({
                "selected": {
                    "queued_id": "queued-next",
                    "prompt": "Steer",
                    "file_ids": [],
                },
                "remaining": 0,
                "future": steer_future,
            })
            for _ in range(100):
                if first.interrupt_calls:
                    break
                await asyncio.sleep(0)
            await first.messages.put({
                "type": "result",
                "result": "",
                "session_id": "provider-1",
                "terminal_reason": "end_turn",
            })
            steer_result = await asyncio.wait_for(steer_future, 0.5)
            await second.messages.put({
                "type": "result",
                "result": "Steered result",
                "session_id": "provider-2",
                "terminal_reason": "end_turn",
            })
            await asyncio.wait_for(runner, 0.5)

        self.assertFalse(steer_result["interrupted"])
        prior_finished = [
            call.args[1]
            for call in append_finished.await_args_list
            if call.args[1].get("run_id") == "run-claude"
        ]
        self.assertEqual(len(prior_finished), 1)
        self.assertEqual(prior_finished[0]["exit_code"], 1)
        self.assertEqual(prior_finished[0]["result_text"], "")
        self.assertTrue(any(
            call.args[1] == "error"
            and call.args[2].get("run_id") == "run-claude"
            and call.args[2].get("message")
            == agent_server.CLAUDE_EMPTY_TURN_ERROR
            for call in append_event.await_args_list
        ))
        runtime_failure.assert_any_call(
            agent_server.BACKEND_CLAUDE,
            agent_server.CLAUDE_EMPTY_TURN_ERROR,
        )

    async def test_system_init_provider_id_survives_stop_before_result(self) -> None:
        handle = FakeClaudeRun([
            {
                "type": "SystemMessage",
                "data": {"session_id": "provider-from-init"},
            }
        ])
        manager = FakeClaudeManager(handle)
        provider_seen = asyncio.Event()
        persist = AsyncMock()

        async def mark_ready(*_args: object, **_kwargs: object) -> None:
            provider_seen.set()

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            side_effect=mark_ready,
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            persist,
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            runner = asyncio.create_task(agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            ))
            await asyncio.wait_for(provider_seen.wait(), 0.5)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner

        matching = [
            call
            for call in persist.await_args_list
            if call.args[:3] == (
                "chat-claude",
                "run-claude",
                agent_server.BACKEND_CLAUDE,
            )
        ]
        # Bind immediately at System init so a crash is resumable, then write
        # the same provider ID idempotently during final cleanup.
        self.assertEqual(len(matching), 2)
        self.assertTrue(
            all(call.args[3] == "provider-from-init" for call in matching)
        )
        self.assertEqual(manager.evict_calls, [("chat-claude", True)])

    async def test_accepted_steer_cancellation_resolves_waiter_as_uncertain(self) -> None:
        first = FakeClaudeRun()
        second = FakeClaudeRun()
        manager = SequencedClaudeManager([first, second])
        handoff_bookkeeping = asyncio.Event()

        async def append_event(
            _session_id: str,
            event_type: str,
            _payload: dict[str, object],
        ) -> dict[str, object]:
            if event_type == "turn_queue_run_now":
                handoff_bookkeeping.set()
                await asyncio.Event().wait()
            return {}

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            side_effect=append_event,
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "build_user_provider_prompt",
            return_value="Steered prompt",
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            runner = asyncio.create_task(agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            ))
            for _ in range(100):
                active = agent_server.ACTIVE.get("chat-claude") or {}
                if active.get("native_steer_queue") is not None:
                    break
                await asyncio.sleep(0)
            steer_future = asyncio.get_running_loop().create_future()
            await agent_server.ACTIVE["chat-claude"]["native_steer_queue"].put({
                "selected": {
                    "queued_id": "queued-next",
                    "prompt": "Steer",
                    "file_ids": [],
                },
                "remaining": 0,
                "future": steer_future,
            })
            for _ in range(100):
                if first.interrupt_calls:
                    break
                await asyncio.sleep(0)
            await first.messages.put({
                "type": "result",
                "result": "Interrupted result",
                "session_id": "provider-1",
                "terminal_reason": "aborted_streaming",
            })
            await asyncio.wait_for(handoff_bookkeeping.wait(), 0.5)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
            with self.assertRaises(agent_server.NativeSteerHandoffError) as raised:
                await asyncio.wait_for(steer_future, 0.5)

        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertFalse(raised.exception.safe_to_requeue)
        self.assertEqual(manager.evict_calls, [("chat-claude", True)])

    async def test_cancellation_during_candidate_query_is_never_replayed(self) -> None:
        first = FakeClaudeRun()
        manager = BlockingCandidateStartManager([first])
        collect_manifest = AsyncMock()

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(side_effect=[{"head": "old"}, {"head": "candidate"}]),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "mark_provider_turn_ready",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "build_user_provider_prompt",
            return_value="Steered prompt",
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            collect_manifest,
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            runner = asyncio.create_task(agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            ))
            for _ in range(100):
                active = agent_server.ACTIVE.get("chat-claude") or {}
                if active.get("native_steer_queue") is not None:
                    break
                await asyncio.sleep(0)
            steer_future = asyncio.get_running_loop().create_future()
            await agent_server.ACTIVE["chat-claude"]["native_steer_queue"].put({
                "selected": {
                    "queued_id": "queued-next",
                    "prompt": "Steer",
                    "file_ids": [],
                },
                "remaining": 0,
                "future": steer_future,
            })
            for _ in range(100):
                if first.interrupt_calls:
                    break
                await asyncio.sleep(0)
            await first.messages.put({
                "type": "result",
                "result": "interrupted",
                "session_id": "provider",
                "terminal_reason": "aborted_streaming",
            })
            await asyncio.wait_for(
                manager.candidate_query_started.wait(),
                0.5,
            )
            candidate_run_id = str(manager.start_calls[1][2])
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
            with self.assertRaises(
                agent_server.NativeSteerHandoffError,
            ) as raised:
                await asyncio.wait_for(steer_future, 0.5)

        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertFalse(raised.exception.safe_to_requeue)
        self.assertEqual(manager.evict_calls, [("chat-claude", True)])
        self.assertTrue(any(
            call.args[:2] == ("chat-claude", candidate_run_id)
            for call in collect_manifest.await_args_list
        ))

    async def _assert_terminal_race_resolves_steer(
        self,
        *,
        let_steer_waiter_consume: bool,
    ) -> None:
        handle = FakeClaudeRun([{
            "type": "result",
            "result": "done",
            "session_id": "provider",
            "terminal_reason": "end_turn",
        }])
        manager = FakeClaudeManager(handle)
        steer_future: asyncio.Future[dict[str, object]] | None = None

        async def project_terminal(*_args: object, **_kwargs: object) -> dict[str, object]:
            nonlocal steer_future
            loop = asyncio.get_running_loop()
            steer_future = loop.create_future()
            active = agent_server.ACTIVE["chat-claude"]
            active["native_steer_queue"].put_nowait({
                "selected": {
                    "queued_id": "queued-race",
                    "prompt": "Steer after terminal",
                    "file_ids": [],
                },
                "remaining": 0,
                "future": steer_future,
            })
            if let_steer_waiter_consume:
                # Exercise the interleaving where queue.get() completes just
                # after asyncio.wait() selected the terminal provider message.
                await asyncio.sleep(0)
            return {
                "session_id": "provider",
                "result_text": "done",
                "terminal_reason": "end_turn",
                "is_error": False,
                "aborted": False,
                "error": "",
            }

        with patch.object(
            agent_server,
            "resolve_claude_resume_provider",
            return_value=(None, None),
        ), patch.object(
            agent_server,
            "capture_git_baseline",
            AsyncMock(return_value={"head": "base"}),
        ), patch.object(
            agent_server,
            "build_claude_sdk_options",
            return_value=(object(), "config", "/usr/bin/claude"),
        ), patch.object(
            agent_server,
            "claude_sdk_manager",
            AsyncMock(return_value=manager),
        ), patch.object(
            agent_server,
            "watch_manifest_artifacts",
            wait_forever,
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "project_claude_sdk_message",
            project_terminal,
        ), patch.object(
            agent_server,
            "cancel_claude_interactions",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "persist_run_provider_session",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_manifest",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "publish_turn_code_diff",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "collect_recent_leftover_manifests",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_turn_finished_event",
            AsyncMock(return_value={}),
        ), patch.object(
            agent_server,
            "release_turn_slot",
            AsyncMock(return_value=True),
        ), patch.object(
            agent_server,
            "record_runtime_success",
            Mock(),
        ), patch.object(
            agent_server,
            "should_schedule_queue_after_finish",
            return_value=False,
        ):
            await agent_server.run_claude_sdk(
                "chat-claude",
                "run-claude",
                "Prompt",
                dict(self.session),
                Path(self.cwd) / ".manifest.json",
            )

        self.assertIsNotNone(steer_future)
        assert steer_future is not None
        with self.assertRaises(agent_server.NativeSteerHandoffError) as raised:
            await steer_future
        self.assertTrue(raised.exception.safe_to_requeue)
        self.assertFalse(raised.exception.delivery_uncertain)
        self.assertEqual(len(manager.start_calls), 1)

    async def test_terminal_race_resolves_steer_waiting_in_queue(self) -> None:
        await self._assert_terminal_race_resolves_steer(
            let_steer_waiter_consume=False,
        )

    async def test_terminal_race_resolves_steer_consumed_by_waiter(self) -> None:
        await self._assert_terminal_race_resolves_steer(
            let_steer_waiter_consume=True,
        )

    def test_native_steer_rejects_changed_working_directory(self) -> None:
        self.session["cwd"] = str(Path(self.cwd) / "other")
        active = {
            "provider_model": self.session["model"],
            "provider_effort": self.session["effort"],
            "cwd": self.cwd,
            "provider_configuration_key": "old-config",
        }

        self.assertFalse(
            agent_server.queued_claude_runtime_matches_active(
                "chat-claude",
                {},
                active,
            )
        )

    def test_pidless_sdk_turn_is_reported_as_active(self) -> None:
        snapshot = agent_server.active_process_snapshot(
            "chat-claude",
            {
                "proc": None,
                "pid": None,
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "cwd": self.cwd,
                "argv": ["claude", "<ClaudeSDKClient>"],
                "started_at": 1.0,
                "started_at_iso": "2026-08-05T00:00:00Z",
                "stop_requested": False,
                "stdout_lines": [],
            },
        )

        self.assertTrue(snapshot["active"])
        self.assertEqual(
            snapshot["transport"],
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        )

    async def test_force_send_cannot_deliver_into_a_detached_terminal_queue(self) -> None:
        native_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)
        selected = {
            "queued_id": "queued-race",
            "prompt": "Continue after completion",
            "file_ids": [],
            "backend": agent_server.BACKEND_CLAUDE,
            "client_capabilities": [
                agent_server.CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY
            ],
        }
        agent_server.QUEUED_TURNS = {
            "chat-claude": deque([selected]),
        }
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "provider_turn_ready": True,
                "native_steer_queue": native_queue,
            }
        }

        def finish_during_selection(*_args: object) -> bool:
            # Reproduce the provider terminalizing after Force Send snapshots
            # ACTIVE but before it delivers into the native queue.
            agent_server.ACTIVE["chat-claude"]["provider_turn_ready"] = False
            agent_server.ACTIVE["chat-claude"]["native_steer_queue"] = None
            return True

        with patch.object(
            agent_server,
            "queued_claude_runtime_matches_active",
            side_effect=finish_during_selection,
        ):
            with self.assertRaises(agent_server.NativeSteerHandoffError) as raised:
                await asyncio.wait_for(
                    agent_server._run_queued_turn_now_once(
                        "chat-claude",
                        "queued-race",
                    ),
                    0.5,
                )

        self.assertTrue(raised.exception.safe_to_requeue)
        self.assertEqual(
            [
                item.get("queued_id")
                for item in agent_server.QUEUED_TURNS["chat-claude"]
            ],
            ["queued-race"],
        )
        self.assertTrue(native_queue.empty())

    async def test_deletion_cleanup_waits_for_one_approval_not_sdk_receiver(self) -> None:
        manager = FakeClaudeManager()
        manager.active_run_id = "run-claude"
        agent_server.CLAUDE_SDK_MANAGER = manager
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "interactive_agent_sdk": True,
                "stop_requested": False,
                "claude_sdk_owner_token": manager.owner_token,
                "claude_permission_run_id": "run-claude",
                "claude_permissions_open": True,
            }
        }
        with patch.dict(sys.modules, fake_claude_sdk_modules()), patch.object(
            agent_server,
            "update_claude_pending_session_metadata",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ):
            callback = asyncio.create_task(
                agent_server.handle_claude_tool_permission(
                    "chat-claude",
                    "Bash",
                    {"command": "pwd"},
                    {"tool_use_id": "tool-1"},
                    owner_token=manager.owner_token,
                )
            )
            for _ in range(100):
                if agent_server.CLAUDE_PENDING_INTERACTIONS:
                    break
                await asyncio.sleep(0)
            self.assertTrue(agent_server.CLAUDE_PENDING_INTERACTIONS)

            agent_server.DELETING_SESSIONS.add("chat-claude")
            await agent_server.cancel_claude_interactions(
                "chat-claude",
                resolution="session_deleted",
            )
            self.assertTrue(
                await agent_server.wait_for_session_tasks(
                    agent_server.CLAUDE_INTERACTION_HANDLER_TASKS,
                    "chat-claude",
                    timeout=0.5,
                )
            )
            result = await asyncio.wait_for(callback, 0.5)

        self.assertIsInstance(result, FakePermissionResultDeny)
        self.assertTrue(getattr(result, "interrupt", False))
        self.assertFalse(agent_server.CLAUDE_PENDING_INTERACTIONS)

    async def test_delete_force_retires_sdk_when_interrupt_has_no_terminal_result(self) -> None:
        handle = FakeClaudeRun()
        manager = FakeClaudeManager(handle)
        manager.active_run_id = "run-claude"
        agent_server.CLAUDE_SDK_MANAGER = manager
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "claude_sdk_run": handle,
                "interactive_agent_sdk": True,
                "stop_requested": False,
                "claude_sdk_owner_token": manager.owner_token,
                "claude_permission_run_id": "run-claude",
                "claude_permissions_open": True,
            }
        }
        turn_task = asyncio.create_task(wait_forever())
        agent_server.register_session_task(
            agent_server.SESSION_TURN_TASKS,
            "chat-claude",
            turn_task,
        )

        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            (state_dir / "sessions" / "chat-claude").mkdir(parents=True)
            with patch.object(
                agent_server,
                "STATE_DIR",
                state_dir,
            ), patch.object(
                agent_server,
                "SESSIONS_FILE",
                state_dir / "sessions.json",
            ), patch.object(
                agent_server,
                "ensure_dirs",
            ), patch.object(
                agent_server,
                "CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS",
                0.01,
            ), patch.object(
                agent_server,
                "wait_for_session_tasks",
                AsyncMock(side_effect=[True, True, False, True]),
            ), patch.object(
                agent_server.JOBS,
                "delete_for_session",
                AsyncMock(return_value=0),
            ), patch.object(
                agent_server,
                "kill_terminal_session",
            ):
                try:
                    result = await asyncio.wait_for(
                        agent_server.delete_session("chat-claude"),
                        0.5,
                    )
                finally:
                    agent_server.DELETED_SESSION_TOMBSTONES.discard(
                        "chat-claude"
                    )

        self.assertTrue(result["deleted"])
        self.assertGreaterEqual(handle.interrupt_calls, 1)
        self.assertIn(("chat-claude", True), manager.evict_calls)
        self.assertTrue(turn_task.cancelled() or turn_task.done())

    async def test_delete_bounds_cancellation_hostile_run_now_then_retries(self) -> None:
        cancellation_observed = asyncio.Event()
        release = asyncio.Event()

        async def cancellation_hostile_run_now() -> dict[str, object]:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Model a Force Send handoff that must finish provider-side
                # cleanup after receiving its first local cancellation.
                cancellation_observed.set()
                await release.wait()
            return {"ok": True}

        run_now_task = asyncio.create_task(cancellation_hostile_run_now())
        run_now_requests = {
            "chat-claude": ("queued-steer", run_now_task),
        }

        import tempfile
        try:
            with tempfile.TemporaryDirectory() as temporary:
                state_dir = Path(temporary)
                (state_dir / "sessions" / "chat-claude").mkdir(parents=True)
                with patch.object(
                    agent_server,
                    "STATE_DIR",
                    state_dir,
                ), patch.object(
                    agent_server,
                    "SESSIONS_FILE",
                    state_dir / "sessions.json",
                ), patch.object(
                    agent_server,
                    "ensure_dirs",
                ), patch.object(
                    agent_server,
                    "CODEX_SESSION_CLEANUP_TIMEOUT_SECONDS",
                    0.01,
                ), patch.object(
                    agent_server,
                    "RUN_NOW_REQUESTS",
                    run_now_requests,
                ), patch.object(
                    agent_server,
                    "CODEX_APP_SERVER_MANAGER",
                    None,
                ), patch.object(
                    agent_server.JOBS,
                    "delete_for_session",
                    AsyncMock(return_value=0),
                ), patch.object(
                    agent_server,
                    "kill_terminal_session",
                ):
                    with self.assertRaises(agent_server.HTTPException) as raised:
                        await asyncio.wait_for(
                            agent_server.delete_session("chat-claude"),
                            0.5,
                        )

                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertIn("Force Send cleanup", str(raised.exception.detail))
                    await asyncio.wait_for(cancellation_observed.wait(), 0.5)
                    self.assertFalse(run_now_task.done())
                    self.assertIn("chat-claude", agent_server.STORE.sessions)
                    self.assertNotIn(
                        "chat-claude",
                        agent_server.DELETING_SESSIONS,
                    )

                    release.set()
                    self.assertEqual(
                        await asyncio.wait_for(run_now_task, 0.5),
                        {"ok": True},
                    )
                    result = await asyncio.wait_for(
                        agent_server.delete_session("chat-claude"),
                        0.5,
                    )

                    self.assertTrue(result["deleted"])
                    self.assertNotIn("chat-claude", run_now_requests)
                    self.assertNotIn("chat-claude", agent_server.STORE.sessions)
        finally:
            release.set()
            if not run_now_task.done():
                run_now_task.cancel()
            await asyncio.gather(run_now_task, return_exceptions=True)
            agent_server.DELETED_SESSION_TOMBSTONES.discard("chat-claude")

    async def test_late_approval_response_is_rejected_after_delete_reservation(self) -> None:
        agent_server.DELETING_SESSIONS.add("chat-claude")
        resolver = AsyncMock()

        with patch.object(
            agent_server,
            "resolve_claude_interaction",
            resolver,
        ):
            with self.assertRaises(agent_server.HTTPException) as raised:
                await agent_server.post_claude_interaction_response(
                    "chat-claude",
                    "interaction-1",
                    agent_server.ClaudeInteractionResponseRequest(
                        response={"decision": "accept"},
                    ),
                )

        self.assertEqual(raised.exception.status_code, 409)
        resolver.assert_not_awaited()

    async def test_approval_accepted_after_stop_fence_is_still_denied(self) -> None:
        manager = FakeClaudeManager()
        manager.active_run_id = "run-claude"
        agent_server.CLAUDE_SDK_MANAGER = manager
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "interactive_agent_sdk": True,
                "stop_requested": False,
                "claude_sdk_owner_token": manager.owner_token,
                "claude_permission_run_id": "run-claude",
                "claude_permissions_open": True,
            }
        }
        with patch.dict(sys.modules, fake_claude_sdk_modules()), patch.object(
            agent_server,
            "update_claude_pending_session_metadata",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ):
            callback = asyncio.create_task(
                agent_server.handle_claude_tool_permission(
                    "chat-claude",
                    "Bash",
                    {"command": "pwd"},
                    {"tool_use_id": "tool-stop-race"},
                    owner_token=manager.owner_token,
                )
            )
            for _ in range(100):
                if agent_server.CLAUDE_PENDING_INTERACTIONS:
                    break
                await asyncio.sleep(0)
            interaction_id = next(iter(
                agent_server.CLAUDE_PENDING_INTERACTIONS
            ))
            async with agent_server.ACTIVE_LOCK:
                active = agent_server.ACTIVE["chat-claude"]
                active["stop_requested"] = True
                active["claude_permissions_open"] = False
            await agent_server.resolve_claude_interaction(
                "chat-claude",
                interaction_id,
                {"decision": "accept"},
            )
            result = await asyncio.wait_for(callback, 0.5)

        self.assertIsInstance(result, FakePermissionResultDeny)
        self.assertTrue(getattr(result, "interrupt", False))
        self.assertFalse(agent_server.CLAUDE_PENDING_INTERACTIONS)

    async def test_ask_user_can_be_skipped_with_empty_answers(self) -> None:
        manager = FakeClaudeManager()
        manager.active_run_id = "run-claude"
        agent_server.CLAUDE_SDK_MANAGER = manager
        agent_server.ACTIVE = {
            "chat-claude": {
                "run_id": "run-claude",
                "backend": agent_server.BACKEND_CLAUDE,
                "transport": agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
                "interactive_agent_sdk": True,
                "stop_requested": False,
                "claude_sdk_owner_token": manager.owner_token,
                "claude_permission_run_id": "run-claude",
                "claude_permissions_open": True,
            }
        }
        with patch.dict(sys.modules, fake_claude_sdk_modules()), patch.object(
            agent_server,
            "update_claude_pending_session_metadata",
            AsyncMock(),
        ), patch.object(
            agent_server,
            "append_event",
            AsyncMock(return_value={}),
        ):
            callback = asyncio.create_task(
                agent_server.handle_claude_tool_permission(
                    "chat-claude",
                    "AskUserQuestion",
                    {
                        "questions": [
                            {
                                "question": "Choose one",
                                "header": "Choice",
                                "multiSelect": False,
                                "options": [
                                    {"label": "A", "description": "First"},
                                ],
                            }
                        ]
                    },
                    {"tool_use_id": "question-1"},
                    owner_token=manager.owner_token,
                )
            )
            for _ in range(100):
                if agent_server.CLAUDE_PENDING_INTERACTIONS:
                    break
                await asyncio.sleep(0)
            interaction_id = next(iter(agent_server.CLAUDE_PENDING_INTERACTIONS))
            await agent_server.resolve_claude_interaction(
                "chat-claude",
                interaction_id,
                {"answers": {}},
            )
            result = await asyncio.wait_for(callback, 0.5)

        self.assertIsInstance(result, FakePermissionResultDeny)
        self.assertFalse(getattr(result, "interrupt", True))
        self.assertIn("skipped", str(getattr(result, "message", "")).lower())


if __name__ == "__main__":
    unittest.main()
