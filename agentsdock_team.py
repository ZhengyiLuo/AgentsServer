#!/usr/bin/env python3
"""Capability-scoped Team Network helper for AgentsDock agents.

Read commands (inbox, feed, sent, read, skills, skill get) are available on
every ordinary turn.  ``routes`` and ``send`` exist only when the user
mentioned Team Network recipients with ``@@`` on this turn; the server freezes
those recipients into opaque per-run routes.  Message bodies arrive on stdin so
they never appear in process arguments.
"""

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


BODY_MAX_BYTES = 49_152
ATTACHMENT_MAX_COUNT = 16
PROVIDER_RUNTIME_VALUE_MAX_BYTES = 4096


class TeamCLIError(RuntimeError):
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
        raise TeamCLIError(f"{label} must be an HTTP origin") from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise TeamCLIError(f"{label} must be an HTTP origin")
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
            raise TeamCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TeamCLIError(f"could not read authority file: {exc}") from exc
    return _bounded_identity_value(
        payload.get("provider_server_origin"),
        "authority provider_server_origin",
    )


def _loopback_server_url() -> str:
    raw_server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip()
    if not raw_server_url:
        raise TeamCLIError("missing AgentsDock agent environment")
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
            raise TeamCLIError(
                "AGENTSDOCK_SERVER_URL conflicts with the live provider origin"
            )
    if loopback:
        return raw_server_url.rstrip("/")
    authority_origin = _authority_server_origin(None)
    if not authority_origin:
        raise TeamCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    canonical_authority, _authority_loopback = _canonical_http_origin(
        authority_origin,
        "authority provider_server_origin",
    )
    if canonical_authority != server_origin:
        raise TeamCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    return server_origin


