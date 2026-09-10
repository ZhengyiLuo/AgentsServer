"""Host/operator moderation against temporary Hub storage only."""
from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from agentsdock_team_hub.store import HubError, HubStore


def key():
    return "fixture-" + uuid.uuid4().hex


class HostContentModerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="host-content-moderation-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="host-moderation-fixture")
        owner = self.store.bootstrap(self.store.bootstrap_proof_path.read_text().strip(),
            "owner@example.test", "Fixture owner", "Fixture device")
        self.owner = self.store.verify_access(owner["access_token"])
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.agent = self.store.local_agent_mail_claims(self.team)
        self.peer_id, self.peer = self.add_peer("departed-source-fixture")
        _, self.other = self.add_peer("ordinary-peer-fixture")
        self.host_node = self.store.list_team_messages(self.host, self.team, box="inbox")["address"]["id"]

    def add_peer(self, identity):
        peer_id = str(uuid.uuid4())
        self.store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, display_name="Fixture peer")
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        return peer_id, self.store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}), expires_at=int(time.time()) + 3600)

    def resources(self):
        bulletin = self.store.create_network_bulletin_post(self.peer, self.team,
            {"body": "Fixture legacy announcement", "body_format": "plain", "idempotency_key": key()})["post"]
        result = [("bulletin", bulletin)]
        for variant in ("announcement", "skill", "mail"):
            request = {"kind": "skill" if variant == "skill" else "message", "body": "Fixture " + variant,
                "recipients": [{"kind": "server", "id": self.host_node}] if variant == "mail" else [{"kind": "all"}],
                "idempotency_key": key()}
            if variant == "skill":
                request.update(title="Fixture skill", skill={"slug": "moderation-fixture"})
            result.append(("message", self.store.create_team_message(self.peer, self.team, request)["message"]))
        return result

    def delete(self, claims, kind, resource, request=None):
        method = self.store.delete_network_bulletin_post if kind == "bulletin" else self.store.delete_team_message
        return method(claims, self.team, resource["id"], request or {"idempotency_key": key()})

    def source_snapshot(self):
        connection = self.store.connect()
        try:
            return {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
                for table in ("messages", "team_messages", "team_message_recipients", "team_skill_versions", "team_skills")}
        finally:
            connection.close()

    def test_host_deletes_departed_server_legacy_v2_skill_and_mail_additively(self):
        resources = self.resources()
        self.store.revoke_secure_peer_service(peer_id=self.peer_id, team_id=self.team)
        before = self.source_snapshot()
        # Historical authors remain readable; deletion does not need presence.
        self.assertEqual(len(self.store.list_network_bulletin(self.host, self.team, after_sequence=0, limit=50)["posts"]), 1)
        self.assertEqual(len(self.store.list_team_messages(self.host, self.team, box="feed")["messages"]), 2)
        self.assertEqual(len(self.store.list_team_messages(self.host, self.team, box="inbox")["messages"]), 1)
        for kind, resource in resources:
            request = {"idempotency_key": key()}
            accepted = self.delete(self.host, kind, resource, request)
            self.assertTrue(accepted["deleted"])
            self.assertEqual(self.delete(self.host, kind, resource, request), accepted)
            self.assertEqual(self.delete(self.host, kind, resource), accepted)
        self.assertEqual(self.source_snapshot(), before)
        self.assertEqual(self.store.list_network_bulletin(self.host, self.team, after_sequence=0, limit=50)["posts"], [])
        for box in ("feed", "inbox"):
            self.assertEqual(self.store.list_team_messages(self.host, self.team, box=box)["messages"], [])
        connection = self.store.connect()
        try:
            for kind, resource in resources:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM network_content_deletions WHERE resource_id=?", (resource["id"],)).fetchone()[0], 1)
                action = "network.bulletin.delete" if kind == "bulletin" else "team.message.delete"
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit_events WHERE resource_id=? AND action=?", (resource["id"], action)).fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM outbox_events WHERE aggregate_id=? AND event_type=?", (resource["id"], action + "d")).fetchone()[0], 1)
        finally:
            connection.close()

    def test_host_agents_and_other_peers_do_not_inherit_operator_moderation(self):
        resources = self.resources()
        self.store.revoke_secure_peer_service(peer_id=self.peer_id, team_id=self.team)
        for actor in (self.agent, self.other):
            for kind, resource in resources:
                with self.subTest(actor=actor.auth_kind, kind=resource.get("kind", kind)), self.assertRaises(HubError) as rejected:
                    self.delete(actor, kind, resource)
                self.assertEqual(rejected.exception.status_code, 403)
        skill = next(resource for _, resource in resources if resource.get("kind") == "skill")
        with self.assertRaises(HubError) as rejected:
            self.delete(self.owner, "message", skill)
        self.assertEqual(rejected.exception.status_code, 403)

    def test_posters_keep_delete_rights_without_host_moderation(self):
        for kind, resource in self.resources():
            self.assertTrue(self.delete(self.peer, kind, resource)["deleted"])

    def test_operator_claims_are_revalidated_not_inferred_from_host_location(self):
        kind, resource = self.resources()[0]
        for actor in (replace(self.agent, auth_kind="managed_server"),
                      replace(self.host, expires_at=0), replace(self.host, team_id="other-team"),
                      replace(self.host, scopes=frozenset({"teamspace.read"}))):
            with self.subTest(auth_kind=actor.auth_kind), self.assertRaises(HubError):
                self.delete(actor, kind, resource)
        self.assertTrue(self.delete(self.host, kind, resource)["deleted"])

    def test_health_negotiates_without_changing_strict_content_records(self):
        self.assertEqual(self.store.health()["capabilities"]["team_host_content_deletion_v1"], {"available": True, "version": 1})
        for _, resource in self.resources():
            self.assertNotIn("can_delete", resource)


if __name__ == "__main__":
    unittest.main()
