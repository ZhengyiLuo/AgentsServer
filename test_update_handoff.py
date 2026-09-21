from __future__ import annotations

from copy import deepcopy
import fcntl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import unittest
from unittest import mock
import threading
import uuid

import execution_install as files
from execution_maintenance import ExecutionMaintenance
from execution_manage import WorkerControl
import test_execution_install as support
import update_handoff as handoff


class Control:
    def __init__(self, layout, value):
        self.record = {"instance_id": value["worker_instance_id"], "pid": 120,
                       "release_root": str(layout.worker_release), "callback_origin": "http://127.0.0.1:12345"}
        self.value = {"worker_instance_id": value["worker_instance_id"], "idle": True,
                      "blockers": {"active_sessions": 0, "managed_update": 0},
                      "lease": {"operation_id": value["operation_id"], "lease_id": value["lease_id"],
                                "sealed": True, "expires_at": None}}
        self.health = {"ok": True, "server_identity": value["expected_server_identity"],
                       "execution_service": {"instance_id": value["worker_instance_id"], "pid": 120}}
        self.requests = []
        self.after_health = None
        self.failure = None
        self.response = None

    def status(self, layout):
        return deepcopy(self.record), deepcopy(self.value)

    def _agent_token(self, layout):
        return "fixture-private-token"

    def callback_health(self, layout, record):
        return self._json(record["callback_origin"] + "/api/health", self._agent_token(layout))

    def _json(self, url, token):
        if self.after_health:
            self.after_health()
        return deepcopy(self.health)

    def _maintenance(self, layout, record, **arguments):
        self.requests.append(deepcopy(arguments))
        if self.failure:
            raise self.failure
        self.value["lease"] = None
        return self.response if self.response is not None else deepcopy(self.value)


class Services:
    def __init__(self):
        self.value = {"worker": {"state": "running", "pid": 120},
                      "gateway": {"state": "running", "pid": 121}}

    def snapshot(self):
        return deepcopy(self.value)


