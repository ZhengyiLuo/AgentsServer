from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest import mock
import uuid

import execution_install as files
import execution_uninstall as uninstall
from tests import test_execution_install as support


class Services:
    def __init__(self):
        self.states = {"worker": {"state": "running", "enabled": True, "pid": 120},
                       "gateway": {"state": "running", "enabled": True, "pid": 121}}
        self.events = []
        self.fail_stop = None
        self.on_stop = None

    def snapshot(self):
        return deepcopy(self.states)

    def set_enabled(self, role, enabled):
        self.events.append(("enabled", role, enabled))
        self.states[role]["enabled"] = enabled

    def stop(self, role):
        self.events.append(("stop", role))
        if self.fail_stop == role:
            raise RuntimeError("fixture stop refused")
        self.states[role] = {"state": "stopped", "enabled": self.states[role]["enabled"]}
        if self.on_stop:
            self.on_stop(role)

    def start(self, role):
        self.events.append(("start", role))
        self.states[role] = {"state": "running", "enabled": True, "pid": 121 if role == "gateway" else 120}

    def reload(self):
        self.events.append(("reload",))


class Control:
    def __init__(self, layout):
        self.layout = layout
        self.record = {"pid": 120, "instance_id": "worker_fixture", "release_root": str(layout.worker_release),
                       "callback_origin": "http://127.0.0.1:12345"}
        self.lease = None
        self.busy = False
        self.after_seal = None
        self.released = []

    def receipt(self, layout, services):
        return {"worker_pid": 120, "gateway_pid": 121, "worker_release": str(layout.worker_release),
                "worker_instance_id": "worker_fixture", "server_identity": "server_fixture"}

    def seal_for_stop(self, layout, operation, native_pid):
        if self.busy:
            raise RuntimeError("execution is busy")
        if self.lease is not None and self.lease.get("operation_id") != operation:
            raise RuntimeError("another operation holds execution")
        self.lease = self.lease or {"operation_id": operation, "lease_id": str(uuid.uuid4()), "sealed": True, "expires_at": None}
        if self.after_seal:
            self.after_seal()
        return {"record": deepcopy(self.record), "lease": deepcopy(self.lease)}

    def status(self, layout):
        return deepcopy(self.record), {"worker_instance_id": self.record["instance_id"], "idle": not self.busy,
                                       "lease": deepcopy(self.lease)}

    def _maintenance(self, layout, record, **kwargs):
        if self.lease["operation_id"] != kwargs["operation"] or self.lease["lease_id"] != kwargs["lease_id"]:
            raise RuntimeError("fixture wrong lease")
        self.released.append(kwargs)
        self.lease = None
        return {"lease": None}

    def _agent_token(self, layout):
        return "owned fixture"

    def callback_health(self, layout, record):
        return self._json(record["callback_origin"] + "/api/health", self._agent_token(layout))

    def _json(self, url, token):
        return {"ok": True, "server_identity": "server_fixture",
                "execution_service": {"pid": 120, "instance_id": self.record["instance_id"]}}


