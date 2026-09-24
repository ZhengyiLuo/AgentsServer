"""Old updater retry admission with real private retirement evidence files."""
import argparse
import hashlib
import json
import subprocess
import sys
import shutil
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import execution_activation as bridge
import execution_install as files
import execution_recovery as recovery
from update_recovery import activation_intent


class LegacyRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.root, self.state, self.home, self.config = [base / name for name in ("root", "state", "home", "config")]
        for path in (self.root, self.state, self.home, self.config, self.root / "releases"):
            path.mkdir(mode=0o700)
        self.first, self.second = [self.root / "releases" / name for name in ("candidate-old", "candidate-new")]
        for path in (self.first, self.second):
            path.mkdir(mode=0o700)
        self.args = argparse.Namespace(root=str(self.root), state_root=str(self.state), home=str(self.home),
            config_root=str(self.config), platform="Linux", release_dir=str(self.second),
            bind="127.0.0.1", port=7850, release_version="2.0.0", api_contract=28,
            candidate_source=str(self.second), managed_update_id="b" * 32,
            expected_server_identity="owned-server", update_file=str(self.state / "proof.json"))
        self.previous = activation_intent(root=self.root, candidate=self.first, version="2.0.0", api_contract=28,
            update_id="a" * 32, server_identity="owned-server")
        self.status = self.state / "admin/server-update.json"
        self.write(self.status, {"phase": "installing", "update_id": "b" * 32,
            "target_version": "2.0.0", "_activation_recovery": self.previous})
        self.write(Path(self.args.update_file), {"update_id": "b" * 32})
        self.services = mock.Mock()
        self.services.snapshot.return_value = {"worker": {"state": "running", "pid": 999}}
        self.transaction = "activation-" + "c" * 24
        self.directory = self.root / recovery.DIRECTORY / self.transaction
        self.directory.parent.mkdir(mode=0o700)
        self.directory.mkdir(mode=0o700)
        payload = {}
        # A retired prior bootstrap predates execution_http.py. It is checked
        # as data, never imported/executed and never resumed by this path.
        for name in set(recovery.PAYLOAD) - {"execution_http.py"}:
            path = self.directory / name
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_bytes(b"historical bootstrap fixture\n")
            path.chmod(0o600)
            payload[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        owner = dict.fromkeys(recovery.OWNER_KEYS)
        owner.update(format=1, root=str(self.root), root_binding=self.previous["root_binding"],
            transaction_id=self.transaction, version="2.0.0", api_contract=28,
            source_binding=self.previous["candidate_binding"], home=str(self.home), platform="Linux",
            state_root=str(self.state), config_root=str(self.config), expected_server_identity="owned-server",
            managed_update_id="a" * 32, payload=payload,
            status_binding={"format": 1, "path": str(self.status), "update_id": "a" * 32,
                "target_version": "2.0.0", "intent_sha256": hashlib.sha256(files._json_bytes(self.previous)).hexdigest()})
        self.write(self.directory / "owner.json", owner)
        self.write(self.directory / "terminal.json", {"format": 1, "transaction_id": self.transaction,
            "phase": "rollback-healthy", "snapshot": {"health": {"server_identity": "owned-server"}}})
        final = {"format": 1, "transaction_id": self.transaction,
                 "terminal_sha256": recovery._digest(self.directory / "terminal.json")}
        self.write(self.directory / "finalized.json", final)
        self.write(self.directory / "retired.json", final)
        patcher = mock.patch.object(recovery.RecoveryService, "running", return_value=False)
        self.native = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, value):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_bytes(files._json_bytes(value)); path.chmod(0o600)

    def reject_unchanged(self):
        before = self.status.read_bytes()
        with self.assertRaises((RuntimeError, ValueError, FileNotFoundError, PermissionError)):
            bridge.seed_legacy_recovery(self.args, self.services)
        self.assertEqual(self.status.read_bytes(), before)
        self.services.stop.assert_not_called()

    def test_new_admitted_update_replaces_only_exact_retired_rollback_intent(self):
        before = {p: p.read_bytes() for p in self.directory.iterdir()}
        bridge.seed_legacy_recovery(self.args, self.services)
        intent = json.loads(self.status.read_bytes())["_activation_recovery"]
        self.assertEqual(intent["update_id"], "b" * 32)
        self.assertEqual(intent["candidate_binding"]["inode"], self.second.stat().st_ino)
        bridge.seed_legacy_recovery(self.args, self.services)  # Same candidate is idempotent.
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.native.assert_called_once()
        self.services.stop.assert_not_called()

    def test_restored_monolith_with_dead_candidate_receipt_reseeds_retired_intent(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
        self.args.expected_native_pid = 999
        self.args.health_file = str(self.state / "health.json")
        self.write(Path(self.args.health_file), {"server_identity": "owned-server",
            "active_count": 0, "update_blocking_queued_count": 0,
            "execution_service": None, "gateway": None,
            "update_service_cgroup": {"safe": True, "unknown_descendant_count": 0}})
        self.write(Path(self.args.update_file), {"update_id": "b" * 32,
            "phase": "installing", "server_identity": "owned-server"})
        receipt = self.state / "execution/worker.json"
        self.write(receipt, {"schema": 1, "protocol": 1, "role": "worker", "pid": child.pid,
            "instance_id": "d" * 32, "release_root": str(self.first), "version": "2.0.0",
            "callback_origin": "http://127.0.0.1:12345", "public_bind": "127.0.0.1", "public_port": 7850})
        lock = receipt.with_name("worker.lock"); lock.touch(mode=0o600)
        original = receipt.read_bytes(), receipt.stat().st_ino
        control = bridge.WorkerControl()
        with mock.patch.object(control, "status", side_effect=AssertionError("no stale callback request")):
            bridge.verify_stop(self.args, {}, self.services, control)
        bridge.seed_legacy_recovery(self.args, self.services)
        self.assertEqual(json.loads(self.status.read_bytes())["_activation_recovery"]["update_id"], "b" * 32)
        self.native.assert_called_once()  # Real retired-owner evidence was validated.
        self.assertEqual((receipt.read_bytes(), receipt.stat().st_ino), original)
        self.services.stop.assert_not_called()

    def test_pending_or_linked_journal_prevents_replacement(self):
        for name in (".activation-transaction", ".execution-transaction", ".execution-uninstall.json"):
            with self.subTest(name=name):
                path = self.root / name; path.symlink_to(self.root / "missing")
                self.reject_unchanged(); path.unlink()

    def test_missing_or_changed_terminal_retirement_proof_refuses(self):
        for name in ("terminal.json", "finalized.json", "retired.json"):
            with self.subTest(name=name):
                path = self.directory / name; before = path.read_bytes(); path.unlink()
                self.reject_unchanged(); path.write_bytes(before); path.chmod(0o600)
        self.write(self.directory / "terminal.json", {"format": 1, "phase": "committed"})
        self.reject_unchanged()

    def test_still_running_or_registered_owner_refuses(self):
        self.native.return_value = True; self.reject_unchanged(); self.native.return_value = False
        owner = json.loads((self.directory / "owner.json").read_bytes())
        service = recovery._service_path(owner); service.parent.mkdir(parents=True)
        service.symlink_to(self.root / "missing"); self.reject_unchanged()

    def test_foreign_intent_and_same_update_different_candidate_refuse(self):
        for change in ({"server_identity": "foreign"}, {"update_id": "b" * 32},
                       {"candidate_binding": {**self.previous["candidate_binding"], "inode": 123}}):
            with self.subTest(change=change):
                status = json.loads(self.status.read_bytes()); status["_activation_recovery"] = {**self.previous, **change}
                self.write(self.status, status); self.reject_unchanged()

    def test_mismatched_new_native_admission_never_replaces_history(self):
        self.write(Path(self.args.update_file), {"update_id": "d" * 32})
        self.reject_unchanged(); self.native.assert_not_called()

    def test_changed_private_bootstrap_refuses(self):
        (self.directory / "execution_recovery.py").write_text("changed")
        self.reject_unchanged()

    def test_pre_arm_failure_retries_without_inventing_a_retirement_receipt(self):
        shutil.rmtree(self.directory.parent)
        bridge.seed_legacy_recovery(self.args, self.services)
        self.assertEqual(json.loads(self.status.read_bytes())["_activation_recovery"]["update_id"], "b" * 32)
        self.assertFalse(self.directory.parent.exists())
        self.native.assert_not_called()

    def test_pre_arm_retry_also_accepts_only_retired_earlier_history(self):
        status = json.loads(self.status.read_bytes())
        status["_activation_recovery"] = {**self.previous, "update_id": "d" * 32}
        self.write(self.status, status)
        bridge.seed_legacy_recovery(self.args, self.services)
        self.assertEqual(json.loads(self.status.read_bytes())["_activation_recovery"]["update_id"], "b" * 32)
        self.native.assert_called_once()
        self.write(self.status, status)
        self.native.return_value = True
        self.reject_unchanged()

    def test_partial_pre_arm_owner_directory_cannot_be_ignored(self):
        partial = self.directory.parent / ("." + self.transaction + ".partial.tmp")
        partial.mkdir(mode=0o700)
        self.reject_unchanged()


if __name__ == "__main__":
    unittest.main()
