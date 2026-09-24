"""Format-3 outer journal recovery across both native jobs and retained workers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock
import uuid

import activation_transaction as activation
import execution_install as execution
from tests import test_activation_transaction as support


class ExecutionActivationTransactionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="execution-activation-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        # Reuse the existing driver's helpers without inheriting its test suite.
        self.driver = support.ActivationTransactionTests()
        self.index = 0

    def layout(self, *, split=False, fresh=False, same_version=False, worker_current=False):
        self.index += 1
        item = support.ActivationLayout(self.base / str(self.index), fresh_install=fresh, same_version=same_version,
            previous=not fresh, env_exists=not fresh, service_exists=not fresh,
            service_mode=0o640, intent="server-update")
        item.state = item.base / "state"
        item.state.mkdir(mode=0o700)
        item.runtime = item.state / "execution"
        item.gateway = item.service.with_name("agents-server-gateway.service")
        item.execution_layout = item.root / "execution-layout.json"
        item.split = split
        item.fresh = fresh
        item.worker = item.old_source
        for path in (item.candidate_source, item.old_source, item.previous_release):
            if path is not None:
                self.runtime_files(path)
        if split:
            if not worker_current:
                item.worker = item.releases / "0.8.0"
                support.ActivationLayout.create_release(item.worker, "0.8.0", marker="retained worker\n")
                self.runtime_files(item.worker)
            support.ActivationLayout.write_file(item.gateway, b"[Service]\nExecStart=/old-gateway\n", 0o640)
            item.prior_layout = execution.ExecutionLayout(item.root, item.config_root, item.state,
                item.base, "Linux", item.worker, item.old_source, "127.0.0.1", 17850)
            support.ActivationLayout.write_file(item.execution_layout,
                execution._json_bytes(execution.layout_manifest(item.prior_layout)), 0o600)
        item.originals = {kind: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None
                          for kind, path in self.configs(item).items()}
        return item

    @staticmethod
    def runtime_files(path):
        (path / "execution_service.py").write_text("# inert fixture\n")
        (path / ".venv/bin").mkdir(parents=True)
        (path / ".venv/bin/python").symlink_to(Path(sys.executable).resolve())

    @staticmethod
    def configs(item):
        return {"env": item.env, "service": item.service, "gateway": item.gateway,
                "execution_layout": item.execution_layout}

    def begin(self, item, *, extra=()):
        return self.driver.invoke("begin", *self.driver.layout_args(item),
            "--release-dir", str(item.release_dir), "--release-version", item.release_version,
            "--old-source", str(item.old_source or ""), "--old-target", str(item.old_target or ""),
            "--candidate-source", str(item.candidate_source),
            "--service-state", "absent" if item.fresh else "running",
            "--service-enabled", "false" if item.fresh else "true",
            "--legacy-service-state", "absent", "--legacy-service-enabled", "false",
            "--prior-port", "17850", "--prior-bind-address", "127.0.0.1", "--intent", item.intent,
            "--client-binding", "owned-client-fixture", "--execution-runtime-dir", str(item.runtime),
            "--gateway-service", str(item.gateway), "--gateway-state", "running" if item.split else "absent",
            "--gateway-enabled", "true" if item.split else "false", *extra).strip()

    def publish(self, item, transaction, kind, *, content=None):
        path = self.configs(item)[kind]
        source = path.parent / f".{path.name}.activation-{transaction}-{kind}.source"
        content = content if content is not None else ("candidate " + kind + "\n").encode()
        # Source files are always private until the publication step fchmods.
        support.ActivationLayout.write_file(source, content, 0o600)
        self.driver.invoke("replace-config", *self.driver.owned_args(item, transaction),
            "--kind", kind, "--source", str(source), "--mode", "600" if kind in {"env", "execution_layout"} else "644")
        return content

    def publish_all(self, item, transaction):
        return {kind: self.publish(item, transaction, kind) for kind in self.configs(item)}

    def rollback(self, item, transaction):
        self.driver.rollback(item, transaction)
        for kind, path in self.configs(item).items():
            expected = item.originals[kind]
            if expected is None:
                self.assertFalse(path.exists())
            else:
                self.assertEqual((path.read_bytes(), stat.S_IMODE(path.stat().st_mode)), expected)
        if item.fresh:
            self.assertFalse(item.current.exists())
        else:
            self.assertEqual(item.current.resolve(), item.old_source)
        self.driver.invoke("finish", *self.driver.owned_args(item, transaction))
        self.assertFalse((item.root / ".activation-transaction").exists())

    def test_legacy_fresh_and_split_snapshot_every_configuration(self):
        for kind in ("legacy", "fresh", "split"):
            with self.subTest(kind=kind):
                item = self.layout(split=kind == "split", fresh=kind == "fresh")
                transaction = self.begin(item)
                value = activation.execution_context(item.root)
                self.assertEqual(value["format"], 3)
                self.assertEqual(value["execution"]["runtime_dir"], str(item.runtime))
                self.assertEqual(value["execution"]["gateway_state"], "running" if kind == "split" else "absent")
                expected_worker = str(item.worker) if item.worker is not None else None
                self.assertEqual(value["execution"]["old_worker_release"].get("source"), expected_worker)
                for name, expected in item.originals.items():
                    self.assertEqual(value[name]["existed"], expected is not None)
                    if expected:
                        backup = item.root / ".activation-transaction" / f"{name}.backup"
                        self.assertEqual(backup.read_bytes(), expected[0])
                        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                        self.assertEqual(value[name]["mode"], expected[1])
                self.rollback(item, transaction)

    def test_prelink_quiescence_recovery_preserves_exact_old_layout(self):
        for phase in ("quiescing", "quiesced"):
            for split in (False, True):
                with self.subTest(phase=phase,split=split):
                    item=self.layout(split=split)
                    transaction=self.begin(item)
                    self.driver.invoke("record", *self.driver.owned_args(item,transaction),"--phase","quiescing")
                    if phase == "quiesced":
                        self.driver.invoke("record", *self.driver.owned_args(item,transaction),"--phase","quiesced")
                    value=activation.execution_context(item.root)
                    self.assertEqual(value["phase"],phase)
                    self.assertEqual(item.current.resolve(),item.old_source)
                    self.assertTrue(item.candidate_source.exists())
                    self.rollback(item,transaction)

    def test_quiesced_split_can_continue_exact_link_takeover(self):
        item=self.layout(split=True)
        transaction=self.begin(item)
        for phase in ("quiescing","quiesced"):
            self.driver.invoke("record", *self.driver.owned_args(item,transaction),"--phase",phase)
        self.driver.activate_to_linked(item,transaction)
        self.assertEqual(item.current.resolve(),item.release_dir)
        self.rollback(item,transaction)

    def test_same_version_replacement_tracks_pinned_worker_into_quarantine(self):
        item = self.layout(split=True, same_version=True, worker_current=True)
        transaction = self.begin(item)
        operation = activation.execution_context(item.root)["execution"]["operation_id"]
        self.driver.activate_to_linked(item, transaction)
        value = activation.execution_context(item.root)
        self.assertEqual(value["execution"]["old_worker_release"]["target"], str(item.old_target))
        self.assertEqual(execution.pending_worker_operation(item.root, item.old_target), operation)
        self.publish_all(item, transaction)
        self.rollback(item, transaction)

    def test_commit_publishes_both_jobs_and_cleans_all_backup_files(self):
        item = self.layout(split=True)
        transaction = self.begin(item)
        self.driver.activate_to_linked(item, transaction)
        desired = self.publish_all(item, transaction)
        for phase in ("candidate-starting", "candidate-healthy", "committing", "committed"):
            self.driver.record(item, transaction, phase)
        self.driver.invoke("finish", *self.driver.owned_args(item, transaction))
        self.assertFalse((item.root / ".activation-transaction").exists())
        self.assertEqual(item.current.resolve(), item.release_dir)
        for kind, path in self.configs(item).items():
            self.assertEqual(path.read_bytes(), desired[kind])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600 if kind in {"env", "execution_layout"} else 0o644)
        self.assertTrue(item.worker.exists(), "retained worker generation must survive gateway publication")

    def test_startup_operation_is_bound_to_candidate_and_actual_retained_worker(self):
        item = self.layout(split=True)
        transaction = self.begin(item)
        operation = activation.execution_context(item.root)["execution"]["operation_id"]
        self.assertEqual(execution.pending_worker_operation(item.root, item.worker), operation)
        self.assertEqual(execution.pending_worker_operation(item.root, item.candidate_source), operation)
        with self.assertRaisesRegex(RuntimeError, "not retained"):
            execution.pending_worker_operation(item.root, item.old_source)
        self.driver.activate_to_linked(item, transaction)
        self.assertEqual(execution.pending_worker_operation(item.root, item.release_dir), operation)
        self.assertEqual(execution.pending_worker_operation(item.root, item.worker), operation)
        retained = execution.retained_releases(item.root)
        self.assertIn(str(item.worker), retained)
        self.assertIn(str(item.release_dir), retained)
        self.publish_all(item, transaction)
        self.rollback(item, transaction)

    def test_rollback_at_each_publication_boundary_preserves_exact_originals(self):
        for boundary in ("prepared", "linking", "linked", "service", "gateway", "execution_layout"):
            with self.subTest(boundary=boundary):
                item = self.layout(split=True)
                transaction = self.begin(item)
                if boundary != "prepared":
                    self.driver.record(item, transaction, "linking")
                    self.driver.invoke("activate-files", *self.driver.owned_args(item, transaction))
                if boundary not in {"prepared", "linking"}:
                    self.driver.record(item, transaction, "linked")
                if boundary in {"service", "gateway", "execution_layout"}:
                    self.publish(item, transaction, "env")
                    self.publish(item, transaction, "service")
                if boundary in {"gateway", "execution_layout"}:
                    self.publish(item, transaction, "gateway")
                if boundary == "execution_layout":
                    self.publish(item, transaction, "execution_layout")
                self.rollback(item, transaction)

    def test_lost_ack_after_gateway_publication_can_restore_exact_job(self):
        item = self.layout(split=True)
        transaction = self.begin(item)
        self.driver.activate_to_linked(item, transaction)
        original = activation._publish_staged_config
        def publish_then_crash(**arguments):
            original(**arguments)
            raise support.SimulatedCrash("gateway publication committed before acknowledgement")
        with mock.patch.object(activation, "_publish_staged_config", side_effect=publish_then_crash):
            with self.assertRaises(support.SimulatedCrash):
                self.publish(item, transaction, "gateway")
        self.assertNotEqual(item.gateway.read_bytes(), item.originals["gateway"][0])
        self.rollback(item, transaction)

    def test_foreign_gateway_or_layout_change_cannot_be_overwritten_by_rollback(self):
        for kind in ("gateway", "execution_layout"):
            with self.subTest(kind=kind):
                item = self.layout(split=True)
                transaction = self.begin(item)
                self.driver.activate_to_linked(item, transaction)
                self.publish_all(item, transaction)
                target = self.configs(item)[kind]
                target.write_bytes(b"foreign replacement must survive\n")
                with self.assertRaisesRegex(RuntimeError, "configuration"):
                    self.driver.record(item, transaction, "rolling-back")
                self.assertEqual(target.read_bytes(), b"foreign replacement must survive\n")

    def test_gateway_path_mode_symlink_and_loaded_without_file_are_rejected(self):
        for case in ("other-path", "unsafe-mode", "linked", "loaded-missing"):
            with self.subTest(case=case):
                item = self.layout(split=True)
                extra = []
                if case == "other-path":
                    extra = ["--gateway-service", str(item.service_root / "other.service")]
                elif case == "unsafe-mode":
                    item.gateway.chmod(0o666)
                elif case == "linked":
                    item.gateway.unlink()
                    item.gateway.symlink_to(item.service)
                else:
                    item.gateway.unlink()
                with self.assertRaises((RuntimeError, PermissionError, OSError)):
                    self.begin(item, extra=extra)
                self.assertFalse((item.root / ".activation-transaction").exists())

    def test_handoff_operation_is_preserved_for_both_startup_generations(self):
        item = self.layout(split=True)
        handoff = {"schema": 1, "operation_id": str(uuid.uuid4()), "worker_instance_id": "owned-epoch",
                   "lease_id": str(uuid.uuid4()), "expected_server_identity": "owned-server"}
        path = item.base / "handoff.json"
        support.ActivationLayout.write_file(path, json.dumps(handoff).encode(), 0o600)
        transaction = self.begin(item, extra=["--execution-handoff-file", str(path)])
        value = activation.execution_context(item.root)
        self.assertEqual(value["execution"]["handoff"], handoff)
        self.assertEqual(execution.pending_worker_operation(item.root, item.worker), handoff["operation_id"])
        self.assertEqual(execution.pending_worker_operation(item.root, item.candidate_source), handoff["operation_id"])
        self.rollback(item, transaction)

    def test_invalid_handoff_is_rejected_before_publishing_journal(self):
        for change in ({"schema": True}, {"operation_id": "not-a-uuid"}, {"lease_id": "not-a-uuid"},
                       {"worker_instance_id": ""}, {"expected_server_identity": ""}):
            with self.subTest(change=change):
                item = self.layout(split=True)
                value = {"schema": 1, "operation_id": str(uuid.uuid4()), "worker_instance_id": "owned-epoch",
                         "lease_id": str(uuid.uuid4()), "expected_server_identity": "owned-server", **change}
                path = item.base / "handoff.json"
                support.ActivationLayout.write_file(path, json.dumps(value).encode(), 0o600)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.begin(item, extra=["--execution-handoff-file", str(path)])
                self.assertFalse((item.root / ".activation-transaction").exists())

    def test_reboot_device_renumber_keeps_worker_and_both_new_config_coordinates(self):
        with mock.patch.object(activation.sys, "platform", "darwin"), mock.patch.object(activation, "_volume_uuid", return_value="12345678-1234-5678-9012-123456789012"):
            item = self.layout(split=True)
            transaction = self.begin(item)
            self.driver.activate_to_linked(item, transaction)
            self.publish_all(item, transaction)
            def renumber(value):
                value["volume_bindings"] = {str(int(device) + 100): binding for device, binding in value["volume_bindings"].items()}
                entries = [value[name] for name in ("candidate_release", "old_release", "previous_release",
                           "desired_env", "desired_service", "desired_gateway", "desired_execution_layout")]
                entries.append(value["execution"]["old_worker_release"])
                for entry in entries:
                    if entry:
                        entry["device"] += 100
            self.driver.rewrite_manifest(item, renumber)
            self.assertEqual(execution.pending_worker_operation(item.root, item.worker),
                             activation.execution_context(item.root)["execution"]["operation_id"])
            self.rollback(item, transaction)

    def test_replaced_retained_worker_inode_is_rejected_even_with_same_version(self):
        item = self.layout(split=True)
        self.begin(item)
        item.worker.rename(item.worker.with_name("original-worker"))
        support.ActivationLayout.create_release(item.worker, "0.8.0", marker="substitution\n")
        self.runtime_files(item.worker)
        with self.assertRaisesRegex(RuntimeError, "pinned worker"):
            activation.execution_context(item.root)

    def test_two_journals_cannot_grant_startup_admission(self):
        item = self.layout(split=True)
        self.begin(item)
        (item.root / ".execution-transaction").mkdir()
        with self.assertRaisesRegex(RuntimeError, "two activation journals"):
            execution.pending_worker_operation(item.root, item.worker)

    def test_startup_read_does_not_clean_an_active_writers_private_manifest_temp(self):
        item = self.layout(split=True)
        self.begin(item)
        temporary = item.root / ".activation-transaction" / (".manifest.json." + "a" * 24 + ".tmp")
        support.ActivationLayout.write_file(temporary, b"writer has not published this revision\n", 0o600)
        before = temporary.read_bytes(), temporary.stat()
        value = activation.execution_context(item.root)
        self.assertEqual(execution.pending_worker_operation(item.root, item.worker), value["execution"]["operation_id"])
        self.assertEqual((temporary.read_bytes(), temporary.stat()), before)


if __name__ == "__main__":
    unittest.main()
