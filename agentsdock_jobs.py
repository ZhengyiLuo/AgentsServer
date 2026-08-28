#!/usr/bin/env python3
"""Chat-scoped scheduled-job CLI for AgentsDock agent turns."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class JobsCLIError(RuntimeError):
    """A safe, user-facing CLI failure."""


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


def nonempty_chat_id(value: str) -> str:
    chat_id = value.strip()
    if not chat_id:
        raise argparse.ArgumentTypeError("--chat-id must not be empty")
    return chat_id


def chat_route_selection(value: str) -> dict[str, str]:
    """Parse an opaque live route without accepting an internal chat id."""

    raw = value.strip()
    route_id, separator, action = raw.partition("=")
    if not re.fullmatch(r"route_[0-9a-f]{32}", route_id):
        raise argparse.ArgumentTypeError(
            "--chat-route must use an opaque route ID returned by the chats helper"
        )
    selected_action = action.strip() if separator else "instruction"
    if selected_action not in {"instruction", "request_reply"}:
        raise argparse.ArgumentTypeError(
            "--chat-route action must be instruction or request_reply"
        )
    return {"route_id": route_id, "action": selected_action}


def provider_authority() -> tuple[str, str]:
    raw_path = os.environ.get("AGENTSDOCK_PROVIDER_AUTHORITY_FILE", "").strip()
    if not raw_path:
        raise JobsCLIError("--authority-file is required")
    path = Path(raw_path).expanduser()
    try:
        if path.stat().st_mode & 0o077:
            raise JobsCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JobsCLIError(f"could not read authority file: {exc}") from exc
    capability = str(payload.get("provider_capability") or payload.get("capability") or "")
    source_session_id = str(payload.get("source_session_id") or "").strip()
    if not capability or not source_session_id:
        raise JobsCLIError("authority file is invalid")
    return capability, source_session_id


def required_environment() -> tuple[str, str, str]:
    server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip().rstrip("/")
    explicit_chat_id = os.environ.get("AGENTSDOCK_CHAT_ID", "").strip()
    token, authority_chat_id = provider_authority()
    if explicit_chat_id and explicit_chat_id != authority_chat_id:
        raise JobsCLIError("--chat-id does not match the authority file")
    chat_id = explicit_chat_id or authority_chat_id
    missing = [
        name
        for name, value in (
            ("AGENTSDOCK_SERVER_URL", server_url),
        )
        if value is None or not value
    ]
    if missing:
        raise JobsCLIError(f"missing agent environment: {', '.join(missing)}")
    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme != "http" or not parsed.hostname or not host_is_loopback(parsed.hostname):
        raise JobsCLIError("AGENTSDOCK_SERVER_URL must be a loopback HTTP URL")
    return server_url, chat_id, token


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    server_url, _chat_id, token = required_environment()
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    headers["X-AgentsDock-Provider-Capability"] = token
    request = urllib.request.Request(f"{server_url}{path}", data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    try:
        with opener.open(request, timeout=30) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw).get("detail") or raw
        except json.JSONDecodeError:
            detail = raw
        raise JobsCLIError(f"server rejected request ({exc.code}): {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise JobsCLIError(f"could not reach AgentsServer: {reason}") from exc
    except json.JSONDecodeError as exc:
        raise JobsCLIError("AgentsServer returned invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise JobsCLIError("AgentsServer returned an invalid response")
    return decoded


def safe_job_projection(job: dict[str, Any]) -> dict[str, Any]:
    """Defensively omit exact saved chat references from agent CLI output."""

    projected = dict(job)
    raw_references = projected.pop("chat_references", None)
    if "chat_target_count" not in projected and isinstance(raw_references, list):
        projected["chat_target_count"] = len(raw_references)
    return projected


PROVIDER_PRIVATE_CROSS_CHAT_RUN_FIELDS = {
    "chat_references",
    "target_session_id",
    "source_session_id",
    "source_run_id",
    "authorization_source_run_id",
    "authorization_route_id",
    "cross_chat_envelope_id",
    "cross_chat_exchange_id",
    "cross_chat_exchange_leg_id",
    "exchange_id",
    "exchange_leg_id",
    "inbound_leg_id",
    "envelope_id",
    "leg_id",
    "parent_leg_id",
    "cross_chat_obligation_ids",
    "cross_chat_exchange_ids",
    "cross_chat_direct_message_ids",
    "secure_peer_envelope_id",
    "requester_session_id",
    "responder_session_id",
    "target_chat_id",
}


def contains_private_chat_target_fields(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            key in PROVIDER_PRIVATE_CROSS_CHAT_RUN_FIELDS
            or (
                key == "id"
                and isinstance(nested, str)
                and re.fullmatch(
                    r"(?:handoff|exchange|leg)_[A-Za-z0-9_-]+",
                    nested,
                )
            )
            or contains_private_chat_target_fields(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(contains_private_chat_target_fields(nested) for nested in value)
    return False


def scoped_jobs() -> list[dict[str, Any]]:
    _server_url, chat_id, _token = required_environment()
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    response = api_request("GET", f"/api/agent/sessions/{encoded_chat_id}/jobs")
    jobs = response.get("jobs")
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise JobsCLIError("AgentsServer returned an invalid jobs list")
    foreign = [str(job.get("id") or "unknown") for job in jobs if job.get("session_id") != chat_id]
    if foreign:
        raise JobsCLIError("AgentsServer returned jobs outside the active chat scope")
    return [safe_job_projection(job) for job in jobs]


def owned_job(job_id: str) -> dict[str, Any]:
    for job in scoped_jobs():
        if job.get("id") == job_id:
            return job
    raise JobsCLIError(f"job {job_id!r} does not exist in the active chat")


def checked_job(response: dict[str, Any]) -> dict[str, Any]:
    _server_url, chat_id, _token = required_environment()
    job = response.get("job")
    if not isinstance(job, dict):
        raise JobsCLIError("AgentsServer returned an invalid job")
    if job.get("session_id") != chat_id:
        raise JobsCLIError("AgentsServer returned a job outside the active chat scope")
    return safe_job_projection(job)


def command_list(_args: argparse.Namespace) -> Any:
    return {"jobs": scoped_jobs()}


def command_get(args: argparse.Namespace) -> Any:
    return {"job": owned_job(args.job_id)}


def command_runs(args: argparse.Namespace) -> Any:
    _server_url, chat_id, _token = required_environment()
    owned_job(args.job_id)
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    job_id = urllib.parse.quote(args.job_id, safe="")
    query = urllib.parse.urlencode({
        key: value
        for key, value in (
            ("before_seq", args.before_seq),
            ("limit", args.limit),
        )
        if value is not None
    })
    path = f"/api/agent/sessions/{encoded_chat_id}/jobs/{job_id}/runs"
    if query:
        path += f"?{query}"
    response = api_request("GET", path)
    runs = response.get("runs")
    if not isinstance(runs, list) or not all(
        isinstance(run, dict) for run in runs
    ):
        raise JobsCLIError("AgentsServer returned invalid job history")
    if (
        response.get("session_id") != chat_id
        or response.get("job_id") != args.job_id
    ):
        raise JobsCLIError("AgentsServer returned history outside the active chat scope")
    if contains_private_chat_target_fields(response):
        raise JobsCLIError("AgentsServer returned private chat target data in job history")
    return response


def command_create(args: argparse.Namespace) -> Any:
    _server_url, chat_id, _token = required_environment()
    if args.interval_seconds is None and args.cron is None and args.rrule is None and args.first_run_at is None:
        raise JobsCLIError("create requires --interval-seconds, --cron, --rrule, or --first-run-at")
    if args.interval_seconds is None and args.cron is None and args.rrule is None:
        if args.loop or (args.max_runs is not None and args.max_runs != 1):
            raise JobsCLIError("a one-time --first-run-at job cannot loop or run more than once without a schedule")
    schedule_kind = "cron" if args.cron is not None else "rrule" if args.rrule is not None else "interval"
    payload: dict[str, Any] = {
        "title": args.title,
        "prompt": args.prompt,
        "schedule_kind": schedule_kind,
        "interval_seconds": args.interval_seconds,
        "cron_expression": args.cron,
        "rrule": args.rrule,
        "timezone": args.timezone,
        "first_run_at": args.first_run_at,
        "loop": args.loop,
        "max_runs": args.max_runs,
        "enabled": not args.disabled,
        "backend": args.backend,
        "context_mode": args.context_mode,
        "chat_routes": list(args.chat_route or []),
    }
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    return {"job": checked_job(api_request("POST", f"/api/agent/sessions/{encoded_chat_id}/jobs", payload))}


def command_update(args: argparse.Namespace) -> Any:
    _server_url, chat_id, _token = required_environment()
    current_job = owned_job(args.job_id)
    patch: dict[str, Any] = {}
    for key in (
        "title",
        "prompt",
        "interval_seconds",
        "rrule",
        "timezone",
        "next_run_at",
        "backend",
        "context_mode",
    ):
        value = getattr(args, key)
        if value is not None:
            patch[key] = value
    target_kind = (
        "interval" if args.interval_seconds is not None
        else "cron" if args.cron is not None
        else "rrule" if args.rrule is not None
        else str(current_job.get("schedule_kind") or "interval")
    )
    if args.max_runs is not None:
        patch["max_runs"] = args.max_runs
        if target_kind == "interval":
            patch["loop"] = False
    elif args.unlimited:
        patch["max_runs"] = None
        if target_kind == "interval":
            patch["loop"] = True
    if args.cron is not None:
        patch["cron_expression"] = args.cron
    if args.interval_seconds is not None:
        patch["schedule_kind"] = "interval"
    elif args.cron is not None:
        patch["schedule_kind"] = "cron"
    elif args.rrule is not None:
        patch["schedule_kind"] = "rrule"
    if args.loop is not None:
        patch["loop"] = args.loop
    if args.enabled is not None:
        patch["enabled"] = args.enabled
    if args.chat_route is not None:
        patch["chat_routes"] = list(args.chat_route)
    elif args.clear_chat_routes:
        patch["chat_routes"] = []
    if not patch:
        raise JobsCLIError("update requires at least one changed field")
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    job_id = urllib.parse.quote(args.job_id, safe="")
    return {
        "job": checked_job(
            api_request("PATCH", f"/api/agent/sessions/{encoded_chat_id}/jobs/{job_id}", patch)
        )
    }


def command_delete(args: argparse.Namespace) -> Any:
    _server_url, chat_id, _token = required_environment()
    owned_job(args.job_id)
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    job_id = urllib.parse.quote(args.job_id, safe="")
    response = api_request("DELETE", f"/api/agent/sessions/{encoded_chat_id}/jobs/{job_id}")
    if response.get("deleted") is not True:
        raise JobsCLIError(f"job {args.job_id!r} was not deleted")
    return {"ok": True, "deleted": True, "job_id": args.job_id}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage scheduled jobs for the current AgentsDock chat.",
    )
    parser.add_argument(
        "--authority-file",
        help="mode-0600 per-run AgentsDock provider authority file",
    )
    parser.add_argument(
        "--chat-id",
        type=nonempty_chat_id,
        help="explicit chat scope (defaults to AGENTSDOCK_CHAT_ID)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list jobs in the active chat")
    list_parser.set_defaults(handler=command_list)

    get_parser = subparsers.add_parser("get", help="get one job in the active chat")
    get_parser.add_argument("job_id")
    get_parser.set_defaults(handler=command_get)

    runs_parser = subparsers.add_parser("runs", help="show recent run status for one job")
    runs_parser.add_argument("job_id")
    runs_parser.add_argument("--before-seq", type=int)
    runs_parser.add_argument("--limit", type=int, default=20)
    runs_parser.set_defaults(handler=command_runs)

    create_parser = subparsers.add_parser("create", help="create a job in the active chat")
    create_parser.add_argument("--title", required=True)
    create_parser.add_argument("--prompt", required=True)
    create_schedule = create_parser.add_mutually_exclusive_group()
    create_schedule.add_argument("--interval-seconds", type=int)
    create_schedule.add_argument("--cron", help="cron expression (seconds-first for 6-7 fields)")
    create_schedule.add_argument("--rrule", help="RFC 5545 RRULE property")
    create_parser.add_argument("--timezone", default="UTC", help="IANA timezone (default: UTC)")
    create_parser.add_argument("--first-run-at", help="ISO-8601 timestamp for the first run")
    create_parser.add_argument("--loop", action="store_true", help="repeat at the interval")
    create_parser.add_argument("--max-runs", type=int)
    create_parser.add_argument("--disabled", action="store_true")
    create_parser.add_argument("--backend", choices=("codex", "claude", "cursor"))
    create_parser.add_argument(
        "--context-mode",
        choices=("chat", "standalone"),
        default="chat",
        help="continue in the parent chat (default) or use a fresh provider context",
    )
    create_parser.add_argument(
        "--chat-route",
        action="append",
        type=chat_route_selection,
        metavar="ROUTE_ID[=ACTION]",
        help=(
            "persist an exact chat target from the current chats-helper list; "
            "the prompt must contain that chat's exact @title (repeatable)"
        ),
    )
    create_parser.set_defaults(handler=command_create)

    update_parser = subparsers.add_parser("update", help="update a job owned by the active chat")
    update_parser.add_argument("job_id")
    update_parser.add_argument("--title")
    update_parser.add_argument("--prompt")
    update_schedule = update_parser.add_mutually_exclusive_group()
    update_schedule.add_argument("--interval-seconds", type=int)
    update_schedule.add_argument("--cron", help="cron expression (seconds-first for 6-7 fields)")
    update_schedule.add_argument("--rrule", help="RFC 5545 RRULE property")
    update_parser.add_argument("--timezone", help="IANA timezone")
    update_parser.add_argument("--next-run-at", help="ISO-8601 timestamp for the next run")
    run_limit_group = update_parser.add_mutually_exclusive_group()
    run_limit_group.add_argument("--max-runs", type=int)
    run_limit_group.add_argument("--unlimited", action="store_true", help="clear a finite run limit")
    update_parser.add_argument("--backend", choices=("codex", "claude", "cursor"))
    update_parser.add_argument(
        "--context-mode",
        choices=("chat", "standalone"),
    )
    loop_group = update_parser.add_mutually_exclusive_group()
    loop_group.add_argument("--loop", dest="loop", action="store_true")
    loop_group.add_argument("--no-loop", dest="loop", action="store_false")
    enabled_group = update_parser.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", dest="enabled", action="store_true")
    enabled_group.add_argument("--disable", dest="enabled", action="store_false")
    update_routes = update_parser.add_mutually_exclusive_group()
    update_routes.add_argument(
        "--chat-route",
        action="append",
        type=chat_route_selection,
        metavar="ROUTE_ID[=ACTION]",
        help=(
            "replace persisted chat targets using opaque routes from the "
            "current chats-helper list (repeatable)"
        ),
    )
    update_routes.add_argument(
        "--clear-chat-routes",
        action="store_true",
        help="revoke every persisted chat target from this job",
    )
    update_parser.set_defaults(
        handler=command_update,
        loop=None,
        enabled=None,
        chat_route=None,
        clear_chat_routes=False,
    )

    delete_parser = subparsers.add_parser("delete", help="delete a job owned by the active chat")
    delete_parser.add_argument("job_id")
    delete_parser.set_defaults(handler=command_delete)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.authority_file:
        os.environ["AGENTSDOCK_PROVIDER_AUTHORITY_FILE"] = args.authority_file
    previous_chat_id = os.environ.get("AGENTSDOCK_CHAT_ID")
    if args.chat_id is not None:
        os.environ["AGENTSDOCK_CHAT_ID"] = args.chat_id
    try:
        try:
            result = args.handler(args)
        except JobsCLIError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    finally:
        if args.chat_id is not None:
            if previous_chat_id is None:
                os.environ.pop("AGENTSDOCK_CHAT_ID", None)
            else:
                os.environ["AGENTSDOCK_CHAT_ID"] = previous_chat_id
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
