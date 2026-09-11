#!/usr/bin/env python3
"""Capability-scoped same-server chat contact CLI for AgentsDock agents."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# The legacy ``response_timeout_seconds`` wire field is now only a requested
# heartbeat interval.  There is deliberately no client response-deadline
# constant.  A provider tool call observes at most one bounded slice, then
# returns either the server's explicit pending receipt or an honest retryable
# transport receipt.  The provider immediately invokes ``wait`` with those
# exact opaque IDs until the server lease reaches terminal state.  Keeping
# every network observation at 30 seconds or less bounds the whole idempotent
# command safely below provider shell caps, instead of turning any
# provider-specific Bash limit into a cross-chat response deadline.
LIVE_RESPONSE_HEARTBEAT_SECONDS = 20
LIVE_RESPONSE_MAX_HEARTBEAT_SECONDS = 20
LIVE_RESPONSE_SOCKET_GRACE_SECONDS = 10
LIVE_RESPONSE_POST_SOCKET_SECONDS = 10
IDEMPOTENT_POST_RETRY_DELAYS_SECONDS = (0.1, 0.5)
IDEMPOTENT_GET_RETRY_DELAYS_SECONDS = (0.1, 0.5)
PROVIDER_RUNTIME_VALUE_MAX_BYTES = 4096
PROVIDER_RUNTIME_HANDLE_MAX_COUNT = 64


class ChatsCLIError(RuntimeError):
    pass


class LiveWaitRetryable(ChatsCLIError):
    """One bounded live-wait slice lost transport, but its lease is intact."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def host_is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def _canonical_http_origin(value: str, label: str) -> tuple[str, bool]:
    raw = value.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port or 80
    except ValueError as exc:
        raise ChatsCLIError(f"{label} must be an HTTP origin") from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ChatsCLIError(f"{label} must be an HTTP origin")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        host = address.compressed
        loopback = address.is_loopback
        url_host = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
    except ValueError:
        loopback = host == "localhost"
        url_host = host
    return f"http://{url_host}:{port}", loopback


