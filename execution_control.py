"""Callback-listener-only controls for the persistent execution owner.

Do not wrap the application exposed over execution IPC with this middleware.
The private control token is independent of all client/provider credentials.
Lease admission and persistence remain the execution owner's responsibility.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import re
import uuid
from typing import Any


CONTROL_PREFIX = "/api/admin/execution"
STATUS_PATH = CONTROL_PREFIX + "/status"
MAINTENANCE_PATH = CONTROL_PREFIX + "/maintenance"
MAX_BODY_BYTES = 4096
MAX_RESPONSE_BYTES = 64 * 1024


class ExecutionControlError(RuntimeError):
    """An expected control refusal raised by an execution-owner callback."""

    def __init__(self, status_code: int, detail: str) -> None:
        if type(status_code) is not int or not 400 <= status_code <= 599:
            raise ValueError("Invalid execution control error status")
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise ExecutionControlError(400, "Duplicate JSON field")
        result[name] = value
    return result


def _constant(_value: str) -> None:
    raise ExecutionControlError(400, "Invalid JSON number")


def _loopback(scope: dict[str, Any]) -> bool:
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or len(client) != 2:
        return False
    try:
        address = ipaddress.ip_address(client[0])
    except (ValueError, TypeError):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def _headers(scope: dict[str, Any]) -> list[tuple[bytes, bytes]]:
    headers = scope.get("headers", [])
    if not isinstance(headers, (list, tuple)) or len(headers) > 128:
        raise ExecutionControlError(400, "Invalid control headers")
    result = []
    size = 0
    for item in headers:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or not all(isinstance(part, bytes) for part in item)):
            raise ExecutionControlError(400, "Invalid control headers")
        size += len(item[0]) + len(item[1])
        if size > 16384:
            raise ExecutionControlError(400, "Control headers exceed their limit")
        result.append((item[0].lower(), item[1]))
    return result


def _header_values(headers: list[tuple[bytes, bytes]], name: bytes) -> list[bytes]:
    return [value for key, value in headers if key == name]


def _content_length(headers: list[tuple[bytes, bytes]]) -> int | None:
    if _header_values(headers, b"transfer-encoding"):
        raise ExecutionControlError(400, "Control requests do not accept transfer encoding")
    values = _header_values(headers, b"content-length")
    if not values:
        return None
    if len(values) != 1 or re.fullmatch(rb"0|[1-9][0-9]{0,9}", values[0]) is None:
        raise ExecutionControlError(400, "Invalid control content length")
    length = int(values[0])
    if length > MAX_BODY_BYTES:
        raise ExecutionControlError(413, "Control request exceeds its size limit")
    return length


async def _body(receive: Any, declared: int | None) -> bytes:
    async def read() -> bytes:
        body = bytearray()
        while True:
            message = await receive()
            if message.get("type") != "http.request" or not isinstance(message.get("body", b""), bytes):
                raise ExecutionControlError(400, "Control request body was interrupted")
            chunk = message.get("body", b"")
            if len(chunk) > MAX_BODY_BYTES - len(body):
                raise ExecutionControlError(413, "Control request exceeds its size limit")
            body.extend(chunk)
            if declared is not None and len(body) > declared:
                raise ExecutionControlError(400, "Control content length does not match its body")
            more = message.get("more_body", False)
            if type(more) is not bool:
                raise ExecutionControlError(400, "Invalid control body framing")
            if not more:
                if declared is not None and declared != len(body):
                    raise ExecutionControlError(400, "Control content length does not match its body")
                return bytes(body)

    try:
        return await asyncio.wait_for(read(), timeout=5.0)
    except asyncio.TimeoutError:
        raise ExecutionControlError(408, "Control request body timed out") from None


class ExecutionControl:
    """Guard private status/maintenance requests before invoking owner callbacks."""

    def __init__(self, app: Any, *, token: str, worker_instance_id: str,
                 status_callback: Any, maintenance_callback: Any) -> None:
        if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{64}", token) is None:
            raise ValueError("Invalid execution control token")
        if (not isinstance(worker_instance_id, str) or not worker_instance_id
                or len(worker_instance_id) > 128 or not worker_instance_id.isascii()
                or any(character.isspace() for character in worker_instance_id)):
            raise ValueError("Invalid worker instance identity")
        self.app = app
        self._token = token
        self.worker_instance_id = worker_instance_id
        self.status_callback = status_callback
        self.maintenance_callback = maintenance_callback

    async def _respond(self, send: Any, status: int, value: dict[str, Any]) -> None:
        try:
            content = json.dumps(value, allow_nan=False, ensure_ascii=True,
                                 separators=(",", ":")).encode("ascii")
            if len(content) > MAX_RESPONSE_BYTES or self._token.encode("ascii") in content:
                raise ValueError("Invalid private control response")
        except (ValueError, TypeError, RecursionError):
            status = 500
            content = b'{"detail":"Execution control response unavailable"}'
        await send({"type": "http.response.start", "status": status, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(content)).encode("ascii")),
            (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
        ]})
        await send({"type": "http.response.body", "body": content})

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if not path.startswith(CONTROL_PREFIX):
            await self.app(scope, receive, send)
            return
        if scope.get("type") == "websocket":
            await send({"type": "websocket.close", "code": 4403, "reason": "Private HTTP control required"})
            return
        if scope.get("type") != "http":
            return
        try:
            headers = _headers(scope)
            forbidden = {b"cookie", b"origin", b"forwarded", b"via", b"x-real-ip",
                         b"x-agentsdock-token", b"x-zenithdock-token"}
            if (not _loopback(scope) or scope.get("query_string", b"")
                    or any(name in forbidden or name.startswith((b"sec-fetch-", b"x-forwarded-", b"tailscale-user-"))
                           for name, _ in headers)):
                raise ExecutionControlError(403, "Private control transport is forbidden")
            authorization = _header_values(headers, b"authorization")
            expected = b"Bearer " + self._token.encode("ascii")
            if len(authorization) != 1 or not hmac.compare_digest(authorization[0], expected):
                raise ExecutionControlError(401, "Execution control authentication required")
            if path not in {STATUS_PATH, MAINTENANCE_PATH}:
                raise ExecutionControlError(404, "Unknown execution control")
            method = scope.get("method")
            if method != ("GET" if path == STATUS_PATH else "POST"):
                raise ExecutionControlError(405, "Execution control method is not allowed")
            length = _content_length(headers)
            if path == STATUS_PATH:
                if length not in {None, 0}:
                    raise ExecutionControlError(400, "Status requests do not accept a body")
                result = await self.status_callback()
            else:
                content_types = _header_values(headers, b"content-type")
                if (len(content_types) != 1 or content_types[0].lower()
                        not in {b"application/json", b"application/json; charset=utf-8"}):
                    raise ExecutionControlError(415, "Execution control requires JSON")
                raw = await _body(receive, length)
                try:
                    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
                except (ValueError, UnicodeError, RecursionError):
                    raise ExecutionControlError(400, "Invalid execution control JSON") from None
                required = {"action", "expected_worker_instance_id", "operation_id"}
                if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - {"lease_id", "lifetime_seconds"}:
                    raise ExecutionControlError(422, "Invalid execution control fields")
                if value["expected_worker_instance_id"] != self.worker_instance_id:
                    raise ExecutionControlError(409, "Execution worker instance changed")
                action = value["action"]
                if not isinstance(action, str) or action not in {"acquire", "renew", "seal", "release"}:
                    raise ExecutionControlError(422, "Invalid maintenance action")
                operation_id = value["operation_id"]
                try:
                    if not isinstance(operation_id, str) or str(uuid.UUID(operation_id)) != operation_id:
                        raise ValueError("noncanonical operation")
                except (ValueError, AttributeError):
                    raise ExecutionControlError(422, "Operation ID must be a canonical UUID") from None
                lease_id = value.get("lease_id")
                if action == "acquire":
                    if "lease_id" in value:
                        raise ExecutionControlError(422, "Acquire must not supply a lease ID")
                elif (not isinstance(lease_id, str) or not 1 <= len(lease_id) <= 128
                      or re.fullmatch(r"[A-Za-z0-9_-]+", lease_id) is None):
                    raise ExecutionControlError(422, "Maintenance requires a valid lease ID")
                lifetime = value.get("lifetime_seconds", 120)
                if type(lifetime) is not int or not 30 <= lifetime <= 300:
                    raise ExecutionControlError(422, "Maintenance lifetime must be 30 to 300 seconds")
                result = await self.maintenance_callback(action, operation_id, lease_id, lifetime)
            if not isinstance(result, dict):
                raise ExecutionControlError(500, "Execution control response unavailable")
            response_status, response_value = 200, result
        except ExecutionControlError as exc:
            response_status, response_value = exc.status_code, {"detail": exc.detail}
        except Exception:
            response_status, response_value = 500, {"detail": "Execution control unavailable"}
        # Response transport loss must not invoke a handler again or attempt a
        # second response after an already committed lease operation.
        await self._respond(send, response_status, response_value)
