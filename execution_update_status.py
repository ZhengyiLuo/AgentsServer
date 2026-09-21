"""Prove a bundled release is running in both independently managed services.

Release files and a successful worker boot are not completion evidence. The
native service owners must match authenticated public health, and an unfinished
activation remains incomplete even after it has published the new files.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import execution_install as files
from execution_manage import NativeServices, WorkerControl
from update_runner import version_key


def current_components(
    root: Path, *, target_version: str, expected_server_identity: str,
    expected_worker_instance: str,
) -> bool:
    """Read-only proof, invoked off the server event loop for an equal-version ensure."""
    root = files._path(root)
    files._owned_directory(root)
    for name in (".activation-transaction", ".execution-transaction"):
        journal = root / name
        if journal.exists() or journal.is_symlink():
            return False
    data, _mode = files._read_file(root / files.LAYOUT_NAME, private=True)
    document = json.loads(data)
    if not isinstance(document, dict) or document.get("format") != files.FORMAT:
        raise ValueError("installed execution layout is invalid")
    layout = files.ExecutionLayout.from_dict(document.get("layout"))
    if layout.install_root != root or document != files.layout_manifest(layout):
        raise ValueError("installed execution layout does not match its runtime")
    receipt: dict[str, Any] = WorkerControl().receipt(layout, NativeServices(layout))
    if (receipt.get("server_identity") != expected_server_identity
            or receipt.get("worker_instance_id") != expected_worker_instance):
        raise RuntimeError("running execution ownership changed during update reconciliation")
    target = version_key(target_version)
    complete = (receipt.get("maintenance_held") is False
                and version_key(receipt.get("worker_version", "")) >= target
                and version_key(receipt.get("gateway_version", "")) >= target)
    # A new activation can begin while the read-only native probes are in flight.
    # Do not claim completion over the operation that just acquired ownership.
    return complete and not any(
        (root / name).exists() or (root / name).is_symlink()
        for name in (".activation-transaction", ".execution-transaction")
    )
