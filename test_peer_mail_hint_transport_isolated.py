"""Recipient-only transport checks; no server import, network socket or provider."""
from __future__ import annotations

import io
import json
import socket
import threading
import unittest
from unittest import mock

from agentsdock_team_hub.mail_hints import MailArrival, MailHintBroker, MailHintClosed
from agentsdock_team_hub.mail_hint_streams import MailHintLease
from agentsdock_team_hub.secure_peer import (
    MAX_MAIL_HINT_FRAME_BYTES, PeerAuthorization, PeerMailHintStream,
    SecurePeerError, SecurePeerGateway, _GatewayHTTPServer, _mail_hint_frame,
    sanitize_proxy_request, _MailHintDisconnectWatcher,
)


TEAM = "team_fixture"
HUB = "hub_fixture"
RECIPIENT = "node_fixture"
PEER = PeerAuthorization("peer_fixture", "pair_fixture", "server_fixture", TEAM,
    frozenset({"teamspace.read"}), "sha256:" + "a" * 64, 200, "Test peer")


def arrival(sequence=0, recipient=RECIPIENT):
    return MailArrival(TEAM, recipient, sequence, f"tmsg_{sequence:032x}" if sequence else None)


def frame(kind="snapshot", sequence=0, recipient=RECIPIENT):
    return {"type": kind, "hub_id": HUB, "cursor": arrival(sequence, recipient).as_dict(reset=kind == "snapshot")}


class PeerMailHintReaderTests(unittest.TestCase):
    def make(self, packets):
        response = io.BytesIO(b"".join(json.dumps(value).encode() + b"\n" for value in packets))
        connection, sock, auth = mock.Mock(), mock.Mock(), mock.Mock()
        stream = PeerMailHintStream(connection, response, sock, hub_id=HUB, team_id=TEAM,
            expires_at=200, revalidate=auth, clock=lambda: 100)
        self.addCleanup(stream.close)
        return stream, response, connection, sock, auth

    def test_exact_snapshot_and_hint_one_deadline_no_idle_auth_work(self):
        stream, _, _, sock, auth = self.make([frame(), frame("hint", 1)])
        auth.assert_not_called()
        self.assertEqual(stream.read(), frame())
        self.assertEqual(stream.read(), frame("hint", 1))
        self.assertEqual(auth.call_count, 2)
        self.assertEqual(sock.settimeout.call_args_list, [mock.call(100), mock.call(100)])
        with self.assertRaises(MailHintClosed): stream.read()
        sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_rejects_extra_body_wrong_realm_reset_hint_and_bad_cursor(self):
        for value in [dict(frame(), body="never"), dict(frame(), hub_id="other"),
                      dict(frame(), cursor={**frame()["cursor"], "team_id": "other"}),
                      dict(frame(), cursor={**frame()["cursor"], "through_sequence": True}),
                      frame("hint", 1), dict(frame(), cursor={**frame()["cursor"], "reset": "yes"})]:
            with self.subTest(value=value):
                stream, _, _, sock, _ = self.make([value])
                with self.assertRaises(SecurePeerError): stream.read()
                sock.shutdown.assert_called_once()

    def test_rejects_second_snapshot_and_changed_recipient(self):
        for second in (frame(), frame("hint", 1, "node_other"),
                       dict(frame("hint", 1), cursor=arrival(1).as_dict(reset=True))):
            stream, _, _, _, _ = self.make([frame(), second])
            stream.read()
            with self.assertRaises(SecurePeerError): stream.read()

    def test_oversized_line_is_read_with_fixed_bound(self):
        stream, _, _, _, _ = self.make([])
        stream._response = mock.Mock()
        stream._response.readline.return_value = b"x" * (MAX_MAIL_HINT_FRAME_BYTES + 1)
        with self.assertRaises(SecurePeerError): stream.read()
        stream._response.readline.assert_called_once_with(MAX_MAIL_HINT_FRAME_BYTES + 1)

    def test_local_revoke_interrupts_blocked_read_without_waiting_on_response_lock(self):
        stream, _, connection, sock, auth = self.make([])
        entered, released, finished = threading.Event(), threading.Event(), threading.Event()
        response = mock.Mock()
        def read(_maximum):
            entered.set()
            if not released.wait(1): raise AssertionError("socket shutdown did not unblock read")
            return b""
        response.readline.side_effect = read
        response.close.side_effect = lambda: self.assertTrue(released.is_set())
        sock.shutdown.side_effect = lambda _how: released.set()
        stream._response = response
        def run():
            try: stream.read()
            except MailHintClosed: finished.set()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(1))
        auth.assert_not_called()
        stream.close()
        thread.join(1)
        self.assertTrue(finished.is_set())
        self.assertFalse(thread.is_alive())
        connection.close.assert_called_once()


