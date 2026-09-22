"""Recipient preconditions through the pure adapter and isolated Hub storage.

No server import, network transport, provider process, or production state.
Run with the guarded public_chat_share_safe_tests.py runner.
"""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid

from agentsdock_team_hub.secure_peer import PeerAuthorization, ProxyRequest
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from agentsdock_team_hub.store import HubError, HubStore


class TeamMailRecipientContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="team-recipient-contract-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub",
                              managed_host_identity="recipient-contract-host")
        self.store.bootstrap_managed_network("Recipient contract fixture")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.adapter = SecurePeerHubAdapter(self.store)
        self.sender, _ = self.add_peer("recipient-contract-sender")
        self.recipient, self.node = self.add_peer("recipient-contract-target")
        self.lifecycle = self.store.get_network_server(
            self.host, self.team, self.node)["server"]["mail_route_lifecycle_id"]

    def add_peer(self, identity):
        peer = PeerAuthorization(
            str(uuid.uuid4()), str(uuid.uuid4()), identity, self.team,
            frozenset({"teamspace.read", "teamspace.write"}),
            "sha256:" + "a" * 64, int(time.time()) + 3600, "Fixture peer")
        self.adapter.provision_peer({"peer_id": peer.peer_id,
            "peer_server_identity": identity, "team_id": self.team},
            display_name=peer.peer_display_name)
        self.adapter.record_peer_heartbeat(peer.peer_id, self.team)
        claims = self.store.secure_peer_claims(peer_id=peer.peer_id,
            peer_server_identity=identity, team_id=self.team, scopes=peer.scopes,
            expires_at=peer.certificate_expires_at)
        node = self.store.list_team_messages(claims, self.team, box="inbox")["address"]["id"]
        return peer, node

    def body(self, recipient=None):
        return {"kind": "message", "body": "Synthetic recipient contract mail",
            "recipients": [recipient if recipient is not None else {
                "kind": "server", "id": self.node,
                "mail_route_lifecycle_id": self.lifecycle}],
            "idempotency_key": "recipient-contract-" + uuid.uuid4().hex}

    def request(self, body):
        return ProxyRequest("POST", f"/v1/teams/{self.team}/network/messages", "", (),
                            json.dumps(body).encode("utf-8"), self.sender)

    def message_count(self):
        with closing(self.store.connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=?",
                                      (self.team,)).fetchone()[0]

    def assert_rejected(self, body, *, code, status):
        before = self.message_count()
        response = self.adapter.forward(self.request(body))
        self.assertEqual(response.status, status)
        self.assertEqual(json.loads(response.body)["error"]["code"], code)
        self.assertEqual(self.message_count(), before, "rejection must not commit mail")

    def test_adapter_preserves_exact_lifecycle_and_member_to_member_commit(self):
        body = self.body()
        self.assertEqual(self.adapter._team_message_body(self.request(body)), body)
        with mock.patch.object(self.store, "create_team_message",
                               wraps=self.store.create_team_message) as create:
            response = self.adapter.forward(self.request(body))
        self.assertEqual(response.status, 200)
        self.assertEqual(create.call_args.args[2]["recipients"], body["recipients"])
        self.assertEqual(self.message_count(), 1)
        result = json.loads(response.body)["message"]
        self.assertEqual(result["body"], body["body"])
        self.assertEqual(result["recipients"][0]["id"], self.node)

    def test_unknown_recipient_fields_still_fail_before_store(self):
        for field in ("unexpected", "server_identity", "lifecycle_id"):
            with self.subTest(field=field):
                body = self.body()
                body["recipients"][0][field] = "not-authorized"
                with mock.patch.object(self.store, "create_team_message") as create:
                    self.assert_rejected(body, code="invalid_request", status=422)
                create.assert_not_called()

    def test_original_unbound_and_explicit_null_recipients_still_work(self):
        for recipient in ({"kind": "server", "id": self.node},
                          {"kind": "server", "id": self.node, "mail_route_lifecycle_id": None}):
            with self.subTest(recipient=recipient):
                body = self.body(recipient)
                self.assertEqual(self.adapter._team_message_body(self.request(body)), body)
                response = self.adapter.forward(self.request(body))
                self.assertEqual(response.status, 200)
        self.assertEqual(self.message_count(), 2)

    def test_store_rejects_invalid_lifecycle_values_after_shape_validation(self):
        for value in ("", "a" * 63, "A" * 64, "g" * 64, True, 123, [], {}):
            with self.subTest(value=value):
                body = self.body()
                body["recipients"][0]["mail_route_lifecycle_id"] = value
                parsed = self.adapter._team_message_body(self.request(body))
                with self.assertRaises(HubError) as failure:
                    self.store.create_team_message(self.host, self.team, parsed)
                self.assertEqual((failure.exception.code, failure.exception.status_code),
                                 ("invalid_request", 422))
        self.assertEqual(self.message_count(), 0)

    def test_lifecycle_precondition_cannot_authorize_other_recipient_kinds(self):
        for kind, target in (("human", "fixture-human"), ("all", "all"),
                             ("all_servers", "all_servers")):
            with self.subTest(kind=kind):
                self.assert_rejected(self.body({"kind": kind, "id": target,
                    "mail_route_lifecycle_id": self.lifecycle}), code="invalid_request", status=422)

    def test_stale_lifecycle_is_not_dropped_by_adapter(self):
        body = self.body()
        body["recipients"][0]["mail_route_lifecycle_id"] = (
            "b" if self.lifecycle != "b" * 64 else "c") * 64
        self.assert_rejected(body, code="mail_route_changed", status=409)

    def test_revoked_recipient_fails_and_rejoin_requires_fresh_lifecycle(self):
        old_body = self.body()
        self.store.revoke_secure_peer_service(peer_id=self.recipient.peer_id, team_id=self.team)
        self.assert_rejected(old_body, code="mail_route_changed", status=409)
        _, replacement_node = self.add_peer(self.recipient.peer_server_identity)
        self.assertEqual(replacement_node, self.node)
        current = self.store.get_network_server(
            self.host, self.team, self.node)["server"]["mail_route_lifecycle_id"]
        self.assertNotEqual(current, self.lifecycle)
        self.assert_rejected(old_body, code="mail_route_changed", status=409)
        fresh = self.body()
        fresh["recipients"][0]["mail_route_lifecycle_id"] = current
        self.assertEqual(self.adapter.forward(self.request(fresh)).status, 200)
        self.assertEqual(self.message_count(), 1)


if __name__ == "__main__":
    unittest.main()
