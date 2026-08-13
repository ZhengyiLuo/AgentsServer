import asyncio
import unittest
from collections.abc import AsyncIterable, AsyncIterator
from typing import Any

from claude_sdk_client import (
    ClaudeSDKConfigurationConflict,
    ClaudeSDKLoopError,
    ClaudeSDKQueryError,
    ClaudeSDKRunActive,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKSupervisorManager,
    ClaudeSDKUnavailable,
    claude_background_tracking_hooks,
    claude_untracked_background_reason,
    reject_untracked_background_hook,
)


class FakeClaudeClient:
    def __init__(
        self,
        options: Any,
        *,
        connect_error: Exception | None = None,
        query_error: Exception | None = None,
        auto_ack: bool = True,
        query_prefix_messages: list[Any] | None = None,
    ) -> None:
        self.options = options
        self.connect_error = connect_error
        self.query_error = query_error
        self.auto_ack = auto_ack
        self.query_prefix_messages = list(query_prefix_messages or [])
        self.messages: asyncio.Queue[Any] = asyncio.Queue()
        self.calls: list[tuple[Any, ...]] = []
        self.query_envelopes: list[list[dict[str, Any]]] = []
        self.connected = False
        self.disconnected = False
        self.owner_loop: asyncio.AbstractEventLoop | None = None
        self.context_usage: dict[str, Any] = {
            "totalTokens": 12_345,
            "maxTokens": 180_000,
            "rawMaxTokens": 200_000,
            "percentage": 6.86,
            "model": "claude-sonnet-4-5",
        }

    def _record(self, *call: Any) -> None:
        loop = asyncio.get_running_loop()
        if self.owner_loop is None:
            self.owner_loop = loop
        elif self.owner_loop is not loop:
            raise AssertionError("fake client crossed event loops")
        self.calls.append(call)

    async def connect(self) -> None:
        self._record("connect")
        if self.connect_error is not None:
            raise self.connect_error
        self.connected = True

    async def query(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        prompt_text, correlation_id, envelope = await materialize_query(prompt)
        self._record("query", prompt_text, kwargs)
        self.query_envelopes.append(envelope)
        if self.query_error is not None:
            raise self.query_error
        for message in self.query_prefix_messages:
            await self.messages.put(message)
        if self.auto_ack and correlation_id:
            await self.messages.put(replay_ack(correlation_id))

    async def receive_messages(self) -> AsyncIterator[Any]:
        self._record("receive_messages")
        while True:
            value = await self.messages.get()
            if isinstance(value, BaseException):
                raise value
            if value is StopAsyncIteration:
                return
            yield value

    async def interrupt(self) -> None:
        self._record("interrupt")

    async def get_context_usage(self) -> dict[str, Any]:
        self._record("get_context_usage")
        return dict(self.context_usage)

    async def disconnect(self) -> None:
        self._record("disconnect")
        self.disconnected = True
        self.connected = False

    async def emit(self, value: Any) -> None:
        await self.messages.put(value)


class FakeFactory:
    def __init__(self) -> None:
        self.clients: list[FakeClaudeClient] = []
        self.connect_error: Exception | None = None
        self.query_error: Exception | None = None
        self.auto_ack = True
        self.query_prefix_messages: list[Any] = []

    def __call__(self, options: Any) -> FakeClaudeClient:
        client = FakeClaudeClient(
            options,
            connect_error=self.connect_error,
            query_error=self.query_error,
            auto_ack=self.auto_ack,
            query_prefix_messages=self.query_prefix_messages,
        )
        self.clients.append(client)
        return client


class BlockingQueryClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.query_started = asyncio.Event()

    async def query(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        prompt_text, _correlation_id, envelope = await materialize_query(prompt)
        self._record("query", prompt_text, kwargs)
        self.query_envelopes.append(envelope)
        self.query_started.set()
        await asyncio.Event().wait()


class BlockingQueryFactory:
    def __init__(self) -> None:
        self.clients: list[BlockingQueryClient] = []

    def __call__(self, options: Any) -> BlockingQueryClient:
        client = BlockingQueryClient(options)
        self.clients.append(client)
        return client


class CancellationHostileQueryClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options, auto_ack=False)
        self.query_started = asyncio.Event()
        self.release_query = asyncio.Event()

    async def query(
        self,
        prompt: str | AsyncIterable[dict[str, Any]],
        **kwargs: Any,
    ) -> None:
        prompt_text, _correlation_id, envelope = await materialize_query(prompt)
        self._record("query", prompt_text, kwargs)
        self.query_envelopes.append(envelope)
        self.query_started.set()
        while not self.release_query.is_set():
            try:
                await self.release_query.wait()
            except asyncio.CancelledError:
                continue


class CancellationHostileQueryFactory:
    def __init__(self) -> None:
        self.clients: list[CancellationHostileQueryClient] = []

    def __call__(self, options: Any) -> CancellationHostileQueryClient:
        client = CancellationHostileQueryClient(options)
        self.clients.append(client)
        return client


class CancellationHostileReceiverClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.receiver_started = asyncio.Event()
        self.release_receiver = asyncio.Event()

    async def receive_messages(self) -> AsyncIterator[Any]:
        self._record("receive_messages")
        self.receiver_started.set()
        while not self.release_receiver.is_set():
            try:
                await self.release_receiver.wait()
            except asyncio.CancelledError:
                # Model a third-party stream that delays cancellation until its
                # own transport eventually settles.
                continue
        if False:  # pragma: no cover - keeps this an async generator
            yield None


class CancellationHostileConnectClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.connect_started = asyncio.Event()
        self.release_connect = asyncio.Event()

    async def connect(self) -> None:
        self._record("connect")
        self.connect_started.set()
        while not self.release_connect.is_set():
            try:
                await self.release_connect.wait()
            except asyncio.CancelledError:
                # Model an SDK transport that does not acknowledge actor
                # cancellation until its own connect attempt settles.
                continue
        self.connected = True


class HostileConnectFactory:
    def __init__(self) -> None:
        self.clients: list[CancellationHostileConnectClient] = []

    def __call__(self, options: Any) -> CancellationHostileConnectClient:
        client = CancellationHostileConnectClient(options)
        self.clients.append(client)
        return client


class HostileThenNormalFactory:
    def __init__(self) -> None:
        self.clients: list[FakeClaudeClient] = []

    def __call__(self, options: Any) -> FakeClaudeClient:
        client: FakeClaudeClient
        if self.clients:
            client = FakeClaudeClient(options)
        else:
            client = CancellationHostileReceiverClient(options)
        self.clients.append(client)
        return client


async def collect(handle: Any) -> list[Any]:
    return [message async for message in handle]


async def materialize_query(
    prompt: str | AsyncIterable[dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]]]:
    if isinstance(prompt, str):
        return prompt, "", []
    envelope = [frame async for frame in prompt]
    if len(envelope) != 1:
        raise AssertionError(f"expected one query frame, got {len(envelope)}")
    frame = envelope[0]
    message = frame.get("message")
    prompt_text = str(message.get("content") or "") if isinstance(message, dict) else ""
    return prompt_text, str(frame.get("uuid") or ""), envelope


