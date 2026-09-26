"""Signed pending preparation and activation; no service/provider mutation."""
import argparse
import shlex
from types import SimpleNamespace
import base64
import hashlib
import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import time
import unittest
from unittest.mock import patch, AsyncMock, Mock
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import update_preparation as preparation
import update_runner
from tests import test_execution_preparation as receipt_tests
from tests import test_server_update_ensure_isolated as ensure_tests


class PendingPreparationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = receipt_tests.PreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.identifier = "a" * 32
        self.directory = preparation.preparation_directory(self.root, self.identifier, create=True)
        self.fixture.receipt = preparation.receipt_path(self.root, self.identifier)
        installer = self.fixture.candidate / "install.sh"
        installer.write_text("#!/bin/sh\nexit 0\n")
        installer.chmod(0o755)
        inventory = json.loads(self.fixture.inventory.read_text())
        inventory["files"]["install.sh"] = {"size": installer.stat().st_size,
            "sha256": hashlib.sha256(installer.read_bytes()).hexdigest()}
        self.fixture.inventory.write_text(json.dumps(inventory))
        self.private = Ed25519PrivateKey.generate()
        self.key = self.root / "key.pem"
        self.key.write_bytes(self.private.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.manifest = {
            "schema": 2, "distribution": "npm", "version": "1.2.3-beta.4", "track": "beta",
            "prerelease": True, "api_contract_version": 28, "commit": "b" * 40,
            "npm": {"name": "@agentsdock/server", "version": "1.2.3-beta.4",
                    "integrity": "sha512-" + base64.b64encode(hashlib.sha512(b"archive").digest()).decode()},
            "archive": {"name": "server-1.2.3-beta.4.tgz",
                "url": "https://registry.npmjs.org/@agentsdock/server/-/server-1.2.3-beta.4.tgz",
                "sha256": "a" * 64, "size": 7}}
        document = json.dumps(self.manifest).encode()
        self.envelope = {"manifest_base64": base64.b64encode(document).decode(),
            "signature_base64": base64.b64encode(self.private.sign(document)).decode()}
        self.status_path = self.root / "status.json"
        update_runner.atomic_json(self.status_path, {"phase": "pending", "preparation_id": self.identifier,
            "schedule_id": "c" * 32, "target_version": "1.2.3-beta.4", "track": "beta",
            "_npm_release": self.envelope})

    def run_preparation(self):
        preparation.run_preparation(status_path=self.status_path, root=self.root,
                                    public_key=self.key, preparation_id=self.identifier)

    def ready(self):
        self.fixture.write()
        with patch.object(update_runner, "download_npm_archive") as download:
            self.run_preparation()
        download.assert_not_called()
        return json.loads(self.status_path.read_text())

    def test_completed_candidate_resumes_after_runner_crash_without_download_or_install(self):
        status = self.ready()
        self.assertEqual(status["phase"], "pending")
        self.assertEqual(status["preparation_phase"], "ready")
        self.assertEqual(status["schedule_id"], "c" * 32)
        self.assertIsNone(status["preparation_runner_pid"])
        prepared = preparation.verify_prepared_status(status, root=self.root, public_key=self.key)
        self.assertEqual(prepared["candidate"], str(self.fixture.candidate))
        self.assertFalse((self.root / ".activation-transaction").exists())
        self.assertFalse((self.root / "execution-layout.json").exists())

    def test_receipt_tampering_and_changed_intent_are_rejected(self):
        status = self.ready()
        with self.assertRaisesRegex(ValueError, "intent"):
            preparation.verify_prepared_status({**status, "preparation_id": "b" * 32}, root=self.root, public_key=self.key)
        self.fixture.receipt.write_text(self.fixture.receipt.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "changed"):
            preparation.verify_prepared_status(status, root=self.root, public_key=self.key)

    def test_canceled_intent_cannot_publish_ready(self):
        update_runner.atomic_json(self.status_path, {"phase": "canceled", "preparation_id": self.identifier})
        before = self.status_path.read_bytes()
        with self.assertRaises(preparation.PreparationSuperseded):
            self.run_preparation()
        self.assertEqual(self.status_path.read_bytes(), before)

    def test_download_stages_once_and_keeps_pending_admission_open(self):
        buffer = io.BytesIO()
        entries = {"package/package.json": json.dumps({"name": "@agentsdock/server", "version": "1.2.3-beta.4"}).encode(),
            "package/server/install.sh": b"#!/bin/sh\n", "package/server/VERSION": b"1.2.3-beta.4\n",
            "package/server/execution_preparation.py": b"# preparation-capable signed release\n"}
        with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
            for name, data in entries.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))
        content = buffer.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        self.manifest["archive"].update(sha256=digest, size=len(content))
        self.manifest["npm"]["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()
        document = json.dumps(self.manifest).encode()
        self.envelope = {"manifest_base64": base64.b64encode(document).decode(),
            "signature_base64": base64.b64encode(self.private.sign(document)).decode()}
        status = json.loads(self.status_path.read_text())
        status["_npm_release"] = self.envelope
        update_runner.atomic_json(self.status_path, status)
        self.fixture.pins["archive_sha256"] = digest

        def stage(command, **arguments):
            pending = json.loads(self.status_path.read_text())
            self.assertEqual((pending["phase"], pending["preparation_phase"]), ("pending", "staging"))
            self.assertIn("--prepare-only", command)
            self.assertNotIn("--activate-prepared", command)
            self.assertEqual(command[command.index("--prepared-receipt") + 1], str(self.fixture.receipt))
            self.assertEqual(command[command.index("--prepared-archive-sha256") + 1], digest)
            self.fixture.write()

        with patch.object(update_runner, "download_npm_archive", return_value=content) as download, \
                patch.object(preparation, "run_preparer", side_effect=stage) as stage_process:
            self.run_preparation()
            self.run_preparation()
        download.assert_called_once()
        stage_process.assert_called_once()
        self.assertEqual(json.loads(self.status_path.read_text())["phase"], "pending")

    def test_named_update_retains_legacy_activation_without_staging_default_gateway(self):
        source = self.root / "named-source"
        source.mkdir()
        (source / "install.sh").write_text("#!/bin/bash\n# --instance)\n")
        (source / "server_instances.py").write_text("INSTANCE_PROTOCOL = 1\n")
        (source / "execution_preparation.py").write_text("# split-capable release\n")
        content = b"synthetic"
        self.manifest["archive"].update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
        self.manifest["npm"]["integrity"] = "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode()
        document = json.dumps(self.manifest).encode()
        status = json.loads(self.status_path.read_text())
        status["_npm_release"] = {
            "manifest_base64": base64.b64encode(document).decode(),
            "signature_base64": base64.b64encode(self.private.sign(document)).decode(),
        }
        update_runner.atomic_json(self.status_path, status)
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTANCE": "work"}), \
                patch.object(update_runner, "download_npm_archive", return_value=b"synthetic") as download, \
                patch.object(update_runner, "safe_extract", return_value=source), \
                patch.object(preparation, "run_preparer") as preparer:
            self.run_preparation()
        download.assert_called_once()
        preparer.assert_not_called()
        status = json.loads(self.status_path.read_text())
        self.assertEqual(status["_prepared_update"]["mode"], "legacy")
        self.assertEqual(status["phase"], "pending")
        self.assertFalse((self.root / "execution-layout.json").exists())
        self.assertFalse(self.fixture.receipt.exists())

    def test_process_lease_prevents_duplicate_preparation(self):
        program = "from pathlib import Path; import sys; from update_preparation import preparation_lease; " \
            "ctx=preparation_lease(Path(sys.argv[1])); ctx.__enter__(); print('ready',flush=True); sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-c", program, str(self.directory)],
            cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertTrue(preparation.preparation_is_active(self.root, self.identifier))
            with self.assertRaises(BlockingIOError):
                self.run_preparation()
        finally:
            child.communicate(timeout=5)
        self.assertFalse(preparation.preparation_is_active(self.root, self.identifier))

    def test_actual_staging_child_keeps_lease_after_observer_is_killed(self):
        child_pid = self.root / "staging.pid"
        stop = self.root / "finish-staging"
        child_code = ("from pathlib import Path; import os,time; "
            f"Path({str(child_pid)!r}).write_text(str(os.getpid()))\n"
            f"while not Path({str(stop)!r}).exists(): time.sleep(0.02)\n")
        observer_code = """from pathlib import Path
import sys
from update_preparation import preparation_lease,run_preparer
directory,status,identifier,child = sys.argv[1:]
with preparation_lease(Path(directory)) as descriptor:
    run_preparer([sys.executable,'-c',child], source=Path(directory),directory=Path(directory),
        status_path=Path(status),preparation_id=identifier,lease_descriptor=descriptor)
"""
        observer = subprocess.Popen([sys.executable, "-c", observer_code, str(self.directory),
            str(self.status_path), self.identifier, child_code], cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not child_pid.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(child_pid.exists())
            observer.kill()
            observer.communicate(timeout=5)
            self.assertTrue(preparation.preparation_is_active(self.root, self.identifier))
            stop.touch()
            deadline = time.monotonic() + 5
            while preparation.preparation_is_active(self.root, self.identifier) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(preparation.preparation_is_active(self.root, self.identifier))
        finally:
            stop.touch()
            if observer.poll() is None:
                observer.kill()
                observer.communicate(timeout=5)

    def test_receipt_digest_handles_large_inventory_and_rejects_links(self):
        path = self.root / "large.json"
        content = b" " * (2 * 1024 * 1024)
        path.write_bytes(content)
        path.chmod(0o600)
        self.assertEqual(preparation.receipt_digest(path), hashlib.sha256(content).hexdigest())
        link = self.root / "linked.json"
        link.symlink_to(path)
        with self.assertRaises(OSError):
            preparation.receipt_digest(link)

    def test_durable_intent_syncs_before_and_after_rename_and_cleans_failed_temp(self):
        events = []
        real_replace, real_fsync = os.replace, os.fsync

        def sync(descriptor):
            events.append("sync")
            return real_fsync(descriptor)

        def replace(source, target):
            events.append("replace")
            return real_replace(source, target)

        with patch.object(os, "fsync", side_effect=sync), patch.object(os, "replace", side_effect=replace):
            update_runner.atomic_json(self.status_path, {"phase": "pending"})
        self.assertEqual(events, ["sync", "replace", "sync"])
        before = self.status_path.read_bytes()
        with patch.object(os, "replace", side_effect=OSError("fixture disk failure")), self.assertRaises(OSError):
            update_runner.atomic_json(self.status_path, {"phase": "starting"})
        self.assertEqual(self.status_path.read_bytes(), before)
        self.assertFalse(list(self.status_path.parent.glob(".status.json.*.tmp")))

    def test_activation_reuses_prepared_bytes_without_network(self):
        status = self.ready()
        status.update(phase="starting", update_id="d" * 32)
        update_runner.atomic_json(self.status_path, status)
        args = argparse.Namespace(status_file=str(self.status_path), public_key=str(self.key), port=7850,
            bind="127.0.0.1", expected_version="1.2.3-beta.4", current_version="1.2.3-beta.3",
            track="beta", npm_descriptor=True, expected_server_identity="server-test", update_id="d" * 32,
            prepared_receipt=str(self.fixture.receipt),
            prepared_receipt_sha256=status["_prepared_update"]["receipt_sha256"])
        with patch.dict(os.environ, {"AGENTS_SERVER_INSTALL_DIR": str(self.root)}), \
                patch.object(update_runner, "download_npm_archive") as download, \
                patch.object(update_runner, "check_release") as check, \
                patch.object(update_runner, "wait_for_server_idle") as idle, \
                patch.object(update_runner, "run_installer") as installer, \
                patch.object(update_runner, "assert_post_update_identity"):
            update_runner.run_update(args)
        download.assert_not_called()
        check.assert_not_called()
        idle.assert_called_once()
        command = installer.call_args.args[0]
        self.assertEqual(command[0], str(self.fixture.candidate / "install.sh"))
        self.assertEqual(command[command.index("--activate-prepared") + 1], str(self.fixture.receipt))
        self.assertEqual(command[command.index("--prepared-archive-sha256") + 1], "a" * 64)
        self.assertEqual(command[command.index("--execution-mode") + 1], "split")
        self.assertNotIn("--prepare-only", command)
        self.assertEqual(json.loads(self.status_path.read_text())["phase"], "complete")

    def test_older_gateway_prevents_false_completion(self):
        health = {"server_identity": "server-test", "server_version": "1.2.3-beta.4",
            "execution_service": {"version": "1.2.3-beta.4"}, "gateway": {"version": "1.2.3-beta.3"}}
        with patch.object(update_runner, "server_health_snapshot", return_value=health), \
                self.assertRaisesRegex(RuntimeError, "converged"):
            update_runner.assert_post_update_identity(7850, token=None,
                expected_server_identity="server-test", expected_server_version="1.2.3-beta.4")


class PreparationAdmissionTests(ensure_tests.ServerUpdateEnsureTests):
    async def test_idle_handoff_is_sealed_under_admission_locks_before_starting(self):
        from execution_maintenance import ExecutionMaintenance
        maintenance = ExecutionMaintenance(self.root / "maintenance.json", "worker-epoch")
        pending = {**self.status, "phase": "pending", "schedule_id": "c" * 32,
                   "preparation_id": "d" * 32, "preparation_phase": "ready", "target_version": "1.0.4-beta.12"}
        runtime = SimpleNamespace(capability=Mock(return_value={}), prepare_maintenance=AsyncMock(return_value=None),
            reopen_admission_sync=Mock(), reopen_admission=AsyncMock(), clear_maintenance=AsyncMock())
        self.ns.update(os=SimpleNamespace(environ={"AGENTS_SERVER_INSTALL_DIR": str(self.root.resolve())}),
            SERVER_ROOT=Path(__file__).resolve().parents[1], prepare_scheduled_server_update=AsyncMock(return_value=(pending, {})),
            EXECUTION_MAINTENANCE=maintenance, DELETING_SESSIONS=set(), CODEX_GOALS_RECONFIGURING=False,
            BUSY_SESSIONS=set(), server_update_active_session_ids_locked=lambda: [],
            server_update_tmux_name=lambda identifier: "fixture-"+identifier,
            sys=sys, shlex=shlex, Path=Path, SERVER_PORT=7850, SERVER_BIND_ADDRESS="127.0.0.1", AGENT_TOKEN="",
            server_identity=lambda: "server-current", TEAM_HUB_RUNTIME=runtime,
            atomic_update_json=update_runner.atomic_json, quiesce_managed_update_service_cgroup=AsyncMock(),
            server_update_runner_environment=lambda: {}, tmux_bin=lambda: "tmux", run_tmux=Mock())
        ordinary_write = self.write.side_effect

        def observe_start(**changes):
            if changes.get("phase") == "starting":
                self.assertTrue(maintenance.is_held())
                self.assertTrue(maintenance.lease["sealed"])
                for lock in ("ACTIVE_LOCK", "QUEUE_LOCK", "UNSAFE_HTTP_MUTATION_ADMISSION_LOCK"):
                    self.assertTrue(self.ns[lock].locked())
                self.assertEqual(changes["_execution_handoff"]["lease_id"], maintenance.lease["lease_id"])
            return ordinary_write(**changes)

        self.write.side_effect = observe_start
        result = await self.ns["ensure_server_update"](self.request())
        self.assertEqual(result["phase"], "starting")
        self.assertEqual(result["preparation_id"], "d" * 32)
        handoff = next((self.root / ".update-preparations" / ("d" * 32)).glob("handoff-*.json"))
        self.assertEqual(json.loads(handoff.read_text())["worker_instance_id"], "worker-epoch")
        self.ns["quiesce_managed_update_service_cgroup"].assert_awaited_once()
        self.ns["run_tmux"].assert_called_once()

    async def test_preparation_does_not_take_provider_or_turn_admission(self):
        async def pending(status, **unused):
            return {"phase": "pending", "preparation_phase": "staging"}, None
        self.ns.update(os=type("Environment", (), {"environ": {"AGENTS_SERVER_INSTALL_DIR": "/fixture"}}),
            SERVER_ROOT=Path(__file__).resolve().parents[1], prepare_scheduled_server_update=pending)
        result = await self.ns["ensure_server_update"](self.request())
        self.assertEqual(result["phase"], "pending")
        self.assertEqual(result["preparation_phase"], "staging")
        self.ns["prepare_provider_background_work_snapshot"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
