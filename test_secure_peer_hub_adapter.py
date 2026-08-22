import json
from collections import deque
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from agentsdock_team_hub.secure_peer import PeerAuthorization, ProxyRequest
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from agentsdock_team_hub.store import HubStore


class SecurePeerHubAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = HubStore(self.root)
        proof = (self.root / "bootstrap-owner.proof").read_text().strip()
        bootstrap = self.store.bootstrap(
            proof,
            "owner@example.com",
            "Owner",
            "Owner Mac",
        )
        self.team_id = bootstrap["teams"][0]["id"]
        owner = self.store.verify_access(bootstrap["access_token"])
        channel = self.store.create_channel(
            owner,
            self.team_id,
            {
                "kind": "board",
                "visibility": "team",
                "slug": "shared",
                "display_name": "Shared",
                "participant_principal_ids": [],
                "idempotency_key": "channel-create-1",
            },
        )["channel"]
        self.channel_id = channel["id"]
        self.peer_id = str(uuid.uuid4())
        self.peer = PeerAuthorization(
            self.peer_id,
            str(uuid.uuid4()),
            "peer-server-identity",
            self.team_id,
            frozenset({"teamspace.read", "teamspace.write"}),
            "sha256:" + "a" * 64,
            int(time.time()) + 600,
            "Paired server",
        )
        self.adapter = SecurePeerHubAdapter(self.store)
        self.adapter.provision_peer(
            {
                "peer_id": self.peer_id,
                "peer_server_identity": self.peer.peer_server_identity,
                "team_id": self.team_id,
            },
            display_name=self.peer.peer_display_name,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        body: dict | None = None,
    ):
        return self.adapter.forward(
            ProxyRequest(
                method,
                path,
                query,
                (),
                b"" if body is None else json.dumps(body).encode(),
                self.peer,
            )
        )

    def test_peer_session_and_team_are_service_scoped(self) -> None:
        session = self.request("GET", "/v1/peer-session")
        self.assertEqual(session.status, 200)
        value = json.loads(session.body)
        self.assertEqual(value["principal"]["id"], "service_secure_peer_" + self.peer_id.replace("-", ""))
        self.assertIsNone(value["principal"]["email"])
        self.assertEqual(value["principal"]["kind"], "service")
        self.assertEqual(len(value["teams"]), 1)
        self.assertEqual(value["teams"][0]["id"], self.team_id)
        self.assertEqual(value["teams"][0]["role"], "automation")
        self.assertEqual(value["teams"][0]["status"], "active")

    def test_message_round_trip_uses_no_bearer_and_rejects_extra_fields(self) -> None:
        created = self.request(
            "POST",
            f"/v1/channels/{self.channel_id}/messages",
            body={
                "body": "hello over mTLS",
                "body_format": "plain",
                "kind": "post",
                "idempotency_key": "message-request-1",
            },
        )
        self.assertEqual(created.status, 200, created.body)
        listed = self.request(
            "GET",
            f"/v1/channels/{self.channel_id}/messages",
            query="limit=20",
        )
        self.assertEqual(listed.status, 200, listed.body)
        self.assertEqual(json.loads(listed.body)["messages"][0]["body"], "hello over mTLS")

        malformed = self.request(
            "POST",
            f"/v1/channels/{self.channel_id}/messages",
            body={
                "body": "not accepted",
                "idempotency_key": "message-request-2",
                "refresh_token": "must-never-cross-this-boundary",
            },
        )
        self.assertEqual(malformed.status, 422)

    def test_revocation_is_checked_again_for_every_request(self) -> None:
        self.adapter.revoke_peer(peer_id=self.peer_id, team_id=self.team_id)
        denied = self.request("GET", "/v1/teams")
        self.assertEqual(denied.status, 401)
        self.assertEqual(json.loads(denied.body)["error"]["code"], "authentication_required")

    def test_authenticated_peer_rate_and_concurrency_limits_fail_closed(self) -> None:
        now = time.monotonic()
        self.adapter._rate_events[(self.peer_id, "all")] = deque([now] * 240)
        limited = self.request("GET", "/v1/teams")
        self.assertEqual(limited.status, 429)
        self.adapter._rate_events.clear()
        self.adapter._in_flight[self.peer_id] = 4
        concurrent = self.request("GET", "/v1/teams")
        self.assertEqual(concurrent.status, 429)

    def test_secure_peer_message_quota_is_durable(self) -> None:
        connection = self.store.connect()
        try:
            connection.execute(
                """
                INSERT INTO rate_limit_buckets(
                    team_id,subject_key,action,window_started_at,count,updated_at
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    self.team_id,
                    f"secure-peer:{self.peer_id}",
                    "peer.message.count.minute",
                    int(time.time()),
                    60,
                    int(time.time()),
                ),
            )
        finally:
            connection.close()
        denied = self.request(
            "POST",
            f"/v1/channels/{self.channel_id}/messages",
            body={"body": "bounded", "idempotency_key": "message-quota-1"},
        )
        self.assertEqual(denied.status, 429, denied.body)
        self.assertEqual(json.loads(denied.body)["error"]["code"], "rate_limited")


if __name__ == "__main__":
    unittest.main()
