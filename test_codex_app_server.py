import asyncio
import json
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerDisconnected,
    CodexAppServerManager,
    CodexAppServerRequestError,
    CodexAppServerSubscriptionClosed,
    CodexAppServerTimeout,
)


NO_RESPONSE = object()


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process
        self.buffer = b""

    def write(self, data: bytes) -> None:
        if self.process.returncode is not None:
            raise BrokenPipeError("fake process exited")
        self.buffer += data
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            if raw:
                self.process.receive(raw)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class FakeProcess:
    _next_pid = 1000

    def __init__(self) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.responders: dict[str, Callable[[dict[str, Any]], Any]] = {
            "initialize": lambda _: {
                "userAgent": "fake",
                "platformFamily": "unix",
                "platformOs": "macos",
                "codexHome": "/tmp/codex",
            }
        }
        self._exited = asyncio.Event()

    def receive(self, raw: bytes) -> None:
        message = json.loads(raw)
        self.messages.append(message)
        method = message.get("method")
        if message.get("id") is None or method not in self.responders:
            return
        result = self.responders[method](message)
        if result is not NO_RESPONSE:
            self.feed({"id": message["id"], "result": result})

    def feed(self, message: dict[str, Any]) -> None:
        self.stdout.feed_data(
            (json.dumps(message, separators=(",", ":")) + "\n").encode()
        )

    def feed_stderr(self, line: str) -> None:
        self.stderr.feed_data((line + "\n").encode())

    def crash(self, returncode: int = 17) -> None:
        if self.returncode is not None:
            return
        self.returncode = returncode
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()

    def terminate(self) -> None:
        self.crash(-15)

    def kill(self) -> None:
        self.crash(-9)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


