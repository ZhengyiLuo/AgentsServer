#!/usr/bin/env python3
"""Chat-scoped scheduled-job CLI for AgentsDock agent turns."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class JobsCLIError(RuntimeError):
    """A safe, user-facing CLI failure."""


def required_environment() -> tuple[str, str, str]:
    server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip().rstrip("/")
    chat_id = os.environ.get("AGENTSDOCK_CHAT_ID", "").strip()
    token = os.environ.get("AGENTSDOCK_AGENT_TOKEN")
    missing = [
        name
        for name, value in (
            ("AGENTSDOCK_SERVER_URL", server_url),
            ("AGENTSDOCK_CHAT_ID", chat_id),
            ("AGENTSDOCK_AGENT_TOKEN", token),
        )
        if value is None or (name != "AGENTSDOCK_AGENT_TOKEN" and not value)
    ]
    if missing:
        raise JobsCLIError(f"missing agent environment: {', '.join(missing)}")
    if not server_url.startswith(("http://", "https://")):
        raise JobsCLIError("AGENTSDOCK_SERVER_URL must be an HTTP(S) URL")
    return server_url, chat_id, token or ""


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    server_url, _chat_id, token = required_environment()
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{server_url}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
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


def scoped_jobs() -> list[dict[str, Any]]:
    _server_url, chat_id, _token = required_environment()
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    response = api_request("GET", f"/api/sessions/{encoded_chat_id}/jobs")
    jobs = response.get("jobs")
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise JobsCLIError("AgentsServer returned an invalid jobs list")
    foreign = [str(job.get("id") or "unknown") for job in jobs if job.get("session_id") != chat_id]
    if foreign:
        raise JobsCLIError("AgentsServer returned jobs outside the active chat scope")
    return jobs


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
    return job


def command_list(_args: argparse.Namespace) -> Any:
    return {"jobs": scoped_jobs()}


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
    }
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    return {"job": checked_job(api_request("POST", f"/api/sessions/{encoded_chat_id}/jobs", payload))}


def command_update(args: argparse.Namespace) -> Any:
    _server_url, chat_id, _token = required_environment()
    current_job = owned_job(args.job_id)
    patch: dict[str, Any] = {}
    for key in ("title", "prompt", "interval_seconds", "rrule", "timezone", "next_run_at", "backend"):
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
    if not patch:
        raise JobsCLIError("update requires at least one changed field")
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    job_id = urllib.parse.quote(args.job_id, safe="")
    return {
        "job": checked_job(
            api_request("PATCH", f"/api/sessions/{encoded_chat_id}/jobs/{job_id}", patch)
        )
    }


def command_delete(args: argparse.Namespace) -> Any:
    _server_url, chat_id, _token = required_environment()
    owned_job(args.job_id)
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    job_id = urllib.parse.quote(args.job_id, safe="")
    response = api_request("DELETE", f"/api/sessions/{encoded_chat_id}/jobs/{job_id}")
    if response.get("deleted") is not True:
        raise JobsCLIError(f"job {args.job_id!r} was not deleted")
    return {"ok": True, "deleted": True, "job_id": args.job_id}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage scheduled jobs for the current AgentsDock chat.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list jobs in the active chat")
    list_parser.set_defaults(handler=command_list)

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
    create_parser.add_argument("--backend", choices=("codex", "claude"))
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
    update_parser.add_argument("--backend", choices=("codex", "claude"))
    loop_group = update_parser.add_mutually_exclusive_group()
    loop_group.add_argument("--loop", dest="loop", action="store_true")
    loop_group.add_argument("--no-loop", dest="loop", action="store_false")
    enabled_group = update_parser.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", dest="enabled", action="store_true")
    enabled_group.add_argument("--disable", dest="enabled", action="store_false")
    update_parser.set_defaults(handler=command_update, loop=None, enabled=None)

    delete_parser = subparsers.add_parser("delete", help="delete a job owned by the active chat")
    delete_parser.add_argument("job_id")
    delete_parser.set_defaults(handler=command_delete)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.handler(args)
    except JobsCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
