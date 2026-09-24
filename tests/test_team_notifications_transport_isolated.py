"""V2 transport checks through guarded runner; no monolith or network I/O."""
import asyncio
import io
import json
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from agentsdock_team_hub.mail_hints import MailHintClosed
from agentsdock_team_hub.notification_hints import BulletinChange, NotificationBroker, NotificationCursor, NotificationLease
from agentsdock_team_hub.secure_peer import PeerMailHintStream, SecurePeerClient, SecurePeerError, _mail_hint_frame
from team_mail_websocket import NOTIFICATION_WEBSOCKET_PROTOCOL, serve_team_mail_hints
from tests import test_peer_mail_hint_transport_isolated as peer_fixtures
from tests import test_team_mail_stream_runtime_isolated as runtime_fixtures


def cursor(mail_sequence=0, bulletin_sequence=0, *, mail_reset=False, bulletin_reset=False):
    return NotificationCursor(
        peer_fixtures.arrival(mail_sequence),
        BulletinChange(peer_fixtures.TEAM, bulletin_sequence,
            f"bchg_{bulletin_sequence:032x}" if bulletin_sequence else None,
            "tmsg_" + "f" * 32 if bulletin_sequence else None,
            "revised" if bulletin_sequence else None, 2 if bulletin_sequence else None),
        mail_reset, bulletin_reset)


def packet(kind="snapshot", **kwargs):
    return {"type": kind, "hub_id": peer_fixtures.HUB, "cursor": cursor(**kwargs).as_dict()}


class PeerNotificationTests(unittest.TestCase):
    def test_snapshot_preserves_independent_resets_and_hint_schema_is_exact(self):
        value = packet(mail_sequence=4, bulletin_sequence=8, bulletin_reset=True)
        self.assertEqual(_mail_hint_frame(value, hub_id=peer_fixtures.HUB,
            team_id=peer_fixtures.TEAM, version=2), value)
        self.assertEqual(set(value["cursor"]), {"version", "mail", "bulletin"})
        self.assertEqual(set(value["cursor"]["mail"]), set(peer_fixtures.frame()["cursor"]))
        for changed in ({"body": "private"}, {"version": 1}, {"bulletin": {}}):
            invalid = {**value, "cursor": {**value["cursor"], **changed}}
            with self.subTest(changed=changed), self.assertRaises(SecurePeerError):
                _mail_hint_frame(invalid, hub_id=peer_fixtures.HUB, team_id=peer_fixtures.TEAM, version=2)

    def test_reader_rejects_nested_scope_reset_and_second_snapshot(self):
        reset = packet("hint", bulletin_sequence=1, bulletin_reset=True)
        wrong_team = packet("hint", bulletin_sequence=1)
        wrong_team["cursor"]["bulletin"]["team_id"] = "team_other"
        wrong_recipient = packet("hint", mail_sequence=1)
        wrong_recipient["cursor"]["mail"]["recipient_server_id"] = "node_other"
        for invalid in (reset, wrong_team, wrong_recipient, packet()):
            response = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in (packet(), invalid)))
            stream = PeerMailHintStream(mock.Mock(), response, mock.Mock(), hub_id=peer_fixtures.HUB,
                team_id=peer_fixtures.TEAM, expires_at=200, revalidate=mock.Mock(), clock=lambda: 100, version=2)
            self.addCleanup(stream.close)
            stream.read()
            with self.subTest(invalid=invalid), self.assertRaises(SecurePeerError):
                stream.read()

    def test_gateway_version_dispatch_keeps_v1_and_v2_callbacks_separate(self):
        fixture = peer_fixtures.GatewayMailHintTests()
        self.addCleanup(fixture.doCleanups)
        gateway, handler, _v1lease, _store = fixture.gateway()
        gateway.notification_hint_snapshot = mock.Mock(return_value={"hub_id": peer_fixtures.HUB,
            "cursor": cursor(bulletin_reset=True).as_dict()})
        handler._json_body.return_value = {"version": 2, "team_id": peer_fixtures.TEAM, "previous_cursor": None}
        handler._mail_hints(stream=False)
        gateway.notification_hint_snapshot.assert_called_once_with(peer_fixtures.PEER, None)
        gateway.mail_hint_snapshot.assert_not_called()
        handler._json.assert_called_once_with(200, gateway.notification_hint_snapshot.return_value)
        gateway.notification_hint_snapshot = None
        with self.assertRaises(SecurePeerError) as disabled:
            handler._mail_hints(stream=False)
        self.assertEqual(disabled.exception.status_code, 404)

    def test_gateway_stream_writes_bulletin_hint_without_mail_arrival(self):
        fixture = peer_fixtures.GatewayMailHintTests()
        self.addCleanup(fixture.doCleanups)
        gateway, handler, _v1lease, _store = fixture.gateway()
        broker = NotificationBroker()
        self.addCleanup(broker.close)
        lease = NotificationLease(broker.subscribe(peer_fixtures.TEAM, peer_fixtures.RECIPIENT),
            cursor(mail_reset=True, bulletin_reset=True).as_dict(), hub_id=peer_fixtures.HUB,
            authorize=lambda: None, expires_at=200, clock=lambda: 100)
        self.addCleanup(lease.close)
        lease.take = mock.Mock(side_effect=[cursor(bulletin_sequence=1), None])
        gateway.notification_hint_subscriber = mock.Mock(return_value=lease)
        handler._json_body.return_value = {"version": 2, "team_id": peer_fixtures.TEAM, "previous_cursor": None}
        handler._mail_hints(stream=True)
        frames = [json.loads(line) for line in handler.wfile.getvalue().splitlines()]
        self.assertEqual([frame["type"] for frame in frames], ["snapshot", "hint"])
        self.assertEqual(frames[-1]["cursor"]["mail"]["through_sequence"], 0)
        self.assertEqual(frames[-1]["cursor"]["bulletin"]["change_kind"], "revised")
        gateway.mail_hint_subscriber.assert_not_called()
        self.assertTrue(lease.closed)
        self.assertEqual(gateway._mail_streams, {})

    def test_authenticated_receipt_defaults_old_hosts_to_v1_and_expires(self):
        client = SecurePeerClient.__new__(SecurePeerClient)
        client._route_guard = threading.RLock()
        client._timestamp = mock.Mock(return_value=100)
        client._mail_hint_health = client._notification_hint_health = None
        row = {"connection_id": "connection_test", "status": "connected", "host_ip": "100.64.0.1", "port": 17857,
            "peer_id": "peer_test", "team_id": "team_test", "host_server_identity": "host_test", "hub_id": "hub_test",
            "host_ca_fingerprint": "ca_test", "certificate_fingerprint": "cert_test", "certificate_expires_at": 1000}
        value = {**row, "remote_route_delivery_available": False, "mail_hints_available": True}
        client._connection_row = mock.Mock(return_value=row)
        client._pinned_context = mock.Mock()
        client._request = mock.Mock(return_value=(200, [], b"", None))
        client._decode_json_response = mock.Mock(return_value=value)
        database = mock.Mock()
        database.execute.return_value.rowcount = 1
        client._connect = mock.Mock(return_value=database)
        client.peer_health("connection_test")
        self.assertTrue(client.mail_hint_capability("connection_test", "cert_test"))
        self.assertFalse(client.notification_hint_capability("connection_test", "cert_test"))
        value["mail_hints_v2_available"] = True
        client.peer_health("connection_test")
        self.assertTrue(client.notification_hint_capability("connection_test", "cert_test"))
        self.assertFalse(client.notification_hint_capability("connection_other", "cert_test"))
        self.assertFalse(client.notification_hint_capability("connection_test", "cert_other"))
        client._timestamp.return_value = 221
        self.assertFalse(client.notification_hint_capability("connection_test", "cert_test"))
        value["mail_hints_v2_available"] = "true"
        with self.assertRaises(SecurePeerError):
            client.peer_health("connection_test")


class RuntimeNotificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = runtime_fixtures.RuntimeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.f = self.fixture.f

    def member(self, *, negotiated=True):
        manager, client, runtime = self.fixture.member()
        fixture = self.fixture
        class Stream:
            def __init__(self):
                self.lease = fixture.adapter.subscribe_team_mail_hints(fixture.peer, version=2)
                self.first = True
            def read(self):
                if self.first:
                    self.first = False
                    return {"type": "snapshot", "hub_id": self.lease.hub_id, "cursor": self.lease.snapshot}
                item = self.lease.take()
                return None if item is None else {"type": "hint", "hub_id": self.lease.hub_id, "cursor": item.as_dict(reset=False)}
            def close(self):
                self.lease.close()
        client.notification_hint_capability = mock.Mock(return_value=negotiated)
        client.open_notification_hint_stream = mock.Mock(side_effect=lambda _id: Stream())
        client.team_notification_hint_snapshot = mock.Mock(side_effect=lambda _id, previous:
            fixture.adapter.team_mail_hint_snapshot(fixture.peer, previous, version=2))
        return manager, client, runtime

    def test_mixed_clients_share_one_upstream_bulletin_never_wakes_v1(self):
        manager, client, _runtime = self.member()
        v1 = manager.subscribe(self.f.team)
        v2 = manager.subscribe(self.f.team, version=2)
        self.assertEqual(client.open_notification_hint_stream.call_count, 1)
        client.open_mail_hint_stream.assert_not_called()
        self.assertEqual(client.team_notification_hint_snapshot.call_count, 1)
        self.assertEqual(client.team_mail_hint_snapshot.call_count, 1)
        with mock.patch.object(self.f.store, "connect", wraps=self.f.store.connect) as connect:
            threading.Event().wait(.02)
            connect.assert_not_called()
        bulletin = self.f.send(recipients=[{"kind": "all"}])
        hint = v2.take(1)
        self.assertEqual(hint.bulletin.message_id, bulletin["id"])
        self.assertEqual(hint.mail.through_sequence, 0)
        self.assertIsNone(v1._subscription.take(.02))
        mail = self.f.send()
        self.assertEqual(v1.take(1).arrival_id, mail["id"])
        combined = v2.take(1)
        self.assertEqual(combined.mail.arrival_id, mail["id"])
        self.assertEqual(combined.bulletin.message_id, bulletin["id"])
        self.assertEqual(set(v1.snapshot), set(peer_fixtures.frame()["cursor"]))
        feed = manager.member
        v1.close()
        self.assertFalse(feed.closed)
        v2.close()
        feed.thread.join(1)
        self.assertFalse(feed.thread.is_alive())
        self.assertEqual(self.fixture.adapter._mail_leases, {})

    def test_old_host_fallback_and_negotiation_upgrade_retire_old_feed(self):
        manager, client, _runtime = self.member(negotiated=False)
        self.assertTrue(manager.capability()["enabled"])
        self.assertFalse(manager.capability(version=2)["enabled"])
        with self.assertRaises(MailHintClosed):
            manager.subscribe(self.f.team, version=2)
        client.open_notification_hint_stream.assert_not_called()
        old = manager.subscribe(self.f.team)
        old_feed = manager.member
        client.notification_hint_capability.return_value = True
        current = manager.subscribe(self.f.team, version=2)
        self.assertTrue(old_feed.closed)
        self.assertIsNone(old.take(.1))
        self.assertIsNot(manager.member, old_feed)
        self.assertEqual(client.open_notification_hint_stream.call_count, 1)
        self.assertTrue(manager.capability(version=2)["enabled"])
        client.notification_hint_capability.return_value = False
        self.assertFalse(manager.capability(version=2)["enabled"])
        with self.assertRaises(MailHintClosed):
            current.write(lambda _value: self.fail("Unnegotiated v2 write"), current.snapshot)

    def test_host_runtime_v2_bulletin_and_maintenance_fence(self):
        runtime = self.fixture.host_runtime()
        self.assertTrue(runtime.team_notification_hint_capability()["enabled"])
        lease = runtime.subscribe_team_notification_hints(self.f.team)
        bulletin = self.f.send(recipients=[{"kind": "all"}])
        self.assertEqual(lease.take(1).bulletin.message_id, bulletin["id"])
        runtime.close_host_admission()
        self.assertTrue(lease.closed)

    def test_peer_revoke_closes_v2_upstream_and_wakes_local_cohort(self):
        manager, _client, _runtime = self.member()
        v1 = manager.subscribe(self.f.team)
        v2 = manager.subscribe(self.f.team, version=2)
        feed = manager.member
        self.fixture.adapter.revoke_peer(peer_id=self.fixture.peer.peer_id, team_id=self.f.team)
        feed.thread.join(1)
        self.assertFalse(feed.thread.is_alive())
        self.assertTrue(feed.closed)
        self.assertIsNone(v1.take(.1))
        self.assertIsNone(v2.take(.1))
        self.assertEqual(manager.leases, set())
        self.assertEqual(self.fixture.adapter._mail_leases, {})


class NotificationWebsocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_metadata_only_frames_share_stream_and_disconnect_releases(self):
        broker = NotificationBroker()
        self.addCleanup(broker.close)
        lease = NotificationLease(broker.subscribe(peer_fixtures.TEAM, peer_fixtures.RECIPIENT),
            cursor(mail_reset=True, bulletin_reset=True).as_dict(), hub_id=peer_fixtures.HUB,
            authorize=lambda: None, expires_at=None)
        self.addCleanup(lease.close)
        runtime = SimpleNamespace(team_notification_hint_capability=lambda: {"enabled": True},
            subscribe_team_notification_hints=mock.Mock(return_value=lease))
        incoming, outgoing, accepted, closed = asyncio.Queue(), asyncio.Queue(), [], []
        await incoming.put(json.dumps({"version": 2, "team_id": peer_fixtures.TEAM, "previous_cursor": None}))
        class Socket:
            query_params = {}
            async def accept(self, **kwargs): accepted.append(kwargs)
            async def receive_text(self): return await incoming.get()
            async def send_text(self, value): await outgoing.put(json.loads(value))
            async def close(self, code): closed.append(code)
        task = asyncio.create_task(serve_team_mail_hints(Socket(), runtime, server_identity="server_test",
            authorized=lambda: True, protocols=[NOTIFICATION_WEBSOCKET_PROTOCOL]))
        try:
            snapshot = await asyncio.wait_for(outgoing.get(), 2)
            broker.publish_bulletin(cursor(bulletin_sequence=1).bulletin)
            hint = await asyncio.wait_for(outgoing.get(), 2)
            self.assertEqual(accepted, [{"subprotocol": NOTIFICATION_WEBSOCKET_PROTOCOL}])
            self.assertEqual(set(snapshot), {"type", "server_identity", "hub_id", "stream_id", "cursor"})
            self.assertEqual(snapshot["stream_id"], hint["stream_id"])
            self.assertEqual(hint["cursor"], cursor(bulletin_sequence=1).as_dict())
            self.assertNotIn("body", json.dumps(hint))
        finally:
            await incoming.put("extra command forbidden")
            await asyncio.wait_for(task, 2)
        self.assertIn(1008, closed)
        self.assertTrue(lease.closed)
        self.assertEqual(broker._count, 0)
