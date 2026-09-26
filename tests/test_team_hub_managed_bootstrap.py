import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from agentsdock_team_hub.store import HubError, HubStore


class ManagedNetworkBootstrapTests(unittest.TestCase):
    def test_all_servers_mail_has_independent_offline_inboxes_and_stable_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = HubStore(Path(temporary) / "hub", managed_host_identity="server-broadcast-host")
            store.bootstrap_managed_network("Research")
            host = store.managed_server_claims()
            team = host.team_id

            def add_peer(name):
                peer_id = str(uuid.uuid4())
                identity = "server-broadcast-" + name
                store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
                    team_id=team, display_name=name)
                return store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
                    team_id=team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
                    expires_at=int(time.time()) + 3600)

            offline = add_peer("offline")
            retired = add_peer("retired")
            store.revoke_secure_peer_service(peer_id=retired.peer_id, team_id=team)
            roster = store.get_network(host, team)["servers"]
            self.assertEqual(len(roster), 2)
            self.assertEqual(next(row for row in roster if row["display_name"] == "offline")["status"], "offline")
            bulletin = store.create_team_message(host, team, {
                "kind": "message", "body": "Shared board only", "recipients": [{"kind": "all"}],
                "idempotency_key": "historical-bulletin"})["message"]
            payload = {"kind": "message", "body": "Mail for every server", "recipients": [{"kind": "all_servers"}],
                "idempotency_key": "all-servers-mail"}
            mail = store.create_team_message(host, team, payload)["message"]
            self.assertEqual(mail["destination"], "all_servers")
            self.assertEqual({row["id"] for row in mail["recipients"]}, {row["id"] for row in roster})
            self.assertTrue(all(row["kind"] == "server" for row in mail["recipients"]))
            self.assertNotIn("destination", bulletin)
            store.record_secure_peer_heartbeat(offline.peer_id, team)
            for caller in (host, offline):
                self.assertEqual([row["id"] for row in store.list_team_messages(caller, team, box="feed")["messages"]], [bulletin["id"]])
                self.assertEqual([row["id"] for row in store.list_team_messages(caller, team, box="inbox")["messages"]], [mail["id"]])
            address = store.list_team_messages(offline, team, box="inbox")["address"]
            store.record_team_message_receipt(offline, team, mail["id"], {
                "state": "read", "address_kind": "server", "address_id": address["id"], "idempotency_key": "offline-read"})
            store.dismiss_team_message(offline, team, mail["id"], {
                "address_kind": "server", "address_id": address["id"], "idempotency_key": "offline-dismiss"})
            self.assertEqual(store.list_team_messages(offline, team, box="inbox")["messages"], [])
            self.assertEqual(store.list_team_messages(host, team, box="inbox", unread=True)["messages"][0]["id"], mail["id"])
            newcomer = add_peer("newcomer")
            store.record_secure_peer_heartbeat(newcomer.peer_id, team)
            self.assertEqual(store.create_team_message(host, team, payload)["message"], mail)
            self.assertEqual(store.list_team_messages(newcomer, team, box="inbox")["messages"], [])
            with self.assertRaises(HubError):
                store.create_team_message(host, team, {**payload, "recipients": [{"kind": "all_servers"}, {"kind": "all"}]})

    def test_creates_server_session_without_human_accounts_and_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "hub"
            store = HubStore(directory, managed_host_identity="server-bootstrap-test", managed_host_display_name="Studio")
            store.bootstrap_managed_network("Research")
            claims = store.managed_server_claims()
            self.assertEqual(store.session_snapshot(claims)["principal"]["kind"], "service")
            projection = store.get_network(claims, claims.team_id)
            self.assertEqual(projection["network"]["display_name"], "Research")
            self.assertEqual(projection["servers"][0]["recipient_display_name"], "Studio")
            connection = store.connect()
            try:
                for table in ("human_accounts", "device_sessions", "refresh_tokens"):
                    self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            finally:
                connection.close()
            store.bootstrap_managed_network("Research")
            with self.assertRaises(HubError):
                store.bootstrap_managed_network("Another network")
            reopened = HubStore(directory, managed_host_identity="server-bootstrap-test", managed_host_display_name="Studio")
            self.assertEqual(reopened.managed_server_claims().team_id, claims.team_id)
            reopened.rename_managed_host("Lab")
            projection = reopened.get_network(reopened.managed_server_claims(), claims.team_id)
            self.assertEqual(projection["servers"][0]["display_name"], "Lab")
            self.assertEqual(projection["servers"][0]["recipient_display_name"], "Lab")

    def test_unmanaged_or_existing_human_network_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = HubStore(Path(temporary) / "unmanaged")
            with self.assertRaises(HubError):
                store.bootstrap_managed_network("Research")
            managed = HubStore(Path(temporary) / "managed", managed_host_identity="server-bootstrap-test")
            proof = managed.bootstrap_proof_path.read_text().strip()
            managed.bootstrap(proof, "owner@example.test", "Owner", "Desktop")
            with self.assertRaises(HubError):
                managed.bootstrap_managed_network("Research")
            claims = managed.managed_server_claims()
            managed.rename_managed_host("Renamed legacy host")
            server = managed.get_network(claims, claims.team_id)["servers"][0]
            self.assertEqual(server["recipient_display_name"], "Renamed legacy host")
            self.assertEqual(managed.get_network_server(claims, claims.team_id, server["id"])["server"]["recipient_display_name"], "Renamed legacy host")

    def test_ordinary_services_cannot_become_team_owners(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = HubStore(Path(temporary) / "hub", managed_host_identity="server-bootstrap-test")
            store.bootstrap_managed_network("Research")
            connection = store.connect()
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE memberships SET role='admin' WHERE principal_id='service_managed_server'")
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute("UPDATE memberships SET role='owner' WHERE principal_id='service_managed_server'")
            finally:
                connection.close()
