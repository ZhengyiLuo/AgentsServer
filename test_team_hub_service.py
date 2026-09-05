from __future__ import annotations

import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from agentsdock_team_hub.service import create_app
from agentsdock_team_hub.store import HubError


class TeamHubServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data_dir = Path(self.temporary.name)
        self.app = create_app(self.data_dir, allowed_hosts={"testserver", "localhost"})
        self.client = TestClient(
            self.app,
            base_url="http://localhost",
            client=("127.0.0.1", 41000),
        )
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def bootstrap(self) -> dict:
        proof = (self.data_dir / "bootstrap-owner.proof").read_text().strip()
        response = self.client.post(
            "/v1/bootstrap/redeem",
            headers={"X-Team-Hub-Bootstrap-Proof": proof},
            json={
                "email": "owner@example.com",
                "display_name": "Owner",
                "device_label": "Owner Mac",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    @staticmethod
    def auth(bundle: dict) -> dict[str, str]:
        return {"Authorization": f"Bearer {bundle['access_token']}"}

    def invite_and_redeem(self, owner: dict, email: str, role: str) -> dict:
        team_id = owner["teams"][0]["id"]
        issued = self.client.post(
            f"/v1/teams/{team_id}/invitations",
            headers=self.auth(owner),
            json={"invitee_email": email, "role": role},
        )
        self.assertEqual(issued.status_code, 200, issued.text)
        redeemed = self.client.post(
            "/v1/invitations/redeem",
            json={
                "token": issued.json()["token"],
                "email": email,
                "display_name": email.split("@", 1)[0],
                "device_label": f"{role} device",
            },
        )
        self.assertEqual(redeemed.status_code, 200, redeemed.text)
        return redeemed.json()

    def test_health_has_stable_hub_identity_and_no_bootstrap_secret(self) -> None:
        first = self.client.get("/v1/health").json()
        self.assertTrue(first["bootstrap_required"])
        self.assertNotIn("proof", " ".join(first.keys()).lower())
        second_app = create_app(self.data_dir, allowed_hosts={"testserver"})
        second = TestClient(second_app).get("/v1/health").json()
        self.assertEqual(second["hub_id"], first["hub_id"])
        self.assertNotEqual(second["instance_id"], first["instance_id"])
        with tempfile.TemporaryDirectory() as other:
            other_app = create_app(other, allowed_hosts={"testserver"})
            other_health = TestClient(other_app).get("/v1/health").json()
        self.assertNotEqual(other_health["hub_id"], first["hub_id"])

    def test_bootstrap_requires_actual_loopback_and_one_exact_proof_header(self) -> None:
        proof = (self.data_dir / "bootstrap-owner.proof").read_text().strip()
        remote = TestClient(self.app, client=("192.0.2.8", 41000))
        denied = remote.post(
            "/v1/bootstrap/redeem",
            headers={
                "Host": "localhost",
                "X-Forwarded-For": "127.0.0.1",
                "X-Team-Hub-Bootstrap-Proof": proof,
            },
            json={"email": "owner@example.com", "display_name": "Owner", "device_label": "Mac"},
        )
        self.assertEqual(denied.status_code, 403)
        proxied_app = create_app(
            Path(self.temporary.name) / "proxied",
            allowed_hosts={"localhost", "hub.example.test"},
        )
        proxied_proof = (
            Path(self.temporary.name) / "proxied" / "bootstrap-owner.proof"
        ).read_text().strip()
        proxied = TestClient(
            proxied_app,
            base_url="http://hub.example.test",
            client=("127.0.0.1", 41001),
        ).post(
            "/v1/bootstrap/redeem",
            headers={"X-Team-Hub-Bootstrap-Proof": proxied_proof},
            json={"email": "owner@example.com", "display_name": "Owner", "device_label": "Mac"},
        )
        self.assertEqual(proxied.status_code, 403)
        malformed = self.client.post(
            "/v1/bootstrap/redeem",
            headers={"X-Team-Hub-Bootstrap-Proof": "short"},
            json={
                "email": "owner@example.com",
                "display_name": "Owner",
                "device_label": "Mac",
            },
        )
        self.assertEqual(malformed.status_code, 403)
        self.assertEqual(
            malformed.json()["error"]["code"],
            "bootstrap_unavailable",
        )
        duplicate = self.client.post(
            "/v1/bootstrap/redeem",
            headers=[
                ("X-Team-Hub-Bootstrap-Proof", proof),
                ("X-Team-Hub-Bootstrap-Proof", proof),
            ],
            json={"email": "owner@example.com", "display_name": "Owner", "device_label": "Mac"},
        )
        self.assertEqual(duplicate.status_code, 403)
        malformed_host = self.client.get("/v1/health", headers={"Host": "[::1]evil"})
        self.assertEqual(malformed_host.status_code, 400)

    def test_unknown_post_paths_share_a_bounded_rate_bucket(self) -> None:
        last = None
        for index in range(121):
            last = self.client.post(f"/unknown/{index}", json={})
        assert last is not None
        self.assertEqual(last.status_code, 429)
        limiter = self.app.state.rate_limiter
        for index in range(5000):
            limiter.allow(f"peer-{index}", f"action-{index}", 1)
        self.assertLessEqual(len(limiter._buckets), 4096)

    def test_refresh_rotation_replay_revokes_entire_session(self) -> None:
        owner = self.bootstrap()
        rotated = self.client.post(
            "/v1/sessions/refresh", json={"refresh_token": owner["refresh_token"]}
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        replay = self.client.post(
            "/v1/sessions/refresh", json={"refresh_token": owner["refresh_token"]}
        )
        self.assertEqual(replay.status_code, 401)
        rejected = self.client.get("/v1/session", headers=self.auth(rotated.json()))
        self.assertEqual(rejected.status_code, 401)
        connection = self.app.state.store.connect()
        try:
            row = connection.execute(
                "SELECT revoked_at FROM device_sessions WHERE id = ?",
                (owner["session"]["id"],),
            ).fetchone()
            self.assertIsNotNone(row["revoked_at"])
        finally:
            connection.close()

    def test_existing_email_invite_cannot_mint_session_and_authenticated_accepts(self) -> None:
        owner = self.bootstrap()
        member = self.invite_and_redeem(owner, "member@example.com", "member")
        team_id = owner["teams"][0]["id"]
        issued = self.client.post(
            f"/v1/teams/{team_id}/invitations",
            headers=self.auth(owner),
            json={"invitee_email": "member@example.com", "role": "member"},
        ).json()
        before = self.app.state.store.connect()
        try:
            session_count = before.execute("SELECT count(*) FROM device_sessions").fetchone()[0]
        finally:
            before.close()
        impersonation = self.client.post(
            "/v1/invitations/redeem",
            json={
                "token": issued["token"],
                "email": "member@example.com",
                "display_name": "Attacker",
                "device_label": "Attacker device",
            },
        )
        self.assertEqual(impersonation.status_code, 409)
        accepted = self.client.post(
            "/v1/invitations/accept",
            headers=self.auth(member),
            json={"token": issued["token"]},
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        after = self.app.state.store.connect()
        try:
            self.assertEqual(
                after.execute("SELECT count(*) FROM device_sessions").fetchone()[0],
                session_count,
            )
        finally:
            after.close()

    def test_local_owner_recovery_restores_same_principal_after_logout(self) -> None:
        owner = self.bootstrap()
        revoked = self.client.post(
            "/v1/sessions/revoke",
            headers=self.auth(owner),
            json={"refresh_token": owner["refresh_token"]},
        )
        self.assertEqual(revoked.status_code, 200)
        proof_path = self.app.state.store.issue_owner_recovery(
            "owner@example.com", "Recovered Mac"
        )
        recovery = self.client.post(
            "/v1/owner-recovery/redeem",
            headers={"X-Team-Hub-Owner-Recovery-Proof": proof_path.read_text().strip()},
            json={"device_label": "Recovered Mac"},
        )
        self.assertEqual(recovery.status_code, 200, recovery.text)
        recovered = recovery.json()
        self.assertEqual(recovered["principal"]["id"], owner["principal"]["id"])
        self.assertEqual(recovered["teams"], owner["teams"])
        replay = self.client.post(
            "/v1/owner-recovery/redeem",
            headers={"X-Team-Hub-Owner-Recovery-Proof": "owner-recovery.invalid-invalid"},
            json={"device_label": "Recovered Mac"},
        )
        self.assertEqual(replay.status_code, 403)

    def test_member_device_recovery_allows_loopback_or_direct_https_only(self) -> None:
        owner = self.bootstrap()
        member = self.invite_and_redeem(owner, "member@example.com", "member")
        self.assertEqual(
            self.client.get("/v1/session", headers=self.auth(member)).status_code,
            200,
        )
        proof_path = self.app.state.store.issue_device_recovery(
            "member@example.com", "Replacement Mac"
        )
        proof = proof_path.read_text().strip()
        # Issuance is the host operator's lost-device action. It invalidates
        # existing access and refresh authority before the proof is delivered.
        self.assertEqual(
            self.client.get("/v1/session", headers=self.auth(member)).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/v1/sessions/refresh",
                json={"refresh_token": member["refresh_token"]},
            ).status_code,
            401,
        )
        remote_http = TestClient(
            self.app, base_url="http://testserver", client=("192.0.2.9", 41000)
        )
        forwarded = remote_http.post(
            "/v1/device-recovery/redeem",
            headers={
                "X-Team-Hub-Device-Recovery-Proof": proof,
                "X-Forwarded-Proto": "https",
            },
            json={"device_label": "Replacement Mac"},
        )
        self.assertEqual(forwarded.status_code, 403)
        wrong_label = self.client.post(
            "/v1/device-recovery/redeem",
            headers={"X-Team-Hub-Device-Recovery-Proof": proof},
            json={"device_label": "Wrong Mac"},
        )
        self.assertEqual(wrong_label.status_code, 403)
        remote_https = TestClient(
            self.app, base_url="https://testserver", client=("192.0.2.9", 41000)
        )
        recovered = remote_https.post(
            "/v1/device-recovery/redeem",
            headers={"X-Team-Hub-Device-Recovery-Proof": proof},
            json={"device_label": "Replacement Mac"},
        )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        replacement = recovered.json()
        self.assertEqual(replacement["principal"]["id"], member["principal"]["id"])
        self.assertEqual(replacement["teams"], member["teams"])
        self.assertEqual(
            self.client.get("/v1/session", headers=self.auth(member)).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/v1/sessions/refresh",
                json={"refresh_token": member["refresh_token"]},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/v1/session", headers=self.auth(replacement)).status_code,
            200,
        )
        replay = remote_https.post(
            "/v1/device-recovery/redeem",
            headers={"X-Team-Hub-Device-Recovery-Proof": proof},
            json={"device_label": "Replacement Mac"},
        )
        self.assertEqual(replay.status_code, 403)

        connection = self.app.state.store.connect()
        try:
            events = connection.execute(
                """
                SELECT actor_principal_id, action, metadata_json
                FROM audit_events
                WHERE action IN ('device_recovery.issue', 'device_recovery.redeem')
                ORDER BY created_at, id
                """
            ).fetchall()
            self.assertEqual(len(events), 2)
            self.assertTrue(
                all(row["actor_principal_id"] == "service_local_control" for row in events)
            )
            self.assertTrue(
                all(
                    member["principal"]["id"] in row["metadata_json"]
                    for row in events
                )
            )
            issue_event = next(row for row in events if row["action"] == "device_recovery.issue")
            issue_metadata = json.loads(issue_event["metadata_json"])
            self.assertEqual(issue_metadata["revoked_session_count"], 1)
            self.assertEqual(
                connection.execute(
                    """
                    SELECT count(*) FROM device_sessions
                    WHERE human_principal_id = ? AND revoked_at IS NULL
                    """,
                    (member["principal"]["id"],),
                ).fetchone()[0],
                1,
            )
        finally:
            connection.close()

    def test_device_recovery_issue_failure_rolls_back_revocation(self) -> None:
        owner = self.bootstrap()
        member = self.invite_and_redeem(owner, "member@example.com", "member")
        store = self.app.state.store
        proof_files_before = set(self.data_dir.glob("owner_recovery_*.proof"))

        with mock.patch.object(store, "_audit", side_effect=OSError("audit unavailable")):
            with self.assertRaisesRegex(OSError, "audit unavailable"):
                store.issue_device_recovery("member@example.com", "Replacement Mac")

        self.assertEqual(
            self.client.get("/v1/session", headers=self.auth(member)).status_code,
            200,
        )
        refreshed = self.client.post(
            "/v1/sessions/refresh",
            json={"refresh_token": member["refresh_token"]},
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        self.assertEqual(
            set(self.data_dir.glob("owner_recovery_*.proof")),
            proof_files_before,
        )

    def test_device_recovery_expiry_suspension_and_concurrent_consumption(self) -> None:
        owner = self.bootstrap()
        member = self.invite_and_redeem(owner, "member@example.com", "member")
        store = self.app.state.store
        proof_path = store.issue_device_recovery("member@example.com", "Concurrent Mac")
        proof = proof_path.read_text().strip()

        def redeem(_: int) -> str:
            try:
                store.redeem_device_recovery(proof, "Concurrent Mac")
                return "accepted"
            except HubError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(redeem, range(2))), ["accepted", "rejected"])

        expired_path = store.issue_device_recovery("member@example.com", "Expired Mac")
        expired = expired_path.read_text().strip()
        with mock.patch(
            "agentsdock_team_hub.store._now", return_value=int(time.time()) + 601
        ):
            with self.assertRaises(HubError):
                store.redeem_device_recovery(expired, "Expired Mac")

        connection = store.connect()
        try:
            connection.execute(
                """
                UPDATE memberships SET status = 'suspended', updated_at = updated_at + 1
                WHERE principal_id = ?
                """,
                (member["principal"]["id"],),
            )
        finally:
            connection.close()
        with self.assertRaises(HubError):
            store.issue_device_recovery("member@example.com", "Suspended Mac")

    def test_node_enrollment_requires_bound_key_pop_and_is_one_time(self) -> None:
        owner = self.bootstrap()
        team_id = owner["teams"][0]["id"]
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        ).decode("ascii")
        wrong_key = Ed25519PrivateKey.generate()
        wrong_public = wrong_key.public_key().public_bytes(
            serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH
        ).decode("ascii")
        issued = self.client.post(
            f"/v1/teams/{team_id}/node-enrollments",
            headers=self.auth(owner),
            json={
                "server_identity": "server:1234567890abcdef",
                "display_name": "Primary node",
                "public_key": public_key,
            },
        )
        self.assertEqual(issued.status_code, 200, issued.text)
        token = issued.json()["token"]
        wrong = self.client.post(
            "/v1/node-enrollments/challenge",
            json={
                "token": token,
                "server_identity": "server:1234567890abcdef",
                "display_name": "Primary node",
                "public_key": wrong_public,
            },
        )
        self.assertEqual(wrong.status_code, 403)
        challenge = self.client.post(
            "/v1/node-enrollments/challenge",
            json={
                "token": token,
                "server_identity": "server:1234567890abcdef",
                "display_name": "Primary node",
                "public_key": public_key,
            },
        ).json()
        bad_signature = base64.b64encode(wrong_key.sign(challenge["signing_payload"].encode())).decode()
        denied = self.client.post(
            "/v1/node-enrollments/redeem",
            json={"challenge_id": challenge["challenge_id"], "signature": bad_signature},
        )
        self.assertEqual(denied.status_code, 403)
        signature = base64.b64encode(
            private_key.sign(challenge["signing_payload"].encode())
        ).decode()
        enrolled = self.client.post(
            "/v1/node-enrollments/redeem",
            json={"challenge_id": challenge["challenge_id"], "signature": signature},
        )
        self.assertEqual(enrolled.status_code, 200, enrolled.text)
        replay = self.client.post(
            "/v1/node-enrollments/redeem",
            json={"challenge_id": challenge["challenge_id"], "signature": signature},
        )
        self.assertEqual(replay.status_code, 403)

    def test_channel_acl_direct_privacy_passive_messages_and_idempotency(self) -> None:
        owner = self.bootstrap()
        member = self.invite_and_redeem(owner, "member@example.com", "member")
        admin = self.invite_and_redeem(owner, "admin@example.com", "admin")
        guest = self.invite_and_redeem(owner, "guest@example.com", "guest")
        team_id = owner["teams"][0]["id"]
        board_request = {
            "kind": "board",
            "visibility": "team",
            "slug": "general",
            "display_name": "General",
            "participant_principal_ids": [],
            "idempotency_key": "channel-general-1",
        }
        board = self.client.post(
            f"/v1/teams/{team_id}/channels", headers=self.auth(owner), json=board_request
        )
        self.assertEqual(board.status_code, 200, board.text)
        repeated = self.client.post(
            f"/v1/teams/{team_id}/channels", headers=self.auth(owner), json=board_request
        )
        self.assertEqual(repeated.json(), board.json())
        conflict_request = {**board_request, "display_name": "Changed"}
        conflict = self.client.post(
            f"/v1/teams/{team_id}/channels",
            headers=self.auth(owner),
            json=conflict_request,
        )
        self.assertEqual(conflict.status_code, 409)
        guest_channels = self.client.get(
            f"/v1/teams/{team_id}/channels", headers=self.auth(guest)
        ).json()["channels"]
        self.assertFalse(guest_channels[0]["permissions"]["post"])
        board_id = board.json()["channel"]["id"]
        guest_post = self.client.post(
            f"/v1/channels/{board_id}/messages",
            headers=self.auth(guest),
            json={"body": "blocked", "idempotency_key": "guest-message-1"},
        )
        self.assertEqual(guest_post.status_code, 404)
        direct = self.client.post(
            f"/v1/teams/{team_id}/channels",
            headers=self.auth(owner),
            json={
                "kind": "direct",
                "visibility": "private",
                "participant_principal_ids": [
                    owner["principal"]["id"],
                    member["principal"]["id"],
                ],
                "idempotency_key": "direct-owner-member",
            },
        )
        self.assertEqual(direct.status_code, 200, direct.text)
        direct_id = direct.json()["channel"]["id"]
        hidden = self.client.get(
            f"/v1/channels/{direct_id}/messages", headers=self.auth(admin)
        )
        self.assertEqual(hidden.status_code, 404)
        message_request = {"body": "Passive only", "idempotency_key": "message-passive-1"}
        message = self.client.post(
            f"/v1/channels/{direct_id}/messages",
            headers=self.auth(owner),
            json=message_request,
        )
        self.assertEqual(message.status_code, 200, message.text)
        same = self.client.post(
            f"/v1/channels/{direct_id}/messages",
            headers=self.auth(owner),
            json=message_request,
        )
        self.assertEqual(same.json(), message.json())
        changed = self.client.post(
            f"/v1/channels/{direct_id}/messages",
            headers=self.auth(owner),
            json={**message_request, "body": "Different"},
        )
        self.assertEqual(changed.status_code, 409)
        dispatch = self.client.post("/v1/dispatches", headers=self.auth(owner), json={})
        self.assertEqual(dispatch.status_code, 501)
        connection = self.app.state.store.connect()
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM dispatch_requests").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages").fetchone()[0], 1)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
