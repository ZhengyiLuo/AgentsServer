import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from execution_control import ExecutionControlError
from execution_maintenance import ExecutionMaintenance


class RetirementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "admin" / "maintenance.json"
        self.now = 1_000.0
        self.manager = ExecutionMaintenance(self.path, "worker-epoch", clock=lambda: self.now)
        self.operation = str(uuid.uuid4())

    def acquire(self):
        return self.manager.apply("acquire", self.operation, None, 30, {})["lease"]

    def test_busy_worker_cannot_be_retired(self):
        with self.assertRaises(ExecutionControlError):
            self.manager.apply("acquire", self.operation, None, 30, {"active_runs": 1})
        self.assertFalse(self.manager.is_held())
        self.assertFalse(self.path.exists())

    def test_expired_preparation_cannot_authorize_stop(self):
        lease = self.acquire()
        self.assertTrue(self.manager.is_held())
        self.now += 31
        self.assertFalse(self.manager.is_held())
        with self.assertRaises(ExecutionControlError):
            self.manager.apply("seal", self.operation, lease["lease_id"], 30, {})
        replacement = self.manager.apply("acquire", str(uuid.uuid4()), None, 30, {})
        self.assertNotEqual(replacement["lease"]["lease_id"], lease["lease_id"])

    def test_sealed_admission_survives_coordinator_delay_and_only_exact_owner_can_release(self):
        lease = self.acquire()
        self.manager.apply("seal", self.operation, lease["lease_id"], 30, {})
        self.now += 1_000_000
        self.assertTrue(self.manager.is_held())
        restored = ExecutionMaintenance(self.path, "worker-epoch", clock=lambda: self.now)
        self.assertTrue(restored.is_held())
        with self.assertRaises(ExecutionControlError):
            restored.apply("release", str(uuid.uuid4()), lease["lease_id"], 30, {})
        with self.assertRaises(ExecutionControlError):
            restored.apply("release", self.operation, str(uuid.uuid4()), 30, {})
        self.assertTrue(restored.is_held())
        result = restored.apply("release", self.operation, lease["lease_id"], 30, {})
        self.assertIsNone(result["lease"])
        self.assertFalse(restored.is_held())

    def test_a_late_background_child_prevents_sealing(self):
        lease = self.acquire()
        with self.assertRaises(ExecutionControlError):
            self.manager.apply("seal", self.operation, lease["lease_id"], 30, {"provider_background_tasks": 1})
        self.assertFalse(self.manager.lease["sealed"])

    def test_failed_persistence_does_not_authorize_retirement(self):
        lease = self.acquire()
        with patch("execution_maintenance.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                self.manager.apply("seal", self.operation, lease["lease_id"], 30, {})
        self.assertFalse(self.manager.lease["sealed"])
        self.assertFalse(json.loads(self.path.read_text())["lease"]["sealed"])

    def test_new_epoch_requires_its_own_startup_hold_and_cannot_reuse_stop_authority(self):
        old = self.acquire()
        self.manager.apply("seal", self.operation, old["lease_id"], 30, {})
        replacement = ExecutionMaintenance(self.path, "new-worker", clock=lambda: self.now)
        self.assertFalse(replacement.is_held())
        replacement.hold_for_startup(self.operation)
        self.assertTrue(replacement.is_held())
        self.assertNotEqual(replacement.lease["lease_id"], old["lease_id"])
        with self.assertRaises(ExecutionControlError):
            replacement.apply("release", self.operation, old["lease_id"], 30, {})

    def test_duplicate_acquire_is_idempotent_without_extending_expiry(self):
        first = self.acquire()
        self.now += 2
        self.assertEqual(self.acquire(), first)
        with self.assertRaises(ExecutionControlError):
            self.manager.apply("acquire", str(uuid.uuid4()), None, 30, {})


if __name__ == "__main__":
    unittest.main()
