"""Automatic Join storage tests: isolated local state and an in-memory transport.

No server module, listener, real socket, subprocess, or user state is needed.
"""

from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from cryptography.hazmat.primitives import serialization

from agentsdock_team_hub.secure_peer import (
    PAIRING_TOKEN_HEADER,
    PAIRING_TTL_SECONDS,
    SecurePeerClient,
    SecurePeerError,
    SecurePeerStore,
)
from agentsdock_team_hub.security import canonical_json


class SecurePeerAutoJoinTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="auto-join-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = int(time.time())
        self.host_ip = "192.0.2.10"
        self.store = SecurePeerStore(
            self.root / "host", "isolated-host-001", "isolated-hub-001", clock=lambda: self.now
        )
        self.store.configure_listener_identity(self.host_ip)
        self.leaf = self.store._server_certificate.public_bytes(serialization.Encoding.DER)
        self.post_hook = None
        self.poll_hook = None
        self.transport = self.enterContext(
            mock.patch.object(SecurePeerClient, "_request", side_effect=self.exchange)
        )
        # Existing certificate/health tests cover transport authentication. These
        # tests retain real pairing signatures/certificates and isolate activation
        # health so its return can be paused at exact storage transitions.
        self.health = self.enterContext(
            mock.patch.object(SecurePeerClient, "_peer_health_locked", return_value={})
        )
        self.client = self.restart()

    def restart(self):
        return SecurePeerClient(
            self.root / "client", "isolated-guest-001", "Isolated guest", clock=lambda: self.now
        )

    def exchange(self, host, port, method, path, **kwargs):
        self.assertEqual((host, port), (self.host_ip, 7851))
        if (method, path) == ("GET", "/v1/health"):
            value = {
                "protocol_version": 1,
                "host_server_identity": self.store.host_server_identity,
                "hub_id": self.store.hub_id,
                "host_ca_fingerprint": self.store.ca_fingerprint,
            }
        elif (method, path) == ("POST", "/v1/pairings"):
            value = self.store.submit_pairing(kwargs["body"])
            if self.post_hook is not None:
                self.post_hook(value)
        elif method == "GET" and path.startswith("/v1/pairings/"):
            value = self.store.poll_pairing(
                path.rsplit("/", 1)[1], kwargs["headers"][PAIRING_TOKEN_HEADER]
            )
            if self.poll_hook is not None:
                self.poll_hook(value)
        elif method == "POST" and path.endswith("/cancel"):
            value = self.store.cancel_pairing(
                path.split("/")[-2],
                kwargs["headers"][PAIRING_TOKEN_HEADER],
                kwargs["body"]["idempotency_key"],
            )
        else:
            self.fail(f"Unexpected mocked request: {method} {path}")
        return 200, [("Content-Type", "application/json")], canonical_json(value), self.leaf

    def begin(self, **kwargs):
        return self.client.begin_pairing(
            self.host_ip,
            expected_ca_fingerprint=self.store.ca_fingerprint,
            requested_scopes=["teamspace.read"],
            **kwargs,
        )

    def approve(self, pairing):
        incoming = next(
            row for row in self.store.list_pairings() if row["pairing_id"] == pairing["pairing_id"]
        )
        self.store.approve_pairing(
            incoming["pairing_id"], "isolated-team-001", ["teamspace.read"], "isolated-owner-001",
            expected_peer_server_identity=incoming["peer_server_identity"],
            expected_transcript_hash=incoming["transcript_hash"],
            idempotency_key=str(uuid.uuid4()),
        )

    def approved(self, *, complete_on_approval=True):
        pending = self.begin(complete_on_approval=complete_on_approval)
        self.approve(pending)
        return self.client.poll_pairing(pending["connection_id"])

    def activate(self, row):
        return self.client.activate_auto_connection(
            row["connection_id"], expected_pairing_id=row["pairing_id"],
            expected_transcript_hash=row["transcript_hash"],
            expected_host_server_identity=row["host_server_identity"], expected_hub_id=row["hub_id"],
        )

    def test_legacy_and_replayed_manual_requests_never_acquire_consent(self):
        request_id = str(uuid.uuid4())
        pending = self.begin(request_id=request_id)
        replay = self.begin(request_id=request_id, complete_on_approval=True)
        self.assertEqual(replay["connection_id"], pending["connection_id"])
        self.assertFalse(replay["complete_on_approval"])
        self.assertIsNone(self.client.auto_completion_info(pending["connection_id"]))
        self.assertEqual(self.client.list_auto_completion_candidates(), [])
        self.approve(pending)
        approved = self.client.poll_pairing(pending["connection_id"])
        with self.assertRaises(SecurePeerError):
            self.activate(approved)
        self.health.assert_not_called()

    def test_intent_precedes_post_and_recovery_preserves_request_and_deadline(self):
        original_time = self.now
        captured = {}

        def lose_response(_response):
            database = self.client._connect()
            try:
                row = database.execute(
                    """SELECT a.*,i.expires_at FROM client_pairing_attempts a
                    JOIN client_join_intents i ON i.connection_id=a.connection_id"""
                ).fetchone()
                captured.update(dict(row))
            finally:
                database.close()
            self.post_hook = None
            raise SecurePeerError("transport_failed", "Simulated response loss", 502)

        self.post_hook = lose_response
        with self.assertRaises(SecurePeerError):
            self.begin(complete_on_approval=True)
        self.assertEqual(captured["expires_at"], original_time + PAIRING_TTL_SECONDS)
        self.now += 30
        self.client = self.restart()
        self.assertEqual(self.client.recover_pairing_attempts()["recovered"], [captured["connection_id"]])
        pending = self.client.get_connection(captured["connection_id"])
        self.assertTrue(pending["complete_on_approval"])
        self.assertTrue(self.client.list_connections()[0]["complete_on_approval"])
        self.assertEqual(self.client.auto_completion_info(pending["connection_id"]), {
            "state": "pending", "deadline": original_time + PAIRING_TTL_SECONDS,
        })
        self.assertEqual(self.client._connection_row(pending["connection_id"])["pairing_request_id"], captured["request_id"])
        self.assertEqual(len(self.client.list_auto_completion_candidates()), 1)

    def test_already_approved_response_recovery_does_not_renew_expired_consent(self):
        def approve_and_lose(response):
            self.approve(response)
            self.post_hook = None
            raise SecurePeerError("transport_failed", "Simulated response loss", 502)

        self.post_hook = approve_and_lose
        with self.assertRaises(SecurePeerError):
            self.begin(complete_on_approval=True)
        self.now += PAIRING_TTL_SECONDS
        self.client = self.restart()
        recovered = self.client.recover_pairing_attempts()["recovered"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(self.client.auto_completion_state(recovered[0]), "expired")
        self.assertEqual(self.client.get_connection(recovered[0])["status"], "expired")
        self.assertEqual(self.client.list_auto_completion_candidates(), [])

    def test_approval_crossing_deadline_expires_at_commit_without_other_worker(self):
        pending = self.begin(complete_on_approval=True)
        self.approve(pending)
        self.poll_hook = lambda _value: setattr(self, "now", pending["pairing_expires_at"])
        result = self.client.poll_pairing(pending["connection_id"])
        self.assertEqual(result["status"], "expired")
        self.assertFalse(result["complete_on_approval"])
        self.assertEqual(self.client.auto_completion_state(pending["connection_id"]), "expired")
        self.assertEqual(list(self.client.keys_dir.iterdir()), [])

    def test_activation_health_crossing_deadline_cannot_consume_intent(self):
        approved = self.approved()
        self.health.side_effect = lambda *_: setattr(self, "now", approved["pairing_expires_at"])
        with self.assertRaises(SecurePeerError):
            self.activate(approved)
        self.assertEqual(self.client.auto_completion_state(approved["connection_id"]), "expired")
        self.assertFalse(self.client.get_connection(approved["connection_id"])["active"])

    def test_activation_is_atomic_idempotent_and_restart_projection_is_adoptable(self):
        approved = self.approved()
        active = self.activate(approved)
        self.assertTrue(active["active"])
        self.assertTrue(active["complete_on_approval"])
        self.assertEqual(self.client.auto_completion_state(active["connection_id"]), "completed")
        self.client = self.restart()
        self.assertTrue(self.client.list_connections()[0]["complete_on_approval"])
        self.assertEqual(self.activate(approved)["status"], "connected")
        self.health.assert_called_once()
        with self.assertRaises(SecurePeerError):
            self.client.cancel_pairing(active["connection_id"], str(uuid.uuid4()))
        self.assertTrue(self.client.get_connection(active["connection_id"])["active"])
        self.client.deactivate_connection(
            active["connection_id"], expected_host_server_identity=active["host_server_identity"],
            expected_hub_id=active["hub_id"],
        )
        self.assertFalse(self.client.get_connection(active["connection_id"])["complete_on_approval"])
        with self.assertRaises(SecurePeerError):
            self.activate(approved)

    def test_completion_receipt_is_atomic_with_concurrent_activation(self):
        approved = self.approved()
        competitor = self.restart()
        writer_commit_attempted = threading.Event()
        writer_finished = threading.Event()
        errors = []
        original_connect = competitor._connect
        original_projection = self.client._public_connection

        def traced_connection():
            database = original_connect()
            database.set_trace_callback(
                lambda sql: writer_commit_attempted.set() if sql == "COMMIT" else None
            )
            return database

        def activate():
            try:
                competitor.activate_auto_connection(
                    approved["connection_id"], expected_pairing_id=approved["pairing_id"],
                    expected_transcript_hash=approved["transcript_hash"],
                    expected_host_server_identity=approved["host_server_identity"],
                    expected_hub_id=approved["hub_id"],
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                writer_finished.set()

        worker = threading.Thread(target=activate)

        def pause_projection(row, active):
            worker.start()
            self.assertTrue(writer_commit_attempted.wait(5))
            self.assertFalse(writer_finished.is_set())
            return original_projection(row, active)

        with mock.patch.object(competitor, "_connect", side_effect=traced_connection):
            try:
                with mock.patch.object(self.client, "_public_connection", side_effect=pause_projection):
                    before = self.client.auto_completion_snapshot(approved["connection_id"])
            finally:
                if worker.ident is not None:
                    worker.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(worker.is_alive())
        self.assertEqual(before["state"], "pending")
        self.assertFalse(before["connection"]["active"])
        after = self.client.auto_completion_snapshot(approved["connection_id"])
        self.assertEqual(after["state"], "completed")
        self.assertTrue(after["connection"]["active"])
        self.assertEqual(before["deadline"], after["deadline"])

    def test_completion_receipt_projects_expiry_without_writing(self):
        pending = self.begin(complete_on_approval=True)
        self.now = pending["pairing_expires_at"]
        database = self.client._connect()
        database.set_authorizer(
            lambda action, *_: sqlite3.SQLITE_DENY
            if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
            else sqlite3.SQLITE_OK
        )
        with mock.patch.object(self.client, "_connect", return_value=database):
            snapshot = self.client.auto_completion_snapshot(pending["connection_id"])
        self.assertEqual(snapshot["state"], "expired")
        self.assertFalse(snapshot["connection"]["complete_on_approval"])
        self.assertEqual(self.client._connection_row(pending["connection_id"])["status"], "pending")

    def test_cancel_during_activation_health_is_terminal_and_retryable(self):
        approved = self.approved()
        self.health.side_effect = lambda *_: self.client.cancel_pairing(
            approved["connection_id"], str(uuid.uuid4())
        )
        with self.assertRaises(SecurePeerError):
            self.activate(approved)
        self.assertEqual(self.client.auto_completion_state(approved["connection_id"]), "cancelled")
        self.assertEqual(self.client.get_connection(approved["connection_id"])["status"], "cancelled")
        self.assertEqual(list(self.client.keys_dir.iterdir()), [])
        self.assertEqual(self.client.cancel_pairing(approved["connection_id"], str(uuid.uuid4()))["status"], "cancelled")

    def test_cancel_fences_consent_before_waiting_for_stalled_poll(self):
        pending = self.begin(complete_on_approval=True)
        self.approve(pending)
        poll_entered = threading.Event()
        release_poll = threading.Event()
        intent_cancelled = threading.Event()
        errors = []
        results = []
        original_invalidate = self.client._invalidate_auto_completion

        def stalled_poll(_response):
            poll_entered.set()
            if not release_poll.wait(5):
                raise TimeoutError("test did not release poll")

        def invalidated(database, connection_id=None, **kwargs):
            original_invalidate(database, connection_id, **kwargs)
            if connection_id == pending["connection_id"]:
                intent_cancelled.set()

        def run(operation):
            try:
                results.append(operation())
            except BaseException as exc:
                errors.append(exc)

        self.poll_hook = stalled_poll
        with mock.patch.object(self.client, "_invalidate_auto_completion", side_effect=invalidated):
            polling = threading.Thread(target=run, args=(lambda: self.client.poll_pairing(pending["connection_id"]),))
            cancellation = threading.Thread(target=run, args=(lambda: self.client.cancel_pairing(pending["connection_id"], str(uuid.uuid4())),))
            polling.start()
            try:
                self.assertTrue(poll_entered.wait(5))
                cancellation.start()
                self.assertTrue(intent_cancelled.wait(5))
                self.assertEqual(self.client.auto_completion_state(pending["connection_id"]), "cancelled")
            finally:
                release_poll.set()
                polling.join(5)
                if cancellation.ident is not None:
                    cancellation.join(5)
        self.assertEqual(errors, [])
        self.assertFalse(polling.is_alive())
        self.assertFalse(cancellation.is_alive())
        self.assertEqual(self.client.get_connection(pending["connection_id"])["status"], "cancelled")
        self.assertEqual(self.client.list_auto_completion_candidates(), [])

    def test_host_pause_without_active_member_and_new_join_supersede_consent(self):
        first = self.begin(complete_on_approval=True)
        second = self.begin(complete_on_approval=True)
        self.assertEqual(self.client.auto_completion_state(first["connection_id"]), "cancelled")
        self.assertEqual([row["connection_id"] for row in self.client.list_auto_completion_candidates()], [second["connection_id"]])
        self.assertIsNone(self.client.pause_active_connection_for_host())
        self.assertEqual(self.client.auto_completion_state(second["connection_id"]), "cancelled")
        self.client = self.restart()
        self.assertEqual(self.client.list_auto_completion_candidates(), [])

    def test_manual_binding_change_and_aba_invalidate_pending_join(self):
        previous = self.approved(complete_on_approval=False)
        requested = self.approved()

        def change_binding(*_):
            with mock.patch.object(self.client, "_peer_health_locked", return_value={}):
                self.client.set_active_connection(previous["connection_id"], expected_current=None)
            self.client.deactivate_connection(
                previous["connection_id"], expected_host_server_identity=previous["host_server_identity"],
                expected_hub_id=previous["hub_id"],
            )

        self.health.side_effect = change_binding
        with self.assertRaises(SecurePeerError):
            self.activate(requested)
        self.assertEqual(self.client.auto_completion_state(requested["connection_id"]), "cancelled")
        self.assertFalse(any(row["active"] for row in self.client.list_connections()))

    def test_active_binding_rejects_new_automatic_intent_without_post(self):
        previous = self.approved(complete_on_approval=False)
        self.client.set_active_connection(previous["connection_id"], expected_current=None)
        self.transport.reset_mock()
        with self.assertRaises(SecurePeerError) as rejected:
            self.begin(complete_on_approval=True)
        self.assertEqual(rejected.exception.code, "active_connection_changed")
        self.assertTrue(all(call.args[2] == "GET" for call in self.transport.call_args_list))
        self.assertTrue(self.client.get_connection(previous["connection_id"])["active"])

    def test_exact_identity_mismatch_rejects_activation_before_health(self):
        approved = self.approved()
        altered = {**approved, "transcript_hash": "0" * 64}
        with self.assertRaises(SecurePeerError):
            self.activate(altered)
        self.health.assert_not_called()
        self.assertEqual(self.client.auto_completion_state(approved["connection_id"]), "pending")

    def test_revocation_and_forgetting_keep_terminal_intent_unusable(self):
        for operation in ("revoke", "forget", "deactivate"):
            with self.subTest(operation=operation):
                approved = self.approved()
                expected = {
                    "expected_host_server_identity": approved["host_server_identity"],
                    "expected_hub_id": approved["hub_id"],
                }
                if operation == "deactivate":
                    self.client.deactivate_connection(approved["connection_id"], **expected)
                else:
                    expected["expected_certificate_fingerprint"] = approved["certificate_fingerprint"]
                    method = self.client.retire_remote_revoked_connection if operation == "revoke" else self.client.forget_connection
                    method(approved["connection_id"], **expected)
                self.assertEqual(self.client.auto_completion_state(approved["connection_id"]), "cancelled")
                with self.assertRaises(SecurePeerError):
                    self.activate(approved)


if __name__ == "__main__":
    unittest.main()
