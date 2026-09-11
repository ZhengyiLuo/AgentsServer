"""Real pinned mTLS/HTTP Mail streams over private AF_UNIX socket pairs only.

The listener constructor/bind/serve and client TCP dial are replaced. Actual
certificate issuance, TLS handshakes, HTTP parsing/framing, gateway handlers,
stream readers, disconnect watcher, and finite worker accounting execute.
Client runtime binding checks and mailbox bootstrap use isolated fixtures;
this is transport acceptance, not full runtime or desktop end-to-end coverage.
No monolithic server import, TCP listener, DNS, or production state is used.
"""
from __future__ import annotations

from contextlib import suppress
import http.client
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
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agentsdock_team_hub.mail_hints import MailArrival, MailHintBroker
from agentsdock_team_hub.mail_hint_streams import MailHintLease
from agentsdock_team_hub.secure_peer import (
    PeerMailHintStream, SecurePeerClient, SecurePeerError, SecurePeerGateway,
    SecurePeerStore, _GatewayHTTPServer, build_pairing_request,
)


HOST = "100.64.0.1"
SOURCE = "100.64.0.2"
TEAM = "team_tls_fixture"
RECIPIENT = "recipient_tls_fixture"
HUB = "hub_tls_fixture"
HTTPS_CONNECTION = http.client.HTTPSConnection


class PeerMailHintTLSAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mail-tls-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = SecurePeerStore(self.root / "host", "server_tls_fixture", HUB)
        key = Ed25519PrivateKey.generate()
        request = build_pairing_request(key, server_identity="peer_tls_fixture",
            display_name="Isolated TLS peer", host_ca_fingerprint=self.store.ca_fingerprint,
            requested_scopes=["teamspace.read"])
        submitted = self.store.submit_pairing(request, source_ip=SOURCE, source_port=40000)
        self.store.approve_pairing(submitted["pairing_id"], TEAM, ["teamspace.read"], "owner_fixture",
            expected_peer_server_identity="peer_tls_fixture",
            expected_transcript_hash=submitted["transcript_hash"], idempotency_key=str(uuid.uuid4()))
        approved = self.store.poll_pairing(submitted["pairing_id"], submitted["poll_token"])
        certificate = x509.load_pem_x509_certificate(approved["client_certificate_pem"].encode())
        self.peer = self.store.authenticate_peer(certificate.public_bytes(serialization.Encoding.DER))
        certificate_path, key_path = self.root / "peer.pem", self.root / "peer-key.pem"
        certificate_path.write_text(approved["client_certificate_pem"])
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        certificate_path.chmod(0o600)
        key_path.chmod(0o600)
        self.row = {
            "host_ip": HOST, "port": 17857, "hub_id": HUB, "team_id": TEAM,
            "host_ca_certificate_pem": self.store.ca_certificate_path.read_text(),
            "host_ca_fingerprint": self.store.ca_fingerprint,
            "certificate_path": str(certificate_path), "key_path": str(key_path),
            "certificate_expires_at": self.peer.certificate_expires_at,
        }
        self.client = SecurePeerClient.__new__(SecurePeerClient)
        self.client.timeout_seconds = 2
        self.client._clock = time.time
        self.client._revalidate_mail_hint_connection = mock.Mock()
        self.broker = MailHintBroker()
        self.addCleanup(self.broker.close)
        self.leases = []
        self.transport_threads = []
        self.streams = []
        self.sockets = []
        self.writes = threading.Condition()
        self.hint_write_started = 0
        self.hint_write_finished = 0
        self.hint_write_failed = threading.Event()
        self.gateway = SecurePeerGateway(self.store, HOST, self.row["port"],
            mail_hint_subscriber=self.subscribe)
        with mock.patch("agentsdock_team_hub.secure_peer._GatewayHTTPServer", side_effect=self.no_listener):
            self.gateway.start()
        self.addCleanup(self.cleanup_transport)

    def subscribe(self, peer, previous):
        self.assertEqual(peer, self.peer)
        self.assertIsNone(previous)
        lease = MailHintLease(self.broker.subscribe(TEAM, RECIPIENT),
            MailArrival(TEAM, RECIPIENT, 0, None).as_dict(reset=True), hub_id=HUB,
            authorize=lambda: None, expires_at=self.peer.certificate_expires_at)
        self.leases.append(lease)
        return lease

    def no_listener(self, address, handler, **kwargs):
        self.assertEqual(address, (HOST, self.row["port"]))
        self.assertFalse(kwargs["bind_and_activate"])
        server = _GatewayHTTPServer.__new__(_GatewayHTTPServer)
        server._worker_slots = threading.BoundedSemaphore(1)
        server._worker_guard = threading.Condition()
        server._workers = set()
        server._source_guard = threading.Lock()
        server._source_connections = {}
        server._maximum_per_source = 1
        server._tls_guard = threading.Lock()
        server._tls_context = None
        server.RequestHandlerClass = handler
        for name in ("server_bind", "server_activate", "server_close", "serve_forever", "shutdown"):
            setattr(server, name, mock.Mock())
        server.handle_error = mock.Mock()
        actual_process = server._process_tls_request

        def process(request, client_address):
            self.transport_threads.append(threading.current_thread())
            actual_process(request, client_address)

        server._process_tls_request = process
        actual_setup = handler.setup
        owner = self

        def setup(instance):
            actual_setup(instance)
            actual_write = instance.wfile.write

            def write(data):
                hint = b'"type":"hint"' in data
                if hint:
                    with owner.writes:
                        owner.hint_write_started += 1
                        owner.writes.notify_all()
                try:
                    result = actual_write(data)
                except BaseException:
                    if hint:
                        owner.hint_write_failed.set()
                    raise
                if hint:
                    with owner.writes:
                        owner.hint_write_finished += 1
                        owner.writes.notify_all()
                return result

            instance.wfile.write = write

        handler.setup = setup
        self.server = server
        return server

    def cleanup_transport(self):
        for stream in self.streams:
            stream.close()
        for sock in self.sockets:
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            sock.close()
        self.gateway.stop(timeout_seconds=2)
        for thread in self.transport_threads:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "TLS handler leaked after cleanup")

    def open_stream(self, *, row=None, mutual_tls=True):
        row = self.row if row is None else row
        context = self.client._pinned_context(row, mutual_tls=mutual_tls)
        server_raw, client_raw = socket.socketpair()
        self.sockets.extend((server_raw, client_raw))
        server_raw.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        client_raw.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
        self.server.process_request(server_raw, (SOURCE, 40000))
        body = {"version": 1, "team_id": TEAM, "previous_cursor": None}

        def dial_local_pair(connection):
            client_raw.settimeout(connection.timeout)
            connection.sock = connection._context.wrap_socket(client_raw, server_hostname=connection.host)

        with mock.patch.object(self.client, "_prepare_mail_hint_request", return_value=(row, context, body), create=True), \
             mock.patch.object(HTTPS_CONNECTION, "connect", dial_local_pair):
            stream = self.client.open_mail_hint_stream("fixture-connection")
        self.assertIsInstance(stream, PeerMailHintStream)
        self.streams.append(stream)
        return stream

    def assert_settled(self):
        with self.gateway._mail_guard:
            self.assertTrue(self.gateway._mail_guard.wait_for(lambda: not self.gateway._mail_streams, 2))
        for thread in self.transport_threads:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "idle remote disconnect did not settle TLS handler")
        self.assertTrue(all(lease.closed for lease in self.leases))
        self.assertEqual(self.server._workers, set())
        self.assertEqual(self.server._source_connections, {})
        self.assertEqual(self.gateway._mail_watcher._entries, {})
        self.server.handle_error.assert_not_called()

    def test_real_pinned_mtls_idle_close_reopens_without_consuming_http_capacity(self):
        for _ in range(3):
            stream = self.open_stream()
            snapshot = stream.read()
            self.assertEqual(snapshot["type"], "snapshot")
            self.assertEqual(snapshot["cursor"]["recipient_server_id"], RECIPIENT)
            self.assertEqual(self.server._workers, set())
            self.assertEqual(self.server._source_connections, {})
            self.assertTrue(self.server._worker_slots.acquire(blocking=False))
            self.server._worker_slots.release()
            self.assertEqual(len(self.gateway._mail_streams), 1)
            stream.close()
            self.assert_settled()

    def test_real_wrong_ca_pin_rejects_before_http_or_subscription(self):
        other = SecurePeerStore(self.root / "other-host", "other_tls_host", "other_tls_hub")
        wrong = {**self.row, "host_ca_certificate_pem": other.ca_certificate_path.read_text(),
            "host_ca_fingerprint": other.ca_fingerprint}
        with self.assertRaises(ssl.SSLCertVerificationError):
            self.open_stream(row=wrong)
        self.assertEqual(self.leases, [])
        self.assert_settled()

    def test_real_missing_mtls_certificate_cannot_subscribe(self):
        with self.assertRaises(SecurePeerError) as error:
            self.open_stream(mutual_tls=False)
        self.assertEqual(error.exception.status_code, 401)
        self.assertEqual(self.leases, [])
        self.assert_settled()

    def test_real_blocked_hint_writer_is_aborted_by_remote_disconnect(self):
        stream = self.open_stream()
        self.assertEqual(stream.read()["type"], "snapshot")
        blocked = False
        for sequence in range(1, 129):
            self.broker.publish(MailArrival(TEAM, RECIPIENT, sequence, f"tmsg_{sequence:032x}"))
            with self.writes:
                self.assertTrue(self.writes.wait_for(lambda: self.hint_write_started >= sequence, 1))
                if not self.writes.wait_for(lambda: self.hint_write_finished >= sequence, 0.05):
                    blocked = True
                    break
        self.assertTrue(blocked, "fixture failed to establish actual socket backpressure")
        self.assertGreater(self.hint_write_started, self.hint_write_finished)
        started = time.monotonic()
        stream.close()
        self.assert_settled()
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(self.hint_write_failed.is_set(), "disconnect did not interrupt the real pending write")
