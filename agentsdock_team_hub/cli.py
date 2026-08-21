"""Team Hub local control and development-listener command line."""

from __future__ import annotations

import argparse
import ipaddress
from pathlib import Path
import sys

import uvicorn

from .security import validate_tls_files
from .service import create_app
from .store import HubError, HubStore


DEFAULT_DATA_DIR = Path.home() / ".agentsdock" / "team-hub"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agentsdock-team-hub")
    subcommands = parser.add_subparsers(dest="command", required=True)

    serve = subcommands.add_parser("serve", help="run the Team Hub API")
    serve.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7851)
    serve.add_argument("--allowed-host", action="append", default=[])
    serve.add_argument("--allowed-origin", action="append", default=[])
    serve.add_argument("--ssl-certfile", type=Path)
    serve.add_argument("--ssl-keyfile", type=Path)

    proof = subcommands.add_parser(
        "bootstrap-proof", help="renew or locate the local initial-owner proof"
    )
    proof.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)

    recovery = subcommands.add_parser(
        "owner-recovery", help="issue a local one-time owner device recovery proof"
    )
    recovery.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    recovery.add_argument("--email", required=True)
    recovery.add_argument("--device-label", required=True)
    recovery.add_argument("--team-id")

    device_recovery = subcommands.add_parser(
        "device-recovery", help="issue a local one-time member device recovery proof"
    )
    device_recovery.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    device_recovery.add_argument("--email", required=True)
    device_recovery.add_argument("--device-label", required=True)
    device_recovery.add_argument("--team-id")

    for command, help_text in (
        (
            "verify-snapshot",
            "verify an exact managed rollback snapshot without changing Hub state",
        ),
        (
            "restore-snapshot",
            "offline verified restore of an exact managed rollback snapshot",
        ),
    ):
        snapshot = subcommands.add_parser(command, help=help_text)
        snapshot.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
        snapshot.add_argument("--snapshot", type=Path, required=True)
        snapshot.add_argument("--expected-host-identity", required=True)
        snapshot.add_argument("--expected-hub-id", required=True)
        snapshot.add_argument("--expected-operation-id", required=True)
    return parser


def _loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("port must be between 1 and 65535")
            if (args.ssl_certfile is None) != (args.ssl_keyfile is None):
                raise ValueError("--ssl-certfile and --ssl-keyfile must be supplied together")
            cert_and_key = args.ssl_certfile is not None and args.ssl_keyfile is not None
            if not _loopback(args.host) and not cert_and_key:
                raise ValueError(
                    "non-loopback Team Hub listeners require --ssl-certfile and --ssl-keyfile"
                )
            if cert_and_key:
                validate_tls_files(args.ssl_certfile, args.ssl_keyfile)
            allowed_hosts = set(args.allowed_host)
            allowed_hosts.add(args.host)
            if _loopback(args.host):
                allowed_hosts.update({"127.0.0.1", "localhost", "[::1]", "::1"})
            lease = HubStore.acquire_managed_runtime_lease(args.data_dir)
            try:
                app = create_app(
                    args.data_dir,
                    allowed_hosts=allowed_hosts,
                    allowed_origins=set(args.allowed_origin),
                )
                store: HubStore = app.state.store
                scheme = "https" if cert_and_key else "http"
                print(f"Team Hub {store.hub_id} listening at {scheme}://{args.host}:{args.port}")
                if store.health()["bootstrap_required"]:
                    print(
                        "Initial-owner proof file: "
                        f"{store.bootstrap_proof_path} (the secret itself is never printed)"
                    )
                uvicorn.run(
                    app,
                    host=args.host,
                    port=args.port,
                    ssl_certfile=str(args.ssl_certfile) if args.ssl_certfile else None,
                    ssl_keyfile=str(args.ssl_keyfile) if args.ssl_keyfile else None,
                    proxy_headers=False,
                    server_header=False,
                )
            finally:
                HubStore.release_managed_runtime_lease(lease)
            return 0
        if args.command in {"verify-snapshot", "restore-snapshot"}:
            operation = (
                HubStore.verify_maintenance_snapshot
                if args.command == "verify-snapshot"
                else HubStore.restore_maintenance_snapshot
            )
            operation(
                args.data_dir,
                args.snapshot,
                expected_host_identity=args.expected_host_identity,
                expected_hub_id=args.expected_hub_id,
                expected_operation_id=args.expected_operation_id,
            )
            print(args.snapshot)
            return 0
        # Local control commands remain available for a managed-bound Hub,
        # while the standalone listener above refuses to serve that database.
        # The same bounded interprocess lock fences proof-file changes from an
        # update/restart snapshot.
        with HubStore.maintenance_control_lock(args.data_dir):
            store = HubStore(args.data_dir, allow_bound_control=True)
            if store.maintenance_fence() is not None:
                raise RuntimeError(
                    "Team Hub local control is unavailable during managed maintenance"
                )
            if args.command == "bootstrap-proof":
                path = store.renew_bootstrap_proof()
                print(path)
                return 0
            if args.command == "owner-recovery":
                path = store.issue_owner_recovery(
                    args.email,
                    args.device_label,
                    team_id=args.team_id,
                )
                print(path)
                return 0
            if args.command == "device-recovery":
                path = store.issue_device_recovery(
                    args.email,
                    args.device_label,
                    team_id=args.team_id,
                )
                print(path)
                return 0
    except (HubError, RuntimeError, ValueError, PermissionError) as exc:
        print(f"Team Hub: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
