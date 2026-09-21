"""Owned subprocess fixture; never imports agent_server or invokes a real provider.

The provider protocol deliberately models only an active event stream and one
approval side effect. Passing this fixture is not Codex/Claude acceptance.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import selectors
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TOKEN = "owned-execution-fixture-token"


def append_json(path: Path, value: dict) -> None:
    with path.open("a") as stream:
        stream.write(json.dumps(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def provider(root: Path) -> None:
    """A real child that continues to work while gateways disappear."""
    owner = os.getppid()
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    counter = 0
    while os.getppid() == owner:
        for _key, _mask in selector.select(timeout=0.04):
            line = sys.stdin.readline()
            if not line:
                return
            command = json.loads(line)
            if command["type"] == "approve":
                append_json(root / "tool-effects.jsonl", command)
                print(json.dumps({"type": "approval_result", "approval_id": command["approval_id"]}), flush=True)
        counter += 1
        event = {"type": "progress", "counter": counter, "provider_pid": os.getpid()}
        append_json(root / "provider-events.jsonl", event)
        print(json.dumps(event), flush=True)


class WorkerFixture:
    def __init__(self, root: Path):
        self.root = root
        self.child = None
        self.reader = None
        self.events = []
        self.changed = asyncio.Condition()
        self.approved = False
        self.approval_requests = 0
        self.approval_effects = 0
        self.mutations = {}
        self.mutation_requests = 0
        self.completed_mutations = 0
        self.release_mutations = asyncio.Event()
        self.active_streams = 0

    async def start(self):
        self.child = await asyncio.create_subprocess_exec(
            sys.executable, "-B", str(Path(__file__).resolve()), "provider", "--root", str(self.root),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await self.emit({"type": "approval_requested", "approval_id": "approval-1", "run_id": "fixture-run-1"})
        self.reader = asyncio.create_task(self.read_provider())

    async def emit(self, event):
        async with self.changed:
            self.events.append({**event, "seq": len(self.events) + 1, "run_id": "fixture-run-1"})
            self.changed.notify_all()

    async def read_provider(self):
        while line := await self.child.stdout.readline():
            await self.emit(json.loads(line))

    async def close(self):
        if self.child is not None and self.child.returncode is None:
            self.child.terminate()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.child.wait(), 3)
            if self.child.returncode is None:
                self.child.kill()
                await self.child.wait()
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader

    def snapshot(self):
        return {
            "worker_pid": os.getpid(), "provider_pid": self.child.pid,
            "provider_returncode": self.child.returncode, "seq": len(self.events),
            "run_id": "fixture-run-1", "pending_approval": None if self.approved else "approval-1",
            "approval_requests": self.approval_requests, "approval_effects": self.approval_effects,
            "mutation_requests": self.mutation_requests, "mutation_effects": len(self.mutations),
            "completed_mutations": self.completed_mutations, "active_streams": self.active_streams,
        }

    @staticmethod
    def guard(scope):
        headers = [(name.lower(), value) for name, value in scope["headers"]]
        values = [value for name, value in headers if name == b"x-agentsdock-token"]
        if values != [TOKEN.encode()] or any(name in {b"authorization", b"cookie"} for name, _ in headers):
            return 401
        if any(name in {b"origin", b"sec-fetch-site"} for name, _ in headers):
            return 403
        return 200

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await self.start()
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await self.close()
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        status = self.guard(scope)
        if scope["type"] == "websocket":
            if status != 200:
                await send({"type": "websocket.close", "code": 4401})
                return
            return await self.websocket(scope, receive, send)
        if scope["type"] != "http":
            raise RuntimeError("Worker lifespan is owned by the fixture process.")
        if status != 200:
            return await self.response(send, status, {"guard": status})
        path = scope["path"]
        if path in {"/state", "/api/health"}:
            return await self.response(send, 200, {**self.snapshot(), "ok": True,
                "server_version": SERVER_VERSION, "server_instance_id": "fixture-worker-1"})
        if path == "/headers":
            return await self.response(send, 200, {
                "headers": [[name.decode("latin1"), value.decode("latin1")] for name, value in scope["headers"]],
                "client": scope.get("client"), "scheme": scope["scheme"],
                "query_string": scope["query_string"].decode("ascii"),
            })
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if not message.get("more_body"):
                break
        request = json.loads(body or b"{}")
        if request.get("expected_server_instance_id", "fixture-worker-1") != "fixture-worker-1":
            return await self.response(send, 409, {"error": "stale instance"})
        if path == "/approval":
            self.approval_requests += 1
            if request.get("approval_id") != "approval-1" or request.get("choice") != "allow":
                return await self.response(send, 409, {"error": "different approval"})
            if not self.approved:
                self.approved = True
                self.approval_effects += 1
                self.child.stdin.write(json.dumps({"type": "approve", "approval_id": "approval-1"}).encode() + b"\n")
                await self.child.stdin.drain()
            return await self.response(send, 200, {"approval_id": "approval-1", "accepted": True})
        if path == "/mutation/release":
            self.release_mutations.set()
            return await self.response(send, 200, {"released": True})
        if path == "/mutation":
            self.mutation_requests += 1
            request_id = request["request_id"]
            value = request["value"]
            if request_id in self.mutations:
                if self.mutations[request_id] != value:
                    return await self.response(send, 409, {"error": "changed operation"})
                return await self.response(send, 200, {"request_id": request_id, "value": value})
            self.mutations[request_id] = value
            append_json(self.root / "mutation-effects.jsonl", {"request_id": request_id, "value": value})
            if request.get("withhold_response"):
                await self.release_mutations.wait()
            self.completed_mutations += 1
            return await self.response(send, 200, {"request_id": request_id, "value": value})
        return await self.response(send, 404, {"error": "not found"})

    @staticmethod
    async def response(send, status, value):
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"x-worker-duplicate", b"one"), (b"x-worker-duplicate", b"two")]})
        await send({"type": "http.response.body", "body": json.dumps(value).encode()})

    async def websocket(self, scope, receive, send):
        from urllib.parse import parse_qs
        after = int(parse_qs(scope["query_string"].decode()).get("after", ["0"])[0])
        assert (await receive())["type"] == "websocket.connect"
        subprotocol = "agentsdock-test" if "agentsdock-test" in scope.get("subprotocols", []) else None
        await send({"type": "websocket.accept", "subprotocol": subprotocol})
        await send({"type": "websocket.send", "text": json.dumps({
            "type": "connection", "headers": [[n.decode("latin1"), v.decode("latin1")] for n, v in scope["headers"]],
            "client": scope.get("client"), "scheme": scope["scheme"],
        })})
        self.active_streams += 1
        incoming = asyncio.create_task(receive())
        try:
            while True:
                for event in self.events[after:]:
                    await send({"type": "websocket.send", "text": json.dumps(event)})
                    after = event["seq"]
                async def changed():
                    async with self.changed:
                        await self.changed.wait_for(lambda: len(self.events) > after)
                change = asyncio.create_task(changed())
                try:
                    done, _ = await asyncio.wait((incoming, change), return_when=asyncio.FIRST_COMPLETED)
                    if incoming in done:
                        if incoming.result()["type"] == "websocket.disconnect":
                            return
                        incoming = asyncio.create_task(receive())
                finally:
                    change.cancel()
                    with suppress(asyncio.CancelledError):
                        await change
        finally:
            self.active_streams -= 1
            incoming.cancel()
            with suppress(asyncio.CancelledError):
                await incoming


SERVER_VERSION = "fixture-engine-v1"
if __name__ != "__main__":
    app = WorkerFixture(Path(os.environ["AGENTSDOCK_EXECUTION_FIXTURE_ROOT"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("provider",))
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    provider(args.root)
