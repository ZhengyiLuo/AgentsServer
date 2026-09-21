from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import execution_install as install
from execution_transport import ensure_execution_secret


class ExecutionInstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.home = self.base / "home"
        self.root = self.home / "install"
        self.config = self.home / "configuration"
        self.state = self.home / "state"
        for directory in (self.home, self.root, self.config, self.state, self.root / "releases"):
            directory.mkdir(mode=0o700)
        self.old = self.release("1.0.0")
        self.new = self.release("1.1.0")
        self.next = self.release("1.2.0")
        self.layout = install.ExecutionLayout(
            self.root, self.config, self.state, self.home, "Linux", self.old, self.old,
            "0.0.0.0", 17850, {"PATH": "/usr/bin:/bin"})
        self.absent = {role: {"state": "absent", "enabled": False} for role in ("worker", "gateway")}
        self.running = {role: {"state": "running", "enabled": True} for role in ("worker", "gateway")}
        self.running["worker"].update(pid=120, instance_id="worker_epoch_fixture")

    def release(self, version: str) -> Path:
        release = self.root / "releases" / version
        (release / ".venv/bin").mkdir(mode=0o700, parents=True)
        (release / "VERSION").write_text(version + "\n")
        (release / "execution_service.py").write_text("# fixture runtime\n")
        (release / ".venv/bin/python").symlink_to(Path(os.sys.executable).resolve())
        return release

    def receipt(self, layout: install.ExecutionLayout) -> dict:
        manifest = install.layout_manifest(layout)
        return {"gateway_version": manifest["gateway_version"], "worker_version": manifest["worker_version"],
                "worker_release": str(layout.worker_release), "protocol_version": 1,
                "server_identity": "server_test_identity", "gateway_pid": 121, "worker_pid": 120,
                "worker_instance_id": "worker_epoch_fixture"}

    def migrate(self, layout: install.ExecutionLayout | None = None) -> None:
        layout = layout or self.layout
        install.prepare_runtime(layout)
        install.stage(layout, scope="migration", prior_services=self.absent)
        install.publish(self.root)
        install.commit(self.root, self.receipt(layout))
        install.finish(self.root)

    def test_split_logs_do_not_take_over_existing_legacy_log_directory(self):
        logs = self.layout.state_root / "logs"
        logs.mkdir(mode=0o755)
        before = logs.stat().st_mode, logs.stat().st_ino
        install.prepare_runtime(self.layout)
        self.assertEqual((logs.stat().st_mode, logs.stat().st_ino), before)
        self.assertEqual((self.layout.runtime_dir / "logs").stat().st_mode & 0o777, 0o700)

    def test_linux_jobs_pin_worker_and_keep_gateway_independent(self) -> None:
        worker = install.render_service(self.layout, "worker").decode()
        gateway = install.render_service(self.layout, "gateway").decode()
        self.assertIn(f'WorkingDirectory={self.old}\n', worker)
        self.assertIn(f'"{self.old}/execution_service.py" "worker"', worker)
        self.assertIn('"--bind" "0.0.0.0" "--port" "17850" "--callback-port" "0"', worker)
        self.assertIn(f'WorkingDirectory={self.root}/current\n', gateway)
        self.assertIn('Wants=agents-server.service', gateway)
        for body in (worker, gateway):
            self.assertNotIn("PartOf=", body)
            self.assertNotIn("BindsTo=", body)
            self.assertNotIn("KillMode=process", body)
        self.assertNotIn("--callback-port", gateway)
        self.assertIn("TimeoutStopSec=180s", worker)
        self.assertIn("SendSIGKILL=no", worker)
        self.assertIn("TimeoutStopSec=10s", gateway)
        command = shlex.split(next(line.split("=", 1)[1] for line in worker.splitlines() if line.startswith("ExecStart=")))
        self.assertEqual(command[0], "/usr/bin/env")
        self.assertIn(f"AGENTSDOCK_STATE_DIR={self.state}", command)
        self.assertIn(f"AGENTS_SERVER_CONFIG_DIR={self.config}", command)
        self.assertNotIn("AGENTSDOCK_AGENT_TOKEN=", " ".join(command))
        self.assertEqual(worker, install.render_service(self.layout, "worker").decode())

    def test_macos_jobs_have_distinct_labels_and_public_metadata(self) -> None:
        layout = replace(self.layout, platform="Darwin")
        worker = plistlib.loads(install.render_service(layout, "worker"))
        gateway = plistlib.loads(install.render_service(layout, "gateway"))
        self.assertEqual(worker["Label"], "com.agentsdock.server")
        self.assertEqual(gateway["Label"], "com.agentsdock.gateway")
        self.assertEqual(worker["ProgramArguments"][0], str(self.old / ".venv/bin/python"))
        self.assertEqual(gateway["ProgramArguments"][0], str(self.root / "current/.venv/bin/python"))
        self.assertEqual(worker["WorkingDirectory"], str(self.old))
        self.assertEqual(worker["ProgramArguments"][-6:], ["--bind", "0.0.0.0", "--port", "17850", "--callback-port", "0"])
        self.assertEqual(worker["EnvironmentVariables"]["AGENTSDOCK_EXECUTION_ROLE"], "worker")
        self.assertEqual(worker["EnvironmentVariables"]["AGENTSDOCK_STATE_DIR"], str(self.state))
        self.assertEqual(worker["EnvironmentVariables"]["AGENTS_SERVER_CONFIG_DIR"], str(self.config))
        self.assertNotEqual(worker["StandardOutPath"], gateway["StandardOutPath"])
        self.assertNotIn("AbandonProcessGroup", worker)
        self.assertEqual(worker["ExitTimeOut"], 0)
        self.assertEqual(gateway["ExitTimeOut"], 10)

    def test_layout_roots_override_supplied_role_and_state_environment(self) -> None:
        layout = replace(self.layout, platform="Darwin", environment={
            "AGENTSDOCK_STATE_DIR": "/wrong/default", "AGENTS_SERVER_CONFIG_DIR": "/wrong/config",
            "AGENTSDOCK_EXECUTION_ROLE": "gateway", "AGENTSDOCK_EXECUTION_RUNTIME_DIR": "/wrong/runtime"})
        environment = plistlib.loads(install.render_service(layout, "worker"))["EnvironmentVariables"]
        self.assertEqual(environment["AGENTSDOCK_STATE_DIR"], str(self.state))
        self.assertEqual(environment["AGENTS_SERVER_CONFIG_DIR"], str(self.config))
        self.assertEqual(environment["AGENTSDOCK_EXECUTION_ROLE"], "worker")
        self.assertEqual(environment["AGENTSDOCK_EXECUTION_RUNTIME_DIR"], str(layout.runtime_dir))

    def test_systemd_quotes_specifiers_and_command_expansion(self) -> None:
        self.assertEqual(install._unit_word('a %n $HOME "quoted"', command=True),
                         '"a %%n $$HOME \\"quoted\\""')
        with self.assertRaises(ValueError):
            install._unit_word("line\ninjection")

    @unittest.skipUnless(shutil.which("systemd-analyze"), "native systemd parser unavailable")
    def test_generated_units_pass_the_native_systemd_parser(self) -> None:
        self.migrate()
        result = subprocess.run([
            "systemd-analyze", "verify", "--man=no",
            str(self.layout.service_path("worker")), str(self.layout.service_path("gateway")),
        ], capture_output=True, text=True, check=False, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runtime_is_private_and_token_is_not_rotated(self) -> None:
        install.prepare_runtime(self.layout)
        token = self.layout.runtime_dir / "control.token"
        before = token.read_bytes(), token.stat().st_ino
        self.assertEqual(ensure_execution_secret(token).encode(), before[0])
        self.assertEqual(len(before[0]), 64)
        install.prepare_runtime(self.layout)
        self.assertEqual((token.read_bytes(), token.stat().st_ino), before)
        self.assertEqual(stat.S_IMODE(token.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.layout.runtime_dir.stat().st_mode), 0o700)
        manifest = install.layout_manifest(self.layout)
        self.assertEqual(manifest["callback"], {"bind": "127.0.0.1", "port": 0, "lifetime": "worker-process"})
        self.assertEqual(manifest["socket_path"], str(self.layout.runtime_dir / "worker.socket"))

    def test_existing_unsafe_runtime_or_token_is_not_repaired(self) -> None:
        self.layout.runtime_dir.mkdir(mode=0o755)
        with self.assertRaises(PermissionError):
            install.prepare_runtime(self.layout)
        self.assertEqual(stat.S_IMODE(self.layout.runtime_dir.stat().st_mode), 0o755)
        self.layout.runtime_dir.chmod(0o700)
        token = self.layout.runtime_dir / "control.token"
        target = self.base / "other-token"
        target.write_text("a" * 64)
        token.symlink_to(target)
        with self.assertRaises(OSError):
            install.prepare_runtime(self.layout)
        self.assertTrue(token.is_symlink())

    def test_new_layout_can_be_published_and_committed(self) -> None:
        self.migrate()
        self.assertEqual(self.layout.current.resolve(), self.old)
        self.assertEqual(install.retained_releases(self.root), [str(self.old)])
        self.assertFalse(self.layout.transaction_dir.exists())
        self.assertEqual(len(list(self.root.glob(".execution-completed-*"))), 1)

    def test_gateway_update_and_rollback_preserve_worker_inode_and_runtime(self) -> None:
        self.migrate()
        token = self.layout.runtime_dir / "control.token"
        worker = self.layout.service_path("worker")
        before = worker.read_bytes(), worker.stat().st_ino, token.read_bytes(), token.stat().st_ino
        upgraded = replace(self.layout, gateway_release=self.new)
        install.stage(upgraded, scope="gateway", prior_services=self.running)
        self.assertEqual(install.retained_releases(self.root), sorted([str(self.old), str(self.new)]))
        install.publish(self.root)
        self.assertEqual(upgraded.current.resolve(), self.new)
        self.assertEqual((worker.read_bytes(), worker.stat().st_ino, token.read_bytes(), token.stat().st_ino), before)
        install.rollback(self.root)
        install.rollback(self.root)
        self.assertEqual(upgraded.current.resolve(), self.old)
        self.assertEqual((worker.read_bytes(), worker.stat().st_ino, token.read_bytes(), token.stat().st_ino), before)
        install.finish(self.root)
        self.assertTrue(self.old.is_dir())
        self.assertTrue(self.new.is_dir())

    def test_gateway_update_refuses_worker_or_public_configuration_change(self) -> None:
        self.migrate()
        for candidate in (replace(self.layout, worker_release=self.new),
                          replace(self.layout, bind="127.0.0.1"),
                          replace(self.layout, port=17851),
                          replace(self.layout, environment={"PATH": "/another/bin"})):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                install.stage(candidate, scope="gateway", prior_services=self.running)
        self.assertFalse(self.layout.transaction_dir.exists())

    def test_worker_activation_does_not_change_gateway_or_current(self) -> None:
        self.migrate()
        gateway = self.layout.service_path("gateway")
        before = gateway.read_bytes(), gateway.stat().st_ino, self.layout.current.lstat().st_ino
        upgraded = replace(self.layout, worker_release=self.new)
        install.stage(upgraded, scope="worker", prior_services=self.running)
        install.publish(self.root)
        self.assertEqual((gateway.read_bytes(), gateway.stat().st_ino, self.layout.current.lstat().st_ino), before)
        self.assertIn(str(self.new).encode(), self.layout.service_path("worker").read_bytes())
        install.commit(self.root, self.receipt(upgraded))
        install.finish(self.root)
        self.assertEqual(install.retained_releases(self.root), sorted([str(self.old), str(self.new)]))

    def test_migration_rollback_restores_old_monolith_and_native_state_snapshot(self) -> None:
        service = self.layout.service_path("worker")
        service.parent.mkdir(mode=0o700, parents=True)
        service.write_bytes(b"old monolith job\n")
        service.chmod(0o644)
        self.layout.current.symlink_to(self.old)
        prior = {"worker": {"state": "running", "enabled": True},
                 "gateway": {"state": "absent", "enabled": False}}
        value = install.stage(self.layout, scope="migration", prior_services=prior)
        self.assertEqual(value["prior_services"], prior)
        install.publish(self.root)
        old_mask = os.umask(0o077)
        try:
            restored = install.rollback(self.root)
        finally:
            os.umask(old_mask)
        self.assertEqual(restored["prior_services"], prior)
        self.assertEqual(service.read_bytes(), b"old monolith job\n")
        self.assertEqual(stat.S_IMODE(service.stat().st_mode), 0o644)
        self.assertFalse(self.layout.service_path("gateway").exists())
        self.assertFalse(self.layout.manifest_path.exists())
        install.finish(self.root)

    def test_crash_after_first_service_publication_is_recoverable(self) -> None:
        install.stage(self.layout, scope="migration", prior_services=self.absent)
        original = install._atomic_write

        def interrupted(path, data, mode=0o600):
            original(path, data, mode)
            if path == self.layout.service_path("worker"):
                raise RuntimeError("simulated interruption after rename")

        with mock.patch.object(install, "_atomic_write", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                install.publish(self.root)
        self.assertTrue(self.layout.service_path("worker").is_file())
        self.assertFalse(self.layout.service_path("gateway").exists())
        install.rollback(self.root)
        self.assertFalse(self.layout.service_path("worker").exists())
        self.assertFalse(self.layout.manifest_path.exists())
        self.assertFalse(self.layout.current.exists())
        install.finish(self.root)

    def test_crash_after_current_link_swap_can_resume_publication(self) -> None:
        install.stage(self.layout, scope="migration", prior_services=self.absent)
        original = install._replace_link

        def interrupted(layout, target):
            original(layout, target)
            raise RuntimeError("simulated link interruption")

        with mock.patch.object(install, "_replace_link", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "simulated link"):
                install.publish(self.root)
        self.assertEqual(self.layout.current.resolve(), self.old)
        install.publish(self.root)
        install.commit(self.root, self.receipt(self.layout))
        install.finish(self.root)

    def test_external_config_change_refuses_rollback_without_partial_restore(self) -> None:
        self.migrate()
        upgraded = replace(self.layout, gateway_release=self.new)
        install.stage(upgraded, scope="gateway", prior_services=self.running)
        install.publish(self.root)
        gateway = upgraded.service_path("gateway")
        gateway.write_bytes(b"unexpected external configuration\n")
        manifest = upgraded.manifest_path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "configuration changed"):
            install.rollback(self.root)
        self.assertEqual(upgraded.manifest_path.read_bytes(), manifest)
        self.assertEqual(upgraded.current.resolve(), self.new)
        self.assertEqual(gateway.read_bytes(), b"unexpected external configuration\n")

    def test_commit_rejects_gateway_only_success_and_wrong_worker_version(self) -> None:
        install.stage(self.layout, scope="migration", prior_services=self.absent)
        install.publish(self.root)
        for receipt in ({"gateway_version": "1.0.0"},
                        {**self.receipt(self.layout), "worker_version": "wrong"},
                        {**self.receipt(self.layout), "worker_release": str(self.new)},
                        {**self.receipt(self.layout), "worker_pid": True}):
            with self.subTest(receipt=receipt), self.assertRaises(ValueError):
                install.commit(self.root, receipt)
        install.commit(self.root, self.receipt(self.layout))
        with self.assertRaises(ValueError):
            install.rollback(self.root)

    def test_gateway_commit_requires_same_worker_pid_and_process_epoch(self) -> None:
        self.migrate()
        upgraded = replace(self.layout, gateway_release=self.new)
        missing_identity = {role: {"state": "running", "enabled": True} for role in ("worker", "gateway")}
        with self.assertRaisesRegex(ValueError, "process identity"):
            install.stage(upgraded, scope="gateway", prior_services=missing_identity)
        install.stage(upgraded, scope="gateway", prior_services=self.running)
        install.publish(self.root)
        for changes in ({"worker_pid": 122}, {"worker_instance_id": "different_epoch"}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "exact worker"):
                install.commit(self.root, {**self.receipt(upgraded), **changes})
        install.commit(self.root, self.receipt(upgraded))

    def test_startup_hold_tracks_both_worker_generations_until_finish(self) -> None:
        self.migrate()
        upgraded = replace(self.layout, worker_release=self.new)
        transaction = install.stage(upgraded, scope="worker", prior_services=self.running)
        operation = install.maintenance_operation_id(transaction)
        self.assertEqual(install.pending_worker_operation(self.root, self.old), operation)
        self.assertEqual(install.pending_worker_operation(self.root, self.new), operation)
        with self.assertRaises(RuntimeError):
            install.pending_worker_operation(self.root, self.next)
        install.publish(self.root)
        install.commit(self.root, self.receipt(upgraded))
        self.assertEqual(install.pending_worker_operation(self.root, self.new), operation)
        install.finish(self.root)
        self.assertIsNone(install.pending_worker_operation(self.root, self.new))

    def test_gateway_transaction_never_sets_worker_startup_hold(self) -> None:
        self.migrate()
        upgraded = replace(self.layout, gateway_release=self.new)
        install.stage(upgraded, scope="gateway", prior_services=self.running)
        self.assertIsNone(install.pending_worker_operation(self.root, self.old))

    def test_active_worker_pin_survives_gateway_advance_and_rejects_tampering(self) -> None:
        self.assertIsNone(install.active_worker_release(self.root))
        self.migrate()
        upgraded = replace(self.layout, gateway_release=self.new)
        install.stage(upgraded, scope="gateway", prior_services=self.running)
        install.publish(self.root)
        self.assertEqual(self.layout.current.resolve(), self.new)
        self.assertEqual(install.active_worker_release(self.root), self.old)
        manifest = json.loads(upgraded.manifest_path.read_text())
        manifest["worker_version"] = "incorrect"
        upgraded.manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError):
            install.active_worker_release(self.root)

    def test_duplicate_transaction_and_pending_legacy_recovery_are_preserved(self) -> None:
        pending = self.root / ".activation-transaction"
        pending.mkdir(mode=0o700)
        with self.assertRaisesRegex(RuntimeError, "legacy activation"):
            install.stage(self.layout, scope="migration", prior_services=self.absent)
        pending.rmdir()
        first = install.stage(self.layout, scope="migration", prior_services=self.absent)
        before = (self.layout.transaction_dir / "manifest.json").read_bytes()
        with self.assertRaises(FileExistsError):
            install.stage(self.layout, scope="migration", prior_services=self.absent)
        self.assertEqual((self.layout.transaction_dir / "manifest.json").read_bytes(), before)
        self.assertEqual(install._load(self.root)[0]["id"], first["id"])

    def test_missing_config_for_running_job_is_rejected_before_staging(self) -> None:
        with self.assertRaisesRegex(ValueError, "restorable configuration"):
            install.stage(self.layout, scope="migration", prior_services=self.running)
        self.assertFalse(self.layout.transaction_dir.exists())

    def test_unsafe_ancestor_and_release_links_are_rejected(self) -> None:
        linked = self.home / "linked"
        linked.symlink_to(self.config, target_is_directory=True)
        with self.assertRaises(ValueError):
            replace(self.layout, config_root=linked).validate()
        self.home.chmod(0o777)
        with self.assertRaises(PermissionError):
            self.layout.validate()
        self.home.chmod(0o700)
        linked_release = self.root / "releases/alias"
        linked_release.symlink_to(self.old, target_is_directory=True)
        with self.assertRaises(ValueError):
            replace(self.layout, worker_release=linked_release).validate()

    def test_journal_tampering_cannot_publish_arbitrary_file(self) -> None:
        install.stage(self.layout, scope="migration", prior_services=self.absent)
        journal = self.layout.transaction_dir / "manifest.json"
        value = json.loads(journal.read_text())
        value["paths"]["worker"] = str(self.base / "unrelated")
        journal.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "paths changed"):
            install.publish(self.root)
        self.assertFalse((self.base / "unrelated").exists())

    def test_interrupted_stage_keeps_retry_possible_without_cleanup(self) -> None:
        original = install._atomic_write

        def interrupted(path, data, mode=0o600):
            original(path, data, mode)
            raise RuntimeError("interrupted draft")

        with mock.patch.object(install, "_atomic_write", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted draft"):
                install.stage(self.layout, scope="migration", prior_services=self.absent)
        self.assertFalse(self.layout.transaction_dir.exists())
        self.assertFalse(self.layout.service_path("worker").exists())
        install.stage(self.layout, scope="migration", prior_services=self.absent)
        install.publish(self.root)
        install.commit(self.root, self.receipt(self.layout))
        install.finish(self.root)

    def test_finish_refuses_reverted_committed_state(self) -> None:
        self.migrate()
        upgraded = replace(self.layout, gateway_release=self.new)
        install.stage(upgraded, scope="gateway", prior_services=self.running)
        install.publish(self.root)
        install.commit(self.root, self.receipt(upgraded))
        install._replace_link(upgraded, str(self.old))
        with self.assertRaisesRegex(RuntimeError, "terminal release"):
            install.finish(self.root)
        self.assertTrue(upgraded.transaction_dir.is_dir())

    def test_cli_detailed_output_is_private_and_never_logged(self) -> None:
        layout = replace(self.layout, environment={"AGENTSDOCK_AGENT_TOKEN": "fixture-private-token"})
        source = self.base / "input.json"
        source.write_text(json.dumps(layout.to_dict()))
        source.chmod(0o600)
        states = self.base / "states.json"
        states.write_text(json.dumps(self.absent))
        states.chmod(0o600)
        output = self.base / "output.json"
        stream = io.StringIO()
        with redirect_stdout(stream):
            install.main(["manifest", "--layout", str(source), "--output", str(output)])
            install.main(["stage", "--layout", str(source), "--scope", "migration", "--prior-services", str(states)])
            install.main(["inspect", "--root", str(self.root), "--output", str(output)])
        self.assertNotIn("fixture-private-token", stream.getvalue())
        self.assertNotIn("AGENTSDOCK_AGENT_TOKEN", stream.getvalue())
        self.assertNotIn('"before"', stream.getvalue())
        self.assertIn("fixture-private-token", output.read_text())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
