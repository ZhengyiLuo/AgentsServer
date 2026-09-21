from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

import execution_install as files
import execution_manage as manage
import test_execution_install as install_tests


class FakeServices:
    def __init__(self, layout, events):
        self.layout, self.events = layout, events
        self.states = {"worker": {"state": "running", "enabled": True, "pid": 120},
                       "gateway": {"state": "running", "enabled": True, "pid": 121}}
        self.worker_release = layout.worker_release
        self.gateway_release = layout.gateway_release
        self.worker_instance = "worker_epoch_initial"
        self.next_pid = 122
        self.control = None
        self.crash_next_start = False

    def snapshot(self):
        return deepcopy(self.states)

    def stop(self, role):
        self.events.append(("stop", role))
        if self.states[role]["state"] != "absent":
            self.states[role] = {"state": "stopped", "enabled": self.states[role]["enabled"]}

    def reload(self):
        self.events.append(("reload",))

    def start(self, role):
        self.events.append(("start", role))
        if self.crash_next_start:
            self.crash_next_start = False
            raise KeyboardInterrupt("simulated installer death")
        self.states[role] = {"state": "running", "enabled": True, "pid": self.next_pid}
        self.next_pid += 1
        if role == "worker":
            self.worker_release = files.active_worker_release(self.layout.install_root)
            self.worker_instance = f"worker_epoch_{self.states[role]['pid']}"
            operation = files.pending_worker_operation(self.layout.install_root, self.worker_release)
            self.control.lease = None if operation is None else {
                "operation_id": operation, "lease_id": "startup_lease", "sealed": True}
        else:
            self.gateway_release = self.layout.current.resolve()

    def restore(self, role, prior):
        if prior["state"] == "running":
            self.start(role)
        else:
            self.states[role] = dict(prior)


class FakeControl:
    def __init__(self, services, events):
        self.services, self.events = services, events
        self.busy = False
        self.lease = None
        self.bad_release = None
        self.make_failed_candidate_busy = False
        self.release_failures = 0
        self.reset_identity_release = None
        services.control = self

    def health(self, layout):
        return {"server_identity": "server_fixture_identity"}

    def status(self, layout):
        state = self.services.states["worker"]
        return {"pid": state["pid"], "instance_id": self.services.worker_instance}, {
            "worker_instance_id": self.services.worker_instance, "idle": not self.busy,
            "lease": deepcopy(self.lease)}

    def seal_for_stop(self, layout, operation, native_pid):
        self.events.append(("seal", self.services.worker_instance))
        if self.busy:
            raise RuntimeError("worker is busy")
        if native_pid != self.services.states["worker"]["pid"]:
            raise RuntimeError("worker process changed")
        self.lease = {"operation_id": operation, "lease_id": "sealed_lease", "sealed": True}
        return {"record": self.status(layout)[0], "lease": self.lease}

    def require_startup_hold(self, layout, operation):
        if not self.lease or self.lease["operation_id"] != operation or not self.lease["sealed"]:
            raise RuntimeError("candidate startup hold is missing")

    def release(self, layout, operation):
        if self.release_failures:
            self.release_failures -= 1
            raise RuntimeError("simulated release failure")
        if self.lease:
            self.events.append(("release", self.services.worker_instance))
            if self.lease["operation_id"] != operation:
                raise RuntimeError("another operation holds admission")
            self.lease = None

    def receipt(self, layout, services):
        self.events.append(("health", str(services.worker_release), str(services.gateway_release)))
        if self.bad_release in {services.worker_release, services.gateway_release}:
            if self.make_failed_candidate_busy:
                self.busy = True
            raise RuntimeError("candidate failed authenticated health")
        return {"gateway_version": (services.gateway_release / "VERSION").read_text().strip(),
                "worker_version": (services.worker_release / "VERSION").read_text().strip(),
                "worker_release": str(services.worker_release), "protocol_version": 1,
                "server_identity": "server_reset_identity" if services.worker_release == self.reset_identity_release else "server_fixture_identity",
                "gateway_pid": services.states["gateway"]["pid"],
                "worker_pid": services.states["worker"]["pid"], "worker_instance_id": services.worker_instance}


class ExecutionManageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = install_tests.ExecutionInstallTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.migrate()
        self.layout = self.fixture.layout
        self.events = []
        self.services = FakeServices(self.layout, self.events)
        self.control = FakeControl(self.services, self.events)

    def controller(self, layout):
        return manage.ActivationController(layout, services=self.services, control=self.control, health_timeout=0)

    def test_launchd_enablement_accepts_both_native_spellings_for_exact_label(self):
        native = manage.NativeServices(replace(self.layout, platform="Darwin"), run=mock.Mock())
        for spelling, enabled in (("true", False), ("false", True), ("disabled", False), ("enabled", True), (None, True)):
            with self.subTest(spelling=spelling):
                output = '\n "com.agentsdock.server-other" => disabled\n "comXagentsdockXserver" => disabled\n'
                if spelling is not None:
                    output += f' "com.agentsdock.server" => {spelling}\n'
                def command(args, **kwargs):
                    return subprocess.CompletedProcess(args, 0, output if args[1] == "print-disabled" else " pid = 123\n", "")
                with mock.patch.object(files, "_snapshot", return_value={"exists": True}), mock.patch.object(native, "_command", side_effect=command):
                    self.assertEqual(native._observe("worker"), {"state": "running", "enabled": enabled, "pid": 123})
        for output in ('"com.agentsdock.server" => unknown\n', '"com.agentsdock.server" => true\n"com.agentsdock.server" => enabled\n'):
            with mock.patch.object(files, "_snapshot", return_value={"exists": True}), mock.patch.object(native, "_command", return_value=subprocess.CompletedProcess([], 0, output, "")):
                with self.assertRaisesRegex(RuntimeError, "ambiguous enablement"):
                    native._observe("worker")

    def test_installer_preserves_both_launchd_disabled_spellings(self):
        source = (Path(__file__).parent / "install.sh").read_text()
        start = source.index('    if printf \'%s\\n\' "$disabled_services"')
        end = source.index('\n    fi', start) + len('\n    fi')
        script = 'set -eu\nLABEL=com.agentsdock.server\n' + source[start:end] + '\nprintf "%s" "$PRIOR_SERVICE_ENABLED"\n'
        for spelling, expected in (("true", "false"), ("disabled", "false"), ("false", "true"), ("enabled", "true"), (None, "true")):
            with self.subTest(spelling=spelling):
                output = ' "comXagentsdockXserver" => disabled\n "com.agentsdock.server-other" => true\n'
                if spelling is not None:
                    output += f' "com.agentsdock.server" => {spelling}\n'
                result = subprocess.run(["/bin/bash", "-c", script], env={**os.environ, "disabled_services": output}, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout, expected)

    def test_busy_gateway_update_never_mutates_or_stops_worker(self):
        self.control.busy = True
        worker_file = self.layout.service_path("worker")
        before = worker_file.stat().st_ino, self.services.worker_instance, self.services.states["worker"]["pid"]
        target = replace(self.layout, gateway_release=self.fixture.new)
        with manage.InstallationLock(self.fixture.root):
            result = self.controller(target).apply("gateway")
        self.assertTrue(result["ok"])
        self.assertEqual((worker_file.stat().st_ino, self.services.worker_instance, self.services.states["worker"]["pid"]), before)
        self.assertNotIn(("stop", "worker"), self.events)
        self.assertFalse(any(event[0] in {"seal", "release"} for event in self.events))
        self.assertEqual(self.services.gateway_release, self.fixture.new)

    def test_busy_worker_update_never_stops_either_service(self):
        self.control.busy = True
        target = replace(self.layout, worker_release=self.fixture.new)
        with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(RuntimeError, "busy"):
            self.controller(target).apply("worker")
        self.assertFalse(any(event[0] in {"stop", "start"} for event in self.events))
        self.assertEqual(files.active_worker_release(self.fixture.root), self.fixture.old)
        self.assertFalse(target.transaction_dir.exists())

    def test_existing_foreign_hold_refuses_before_staging(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        self.control.lease = {"operation_id": "another-operation", "lease_id": "other-lease", "sealed": True}
        before = dict(self.control.lease)
        with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(RuntimeError, "already owns"):
            self.controller(target).apply("worker")
        self.assertEqual(self.control.lease, before)
        self.assertFalse(target.transaction_dir.exists())
        self.assertFalse(any(event[0] in {"stop", "start", "release", "seal"} for event in self.events))

    def test_racing_foreign_hold_does_not_leave_an_unrelated_pending_transaction(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        foreign = {"operation_id": "another-operation", "lease_id": "other-lease", "sealed": True}

        def win_admission(*args, **kwargs):
            self.control.lease = dict(foreign)
            raise RuntimeError("another operation won admission")

        with manage.InstallationLock(self.fixture.root), \
                mock.patch.object(self.control, "seal_for_stop", side_effect=win_admission), \
                self.assertRaisesRegex(RuntimeError, "won admission"):
            self.controller(target).apply("worker")
        self.assertEqual(self.control.lease, foreign)
        self.assertFalse(target.transaction_dir.exists())
        self.assertEqual(files.active_worker_release(self.fixture.root), self.fixture.old)
        self.assertFalse(any(event[0] in {"stop", "start", "release"} for event in self.events))

    def test_worker_update_seals_before_stop_and_releases_after_commit(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        gateway_pid = self.services.states["gateway"]["pid"]
        with manage.InstallationLock(self.fixture.root):
            result = self.controller(target).apply("worker")
        self.assertTrue(result["ok"])
        self.assertLess(next(i for i, item in enumerate(self.events) if item[0] == "seal"),
                        self.events.index(("stop", "worker")))
        self.assertEqual(self.services.states["gateway"]["pid"], gateway_pid)
        self.assertEqual(self.events[-1][0], "release")
        self.assertFalse(target.transaction_dir.exists())
        self.assertIsNone(self.control.lease)

    def test_gateway_health_failure_rolls_back_only_gateway(self):
        target = replace(self.layout, gateway_release=self.fixture.new)
        self.control.bad_release = self.fixture.new
        with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(RuntimeError, "authenticated health"):
            self.controller(target).apply("gateway")
        self.assertEqual(self.services.gateway_release, self.fixture.old)
        self.assertEqual(self.services.states["worker"]["pid"], 120)
        self.assertNotIn(("stop", "worker"), self.events)
        self.assertFalse(any(event[0] in {"seal", "release"} for event in self.events))
        self.assertFalse(target.transaction_dir.exists())

    def test_worker_health_failure_reseals_candidate_before_rollback(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        self.control.bad_release = self.fixture.new
        with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(RuntimeError, "authenticated health"):
            self.controller(target).apply("worker")
        seals = [event for event in self.events if event[0] == "seal"]
        self.assertGreaterEqual(len(seals), 3)
        self.assertNotEqual(seals[0][1], seals[-1][1])
        self.assertEqual(self.services.worker_release, self.fixture.old)
        self.assertIsNone(self.control.lease)
        self.assertFalse(target.transaction_dir.exists())

    def test_unexpected_busy_candidate_is_never_killed_during_rollback(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        self.control.bad_release = self.fixture.new
        self.control.make_failed_candidate_busy = True
        with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(RuntimeError, "busy"):
            self.controller(target).apply("worker")
        self.assertEqual(self.services.worker_release, self.fixture.new)
        self.assertEqual(sum(event == ("stop", "worker") for event in self.events), 1)
        self.assertTrue(target.transaction_dir.exists())

    def test_interrupted_start_recovers_original_worker_with_new_startup_hold(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        controller = self.controller(target)
        self.services.crash_next_start = True
        with manage.InstallationLock(self.fixture.root), self.assertRaises(KeyboardInterrupt):
            controller.apply("worker")
        self.assertTrue(target.transaction_dir.exists())
        with manage.InstallationLock(self.fixture.root):
            result = controller.recover()
        self.assertEqual(result["phase"], "rolled-back")
        self.assertEqual(self.services.worker_release, self.fixture.old)
        self.assertIsNone(self.control.lease)

    def test_failed_release_keeps_committed_journal_and_recovery_never_rolls_back(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        self.control.release_failures = 1
        controller = self.controller(target)
        with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(RuntimeError, "release failure"):
            controller.apply("worker")
        self.assertEqual(files._load(self.fixture.root)[0]["phase"], "committed")
        stops = [event for event in self.events if event[0] == "stop"]
        with manage.InstallationLock(self.fixture.root):
            result = controller.recover()
        self.assertEqual(result["phase"], "committed")
        self.assertEqual([event for event in self.events if event[0] == "stop"], stops)
        self.assertEqual(self.services.worker_release, self.fixture.new)
        self.assertIsNone(self.control.lease)

    def test_worker_that_resets_identity_is_not_accepted_and_rolls_back(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        self.control.reset_identity_release = self.fixture.new
        with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(ValueError, "server identity"):
            self.controller(target).apply("worker")
        self.assertEqual(self.services.worker_release, self.fixture.old)
        self.assertFalse(target.transaction_dir.exists())

    def test_running_legacy_without_private_control_is_refused_before_staging(self):
        target = replace(self.layout, worker_release=self.fixture.new)
        with mock.patch.object(self.control, "status", side_effect=RuntimeError("legacy private control unavailable")):
            with manage.InstallationLock(self.fixture.root), self.assertRaisesRegex(RuntimeError, "legacy"):
                self.controller(target).apply("worker")
        self.assertFalse(target.transaction_dir.exists())
        self.assertEqual(self.events, [])

    def test_install_lock_blocks_second_installer_and_reaps_only_dead_owner(self):
        with manage.InstallationLock(self.fixture.root):
            with self.assertRaisesRegex(RuntimeError, "another installer"):
                with manage.InstallationLock(self.fixture.root):
                    self.fail("concurrent lock admitted")
        stale = self.fixture.root / ".install-lock"
        stale.mkdir(mode=0o700)
        files._atomic_write(stale / "pid", b"999999\n")
        with mock.patch.object(manage.os, "kill", side_effect=ProcessLookupError):
            with manage.InstallationLock(self.fixture.root):
                self.assertEqual((stale / "pid").read_text(), f"{os.getpid()}\n")
        self.assertFalse(stale.exists())

    def test_native_systemd_adapter_uses_distinct_jobs_without_kill_override(self):
        calls = []
        state = {"worker": "running", "gateway": "running"}

        def run(args, **kwargs):
            calls.append(args)
            role = "gateway" if files.GATEWAY_UNIT in args else "worker"
            if "show" in args:
                active = state[role] == "running"
                return subprocess.CompletedProcess(args, 0, stdout=("LoadState=loaded\n"
                    f"ActiveState={'active' if active else 'inactive'}\nUnitFileState=enabled\nMainPID={120 if active else 0}\n"), stderr="")
            if "stop" in args:
                state[role] = "stopped"
            if "start" in args:
                state[role] = "running"
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        adapter = manage.NativeServices(self.layout, run=run)
        adapter.stop("gateway")
        adapter.reload()
        adapter.start("gateway")
        self.assertIn(["systemctl", "--user", "stop", "--no-block", files.GATEWAY_UNIT], calls)
        self.assertIn(["systemctl", "--user", "start", "--no-block", files.GATEWAY_UNIT], calls)
        self.assertFalse(any("kill" in call for call in calls))
        self.assertFalse(any(files.WORKER_UNIT in call and ("stop" in call or "start" in call) for call in calls))

    def test_invalid_stopped_systemd_candidate_remains_recoverable(self):
        def run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=("LoadState=bad-setting\n"
                "ActiveState=inactive\nUnitFileState=enabled\nMainPID=0\n"), stderr="")
        adapter = manage.NativeServices(self.layout, run=run)
        self.assertEqual(adapter.snapshot()['worker']['state'], 'stopped')
        with mock.patch.object(adapter, '_command', return_value=subprocess.CompletedProcess([], 0,
                stdout="LoadState=bad-setting\nActiveState=active\nUnitFileState=enabled\nMainPID=120\n", stderr="")):
            with self.assertRaisesRegex(RuntimeError, "active process"):
                adapter.snapshot()


if __name__ == "__main__":
    unittest.main()
