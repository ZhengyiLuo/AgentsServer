"""Bounded, data-only Claude task receipts; no I/O or provider transcripts."""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
import json


RECONCILED_EVENT = "claude_background_tasks_reconciled"
CONSUMED_EVENT = "claude_background_task_reconciliation_consumed"
MAX_TASKS = 64
MAX_TASK_BYTES = 6144
SESSION_FIELD = "claude_background_reconciliation"
STATUSES = frozenset({"running", "completed", "failed", "stopped", "killed", "tracking_lost"})


def _identifier(value, limit=256):
    return value if isinstance(value, str) and 0 < len(value) <= limit and all(
        ord(char) >= 32 and ord(char) != 127 for char in value
    ) else None


def normalize_task_receipts(receipts, *, run_id, provider_session_id, tracking_lost=False):
    """Allowlist exact task ownership and statuses, never commands or summaries."""
    if not _identifier(run_id) or not _identifier(provider_session_id):
        return []
    if not isinstance(receipts, (list, tuple)):
        return []
    result = OrderedDict()
    for receipt in receipts[:MAX_TASKS]:
        if not isinstance(receipt, Mapping):
            continue
        task_id = _identifier(receipt.get("task_id"))
        status = receipt.get("status")
        task_type = _identifier(receipt.get("task_type"), 64)
        if not task_id or not isinstance(status, str) or status not in STATUSES or not task_type:
            continue
        if receipt.get("owner_run_id") != run_id or receipt.get("provider_session_id") not in (None, "", provider_session_id):
            continue
        task = {
            "task_id": task_id, "task_type": task_type,
            "status": "tracking_lost" if tracking_lost and status == "running" else status,
            "owner_run_id": run_id, "provider_session_id": provider_session_id,
        }
        tool_id = _identifier(receipt.get("tool_use_id"))
        if tool_id:
            task["tool_use_id"] = tool_id
        result[task_id] = task
    return list(result.values())


def reconciliation_envelope(batches):
    """Latest exact-owner state wins; discarded states are counted, not invented."""
    tasks = OrderedDict()
    overflow = 0
    for batch in batches[-2:]:
        overflow += max(0, min(int(batch.get("overflow_count") or 0), 1_000_000))
        for task in batch.get("tasks", ()):
            key = (task["provider_session_id"], task["owner_run_id"], task["task_id"])
            tasks[key] = dict(task)
            tasks.move_to_end(key)
    selected = list(tasks.values())
    while len(selected) > MAX_TASKS or len(json.dumps(selected, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > MAX_TASK_BYTES:
        selected.pop(0)
        overflow += 1
    return {"tasks": selected, "overflow_count": min(overflow, 1_000_000)}


def pending_task_reconciliations(value, *, provider_session_id: str):
    """Read a single bounded persisted checkpoint without touching event logs."""
    if not isinstance(value, dict) or value.get("version") != 1 or value.get("provider_session_id") != provider_session_id:
        return []
    identifier = _identifier(value.get("reconciliation_id"))
    overflow = value.get("overflow_count", 0)
    source_tasks = value.get("tasks")
    if not identifier or type(overflow) is not int or overflow < 0 or not isinstance(source_tasks, list):
        return []
    tasks = []
    for receipt in source_tasks[:MAX_TASKS]:
        if isinstance(receipt, dict):
            tasks.extend(normalize_task_receipts([receipt], run_id=receipt.get("owner_run_id"), provider_session_id=provider_session_id))
    envelope = reconciliation_envelope([{"tasks": tasks, "overflow_count": overflow + max(0, len(source_tasks) - MAX_TASKS)}])
    return [{"reconciliation_id": identifier, **envelope}]


def pending_reconciliation_state(previous, batch, *, provider_session_id):
    pending = pending_task_reconciliations(previous, provider_session_id=provider_session_id)
    return {
        "version": 1, "provider_session_id": provider_session_id,
        "reconciliation_id": batch["reconciliation_id"],
        **reconciliation_envelope([*pending, batch]),
    }
