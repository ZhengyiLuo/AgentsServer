import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentsdock_team_hub.store import HubError, HubStore


class ManagedNetworkBootstrapTests(unittest.TestCase):
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
