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


def send(args: argparse.Namespace) -> dict[str, Any]:
    server_url = environment()
    capability = authority(args.authority_file)
    message = str(args.message or "").strip()
    if not message:
        raise ChatsCLIError("--message must not be empty")
    stable_key = "cli_" + hashlib.sha256(
        f"{capability}\0{args.target}\0{message}".encode("utf-8")
    ).hexdigest()
    payload = {
        "target_session_id": args.target,
        "action": "instruction",
        "body": message,
        "idempotency_key": args.idempotency_key or stable_key,
        "artifact_grants": [],
    }
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-AgentsDock-Cross-Chat-Capability": capability,
        "X-AgentsDock-Provider-Capability": capability,
    }
    request = urllib.request.Request(
        f"{server_url}/api/agent/cross-chat/handoffs",
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
    if not isinstance(result, dict) or not isinstance(result.get("handoff"), dict):
        raise ChatsCLIError("AgentsServer returned an invalid handoff response")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Send a user-authorized message to another AgentsDock chat.")
    root.add_argument("--authority-file", required=True)
    commands = root.add_subparsers(dest="command", required=True)
    command = commands.add_parser("send", help="send one authorized instruction")
    command.add_argument("--target", required=True)
    command.add_argument("--message", required=True)
    command.add_argument("--idempotency-key")
    command.set_defaults(handler=send)
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