class FailedHandoffTests(unittest.TestCase):
    def setUp(self):
        self.support = support.ExecutionInstallTests()
        self.support.setUp()
        self.addCleanup(self.support.doCleanups)
        self.support.migrate()
        self.root = self.support.root
        self.layout = self.support.layout
        parent = self.root / ".update-preparations" / ("a" * 32)
        parent.mkdir(mode=0o700, parents=True)
        parent.parent.chmod(0o700)
        self.path = parent / ("handoff-" + "b" * 32 + ".json")
        self.value = {"schema": 1, "operation_id": str(uuid.uuid4()),
                      "worker_instance_id": "worker_fixture_epoch", "lease_id": str(uuid.uuid4()),
                      "expected_server_identity": "server_fixture_identity"}
        files._atomic_write(self.path, files._json_bytes(self.value))
        self.control = Control(self.layout, self.value)
        self.services = Services()

    def release(self):
        return handoff.release_existing_handoff(self.root, self.path, control=self.control, services=self.services)

    def failed_status(self):
        self.value["operation_id"] = str(uuid.UUID(hex="b" * 32))
        self.control.value["lease"]["operation_id"] = self.value["operation_id"]
        files._atomic_write(self.path, files._json_bytes(self.value))
        status_path = self.layout.state_root / "admin/server-update.json"
        status_path.parent.mkdir(mode=0o700, exist_ok=True)
        value = {"phase": "failed", "update_id": "b" * 32, "preparation_id": "a" * 32,
                 "error_code": "server_update_handoff_release_failed", "retryable": True,
                 "runner_pid": None, "_execution_handoff": deepcopy(self.value)}
        files._atomic_write(status_path, files._json_bytes(value))
        return status_path, value

    def retry(self, path, status):
        return handoff.retry_failed_handoff(self.root, path, status,
                                            control=self.control, services=self.services)

    def test_explicit_failed_cleanup_retry_releases_exact_hold_without_changing_status(self):
        path, status = self.failed_status()
        before = path.read_bytes(), self.path.read_bytes()
        self.control.failure = RuntimeError("provider settlement failed")
        with self.assertRaisesRegex(RuntimeError, "settlement"):
            self.release()
        self.control.failure = None
        result = self.retry(path, status)
        self.assertFalse(result["already_released"])
        self.assertEqual(len(self.control.requests), 2)
        self.assertEqual((path.read_bytes(), self.path.read_bytes()), before)

    def test_lost_release_reply_retry_acknowledges_same_unheld_worker_without_another_post(self):
        path, status = self.failed_status()
        self.control.response = {"worker_instance_id": "lost-response", "lease": None}
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            self.release()
        self.assertIsNone(self.control.value["lease"])
        # Existing agents can have resumed after the successful but unobserved release.
        self.control.value.update(idle=False, blockers={"active_sessions": 2, "managed_update": 0})
        result = self.retry(path, status)
        self.assertTrue(result["already_released"])
        self.assertEqual(len(self.control.requests), 1)

    def test_retry_requires_exact_saved_failed_status_and_handoff(self):
        path, status = self.failed_status()
        mutations = ({"phase": "starting"}, {"error_code": "other"}, {"retryable": False},
                     {"runner_pid": 999},
                     {"update_id": "c" * 32}, {"_execution_handoff": {**self.value, "lease_id": str(uuid.uuid4())}})
        for changes in mutations:
            value = {**status, **changes}
            files._atomic_write(path, files._json_bytes(value))
            with self.subTest(changes=changes), self.assertRaises((ValueError, RuntimeError, FileNotFoundError)):
                self.retry(path, value)
        files._atomic_write(path, files._json_bytes(status))
        with self.assertRaisesRegex(RuntimeError, "status changed"):
            self.retry(path, {**status, "message": "stale snapshot"})
        self.assertEqual(self.control.requests, [])

    def test_retry_private_status_path_permissions_and_links_are_validated(self):
        path, status = self.failed_status()
        path.chmod(0o644)
        with self.assertRaises(PermissionError):
            self.retry(path, status)
        path.chmod(0o600)
        other = path.with_name("other.json")
        files._atomic_write(other, files._json_bytes(status))
        with self.assertRaisesRegex(ValueError, "outside"):
            self.retry(other, status)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises((ValueError, OSError)):
            self.retry(path, status)
        self.assertEqual(self.control.requests, [])

    def test_retry_rechecks_status_after_authenticated_callback(self):
        path, status = self.failed_status()
        self.control.after_health = lambda: files._atomic_write(path, files._json_bytes({**status, "update_id": "c" * 32}))
        with self.assertRaisesRegex(RuntimeError, "status changed"):
            self.retry(path, status)
        self.assertEqual(self.control.requests, [])

    def test_retry_never_releases_foreign_hold_or_acknowledges_restarted_worker(self):
        path, status = self.failed_status()
        self.control.value["lease"]["operation_id"] = str(uuid.uuid4())
        with self.assertRaisesRegex(RuntimeError, "exact sealed"):
            self.retry(path, status)
        self.control.value["lease"] = None
        self.control.value["worker_instance_id"] = "new-epoch"
        with self.assertRaisesRegex(RuntimeError, "native worker"):
            self.retry(path, status)
        self.assertEqual(self.control.requests, [])

    def replacement_epoch(self):
        path, status = self.failed_status()
        # A completed rollback can retain historical recovery evidence.
        status["_activation_recovery"] = {"historical": "kept unchanged"}
        files._atomic_write(path, files._json_bytes(status))
        self.control.record.update(instance_id="replacement_epoch", pid=220)
        self.control.value.update(worker_instance_id="replacement_epoch", lease=None,
                                  idle=False, blockers={"active_sessions": 1, "managed_update": 0})
        self.control.health["execution_service"].update(instance_id="replacement_epoch", pid=220)
        self.services.value["worker"]["pid"] = 220
        lock = self.layout.state_root / "admin/state-owner.lock"
        files._atomic_write(lock, b"")
        descriptor = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return path, status, descriptor

    def test_unheld_replacement_epoch_acknowledged_without_post_or_deleting_recovery_evidence(self):
        path, status, _descriptor = self.replacement_epoch()
        before = path.read_bytes(), self.path.read_bytes()
        result = self.retry(path, status)
        self.assertTrue(result["already_released"])
        self.assertEqual(result["worker_instance_id"], "replacement_epoch")
        self.assertEqual(self.control.requests, [])
        self.assertEqual((path.read_bytes(), self.path.read_bytes()), before)

    def test_replacement_epoch_with_even_original_hold_cannot_receive_release_post(self):
        path, status, _descriptor = self.replacement_epoch()
        self.control.value.update(idle=True, blockers={"active_sessions": 0, "managed_update": 0},
            lease={"operation_id": self.value["operation_id"], "lease_id": self.value["lease_id"],
                   "sealed": True, "expires_at": None})
        with self.assertRaisesRegex(RuntimeError, "native worker"):
            self.retry(path, status)
        self.assertEqual(self.control.requests, [])

    def test_replacement_epoch_requires_unchanged_exclusive_state_ownership(self):
        path, status, descriptor = self.replacement_epoch()
        self.control.after_health = lambda: fcntl.flock(descriptor, fcntl.LOCK_UN)
        with self.assertRaisesRegex(RuntimeError, "exclusive state ownership"):
            self.retry(path, status)
        self.control.after_health = None
        with self.assertRaisesRegex(RuntimeError, "exclusive state ownership"):
            self.retry(path, status)
        self.assertEqual(self.control.requests, [])

    def test_replacement_epoch_probes_must_not_change_process_or_accept_a_foreign_hold(self):
        path, status, _descriptor = self.replacement_epoch()
        self.control.after_health = lambda: self.control.value.update(lease={"operation_id": str(uuid.uuid4())})
        with self.assertRaisesRegex(RuntimeError, "native worker"):
            self.retry(path, status)
        self.assertEqual(self.control.requests, [])

    def test_uninstall_intent_including_dangling_link_owns_recovery(self):
        path, status = self.failed_status()
        marker = self.root / ".execution-uninstall.json"
        for linked in (False, True):
            marker.symlink_to(self.root / "missing") if linked else marker.write_text("{}")
            with self.assertRaisesRegex(RuntimeError, "uninstall"):
                self.release()
            with self.assertRaisesRegex(RuntimeError, "uninstall"):
                self.retry(path, status)
            marker.unlink()
        self.assertEqual(self.control.requests, [])

    def test_releases_only_the_exact_sealed_handoff_and_retains_evidence(self):
        before = self.path.read_bytes(), self.path.stat()
        result = self.release()
        self.assertTrue(result["released"])
        self.assertEqual(self.control.requests, [{"action": "release", "operation": self.value["operation_id"],
                                                 "lease_id": self.value["lease_id"]}])
        self.assertEqual((self.path.read_bytes(), self.path.stat()), before)
        self.assertFalse((self.root / ".install-lock").exists())
        self.assertIsNone(self.control.value["lease"])
        with self.assertRaisesRegex(RuntimeError, "exact sealed"):
            self.release()
        self.assertEqual(len(self.control.requests), 1)

    def test_every_activation_marker_including_dangling_links_blocks_release(self):
        for name in (".activation-transaction", ".execution-transaction"):
            for kind in ("file", "directory", "symlink"):
                with self.subTest(name=name, kind=kind):
                    path = self.root / name
                    if kind == "file":
                        path.write_text("owned by installer")
                    elif kind == "directory":
                        path.mkdir()
                    else:
                        path.symlink_to(self.root / "missing")
                    with self.assertRaisesRegex(RuntimeError, "activation already owns"):
                        self.release()
                    path.rmdir() if kind == "directory" else path.unlink()
        self.assertEqual(self.control.requests, [])

    def test_private_lease_and_native_process_identity_mismatches_do_not_release(self):
        original = deepcopy(self.control.value), deepcopy(self.control.record), deepcopy(self.services.value)
        mutations = (
            lambda: self.control.value.update(worker_instance_id="other"),
            lambda: self.control.record.update(instance_id="other"),
            lambda: self.control.record.update(release_root=str(self.support.new)),
            lambda: self.services.value["worker"].update(pid=999),
            lambda: self.services.value["worker"].update(state="stopped"),
            lambda: self.control.value["lease"].update(operation_id=str(uuid.uuid4())),
            lambda: self.control.value["lease"].update(lease_id=str(uuid.uuid4())),
            lambda: self.control.value["lease"].update(sealed=False),
            lambda: self.control.value["lease"].update(expires_at=12345),
        )
        for mutation in mutations:
            mutation()
            with self.assertRaises(RuntimeError):
                self.release()
            self.control.value, self.control.record, self.services.value = deepcopy(original)
        self.assertEqual(self.control.requests, [])

    def test_busy_or_unsettled_provider_cleanup_never_reopens_admission(self):
        for status in ({"idle": False}, {"blockers": {"managed_update": 1}},
                       {"blockers": {}}, {"blockers": {"active_sessions": False}}):
            original = deepcopy(self.control.value)
            self.control.value.update(status)
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, "not settled"):
                self.release()
            self.control.value = original
        self.assertEqual(self.control.requests, [])

    def test_health_must_authenticate_the_same_server_and_worker_epoch(self):
        for mutation in ({"server_identity": "other_server"}, {"ok": False},
                         {"execution_service": {"instance_id": "other", "pid": 120}}):
            original = deepcopy(self.control.health)
            self.control.health.update(mutation)
            with self.assertRaisesRegex(RuntimeError, "authenticated worker"):
                self.release()
            self.control.health = original
        self.assertEqual(self.control.requests, [])

    def test_takeover_after_health_is_rechecked_before_release(self):
        self.control.after_health = lambda: self.control.value["lease"].update(lease_id=str(uuid.uuid4()))
        with self.assertRaisesRegex(RuntimeError, "exact sealed"):
            self.release()
        self.assertEqual(self.control.requests, [])

    def test_installer_journal_appearing_after_health_takes_recovery_ownership(self):
        self.control.after_health = lambda: (self.root / ".activation-transaction").mkdir()
        with self.assertRaisesRegex(RuntimeError, "activation already owns"):
            self.release()
        self.assertEqual(self.control.requests, [])

    def test_handoff_changed_after_health_cannot_release_original_or_new_hold(self):
        def replace_handoff():
            value = {**self.value, "lease_id": str(uuid.uuid4())}
            files._atomic_write(self.path, files._json_bytes(value))
        self.control.after_health = replace_handoff
        with self.assertRaisesRegex(RuntimeError, "provenance changed"):
            self.release()
        self.assertEqual(self.control.requests, [])

    def test_release_failure_is_propagated_once_without_retry_or_deleting_evidence(self):
        self.control.failure = RuntimeError("fixture provider cleanup failed")
        with self.assertRaisesRegex(RuntimeError, "provider cleanup failed"):
            self.release()
        self.assertEqual(len(self.control.requests), 1)
        self.assertTrue(self.path.exists())
        self.assertIsNotNone(self.control.value["lease"])

    def test_ambiguous_release_response_never_claims_success(self):
        self.control.response = {"worker_instance_id": "other", "lease": None}
        with self.assertRaisesRegex(RuntimeError, "did not confirm"):
            self.release()
        self.assertEqual(len(self.control.requests), 1)

    def test_missing_legacy_layout_is_not_treated_as_a_split_worker_handoff(self):
        (self.root / files.LAYOUT_NAME).unlink()
        with self.assertRaises(FileNotFoundError):
            self.release()
        self.assertEqual(self.control.requests, [])

    def test_handoff_private_permissions_links_and_schema_are_required(self):
        for mode in (0o644, 0o666):
            self.path.chmod(mode)
            with self.assertRaises(PermissionError):
                self.release()
        self.path.chmod(0o600)
        alias = self.path.with_name("alias")
        os.link(self.path, alias)
        with self.assertRaises(PermissionError):
            self.release()
        alias.unlink()
        original = self.path.read_bytes()
        for mutation in (lambda value: value.update(schema=True), lambda value: value.pop("expected_server_identity"),
                         lambda value: value.update(operation_id="not-a-uuid")):
            value = deepcopy(self.value)
            mutation(value)
            self.path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                self.release()
        self.path.write_bytes(original)
        self.path.rename(alias)
        self.path.symlink_to(alias)
        with self.assertRaises(ValueError):
            self.release()
        self.assertEqual(self.control.requests, [])

    def test_live_installer_lock_prevents_inspection_and_release(self):
        with handoff.InstallationLock(self.root):
            with self.assertRaisesRegex(RuntimeError, "another installer owns"):
                self.release()
        self.assertEqual(self.control.requests, [])

    def test_real_authenticated_callback_releases_exact_durable_lease_once(self):
        status_path, failed_status = self.failed_status()
        runtime = self.layout.runtime_dir
        controller = ExecutionMaintenance(runtime / "maintenance.json", self.value["worker_instance_id"])
        controller._persist({"operation_id": self.value["operation_id"], "lease_id": self.value["lease_id"],
                             "sealed": True, "expires_at": None})
        control_token = (runtime / "control.token").read_text()
        agent_token = "fixture-agent-" + "a" * 40
        files._atomic_write(self.layout.config_root / "env", f"AGENTSDOCK_AGENT_TOKEN={agent_token}\n".encode())
        requests = []
        expected_health = deepcopy(self.control.health)
        expected_health["execution_service"]["pid"] = os.getpid()
        self.services.value["worker"]["pid"] = os.getpid()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *unused):
                pass

            def reply(self, status, value):
                body = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                expected = agent_token if self.path == "/api/health" else control_token
                if self.headers.get("Authorization") != "Bearer " + expected:
                    self.reply(401, {"error": "fixture authorization failed"})
                elif self.path == "/api/health":
                    self.reply(200, expected_health)
                elif self.path == "/api/admin/execution/status":
                    self.reply(200, controller.status({"active_sessions": 0, "managed_update": 0}))
                else:
                    self.reply(404, {})

            def do_POST(self):
                if self.headers.get("Authorization") != "Bearer " + control_token:
                    self.reply(401, {})
                    return
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(value)
                if value["expected_worker_instance_id"] != controller.worker_instance_id:
                    self.reply(409, {})
                    return
                result = controller.apply(value["action"], value["operation_id"], value["lease_id"], 120,
                                          {"active_sessions": 0, "managed_update": 0})
                self.reply(200, result)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            record = {**self.control.record, "role": "worker", "protocol": 1, "pid": os.getpid(),
                      "callback_origin": f"http://127.0.0.1:{server.server_port}"}
            files._atomic_write(runtime / "worker.json", files._json_bytes(record))
            import execution_http
            native_platform = "Darwin" if sys.platform == "darwin" else "Linux"
            real_request = execution_http.request_json
            request_patch = mock.patch("execution_manage.request_json", side_effect=lambda *a, **kw:
                real_request(*a, **{**kw, "platform": native_platform}))
            request_patch.start()
            self.addCleanup(request_patch.stop)
            result = handoff.release_existing_handoff(self.root, self.path, control=WorkerControl(services=self.services), services=self.services)
            self.assertTrue(result["released"])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0], {"action": "release", "expected_worker_instance_id": self.value["worker_instance_id"],
                                          "operation_id": self.value["operation_id"], "lease_id": self.value["lease_id"]})
            self.assertIsNone(json.loads((runtime / "maintenance.json").read_text())["lease"])
            with self.assertRaisesRegex(RuntimeError, "exact sealed"):
                handoff.release_existing_handoff(self.root, self.path, control=WorkerControl(services=self.services), services=self.services)
            acknowledged = handoff.retry_failed_handoff(self.root, status_path, failed_status,
                                                        control=WorkerControl(services=self.services), services=self.services)
            self.assertTrue(acknowledged["already_released"])
            self.assertEqual(len(requests), 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
