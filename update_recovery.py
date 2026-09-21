"""Resume one admitted activation; never stage or create another installation.

The durable intent is written before install.sh starts. Its directory identities
bind the outer journal to that exact verified candidate, including after rename
or reboot. The installer remains the sole service/state rollback authority.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys
from typing import Any

import activation_transaction as activation
import execution_preparation as preparation
import update_runner as updates


def activation_intent(*, root: Path, candidate: Path, version: str, api_contract: int,
                      update_id: str, server_identity: str) -> dict[str, Any]:
    root, candidate = preparation._path(root), preparation._path(candidate)
    preparation._candidate(root, candidate)
    if re.fullmatch(r"[0-9a-f]{32}", update_id) is None or not server_identity:
        raise ValueError("activation recovery identity is invalid")
    return {"format": 1, "root": str(root), "root_binding": preparation._binding(root),
            "candidate_binding": preparation._binding(candidate), "version": version,
            "api_contract": api_contract, "update_id": update_id, "server_identity": server_identity}


def recovery_context(root: Path, status: dict[str, Any], *, server_identity: str) -> dict[str, Any] | None:
    """Inspect without cleaning journal temporaries or taking service ownership."""
    root = preparation._path(root)
    journal = root / ".activation-transaction"
    if not journal.exists() and not journal.is_symlink():
        return None
    intent = status.get("_activation_recovery")
    if (not isinstance(intent, dict) or set(intent) != {
        "format", "root", "root_binding", "candidate_binding", "version", "api_contract", "update_id", "server_identity",
    } or type(intent["format"]) is not int or intent["format"] != 1
            or intent["root"] != str(root) or intent["server_identity"] != server_identity
            or intent["update_id"] != status.get("update_id")
            or re.fullmatch(r"[0-9a-f]{32}", str(intent["update_id"])) is None
            or intent["version"] != status.get("target_version")
            or type(intent["api_contract"]) is not int or intent["api_contract"] < 1):
        raise RuntimeError("activation journal is not bound to this admitted update")
    preparation._check_binding(root, intent["root_binding"])
    value = activation.execution_context(root)
    if (value is None or value["release_version"] != intent["version"]
            or value["execution"]["api_contract"] != intent["api_contract"]):
        raise RuntimeError("activation journal release pins changed")
    saved = intent["candidate_binding"]
    candidate = value["candidate_release"]
    if (not isinstance(saved, dict) or set(saved) != {"device", "inode", "volume_uuid"}
            or any(type(saved.get(name)) is not int or saved[name] < 1 for name in ("device", "inode"))
            or saved["inode"] != candidate["inode"]):
        raise RuntimeError("activation journal candidate identity changed")
    # execution_context already checks persistent volume UUIDs and rebases
    # Darwin device IDs; candidates and the install root share the same volume.
    if sys.platform == "darwin":
        if not saved["volume_uuid"] or saved["volume_uuid"] != intent["root_binding"]["volume_uuid"]:
            raise RuntimeError("activation candidate volume changed")
    elif saved["volume_uuid"] is not None or saved["device"] != candidate["device"]:
        raise RuntimeError("activation candidate filesystem changed")
    handoff = value["execution"]["handoff"]
    if handoff is not None and handoff != status.get("_execution_handoff"):
        raise RuntimeError("activation journal worker handoff changed")
    return value


def run_recovery(args: argparse.Namespace) -> None:
    """Run only the retained journal, with the same status owner and heartbeat."""
    status_path = Path(args.status_file).expanduser().resolve()
    root = preparation._path(os.environ["AGENTS_SERVER_INSTALL_DIR"])
    with updates.server_update_status_lock(status_path):
        status = updates._read_status_unlocked(status_path)
        if (status.get("update_id") != args.update_id
                or status.get("phase") not in updates.RUNNER_OWNED_ACTIVE_PHASES):
            raise updates.UpdateOwnershipLostError("activation recovery lost its admitted update")
    context = recovery_context(root, status, server_identity=args.expected_server_identity)
    if (context is None or context["transaction_id"] != args.recovery_transaction
            or context["release_version"] != args.expected_version):
        raise RuntimeError("activation recovery transaction changed before launch")
    version = context["release_version"]
    api = context["execution"]["api_contract"]
    token = updates.consume_auth_token_file(getattr(args, "auth_token_file", None))
    updates.update_status(status_path, expected_update_id=args.update_id, phase="installing",
        runner_pid=os.getpid(), heartbeat_at=updates.utc_now(),
        message="Recovering the interrupted server activation.")
    # Use the running release's recovery implementation. The candidate may have
    # already been retired by a partly completed rollback.
    source = Path(__file__).resolve().parent
    command = [str(source / "install.sh"), "--recover-only", "--non-interactive",
        "--release-version", version, "--expected-api-contract", str(api),
        "--expected-activation-id", context["transaction_id"],
        "--expected-server-identity", args.expected_server_identity,
        "--managed-update-id", args.update_id,
        "--port", str(args.port), "--bind", args.bind]
    try:
        updates.run_installer(command, cwd=source, status_path=status_path,
            log_path=status_path.with_name("server-update.log"), version=version,
            expected_update_id=args.update_id, managed_update_id=args.update_id,
            expected_service_cgroup=getattr(args, "expected_service_cgroup", None),
            accepted_returncodes=(0, 75))
    except updates.InstallerRolledBack:
        if journal_present(root):
            raise RuntimeError("rollback recovery retained an unfinished journal")
        updates.update_status(status_path, expected_update_id=args.update_id,
            phase="failed", runner_pid=None, heartbeat_at=None, retryable=True,
            error_code="server_update_rolled_back",
            error_action="Retry the bundled update when ready.",
            message="The interrupted update was rolled back to the verified previous installation.",
            finished_at=updates.utc_now())
        return
    if journal_present(root):
        raise RuntimeError("activation recovery exited without finalizing its journal")
    identity = dict(token=token, expected_server_identity=args.expected_server_identity,
                    expected_server_version=version, expected_api_contract_version=api)
    if status.get("team_hub_id"):
        identity.update(expected_team_hub_id=status["team_hub_id"],
            expected_team_hub_transport=status.get("team_hub_transport"),
            expected_team_hub_url=status.get("team_hub_url"),
            expected_team_hub_direct_ip_url=status.get("team_hub_direct_ip_url"))
    updates.assert_post_update_identity(args.port, **identity)
    if status.get("team_hub_repair_mode") == "failed_start":
        updates.assert_repaired_team_hub_identity(args.port, token=token,
            expected_server_identity=args.expected_server_identity,
            expected_team_hub_transport=status.get("team_hub_transport") or "",
            expected_team_hub_url=status.get("team_hub_url"),
            expected_team_hub_direct_ip_url=status.get("team_hub_direct_ip_url") or "")
    updates.update_status(status_path, expected_update_id=args.update_id, phase="complete",
        installed_version=version, update_available=False, runner_pid=None, heartbeat_at=None,
        elapsed_seconds=None, error_code=None, error_action=None, retryable=None,
        message=f"AgentsServer {version} recovered and is healthy.", finished_at=updates.utc_now())


def journal_present(root: Path) -> bool:
    return any((root / name).exists() or (root / name).is_symlink()
               for name in (".activation-transaction", ".execution-transaction"))
