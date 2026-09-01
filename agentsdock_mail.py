#!/usr/bin/env python3
"""Capability-scoped Team Network mail CLI for AgentsDock agents."""

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


MAIL_BODY_MAX_BYTES = 8_192


class MailCLIError(RuntimeError):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _loopback_server_url() -> str:
    server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip().rstrip("/")
    if not server_url:
        raise MailCLIError("missing AgentsDock agent environment")
    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise MailCLIError("AGENTSDOCK_SERVER_URL must be a loopback HTTP URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        loopback = address.is_loopback
    except ValueError:
        loopback = parsed.hostname.lower() == "localhost"
    if not loopback:
        raise MailCLIError("refusing to send provider authority to a non-loopback server")
    return server_url


def _provider_authority(authority_file: str | None) -> tuple[str, str]:
    raw_path = str(
        authority_file
        or os.environ.get("AGENTSDOCK_PROVIDER_AUTHORITY_FILE")
        or ""
    ).strip()
    if not raw_path:
        raise MailCLIError("--authority-file is required")
    path = Path(raw_path).expanduser()
    try:
        if path.stat().st_mode & 0o077:
            raise MailCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailCLIError(f"could not read authority file: {exc}") from exc
    capability = str(payload.get("provider_capability") or payload.get("capability") or "")
    source_session_id = str(payload.get("source_session_id") or "").strip()
    if not capability or not source_session_id:
        raise MailCLIError("authority file is invalid")
    return capability, source_session_id


def _request_json(
    method: str,
    path: str,
    capability: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{_loopback_server_url()}{path}",
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-AgentsDock-Provider-Capability": capability,
        },
        method=method,
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
        raise MailCLIError(
            f"server rejected Team Network mail ({exc.code}): {detail or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MailCLIError(
            f"could not reach AgentsServer: {getattr(exc, 'reason', exc)}"
        ) from exc
    if not isinstance(result, dict):
        raise MailCLIError("AgentsServer returned an invalid response")
    return result


def list_routes(args: argparse.Namespace) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    result = _request_json("GET", "/api/agent/team-mail/routes", capability)
    routes = result.get("routes")
    if (
        not isinstance(routes, list)
        or any(not isinstance(route, dict) for route in routes)
    ):
        raise MailCLIError("AgentsServer returned an invalid Team Network mail route list")
    return result


def send(args: argparse.Namespace) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    if sys.stdin.isatty():
        raise MailCLIError("Team Network mail body must be provided on stdin")
    input_stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw_message = input_stream.read(MAIL_BODY_MAX_BYTES + 1)
    if isinstance(raw_message, str):
        raw_message = raw_message.encode("utf-8")
    if len(raw_message) > MAIL_BODY_MAX_BYTES:
        raise MailCLIError("Team Network mail body exceeds the configured size limit")
    try:
        message = raw_message.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise MailCLIError("Team Network mail body must be valid UTF-8") from exc
    if not message:
        raise MailCLIError("Team Network mail body on stdin must not be empty")
    route_id = str(args.route or "").strip()
    kind = str(args.kind or "message")
    if kind != "message":
        raise MailCLIError("AgentsDock agent mail permits messages only")
    stable_key = "mail_cli_" + hashlib.sha256(
        f"{capability}\0{route_id}\0{kind}\0{message}".encode("utf-8")
    ).hexdigest()
    result = _request_json(
        "POST",
        f"/api/agent/team-mail/routes/{urllib.parse.quote(route_id, safe='')}",
        capability,
        {
            "kind": kind,
            "message": message,
            "idempotency_key": args.idempotency_key or stable_key,
        },
    )
    if set(result) != {"ok", "route_id", "kind", "accepted", "duplicate"}:
        raise MailCLIError("AgentsServer returned an invalid Team Network mail receipt")
    if (
        result.get("ok") is not True
        or result.get("route_id") != route_id
        or result.get("kind") != kind
        or result.get("accepted") is not True
        or type(result.get("duplicate")) is not bool
    ):
        raise MailCLIError("AgentsServer returned an invalid Team Network mail receipt")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Send passive Team Network mail using this live agent turn.",
    )
    root.add_argument(
        "--authority-file",
        help="mode-0600 per-run AgentsDock provider authority file",
    )
    commands = root.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser("list", help="list this turn's opaque mail routes")
    list_command.set_defaults(handler=list_routes)
    send_command = commands.add_parser(
        "send",
        help="send one passive mailbox item with its UTF-8 body on stdin",
    )
    send_command.add_argument("--route", required=True, help="opaque route from list")
    send_command.add_argument(
        "--kind",
        choices=("message",),
        default="message",
    )
    send_command.add_argument("--idempotency-key", help=argparse.SUPPRESS)
    send_command.set_defaults(handler=send)
    return root


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        result = args.handler(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except MailCLIError as exc:
        print(f"agentsdock-mail: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
