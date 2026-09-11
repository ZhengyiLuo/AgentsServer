"""Bounded in-process Hub checks; no AgentsServer import or live state."""
import json
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid

from agentsdock_team_hub.secure_peer import PeerAuthorization, SecurePeerError, sanitize_proxy_request
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from agentsdock_team_hub.store import HubError, HubStore


class TeamMailThreadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mail-thread-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="thread-host")
        self.store.bootstrap_managed_network("Thread tests")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.alice, self.alice_node = self.peer("Alice")
        self.bob, self.bob_node = self.peer("Bob")
        self.host_node = next(row["id"] for row in self.store.get_network(self.host, self.team)["servers"]
            if row["server_identity"] == "thread-host")

    def peer(self, name):
        peer_id = str(uuid.uuid4())
        identity = "thread-peer-" + name
        self.store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, display_name=name)
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        claims = self.store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600)
        node = next(row["id"] for row in self.store.get_network(self.host, self.team)["servers"]
            if row["display_name"] == name)
        return claims, node

    def send(self, caller, nodes, parent=None, **extra):
        return self.store.create_team_message(caller, self.team, {
            "kind": "message", "title": "Repeated subject", "body": "Repeated message body",
            "recipients": [{"kind": "server", "id": node} for node in nodes],
            "in_reply_to_message_id": parent,
            "idempotency_key": "thread-" + uuid.uuid4().hex, **extra,
        })["message"]

    def thread(self, caller, message, **query):
        return self.store.get_team_message_thread(caller, self.team, message["id"], **query)

    def test_exact_parent_pages_include_incoming_outgoing_not_same_subject(self):
        original = self.send(self.host, [self.alice_node])
        reply = self.send(self.alice, [self.host_node], original["id"], title="Different subject")
        last = self.send(self.host, [self.alice_node], reply["id"])
        self.send(self.alice, [self.host_node])
        with mock.patch.object(self.store, "_team_message_public", wraps=self.store._team_message_public) as project:
            first = self.thread(self.host, reply, limit=2)
            self.assertEqual(project.call_count, 2)  # Never materialize the whole graph's bodies.
        self.assertEqual(first["root_message_id"], original["id"])
        self.assertEqual(first["anchor_message_id"], reply["id"])
        self.assertEqual([row["id"] for row in first["messages"]], [original["id"], reply["id"]])
        self.assertEqual(first["messages"][1]["title"], "Different subject")
        self.assertIn("body", first["messages"][0])
        self.assertIn("mailbox_state", first["messages"][1])
        self.assertTrue(first["has_more"])
        rest = self.thread(self.host, reply, limit=2, after_sequence=first["next_after_sequence"])
        self.assertEqual([row["id"] for row in rest["messages"]], [last["id"]])
        self.assertFalse(rest["has_more"])
        self.assertFalse(rest["truncated"])

    def test_fanout_private_branches_are_independently_hidden(self):
        original = self.send(self.host, [self.alice_node, self.bob_node])
        alice_reply = self.send(self.alice, [self.host_node], original["id"])
        bob_reply = self.send(self.bob, [self.host_node], original["id"])
        host_private = self.send(self.host, [self.bob_node], bob_reply["id"])
        alice_page = self.thread(self.alice, original)
        self.assertEqual([row["id"] for row in alice_page["messages"]], [original["id"], alice_reply["id"]])
        self.assertNotIn(bob_reply["id"], json.dumps(alice_page))
        self.assertNotIn(host_private["id"], json.dumps(alice_page))
        self.assertEqual(len(self.thread(self.host, original)["messages"]), 4)
        with self.assertRaises(HubError) as caught:
            self.thread(self.alice, bob_reply)
        self.assertEqual(caught.exception.status_code, 404)

    def test_deleted_or_unreadable_parent_is_an_explicit_available_history_boundary(self):
        hidden = self.send(self.host, [self.alice_node])
        visible = self.send(self.host, [self.bob_node], hidden["id"])
        page = self.thread(self.bob, visible)
        self.assertEqual(page["root_message_id"], visible["id"])
        self.assertTrue(page["truncated"])
        self.store.delete_team_message(self.host, self.team, hidden["id"], {"idempotency_key": "delete-thread-parent"})
        page = self.thread(self.host, visible)
        self.assertEqual([row["id"] for row in page["messages"]], [visible["id"]])
        self.assertTrue(page["truncated"])
        with self.assertRaises(HubError) as caught:
            self.thread(self.host, hidden)
        self.assertEqual(caught.exception.status_code, 404)

    def test_graph_depth_cycle_and_response_budgets_are_explicit(self):
        chain = [self.send(self.host, [self.alice_node])]
        for _ in range(4):
            chain.append(self.send(self.host, [self.alice_node], chain[-1]["id"]))
        with mock.patch("agentsdock_team_hub.store.MAX_TEAM_MAIL_THREAD_ANCESTORS", 2):
            page = self.thread(self.host, chain[-1])
            self.assertTrue(page["truncated"])
            self.assertEqual(page["root_message_id"], chain[-2]["id"])
        with mock.patch("agentsdock_team_hub.store.MAX_TEAM_MAIL_THREAD_ITEMS", 3):
            page = self.thread(self.host, chain[0])
            self.assertTrue(page["truncated"])
            self.assertEqual(len(page["messages"]), 3)
        first_size = len(json.dumps(self.thread(self.host, chain[0], limit=1)).encode())
        with mock.patch("agentsdock_team_hub.store.MAX_TEAM_MAIL_THREAD_RESPONSE_BYTES", first_size + 80):
            page = self.thread(self.host, chain[0])
            self.assertEqual(len(page["messages"]), 1)
            self.assertTrue(page["has_more"])
            self.assertEqual(page["next_after_sequence"], chain[0]["sequence"])
        # Corrupt only this disposable fixture; production parent links are immutable.
        with closing(self.store.connect()) as db:
            db.execute("DROP TRIGGER team_messages_are_immutable")
            db.execute("UPDATE team_messages SET in_reply_to_message_id=? WHERE id=?", (chain[-1]["id"], chain[0]["id"]))
        page = self.thread(self.host, chain[0])
        self.assertTrue(page["truncated"])
        self.assertEqual(len({row["id"] for row in page["messages"]}), 5)

    def test_bulletin_and_skill_are_not_mail_and_stop_at_the_available_boundary(self):
        for extra in ({}, {"kind": "skill", "skill": {"slug": "thread-skill"}}):
            bulletin = self.send(self.host, [], recipients=[{"kind": "all"}], **extra)
            with self.assertRaises(HubError) as caught:
                self.thread(self.host, bulletin)
            self.assertEqual(caught.exception.status_code, 404)
            reply = self.send(self.host, [self.alice_node], bulletin["id"])
            page = self.thread(self.host, reply)
            self.assertEqual(page["root_message_id"], reply["id"])
            self.assertEqual([row["id"] for row in page["messages"]], [reply["id"]])
            self.assertTrue(page["truncated"])
        ordinary = self.send(self.host, [self.alice_node])
        self.send(self.host, [], ordinary["id"], recipients=[{"kind": "all"}])
        self.assertEqual([row["id"] for row in self.thread(self.host, ordinary)["messages"]], [ordinary["id"]])

    def test_parent_lookup_uses_index_capability_and_query_bounds(self):
        caps = self.store.health()["capabilities"]
        self.assertEqual(caps["team_mail_threads_v1"], {"available": True, "version": 1,
            "max_page_items": 25, "max_thread_items": 2048})
        with closing(self.store.connect()) as db:
            plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM team_messages WHERE team_id=? AND in_reply_to_message_id=? ORDER BY queue_ordinal LIMIT 10", (self.team, "test-parent")).fetchall()
        self.assertTrue(any("team_messages_parent_order" in str(row[3]) for row in plan))
        original = self.send(self.host, [self.alice_node])
        for query in ({"limit": 26}, {"limit": True}, {"after_sequence": -1}, {"after_sequence": 2**63}):
            with self.subTest(query=query), self.assertRaises(HubError):
                self.thread(self.host, original, **query)


