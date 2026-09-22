"""Host endpoint recovery with temporary stores and real loopback listeners.

Never imports agent_server or reads application/user state. Only the test's
loopback address bypasses the production endpoint validator; all other endpoint
validation and the missing-interface socket failures remain real.
"""

import ast
import errno
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentsdock_team_hub.secure_peer import (
    SecurePeerError,
    SecurePeerGateway,
    build_pairing_request,
    canonical_peer_ipv4,
)
from agentsdock_team_hub.store import HubStore
from secure_peer_runtime import SecurePeerRuntime


LOCAL_IP = "127.0.0.1"
STALE_IP = "192.0.2.254"
OTHER_MISSING_IP = "192.0.2.253"
ADDRESS_ERROR = "secure_peer_host_address_unavailable"


class SecurePeerHostEndpointRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-host-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

        def allow_test_loopback(value):
            return value if value == LOCAL_IP else canonical_peer_ipv4(value)

        for target in (
            "secure_peer_runtime.canonical_peer_ipv4",
            "agentsdock_team_hub.secure_peer.canonical_peer_ipv4",
        ):
            self.enterContext(mock.patch(target, side_effect=allow_test_loopback))
        self.runtime = SecurePeerRuntime(
            self.root / "secure-peers",
            server_identity="isolated-host-endpoint",
            server_instance_id="isolated-host-instance",
        )
        self.addCleanup(self.runtime.shutdown)
        self.hub = HubStore(self.root / "hub")
        self.port = self.free_port()

    @staticmethod
    def free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOCAL_IP, 0))
            return int(probe.getsockname()[1])

    def configure_persisted_host(self, host):
        config = {
            **self.runtime._config,
            "enabled": True,
            "advertised_host": host,
            "listen_port": self.port,
        }
        self.runtime._write_config(config)
        self.runtime._config = config

    def attach(self):
        self.runtime.attach_host_hub(
            hub_id=self.hub.hub_id,
            hub_data_dir=self.hub.data_dir,
            hub_store=self.hub,
        )

    def stale_host(self):
        self.configure_persisted_host(STALE_IP)
        with self.assertRaises(SecurePeerError) as raised:
            self.attach()
        self.assertEqual(raised.exception.code, ADDRESS_ERROR)
        self.assertEqual(raised.exception.__cause__.errno, errno.EADDRNOTAVAIL)
        self.assertIsNone(self.runtime._gateway)
        return raised.exception

    def assert_live(self):
        host = self.runtime.status()["host"]
        self.assertTrue(host["enabled"])
        self.assertTrue(host["available"])
        self.assertIsNone(host["error"])
        self.assertIsNone(host["error_code"])
        self.assertEqual(self.runtime._gateway.address, (LOCAL_IP, self.port))
        with socket.create_connection((LOCAL_IP, self.port), timeout=1):
            pass

    def assert_config_unchanged(self, expected):
        self.assertEqual(self.runtime._config, expected)
        self.assertEqual(json.loads(self.runtime.config_path.read_text()), expected)

    def test_explicit_recovery_preserves_ca_approved_peer_and_hub_identity(self):
        self.stale_host()
        status = self.runtime.status()["host"]
        self.assertEqual(status["error_code"], ADDRESS_ERROR)
        self.assertIn(STALE_IP, status["error"])
        self.assertIn(f"errno {errno.EADDRNOTAVAIL}", status["error"])
        self.assertIn("current local IPv4", status["action"])

        proof = (self.hub.data_dir / "bootstrap-owner.proof").read_text().strip()
        owner = self.hub.bootstrap(proof, "owner@example.com", "Owner", "Test")
        store = self.runtime._host_store
        pending = store.submit_pairing(build_pairing_request(
            Ed25519PrivateKey.generate(),
            server_identity="isolated-approved-peer",
            display_name="Approved peer",
            host_ca_fingerprint=store.ca_fingerprint,
            capabilities=["cert_renewal", "teamspace", "durable_pairing_approval"],
            requested_scopes=["teamspace.read"],
        ))
        incoming = store.list_pairings()[0]
        self.runtime.approve_pairing(
            pairing_id=pending["pairing_id"],
            team_id=owner["teams"][0]["id"],
            scopes=["teamspace.read"],
            approved_by="isolated-owner",
            expected_peer_server_identity=incoming["peer_server_identity"],
            expected_transcript_hash=incoming["transcript_hash"],
            idempotency_key=str(uuid.uuid4()),
        )
        old_fingerprint = store.ca_fingerprint
        old_peers = store.list_peers()
        old_pairings = store.list_pairings()
        self.assertEqual(old_pairings[0]["status"], "approved")

        # Real startup callers used to replace the bind error with this
        # generic status. The explicit request must reclassify its retry.
        self.runtime.mark_host_unavailable("Secure peer host could not be initialized")
        result = self.runtime.configure_host(
            enabled=True, advertised_host=LOCAL_IP, listen_port=self.port,
        )
        self.assert_live()
        self.assertTrue(self.runtime._peer_accepting)
        self.assertIsNone(self.runtime._pending_host_attachment)
        self.assertIs(self.runtime._hub_store, self.hub)
        self.assertEqual(result["host"]["advertised_host"], LOCAL_IP)
        self.assertEqual(self.runtime._host_store.ca_fingerprint, old_fingerprint)
        self.assertEqual(self.runtime._host_store.list_peers(), old_peers)
        self.assertEqual(self.runtime._host_store.list_pairings(), old_pairings)
        self.assertEqual(self.runtime._read_config()["advertised_host"], LOCAL_IP)

    def test_missing_candidate_keeps_stale_config_and_original_candidate_error(self):
        self.stale_host()
        old_config = dict(self.runtime._config)
        with self.assertRaises(SecurePeerError) as raised:
            self.runtime.configure_host(
                enabled=True, advertised_host=OTHER_MISSING_IP, listen_port=self.port,
            )
        self.assertEqual(raised.exception.code, ADDRESS_ERROR)
        self.assertIn(OTHER_MISSING_IP, str(raised.exception))
        self.assert_config_unchanged(old_config)
        self.assertIsNone(self.runtime._gateway)
        self.assertIsNotNone(self.runtime._pending_host_attachment)
        self.assertIn(STALE_IP, self.runtime.status()["host"]["error"])
        self.assertFalse(self.runtime._peer_accepting)

    def test_missing_candidate_restores_previously_live_listener(self):
        self.configure_persisted_host(LOCAL_IP)
        self.attach()
        old_config = dict(self.runtime._config)
        with self.assertRaises(SecurePeerError) as raised:
            self.runtime.configure_host(
                enabled=True, advertised_host=STALE_IP, listen_port=self.port,
            )
        self.assertEqual(raised.exception.code, ADDRESS_ERROR)
        self.assert_config_unchanged(old_config)
        self.assert_live()
        self.assertTrue(self.runtime._peer_accepting)

    def test_persistence_failure_restores_live_listener_and_old_config(self):
        self.configure_persisted_host(LOCAL_IP)
        self.attach()
        old_config = dict(self.runtime._config)
        next_port = self.free_port()
        error = OSError(errno.ENOSPC, "isolated persistence failure")
        with mock.patch.object(self.runtime, "_write_config", side_effect=error):
            with self.assertRaises(OSError) as raised:
                self.runtime.configure_host(
                    enabled=True, advertised_host=LOCAL_IP, listen_port=next_port,
                )
        self.assertIs(raised.exception, error)
        self.assert_config_unchanged(old_config)
        self.assert_live()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOCAL_IP, next_port))

    def test_persistence_failure_from_stale_host_does_not_mask_error_with_rollback_bind(self):
        self.stale_host()
        old_config = dict(self.runtime._config)
        error = OSError(errno.ENOSPC, "isolated persistence failure")
        with mock.patch.object(self.runtime, "_write_config", side_effect=error):
            with self.assertRaises(OSError) as raised:
                self.runtime.configure_host(
                    enabled=True, advertised_host=LOCAL_IP, listen_port=self.port,
                )
        self.assertIs(raised.exception, error)
        self.assert_config_unchanged(old_config)
        self.assertIsNone(self.runtime._gateway)
        self.assertIsNotNone(self.runtime._pending_host_attachment)
        self.assertEqual(self.runtime.status()["host"]["error_code"], ADDRESS_ERROR)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((LOCAL_IP, self.port))

    def test_failed_rollback_marks_host_unavailable_and_retains_retry(self):
        self.configure_persisted_host(LOCAL_IP)
        self.attach()
        old_config = dict(self.runtime._config)
        real_start = SecurePeerGateway.start
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as competing_listener:
            def start(gateway):
                if gateway.bind_ip == STALE_IP:
                    competing_listener.bind((LOCAL_IP, self.port))
                    competing_listener.listen()
                return real_start(gateway)

            with mock.patch.object(SecurePeerGateway, "start", autospec=True, side_effect=start):
                with self.assertRaises(SecurePeerError) as raised:
                    self.runtime.configure_host(
                        enabled=True, advertised_host=STALE_IP, listen_port=self.port,
                    )
            self.assertEqual(raised.exception.code, ADDRESS_ERROR)
            self.assertIn(STALE_IP, str(raised.exception))
            self.assert_config_unchanged(old_config)
            self.assertIsNone(self.runtime._gateway)
            self.assertFalse(self.runtime._peer_accepting)
            self.assertIsNotNone(self.runtime._pending_host_attachment)
            status = self.runtime.status()["host"]
            self.assertFalse(status["available"])
            self.assertEqual(status["error_code"], "secure_peer_host_listener_failed")
            self.assertIn(str(errno.EADDRINUSE), status["error"])
        self.assertTrue(self.runtime.retry_host_attachment())
        self.assert_live()

    def test_recovery_preserves_preexisting_maintenance_admission_closure(self):
        self.stale_host()
        self.runtime.close_host_admission()
        self.runtime.configure_host(
            enabled=True, advertised_host=LOCAL_IP, listen_port=self.port,
        )
        self.assertTrue(self.runtime._host_admission_closed)
        self.assertFalse(self.runtime._peer_accepting)
        self.assertIsNotNone(self.runtime._gateway)

    def test_projection_failure_cannot_be_bypassed_as_an_endpoint_error(self):
        self.stale_host()
        old_config = dict(self.runtime._config)
        with mock.patch.object(
            self.hub, "provision_local_agent_mail", side_effect=OSError("isolated projection failure"),
        ):
            with self.assertRaises(SecurePeerError) as raised:
                self.runtime.configure_host(
                    enabled=True, advertised_host=LOCAL_IP, listen_port=self.port,
                )
        self.assertEqual(raised.exception.code, "host_unavailable")
        self.assert_config_unchanged(old_config)
        self.assertIsNone(self.runtime._gateway)
        self.assertEqual(
            self.runtime.status()["host"]["error_code"],
            "secure_peer_host_initialization_failed",
        )

    def test_wildcard_address_is_rejected_before_recovery(self):
        self.stale_host()
        old_config = dict(self.runtime._config)
        with mock.patch.object(self.runtime, "retry_host_attachment") as retry:
            with self.assertRaises(ValueError):
                self.runtime.configure_host(
                    enabled=True, advertised_host="0.0.0.0", listen_port=self.port,
                )
        retry.assert_not_called()
        self.assert_config_unchanged(old_config)

    def check_hint_callback_parity(self, enabled):
        self.runtime._mail_hints.enabled = enabled
        self.configure_persisted_host(LOCAL_IP)
        callback_names = (
            "mail_hint_subscriber", "mail_hint_snapshot",
            "notification_hint_subscriber", "notification_hint_snapshot",
        )
        with mock.patch("secure_peer_runtime.SecurePeerGateway", wraps=SecurePeerGateway) as construct:
            self.attach()
            attachment_callbacks = {
                key: construct.call_args.kwargs[key]
                for key in callback_names if key in construct.call_args.kwargs
            }
            self.assertEqual(set(attachment_callbacks), set(callback_names) if enabled else set())
            self.port = self.free_port()
            self.runtime.configure_host(
                enabled=True, advertised_host=LOCAL_IP, listen_port=self.port,
            )
            with mock.patch.object(self.runtime, "_write_config", side_effect=OSError("isolated write failure")):
                with self.assertRaises(OSError):
                    self.runtime.configure_host(
                        enabled=True, advertised_host=LOCAL_IP, listen_port=self.free_port(),
                    )
            # Initial attachment, reconfiguration, failed candidate, rollback.
            self.assertEqual(construct.call_count, 4)
            for call in construct.call_args_list:
                self.assertEqual(
                    {key: call.kwargs[key] for key in callback_names if key in call.kwargs},
                    attachment_callbacks,
                )
        self.assert_live()

    def test_reconfiguration_and_rollback_preserve_enabled_hint_callbacks(self):
        self.check_hint_callback_parity(True)

    def test_reconfiguration_and_rollback_keep_hint_callbacks_disabled_by_default(self):
        self.check_hint_callback_parity(False)

    def run_managed_host_catch(self, method_name):
        # Compile only the two real caller methods; never import the managed
        # service entrypoint or let a test initialize its application routes.
        source = Path(__file__).with_name("team_hub_host.py")
        tree = ast.parse(source.read_text())
        host_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                          and node.name == "ManagedTeamHubHost")
        method = next(node for node in host_class.body if isinstance(node, ast.FunctionDef)
                      and node.name == method_name)
        module = ast.Module(body=[ast.ImportFrom(
            module="__future__", names=[ast.alias(name="annotations")], level=0,
        ), method], type_ignores=[])
        namespace = {"SecurePeerError": SecurePeerError}
        exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
        boundary = SimpleNamespace(
            secure_peer_manager=self.runtime, logger=mock.Mock(),
            data_dir=self.hub.data_dir, _store=self.hub,
            _guard=threading.RLock(), _fenced_finalize_in_progress=False,
        )
        if method_name == "_attach_host_store":
            namespace[method_name](boundary, self.hub)
        else:
            self.hub.maintenance_fenced_start = True
            with mock.patch.object(self.hub, "maintenance_fence", return_value=None):
                namespace[method_name](boundary)
            self.assertFalse(boundary._fenced_finalize_in_progress)
            self.assertTrue(self.hub.maintenance_fenced_start)

    def check_managed_host_missing_address_status(self, method_name):
        self.configure_persisted_host(STALE_IP)
        with mock.patch.object(self.runtime, "retry_host_attachment") as retry:
            self.run_managed_host_catch(method_name)
        retry.assert_not_called()
        status = self.runtime.status()["host"]
        self.assertEqual(status["error_code"], ADDRESS_ERROR)
        self.assertIn(f"{STALE_IP}:{self.port}", status["error"])
        self.assertIn(f"errno {errno.EADDRNOTAVAIL}", status["error"])
        self.assertIn("current local IPv4 address", status["action"])
        self.assertIsNone(self.runtime._gateway)
        self.assertIsNotNone(self.runtime._pending_host_attachment)

    def test_managed_host_attach_preserves_immediate_missing_address_diagnostics(self):
        self.check_managed_host_missing_address_status("_attach_host_store")

    def test_post_commit_attach_preserves_immediate_missing_address_diagnostics(self):
        self.check_managed_host_missing_address_status("_finalize_committed_fenced_start")

    def test_managed_host_catches_keep_generic_recovery_errors(self):
        for method_name in ("_attach_host_store", "_finalize_committed_fenced_start"):
            with self.subTest(method=method_name), mock.patch.object(
                self.runtime, "attach_host_hub", side_effect=OSError("isolated database failure"),
            ):
                self.run_managed_host_catch(method_name)
            status = self.runtime.status()["host"]
            self.assertEqual(status["error_code"], "secure_peer_host_recovery_failed")
            self.assertEqual(status["error"], "The secure peer host could not finish recovery.")
            self.assertEqual(status["action"], "Retry after the Team Hub database is available.")


if __name__ == "__main__":
    unittest.main()
