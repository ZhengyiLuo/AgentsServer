"""AST-only durable Join observation with virtual time and no live runtime."""
import ast
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hmac
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest

import tests.test_secure_peer_auto_join_runtime_isolated as legacy_fixture


PAIRING = legacy_fixture.PAIRING_ID
CONNECTION = legacy_fixture.CONNECTION_ID
TRANSCRIPT = legacy_fixture.TRANSCRIPT


class VirtualWait:
    def __init__(self):
        self.wall = 10_000.0
        self.monotonic = 100.0
        self.timeouts = []
        self.entered = asyncio.Event()
        self.mode = "timeout"
        self.before_timeout = None

    def time(self):
        return self.wall

    def get_running_loop(self):
        loop = asyncio.get_running_loop()
        return SimpleNamespace(time=lambda: self.monotonic,
                               call_soon_threadsafe=loop.call_soon_threadsafe)

    async def to_thread(self, function, *args, **kwargs):
        # The production worker is read-only. Execute the exact snapshot inline
        # so these tests cannot launch a worker or require wall-clock sleeps.
        return function(*args, **kwargs)

    async def wait_for(self, awaitable, *, timeout):
        self.timeouts.append(timeout)
        self.entered.set()
        if self.mode == "block":
            return await awaitable
        awaitable.close()
        self.wall += timeout
        self.monotonic += timeout
        if self.before_timeout is not None:
            self.before_timeout()
        raise asyncio.TimeoutError

    def facade(self):
        return SimpleNamespace(Event=asyncio.Event, get_running_loop=self.get_running_loop,
                               to_thread=self.to_thread, wait_for=self.wait_for,
                               TimeoutError=asyncio.TimeoutError)


def runtime_fixture(deadline=None):
    clock = VirtualWait()
    path = Path(__file__).with_name("secure_peer_runtime.py")
    tree = ast.parse(path.read_text())
    source = next(node for node in tree.body
                  if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    names = {"_pairing_completion_snapshot", "wait_pairing_completion",
             "_notify_pairing_completion", "_outgoing_for_pairing", "_outgoing_pairing",
             "_incoming_pairing", "_status", "_trust_state", "_transport_state", "_displayed_scopes"}
    methods = [node for node in source.body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in methods} == names
    iso = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_iso8601")
    namespace = {"asyncio": clock.facade(), "time": clock, "hmac": hmac,
                 "suppress": suppress, "datetime": datetime, "timezone": timezone,
                 "SecurePeerError": legacy_fixture.PeerError,
                 "SECURE_PEER_LEASE_SECONDS": 120, "SECURE_PEER_OFFLINE_FAILURES": 3,
                 "SECURE_PEER_PROXY_PREFIX": "/fixture-proxy"}
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=methods)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
                             iso, selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    runtime = namespace["Runtime"]()
    runtime._guard = threading.RLock()
    runtime._completion_waiters = {}
    runtime._completion_closing = runtime._host_role_active = runtime._relay_enabled = False
    runtime._client_failure_counts = {}
    runtime._host_store = None
    runtime.server_identity = "synthetic-observer-server"
    runtime._mail_hints = SimpleNamespace(invalidate=lambda: None)
    connection = {"connection_id": CONNECTION, "pairing_id": PAIRING,
                  "transcript_hash": TRANSCRIPT, "host_server_identity": "fixture-host",
                  "status": "pending", "active": False, "complete_on_approval": True,
                  "pairing_expires_at": 0 if deadline is None else deadline,
                  "peer_public_key_fingerprint": "sha256:" + "b" * 64,
                  "created_at": 1, "requested_scopes": ["teamspace.read", "teamspace.write"]}
    client = SimpleNamespace(connection=connection, state="pending", deadline=deadline,
                             reads=0, after_snapshot=None)
    client.list_connections = lambda: [dict(connection)]
    def snapshot(identifier):
        assert identifier == CONNECTION
        assert runtime._completion_waiters or not getattr(client, "require_observer", False)
        client.reads += 1
        result = {"connection": dict(connection), "state": client.state, "deadline": client.deadline}
        if client.after_snapshot is not None:
            callback, client.after_snapshot = client.after_snapshot, None
            callback()
        return result
    client.auto_completion_snapshot = snapshot
    runtime.client = client
    return runtime, clock, client


