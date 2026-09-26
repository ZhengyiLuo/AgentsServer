"""Verified endpoint moves with temporary state and private socket-pair TLS only.

No monolithic server import, TCP listener, DNS, or production state is used.
"""

from contextlib import closing
import http.client
import ipaddress
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from agentsdock_team_hub.secure_peer import (
    PAIRING_TOKEN_HEADER, SecurePeerClient, SecurePeerError, SecurePeerStore,
)
from agentsdock_team_hub.security import canonical_json


OLD_HOST = "192.0.2.10"
NEW_HOST = "192.0.2.20"
OLD_PORT = 7851
NEW_PORT = 7852


class SecurePeerEndpointMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="peer-endpoint-migration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = int(time.time())
        self.store = SecurePeerStore(
            self.root / "host", "migration-host-001", "migration-hub-001",
            clock=lambda: self.now,
        )
        self.store.configure_listener_identity(OLD_HOST)
        self.client = self.new_client()
        with mock.patch.object(self.client, "_request", side_effect=self.pairing_exchange):
            pending = self.client.begin_pairing(
                OLD_HOST, port=OLD_PORT, expected_ca_fingerprint=self.store.ca_fingerprint,
                requested_scopes=["teamspace.read"],
            )
            self.connection_id = pending["connection_id"]
            self.store.approve_pairing(
                pending["pairing_id"], "migration-team-001", ["teamspace.read"],
                "migration-owner-001", expected_peer_server_identity=self.client.server_identity,
                expected_transcript_hash=pending["transcript_hash"],
                idempotency_key=str(uuid.uuid4()),
            )
            self.client.poll_pairing(self.connection_id)
            self.client.set_active_connection(self.connection_id, expected_current=None)
        self.original = dict(self.client._connection_row(self.connection_id))
        self.expected = {
            "expected_host_server_identity": self.store.host_server_identity,
            "expected_hub_id": self.store.hub_id,
            "expected_host_ip": OLD_HOST,
            "expected_port": OLD_PORT,
        }
        with closing(self.client._connect()) as database:
            database.execute(
                """INSERT INTO client_routes(route_id,connection_id,revision,alias,
                display_title,actions_json,chat_id,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), self.connection_id, "revision-001", "route-alias",
                 "Retained route", '["request_reply"]', "chat-001", "active", self.now, self.now),
            )

    def new_client(self):
        return SecurePeerClient(
            self.root / "client", "migration-peer-001", "Migration peer",
            clock=lambda: self.now, timeout_seconds=2,
        )

    def health(self, *, store=None, peer=None):
        store = self.store if store is None else store
        row = self.client._connection_row(self.connection_id)
        return {
            "ok": True,
            "peer_id": row["peer_id"] if peer is None else peer.peer_id,
            "team_id": row["team_id"] if peer is None else peer.team_id,
            "host_server_identity": store.host_server_identity,
            "hub_id": store.hub_id,
            "host_ca_fingerprint": store.ca_fingerprint,
            "certificate_fingerprint": row["certificate_fingerprint"] if peer is None else peer.certificate_fingerprint,
            "certificate_expires_at": row["certificate_expires_at"] if peer is None else peer.certificate_expires_at,
            "remote_route_delivery_available": False,
            "mail_hints_available": True,
            "mail_hints_v2_available": True,
        }

    def response(self, value, *, store=None):
        store = self.store if store is None else store
        return (200, [("Content-Type", "application/json")], canonical_json(value),
                store._server_certificate.public_bytes(serialization.Encoding.DER))

    def pairing_exchange(self, host, port, method, path, **kwargs):
        self.assertEqual((host, port), (OLD_HOST, OLD_PORT))
        if (method, path) == ("GET", "/v1/health"):
            value = self.store.public_health()
        elif (method, path) == ("POST", "/v1/pairings"):
            value = self.store.submit_pairing(kwargs["body"])
        elif method == "GET" and path.startswith("/v1/pairings/"):
            value = self.store.poll_pairing(
                path.rsplit("/", 1)[1], kwargs["headers"][PAIRING_TOKEN_HEADER]
            )
        elif (method, path) == ("GET", "/v1/peer/health"):
            value = self.health()
        else:
            self.fail(f"Unexpected fixture request: {method} {path}")
        return self.response(value)

    def migrate(self, **kwargs):
        arguments = dict(self.expected)
        arguments.update(kwargs)
        return self.client.update_connection_endpoint(
            self.connection_id, NEW_HOST, NEW_PORT, **arguments
        )

    def tls_migrate(self, *, store=None, certificate_host=NEW_HOST):
        store = self.store if store is None else store
        context = store.tls_server_context(certificate_host)
        context.verify_mode = ssl.CERT_REQUIRED
        server_raw, client_raw = socket.socketpair()
        server_raw.settimeout(2)
        client_raw.settimeout(2)
        errors = []
        requests = []

        def serve():
            try:
                with context.wrap_socket(server_raw, server_side=True) as secured:
                    peer = store.authenticate_peer(secured.getpeercert(binary_form=True))
                    request = b""
                    while b"\r\n\r\n" not in request:
                        chunk = secured.recv(4096)
                        if not chunk:
                            raise RuntimeError("Client closed before sending health request")
                        request += chunk
                    requests.append(request)
                    body = canonical_json(self.health(store=store, peer=peer))
                    secured.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                        + body
                    )
            except BaseException as error:
                errors.append(error)

        def connect(connection):
            self.assertEqual((connection.host, connection.port), (NEW_HOST, NEW_PORT))
            self.assertTrue(connection._context.check_hostname)
            self.assertEqual(connection._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertEqual(connection._context.minimum_version, ssl.TLSVersion.TLSv1_3)
            connection.sock = connection._context.wrap_socket(
                client_raw, server_hostname=connection.host
            )

        worker = threading.Thread(target=serve)
        worker.start()
        try:
            with mock.patch.object(http.client.HTTPSConnection, "connect", connect):
                result = self.migrate()
            self.assertEqual(errors, [])
            self.assertEqual(len(requests), 1)
            self.assertTrue(requests[0].startswith(b"GET /v1/peer/health HTTP/1.1\r\n"))
            return result
        finally:
            client_raw.close()
            server_raw.close()
            worker.join(3)
            self.assertFalse(worker.is_alive(), "TLS fixture did not terminate")

    def assert_original(self):
        self.assertEqual(dict(self.client._connection_row(self.connection_id)), self.original)

    def test_pinned_mtls_move_preserves_authority_routes_and_key_material(self):
        with closing(self.client._connect()) as database:
            routes_before = [dict(row) for row in database.execute("SELECT * FROM client_routes")]
        keys_before = {path.name: path.read_bytes() for path in self.client.keys_dir.iterdir()}
        self.now += 10
        result = self.tls_migrate()
        self.assertEqual((result["host_ip"], result["port"]), (NEW_HOST, NEW_PORT))
        self.assertEqual(result["status"], "connected")
        self.assertTrue(result["active"])
        self.assertEqual(result["last_validated_at"], self.now)
        self.assertNotIn("endpoint_generation", result)
        current = dict(self.client._connection_row(self.connection_id))
        mutable = {"host_ip", "port", "endpoint_generation", "last_validated_at", "updated_at", "relay_available"}
        self.assertEqual(
            {key: value for key, value in current.items() if key not in mutable},
            {key: value for key, value in self.original.items() if key not in mutable},
        )
        self.assertEqual(current["endpoint_generation"], 1)
        self.assertTrue(self.client.mail_hint_capability(self.connection_id))
        self.assertTrue(self.client.notification_hint_capability(self.connection_id))
        with closing(self.client._connect()) as database:
            self.assertEqual([dict(row) for row in database.execute("SELECT * FROM client_routes")], routes_before)
        self.assertEqual({path.name: path.read_bytes() for path in self.client.keys_dir.iterdir()}, keys_before)
        self.assertEqual(self.new_client().get_connection(self.connection_id), result)

    def test_legacy_database_migration_is_idempotent_and_private(self):
        before = self.client.get_connection(self.connection_id)
        with closing(self.client._connect()) as database:
            database.execute("ALTER TABLE client_connections DROP COLUMN endpoint_generation")
        for _ in range(2):
            restarted = self.new_client()
            self.assertEqual(restarted.get_connection(self.connection_id), before)
            self.assertEqual(restarted._connection_row(self.connection_id)["endpoint_generation"], 0)
            self.assertNotIn("endpoint_generation", restarted.list_connections()[0])

    def test_different_ca_fails_tls_without_saving_candidate(self):
        other = SecurePeerStore(self.root / "other-host", self.store.host_server_identity, self.store.hub_id)
        with self.assertRaises(SecurePeerError) as raised:
            self.tls_migrate(store=other)
        self.assertEqual(raised.exception.code, "transport_failed")
        self.assert_original()

    def test_old_certificate_ip_fails_tls_without_saving_candidate(self):
        with self.assertRaises(SecurePeerError) as raised:
            self.tls_migrate(certificate_host=OLD_HOST)
        self.assertEqual(raised.exception.code, "transport_failed")
        self.assert_original()

    def test_signed_leaf_for_another_host_identity_is_rejected(self):
        self.store.configure_listener_identity(NEW_HOST)
        leaf = self.store._server_certificate
        builder = (x509.CertificateBuilder().subject_name(leaf.subject).issuer_name(leaf.issuer)
                   .public_key(leaf.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(leaf.not_valid_before_utc).not_valid_after(leaf.not_valid_after_utc))
        for extension in leaf.extensions:
            value = extension.value
            if isinstance(value, x509.SubjectAlternativeName):
                value = x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address(NEW_HOST)),
                    x509.UniformResourceIdentifier("urn:agentsdock:server:other-host-001"),
                ])
            builder = builder.add_extension(value, extension.critical)
        key = serialization.load_pem_private_key(self.store.ca_key_path.read_bytes(), password=None)
        wrong_leaf = builder.sign(key, algorithm=None).public_bytes(serialization.Encoding.DER)
        response = self.response(self.health())
        with mock.patch.object(self.client, "_request", return_value=(*response[:3], wrong_leaf)):
            with self.assertRaises(SecurePeerError) as raised:
                self.migrate()
        self.assertEqual(raised.exception.code, "host_identity_mismatch")
        self.assert_original()

    def test_health_identity_and_capability_mismatches_never_change_endpoint(self):
        self.store.configure_listener_identity(NEW_HOST)
        for field, wrong in (
            ("host_server_identity", "other-host-001"), ("hub_id", "other-hub-001"),
            ("peer_id", str(uuid.uuid4())), ("team_id", "other-team-001"),
            ("host_ca_fingerprint", "sha256:" + "0" * 64),
            ("certificate_fingerprint", "sha256:" + "0" * 64),
            ("certificate_expires_at", self.now + 1), ("remote_route_delivery_available", 1),
            ("mail_hints_available", "true"), ("mail_hints_v2_available", 1),
        ):
            with self.subTest(field=field):
                health = self.health()
                health[field] = wrong
                with mock.patch.object(self.client, "_request", return_value=self.response(health)):
                    with self.assertRaises(SecurePeerError) as raised:
                        self.migrate()
                self.assertEqual(raised.exception.code, "host_identity_mismatch")
                self.assert_original()

    def test_invalid_expected_endpoint_or_identity_fails_before_network(self):
        with mock.patch.object(self.client, "_request") as request:
            for wrong in (
                {"expected_host_ip": "192.0.2.99"}, {"expected_port": 7853},
                {"expected_host_server_identity": "other-host-001"}, {"expected_hub_id": "other-hub-001"},
            ):
                with self.subTest(wrong=wrong), self.assertRaises(SecurePeerError) as raised:
                    self.migrate(**wrong)
                self.assertEqual(raised.exception.code, "connection_changed")
            for wrong in ({"expected_host_ip": None}, {"expected_port": None}):
                with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                    self.migrate(**wrong)
            request.assert_not_called()
        self.assert_original()

    def test_transport_failure_keeps_endpoint_and_health_receipts(self):
        receipt = self.client._mail_hint_health
        with mock.patch.object(self.client, "_request", side_effect=SecurePeerError("transport_failed", "Unavailable", 502)):
            with self.assertRaises(SecurePeerError):
                self.migrate()
        self.assert_original()
        self.assertEqual(self.client._mail_hint_health, receipt)

    def test_expiring_and_revoked_credentials_cannot_be_migrated(self):
        with mock.patch.object(self.client, "_request") as request:
            self.now = self.original["certificate_expires_at"] - 60
            with self.assertRaises(SecurePeerError) as raised:
                self.migrate()
            self.assertEqual(raised.exception.code, "connection_unavailable")
            self.now = self.original["created_at"]
            with closing(self.client._connect()) as database:
                database.execute("UPDATE client_connections SET status='revoked'")
            with self.assertRaises(SecurePeerError) as raised:
                self.migrate()
            self.assertEqual(raised.exception.code, "pairing_incomplete")
            request.assert_not_called()

    def test_deactivated_connection_stays_deactivated(self):
        self.client.deactivate_connection(self.connection_id,
            expected_host_server_identity=self.store.host_server_identity, expected_hub_id=self.store.hub_id)
        result = self.tls_migrate()
        self.assertEqual(result["status"], "deactivated")
        self.assertFalse(result["active"])

    def test_migrating_inactive_connection_preserves_active_connection_hint_receipts(self):
        original_id = self.connection_id
        original_store = self.store
        self.store = SecurePeerStore(self.root / "second-host", "second-host-001", "second-hub-001")
        self.store.configure_listener_identity(OLD_HOST)
        with mock.patch.object(self.client, "_request", side_effect=self.pairing_exchange):
            pending = self.client.begin_pairing(
                OLD_HOST, port=OLD_PORT, expected_ca_fingerprint=self.store.ca_fingerprint,
                requested_scopes=["teamspace.read"],
            )
            self.store.approve_pairing(
                pending["pairing_id"], "migration-team-001", ["teamspace.read"],
                "migration-owner-001", expected_peer_server_identity=self.client.server_identity,
                expected_transcript_hash=pending["transcript_hash"],
                idempotency_key=str(uuid.uuid4()),
            )
            self.connection_id = pending["connection_id"]
            self.client.poll_pairing(self.connection_id)
            self.client.set_active_connection(self.connection_id, expected_current=original_id)
        active_id = self.connection_id
        self.connection_id = original_id
        self.store = original_store
        mail_receipt = self.client._mail_hint_health
        notification_receipt = self.client._notification_hint_health
        result = self.tls_migrate()
        self.assertFalse(result["active"])
        self.assertEqual(self.client._mail_hint_health, mail_receipt)
        self.assertEqual(self.client._notification_hint_health, notification_receipt)
        self.assertTrue(self.client.get_connection(active_id)["active"])
        self.assertTrue(self.client.mail_hint_capability(active_id))
        self.assertTrue(self.client.notification_hint_capability(active_id))

    def test_revocation_during_probe_is_not_blocked_or_resurrected(self):
        self.store.configure_listener_identity(NEW_HOST)
        response = self.response(self.health())
        entered, release = threading.Event(), threading.Event()
        errors = []

        def request(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("Migration probe was not released")
            return response

        def migrate():
            try:
                self.migrate()
            except BaseException as error:
                errors.append(error)

        with mock.patch.object(self.client, "_request", side_effect=request):
            worker = threading.Thread(target=migrate)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(self.client._route_guard.acquire(timeout=1))
                try:
                    self.client.retire_remote_revoked_connection(
                        self.connection_id, expected_host_server_identity=self.store.host_server_identity,
                        expected_hub_id=self.store.hub_id,
                        expected_certificate_fingerprint=self.original["certificate_fingerprint"],
                    )
                finally:
                    self.client._route_guard.release()
            finally:
                release.set()
                worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], SecurePeerError)
        self.assertEqual(errors[0].code, "connection_changed")
        current = self.client.get_connection(self.connection_id)
        self.assertEqual((current["host_ip"], current["port"]), (OLD_HOST, OLD_PORT))
        self.assertEqual(current["status"], "revoked")
        self.assertFalse(current["active"])

    def test_concurrent_endpoint_roundtrip_is_detected_even_in_same_second(self):
        other_client = self.new_client()
        self.store.configure_listener_identity(NEW_HOST)
        response = self.response(self.health())

        def other_request(host, *args, **kwargs):
            self.store.configure_listener_identity(host)
            return self.response(self.health())

        def race(*args, **kwargs):
            with mock.patch.object(other_client, "_request", side_effect=other_request):
                for host, port in (("192.0.2.30", 7853), (OLD_HOST, OLD_PORT)):
                    other_client.update_connection_endpoint(self.connection_id, host, port,
                        expected_host_server_identity=self.store.host_server_identity,
                        expected_hub_id=self.store.hub_id)
            return response

        with mock.patch.object(self.client, "_request", side_effect=race):
            with self.assertRaises(SecurePeerError) as raised:
                self.migrate()
        self.assertEqual(raised.exception.code, "connection_changed")
        current = self.client._connection_row(self.connection_id)
        self.assertEqual((current["host_ip"], current["port"]), (OLD_HOST, OLD_PORT))
        self.assertEqual(current["endpoint_generation"], 2)

    def test_renewal_or_active_selection_change_during_probe_is_rejected(self):
        self.store.configure_listener_identity(NEW_HOST)
        response = self.response(self.health())
        for change in ("renewal", "active_selection", "expiry"):
            with self.subTest(change=change):
                def race(*args, **kwargs):
                    with closing(self.client._connect()) as database:
                        if change == "renewal":
                            database.execute(
                                """INSERT INTO client_renewals(request_id,connection_id,
                                old_certificate_fingerprint,request_json,key_path,status,created_at,updated_at)
                                VALUES(?,?,?,?,?,'pending',?,?)""",
                                (str(uuid.uuid4()), self.connection_id, self.original["certificate_fingerprint"],
                                 "{}", self.original["key_path"], self.now, self.now),
                            )
                        elif change == "active_selection":
                            database.execute("UPDATE client_meta SET value=NULL WHERE key='active_connection_id'")
                        else:
                            self.now = self.original["certificate_expires_at"] - 60
                    return response

                with mock.patch.object(self.client, "_request", side_effect=race):
                    with self.assertRaises(SecurePeerError) as raised:
                        self.migrate()
                self.assertEqual(raised.exception.code, "connection_changed")
                self.assert_original()
                with closing(self.client._connect()) as database:
                    database.execute("DELETE FROM client_renewals")
                    database.execute("UPDATE client_meta SET value=? WHERE key='active_connection_id'", (self.connection_id,))


if __name__ == "__main__":
    unittest.main()