def _bounded_identity_value(value: str | None, label: str) -> str:
    clean = str(value or "").strip()
    try:
        size = len(clean.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise TeamCLIError(f"{label} is not valid UTF-8") from exc
    if size > PROVIDER_RUNTIME_VALUE_MAX_BYTES:
        raise TeamCLIError(f"{label} exceeds the provider runtime limit")
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
            raise TeamCLIError(
                "--authority-file conflicts with the live provider authority"
            )
    selected = explicit or ambient
    if not selected:
        raise TeamCLIError("--authority-file is required")
    return Path(selected).expanduser()


def _provider_authority(authority_file: str | None) -> tuple[str, str]:
    path = _selected_authority_path(authority_file)
    try:
        if path.stat().st_mode & 0o077:
            raise TeamCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TeamCLIError(f"could not read authority file: {exc}") from exc
    capability = str(payload.get("provider_capability") or payload.get("capability") or "")
    source_session_id = str(payload.get("source_session_id") or "").strip()
    if not capability or not source_session_id:
        raise TeamCLIError("authority file is invalid")
    environment_chat_id = _bounded_identity_value(
        os.environ.get("AGENTSDOCK_CHAT_ID"),
        "AGENTSDOCK_CHAT_ID",
    )
    if environment_chat_id and environment_chat_id != source_session_id:
        raise TeamCLIError(
            "AGENTSDOCK_CHAT_ID does not match the authority file"
        )
    return capability, source_session_id


def _request_json(
    method: str,
    path: str,
    capability: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 60.0,
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
        with opener.open(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail") or raw
        except (json.JSONDecodeError, AttributeError):
            detail = raw
        raise TeamCLIError(
            f"server rejected Team Network request ({exc.code}): {detail or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TeamCLIError(
            f"could not reach AgentsServer: {getattr(exc, 'reason', exc)}"
        ) from exc
    if not isinstance(result, dict):
        raise TeamCLIError("AgentsServer returned an invalid response")
    return result


def _query(params: dict[str, Any]) -> str:
    clean = {key: value for key, value in params.items() if value not in (None, "", False)}
    if not clean:
        return ""
    return "?" + urllib.parse.urlencode(
        {key: ("1" if value is True else str(value)) for key, value in clean.items()}
    )


def _filter_by_sender(items: list[dict[str, Any]], name: str | None) -> list[dict[str, Any]]:
    if not name:
        return items
    needle = name.strip().casefold()
    return [
        item
        for item in items
        if needle in str((item.get("sender") or {}).get("display_name") or "").casefold()
    ]


def list_box(args: argparse.Namespace, box: str) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    result = _request_json(
        "GET",
        "/api/agent/team/messages"
        + _query(
            {
                "box": box,
                "unread": getattr(args, "unread", False),
                "since": getattr(args, "since", None),
                "after_sequence": getattr(args, "after", None),
                "limit": args.limit,
                "team": getattr(args, "team", None),
            }
        ),
        capability,
    )
    messages = result.get("messages")
    if not isinstance(messages, list):
        raise TeamCLIError("AgentsServer returned an invalid message list")
    result["messages"] = _filter_by_sender(messages, getattr(args, "from_name", None))
    return result


def inbox(args: argparse.Namespace) -> dict[str, Any]:
    return list_box(args, "inbox")


def feed(args: argparse.Namespace) -> dict[str, Any]:
    return list_box(args, "feed")


def sent(args: argparse.Namespace) -> dict[str, Any]:
    return list_box(args, "sent")


def read(args: argparse.Namespace) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    message_id = str(args.message_id or "").strip()
    if not message_id:
        raise TeamCLIError("MESSAGE_ID is required")
    return _request_json(
        "GET",
        f"/api/agent/team/messages/{urllib.parse.quote(message_id, safe='')}"
        + _query({"download": bool(args.download), "team": getattr(args, "team", None)}),
        capability,
        timeout=600.0 if args.download else 60.0,
    )


def skills(args: argparse.Namespace) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    return _request_json(
        "GET",
        "/api/agent/team/skills"
        + _query(
            {
                "include_archived": bool(args.include_archived),
                "team": getattr(args, "team", None),
            }
        ),
        capability,
    )


def skill_get(args: argparse.Namespace) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    slug = str(args.slug or "").strip().lower()
    if not slug:
        raise TeamCLIError("SLUG is required")
    return _request_json(
        "GET",
        f"/api/agent/team/skills/{urllib.parse.quote(slug, safe='')}"
        + _query(
            {
                "version": args.version,
                "download": bool(args.download),
                "team": getattr(args, "team", None),
            }
        ),
        capability,
        timeout=600.0 if args.download else 60.0,
    )


def routes(args: argparse.Namespace) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    result = _request_json("GET", "/api/agent/team/routes", capability)
    if not isinstance(result.get("routes"), list):
        raise TeamCLIError("AgentsServer returned an invalid route list")
    return result


def _read_body() -> str:
    if sys.stdin.isatty():
        raise TeamCLIError("the message body must be provided on stdin")
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    raw = stream.read(BODY_MAX_BYTES + 1)
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > BODY_MAX_BYTES:
        raise TeamCLIError(f"the message body exceeds {BODY_MAX_BYTES} bytes")
    try:
        body = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise TeamCLIError("the message body must be valid UTF-8") from exc
    if not body:
        raise TeamCLIError("the message body on stdin must not be empty")
    return body


def _attachment_paths(values: list[str]) -> list[str]:
    paths: list[str] = []
    for value in values:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise TeamCLIError(f"attachment paths must be absolute: {value}")
        if not path.is_file():
            raise TeamCLIError(f"attachment is not a regular file: {value}")
        resolved = str(path.resolve())
        if resolved not in paths:
            paths.append(resolved)
    if len(paths) > ATTACHMENT_MAX_COUNT:
        raise TeamCLIError(f"at most {ATTACHMENT_MAX_COUNT} attachments per message")
    return paths


def send(args: argparse.Namespace) -> dict[str, Any]:
    capability, _session_id = _provider_authority(args.authority_file)
    route_id = str(args.route or "").strip()
    if not route_id:
        raise TeamCLIError("--route is required; run `routes` first")
    kind = str(args.kind or "message")
    if kind not in {"message", "skill"}:
        raise TeamCLIError("--kind must be message or skill")
    if kind == "message" and args.title:
        raise TeamCLIError("--title requires --kind skill")
    body = _read_body()
    attachments = _attachment_paths(list(args.attach or []))
    payload: dict[str, Any] = {
        "kind": kind,
        "body": body,
        "body_format": "markdown",
        "attachments": attachments,
    }
    if args.title:
        payload["title"] = str(args.title).strip()
    if kind == "skill":
        if not args.skill_slug:
            raise TeamCLIError("--skill-slug is required for --kind skill")
        if not args.title:
            raise TeamCLIError("--title is required for --kind skill")
        skill: dict[str, Any] = {"slug": str(args.skill_slug).strip().lower()}
        if args.summary:
            skill["summary"] = str(args.summary).strip()
        if args.tags:
            skill["tags"] = [tag.strip().lower() for tag in str(args.tags).split(",") if tag.strip()]
        if args.change_note:
            skill["change_note"] = str(args.change_note).strip()
        if args.expected_version is not None:
            skill["expected_version"] = int(args.expected_version)
        payload["skill"] = skill
    elif args.skill_slug or args.expected_version is not None:
        raise TeamCLIError("skill options require --kind skill")
    stable_key = "team_cli_" + hashlib.sha256(
        json.dumps(
            [capability, route_id, payload],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    payload["idempotency_key"] = args.idempotency_key or stable_key
    result = _request_json(
        "POST",
        f"/api/agent/team/routes/{urllib.parse.quote(route_id, safe='')}",
        capability,
        payload,
        timeout=900.0,
    )
    required = {"ok", "route_id", "message_id", "kind", "accepted", "duplicate", "attachments"}
    if (
        not required.issubset(result)
        or result.get("ok") is not True
        or result.get("route_id") != route_id
        or result.get("kind") != kind
        or result.get("accepted") is not True
        or type(result.get("duplicate")) is not bool
    ):
        raise TeamCLIError("AgentsServer returned an invalid Team Network send receipt")
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Read Team Network mail and skills, and send to @@-mentioned recipients.",
        allow_abbrev=False,
    )
    root.add_argument(
        "--authority-file",
        help="mode-0600 per-run AgentsDock provider authority file",
    )
    commands = root.add_subparsers(dest="command", required=True)

    def listing(name: str, help_text: str):
        command = commands.add_parser(name, help=help_text, allow_abbrev=False)
        command.add_argument("--limit", type=int, default=20)
        command.add_argument("--after", type=int, default=None, help="sequence cursor")
        command.add_argument("--team", default=None, help="team id when this server is in several teams")
        return command

    inbox_command = listing("inbox", "messages sent to this server")
    inbox_command.add_argument("--unread", action="store_true")
    inbox_command.add_argument("--from", dest="from_name", default=None, help="sender display name filter")
    inbox_command.add_argument("--since", default=None, help="ISO-8601 timestamp or epoch seconds")
    inbox_command.set_defaults(handler=inbox)

    feed_command = listing("feed", "messages sent to the whole team")
    feed_command.add_argument("--from", dest="from_name", default=None)
    feed_command.add_argument("--since", default=None)
    feed_command.set_defaults(handler=feed)

    sent_command = listing("sent", "messages this server sent")
    sent_command.set_defaults(handler=sent)

    read_command = commands.add_parser(
        "read", help="read one message with its attachments", allow_abbrev=False
    )
    read_command.add_argument("message_id")
    read_command.add_argument("--download", action="store_true", help="fetch attachments into the local team cache and print their paths")
    read_command.add_argument("--team", default=None)
    read_command.set_defaults(handler=read)

    skills_command = commands.add_parser(
        "skills", help="list the team Skills library", allow_abbrev=False
    )
    skills_command.add_argument("--include-archived", action="store_true")
    skills_command.add_argument("--team", default=None)
    skills_command.set_defaults(handler=skills)

    skill_command = commands.add_parser(
        "skill", help="skill operations", allow_abbrev=False
    )
    skill_sub = skill_command.add_subparsers(dest="skill_command", required=True)
    skill_get_command = skill_sub.add_parser(
        "get",
        help="read one skill (latest version by default)",
        allow_abbrev=False,
    )
    skill_get_command.add_argument("slug")
    skill_get_command.add_argument("--version", type=int, default=None)
    skill_get_command.add_argument("--download", action="store_true")
    skill_get_command.add_argument("--team", default=None)
    skill_get_command.set_defaults(handler=skill_get)

    routes_command = commands.add_parser(
        "routes",
        help="list this run's @@ recipient routes",
        allow_abbrev=False,
    )
    routes_command.set_defaults(handler=routes)

    send_command = commands.add_parser(
        "send",
        help="send one message to a route; the Markdown body is read from stdin",
        allow_abbrev=False,
    )
    send_command.add_argument("--route", required=True)
    send_command.add_argument("--kind", choices=("message", "skill"), default="message")
    send_command.add_argument("--title", default=None)
    send_command.add_argument("--skill-slug", default=None)
    send_command.add_argument("--summary", default=None)
    send_command.add_argument("--tags", default=None, help="comma-separated tags")
    send_command.add_argument("--change-note", default=None)
    send_command.add_argument("--expected-version", type=int, default=None)
    send_command.add_argument("--attach", action="append", default=[], metavar="/abs/path")
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
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    except TeamCLIError as exc:
        print(f"agentsdock-team: {exc}", file=sys.stderr)
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
