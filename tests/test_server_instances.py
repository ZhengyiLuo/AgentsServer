"""Instance tests: temporary homes, mocked services; never import agent_server."""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import server_instances as instances
import update_runner

ROOT = Path(__file__).resolve().parents[1]


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agents-instances-test-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve() / "home"
        self.home.mkdir(mode=0o700)
        self.registry = instances.Registry(self.home)
        self.default = instances.Instance("default", self.home)
        self.work = instances.Instance("work", self.home)

    def configured(self, instance, port=7851, platform=sys.platform):
        instance.config.mkdir(parents=True, exist_ok=True)
        instance.runtime.mkdir(parents=True, exist_ok=True)
        instance.state.mkdir(parents=True, exist_ok=True)
        env = {**instance.environment(), "AGENTSDOCK_AGENT_PORT": str(port),
               "AGENTSDOCK_AGENT_BIND": "127.0.0.1", "AGENTSDOCK_AGENT_TOKEN": "test-token-not-real"}
        (instance.config / "env").write_text("".join(f"{key}={value}\n" for key, value in env.items()))
        (instance.state / "sessions.json").write_text('{"synthetic": "history"}')
        service = instance.service_file(platform)
        service.parent.mkdir(parents=True, exist_ok=True)
        if platform == "darwin":
            service.write_bytes(plistlib.dumps({"Label": instances.launchd_label(instance.name),
                "ProgramArguments": [str(instance.runtime / "current/.venv/bin/python"), str(instance.runtime / "current/agent_server.py"), "serve"],
                "EnvironmentVariables": env}))
        else:
            service.write_text(f"[Service]\nEnvironmentFile={instance.config / 'env'}\nExecStart={instance.runtime / 'current/.venv/bin/python'} {instance.runtime / 'current/agent_server.py'} serve --port {port}\n")

    def cli(self, *args, **patches):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(instances, "Registry", return_value=self.registry))
            stack.enter_context(patch.object(instances, "service_status", return_value="stopped"))
            output, errors = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(contextlib.redirect_stderr(errors))
            mocks = {name: stack.enter_context(patch.object(instances, name, **options)) for name, options in patches.items()}
            result = instances.main(list(args))
            return result, output.getvalue(), errors.getvalue(), mocks

    def test_names_are_bounded_and_cannot_be_paths(self):
        for name in ("", "../default", "a/b", "Work", "-x", "1name", "a" * 33, "a\n", "$(whoami)"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                instances.Instance(name, self.home)
        self.assertEqual(instances.instance_name("work-2"), "work-2")

    def test_names_do_not_encode_the_port(self):
        self.configured(self.work, port=7900)
        with patch.object(instances, "service_status", return_value="stopped"):
            self.assertEqual(instances.describe(self.work)["port"], 7900)
        self.assertEqual(self.work.name, "work")

    def test_all_managed_roots_are_disjoint_from_default(self):
        paths = []
        for instance in (self.default, self.work, instances.Instance("other", self.home)):
            paths.extend((instance.runtime, instance.config, instance.state, instance.logs))
        for index, first in enumerate(paths):
            for second in paths[index + 1:]:
                self.assertNotEqual(first, second)
                self.assertNotIn(first, second.parents)
                self.assertNotIn(second, first.parents)
        self.assertEqual(instances.service_name("default"), "agents-server")
        self.assertEqual(instances.launchd_label("work"), "com.agentsdock.server.work")

    def test_list_discovers_default_without_writing_a_registry(self):
        self.configured(self.default, 7850)
        result, output, _, _ = self.cli("list")
        self.assertEqual(result, 0)
        self.assertIn("default", output)
        self.assertIn("7850", output)
        self.assertIn("This machine only", output)
        self.assertNotIn("test-token", output)
        self.assertFalse(self.registry.root.exists())

    def test_list_discovers_direct_named_install(self):
        self.configured(self.work)
        self.assertEqual([item.name for item in self.registry.instances()], ["work"])

    def test_private_registry_contains_only_name_and_status(self):
        with self.registry.locked():
            self.registry.save(self.work, "installed")
        self.assertEqual(self.registry.file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.registry.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(json.loads(self.registry.file.read_text()), {"version": 1, "instances": {"work": {"status": "installed"}}})

    def test_registry_rejects_injected_deletion_paths(self):
        with self.registry.locked():
            self.registry.file.write_text(json.dumps({"version": 1, "instances": {"work": {"status": "installed", "state": str(self.home)}}}))
            with self.assertRaises(ValueError):
                self.registry.records()

    def test_registry_remembers_valid_port_without_paths_or_credentials(self):
        with self.registry.locked():
            self.registry.save(self.work, "installed", 7852)
            self.registry.save(self.work, "removed")
        self.assertEqual(self.registry.records()["work"], {"status": "removed", "port": 7852})
        for port in (0, 65536, True, "7852"):
            with self.subTest(port=port), self.registry.locked(), self.assertRaises(ValueError):
                self.registry.save(self.work, "removed", port)

    def test_symlink_and_writable_parents_are_rejected(self):
        self.work.config.parent.mkdir(parents=True)
        self.work.config.symlink_to(self.home)
        with self.assertRaises(ValueError):
            instances.validate_binding(self.work)
        self.work.config.unlink()
        self.work.config.parent.chmod(0o777)
        with self.assertRaises(ValueError):
            instances.validate_binding(self.work)

    def test_foreign_state_binding_is_rejected_before_service_calls(self):
        self.configured(self.work)
        env = self.work.config / "env"
        env.write_text(env.read_text().replace(str(self.work.state), str(self.default.state)))
        with patch.object(instances, "run") as run:
            with self.assertRaises(ValueError):
                instances.control(self.work, "stop")
            run.assert_not_called()

    def test_service_ownership_rejects_another_instances_plist(self):
        self.configured(self.work, platform="darwin")
        service = self.work.service_file("darwin")
        doc = plistlib.loads(service.read_bytes())
        doc["Label"] = instances.launchd_label("default")
        service.write_bytes(plistlib.dumps(doc))
        with self.assertRaises(ValueError):
            instances.validate_binding(self.work, "darwin")

    def test_linux_control_addresses_only_exact_named_unit(self):
        self.configured(self.work, platform="linux")
        with patch.object(instances, "run") as run:
            instances.control(self.work, "stop", "linux")
        run.assert_called_once_with(["systemctl", "--user", "stop", "agents-server-work.service"])

    def test_mac_restart_waits_for_unload_before_bootstrap(self):
        self.configured(self.work, platform="darwin")
        with patch.object(instances, "service_status", side_effect=["running", "stopped"]), patch.object(instances, "run") as run:
            instances.control(self.work, "restart", "darwin")
        self.assertEqual(run.call_args_list[0].args[0], ["launchctl", "bootout", f"gui/{os.getuid()}/com.agentsdock.server.work"])
        self.assertEqual(run.call_args_list[1].args[0][-1], str(self.work.service_file("darwin")))

    def test_auto_port_skips_registered_and_occupied_ports(self):
        self.configured(self.default, 7850)
        self.configured(self.work, 7851)
        with patch.object(instances, "port_available", side_effect=lambda port: port != 7852):
            self.assertEqual(instances.select_port(self.registry, None), 7853)
            with self.assertRaises(ValueError):
                instances.select_port(self.registry, 7851)
            with self.assertRaises(ValueError):
                instances.select_port(self.registry, 7852)

    def test_real_occupied_ephemeral_port_is_not_available(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.assertFalse(instances.port_available(listener.getsockname()[1]))

    def test_bad_ports_are_rejected(self):
        for port in (0, -1, 65536, True, "7851"):
            with self.subTest(port=port), self.assertRaises(ValueError):
                instances.port_available(port)

    def test_registry_lock_prevents_two_concurrent_managers(self):
        with self.registry.locked():
            with self.assertRaises(ValueError):
                with self.registry.locked():
                    self.fail("Second manager acquired lock")

    def test_same_state_cannot_be_served_twice_and_distinct_states_can(self):
        first = instances.acquire_state_lock(self.default.state)
        second = instances.acquire_state_lock(self.work.state)
        try:
            with self.assertRaises(ValueError):
                instances.acquire_state_lock(self.default.state)
        finally:
            first.__exit__(None, None, None)
            second.__exit__(None, None, None)
        with instances.exclusive_lock(self.default.state / ".server-process.lock"):
            pass

    def test_default_environment_cannot_leak_into_new_instance(self):
        with patch.dict(os.environ, {"AGENTSDOCK_AGENT_TOKEN": "secret", "AGENTSDOCK_TEAM_HUB_MODE": "host", "AGENTSDOCK_STATE_DIR": "/other", "AGENTS_SERVER_CONFIG_DIR": "/other-config"}):
            env = instances.clean_environment(self.work)
        self.assertNotIn("AGENTSDOCK_AGENT_TOKEN", env)
        self.assertNotIn("AGENTSDOCK_TEAM_HUB_MODE", env)
        self.assertEqual(env["AGENTSDOCK_STATE_DIR"], str(self.work.state))
        self.assertEqual(env["AGENTS_SERVER_CONFIG_DIR"], str(self.work.config))

    def test_runtime_rejects_default_configuration_for_named_server(self):
        env = self.work.environment()
        instances.validate_runtime_environment(env, self.home)
        env["AGENTS_SERVER_CONFIG_DIR"] = str(self.default.config)
        with self.assertRaises(ValueError):
            instances.validate_runtime_environment(env, self.home)

    def test_automatic_name_and_port_do_not_modify_existing_default(self):
        self.configured(self.default, 7850)
        before = self.snapshot(self.default)
        code, _, errors, mocks = self.cli("new", port_available={"return_value": True}, install_instance={})
        self.assertEqual(code, 0, errors)
        self.assertEqual(mocks["install_instance"].call_args.args, (instances.Instance("instance-1", self.home), 7851, "0.0.0.0"))
        self.assertEqual(self.snapshot(self.default), before)
        code, _, _, mocks = self.cli("new", port_available={"return_value": True}, install_instance={})
        self.assertEqual(mocks["install_instance"].call_args.args[0].name, "instance-2")

    def test_duplicate_name_and_preserved_history_are_not_overwritten(self):
        self.configured(self.work)
        code, _, _, mocks = self.cli("new", "--name", "work", install_instance={})
        self.assertEqual(code, 1)
        mocks["install_instance"].assert_not_called()
        orphan = instances.Instance("orphan", self.home)
        orphan.state.mkdir(parents=True)
        code, _, _, mocks = self.cli("new", "--name", "orphan", install_instance={})
        self.assertEqual(code, 1)
        mocks["install_instance"].assert_not_called()

    def test_explicit_port_conflict_never_stops_a_service(self):
        code, _, _, mocks = self.cli("new", "--port", "7851", port_available={"return_value": False}, install_instance={}, run={})
        self.assertEqual(code, 1)
        mocks["install_instance"].assert_not_called()
        mocks["run"].assert_not_called()

    def test_failed_install_keeps_recoverable_record(self):
        code, _, _, _ = self.cli("new", "--name", "work", port_available={"return_value": True}, install_instance={"side_effect": ValueError("mock failure")})
        self.assertEqual(code, 1)
        self.assertEqual(self.registry.records()["work"]["status"], "failed")

    def removed_history(self, *, port=7852):
        self.work.state.mkdir(parents=True)
        (self.work.state / "sessions.json").write_text("old imported chat index")
        (self.work.state / "upload.png").write_bytes(b"AgentsDock-only upload")
        with self.registry.locked():
            self.registry.save(self.work, "removed", port)

    def test_reuse_removed_name_backs_up_state_and_preserves_provider_and_default(self):
        self.configured(self.default, 7850)
        before = self.snapshot(self.default)
        self.removed_history()
        provider = self.home / ".claude/projects/native.jsonl"
        provider.parent.mkdir(parents=True)
        provider.write_text("original provider transcript")
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="release work") as prompt:
            code, output, errors, mocks = self.cli("new", "--name", "work", install_instance={}, port_available={"return_value": True})
        self.assertEqual(code, 0, errors)
        prompt.assert_called_once_with("Type 'release work' to confirm (Enter cancels): ")
        self.assertEqual(mocks["install_instance"].call_args.args, (self.work, 7852, "0.0.0.0"))
        backup, = (self.registry.root / "history-backups").glob("work-*/state")
        self.assertEqual((backup / "sessions.json").read_text(), "old imported chat index")
        self.assertEqual((backup / "upload.png").read_bytes(), b"AgentsDock-only upload")
        self.assertEqual(backup.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse(self.work.state.exists())  # Mock installer has not created the fresh state.
        self.assertEqual(provider.read_text(), "original provider transcript")
        self.assertEqual(self.snapshot(self.default), before)
        self.assertIn(str(backup), output)
        self.assertIn("AgentsDock-only content remains in the backup", output)
        self.assertEqual(self.registry.records()["work"], {"status": "installed", "port": 7852})

    def test_reuse_cancel_wrong_name_noninteractive_and_eof_leave_history_unchanged(self):
        self.removed_history()
        before = self.snapshot(self.work)
        for tty, answer in ((True, ""), (True, "yes"), (True, "release default"), (False, "release work"), (True, EOFError())):
            with self.subTest(tty=tty, answer=answer), patch("sys.stdin.isatty", return_value=tty), patch("builtins.input", **({"side_effect": answer} if isinstance(answer, EOFError) else {"return_value": answer})):
                code, _, errors, mocks = self.cli("new", "--name", "work", install_instance={}, port_available={"return_value": True})
            self.assertEqual(code, 1)
            self.assertIn("not confirmed", errors)
            mocks["install_instance"].assert_not_called()
            self.assertEqual(self.snapshot(self.work), before)
            self.assertFalse((self.registry.root / "history-backups").exists())
            self.assertEqual(self.registry.records()["work"]["status"], "removed")

    def test_reuse_explicit_port_overrides_saved_port(self):
        self.removed_history()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="release work"):
            code, _, errors, mocks = self.cli("new", "--name", "work", "--port", "7952", install_instance={}, port_available={"return_value": True})
        self.assertEqual(code, 0, errors)
        self.assertEqual(mocks["install_instance"].call_args.args[1], 7952)

    def test_reuse_occupied_port_never_prompts_or_moves_history(self):
        self.removed_history()
        before = self.snapshot(self.work)
        with patch("builtins.input") as prompt:
            code, _, errors, mocks = self.cli("new", "--name", "work", install_instance={}, port_available={"return_value": False})
        self.assertEqual(code, 1)
        self.assertIn("port is occupied", errors)
        prompt.assert_not_called()
        mocks["install_instance"].assert_not_called()
        self.assertEqual(self.snapshot(self.work), before)

    def test_reuse_legacy_record_requires_explicit_port(self):
        self.removed_history(port=None)
        with patch("builtins.input") as prompt:
            code, _, errors, mocks = self.cli("new", "--name", "work", install_instance={})
        self.assertEqual(code, 1)
        self.assertIn("Specify --port", errors)
        prompt.assert_not_called()
        mocks["install_instance"].assert_not_called()

    def test_reuse_port_becoming_busy_during_prompt_keeps_history(self):
        self.removed_history()
        before = self.snapshot(self.work)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="release work"):
            code, _, errors, mocks = self.cli("new", "--name", "work", install_instance={}, port_available={"side_effect": [True, False]})
        self.assertEqual(code, 1)
        self.assertIn("became occupied", errors)
        mocks["install_instance"].assert_not_called()
        self.assertEqual(self.snapshot(self.work), before)
        self.assertFalse((self.registry.root / "history-backups").exists())

    def test_reuse_rejects_state_or_backup_symlink_without_touching_target(self):
        self.removed_history()
        self.configured(self.default, 7850)
        before = self.snapshot(self.default)
        original_state = self.work.state.with_name("saved-work-test-fixture")
        self.work.state.rename(original_state)
        self.work.state.symlink_to(self.default.state)
        with patch("builtins.input") as prompt:
            code, _, _, mocks = self.cli("new", "--name", "work", install_instance={})
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        mocks["install_instance"].assert_not_called()
        self.work.state.unlink()  # Only this test-created symlink.
        original_state.rename(self.work.state)
        (self.registry.root / "history-backups").symlink_to(self.default.state)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="release work"):
            code, _, _, mocks = self.cli("new", "--name", "work", install_instance={}, port_available={"return_value": True})
        self.assertEqual(code, 1)
        mocks["install_instance"].assert_not_called()
        self.assertEqual(self.snapshot(self.default), before)
        self.assertEqual((self.work.state / "sessions.json").read_text(), "old imported chat index")

    def test_reuse_rejects_live_state_lock(self):
        self.removed_history()
        with instances.exclusive_lock(self.work.state / ".server-process.lock"):
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="release work"):
                code, _, errors, mocks = self.cli("new", "--name", "work", install_instance={}, port_available={"return_value": True})
        self.assertEqual(code, 1)
        self.assertIn("Another process owns", errors)
        mocks["install_instance"].assert_not_called()
        self.assertTrue((self.work.state / "sessions.json").exists())
        self.assertFalse((self.registry.root / "history-backups").exists())

    def test_reuse_installed_default_or_running_service_is_never_allowed(self):
        self.configured(self.default, 7850)
        self.configured(self.work)
        for name in ("default", "work"):
            with self.subTest(name=name), patch("builtins.input") as prompt:
                code, _, _, mocks = self.cli("new", "--name", name, install_instance={})
            self.assertEqual(code, 1)
            prompt.assert_not_called()
            mocks["install_instance"].assert_not_called()
        with self.registry.locked():
            self.registry.save(self.work, "removed", 7852)
        with patch("builtins.input") as prompt:
            code, _, errors, mocks = self.cli("new", "--name", "work", install_instance={})
        self.assertEqual(code, 1)
        self.assertIn("still installed", errors)
        prompt.assert_not_called()
        mocks["install_instance"].assert_not_called()

    def test_reuse_missing_plist_but_running_or_unknown_service_is_refused(self):
        self.removed_history()
        for status in ("running", "unknown"):
            with self.subTest(status=status), patch("builtins.input") as prompt:
                code, _, errors, mocks = self.cli("new", "--name", "work", install_instance={}, service_status={"return_value": status})
            self.assertEqual(code, 1)
            self.assertIn("cannot confirm", errors)
            prompt.assert_not_called()
            mocks["install_instance"].assert_not_called()

    def test_reuse_failed_install_leaves_old_data_in_reported_backup(self):
        self.removed_history()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="release work"):
            code, _, errors, _ = self.cli("new", "--name", "work", install_instance={"side_effect": ValueError("mock installer failure")}, port_available={"return_value": True})
        self.assertEqual(code, 1)
        backup, = (self.registry.root / "history-backups").glob("work-*/state")
        self.assertIn(str(backup), errors)
        self.assertEqual((backup / "sessions.json").read_text(), "old imported chat index")
        self.assertEqual(self.registry.records()["work"]["status"], "failed")

    def test_retry_failed_preflight_with_no_state_needs_no_release(self):
        with self.registry.locked():
            self.registry.save(self.work, "failed")  # Legacy failed record, like test2-somi.
        with patch("builtins.input") as prompt:
            code, _, errors, mocks = self.cli("new", "--name", "work", "--port", "7852", install_instance={}, port_available={"return_value": True})
        self.assertEqual(code, 0, errors)
        prompt.assert_not_called()
        self.assertEqual(mocks["install_instance"].call_args.args[1], 7852)

    def test_reuse_manifest_rejects_duplicate_names_before_confirmation(self):
        self.removed_history()
        manifest = self.home / "manifest.json"
        manifest.write_text(json.dumps([{"name": "work", "port": 7852}, {"name": "work", "port": 7853}]))
        with patch("builtins.input") as prompt:
            code, _, _, mocks = self.cli("install", "--manifest", str(manifest), install_instance={}, port_available={"return_value": True})
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        mocks["install_instance"].assert_not_called()
        self.assertTrue((self.work.state / "sessions.json").exists())

    def test_manifest_validates_entire_plan_before_install(self):
        manifest = self.home / "manifest.json"
        manifest.write_text(json.dumps([{"name": "one", "port": 7851}, {"name": "two", "port": 7851}]))
        code, _, _, mocks = self.cli("install", "--manifest", str(manifest), port_available={"return_value": True}, install_instance={})
        self.assertEqual(code, 1)
        mocks["install_instance"].assert_not_called()

    def test_manifest_allocates_unique_ports_and_reports_partial_failure(self):
        manifest = self.home / "manifest.json"
        manifest.write_text(json.dumps([{"name": "one"}, {"name": "two"}]))
        code, output, _, mocks = self.cli("install", "--manifest", str(manifest), port_available={"return_value": True}, install_instance={"side_effect": [ValueError("mock"), None]})
        self.assertEqual(code, 1)
        self.assertIn("one:7851, two:7852", output)
        self.assertEqual(self.registry.records()["two"]["status"], "installed")

    def test_bulk_remove_exclusion_does_not_call_default(self):
        self.configured(self.default, 7850)
        self.configured(self.work)
        code, output, errors, mocks = self.cli("remove", "--all", "--exclude", "default", "--yes", run={})
        self.assertEqual(code, 0, errors)
        command = mocks["run"].call_args.args[0]
        self.assertIn("work", command)
        self.assertNotIn("default", command)
        self.assertIn("1 AgentsServer", output)
        self.assertIn("PRESERVE history", output)
        self.assertTrue(output.endswith("\n\nSuccessful!\n"))
        self.assertEqual(output.count("Successful!"), 1)
        self.assertNotIn("remove completed", output)
        self.assertEqual(self.registry.records()["work"]["status"], "removed")
        self.assertEqual(self.registry.records()["work"]["port"], 7851)

    def test_failed_or_partial_removal_never_reports_success(self):
        self.configured(self.default, 7850)
        self.configured(self.work)
        for results in ([ValueError("mock failure"), None], [None, ValueError("mock failure")]):
            with self.subTest(results=results):
                code, output, errors, mocks = self.cli("remove", "--all", "--yes", run={"side_effect": results})
                self.assertEqual(code, 1)
                self.assertEqual(mocks["run"].call_count, 2)
                self.assertIn("failed", errors)
                self.assertNotIn("Successful!", output)

    def test_bulk_removal_reports_success_once(self):
        self.configured(self.default, 7850)
        self.configured(self.work)
        code, output, errors, mocks = self.cli("remove", "--all", "--yes", run={})
        self.assertEqual(code, 0, errors)
        self.assertEqual(mocks["run"].call_count, 2)
        self.assertEqual(output.count("Successful!"), 1)
        self.assertTrue(output.endswith("\n\nSuccessful!\n"))

    def test_bulk_remove_requires_interactive_confirmation(self):
        self.configured(self.work)
        with patch("sys.stdin.isatty", return_value=False):
            code, output, _, mocks = self.cli("remove", "--all", run={})
        self.assertEqual(code, 1)
        mocks["run"].assert_not_called()
        self.assertIn("1 AgentsServer", output)

    def test_misspelled_exclusion_cannot_remove_default(self):
        self.configured(self.default, 7850)
        code, _, errors, mocks = self.cli("remove", "--all", "--exclude", "defualt", "--yes", run={})
        self.assertEqual(code, 1)
        self.assertIn("Unknown excluded", errors)
        mocks["run"].assert_not_called()

    def test_yes_never_bypasses_history_purge_confirmation(self):
        self.configured(self.work)
        with patch("sys.stdin.isatty", return_value=False):
            code, output, _, mocks = self.cli("remove", "work", "--yes", "--purge-state", run={})
        self.assertEqual(code, 1)
        mocks["run"].assert_not_called()
        self.assertIn("CANNOT BE UNDONE", output)

    def test_exact_name_confirmation_is_required_and_warning_is_colored(self):
        for answer in ("UNINSTALL 1", "uninstall 1", "uninstall", "uninstall default", "uninstall work extra", ""):
            with self.subTest(answer=answer), contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=answer), patch.object(instances, "show"):
                with self.assertRaises(ValueError):
                    instances.confirm_removal([self.work], False, False)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(output, "isatty", return_value=True), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="uninstall work") as prompt, patch.object(instances, "show"), patch.dict(os.environ, {"TERM": "xterm"}, clear=True):
            instances.confirm_removal([self.work], False, False)
        self.assertIn("\033[1;31mWARNING", output.getvalue())
        self.assertIn(f"\033[31m  Remove runtime: {self.work.runtime}\033[0m\n", output.getvalue())
        self.assertIn(f"\033[32m  PRESERVE history: {self.work.state}\033[0m\n", output.getvalue())
        prompt.assert_called_once_with("Type 'uninstall work' to confirm: ")

    def test_bulk_confirmation_requires_every_exact_selected_name(self):
        for answer in ("UNINSTALL 2", "uninstall 2", "uninstall all", "uninstall work", "uninstall work default"):
            with self.subTest(answer=answer), contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=answer), patch.object(instances, "show"):
                with self.assertRaises(ValueError):
                    instances.confirm_removal([self.default, self.work], False, False)
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="uninstall default work") as prompt, patch.object(instances, "show"):
            instances.confirm_removal([self.default, self.work], False, False)
        prompt.assert_called_once_with("Type 'uninstall default work' to confirm: ")

    def test_wrong_name_or_count_does_not_invoke_uninstaller(self):
        self.configured(self.default, 7850)
        self.configured(self.work)
        for answer in ("UNINSTALL 1", "uninstall default", "uninstall work default"):
            with self.subTest(answer=answer), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=answer):
                code, _, errors, mocks = self.cli("remove", "work", run={})
            self.assertEqual(code, 1)
            self.assertIn("Not confirmed", errors)
            mocks["run"].assert_not_called()
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="uninstall work"):
            code, _, errors, mocks = self.cli("remove", "work", run={})
        self.assertEqual(code, 0, errors)
        self.assertEqual(mocks["run"].call_args.args[0][-3:], ["--managed-instance", "work", "--yes"])

    def test_history_purge_requires_explicit_action_and_names_even_with_yes(self):
        for answer in ("DELETE HISTORY 1", "delete history 1", "uninstall work", "delete history default"):
            with self.subTest(answer=answer), contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value=answer), patch.object(instances, "show"):
                with self.assertRaises(ValueError):
                    instances.confirm_removal([self.work], True, True)
        with contextlib.redirect_stdout(io.StringIO()), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="delete history work") as prompt, patch.object(instances, "show"):
            instances.confirm_removal([self.work], True, True)
        prompt.assert_called_once_with("Type 'delete history work' to confirm: ")

    def test_loopback_binding_never_advertises_lan(self):
        with patch.object(instances.socket, "getaddrinfo") as resolve:
            self.assertEqual(instances.candidate_addresses("127.0.0.1", 7900), ["http://127.0.0.1:7900 (This machine only)"])
            self.assertEqual(instances.candidate_addresses("::1", 7900), ["http://[::1]:7900 (This machine only)"])
            resolve.assert_not_called()

    def test_specific_bind_only_advertises_that_ip(self):
        self.assertEqual(instances.candidate_addresses("192.0.2.1", 7900), ["http://192.0.2.1:7900"])

    def test_update_rejects_old_default_only_release(self):
        release = self.home / "release"
        release.mkdir()
        (release / "install.sh").write_text("#!/bin/sh\n")
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTANCE": "work"}):
            with self.assertRaisesRegex(RuntimeError, "does not support named instances"):
                update_runner.instance_installer_arguments(release)
            self.assertEqual(update_runner.instance_installer_arguments(ROOT), ["--instance", "work"])
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTANCE": "default"}):
            self.assertEqual(update_runner.instance_installer_arguments(release), [])

    def snapshot(self, instance):
        return {str(path.relative_to(self.home)): path.read_bytes() for root in (instance.runtime, instance.config, instance.state) if root.exists() for path in root.rglob("*") if path.is_file()}

    def test_real_uninstaller_with_mocked_launchd_preserves_7850_fixture(self):
        output = self.check_named_uninstaller_output()
        self.assertNotIn("\033[", output)

    def test_named_uninstall_terminal_colors_spacing_and_single_success(self):
        output = self.check_named_uninstaller_output(terminal=True)
        self.assertIn(f"\033[31mRemoving release runtime at {self.work.runtime}\033[0m\n", output)
        self.assertIn(f"\033[31mRemoving configuration at {self.work.config}\033[0m\n", output)
        self.assertIn("\n\n\033[32mPreserved chat history", output)
        self.assertIn(f"{self.work.state}.\033[0m\n\nTo reuse this named history", output)
        self.assertIn("\n\nNote: persistent chat terminals", output)
        self.assertTrue(output.endswith("\n\n\033[32mSuccessful!\033[0m\n"), output)

    def test_named_uninstall_no_color_remains_plain_in_terminal(self):
        output = self.check_named_uninstaller_output(terminal=True, extra_env={"NO_COLOR": ""})
        self.assertNotIn("\033[", output)
        self.assertTrue(output.endswith("\n\nSuccessful!\n"))

    def test_named_uninstall_dumb_terminal_remains_plain(self):
        output = self.check_named_uninstaller_output(terminal=True, extra_env={"TERM": "dumb"})
        self.assertNotIn("\033[", output)
        self.assertTrue(output.endswith("\n\nSuccessful!\n"))

    def check_named_uninstaller_output(self, *, terminal=False, extra_env=None):
        # All destructive commands target this test's temporary home; launchd
        # is stubbed. Run the actual manager + child to check color propagation
        # and ensure they do not both emit a success footer.
        self.configured(self.default, 7850, "darwin")
        self.configured(self.work, 7851, "darwin")
        self.default.logs.mkdir(parents=True)
        (self.default.logs / "server.log").write_text("default log stays")
        self.work.logs.mkdir(parents=True)
        (self.work.logs / "server.log").write_text("named log")
        legacy = self.home / ".zenithbot-agent"
        legacy.symlink_to(self.default.state)
        fake_bin = self.home / "bin"
        fake_bin.mkdir()
        calls = self.home / "launchctl-calls"
        for name, content in {"uname": "#!/bin/sh\necho Darwin\n", "launchctl": '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_SERVICE_LOG"\nexit 1\n'}.items():
            target = fake_bin / name
            target.write_text(content)
            target.chmod(0o755)
        (fake_bin / "python3").symlink_to(sys.executable)
        before = self.snapshot(self.default)
        service_before = self.default.service_file("darwin").read_bytes()
        env = {**instances.clean_environment(self.work), "HOME": str(self.home), "PATH": f"{fake_bin}:/usr/bin:/bin", "TEST_SERVICE_LOG": str(calls), "TERM": "xterm"}
        env.pop("NO_COLOR", None)
        env.update(extra_env or {})
        command = ["/bin/bash", str(ROOT / "uninstall.sh"), "--instance", "work", "--yes"]
        if terminal:
            from tests.test_installer_token_output import TokenOutputTests
            output = TokenOutputTests().run_terminal("exec " + shlex.join(command), environment=env)
        else:
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = result.stdout
        self.assertEqual(self.snapshot(self.default), before)
        self.assertEqual(self.default.service_file("darwin").read_bytes(), service_before)
        self.assertEqual((self.default.logs / "server.log").read_text(), "default log stays")
        self.assertTrue(legacy.is_symlink())
        self.assertTrue((self.work.state / "sessions.json").exists())
        self.assertFalse(self.work.runtime.exists())
        self.assertFalse(self.work.config.exists())
        self.assertFalse(self.work.service_file("darwin").exists())
        self.assertTrue(all("com.agentsdock.server.work" in line for line in calls.read_text().splitlines()))
        self.assertIn("./install.sh --instance work --port PORT", output)
        self.assertIn("tmux -L agents-server-work ls", output)
        self.assertNotIn("Re-running ./install.sh will pick", output)
        self.assertNotIn("tmux sessions named zd_*", output)
        self.assertNotIn("remove completed", output)
        self.assertEqual(output.count("Successful!"), 1)
        return output

    def test_bare_uninstall_never_proceeds_without_confirmation(self):
        self.configured(self.default, 7850)
        fake_bin = self.home / "bin"
        fake_bin.mkdir()
        (fake_bin / "python3").symlink_to(sys.executable)
        for command in ("launchctl", "systemctl"):
            file = fake_bin / command
            file.write_text("#!/bin/sh\nexit 1\n")
            file.chmod(0o755)
        before = self.snapshot(self.default)
        env = {**os.environ, "HOME": str(self.home), "PATH": f"{fake_bin}:/usr/bin:/bin"}
        result = subprocess.run(["/bin/bash", str(ROOT / "uninstall.sh")], env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("1 AgentsServer", result.stdout)
        self.assertIn("Not confirmed", result.stderr)
        self.assertEqual(self.snapshot(self.default), before)

    def test_installer_renders_named_service_without_touching_default(self):
        source = (ROOT / "install.sh").read_text()
        body = source[source.index("write_service_files() {"):source.index("HEALTH_CHECK_HEARTBEAT_ATTEMPTS=")]
        self.configured(self.default, 7850, "darwin")
        before = self.snapshot(self.default)
        for platform in ("Darwin", "Linux"):
            with self.subTest(platform=platform):
                service = self.work.service_file("darwin" if platform == "Darwin" else "linux")
                script = "set -eu\n" + self.work.shell_bindings() + "\n" + "\n".join(f"{key}={shlex.quote(value)}" for key, value in {
                    "HOME": str(self.home), "OS_NAME": platform, "INSTANCE_NAME": "work", "CURRENT_LINK": str(self.work.runtime / "current"), "ENV_FILE": str(self.work.config / "env"), "PORT": "7851", "BIND_ADDRESS": "127.0.0.1", "TOKEN": "synthetic-token", "SERVER_NAME": "", "SERVER_PATH": "/usr/bin:/bin", "TEAM_HUB_MODE": "disabled", "TEAM_HUB_TRANSPORT": "loopback", "TEAM_HUB_URL": "", "TEAM_HUB_DIRECT_IP_URL": "", "ACTIVATION_TRANSACTION_ID": "test", "SERVICE_OUTPUT": str(service)}.items())
                script += '\nreplace_activation_config() { cp "$2" "$SERVICE_OUTPUT"; }\n' + body + "\nwrite_service_files\n"
                result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                contents = service.read_text()
                self.assertIn(str(self.work.config), contents)
                if platform == "Darwin":
                    doc = plistlib.loads(service.read_bytes())
                    self.assertEqual(doc["Label"], "com.agentsdock.server.work")
                    self.assertEqual(doc["EnvironmentVariables"]["AGENTS_SERVER_INSTANCE"], "work")
                    self.assertEqual(doc["StandardOutPath"], str(self.work.logs / "server.log"))
        self.assertEqual(self.snapshot(self.default), before)

    def test_named_linux_activation_and_rollback_never_manage_legacy_service(self):
        source = (ROOT / "install.sh").read_text()
        restart = source[source.index("restart_service() {"):source.index("restore_previous_release_transaction() {")]
        restore = source[source.index("restore_prior_service_state() {"):source.index("restore_team_hub_snapshot() {")]
        script = '''set -eu
OS_NAME=Linux
INSTANCE_NAME=work
SERVICE_NAME=agents-server-work
LEGACY_SERVICE_NAME=zenithbot-agent
LEGACY_SERVICE_FILE=/synthetic/legacy.service
SYSTEMD_SERVICE_FILE=/synthetic/agents-server-work.service
PRIOR_SERVICE_ENABLED=true
PRIOR_SERVICE_STATE=running
PRIOR_LEGACY_SERVICE_ENABLED=true
PRIOR_LEGACY_SERVICE_STATE=running
MANAGED_UPDATE_ID=
systemctl() { printf '%s\\n' "$*"; }
''' + restart + restore + "\nrestart_service\nrestore_prior_service_state\n"
        result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("agents-server-work.service", result.stdout)
        self.assertNotIn("zenithbot-agent", result.stdout)
        self.assertNotIn(" agents-server.service", result.stdout)

    def test_complete_named_installer_with_fake_services_preserves_default(self):
        self.check_complete_named_install()

    def test_complete_release_and_fresh_install_with_fake_services_preserves_default(self):
        self.check_complete_named_install(release=True)

    def check_complete_named_install(self, *, release=False):
        # Reuse the established installer's fake dependency/health layer. All
        # service commands are stubbed; no uv downloads or OS jobs are started.
        from tests.test_installer import InstallerContractTests
        fixture = InstallerContractTests()
        fake_bin = self.home / "bin"
        fake_bin.mkdir()
        (fake_bin / "python3").symlink_to(sys.executable)
        fixture.write_executable(fake_bin / "uname", '#!/bin/sh\nif [ "${1:-}" = -m ]; then echo arm64; else echo Darwin; fi\n')
        fixture.write_executable(fake_bin / "tmux", "#!/bin/sh\nexit 0\n")
        fixture.write_exact_health_uv(fake_bin)
        fixture.write_json_health_curl(fake_bin)
        loaded = self.home / "loaded"
        calls = self.home / "calls"
        fixture.write_executable(fake_bin / "launchctl", '''#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_EVENT_LOG"
case "$1" in
  print-disabled) echo '{}'; exit 0 ;;
  print)
    if [ "$2" = "gui/$(id -u)" ]; then exit 0; fi
    if [ -f "$FAKE_LOADED" ]; then echo 'pid = 4242;'; exit 0; fi
    echo 'Could not find service' >&2; exit 3 ;;
  bootstrap) touch "$FAKE_LOADED"; exit 0 ;;
  bootout) rm -f "$FAKE_LOADED"; exit 0 ;;
  enable|disable) exit 0 ;;
esac
exit 2
''')
        self.configured(self.default, 7850, "darwin")
        before = self.snapshot(self.default)
        default_service = self.default.service_file("darwin").read_bytes()
        env = {**instances.clean_environment(self.work), "HOME": str(self.home),
               "PATH": f"{fake_bin}:/usr/bin:/bin", "REAL_PYTHON": sys.executable,
               "FAKE_EVENT_LOG": str(calls), "FAKE_LOADED": str(loaded),
               "FAKE_HEALTH_VERSION": fixture.release_version(),
               "FAKE_SERVER_IDENTITY": "named_instance_test_12345678", "FAKE_TEAM_HUB_ID": "",
               "FAKE_TEAM_HUB_MODE": "disabled", "AGENTS_SERVER_HEALTH_CHECK_ATTEMPTS": "1"}
        if release:
            self.removed_history(port=17851)
            with patch.dict(os.environ, env, clear=True), patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="release work"):
                code, output, errors, _ = self.cli(
                    "new", "--name", "work",
                    run={"side_effect": lambda command, **kwargs: subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)},
                )
            result = subprocess.CompletedProcess([], code, output, errors)
            backup, = (self.registry.root / "history-backups").glob("work-*/state")
            self.assertEqual((backup / "sessions.json").read_text(), "old imported chat index")
            self.assertEqual((backup / "upload.png").read_bytes(), b"AgentsDock-only upload")
            self.assertFalse((self.work.state / "upload.png").exists())
            self.assertEqual(self.registry.records()["work"], {"status": "installed", "port": 17851})
        else:
            result = subprocess.run(["/bin/bash", str(ROOT / "install.sh"), "--instance", "work", "--port", "17851", "--non-interactive"], env=env, capture_output=True, text=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stderr[-6000:])
        self.assertEqual(self.snapshot(self.default), before)
        self.assertEqual(self.default.service_file("darwin").read_bytes(), default_service)
        self.assertNotIn("/com.agentsdock.server\n", calls.read_text())
        self.assertTrue((self.work.runtime / "current/server_instances.py").is_file())
        config = instances.read_config(self.work)
        self.assertEqual(config["AGENTS_SERVER_INSTANCE"], "work")
        self.assertEqual(config["AGENTSDOCK_AGENT_PORT"], "17851")
        self.assertNotEqual(config["AGENTSDOCK_AGENT_TOKEN"], "test-token-not-real")
        instances.validate_binding(self.work, "darwin")


class RuntimeBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Extract just these pure/probe functions. Never import/start the server.
        source = (ROOT / "agent_server.py").read_text()
        tree = ast.parse(source)
        names = {"terminal_session_name", "run_tmux", "server_update_runner_environment"}
        cls.functions = compile(ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names], type_ignores=[]), "instance-runtime", "exec", flags=__import__("__future__").annotations.compiler_flag)

    def namespace(self, name):
        values = {"SERVER_INSTANCE_NAME": name, "TMUX_INSTANCE_ARGS": () if name == "default" else ("-L", "agents-server-" + name), "TMUX_COMMAND_TIMEOUT_SECONDS": 1, "Path": Path, "os": os, "sys": sys, "re": re, "subprocess": subprocess, "CONFIG_ENV_FILE": Path("/synthetic/config/env"), "STATE_DIR": Path("/synthetic/state"), "tmux_bin": lambda: "/synthetic/tmux"}
        exec(self.functions, values)
        return values

    def test_default_terminal_names_unchanged_and_named_disjoint(self):
        default, work, other = (self.namespace(name) for name in ("default", "work", "other"))
        self.assertEqual(default["terminal_session_name"]("sess_a"), "zd_sess_a")
        names = {value["terminal_session_name"]("sess_a") for value in (default, work, other)}
        self.assertEqual(len(names), 3)

    def test_named_tmux_commands_use_a_separate_socket(self):
        with patch.object(subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            namespace = self.namespace("work")
            namespace["tmux_bootstrap_environment"] = lambda: {}
            namespace["run_tmux"](["list-sessions"])
        self.assertEqual(run.call_args.args[0], ["/synthetic/tmux", "-L", "agents-server-work", "list-sessions"])

    def test_update_runner_receives_exact_binding_on_mac_and_linux(self):
        for platform in ("darwin", "linux"):
            with patch.object(sys, "platform", platform), patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": "/synthetic/runtime", "XDG_RUNTIME_DIR": "/synthetic/user", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/synthetic/bus"}):
                result = self.namespace("work")["server_update_runner_environment"]()
            self.assertEqual(result["AGENTS_SERVER_INSTANCE"], "work")
            self.assertEqual(result["AGENTS_SERVER_INSTALL_DIR"], "/synthetic/runtime")
            self.assertEqual(result["AGENTS_SERVER_CONFIG_DIR"], "/synthetic/config")
            self.assertEqual(result["AGENTSDOCK_STATE_DIR"], "/synthetic/state")


if __name__ == "__main__":
    unittest.main()
