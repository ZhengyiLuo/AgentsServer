"""Compile only the join runtime/control seam; never import agent_server."""

import ast
import asyncio
from contextlib import suppress
import hmac
import ipaddress
from pathlib import Path
import threading
import types
import unittest
import uuid
from unittest import mock

from pydantic import BaseModel, Field, field_validator


REPO = Path(__file__).resolve().parent
PAIRING_ID = "6c5d8c3c-0f74-4092-8c63-565b78e2d04e"
CONNECTION_ID = "8aa74e9b-f74e-4917-91b7-340821e22652"
TRANSCRIPT = "a" * 64


class PeerError(Exception):
    def __init__(self, code, message, status_code):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


class HTTPError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code


class Response:
    def __init__(self, content=None, *, status_code=200, headers=None):
        self.content, self.status_code, self.headers = content, status_code, headers or {}


def extracted_runtime():
    tree = ast.parse((REPO / "secure_peer_runtime.py").read_text())
    source = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    names = {
        "begin_pairing", "poll_pairing", "cancel_pairing", "maintenance_once",
        "_maintenance_once_unlocked", "_notify_pairing_completion",
        "_pairing_completion_snapshot", "wait_pairing_completion",
        "_complete_automatic_pairing_once", "_outgoing_for_pairing",
    }
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=[
        node for node in source.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ])
    namespace = {
        "asyncio": asyncio, "hmac": hmac, "suppress": suppress,
        "time": types.SimpleNamespace(time=lambda: 1000.99),
        "SecurePeerError": PeerError, "SECURE_PEER_LEASE_SECONDS": 120,
        "_safe_status_error": str,
    }
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<isolated-join-runtime>", "exec"), namespace)
    return namespace["Runtime"]


class Client:
    def __init__(self):
        self.connection = {
            "connection_id": CONNECTION_ID, "pairing_id": PAIRING_ID,
            "transcript_hash": TRANSCRIPT, "host_server_identity": "host-server",
            "hub_id": "host-hub-id", "status": "pending", "active": False,
        }
        self.info = {"state": "pending", "deadline": 1600}
        self.calls = []
        self.eligible = True
        self.remote_status = "pending"

    def list_connections(self):
        return [dict(self.connection)]

    def auto_completion_info(self, identifier):
        self.calls.append(("info", identifier))
        return None if self.info is None else dict(self.info)

    def auto_completion_snapshot(self, identifier):
        info = self.auto_completion_info(identifier) or {}
        return {"connection": dict(self.connection), "state": info.get("state"), "deadline": info.get("deadline")}

    def list_auto_completion_candidates(self, *, limit):
        self.calls.append(("candidates", limit))
        return [dict(self.connection)] if self.eligible else []

    def poll_pairing(self, identifier):
        self.calls.append(("poll", identifier))
        self.connection["status"] = self.remote_status
        return dict(self.connection)

    def activate_auto_connection(self, identifier, **expected):
        self.calls.append(("activate", identifier, expected))
        self.complete()

    def complete(self):
        self.connection.update(status="connected", active=True)
        self.info["state"] = "completed"

    def expire_pending_pairings(self):
        self.calls.append(("expire",))
        return 0

    def recover_pairing_attempts(self, *, limit):
        self.calls.append(("recover", limit))
        return {"remaining": 0}

    def cancel_pairing(self, identifier, *, idempotency_key):
        self.calls.append(("cancel", identifier))
        self.info["state"] = "cancelled"
        self.connection["status"] = "cancelled"


def runtime_fixture():
    runtime = extracted_runtime()()
    runtime.client = Client()
    runtime._guard = threading.RLock()
    runtime._outbound_guard = threading.RLock()
    runtime._completion_waiters = {}
    runtime._completion_closing = False
    runtime._host_role_active = False
    runtime._initialization_error = None
    runtime._adapter = runtime._host_store = runtime._gateway = None
    runtime._client_failure_counts = {}
    runtime._client_error = None
    runtime.logger = None
    runtime.display_name = "Guest test"
    runtime.retry_host_attachment = lambda: None
    runtime._outgoing_pairing = lambda connection: {"id": connection["pairing_id"], **connection}
    runtime.status = lambda: {"active": runtime.client.connection["active"]}
    runtime.publish_display_name = lambda name: runtime.client.calls.append(("name", name))
    return runtime