class DurablePairingObserverTests(unittest.IsolatedAsyncioTestCase):
    def test_durable_snapshot_stays_pending_days_after_join_and_projects_no_expiry(self):
        for deadline in (None, 0):
            with self.subTest(deadline=deadline):
                runtime, clock, client = runtime_fixture(deadline)
                clock.wall += 7 * 24 * 3600
                receipt, observed_deadline = runtime._pairing_completion_snapshot(
                    PAIRING, expected_transcript_hash=TRANSCRIPT)
                self.assertEqual(receipt["completion_state"], "pending")
                self.assertIsNone(observed_deadline)
                self.assertIsNone(receipt["pairing"]["expires_at"])
                self.assertTrue(receipt["pairing"]["complete_on_approval"])
                self.assertEqual(client.state, "pending")

    def test_incoming_and_outgoing_zero_expiry_is_null_but_certificate_expiry_is_preserved(self):
        runtime, _clock, client = runtime_fixture()
        value = {**client.connection, "expires_at": 0, "pairing_expires_at": 15_000,
                 "certificate_expires_at": 20_000}
        for project in (runtime._incoming_pairing, runtime._outgoing_pairing):
            with self.subTest(direction=project.__name__):
                receipt = project(value)
                self.assertIsNone(receipt["expires_at"])
                self.assertEqual(receipt["certificate_expires_at"], "1970-01-01T05:33:20Z")

    async def test_window_cap_releases_observer_without_expiring_or_consuming_join(self):
        runtime, clock, client = runtime_fixture()
        client.require_observer = True
        before = dict(client.connection)
        for _ in range(2):
            receipt = await runtime.wait_pairing_completion(PAIRING, expected_transcript_hash=TRANSCRIPT)
            self.assertEqual(receipt["completion_state"], "unavailable")
            self.assertEqual(receipt["reason"], "observation_window_elapsed")
            self.assertIsNone(receipt["pairing"]["expires_at"])
            self.assertEqual(runtime._completion_waiters, {})
        self.assertEqual(clock.timeouts, [600.0, 600.0])
        self.assertEqual(client.reads, 4, "read on ingress and timeout only; no polling")
        self.assertEqual(client.connection, before)
        self.assertEqual(client.state, "pending")

    async def test_original_positive_deadline_still_expires_before_observer_cap(self):
        runtime, clock, client = runtime_fixture(10_030)
        receipt = await runtime.wait_pairing_completion(PAIRING, expected_transcript_hash=TRANSCRIPT)
        self.assertEqual(receipt["completion_state"], "expired")
        self.assertNotIn("reason", receipt)
        self.assertEqual(clock.timeouts, [30.0])
        self.assertEqual(client.deadline, 10_030)
        self.assertIsNotNone(receipt["pairing"]["expires_at"])

    async def test_long_positive_deadline_is_not_shortened_by_observer_cap(self):
        runtime, clock, client = runtime_fixture(13_600)
        receipt = await runtime.wait_pairing_completion(PAIRING, expected_transcript_hash=TRANSCRIPT)
        self.assertEqual((receipt["completion_state"], receipt["reason"]),
                         ("unavailable", "observation_window_elapsed"))
        self.assertEqual(clock.timeouts, [600.0])
        self.assertEqual(client.deadline, 13_600)
        self.assertIsNotNone(receipt["pairing"]["expires_at"])

    async def test_terminal_commit_wins_over_window_timeout_without_notification(self):
        for state, status, active, expected in (
                ("completed", "connected", True, "completed"),
                ("cancelled", "cancelled", False, "cancelled"),
                ("pending", "rejected", False, "cancelled"),
                ("pending", "revoked", False, "cancelled")):
            with self.subTest(status=status):
                runtime, clock, client = runtime_fixture()
                def terminal():
                    client.state = state
                    client.connection.update(status=status, active=active)
                clock.before_timeout = terminal
                receipt = await runtime.wait_pairing_completion(PAIRING, expected_transcript_hash=TRANSCRIPT)
                self.assertEqual(receipt["completion_state"], expected)
                self.assertNotIn("reason", receipt)
                self.assertEqual(client.reads, 2)
                self.assertEqual(runtime._completion_waiters, {})

    async def test_notification_between_snapshot_and_wait_is_not_lost(self):
        runtime, clock, client = runtime_fixture()
        clock.mode = "block"
        def completed():
            client.state = "completed"
            client.connection.update(status="connected", active=True)
            runtime._notify_pairing_completion()
        client.after_snapshot = completed
        receipt = await asyncio.wait_for(runtime.wait_pairing_completion(
            PAIRING, expected_transcript_hash=TRANSCRIPT), 1)
        self.assertEqual(receipt["completion_state"], "completed")
        self.assertEqual(client.reads, 2)
        self.assertEqual(runtime._completion_waiters, {})

    async def test_observer_cancel_keeps_durable_join_pending(self):
        runtime, clock, client = runtime_fixture()
        clock.mode = "block"
        task = asyncio.create_task(runtime.wait_pairing_completion(PAIRING, expected_transcript_hash=TRANSCRIPT))
        await asyncio.wait_for(clock.entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(runtime._completion_waiters, {})
        self.assertEqual(client.state, "pending")
        self.assertFalse(client.connection["active"])

    async def test_http_disconnect_releases_only_durable_observer(self):
        runtime, clock, client = runtime_fixture()
        clock.mode = "block"
        endpoint = legacy_fixture.extracted_endpoint(runtime)["secure_peer_pairing_completion_endpoint"]
        request = legacy_fixture.Request()
        task = asyncio.create_task(endpoint(PAIRING, request, "guest-server", "guest-instance", TRANSCRIPT))
        await asyncio.wait_for(clock.entered.wait(), 1)
        await request.messages.put({"type": "http.disconnect"})
        response = await asyncio.wait_for(task, 1)
        self.assertEqual(response.status_code, 499)
        self.assertEqual(runtime._completion_waiters, {})
        self.assertEqual(client.state, "pending")

    async def test_shutdown_unavailable_and_transcript_error_both_release_observer(self):
        runtime, _clock, client = runtime_fixture()
        runtime._completion_closing = True
        receipt = await runtime.wait_pairing_completion(PAIRING, expected_transcript_hash=TRANSCRIPT)
        self.assertEqual(receipt["completion_state"], "unavailable")
        self.assertNotIn("reason", receipt)
        runtime._completion_closing = False
        with self.assertRaises(legacy_fixture.PeerError) as failure:
            await runtime.wait_pairing_completion(PAIRING, expected_transcript_hash="b" * 64)
        self.assertEqual(failure.exception.code, "pairing_changed")
        self.assertEqual(runtime._completion_waiters, {})
        self.assertEqual(client.state, "pending")

    async def test_no_consent_and_legacy_expiry_do_not_become_durable(self):
        for state, deadline, expected in ((None, None, "unavailable"),
                                          ("pending", 9_999, "expired")):
            with self.subTest(state=state, deadline=deadline):
                runtime, clock, client = runtime_fixture(deadline)
                client.state = state
                receipt = await runtime.wait_pairing_completion(PAIRING, expected_transcript_hash=TRANSCRIPT)
                self.assertEqual(receipt["completion_state"], expected)
                self.assertEqual(clock.timeouts, [])
                self.assertEqual(runtime._completion_waiters, {})


if __name__ == "__main__":
    unittest.main()
