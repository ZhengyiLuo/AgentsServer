"""Update status concurrency tests using only allowlisted server AST functions.

No server import, endpoint invocation, real status files, downloads or runtime
state. Every collaborator is a fake except asyncio's local task/lock primitives.
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "server_update_status", "check_server_update", "require_server_update_target",
    "server_update_error_detail", "public_server_update_status",
    "bounded_lock",
}


class FakeHTTPException(Exception):
    def __init__(self, *, status_code, detail):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


def load_functions():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS]
    if {node.name for node in nodes} != FUNCTIONS:
        raise AssertionError("The isolated update status helper allowlist is incomplete")
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *nodes,
    ], type_ignores=[]))
    return compile(module, str(SOURCE), "exec")


class ServerUpdateStatusResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # Reading/parsing source is the only filesystem access in these tests.
        cls.compiled = load_functions()

    async def asyncSetUp(self):
        self.lock = asyncio.Lock()
        self.status = {
            "phase": "complete", "track": "beta", "current_version": "1.0.0-beta.1",
            "target_version": "1.0.0-beta.1", "update_id": "completed-update",
            "finished_at": "2026-09-09T22:00:00Z",
            "message": "The last update completed.",
            "_force_restart_request_id": "private-request",
            "_force_restart_requested_at": "private-time",
        }
        self.read = Mock(side_effect=lambda: dict(self.status))
        self.write = Mock(side_effect=lambda **changes: self._write(changes))
        self.finalize = Mock(return_value={"phase": "failed", "message": "Orphan finalized."})
        self.reopen = AsyncMock(return_value=True)
        self.resume = AsyncMock()
        self.manifest = AsyncMock(return_value={"version": "1.0.0-beta.2"})
        self.quiesce_failure_write = Mock(return_value={"phase": "failed", "error_code": "provider_quiesce_failed"})
        self.namespace = {
            "asyncio": asyncio, "asynccontextmanager": asynccontextmanager,
            "HTTPException": FakeHTTPException,
            "SERVER_UPDATE_OPERATION_LOCK": self.lock,
            "SERVER_RESTART_STATUS_LOCK_TIMEOUT_SECONDS": 0.025,
            "SERVER_INSTANCE_ID": "boot-current", "server_identity": Mock(return_value="server-current"),
            "SERVER_VERSION": "1.0.0-beta.1", "SERVER_UPDATE_ACTIVE_PHASES": {"starting", "downloading", "installing"},
            "SERVER_UPDATE_START_GRACE_SECONDS": 45.0,
            "read_server_update_status": self.read,
            "write_server_update_status": self.write,
            "write_fresh_server_update_status": self.write,
            "managed_server_restart_blocks_work": Mock(return_value=False),
            "managed_update_provider_quiesce_failed": Mock(return_value=False),
            "ensure_managed_update_provider_quiesce_failure_status": self.quiesce_failure_write,
            "managed_server_update_is_pending": Mock(side_effect=lambda status: status.get("phase") == "pending"),
            "managed_server_update_blocks_work": Mock(side_effect=lambda status: status.get("phase") in {"starting", "downloading", "installing"}),
            "server_update_is_active": Mock(return_value=False),
            "server_update_status_age_seconds": Mock(return_value=60.0),
            "finalize_abandoned_server_update": self.finalize,
            "signed_release_manifest": self.manifest,
            "server_release_track": Mock(return_value="beta"),
            "server_release_transition_allowed": Mock(return_value=True),
            "update_utc_now": Mock(return_value="2026-09-09T22:01:00Z"),
            "TERMINAL_ATTACHMENTS": SimpleNamespace(reopen_if_update_inactive=self.reopen),
            "JOBS": SimpleNamespace(resume_update_parked=self.resume),
            "logger": Mock(),
        }
        exec(self.compiled, self.namespace)

    def _write(self, changes):
        self.assertTrue(self.lock.locked(), "a status mutation escaped the operation lock")
        self.status.update(changes)
        return dict(self.status)

    def assert_no_reconciliation(self):
        self.write.assert_not_called()
        self.finalize.assert_not_called()
        self.quiesce_failure_write.assert_not_called()
        self.reopen.assert_not_awaited()
        self.resume.assert_not_awaited()

    async def status_response(self, identity="server-current", instance="boot-current"):
        return await asyncio.wait_for(self.namespace["server_update_status"](identity, instance), 0.25)

    async def test_status_returns_durable_receipt_while_actual_check_waits_for_manifest(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def stalled_manifest(track):
            self.assertEqual(track, "beta")
            self.assertTrue(self.lock.locked())
            entered.set()
            await release.wait()
            return {"version": "1.0.0-beta.2"}

        self.manifest.side_effect = stalled_manifest
        checking = asyncio.create_task(self.namespace["check_server_update"](SimpleNamespace(
            expected_server_identity="server-current", expected_server_instance_id="boot-current", track="beta")))
        try:
            await asyncio.wait_for(entered.wait(), 0.25)
            receipt = await self.status_response()
            self.assertFalse(checking.done(), "status must not wait for metadata completion")
            self.assertEqual(receipt["phase"], "complete")
            self.assertEqual(receipt["update_id"], "completed-update")
            self.assertEqual(receipt["target_version"], "1.0.0-beta.1")
            self.assertEqual(receipt["server_identity"], "server-current")
            self.assertEqual(receipt["server_instance_id"], "boot-current")
            self.assertNotIn("_force_restart_request_id", receipt)
            self.assertNotIn("_force_restart_requested_at", receipt)
            self.assert_no_reconciliation()
            self.manifest.assert_awaited_once_with("beta")
            self.assertTrue(self.lock.locked(), "status must not release another operation's lock")
        finally:
            release.set()
            await asyncio.wait_for(checking, 0.25)
        self.write.assert_called_once()
        self.assertEqual(self.status["latest_version"], "1.0.0-beta.2")
        self.assertFalse(self.lock.locked())

    async def test_busy_status_rejects_stale_and_incomplete_targets_before_state_access(self):
        async with self.lock:
            for identity, instance, code in (
                ("wrong-server", "boot-current", 409), ("server-current", "wrong-boot", 409),
                ("server-current", None, 400), (None, "boot-current", 400),
            ):
                with self.subTest(identity=identity, instance=instance):
                    with self.assertRaises(FakeHTTPException) as caught:
                        await self.status_response(identity, instance)
                    self.assertEqual(caught.exception.status_code, code)
            self.read.assert_not_called()
            self.assert_no_reconciliation()
            self.manifest.assert_not_awaited()

    async def queued_waiter_status(self, *, change_identity=False):
        queued, holding, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def competitor():
            queued.set()
            async with self.lock:
                if change_identity:
                    self.namespace["server_identity"].return_value = "replacement-server"
                holding.set()
                await release.wait()

        async def handoff():
            await self.lock.acquire()
            waiting = asyncio.create_task(competitor())
            try:
                await queued.wait()
                self.lock.release()
                self.assertFalse(self.lock.locked(), "exercise the released-but-queued lock window")
                # Enter directly in this task, before the queued waiter runs.
                # A wait_for around just status would schedule it too late to
                # exercise asyncio.Lock's fairness handoff window.
                response = await self.namespace["server_update_status"]("server-current", "boot-current")
                self.assertTrue(holding.is_set())
                self.assertTrue(self.lock.locked(), "snapshot fallback released the competitor's lock")
                return response
            finally:
                release.set()
                await waiting

        return await asyncio.wait_for(handoff(), 0.25)

    async def test_released_lock_with_prior_waiter_uses_bounded_snapshot_fallback(self):
        response = await self.queued_waiter_status()
        self.assertEqual(response["update_id"], "completed-update")
        self.assert_no_reconciliation()
        self.manifest.assert_not_awaited()
        self.assertFalse(self.lock.locked())

    async def test_bounded_wait_revalidates_identity_before_snapshot_read(self):
        with self.assertRaises(FakeHTTPException) as caught:
            await self.queued_waiter_status(change_identity=True)
        self.assertEqual(caught.exception.status_code, 409)
        self.read.assert_not_called()
        self.assert_no_reconciliation()

    async def test_busy_active_failed_and_pending_snapshots_never_reconcile_or_write(self):
        self.namespace["managed_update_provider_quiesce_failed"].return_value = True
        async with self.lock:
            for phase in ("downloading", "failed", "pending"):
                with self.subTest(phase=phase):
                    self.status.update(phase=phase, error_code="existing-receipt", schedule_id="exact-schedule")
                    receipt = await self.status_response()
                    self.assertEqual(receipt["phase"], phase)
                    self.assertEqual(receipt["error_code"], "existing-receipt")
            self.assert_no_reconciliation()
            self.namespace["managed_update_provider_quiesce_failed"].assert_not_called()
            self.namespace["server_update_is_active"].assert_not_called()
            self.manifest.assert_not_awaited()

    async def test_unbound_legacy_read_compatibility_is_preserved_while_busy(self):
        async with self.lock:
            receipt = await self.status_response(None, None)
        self.assertEqual(receipt["server_identity"], "server-current")
        self.assert_no_reconciliation()

    async def test_unlocked_status_rechecks_target_and_keeps_reconciliation_locked(self):
        validate = Mock(wraps=self.namespace["require_server_update_target"])
        self.namespace["require_server_update_target"] = validate

        def locked_read():
            self.assertTrue(self.lock.locked())
            return dict(self.status)

        async def locked_resume():
            self.assertTrue(self.lock.locked())

        self.read.side_effect = locked_read
        self.resume.side_effect = locked_resume
        receipt = await self.status_response()
        self.assertEqual(validate.call_count, 2)
        self.reopen.assert_awaited_once_with(receipt)
        self.resume.assert_awaited_once_with()
        self.finalize.assert_not_called()
        self.write.assert_not_called()
        self.assertFalse(self.lock.locked())

    async def test_unlocked_pending_status_rearms_only_its_schedule(self):
        self.status.update(phase="pending", schedule_id="exact-schedule")
        await self.status_response()
        self.resume.assert_awaited_once_with(active_schedule_id="exact-schedule")
        self.write.assert_not_called()

    async def test_unlocked_orphan_status_still_finalizes_then_reopens(self):
        self.status["phase"] = "downloading"
        receipt = await self.status_response()
        self.finalize.assert_called_once()
        self.assertEqual(receipt["phase"], "failed")
        self.reopen.assert_awaited_once_with(receipt)
        self.resume.assert_awaited_once_with()

    async def test_unlocked_quiesce_failure_keeps_existing_failure_reconciliation(self):
        self.namespace["managed_update_provider_quiesce_failed"].return_value = True
        receipt = await self.status_response()
        self.assertEqual(receipt["error_code"], "provider_quiesce_failed")
        self.quiesce_failure_write.assert_called_once()
        self.reopen.assert_not_awaited()
        self.resume.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
