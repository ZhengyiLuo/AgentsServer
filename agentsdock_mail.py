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
PROVIDER_RUNTIME_VALUE_MAX_BYTES = 4096


class MailCLIError(RuntimeError):
    pass


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _canonical_http_origin(value: str, label: str) -> tuple[str, bool]:
    raw = value.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port or 80
    except ValueError as exc:
        raise MailCLIError(f"{label} must be an HTTP origin") from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise MailCLIError(f"{label} must be an HTTP origin")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        loopback = address.is_loopback
        host = address.compressed
        url_host = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
    except ValueError:
        loopback = host == "localhost"
        url_host = host
    return f"http://{url_host}:{port}", loopback


def _authority_server_origin(authority_file: str | None) -> str:
    path = _selected_authority_path(authority_file)
    try:
        if path.stat().st_mode & 0o077:
            raise MailCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MailCLIError(f"could not read authority file: {exc}") from exc
    return _bounded_identity_value(
        payload.get("provider_server_origin"),
        "authority provider_server_origin",
    )


def _loopback_server_url() -> str:
    raw_server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip()
    if not raw_server_url:
        raise MailCLIError("missing AgentsDock agent environment")
    server_origin, loopback = _canonical_http_origin(
        raw_server_url,
        "AGENTSDOCK_SERVER_URL",
    )
    runtime_origin = _bounded_identity_value(
        os.environ.get("AGENTSDOCK_PROVIDER_SERVER_ORIGIN"),
        "AGENTSDOCK_PROVIDER_SERVER_ORIGIN",
    )
    if runtime_origin:
        canonical_runtime, _runtime_loopback = _canonical_http_origin(
            runtime_origin,
            "AGENTSDOCK_PROVIDER_SERVER_ORIGIN",
        )
        if canonical_runtime != server_origin:
            raise MailCLIError(
                "AGENTSDOCK_SERVER_URL conflicts with the live provider origin"
            )
    if loopback:
        return raw_server_url.rstrip("/")
    authority_origin = _authority_server_origin(None)
    if not authority_origin:
        raise MailCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    canonical_authority, _authority_loopback = _canonical_http_origin(
        authority_origin,
        "authority provider_server_origin",
    )
    if canonical_authority != server_origin:
        raise MailCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    return server_origin


def _bounded_identity_value(value: str | None, label: str) -> str:
    clean = str(value or "").strip()
    try:
        size = len(clean.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise MailCLIError(f"{label} is not valid UTF-8") from exc
    if size > PROVIDER_RUNTIME_VALUE_MAX_BYTES:
        raise MailCLIError(f"{label} exceeds the provider runtime limit")
    return clean


def _selected_authority_path(authority_file: str | None) -> Path:
    explicit = _bounded_identity_value(authority_file, "--authority-file")
    ambient = _bounded_identity_value(
        os.environ.get("AGENTSDOCK_PROVIDER_AUTHORITY_FILE"),
        "AGENTSDOCK_PROVIDER_AUTHORITY_FILE",
    )
    if explicit and ambient:
        explicit_key = os.path.abspath(os.path.expanduser(explicit))
        ambient_key = os.path.abspath(os.path.expanduser(ambient))
        if explicit_key != ambient_key:
            raise MailCLIError(
                "--authority-file conflicts with the live provider authority"
            )
    selected = explicit or ambient
    if not selected:
        raise MailCLIError("--authority-file is required")
    return Path(selected).expanduser()


def _provider_authority(authority_file: str | None) -> tuple[str, str]:
    path = _selected_authority_path(authority_file)
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
    environment_chat_id = _bounded_identity_value(
        os.environ.get("AGENTSDOCK_CHAT_ID"),
        "AGENTSDOCK_CHAT_ID",
    )
    if environment_chat_id and environment_chat_id != source_session_id:
        raise MailCLIError(
            "AGENTSDOCK_CHAT_ID does not match the authority file"
        )
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
        allow_abbrev=False,
    )
    root.add_argument(
        "--authority-file",
        help="mode-0600 per-run AgentsDock provider authority file",
    )
    commands = root.add_subparsers(dest="command", required=True)
    list_command = commands.add_parser(
        "list", help="list this turn's opaque mail routes", allow_abbrev=False
    )
    list_command.set_defaults(handler=list_routes)
    send_command = commands.add_parser(
        "send",
        help="send one passive mailbox item with its UTF-8 body on stdin",
        allow_abbrev=False,
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
    previous_authority_file = os.environ.get(
        "AGENTSDOCK_PROVIDER_AUTHORITY_FILE"
    )
    try:
        args = parser().parse_args(argv)
        selected_authority = _selected_authority_path(args.authority_file)
        os.environ["AGENTSDOCK_PROVIDER_AUTHORITY_FILE"] = str(
            selected_authority
        )
        result = args.handler(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except MailCLIError as exc:
        print(f"agentsdock-mail: {exc}", file=sys.stderr)
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
