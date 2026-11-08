"""Detached-runner/status reconciliation races with isolated durable receipts.

These exercise real server callers and runner receipt writes. Liveness, service
operations and maintenance collaborators are mocked; no native update is run.
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import agent_server
import update_recovery
import update_runner
from fastapi import HTTPException


class ServerUpdateFinalizerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = root / "status.json"
        self.enterContext(patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(root)}))
        for name, value in (
            ("SERVER_UPDATE_STATUS_FILE", self.path),
            ("SERVER_VERSION", "1.0.0"),
            ("SERVER_UPDATE_OPERATION_LOCK", asyncio.Lock()),
        ):
            self.enterContext(patch.object(agent_server, name, value))
        self.enterContext(patch.object(agent_server, "managed_server_restart_blocks_work", return_value=False))
        self.enterContext(patch.object(agent_server, "managed_update_provider_quiesce_failed", return_value=False))
        self.enterContext(patch.object(agent_server, "resume_server_update_activation", new=AsyncMock(return_value=None)))
        self.enterContext(patch.object(agent_server, "server_update_status_age_seconds", return_value=60.0))
        self.clear_fence = self.enterContext(patch.object(agent_server, "_clear_exact_server_update_team_hub_fence"))
        self.reconcile_fence = self.enterContext(patch.object(agent_server, "_reconcile_server_update_team_hub_fence"))
        self.journal = self.enterContext(patch.object(update_recovery, "journal_present", return_value=True))
        self.maintenance = MagicMock()
        self.enterContext(patch.object(agent_server, "EXECUTION_MAINTENANCE", self.maintenance, create=True))
        self.jobs = AsyncMock()
        self.enterContext(patch.object(agent_server.JOBS, "resume_update_parked", self.jobs))
        self.enterContext(patch.object(agent_server.TERMINAL_ATTACHMENTS, "reopen_if_update_inactive", new=AsyncMock(return_value=False)))
        self.manifest = self.enterContext(patch.object(agent_server, "signed_release_manifest", new=AsyncMock(
            side_effect=AssertionError("a progressed updater must not start a new release check"))))
        self.launch = self.enterContext(patch.object(agent_server, "run_tmux", side_effect=AssertionError(
            "a progressed updater must not launch a replacement")))

    def write_initial(self, **changes):
        agent_server.atomic_update_json(self.path, {
            "update_id": "owned-update", "phase": "checking", "track": "stable",
            "target_version": "1.1.0", "runner_pid": 424242,
            "heartbeat_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z",
            "_execution_handoff": {"operation_id": "owned-operation", "lease_id": "owned-lease"},
            **changes,
        })
        return agent_server.read_server_update_status()

    async def test_callers_preserve_progress_written_during_failed_probe(self):
        changes = {
            "timestamp only": {},
            "heartbeat": {"heartbeat_at": "2026-01-01T00:01:00Z"},
            "runner PID": {"runner_pid": 424243},
            "phase": {"phase": "verifying"},
            "recovery ownership": {"_activation_recovery": {"update_id": "owned-update", "journal": "new"}},
            "execution ownership": {"_execution_handoff": {"operation_id": "new-operation", "lease_id": "new-lease"}},
        }
        calls = {
            "status": agent_server.server_update_status,
            "startup": agent_server.reconcile_server_update_status_after_startup,
            "check": agent_server.check_server_update,
            "start": lambda: agent_server.start_server_update(agent_server.ServerUpdateRequest(version="1.1.0")),
        }
        for change_name, change in changes.items():
            for caller, call in calls.items():
                with self.subTest(change=change_name, caller=caller):
                    original = self.write_initial()
                    for mock in (self.clear_fence, self.reconcile_fence, self.journal, self.maintenance, self.jobs):
                        mock.reset_mock()
                    progressed = None
                    def failed_probe(snapshot):
                        nonlocal progressed
                        self.assertEqual(snapshot, original)
                        update_runner.update_status(self.path, expected_update_id="owned-update", **change)
                        progressed = self.path.read_bytes()
                        return False
                    with patch.object(agent_server, "server_update_is_active", side_effect=failed_probe) as probe, \
                         patch.object(update_runner, "utc_now", return_value=(
                             "2026-01-01T00:01:00Z" if change_name == "timestamp only" else original["updated_at"])), \
                         patch.object(agent_server, "_write_server_update_status_unlocked", wraps=agent_server._write_server_update_status_unlocked) as write:
                        if caller in {"check", "start"}:
                            with self.assertRaises(HTTPException) as raised:
                                await call()
                            self.assertEqual(raised.exception.status_code, 409)
                        else:
                            result = await call()
                            self.assertEqual(result["phase"], change.get("phase", "checking"))
                        probe.assert_called_once()
                        write.assert_not_called()
                    self.assertEqual(self.path.read_bytes(), progressed)
                    self.clear_fence.assert_not_called()
                    # Startup has one pre-probe reconciliation; finalization must add none.
                    self.assertEqual(self.reconcile_fence.call_count, int(caller == "startup"))
                    self.journal.assert_not_called()
                    self.assertEqual(self.maintenance.mock_calls, [])
                    self.jobs.assert_not_awaited()
                    self.manifest.assert_not_awaited()
                    self.launch.assert_not_called()

    def test_replaced_or_removed_recovery_ownership_skips_stale_journal(self):
        for replacement in (None, {"update_id": "owned-update", "journal": "replacement"}):
            with self.subTest(replacement=replacement):
                original = self.write_initial(_activation_recovery={"update_id": "owned-update", "journal": "old"})
                update_runner.update_status(self.path, expected_update_id="owned-update", _activation_recovery=replacement)
                progressed = self.path.read_bytes()
                current = agent_server.read_server_update_status()
                self.assertEqual(agent_server.finalize_abandoned_server_update(original), current)
                self.assertEqual(self.path.read_bytes(), progressed)
                self.journal.assert_not_called()
                self.clear_fence.assert_not_called()
                self.reconcile_fence.assert_not_called()

    async def test_check_preserves_new_pending_reservation_after_failed_probe(self):
        self.write_initial()
        def failed_probe(_snapshot):
            update_runner.update_status(self.path, expected_update_id="owned-update",
                phase="pending", update_id=None, schedule_id="new-schedule")
            return False
        with patch.object(agent_server, "server_update_is_active", side_effect=failed_probe):
            result = await agent_server.check_server_update()
        self.assertEqual(result["phase"], "pending")
        self.assertEqual(result["schedule_id"], "new-schedule")
        self.manifest.assert_not_awaited()
        self.clear_fence.assert_not_called()
        self.reconcile_fence.assert_not_called()

    def test_superseded_or_terminal_receipt_is_preserved(self):
        for change in ({"update_id": "new-update"}, {"phase": "complete"}, {"phase": "failed"}):
            with self.subTest(change=change):
                original = self.write_initial()
                update_runner.update_status(self.path, expected_update_id="owned-update", **change)
                current = agent_server.read_server_update_status()
                before = self.path.read_bytes()
                self.assertEqual(agent_server.finalize_abandoned_server_update(original), current)
                self.assertEqual(self.path.read_bytes(), before)
                self.clear_fence.assert_not_called()
                self.reconcile_fence.assert_not_called()

    def test_unchanged_dead_runner_still_finalizes_exact_hold(self):
        original = self.write_initial()
        result = agent_server.finalize_abandoned_server_update(original)
        self.assertEqual(result["phase"], "failed")
        self.assertIn("detached updater exited", result["message"])
        self.assertIsNone(result["runner_pid"])
        self.assertEqual(result["error_code"], "server_update_handoff_release_failed")
        self.assertEqual(result["_execution_handoff"], original["_execution_handoff"])
        self.clear_fence.assert_called_once_with(original)
        self.reconcile_fence.assert_called_once_with(original)
        self.journal.assert_not_called()

    def test_unchanged_activation_journal_keeps_recovery_guard(self):
        original = self.write_initial(_activation_recovery={"update_id": "owned-update", "journal": "retained"})
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "retained installer recovery"):
            agent_server.finalize_abandoned_server_update(original)
        self.assertEqual(self.path.read_bytes(), before)
        self.journal.assert_called_once()
        self.clear_fence.assert_not_called()
        self.reconcile_fence.assert_not_called()


if __name__ == "__main__":
    unittest.main()
