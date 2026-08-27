#!/usr/bin/env python3
"""Raise an explicit emergency alert for the active AgentsDock agent turn."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


class EmergencyCLIError(RuntimeError):
    """A concise, user-facing emergency helper failure."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def clean_emergency_message(value: Any) -> str:
    """Mirror the server's plain-text control and whitespace normalization."""

    characters: list[str] = []
    for character in str(value or "")[: 500 * 4]:
        category = unicodedata.category(character)
        characters.append(" " if category.startswith("C") else character)
    return " ".join("".join(characters).split())


def nonempty_chat_id(value: str) -> str:
    chat_id = value.strip()
    if not chat_id:
        raise argparse.ArgumentTypeError("--chat-id must not be empty")
    return chat_id


def host_is_loopback(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() == "localhost"
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback


def provider_authority(authority_file: str | None) -> tuple[str, str]:
    raw_path = str(
        authority_file
        or os.environ.get("AGENTSDOCK_PROVIDER_AUTHORITY_FILE")
        or ""
    ).strip()
    if not raw_path:
        raise EmergencyCLIError("--authority-file is required")
    path = Path(raw_path).expanduser()
    try:
        if path.stat().st_mode & 0o077:
            raise EmergencyCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmergencyCLIError(f"could not read authority file: {exc}") from exc
    capability = str(
        payload.get("provider_capability") or payload.get("capability") or ""
    )
    chat_id = str(payload.get("source_session_id") or "").strip()
    if not capability or not chat_id:
        raise EmergencyCLIError("authority file is invalid")
    return capability, chat_id


def required_environment(
    authority_file: str | None,
    requested_chat_id: str | None,
) -> tuple[str, str, str]:
    server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip().rstrip("/")
    if not server_url:
        raise EmergencyCLIError("missing agent environment: AGENTSDOCK_SERVER_URL")
    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme != "http" or not parsed.hostname or not host_is_loopback(parsed.hostname):
        raise EmergencyCLIError("AGENTSDOCK_SERVER_URL must be a loopback HTTP URL")
    token, authority_chat_id = provider_authority(authority_file)
    environment_chat_id = os.environ.get("AGENTSDOCK_CHAT_ID", "").strip()
    explicit_chat_id = str(requested_chat_id or environment_chat_id or "").strip()
    if explicit_chat_id and explicit_chat_id != authority_chat_id:
        raise EmergencyCLIError("--chat-id does not match the authority file")
    return server_url, authority_chat_id, token


def raise_alert(
    message: str,
    *,
    authority_file: str | None = None,
    chat_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    clean_message = clean_emergency_message(message)
    if not clean_message:
        raise EmergencyCLIError("--message must not be empty")
    if len(clean_message) > 500:
        raise EmergencyCLIError("--message must be 500 characters or fewer")
    server_url, authority_chat_id, token = required_environment(
        authority_file,
        chat_id,
    )
    request_id = request_id or f"emg_{uuid.uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_id):
        raise EmergencyCLIError("--request-id is invalid")
    body = json.dumps({
        "request_id": request_id,
        "message": clean_message,
    }, separators=(",", ":")).encode("utf-8")
    encoded_chat_id = urllib.parse.quote(authority_chat_id, safe="")
    url = f"{server_url}/api/agent/sessions/{encoded_chat_id}/emergency-alerts"
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    decoded: Any = None
    ambiguous_error: BaseException | None = None
    for attempt in range(2):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-AgentsDock-Provider-Capability": token,
                **({"X-AgentsDock-Emergency-Retry": "1"} if attempt else {}),
            },
            method="POST",
        )
        try:
            with opener.open(request, timeout=30) as response:
                decoded = json.loads(response.read().decode("utf-8"))
            ambiguous_error = None
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("detail") or raw
            except json.JSONDecodeError:
                detail = raw
            raise EmergencyCLIError(
                f"server rejected emergency alert ({exc.code}): {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            ambiguous_error = exc
    if ambiguous_error is not None:
        reason = getattr(ambiguous_error, "reason", ambiguous_error)
        raise EmergencyCLIError(
            f"could not confirm whether the emergency alert was raised: {reason}"
        ) from ambiguous_error
    alert = decoded.get("alert") if isinstance(decoded, dict) else None
    if (
        not isinstance(decoded, dict)
        or decoded.get("ok") is not True
        or decoded.get("chat_id") != authority_chat_id
        or not isinstance(alert, dict)
        or not re.fullmatch(r"emergency_[0-9a-f]{32}", str(alert.get("id") or ""))
        or alert.get("status") != "active"
        or alert.get("severity") != "critical"
        or alert.get("message") != clean_message
        or not isinstance(alert.get("raised_at"), str)
        or not str(alert.get("raised_at") or "").strip()
        or not isinstance(decoded.get("event_id"), str)
        or not str(decoded.get("event_id") or "").strip()
        or not isinstance(decoded.get("event_seq"), int)
        or isinstance(decoded.get("event_seq"), bool)
        or int(decoded["event_seq"]) <= 0
        or not isinstance(decoded.get("unacknowledged_emergency_count"), int)
        or isinstance(decoded.get("unacknowledged_emergency_count"), bool)
        or int(decoded["unacknowledged_emergency_count"]) < 0
    ):
        raise EmergencyCLIError("AgentsServer returned an invalid emergency receipt")
    return decoded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Contact the user about a critical AgentsDock emergency.",
    )
    parser.add_argument(
        "--authority-file",
        help="mode-0600 per-run AgentsDock provider authority file",
    )
    parser.add_argument("--chat-id", type=nonempty_chat_id)
    subparsers = parser.add_subparsers(dest="command", required=True)
    alert = subparsers.add_parser("alert", help="raise a critical alert")
    alert.add_argument("--message", required=True)
    alert.add_argument("--request-id", help="idempotency key (normally generated)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = raise_alert(
            args.message,
            authority_file=args.authority_file,
            chat_id=args.chat_id,
            request_id=args.request_id,
        )
    except EmergencyCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
