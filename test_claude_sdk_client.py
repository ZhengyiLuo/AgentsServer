import asyncio
import unittest
from collections.abc import AsyncIterator
from typing import Any

from claude_sdk_client import (
    ClaudeSDKConfigurationConflict,
    ClaudeSDKLoopError,
    ClaudeSDKQueryError,
    ClaudeSDKRunActive,
    ClaudeSDKSupervisorClosed,
    ClaudeSDKSupervisorManager,
    ClaudeSDKUnavailable,
)


class FakeClaudeClient:
    def __init__(
        self,
        options: Any,
        *,
        connect_error: Exception | None = None,
        query_error: Exception | None = None,
    ) -> None:
        self.options = options
        self.connect_error = connect_error
        self.query_error = query_error
        self.messages: asyncio.Queue[Any] = asyncio.Queue()
        self.calls: list[tuple[Any, ...]] = []
        self.connected = False
        self.disconnected = False
        self.owner_loop: asyncio.AbstractEventLoop | None = None

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

    async def query(self, prompt: str, **kwargs: Any) -> None:
        self._record("query", prompt, kwargs)
        if self.query_error is not None:
            raise self.query_error

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

    def __call__(self, options: Any) -> FakeClaudeClient:
        client = FakeClaudeClient(
            options,
            connect_error=self.connect_error,
            query_error=self.query_error,
        )
        self.clients.append(client)
        return client


class BlockingQueryClient(FakeClaudeClient):
    def __init__(self, options: Any) -> None:
        super().__init__(options)
        self.query_started = asyncio.Event()

    async def query(self, prompt: str, **kwargs: Any) -> None:
        self._record("query", prompt, kwargs)
        self.query_started.set()
        await asyncio.Event().wait()


class BlockingQueryFactory:
    def __init__(self) -> None:
        self.clients: list[BlockingQueryClient] = []

    def __call__(self, options: Any) -> BlockingQueryClient:
        client = BlockingQueryClient(options)
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
        self.assertEqual(
            [call for call in client.calls if call[0] == "receive_messages"],
            [("receive_messages",)],
        )
        self.assertIn(
            ("query", "Second", {"session_id": "logical-session"}),
            client.calls,
        )

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


if __name__ == "__main__":
    unittest.main()
