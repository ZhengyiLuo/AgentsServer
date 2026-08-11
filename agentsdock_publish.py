#!/usr/bin/env python3
"""Publish files to the active AgentsDock chat turn and verify the receipt."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


class PublishCLIError(RuntimeError):
    """A concise user-facing publication failure."""


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the privileged publication request on the validated loopback URL."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


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


def loopback_server_url() -> str:
    server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip().rstrip("/")
    if not server_url:
        raise PublishCLIError("missing agent environment: AGENTSDOCK_SERVER_URL")
    parsed = urllib.parse.urlsplit(server_url)
    if parsed.scheme != "http" or not parsed.hostname:
        raise PublishCLIError("AGENTSDOCK_SERVER_URL must be a loopback HTTP URL")
    if not host_is_loopback(parsed.hostname):
        raise PublishCLIError("refusing to send provider authority to a non-loopback server")
    return server_url


def provider_authority(authority_file: str | None) -> tuple[str, str]:
    raw_path = str(authority_file or os.environ.get("AGENTSDOCK_PROVIDER_AUTHORITY_FILE") or "").strip()
    if not raw_path:
        raise PublishCLIError("--authority-file is required")
    path = Path(raw_path).expanduser()
    try:
        if path.stat().st_mode & 0o077:
            raise PublishCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishCLIError(f"could not read authority file: {exc}") from exc
    capability = str(payload.get("provider_capability") or payload.get("capability") or "")
    source_session_id = str(payload.get("source_session_id") or "").strip()
    if not capability or not source_session_id:
        raise PublishCLIError("authority file is invalid")
    return capability, source_session_id


def load_manifest(path: str) -> list[Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishCLIError(f"could not read manifest {path!r}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        raise PublishCLIError("manifest must be a JSON object with a files array")
    return list(data["files"])


def parse_entry_json(value: str) -> dict[str, Any]:
    try:
        entry = json.loads(value)
    except json.JSONDecodeError as exc:
        raise argparse.ArgumentTypeError(f"invalid --entry-json: {exc}") from exc
    if not isinstance(entry, dict):
        raise argparse.ArgumentTypeError("--entry-json must decode to an object")
    return entry


def requested_files(args: argparse.Namespace) -> list[Any]:
    entries: list[Any] = list(args.paths)
    entries.extend(args.entry_json)
    if args.manifest:
        entries.extend(load_manifest(args.manifest))
    if not entries:
        raise PublishCLIError("provide at least one absolute file path")
    return entries


def http_error_detail(raw: str) -> str:
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(decoded, dict):
        detail = decoded.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail, separators=(",", ":"))
    return json.dumps(decoded, separators=(",", ":"))


def publish(
    chat_id: str | None,
    files: list[Any],
    *,
    publication_id: str | None = None,
    authority_file: str | None = None,
) -> dict[str, Any]:
    server_url = loopback_server_url()
    capability, authority_chat_id = provider_authority(authority_file)
    requested_chat_id = str(chat_id or os.environ.get("AGENTSDOCK_CHAT_ID") or "").strip()
    if requested_chat_id and requested_chat_id != authority_chat_id:
        raise PublishCLIError("--chat-id does not match the authority file")
    chat_id = requested_chat_id or authority_chat_id
    publication_id = publication_id or f"pub_{uuid.uuid4().hex}"
    encoded_chat_id = urllib.parse.quote(chat_id, safe="")
    body = json.dumps({
        "publication_id": publication_id,
        "files": files,
    }).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-AgentsDock-Provider-Capability": capability,
    }
    decoded: Any = None
    ambiguous_error: BaseException | None = None
    # urllib honors HTTP_PROXY even for loopback addresses on some hosts.
    # Explicitly disable proxies and redirects so this token cannot leave the
    # already-validated local endpoint.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirectHandler(),
    )
    for attempt in range(2):
        attempt_headers = dict(headers)
        if attempt:
            attempt_headers["X-AgentsDock-Publication-Retry"] = "1"
        request = urllib.request.Request(
            f"{server_url}/api/agent/sessions/{encoded_chat_id}/artifacts",
            data=body,
            headers=attempt_headers,
            method="POST",
        )
        try:
            # Large videos can take time to copy from a network workspace. The
            # server performs that copy outside its event loop and returns only
            # after the artifact events are durable. One retry uses the same
            # publication ID, so a lost response cannot create duplicate cards.
            with opener.open(request, timeout=600) as response:
                raw_response = response.read().decode("utf-8")
            try:
                decoded = json.loads(raw_response)
            except json.JSONDecodeError as exc:
                # A truncated response is ambiguous: the event batch may
                # already be durable. Retry once with the same publication ID.
                ambiguous_error = exc
                if attempt == 0:
                    continue
                break
            ambiguous_error = None
            break
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            detail = http_error_detail(raw)
            raise PublishCLIError(
                f"server rejected publication ({exc.code}): {detail or exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            ambiguous_error = exc
            if attempt == 0:
                continue
    if ambiguous_error is not None:
        reason = getattr(ambiguous_error, "reason", ambiguous_error)
        raise PublishCLIError(
            f"could not confirm publication {publication_id}: {reason}"
        ) from ambiguous_error
    if not isinstance(decoded, dict) or decoded.get("ok") is not True:
        raise PublishCLIError("AgentsServer did not confirm publication")
    if decoded.get("publication_id") != publication_id:
        raise PublishCLIError("AgentsServer returned a receipt for another publication")
    if decoded.get("chat_id") != chat_id:
        raise PublishCLIError("AgentsServer returned a receipt for another chat")
    run_id = decoded.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        raise PublishCLIError("AgentsServer returned a receipt without a run ID")
    receipts = decoded.get("receipts")
    if not isinstance(receipts, list) or len(receipts) != len(files):
        raise PublishCLIError("AgentsServer returned an incomplete publication receipt")
    artifact_ids: set[str] = set()
    event_ids: set[str] = set()
    for receipt in receipts:
        artifact_id = str(receipt.get("artifact_id") or "") if isinstance(receipt, dict) else ""
        event_id = str(receipt.get("event_id") or "") if isinstance(receipt, dict) else ""
        if (
            not isinstance(receipt, dict)
            or not artifact_id.strip()
            or not event_id.strip()
            or not isinstance(receipt.get("event_seq"), int)
            or isinstance(receipt.get("event_seq"), bool)
            or int(receipt["event_seq"]) <= 0
            or receipt.get("run_id") != run_id
            or artifact_id in artifact_ids
            or event_id in event_ids
        ):
            raise PublishCLIError("AgentsServer returned an invalid publication receipt")
        artifact_ids.add(artifact_id)
        event_ids.add(event_id)
    return decoded


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Attach files to the currently active AgentsDock chat turn.",
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
    parser.add_argument(
        "--manifest",
        help="read additional entries from a legacy {\"files\": [...]} manifest",
    )
    parser.add_argument(
        "--publication-id",
        help="idempotency key (normally generated automatically)",
    )
    parser.add_argument(
        "--entry-json",
        action="append",
        default=[],
        type=parse_entry_json,
        metavar="JSON",
        help='publish an object entry such as {"path":"/tmp/demo.mp4","title":"Demo"}',
    )
    parser.add_argument("paths", nargs="*", help="absolute file paths")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = publish(
            args.chat_id,
            requested_files(args),
            publication_id=args.publication_id,
            authority_file=args.authority_file,
        )
    except PublishCLIError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