def extracted_endpoint(runtime):
    names = {
        "require_secure_peer_control", "canonical_secure_peer_path_uuid",
        "secure_peer_automatic_completion_capability", "secure_peer_pairing_completion_endpoint",
        "canonical_secure_peer_uuid", "canonical_secure_peer_ipv4", "canonical_secure_peer_scopes",
        "SecurePeerControlRequest", "SecurePeerPairingRequest",
    }
    tree = ast.parse((REPO / "agent_server.py").read_text())
    nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names]
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = []
    namespace = {
        "asyncio": asyncio, "uuid": uuid, "ipaddress": ipaddress,
        "BaseModel": BaseModel, "Field": Field, "field_validator": field_validator,
        "Any": object, "SECURE_PEER_SCOPES": ["teamspace.read", "teamspace.write"],
        "Query": lambda **kwargs: None, "Request": object, "Response": Response, "JSONResponse": Response,
        "HTTPException": HTTPError, "SecurePeerError": PeerError,
        "AGENT_TOKEN": "isolated-native-token", "SERVER_INSTANCE_ID": "guest-instance",
        "server_identity": lambda: "guest-server", "SECURE_PEER_RUNTIME": runtime,
        "secure_peer_browser_request_forbidden": lambda request: request.browser,
        "request_exact_secure_peer_control_authorized": lambda request: request.authorized,
        "secure_peer_error_response": lambda error: Response({"error": error.code}, status_code=error.status_code),
    }
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), "<isolated-join-control>", "exec"), namespace)
    namespace["SecurePeerPairingRequest"].model_rebuild(_types_namespace=namespace)
    return namespace


class Request:
    def __init__(self, *, authorized=True, browser=False):
        self.authorized, self.browser = authorized, browser
        self.messages = asyncio.Queue()

    async def receive(self):
        return await self.messages.get()


class AutoJoinRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def wait_until(self, predicate):
        for _ in range(1000):
            if predicate():
                return
            await asyncio.sleep(0.001)
        self.fail("isolated observer did not reach expected state")

    async def test_observer_is_not_a_poll_loop_and_wakes_on_completion(self):
        runtime = runtime_fixture()
        task = asyncio.create_task(runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash=TRANSCRIPT))
        await self.wait_until(lambda: bool(runtime.client.calls))
        await asyncio.sleep(0.01)
        self.assertEqual(runtime.client.calls, [("info", CONNECTION_ID)])
        runtime.client.complete()
        runtime._notify_pairing_completion()
        receipt = await asyncio.wait_for(task, 1)
        self.assertEqual(receipt["completion_state"], "completed")
        self.assertEqual(runtime._completion_waiters, {})

    async def test_observer_cancellation_keeps_durable_join_pending(self):
        runtime = runtime_fixture()
        task = asyncio.create_task(runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash=TRANSCRIPT))
        await self.wait_until(lambda: bool(runtime._completion_waiters))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(runtime._completion_waiters, {})
        self.assertEqual(runtime.client.info["state"], "pending")
        self.assertFalse(any(call[0] == "cancel" for call in runtime.client.calls))

    async def test_timeout_rechecks_a_concurrently_completed_receipt(self):
        runtime = runtime_fixture()
        runtime.client.info["deadline"] = 1001
        task = asyncio.create_task(runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash=TRANSCRIPT))
        await self.wait_until(lambda: bool(runtime.client.calls))
        runtime.client.complete()  # Deliberately delay/drop the notification.
        receipt = await asyncio.wait_for(task, 1)
        self.assertEqual(receipt["completion_state"], "completed")
        self.assertEqual(len(runtime.client.calls), 2)

    async def test_expiry_is_original_deadline_and_legacy_has_no_consent(self):
        runtime = runtime_fixture()
        runtime.client.info["deadline"] = 1000
        receipt = await runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash=TRANSCRIPT)
        self.assertEqual(receipt["completion_state"], "expired")
        runtime.client.info = None
        receipt = await runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash=TRANSCRIPT)
        self.assertEqual(receipt["completion_state"], "unavailable")

    async def test_transcript_mismatch_cleans_up_without_waiting(self):
        runtime = runtime_fixture()
        with self.assertRaises(PeerError) as raised:
            await runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash="b" * 64)
        self.assertEqual(raised.exception.code, "pairing_changed")
        self.assertEqual(runtime.client.calls, [("info", CONNECTION_ID)])
        self.assertEqual(runtime._completion_waiters, {})

    async def test_cancel_notification_wakes_observer_and_does_not_activate(self):
        runtime = runtime_fixture()
        task = asyncio.create_task(runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash=TRANSCRIPT))
        await self.wait_until(lambda: bool(runtime._completion_waiters))
        runtime.cancel_pairing(PAIRING_ID, idempotency_key=str(uuid.uuid4()))
        self.assertEqual((await asyncio.wait_for(task, 1))["completion_state"], "cancelled")
        self.assertFalse(runtime.client.connection["active"])

    async def test_waiter_capacity_is_bounded(self):
        runtime = runtime_fixture()
        runtime._completion_waiters = {object(): lambda: None for _ in range(32)}
        with self.assertRaises(PeerError) as raised:
            await runtime.wait_pairing_completion(PAIRING_ID, expected_transcript_hash=TRANSCRIPT)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(len(runtime._completion_waiters), 32)

    def test_maintenance_checks_only_opted_in_candidate_once(self):
        runtime = runtime_fixture()
        runtime._complete_automatic_pairing_once()
        self.assertEqual(runtime.client.calls, [("candidates", 1), ("poll", CONNECTION_ID)])
        runtime.client.calls.clear()
        runtime.client.eligible = False
        runtime._complete_automatic_pairing_once()
        self.assertEqual(runtime.client.calls, [("candidates", 1)])

    def test_approval_activates_exact_frozen_candidate_and_publishes_name(self):
        runtime = runtime_fixture()
        runtime.client.remote_status = "approved"
        runtime._complete_automatic_pairing_once()
        activation = next(call for call in runtime.client.calls if call[0] == "activate")
        self.assertEqual(activation[2], {
            "expected_pairing_id": PAIRING_ID, "expected_transcript_hash": TRANSCRIPT,
            "expected_host_server_identity": "host-server", "expected_hub_id": "host-hub-id",
        })
        self.assertTrue(runtime.client.connection["active"])
        self.assertIn(("name", "Guest test"), runtime.client.calls)

    def test_existing_maintenance_skips_guest_work_in_host_mode(self):
        runtime = runtime_fixture()
        runtime._host_role_active = True
        self.assertTrue(runtime.maintenance_once()["host_role_active"])
        self.assertEqual(runtime.client.calls, [])

    def test_empty_maintenance_preserves_existing_recovery_without_requests(self):
        runtime = runtime_fixture()
        runtime.client.eligible = False
        result = runtime.maintenance_once()
        self.assertFalse(result["active"])
        self.assertEqual(runtime.client.calls, [("expire",), ("recover", 2), ("candidates", 1)])

    async def test_endpoint_disconnect_releases_only_observer(self):
        runtime = runtime_fixture()
        namespace = extracted_endpoint(runtime)
        request = Request()
        task = asyncio.create_task(namespace["secure_peer_pairing_completion_endpoint"](
            PAIRING_ID, request, "guest-server", "guest-instance", TRANSCRIPT
        ))
        await self.wait_until(lambda: bool(runtime._completion_waiters))
        await request.messages.put({"type": "http.disconnect"})
        response = await asyncio.wait_for(task, 1)
        self.assertEqual(response.status_code, 499)
        self.assertEqual(runtime._completion_waiters, {})
        self.assertEqual(runtime.client.info["state"], "pending")

    async def test_endpoint_auth_target_and_browser_reject_before_observing(self):
        runtime = runtime_fixture()
        endpoint = extracted_endpoint(runtime)["secure_peer_pairing_completion_endpoint"]
        for request, server, status in [(Request(authorized=False), "guest-server", 401), (Request(browser=True), "guest-server", 403), (Request(), "other-server", 409)]:
            with self.subTest(status=status), self.assertRaises(HTTPError) as raised:
                await endpoint(PAIRING_ID, request, server, "guest-instance", TRANSCRIPT)
            self.assertEqual(raised.exception.status_code, status)
        self.assertEqual(runtime.client.calls, [])
        self.assertEqual(runtime._completion_waiters, {})

    async def test_completed_endpoint_is_exact_no_store_and_releases_tasks(self):
        runtime = runtime_fixture()
        runtime.client.complete()
        namespace = extracted_endpoint(runtime)
        response = await namespace["secure_peer_pairing_completion_endpoint"](
            PAIRING_ID, Request(), "guest-server", "guest-instance", TRANSCRIPT
        )
        self.assertEqual(response.content["completion_state"], "completed")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(runtime._completion_waiters, {})

    def test_join_opt_in_is_strict_and_capability_is_separate(self):
        runtime = runtime_fixture()
        runtime.state_available = lambda: True
        namespace = extracted_endpoint(runtime)
        model = namespace["SecurePeerPairingRequest"]
        payload = {"request_id": PAIRING_ID, "expected_server_identity": "guest-server", "expected_server_instance_id": "guest-instance", "host": "192.0.2.5", "display_name": "Test", "requested_scopes": ["teamspace.read"]}
        self.assertFalse(model(**payload).complete_on_approval)
        self.assertTrue(model(**payload, complete_on_approval=True).complete_on_approval)
        for invalid in [1, 0, "true", None]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                model(**payload, complete_on_approval=invalid)
        self.assertEqual(namespace["secure_peer_automatic_completion_capability"]()["max_wait_seconds"], 600)
        namespace["AGENT_TOKEN"] = ""
        self.assertFalse(namespace["secure_peer_automatic_completion_capability"]()["available"])


if __name__ == "__main__":
    unittest.main()
