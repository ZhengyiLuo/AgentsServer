#!/usr/bin/env python3
"""Capability-scoped cross-chat handoff CLI for AgentsDock agent turns."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class ChatsCLIError(RuntimeError):
    pass


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


def environment() -> str:
    server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip().rstrip("/")
    if not server_url:
        raise ChatsCLIError("missing AgentsDock agent environment")
    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme != "http" or not parsed.hostname or not host_is_loopback(parsed.hostname):
        raise ChatsCLIError("AGENTSDOCK_SERVER_URL must be a loopback HTTP URL")
    return server_url


def authority(path: str) -> str:
    authority_path = Path(path).expanduser()
    try:
        mode = authority_path.stat().st_mode & 0o777
        if mode & 0o077:
            raise ChatsCLIError("authority file permissions are unsafe")
        payload = json.loads(authority_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChatsCLIError(f"could not read authority file: {exc}") from exc
    token = str(payload.get("provider_capability") or payload.get("capability") or "")
    if not token:
        raise ChatsCLIError("authority file is invalid")
    return token


def post_json(
    path: str,
    payload: dict[str, Any],
    capability: str,
) -> dict[str, Any]:
    server_url = environment()
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-AgentsDock-Cross-Chat-Capability": capability,
        "X-AgentsDock-Provider-Capability": capability,
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
    try:
        with opener.open(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail") or raw
        except json.JSONDecodeError:
            detail = raw
        raise ChatsCLIError(f"server rejected handoff ({exc.code}): {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChatsCLIError(f"could not reach AgentsServer: {getattr(exc, 'reason', exc)}") from exc
    if not isinstance(result, dict):
        raise ChatsCLIError("AgentsServer returned an invalid response")
    return result


def get_json(path: str, capability: str) -> dict[str, Any]:
    server_url = environment()
    request = urllib.request.Request(
        f"{server_url}{path}",
        headers={
            "Accept": "application/json",
            "X-AgentsDock-Cross-Chat-Capability": capability,
            "X-AgentsDock-Provider-Capability": capability,
        },
        method="GET",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail") or raw
        except json.JSONDecodeError:
            detail = raw
        raise ChatsCLIError(
            f"server rejected route listing ({exc.code}): {detail or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ChatsCLIError(
            f"could not reach AgentsServer: {getattr(exc, 'reason', exc)}"
        ) from exc
    if not isinstance(result, dict):
        raise ChatsCLIError("AgentsServer returned an invalid response")
    return result


def list_routes(args: argparse.Namespace) -> dict[str, Any]:
    capability = authority(args.authority_file)
    result = get_json("/api/agent/cross-chat/routes", capability)
    routes = result.get("routes")
    if not isinstance(routes, list) or any(
        not isinstance(route, dict) for route in routes
    ):
        raise ChatsCLIError("AgentsServer returned an invalid route list")
    return result


def send_action(args: argparse.Namespace, action: str) -> dict[str, Any]:
    capability = authority(args.authority_file)
    message = str(args.message or "").strip()
    if not message:
        raise ChatsCLIError("--message must not be empty")
    route = str(getattr(args, "route", None) or "")
    target = str(getattr(args, "target", None) or "")
    if bool(route) == bool(target):
        raise ChatsCLIError("provide exactly one of --route or --target")
    destination = route if route else target
    stable_key = "cli_" + hashlib.sha256(
        (
            f"{capability}\0{action}\0"
            f"{'route' if route else 'target'}\0{destination}\0{message}"
        ).encode("utf-8")
    ).hexdigest()
    payload: dict[str, Any] = {
        "action": action,
        "body": message,
        "idempotency_key": args.idempotency_key or stable_key,
        "artifact_grants": [],
    }
    if route:
        path = (
            "/api/agent/cross-chat/routes/"
            f"{urllib.parse.quote(route, safe='')}/handoffs"
        )
    else:
        path = "/api/agent/cross-chat/handoffs"
        payload["target_session_id"] = target
    result = post_json(path, payload, capability)
    if route:
        if (
            result.get("ok") is not True
            or result.get("route_id") != route
            or result.get("action") != action
            or result.get("accepted") is not True
        ):
            raise ChatsCLIError("AgentsServer returned an invalid route handoff response")
    else:
        expected = "exchange" if action == "request_reply" else "handoff"
        if not isinstance(result.get(expected), dict):
            raise ChatsCLIError(f"AgentsServer returned an invalid {expected} response")
        if action == "request_reply" and not isinstance(result.get("leg"), dict):
            raise ChatsCLIError("AgentsServer returned an invalid exchange leg response")
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
    stable_key = "cli_" + hashlib.sha256(
        (
            f"{capability}\0respond\0{args.exchange}\0{args.inbound_leg}\0"
            f"{int(bool(args.request_response))}\0{message}"
        ).encode("utf-8")
    ).hexdigest()
    result = post_json(
        f"/api/agent/cross-chat/exchanges/{urllib.parse.quote(args.exchange, safe='')}/responses",
        {
            "inbound_leg_id": args.inbound_leg,
            "body": message,
            "request_response": bool(args.request_response),
            "idempotency_key": args.idempotency_key or stable_key,
            "artifact_grants": [],
        },
        capability,
    )
    if result.get("action") == "response":
        if (
            set(result) != {"ok", "action", "accepted"}
            or result.get("ok") is not True
            or result.get("accepted") is not True
        ):
            raise ChatsCLIError(
                "AgentsServer returned an invalid configured-route response"
            )
        return result
    if not isinstance(result.get("exchange"), dict) or not isinstance(result.get("leg"), dict):
        raise ChatsCLIError("AgentsServer returned an invalid exchange response")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Send a user-authorized message to another AgentsDock chat.")
    root.add_argument("--authority-file", required=True)
    commands = root.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser(
        "list",
        help="list configured routes issued to this live turn",
    )
    list_command.set_defaults(handler=list_routes)
    command = commands.add_parser("send", help="send one authorized instruction")
    send_destination = command.add_mutually_exclusive_group(required=True)
    send_destination.add_argument("--route")
    send_destination.add_argument("--target")
    command.add_argument("--message", required=True)
    command.add_argument("--idempotency-key")
    command.set_defaults(handler=send)
    ask_command = commands.add_parser("ask", help="start one bounded request/reply exchange")
    ask_destination = ask_command.add_mutually_exclusive_group(required=True)
    ask_destination.add_argument("--route")
    ask_destination.add_argument("--target")
    ask_command.add_argument("--message", required=True)
    ask_command.add_argument("--idempotency-key")
    ask_command.set_defaults(handler=ask)
    response_command = commands.add_parser("respond", help="respond to the exact inbound exchange leg")
    response_command.add_argument("--exchange", required=True)
    response_command.add_argument("--inbound-leg", required=True)
    response_command.add_argument("--message", required=True)
    response_command.add_argument("--request-response", action="store_true")
    response_command.add_argument("--idempotency-key")
    response_command.set_defaults(handler=respond)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        print(json.dumps(args.handler(args), ensure_ascii=False))
        return 0
    except ChatsCLIError as exc:
        print(f"agentsdock-chats: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
