"""Execution ownership, lifecycle and truthful component-health contracts."""

import asyncio
import json
from pathlib import Path
import resource
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import execution_service
from execution_service import ComponentHealth, ProcessLease, WorkerLifespan, remove_stale_worker_socket, serve_worker


class FileDescriptorLimitTests(unittest.TestCase):
    def test_service_entrypoints_have_headroom_with_old_low_limit_configuration(self):
        # Exercise real kernel limits only inside owned child processes. The
        # existing service definitions need not be rewritten to gain headroom.
        script = '''
import asyncio, json, os, resource, sys, tempfile
from pathlib import Path
from unittest.mock import patch
import execution_service
role = sys.argv[1]
soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
if hard != resource.RLIM_INFINITY and hard < 512:
    sys.exit(77)
resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
async def serve(args, lease):
    descriptors = []
    try:
        for _ in range(300):
            descriptors.append(os.open(os.devnull, os.O_RDONLY))
        current_soft, current_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        print(json.dumps({"role": args.role, "opened": len(descriptors),
                          "soft": current_soft, "hard_preserved": current_hard == hard}))
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
with tempfile.TemporaryDirectory() as temporary:
    sys.argv = ["execution_service", role, "--runtime-dir", str(Path(temporary).resolve() / "runtime")]
    with patch.object(execution_service, "serve_worker", serve), patch.object(execution_service, "serve_gateway", serve):
        sys.exit(execution_service.main())
'''
        for role in ("worker", "gateway"):
            with self.subTest(role=role):
                result = subprocess.run(
                    [sys.executable, "-B", "-c", script, role],
                    cwd=Path(execution_service.__file__).parent,
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 77:
                    self.skipTest("OS hard file-descriptor limit is below the probe's requirement")
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = json.loads(result.stdout)
                self.assertEqual(observed["role"], role)
                self.assertEqual(observed["opened"], 300)
                self.assertTrue(observed["hard_preserved"])
                self.assertGreaterEqual(observed["soft"], 512)

    def test_low_soft_limit_is_raised_without_changing_hard_limit(self):
        for hard, expected in ((resource.RLIM_INFINITY, 8192), (1024, 1024)):
            with self.subTest(hard=hard), \
                    patch("execution_service.resource.getrlimit", return_value=(256, hard)), \
                    patch("execution_service.resource.setrlimit") as set_limit:
                execution_service.ensure_file_descriptor_headroom()
                set_limit.assert_called_once_with(resource.RLIMIT_NOFILE, (expected, hard))

    def test_existing_higher_or_infinite_limits_are_never_lowered(self):
        for limits in ((16384, 32768), (resource.RLIM_INFINITY, resource.RLIM_INFINITY), (256, 256)):
            with self.subTest(limits=limits), \
                    patch("execution_service.resource.getrlimit", return_value=limits), \
                    patch("execution_service.resource.setrlimit") as set_limit:
                execution_service.ensure_file_descriptor_headroom()
                set_limit.assert_not_called()

    def test_os_refusal_does_not_prevent_service_startup(self):
        for error in (OSError("not permitted"), ValueError("exceeds system maximum")):
            with self.subTest(error=type(error).__name__), \
                    patch("execution_service.resource.getrlimit", return_value=(256, resource.RLIM_INFINITY)), \
                    patch("execution_service.resource.setrlimit", side_effect=error), \
                    self.assertLogs(level="WARNING"):
                execution_service.ensure_file_descriptor_headroom()


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
