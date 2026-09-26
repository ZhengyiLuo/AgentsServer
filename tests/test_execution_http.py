from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from types import SimpleNamespace

import execution_http as transport
import execution_install as files
from execution_manage import WorkerControl


# A real separate process owns the accepted socket; it never prints the fixture
# credential. The receipt proves whether *any* bytes reached an untrusted peer.
LISTENER = r'''
import json, socket, sys, time
response = sys.stdin.buffer.read()
listener = socket.socket()
listener.bind(("127.0.0.1", 0)); listener.listen(1)
print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
connection, _ = listener.accept()
connection.settimeout(10)
data = b""
try:
    while b"\r\n\r\n" not in data:
        block = connection.recv(65536)
        if not block: break
        data += block
    if b"\r\n\r\n" in data:
        headers, body = data.split(b"\r\n\r\n", 1)
        length = 0
        for line in headers.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(body) < length:
            block = connection.recv(65536)
            if not block: break
            body += block; data += block
        time.sleep(float(sys.argv[1]))
        connection.sendall(response)
finally:
    connection.close(); listener.close()
print(json.dumps({"received": len(data), "authorized": b"Authorization: Bearer fixture-token\r\n" in data,
                  "post": data.startswith(b"POST "), "body": body.decode() if data and b"\r\n\r\n" in data else None}), flush=True)
'''

GATEWAY = r'''
import asyncio, json, os, socket, sys
from pathlib import Path
import uvicorn
from execution_transport import ExecutionGateway, ExecutionTransportServer, ensure_execution_secret
async def main():
    root = Path(sys.argv[1])
    ensure_execution_secret(root / "t")
    async def app(scope, receive, send):
        assert dict(scope["headers"])[b"authorization"] == b"Bearer fixture-token"
        body = json.dumps({"ok": True, "pid": os.getpid()}).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
        asyncio.get_running_loop().call_later(.2, setattr, gateway, "should_exit", True)
    worker = ExecutionTransportServer(app, socket_path=root / "s", secret_path=root / "t")
    await worker.start()
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen(16)
    gateway = uvicorn.Server(uvicorn.Config(ExecutionGateway(socket_path=root / "s", secret_path=root / "t"),
                                           log_level="error", access_log=False))
    task = asyncio.create_task(gateway.serve(sockets=[sock]))
    try:
        while not gateway.started: await asyncio.sleep(.01)
        print(json.dumps({"port": sock.getsockname()[1]}), flush=True)
        await task
    finally:
        await worker.close(); sock.close()
asyncio.run(main())
'''


def response(value, status="200 OK", extra=""):
    body = json.dumps(value).encode()
    return (f"HTTP/1.1 {status}\r\nContent-Length: {len(body)}\r\n{extra}\r\n".encode() + body)