def replay_ack(correlation_id: str) -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": correlation_id,
        "isReplay": True,
        "message": {"role": "user", "content": "ack"},
    }


class UserMessage:
    """Minimal stand-in for the SDK type whose parser drops ``isReplay``."""

    def __init__(self, correlation_id: str) -> None:
        self.uuid = correlation_id
        self.content: list[Any] = []


class PlainMessage:
    def __init__(self, correlation_id: str) -> None:
        self.uuid = correlation_id


class ClaudeSDKSupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.factory = FakeFactory()
        self.manager = ClaudeSDKSupervisorManager(
            client_factory=self.factory,
            max_clients=4,
            idle_ttl_seconds=None,
        )

    async def asyncTearDown(self) -> None:
        await self.manager.close_all()

    async def test_start_run_returns_only_after_query_and_streams_through_result(self) -> None:
        handle = await self.manager.start_run(
            "chat-1",
            "Inspect the repo",
            run_id="run-1",
            options={"cwd": "/tmp"},
            configuration_key="config-a",
        )
        client = self.factory.clients[0]

        self.assertTrue(handle.accepted)
        self.assertEqual(client.calls[0], ("connect",))
        self.assertIn(("receive_messages",), client.calls)
        self.assertIn(("query", "Inspect the repo", {}), client.calls)
        await client.emit({"type": "assistant", "text": "working"})
        result = {"type": "result", "session_id": "provider-1", "result": "done"}
        await client.emit(result)

        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [
            {"type": "assistant", "text": "working"},
            result,
        ])
        self.assertEqual(await handle.wait_result(), result)

    async def test_delegated_task_keeps_run_open_until_followup_result(self) -> None:
        handle = await self.manager.start_run(
            "chat-background",
            "Delegate the review",
            run_id="run-background",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        task_started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "task-1",
            "task_type": "local_agent",
        }
        intermediate_result = {
            "type": "result",
            "is_error": False,
            "result": "delegated",
        }
        task_finished = {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "task-1",
            "status": "completed",
        }
        followup = {"type": "assistant", "text": "Review complete"}
        final_result = {
            "type": "result",
            "is_error": False,
            "result": "done",
        }

        await client.emit(task_started)
        await client.emit(intermediate_result)
        await asyncio.sleep(0)
        self.assertFalse(handle.done)
        for message in (task_finished, followup, final_result):
            await client.emit(message)

        self.assertEqual(
            await asyncio.wait_for(collect(handle), 1),
            [task_started, task_finished, followup, final_result],
        )
        self.assertEqual(await handle.wait_result(), final_result)

    async def test_multiple_delegated_milestones_do_not_finish_top_level_run(self) -> None:
        handle = await self.manager.start_run(
            "chat-milestones",
            "Complete both milestones",
            run_id="run-milestones",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        first_started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "agent-1",
            "task_type": "local_agent",
        }
        first_finished = {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "agent-1",
            "status": "completed",
        }
        second_started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "workflow-2",
            "task_type": "local_workflow",
        }
        second_finished = {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "workflow-2",
            "status": "completed",
        }
        first_progress = {"type": "assistant", "text": "First milestone complete"}
        second_progress = {"type": "assistant", "text": "Second milestone complete"}
        final_result = {"type": "result", "is_error": False, "result": "all done"}

        for message in (
            first_started,
            {"type": "result", "is_error": False, "result": "first done"},
        ):
            await client.emit(message)
        await asyncio.sleep(0)
        self.assertFalse(handle.done)

        for message in (
            first_finished,
            first_progress,
            second_started,
            {"type": "result", "is_error": False, "result": "second done"},
        ):
            await client.emit(message)
        await asyncio.sleep(0)
        self.assertFalse(handle.done)

        for message in (second_finished, second_progress, final_result):
            await client.emit(message)

        self.assertEqual(
            await asyncio.wait_for(collect(handle), 1),
            [
                first_started,
                first_finished,
                first_progress,
                second_started,
                second_finished,
                second_progress,
                final_result,
            ],
        )
        self.assertEqual(await handle.wait_result(), final_result)

    async def test_terminal_task_update_allows_followup_result(self) -> None:
        from claude_agent_sdk.types import TaskUpdatedMessage

        handle = await self.manager.start_run(
            "chat-task-update",
            "Delegate the workflow",
            run_id="run-task-update",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        task_started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "workflow-1",
            "task_type": "local_workflow",
        }
        task_updated = TaskUpdatedMessage(
            subtype="task_updated",
            data={
                "subtype": "task_updated",
                "task_id": "workflow-1",
                "patch": {"status": "completed"},
            },
            task_id="workflow-1",
            patch={"status": "completed"},
            status="completed",
        )
        final_result = {"type": "result", "is_error": False, "result": "done"}
        for message in (
            task_started,
            {"type": "result", "is_error": False, "result": "waiting"},
            task_updated,
            final_result,
        ):
            await client.emit(message)

        self.assertEqual(
            await asyncio.wait_for(collect(handle), 1),
            [task_started, task_updated, final_result],
        )

    async def test_error_and_aborted_results_end_delegated_run(self) -> None:
        terminal_results = (
            {"type": "result", "is_error": True, "result": "failed"},
            {
                "type": "result",
                "is_error": False,
                "terminal_reason": "aborted_streaming",
                "result": "stopped",
            },
        )
        for index, terminal_result in enumerate(terminal_results):
            with self.subTest(terminal_result=terminal_result):
                chat_id = f"chat-terminal-{index}"
                handle = await self.manager.start_run(
                    chat_id,
                    "Delegate then stop",
                    run_id=f"run-terminal-{index}",
                    options={},
                    configuration_key="same",
                )
                client = self.factory.clients[index]
                task_started = {
                    "type": "system",
                    "subtype": "task_started",
                    "task_id": f"task-{index}",
                    "task_type": "local_agent",
                }
                await client.emit(task_started)
                await client.emit(terminal_result)
                self.assertEqual(
                    await asyncio.wait_for(collect(handle), 1),
                    [task_started, terminal_result],
                )
                self.assertEqual(await handle.wait_result(), terminal_result)

    async def test_forced_terminal_retires_late_frames_before_fresh_run(self) -> None:
        first_handle = await self.manager.start_run(
            "chat-reconnect",
            "Delegate then stop",
            run_id="run-before-stop",
            options={},
            configuration_key="same",
        )
        old_client = self.factory.clients[0]
        task_started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "task-before-stop",
            "task_type": "local_agent",
        }
        aborted_result = {
            "type": "result",
            "is_error": False,
            "terminal_reason": "aborted_tools",
            "result": "stopped",
        }
        await old_client.emit(task_started)
        await old_client.emit(aborted_result)
        self.assertEqual(
            await asyncio.wait_for(collect(first_handle), 1),
            [task_started, aborted_result],
        )

        second_handle = await self.manager.start_run(
            "chat-reconnect",
            "Fresh prompt",
            run_id="run-after-stop",
            options={},
            configuration_key="same",
        )
        self.assertTrue(old_client.disconnected)
        self.assertEqual(len(self.factory.clients), 2)
        new_client = self.factory.clients[1]

        # The retired provider can no longer route an orphan continuation into
        # the fresh, exactly-acknowledged query.
        await old_client.emit({
            "type": "system",
            "subtype": "task_notification",
            "task_id": "task-before-stop",
            "status": "stopped",
        })
        await old_client.emit({"type": "result", "result": "orphan"})
        fresh_assistant = {"type": "assistant", "text": "fresh"}
        fresh_result = {"type": "result", "result": "fresh done"}
        await new_client.emit(fresh_assistant)
        await new_client.emit(fresh_result)
        self.assertEqual(
            await asyncio.wait_for(collect(second_handle), 1),
            [fresh_assistant, fresh_result],
        )


    async def test_unknown_task_type_does_not_extend_run(self) -> None:
        handle = await self.manager.start_run(
            "chat-shell",
            "Start a background shell",
            run_id="run-shell",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        task_started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "shell-1",
            "task_type": "background_shell",
        }
        result = {"type": "result", "is_error": False, "result": "done"}
        await client.emit(task_started)
        await client.emit(result)
        self.assertEqual(
            await asyncio.wait_for(collect(handle), 1),
            [task_started, result],
        )

    async def test_stale_frames_are_dropped_until_exact_replay_ack(self) -> None:
        notification = {
            "type": "system",
            "subtype": "task_notification",
            "task_id": "stale-task",
            "status": "stopped",
        }
        stale_result = {"type": "result", "result": "stale"}
        stale_task_started = {
            "type": "system",
            "subtype": "task_started",
            "task_id": "stale-agent",
            "task_type": "local_agent",
        }
        self.factory.query_prefix_messages = [
            stale_task_started,
            notification,
            stale_result,
        ]
        handle = await self.manager.start_run(
            "chat-1",
            "Fresh prompt",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        fresh_assistant = {"type": "assistant", "text": "fresh"}
        fresh_result = {"type": "result", "result": "finished"}
        await client.emit(fresh_assistant)
        await client.emit(fresh_result)

        self.assertEqual(
            await asyncio.wait_for(collect(handle), 1),
            [fresh_assistant, fresh_result],
        )
        self.assertEqual(await handle.wait_result(), fresh_result)
        self.assertEqual(
            [call for call in client.calls if call[0] == "query"],
            [("query", "Fresh prompt", {})],
        )
        self.assertEqual(len(client.query_envelopes), 1)
        self.assertEqual(
            client.query_envelopes[0][0]["uuid"],
            handle.correlation_id,
        )

    async def test_wrong_uuid_and_non_replay_user_never_open_gate(self) -> None:
        self.factory.auto_ack = False
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        await client.emit(replay_ack("wrong-uuid"))
        await client.emit({
            "type": "user",
            "uuid": handle.correlation_id,
            "isReplay": False,
        })
        await client.emit({"type": "result", "result": "stale"})
        await asyncio.sleep(0)
        self.assertFalse(handle.acknowledged)
        self.assertFalse(handle.done)

        await client.emit(replay_ack(handle.correlation_id))
        result = {"type": "result", "result": "fresh"}
        await client.emit(result)
        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [result])
        self.assertEqual(await handle.wait_result(), result)

    async def test_ack_waiter_opens_only_for_exact_replay_ack(self) -> None:
        self.factory.auto_ack = False
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        waiter = asyncio.create_task(handle.wait_acknowledged())

        await client.emit(replay_ack("wrong-uuid"))
        await client.emit({
            "type": "user",
            "uuid": handle.correlation_id,
            "isReplay": False,
        })
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())
        self.assertFalse(handle.acknowledged)

        await client.emit(replay_ack(handle.correlation_id))
        await asyncio.wait_for(waiter, 1)
        self.assertTrue(handle.acknowledged)
        # Event semantics also cover ACKs that arrive before the server starts
        # watching provider readiness.
        await asyncio.wait_for(handle.wait_acknowledged(), 1)

        result = {"type": "result", "result": "done"}
        await client.emit(result)
        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [result])
        self.assertEqual(await handle.wait_result(), result)

    async def test_duplicate_matching_ack_is_suppressed(self) -> None:
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        await client.emit(replay_ack(handle.correlation_id))
        result = {"type": "result", "result": "done"}
        await client.emit(result)
        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [result])

    async def test_typed_user_message_ack_opens_gate_but_plain_object_does_not(self) -> None:
        self.factory.auto_ack = False
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        await client.emit(PlainMessage(handle.correlation_id))
        await client.emit({"type": "result", "result": "stale"})
        await asyncio.sleep(0)
        self.assertFalse(handle.acknowledged)
        self.assertFalse(handle.done)

        await client.emit(UserMessage(handle.correlation_id))
        result = {
            "type": "result",
            "result": "done",
            "session_id": "provider-post-ack",
        }
        await client.emit(result)
        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [result])
        self.assertEqual((await handle.wait_result())["session_id"], "provider-post-ack")

    async def test_real_sdk_parser_preserves_uuid_for_typed_replay_ack(self) -> None:
        from claude_agent_sdk._internal.message_parser import parse_message

        self.factory.auto_ack = False
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        typed_ack = parse_message({
            "type": "user",
            "uuid": handle.correlation_id,
            "isReplay": True,
            "message": {"role": "user", "content": "Prompt"},
        })
        self.assertIsNotNone(typed_ack)
        self.assertFalse(hasattr(typed_ack, "isReplay"))
        await client.emit(typed_ack)
        result = {"type": "result", "result": "done"}
        await client.emit(result)
        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [result])

    async def test_stop_before_ack_fails_and_disconnects_without_accepting_abort(self) -> None:
        self.factory.auto_ack = False
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        self.assertTrue(await self.manager.interrupt("chat-1", run_id="run-1"))
        await client.emit({
            "type": "result",
            "result": "stale abort",
            "terminal_reason": "aborted_streaming",
        })

        with self.assertRaises(ClaudeSDKQueryError) as raised:
            await handle.wait_result()
        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertTrue(client.disconnected)
        self.assertEqual(
            [call for call in client.calls if call[0] == "interrupt"],
            [("interrupt",)],
        )

    async def test_ack_timeout_warns_and_accepts_late_exact_replay(self) -> None:
        factory = FakeFactory()
        factory.auto_ack = False
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            ack_timeout_seconds=0.01,
        )
        handle = await manager.start_run(
            "chat-timeout",
            "Prompt",
            run_id="run-timeout",
            options={},
            configuration_key="same",
        )
        client = factory.clients[0]

        with self.assertLogs("claude_sdk_client", level="WARNING") as captured:
            await asyncio.sleep(0.03)
        self.assertIn("continuing to wait", "\n".join(captured.output))
        self.assertFalse(handle.done)
        self.assertFalse(handle.acknowledged)
        self.assertFalse(client.disconnected)

        await client.emit(replay_ack(handle.correlation_id))
        result = {"type": "result", "result": "late but valid"}
        await client.emit(result)
        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [result])
        self.assertEqual(await handle.wait_result(), result)
        self.assertFalse(client.disconnected)
        self.assertEqual(
            [call for call in client.calls if call[0] == "query"],
            [("query", "Prompt", {})],
        )
        await manager.close_all()

    async def test_default_ack_window_accepts_matching_replay_after_ten_seconds(self) -> None:
        factory = FakeFactory()
        factory.auto_ack = False
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        handle = await manager.start_run(
            "chat-slow-ack",
            "Prompt",
            run_id="run-slow-ack",
            options={},
            configuration_key="same",
        )
        client = factory.clients[0]

        await asyncio.sleep(10.05)
        self.assertFalse(handle.done)
        await client.emit(replay_ack(handle.correlation_id))
        result = {"type": "result", "result": "late but valid"}
        await client.emit(result)
        self.assertEqual(await asyncio.wait_for(collect(handle), 1), [result])
        self.assertEqual(await handle.wait_result(), result)
        await manager.close_all()

    async def test_query_delivery_timeout_bounds_cancellation_hostile_write(self) -> None:
        factory = CancellationHostileQueryFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.01,
            query_delivery_timeout_seconds=0.01,
        )
        start_task = asyncio.create_task(manager.start_run(
            "chat-query-timeout",
            "Prompt",
            run_id="run-query-timeout",
            options={},
            configuration_key="same",
        ))
        while not factory.clients:
            await asyncio.sleep(0)
        client = factory.clients[0]
        await asyncio.wait_for(client.query_started.wait(), 1)

        with self.assertRaises(ClaudeSDKQueryError) as raised:
            await asyncio.wait_for(start_task, 0.5)
        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertTrue(client.disconnected)
        self.assertEqual(
            [call for call in client.calls if call[0] == "query"],
            [("query", "Prompt", {})],
        )
        client.release_query.set()
        await asyncio.sleep(0)
        await manager.close_all()

    async def test_one_permanent_receiver_serves_multiple_runs(self) -> None:
        first = await self.manager.start_run(
            "chat-1",
            "First",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        await client.emit({"type": "result", "result": "first"})
        await first.wait_result()

        second = await self.manager.start_run(
            "chat-1",
            "Second",
            run_id="run-2",
            options={},
            configuration_key="same",
            query_session_id="logical-session",
        )
        await client.emit({"type": "result", "result": "second"})
        await second.wait_result()

        self.assertEqual(len(self.factory.clients), 1)
        self.assertNotEqual(first.correlation_id, second.correlation_id)
        self.assertEqual(
            [frames[0]["uuid"] for frames in client.query_envelopes],
            [first.correlation_id, second.correlation_id],
        )
        self.assertEqual(
            [call for call in client.calls if call[0] == "receive_messages"],
            [("receive_messages",)],
        )
        self.assertIn(
            ("query", "Second", {"session_id": "logical-session"}),
            client.calls,
        )

    async def test_late_run_one_result_cannot_finish_unacknowledged_steer(self) -> None:
        first = await self.manager.start_run(
            "chat-1",
            "First",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        for _ in range(100):
            if first.acknowledged:
                break
            await asyncio.sleep(0)
        self.assertTrue(first.acknowledged)
        self.assertTrue(await first.interrupt())
        first_result = {
            "type": "result",
            "result": "stopped",
            "terminal_reason": "aborted_streaming",
        }
        await client.emit(first_result)
        self.assertEqual(await first.wait_result(), first_result)

        client.auto_ack = False
        second = await self.manager.start_run(
            "chat-1",
            "Steered",
            run_id="run-2",
            options={},
            configuration_key="same",
        )
        await client.emit({"type": "result", "result": "late run one"})
        await asyncio.sleep(0)
        self.assertFalse(second.acknowledged)
        self.assertFalse(second.done)

        await client.emit(replay_ack(second.correlation_id))
        second_result = {"type": "result", "result": "fresh run two"}
        await client.emit(second_result)
        self.assertEqual(await asyncio.wait_for(collect(second), 1), [second_result])
        self.assertEqual(await second.wait_result(), second_result)

    async def test_context_usage_is_actor_serialized_and_owner_fenced(self) -> None:
        supervisor = await self.manager.get(
            "chat-1",
            options={},
            configuration_key="same",
        )
        handle = await self.manager.start_run(
            "chat-1",
            "Inspect",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        client = self.factory.clients[0]
        await client.emit({"type": "result", "result": "done"})
        await handle.wait_result()

        sampled = await self.manager.get_context_usage(
            "chat-1",
            ownership_token=supervisor.ownership_token,
        )

        self.assertIsNotNone(sampled)
        assert sampled is not None
        usage, generation = sampled
        self.assertEqual(usage, client.context_usage)
        self.assertEqual(generation, 1)
        self.assertIn(("get_context_usage",), client.calls)
        self.assertIsNone(await self.manager.get_context_usage(
            "chat-1",
            ownership_token="stale-owner",
        ))

    async def test_second_run_is_rejected_while_first_is_active(self) -> None:
        await self.manager.start_run(
            "chat-1",
            "First",
            run_id="run-1",
            options={},
            configuration_key="same",
        )
        with self.assertRaises(ClaudeSDKRunActive):
            await self.manager.start_run(
                "chat-1",
                "Second",
                run_id="run-2",
                options={},
                configuration_key="same",
            )

    async def test_interrupt_is_scoped_to_expected_chat_and_run(self) -> None:
        handle = await self.manager.start_run(
            "chat-1",
            "One",
            run_id="run-1",
            options={},
            configuration_key="a",
        )
        await self.manager.start_run(
            "chat-2",
            "Two",
            run_id="run-2",
            options={},
            configuration_key="b",
        )

        self.assertFalse(await self.manager.interrupt("chat-1", run_id="wrong"))
        self.assertTrue(await handle.interrupt())
        self.assertEqual(
            [call for call in self.factory.clients[0].calls if call[0] == "interrupt"],
            [("interrupt",)],
        )
        self.assertNotIn(("interrupt",), self.factory.clients[1].calls)

        await self.factory.clients[0].emit({"type": "result", "result": "stopped"})
        await handle.wait_result()
        self.assertFalse(await handle.interrupt())

    async def test_is_loaded_tracks_connected_client_lifecycle(self) -> None:
        self.assertFalse(self.manager.is_loaded("chat-1"))
        supervisor = await self.manager.get(
            "chat-1",
            options={},
            configuration_key="a",
        )
        self.assertFalse(self.manager.is_loaded("chat-1"))

        handle = await supervisor.start_run("Prompt", run_id="run-1")
        self.assertTrue(self.manager.is_loaded("chat-1"))
        await self.factory.clients[0].emit({"type": "result", "result": "done"})
        await handle.wait_result()
        self.assertTrue(self.manager.is_loaded("chat-1"))

        self.assertTrue(await self.manager.evict("chat-1"))
        self.assertFalse(self.manager.is_loaded("chat-1"))

    async def test_connect_failure_is_safe_to_fallback(self) -> None:
        self.factory.connect_error = RuntimeError("SDK unavailable")
        with self.assertRaises(ClaudeSDKUnavailable) as raised:
            await self.manager.start_run(
                "chat-1",
                "Prompt",
                run_id="run-1",
                options={},
                configuration_key="a",
            )
        self.assertTrue(raised.exception.safe_to_fallback)

    async def test_query_failure_is_delivery_uncertain_and_retires_only_chat(self) -> None:
        self.factory.query_error = RuntimeError("pipe broke")
        with self.assertRaises(ClaudeSDKQueryError) as raised:
            await self.manager.start_run(
                "chat-1",
                "Prompt",
                run_id="run-1",
                options={},
                configuration_key="a",
            )
        self.assertFalse(raised.exception.safe_to_fallback)
        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertTrue(self.factory.clients[0].disconnected)

    async def test_receiver_failure_fails_run_and_next_run_reconnects(self) -> None:
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="a",
        )
        await self.factory.clients[0].emit(RuntimeError("stream failed"))
        with self.assertRaisesRegex(Exception, "stream stopped"):
            await handle.wait_result()

        replacement = await self.manager.start_run(
            "chat-1",
            "Retry",
            run_id="run-2",
            options={},
            configuration_key="a",
        )
        self.assertEqual(len(self.factory.clients), 2)
        await self.factory.clients[1].emit({"type": "result", "result": "ok"})
        await replacement.wait_result()

    async def test_receiver_stop_before_ack_is_delivery_uncertain(self) -> None:
        self.factory.auto_ack = False
        handle = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="a",
        )
        client = self.factory.clients[0]
        await client.emit(StopAsyncIteration)

        with self.assertRaises(ClaudeSDKQueryError) as raised:
            await handle.wait_result()
        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertTrue(client.disconnected)

    async def test_configuration_change_replaces_idle_client_but_not_active_one(self) -> None:
        first = await self.manager.start_run(
            "chat-1",
            "First",
            run_id="run-1",
            options={"model": "a"},
            configuration_key="a",
        )
        with self.assertRaises(ClaudeSDKConfigurationConflict):
            await self.manager.get(
                "chat-1",
                options={"model": "b"},
                configuration_key="b",
            )
        await self.factory.clients[0].emit({"type": "result", "result": "done"})
        await first.wait_result()

        replacement = await self.manager.get(
            "chat-1",
            options={"model": "b"},
            configuration_key="b",
        )
        self.assertEqual(replacement.configuration_key, "b")
        self.assertTrue(self.factory.clients[0].disconnected)

    async def test_lru_never_evicts_active_chat(self) -> None:
        manager = ClaudeSDKSupervisorManager(
            client_factory=self.factory,
            max_clients=1,
            idle_ttl_seconds=None,
        )
        active = await manager.start_run(
            "chat-a",
            "Active",
            run_id="run-a",
            options={},
            configuration_key="a",
        )
        other = await manager.get(
            "chat-b",
            options={},
            configuration_key="b",
        )
        self.assertFalse(other.closed)
        self.assertEqual(
            [item.chat_id for item in manager.snapshots()],
            ["chat-a", "chat-b"],
        )
        self.assertTrue(self.factory.clients[0].connected)
        await self.factory.clients[0].emit({"type": "result", "result": "done"})
        await active.wait_result()
        await manager.close_all()

    async def test_evict_disconnects_only_selected_chat(self) -> None:
        run = await self.manager.start_run(
            "chat-1",
            "Prompt",
            run_id="run-1",
            options={},
            configuration_key="a",
        )
        client = self.factory.clients[0]
        self.assertFalse(await self.manager.evict("chat-1"))
        self.assertTrue(await self.manager.evict("chat-1", force=True))
        self.assertTrue(client.disconnected)
        with self.assertRaisesRegex(Exception, "closed"):
            await run.wait_result()

    async def test_force_evict_aborts_a_query_stuck_before_acceptance(self) -> None:
        factory = BlockingQueryFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.1,
        )
        start_task = asyncio.create_task(manager.start_run(
            "chat-stuck",
            "Prompt",
            run_id="run-stuck",
            options={},
            configuration_key="a",
        ))
        while not factory.clients:
            await asyncio.sleep(0)
        client = factory.clients[0]
        await asyncio.wait_for(client.query_started.wait(), 1)

        self.assertTrue(
            await asyncio.wait_for(
                manager.evict("chat-stuck", force=True),
                0.5,
            )
        )
        with self.assertRaises(ClaudeSDKSupervisorClosed):
            await start_task
        self.assertTrue(client.disconnected)
        self.assertFalse(manager.is_loaded("chat-stuck"))
        await manager.close_all()

    async def test_force_evict_bounds_hostile_receiver_and_allows_reconnect(self) -> None:
        factory = HostileThenNormalFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.02,
        )
        first = await manager.start_run(
            "chat-hostile",
            "First",
            run_id="run-first",
            options={},
            configuration_key="a",
        )
        hostile = factory.clients[0]
        assert isinstance(hostile, CancellationHostileReceiverClient)
        await asyncio.wait_for(hostile.receiver_started.wait(), 1)

        self.assertTrue(
            await asyncio.wait_for(
                manager.evict("chat-hostile", force=True),
                0.5,
            )
        )
        with self.assertRaises(ClaudeSDKSupervisorClosed):
            await first.wait_result()

        replacement = await asyncio.wait_for(
            manager.start_run(
                "chat-hostile",
                "Second",
                run_id="run-second",
                options={},
                configuration_key="a",
            ),
            0.5,
        )
        self.assertEqual(len(factory.clients), 2)
        await factory.clients[1].emit({"type": "result", "result": "done"})
        await replacement.wait_result()

        hostile.release_receiver.set()
        await asyncio.sleep(0)
        await manager.close_all()

    async def test_force_evict_fences_a_cancellation_hostile_connect_before_query(self) -> None:
        factory = HostileConnectFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.02,
        )
        start_task = asyncio.create_task(manager.start_run(
            "chat-hostile-connect",
            "Must never be delivered",
            run_id="run-hostile-connect",
            options={},
            configuration_key="a",
        ))
        while not factory.clients:
            await asyncio.sleep(0)
        hostile = factory.clients[0]
        await asyncio.wait_for(hostile.connect_started.wait(), 0.5)

        self.assertTrue(await asyncio.wait_for(
            manager.evict("chat-hostile-connect", force=True),
            0.5,
        ))
        with self.assertRaises(ClaudeSDKSupervisorClosed):
            await asyncio.wait_for(start_task, 0.5)
        self.assertFalse(any(call[0] == "query" for call in hostile.calls))

        hostile.release_connect.set()
        await asyncio.sleep(0.05)
        self.assertFalse(any(call[0] == "query" for call in hostile.calls))
        self.assertTrue(hostile.disconnected)
        await manager.close_all()

    async def test_admission_hook_runs_after_connect_with_active_owner_before_query(self) -> None:
        factory = HostileConnectFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.02,
        )
        hook_called = asyncio.Event()

        async def reject_stopped_query(ownership_token: str) -> None:
            client = factory.clients[0]
            self.assertTrue(client.connected)
            self.assertTrue(manager.owns_active_run(
                "chat-admission",
                ownership_token,
                "run-admission",
            ))
            hook_called.set()
            raise asyncio.CancelledError

        start_task = asyncio.create_task(manager.start_run(
            "chat-admission",
            "Must not be delivered after Stop",
            run_id="run-admission",
            options={},
            configuration_key="a",
            on_supervisor_ready=reject_stopped_query,
        ))
        while not factory.clients:
            await asyncio.sleep(0)
        client = factory.clients[0]
        await asyncio.wait_for(client.connect_started.wait(), 0.5)
        self.assertFalse(hook_called.is_set())
        client.release_connect.set()

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(start_task, 0.5)
        self.assertTrue(hook_called.is_set())
        self.assertFalse(any(call[0] == "query" for call in client.calls))
        await manager.close_all()


