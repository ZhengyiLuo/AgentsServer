"""Execution ownership, lifecycle and truthful component-health contracts."""

import asyncio
import json
from pathlib import Path
import socket
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from execution_service import ComponentHealth, ProcessLease, WorkerLifespan, remove_stale_worker_socket, serve_worker


class LeaseTests(unittest.TestCase):
    def test_only_one_worker_can_own_state_but_gateway_is_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / "runtime"
            with ProcessLease(directory, "worker") as worker:
                worker.publish(version="old")
                with self.assertRaises(BlockingIOError):
                    with ProcessLease(directory, "worker"):
                        self.fail("Second worker acquired the state")
                with ProcessLease(directory, "gateway") as gateway:
                    gateway.publish(version="new")
                    self.assertEqual(json.loads((directory / "worker.json").read_text())["version"], "old")
                self.assertTrue((directory / "worker.json").exists())
                self.assertFalse((directory / "gateway.json").exists())
            self.assertFalse((directory / "worker.json").exists())
            with ProcessLease(directory, "worker"):
                pass

    def test_refuses_symlink_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / "runtime"
            directory.mkdir(mode=0o700)
            victim = Path(temporary) / "unrelated"
            victim.write_text("preserve")
            (directory / "worker.lock").symlink_to(victim)
            with self.assertRaises(OSError):
                with ProcessLease(directory, "worker"):
                    pass
            self.assertEqual(victim.read_text(), "preserve")

    def test_stale_socket_recovery_refuses_live_or_unrelated_path(self):
        # macOS AF_UNIX paths are short; this is an ephemeral socket, not a build.
        with tempfile.TemporaryDirectory(prefix="adw-", dir="/tmp") as temporary:
            path = Path(temporary) / "worker.socket"
            listener = socket.socket(socket.AF_UNIX)
            try:
                listener.bind(str(path))
                listener.listen(1)
                with self.assertRaises(RuntimeError):
                    remove_stale_worker_socket(path)
                self.assertTrue(path.exists())
            finally:
                listener.close()
            remove_stale_worker_socket(path)
            self.assertFalse(path.exists())
            path.write_text("not a socket")
            with self.assertRaises(ValueError):
                remove_stale_worker_socket(path)
            self.assertEqual(path.read_text(), "not a socket")


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_maintenance_status_changes_after_commit_without_a_new_boot(self):
        held = True
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"server_version":"new","server_instance_id":"same-run"}'})
        wrapped = ComponentHealth(app, "execution_service", {"version": "new", "instance_id": "same-worker"},
                                  lambda: {"maintenance_held": held})
        for expected in (True, False):
            held = expected
            messages = []
            async def send(message):
                messages.append(message)
            await wrapped({"type": "http", "path": "/api/health"}, None, send)
            result = json.loads(messages[1]["body"])
            self.assertEqual(result["execution_service"], {
                "version": "new", "instance_id": "same-worker", "maintenance_held": expected})
            self.assertEqual(result["server_instance_id"], "same-run")

    async def test_gateway_does_not_claim_older_execution_was_upgraded(self):
        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": b'{"server_version":"old","server_instance_id":"same-run"}'})

        wrapped = ComponentHealth(ComponentHealth(app, "execution_service", {"version": "old"}), "gateway", {"version": "new"})
        messages = []

        async def send(message):
            messages.append(message)

        await wrapped({"type": "http", "path": "/api/health"}, None, send)
        result = json.loads(messages[1]["body"])
        self.assertEqual(result["server_version"], "old")
        self.assertEqual(result["server_instance_id"], "same-run")
        self.assertEqual(result["gateway"]["version"], "new")
        self.assertEqual(result["execution_service"]["version"], "old")
        self.assertEqual(dict(messages[0]["headers"])[b"content-length"], str(len(messages[1]["body"])).encode())

    async def test_denied_health_remains_unchanged(self):
        expected = [
            {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]},
            {"type": "http.response.body", "body": b'{"error":"unauthorized"}'},
        ]

        async def app(scope, receive, send):
            for message in expected:
                await send(message)

        received = []

        async def send(message):
            received.append(message)

        await ComponentHealth(app, "gateway", {"version": "private"})({"type": "http", "path": "/api/health"}, None, send)
        self.assertEqual(received, expected)


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_alternate_runtime_directory_cannot_bypass_state_ownership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            args = SimpleNamespace(application="agent_server:app", runtime_dir=root / "other")
            module = SimpleNamespace(STATE_DIR=root / "state")
            with ProcessLease(args.runtime_dir, "worker") as lease:
                with patch("execution_service.load_application", return_value=(module, object())), \
                        patch("execution_service.remove_stale_worker_socket") as remove:
                    with self.assertRaisesRegex(ValueError, "state directory"):
                        await serve_worker(args, lease)
                    remove.assert_not_called()

    async def test_execution_app_lifespan_runs_once_and_ipc_closes_before_provider_teardown(self):
        observed = []

        class Transport:
            async def start(self):
                observed.append("ipc-start")

            async def close(self):
                observed.append("ipc-close")

        async def app(scope, receive, send):
            self.assertEqual((await receive())["type"], "lifespan.startup")
            observed.append("provider-start")
            await send({"type": "lifespan.startup.complete"})
            self.assertEqual((await receive())["type"], "lifespan.shutdown")
            observed.append("provider-stop")
            await send({"type": "lifespan.shutdown.complete"})

        queue = asyncio.Queue()
        await queue.put({"type": "lifespan.startup"})
        await queue.put({"type": "lifespan.shutdown"})

        async def send(message):
            observed.append(message["type"])

        await WorkerLifespan(app, Transport(), lambda: observed.append("ready"))({"type": "lifespan"}, queue.get, send)
        self.assertEqual(observed, ["provider-start", "ipc-start", "ready", "lifespan.startup.complete", "ipc-close", "provider-stop", "lifespan.shutdown.complete"])


if __name__ == "__main__":
    unittest.main()
