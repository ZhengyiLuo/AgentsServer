"""Exact interrupted activation recovery; no installed native jobs are touched."""
from __future__ import annotations

import argparse
import ast
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid

from fastapi import HTTPException
import activation_transaction as activation
import test_execution_activation_transaction as fixtures
import update_recovery as recovery
import update_runner as updates


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ExecutionActivationTransactionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.item = self.fixture.layout(split=True)
        self.update_id = "a" * 32
        self.status = {"phase": "installing", "update_id": self.update_id,
            "target_version": self.item.release_version, "track": "stable"}
        self.status["_activation_recovery"] = recovery.activation_intent(
            root=self.item.root, candidate=self.item.candidate_source,
            version=self.item.release_version, api_contract=28, update_id=self.update_id,
            server_identity="server-fixture")
        self.transaction = self.fixture.begin(self.item, extra=("--execution-api-contract", "28"))
        self.status_path = self.item.state / "server-update.json"
        updates.atomic_json(self.status_path, self.status)

    def context(self):
        return recovery.recovery_context(self.item.root, self.status, server_identity="server-fixture")

    def args(self):
        return argparse.Namespace(status_file=str(self.status_path), update_id=self.update_id,
            recovery_transaction=self.transaction, expected_version=self.item.release_version,
            expected_server_identity="server-fixture", port=17850, bind="127.0.0.1")

    def test_exact_candidate_is_bound_before_and_after_publication(self):
        self.assertEqual(self.context()["transaction_id"], self.transaction)
        self.fixture.driver.activate_to_linked(self.item, self.transaction)
        self.assertEqual(self.context()["candidate_release"]["inode"],
                         self.item.release_dir.stat().st_ino)

    def test_identity_and_version_changes_cannot_resume_another_transaction(self):
        for key, value in (("server_identity", "another-server"), ("update_id", "b" * 32),
                           ("version", "99.0.0"), ("api_contract", 29), ("format", True)):
            with self.subTest(key=key), patch.dict(self.status["_activation_recovery"], {key: value}):
                with self.assertRaises(RuntimeError):
                    self.context()
        with patch.dict(self.status["_activation_recovery"]["candidate_binding"], {"inode": 1}):
            with self.assertRaisesRegex(RuntimeError, "candidate identity"):
                self.context()

    def test_reader_does_not_clean_an_installers_live_manifest_temporary(self):
        temporary = self.item.root / ".activation-transaction" / ".manifest-active.tmp"
        temporary.write_text("owned writer still publishing")
        temporary.chmod(0o600)
        self.context()
        self.assertTrue(temporary.exists())

    def test_recovery_never_downloads_or_reinstalls_and_requires_post_commit_health(self):
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(updates, "run_installer") as installer, \
                patch.object(updates, "download_npm_archive") as download, \
                patch.object(updates, "assert_post_update_identity") as health, \
                patch.object(recovery, "journal_present", return_value=False):
            recovery.run_recovery(self.args())
        command = installer.call_args.args[0]
        self.assertIn("--recover-only", command)
        self.assertEqual(command[command.index("--expected-activation-id") + 1], self.transaction)
        self.assertNotIn("--activate-prepared", command)
        download.assert_not_called()
        health.assert_called_once()
        self.assertEqual(json.loads(self.status_path.read_text())["phase"], "complete")

    def test_replaced_journal_or_update_refuses_before_installer(self):
        for field, value in (("recovery_transaction", "activation-" + "b" * 24),
                             ("update_id", "b" * 32)):
            args = self.args()
            setattr(args, field, value)
            with self.subTest(field=field), \
                    patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}), \
                    patch.object(updates, "run_installer") as installer:
                with self.assertRaises(RuntimeError):
                    recovery.run_recovery(args)
                installer.assert_not_called()

    def test_verified_rollback_is_failed_retryable_not_false_success(self):
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(updates, "run_installer", side_effect=updates.InstallerRolledBack()), \
                patch.object(recovery, "journal_present", return_value=False), \
                patch.object(updates, "assert_post_update_identity") as health:
            recovery.run_recovery(self.args())
        health.assert_not_called()
        result = json.loads(self.status_path.read_text())
        self.assertEqual(result["phase"], "failed")
        self.assertEqual(result["error_code"], "server_update_rolled_back")
        self.assertTrue(result["retryable"])

    def test_installer_zero_with_retained_journal_never_reports_complete(self):
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(updates, "run_installer"), \
                self.assertRaisesRegex(RuntimeError, "without finalizing"):
            recovery.run_recovery(self.args())
        self.assertEqual(json.loads(self.status_path.read_text())["phase"], "installing")

    def test_real_child_exit75_only_means_rollback_in_recovery_mode(self):
        script = self.item.base / "rollback-fixture.sh"
        script.write_text("#!/bin/sh\nexit 75\n")
        script.chmod(0o700)
        kwargs = dict(cwd=script.parent, status_path=self.status_path,
            log_path=self.item.base / "installer.log", version=self.item.release_version,
            expected_update_id=self.update_id)
        with self.assertRaises(updates.InstallerRolledBack):
            updates.run_installer([str(script)], accepted_returncodes=(0, 75), **kwargs)
        with self.assertRaisesRegex(RuntimeError, "installer failed"):
            updates.run_installer([str(script)], **kwargs)

    def test_interrupted_installer_keeps_legacy_admission_drained_for_recovery(self):
        argv = ["update_runner.py", "--status-file", str(self.status_path),
            "--public-key", str(self.item.base / "unused-key"), "--port", "17850", "--bind", "127.0.0.1",
            "--expected-version", self.item.release_version, "--expected-server-identity", "server-fixture",
            "--update-id", self.update_id]
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(sys, "argv", argv), \
                patch.object(updates, "team_hub_maintenance_fence_present", return_value=True), \
                patch.object(updates, "run_update", side_effect=RuntimeError("interrupted fixture")):
            self.assertEqual(updates.main(), 1)
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "installing")
        self.assertEqual(status["error_code"], "server_update_recovery_pending")
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}), \
                patch.object(sys, "argv", argv + ["--recover-only", "--recovery-transaction", self.transaction]), \
                patch.object(recovery, "run_recovery", side_effect=RuntimeError("failed fixture recovery")):
            self.assertEqual(updates.main(), 1)
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["phase"], "installing")
        self.assertEqual(status["error_code"], "server_update_recovery_failed")
        self.assertTrue(status["retryable"])
        self.assertTrue((self.item.root / ".activation-transaction").is_dir())

    def test_installer_preflight_failure_without_journal_recovers_its_old_handoff(self):
        self.status["_execution_handoff"] = {"owned": True}
        argv = ["update_runner.py", "--status-file", str(self.status_path),
            "--public-key", str(self.item.base / "unused-key"), "--port", "17850", "--bind", "127.0.0.1",
            "--expected-version", self.item.release_version, "--expected-server-identity", "server-fixture",
            "--update-id", self.update_id, "--execution-handoff-file", str(self.item.base / "handoff.json")]
        for error in (None, RuntimeError("fixture callback unreachable")):
            updates.atomic_json(self.status_path, self.status)
            with self.subTest(error=bool(error)), \
                    patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}), \
                    patch.object(sys, "argv", argv), patch.object(recovery, "journal_present", return_value=False), \
                    patch.object(updates, "run_update", side_effect=RuntimeError("preflight refused")), \
                    patch("update_handoff.release_existing_handoff", side_effect=error) as release:
                self.assertEqual(updates.main(), 1)
            release.assert_called_once()
            value = json.loads(self.status_path.read_text())
            self.assertEqual(value["phase"], "failed")
            self.assertEqual(value.get("error_code"), "server_update_handoff_release_failed" if error else None)
            self.assertTrue(value["retryable"])

    def test_abandoned_preinstaller_runner_retains_a_visible_handoff_retry(self):
        from types import SimpleNamespace
        row = {"phase": "checking", "update_id": self.update_id,
               "target_version": "9.0.0", "_execution_handoff": {"owned": True}}
        source = Path(__file__).with_name("agent_server.py")
        tree = ast.parse(source.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "finalize_abandoned_server_update"]
        namespace = {"Any": Any, "SERVER_UPDATE_STATUS_FILE": self.status_path,
            "Path": Path, "os": SimpleNamespace(environ={"AGENTS_SERVER_INSTALL_DIR": str(self.item.root)}),
            "server_identity": lambda: "server-fixture",
            "server_update_status_lock": lambda path: nullcontext(),
            "read_server_update_status": lambda: row,
            "SERVER_UPDATE_ACTIVE_PHASES": updates.RUNNER_OWNED_ACTIVE_PHASES,
            "_reconcile_server_update_team_hub_fence": Mock(),
            "_clear_exact_server_update_team_hub_fence": Mock(),
            "_verify_server_update_team_hub_identity": Mock(),
            "TEAM_HUB_RUNTIME": SimpleNamespace(maintenance_fence_sync=lambda: None),
            "SERVER_VERSION": "8.0.0", "update_utc_now": updates.utc_now,
            "_write_server_update_status_unlocked": lambda **changes: {**row, **changes}}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), namespace)
        value = namespace["finalize_abandoned_server_update"](row)
        self.assertEqual(value["error_code"], "server_update_handoff_release_failed")
        self.assertTrue(value["retryable"])
        # Same-version repair is not complete merely because the original
        # sealed worker already reports that version. Only paired native
        # health with admission released may settle a lost completion reply.
        row["target_version"] = namespace["SERVER_VERSION"]
        for proof_result in (False, RuntimeError("native ownership unavailable"), True):
            for worker_present in (False, True):
                with self.subTest(proof=proof_result, worker_present=worker_present):
                    namespace["EXECUTION_MAINTENANCE"] = (
                        SimpleNamespace(worker_instance_id="worker-fixture") if worker_present else None)
                    options = ({"side_effect": proof_result} if isinstance(proof_result, Exception)
                               else {"return_value": proof_result})
                    with patch("execution_update_status.current_components", **options):
                        value = namespace["finalize_abandoned_server_update"](row)
                    completed = worker_present and proof_result is True
                    self.assertEqual(value["phase"], "complete" if completed else "failed")
                    if not completed:
                        self.assertEqual(value["error_code"], "server_update_handoff_release_failed")
                        self.assertTrue(value["retryable"])


class RecoveryLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = RecoveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.status = dict(fixture.status)
        self.launch = Mock()
        def write(**changes):
            self.status.update(changes)
            return dict(self.status)
        def atomic(path, value):
            updates.atomic_json(path, value)
        self.ns = {
            "Any": Any, "Path": Path, "os": os, "asyncio": asyncio, "sys": sys,
            "uuid": uuid, "shlex": shlex, "HTTPException": HTTPException,
            "SERVER_UPDATE_STATUS_FILE": fixture.status_path,
            "SERVER_UPDATE_PUBLIC_KEY": fixture.item.base / "unused-public.pem",
            "SERVER_UPDATE_RUNNER": Path(updates.__file__), "SERVER_PORT": 17850,
            "SERVER_BIND_ADDRESS": "127.0.0.1", "AGENT_TOKEN": "owned-fixture-token",
            "SERVER_UPDATE_ACTIVE_PHASES": updates.RUNNER_OWNED_ACTIVE_PHASES,
            "SERVER_UPDATE_START_GRACE_SECONDS": 45,
            "server_identity": lambda: "server-fixture", "server_update_is_active": lambda row: False,
            "server_update_status_age_seconds": lambda row: 100,
            "ensure_managed_update_tmux_isolated": lambda: None,
            "server_update_runner_environment": lambda: {},
            "server_update_status_lock": lambda path: nullcontext(),
            "read_server_update_status": lambda: dict(self.status),
            "atomic_update_json": atomic, "_write_server_update_status_unlocked": write,
            "update_utc_now": lambda: updates.utc_now(), "tmux_bin": lambda: "/usr/bin/tmux",
            "run_tmux": self.launch,
        }
        source = Path(__file__).with_name("agent_server.py")
        tree = ast.parse(source.read_text())
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name in {"resume_server_update_activation", "prepare_scheduled_server_update"}]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), str(source), "exec"), self.ns)
        self.patch = patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(fixture.item.root)})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def run_resume(self, **kwargs):
        return await self.ns["resume_server_update_activation"](dict(self.status), **kwargs)

    async def test_stale_runner_resumes_same_operation_with_private_auth(self):
        result = await self.run_resume()
        self.assertEqual(result["update_id"], self.fixture.update_id)
        self.assertEqual(result["phase"], "restarting")
        command = self.launch.call_args.args[0]
        self.assertIn("--recover-only", command[-1])
        self.assertIn(self.fixture.transaction, command[-1])
        self.assertNotIn("owned-fixture-token", command[-1])
        token_files = list(self.fixture.status_path.parent.glob(".server-recovery-*.auth.json"))
        self.assertEqual(len(token_files), 1)
        self.assertEqual(token_files[0].stat().st_mode & 0o777, 0o600)

    async def test_live_runner_or_start_grace_is_never_duplicated(self):
        for active, age in ((True, 100), (False, 0)):
            self.ns["server_update_is_active"] = lambda row: active
            self.ns["server_update_status_age_seconds"] = lambda row: age
            await self.run_resume()
        self.launch.assert_not_called()

    async def test_native_recovery_owner_is_joined_without_another_runner(self):
        with patch("execution_recovery.resume_owner", return_value={"transaction_id": self.fixture.transaction}) as resume:
            result = await self.run_resume(explicit=True)
        resume.assert_called_once_with(self.fixture.item.root, self.fixture.transaction)
        self.assertEqual(result, self.status)
        self.launch.assert_not_called()
        self.assertEqual(list(self.fixture.status_path.parent.glob(".server-recovery-*.auth.json")), [])

    async def test_changed_native_owner_refuses_legacy_recovery_fallback(self):
        with patch("execution_recovery.resume_owner", side_effect=RuntimeError("native registration changed")):
            with self.assertRaisesRegex(RuntimeError, "native registration changed"):
                await self.run_resume(explicit=True)
        self.launch.assert_not_called()

    async def test_failure_stays_visible_until_explicit_retry(self):
        self.status["phase"] = "failed"
        await self.run_resume()
        self.launch.assert_not_called()
        await self.run_resume(explicit=True)
        self.launch.assert_called_once()

    async def test_launch_failure_keeps_journal_hold_and_removes_unused_auth(self):
        self.launch.side_effect = OSError("fixture launch failure")
        with self.assertRaises(HTTPException):
            await self.run_resume()
        self.assertEqual(self.status["error_code"], "server_update_recovery_launch_failed")
        self.assertEqual(self.status["phase"], "installing", "older monoliths still require the active drain")
        self.assertTrue((self.fixture.item.root / ".activation-transaction").is_dir())
        self.assertEqual(list(self.fixture.status_path.parent.glob(".server-recovery-*.auth.json")), [])

    async def test_explicit_recovery_retry_is_not_delayed_by_failed_launch_grace(self):
        self.status.update(phase="installing", error_code="server_update_recovery_failed")
        self.ns["server_update_status_age_seconds"] = lambda row: 0
        await self.run_resume()
        self.launch.assert_not_called()
        await self.run_resume(explicit=True)
        self.launch.assert_called_once()

    async def test_foreign_maintenance_cannot_start_preparation(self):
        from execution_maintenance import ExecutionMaintenance
        maintenance = ExecutionMaintenance(self.fixture.item.state / "maintenance.json", "owned-worker")
        maintenance.hold_for_startup(str(uuid.uuid4()))
        self.ns["EXECUTION_MAINTENANCE"] = maintenance
        with self.assertRaises(HTTPException) as error:
            await self.ns["prepare_scheduled_server_update"](dict(self.status),
                requested=self.fixture.item.release_version, track="stable", npm_release=None)
        self.assertEqual(error.exception.status_code, 409)
        self.launch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
