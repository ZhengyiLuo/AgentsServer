from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock

import activation_transaction as activation
import execution_recovery as recovery
from execution_manage import InstallationLock
from tests import test_execution_activation_transaction as fixtures


class Native:
    def __init__(self):
        self.events = []

    def start(self, path):
        self.events.append(("start", path.name))

    def assert_absent(self, path):
        pass

    def disable(self, path):
        self.events.append(("disable", path.name))

    def reload(self):
        self.events.append(("reload",))


class Services:
    def snapshot(self):
        return {role: {"state": "absent", "enabled": False} for role in ("worker", "gateway")}


class RecoveryOwnerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionActivationTransactionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.item = self.fixture.layout(fresh=True)
        item = self.item
        # The reusable journal fixture uses an arbitrary service directory;
        # this native-owner boundary must use the real per-user native path.
        item.service_root = item.base / ".config/systemd/user"
        item.service = item.service_root / "agents-server.service"
        item.gateway = item.service_root / "agents-server-gateway.service"
        source = Path(recovery.__file__).parent
        for name in recovery.PAYLOAD:
            shutil.copyfile(source / name, item.candidate_source / name)
        (item.candidate_source / "install.sh").write_text("#!/bin/sh\nexit 1\n")
        self.transaction = self.fixture.begin(item, extra=("--execution-api-contract", "28"))
        self.native = Native()
        self.services = Services()
        self.directory = item.root / recovery.DIRECTORY / self.transaction

    def arm(self):
        with InstallationLock(self.item.root):
            return recovery.arm(self.item.root, self.item.candidate_source, home=self.item.base,
                platform="Linux", bind="127.0.0.1", port=17850, expected_server_identity="", native=self.native)

    def finish_rollback(self):
        item = self.item
        with InstallationLock(item.root):
            self.fixture.driver.rollback(item, self.transaction)
            recovery.complete(item.root, self.transaction, services=self.services)
            self.fixture.driver.invoke("finish", *self.fixture.driver.owned_args(item, self.transaction))
            recovery.finalized(item.root, self.transaction, services=self.services)

    def test_arm_pins_independent_native_job_bootstrap_and_exact_sources(self):
        value = self.arm()
        self.assertEqual(value["transaction_id"], self.transaction)
        self.assertEqual(value["api_contract"], 28)
        self.assertTrue(value["fresh"])
        self.assertEqual(Path(value["interpreter"]), Path(sys.executable).resolve())
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.directory / "owner.json").stat().st_mode & 0o777, 0o600)
        body = recovery._service_path(value).read_text()
        self.assertIn(str(self.directory / "execution_recovery.py"), body)
        self.assertNotIn("PartOf=", body)
        self.assertNotIn("BindsTo=", body)
        self.assertNotIn("agents-server.service", body)
        self.assertIn("WantedBy=default.target", body)
        self.assertIn("SendSIGKILL=no", body)
        self.assertEqual(self.native.events, [("start", recovery._service_path(value).name)])
        lock = json.loads((self.directory / "lock.json").read_text())
        self.assertEqual(lock["pid"], os.getpid())
        self.assertTrue(lock["boot_id"])
        self.assertTrue(lock["process_start"])
        self.assertEqual(set(lock["incarnation"]), {"directory_ctime_ns", "pid_inode", "pid_ctime_ns"})

    def test_arm_requires_existing_installer_authority_before_writing_owner(self):
        with self.assertRaises(FileNotFoundError):
            recovery.arm(self.item.root, self.item.candidate_source, home=self.item.base,
                platform="Linux", bind="127.0.0.1", port=17850, expected_server_identity="", native=self.native)
        self.assertFalse((self.item.root / recovery.DIRECTORY).exists())
        self.assertEqual(self.native.events, [])

    def test_systemd_recovery_enablement_is_exact_and_synced_before_arm_returns(self):
        value = self.arm()
        path = recovery._service_path(value)
        wants = path.parent / "default.target.wants"
        wants.mkdir(mode=0o700)
        events = []
        service = recovery.RecoveryService(value)
        def command(args):
            events.append(tuple(args))
            if "enable" in args:
                (wants / path.name).symlink_to(path)
        service._command = command
        with mock.patch.object(recovery.files, "_fsync_directory", side_effect=lambda p: events.append(("sync", p))):
            service.start(path)
        self.assertEqual(events[-2:], [("sync", wants), ("sync", path.parent)])
        self.assertIn(("systemctl", "--user", "enable", "--now", path.name), events)

    def test_changed_systemd_enablement_never_authorizes_quiescing(self):
        value = self.arm()
        path = recovery._service_path(value)
        wants = path.parent / "default.target.wants"
        wants.mkdir(mode=0o700)
        other = path.parent / "foreign.service"
        other.write_text("foreign")
        (wants / path.name).symlink_to(other)
        service = recovery.RecoveryService(value)
        service._command = mock.Mock()
        with self.assertRaisesRegex(RuntimeError, "does not target"):
            service.start(path)
        self.assertEqual(other.read_text(), "foreign")

    def test_darwin_post_registration_flushes_both_owner_and_native_volume_anchors(self):
        value = {**self.arm(), "platform": "Darwin"}
        path = recovery._service_path(value)
        recovery.files._atomic_write(path, recovery.render_service(self.directory, value))
        anchors = []
        def flush(fd, operation):
            self.assertEqual(operation, getattr(fcntl, "F_FULLFSYNC", 51))
            anchors.append(os.fstat(fd).st_ino)
        with mock.patch.object(recovery.fcntl, "fcntl", side_effect=flush):
            recovery._registration_barrier(self.directory, value)
        self.assertEqual(anchors, [(self.directory / "owner.json").stat().st_ino, path.stat().st_ino])
        with mock.patch.object(recovery.fcntl, "fcntl", side_effect=OSError("flush failed")):
            with self.assertRaisesRegex(OSError, "flush failed"):
                recovery._registration_barrier(self.directory, value)

    def test_loaded_stopped_launchd_owner_is_started_without_killing_live_owner(self):
        value = {**self.arm(), "platform": "Darwin"}
        service = recovery.RecoveryService(value)
        service._command = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=b"state = waiting\n"))
        service.start(recovery._service_path(value))
        self.assertIn(mock.call(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{recovery._label(value)}"]),
                      service._command.call_args_list)
        self.assertNotIn("-k", str(service._command.call_args_list))

    def test_api_resume_finishes_missing_registration_from_exact_existing_owner(self):
        with mock.patch.object(recovery, "_register", side_effect=RuntimeError("partial arm")):
            with self.assertRaisesRegex(RuntimeError, "partial arm"):
                self.arm()
        before = (self.directory / "owner.json").read_bytes()
        result = recovery.resume_owner(self.item.root, self.transaction, native=self.native)
        self.assertTrue(result["joining"])
        self.assertEqual((self.directory / "owner.json").read_bytes(), before)
        self.assertEqual(len(self.native.events), 1)
        self.assertTrue(Path(result["service_path"]).exists())

    def test_api_resume_after_registration_before_start_launches_actual_recovery_process(self):
        marker = self.item.base / "resumed-installer-args"
        script = self.item.candidate_source / "install.sh"
        script.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\" > '" + str(marker) + "'\nexit 1\n")
        self.native.start = mock.Mock(side_effect=RuntimeError("crash before native start"))
        with self.assertRaisesRegex(RuntimeError, "crash before native start"):
            self.arm()
        processes = []
        def start(_path):
            processes.append(subprocess.Popen([sys.executable, "-B", str(self.directory / "execution_recovery.py"),
                "run", "--root", str(self.item.root), "--transaction-id", self.transaction],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.native.start = start
        try:
            result = recovery.resume_owner(self.item.root, self.transaction, native=self.native)
            self.assertTrue(result["joining"])
            self.assertEqual(len(processes), 1)
            self.assertEqual(processes[0].wait(timeout=15), 1)
            arguments = marker.read_text().splitlines()
            self.assertEqual(arguments[0], "--recover-only")
            self.assertEqual(arguments[arguments.index("--expected-activation-id") + 1], self.transaction)
            self.assertTrue((self.directory / "failure.json").is_file())
            self.assertTrue((self.item.root / ".activation-transaction").is_dir())
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)

    def test_api_resume_joins_exact_live_installer_without_registration_or_wait(self):
        self.arm()
        self.native.events.clear()
        with InstallationLock(self.item.root):
            recovery.observe_lock(self.item.root, self.transaction)
            before = (self.item.root / ".install-lock/pid").read_bytes()
            result = recovery.resume_owner(self.item.root, self.transaction, native=self.native)
            self.assertTrue(result["joining"])
            self.assertEqual(self.native.events, [])
            self.assertEqual((self.item.root / ".install-lock/pid").read_bytes(), before)

    def test_api_resume_never_adopts_foreign_registration_or_new_install_lock(self):
        value = self.arm()
        path = recovery._service_path(value)
        original = path.read_bytes()
        path.write_bytes(b"foreign")
        with self.assertRaisesRegex(RuntimeError, "registration changed"):
            recovery.resume_owner(self.item.root, self.transaction, native=self.native)
        path.write_bytes(original)
        with InstallationLock(self.item.root):
            with self.assertRaisesRegex(RuntimeError, "another installer owns"):
                recovery.resume_owner(self.item.root, self.transaction, native=self.native)

    def test_genuinely_missing_native_parent_is_created_without_changing_existing_modes(self):
        self.assertFalse(self.item.service_root.parent.exists())
        before = self.item.base.stat().st_mode
        value = self.arm()
        self.assertEqual(self.item.service_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.item.base.stat().st_mode, before)
        self.assertTrue(recovery._service_path(value).exists())

    def test_unsafe_existing_native_parent_is_not_repaired_or_registered(self):
        parent = self.item.base / ".config"
        parent.mkdir()
        parent.chmod(0o777)
        with self.assertRaises((PermissionError, RuntimeError)):
            self.arm()
        self.assertEqual(parent.stat().st_mode & 0o777, 0o777)
        self.assertEqual(self.native.events, [])

    def test_arm_retry_finishes_registration_after_owner_publication_crash(self):
        with mock.patch.object(recovery, "_register", side_effect=RuntimeError("fault after owner publication")):
            with self.assertRaisesRegex(RuntimeError, "fault after owner"):
                self.arm()
        before = (self.directory / "owner.json").read_bytes()
        self.assertTrue(self.directory.exists())
        self.assertFalse(any(self.item.service_root.glob("*recovery*")))
        value = self.arm()
        self.assertEqual((self.directory / "owner.json").read_bytes(), before)
        self.assertEqual(recovery._service_path(value).read_bytes(), recovery.render_service(self.directory, value))
        self.assertEqual(len(self.native.events), 1)

    def test_missing_registration_retry_refuses_an_unknown_loaded_native_job(self):
        with mock.patch.object(recovery, "_register", side_effect=RuntimeError("fault")):
            with self.assertRaises(RuntimeError):
                self.arm()
        self.native.assert_absent = mock.Mock(side_effect=RuntimeError("foreign loaded job"))
        with self.assertRaisesRegex(RuntimeError, "foreign loaded"):
            self.arm()
        self.assertEqual(self.native.events, [])

    def test_actual_large_server_source_is_included_without_reading_dependencies(self):
        path = self.item.candidate_source / "agent_server.py"
        path.write_bytes(b"# large production source\n" * 70000)
        value = self.arm()
        self.assertIn("agent_server.py", value["source_inventory"])
        self.assertFalse(any(name.startswith(".venv/") for name in value["source_inventory"]))

    def test_launchd_job_is_per_transaction_and_survives_worker_disable(self):
        value = {**self.arm(), "platform": "Darwin"}
        result = plistlib.loads(recovery.render_service(self.directory, value))
        self.assertEqual(result["KeepAlive"], {"SuccessfulExit": False})
        self.assertTrue(result["RunAtLoad"])
        self.assertEqual(result["ExitTimeOut"], 0)
        self.assertNotIn("AGENTSDOCK_AGENT_TOKEN", result["EnvironmentVariables"])
        self.assertIn(self.transaction.removeprefix("activation-"), result["Label"])

    def test_replaced_native_registration_is_never_overwritten(self):
        value = self.arm()
        recovery._service_path(value).write_text("foreign native registration")
        with self.assertRaisesRegex(RuntimeError, "registration changed"):
            self.arm()
        self.assertEqual(recovery._service_path(value).read_text(), "foreign native registration")
        self.assertEqual(len(self.native.events), 1)

    def test_context_survives_candidate_rename_and_keeps_bootstrap_stable(self):
        value = self.arm()
        self.fixture.driver.activate_to_linked(self.item, self.transaction)
        context, source = recovery._context(value)
        self.assertEqual(source, self.item.release_dir)
        self.assertEqual(context["transaction_id"], self.transaction)
        self.assertTrue((self.directory / "execution_recovery.py").is_file())

    def test_pending_journal_reader_never_cleans_writer_temporary(self):
        self.arm()
        path = self.item.root / ".activation-transaction" / (".manifest.json." + "a" * 24 + ".tmp")
        path.write_text("writer has not published")
        path.chmod(0o600)
        with mock.patch.object(recovery, "_context", wraps=recovery._context):
            with self.assertRaisesRegex(RuntimeError, "did not complete"):
                recovery.run_once(self.item.root, self.transaction, native=self.native,
                    installer=lambda *args, **kwargs: SimpleNamespace(returncode=1))
        self.assertEqual(path.read_text(), "writer has not published")

    def test_source_and_bootstrap_tampering_refuse_before_installer(self):
        self.arm()
        installer = mock.Mock()
        source = self.item.candidate_source / "install.sh"
        original = source.read_bytes()
        source.write_bytes(original + b"# changed\n")
        with self.assertRaisesRegex(RuntimeError, "runtime source changed"):
            recovery.run_once(self.item.root, self.transaction, native=self.native, installer=installer)
        source.write_bytes(original)
        (self.directory / "execution_recovery.py").write_text("changed bootstrap")
        with self.assertRaisesRegex(RuntimeError, "bootstrap source changed"):
            recovery.run_once(self.item.root, self.transaction, native=self.native, installer=installer)
        installer.assert_not_called()

    def test_real_subprocess_runs_only_exact_recovery_entrypoint_and_preserves_failure_evidence(self):
        script = self.item.candidate_source / "install.sh"
        marker = self.item.base / "installer-arguments.json"
        script.write_text("#!/bin/sh\n" + "printf '%s\\n' \"$@\" > '" + str(marker) + "'\nexit 1\n")
        self.arm()
        child = subprocess.run([sys.executable, "-B", str(self.directory / "execution_recovery.py"), "run",
            "--root", str(self.item.root), "--transaction-id", self.transaction],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 1)
        arguments = marker.read_text().splitlines()
        self.assertEqual(arguments[0], "--recover-only")
        self.assertEqual(arguments[arguments.index("--expected-activation-id") + 1], self.transaction)
        self.assertEqual(arguments[arguments.index("--expected-api-contract") + 1], "28")
        self.assertNotIn("--activate-prepared", arguments)
        self.assertTrue((self.item.root / ".activation-transaction/manifest.json").is_file())
        self.assertFalse((self.directory / "finalized.json").exists())

    def test_active_installer_is_not_signaled_or_reaped(self):
        self.arm()
        with InstallationLock(self.item.root):
            recovery.observe_lock(self.item.root, self.transaction)
            owner = (self.item.root / ".install-lock/pid").read_bytes()
            self.assertFalse(recovery._reap_observed_lock(self.item.root, self.directory, self.transaction))
            with self.assertRaisesRegex(RuntimeError, "another installer owns"):
                recovery.run_once(self.item.root, self.transaction, native=self.native, installer=mock.Mock())
            self.assertEqual((self.item.root / ".install-lock/pid").read_bytes(), owner)

    def test_recorded_prior_boot_lock_is_reaped_even_if_pid_was_reused(self):
        self.arm()
        lock = InstallationLock(self.item.root)
        lock.__enter__()
        recovery.observe_lock(self.item.root, self.transaction)
        lock.identity = None  # simulate process/reboot loss without cleanup
        with mock.patch.object(recovery, "_boot_id", return_value="different-boot"), \
                mock.patch.object(recovery, "_process_start", side_effect=AssertionError("do not probe reused PID")):
            self.assertTrue(recovery._reap_observed_lock(self.item.root, self.directory, self.transaction))
        self.assertFalse((self.item.root / ".install-lock").exists())

    def test_newer_installer_lock_is_never_reaped_by_old_boot_receipt(self):
        self.arm()
        with InstallationLock(self.item.root):
            before = (self.item.root / ".install-lock").stat().st_ino
            with mock.patch.object(recovery, "_boot_id", return_value="different-boot"):
                self.assertFalse(recovery._reap_observed_lock(self.item.root, self.directory, self.transaction))
            self.assertEqual((self.item.root / ".install-lock").stat().st_ino, before)

    def test_identical_pid_replacement_is_not_the_recorded_lock_incarnation(self):
        self.arm()
        with InstallationLock(self.item.root):
            recovery.observe_lock(self.item.root, self.transaction)
            path = self.item.root / ".install-lock"
            saved = json.loads((self.directory / "lock.json").read_text())
            data = (path / "pid").read_bytes()
            recovery.files._atomic_write(path / "pid", data)
            self.assertEqual(path.stat().st_ino, saved["binding"]["inode"])
            self.assertNotEqual((path / "pid").stat().st_ino, saved["incarnation"]["pid_inode"])
            self.assertFalse(recovery._observed_installer_is_live(self.item.root, self.directory, self.transaction))
            with mock.patch.object(recovery, "_boot_id", return_value="different-boot"), \
                    mock.patch.object(recovery.os, "kill", side_effect=AssertionError("never signal a replacement owner")):
                self.assertFalse(recovery._reap_observed_lock(self.item.root, self.directory, self.transaction))
            self.assertEqual((path / "pid").read_bytes(), data)

    def test_receipt_without_lock_incarnation_cannot_reap_or_claim_live_ownership(self):
        self.arm()
        with InstallationLock(self.item.root):
            recovery.observe_lock(self.item.root, self.transaction)
            path = self.item.root / ".install-lock"
            saved = json.loads((self.directory / "lock.json").read_text())
            saved.pop("incarnation")
            recovery._write(self.directory / "lock.json", saved)
            before = (path / "pid").read_bytes(), path.stat().st_ino
            for inspect in (recovery._reap_observed_lock, recovery._observed_installer_is_live):
                with self.assertRaisesRegex(ValueError, "provenance"):
                    inspect(self.item.root, self.directory, self.transaction)
            self.assertEqual(((path / "pid").read_bytes(), path.stat().st_ino), before)

    def test_finalization_requires_terminal_journal_and_proven_retirement(self):
        self.arm()
        with InstallationLock(self.item.root):
            with self.assertRaisesRegex(RuntimeError, "terminal boundary"):
                recovery.complete(self.item.root, self.transaction, services=self.services)
            with self.assertRaisesRegex(RuntimeError, "has not been retired"):
                recovery.finalized(self.item.root, self.transaction, services=self.services)
        self.assertFalse((self.directory / "terminal.json").exists())

    def committed_fixture(self):
        self.arm()
        self.fixture.driver.activate_to_linked(self.item, self.transaction)
        self.fixture.publish_all(self.item, self.transaction)
        for phase in ("candidate-starting", "candidate-healthy", "committing", "committed"):
            self.fixture.driver.record(self.item, self.transaction, phase)
        (self.item.state / "server-identity").write_text("fresh-server-identity\n")
        (self.item.state / "server-identity").chmod(0o600)
        services = mock.Mock()
        services.snapshot.return_value = {
            "worker": {"state": "running", "enabled": True, "pid": 120},
            "gateway": {"state": "running", "enabled": True, "pid": 121}}
        health = {"ok": True, "server_identity": "fresh-server-identity", "server_version": self.item.release_version,
            "api_contract_version": 28, "execution_service": {"pid": 120, "version": self.item.release_version,
            "maintenance_held": False}, "gateway": {"pid": 121, "version": self.item.release_version}}
        control = mock.Mock()
        control.health.side_effect = lambda *args: json.loads(json.dumps(health))
        return services, control, health

    def test_fresh_commit_binds_created_identity_and_both_native_components(self):
        services, control, health = self.committed_fixture()
        with InstallationLock(self.item.root):
            recovery.complete(self.item.root, self.transaction, services=services, control=control)
            self.fixture.driver.invoke("finish", *self.fixture.driver.owned_args(self.item, self.transaction))
            recovery.finalized(self.item.root, self.transaction, services=services, control=control)
        proof = json.loads((self.directory / "terminal.json").read_text())
        self.assertEqual(proof["snapshot"]["health"]["server_identity"], health["server_identity"])
        self.assertEqual(proof["phase"], "committed")

    def test_mixed_release_held_worker_or_changed_identity_cannot_complete(self):
        services, control, health = self.committed_fixture()
        original = json.loads(json.dumps(health))
        mutations = (
            lambda: health["gateway"].update(version="0.0.1"),
            lambda: health["execution_service"].update(maintenance_held=True),
            lambda: health.update(server_identity="foreign-server"),
        )
        with InstallationLock(self.item.root):
            for mutation in mutations:
                mutation()
                with self.assertRaises(RuntimeError):
                    recovery.complete(self.item.root, self.transaction, services=services, control=control)
                health.clear()
                health.update(json.loads(json.dumps(original)))
        self.assertFalse((self.directory / "terminal.json").exists())

    def test_fresh_rollback_terminal_proof_survives_candidate_and_journal_removal(self):
        value = self.arm()
        self.finish_rollback()
        self.assertFalse(self.item.candidate_source.exists())
        self.assertFalse((self.item.root / ".activation-transaction").exists())
        self.assertTrue(recovery.run_once(self.item.root, self.transaction, native=self.native))
        self.assertFalse(recovery._service_path(value).exists())
        self.assertTrue((self.directory / "retired.json").is_file())
        self.assertTrue((self.directory / "owner.json").is_file())

    def test_crash_after_journal_finish_revalidates_terminal_health_before_retiring(self):
        value = self.arm()
        with InstallationLock(self.item.root):
            self.fixture.driver.rollback(self.item, self.transaction)
            recovery.complete(self.item.root, self.transaction, services=self.services)
            self.fixture.driver.invoke("finish", *self.fixture.driver.owned_args(self.item, self.transaction))
        self.assertFalse((self.directory / "finalized.json").exists())
        self.assertTrue(recovery.run_once(self.item.root, self.transaction, native=self.native, services=self.services))
        self.assertFalse(recovery._service_path(value).exists())

    def test_finalized_old_owner_retires_only_its_job_when_another_journal_exists(self):
        value = self.arm()
        self.finish_rollback()
        marker = self.item.root / ".activation-transaction"
        marker.mkdir(mode=0o700)
        (marker / "foreign-evidence").write_text("next transaction owns this")
        self.assertTrue(recovery.run_once(self.item.root, self.transaction, native=self.native))
        self.assertEqual((marker / "foreign-evidence").read_text(), "next transaction owns this")
        self.assertFalse(recovery._service_path(value).exists())

    def test_live_original_updater_defers_native_owner_retirement(self):
        value = self.arm()
        self.finish_rollback()
        with mock.patch("execution_recovery_status.settle_status", return_value=False):
            self.assertFalse(recovery.run_once(self.item.root, self.transaction, native=self.native))
        self.assertTrue(recovery._service_path(value).exists())
        self.assertFalse((self.directory / "retired.json").exists())

    def test_readonly_owner_inspection_joins_without_starting_any_service(self):
        self.assertIsNone(recovery.inspect_owner(self.item.root, self.transaction))
        value = self.arm()
        before = len(self.native.events)
        result = recovery.inspect_owner(self.item.root, self.transaction)
        self.assertFalse(result["finalized"])
        self.assertFalse(result["retired"])
        self.assertEqual(result["service_path"], str(recovery._service_path(value)))
        self.assertEqual(len(self.native.events), before)

    def test_uninstall_waits_for_recovery_job_then_accepts_retained_retirement_evidence(self):
        self.arm()
        with self.assertRaisesRegex(RuntimeError, "has not retired"):
            recovery.require_retired_owners(self.item.root)
        self.finish_rollback()
        with self.assertRaisesRegex(RuntimeError, "has not retired"):
            recovery.require_retired_owners(self.item.root)
        recovery.run_once(self.item.root, self.transaction, native=self.native)
        with mock.patch.object(recovery.RecoveryService, "running", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "still registered or running"):
                recovery.require_retired_owners(self.item.root)
        with mock.patch.object(recovery.RecoveryService, "running", return_value=False):
            recovery.require_retired_owners(self.item.root)

    def test_interrupted_native_disable_keeps_registration_and_blocks_uninstall(self):
        self.arm()
        self.finish_rollback()
        self.native.disable = mock.Mock(side_effect=RuntimeError("native manager unavailable"))
        with self.assertRaisesRegex(RuntimeError, "native manager"):
            recovery.run_once(self.item.root, self.transaction, native=self.native)
        with self.assertRaisesRegex(RuntimeError, "still registered"):
            recovery.require_retired_owners(self.item.root)
        self.native.disable = lambda path: None
        self.assertTrue(recovery.run_once(self.item.root, self.transaction, native=self.native))

    def test_missing_journal_without_terminal_evidence_never_retires_or_installs(self):
        value = self.arm()
        journal = self.item.root / ".activation-transaction"
        journal.rename(self.item.root / "owned-missing-journal-fixture")
        installer = mock.Mock()
        with self.assertRaises(FileNotFoundError):
            recovery.run_once(self.item.root, self.transaction, native=self.native, installer=installer)
        installer.assert_not_called()
        self.assertTrue(recovery._service_path(value).exists())


if __name__ == "__main__":
    unittest.main()
