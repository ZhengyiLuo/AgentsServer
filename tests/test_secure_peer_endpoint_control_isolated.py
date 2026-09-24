"""Exercise endpoint migration control seams without importing the server."""

import ast
import asyncio
from contextlib import suppress
import ipaddress
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from typing import Any, Literal
import unittest
from unittest import mock
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator
from starlette.responses import JSONResponse


REPO = Path(__file__).resolve().parents[1]
CONNECTION_ID = "8aa74e9b-f74e-4917-91b7-340821e22652"
OTHER_CONNECTION_ID = "123ddad9-b98a-432b-9122-35a560f43188"


class PeerError(Exception):
    def __init__(self, code, message, status_code):
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code


def compile_nodes(nodes, namespace, filename):
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *nodes,
    ], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), filename, "exec"), namespace)


def extracted_control(runtime):
    names = {
        "canonical_secure_peer_uuid", "canonical_secure_peer_ipv4",
        "SecurePeerControlRequest", "SecurePeerConfirmedRequest",
        "SecurePeerDeactivateRequest", "SecurePeerEndpointUpdateRequest",
        "require_secure_peer_control", "require_secure_peer_target",
        "canonical_secure_peer_path_uuid", "secure_peer_error_response",
        "secure_peer_connection_endpoint_update_endpoint",
    }
    source = ast.parse((REPO / "agent_server.py").read_text())
    nodes = [node for node in source.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and node.name in names]
    for node in nodes:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.decorator_list = []
    namespace = {
        "asyncio": asyncio, "uuid": uuid, "ipaddress": ipaddress,
        "Any": Any, "Literal": Literal,
        "BaseModel": BaseModel, "Field": Field, "field_validator": field_validator,
        "Request": object, "Response": JSONResponse, "JSONResponse": JSONResponse,
        "HTTPException": HTTPException, "SecurePeerError": PeerError, "HubError": PeerError,
        "AGENT_TOKEN": "isolated-token", "SERVER_INSTANCE_ID": "guest-instance",
        "server_identity": lambda: "guest-server", "SECURE_PEER_RUNTIME": runtime,
        "secure_peer_browser_request_forbidden": lambda request: request.browser,
        "request_exact_secure_peer_control_authorized": lambda request: request.authorized,
    }
    compile_nodes(nodes, namespace, "<isolated-peer-endpoint-control>")
    namespace["SecurePeerEndpointUpdateRequest"].model_rebuild(_types_namespace=namespace)
    return namespace


def extracted_runtime():
    source = ast.parse((REPO / "secure_peer_runtime.py").read_text())
    runtime = next(node for node in source.body
                   if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=[
        node for node in runtime.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"update_connection_endpoint", "_notify_pairing_completion"}
    ])
    namespace = {"SecurePeerError": PeerError, "suppress": suppress}
    compile_nodes([selected], namespace, "<isolated-peer-endpoint-runtime>")
    instance = namespace["Runtime"]()
    instance._outbound_guard = threading.RLock()
    instance._guard = threading.RLock()
    instance._host_role_active = False
    instance._client_failure_counts = {CONNECTION_ID: 3, OTHER_CONNECTION_ID: 2}
    instance._client_error = "Existing active transport error"
    instance._mail_hints = mock.Mock()
    instance._completion_waiters = {object(): mock.Mock()}
    instance.client = mock.Mock()
    instance.client.update_connection_endpoint.return_value = {"active": True}
    instance.status = mock.Mock(return_value={"endpoint_updated": True})
    return instance


def migration_arguments():
    return {
        "host_ip": "192.0.2.45", "port": 7852,
        "expected_host_ip": "192.0.2.44", "expected_port": 7851,
        "expected_host_server_identity": "host-server", "expected_hub_id": "host-hub-id",
    }


def request_values():
    return {
        "request_id": "587adeee-dc64-4d20-b0aa-0764ae33e244",
        "expected_server_identity": "guest-server",
        "expected_server_instance_id": "guest-instance", "confirmed": True,
        **migration_arguments(),
    }


class EndpointControlTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = mock.Mock()
        cls.control = extracted_control(cls.runtime)

    def setUp(self):
        self.runtime.reset_mock(return_value=True, side_effect=True)
        self.control["AGENT_TOKEN"] = "isolated-token"
        self.body = self.control["SecurePeerEndpointUpdateRequest"](**request_values())
        self.request = SimpleNamespace(authorized=True, browser=False)
        self.endpoint = self.control["secure_peer_connection_endpoint_update_endpoint"]

    def test_model_requires_exact_confirmation_and_canonical_endpoint(self):
        model = self.control["SecurePeerEndpointUpdateRequest"]
        invalid = {
            "confirmed": [False, 1, "true", None],
            "host_ip": ["localhost", "::1", "127.0.0.1", "0.0.0.0", "169.254.1.2", "224.0.0.1", "192.0.2.45 "],
            "expected_host_ip": ["host.example", "192.000.2.44"],
            "port": [True, "7852", 7852.0, 1023, 65536],
            "expected_port": [False, "7851", 1023, 65536],
            "request_id": [str(uuid.uuid1()), "587ADEEE-DC64-4D20-B0AA-0764AE33E244"],
            "unexpected": ["value"],
        }
        for field, values in invalid.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                    model(**{**request_values(), field: value})
        for field in request_values():
            values = request_values()
            del values[field]
            with self.subTest(missing=field), self.assertRaises(ValidationError):
                model(**values)

    def test_advertised_migration_capability_matches_registered_put_route(self):
        source = ast.parse((REPO / "agent_server.py").read_text())
        capability_nodes = [value for node in ast.walk(source) if isinstance(node, ast.Dict)
                            for key, value in zip(node.keys, node.values)
                            if isinstance(key, ast.Constant) and key.value == "secure_peer_v1"]
        self.assertEqual(len(capability_nodes), 1)
        for enabled in (False, True):
            namespace = {"AGENT_TOKEN": "isolated-token" if enabled else "",
                         "SECURE_PEER_RUNTIME": SimpleNamespace(
                             state_available=lambda: True, state_error_code=lambda: None)}
            capability = eval(compile(ast.Expression(capability_nodes[0]),
                                      "<isolated-secure-peer-capability>", "eval"), namespace)
            self.assertIs(capability["available"], enabled)
            self.assertEqual(capability["endpoint_update_version"], 1)
            self.assertEqual(capability["endpoint_update_path"],
                             "/api/admin/secure-peers/v1/connections/{connection_id}/endpoint")
        endpoint = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef)
                        and node.name == "secure_peer_connection_endpoint_update_endpoint")
        routes = [decorator for decorator in endpoint.decorator_list
                  if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                  and decorator.func.attr == "put"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(ast.literal_eval(routes[0].args[0]), capability["endpoint_update_path"])

    async def test_authorization_is_required_before_runtime_work(self):
        for browser, authorized, token, status in (
            (True, True, "isolated-token", 403),
            (False, False, "isolated-token", 401),
            (False, True, "", 503),
        ):
            self.control["AGENT_TOKEN"] = token
            request = SimpleNamespace(browser=browser, authorized=authorized)
            with self.subTest(status=status), self.assertRaises(HTTPException) as caught:
                await self.endpoint(CONNECTION_ID, self.body, request)
            self.assertEqual(caught.exception.status_code, status)
        self.runtime.update_connection_endpoint.assert_not_called()

    async def test_changed_server_or_instance_is_rejected_before_runtime(self):
        for field in ("expected_server_identity", "expected_server_instance_id"):
            body = self.control["SecurePeerEndpointUpdateRequest"](
                **{**request_values(), field: "different-target"})
            with self.subTest(field=field), self.assertRaises(HTTPException) as caught:
                await self.endpoint(CONNECTION_ID, body, self.request)
            self.assertEqual(caught.exception.status_code, 409)
        self.runtime.update_connection_endpoint.assert_not_called()

    async def test_connection_path_must_be_canonical_uuid_v4(self):
        for connection_id in ("unknown", CONNECTION_ID.upper(), str(uuid.uuid1())):
            with self.subTest(connection_id=connection_id), self.assertRaises(HTTPException) as caught:
                await self.endpoint(connection_id, self.body, self.request)
            self.assertEqual(caught.exception.status_code, 404)
        self.runtime.update_connection_endpoint.assert_not_called()

    async def test_migration_runs_off_event_loop_once_with_exact_arguments(self):
        caller_thread = threading.get_ident()
        worker_threads = []

        def migrate(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            return {"connection_id": CONNECTION_ID}

        self.runtime.update_connection_endpoint.side_effect = migrate
        response = await self.endpoint(CONNECTION_ID, self.body, self.request)
        self.runtime.update_connection_endpoint.assert_called_once_with(
            CONNECTION_ID, **migration_arguments())
        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], caller_thread)
        self.assertEqual(json.loads(response.body), {"connection_id": CONNECTION_ID})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")

    async def test_failed_probe_preserves_typed_error_and_disables_caching(self):
        self.runtime.update_connection_endpoint.side_effect = PeerError(
            "host_identity_mismatch", "Candidate identity did not match", 409)
        response = await self.endpoint(CONNECTION_ID, self.body, self.request)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body), {"error": {
            "code": "host_identity_mismatch", "message": "Candidate identity did not match"}})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["pragma"], "no-cache")