class FakeProcessFactory:
    def __init__(self, *processes: FakeProcess) -> None:
        self.processes = list(processes) or [FakeProcess()]
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    @property
    def process(self) -> FakeProcess:
        return self.processes[0]

    async def __call__(self, *args: Any, **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
        index = len(self.calls) - 1
        if index >= len(self.processes):
            self.processes.append(FakeProcess())
        return self.processes[index]


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class CodexAppServerClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(
        self,
        factory: FakeProcessFactory,
        **kwargs: Any,
    ) -> CodexAppServerClient:
        return CodexAppServerClient(
            "codex",
            cwd="/tmp",
            env_factory=lambda: {"PATH": "/usr/bin"},
            process_factory=factory,
            request_timeout=1,
            **kwargs,
        )

    async def test_initialize_once_and_reuse_one_process(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        process.responders["thread/start"] = lambda _: {"thread": {"id": "thr_1"}}
        client = self.make_client(factory)
        self.addAsyncCleanup(client.close)

        await asyncio.gather(*(client.start() for _ in range(8)))
        await client.start()
        self.assertEqual(await client.start_thread({"cwd": "/repo"}), "thr_1")

        self.assertEqual(len(factory.calls), 1)
        args, kwargs = factory.calls[0]
        self.assertEqual(args, ("codex", "app-server", "--listen", "stdio://"))
        self.assertEqual(kwargs["cwd"], "/tmp")
        self.assertEqual(kwargs["env"], {"PATH": "/usr/bin"})
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(client.generation, 1)
        self.assertEqual(
            client.initialize_result,
            {
                "userAgent": "fake",
                "platformFamily": "unix",
                "platformOs": "macos",
                "codexHome": "/tmp/codex",
            },
        )

        initialize = process.messages[0]
        self.assertEqual(initialize["method"], "initialize")
        self.assertEqual(
            initialize["params"]["clientInfo"],
            {"name": "agents_server", "title": "AgentsServer", "version": "1"},
        )
        self.assertEqual(
            initialize["params"]["capabilities"],
            {"experimentalApi": True},
        )
        self.assertEqual(process.messages[1], {"method": "initialized"})
        self.assertEqual(
            [message.get("method") for message in process.messages].count("initialize"),
            1,
        )

    async def test_thread_methods_track_loaded_threads_and_exact_payloads(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        process.responders.update(
            {
                "thread/start": lambda _: {"thread": {"id": "thr_new"}},
                "thread/resume": lambda message: {
                    "thread": {"id": message["params"]["threadId"]}
                },
                "thread/fork": lambda _: {"thread": {"id": "thr_fork"}},
                "thread/inject_items": lambda _: {},
                "thread/read": lambda message: {
                    "thread": {
                        "id": message["params"]["threadId"],
                        "turns": [{"id": "turn_recovered"}],
                    }
                },
                "thread/turns/list": lambda _: {
                    "data": [{"id": "turn_recovered"}],
                    "nextCursor": None,
                },
                "thread/unsubscribe": lambda _: {"status": "unsubscribed"},
            }
        )
        client = self.make_client(factory)
        self.addAsyncCleanup(client.close)

        self.assertEqual(
            await client.start_thread(
                {
                    "cwd": "/repo",
                    "developerInstructions": "short stable instructions",
                }
            ),
            "thr_new",
        )
        self.assertTrue(client.is_thread_loaded("thr_new"))

        self.assertEqual(
            await client.resume_thread(
                "thr_existing",
                {"cwd": "/repo", "developerInstructions": "resume instructions"},
            ),
            "thr_existing",
        )
        self.assertTrue(client.is_thread_loaded("thr_existing"))

        self.assertEqual(
            await client.fork_thread(
                "thr_existing",
                {"cwd": "/repo", "developerInstructions": "fork instructions"},
                last_turn_id="turn_4",
            ),
            "thr_fork",
        )
        self.assertTrue(client.is_thread_loaded("thr_fork"))

        injected = [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "migration marker"}],
            }
        ]
        await client.inject_items("thr_existing", injected)
        self.assertEqual(
            await client.read_thread("thr_existing", include_turns=True),
            {
                "id": "thr_existing",
                "turns": [{"id": "turn_recovered"}],
            },
        )
        self.assertEqual(
            await client.list_turns("thr_existing", limit=2),
            [{"id": "turn_recovered"}],
        )
        self.assertEqual(await client.unsubscribe_thread("thr_existing"), "unsubscribed")
        self.assertFalse(client.is_thread_loaded("thr_existing"))

        by_method = {
            message["method"]: message
            for message in process.messages
            if message.get("id") is not None and message.get("method") != "initialize"
        }
        self.assertEqual(
            by_method["thread/resume"]["params"],
            {
                "cwd": "/repo",
                "developerInstructions": "resume instructions",
                "threadId": "thr_existing",
                "excludeTurns": True,
            },
        )
        self.assertEqual(
            by_method["thread/fork"]["params"],
            {
                "cwd": "/repo",
                "developerInstructions": "fork instructions",
                "threadId": "thr_existing",
                "lastTurnId": "turn_4",
                "excludeTurns": True,
            },
        )
        self.assertEqual(
            by_method["thread/inject_items"]["params"],
            {"threadId": "thr_existing", "items": injected},
        )
        self.assertEqual(
            by_method["thread/read"]["params"],
            {"threadId": "thr_existing", "includeTurns": True},
        )
        self.assertEqual(
            by_method["thread/turns/list"]["params"],
            {
                "threadId": "thr_existing",
                "limit": 2,
                "itemsView": "full",
                "sortDirection": "desc",
            },
        )

        fork_events = client.subscribe_thread("thr_fork")
        closed = {
            "method": "thread/closed",
            "params": {"threadId": "thr_fork"},
        }
        process.feed(closed)
        self.assertEqual(await fork_events.next_notification(timeout=1), closed)
        with self.assertRaises(CodexAppServerSubscriptionClosed):
            await fork_events.next_notification(timeout=1)
        await wait_until(lambda: not client.is_thread_loaded("thr_fork"))

    async def test_multiplexed_turns_route_by_thread_and_turn(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process

        def start_turn(message: dict[str, Any]) -> dict[str, Any]:
            thread_id = message["params"]["threadId"]
            turn_id = f"turn_{thread_id[-1]}"
            if thread_id == "thread_a":
                # Exercise the real race where notifications can precede the
                # turn/start response.
                process.feed(
                    {
                        "method": "item/started",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "item": {"id": "early"},
                        },
                    }
                )
            return {"turn": {"id": turn_id, "status": "inProgress", "items": []}}

        process.responders.update(
            {
                "turn/start": start_turn,
                "turn/steer": lambda message: {
                    "turnId": message["params"]["expectedTurnId"]
                },
                "turn/interrupt": lambda _: {},
            }
        )
        client = self.make_client(factory)
        self.addAsyncCleanup(client.close)
        thread_a_events = client.subscribe_thread("thread_a")
        self.addCleanup(thread_a_events.close)

        turn_a, turn_b = await asyncio.gather(
            client.start_turn(
                "thread_a",
                [{"type": "text", "text": "A"}],
                overrides={"model": "gpt-5.4", "effort": "high"},
            ),
            client.start_turn(
                "thread_b",
                [{"type": "text", "text": "B"}],
            ),
        )
        self.assertEqual(turn_a.turn_id, "turn_a")
        self.assertEqual(turn_b.turn_id, "turn_b")
        self.assertIs(client.active_turn("thread_a"), turn_a)
        self.assertIs(client.active_turn("thread_b"), turn_b)

        early = await turn_a.next_notification(timeout=1)
        self.assertEqual(early["params"]["item"]["id"], "early")
        self.assertEqual(await thread_a_events.next_notification(timeout=1), early)

        wrong_turn = {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread_a",
                "turnId": "turn_other",
                "delta": "must not leak",
            },
        }
        process.feed(wrong_turn)
        self.assertEqual(await thread_a_events.next_notification(timeout=1), wrong_turn)
        with self.assertRaises(asyncio.TimeoutError):
            await turn_a.next_notification(timeout=0.01)

        turn_b_event = {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thread_b",
                "turnId": "turn_b",
                "delta": "B",
            },
        }
        process.feed(turn_b_event)
        self.assertEqual(await turn_b.next_notification(timeout=1), turn_b_event)

        self.assertEqual(
            await turn_a.steer(
                [{"type": "text", "text": "steer A"}],
                client_user_message_id="message-a",
            ),
            "turn_a",
        )
        await turn_a.interrupt()

        completed = {
            "method": "turn/completed",
            "params": {
                "threadId": "thread_a",
                "turn": {"id": "turn_a", "status": "completed"},
            },
        }
        process.feed(completed)
        self.assertEqual(await turn_a.next_notification(timeout=1), completed)
        await wait_until(lambda: client.active_turn("thread_a") is None)
        with self.assertRaises(CodexAppServerSubscriptionClosed):
            await turn_a.next_notification(timeout=1)

        await turn_b.close()
        self.assertIsNone(client.active_turn("thread_b"))

        turn_starts = [
            message
            for message in process.messages
            if message.get("method") == "turn/start"
        ]
        self.assertEqual(len(turn_starts), 2)
        turn_a_start = next(
            message
            for message in turn_starts
            if message["params"]["threadId"] == "thread_a"
        )
        self.assertEqual(
            turn_a_start["params"],
            {
                "model": "gpt-5.4",
                "effort": "high",
                "threadId": "thread_a",
                "input": [{"type": "text", "text": "A"}],
            },
        )

        steer = next(
            message
            for message in process.messages
            if message.get("method") == "turn/steer"
        )
        self.assertEqual(steer["params"]["expectedTurnId"], "turn_a")
        self.assertEqual(steer["params"]["clientUserMessageId"], "message-a")
        self.assertNotIn("additionalContext", steer["params"])

    async def test_resume_accepts_a_jsonl_response_larger_than_16_mib(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        process.stdout = asyncio.StreamReader(limit=32 * 1024 * 1024)
        large_value = "x" * (17 * 1024 * 1024)
        process.responders["thread/resume"] = lambda message: {
            "thread": {
                "id": message["params"]["threadId"],
                "turns": [{"id": "large-turn", "summary": large_value}],
            }
        }
        client = self.make_client(
            factory,
            process_stream_limit=32 * 1024 * 1024,
            json_parse_thread_threshold=1024,
        )
        self.addAsyncCleanup(client.close)

        self.assertEqual(await client.resume_thread("thr_large"), "thr_large")
        self.assertEqual(
            factory.calls[0][1]["limit"],
            32 * 1024 * 1024,
        )
        resume = next(
            message
            for message in process.messages
            if message.get("method") == "thread/resume"
        )
        self.assertTrue(resume["params"]["excludeTurns"])

    async def test_resume_uses_the_separate_lifecycle_timeout(self) -> None:
        factory = FakeProcessFactory()
        client = self.make_client(factory, lifecycle_timeout=123)
        request = AsyncMock(
            return_value={"thread": {"id": "thr_lifecycle"}}
        )
        with patch.object(client, "request", request):
            self.assertEqual(
                await client.resume_thread("thr_lifecycle"),
                "thr_lifecycle",
            )

        request.assert_awaited_once_with(
            "thread/resume",
            {"threadId": "thr_lifecycle", "excludeTurns": True},
            timeout=123,
        )

    async def test_notification_backlog_is_bounded_and_fails_subscription(
        self,
    ) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        client = self.make_client(factory, notification_queue_limit=2)
        self.addAsyncCleanup(client.close)
        await client.start()
        subscription = client.subscribe_thread("thr_backlog")

        for index in range(3):
            process.feed(
                {
                    "method": "item/agentMessage/delta",
                    "params": {
                        "threadId": "thr_backlog",
                        "turnId": "turn_backlog",
                        "itemId": "item_backlog",
                        "delta": str(index),
                    },
                }
            )

        with self.assertRaises(CodexAppServerDisconnected) as raised:
            await subscription.next_notification(timeout=1)
        self.assertIn("backlog", str(raised.exception))
        self.assertTrue(subscription._closed)

    async def test_concurrent_requests_match_out_of_order_responses(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        client = self.make_client(factory)
        self.addAsyncCleanup(client.close)
        await client.start()

        first = asyncio.create_task(client.request("test/first", {"value": 1}))
        second = asyncio.create_task(client.request("test/second", {"value": 2}))
        await wait_until(lambda: len(process.messages) >= 4)
        first_message, second_message = process.messages[-2:]
        self.assertNotEqual(first_message["id"], second_message["id"])

        process.feed({"id": second_message["id"], "result": {"order": 2}})
        process.feed({"id": first_message["id"], "result": {"order": 1}})
        self.assertEqual(await first, {"order": 1})
        self.assertEqual(await second, {"order": 2})

        error_task = asyncio.create_task(client.request("test/error", {}))
        await wait_until(lambda: process.messages[-1].get("method") == "test/error")
        process.feed(
            {
                "id": process.messages[-1]["id"],
                "error": {"code": -32001, "message": "Server overloaded; retry later."},
            }
        )
        with self.assertRaises(CodexAppServerRequestError) as raised:
            await error_task
        self.assertEqual(raised.exception.code, -32001)
        self.assertTrue(raised.exception.request_sent)
        self.assertTrue(raised.exception.safe_to_retry)

    async def test_timeout_is_ambiguous_and_not_safe_to_replay(self) -> None:
        factory = FakeProcessFactory()
        client = self.make_client(factory)
        client.request_timeout = 0.01
        self.addAsyncCleanup(client.close)
        await client.start()

        with self.assertRaises(CodexAppServerTimeout) as raised:
            await client.request("turn/start", {"threadId": "thr", "input": []})
        self.assertTrue(raised.exception.request_sent)
        self.assertFalse(raised.exception.safe_to_retry)

    async def test_ambiguous_turn_start_keeps_a_routed_provisional_handle(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        client = self.make_client(factory)
        client.request_timeout = 0.01
        self.addAsyncCleanup(client.close)

        with self.assertRaises(CodexAppServerTimeout) as raised:
            await client.start_turn(
                "thr_ambiguous",
                [{"type": "text", "text": "do not replay me"}],
            )

        pending_turn = raised.exception.pending_turn
        self.assertIsNotNone(pending_turn)
        assert pending_turn is not None
        self.assertIs(client.active_turn("thr_ambiguous"), pending_turn)
        self.assertEqual(pending_turn.turn_id, "")

        accepted_event = {
            "method": "item/started",
            "params": {
                "threadId": "thr_ambiguous",
                "turnId": "turn_late",
                "item": {"id": "proof_of_acceptance"},
            },
        }
        process.feed(accepted_event)
        self.assertEqual(
            await pending_turn.next_notification(timeout=1),
            accepted_event,
        )
        self.assertEqual(pending_turn.turn_id, "turn_late")
        await pending_turn.close()

    async def test_default_server_request_handler_declines_without_wedging(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        client = self.make_client(factory)
        self.addAsyncCleanup(client.close)
        await client.start()

        requests = [
            ("command", "item/commandExecution/requestApproval", {}, {"decision": "decline"}),
            ("file", "item/fileChange/requestApproval", {}, {"decision": "decline"}),
            ("legacy_exec", "execCommandApproval", {}, {"decision": "denied"}),
            ("legacy_patch", "applyPatchApproval", {}, {"decision": "denied"}),
            (
                "input",
                "item/tool/requestUserInput",
                {"questions": [{"id": "choice"}]},
                {"answers": {}},
            ),
            ("mcp", "mcpServer/elicitation/request", {}, {"action": "decline"}),
            (
                "permissions",
                "item/permissions/requestApproval",
                {},
                {"permissions": {}, "scope": "turn", "strictAutoReview": False},
            ),
        ]
        for request_id, method, params, _expected in requests:
            process.feed({"id": request_id, "method": method, "params": params})
        process.feed({"id": "unknown", "method": "item/tool/call", "params": {}})

        await wait_until(
            lambda: len(
                [
                    message
                    for message in process.messages
                    if message.get("id") in {item[0] for item in requests} | {"unknown"}
                ]
            )
            == len(requests) + 1
        )
        responses = {
            message["id"]: message
            for message in process.messages
            if message.get("id") in {item[0] for item in requests} | {"unknown"}
        }
        for request_id, _method, _params, expected in requests:
            self.assertEqual(responses[request_id]["result"], expected)
        self.assertEqual(responses["unknown"]["error"]["code"], -32601)
        self.assertNotIn("result", responses["unknown"])

    async def test_server_request_is_cancelled_when_resolved_elsewhere(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        handler_started = asyncio.Event()
        handler_cancelled = asyncio.Event()

        async def handler(
            request_id: Any,
            method: str,
            params: dict[str, Any],
        ) -> dict[str, Any]:
            self.assertEqual(request_id, "approval_1")
            self.assertEqual(method, "item/commandExecution/requestApproval")
            self.assertEqual(params["threadId"], "thr_1")
            handler_started.set()
            try:
                await asyncio.Future()
            finally:
                handler_cancelled.set()
            return {"decision": "decline"}

        client = self.make_client(factory, server_request_handler=handler)
        self.addAsyncCleanup(client.close)
        await client.start()
        subscription = client.subscribe_thread("thr_1")
        self.addCleanup(subscription.close)

        process.feed(
            {
                "id": "approval_1",
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thr_1",
                    "turnId": "turn_1",
                    "itemId": "item_1",
                },
            }
        )
        await asyncio.wait_for(handler_started.wait(), timeout=1)

        resolved = {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thr_1", "requestId": "approval_1"},
        }
        process.feed(resolved)
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
        self.assertEqual(await subscription.next_notification(timeout=1), resolved)
        self.assertFalse(
            any(
                message.get("id") == "approval_1"
                and ("result" in message or "error" in message)
                for message in process.messages
            )
        )

    async def test_process_crash_fails_pending_requests_and_subscriptions_then_restarts(self) -> None:
        first_process = FakeProcess()
        second_process = FakeProcess()
        first_process.responders["thread/start"] = lambda _: {"thread": {"id": "thr_1"}}
        first_process.responders["turn/start"] = lambda _: {
            "turn": {"id": "turn_1", "status": "inProgress", "items": []}
        }
        second_process.responders["thread/resume"] = lambda message: {
            "thread": {"id": message["params"]["threadId"]}
        }
        factory = FakeProcessFactory(first_process, second_process)
        client = self.make_client(factory)
        self.addAsyncCleanup(client.close)

        self.assertEqual(await client.start_thread({}), "thr_1")
        turn = await client.start_turn("thr_1", [{"type": "text", "text": "go"}])
        thread_events = client.subscribe_thread("thr_1")

        pending = asyncio.create_task(client.request("test/hang", {}))
        await wait_until(
            lambda: first_process.messages[-1].get("method") == "test/hang"
        )
        first_process.crash(23)

        with self.assertRaises(CodexAppServerDisconnected) as request_failure:
            await pending
        self.assertTrue(request_failure.exception.request_sent)
        self.assertFalse(request_failure.exception.safe_to_retry)
        with self.assertRaises(CodexAppServerDisconnected):
            await turn.next_notification(timeout=1)
        with self.assertRaises(CodexAppServerDisconnected):
            await thread_events.next_notification(timeout=1)
        self.assertFalse(client.is_thread_loaded("thr_1"))
        self.assertIsNone(client.active_turn("thr_1"))

        self.assertEqual(await client.resume_thread("thr_1"), "thr_1")
        self.assertEqual(len(factory.calls), 2)
        self.assertEqual(client.generation, 2)
        self.assertTrue(client.is_thread_loaded("thr_1"))

    async def test_manager_is_a_policy_agnostic_single_client_facade(self) -> None:
        factory = FakeProcessFactory()
        factory.process.responders["thread/start"] = lambda _: {
            "thread": {"id": "thr_manager"}
        }
        manager = CodexAppServerManager(
            "codex",
            cwd="/tmp",
            env_factory=lambda: {"PATH": "/usr/bin"},
            process_factory=factory,
            request_timeout=1,
        )
        self.addAsyncCleanup(manager.close)

        self.assertFalse(manager.ready)
        self.assertEqual(await manager.start_thread({}), "thr_manager")
        self.assertTrue(manager.ready)
        self.assertTrue(manager.is_thread_loaded("thr_manager"))
        self.assertEqual(len(factory.calls), 1)


if __name__ == "__main__":
    unittest.main()
