"""Task handoff/receipt checks without importing or starting the server."""
from __future__ import annotations

import ast
import asyncio
from collections import OrderedDict
import json
from pathlib import Path
import re
import tempfile
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import uuid

import claude_background_reconciliation as receipts


def task(task_id="unfinished", status="running", owner="run-old"):
    return {"task_id": task_id, "task_type": "local_agent", "status": status,
            "owner_run_id": owner, "provider_session_id": "provider-fixture", "tool_use_id": "tool-" + task_id}


def load_helpers(path=None):
    source = Path(__file__).with_name("agent_server.py")
    names = {"persist_claude_background_task_receipts", "acknowledge_claude_background_reconciliation",
             "build_claude_subagent_snapshot", "normalize_subagent_status", "compact_subagent_text",
             "claude_subagent_child_activity", "claude_subagent_progress_activity"}
    selected = [node for node in ast.parse(source.read_text()).body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in selected} == names
    namespace = {
        "asyncio": asyncio, "uuid": uuid, "json": json, "re": re, "OrderedDict": OrderedDict,
        "BACKEND_CLAUDE": "claude", "SUBAGENT_SNAPSHOT_STATE_LIMIT": 256,
        "SUBAGENT_SNAPSHOT_LOG_LIMIT": 80, "SUBAGENT_SNAPSHOT_TEXT_LIMIT": 600,
        "CLAUDE_BACKGROUND_RECONCILED_EVENT": receipts.RECONCILED_EVENT,
        "CLAUDE_BACKGROUND_CONSUMED_EVENT": receipts.CONSUMED_EVENT,
        "CLAUDE_BACKGROUND_SESSION_FIELD": receipts.SESSION_FIELD,
        "normalize_task_receipts": receipts.normalize_task_receipts,
        "pending_reconciliation_state": receipts.pending_reconciliation_state,
        "STORE": SimpleNamespace(_lock=asyncio.Lock(), sessions={"qa-claude-lifecycle": {}}, save=AsyncMock()),
        "append_event": AsyncMock(return_value={"seq": 1}),
        "events_path": lambda _session: path,
    }
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0), *selected], type_ignores=[]))
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


def lifecycle_fixture_events():
    common = {"session_id": "qa-claude-lifecycle", "backend": "claude", "run_id": "run-old", "ts": "2026-09-10T10:00:00Z"}
    return [
        {**common, "type": "raw_event", "seq": 1, "raw": json.dumps({"type": "system", "subtype": "task_started", "task_id": "unfinished", "tool_use_id": "tool-unfinished", "task_type": "local_agent", "description": "Fixture research"})},
        {**common, "type": receipts.RECONCILED_EVENT, "seq": 2, "provider_session_id": "provider-fixture", "tasks": [task(status="tracking_lost"), task("finished", "completed")]},
        {**common, "type": "turn_stopped", "seq": 3, "native_steer": True, "superseded_by_run_id": "run-next"},
    ]


class BackgroundReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_restart_preserves_exact_states_until_actual_context_consumption(self):
        env = load_helpers()
        handle = SimpleNamespace(background_task_receipts=(MappingProxyType(task()), MappingProxyType(task("finished", "completed"))), background_task_overflow_count=0)
        pending = await env["persist_claude_background_task_receipts"]("qa-claude-lifecycle", "run-old", "provider-fixture", handle, tracking_lost=True)
        self.assertEqual([item["status"] for item in pending["tasks"]], ["tracking_lost", "completed"])
        # Restore only serialized sessions metadata: no ledger or live handle.
        env["STORE"].sessions = json.loads(json.dumps(env["STORE"].sessions))
        batches = receipts.pending_task_reconciliations(env["STORE"].sessions["qa-claude-lifecycle"][receipts.SESSION_FIELD], provider_session_id="provider-fixture")
        successor = SimpleNamespace(background_task_reconciliation_consumed=False)
        self.assertEqual(await env["acknowledge_claude_background_reconciliation"]("qa-claude-lifecycle", "run-next", "provider-fixture", successor, batches), batches)
        successor.background_task_reconciliation_consumed = True
        self.assertEqual(await env["acknowledge_claude_background_reconciliation"]("qa-claude-lifecycle", "run-next", "provider-fixture", successor, batches), [])
        self.assertNotIn(receipts.SESSION_FIELD, env["STORE"].sessions["qa-claude-lifecycle"])

    async def test_interrupted_again_merges_pending_without_clearing_newer_checkpoint(self):
        env = load_helpers()
        persist = env["persist_claude_background_task_receipts"]
        old = await persist("qa-claude-lifecycle", "run-old", "provider-fixture", SimpleNamespace(background_task_receipts=(task(),)), tracking_lost=True)
        newer = await persist("qa-claude-lifecycle", "run-next", "provider-fixture", SimpleNamespace(background_task_receipts=(task("next", owner="run-next"),)), tracking_lost=True)
        await env["acknowledge_claude_background_reconciliation"]("qa-claude-lifecycle", "run-next", "provider-fixture", SimpleNamespace(background_task_reconciliation_consumed=True), [old])
        self.assertEqual(env["STORE"].sessions["qa-claude-lifecycle"][receipts.SESSION_FIELD], newer)
        self.assertEqual({item["task_id"] for item in newer["tasks"]}, {"unfinished", "next"})

    async def test_discarded_or_failed_ack_retains_pending(self):
        env = load_helpers()
        pending = await env["persist_claude_background_task_receipts"]("qa-claude-lifecycle", "run-old", "provider-fixture", SimpleNamespace(background_task_receipts=(task(),)))
        for outcome in ({"discarded": True}, OSError("audit unavailable")):
            env["append_event"] = AsyncMock(side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome)
            try:
                await env["acknowledge_claude_background_reconciliation"]("qa-claude-lifecycle", "run-next", "provider-fixture", SimpleNamespace(background_task_reconciliation_consumed=True), [pending])
            except OSError:
                pass
            self.assertEqual(env["STORE"].sessions["qa-claude-lifecycle"][receipts.SESSION_FIELD], pending)

    async def test_save_cancellation_preserves_committed_pending_and_committed_clear(self):
        env = load_helpers()
        env["STORE"].save.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await env["persist_claude_background_task_receipts"]("qa-claude-lifecycle", "run-old", "provider-fixture", SimpleNamespace(background_task_receipts=(task(),)))
        pending = env["STORE"].sessions["qa-claude-lifecycle"][receipts.SESSION_FIELD]
        with self.assertRaises(asyncio.CancelledError):
            await env["acknowledge_claude_background_reconciliation"]("qa-claude-lifecycle", "run-next", "provider-fixture", SimpleNamespace(background_task_reconciliation_consumed=True), [pending])
        self.assertNotIn(receipts.SESSION_FIELD, env["STORE"].sessions["qa-claude-lifecycle"])

    def test_schema_is_bounded_and_exact_owner_only(self):
        values = [task(), {**task("foreign"), "owner_run_id": "foreign"}, {**task("other-provider"), "provider_session_id": "foreign"}]
        values[0]["command"] = "must not be retained"
        normalized = receipts.normalize_task_receipts(values, run_id="run-old", provider_session_id="provider-fixture")
        self.assertEqual(normalized, [task()])
        envelope = receipts.reconciliation_envelope([{"tasks": [task("task-" + str(index) + "x" * 200) for index in range(64)], "overflow_count": 0}])
        self.assertLessEqual(len(json.dumps(envelope["tasks"], ensure_ascii=True, separators=(",", ":")).encode()), receipts.MAX_TASK_BYTES)
        self.assertGreater(envelope["overflow_count"], 0)

    def test_actual_snapshot_keeps_lost_task_inactive_and_terminal_sibling_completed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("".join(json.dumps(event) + "\n" for event in lifecycle_fixture_events()))
            snapshot = load_helpers(path)["build_claude_subagent_snapshot"]("qa-claude-lifecycle")
        states = {state["subagent_id"]: state for state in snapshot["subagents"]}
        self.assertEqual(states["unfinished"]["subagent_status"], "tracking_lost")
        self.assertIn("completion is not confirmed", states["unfinished"]["subagent_activity"])
        self.assertEqual(states["finished"]["subagent_status"], "completed")

    def test_background_snapshot_respects_reported_terminal_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            event = {"type": "raw_event", "backend": "claude", "run_id": "run-old", "seq": 1,
                     "raw": json.dumps({"type": "system", "subtype": "background_tasks_changed", "tasks": [
                         {"task_id": "done", "task_type": "local_agent", "status": "completed"}]})}
            path.write_text(json.dumps(event) + "\n")
            snapshot = load_helpers(path)["build_claude_subagent_snapshot"]("qa-claude-lifecycle")
        self.assertEqual(snapshot["subagents"][0]["subagent_status"], "completed")


if __name__ == "__main__":
    unittest.main()
