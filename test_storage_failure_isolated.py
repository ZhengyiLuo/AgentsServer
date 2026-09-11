"""Exact storage/finalizer source with synthetic failures; no server import."""
from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
from copy import deepcopy
import errno
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from test_claude_shutdown_status_isolated import load_probe


SOURCE = Path(__file__).with_name("agent_server.py")


def storage_source():
    tree = ast.parse(SOURCE.read_text())
    names = {"append_turn_finished_event", "join_task_despite_caller_cancellation", "append_durable_event_batch_sync"}
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes += [deepcopy(node) for node in tree.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    admission = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_start_turn_locked")
    classification = next(node for node in ast.walk(admission) if isinstance(node, ast.If)
                          and "Free storage and retry." in ast.unparse(node)
                          and "started_event is None" in ast.unparse(node.test))
    probe = ast.parse("def classify(start_error, started_event=None):\n    pass").body[0]
    probe.body = [deepcopy(classification)]
    nodes.append(probe)

    class AdmissionWait(Exception):
        def __init__(self, *, status_code, detail):
            self.status_code, self.detail = status_code, detail

    ns = dict(asyncio=asyncio, suppress=suppress, errno=errno, json=json, uuid=uuid,
              logger=logging.getLogger("isolated-storage"), now_iso=lambda: "2026-01-01T00:00:00Z",
              TransientAdmissionWait=AdmissionWait, ACTIVE_LOCK=asyncio.Lock(), ACTIVE={}, CURRENT_TURNS={},
              STORE=SimpleNamespace(sessions={"chat": {"active_run": {"run_id": "finished-run"}}}),
              append_event=AsyncMock(), revoke_cross_chat_capability=AsyncMock(),
              finalize_cross_chat_terminal=AsyncMock(), finalize_handoff_digest_turn=AsyncMock(),
              finish_handoff_digest_delivery=AsyncMock(), concise_error_message=str)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "<isolated-storage>", "exec"), ns)
    return ns


class StorageFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_enospc_cleans_exact_run_without_fake_event_or_forwarding(self):
        ns = storage_source()
        failure = OSError(errno.ENOSPC, "Synthetic storage full")
        ns["append_event"].side_effect = failure
        with self.assertRaises(OSError) as caught:
            await ns["append_turn_finished_event"]("chat", {"run_id": "finished-run", "result_text": "Synthetic final"})
        self.assertIs(caught.exception, failure)
        self.assertNotIn("active_run", ns["STORE"].sessions["chat"])
        ns["revoke_cross_chat_capability"].assert_awaited_once_with("finished-run")
        ns["finalize_cross_chat_terminal"].assert_not_awaited()
        ns["append_event"].assert_awaited_once()

    async def test_failed_old_terminal_preserves_successor_and_cleanup_survives_cancellation(self):
        ns = storage_source()
        ns["ACTIVE"]["chat"] = {"run_id": "successor"}
        ns["STORE"].sessions["chat"]["active_run"] = {"run_id": "successor"}
        ns["append_event"].side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await ns["append_turn_finished_event"]("chat", {"run_id": "finished-run"})
        self.assertEqual(ns["STORE"].sessions["chat"]["active_run"], {"run_id": "successor"})
        self.assertEqual(ns["ACTIVE"]["chat"], {"run_id": "successor"})
        ns["revoke_cross_chat_capability"].assert_awaited_once_with("finished-run")

    async def test_successful_retry_uses_real_terminal_and_sdk_failure_does_not_drain_queue(self):
        ns = storage_source()
        event = {"run_id": "finished-run", "type": "turn_finished", "seq": 2}
        ns["append_event"].return_value = event
        self.assertIs(await ns["append_turn_finished_event"]("chat", {"run_id": "finished-run"}), event)
        ns["finalize_cross_chat_terminal"].assert_awaited_once_with(event)
        env = load_probe("iterator", shutting_down=False)
        env["should_schedule_queue_after_finish"] = lambda *_: True
        env["append_turn_finished_event"].side_effect = OSError(errno.ENOSPC, "Synthetic storage full")
        await env["probe"](RuntimeError("Synthetic stream ended"))
        env["release_turn_slot"].assert_awaited_once_with("isolated-chat", expected_run_id="exact-current-run")
        env["schedule_next_queued_turn"].assert_not_called()
        self.assertEqual(env["STOPPED_RUNS"], {"unrelated-run"})

    async def test_prelaunch_storage_error_is_retryable_but_not_committed_or_unrelated_errors(self):
        ns = storage_source()
        for code in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
            with self.assertRaises(ns["TransientAdmissionWait"]) as caught:
                ns["classify"](OSError(code, "Synthetic full"))
            self.assertEqual(caught.exception.status_code, 503)
            self.assertIn("No agent turn was started", caught.exception.detail)
            self.assertIsNone(ns["classify"](OSError(code, "Synthetic full"), {"seq": 1}))
        self.assertIsNone(ns["classify"](OSError(errno.EACCES, "Synthetic denied")))

    async def test_durable_batch_fsync_failure_rolls_back_and_raises(self):
        ns = storage_source()
        failure = OSError(errno.ENOSPC, "Synthetic fsync failure")
        ns["os"] = SimpleNamespace(fsync=Mock(side_effect=[failure, None]))
        with tempfile.TemporaryDirectory(prefix="storage-failure-") as temporary:
            path = Path(temporary) / "events.jsonl"
            original = b'{"seq":1,"type":"existing"}\n'
            path.write_bytes(original)
            with self.assertRaises(OSError) as caught:
                ns["append_durable_event_batch_sync"](path, "synthetic-chat", 2, [("turn_started", {"prompt": "Synthetic"})])
            self.assertIs(caught.exception, failure)
            self.assertEqual(path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
