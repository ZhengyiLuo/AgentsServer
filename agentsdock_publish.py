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


PROVIDER_RUNTIME_VALUE_MAX_BYTES = 4096


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


def canonical_http_origin(value: str, label: str) -> tuple[str, bool]:
    raw = value.strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
        port = parsed.port or 80
    except ValueError as exc:
        raise PublishCLIError(f"{label} must be an HTTP origin") from exc
    if (
        parsed.scheme.lower() != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise PublishCLIError(f"{label} must be an HTTP origin")
    host = parsed.hostname.lower()
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        host = address.compressed
        loopback = address.is_loopback
        url_host = f"[{host}]" if isinstance(address, ipaddress.IPv6Address) else host
    except ValueError:
        loopback = host == "localhost"
        url_host = host
    return f"http://{url_host}:{port}", loopback


def validated_server_url(authority_origin: str = "") -> str:
    raw_server_url = os.environ.get("AGENTSDOCK_SERVER_URL", "").strip()
    if not raw_server_url:
        raise PublishCLIError("missing agent environment: AGENTSDOCK_SERVER_URL")
    server_origin, loopback = canonical_http_origin(
        raw_server_url,
        "AGENTSDOCK_SERVER_URL",
    )
    runtime_origin = bounded_identity_value(
        os.environ.get("AGENTSDOCK_PROVIDER_SERVER_ORIGIN"),
        "AGENTSDOCK_PROVIDER_SERVER_ORIGIN",
    )
    if runtime_origin:
        canonical_runtime, _runtime_loopback = canonical_http_origin(
            runtime_origin,
            "AGENTSDOCK_PROVIDER_SERVER_ORIGIN",
        )
        if canonical_runtime != server_origin:
            raise PublishCLIError(
                "AGENTSDOCK_SERVER_URL conflicts with the live provider origin"
            )
    if loopback:
        return raw_server_url.rstrip("/")
    if not authority_origin:
        raise PublishCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    canonical_authority, _authority_loopback = canonical_http_origin(
        authority_origin,
        "authority provider_server_origin",
    )
    if canonical_authority != server_origin:
        raise PublishCLIError(
            "non-loopback AGENTSDOCK_SERVER_URL must match the authority origin"
        )
    return server_origin


def loopback_server_url() -> str:
    """Compatibility validator for legacy explicit loopback CLI calls."""

    return validated_server_url()


def bounded_identity_value(value: str | None, label: str) -> str:
    clean = str(value or "").strip()
    try:
        size = len(clean.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PublishCLIError(f"{label} is not valid UTF-8") from exc
    if size > PROVIDER_RUNTIME_VALUE_MAX_BYTES:
        raise PublishCLIError(f"{label} exceeds the provider runtime limit")
    return clean


def selected_authority_path(authority_file: str | None) -> Path:
    explicit = bounded_identity_value(authority_file, "--authority-file")
    ambient = bounded_identity_value(
        os.environ.get("AGENTSDOCK_PROVIDER_AUTHORITY_FILE"),
        "AGENTSDOCK_PROVIDER_AUTHORITY_FILE",
    )
    if explicit and ambient:
        explicit_key = os.path.abspath(os.path.expanduser(explicit))
        ambient_key = os.path.abspath(os.path.expanduser(ambient))
        if explicit_key != ambient_key:
            raise PublishCLIError(
                "--authority-file conflicts with the live provider authority"
            )
    selected = explicit or ambient
    if not selected:
        raise PublishCLIError("--authority-file is required")
    return Path(selected).expanduser()


def provider_authority(authority_file: str | None) -> tuple[str, str]:
    path = selected_authority_path(authority_file)
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


def authority_server_origin(authority_file: str | None) -> str:
    path = selected_authority_path(authority_file)
    try:
        if path.stat().st_mode & 0o077:
            raise PublishCLIError("authority file permissions are unsafe")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublishCLIError(f"could not read authority file: {exc}") from exc
    return bounded_identity_value(
        payload.get("provider_server_origin"),
        "authority provider_server_origin",
    )


def requested_chat_scope(
    chat_id: str | None,
    authority_chat_id: str,
) -> str:
    explicit = bounded_identity_value(chat_id, "--chat-id")
    ambient = bounded_identity_value(
        os.environ.get("AGENTSDOCK_CHAT_ID"),
        "AGENTSDOCK_CHAT_ID",
    )
    if explicit and ambient and explicit != ambient:
        raise PublishCLIError("--chat-id conflicts with AGENTSDOCK_CHAT_ID")
    for candidate in (explicit, ambient):
        if candidate and candidate != authority_chat_id:
            raise PublishCLIError("--chat-id does not match the authority file")
    return authority_chat_id


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
    capability, authority_chat_id = provider_authority(authority_file)
    server_url = validated_server_url(authority_server_origin(authority_file))
    chat_id = requested_chat_scope(chat_id, authority_chat_id)
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
        allow_abbrev=False,
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
