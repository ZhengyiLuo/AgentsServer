#!/usr/bin/env python3
"""Send a run-scoped notification whose level is chosen by the agent."""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from agentsdock_emergency import (
    EmergencyCLIError,
    NoRedirectHandler,
    clean_emergency_message,
    nonempty_chat_id,
    required_environment,
)


def send_notification(
    level: str,
    message: str,
    *,
    authority_file: str | None = None,
    chat_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    clean_message = clean_emergency_message(message)
    if not clean_message or len(clean_message) > 500:
        raise EmergencyCLIError("--message must contain 1 to 500 characters")
    if level not in {"normal", "important", "emergency"}:
        raise EmergencyCLIError("--level must be normal, important, or emergency")
    server_url, authority_chat_id, token = required_environment(authority_file, chat_id)
    request_id = request_id or f"note_{uuid.uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_id):
        raise EmergencyCLIError("--request-id is invalid")
    body = json.dumps({"request_id": request_id, "level": level, "message": clean_message},
                      separators=(",", ":")).encode("utf-8")
    url = f"{server_url}/api/agent/sessions/{urllib.parse.quote(authority_chat_id, safe='')}/notifications"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirectHandler())
    decoded: Any = None
    ambiguous_error: BaseException | None = None
    for attempt in range(2):
        request = urllib.request.Request(
            url, data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-AgentsDock-Provider-Capability": token,
                **({"X-AgentsDock-Notification-Retry": "1"} if attempt else {}),
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
                f"server rejected notification ({exc.code}): {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            ambiguous_error = exc
    if ambiguous_error is not None:
        reason = getattr(ambiguous_error, "reason", ambiguous_error)
        raise EmergencyCLIError(f"could not confirm whether the notification was sent: {reason}") from ambiguous_error
    if (
        not isinstance(decoded, dict)
        or decoded.get("ok") is not True
        or decoded.get("chat_id") != authority_chat_id
        or decoded.get("level") != level
        or not isinstance(decoded.get("event_id"), str)
        or not decoded["event_id"]
        or not isinstance(decoded.get("event_seq"), int)
        or isinstance(decoded["event_seq"], bool)
        or decoded["event_seq"] <= 0
        or not isinstance(decoded.get("duplicate"), bool)
    ):
        raise EmergencyCLIError("AgentsServer returned an invalid notification receipt")
    return decoded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Notify the user from the current AgentsDock turn", allow_abbrev=False)
    parser.add_argument("--authority-file")
    parser.add_argument("--chat-id", type=nonempty_chat_id)
    commands = parser.add_subparsers(dest="command", required=True)
    send = commands.add_parser("send", allow_abbrev=False)
    send.add_argument("--level", required=True, choices=("normal", "important", "emergency"))
    send.add_argument("--message", required=True)
    send.add_argument("--request-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = send_notification(
            args.level, args.message,
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