def _authority_server_origin(path: str | None) -> str:
    authority_path = _authority_path(path)
    try:
        if authority_path.stat().st_mode & 0o077:
            raise ChatsCLIError("authority file permissions are unsafe")
        payload = json.loads(authority_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatsCLIError(f"could not read authority file: {exc}") from exc
    return _bounded_identity_value(
        payload.get("provider_server_origin"),
        "authority provider_server_origin",
    )


def environment() -> str:
    raw_server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip()
    if not raw_server_url:
        raise ChatsCLIError("missing AgentsDock agent environment")
    server_origin, loopback = _canonical_http_origin(
        raw_server_url,
        "AGENTSDOCK_SERVER_URL",
    )
    runtime_origin = _bounded_runtime_value(
        "AGENTSDOCK_PROVIDER_SERVER_ORIGIN"
    )
    if runtime_origin:
        canonical_runtime, _runtime_loopback = _canonical_http_origin(
            runtime_origin,
            "AGENTSDOCK_PROVIDER_SERVER_ORIGIN",
        )
        if canonical_runtime != server_origin:
            raise ChatsCLIError(
                "AGENTSDOCK_SERVER_URL conflicts with the live provider origin"
            )
    if loopback:
        return raw_server_url.rstrip("/")
    authority_origin = _authority_server_origin(None)
    if not authority_origin:
        raise ChatsCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    canonical_authority, _authority_loopback = _canonical_http_origin(
        authority_origin,
        "authority provider_server_origin",
    )
    if canonical_authority != server_origin:
        raise ChatsCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    return server_origin


def _bounded_identity_value(value: str | None, label: str) -> str:
    clean = str(value or "").strip()
    try:
        encoded_size = len(clean.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ChatsCLIError(f"{label} is not valid UTF-8") from exc
    if encoded_size > PROVIDER_RUNTIME_VALUE_MAX_BYTES:
        raise ChatsCLIError(f"{label} exceeds the provider runtime limit")
    return clean


def _bounded_runtime_value(name: str) -> str:
    return _bounded_identity_value(os.environ.get(name), name)


def _authority_path(path: str | None) -> Path:
    explicit = _bounded_identity_value(path, "--authority-file")
    ambient = _bounded_runtime_value("AGENTSDOCK_PROVIDER_AUTHORITY_FILE")
    if explicit and ambient:
        explicit_key = os.path.abspath(os.path.expanduser(explicit))
        ambient_key = os.path.abspath(os.path.expanduser(ambient))
        if explicit_key != ambient_key:
            raise ChatsCLIError(
                "--authority-file conflicts with the live provider authority"
            )
    selected = explicit or ambient
    if not selected:
        raise ChatsCLIError("--authority-file is required")
    return Path(selected).expanduser()


def authority(path: str | None) -> str:
    authority_path = _authority_path(path)
    try:
        mode = authority_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ChatsCLIError("authority file permissions are unsafe")
        payload = json.loads(authority_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatsCLIError(f"could not read authority file: {exc}") from exc
    token = str(payload.get("provider_capability") or payload.get("capability") or "")
    source_session_id = str(payload.get("source_session_id") or "").strip()
    if not token or not source_session_id:
        raise ChatsCLIError("authority file is invalid")
    environment_chat_id = _bounded_runtime_value("AGENTSDOCK_CHAT_ID")
    if environment_chat_id and environment_chat_id != source_session_id:
        raise ChatsCLIError(
            "AGENTSDOCK_CHAT_ID does not match the authority file"
        )
    return token


def positive_target_index(value: str) -> int:
    if len(value) > 2 or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("--target-index must be a positive integer")
    index = int(value)
    if index > PROVIDER_RUNTIME_HANDLE_MAX_COUNT:
        raise argparse.ArgumentTypeError(
            f"--target-index must be at most {PROVIDER_RUNTIME_HANDLE_MAX_COUNT}"
        )
    return index


def provider_handle(index: int, action: str) -> tuple[str, bool]:
    count_text = _bounded_runtime_value("AGENTSDOCK_CROSS_CHAT_HANDLE_COUNT")
    if re.fullmatch(r"0|[1-9][0-9]*", count_text) is None:
        raise ChatsCLIError("the live @Chat handle count is unavailable")
    count = int(count_text)
    if count > PROVIDER_RUNTIME_HANDLE_MAX_COUNT or index > count:
        raise ChatsCLIError("the requested @Chat handle is unavailable")
    prefix = f"AGENTSDOCK_CROSS_CHAT_HANDLE_{index}"
    handle = _bounded_runtime_value(prefix)
    granted_action = _bounded_runtime_value(f"{prefix}_ACTION")
    async_text = _bounded_runtime_value(f"{prefix}_ASYNC")
    expected_action = "instruction" if action == "instruction" else "request_reply"
    if not handle or granted_action != expected_action or async_text not in {"0", "1"}:
        raise ChatsCLIError("the requested @Chat handle is unavailable")
    if action == "instruction" and async_text != "0":
        raise ChatsCLIError("the requested @Chat handle is malformed")
    return handle, async_text == "1"


def respond_current(args: argparse.Namespace) -> dict[str, Any]:
    if _bounded_runtime_value("AGENTSDOCK_CROSS_CHAT_RESPONSE_MODE") == "async_route_v1":
        route_id = _bounded_runtime_value("AGENTSDOCK_CROSS_CHAT_RESPONSE_ROUTE_ID")
        if re.fullmatch(r"route_[0-9a-f]{32}", route_id) is None:
            raise ChatsCLIError("the current inbound conversation route is unavailable")
        values = vars(args).copy()
        values.update({"route": route_id, "target": None, "target_index": None,
                       "mode": "async_route_v1", "async_response": True})
        return send_action(argparse.Namespace(**values), "instruction")
    exchange_id = _bounded_runtime_value(
        "AGENTSDOCK_CROSS_CHAT_RESPONSE_EXCHANGE_ID"
    )
    inbound_leg_id = _bounded_runtime_value(
        "AGENTSDOCK_CROSS_CHAT_RESPONSE_INBOUND_LEG_ID"
    )
    followup = _bounded_runtime_value(
        "AGENTSDOCK_CROSS_CHAT_RESPONSE_FOLLOWUP"
    ) or "none"
    if (
        re.fullmatch(r"exchange_[0-9a-f]{32}", exchange_id) is None
        or re.fullmatch(r"leg_[0-9a-f]{32}", inbound_leg_id) is None
        or followup not in {"none", "allowed", "allowed-async"}
    ):
        raise ChatsCLIError("the current inbound reply grant is unavailable")
    request_response = bool(args.request_response)
    if request_response and followup == "none":
        raise ChatsCLIError("the current inbound reply has no follow-up grant")
    values = vars(args).copy()
    values.update({
        "exchange": exchange_id,
        "inbound_leg": inbound_leg_id,
        "async_response": request_response and followup == "allowed-async",
    })
    return respond(argparse.Namespace(**values))


def provider_headers(capability: str) -> dict[str, str]:
    """Return the one canonical header accepted by agent-helper routes.

    The retired cross-chat-specific header is intentionally omitted.  The
    server rejects requests that mix legacy and current authority names so a
    browser or stale helper cannot smuggle ambiguous credentials.
    """

    return {
        "Accept": "application/json",
        "X-AgentsDock-Provider-Capability": capability,
    }


def post_json(
    path: str,
    payload: dict[str, Any],
    capability: str,
) -> dict[str, Any]:
    server_url = environment()
    body = json.dumps(payload).encode("utf-8")
    headers = {
        **provider_headers(capability),
        "Content-Type": "application/json",
    }
    request = urllib.request.Request(
        f"{server_url}{path}",
        data=body,
        headers=headers,
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    promotion_deadline = time.monotonic() + 10.0
    transport_retry = 0
    while True:
        try:
            # The POST commits and returns a lease; response waiting happens
            # through bounded GET heartbeats below.
            socket_timeout = LIVE_RESPONSE_POST_SOCKET_SECONDS
            with opener.open(request, timeout=socket_timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except (OSError, http.client.IncompleteRead) as read_exc:
                retryable = bool(payload.get("idempotency_key"))
                if (
                    retryable
                    and transport_retry
                    < len(IDEMPOTENT_POST_RETRY_DELAYS_SECONDS)
                ):
                    delay = IDEMPOTENT_POST_RETRY_DELAYS_SECONDS[
                        transport_retry
                    ]
                    transport_retry += 1
                    time.sleep(delay)
                    continue
                raise ChatsCLIError(
                    "could not confirm whether AgentsServer accepted the "
                    "request because its error response was truncated; do "
                    "not resend it with different wording"
                ) from read_exc
            try:
                detail = json.loads(raw).get("detail") or raw
            except json.JSONDecodeError:
                detail = raw
            if (
                exc.code == 409
                and detail == "agent chat access is waiting for turn promotion"
                and time.monotonic() < promotion_deadline
            ):
                # The body and idempotency key are identical on every attempt.
                # Promotion has made no durable target effect yet.
                time.sleep(0.05)
                continue
            raise ChatsCLIError(
                f"server rejected handoff ({exc.code}): {detail or exc.reason}"
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.IncompleteRead,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            retryable = bool(payload.get("idempotency_key"))
            if (
                retryable
                and transport_retry < len(IDEMPOTENT_POST_RETRY_DELAYS_SECONDS)
            ):
                delay = IDEMPOTENT_POST_RETRY_DELAYS_SECONDS[transport_retry]
                transport_retry += 1
                # Reuse the byte-identical request and idempotency key. The
                # prior server attempt may still commit after its socket dies.
                time.sleep(delay)
                continue
            detail = getattr(exc, "reason", exc)
            if retryable:
                raise ChatsCLIError(
                    "could not confirm whether AgentsServer accepted the "
                    "request after retrying the same idempotency key; do not "
                    f"resend it with different wording: {detail}"
                ) from exc
            raise ChatsCLIError(
                f"could not reach AgentsServer: {detail}"
            ) from exc
    if not isinstance(result, dict):
        raise ChatsCLIError("AgentsServer returned an invalid response")
    return result


def get_json(
    path: str,
    capability: str,
    *,
    timeout: float = 30,
    live_slice: bool = False,
) -> dict[str, Any]:
    server_url = environment()
    request = urllib.request.Request(
        f"{server_url}{path}",
        headers=provider_headers(capability),
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    transport_retry = 0

    def retry_delay() -> float:
        return IDEMPOTENT_GET_RETRY_DELAYS_SECONDS[transport_retry]

    while True:
        try:
            with opener.open(request, timeout=timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except (OSError, http.client.IncompleteRead) as read_exc:
                if live_slice:
                    raise LiveWaitRetryable(
                        "the live-response transport was interrupted"
                    ) from read_exc
                if (
                    transport_retry < len(IDEMPOTENT_GET_RETRY_DELAYS_SECONDS)
                ):
                    delay = retry_delay()
                    transport_retry += 1
                    time.sleep(delay)
                    continue
                raise ChatsCLIError(
                    "AgentsServer returned a truncated error response after "
                    "retrying the exact live-response lease"
                ) from read_exc
            try:
                detail = json.loads(raw).get("detail") or raw
            except json.JSONDecodeError:
                detail = raw
            if live_slice and (
                exc.code in {408, 425, 429, 499}
                or 500 <= exc.code <= 599
            ):
                raise LiveWaitRetryable(
                    "the live-response transport is temporarily unavailable"
                ) from exc
            raise ChatsCLIError(
                f"server rejected request ({exc.code}): {detail or exc.reason}"
            ) from exc
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.IncompleteRead,
        ) as exc:
            if live_slice:
                raise LiveWaitRetryable(
                    "the live-response transport is temporarily unavailable"
                ) from exc
            if (
                transport_retry < len(IDEMPOTENT_GET_RETRY_DELAYS_SECONDS)
            ):
                delay = retry_delay()
                transport_retry += 1
                # GET is side-effect free and the live-response URL contains
                # the same exact lease on every attempt. The server retains
                # the result for this exact live provider-run owner.
                time.sleep(delay)
                continue
            raise ChatsCLIError(
                f"could not reach AgentsServer: {getattr(exc, 'reason', exc)}"
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # A malformed HTTP success body is a protocol error, not evidence
            # that a busy peer still has work queued. A live slice must also
            # remain one bounded provider-tool observation, so fail it
            # immediately instead of multiplying its socket-timeout budget.
            if live_slice:
                raise ChatsCLIError(
                    "AgentsServer returned an invalid live-response body"
                ) from exc
            # Other side-effect-free GETs retain the small ambiguity retry
            # window, then fail instead of spinning forever on a corrupt or
            # incompatible server.
            if transport_retry < len(IDEMPOTENT_GET_RETRY_DELAYS_SECONDS):
                delay = retry_delay()
                transport_retry += 1
                time.sleep(delay)
                continue
            raise ChatsCLIError(
                "AgentsServer returned an invalid live-response body"
            ) from exc
    if not isinstance(result, dict):
        raise ChatsCLIError("AgentsServer returned an invalid response")
    return result


def live_response_heartbeat_seconds(value: int) -> int:
    return max(1, min(int(value), LIVE_RESPONSE_MAX_HEARTBEAT_SECONDS))


def await_live_response(
    receipt: dict[str, Any],
    capability: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    exchange_id = str(receipt.get("exchange_id") or "")
    inbound_leg_id = str(receipt.get("inbound_leg_id") or "")
    lease_id = str(receipt.get("live_response_lease_id") or "")
    if not re.fullmatch(r"exchange_[0-9a-f]{32}", exchange_id):
        raise ChatsCLIError("live response exchange id is invalid")
    if not re.fullmatch(r"leg_[0-9a-f]{32}", inbound_leg_id):
        raise ChatsCLIError("live response inbound leg id is invalid")
    if not re.fullmatch(r"lease_[0-9a-f]{32}", lease_id):
        raise ChatsCLIError("live response lease id is invalid")
    heartbeat_seconds = live_response_heartbeat_seconds(timeout_seconds)
    query = urllib.parse.urlencode({
        "lease_id": lease_id,
        # Compatibility query name: the server treats it as a bounded
        # transport heartbeat, never a total response deadline.
        "timeout_seconds": heartbeat_seconds,
    })
    path = (
        "/api/agent/cross-chat/exchanges/"
        f"{urllib.parse.quote(exchange_id, safe='')}/legs/"
        f"{urllib.parse.quote(inbound_leg_id, safe='')}/live-response?{query}"
    )
    answer_keys = {
        "ok", "exchange_id", "inbound_leg_id", "body", "request_response",
    }
    deferred_keys = {
        "ok", "exchange_id", "inbound_leg_id", "deferred", "delivery",
        "message",
    }
    pending_keys = {"ok", "exchange_id", "inbound_leg_id", "pending"}
    try:
        result = get_json(
            path,
            capability,
            timeout=(
                heartbeat_seconds
                + LIVE_RESPONSE_SOCKET_GRACE_SECONDS
            ),
            live_slice=True,
        )
    except LiveWaitRetryable:
        # A transport failure says nothing about durable server state.  In
        # particular, the response may already be committed while the HTTP
        # handler is still disconnecting.  Never relabel that ambiguity as a
        # genuine server-owned pending exchange.  Return the same exact lease
        # as a distinct retry receipt; replaying its GET is side-effect free
        # and can recover a committed answer without resending the ask.
        return {
            "ok": False,
            "exchange_id": exchange_id,
            "inbound_leg_id": inbound_leg_id,
            "live_response_lease_id": lease_id,
            "transport_error": True,
            "retryable": True,
            "message": (
                "AgentsServer did not confirm the live-response state because "
                "the transport was interrupted. Retry the existing wait "
                f"exactly with --exchange {exchange_id} "
                f"--inbound-leg {inbound_leg_id} --lease {lease_id}; "
                "do not resend the ask or change its wording."
            ),
        }
    valid_answer = (
        set(result) == answer_keys
        and isinstance(result.get("body"), str)
        and isinstance(result.get("request_response"), bool)
    )
    valid_deferred = (
        set(result) == deferred_keys
        and result.get("deferred") is True
        and result.get("delivery") == "asynchronous"
        and isinstance(result.get("message"), str)
    )
    valid_pending = (
        set(result) == pending_keys
        and result.get("pending") is True
        and result.get("inbound_leg_id") == inbound_leg_id
    )
    if (
        not (valid_answer or valid_deferred or valid_pending)
        or result.get("ok") is not True
        or result.get("exchange_id") != exchange_id
        or not isinstance(result.get("inbound_leg_id"), str)
    ):
        raise ChatsCLIError("AgentsServer returned an invalid live response")
    if valid_pending:
        return {**result, "live_response_lease_id": lease_id}
    return result


def wait(args: argparse.Namespace) -> dict[str, Any]:
    """Observe one bounded slice of an already-committed live exchange."""

    capability = authority(args.authority_file)
    return await_live_response(
        {
            "exchange_id": args.exchange,
            "inbound_leg_id": args.inbound_leg,
            "live_response_lease_id": args.lease,
        },
        capability,
        int(getattr(args, "timeout_seconds", LIVE_RESPONSE_HEARTBEAT_SECONDS)),
    )


def list_routes(args: argparse.Namespace) -> dict[str, Any]:
    capability = authority(args.authority_file)
    cursor = str(getattr(args, "cursor", None) or "")
    if cursor and re.fullmatch(r"route_[0-9a-f]{32}", cursor) is None:
        raise ChatsCLIError("--cursor must be the previous route page's next_cursor")
    path = "/api/agent/cross-chat/routes"
    if cursor:
        path += "?" + urllib.parse.urlencode({"cursor": cursor})
    result = get_json(path, capability)
    routes = result.get("routes")
    if not isinstance(routes, list) or any(
        not isinstance(route, dict) for route in routes
    ):
        raise ChatsCLIError("AgentsServer returned an invalid route list")
    next_cursor = result.get("next_cursor")
    if next_cursor is not None and (
        not isinstance(next_cursor, str)
        or re.fullmatch(r"route_[0-9a-f]{32}", next_cursor) is None
        or next_cursor == cursor
    ):
        raise ChatsCLIError("AgentsServer returned an invalid route cursor")
    if cursor and "next_cursor" not in result:
        raise ChatsCLIError("this AgentsServer does not support paginated route discovery")
    return result


def negotiated_route_mode(capability: str, route_id: str, requested: str = "") -> str:
    """Discover mode through a read before sending any state-changing request."""

    # Older servers ignore this additive query and return their complete route
    # list; keep exact filtering for both contracts. New servers return only
    # the requested live route, including routes beyond the first list page.
    response = get_json(
        "/api/agent/cross-chat/routes?" + urllib.parse.urlencode({"route_id": route_id}),
        capability,
    )
    routes = response.get("routes")
    if not isinstance(routes, list):
        raise ChatsCLIError("AgentsServer returned an invalid route list")
    matches = [route for route in routes if isinstance(route, dict)
               and route.get("route_id") == route_id]
    if len(matches) != 1 or matches[0].get("available") is not True:
        raise ChatsCLIError("the requested route is unavailable")
    mode = str(matches[0].get("mode") or "")
    if mode not in {"", "async_route_v1"} or (requested and mode != requested):
        raise ChatsCLIError("AgentsServer did not negotiate the requested conversation mode")
    return mode


def send_action(args: argparse.Namespace, action: str) -> dict[str, Any]:
    capability = authority(args.authority_file)
    message = str(args.message or "").strip()
    if not message:
        raise ChatsCLIError("--message must not be empty")
    route = str(getattr(args, "route", None) or "")
    target = str(getattr(args, "target", None) or "")
    target_index = getattr(args, "target_index", None)
    if sum((bool(route), bool(target), target_index is not None)) != 1:
        raise ChatsCLIError(
            "provide exactly one of --route, --target, or --target-index"
        )
    if target_index is not None:
        if bool(getattr(args, "async_response", False)):
            raise ChatsCLIError(
                "--async-response is selected by the live @Chat grant"
            )
        target, grant_is_async = provider_handle(int(target_index), action)
        if action == "request_reply":
            args.async_response = grant_is_async
    destination = route if route else target
    requested_mode = str(getattr(args, "mode", None) or "")
    if requested_mode and not route:
        raise ChatsCLIError("conversation mode requires an exact route")
    discover_mode = bool(requested_mode) or (
        _bounded_runtime_value("AGENTSDOCK_CROSS_CHAT_MODE") == "async_route_v1"
    )
    mode = negotiated_route_mode(capability, route, requested_mode) if route and discover_mode else ""
    if mode == "async_route_v1":
        # Ask is an explicitly sent question in this mode. Any response is a
        # separate message, so neither alias opens a legacy exchange or wait.
        action = "instruction"
    live_wait = (
        action == "request_reply"
        and mode != "async_route_v1"
        and not bool(getattr(args, "async_response", False))
    )
    stable_key = "cli_" + hashlib.sha256(
        (
            f"{capability}\0{action}\0"
            f"{'route' if route else 'target'}\0{destination}\0"
            f"{int(live_wait)}\0{message}"
        ).encode("utf-8")
    ).hexdigest()
    payload: dict[str, Any] = {
        "action": action,
        "body": message,
        "idempotency_key": args.idempotency_key or stable_key,
        "artifact_grants": [],
    }
    if mode:
        payload["mode"] = mode
    if live_wait:
        heartbeat_seconds = live_response_heartbeat_seconds(
            int(getattr(
                args,
                "timeout_seconds",
                LIVE_RESPONSE_HEARTBEAT_SECONDS,
            ))
        )
        payload["wait_for_response"] = True
        payload["response_timeout_seconds"] = heartbeat_seconds
    if route:
        path = (
            "/api/agent/cross-chat/routes/"
            f"{urllib.parse.quote(route, safe='')}/handoffs"
        )
    else:
        path = "/api/agent/cross-chat/handoffs"
        payload["target_session_id"] = target
    result = post_json(path, payload, capability)
    if mode == "async_route_v1":
        if (set(result) != {"ok", "route_id", "action", "accepted", "mode", "message_id", "duplicate"}
                or result.get("ok") is not True or result.get("accepted") is not True
                or result.get("route_id") != route or result.get("action") != "instruction"
                or result.get("mode") != mode or not isinstance(result.get("duplicate"), bool)
                or re.fullmatch(r"handoff_[0-9a-f]{32}", str(result.get("message_id") or "")) is None):
            raise ChatsCLIError("AgentsServer returned an invalid asynchronous message receipt")
        return result
    minimal_expected = {"ok", "action", "accepted"}
    if route:
        minimal_expected.add("route_id")
    expected = set(minimal_expected)
    deferred_expected = set(minimal_expected)
    wait_expected = set(minimal_expected)
    pending_expected = set(minimal_expected)
    if live_wait:
        expected.update({
            "exchange_id",
            "inbound_leg_id",
            "body",
            "request_response",
        })
        wait_expected.update({
            "exchange_id",
            "inbound_leg_id",
            "live_response_lease_id",
        })
        pending_expected.update({
            "exchange_id",
            "inbound_leg_id",
            "live_response_lease_id",
            "pending",
        })
        deferred_expected.update({
            "exchange_id",
            "inbound_leg_id",
            "deferred",
            "delivery",
            "message",
        })
        if frozenset(result) == frozenset(wait_expected):
            result = {
                **{key: result[key] for key in minimal_expected},
                "exchange_id": result["exchange_id"],
                "inbound_leg_id": result["inbound_leg_id"],
                "live_response_lease_id": result["live_response_lease_id"],
                "pending": True,
            }
        elif frozenset(result) == frozenset(minimal_expected):
            raise ChatsCLIError(
                "AgentsServer does not support a live response for this route"
            )
    has_live_response = live_wait and frozenset(result) == frozenset(expected)
    has_deferred_response = (
        live_wait and frozenset(result) == frozenset(deferred_expected)
    )
    has_pending_response = (
        live_wait and frozenset(result) == frozenset(pending_expected)
    )
    if route:
        if (
            frozenset(result) not in {
                frozenset(minimal_expected),
                frozenset(expected),
                frozenset(deferred_expected),
                frozenset(pending_expected),
            }
            or result.get("ok") is not True
            or result.get("route_id") != route
            or result.get("action") != action
            or result.get("accepted") is not True
            or (has_live_response and not isinstance(result.get("body"), str))
            or (
                has_pending_response
                and (
                    result.get("pending") is not True
                    or not isinstance(result.get("live_response_lease_id"), str)
                )
            )
            or (
                has_deferred_response
                and (
                    result.get("deferred") is not True
                    or result.get("delivery") != "asynchronous"
                )
            )
        ):
            raise ChatsCLIError("AgentsServer returned an invalid route handoff response")
    else:
        if (
            frozenset(result) not in {
                frozenset(minimal_expected),
                frozenset(expected),
                frozenset(deferred_expected),
                frozenset(pending_expected),
            }
            or result.get("ok") is not True
            or result.get("action") != action
            or result.get("accepted") is not True
            or (has_live_response and not isinstance(result.get("body"), str))
            or (
                has_pending_response
                and (
                    result.get("pending") is not True
                    or not isinstance(result.get("live_response_lease_id"), str)
                )
            )
            or (
                has_deferred_response
                and (
                    result.get("deferred") is not True
                    or result.get("delivery") != "asynchronous"
                )
            )
        ):
            raise ChatsCLIError("AgentsServer returned an invalid direct handoff response")
    return result


def send(args: argparse.Namespace) -> dict[str, Any]:
    return send_action(args, "instruction")


def ask(args: argparse.Namespace) -> dict[str, Any]:
    return send_action(args, "request_reply")


def respond(args: argparse.Namespace) -> dict[str, Any]:
    capability = authority(args.authority_file)
    message = str(args.message or "").strip()
    if not message:
        raise ChatsCLIError("--message must not be empty")
    request_response = bool(args.request_response)
    async_response = bool(getattr(args, "async_response", False))
    if async_response and not request_response:
        raise ChatsCLIError("--async-response requires --request-response")
    live_wait = request_response and not async_response
    stable_key = "cli_" + hashlib.sha256(
        (
            f"{capability}\0respond\0{args.exchange}\0{args.inbound_leg}\0"
            f"{int(request_response)}\0{int(live_wait)}\0{message}"
        ).encode("utf-8")
    ).hexdigest()
    payload = {
        "inbound_leg_id": args.inbound_leg,
        "body": message,
        "request_response": request_response,
        "idempotency_key": args.idempotency_key or stable_key,
        "artifact_grants": [],
    }
    if live_wait:
        heartbeat_seconds = live_response_heartbeat_seconds(
            int(getattr(
                args,
                "timeout_seconds",
                LIVE_RESPONSE_HEARTBEAT_SECONDS,
            ))
        )
        payload["wait_for_response"] = True
        payload["response_timeout_seconds"] = heartbeat_seconds
    result = post_json(
        f"/api/agent/cross-chat/exchanges/{urllib.parse.quote(args.exchange, safe='')}/responses",
        payload,
        capability,
    )
    minimal_expected = {"ok", "action", "accepted"}
    expected = set(minimal_expected)
    deferred_expected = set(minimal_expected)
    wait_expected = set(minimal_expected)
    pending_expected = set(minimal_expected)
    if live_wait:
        expected.update({
            "exchange_id",
            "inbound_leg_id",
            "body",
            "request_response",
        })
        wait_expected.update({
            "exchange_id",
            "inbound_leg_id",
            "live_response_lease_id",
        })
        pending_expected.update({
            "exchange_id",
            "inbound_leg_id",
            "live_response_lease_id",
            "pending",
        })
        deferred_expected.update({
            "exchange_id",
            "inbound_leg_id",
            "deferred",
            "delivery",
            "message",
        })
        if frozenset(result) == frozenset(wait_expected):
            result = {
                **{key: result[key] for key in minimal_expected},
                "exchange_id": result["exchange_id"],
                "inbound_leg_id": result["inbound_leg_id"],
                "live_response_lease_id": result["live_response_lease_id"],
                "pending": True,
            }
        elif frozenset(result) == frozenset(minimal_expected):
            raise ChatsCLIError(
                "AgentsServer does not support a live follow-up response"
            )
    has_live_response = live_wait and frozenset(result) == frozenset(expected)
    has_deferred_response = (
        live_wait and frozenset(result) == frozenset(deferred_expected)
    )
    has_pending_response = (
        live_wait and frozenset(result) == frozenset(pending_expected)
    )
    if (
        frozenset(result) not in {
            frozenset(minimal_expected),
            frozenset(expected),
            frozenset(deferred_expected),
            frozenset(pending_expected),
        }
        or result.get("ok") is not True
        or result.get("action") != "response"
        or result.get("accepted") is not True
        or (has_live_response and not isinstance(result.get("body"), str))
        or (
            has_pending_response
            and (
                result.get("pending") is not True
                or not isinstance(result.get("live_response_lease_id"), str)
            )
        )
        or (
            has_deferred_response
            and (
                result.get("deferred") is not True
                or result.get("delivery") != "asynchronous"
            )
        )
    ):
        raise ChatsCLIError(
            "AgentsServer returned an invalid cross-chat response"
        )
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Contact an eligible chat on this AgentsDock server.",
        allow_abbrev=False,
    )
    root.add_argument(
        "--authority-file",
        help=(
            "mode-0600 per-run authority file; defaults to the live provider "
            "environment"
        ),
    )
    commands = root.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser(
        "list",
        help="list one page of eligible same-server chats for this live run",
        allow_abbrev=False,
    )
    list_command.add_argument(
        "--cursor",
        help="continue listing with the previous response's non-null next_cursor",
    )
    list_command.set_defaults(handler=list_routes)
    command = commands.add_parser(
        "send",
        help="send one authorized instruction",
        allow_abbrev=False,
    )
    send_destination = command.add_mutually_exclusive_group(required=True)
    send_destination.add_argument("--route")
    send_destination.add_argument("--target")
    send_destination.add_argument("--target-index", type=positive_target_index)
    command.add_argument("--message", required=True)
    command.add_argument("--idempotency-key")
    command.add_argument("--mode", choices=["async_route_v1"])
    command.set_defaults(handler=send)
    ask_command = commands.add_parser(
        "ask",
        help="ask a same-server agent and wait until it answers or is stopped",
        allow_abbrev=False,
    )
    ask_destination = ask_command.add_mutually_exclusive_group(required=True)
    ask_destination.add_argument("--route")
    ask_destination.add_argument("--target")
    ask_destination.add_argument("--target-index", type=positive_target_index)
    ask_command.add_argument("--message", required=True)
    ask_command.add_argument("--idempotency-key")
    ask_command.add_argument("--mode", choices=["async_route_v1"])
    ask_command.add_argument(
        "--async-response",
        action="store_true",
        help=(
            "return after durable send and receive the peer reply in a later "
            "turn (required for secure-peer routes)"
        ),
    )
    ask_command.add_argument(
        "--timeout-seconds",
        type=int,
        choices=range(1, 3601),
        default=LIVE_RESPONSE_HEARTBEAT_SECONDS,
        help=(
            "deprecated compatibility value; live same-server waits have no "
            "response deadline"
        ),
    )
    ask_command.set_defaults(handler=ask)
    response_command = commands.add_parser(
        "respond",
        help="respond to the exact inbound exchange leg",
        allow_abbrev=False,
    )
    response_command.add_argument("--exchange", required=True)
    response_command.add_argument("--inbound-leg", required=True)
    response_command.add_argument("--message", required=True)
    response_command.add_argument("--request-response", action="store_true")
    response_command.add_argument(
        "--async-response",
        action="store_true",
        help=(
            "with --request-response, receive the peer reply in a later turn "
            "instead of waiting on this provider call"
        ),
    )
    response_command.add_argument("--idempotency-key")
    response_command.add_argument(
        "--timeout-seconds",
        type=int,
        choices=range(1, 3601),
        default=LIVE_RESPONSE_HEARTBEAT_SECONDS,
        help=(
            "deprecated compatibility value; live same-server waits have no "
            "response deadline"
        ),
    )
    response_command.set_defaults(handler=respond)
    current_response_command = commands.add_parser(
        "respond-current",
        help="respond using this run's current inbound reply grant",
        allow_abbrev=False,
    )
    current_response_command.add_argument("--message", required=True)
    current_response_command.add_argument(
        "--request-response",
        action="store_true",
    )
    current_response_command.add_argument("--idempotency-key")
    current_response_command.add_argument(
        "--timeout-seconds",
        type=int,
        choices=range(1, 3601),
        default=LIVE_RESPONSE_HEARTBEAT_SECONDS,
        help=(
            "deprecated compatibility value; live same-server waits have no "
            "response deadline"
        ),
    )
    current_response_command.set_defaults(handler=respond_current)
    wait_command = commands.add_parser(
        "wait",
        help=(
            "observe one bounded foreground slice of a pending same-server "
            "request"
        ),
        allow_abbrev=False,
    )
    wait_command.add_argument("--exchange", required=True)
    wait_command.add_argument("--inbound-leg", required=True)
    wait_command.add_argument("--lease", required=True)
    wait_command.add_argument(
        "--timeout-seconds",
        type=int,
        choices=range(1, 3601),
        default=LIVE_RESPONSE_HEARTBEAT_SECONDS,
        help=(
            "bounded transport slice only; repeat wait after every pending "
            "receipt"
        ),
    )
    wait_command.set_defaults(handler=wait)
    return root


def main(argv: list[str] | None = None) -> int:
    previous_authority_file = os.environ.get(
        "AGENTSDOCK_PROVIDER_AUTHORITY_FILE"
    )
    try:
        args = parser().parse_args(argv)
        selected_authority = _authority_path(args.authority_file)
        os.environ["AGENTSDOCK_PROVIDER_AUTHORITY_FILE"] = str(
            selected_authority
        )
        result = args.handler(args)
        print(json.dumps(result, ensure_ascii=False))
        # A retryable live-response transport failure is structured so the
        # caller retains its exact lease, but it is not a successful pending
        # observation.  Exit nonzero after printing the receipt so automation
        # cannot silently treat network ambiguity as server-owned waiting.
        return 2 if result.get("transport_error") is True else 0
    except ChatsCLIError as exc:
        print(f"agentsdock-chats: {exc}", file=sys.stderr)
        return 2
    finally:
        if previous_authority_file is None:
            os.environ.pop("AGENTSDOCK_PROVIDER_AUTHORITY_FILE", None)
        else:
            os.environ[
                "AGENTSDOCK_PROVIDER_AUTHORITY_FILE"
            ] = previous_authority_file


if __name__ == "__main__":
    raise SystemExit(main())