@unittest.skipUnless(sys.platform in {"darwin", "linux"}, "requires native socket ownership inspection")
class PinnedCredentialTests(unittest.TestCase):
    platform = "Darwin" if sys.platform == "darwin" else "Linux"

    @contextmanager
    def listener(self, wire=None, delay=0):
        payload = tempfile.TemporaryFile()
        payload.write(wire or response({"ok": True}))
        payload.seek(0)
        child = subprocess.Popen([sys.executable, "-B", "-u", "-c", LISTENER, str(delay)],
                                 stdin=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True)
        try:
            port = json.loads(child.stdout.readline())["port"]
            yield child, f"http://127.0.0.1:{port}/api/health"
        finally:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=5)
            payload.close()

    def request(self, child, url, **options):
        return transport.request_json(url, "fixture-token", expected_pid=child.pid,
            platform=self.platform, verify_owner=lambda: None, timeout=3, **options)

    def receipt(self, child):
        value = json.loads(child.stdout.readline())
        self.assertEqual(child.wait(timeout=5), 0)
        return value

    def test_real_owned_process_receives_one_authenticated_get(self):
        with self.listener() as (child, url):
            self.assertEqual(self.request(child, url), {"ok": True})
            proof = self.receipt(child)
            self.assertTrue(proof["authorized"])
            self.assertFalse(proof["post"])

    def test_real_owned_process_receives_one_post_without_retry(self):
        with self.listener() as (child, url):
            self.assertEqual(self.request(child, url, body={"action": "release"}), {"ok": True})
            proof = self.receipt(child)
            self.assertTrue(proof["authorized"])
            self.assertTrue(proof["post"])
            self.assertEqual(json.loads(proof["body"]), {"action": "release"})

    def test_foreign_listener_receives_zero_bytes_even_with_forged_health(self):
        with self.listener(response({"ok": True, "pid": os.getpid()})) as (child, url):
            with self.assertRaisesRegex(RuntimeError, "ownership could not be proven"):
                transport.request_json(url, "fixture-token", expected_pid=os.getpid(),
                    platform=self.platform, verify_owner=lambda: None, timeout=.3)
            self.assertEqual(self.receipt(child)["received"], 0)

    def test_native_role_change_after_connect_sends_no_bytes(self):
        with self.listener() as (child, url):
            def changed():
                raise RuntimeError("native role changed")
            with self.assertRaisesRegex(RuntimeError, "native role changed"):
                transport.request_json(url, "fixture-token", expected_pid=child.pid,
                    platform=self.platform, verify_owner=changed, timeout=3)
            self.assertEqual(self.receipt(child)["received"], 0)

    def test_header_wait_uses_remaining_request_deadline(self):
        with self.listener(delay=3) as (child, url):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                transport.request_json(url, "fixture-token", expected_pid=child.pid,
                    platform=self.platform, verify_owner=lambda: None, timeout=.5)
            self.assertLess(time.monotonic() - started, 1.5)

    def test_redirect_is_refused_without_following_or_replaying(self):
        with socket.socket() as unrelated:
            unrelated.bind(("127.0.0.1", 0)); unrelated.listen(1); unrelated.settimeout(.1)
            location = f"Location: http://127.0.0.1:{unrelated.getsockname()[1]}/other\r\n"
            with self.listener(response({}, "302 Found", location)) as (child, url):
                with self.assertRaisesRegex(RuntimeError, "response is invalid"):
                    self.request(child, url, body={"action": "release"})
                self.assertTrue(self.receipt(child)["post"])
                with self.assertRaises(TimeoutError):
                    unrelated.accept()

    def test_truncated_and_oversized_responses_are_refused(self):
        cases = [b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{}",
                 response({"excess": "x" * 200})]
        for wire in cases:
            with self.subTest(wire=wire[:30]), self.listener(wire) as (child, url):
                with self.assertRaisesRegex(RuntimeError, "response is invalid"):
                    self.request(child, url, maximum_body=64)
                self.assertTrue(self.receipt(child)["authorized"])

    def test_valid_large_json_body_does_not_count_toward_header_limit(self):
        value = {"diagnostics": "x" * (128 * 1024)}
        with self.listener(response(value)) as (child, url):
            self.assertEqual(self.request(child, url), value)
            self.assertTrue(self.receipt(child)["authorized"])

    def fixture(self, root, child, url):
        config, runtime, release = root / "config", root / "state/execution", root / "releases/1.0.0"
        for path in (config, runtime, release):
            path.mkdir(parents=True, mode=0o700)
        files._atomic_write(config / "env", b"AGENTSDOCK_AGENT_TOKEN=fixture-token\n")
        files._atomic_write(runtime / "control.token", b"f" * 64)
        record = {"role": "worker", "protocol": 1, "pid": child.pid, "instance_id": "fixture-epoch",
                  "callback_origin": url.removesuffix("/api/health"), "release_root": str(release)}
        files._atomic_write(runtime / "worker.json", files._json_bytes(record))
        layout = SimpleNamespace(install_root=root, runtime_dir=runtime, config_root=config,
            manifest_path=config / "execution-layout.json", platform=self.platform,
            environment={}, bind="127.0.0.1", port=int(url.split(":")[2].split("/")[0]))
        services = mock.Mock()
        services.snapshot.return_value = {role: {"state": "running", "pid": child.pid}
                                          for role in ("worker", "gateway")}
        return layout, record, services

    def test_worker_control_public_and_callback_health_use_native_ownership(self):
        for callback in (False, True):
            with self.subTest(callback=callback), tempfile.TemporaryDirectory() as temp, self.listener() as (child, url):
                layout, record, services = self.fixture(Path(temp).resolve(), child, url)
                control = WorkerControl(services=services)
                result = control.callback_health(layout, record) if callback else control.health(layout)
                self.assertEqual(result, {"ok": True})
                self.assertTrue(self.receipt(child)["authorized"])
                self.assertGreaterEqual(services.snapshot.call_count, 2)

    def test_production_gateway_json_response_works_with_pinned_transport(self):
        with tempfile.TemporaryDirectory(prefix="ad-http-", dir="/tmp") as temp:
            root = Path(temp).resolve()
            env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
            child = subprocess.Popen([sys.executable, "-B", "-u", "-c", GATEWAY, str(root)],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                ready = child.stdout.readline()
                if not ready:
                    self.fail("Fixture gateway did not start: " + child.stderr.read())
                port = json.loads(ready)["port"]
                value = self.request(child, f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(value, {"ok": True, "pid": child.pid})
                self.assertEqual(child.wait(timeout=5), 0)
            finally:
                if child.poll() is None:
                    child.terminate()
                child.communicate(timeout=5)

    def test_stale_receipt_cannot_substitute_for_native_worker(self):
        with tempfile.TemporaryDirectory() as temp, self.listener() as (child, url):
            layout, record, services = self.fixture(Path(temp).resolve(), child, url)
            services.snapshot.return_value["worker"]["pid"] = os.getpid()
            with mock.patch.object(transport.socket, "create_connection") as connect:
                with self.assertRaisesRegex(RuntimeError, "native worker or callback"):
                    WorkerControl(services=services).status(layout)
                connect.assert_not_called()

    def test_native_receipt_change_during_connection_prevents_token_send(self):
        with tempfile.TemporaryDirectory() as temp, self.listener() as (child, url):
            layout, record, services = self.fixture(Path(temp).resolve(), child, url)
            original = services.snapshot.return_value
            services.snapshot.side_effect = [original, {**original, "worker": {"state": "running", "pid": os.getpid()}}]
            with self.assertRaisesRegex(RuntimeError, "native worker or callback"):
                WorkerControl(services=services).callback_health(layout, record)
            self.assertEqual(self.receipt(child)["received"], 0)

    def test_split_public_health_does_not_fall_back_to_worker_listener(self):
        with tempfile.TemporaryDirectory() as temp, self.listener() as (child, url):
            layout, record, services = self.fixture(Path(temp).resolve(), child, url)
            layout.manifest_path.write_text("{}")
            services.snapshot.return_value["gateway"] = {"state": "absent"}
            with mock.patch.object(transport.socket, "create_connection") as connect:
                with self.assertRaisesRegex(RuntimeError, "installed native gateway"):
                    WorkerControl(services=services).health(layout)
                connect.assert_not_called()


class LinuxTupleTests(unittest.TestCase):
    def test_tuple_requires_exact_pid_socket_inode_and_both_endpoints(self):
        line = "0: 0100007F:1F90 0100007F:C350 01 0:0 00:0 0 501 0 1234"
        with mock.patch.object(Path, "iterdir", return_value=iter([Path("/proc/42/fd/7")])), \
                mock.patch.object(Path, "read_text", return_value="header\n" + line), \
                mock.patch.object(os, "readlink", return_value="socket:[1234]"):
            self.assertTrue(transport.linux_connection_owned(42, ("127.0.0.1", 8080), ("127.0.0.1", 50000)))
        with mock.patch.object(Path, "iterdir", side_effect=lambda path=None: iter([Path("/proc/42/fd/7")])), \
                mock.patch.object(Path, "read_text", return_value="header\n" + line), \
                mock.patch.object(os, "readlink", return_value="socket:[9999]"):
            self.assertFalse(transport.linux_connection_owned(42, ("127.0.0.1", 8080), ("127.0.0.1", 50000)))


if __name__ == "__main__":
    unittest.main()