class ClaudeSDKLoopOwnershipTests(unittest.TestCase):
    def test_manager_rejects_cross_event_loop_use(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )

        async def bind() -> None:
            await manager.get(
                "chat-1",
                options={},
                configuration_key="a",
            )

        asyncio.run(bind())

        async def cross_loop() -> None:
            with self.assertRaises(ClaudeSDKLoopError):
                await manager.get(
                    "chat-1",
                    options={},
                    configuration_key="a",
                )

        asyncio.run(cross_loop())


class ClaudeSDKBackgroundTrackingHookTests(unittest.IsolatedAsyncioTestCase):
    def test_rejects_common_untracked_shell_detachment(self) -> None:
        for command in (
            "nohup python sweep.py > sweep.log 2>&1 &",
            "python sweep.py > sweep.log 2>&1 &",
            "disown %1",
            "env MODE=prod nohup ./worker",
            "setsid --fork ./worker",
            "setsid -f ./worker",
            "setsid ./foreground-worker",
            "bash -c 'python sweep.py > sweep.log 2>&1 &'",
            "sh -lc 'nohup python sweep.py &'",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(
                    claude_untracked_background_reason("Bash", {"command": command})
                )

    def test_rejects_native_bash_background_mode(self) -> None:
        self.assertIsNotNone(
            claude_untracked_background_reason(
                "Bash",
                {"command": "python sweep.py", "run_in_background": True},
            )
        )

    def test_allows_foreground_shell_syntax_without_detachment(self) -> None:
        for command in (
            "python sweep.py",
            "python sweep.py && python summarize.py",
            "echo 'literal & text'",
            r"echo literal \& text",
            "echo 'setsid -f is documentation'",
            "echo ok # later &",
            "python3 - <<'PY'\nx = 1 & 2\nPY",
            "cat <<'CPP'\nvoid f(int &value) {}\nCPP",
            "cat <<'HTML'\na &copy; b\nHTML",
            "python sweep.py > sweep.log 2>&1",
        ):
            with self.subTest(command=command):
                self.assertIsNone(
                    claude_untracked_background_reason("Bash", {"command": command})
                )
        self.assertIsNone(
            claude_untracked_background_reason(
                "Read", {"command": "nohup worker &"}
            )
        )

    async def test_hook_denies_with_actionable_tracked_background_instruction(self) -> None:
        result = await reject_untracked_background_hook(
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "./worker",
                    "run_in_background": True,
                },
            },
            "tool-1",
            {"signal": None},
        )
        output = result["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("run_in_background", output["permissionDecisionReason"])

    async def test_hook_allows_normal_bash_and_policy_is_bash_scoped(self) -> None:
        self.assertEqual(
            await reject_untracked_background_hook(
                {"tool_name": "Bash", "tool_input": {"command": "pwd"}},
                "tool-1",
                {"signal": None},
            ),
            {},
        )
        hooks = claude_background_tracking_hooks()
        self.assertEqual(set(hooks), {"PreToolUse"})
        matcher = hooks["PreToolUse"][0]
        self.assertEqual(matcher.matcher, "Bash")
        self.assertEqual(matcher.timeout, 5.0)
        self.assertEqual(matcher.hooks, [reject_untracked_background_hook])


if __name__ == "__main__":
    unittest.main()
