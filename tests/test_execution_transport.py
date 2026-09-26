import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
import struct
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from execution_transport import (
    ExecutionGateway,
    ExecutionTransportError,
    ExecutionTransportServer,
    MAX_FRAME_BYTES,
    _read_frame,
    _scope,
    _write_frame,
    ensure_execution_secret,
)


def http_scope(**changes):
    return {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "https", "path": "/api/example",
        "raw_path": b"/api/example", "query_string": b"a=%2F&a=%ff",
        "root_path": "", "headers": [(b"host", b"example.test")],
        "client": ("203.0.113.77", 49210), "server": ("10.0.0.5", 7850),
        **changes,
    }


class ExecutionTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Stay on the development volume, and keep paths below Darwin's Unix
        # socket length limit. Resolve ancestors so macOS /var links are absent.
        self.temporary = tempfile.TemporaryDirectory(prefix="ipc-", dir=Path(__file__).resolve().parents[1])
        self.root = Path(self.temporary.name)
        self.secret_path = self.root / "token"
        self.socket_path = self.root / "s"
        self.secret = ensure_execution_secret(self.secret_path)
        self.servers = []

    async def asyncTearDown(self):
        for server in self.servers:
            await server.close(timeout=0.1)
        self.temporary.cleanup()

    async def start(self, app):
        server = ExecutionTransportServer(app, socket_path=self.socket_path, secret_path=self.secret_path)
        await server.start()
        self.servers.append(server)
        gateway = ExecutionGateway(socket_path=self.socket_path, secret_path=self.secret_path)
        return server, gateway

    async def request(self, gateway, *, scope=None, events=None):
        incoming = asyncio.Queue()
        for event in events or [{"type": "http.request", "body": b"", "more_body": False}]:
            await incoming.put(event)
        sent = []

        async def send(message):
            sent.append(message)

        await asyncio.wait_for(gateway(scope or http_scope(), incoming.get, send), 3)
        return sent

    async def raw_open(self, scope=None, secret=None):
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        await _write_frame(writer, {"kind": "open", "secret": self.secret if secret is None else secret,
                                    "scope": _scope(scope or http_scope(), encode=True)})
        return reader, writer

    async def test_exact_peer_headers_query_origin_and_no_injected_authority(self):
        captured = []

        async def app(scope, receive, send):
            captured.append(scope)
            self.assertEqual((await receive())["body"], b"hello")
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")]})
            await send({"type": "http.response.body", "body": b"denied"})

        _, gateway = await self.start(app)
        headers = [(b"host", b"public.example:8443"), (b"x-agentsdock-token", b"first"),
                   (b"x-agentsdock-token", b"second"), (b"authorization", b"Bearer third"),
                   (b"origin", b"https://evil.test"), (b"sec-fetch-site", b"cross-site"),
                   (b"x-forwarded-for", b"127.0.0.1"), (b"cookie", b"a=1"),
                   (b"x-opaque", b"\xff\x80")]
        scope = http_scope(headers=headers, path="/a/b", raw_path=b"/a%2Fb",
                           state={"authorized": True}, arbitrary_auth_marker=True,
                           extensions={"http.response.pathsend": {}, "trusted": {}})
        result = await self.request(gateway, scope=scope, events=[{"type": "http.request", "body": b"hello"}])
        self.assertEqual(captured[0]["headers"], headers)
        for name in ("client", "server", "path", "raw_path", "query_string", "scheme"):
            self.assertEqual(captured[0][name], scope[name])
        self.assertNotIn("state", captured[0])
        self.assertNotIn("arbitrary_auth_marker", captured[0])
        self.assertEqual(captured[0]["extensions"], {})
        self.assertEqual(result[0]["status"], 403)
        self.assertEqual(result[0]["headers"], [(b"set-cookie", b"a=1"), (b"set-cookie", b"b=2")])

    async def test_streams_before_completion_and_chunks_large_upload_without_replay(self):
        first = asyncio.Event()
        release = asyncio.Event()
        body_parts = []

        async def app(scope, receive, send):
            while True:
                event = await receive()
                body_parts.append(event["body"])
                if not event["more_body"]:
                    break
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"first", "more_body": True})
            await release.wait()
            await send({"type": "http.response.body", "body": b"x" * 190000})

        _, gateway = await self.start(app)
        queue = asyncio.Queue()
        payload = b"upload" * 42000
        await queue.put({"type": "http.request", "body": payload})
        responses = []

        async def send(message):
            responses.append(message)
            if message.get("body") == b"first":
                first.set()

        request = asyncio.create_task(gateway(http_scope(), queue.get, send))
        await asyncio.wait_for(first.wait(), 2)
        self.assertFalse(request.done())
        self.assertEqual(b"".join(body_parts), payload)
        self.assertGreater(len(body_parts), 1)
        release.set()
        await asyncio.wait_for(request, 2)
        self.assertEqual(b"".join(item.get("body", b"") for item in responses), b"first" + b"x" * 190000)
        self.assertFalse(responses[-1]["more_body"])

    async def test_early_response_without_receiving_body_is_never_truncated(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 403})
            await send({"type": "http.response.body", "body": b"response-before-request-body"})

        _, gateway = await self.start(app)
        results = await asyncio.gather(*(
            self.request(gateway, events=[{"type": "http.request", "body": b"x" * 250000}])
            for _ in range(24)
        ))
        for result in results:
            self.assertEqual(result[0]["status"], 403)
            self.assertEqual(b"".join(item.get("body", b"") for item in result), b"response-before-request-body")

    async def test_completed_http_response_disconnect_keeps_worker_cleanup_alive(self):
        finish = asyncio.Event()
        committed = asyncio.Event()
        cancelled = []
        calls = []

        async def app(scope, receive, send):
            calls.append(True)
            await receive()
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"complete"})
            try:
                await finish.wait()
                committed.set()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        server, gateway = await self.start(app)
        incoming = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": b""})
        responses = []

        async def send(message):
            responses.append(message)
            if message["type"] == "http.response.body":
                # Uvicorn's receive returns disconnect once send completes the
                # response, even while ASGI background work is still running.
                incoming.put_nowait({"type": "http.disconnect"})
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(gateway(http_scope(), incoming.get, send), 2)
            self.assertEqual(responses[-1]["body"], b"complete")
            self.assertEqual(server.active_connections, 1)
            self.assertFalse(committed.is_set())
            self.assertFalse(cancelled)
        finally:
            finish.set()
        await asyncio.wait_for(committed.wait(), 2)
        self.assertEqual(calls, [True])
        self.assertFalse(cancelled)

    async def test_declared_trailers_are_forwarded_before_response_completes(self):
        body_sent = asyncio.Event()
        release_trailers = asyncio.Event()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "trailers": True})
            await send({"type": "http.response.body", "body": b"complete"})
            body_sent.set()
            await release_trailers.wait()
            await send({"type": "http.response.trailers", "headers": [(b"x-checksum", b"first")],
                        "more_trailers": True})
            await send({"type": "http.response.trailers", "headers": [(b"x-checksum", b"last")]})

        _, gateway = await self.start(app)
        request = asyncio.create_task(self.request(gateway))
        await asyncio.wait_for(body_sent.wait(), 2)
        self.assertFalse(request.done())
        release_trailers.set()
        responses = await asyncio.wait_for(request, 2)
        self.assertEqual([message["type"] for message in responses],
                         ["http.response.start", "http.response.body", "http.response.trailers", "http.response.trailers"])
        self.assertEqual(responses[-1]["headers"], [(b"x-checksum", b"last")])

    async def test_app_completion_does_not_hide_an_incomplete_http_stream(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            await send({"type": "http.response.body", "body": b"partial", "more_body": True})

        _, gateway = await self.start(app)
        with self.assertRaisesRegex(ExecutionTransportError, "Execution response interrupted"):
            await self.request(gateway)

    async def test_worker_drains_early_response_until_ingress_closes(self):
        async def app(scope, receive, send):
            self.assertTrue((await receive())["more_body"])
            await send({"type": "http.response.start", "status": 403})
            await send({"type": "http.response.body", "body": b"early"})

        await self.start(app)
        reader, writer = await self.raw_open()
        try:
            self.assertEqual((await _read_frame(reader))["kind"], "ready")
            await _write_frame(writer, {"kind": "event", "event": {
                "type": "http.request", "body": "", "more_body": True}})
            self.assertEqual((await _read_frame(reader))["event"]["type"], "http.response.start")
            self.assertEqual((await _read_frame(reader))["event"]["type"], "http.response.body")
            self.assertEqual((await _read_frame(reader))["kind"], "complete")
            # The ingress may still be writing body bytes. Closing immediately
            # here can reset the socket and discard its buffered response.
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(reader.read(1), 0.03)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_response_backpressure_does_not_buffer_entire_application_stream(self):
        first = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            for index in range(512):
                await send({"type": "http.response.body", "body": b"a" * 65536,
                            "more_body": index < 511})
            finished.set()

        _, gateway = await self.start(app)
        queue = asyncio.Queue()
        queue.put_nowait({"type": "http.request", "body": b""})
        received_bytes = 0

        async def send(message):
            nonlocal received_bytes
            if message.get("body"):
                first.set()
                await release.wait()
                received_bytes += len(message["body"])

        request = asyncio.create_task(gateway(http_scope(), queue.get, send))
        try:
            await asyncio.wait_for(first.wait(), 2)
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(finished.wait(), 0.05)
        finally:
            release.set()
        await asyncio.wait_for(request, 5)
        self.assertEqual(received_bytes, 512 * 65536)

    async def test_gateway_loss_does_not_cancel_or_retry_accepted_mutation(self):
        accepted = asyncio.Event()
        finish = asyncio.Event()
        committed = asyncio.Event()
        cancelled = []
        calls = []
        disconnects = []

        async def app(scope, receive, send):
            calls.append(scope["path"])
            self.assertEqual((await receive())["body"], b"mutate")
            accepted.set()
            try:
                disconnects.append(await receive())
                await finish.wait()
                committed.set()
                await send({"type": "http.response.start", "status": 200})
                await send({"type": "http.response.body", "body": b"committed"})
            except asyncio.CancelledError:
                cancelled.append(True)
                raise

        server, gateway = await self.start(app)
        queue = asyncio.Queue()
        await queue.put({"type": "http.request", "body": b"mutate"})

        async def send(message):
            pass

        request = asyncio.create_task(gateway(http_scope(), queue.get, send))
        await asyncio.wait_for(accepted.wait(), 2)
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        for _ in range(100):
            if disconnects:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(disconnects, [{"type": "http.disconnect"}])
        self.assertFalse(cancelled)
        self.assertEqual(server.active_connections, 1)
        finish.set()
        await asyncio.wait_for(committed.wait(), 2)
        self.assertEqual(calls, ["/api/example"])
        self.assertFalse(cancelled)

    async def test_websocket_preserves_binary_text_protocol_and_close(self):
        expected_headers = [(b"sec-websocket-protocol", b"agentsdock.events,agentsdock-token.fake")]

        async def app(scope, receive, send):
            self.assertEqual(scope["client"], ("203.0.113.77", 49210))
            self.assertEqual(scope["headers"], expected_headers)
            self.assertEqual(await receive(), {"type": "websocket.connect"})
            await send({"type": "websocket.accept", "subprotocol": "agentsdock.events"})
            self.assertEqual(await receive(), {"type": "websocket.receive", "bytes": b"\x00\xff"})
            self.assertEqual(await receive(), {"type": "websocket.receive", "text": '{"resize":80}'})
            await send({"type": "websocket.send", "bytes": b"\xff\x00"})
            await send({"type": "websocket.send", "text": "event"})
            await send({"type": "websocket.close", "code": 4409, "reason": "Archived"})

        _, gateway = await self.start(app)
        scope = http_scope(type="websocket", scheme="wss", headers=expected_headers,
                           subprotocols=["agentsdock.events", "agentsdock-token.fake"])
        scope.pop("method")
        sent = await self.request(gateway, scope=scope, events=[
            {"type": "websocket.connect"}, {"type": "websocket.receive", "bytes": b"\x00\xff"},
            {"type": "websocket.receive", "text": '{"resize":80}'}])
        self.assertEqual(sent[0]["subprotocol"], "agentsdock.events")
        self.assertEqual(sent[1], {"type": "websocket.send", "bytes": b"\xff\x00"})
        self.assertEqual(sent[2], {"type": "websocket.send", "text": "event"})
        self.assertEqual(sent[3], {"type": "websocket.close", "code": 4409, "reason": "Archived"})

    async def test_wrong_secret_never_enters_app(self):
        calls = []

        async def app(*args):
            calls.append(True)

        await self.start(app)
        reader, writer = await self.raw_open(secret="0" * 64)
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        await writer.wait_closed()
        self.assertEqual(calls, [])

    async def test_wrong_version_oversized_and_duplicate_frames_are_rejected(self):
        calls = []

        async def app(*args):
            calls.append(True)

        await self.start(app)
        frames = [struct.pack("!I", MAX_FRAME_BYTES + 1)]
        for payload in (b'{"v":2}', b'{"v":1,"v":1}', b'{"v":true}', b'{"v":NaN}'):
            frames.append(struct.pack("!I", len(payload)) + payload)
        for frame in frames:
            reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
            writer.write(frame)
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
            writer.close()
            await writer.wait_closed()
        self.assertEqual(calls, [])

    async def test_wire_scope_cannot_inject_process_authentication_state(self):
        calls = []

        async def app(*args):
            calls.append(True)

        await self.start(app)
        reader, writer = await asyncio.open_unix_connection(str(self.socket_path))
        scope = _scope(http_scope(), encode=True)
        scope["state"] = {"codex_provider_mcp_authenticated": True}
        await _write_frame(writer, {"kind": "open", "secret": self.secret, "scope": scope})
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        writer.close()
        await writer.wait_closed()
        self.assertEqual(calls, [])

    async def test_lifespan_is_gateway_local_never_forwarded(self):
        calls = []

        async def app(*args):
            calls.append(True)

        _, gateway = await self.start(app)
        queue = asyncio.Queue()
        queue.put_nowait({"type": "lifespan.startup"})
        queue.put_nowait({"type": "lifespan.shutdown"})
        sent = []

        async def send(message):
            sent.append(message)

        await gateway({"type": "lifespan"}, queue.get, send)
        self.assertEqual(sent, [{"type": "lifespan.startup.complete"}, {"type": "lifespan.shutdown.complete"}])
        self.assertEqual(calls, [])

    async def test_explicit_worker_shutdown_cancels_owned_request_after_bounded_drain(self):
        accepted = asyncio.Event()
        cancelled = asyncio.Event()

        async def app(scope, receive, send):
            accepted.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        server, _ = await self.start(app)
        reader, writer = await self.raw_open()
        self.assertEqual((await _read_frame(reader))["kind"], "ready")
        await accepted.wait()
        await asyncio.wait_for(server.close(timeout=0.01), 1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(self.socket_path.exists())
        writer.close()
        await writer.wait_closed()

    async def test_worker_shutdown_is_bounded_when_ingress_stops_reading(self):
        started = asyncio.Event()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200})
            started.set()
            for _ in range(512):
                await send({"type": "http.response.body", "body": b"a" * 65536, "more_body": True})

        server, _ = await self.start(app)
        reader, writer = await self.raw_open()
        self.assertEqual((await _read_frame(reader))["kind"], "ready")
        await started.wait()
        await asyncio.wait_for(server.close(timeout=0.01), 2)
        self.assertFalse(self.socket_path.exists())
        writer.close()
        await writer.wait_closed()

    async def test_worker_shutdown_skips_transport_closed_during_request_drain(self):
        accepted = asyncio.Event()
        cancelled = asyncio.Event()

        async def app(scope, receive, send):
            accepted.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        server, _ = await self.start(app)
        reader, writer = await self.raw_open()
        self.assertEqual((await _read_frame(reader))["kind"], "ready")
        await accepted.wait()
        owned_writer = next(iter(server._writers))
        # The request remains owned after its real Unix connection closes.
        # CPython releases the transport's event loop at this boundary.
        owned_writer.close()
        await owned_writer.wait_closed()
        self.assertEqual(owned_writer.get_extra_info("socket").fileno(), -1)
        self.assertEqual(server.active_connections, 1)
        try:
            with mock.patch.object(owned_writer.transport, "write", wraps=owned_writer.transport.write) as write:
                with self.assertRaises(ConnectionError):
                    await _write_frame(owned_writer, {"kind": "error"})
                write.assert_not_called()
            with mock.patch.object(owned_writer.transport, "abort", wraps=owned_writer.transport.abort) as abort:
                await asyncio.wait_for(server.close(timeout=0.01), 1)
                abort.assert_not_called()
            self.assertTrue(cancelled.is_set())
            self.assertFalse(self.socket_path.exists())
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_secret_permissions_links_and_existing_socket_paths_fail_closed(self):
        self.assertEqual(ensure_execution_secret(self.secret_path), self.secret)
        self.assertEqual(self.secret_path.stat().st_mode & 0o777, 0o600)
        self.secret_path.chmod(0o644)
        with self.assertRaises(ExecutionTransportError):
            ensure_execution_secret(self.secret_path)
        self.secret_path.chmod(0o600)
        link = self.root / "linked-token"
        link.symlink_to(self.secret_path)
        with self.assertRaises((ExecutionTransportError, OSError)):
            ensure_execution_secret(link)
        self.socket_path.write_text("owned ordinary file")
        server = ExecutionTransportServer(None, socket_path=self.socket_path, secret_path=self.secret_path)
        with self.assertRaises(ExecutionTransportError):
            await server.start()
        self.assertEqual(self.socket_path.read_text(), "owned ordinary file")

    def test_concurrent_initializers_never_observe_a_partially_written_secret(self):
        path = self.root / "new-token"
        writing = threading.Event()
        release = threading.Event()
        second_started = threading.Event()

        def paused_token(_size):
            writing.set()
            if not release.wait(2):
                raise AssertionError("initializer was not released")
            return "a" * 64

        def second_initializer():
            second_started.set()
            return ensure_execution_secret(path)

        with ThreadPoolExecutor(max_workers=2) as executor, mock.patch(
                "execution_transport.secrets.token_hex", side_effect=paused_token):
            first = executor.submit(ensure_execution_secret, path)
            try:
                self.assertTrue(writing.wait(1))
                self.assertEqual(path.read_bytes(), b"")
                second = executor.submit(second_initializer)
                self.assertTrue(second_started.wait(1))
                with self.assertRaises(TimeoutError):
                    second.result(timeout=0.05)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=1), "a" * 64)
            self.assertEqual(second.result(timeout=1), first.result())
        self.assertEqual(path.stat().st_nlink, 1)
        with ThreadPoolExecutor(max_workers=8) as executor:
            self.assertEqual(set(executor.map(ensure_execution_secret, [path] * 24)), {"a" * 64})

    def test_initializer_lock_and_malformed_secret_fail_without_replacement(self):
        path = self.root / "bad-token"
        path.write_bytes(b"unfinished")
        path.chmod(0o600)
        for _ in range(2):
            with self.assertRaisesRegex(ExecutionTransportError, "Invalid execution secret file"):
                ensure_execution_secret(path)
            self.assertEqual(path.read_bytes(), b"unfinished")
        lock_path = self.root / ".bad-token.lock"
        lock_path.chmod(0o644)
        with self.assertRaisesRegex(ExecutionTransportError, "initializer lock"):
            ensure_execution_secret(path)
        lock_path.unlink()
        lock_path.symlink_to(self.secret_path)
        with self.assertRaises(OSError):
            ensure_execution_secret(path)
        self.assertEqual(self.secret_path.read_text(), self.secret)

    async def test_cleanup_does_not_remove_replacement_socket_path(self):
        async def app(*args):
            pass

        server, _ = await self.start(app)
        self.socket_path.unlink()
        self.socket_path.write_text("replacement")
        await server.close()
        self.assertEqual(self.socket_path.read_text(), "replacement")

    async def test_gateway_unavailable_fails_once_without_invoking_receive(self):
        gateway = ExecutionGateway(socket_path=self.socket_path, secret_path=self.secret_path)
        sent = []

        async def receive():
            self.fail("No body should be consumed without an execution connection")

        async def send(message):
            sent.append(message)

        await gateway(http_scope(), receive, send)
        self.assertEqual(sent[0]["status"], 503)
        self.assertEqual(sent[-1]["body"], b"Execution service unavailable")


if __name__ == "__main__":
    unittest.main()
