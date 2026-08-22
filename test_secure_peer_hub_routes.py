from pathlib import Path
import tempfile
import unittest
from unittest.mock import ANY

from fastapi.testclient import TestClient

from agentsdock_team_hub.service import create_app


class _SecurePeerManager:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def list_pairings(self, **values):
        self.calls.append(("list_pairings", values))
        return {"pairings": []}

    def approve_pairing(self, **values):
        self.calls.append(("approve_pairing", values))
        return {"pairing": {"id": values["pairing_id"], "status": "approved"}}

    def reject_pairing(self, **values):
        self.calls.append(("reject_pairing", values))
        return {"pairing": {"id": values["pairing_id"], "status": "rejected"}}

    def list_peers(self, **values):
        self.calls.append(("list_peers", values))
        return {"peers": []}

    def revoke_peer(self, **values):
        self.calls.append(("revoke_peer", values))
        return {"peer": {"id": values["peer_id"], "status": "revoked"}}


class SecurePeerHubRouteTests(unittest.TestCase):
    def _owner_client(self, root: Path, manager: _SecurePeerManager):
        application = create_app(
            root,
            secure_peer_manager=manager,
            require_loopback_transport=True,
        )
        client = TestClient(
            application,
            base_url="http://localhost",
            client=("127.0.0.1", 41000),
        )
        proof = (root / "bootstrap-owner.proof").read_text().strip()
        bootstrap = client.post(
            "/v1/bootstrap/redeem",
            headers={"X-Team-Hub-Bootstrap-Proof": proof},
            json={
                "email": "owner@example.com",
                "display_name": "Owner",
                "device_label": "Owner Mac",
            },
        )
        self.assertEqual(bootstrap.status_code, 200, bootstrap.text)
        access_token = bootstrap.json()["access_token"]
        teams = client.get(
            "/v1/teams",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(teams.status_code, 200, teams.text)
        team_id = teams.json()["teams"][0]["id"]
        return client, team_id, {"Authorization": f"Bearer {access_token}"}

    def test_pending_pairing_is_not_exposed_to_team_membership(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = _SecurePeerManager()
            client, team_id, headers = self._owner_client(
                Path(temporary) / "hub",
                manager,
            )
            listed = client.get(
                f"/v1/teams/{team_id}/peer-pairings?status=pending",
                headers=headers,
            )
            self.assertEqual(listed.status_code, 404, listed.text)
            approved = client.post(
                f"/v1/teams/{team_id}/peer-pairings/pairing-1/approve",
                headers=headers,
                json={
                    "idempotency_key": "approve-request-1",
                    "sas_confirmed": True,
                    "expected_peer_server_identity": "peer-server-identity",
                    "expected_transcript_hash": "a" * 64,
                    "scopes": ["teamspace.read", "cross_chat.request_reply"],
                },
            )
            self.assertEqual(approved.status_code, 404, approved.text)
            peers = client.get(
                f"/v1/teams/{team_id}/secure-peers",
                headers=headers,
            )
            self.assertEqual(peers.status_code, 200, peers.text)
            revoked = client.post(
                f"/v1/teams/{team_id}/secure-peers/peer-1/revoke",
                headers=headers,
                json={
                    "idempotency_key": "revoke-request-1",
                    "expected_certificate_fingerprint": "sha256:" + "b" * 64,
                },
            )
            self.assertEqual(revoked.status_code, 200, revoked.text)
            self.assertEqual(
                manager.calls,
                [
                    ("list_peers", {"team_id": team_id}),
                    (
                        "revoke_peer",
                        {
                            "peer_id": "peer-1",
                            "team_id": team_id,
                            "revoked_by": ANY,
                            "expected_certificate_fingerprint": "sha256:" + "b" * 64,
                            "idempotency_key": "revoke-request-1",
                        },
                    ),
                ],
            )

    def test_removed_pairing_routes_do_not_parse_or_dispatch_requests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = _SecurePeerManager()
            client, team_id, headers = self._owner_client(
                Path(temporary) / "hub",
                manager,
            )
            unauthenticated = client.get(
                f"/v1/teams/{team_id}/peer-pairings"
            )
            self.assertEqual(unauthenticated.status_code, 404)
            malformed = client.post(
                f"/v1/teams/{team_id}/peer-pairings/pairing-1/approve",
                headers=headers,
                json={
                    "idempotency_key": "approve-request-1",
                    "sas_confirmed": False,
                    "expected_peer_server_identity": "peer-server-identity",
                    "expected_transcript_hash": "not-a-hash",
                    "scopes": ["all"],
                    "unexpected": True,
                },
            )
            self.assertEqual(malformed.status_code, 404)
            self.assertEqual(manager.calls, [])


if __name__ == "__main__":
    unittest.main()
