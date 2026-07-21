import asyncio
import unittest
from collections.abc import Callable
from typing import Any

from codex_app_server import (
    CodexAppServerClient,
    CodexAppServerRequestError,
)


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process
        self.buffer = b""

    def write(self, data: bytes) -> None:
        self.buffer += data
        while b"\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n", 1)
            if raw:
                self.process.receive(raw)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.returncode: int | None = None
        self.messages: list[dict[str, Any]] = []
        self.responders: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "initialize": lambda _: {
                "userAgent": "fake",
                "platformFamily": "unix",
                "platformOs": "macos",
                "codexHome": "/tmp/codex",
            }
        }

    def receive(self, raw: bytes) -> None:
        import json

        message = json.loads(raw)
        self.messages.append(message)
        method = message.get("method")
        if message.get("id") is not None and method in self.responders:
            result = self.responders[method](message)
            self.feed({"id": message["id"], "result": result})

    def feed(self, message: dict[str, Any]) -> None:
        import json

        self.stdout.feed_data((json.dumps(message, separators=(",", ":")) + "\n").encode())

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    def kill(self) -> None:
        self.terminate()

    async def wait(self) -> int:
        while self.returncode is None:
            await asyncio.sleep(0)
        return self.returncode


class FakeProcessFactory:
    def __init__(self) -> None:
        self.process = FakeProcess()
        self.args: tuple[Any, ...] | None = None
        self.kwargs: dict[str, Any] | None = None

    async def __call__(self, *args: Any, **kwargs: Any) -> FakeProcess:
        self.args = args
        self.kwargs = kwargs
        return self.process


async def wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), timeout=timeout)


class CodexAppServerClientTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, factory: FakeProcessFactory, **kwargs: Any) -> CodexAppServerClient:
        return CodexAppServerClient(
            "codex",
            cwd="/tmp",
            env_factory=lambda: {"PATH": "/usr/bin"},
            process_factory=factory,
            request_timeout=1,
            **kwargs,
        )

    async def test_full_thread_turn_lifecycle_and_notification_routing(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        process.responders.update(
            {
                "thread/start": lambda _: {"thread": {"id": "thr_new"}},
                "thread/resume": lambda message: {"thread": {"id": message["params"]["threadId"]}},
                "turn/start": lambda _: {"turn": {"id": "turn_1", "status": "inProgress", "items": []}},
                "turn/steer": lambda _: {"turnId": "turn_1"},
                "turn/interrupt": lambda _: {},
            }
        )
        callback_seen = asyncio.Event()

        async def callback(notification: dict[str, Any]) -> None:
            if notification["method"] == "item/agentMessage/delta":
                callback_seen.set()

        client = self.make_client(factory)
        client.add_notification_handler(callback)
        await client.start()

        self.assertEqual(factory.args, ("codex", "app-server", "--listen", "stdio://"))
        self.assertIs(client.process, process)
        self.assertTrue(client.ready)
        self.assertEqual(process.messages[0]["method"], "initialize")
        self.assertEqual(process.messages[1], {"method": "initialized", "params": {}})
        self.assertNotIn("jsonrpc", process.messages[0])

        self.assertEqual(
            await client.start_thread(
                {"cwd": "/repo", "sandbox": "danger-full-access", "approvalPolicy": "never", "ephemeral": False}
            ),
            "thr_new",
        )
        self.assertEqual(
            await client.resume_thread("thr_existing", {"cwd": "/repo", "approvalPolicy": "never"}),
            "thr_existing",
        )
        turn = await client.start_turn(
            "thr_new",
            [{"type": "text", "text": "hello"}],
            overrides={"model": "gpt-5.4", "effort": "high"},
        )
        self.assertEqual(turn.turn_id, "turn_1")
        self.assertIs(client.active_turn("thr_new"), turn)

        notification = {
            "method": "item/agentMessage/delta",
            "params": {
                "threadId": "thr_new",
                "turnId": "turn_1",
                "itemId": "item_1",
                "delta": "hello",
            },
        }
        process.feed(notification)
        self.assertEqual(await turn.next_notification(timeout=1), notification)
        self.assertEqual(await client.next_notification(timeout=1), notification)
        await asyncio.wait_for(callback_seen.wait(), timeout=1)

        self.assertEqual(await turn.steer([{"type": "text", "text": "more"}]), "turn_1")
        await turn.interrupt()
        await turn.close()
        self.assertIsNone(client.active_turn("thr_new"))

        methods = [message.get("method") for message in process.messages]
        self.assertIn("turn/steer", methods)
        self.assertIn("turn/interrupt", methods)
        await client.close()
        self.assertFalse(client.ready)

    async def test_concurrent_request_ids_match_out_of_order_responses(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        client = self.make_client(factory)
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
                "error": {"code": -32000, "message": "nope"},
            }
        )
        with self.assertRaisesRegex(CodexAppServerRequestError, r"test/error failed: nope"):
            await error_task
        await client.close()

    async def test_server_request_is_cancelled_when_resolved_elsewhere(self) -> None:
        factory = FakeProcessFactory()
        process = factory.process
        handler_started = asyncio.Event()
        handler_cancelled = asyncio.Event()

        async def handler(request_id: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
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
        await client.start()
        process.feed(
            {
                "id": "approval_1",
                "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "thr_1", "turnId": "turn_1", "itemId": "item_1"},
            }
        )
        await asyncio.wait_for(handler_started.wait(), timeout=1)
        resolved = {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thr_1", "requestId": "approval_1"},
        }
        process.feed(resolved)
        await asyncio.wait_for(handler_cancelled.wait(), timeout=1)
        self.assertEqual(await client.next_notification(timeout=1), resolved)
        self.assertFalse(any(message.get("id") == "approval_1" for message in process.messages))
        await client.close()


if __name__ == "__main__":
    unittest.main()
