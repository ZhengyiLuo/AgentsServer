"""Fixture-only paired lifecycle acceptance: no developer native services."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock
import uuid

import execution_install as files
import execution_manage as manage
import execution_uninstall as uninstall
import server_instances as instances
from tests import test_execution_uninstall as support


class Services(support.Services):
    def __init__(self):
        super().__init__()
        self.registration_valid = True
        self.on_start = None
        self.fail_start = None

    def validate_registered_bindings(self):
        if not self.registration_valid:
            raise RuntimeError("fixture foreign registration")

    def start(self, role):
        if self.fail_start == role:
            raise RuntimeError("fixture failed start")
        super().start(role)
        if self.on_start:
            self.on_start(role)


class Control(support.Control):
    protocol = 1
    identity = "server_fixture"

    def status(self, layout):
        record, status = super().status(layout)
        status["native_lifecycle_protocol"] = self.protocol
        return record, status

    def receipt(self, layout, services):
        result = super().receipt(layout, services)
        result.update(worker_instance_id=self.record["instance_id"], server_identity=self.identity,
                      worker_version="1.0.0", gateway_version="1.0.0")
        return result

    def require_startup_hold(self, layout, operation):
        if self.busy or not self.lease or self.lease.get("operation_id") != operation or not self.lease.get("sealed"):
            raise RuntimeError("fixture missing startup hold")

    def release(self, layout, operation):
        if self.lease is None:
            return
        self.require_startup_hold(layout, operation)
        self._maintenance(layout, self.record, operation=operation, lease_id=self.lease["lease_id"])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.support = support.SplitUninstallTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.layout = self.support.layout
        self.root = self.layout.install_root
        self.services = Services()
        self.control = Control(self.layout)
        self.controller = manage.LifecycleController(self.layout, services=self.services,
                                                    control=self.control, health_timeout=0)
        self.before_state = self.support.contents(self.layout.state_root)
        self.before_config = self.support.contents(self.layout.config_root)

        def startup(role):
            if role == "worker":
                operation = files.pending_worker_operation(self.root, self.layout.worker_release)
                self.control.record["instance_id"] = "worker_restarted"
                self.control.lease = {"operation_id": operation, "lease_id": str(uuid.uuid4()),
                                      "sealed": True, "expires_at": None}
        self.services.on_start = startup

    def assert_data_preserved(self):
        self.assertEqual(self.support.contents(self.layout.state_root), self.before_state)
        self.assertEqual(self.support.contents(self.layout.config_root), self.before_config)

    def test_stop_preserves_both_components_and_data_and_has_owned_retry(self):
        self.assertEqual(self.controller.run("stop")["status"], "stopped")
        self.assertEqual([event for event in self.services.events if event[0] == "stop"],
                         [("stop", "gateway"), ("stop", "worker")])
        intent = manage.read_lifecycle_intent(self.layout)
        self.assertEqual(intent["phase"], "stopped")
        self.assertEqual(files.pending_worker_operation(self.root, self.layout.worker_release), intent["operation_id"])
        self.assertEqual(self.controller.run("stop")["status"], "stopped")
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["operation_id"], intent["operation_id"])
        self.assert_data_preserved()

    def test_restart_stops_gateway_first_and_starts_worker_held_before_gateway(self):
        result = self.controller.run("restart")
        self.assertEqual(result["status"], "running")
        self.assertEqual([event for event in self.services.events if event[0] in {"stop", "start"}],
                         [("stop", "gateway"), ("stop", "worker"), ("start", "worker"), ("start", "gateway")])
        self.assertIsNone(self.control.lease)
        self.assertIsNone(manage.read_lifecycle_intent(self.layout))
        self.assert_data_preserved()

    def test_stop_then_start_uses_held_epoch_and_clears_only_owned_journal(self):
        self.controller.run("stop")
        self.services.events.clear()
        self.controller.run("start")
        self.assertEqual([event for event in self.services.events if event[0] == "start"],
                         [("start", "worker"), ("start", "gateway")])
        self.assertIsNone(manage.read_lifecycle_intent(self.layout))
        self.assert_data_preserved()

    def test_healthy_start_is_idempotent_and_does_not_require_idle(self):
        self.control.busy = True
        self.controller.run("start")
        self.assertEqual(self.services.events, [])
        self.assertIsNone(manage.read_lifecycle_intent(self.layout))

    def test_busy_or_foreign_admission_does_not_mutate_services(self):
        for busy, lease in ((True, None), (False, {"operation_id": str(uuid.uuid4())})):
            with self.subTest(busy=busy):
                self.control.busy, self.control.lease = busy, lease
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    self.controller.run("restart")
                self.assertEqual(self.services.events, [])
                self.assertIsNone(manage.read_lifecycle_intent(self.layout))

    def test_new_cli_old_installed_worker_requires_signed_update_before_mutation(self):
        for protocol in (None, 0, True, "1", 2):
            for action in ("stop", "restart"):
                with self.subTest(protocol=protocol, action=action):
                    self.control.protocol = protocol
                    with self.assertRaisesRegex(RuntimeError, "signed managed update first"):
                        self.controller.run(action)
                    self.assertEqual(self.services.events, [])
                    self.assertIsNone(manage.read_lifecycle_intent(self.layout))

    def test_foreign_registration_fails_before_either_service_mutates(self):
        self.services.registration_valid = False
        with self.assertRaisesRegex(RuntimeError, "foreign registration"):
            self.controller.run("restart")
        self.assertEqual(self.services.events, [])

    def test_changed_gateway_pid_after_seal_is_refused_before_disable(self):
        self.control.after_seal = lambda: self.services.states["gateway"].update(pid=999)
        with self.assertRaisesRegex(RuntimeError, "jobs changed"):
            self.controller.run("stop")
        self.assertEqual(self.services.events, [])

    def test_owned_stop_with_held_state_lease_cannot_complete_or_remove(self):
        self.controller.run("stop")
        with uninstall._owned_lease(self.layout.state_root / "admin/state-owner.lock"):
            with self.assertRaisesRegex(RuntimeError, "another process"):
                self.controller.run("start")
            with self.assertRaisesRegex(RuntimeError, "another process"):
                uninstall.uninstall(self.root, config_root=self.layout.config_root,
                    state_root=self.layout.state_root, home=self.layout.home, services=self.services, control=self.control)
        self.assertTrue(self.root.exists())
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["phase"], "stopped")

    def test_unknown_or_unjournaled_stopped_pair_is_not_guessed_safe(self):
        for state in ("stopped", "absent", "transitioning"):
            self.services.states["worker"] = {"state": state, "enabled": False}
            for action in ("start", "stop", "restart"):
                with self.subTest(state=state, action=action):
                    with self.assertRaisesRegex(RuntimeError, "ownership recovery"):
                        self.controller.run(action)
                    self.assertEqual(self.services.events, [])

    def test_prepared_update_blocks_lifecycle_without_mutation(self):
        files._atomic_write(self.layout.state_root / "admin/server-update.json", b'{"phase":"staged"}')
        with self.assertRaisesRegex(RuntimeError, "pending"):
            self.controller.run("stop")
        self.assertEqual(self.services.events, [])

    def test_activation_and_uninstall_conflicts_block_before_native_changes(self):
        for name in (".activation-transaction", ".execution-transaction", ".execution-uninstall.json"):
            path = self.root / name
            files._atomic_write(path, b"{}")
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.controller.run("stop")
            path.unlink()
            self.assertEqual(self.services.events, [])

    def test_worker_stop_failure_preserves_intent_and_retry_finishes_exact_epoch(self):
        self.services.fail_stop = "worker"
        with self.assertRaisesRegex(RuntimeError, "stop refused"):
            self.controller.run("stop")
        intent = manage.read_lifecycle_intent(self.layout)
        self.assertEqual(intent["phase"], "sealed")
        self.assertEqual(self.services.states["worker"]["state"], "running")
        self.assertEqual(self.services.states["gateway"]["state"], "stopped")
        self.services.fail_stop = None
        self.controller.run("stop")
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["operation_id"], intent["operation_id"])
        self.assert_data_preserved()

    def test_retry_refuses_changed_worker_epoch_or_lease(self):
        self.services.fail_stop = "worker"
        with self.assertRaises(RuntimeError):
            self.controller.run("stop")
        self.services.events.clear()
        self.services.fail_stop = None
        self.control.record["instance_id"] = "foreign_epoch"
        with self.assertRaisesRegex(RuntimeError, "epoch changed"):
            self.controller.run("stop")
        self.assertEqual(self.services.events, [])
        self.control.record["instance_id"] = "worker_fixture"
        self.control.lease["lease_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(RuntimeError, "admission changed"):
            self.controller.run("stop")
        self.assertEqual(self.services.events, [])

    def test_lost_seal_reply_can_retry_same_owned_admission(self):
        seal = self.control.seal_for_stop
        def lost(*args):
            seal(*args)
            raise RuntimeError("lost response")
        with mock.patch.object(self.control, "seal_for_stop", side_effect=lost):
            with self.assertRaisesRegex(RuntimeError, "owned retry evidence"):
                self.controller.run("stop")
        self.assertEqual(self.services.events, [])
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["phase"], "prepared")
        self.controller.run("stop")
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["phase"], "stopped")

    def test_work_winning_admission_race_keeps_actionable_same_operation_retry(self):
        seal = self.control.seal_for_stop
        def race(*args):
            self.control.busy = True
            return seal(*args)
        with mock.patch.object(self.control, "seal_for_stop", side_effect=race):
            with self.assertRaisesRegex(RuntimeError, "retry the same command when work is idle"):
                self.controller.run("stop")
        self.assertEqual(self.services.events, [])
        intent = manage.read_lifecycle_intent(self.layout)
        self.assertEqual(intent["phase"], "prepared")
        self.control.busy = False
        self.controller.run("stop")
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["operation_id"], intent["operation_id"])

    def test_unobserved_provider_children_keep_stopped_evidence_not_completion(self):
        files._atomic_write(self.layout.state_root / "admin/provider-children.json", b'{"children":[{"pid":17}]}')
        with self.assertRaisesRegex(RuntimeError, "provider child"):
            self.controller.run("stop")
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["phase"], "sealed")
        self.assertTrue(self.root.exists())

    def test_startup_without_hold_is_not_completed_and_gateway_not_started(self):
        self.controller.run("stop")
        self.services.events.clear()
        self.services.on_start = lambda _role: setattr(self.control, "lease", None)
        with self.assertRaisesRegex(RuntimeError, "held startup"):
            self.controller.run("start")
        self.assertEqual([event for event in self.services.events if event[0] == "start"], [("start", "worker")])
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["phase"], "starting")

    def test_gateway_start_failure_can_retry_without_restarting_worker(self):
        self.services.fail_start = "gateway"
        with self.assertRaisesRegex(RuntimeError, "failed start"):
            self.controller.run("restart")
        self.services.fail_start = None
        self.services.events.clear()
        self.controller.run("start")
        self.assertEqual([event for event in self.services.events if event[0] == "start"], [("start", "gateway")])

    def test_changed_startup_worker_is_not_replaced_or_released(self):
        self.services.fail_start = "gateway"
        with self.assertRaises(RuntimeError):
            self.controller.run("restart")
        self.services.fail_start = None
        self.services.events.clear()
        self.control.record["instance_id"] = "different_worker"
        with self.assertRaisesRegex(RuntimeError, "held startup"):
            self.controller.run("start")
        self.assertEqual(self.services.events, [])
        self.assertIsNotNone(self.control.lease)

    def test_wrong_server_identity_remains_held_and_retryable_not_completed(self):
        original_start = self.services.on_start
        def change(role):
            original_start(role)
            if role == "gateway":
                self.control.identity = "foreign_server"
        self.services.on_start = change
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self.controller.run("restart")
        self.assertIsNotNone(self.control.lease)
        self.assertEqual(manage.read_lifecycle_intent(self.layout)["phase"], "starting")

    def test_lost_release_ack_can_finish_without_restarting_or_stopping(self):
        release = self.control.release
        def lost(*args):
            release(*args)
            raise RuntimeError("lost release response")
        with mock.patch.object(self.control, "release", side_effect=lost):
            with self.assertRaisesRegex(RuntimeError, "lost release"):
                self.controller.run("restart")
        self.services.events.clear()
        self.controller.run("start")
        self.assertEqual(self.services.events, [])
        self.assertIsNone(manage.read_lifecycle_intent(self.layout))

    def test_owned_stop_removes_pair_without_starting_and_preserves_state(self):
        self.controller.run("stop")
        self.services.events.clear()
        result = uninstall.uninstall(self.root, config_root=self.layout.config_root,
            state_root=self.layout.state_root, home=self.layout.home, services=self.services, control=self.control)
        self.assertTrue(result["state_preserved"])
        self.assertEqual(self.services.events, [("reload",)])
        self.assertFalse(self.root.exists())
        self.assertFalse(self.layout.config_root.exists())
        self.assertEqual(self.support.contents(self.layout.state_root), self.before_state)
        for role in ("worker", "gateway"):
            self.assertFalse(self.layout.service_path(role).exists())

    def test_uninstall_refuses_lifecycle_partial_stop_without_starting(self):
        self.services.fail_stop = "worker"
        with self.assertRaises(RuntimeError):
            self.controller.run("stop")
        self.services.events.clear()
        with self.assertRaisesRegex(RuntimeError, "finish the owned lifecycle stop"):
            uninstall.uninstall(self.root, config_root=self.layout.config_root,
                state_root=self.layout.state_root, home=self.layout.home, services=self.services, control=self.control)
        self.assertEqual(self.services.events, [])
        self.assertTrue(self.root.exists())

    def test_adoption_crash_with_two_matching_journals_resumes_without_starting(self):
        self.controller.run("stop")
        lifecycle = manage.read_lifecycle_intent(self.layout)
        uninstall._write_intent(self.root, self.layout, lifecycle["operation_id"], False, lifecycle["server_identity"])
        self.assertEqual(files.pending_worker_operation(self.root, self.layout.worker_release), lifecycle["operation_id"])
        self.services.events.clear()
        uninstall.uninstall(self.root, config_root=self.layout.config_root,
            state_root=self.layout.state_root, home=self.layout.home, services=self.services, control=self.control)
        self.assertEqual(self.services.events, [("reload",)])

    def test_conflicting_uninstall_operation_or_activation_never_claims_startup(self):
        self.controller.run("stop")
        uninstall._write_intent(self.root, self.layout, str(uuid.uuid4()), False, "server_fixture")
        with self.assertRaisesRegex(RuntimeError, "conflict"):
            files.pending_worker_operation(self.root, self.layout.worker_release)
        (self.root / uninstall.INTENT_NAME).unlink()
        (self.root / files.TRANSACTION_NAME).mkdir()
        with self.assertRaisesRegex(RuntimeError, "both claim"):
            files.pending_worker_operation(self.root, self.layout.worker_release)

    def test_changed_manifest_symlink_or_worker_runtime_never_releases_owned_hold(self):
        self.controller.run("stop")
        self.services.events.clear()
        before = self.layout.manifest_path.read_bytes()
        files._atomic_write(self.layout.manifest_path, before + b"\n")
        with self.assertRaisesRegex(RuntimeError, "layout changed"):
            self.controller.run("start")
        self.assertEqual(self.services.events, [])


class ManifestTests(unittest.TestCase):
    setUp = LifecycleTests.setUp
    def test_legitimate_retained_history_is_accepted_without_rewriting_or_deleting(self):
        value = json.loads(self.layout.manifest_path.read_bytes())
        value["retained_releases"].append(str(self.support.support.new))
        data = files._json_bytes(value)
        files._atomic_write(self.layout.manifest_path, data)
        self.assertEqual(files.installed_layout(self.root), self.layout)
        self.assertEqual(files.active_worker_release(self.root), self.layout.worker_release)
        self.controller.run("restart")
        self.assertEqual(self.layout.manifest_path.read_bytes(), data)
        self.assertTrue(self.support.support.new.exists())

    def test_retained_history_rejects_foreign_symlink_missing_and_duplicate_entries(self):
        original = json.loads(self.layout.manifest_path.read_bytes())
        link = self.root / "releases/alias"
        link.symlink_to(self.support.support.new)
        for extra in (str(self.layout.home), str(link), str(self.root / "releases/missing"), str(self.layout.worker_release)):
            value = deepcopy(original)
            value["retained_releases"].append(extra)
            files._atomic_write(self.layout.manifest_path, files._json_bytes(value))
            with self.subTest(extra=extra), self.assertRaises((ValueError, OSError)):
                files.installed_layout(self.root)
        self.assertEqual(self.services.events, [])


class NativeBindingTests(unittest.TestCase):
    def setUp(self):
        self.support = support.SplitUninstallTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.layout = self.support.layout

    def test_linux_requires_exact_loaded_fragment_no_overrides_and_no_stale_cache(self):
        def invoke(field=None, value=None):
            def run(command, **_kwargs):
                role = "gateway" if files.GATEWAY_UNIT in command else "worker"
                output = {"FragmentPath": str(self.layout.service_path(role)), "DropInPaths": "", "NeedDaemonReload": "no"}
                if field:
                    output[field] = value
                return subprocess.CompletedProcess(command, 0, "\n".join(f"{key}={item}" for key, item in output.items()), "")
            return manage.NativeServices(self.layout, run=run).validate_registered_bindings()
        invoke()
        for field, value in (("FragmentPath", "/foreign.service"), ("DropInPaths", "/override.conf"), ("NeedDaemonReload", "yes")):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "registration"):
                invoke(field, value)

    def test_macos_requires_exact_loaded_path_program_and_arguments_even_without_pid(self):
        layout = replace(self.layout, platform="Darwin")
        def invoke(foreign=False, missing=False):
            def run(command, **_kwargs):
                if missing:
                    return subprocess.CompletedProcess(command, 113, "", "Could not find service")
                role = "gateway" if command[-1].endswith(files.GATEWAY_LABEL) else "worker"
                args = files.service_arguments(layout, role)
                output = f"path = {layout.service_path(role)}\nprogram = {args[0]}\narguments = {{\n" + "\n".join(args) + "\n}\n"
                if foreign:
                    output = output.replace("execution_service.py", "foreign.py")
                return subprocess.CompletedProcess(command, 0, output, "")
            return manage.NativeServices(layout, run=run).validate_registered_bindings()
        invoke()
        invoke(missing=True)
        with self.assertRaisesRegex(RuntimeError, "registration"):
            invoke(foreign=True)

    def test_macos_paths_with_spaces_preserved_and_ambiguous_or_unknown_output_denied(self):
        layout = replace(self.layout, platform="Darwin", home=self.layout.home / "space home",
                         worker_release=self.layout.worker_release / "space runtime")
        # Read-only registration parser fixture; rendering/validation and real
        # native startup are covered separately, never against this host.
        def runner(change):
            def run(command, **_kwargs):
                role = "gateway" if command[-1].endswith(files.GATEWAY_LABEL) else "worker"
                args = files.service_arguments(layout, role)
                output = f"path = {layout.service_path(role)}\nprogram = {args[0]}\narguments = {{\n" + "\n".join(args) + "\n}\n"
                return change(command, output)
            return manage.NativeServices(layout, run=run)
        good = lambda cmd, output: subprocess.CompletedProcess(cmd, 0, output, "")
        runner(good).validate_registered_bindings()
        for suffix in ("path = /foreign.plist\n", "program = /foreign/python\n"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(RuntimeError, "registration"):
                runner(lambda cmd, output: good(cmd, output + suffix)).validate_registered_bindings()
        with self.assertRaisesRegex(RuntimeError, "unknown"):
            runner(lambda cmd, output: subprocess.CompletedProcess(cmd, 1, "", "Permission denied")).validate_registered_bindings()


class InstanceBindingTests(unittest.TestCase):
    def setUp(self):
        self.support = support.support.ExecutionInstallTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.instance = instances.Instance("default", self.support.home)
        for previous, target in ((self.support.root, self.instance.runtime),
                                 (self.support.config, self.instance.config), (self.support.state, self.instance.state)):
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            previous.rename(target)
        self.support.root = self.instance.runtime
        self.support.layout = replace(self.support.layout, install_root=self.instance.runtime,
            config_root=self.instance.config, state_root=self.instance.state,
            worker_release=self.instance.runtime / "releases/1.0.0",
            gateway_release=self.instance.runtime / "releases/1.0.0")
        self.support.migrate()
        self.layout = self.support.layout
        self.services = Services()

    def test_exact_linux_split_bindings_are_recognized_without_legacy_fallback(self):
        instances.validate_binding(self.instance, "linux")
        with mock.patch.object(manage, "LifecycleController") as controller:
            instances.control(self.instance, "stop", "linux")
        controller.assert_called_once_with(self.layout)
        controller.return_value.run.assert_called_once_with("stop")

    def test_exact_darwin_split_bindings_are_recognized(self):
        layout = replace(self.layout, platform="Darwin")
        for role in ("worker", "gateway"):
            layout.service_path(role).parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            files._atomic_write(layout.service_path(role), files.render_service(layout, role))
        files._atomic_write(layout.manifest_path, files._json_bytes(files.layout_manifest(layout)))
        instances.validate_binding(self.instance, "darwin")

    def test_status_aggregates_both_components_and_never_reports_partial_as_running(self):
        with mock.patch.object(manage, "NativeServices", return_value=self.services):
            self.assertEqual(instances.service_status(self.instance, "linux"), "running")
            self.services.states["gateway"] = {"state": "stopped", "enabled": False}
            self.assertEqual(instances.service_status(self.instance, "linux"), "partial")
            self.services.states["worker"] = {"state": "stopped", "enabled": False}
            self.assertEqual(instances.service_status(self.instance, "linux"), "stopped")
            self.services.registration_valid = False
            self.assertEqual(instances.service_status(self.instance, "linux"), "unknown")

    def test_foreign_gateway_registration_and_roots_refuse_default_mutation(self):
        path = self.layout.service_path("gateway")
        files._atomic_write(path, files.render_service(self.layout, "gateway") + b"# foreign edit\n")
        with self.assertRaisesRegex(RuntimeError, "native service configuration"):
            instances.validate_binding(self.instance, "linux")
        files._atomic_write(path, files.render_service(self.layout, "gateway"))
        with self.assertRaisesRegex(ValueError, "roots/platform"):
            instances.validate_binding(self.instance, "darwin")

    def test_unsafe_manifest_cannot_fall_back_to_single_service(self):
        marker = self.layout.manifest_path
        original = marker.with_name("original.json")
        marker.rename(original)
        marker.symlink_to(original)
        with self.assertRaises(OSError):
            instances.validate_binding(self.instance, "linux")

    def test_named_instances_do_not_claim_default_split_layout(self):
        named = instances.Instance("separate", self.instance.home)
        self.assertIsNone(instances.split_layout(named, "linux"))


if __name__ == "__main__":
    unittest.main()
