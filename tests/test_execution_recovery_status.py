"""Real private files, retained journals and subprocesses; no native jobs."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import activation_transaction as activation
import execution_install as files
import execution_recovery_status as recovery
from tests import test_execution_activation_transaction as fixtures
from update_recovery import activation_intent


class RecoveryStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionActivationTransactionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.item = self.fixture.layout(split=True)
        self.update_id = "a" * 32
        self.path = self.item.state / "admin/server-update.json"
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        self.status = {"phase": "installing", "update_id": self.update_id,
            "target_version": self.item.release_version, "runner_pid": None,
            "updated_at": "2026-01-01T00:00:00Z", "message": "Installing fixture",
            "_activation_recovery": activation_intent(root=self.item.root,
                candidate=self.item.candidate_source, version=self.item.release_version,
                api_contract=28, update_id=self.update_id, server_identity="fixture-server")}
        self.write()
        self.transaction = self.fixture.begin(self.item, extra=("--execution-api-contract", "28"))
        self.context = activation.execution_context(self.item.root)

    def write(self):
        files._atomic_write(self.path, files._json_bytes(self.status))

    def capture(self, supplied=""):
        return recovery.capture_status_binding(self.item.root, self.context,
            supplied_update_id=supplied, expected_server_identity="fixture-server")

    def owner(self):
        return {"root": str(self.item.root), "state_root": str(self.item.state),
                "transaction_id": self.transaction, "version": self.item.release_version,
                "api_contract": 28, "expected_server_identity": "fixture-server",
                "status_binding": self.capture()}

    def terminal(self, *, committed=False):
        version = self.item.release_version if committed else "0.9.0"
        return {"format": 1, "transaction_id": self.transaction,
            "phase": "committed" if committed else "rollback-healthy",
            "snapshot": {"health": {"server_identity": "fixture-server", "api_contract_version": 28,
                "server_version": version, "worker_version": version, "gateway_version": version}}}

    def retire_journal(self):
        self.fixture.driver.rollback(self.item, self.transaction)
        self.fixture.driver.invoke("finish", *self.fixture.driver.owned_args(self.item, self.transaction))

    def test_old_mac_id_derives_only_from_bound_source_and_identity(self):
        self.assertEqual(self.capture()["update_id"], self.update_id)
        self.assertEqual(self.capture(self.update_id), self.capture())
        with self.assertRaises(RuntimeError):
            self.capture("b" * 32)
        for key, value in (("server_identity", "other-server"), ("api_contract", 29), ("version", "99.0.0")):
            with self.subTest(key=key):
                original = copy.deepcopy(self.status)
                self.status["_activation_recovery"][key] = value
                self.write()
                with self.assertRaises(RuntimeError):
                    self.capture()
                self.status = original
                self.write()
        self.status["_activation_recovery"]["candidate_binding"]["inode"] += 1
        self.write()
        with self.assertRaises(RuntimeError):
            self.capture()

    def test_manual_install_does_not_adopt_historical_receipt(self):
        self.status["phase"] = "complete"
        self.write()
        self.assertIsNone(self.capture())
        with self.assertRaises(RuntimeError):
            self.capture(self.update_id)
        self.path.unlink()
        self.assertIsNone(self.capture())
        with self.assertRaises(RuntimeError):
            self.capture(self.update_id)

    def test_active_unbound_update_is_not_guessed(self):
        self.status.pop("_activation_recovery")
        self.write()
        with self.assertRaises(RuntimeError):
            self.capture()

    def test_finalized_rollback_settles_once_without_reporting_success(self):
        owner = self.owner()
        self.retire_journal()
        self.assertTrue(recovery.settle_status(owner, self.terminal()))
        result = json.loads(self.path.read_text())
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(result["error_code"], "server_update_rolled_back")
        self.assertTrue(result["retryable"])
        self.assertEqual(result["_activation_recovery"], self.status["_activation_recovery"])
        before = self.path.read_bytes()
        self.assertTrue(recovery.settle_status(owner, self.terminal()))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_completion_requires_actual_paired_terminal_proof(self):
        owner, terminal = self.owner(), self.terminal(committed=True)
        self.retire_journal()
        terminal["snapshot"]["health"]["gateway_version"] = "0.9.0"
        with self.assertRaises(RuntimeError):
            recovery.settle_status(owner, terminal)
        self.assertEqual(json.loads(self.path.read_text())["phase"], "installing")
        self.assertTrue(recovery.settle_status(owner, self.terminal(committed=True)))
        result = json.loads(self.path.read_text())
        self.assertEqual(result["phase"], "complete")
        self.assertEqual(result["installed_version"], self.item.release_version)
        self.assertFalse(result["update_available"])

    def test_original_live_updater_keeps_settlement_ownership(self):
        owner = self.owner()
        self.retire_journal()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)",
                                  "update_runner.py", "--update-id", self.update_id],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            self.status["runner_pid"] = child.pid
            self.write()
            before = self.path.read_bytes()
            self.assertFalse(recovery.settle_status(owner, self.terminal()))
            self.assertEqual(self.path.read_bytes(), before)
        finally:
            child.terminate()
            child.wait(timeout=5)
        self.assertTrue(recovery.settle_status(owner, self.terminal()))

    def test_reused_pid_of_unrelated_process_cannot_pin_the_update(self):
        owner = self.owner()
        self.retire_journal()
        self.status["runner_pid"] = os.getpid()
        self.write()
        self.assertTrue(recovery.settle_status(owner, self.terminal()))
        self.assertEqual(json.loads(self.path.read_text())["phase"], "failed")

    def test_changed_status_owner_and_terminal_result_are_not_overwritten(self):
        owner = self.owner()
        self.retire_journal()
        for updates in ({"update_id": "b" * 32}, {"phase": "failed", "message": "Exact original health failure"}):
            with self.subTest(updates=updates):
                previous = dict(self.status)
                self.status.update(updates)
                self.write()
                before = self.path.read_bytes()
                self.assertTrue(recovery.settle_status(owner, self.terminal()))
                self.assertEqual(self.path.read_bytes(), before)
                self.status = previous
        self.status["_activation_recovery"]["server_identity"] = "changed"
        self.write()
        with self.assertRaises(RuntimeError):
            recovery.settle_status(owner, self.terminal())

    def test_retained_journal_and_linked_status_files_cannot_be_settled(self):
        owner = self.owner()
        with self.assertRaisesRegex(RuntimeError, "transaction still owns"):
            recovery.settle_status(owner, self.terminal())
        self.retire_journal()
        another = self.path.with_name("shared-status.json")
        os.link(self.path, another)
        with self.assertRaises(PermissionError):
            recovery.settle_status(owner, self.terminal())


if __name__ == "__main__":
    unittest.main()