class SplitUninstallTests(unittest.TestCase):
    def setUp(self):
        self.support = support.ExecutionInstallTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.support.migrate()
        self.layout = self.support.layout
        self.root = self.layout.install_root
        self.state = self.layout.state_root
        self.config = self.layout.config_root
        (self.state / "admin").mkdir(mode=0o700)
        files._atomic_write(self.state / "admin/state-owner.lock", b"")
        for name, content in (("sessions.json", b"preserved history"),
                              ("secure-peers/client/identity.key", b"owned fixture peer"),
                              ("team-hub/team-hub.sqlite3", b"owned fixture hub"),
                              ("providers/sentinel", b"do not alter credentials")):
            path = self.state / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        files._atomic_write(self.config / "env", b"AGENTSDOCK_AGENT_TOKEN=fixture\n")
        self.services = Services()
        self.control = Control(self.layout)

    def invoke(self, *, purge=False):
        return uninstall.uninstall(self.root, config_root=self.config, state_root=self.state,
            home=self.layout.home, purge_state=purge, services=self.services, control=self.control)

    @staticmethod
    def contents(root):
        return {str(path.relative_to(root)): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

    def assert_preserved(self):
        self.assertTrue(self.root.exists())
        self.assertTrue(self.config.exists())
        self.assertTrue(self.layout.worker_release.exists())
        self.assertTrue(self.layout.gateway_release.exists())
        self.assertTrue(self.layout.service_path("worker").exists())
        self.assertTrue(self.layout.service_path("gateway").exists())
        self.assertTrue((self.state / "sessions.json").exists())

    def test_idle_pair_removed_only_after_both_stops_and_state_is_preserved(self):
        before = self.contents(self.state)
        self.services.on_stop = lambda role: self.assert_preserved()
        result = self.invoke()
        self.assertTrue(result["state_preserved"])
        self.assertEqual(self.contents(self.state), before)
        self.assertFalse(self.root.exists())
        self.assertFalse(self.config.exists())
        for role in ("worker", "gateway"):
            self.assertFalse(self.layout.service_path(role).exists())
        self.assertEqual(self.services.events, [("enabled", "gateway", False), ("enabled", "worker", False),
                                               ("stop", "gateway"), ("stop", "worker"), ("reload",)])

    def test_explicit_purge_removes_state_and_only_its_matching_legacy_alias(self):
        alias = self.layout.home / ".zenithbot-agent"
        alias.symlink_to(self.state, target_is_directory=True)
        unrelated = self.layout.home / "unrelated"
        unrelated.mkdir()
        sentinel = unrelated / "keep"
        sentinel.write_text("unrelated")
        (self.root / "outside-link").symlink_to(unrelated, target_is_directory=True)
        result = self.invoke(purge=True)
        self.assertFalse(result["state_preserved"])
        self.assertFalse(self.state.exists())
        self.assertFalse(alias.is_symlink())
        self.assertEqual(sentinel.read_text(), "unrelated")

    def test_uv_umask_directories_inside_private_root_are_removable(self):
        for path in (self.layout.worker_release / "__pycache__", self.layout.worker_release / ".venv/lib"):
            path.mkdir(exist_ok=True)
            path.chmod(0o775)
            (path / "owned-fixture").write_text("created with umask 0002")
        self.assertTrue(self.invoke()["removed"])
        self.assertFalse(self.root.exists())

    def test_exposed_removal_root_is_refused_before_service_changes(self):
        for root in (self.root, self.config):
            for mode in (0o755, 0o770, 0o777):
                root.chmod(mode)
                with self.subTest(root=root, mode=mode), self.assertRaises(PermissionError):
                    self.invoke()
                self.assert_preserved()
                self.assertEqual(self.services.events, [])
                root.chmod(0o700)

    def test_foreign_owned_or_mounted_descendant_is_refused(self):
        target = self.layout.worker_release / ".venv"
        original = Path.lstat
        for index, changed in ((4, os.getuid() + 1), (2, target.lstat().st_dev + 1)):
            def changed_stat(path, *args, **kwargs):
                result = original(path, *args, **kwargs)
                if path == target:
                    values = list(result)
                    values[index] = changed
                    return os.stat_result(values)
                return result
            with mock.patch.object(Path, "lstat", changed_stat):
                with self.assertRaises((PermissionError, RuntimeError)):
                    self.invoke()
            self.assert_preserved()
            self.assertEqual(self.services.events, [])

    def test_active_agent_or_another_lease_leaves_services_and_generations_untouched(self):
        for busy in (True, False):
            self.control.busy = busy
            self.control.lease = None if busy else {"operation_id": "foreign"}
            with self.assertRaisesRegex(RuntimeError, "busy|another operation"):
                self.invoke()
            self.assert_preserved()
            self.assertEqual(self.services.events, [])
            self.assertEqual(self.control.released, [])

    def test_pending_preparation_and_activation_journals_refuse_before_native_mutation(self):
        status = self.state / "admin/server-update.json"
        for phase in ("pending", "starting", "verifying", "installing", "unknown"):
            files._atomic_write(status, json.dumps({"phase": phase, "preparation_phase": "ready"}).encode())
            with self.assertRaisesRegex(RuntimeError, "pending"):
                self.invoke()
            self.assert_preserved()
        status.unlink()
        for name in (".activation-transaction", ".execution-transaction"):
            marker = self.root / name
            marker.symlink_to(self.root / "missing")
            with self.assertRaisesRegex(RuntimeError, "activation"):
                self.invoke()
            marker.unlink()
        self.assertEqual(self.services.events, [])

    def test_preparation_worker_lock_prevents_runtime_removal_even_with_terminal_status(self):
        directory = self.root / ".update-preparations" / ("a" * 32)
        directory.mkdir(mode=0o700, parents=True)
        directory.parent.chmod(0o700)
        lock = directory / "runner.lock"
        files._atomic_write(lock, b"")
        with uninstall._owned_lease(lock):
            with self.assertRaisesRegex(RuntimeError, "another process"):
                self.invoke()
        self.assert_preserved()
        self.assertEqual(self.services.events, [])

    def test_foreign_native_job_is_never_stopped_or_removed(self):
        path = self.layout.service_path("gateway")
        path.write_text("foreign service")
        with self.assertRaisesRegex(RuntimeError, "not this installed"):
            self.invoke()
        self.assertEqual(path.read_text(), "foreign service")
        self.assertEqual(self.services.events, [])
        self.assert_preserved()

    def test_running_native_pid_must_match_authenticated_process(self):
        self.services.states["worker"]["pid"] = 999
        with self.assertRaisesRegex(RuntimeError, "authenticated runtime"):
            self.invoke()
        self.assertEqual(self.services.events, [])
        self.assert_preserved()

    def test_stopped_broken_install_requires_recovery_not_guessed_provider_ownership(self):
        self.services.states["worker"] = {"state": "stopped", "enabled": False}
        with self.assertRaisesRegex(RuntimeError, "stopped or broken"):
            self.invoke()
        self.assertEqual(self.services.events, [])
        self.assert_preserved()

    def test_pending_update_appearing_after_seal_releases_only_own_hold_without_stopping(self):
        self.control.after_seal = lambda: files._atomic_write(self.state / "admin/server-update.json", b'{"phase":"pending"}')
        with self.assertRaisesRegex(RuntimeError, "pending"):
            self.invoke()
        self.assertEqual(self.services.events, [])
        self.assertEqual(len(self.control.released), 1)
        self.assertIsNone(self.control.lease)
        self.assert_preserved()

    def test_failed_worker_stop_restores_gateway_and_reopens_exact_still_running_worker(self):
        self.services.fail_stop = "worker"
        with self.assertRaisesRegex(RuntimeError, "fixture stop refused"):
            self.invoke()
        self.assert_preserved()
        self.assertEqual(self.services.states["gateway"]["state"], "running")
        self.assertEqual(self.services.states["worker"]["pid"], 120)
        self.assertEqual(len(self.control.released), 1)
        self.assertTrue(all(item["enabled"] for item in self.services.states.values()))

    def test_unknown_worker_after_failed_stop_never_restarts_or_releases_foreign_hold(self):
        def changed_after_gateway(role):
            if role == "gateway":
                self.services.states["worker"]["pid"] = 999
        self.services.on_stop = changed_after_gateway
        self.services.fail_stop = "worker"
        with self.assertRaisesRegex(RuntimeError, "ownership verification"):
            self.invoke()
        self.assert_preserved()
        self.assertEqual(self.control.released, [])
        self.assertFalse(any(event[0] == "start" for event in self.services.events))
        self.assertNotIn(("stop", "worker"), self.services.events)

    def test_state_owner_must_be_released_before_any_file_is_deleted(self):
        with uninstall._owned_lease(self.state / "admin/state-owner.lock"):
            with self.assertRaisesRegex(RuntimeError, "ownership verification"):
                self.invoke()
        self.assert_preserved()
        self.assertEqual(self.control.released, [])

    def test_purge_cli_never_accepts_yes_without_interactive_exact_path(self):
        process = subprocess.run([sys.executable, "-B", str(Path(uninstall.__file__)),
            "--root", str(self.root), "--config-root", str(self.config), "--state-root", str(self.state),
            "--home", str(self.layout.home), "--yes", "--purge-state"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertIn("--yes never bypasses", process.stderr)
        self.assert_preserved()

    def test_startup_holds_exact_retained_generation_and_conflicting_journal_refuses(self):
        operation = str(uuid.uuid4())
        value = uninstall._write_intent(self.root, self.layout, operation, False, "server_fixture")
        intent = self.root / uninstall.INTENT_NAME
        before = intent.read_bytes(), intent.stat()
        self.assertEqual(files.pending_worker_operation(self.root, self.layout.worker_release), operation)
        self.assertEqual((intent.read_bytes(), intent.stat()), before)
        with self.assertRaisesRegex(RuntimeError, "not retained"):
            files.pending_worker_operation(self.root, self.support.new)
        for name in (".execution-transaction", ".activation-transaction"):
            marker = self.root / name
            marker.symlink_to(self.root / "missing")
            with self.assertRaisesRegex(RuntimeError, "both claim"):
                files.pending_worker_operation(self.root, self.layout.worker_release)
            marker.unlink()
        intent.chmod(0o644)
        with self.assertRaises(PermissionError):
            files.pending_worker_operation(self.root, self.layout.worker_release)

    def test_startup_rejects_uninstall_generation_inode_substitution(self):
        uninstall._write_intent(self.root, self.layout, str(uuid.uuid4()), False, "server_fixture")
        original = self.layout.worker_release
        original.rename(original.with_name("retired-original"))
        self.support.release("1.0.0")
        with self.assertRaisesRegex(RuntimeError, "directory identity"):
            files.pending_worker_operation(self.root, self.layout.worker_release)

    def test_interrupted_stopped_uninstall_resumes_without_provider_or_service_start(self):
        uninstall._write_intent(self.root, self.layout, str(uuid.uuid4()), False, "server_fixture")
        self.services.states = {role: {"state": "stopped", "enabled": False} for role in ("worker", "gateway")}
        result = self.invoke()
        self.assertTrue(result["state_preserved"])
        self.assertEqual(self.services.events, [("reload",)])
        self.assertEqual(self.control.released, [])

    def test_recorded_stopped_uninstall_resumes_without_provider_or_service_start(self):
        value = uninstall._write_intent(self.root, self.layout, str(uuid.uuid4()), False, "server_fixture")
        files._atomic_write(self.root / uninstall.INTENT_NAME, files._json_bytes({**value, "phase": "stopped"}))
        self.services.states = {role: {"state": "stopped", "enabled": False} for role in ("worker", "gateway")}
        self.assertTrue(self.invoke()["state_preserved"])
        self.assertEqual(self.services.events, [("reload",)])

    def test_uninstall_never_deletes_runtime_with_unresolved_provider_registry(self):
        uninstall._write_intent(self.root, self.layout, str(uuid.uuid4()), False, "server_fixture")
        self.services.states = {role: {"state": "stopped", "enabled": False} for role in ("worker", "gateway")}
        files._atomic_write(self.state / "admin/provider-children.json", b'{"children":[{"pid":123,"kind":"fixture"}]}')
        with self.assertRaisesRegex(RuntimeError, "provider child ownership"):
            self.invoke()
        self.assert_preserved()
        self.assertEqual(self.services.events, [])

    def test_uninstall_retry_cannot_change_preserve_data_choice(self):
        uninstall._write_intent(self.root, self.layout, str(uuid.uuid4()), False, "server_fixture")
        with self.assertRaisesRegex(RuntimeError, "data-removal choice"):
            self.invoke(purge=True)
        self.assert_preserved()
        self.assertEqual(self.services.events, [])

    def test_retry_after_gateway_stop_uses_exact_held_worker_callback_without_restart(self):
        operation = str(uuid.uuid4())
        uninstall._write_intent(self.root, self.layout, operation, False, "server_fixture")
        self.control.lease = {"operation_id": operation, "lease_id": str(uuid.uuid4()), "sealed": True, "expires_at": None}
        self.services.states["gateway"] = {"state": "stopped", "enabled": False}
        self.assertTrue(self.invoke()["state_preserved"])
        self.assertNotIn(("stop", "gateway"), self.services.events)
        self.assertFalse(any(event[0] == "start" for event in self.services.events))

    def test_restarted_worker_epoch_keeps_same_uninstall_operation_and_can_resume(self):
        operation = str(uuid.uuid4())
        uninstall._write_intent(self.root, self.layout, operation, False, "server_fixture")
        self.control.record["instance_id"] = "restarted_worker_fixture"
        self.control.lease = {"operation_id": operation, "lease_id": str(uuid.uuid4()), "sealed": True, "expires_at": None}
        self.services.states["gateway"] = {"state": "stopped", "enabled": False}
        self.assertEqual(files.pending_worker_operation(self.root, self.layout.worker_release), operation)
        self.assertTrue(self.invoke()["state_preserved"])

    def test_lost_seal_ack_releases_only_its_recorded_operation_without_native_changes(self):
        original = self.control.seal_for_stop
        def lose_ack(*args):
            original(*args)
            raise RuntimeError("lost seal acknowledgement")
        with mock.patch.object(self.control, "seal_for_stop", side_effect=lose_ack):
            with self.assertRaisesRegex(RuntimeError, "lost seal acknowledgement"):
                self.invoke()
        self.assert_preserved()
        self.assertEqual(self.services.events, [])
        self.assertEqual(len(self.control.released), 1)
        self.assertIsNone(self.control.lease)
        self.assertFalse((self.root / uninstall.INTENT_NAME).exists())

    def test_work_appearing_before_seal_removes_only_unaccepted_intent(self):
        def newly_busy(*args):
            self.control.busy = True
            raise RuntimeError("new work arrived")
        with mock.patch.object(self.control, "seal_for_stop", side_effect=newly_busy):
            with self.assertRaisesRegex(RuntimeError, "new work arrived"):
                self.invoke()
        self.assert_preserved()
        self.assertEqual(self.services.events, [])
        self.assertEqual(self.control.released, [])
        self.assertFalse((self.root / uninstall.INTENT_NAME).exists())


if __name__ == "__main__":
    unittest.main()
