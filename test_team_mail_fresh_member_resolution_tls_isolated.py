"""Fresh-member @@ admission and send over private socketpair mTLS only.

Run via public_chat_share_safe_tests.py; no server import or TCP listener.
"""
from contextlib import closing
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid

from fastapi import HTTPException
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from agentsdock_team_hub.secure_peer import SecurePeerError, build_pairing_request
import team_mail_grants as grants
from tests.test_durable_team_mail_grants_isolated import endpoint_fixture
import test_team_mail_lifecycle_pipeline_tls_isolated as lifecycle


class FreshMemberResolutionTLSAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    requested_scopes = ["teamspace.read", "teamspace.write"]
    setUp = lifecycle.MailLifecyclePipelineTLSAcceptanceTests.setUp

    async def test_first_host_mail_resolves_real_reference_admits_route_and_commits(self):
        await self.first_mail(self.host_recipient, self.host, "server_tls_fixture")

    async def test_fresh_member_to_another_approved_member_resolves_and_commits_first_mail(self):
        _peer, recipient, claims = self.approve_recipient("second_peer_fixture")
        self.assertNotEqual(recipient, self.host_recipient)
        self.assertNotEqual(recipient, self.recipient)
        await self.first_mail(recipient, claims, "second_peer_fixture")

    def approve_recipient(self, identity):
        # Issue and authenticate a second actual host-signed certificate, then
        # run the same approved-peer provisioning used by the host. No second
        # listener or runtime is needed merely to target its durable Inbox.
        key = Ed25519PrivateKey.generate()
        request = build_pairing_request(key, server_identity=identity,
            display_name="Second isolated member", host_ca_fingerprint=self.tls.store.ca_fingerprint,
            requested_scopes=self.requested_scopes)
        submitted = self.tls.store.submit_pairing(request, source_ip="100.64.0.3", source_port=40001)
        self.tls.store.approve_pairing(submitted["pairing_id"], self.team, self.requested_scopes,
            "owner_fixture", expected_peer_server_identity=identity,
            expected_transcript_hash=submitted["transcript_hash"], idempotency_key=str(uuid.uuid4()))
        approved = self.tls.store.poll_pairing(submitted["pairing_id"], submitted["poll_token"])
        certificate = x509.load_pem_x509_certificate(approved["client_certificate_pem"].encode())
        peer = self.tls.store.authenticate_peer(certificate.public_bytes(serialization.Encoding.DER))
        self.adapter.provision_peer({"peer_id": peer.peer_id,
            "peer_server_identity": peer.peer_server_identity, "team_id": self.team},
            display_name="Second isolated member")
        self.adapter.record_peer_heartbeat(peer.peer_id, self.team)
        claims = self.adapter._claims(self.hub, peer)
        recipient = self.hub.team_mail_arrival_snapshot(claims, self.team)["recipient_server_id"]
        return peer, recipient, claims

    async def first_mail(self, recipient, recipient_claims, recipient_identity):
        with closing(self.hub.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=?",
                                                (self.team,)).fetchone()[0], 0)
        self.assertTrue(self.client.get_connection(self.connection_id)["active"])
        self.assertEqual(self.tls.peer.scopes, frozenset(self.requested_scopes))
        realm = self.runtime.team_realm(self.team)
        # The native mention's exact ID and visible label come from the real
        # authenticated Hub projection, not a fabricated private route.
        target = self.runtime._team_network_server(realm, recipient)
        self.assertIsNotNone(target)
        self.assertEqual(target["status"], "active")
        reference = {"kind": "recipient", "recipient_kind": "server", "team_id": self.team,
                     "target_id": target["id"], "display_name_snapshot":
                     target.get("recipient_display_name") or target["display_name"]}
        resolved = self.runtime.resolve_team_references([reference])
        self.assertEqual(len(resolved), 1)
        self.assertIn("durable_server_binding", resolved[0])
        self.assertEqual(resolved[0]["durable_server_binding"]["server_identity"], recipient_identity)

        namespace = endpoint_fixture()
        namespace["SECURE_PEER_RUNTIME"] = self.runtime
        namespace["TeamReferenceTargetRepairRequired"] = HTTPException
        session_id = "qa-away-chat"
        session = namespace["STORE"].sessions[session_id]
        session[grants.ROUTES_KEY] = []
        mutation = await namespace["stage_provider_team_mail_grants"](
            session_id, [SimpleNamespace(**reference)],
            admission_id="grant_admission_" + uuid.uuid4().hex, event_type="turn_queued")
        self.assertIsNotNone(mutation)
        self.assertEqual(grants.live_routes(session), [], "unaccepted admission must grant nothing")
        await namespace["settle_provider_team_mail_grants"](session_id, mutation, accepted=True)
        routes = grants.live_routes(session)
        self.assertEqual(len(routes), 1)
        listing = await namespace["list_agent_team_mail_routes"](session_id)
        self.assertEqual(len(listing["routes"]), 1)
        self.assertTrue(listing["routes"][0]["available"], listing)
        self.assertEqual(listing["routes"][0]["target_id"], recipient)
        frozen = {**resolved[0], "durable_mail_grant": grants.snapshot(routes)[0]}
        generation = self.runtime.team_authority_generation()
        authorized = await namespace["resolve_provider_durable_team_reference"](
            session_id, frozen, generation)
        payload = {"kind": "message", "title": "First mail after joining", "body": "Synthetic first mail"}
        key = "fresh-member-" + uuid.uuid4().hex
        decode = self.runtime._decoded_proxy_json
        def diagnostic(response, **kwargs):
            try:
                return decode(response, **kwargs)
            except Exception as exc:
                exc.add_note(f"Actual fixture HTTP rejection: {response.status} {response.body!r}")
                raise
        with mock.patch.object(self.runtime, "_decoded_proxy_json", side_effect=diagnostic):
            result = await asyncio.to_thread(self.runtime.team_authorized_write, generation,
                self.runtime.team_send_message, authorized, payload=payload, attachment_paths=[],
                idempotency_key=key,
                provenance={"via": "agent", "chat_id": session_id})
        self.assertEqual(result["message"]["body"], payload["body"])
        with closing(self.hub.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=?",
                                                (self.team,)).fetchone()[0], 1)
        self.assertEqual(self.hub.get_team_message(recipient_claims, self.team, result["message"]["id"])
                         ["message"]["body"], payload["body"])
        replay = await asyncio.to_thread(self.runtime.team_authorized_write, generation,
            self.runtime.team_send_message, authorized, payload=payload, attachment_paths=[],
            idempotency_key=key, provenance={"via": "agent", "chat_id": session_id})
        self.assertEqual(replay["message"], result["message"])
        # The recipient's reply is committed with its actual authenticated Hub
        # claims. The fresh member reads and replies through the real TLS lane.
        incoming = self.hub.create_team_message(recipient_claims, self.team, {
            "kind": "message", "title": payload["title"], "body": "Synthetic recipient reply",
            "recipients": [{"kind": "server", "id": self.recipient}],
            "in_reply_to_message_id": result["message"]["id"],
            "idempotency_key": "recipient-reply-" + uuid.uuid4().hex})["message"]
        reply_payload = {"kind": "message", "body": "Synthetic member follow-up",
                         "in_reply_to_message_id": incoming["id"]}
        reply_key = "member-followup-" + uuid.uuid4().hex
        reply = await asyncio.to_thread(self.runtime.team_authorized_write, generation,
            self.runtime.team_send_message, authorized, payload=reply_payload, attachment_paths=[],
            idempotency_key=reply_key, provenance={"via": "agent", "chat_id": session_id})
        retry = await asyncio.to_thread(self.runtime.team_authorized_write, generation,
            self.runtime.team_send_message, authorized, payload=reply_payload, attachment_paths=[],
            idempotency_key=reply_key, provenance={"via": "agent", "chat_id": session_id})
        self.assertEqual(retry["message"], reply["message"])
        self.assertEqual(reply["message"]["in_reply_to_message_id"], incoming["id"])
        self.assertEqual(reply["message"]["title"], payload["title"])
        thread = self.hub.get_team_message_thread(recipient_claims, self.team, result["message"]["id"])
        self.assertEqual([item["id"] for item in thread["messages"]],
                         [result["message"]["id"], incoming["id"], reply["message"]["id"]])
        with closing(self.hub.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=?",
                                                (self.team,)).fetchone()[0], 3)
        self.tls.assert_settled()

    async def test_authenticated_gateway_rejects_invalid_or_stale_lifecycle_without_mail(self):
        realm = self.runtime.team_realm(self.team)
        target = self.runtime._team_network_server(realm, self.host_recipient)
        lifecycle_id = target["mail_route_lifecycle_id"]
        self.assertRegex(lifecycle_id, r"^[0-9a-f]{64}$")
        cases = [
            ({"kind": "server", "id": self.host_recipient, "mail_route_lifecycle_id": value}, 422)
            for value in ("", "not-a-lifecycle", "f" * 63, "F" * 64, 1, {}, [])
        ]
        cases.extend([
            ({"kind": "server", "id": self.host_recipient,
              "mail_route_lifecycle_id": "0" * 64}, 409),
            ({"kind": "all", "mail_route_lifecycle_id": lifecycle_id}, 422),
            ({"kind": "server", "id": self.host_recipient,
              "mail_route_lifecycle_id": lifecycle_id, "unknown_field": True}, 422),
        ])
        for recipient, status in cases:
            with self.subTest(recipient=recipient):
                response = self.runtime.proxy(self.connection_id, "POST", self.path, query="",
                    headers={"content-type": "application/json"}, body=json.dumps({
                        "kind": "message", "body": "Must not commit",
                        "recipients": [recipient], "idempotency_key": "bad-mail-" + uuid.uuid4().hex}).encode())
                self.assertEqual(response.status, status, response.body)
                error = json.loads(response.body)["error"]
                self.assertEqual(error["code"], "mail_route_changed" if status == 409 else "invalid_request")
        with closing(self.hub.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=?",
                                                (self.team,)).fetchone()[0], 0)
        self.tls.assert_settled()

    async def test_changed_incarnation_between_resolution_and_post_cannot_drop_binding(self):
        peer, recipient, _claims = self.approve_recipient("race_peer_fixture")
        realm = self.runtime.team_realm(self.team)
        target = self.runtime._team_network_server(realm, recipient)
        reference = self.runtime.resolve_team_references([{
            "kind": "recipient", "recipient_kind": "server", "team_id": self.team,
            "target_id": target["id"], "display_name_snapshot": target["recipient_display_name"]}])[0]
        proxy = self.runtime.proxy
        post_count = 0
        def rotate_then_send(connection_id, method, path, **kwargs):
            nonlocal post_count
            if method == "POST" and path == self.path:
                post_count += 1
                self.assertEqual(json.loads(kwargs["body"])["recipients"][0]["mail_route_lifecycle_id"],
                                 reference["durable_server_binding"]["lifecycle_id"])
                self.tls.store.revoke_peer(peer.peer_id, self.team, peer.certificate_fingerprint,
                                           str(uuid.uuid4()), "owner_fixture")
                self.adapter.revoke_peer(peer_id=peer.peer_id, team_id=self.team)
                _replacement, new_recipient, _new_claims = self.approve_recipient("race_peer_fixture")
                self.assertEqual(new_recipient, recipient, "same logical server, new approved incarnation")
                renewed = self.hub.get_network_server(self.host, self.team, recipient)["server"]
                self.assertNotEqual(renewed["mail_route_lifecycle_id"],
                                    reference["durable_server_binding"]["lifecycle_id"])
            return proxy(connection_id, method, path, **kwargs)
        with mock.patch.object(self.runtime, "proxy", side_effect=rotate_then_send):
            with self.assertRaises(SecurePeerError):
                await asyncio.to_thread(self.runtime.team_authorized_write,
                    self.runtime.team_authority_generation(), self.runtime.team_send_message, reference,
                    payload={"kind": "message", "body": "Must not follow replacement"},
                    attachment_paths=[], idempotency_key="race-" + uuid.uuid4().hex,
                    provenance={"via": "agent", "chat_id": "qa-away-chat"})
        self.assertEqual(post_count, 1, "no fallback or automatic send retry")
        with closing(self.hub.connect()) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=?",
                                                (self.team,)).fetchone()[0], 0)
        self.tls.assert_settled()


if __name__ == "__main__":
    unittest.main()
