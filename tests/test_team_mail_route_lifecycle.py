"""Exact recipient lifecycle fences with isolated real Hub storage."""
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid

from agentsdock_team_hub.service import TeamRecipientRequest
from agentsdock_team_hub.store import HubError, HubStore


class MailRouteLifecycleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mail-route-lifecycle-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="test-mail-host")
        self.store.bootstrap_managed_network("Mail lifecycle tests")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.peer_id, self.peer = self.add_peer("test-mail-peer")
        self.node = self.store.list_team_messages(self.peer, self.team, box="inbox")["address"]["id"]

    def add_peer(self, identity):
        peer_id = str(uuid.uuid4())
        self.store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, display_name="Recipient")
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        claims = self.store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600)
        return peer_id, claims

    def lifecycle(self, node=None):
        return self.store.get_network_server(self.host, self.team, node or self.node)["server"]["mail_route_lifecycle_id"]

    def request(self, lifecycle=None, key=None):
        recipient = {"kind": "server", "id": self.node}
        if lifecycle is not None:
            recipient["mail_route_lifecycle_id"] = lifecycle
        return {"kind": "message", "body": "Fixture mail", "recipients": [recipient],
            "idempotency_key": key or "fixture-" + uuid.uuid4().hex}

    def send(self, request):
        return self.store.create_team_message(self.host, self.team, request)

    def revoke(self):
        self.store.revoke_secure_peer_service(peer_id=self.peer_id, team_id=self.team)

    def test_identity_survives_rename_and_offline_without_roster_scan(self):
        initial = self.lifecycle()
        self.assertRegex(initial, r"^[0-9a-f]{64}$")
        self.store.rename_network_server(self.peer, self.team, "Renamed")
        connection = self.store.connect()
        try:
            connection.execute("UPDATE nodes SET status='offline' WHERE id=?", (self.node,))
        finally:
            connection.close()
        with mock.patch.object(self.store, "get_network", side_effect=AssertionError("roster scan")):
            self.assertEqual(initial, self.lifecycle())
            self.assertIn("message", self.send(self.request(initial)))

    def test_host_identity_survives_display_name_change(self):
        node = self.store.list_team_messages(self.host, self.team, box="inbox")["address"]["id"]
        initial = self.lifecycle(node)
        self.store = HubStore(self.store.data_dir, managed_host_identity="test-mail-host",
            managed_host_display_name="New host name")
        self.host = self.store.managed_server_claims()
        self.assertEqual(initial, self.lifecycle(node))

    def test_departed_recipient_is_rejected_without_creating_mail(self):
        initial = self.lifecycle()
        self.revoke()
        with self.assertRaises(HubError) as failure:
            self.send(self.request(initial))
        self.assertEqual(failure.exception.code, "mail_route_changed")

    def test_rejoin_same_node_requires_new_explicit_identity(self):
        initial = self.lifecycle()
        self.revoke()
        _, replacement = self.add_peer("test-mail-peer")
        replacement_node = self.store.list_team_messages(replacement, self.team, box="inbox")["address"]["id"]
        self.assertEqual(replacement_node, self.node)
        current = self.lifecycle()
        self.assertNotEqual(current, initial)
        with self.assertRaises(HubError) as failure:
            self.send(self.request(initial))
        self.assertEqual(failure.exception.code, "mail_route_changed")
        self.assertIn("message", self.send(self.request(current)))

    def test_committed_receipt_retry_is_not_a_new_send_after_rejoin(self):
        initial = self.lifecycle()
        request = self.request(initial)
        accepted = self.send(request)
        self.revoke()
        self.add_peer("test-mail-peer")
        self.assertEqual(accepted, self.send(request))
        with self.assertRaises(HubError) as failure:
            self.send(self.request(self.lifecycle(), request["idempotency_key"]))
        self.assertEqual(failure.exception.code, "idempotency_conflict")

    def test_lifecycle_is_checked_inside_message_write_transaction(self):
        initial = self.lifecycle()
        actual = self.store._network_server_mail_route_lifecycle
        observations = []
        def check(connection, team, node):
            observations.append(connection.in_transaction)
            return actual(connection, team, node)
        with mock.patch.object(self.store, "_network_server_mail_route_lifecycle", side_effect=check):
            self.send(self.request(initial))
        self.assertEqual(observations, [True])

    def test_old_recipient_shape_and_explicit_none_remain_idempotent(self):
        request = self.request()
        accepted = self.send(request)
        request["recipients"][0] = TeamRecipientRequest(**request["recipients"][0]).model_dump()
        self.assertEqual(accepted, self.send(request))

    def test_conflicting_and_wrong_kind_preconditions_are_rejected(self):
        initial = self.lifecycle()
        invalid = self.request(initial)
        invalid["recipients"].append({"kind": "server", "id": self.node})
        with self.assertRaises(HubError):
            self.send(invalid)
        invalid["recipients"] = [{"kind": "all", "mail_route_lifecycle_id": initial}]
        with self.assertRaises(HubError):
            self.send(invalid)


if __name__ == "__main__":
    unittest.main()
