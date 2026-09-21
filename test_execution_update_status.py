"""Completion proof with actual layout files and a controlled native probe."""
import json
import unittest
from unittest.mock import Mock, patch

import execution_update_status as status
import test_execution_install as install_tests


class ExecutionUpdateStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = install_tests.ExecutionInstallTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.migrate()
        self.root = self.fixture.root
        self.version = (self.fixture.old / "VERSION").read_text().strip()
        self.receipt = {"worker_version": self.version, "gateway_version": self.version,
                        "server_identity": "owned-server", "worker_instance_id": "owned-worker", "maintenance_held": False}
        self.control = Mock()
        self.control.receipt.return_value = self.receipt
        self.addCleanup(patch.stopall)
        patch.object(status, "WorkerControl", return_value=self.control).start()
        self.services = patch.object(status, "NativeServices").start()

    def observe(self, target=None):
        return status.current_components(self.root, target_version=target or self.version,
            expected_server_identity="owned-server", expected_worker_instance="owned-worker")

    def test_complete_requires_both_running_versions(self):
        self.assertTrue(self.observe())
        newer = (self.fixture.new / "VERSION").read_text().strip()
        self.receipt["worker_version"] = newer
        self.assertFalse(self.observe(newer))
        self.receipt["gateway_version"] = newer
        self.assertTrue(self.observe(newer))
        self.receipt["worker_version"] = self.version
        self.assertFalse(self.observe(newer))

    def test_unfinished_journal_prevents_completion_before_native_probes(self):
        for name in (".activation-transaction", ".execution-transaction"):
            with self.subTest(name=name):
                journal = self.root / name
                journal.mkdir()
                self.assertFalse(self.observe())
                self.control.receipt.assert_not_called()
                journal.rmdir()

    def test_held_or_missing_admission_proof_does_not_count_as_complete(self):
        for held in (True, None):
            self.receipt["maintenance_held"] = held
            self.assertFalse(self.observe())

    def test_broken_journal_link_is_not_ignored(self):
        (self.root / ".activation-transaction").symlink_to(self.root / "missing")
        self.assertFalse(self.observe())
        self.services.assert_not_called()

    def test_new_activation_during_probe_prevents_completion(self):
        def receipt(*unused):
            (self.root / ".activation-transaction").mkdir()
            return self.receipt
        self.control.receipt.side_effect = receipt
        self.assertFalse(self.observe())

    def test_different_server_or_worker_epoch_is_rejected(self):
        for key in ("server_identity", "worker_instance_id"):
            with self.subTest(key=key):
                old = self.receipt[key]
                self.receipt[key] = "another-owner"
                with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                    self.observe()
                self.receipt[key] = old

    def test_native_probe_failure_never_falls_back_to_installed_version(self):
        self.control.receipt.side_effect = RuntimeError("native gateway PID changed")
        with self.assertRaisesRegex(RuntimeError, "PID changed"):
            self.observe()

    def test_invalid_layout_is_rejected_before_probe(self):
        path = self.root / "execution-layout.json"
        value = json.loads(path.read_text())
        value["gateway_version"] = "99.0.0"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.observe()
        self.control.receipt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