class EndpointRuntimeTests(unittest.TestCase):
    def test_host_role_rejects_migration_without_touching_saved_connection(self):
        runtime = extracted_runtime()
        runtime._host_role_active = True
        with self.assertRaises(PeerError) as caught:
            runtime.update_connection_endpoint(CONNECTION_ID, **migration_arguments())
        self.assertEqual(caught.exception.code, "host_role_active")
        runtime.client.update_connection_endpoint.assert_not_called()
        runtime._mail_hints.invalidate.assert_not_called()

    def test_success_clears_active_failure_and_notifies_existing_observers(self):
        runtime = extracted_runtime()
        result = runtime.update_connection_endpoint(CONNECTION_ID, **migration_arguments())
        args = migration_arguments()
        host, port = args.pop("host_ip"), args.pop("port")
        runtime.client.update_connection_endpoint.assert_called_once_with(CONNECTION_ID, host, port, **args)
        self.assertEqual(len(runtime.client.mock_calls), 1)
        self.assertIsNone(runtime._client_error)
        self.assertEqual(runtime._client_failure_counts, {OTHER_CONNECTION_ID: 2})
        runtime._mail_hints.invalidate.assert_called_once_with()
        for waiter in runtime._completion_waiters.values():
            waiter.assert_called_once_with()
        self.assertEqual(result, {"endpoint_updated": True})

    def test_inactive_migration_preserves_error_for_other_active_connection(self):
        runtime = extracted_runtime()
        runtime.client.update_connection_endpoint.return_value = {"active": False}
        runtime.update_connection_endpoint(CONNECTION_ID, **migration_arguments())
        self.assertEqual(runtime._client_error, "Existing active transport error")
        self.assertEqual(runtime._client_failure_counts, {OTHER_CONNECTION_ID: 2})
        self.assertEqual(len(runtime.client.mock_calls), 1)
        runtime._mail_hints.invalidate.assert_not_called()
        for waiter in runtime._completion_waiters.values():
            waiter.assert_not_called()

    def test_failed_probe_does_not_clear_state_or_notify_or_retry(self):
        runtime = extracted_runtime()
        runtime.client.update_connection_endpoint.side_effect = PeerError(
            "transport_failed", "Candidate unavailable", 502)
        with self.assertRaises(PeerError):
            runtime.update_connection_endpoint(CONNECTION_ID, **migration_arguments())
        self.assertEqual(runtime._client_error, "Existing active transport error")
        self.assertEqual(runtime._client_failure_counts, {CONNECTION_ID: 3, OTHER_CONNECTION_ID: 2})
        self.assertEqual(len(runtime.client.mock_calls), 1)
        runtime._mail_hints.invalidate.assert_not_called()
        runtime.status.assert_not_called()
        for waiter in runtime._completion_waiters.values():
            waiter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