class TeamMailThreadPeerTests(unittest.TestCase):
    def test_exact_read_route_query_and_adapter_are_supported(self):
        peer = PeerAuthorization(str(uuid.uuid4()), str(uuid.uuid4()), "thread-peer-server", "thread-team-001",
            frozenset({"teamspace.read"}), "sha256:" + "a" * 64, int(time.time()) + 600, "Thread peer")
        path = f"/v1/teams/{peer.team_id}/network/messages/message_thread_001/thread"
        store = mock.Mock()
        store.get_team_message_thread.return_value = {"messages": []}
        adapter = SecurePeerHubAdapter(store)
        with mock.patch.object(adapter, "_claims", return_value=object()):
            request = sanitize_proxy_request(peer, "GET", path, "after_sequence=3&limit=2", (), b"")
            result = adapter.forward(request)
        self.assertEqual(result.status, 200)
        self.assertEqual(store.get_team_message_thread.call_args.kwargs, {"after_sequence": 3, "limit": 2})
        for query in ("limit=26", "limit=01", "after_sequence=-1", "after_sequence=9223372036854775808",
                      "limit=1&limit=2", "include_mail_subject=true", "unknown=1"):
            with self.subTest(query=query), self.assertRaises(SecurePeerError):
                sanitize_proxy_request(peer, "GET", path, query, (), b"")
        for method in ("POST", "DELETE"):
            with self.subTest(method=method), self.assertRaises(SecurePeerError):
                sanitize_proxy_request(peer, method, path, "", (("content-type", "application/json"),), b"{}")


class TeamMailThreadServiceTests(unittest.TestCase):
    def test_http_route_requires_auth_and_forwards_bounded_query(self):
        from fastapi.testclient import TestClient
        from agentsdock_team_hub.service import create_app

        with tempfile.TemporaryDirectory(prefix="mail-thread-http-") as directory:
            root = Path(directory)
            app = create_app(root, allowed_hosts={"localhost"})
            with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 41000)) as client:
                owner = client.post("/v1/bootstrap/redeem",
                    headers={"X-Team-Hub-Bootstrap-Proof": (root / "bootstrap-owner.proof").read_text().strip()},
                    json={"email": "thread@example.com", "display_name": "Thread tests", "device_label": "Isolated tests"})
                self.assertEqual(owner.status_code, 200)
                auth = {"Authorization": "Bearer " + owner.json()["access_token"]}
                team = owner.json()["teams"][0]["id"]
                path = f"/v1/teams/{team}/network/messages/message_thread_001/thread"
                with mock.patch.object(HubStore, "get_team_message_thread", return_value={"messages": []}) as read:
                    self.assertEqual(client.get(path).status_code, 401)
                    self.assertEqual(client.get(path + "?limit=26", headers=auth).status_code, 422)
                    read.assert_not_called()
                    self.assertEqual(client.get(path + "?after_sequence=3&limit=2", headers=auth).status_code, 200)
                    self.assertEqual(read.call_args.kwargs, {"after_sequence": 3, "limit": 2})


if __name__ == "__main__":
    unittest.main()