class GatewayMailHintTests(unittest.TestCase):
    def gateway(self, *, enabled=True):
        store = mock.Mock(hub_id=HUB)
        store._timestamp.return_value = 100
        store.authenticate_peer.return_value = PEER
        broker = MailHintBroker()
        self.addCleanup(broker.close)
        lease = MailHintLease(broker.subscribe(TEAM, RECIPIENT), arrival().as_dict(reset=True),
            hub_id=HUB, authorize=mock.Mock(), expires_at=200, clock=lambda: 100)
        self.addCleanup(lease.close)
        subscriber = mock.Mock(return_value=lease) if enabled else None
        gateway = SecurePeerGateway(store, "100.64.0.1", 17857,
            mail_hint_subscriber=subscriber,
            mail_hint_snapshot=mock.Mock(return_value={"hub_id": HUB, "cursor": arrival().as_dict(reset=True)}) if enabled else None)
        server = mock.Mock()
        classes = []
        def make_server(address, handler, **kwargs):
            classes.append(handler)
            return server
        with mock.patch("agentsdock_team_hub.secure_peer._GatewayHTTPServer", side_effect=make_server), \
             mock.patch("agentsdock_team_hub.secure_peer._MailHintDisconnectWatcher"), \
             mock.patch("agentsdock_team_hub.secure_peer.threading.Thread"):
            gateway.start()
        handler = classes[0].__new__(classes[0])
        handler.server, handler.connection = server, mock.Mock()
        handler.client_address = ("100.64.0.2", 1234)
        handler.command = "POST"
        handler.wfile = io.BytesIO()
        handler._peer = mock.Mock(return_value=PEER)
        handler._peer_rate = mock.Mock()
        handler._reject_browser_headers = mock.Mock()
        handler._json_body = mock.Mock(return_value={"version": 1, "team_id": TEAM, "previous_cursor": None})
        for name in ("send_response", "send_header", "end_headers", "_json"):
            setattr(handler, name, mock.Mock())
        return gateway, handler, lease, store

    def test_stream_transfers_admission_before_wait_no_extra_worker(self):
        gateway, handler, lease, store = self.gateway()
        def take(timeout):
            handler.server.release_worker.assert_called_once_with("100.64.0.2")
            self.assertEqual(len(gateway._mail_streams), 1)
            self.assertEqual(timeout, 100)
            return None
        lease.take = mock.Mock(side_effect=take)
        handler._mail_hints(stream=True)
        self.assertEqual(json.loads(handler.wfile.getvalue()), frame())
        self.assertEqual(store.authenticate_peer.call_count, 1)
        self.assertEqual(gateway._mail_streams, {})
        self.assertTrue(lease.closed)
        self.assertTrue(handler.close_connection)

    def test_disabled_and_cross_team_requests_never_subscribe(self):
        gateway, handler, _, _ = self.gateway(enabled=False)
        with self.assertRaises(SecurePeerError) as error: handler._mail_hints(stream=True)
        self.assertEqual(error.exception.status_code, 404)
        gateway, handler, _, _ = self.gateway()
        handler._json_body.return_value["team_id"] = "other"
        with self.assertRaises(SecurePeerError): handler._mail_hints(stream=True)
        gateway.mail_hint_subscriber.assert_not_called()

    def test_snapshot_is_finite_and_foreign_retained_recipient_is_not_authority(self):
        gateway, handler, _, _ = self.gateway()
        handler._json_body.return_value["previous_cursor"] = arrival(1, "old_recipient").as_dict()
        handler._mail_hints(stream=False)
        handler._json.assert_called_once_with(200, {"hub_id": HUB, "cursor": arrival().as_dict(reset=True)})
        handler.server.release_worker.assert_not_called()
        gateway.mail_hint_subscriber.assert_not_called()

    def test_stream_capacity_independent_and_exact_revocation(self):
        gateway, _, _, _ = self.gateway()
        first, second, other = mock.Mock(), mock.Mock(), mock.Mock()
        keys = [gateway._register_mail_hint_stream("a", abort) for abort in (first, second)]
        with self.assertRaises(SecurePeerError): gateway._register_mail_hint_stream("a", mock.Mock())
        gateway._register_mail_hint_stream("b", other)
        gateway.close_mail_hint_streams("a")
        first.assert_called_once(); second.assert_called_once(); other.assert_not_called()
        for key in keys: gateway._finish_mail_hint_stream(key)
        gateway._register_mail_hint_stream("a", mock.Mock())
        self.assertEqual(len(gateway._mail_streams), 2)

    def test_watcher_removal_failure_cannot_retain_registry_or_lease(self):
        gateway, handler, lease, _ = self.gateway()
        lease.take = mock.Mock(return_value=None)
        gateway._mail_watcher.remove.side_effect = RuntimeError("watcher stopped")
        with self.assertRaisesRegex(RuntimeError, "watcher stopped"):
            handler._mail_hints(stream=True)
        self.assertEqual(gateway._mail_streams, {})
        self.assertTrue(lease.closed)

    def test_finish_reads_stopping_watcher_once(self):
        gateway, _, _, _ = self.gateway()
        watcher = mock.Mock()
        key = gateway._register_mail_hint_stream("a", mock.Mock())
        # Stop may detach the watcher immediately after a handler captures it.
        with mock.patch.object(type(gateway), "_mail_watcher",
                               new_callable=mock.PropertyMock, create=True) as current:
            current.side_effect = [watcher, None]
            gateway._finish_mail_hint_stream(key)
            self.assertEqual(current.call_count, 1)
        watcher.remove.assert_called_once_with(key)
        self.assertEqual(gateway._mail_streams, {})

    def test_pre_header_closed_authority_is_terminal_but_stopping_watcher_is_retryable(self):
        gateway, handler, lease, _ = self.gateway()
        lease.close()
        with self.assertRaises(SecurePeerError) as closed:
            handler._mail_hints(stream=True)
        self.assertEqual(closed.exception.status_code, 403)
        handler.send_response.assert_not_called()
        self.assertEqual(gateway._mail_streams, {})
        for watcher in (None, mock.Mock()):
            gateway, handler, lease, _ = self.gateway()
            if watcher is not None:
                watcher.add.side_effect = MailHintClosed("stopping")
            gateway._mail_watcher = watcher
            with self.assertRaises(SecurePeerError) as stopping:
                handler._mail_hints(stream=True)
            self.assertEqual(stopping.exception.status_code, 503)
            self.assertTrue(lease.closed)
            self.assertEqual(gateway._mail_streams, {})
            handler.send_response.assert_not_called()

    def test_http_transfer_and_finally_release_worker_exactly_once(self):
        server = _GatewayHTTPServer.__new__(_GatewayHTTPServer)
        server._worker_guard = threading.Condition()
        server._source_guard = threading.Lock()
        server._worker_slots = threading.BoundedSemaphore(1)
        server._worker_slots.acquire()
        server._workers = {threading.current_thread()}
        server._source_connections = {"source": 1}
        server.release_worker("source")
        server.release_worker("source")
        self.assertEqual(server._workers, set())
        self.assertEqual(server._source_connections, {})
        self.assertTrue(server._worker_slots.acquire(blocking=False))
        self.assertFalse(server._worker_slots.acquire(blocking=False))

    def test_coverage_query_is_only_on_message_list_and_strict(self):
        path = "/v1/teams/team_fixture/network/messages"
        request = sanitize_proxy_request(PEER, "GET", path,
            "box=inbox&include_mailbox_coverage=true&after_sequence=1&after_arrival_id=tmsg_" + "1" * 32,
            (), b"")
        self.assertIn("include_mailbox_coverage=true", request.query)
        for query in ("include_mailbox_coverage=maybe", "after_arrival_id=bad",
                      "after_arrival_id=tmsg_" + "1" * 32 + "%0A"):
            with self.subTest(query=query), self.assertRaises(SecurePeerError):
                sanitize_proxy_request(PEER, "GET", path, query, (), b"")
        with self.assertRaises(SecurePeerError):
            sanitize_proxy_request(PEER, "GET", path + "/message_fixture", "include_mailbox_coverage=true", (), b"")


