import asyncio
import gc
import json
import re
import unittest
from collections.abc import AsyncIterable, AsyncIterator
from importlib.metadata import version
from typing import Any

from claude_sdk_client import (
    CLAUDE_PROVIDER_MCP_SERVER_NAME,
    CLAUDE_PROVIDER_MCP_TOOL_NAME,
    CLAUDE_SDK_MCP_STATUS_SCAN_LIMIT,
    CLAUDE_SDK_MCP_STATUS_TRUNCATED_KEY,
    ClaudeSDKConfigurationConflict,
    ClaudeSDKControlTimeout,
    ClaudeSDKGenerationChanged,
    ClaudeSDKGoalReceipt,
    ClaudeSDKLoopError,
    ClaudeSDKMCPServerNotFound,
    ClaudeSDKQueryError,
    ClaudeSDKRunActive,
    ClaudeSDKSupervisorError,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKSupervisorManager,
    ClaudeSDKUnavailable,
    claude_background_tracking_hooks,
    reject_subagent_provider_tool_hook,
    _parse_claude_sdk_message,
    _query_message_stream,
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
        self.auto_queue = True
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
        self.mcp_servers: list[dict[str, Any]] = [
            {
                "name": "calendar",
                "status": "connected",
                "scope": "user",
                "tools": [{"name": "events"}],
            },
            {"name": "dayone", "status": "failed", "error": "private"},
            {"name": "login", "status": "needs-auth"},
            {"name": "disabled", "status": "disabled"},
        ]
        self.server_info: dict[str, Any] = {
            "commands": [
                {
                    "name": "review",
                    "description": "Review the current changes",
                    "argumentHint": "[focus]",
                }
            ],
            "account": {
                "email": "private@example.test",
                "organization": "Private Org",
            },
            "pid": 12345,
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
        if self.auto_queue and envelope and envelope[0].get("priority") in {"next", "now"}:
            await self.messages.put({"type": "command_lifecycle", "state": "queued",
                                     "command_uuid": correlation_id})
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

    async def stop_task(self, task_id: str) -> None:
        self._record("stop_task", task_id)

    async def send_now(self, message_uuid: str) -> dict[str, Any]:
        self._record("send_now", message_uuid)
        return {"send_now": "stopped", "still_queued": [message_uuid]}

    async def cancel_queued_input(self, message_uuid: str) -> dict[str, Any]:
        self._record("cancel_queued_input", message_uuid)
        return {"cancelled": True}

    async def get_context_usage(self) -> dict[str, Any]:
        self._record("get_context_usage")
        return dict(self.context_usage)

    async def get_mcp_status(self) -> dict[str, Any]:
        self._record("get_mcp_status")
        return {"mcpServers": [dict(item) for item in self.mcp_servers]}

    async def get_server_info(self) -> dict[str, Any]:
        self._record("get_server_info")
        return dict(self.server_info)

    async def reconnect_mcp_server(self, server_name: str) -> None:
        self._record("reconnect_mcp_server", server_name)
        for server in self.mcp_servers:
            if server.get("name") == server_name:
                server["status"] = "connected"

    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None:
        self._record("toggle_mcp_server", server_name, enabled)
        for server in self.mcp_servers:
            if server.get("name") == server_name:
                server["status"] = "connected" if enabled else "disabled"

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


class GuardedMCPServerList(list[dict[str, Any]]):
    """Raise if a status consumer touches the first row past the scan bound."""

    def __init__(self) -> None:
        super().__init__([
            {"name": f"server-{index:03d}", "status": "failed"}
            for index in range(CLAUDE_SDK_MCP_STATUS_SCAN_LIMIT)
        ] + [{"name": "beyond-limit", "status": "failed"}])
        self.beyond_limit_accesses = 0

    def __getitem__(self, index: Any) -> Any:
        if isinstance(index, int) and index >= CLAUDE_SDK_MCP_STATUS_SCAN_LIMIT:
            self.beyond_limit_accesses += 1
            raise AssertionError("MCP status scan crossed its bound")
        return super().__getitem__(index)

    def __iter__(self):
        for index in range(len(self)):
            yield self[index]


class GuardedMCPStatusClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.guarded_servers = GuardedMCPServerList()

    async def get_mcp_status(self) -> dict[str, Any]:
        self._record("get_mcp_status")
        return {
            "mcpServers": self.guarded_servers,
            "providerSecret": "must not survive the actor projection",
        }


class GuardedMCPFactory:
    def __init__(self) -> None:
        self.clients: list[GuardedMCPStatusClient] = []

    def __call__(self, options: Any) -> GuardedMCPStatusClient:
        client = GuardedMCPStatusClient(options)
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


class DisconnectSettledHostileConnectClient(CancellationHostileConnectClient):
    """A hostile connect that settles only when its transport is disconnected."""

    async def connect(self) -> None:
        await super().connect()
        if self.disconnected:
            self.connected = False

    async def disconnect(self) -> None:
        await super().disconnect()
        # A real SDK disconnect tears down the transport that connect() is
        # awaiting. The connect coroutine remains cancellation-hostile, but
        # must settle once its exact process has been disconnected.
        self.release_connect.set()


class HostileConnectFactory:
    def __init__(self) -> None:
        self.clients: list[CancellationHostileConnectClient] = []

    def __call__(self, options: Any) -> CancellationHostileConnectClient:
        client = CancellationHostileConnectClient(options)
        self.clients.append(client)
        return client


class HostileConnectThenNormalFactory:
    def __init__(self) -> None:
        self.clients: list[FakeClaudeClient] = []

    def __call__(self, options: Any) -> FakeClaudeClient:
        client: FakeClaudeClient
        if self.clients:
            client = FakeClaudeClient(options)
        else:
            client = DisconnectSettledHostileConnectClient(options)
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


class BlockingMCPStatusClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.status_started = asyncio.Event()
        self.release_status = asyncio.Event()

    async def get_mcp_status(self) -> dict[str, Any]:
        self._record("get_mcp_status")
        self.status_started.set()
        await self.release_status.wait()
        return {"mcpServers": [dict(item) for item in self.mcp_servers]}


class BlockingMCPFactory:
    def __init__(self) -> None:
        self.clients: list[BlockingMCPStatusClient] = []

    def __call__(self, options: Any) -> BlockingMCPStatusClient:
        client = BlockingMCPStatusClient(options)
        self.clients.append(client)
        return client


class CancellationHostileMCPStatusClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.status_started = asyncio.Event()
        self.release_status = asyncio.Event()
        self.late_completions = 0

    async def get_mcp_status(self) -> dict[str, Any]:
        self._record("get_mcp_status")
        self.status_started.set()
        while not self.release_status.is_set():
            try:
                await self.release_status.wait()
            except asyncio.CancelledError:
                continue
        self.late_completions += 1
        return {"mcpServers": [dict(item) for item in self.mcp_servers]}


class HostileMCPThenNormalFactory:
    def __init__(self) -> None:
        self.clients: list[FakeClaudeClient] = []

    def __call__(self, options: Any) -> FakeClaudeClient:
        client: FakeClaudeClient
        if self.clients:
            client = FakeClaudeClient(options)
        else:
            client = CancellationHostileMCPStatusClient(options)
        self.clients.append(client)
        return client


class CancellationHostileMCPToggleClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.toggle_started = asyncio.Event()
        self.release_toggle = asyncio.Event()
        self.late_completions = 0

    async def toggle_mcp_server(self, server_name: str, enabled: bool) -> None:
        self._record("toggle_mcp_server", server_name, enabled)
        self.toggle_started.set()
        while not self.release_toggle.is_set():
            try:
                await self.release_toggle.wait()
            except asyncio.CancelledError:
                continue
        self.late_completions += 1
        await super().toggle_mcp_server(server_name, enabled)


class HostileToggleThenNormalFactory:
    def __init__(self) -> None:
        self.clients: list[FakeClaudeClient] = []

    def __call__(self, options: Any) -> FakeClaudeClient:
        client: FakeClaudeClient
        if self.clients:
            client = FakeClaudeClient(options)
        else:
            client = CancellationHostileMCPToggleClient(options)
        self.clients.append(client)
        return client


class HostileToggleThenBlockingFactory:
    def __init__(self) -> None:
        self.clients: list[FakeClaudeClient] = []

    def __call__(self, options: Any) -> FakeClaudeClient:
        client: FakeClaudeClient
        if self.clients:
            client = BlockingMCPStatusClient(options)
        else:
            client = CancellationHostileMCPToggleClient(options)
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

    async def _start_force_send_fixture(self, chat: str = "force-chat", options=None):
        options = {} if options is None else options
        handle = await self.manager.start_run(
            chat, "Start work", run_id="force-run", options=options,
            configuration_key="same",
        )
        await asyncio.wait_for(handle.wait_acknowledged(), 1)
        supervisor = await self.manager.get(chat, options=options, configuration_key="same")
        await self.factory.clients[-1].emit({"type": "system", "subtype": "init",
                                             "capabilities": ["interrupt_send_now_v1", "msg_lifecycle_v1"]})
        await asyncio.sleep(0)
        await supervisor.get_context_usage()
        return handle, self.factory.clients[-1], supervisor

    async def test_force_send_preserves_agent_and_workflow_until_real_completion(self) -> None:
        for task_type in ("local_agent", "local_workflow"):
            with self.subTest(task_type=task_type):
                bindings = []
                options = {"_agentsdock_bind_provider_tool_owner": lambda *owner: bindings.append(owner)}
                handle, client, supervisor = await self._start_force_send_fixture(task_type, options)
                started = {"type": "system", "subtype": "task_started", "task_id": "child",
                           "task_type": task_type}
                await client.emit(started)
                await supervisor.get_context_usage()
                original_bindings = list(bindings)
                client.auto_ack = False
                steering = asyncio.create_task(self.manager.steer_run(
                    task_type, "Keep the child; change the parent response",
                    run_id=handle.run_id, ownership_token=supervisor.ownership_token,
                    query_session_id="provider-session",
                ))
                while len(client.query_envelopes) < 2:
                    await asyncio.sleep(0)
                frame = client.query_envelopes[-1][0]
                self.assertEqual(frame["priority"], "next")
                self.assertNotEqual(frame["uuid"], handle.correlation_id)
                old_result = {"type": "result", "terminal_reason": "aborted_streaming",
                              "user_message_uuid": handle.correlation_id}
                await client.emit(old_result)
                await client.emit(replay_ack("unrelated-input"))
                await supervisor.get_context_usage()
                self.assertFalse(steering.done())
                self.assertFalse(handle.done)
                self.assertEqual(handle.background_task_receipts[0]["status"], "running")
                await client.emit(replay_ack(frame["uuid"]))
                self.assertEqual(await asyncio.wait_for(steering, 1), frame["uuid"])
                await client.emit(old_result)  # A delayed predecessor Result after the ACK.
                await client.emit(replay_ack(frame["uuid"]))  # Duplicate protocol receipt.
                await client.emit({"type": "result", "result": "parent updated",
                                   "user_message_uuid": frame["uuid"]})
                await supervisor.get_context_usage()
                self.assertFalse(handle.done)
                self.assertTrue(self.manager.owns_active_run(task_type, supervisor.ownership_token, handle.run_id))
                self.assertEqual(bindings, original_bindings)
                self.assertNotIn(("interrupt",), client.calls)
                self.assertIn(("send_now", frame["uuid"]), client.calls)
                self.assertFalse(client.disconnected)
                finished = {"type": "system", "subtype": "task_notification",
                            "task_id": "child", "status": "completed"}
                final = {"type": "result", "result": "child finished", "user_message_uuid": "task-wake"}
                await client.emit(finished)
                await client.emit(final)
                received = await asyncio.wait_for(collect(handle), 1)
                self.assertIn(started, received)
                self.assertIn(finished, received)
                self.assertNotIn(old_result, received)
                self.assertEqual(received[-1], final)
                self.assertEqual(handle.background_task_receipts[0]["status"], "completed")

    async def test_force_send_tracks_child_completion_before_replay_ack(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "child",
                           "task_type": "local_agent"})
        client.auto_ack = False
        steering = asyncio.create_task(self.manager.steer_run(
            "force-chat", "Follow up", run_id=handle.run_id, ownership_token=supervisor.ownership_token,
        ))
        while len(client.query_envelopes) < 2:
            await asyncio.sleep(0)
        await client.emit({"type": "system", "subtype": "task_notification",
                           "task_id": "child", "status": "completed"})
        await client.emit({"type": "result", "result": "previous turn completed"})
        async with asyncio.timeout(1):
            while handle.background_task_receipts[0]["status"] != "completed":
                await asyncio.sleep(0)
        self.assertFalse(handle.done)
        self.assertEqual(handle.background_task_receipts[0]["status"], "completed")
        correlation = client.query_envelopes[-1][0]["uuid"]
        await client.emit(replay_ack(correlation))
        await asyncio.wait_for(steering, 1)
        final = {"type": "result", "result": "follow up complete", "user_message_uuid": correlation}
        await client.emit(final)
        self.assertEqual(await asyncio.wait_for(handle.wait_result(), 1), final)

    async def test_force_send_requires_exact_owner_and_final_admission(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        for run_id, owner in (("stale-run", supervisor.ownership_token), (handle.run_id, "stale-owner")):
            with self.assertRaises(ClaudeSDKGenerationChanged):
                await self.manager.steer_run("force-chat", "Wrong owner", run_id=run_id, ownership_token=owner)

        async def deny(owner):
            self.assertEqual(owner, supervisor.ownership_token)
            raise RuntimeError("stopped before delivery")

        with self.assertRaisesRegex(RuntimeError, "stopped before delivery"):
            await self.manager.steer_run(
                "force-chat", "Too late", run_id=handle.run_id, ownership_token=supervisor.ownership_token,
                on_before_send=deny,
            )
        self.assertEqual(len(client.query_envelopes), 1)
        self.assertFalse(handle.done)

    async def test_force_send_rejects_old_cli_before_query_or_interrupt(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        await client.emit({"type": "system", "subtype": "init", "capabilities": []})
        await asyncio.sleep(0)
        await supervisor.get_context_usage()
        with self.assertRaisesRegex(ClaudeSDKConfigurationConflict, "update Claude Code") as raised:
            await self.manager.steer_run("force-chat", "Keep children", run_id=handle.run_id,
                                         ownership_token=supervisor.ownership_token)
        self.assertTrue(raised.exception.safe_to_requeue)
        self.assertEqual(len(client.query_envelopes), 1)
        self.assertFalse(any(call[0] in {"interrupt", "send_now"} for call in client.calls))
        self.assertFalse(handle.done)

    async def test_force_send_control_waits_for_exact_native_queued_receipt(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = client.auto_queue = False
        steering = asyncio.create_task(self.manager.steer_run(
            "force-chat", "Follow up", run_id=handle.run_id, ownership_token=supervisor.ownership_token,
        ))
        while len(client.query_envelopes) < 2:
            await asyncio.sleep(0)
        await client.emit({"type": "command_lifecycle", "state": "queued", "command_uuid": "unrelated"})
        await asyncio.sleep(0)
        await supervisor.get_context_usage()
        self.assertFalse(any(call[0] == "send_now" for call in client.calls))
        correlation = client.query_envelopes[-1][0]["uuid"]
        queued = {"type": "command_lifecycle", "state": "queued", "command_uuid": correlation}
        await client.emit(queued)
        await client.emit(queued)
        await asyncio.sleep(0)
        await supervisor.get_context_usage()
        self.assertEqual([call for call in client.calls if call[0] == "send_now"], [("send_now", correlation)])
        self.assertFalse(steering.done())
        await client.emit(replay_ack(correlation))
        self.assertEqual(await asyncio.wait_for(steering, 1), correlation)

    async def test_force_send_control_failure_does_not_stop_tasks_or_replay_input(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False

        async def failed_control(_uuid):
            raise OSError("receipt transport unavailable")

        client.send_now = failed_control
        with self.assertRaises(ClaudeSDKQueryError) as raised:
            await self.manager.steer_run("force-chat", "Follow up", run_id=handle.run_id,
                                         ownership_token=supervisor.ownership_token)
        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertFalse(handle.done)
        self.assertFalse(client.disconnected)
        self.assertNotIn(("interrupt",), client.calls)
        self.assertEqual(len(client.query_envelopes), 2)
        await client.emit(replay_ack(client.query_envelopes[-1][0]["uuid"]))
        await asyncio.sleep(0)
        await supervisor.get_context_usage()
        self.assertIsNone(supervisor._pending_steer)

    async def test_force_send_cancel_before_write_does_not_send_or_interrupt(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        entered, release = asyncio.Event(), asyncio.Event()

        async def admission(_owner):
            entered.set()
            await release.wait()

        steering = asyncio.create_task(self.manager.steer_run(
            "force-chat", "Cancelled", run_id=handle.run_id, ownership_token=supervisor.ownership_token,
            on_before_send=admission,
        ))
        await asyncio.wait_for(entered.wait(), 1)
        steering.cancel()
        with self.assertRaises(asyncio.CancelledError) as raised:
            await steering
        release.set()
        await supervisor.get_context_usage()
        self.assertTrue(steering.cancelled())
        self.assertIs(getattr(raised.exception, "delivery_uncertain", None), False)
        self.assertIs(getattr(raised.exception, "safe_to_requeue", None), True)
        self.assertEqual(len(client.query_envelopes), 1)
        self.assertNotIn(("interrupt",), client.calls)
        self.assertFalse(handle.done)

    async def test_force_send_cancel_after_write_keeps_receipt_lane_without_retry(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        steering = asyncio.create_task(self.manager.steer_run(
            "force-chat", "Accepted maybe", run_id=handle.run_id, ownership_token=supervisor.ownership_token,
        ))
        while len(client.query_envelopes) < 2:
            await asyncio.sleep(0)
        steering.cancel()
        with self.assertRaises(ClaudeSDKQueryError) as raised:
            await steering
        self.assertTrue(raised.exception.delivery_uncertain)
        with self.assertRaises(ClaudeSDKRunActive):
            await self.manager.steer_run("force-chat", "Do not duplicate", run_id=handle.run_id,
                                         ownership_token=supervisor.ownership_token)
        with self.assertRaises(ClaudeSDKRunActive):
            await supervisor.clear_goal(run_id=handle.run_id)
        await client.emit({"type": "result", "terminal_reason": "aborted_streaming"})
        await supervisor.get_context_usage()
        self.assertFalse(handle.done)
        await client.emit(replay_ack(client.query_envelopes[-1][0]["uuid"]))
        await supervisor.get_context_usage()
        self.assertIsNone(supervisor._pending_steer)
        self.assertEqual(len(client.query_envelopes), 2)
        self.assertNotIn(("interrupt",), client.calls)
        self.assertFalse(client.disconnected)

    async def test_force_send_failed_write_keeps_children_and_late_ack_without_replay(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "child",
                           "task_type": "local_agent"})
        original_query = client.query

        async def failed_write(prompt, **kwargs):
            await original_query(prompt, **kwargs)
            raise OSError("write failed after accepting frame")

        client.query = failed_write
        with self.assertRaises(ClaudeSDKQueryError) as raised:
            await self.manager.steer_run("force-chat", "Uncertain", run_id=handle.run_id,
                                         ownership_token=supervisor.ownership_token)
        self.assertTrue(raised.exception.delivery_uncertain)
        self.assertFalse(raised.exception.safe_to_requeue)
        self.assertEqual(len(client.query_envelopes), 2)
        self.assertFalse(handle.done)
        self.assertFalse(client.disconnected)
        self.assertEqual(handle.background_task_receipts[0]["status"], "running")
        await client.emit(replay_ack(client.query_envelopes[-1][0]["uuid"]))
        await asyncio.sleep(0)
        await supervisor.get_context_usage()
        self.assertIsNone(supervisor._pending_steer)
        self.assertEqual(len(client.query_envelopes), 2)
        self.assertFalse(handle.done)
        self.assertNotIn(("interrupt",), client.calls)
        self.assertEqual(handle.background_task_receipts[0]["status"], "running")

    async def test_explicit_stop_during_force_send_still_interrupts_and_retires_children(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "child",
                           "task_type": "local_agent"})
        client.auto_ack = False
        steering = asyncio.create_task(self.manager.steer_run(
            "force-chat", "Follow up", run_id=handle.run_id, ownership_token=supervisor.ownership_token,
        ))
        while len(client.query_envelopes) < 2:
            await asyncio.sleep(0)
        self.assertTrue(await handle.interrupt())
        with self.assertRaises(ClaudeSDKQueryError):
            await steering
        self.assertIn(("interrupt",), client.calls)
        terminal = {"type": "result", "terminal_reason": "aborted_streaming"}
        await client.emit(terminal)
        self.assertEqual(await asyncio.wait_for(handle.wait_result(), 1), terminal)
        await supervisor.get_context_usage()
        self.assertTrue(client.disconnected)
        self.assertEqual(handle.background_task_receipts[0]["status"], "tracking_lost")

    async def test_busy_goal_native_queue_preserves_run_children_and_waits_for_exact_ack(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "child", "task_type": "local_agent"})
        command_id = await self.manager.queue_goal(
            "force-chat", "Finish safely", run_id=handle.run_id,
            ownership_token=supervisor.ownership_token, command_id="goal-input",
            expected_generation=supervisor.control_generation,
        )
        self.assertEqual(command_id, "goal-input")
        frame = client.query_envelopes[-1][0]
        self.assertEqual(frame["message"]["content"], "/goal Finish safely")
        self.assertEqual(frame["priority"], "now")
        self.assertNotIn("client_composed", frame)
        old_result = {"type": "result", "result": "prior", "user_message_uuid": handle.correlation_id}
        await client.emit(old_result)
        await client.emit(replay_ack("unrelated"))
        await supervisor.get_context_usage()
        self.assertFalse(handle.done)
        self.assertIsNotNone(supervisor._pending_goal)
        await client.emit(replay_ack(command_id))
        await client.emit(old_result)
        await client.emit({"type": "result", "result": "goal parent response", "user_message_uuid": command_id})
        await supervisor.get_context_usage()
        self.assertFalse(handle.done)
        self.assertIsNone(supervisor._pending_goal)
        self.assertEqual(handle.background_task_receipts[0]["status"], "running")
        await client.emit({"type": "system", "subtype": "task_notification", "task_id": "child", "status": "completed"})
        final = {"type": "result", "result": "complete", "user_message_uuid": "child-wake"}
        await client.emit(final)
        self.assertEqual(await handle.wait_result(), final)
        receipts = [row for row in await collect(handle) if isinstance(row, ClaudeSDKGoalReceipt)]
        self.assertEqual(receipts, [ClaudeSDKGoalReceipt(command_id)])
        self.assertFalse(any(call[0] in {"interrupt", "send_now"} for call in client.calls))
        self.assertFalse(client.disconnected)

    async def test_busy_goal_refusal_settles_without_finishing_running_children(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "workflow", "task_type": "local_workflow"})
        await self.manager.queue_goal("force-chat", "Finish", run_id=handle.run_id,
                                      ownership_token=supervisor.ownership_token, command_id="goal-input")
        await client.emit({"type": "result", "result": "prior", "user_message_uuid": handle.correlation_id})
        await client.emit({"type": "result", "local_command": "goal", "is_error": False,
                           "result": "Hooks are restricted", "user_message_uuid": "goal-input"})
        await supervisor.get_context_usage()
        self.assertIsNone(supervisor._pending_goal)
        self.assertFalse(handle.done)
        self.assertEqual(handle.background_task_receipts[0]["status"], "running")
        await client.emit({"type": "system", "subtype": "task_notification", "task_id": "workflow", "status": "completed"})
        await client.emit({"type": "result", "result": "workflow finished"})
        receipts = [row for row in await collect(handle) if isinstance(row, ClaudeSDKGoalReceipt)]
        self.assertEqual(receipts, [ClaudeSDKGoalReceipt("goal-input", "Hooks are restricted")])
        self.assertFalse(client.disconnected)

    async def test_pending_goal_does_not_block_force_send(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await self.manager.queue_goal("force-chat", "Finish", run_id=handle.run_id,
                                      ownership_token=supervisor.ownership_token, command_id="goal-input")
        force_send = asyncio.create_task(self.manager.steer_run(
            "force-chat", "Follow up", run_id=handle.run_id, ownership_token=supervisor.ownership_token,
        ))
        while len(client.query_envelopes) < 3:
            await asyncio.sleep(0)
        followup_id = client.query_envelopes[-1][0]["uuid"]
        await client.emit(replay_ack(followup_id))
        self.assertEqual(await asyncio.wait_for(force_send, 1), followup_id)
        self.assertIsNotNone(supervisor._pending_goal)
        await client.emit({"type": "result", "user_message_uuid": followup_id})
        await supervisor.get_context_usage()
        self.assertFalse(handle.done)
        await client.emit(replay_ack("goal-input"))
        final = {"type": "result", "result": "finished", "user_message_uuid": "goal-input"}
        await client.emit(final)
        self.assertEqual(await handle.wait_result(), final)
        self.assertEqual([call for call in client.calls if call[0] == "send_now"], [("send_now", followup_id)])

    async def test_busy_goal_refused_or_discarded_before_dispatch_settles_receipt(self) -> None:
        for state in ("refused", "discarded"):
            with self.subTest(state=state):
                handle, client, supervisor = await self._start_force_send_fixture(state)
                client.auto_ack = False
                await self.manager.queue_goal(state, "Finish", run_id=handle.run_id,
                                              ownership_token=supervisor.ownership_token, command_id="goal-input")
                await client.emit({"type": "command_lifecycle", "command_uuid": "goal-input", "state": state})
                await supervisor.get_context_usage()
                self.assertIsNone(supervisor._pending_goal)
                self.assertFalse(handle.done)
                receipt = await handle.__anext__()
                while not isinstance(receipt, ClaudeSDKGoalReceipt):
                    receipt = await handle.__anext__()
                self.assertIn(state, receipt.error)

    async def test_busy_goal_rejects_stale_owner_generation_and_clear_alias_without_writing(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        cases = [dict(ownership_token="stale"), dict(run_id="stale"), dict(expected_generation="stale")]
        for overrides in cases:
            with self.assertRaises(ClaudeSDKGenerationChanged):
                await self.manager.queue_goal("force-chat", "Finish", **{
                    "run_id": handle.run_id, "ownership_token": supervisor.ownership_token,
                    "command_id": "goal-input", **overrides,
                })
        for condition in ("clear", "Stop", "off", "reset", "none", "cancel"):
            with self.assertRaises(ValueError):
                await self.manager.queue_goal("force-chat", condition, run_id=handle.run_id,
                                              ownership_token=supervisor.ownership_token, command_id="goal-input")
        self.assertEqual(len(client.query_envelopes), 1)
        self.assertFalse(handle.done)

    async def test_busy_goal_explicit_stop_and_disconnect_settle_pending_receipt(self) -> None:
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                chat = f"goal-stop-{disconnect}"
                handle, client, supervisor = await self._start_force_send_fixture(chat)
                client.auto_ack = False
                await self.manager.queue_goal(chat, "Finish", run_id=handle.run_id,
                                              ownership_token=supervisor.ownership_token, command_id="goal-input")
                if disconnect:
                    await client.emit(OSError("transport closed"))
                else:
                    await handle.interrupt()
                    self.assertLess(client.calls.index(("cancel_queued_input", "goal-input")),
                                    client.calls.index(("interrupt",)))
                    self.assertFalse(handle.done)
                    self.assertFalse(client.disconnected)
                    await client.emit({"type": "result", "terminal_reason": "aborted_tools", "is_error": True})
                try:
                    await handle.wait_result()
                except ClaudeSDKSupervisorError:
                    pass
                self.assertIsNone(supervisor._pending_goal)
                receipt = await handle.__anext__()
                while not isinstance(receipt, ClaudeSDKGoalReceipt):
                    receipt = await handle.__anext__()
                self.assertEqual(receipt.command_id, "goal-input")
                self.assertIsNotNone(receipt.error)
                self.assertEqual(receipt.cancelled_by_user, not disconnect)
                if not disconnect:
                    self.assertTrue(supervisor.closed)
                    self.assertTrue(client.disconnected)
                    next_handle = await self.manager.start_run(
                        chat, "Continue", run_id="next-run", options={}, configuration_key="native-force",
                    )
                    self.assertIsNot(self.factory.clients[-1], client)
                    self.assertEqual(next_handle.run_id, "next-run")

    async def test_goal_stop_stops_late_native_task_and_waits_for_its_terminal_receipt(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await self.manager.queue_goal("force-chat", "Finish", run_id=handle.run_id,
                                      ownership_token=supervisor.ownership_token, command_id="goal-input")
        await handle.interrupt()
        await client.emit({"type": "system", "subtype": "task_started",
                           "task_id": "late-foreground", "task_type": "local_bash"})
        terminal = {"type": "result", "terminal_reason": "aborted_tools", "is_error": True}
        await client.emit(terminal)
        for _ in range(100):
            if supervisor._goal_stop_result is not None:
                break
            await asyncio.sleep(0)
        self.assertIn(("stop_task", "late-foreground"), client.calls)
        self.assertFalse(handle.done)
        self.assertFalse(client.disconnected)
        with self.assertRaises(ClaudeSDKRunActive):
            await self.manager.steer_run("force-chat", "Too late", run_id=handle.run_id,
                                          ownership_token=supervisor.ownership_token)
        await client.emit({"type": "system", "subtype": "task_notification",
                           "task_id": "late-foreground", "status": "stopped"})
        self.assertEqual(await asyncio.wait_for(handle.wait_result(), 1), terminal)
        self.assertTrue(client.disconnected)
        next_handle = await self.manager.start_run("force-chat", "Next", run_id="next-run",
                                                    options={}, configuration_key="native-force")
        next_client = self.factory.clients[-1]
        await next_client.emit({"type": "system", "subtype": "task_started",
                                "task_id": "next-foreground", "task_type": "local_bash"})
        await asyncio.wait_for(next_handle.__anext__(), 1)
        self.assertNotIn(("stop_task", "next-foreground"), next_client.calls)

    async def test_goal_stop_withdraws_concurrent_force_send_before_draining_tasks(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await self.manager.queue_goal("force-chat", "Finish", run_id=handle.run_id,
                                      ownership_token=supervisor.ownership_token, command_id="goal-input")
        followup = asyncio.create_task(self.manager.steer_run("force-chat", "Follow up",
            run_id=handle.run_id, ownership_token=supervisor.ownership_token))
        while len(client.query_envelopes) < 3:
            await asyncio.sleep(0)
        followup_id = client.query_envelopes[-1][0]["uuid"]
        await handle.interrupt()
        with self.assertRaises(ClaudeSDKQueryError):
            await followup
        self.assertLess(client.calls.index(("cancel_queued_input", followup_id)),
                        client.calls.index(("interrupt",)))
        self.assertLess(client.calls.index(("cancel_queued_input", "goal-input")),
                        client.calls.index(("interrupt",)))
        await client.emit({"type": "result", "terminal_reason": "aborted_tools", "is_error": True})
        await asyncio.wait_for(handle.wait_result(), 1)

    async def test_goal_stop_ignores_delayed_result_from_superseded_force_send_input(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        await self.manager.steer_run("force-chat", "Follow up", run_id=handle.run_id,
                                    ownership_token=supervisor.ownership_token)
        current_id = client.query_envelopes[-1][0]["uuid"]
        client.auto_ack = False
        await self.manager.queue_goal("force-chat", "Finish", run_id=handle.run_id,
                                      ownership_token=supervisor.ownership_token, command_id="goal-input")
        await handle.interrupt()
        await client.emit({"type": "result", "user_message_uuid": handle.correlation_id})
        await supervisor.get_context_usage()
        self.assertIsNone(supervisor._goal_stop_result)
        self.assertFalse(handle.done)
        self.assertFalse(client.disconnected)
        await client.emit({"type": "system", "subtype": "task_started",
                           "task_id": "current-foreground", "task_type": "local_bash"})
        await client.emit({"type": "system", "subtype": "task_notification",
                           "task_id": "current-foreground", "status": "stopped"})
        await client.emit({"type": "result", "user_message_uuid": current_id,
                           "terminal_reason": "aborted_tools", "is_error": True})
        await asyncio.wait_for(handle.wait_result(), 1)
        self.assertIn(("stop_task", "current-foreground"), client.calls)

    async def test_goal_stop_still_interrupts_and_drains_tasks_when_withdrawal_fails(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await self.manager.queue_goal("force-chat", "Finish", run_id=handle.run_id,
                                      ownership_token=supervisor.ownership_token, command_id="goal-input")

        async def failed_cancel(_message_id):
            raise OSError("control write uncertain")

        client.cancel_queued_input = failed_cancel
        with self.assertRaisesRegex(ClaudeSDKQueryError, "withdrawal is uncertain"):
            await handle.interrupt()
        self.assertIn(("interrupt",), client.calls)
        self.assertFalse(handle.done)
        await client.emit({"type": "system", "subtype": "task_started",
                           "task_id": "late-foreground", "task_type": "local_bash"})
        await client.emit({"type": "system", "subtype": "task_notification",
                           "task_id": "late-foreground", "status": "stopped"})
        await client.emit({"type": "result", "terminal_reason": "aborted_tools", "is_error": True})
        await asyncio.wait_for(handle.wait_result(), 1)
        self.assertIn(("stop_task", "late-foreground"), client.calls)
        self.assertTrue(client.disconnected)

    async def test_busy_goal_clear_withdraws_queued_goal_before_native_clear(self) -> None:
        handle, client, supervisor = await self._start_force_send_fixture()
        client.auto_ack = False
        await self.manager.queue_goal("force-chat", "Finish", run_id=handle.run_id,
                                      ownership_token=supervisor.ownership_token, command_id="goal-input")
        clear = asyncio.create_task(self.manager.clear_goal("force-chat", run_id=handle.run_id))
        while len(client.query_envelopes) < 3:
            await asyncio.sleep(0)
        self.assertIsNone(supervisor._pending_goal)
        self.assertLess(client.calls.index(("cancel_queued_input", "goal-input")), client.calls.index(("interrupt",)))
        self.assertEqual(client.query_envelopes[-1][0]["message"]["content"], "/goal clear")
        await client.emit({"type": "result", "terminal_reason": "aborted_tools", "is_error": True})
        await client.emit({"type": "assistant", "local_command_run": {"command": "goal", "args": "clear"}})
        await client.emit({"type": "result", "local_command": "goal", "result": "Goal cleared"})
        await asyncio.wait_for(clear, 1)

    async def test_native_busy_goal_clear_reserves_receipt_after_aborted_run(self) -> None:
        for background in (False, True):
            with self.subTest(background=background):
                chat = f"goal-{background}"
                handle = await self.manager.start_run(
                    chat, "/goal finish the task", run_id="goal-run", options={},
                    configuration_key="same", validated_provider_command_name="goal",
                )
                client = self.factory.clients[-1]
                if background:
                    await client.emit({"type": "system", "subtype": "task_started",
                        "task_id": "child", "task_type": "local_agent"})
                    await asyncio.wait_for(handle.__anext__(), 1)
                generation = self.manager._supervisors[chat].control_generation
                clear = asyncio.create_task(self.manager.clear_goal(
                    chat, run_id="goal-run", expected_generation=generation,
                ))
                for _ in range(100):
                    if len(client.query_envelopes) == 2:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(client.query_envelopes[1][0]["priority"], "now")
                self.assertEqual(client.query_envelopes[1][0]["message"]["content"], "/goal clear")
                clear_call = next(index for index, call in enumerate(client.calls)
                    if call[:2] == ("query", "/goal clear"))
                self.assertEqual(client.calls[clear_call - 1], ("interrupt",))
                aborted = {"type": "result",
                    "subtype": "error_during_execution" if background else "success",
                    "is_error": background,
                    "terminal_reason": "aborted_tools" if background else "aborted_streaming",
                    "result": "partial"}
                await client.emit(aborted)
                self.assertEqual(await asyncio.wait_for(handle.wait_result(), 1), aborted)
                self.assertFalse(clear.done())
                self.assertFalse(client.disconnected)
                with self.assertRaises(ClaudeSDKRunActive):
                    await self.manager.start_run(chat, "next", run_id="next", options={}, configuration_key="same")
                await client.emit({"type": "assistant", "local_command_run": {
                    "command": "goal", "args": "clear"}, "content": []})
                await client.emit({"type": "result", "subtype": "success", "is_error": False,
                    "local_command": "goal", "result": "Goal cleared: finish the task"})
                result, returned_generation = await asyncio.wait_for(clear, 1)
                self.assertEqual(returned_generation, generation)
                self.assertEqual(result["result"], "Goal cleared: finish the task")
                self.assertEqual(client.disconnected, background)
                next_handle = await self.manager.start_run(chat, "next", run_id="next", options={}, configuration_key="same")
                self.assertFalse(next_handle.done)

    async def test_goal_clear_rejects_stale_owner_without_query(self) -> None:
        await self.manager.start_run("goal", "work", run_id="current", options={}, configuration_key="same")
        for arguments in ({"run_id": "previous"}, {"run_id": "current", "expected_generation": "previous"}):
            with self.assertRaises(ClaudeSDKGenerationChanged):
                await self.manager.clear_goal("goal", **arguments)
        self.assertEqual(len(self.factory.clients[0].query_envelopes), 1)

    async def test_goal_clear_before_replay_ack_ends_uncertain_run(self) -> None:
        self.factory.auto_ack = False
        handle = await self.manager.start_run("goal", "work", run_id="current", options={}, configuration_key="same")
        client = self.factory.clients[0]
        clear = asyncio.create_task(self.manager.clear_goal("goal", run_id="current"))
        for _ in range(100):
            if len(client.query_envelopes) == 2:
                break
            await asyncio.sleep(0)
        await client.emit({"type": "result", "terminal_reason": "aborted_streaming"})
        with self.assertRaises(ClaudeSDKQueryError):
            await asyncio.wait_for(handle.wait_result(), 1)
        self.assertFalse(client.disconnected)
        await client.emit({"type": "assistant", "local_command_run": {"command": "goal", "args": "clear"}})
        await client.emit({"type": "result", "local_command": "goal", "is_error": False, "result": "Goal cleared"})
        result, _ = await asyncio.wait_for(clear, 1)
        self.assertFalse(result["is_error"])
        self.assertTrue(client.disconnected)

    async def test_goal_clear_timeout_retires_exact_owner(self) -> None:
        await self.manager.start_run("goal", "work", run_id="current", options={}, configuration_key="same")
        self.manager._supervisors["goal"]._control_timeout_seconds = 0.01
        with self.assertRaises(ClaudeSDKControlTimeout):
            await self.manager.clear_goal("goal", run_id="current")
        self.assertTrue(self.factory.clients[0].disconnected)
        self.assertNotIn("goal", self.manager._supervisors)
        resumed = await self.manager.start_run(
            "goal", "continue", run_id="after-timeout", options={}, configuration_key="same",
        )
        await self.factory.clients[1].emit({"type": "result", "result": "resumed"})
        result = await asyncio.wait_for(resumed.wait_result(), 1)
        self.assertEqual(result["result"], "resumed")

    def test_sdk_parser_preserves_native_local_goal_provenance(self) -> None:
        message = _parse_claude_sdk_message({"type": "result", "subtype": "success",
            "duration_ms": 1, "duration_api_ms": 0, "is_error": False, "num_turns": 0,
            "session_id": "provider", "total_cost_usd": 0, "usage": {}, "result": "Goal cleared",
            "local_command": "goal", "terminal_reason": "completed"})
        self.assertEqual(message.local_command, "goal")
        self.assertEqual(message.terminal_reason, "completed")
        event = {"type": "active_goal", "value": None}
        self.assertEqual(_parse_claude_sdk_message(event), event)

    def test_sdk_parser_preserves_force_send_result_correlation(self) -> None:
        result = _parse_claude_sdk_message({
            "type": "result", "subtype": "success", "duration_ms": 1,
            "duration_api_ms": 1, "is_error": False, "num_turns": 1,
            "session_id": "provider", "total_cost_usd": 0, "usage": {},
            "result": "done", "user_message_uuid": "steering-input",
        })
        self.assertEqual(result.user_message_uuid, "steering-input")
        queued = {"type": "command_lifecycle", "state": "queued", "command_uuid": "steering-input"}
        self.assertEqual(_parse_claude_sdk_message(queued), queued)

    async def test_pending_mail_hint_is_exact_root_checkpoint_without_new_query(self) -> None:
        calls = []
        def notice() -> str | None:
            calls.append("notice")
            return "Pending replies are available in the inbox."
        options = {"hooks": claude_background_tracking_hooks(), "resume": "provider"}
        handle = await self.manager.start_run("mail-chat", "continue work", run_id="run-one",
            options=options, configuration_key="same", pending_mail_hint=notice)
        client = self.factory.clients[0]
        hook = client.options["hooks"]["PostToolUse"][0].hooks[0]
        self.assertIs(hook, client.options["hooks"]["PostToolUseFailure"][0].hooks[0])
        tool = {"hook_event_name": "PostToolUse", "session_id": "provider", "tool_use_id": "tool-one",
                "tool_name": "Read", "tool_input": {"private": "never copied"}, "tool_response": "never copied"}
        self.assertEqual(await hook(tool, "tool-one", {}), {})
        await client.emit({"type": "assistant", "session_id": "provider", "content": [
            {"type": "tool_use", "id": "tool-one", "name": "Read", "input": {}}]})
        await asyncio.wait_for(handle.__anext__(), 1)
        for changed in ({"agent_id": "child"}, {"session_id": "other"}, {"tool_name": "Bash"},
                        {"tool_use_id": "other"}, {"is_interrupt": True}):
            self.assertEqual(await hook({**tool, **changed}, "tool-one", {}), {})
        result = await hook(tool, "tool-one", {})
        self.assertEqual(result, {"hookSpecificOutput": {"hookEventName": "PostToolUse",
            "additionalContext": "Pending replies are available in the inbox."}})
        self.assertEqual(await hook(tool, "tool-one", {}), {})
        self.assertEqual(calls, ["notice"])
        self.assertFalse(handle.done)
        self.assertEqual([call[0] for call in client.calls if call[0] in {"query", "interrupt"}], ["query"])
        self.assertIn(("query", "continue work", {}), client.calls)

    async def test_pending_mail_hint_failure_bounds_and_retired_hook_fences(self) -> None:
        calls = []
        options = {"hooks": claude_background_tracking_hooks(), "resume": "provider"}
        first = await self.manager.start_run("mail-chat", "first", run_id="run-one", options=options,
            configuration_key="first", pending_mail_hint=lambda: calls.append("old") or "Old pending notice")
        old_client = self.factory.clients[0]
        old_hook = old_client.options["hooks"]["PostToolUse"][0].hooks[0]
        await old_client.emit({"type": "assistant", "session_id": "provider", "content": [
            {"type": "tool_use", "id": "old-tool", "name": "Read", "input": {}}]})
        await asyncio.wait_for(first.__anext__(), 1)
        await old_client.emit({"type": "result", "result": "finished"})
        await asyncio.wait_for(collect(first), 1)
        second = await self.manager.start_run("mail-chat", "second", run_id="run-two", options=options,
            configuration_key="second", pending_mail_hint=lambda: calls.append("new") or "x" * 2049)
        client = self.factory.clients[-1]
        hook = client.options["hooks"]["PostToolUseFailure"][0].hooks[0]
        old_input = {"hook_event_name": "PostToolUseFailure", "session_id": "provider",
                     "tool_use_id": "old-tool", "tool_name": "Read", "error": "private"}
        self.assertEqual(await old_hook(old_input, "old-tool", {}), {})
        self.assertEqual(await hook(old_input, "old-tool", {}), {})
        await client.emit({"type": "assistant", "session_id": "provider", "content": [
            {"type": "tool_use", "id": "new-tool", "name": "Read", "input": {}}]})
        await asyncio.wait_for(second.__anext__(), 1)
        self.assertEqual(await hook({**old_input, "tool_use_id": "new-tool"}, "new-tool", {}), {})
        self.assertEqual(calls, ["new"])
        self.assertFalse(second.done)

    async def test_pending_mail_hint_without_hook_or_before_ack_is_silent(self) -> None:
        self.factory.auto_ack = False
        calls = []
        handle = await self.manager.start_run("mail-chat", "continue", run_id="one",
            options={"hooks": claude_background_tracking_hooks(), "resume": "provider"},
            configuration_key="first", pending_mail_hint=lambda: calls.append("hint") or "Pending replies")
        client = self.factory.clients[0]
        hook = client.options["hooks"]["PostToolUse"][0].hooks[0]
        await client.emit({"type": "assistant", "session_id": "provider", "content": [
            {"type": "tool_use", "id": "tool", "name": "Read", "input": {}}]})
        await asyncio.sleep(0)
        self.assertEqual(await hook({"hook_event_name": "PostToolUse", "session_id": "provider",
            "tool_use_id": "tool", "tool_name": "Read"}, "tool", {}), {})
        self.assertFalse(handle.acknowledged)
        self.assertEqual(calls, [])
        ordinary = await self.manager.start_run("no-hooks", "ordinary", run_id="two", options={},
            configuration_key="none", pending_mail_hint=lambda: calls.append("missing") or "Pending replies")
        self.assertTrue(ordinary.accepted)
        self.assertEqual(calls, [])

    async def test_task_receipts_survive_interruption_without_inventing_cancellation(self) -> None:
        handle = await self.manager.start_run("chat-receipts", "Work", run_id="old-run",
                                              options={}, configuration_key="same")
        client = self.factory.clients[0]
        for task in ("completed", "stopped", "pending"):
            await client.emit({"type": "system", "subtype": "task_started", "task_id": task,
                               "task_type": "local_workflow", "session_id": "provider-one",
                               "tool_use_id": "tool-one", "description": "must not be retained"})
        await client.emit({"type": "system", "subtype": "task_updated", "task_id": "completed",
                           "patch": {"status": "completed", "result": "must not be retained"}})
        await client.emit({"type": "system", "subtype": "task_notification", "task_id": "stopped", "status": "stopped"})
        await client.emit({"type": "result", "terminal_reason": "aborted_tools", "is_error": False})
        await asyncio.wait_for(collect(handle), 1)
        receipts = handle.background_task_receipts
        self.assertEqual([item["status"] for item in receipts], ["completed", "stopped", "tracking_lost"])
        self.assertTrue(all(item["owner_run_id"] == "old-run" for item in receipts))
        self.assertNotIn("must not be retained", repr(receipts))
        with self.assertRaises(TypeError):
            receipts[0]["status"] = "running"
        self.assertEqual(handle.background_task_overflow_count, 0)

    async def test_reconciliation_hook_is_query_scoped_and_old_generation_cannot_consume_it(self) -> None:
        options = {"hooks": claude_background_tracking_hooks(), "resume": "provider-one"}
        reconciliation = {"tasks": [{"task_id": "prior-task", "task_type": "local_workflow",
                                     "owner_run_id": "prior-run", "status": "tracking_lost"}]}
        first = await self.manager.start_run("chat-hooks", "same prompt", run_id="first",
            options=options, configuration_key="same", background_task_reconciliation=reconciliation)
        old_client = self.factory.clients[0]
        old_hook = old_client.options["hooks"]["UserPromptSubmit"][0].hooks[0]
        await first.interrupt()
        hook_input = {"hook_event_name": "UserPromptSubmit", "prompt": "same prompt", "session_id": "provider-one"}
        self.assertEqual(await old_hook(hook_input, None, {}), {})
        self.assertFalse(first.background_task_reconciliation_consumed)
        await old_client.emit({"type": "result", "terminal_reason": "aborted_tools"})
        await asyncio.wait_for(collect(first), 1)
        second = await self.manager.start_run("chat-hooks", "same prompt", run_id="second",
            options=options, configuration_key="same", background_task_reconciliation=reconciliation)
        new_client = self.factory.clients[-1]
        self.assertIsNot(new_client, old_client)
        hook = new_client.options["hooks"]["UserPromptSubmit"][0].hooks[0]
        self.assertEqual(await old_hook(hook_input, None, {}), {})
        for patch in ({"session_id": "other"}, {"agent_id": "child"}, {"prompt": "other"}):
            self.assertEqual(await hook({**hook_input, **patch}, None, {}), {})
        self.assertFalse(second.background_task_reconciliation_consumed)
        result = await hook(hook_input, None, {})
        self.assertIn('"status":"tracking_lost"', result["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(second.background_task_reconciliation_consumed)
        await new_client.emit({"type": "assistant", "text": "Observed task status"})
        await asyncio.wait_for(second.__anext__(), 1)
        self.assertTrue(second.background_task_reconciliation_consumed)
        self.assertEqual(second.background_task_receipts, ())
        self.assertEqual(await hook(hook_input, None, {}), {})
        self.assertIn(("query", "same prompt", {}), new_client.calls)
        await new_client.emit({"type": "result", "result": "done"})
        await asyncio.wait_for(collect(second), 1)
        ordinary = await self.manager.start_run("chat-hooks", "same prompt", run_id="ordinary",
                                               options=options, configuration_key="same")
        self.assertEqual(await hook(hook_input, None, {}), {})
        self.assertFalse(ordinary.background_task_reconciliation_consumed)

    async def test_hook_emission_before_ack_then_abort_does_not_consume_reconciliation(self) -> None:
        self.factory.auto_ack = False
        handle = await self.manager.start_run("chat-pre-ack", "check", run_id="unacknowledged",
            options={"hooks": claude_background_tracking_hooks()}, configuration_key="same",
            background_task_reconciliation={"tasks": [{"task_id": "old-task", "task_type": "local_workflow",
                                                       "owner_run_id": "old-run", "status": "tracking_lost"}]})
        hook = self.factory.clients[0].options["hooks"]["UserPromptSubmit"][0].hooks[0]
        result = await hook({"hook_event_name": "UserPromptSubmit", "prompt": "check"}, None, {})
        self.assertIn("additionalContext", result["hookSpecificOutput"])
        self.assertFalse(handle.background_task_reconciliation_consumed)
        await handle.interrupt()
        with self.assertRaises(ClaudeSDKQueryError):
            await asyncio.wait_for(collect(handle), 1)
        self.assertFalse(handle.background_task_reconciliation_consumed)

    async def test_task_receipt_bound_keeps_later_active_work_over_old_terminals(self) -> None:
        handle = await self.manager.start_run("chat-receipt-bound", "Work", run_id="old-run",
                                              options={}, configuration_key="same")
        client = self.factory.clients[0]
        for index in range(64):
            await client.emit({"type": "system", "subtype": "task_started", "task_id": str(index), "task_type": "local_agent"})
            await client.emit({"type": "system", "subtype": "task_notification", "task_id": str(index), "status": "completed"})
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "later-workflow", "task_type": "local_workflow"})
        await client.emit({"type": "result", "terminal_reason": "aborted_tools"})
        await asyncio.wait_for(collect(handle), 1)
        receipts = handle.background_task_receipts
        self.assertEqual(len(receipts), 64)
        self.assertEqual(handle.background_task_overflow_count, 1)
        self.assertEqual(receipts[-1]["task_id"], "later-workflow")
        self.assertEqual(receipts[-1]["status"], "tracking_lost")

    async def test_reconciliation_is_bounded_and_missing_hook_does_not_deliver_query(self) -> None:
        tasks = [{"task_id": str(index) + "x" * 250, "task_type": "local_workflow",
                  "owner_run_id": "o" * 256, "provider_session_id": "p" * 256,
                  "tool_use_id": "t" * 256, "status": "completed"} for index in range(66)]
        tasks[63]["status"] = "tracking_lost"
        with self.assertRaises(ClaudeSDKConfigurationConflict):
            await self.manager.start_run("no-hook", "unchanged", run_id="missing", options={},
                configuration_key="same", background_task_reconciliation={"tasks": tasks})
        self.assertFalse(any(call[0] == "query" for call in self.factory.clients[0].calls))
        prompt = "literal \ud800 text"
        handle = await self.manager.start_run("bounded-hook", prompt, run_id="bounded",
            options={"hooks": claude_background_tracking_hooks()}, configuration_key="same",
            background_task_reconciliation={"tasks": tasks})
        client = self.factory.clients[-1]
        hook = client.options["hooks"]["UserPromptSubmit"][0].hooks[0]
        output = await hook({"hook_event_name": "UserPromptSubmit", "prompt": prompt}, None, {})
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context.encode("utf-8")), 8192)
        payload = json.loads(context.rsplit("\n", 1)[1])
        self.assertEqual(len(payload["tasks"]) + payload["overflow_count"], 66)
        self.assertEqual(payload["tasks"][0]["status"], "tracking_lost")
        for index in range(66):
            await client.emit({"type": "system", "subtype": "task_started", "task_id": str(index), "task_type": "local_agent"})
        await client.emit(RuntimeError("stream ended"))
        with self.assertRaises(Exception):
            await asyncio.wait_for(collect(handle), 1)
        self.assertEqual(len(handle.background_task_receipts), 64)
        self.assertEqual(handle.background_task_overflow_count, 2)
        self.assertTrue(all(item["status"] == "tracking_lost" for item in handle.background_task_receipts))

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

    async def test_leading_slash_text_uses_native_metadata_without_rewriting_prompt(self) -> None:
        cases = (
            ("/team use research", True),
            (" \ufeff  /compact this explanation", True),
            ("/hdd/projects/example.py\nPlease inspect this file.", True),
            ("\n\t/目录/文件.txt  \n", True),
            ("What does /team mean?", False),
            ("https://example.test/team", False),
            ("@README.md explain this file", False),
        )
        for index, (prompt, verbatim) in enumerate(cases):
            with self.subTest(prompt=prompt):
                handle = await self.manager.start_run(
                    "chat-1",
                    prompt,
                    run_id=f"run-{index}",
                    options={"cwd": "/tmp"},
                    configuration_key="config-a",
                )
                client = self.factory.clients[0]
                transmitted = client.calls[-1][1]
                self.assertEqual(transmitted, prompt)
                envelope = client.query_envelopes[-1][0]
                self.assertEqual(envelope["message"]["content"], prompt)
                if verbatim:
                    self.assertIs(envelope["client_composed"], True)
                else:
                    self.assertNotIn("client_composed", envelope)
                self.assertEqual(
                    client.query_envelopes[-1][0]["uuid"],
                    handle.correlation_id,
                )
                result = {"type": "result", "result": "done"}
                await client.emit(result)
                await handle.wait_result()

    async def test_validated_local_command_is_raw_and_streams_without_replay_ack(self) -> None:
        await self.manager.close_all()
        self.factory = FakeFactory()
        self.factory.auto_ack = False
        assistant = {"type": "assistant", "text": "command output"}
        result = {
            "type": "result",
            "is_error": False,
            "result": "done",
            "terminal_reason": None,
        }
        self.factory.query_prefix_messages = [assistant, result]
        self.manager = ClaudeSDKSupervisorManager(
            client_factory=self.factory,
            max_clients=4,
            idle_ttl_seconds=None,
            ack_timeout_seconds=0.01,
        )
        _info, generation = await self.manager.get_server_info(
            "command-chat",
            options={"cwd": "/tmp"},
            configuration_key="config-a",
        )

        handle = await self.manager.start_run(
            "command-chat",
            "/review staged files",
            run_id="run-command",
            options={"cwd": "/tmp"},
            configuration_key="config-a",
            validated_provider_command_name="review",
            expected_provider_command_generation=generation,
        )

        self.assertTrue(handle.accepted)
        self.assertTrue(handle.acknowledged)
        self.assertIn(
            ("query", "/review staged files", {}),
            self.factory.clients[0].calls,
        )
        self.assertNotIn(
            "client_composed", self.factory.clients[0].query_envelopes[0][0],
        )
        self.assertEqual(
            await asyncio.wait_for(collect(handle), 1),
            [assistant, result],
        )
        self.assertEqual(await handle.wait_result(), result)
        await asyncio.sleep(0.02)

    async def test_validated_local_command_matches_reconciliation_hook_prompt(self) -> None:
        options = {
            "cwd": "/tmp",
            "hooks": claude_background_tracking_hooks(),
        }
        _info, generation = await self.manager.get_server_info(
            "command-reconciliation-chat",
            options=options,
            configuration_key="config-a",
        )
        handle = await self.manager.start_run(
            "command-reconciliation-chat",
            "/review staged files",
            run_id="run-command-reconciliation",
            options=options,
            configuration_key="config-a",
            validated_provider_command_name="review",
            expected_provider_command_generation=generation,
            background_task_reconciliation={
                "tasks": [
                    {
                        "task_id": "prior-task",
                        "task_type": "local_workflow",
                        "owner_run_id": "prior-run",
                        "status": "tracking_lost",
                    }
                ]
            },
        )
        client = self.factory.clients[0]
        hook = client.options["hooks"]["UserPromptSubmit"][0].hooks[0]
        output = await hook(
            {
                "hook_event_name": "UserPromptSubmit",
                "prompt": "/review staged files",
            },
            None,
            {},
        )

        self.assertIn(
            '"status":"tracking_lost"',
            output["hookSpecificOutput"]["additionalContext"],
        )
        await client.emit({"type": "result", "result": "done"})
        await asyncio.wait_for(collect(handle), 1)

    async def test_command_discovery_reconnects_an_idle_pending_hook(self) -> None:
        options = {
            "cwd": "/tmp",
            "hooks": claude_background_tracking_hooks(),
        }
        _info, first_generation = await self.manager.get_server_info(
            "pending-reconciliation-chat",
            options=options,
            configuration_key="config-a",
        )
        first = await self.manager.start_run(
            "pending-reconciliation-chat",
            "Check background work",
            run_id="run-background-check",
            options=options,
            configuration_key="config-a",
            background_task_reconciliation={
                "tasks": [
                    {
                        "task_id": "prior-task",
                        "task_type": "local_workflow",
                        "owner_run_id": "prior-run",
                        "status": "tracking_lost",
                    }
                ]
            },
        )
        first_client = self.factory.clients[0]
        await first_client.emit({"type": "result", "result": "done"})
        await asyncio.wait_for(collect(first), 1)

        _info, current_generation = await self.manager.get_server_info(
            "pending-reconciliation-chat",
            options=options,
            configuration_key="config-a",
        )
        self.assertNotEqual(current_generation, first_generation)
        self.assertEqual(len(self.factory.clients), 2)
        second = await self.manager.start_run(
            "pending-reconciliation-chat",
            "/review staged files",
            run_id="run-command-after-reconnect",
            options=options,
            configuration_key="config-a",
            validated_provider_command_name="review",
            expected_provider_command_generation=current_generation,
        )
        second_client = self.factory.clients[1]
        self.assertIn(
            ("query", "/review staged files", {}),
            second_client.calls,
        )
        await second_client.emit({"type": "result", "result": "done"})
        await asyncio.wait_for(collect(second), 1)

    async def test_fork_reconnect_uses_latest_child_binding_without_replacing_live_owner(self) -> None:
        options = {
            "cwd": "/shared-project", "resume": "parent-native", "fork_session": True,
            "extra_args": {"resume-session-at": "parent-cutoff"},
            "hooks": claude_background_tracking_hooks(),
        }
        first = await self.manager.start_run(
            "child-chat", "First child turn", run_id="child-1",
            options=options, configuration_key="same",
            background_task_reconciliation={"tasks": [{
                "task_id": "prior-task", "task_type": "local_workflow",
                "owner_run_id": "prior-run", "status": "completed",
            }]},
        )
        first_client = self.factory.clients[0]
        child_options = {**options, "resume": "child-native", "fork_session": False,
                         "extra_args": {}}
        # The persisted native binding changes while this same process is
        # still active. Updating future reconnect options must not replace it.
        owner = await self.manager.get("child-chat", options=child_options, configuration_key="same")
        self.assertFalse(first_client.disconnected)
        self.assertIs(owner.options, options)
        await first_client.emit({"type": "result", "session_id": "child-native", "result": "child B"})
        await asyncio.wait_for(collect(first), 1)

        # No hook receipt was observed, so the actor itself reconnects. This
        # must resume B, not fork an updated parent conversation again.
        second = await self.manager.start_run(
            "child-chat", "Recall child B", run_id="child-2",
            options=child_options, configuration_key="same",
        )
        second_client = self.factory.clients[1]
        self.assertTrue(first_client.disconnected)
        self.assertEqual(second_client.options["resume"], "child-native")
        self.assertFalse(second_client.options["fork_session"])
        self.assertNotIn("resume-session-at", second_client.options["extra_args"])
        await second_client.emit({"type": "result", "session_id": "child-native", "result": "child B retained"})
        await asyncio.wait_for(collect(second), 1)

    async def test_fork_receiver_close_after_lookup_keeps_latest_child_binding(self) -> None:
        options = {"resume": "parent-native", "fork_session": True}
        first = await self.manager.start_run(
            "child-chat", "First child turn", run_id="child-1",
            options=options, configuration_key="same",
        )
        client = self.factory.clients[0]
        await client.emit({"type": "result", "session_id": "child-native", "result": "done"})
        await asyncio.wait_for(collect(first), 1)
        owner = await self.manager.get("child-chat", options=options, configuration_key="same")
        start = owner.start_run

        async def start_after_receiver_close(*args, **kwargs):
            # A real EOF lands after the registry lookup but before the actor
            # handles this start. There is no second lookup to refresh options.
            await client.emit(StopAsyncIteration)
            async def wait_for_close():
                while owner.connected:
                    await asyncio.sleep(0)
            await asyncio.wait_for(wait_for_close(), 1)
            return await start(*args, **kwargs)

        owner.start_run = start_after_receiver_close
        second = await self.manager.start_run(
            "child-chat", "Keep child context", run_id="child-2",
            options={"resume": "child-native", "fork_session": False},
            configuration_key="same",
        )
        replacement = self.factory.clients[1]
        self.assertEqual(replacement.options["resume"], "child-native")
        self.assertFalse(replacement.options["fork_session"])
        await replacement.emit({"type": "result", "session_id": "child-native", "result": "done"})
        await asyncio.wait_for(collect(second), 1)

    async def test_fork_reconciliation_matches_new_child_then_persisted_child_identity(self) -> None:
        options = {"resume": "parent-native", "fork_session": True,
                   "hooks": claude_background_tracking_hooks()}
        receipt = {"tasks": [{"task_id": "task", "task_type": "local_workflow",
                              "owner_run_id": "old", "status": "completed"}]}
        for index, current_options in enumerate((
            options, {**options, "resume": "child-native", "fork_session": False},
        )):
            prompt = f"Child turn {index}"
            handle = await self.manager.start_run(
                "child-chat", prompt, run_id=f"child-{index}",
                options=current_options, configuration_key="same",
                background_task_reconciliation=receipt,
            )
            self.assertEqual(len(self.factory.clients), 1)
            client = self.factory.clients[0]
            hook = client.options["hooks"]["UserPromptSubmit"][0].hooks[0]
            if index:
                self.assertEqual(await hook({"hook_event_name": "UserPromptSubmit",
                    "prompt": prompt, "session_id": "parent-native"}, None, {}), {})
            value = await hook({"hook_event_name": "UserPromptSubmit",
                "prompt": prompt, "session_id": "child-native"}, None, {})
            self.assertIn("additionalContext", value.get("hookSpecificOutput", {}))
            await client.emit({"type": "result", "session_id": "child-native", "result": "done"})
            await asyncio.wait_for(collect(handle), 1)

    async def test_validated_local_command_rejects_changed_generation_before_query(self) -> None:
        _info, generation = await self.manager.get_server_info(
            "generation-chat",
            options={"cwd": "/tmp"},
            configuration_key="config-a",
        )
        client = self.factory.clients[0]

        with self.assertRaises(ClaudeSDKGenerationChanged):
            await self.manager.start_run(
                "generation-chat",
                "/review",
                run_id="run-stale-generation",
                options={"cwd": "/tmp"},
                configuration_key="config-a",
                validated_provider_command_name="review",
                expected_provider_command_generation=generation + "-stale",
            )

        self.assertFalse(any(call[0] == "query" for call in client.calls))

    async def test_validated_local_command_requires_exact_byte_zero_token(self) -> None:
        for index, prompt in enumerate((
            "/review-more",
            " /review",
            "\ufeff/review",
            "/other",
            "/review\vdetails",
        )):
            with self.subTest(prompt=prompt):
                with self.assertRaises(ClaudeSDKSupervisorError):
                    await self.manager.start_run(
                        f"command-mismatch-{index}",
                        prompt,
                        run_id=f"run-mismatch-{index}",
                        options={},
                        configuration_key="config-a",
                        validated_provider_command_name="review",
                    )

        self.assertFalse(self.factory.clients)

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

    async def test_native_bash_liveness_survives_result_and_settles_while_idle(self) -> None:
        handle = await self.manager.start_run("background", "Start native Bash", run_id="run", options={}, configuration_key="same")
        client = self.factory.clients[0]
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "bash", "task_type": "local_bash"})
        await client.emit({"type": "result", "result": "Started"})
        await asyncio.wait_for(collect(handle), 1)
        self.assertIsNone(self.manager.snapshots()[0].active_run_id)
        before = self.manager.background_work_snapshot()
        self.assertTrue(before[0][4])
        # A future native schedule has no running task and must not become a
        # permanent admission blocker. Only live command receipts count.
        await client.emit({"type": "system", "subtype": "task_started", "task_id": "schedule", "task_type": "scheduled_task"})
        await client.emit({"type": "system", "subtype": "task_notification", "task_id": "bash", "status": "completed"})
        for _ in range(20):
            if not self.manager.background_work_snapshot()[0][4]:
                break
            await asyncio.sleep(0)
        after = self.manager.background_work_snapshot()
        self.assertFalse(after[0][4])
        self.assertGreater(after[0][3], before[0][3])
        self.assertFalse(client.disconnected)
        await self.manager.evict("background", force=True)
        self.assertEqual(self.manager.background_work_snapshot(), ())

    async def test_context_observation_cancel_or_timeout_keeps_native_background_owner(self) -> None:
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                manager = ClaudeSDKSupervisorManager(client_factory=self.factory, control_timeout_seconds=0.02)
                try:
                    handle = await manager.start_run("background", "Start", run_id="run", options={}, configuration_key="same")
                    client = self.factory.clients[-1]
                    await client.emit({"type": "system", "subtype": "task_started", "task_id": "bash", "task_type": "local_bash"})
                    await client.emit({"type": "result", "result": "Started"})
                    await asyncio.wait_for(collect(handle), 1)
                    started = asyncio.Event()
                    async def blocked_usage():
                        started.set()
                        await asyncio.Event().wait()
                    client.get_context_usage = blocked_usage
                    request = asyncio.create_task(manager.get_context_usage("background"))
                    await asyncio.wait_for(started.wait(), 1)
                    if cancel:
                        request.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await request
                    else:
                        with self.assertRaises(ClaudeSDKControlTimeout):
                            await asyncio.wait_for(request, 1)
                    self.assertFalse(client.disconnected)
                    self.assertTrue(manager.background_work_snapshot()[0][4])
                    # The observation cannot leave the actor queue wedged.
                    followup = await asyncio.wait_for(manager.start_run("background", "Follow up", run_id="next", options={}, configuration_key="same"), 1)
                    await client.emit({"type": "result", "result": "Done"})
                    await asyncio.wait_for(collect(followup), 1)
                    self.assertEqual(len([item for item in client.calls if item[0] == "connect"]), 1)
                finally:
                    await manager.close_all()

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

    async def test_cold_connect_timeout_retires_owner_and_reconnects_cleanly(self) -> None:
        factory = HostileConnectThenNormalFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            connect_timeout_seconds=0.02,
            disconnect_timeout_seconds=0.02,
        )
        try:
            with self.assertRaisesRegex(
                ClaudeSDKUnavailable,
                r"connect timed out after 0\.02s",
            ):
                await asyncio.wait_for(manager.start_run(
                    "chat-connect-timeout",
                    "Never delivered",
                    run_id="run-timeout",
                    options={},
                    configuration_key="a",
                ), 0.5)

            hostile = factory.clients[0]
            self.assertIsInstance(hostile, CancellationHostileConnectClient)
            self.assertTrue(hostile.disconnected)
            self.assertFalse(any(call[0] == "query" for call in hostile.calls))
            self.assertFalse(manager.snapshots())
            await asyncio.sleep(0)
            self.assertFalse([
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and "chat-connect-timeout" in task.get_name()
            ])

            replacement = await asyncio.wait_for(manager.start_run(
                "chat-connect-timeout",
                "Retry",
                run_id="run-retry",
                options={},
                configuration_key="a",
            ), 0.5)
            self.assertEqual(len(factory.clients), 2)
            await factory.clients[1].emit({"type": "result", "result": "done"})
            await replacement.wait_result()
        finally:
            await manager.close_all()

    async def test_late_connect_is_disconnected_again_after_owner_retirement(self) -> None:
        factory = HostileConnectFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            connect_timeout_seconds=0.02,
            disconnect_timeout_seconds=0.01,
        )
        try:
            with self.assertRaisesRegex(
                ClaudeSDKUnavailable,
                r"connect timed out after 0\.02s",
            ):
                await asyncio.wait_for(manager.start_run(
                    "chat-late-connect",
                    "Never delivered",
                    run_id="run-timeout",
                    options={},
                    configuration_key="a",
                ), 0.5)

            hostile = factory.clients[0]
            self.assertFalse(hostile.connected)
            self.assertEqual(
                sum(call[0] == "disconnect" for call in hostile.calls),
                1,
            )
            self.assertFalse(manager.snapshots())

            # The SDK ignored cancellation and established its transport only
            # after its supervisor had been retired. The retained exact-client
            # cleanup must observe that late completion and disconnect again.
            hostile.release_connect.set()
            for _ in range(50):
                if (
                    not hostile.connected
                    and sum(
                        call[0] == "disconnect" for call in hostile.calls
                    ) >= 2
                ):
                    break
                await asyncio.sleep(0.01)
            self.assertFalse(hostile.connected)
            self.assertGreaterEqual(
                sum(call[0] == "disconnect" for call in hostile.calls),
                2,
            )
            self.assertFalse([
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and "chat-late-connect" in task.get_name()
            ])
        finally:
            for client in factory.clients:
                client.release_connect.set()
            await asyncio.sleep(0)
            await manager.close_all()

    async def test_cancelled_cold_start_retires_owner_without_ready_callback(self) -> None:
        factory = HostileConnectThenNormalFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            connect_timeout_seconds=1,
            disconnect_timeout_seconds=0.02,
        )
        ready_owners: list[str] = []

        async def capture_owner(ownership_token: str) -> None:
            ready_owners.append(ownership_token)

        try:
            start_task = asyncio.create_task(manager.start_run(
                "chat-cancelled-connect",
                "Never delivered",
                run_id="run-cancelled",
                options={},
                configuration_key="a",
                on_supervisor_ready=capture_owner,
            ))
            while not factory.clients:
                await asyncio.sleep(0)
            hostile = factory.clients[0]
            assert isinstance(hostile, CancellationHostileConnectClient)
            await asyncio.wait_for(hostile.connect_started.wait(), 0.5)

            start_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(start_task, 0.5)

            self.assertFalse(ready_owners)
            self.assertTrue(hostile.disconnected)
            self.assertFalse(any(call[0] == "query" for call in hostile.calls))
            self.assertFalse(manager.snapshots())
            await asyncio.sleep(0)
            self.assertFalse([
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and "chat-cancelled-connect" in task.get_name()
            ])

            replacement = await asyncio.wait_for(manager.start_run(
                "chat-cancelled-connect",
                "Retry",
                run_id="run-retry",
                options={},
                configuration_key="a",
                on_supervisor_ready=capture_owner,
            ), 0.5)
            self.assertEqual(len(factory.clients), 2)
            self.assertEqual(len(ready_owners), 1)
            await factory.clients[1].emit({"type": "result", "result": "done"})
            await replacement.wait_result()
        finally:
            await manager.close_all()

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

    async def test_default_retention_keeps_native_background_clients_after_parent_result(self) -> None:
        manager = ClaudeSDKSupervisorManager(client_factory=self.factory)
        try:
            for index in range(14):
                chat = f"background-{index}"
                handle = await manager.start_run(chat, "Start background work", run_id=f"run-{index}",
                                                  options={}, configuration_key="same")
                client = self.factory.clients[-1]
                await client.emit({"type": "system", "subtype": "task_started",
                                   "task_id": f"shell-{index}", "task_type": "local_bash"})
                await client.emit({"type": "result", "result": "Background work started"})
                await handle.wait_result()
                manager._supervisors[chat]._last_used_at -= 86400
            self.assertEqual(await manager.evict_idle(), [])
            self.assertEqual(len(manager.snapshots()), 14)
            self.assertIsNone(manager._reaper_task)
            self.assertTrue(all(client.connected and not client.disconnected for client in self.factory.clients))
        finally:
            await manager.close_all()

    async def test_explicit_retention_limits_still_evict_idle_clients(self) -> None:
        for policy in ({"max_clients": 1}, {"idle_ttl_seconds": 60}):
            with self.subTest(policy=policy):
                manager = ClaudeSDKSupervisorManager(client_factory=self.factory, **policy)
                try:
                    handle = await manager.start_run("old", "Work", run_id="old-run", options={}, configuration_key="same")
                    client = self.factory.clients[-1]
                    await client.emit({"type": "result", "result": "Done"})
                    await handle.wait_result()
                    manager._supervisors["old"]._last_used_at -= 120
                    await manager.get("new", options={}, configuration_key="same")
                    self.assertTrue(client.disconnected)
                    self.assertEqual([item.chat_id for item in manager.snapshots()], ["new"])
                finally:
                    await manager.close_all()

    async def test_subagent_limit_reconfiguration_waits_for_background_agent_completion(self) -> None:
        cap_name = "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"
        handle = await self.manager.start_run(
            "chat-limited", "Delegate", run_id="run-limited",
            options={"env": {cap_name: "3"}}, configuration_key="limit-3",
        )
        client = self.factory.clients[0]
        started = {
            "type": "system", "subtype": "task_started",
            "task_id": "agent-still-running", "task_type": "local_agent",
        }
        progress = {"type": "assistant", "text": "Still waiting for the agent"}
        await client.emit(started)
        await client.emit({"type": "result", "is_error": False, "result": "parent milestone"})
        await client.emit(progress)
        self.assertEqual(await asyncio.wait_for(handle.__anext__(), 1), started)
        self.assertEqual(await asyncio.wait_for(handle.__anext__(), 1), progress)
        self.assertFalse(handle.done)

        with self.assertRaises(ClaudeSDKConfigurationConflict):
            await self.manager.get(
                "chat-limited", options={"env": {cap_name: "1"}},
                configuration_key="limit-1",
            )
        self.assertFalse(client.disconnected)
        self.assertEqual(len(self.factory.clients), 1)

        await client.emit({
            "type": "system", "subtype": "task_notification",
            "task_id": "agent-still-running", "status": "completed",
        })
        await client.emit({"type": "result", "is_error": False, "result": "all done"})
        await asyncio.wait_for(handle.wait_result(), 1)
        replacement = await self.manager.get(
            "chat-limited", options={"env": {cap_name: "1"}},
            configuration_key="limit-1",
        )
        self.assertEqual(replacement.configuration_key, "limit-1")
        self.assertTrue(client.disconnected)

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

    async def test_stale_owner_token_cannot_evict_replacement(self) -> None:
        owner_tokens: list[str] = []

        async def capture_owner(token: str) -> None:
            owner_tokens.append(token)

        old_run = await self.manager.start_run(
            "chat-1",
            "Old prompt",
            run_id="run-old",
            options={},
            configuration_key="a",
            on_supervisor_ready=capture_owner,
        )
        old_token = owner_tokens[-1]
        self.assertTrue(await self.manager.evict("chat-1", force=True))
        with self.assertRaisesRegex(Exception, "closed"):
            await old_run.wait_result()

        new_run = await self.manager.start_run(
            "chat-1",
            "New prompt",
            run_id="run-new",
            options={},
            configuration_key="a",
            on_supervisor_ready=capture_owner,
        )
        new_token = owner_tokens[-1]
        self.assertNotEqual(old_token, new_token)

        self.assertFalse(await self.manager.evict(
            "chat-1",
            force=True,
            ownership_token=old_token,
        ))
        self.assertTrue(self.manager.is_loaded("chat-1"))
        self.assertTrue(self.manager.owns_active_run(
            "chat-1",
            new_token,
            "run-new",
        ))
        await self.factory.clients[-1].emit({
            "type": "result",
            "result": "new done",
        })
        self.assertEqual(
            (await new_run.wait_result())["result"],
            "new done",
        )

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


class ClaudeSDKMCPControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_pinned_sdk_serializes_per_message_verbatim_metadata(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient, Transport

        class RecordingTransport(Transport):
            def __init__(self) -> None:
                self.frames: list[dict[str, Any]] = []
                self.incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
                self.ready = False

            async def connect(self) -> None:
                self.ready = True

            async def write(self, data: str) -> None:
                frame = json.loads(data)
                self.frames.append(frame)
                if frame["type"] == "control_request":
                    await self.incoming.put({
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": frame["request_id"],
                            "response": {},
                        },
                    })

            async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
                while self.ready:
                    yield await self.incoming.get()

            async def close(self) -> None:
                self.ready = False

            def is_ready(self) -> bool:
                return self.ready

            async def end_input(self) -> None:
                pass

        transport = RecordingTransport()
        client = ClaudeSDKClient(transport=transport)
        await client.connect()
        try:
            for prompt, command, verbatim in (
                (" \ufeff/hdd/project/source.py\nRead this.\n", None, True),
                ("/review staged files", "review", False),
                ("@README.md explain this file", None, False),
            ):
                with self.subTest(prompt=prompt):
                    await client.query(
                        _query_message_stream(prompt, "query-uuid", command),
                        session_id="provider-session",
                    )
                    frame = transport.frames[-1]
                    self.assertEqual(frame["message"], {
                        "role": "user", "content": prompt,
                    })
                    self.assertEqual(frame["uuid"], "query-uuid")
                    self.assertEqual(frame["session_id"], "provider-session")
                    if verbatim:
                        self.assertIs(frame["client_composed"], True)
                    else:
                        self.assertNotIn("client_composed", frame)
            await client.query(_query_message_stream(
                "/literal follow-up", "force-send-uuid", priority="now",
            ))
            self.assertEqual(transport.frames[-1]["priority"], "now")
            self.assertEqual(transport.frames[-1]["uuid"], "force-send-uuid")
            self.assertIs(transport.frames[-1]["client_composed"], True)
        finally:
            await client.disconnect()

    def test_pinned_sdk_exposes_native_mcp_controls(self) -> None:
        from claude_agent_sdk import ClaudeSDKClient

        self.assertEqual(version("claude-agent-sdk"), "0.2.130")
        for method in (
            "get_server_info",
            "get_mcp_status",
            "reconnect_mcp_server",
            "toggle_mcp_server",
        ):
            self.assertTrue(callable(getattr(ClaudeSDKClient, method, None)))

    async def test_server_info_projects_only_bounded_commands_and_keeps_active_run(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            handle = await manager.start_run(
                "command-info-chat",
                "Keep working",
                run_id="run-active",
                options={},
                configuration_key="profile-a",
            )
            client = factory.clients[0]
            client.server_info = {
                "commands": [
                    {
                        "name": "_private-command",
                        "description": "Useful command",
                        "argumentHint": "[value]",
                        "untrusted": {"path": "/Users/private/secret"},
                    },
                    {"name": "plugin:task", "description": "Plugin task"},
                ],
                "account": {"email": "private@example.test"},
                "models": [{"id": "secret-model-metadata"}],
                "pid": 999,
            }

            info, generation = await manager.get_server_info(
                "command-info-chat",
                options={},
                configuration_key="profile-a",
            )

            self.assertTrue(generation.startswith("claudemcp_"))
            self.assertEqual(
                info,
                {
                    "commands": [
                        {
                            "name": "_private-command",
                            "description": "Useful command",
                            "argumentHint": "[value]",
                        },
                        {
                            "name": "plugin:task",
                            "description": "Plugin task",
                        },
                    ],
                    "_agentsdock_provider_commands_truncated": False,
                },
            )
            self.assertFalse(handle.done)
            self.assertNotIn(("interrupt",), client.calls)

            result = {"type": "result", "result": "done"}
            await client.emit(result)
            self.assertEqual(await handle.wait_result(), result)
        finally:
            await manager.close_all()

    async def test_status_is_lazy_and_mutations_return_same_opaque_generation(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            status, generation = await manager.get_mcp_status(
                "mcp-chat",
                options={"cwd": "/tmp"},
                configuration_key="profile-a",
            )

            self.assertTrue(generation.startswith("claudemcp_"))
            self.assertEqual(len(generation), 34)
            self.assertEqual(status["mcpServers"][0]["name"], "calendar")
            client = factory.clients[0]
            self.assertEqual(client.calls[0], ("connect",))
            self.assertIn(("receive_messages",), client.calls)
            self.assertIn(("get_mcp_status",), client.calls)

            updated, settled_generation = await manager.mutate_mcp_server(
                "mcp-chat",
                action="disable",
                server_name="calendar",
                expected_generation=generation,
                options={"cwd": "/tmp"},
                configuration_key="profile-a",
            )

            self.assertEqual(settled_generation, generation)
            self.assertEqual(updated["mcpServers"][0]["status"], "disabled")
            self.assertIn(("toggle_mcp_server", "calendar", False), client.calls)
        finally:
            await manager.close_all()

    async def test_stale_generation_and_unknown_server_never_mutate(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            _status, generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            client = factory.clients[0]

            with self.assertRaises(ClaudeSDKGenerationChanged):
                await manager.mutate_mcp_server(
                    "mcp-chat",
                    action="disable",
                    server_name="calendar",
                    expected_generation="claudemcp_stale",
                    options={},
                    configuration_key="profile-a",
                )
            with self.assertRaises(ClaudeSDKMCPServerNotFound):
                await manager.mutate_mcp_server(
                    "mcp-chat",
                    action="reconnect",
                    server_name="missing",
                    expected_generation=generation,
                    options={},
                    configuration_key="profile-a",
                )

            self.assertNotIn(
                ("toggle_mcp_server", "calendar", False),
                client.calls,
            )
            self.assertNotIn(
                ("reconnect_mcp_server", "missing"),
                client.calls,
            )
        finally:
            await manager.close_all()

    async def test_noncanonical_provider_name_cannot_alias_control_name(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            _status, generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            client = factory.clients[0]
            client.mcp_servers = [
                {"name": " calendar ", "status": "connected"},
                {"name": "spoof\u202e", "status": "failed"},
            ]

            for server_name in ("calendar", "spoof\u202e"):
                with self.subTest(server_name=server_name):
                    with self.assertRaises(ClaudeSDKMCPServerNotFound):
                        await manager.mutate_mcp_server(
                            "mcp-chat",
                            action="disable",
                            server_name=server_name,
                            expected_generation=generation,
                            options={},
                            configuration_key="profile-a",
                        )

            self.assertFalse(any(
                call[0] == "toggle_mcp_server" for call in client.calls
            ))
        finally:
            await manager.close_all()

    async def test_actor_bounds_status_scan_and_never_controls_past_limit(self) -> None:
        factory = GuardedMCPFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            status, generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            client = factory.clients[0]

            self.assertEqual(
                len(status["mcpServers"]),
                CLAUDE_SDK_MCP_STATUS_SCAN_LIMIT,
            )
            self.assertTrue(status[CLAUDE_SDK_MCP_STATUS_TRUNCATED_KEY])
            self.assertNotIn("providerSecret", status)
            self.assertEqual(client.guarded_servers.beyond_limit_accesses, 0)

            with self.assertRaises(ClaudeSDKMCPServerNotFound):
                await manager.mutate_mcp_server(
                    "mcp-chat",
                    action="disable",
                    server_name="beyond-limit",
                    expected_generation=generation,
                    options={},
                    configuration_key="profile-a",
                )
            self.assertEqual(client.guarded_servers.beyond_limit_accesses, 0)
            self.assertNotIn(
                ("toggle_mcp_server", "beyond-limit", False),
                client.calls,
            )
        finally:
            await manager.close_all()

    async def test_reconnect_all_is_serial_and_respects_disabled_servers(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            _status, generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            factory.clients[0].mcp_servers.extend([
                {
                    "name": CLAUDE_PROVIDER_MCP_SERVER_NAME,
                    "status": "failed",
                },
                {
                    "name": "agentsdock",
                    "status": "failed",
                },
            ])
            updated, _generation = await manager.mutate_mcp_server(
                "mcp-chat",
                action="reconnect_all",
                server_name=None,
                expected_generation=generation,
                options={},
                configuration_key="profile-a",
            )

            client = factory.clients[0]
            reconnects = [call for call in client.calls if call[0] == "reconnect_mcp_server"]
            self.assertEqual(reconnects, [
                ("reconnect_mcp_server", "dayone"),
                ("reconnect_mcp_server", "login"),
                ("reconnect_mcp_server", "agentsdock"),
            ])
            self.assertNotIn(
                ("reconnect_mcp_server", CLAUDE_PROVIDER_MCP_SERVER_NAME),
                reconnects,
            )
            statuses = {
                item["name"]: item["status"]
                for item in updated["mcpServers"]
            }
            self.assertEqual(statuses["dayone"], "connected")
            self.assertEqual(statuses["login"], "connected")
            self.assertEqual(statuses["disabled"], "disabled")
        finally:
            await manager.close_all()

    async def test_status_and_mutation_fail_closed_during_active_run(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            handle = await manager.start_run(
                "mcp-chat",
                "keep working",
                run_id="run-active",
                options={},
                configuration_key="profile-a",
            )
            with self.assertRaises(ClaudeSDKRunActive):
                await manager.get_mcp_status(
                    "mcp-chat",
                    options={},
                    configuration_key="profile-a",
                )
            with self.assertRaises(ClaudeSDKRunActive):
                await manager.mutate_mcp_server(
                    "mcp-chat",
                    action="reconnect_all",
                    server_name=None,
                    expected_generation="claudemcp_stale",
                    options={},
                    configuration_key="profile-a",
                )
            self.assertFalse(any(call[0] == "get_mcp_status" for call in factory.clients[0].calls))
            await factory.clients[0].emit({"type": "result", "result": "done"})
            await handle.wait_result()
        finally:
            await manager.close_all()

    async def test_mcp_status_pin_blocks_idle_eviction(self) -> None:
        factory = BlockingMCPFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            control_timeout_seconds=1,
        )
        try:
            status_task = asyncio.create_task(manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            ))
            while not factory.clients:
                await asyncio.sleep(0)
            client = factory.clients[0]
            await asyncio.wait_for(client.status_started.wait(), 1)

            self.assertFalse(await manager.evict("mcp-chat", force=False))
            client.release_status.set()
            status, generation = await asyncio.wait_for(status_task, 1)
            self.assertTrue(status["mcpServers"])
            self.assertTrue(generation.startswith("claudemcp_"))
        finally:
            await manager.close_all()

    async def test_replacement_never_reuses_opaque_generation(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            _status, first_generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            self.assertTrue(await manager.evict("mcp-chat"))
            _status, second_generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )

            self.assertNotEqual(first_generation, second_generation)
            with self.assertRaises(ClaudeSDKGenerationChanged):
                await manager.mutate_mcp_server(
                    "mcp-chat",
                    action="disable",
                    server_name="calendar",
                    expected_generation=first_generation,
                    options={},
                    configuration_key="profile-a",
                )
        finally:
            await manager.close_all()

    async def test_closed_registry_owner_is_replaced_before_next_status(self) -> None:
        factory = FakeFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
        )
        try:
            _status, first_generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            first = manager._supervisors["mcp-chat"]
            await first.close()

            status, second_generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )

            self.assertIsNot(manager._supervisors["mcp-chat"], first)
            self.assertNotEqual(first_generation, second_generation)
            self.assertEqual(status["mcpServers"][0]["name"], "calendar")
            self.assertEqual(len(factory.clients), 2)
        finally:
            await manager.close_all()

    async def test_timed_out_mutation_retires_exact_owner_before_replacement(self) -> None:
        factory = HostileToggleThenNormalFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.01,
            control_timeout_seconds=0.01,
        )
        try:
            _status, first_generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            first = factory.clients[0]
            assert isinstance(first, CancellationHostileMCPToggleClient)

            with self.assertRaises(ClaudeSDKControlTimeout):
                await manager.mutate_mcp_server(
                    "mcp-chat",
                    action="disable",
                    server_name="calendar",
                    expected_generation=first_generation,
                    options={},
                    configuration_key="profile-a",
                )
            self.assertFalse(manager.snapshots())

            status, second_generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            second = factory.clients[1]
            self.assertNotEqual(first_generation, second_generation)
            self.assertEqual(status["mcpServers"][0]["status"], "connected")

            first.release_toggle.set()
            await asyncio.sleep(0.02)
            self.assertEqual(first.late_completions, 1)
            self.assertEqual(second.mcp_servers[0]["status"], "connected")
        finally:
            for client in factory.clients:
                if isinstance(client, CancellationHostileMCPToggleClient):
                    client.release_toggle.set()
            await manager.close_all()

    async def test_old_timeout_unpin_cannot_remove_replacement_pin(self) -> None:
        factory = HostileToggleThenBlockingFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.01,
            control_timeout_seconds=0.01,
        )
        old_unpin_entered = asyncio.Event()
        allow_old_unpin = asyncio.Event()
        mutation: asyncio.Task[Any] | None = None
        replacement: asyncio.Task[Any] | None = None
        try:
            _status, first_generation = await manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            )
            old_supervisor = manager._supervisors["mcp-chat"]
            original_unpin = manager._unpin_mcp_supervisor
            pause_old_unpin = True

            async def gated_unpin(chat_id: str, supervisor: Any) -> None:
                if pause_old_unpin and supervisor is old_supervisor:
                    old_unpin_entered.set()
                    await allow_old_unpin.wait()
                await original_unpin(chat_id, supervisor)

            manager._unpin_mcp_supervisor = gated_unpin  # type: ignore[method-assign]
            mutation = asyncio.create_task(manager.mutate_mcp_server(
                "mcp-chat",
                action="disable",
                server_name="calendar",
                expected_generation=first_generation,
                options={},
                configuration_key="profile-a",
            ))
            await asyncio.wait_for(old_unpin_entered.wait(), 1)

            replacement = asyncio.create_task(manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            ))
            while len(factory.clients) < 2:
                await asyncio.sleep(0)
            second = factory.clients[1]
            assert isinstance(second, BlockingMCPStatusClient)
            await asyncio.wait_for(second.status_started.wait(), 1)
            self.assertEqual(manager._pins.get("mcp-chat"), 1)

            allow_old_unpin.set()
            with self.assertRaises(ClaudeSDKControlTimeout):
                await mutation
            self.assertEqual(manager._pins.get("mcp-chat"), 1)
            self.assertFalse(await manager.evict("mcp-chat", force=False))

            second.release_status.set()
            await asyncio.wait_for(replacement, 1)
        finally:
            allow_old_unpin.set()
            for client in factory.clients:
                if isinstance(client, CancellationHostileMCPToggleClient):
                    client.release_toggle.set()
                if isinstance(client, BlockingMCPStatusClient):
                    client.release_status.set()
            for task in (mutation, replacement):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (mutation, replacement) if task is not None),
                return_exceptions=True,
            )
            await manager.close_all()

    async def test_cancelled_status_settles_response_without_future_warning(self) -> None:
        factory = BlockingMCPFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.05,
            control_timeout_seconds=1,
        )
        loop = asyncio.get_running_loop()
        contexts: list[dict[str, Any]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))
        try:
            status_task = asyncio.create_task(manager.get_mcp_status(
                "mcp-chat",
                options={},
                configuration_key="profile-a",
            ))
            while not factory.clients:
                await asyncio.sleep(0)
            client = factory.clients[0]
            await asyncio.wait_for(client.status_started.wait(), 1)

            status_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await status_task
            gc.collect()
            await asyncio.sleep(0)

            self.assertFalse(manager.snapshots())
            self.assertFalse([
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and not task.done()
                and "claude-sdk-mcp-status:mcp-chat" in task.get_name()
            ])
            self.assertFalse([
                context
                for context in contexts
                if "Future exception was never retrieved"
                in str(context.get("message") or "")
            ])
        finally:
            for client in factory.clients:
                client.release_status.set()
            loop.set_exception_handler(previous_handler)
            await manager.close_all()

    async def test_lazy_connect_is_covered_by_control_timeout(self) -> None:
        factory = HostileConnectFactory()
        manager = ClaudeSDKSupervisorManager(
            client_factory=factory,
            idle_ttl_seconds=None,
            disconnect_timeout_seconds=0.01,
            control_timeout_seconds=0.01,
        )
        try:
            with self.assertRaises(ClaudeSDKControlTimeout):
                await manager.get_mcp_status(
                    "mcp-chat",
                    options={},
                    configuration_key="profile-a",
                )
            self.assertFalse(manager.snapshots())
        finally:
            for client in factory.clients:
                client.release_connect.set()
            await asyncio.sleep(0)
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
    async def test_provider_tool_hook_allows_root_and_denies_subagent(self) -> None:
        root = await reject_subagent_provider_tool_hook(
            {
                "tool_name": CLAUDE_PROVIDER_MCP_TOOL_NAME,
                "tool_input": {},
            },
            "tool-root",
            {"signal": None},
        )
        self.assertEqual(root, {})

        child = await reject_subagent_provider_tool_hook(
            {
                "tool_name": CLAUDE_PROVIDER_MCP_TOOL_NAME,
                "tool_input": {},
                "agent_id": "child-1",
                "agent_type": "general-purpose",
            },
            "tool-child",
            {"signal": None},
        )
        output = child["hookSpecificOutput"]
        self.assertEqual(output["permissionDecision"], "deny")
        self.assertIn("top-level live turn", output["permissionDecisionReason"])

    async def test_native_background_and_scheduling_tools_have_no_added_policy_denial(self) -> None:
        hooks = claude_background_tracking_hooks()
        cases = [
            ("Bash", {"command": "python worker.py", "run_in_background": True}),
            ("Bash", {"command": "nohup python worker.py > worker.log 2>&1 &"}),
            ("Bash", {"command": "disown %1"}),
            ("Bash", {"command": "setsid ./worker"}),
            ("Bash", {"command": "sh -lc 'python worker.py &'"}),
            ("CronCreate", {"cron": "* * * * *", "prompt": "Check progress"}),
            ("Monitor", {}),
            ("ScheduleWakeup", {}),
        ]
        for tool_name, tool_input in cases:
            with self.subTest(tool_name=tool_name, tool_input=tool_input):
                for matcher in hooks.get("PreToolUse", []):
                    if matcher.matcher is None or re.fullmatch(matcher.matcher, tool_name):
                        for hook in matcher.hooks:
                            result = await hook({"tool_name": tool_name, "tool_input": tool_input},
                                                "native-tool", {"signal": None})
                            self.assertNotEqual(result.get("hookSpecificOutput", {}).get("permissionDecision"), "deny")

    def test_hooks_keep_receipts_mail_and_exact_provider_authority(self) -> None:
        hooks = claude_background_tracking_hooks()
        self.assertEqual(set(hooks), {"PreToolUse", "UserPromptSubmit", "PostToolUse", "PostToolUseFailure"})
        matchers = hooks["PreToolUse"]
        self.assertEqual([matcher.matcher for matcher in matchers], [CLAUDE_PROVIDER_MCP_TOOL_NAME])
        self.assertEqual(matchers[0].hooks, [reject_subagent_provider_tool_hook])


if __name__ == "__main__":
    unittest.main()