class PassiveDisconnectTests(unittest.TestCase):
    def test_idle_disconnect_wakes_without_timer_or_another_mail(self):
        watcher = _MailHintDisconnectWatcher()
        self.addCleanup(watcher.close)
        # Local IPC socketpair only; no bound port, network, TLS or live state.
        server_end, client_end = socket.socketpair()
        self.addCleanup(server_end.close)
        self.addCleanup(client_end.close)
        disconnected = threading.Event()
        watcher.add(object(), server_end, disconnected.set)
        self.assertFalse(disconnected.is_set())
        client_end.close()
        self.assertTrue(disconnected.wait(1))
        self.assertEqual(watcher._entries, {})

    def test_disconnect_before_registration_and_exact_token_removal(self):
        watcher = _MailHintDisconnectWatcher()
        self.addCleanup(watcher.close)
        server_end, client_end = socket.socketpair()
        self.addCleanup(server_end.close)
        removed = mock.Mock()
        token = object()
        watcher.add(token, server_end, removed)
        watcher.remove(token)
        client_end.close()
        disconnected = threading.Event()
        watcher.add(object(), server_end, disconnected.set)
        self.assertTrue(disconnected.wait(1))
        removed.assert_not_called()

    def test_stop_aborts_owned_idle_sockets_and_joins_watcher(self):
        watcher = _MailHintDisconnectWatcher()
        server_end, client_end = socket.socketpair()
        self.addCleanup(server_end.close)
        self.addCleanup(client_end.close)
        abort = mock.Mock()
        watcher.add(object(), server_end, abort)
        watcher.close()
        abort.assert_called_once()
        self.assertFalse(watcher._thread.is_alive())
        with self.assertRaises(MailHintClosed): watcher.add(object(), server_end, abort)
