"""Negotiated durable approvals: real signed requests/store, memory transport.

Run under public_chat_share_safe_tests.py; no monolith, listener or network.
"""
from contextlib import closing
import json
from pathlib import Path
import unittest
from unittest import mock
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from agentsdock_team_hub.secure_peer import (
    PAIRING_ATTEMPT_RETENTION_SECONDS, PAIRING_TTL_SECONDS, SecurePeerClient,
    SecurePeerError, SecurePeerStore, build_pairing_request,
)
from tests import test_secure_peer_auto_join as legacy


class DurableApprovalTests(unittest.TestCase):
    setUp = legacy.SecurePeerAutoJoinTests.setUp
    restart = legacy.SecurePeerAutoJoinTests.restart
    begin = legacy.SecurePeerAutoJoinTests.begin
    approve = legacy.SecurePeerAutoJoinTests.approve
    approved = legacy.SecurePeerAutoJoinTests.approved
    activate = legacy.SecurePeerAutoJoinTests.activate
    advertisement = True

    def exchange(self, host, port, method, path, **kwargs):
        if (method, path) == ("GET", "/v1/health"):
            from agentsdock_team_hub.security import canonical_json
            health = self.store.public_health()
            if self.advertisement is None:
                health.pop("durable_pairing_approval_v1", None)
            else:
                health["durable_pairing_approval_v1"] = self.advertisement
            return 200, [("Content-Type", "application/json")], canonical_json(health), self.leaf
        return legacy.SecurePeerAutoJoinTests.exchange(self, host, port, method, path, **kwargs)

    def request(self, pending):
        return json.loads(self.client._connection_row(pending["connection_id"])["pairing_request_json"])

    def test_host_advertises_and_builder_default_remains_legacy(self):
        self.assertIs(self.store.public_health()["durable_pairing_approval_v1"], True)
        key = Ed25519PrivateKey.generate()
        request = build_pairing_request(key, server_identity="legacy-peer", display_name="Legacy peer",
            host_ca_fingerprint=self.store.ca_fingerprint, created_at=self.now, requested_scopes=["teamspace.read"])
        self.assertNotIn("durable_pairing_approval", request["capabilities"])
        pending = self.store.submit_pairing(request)
        self.assertEqual(pending["expires_at"], self.now + PAIRING_TTL_SECONDS)
        self.now += PAIRING_TTL_SECONDS + 1
        self.assertEqual(self.store.poll_pairing(pending["pairing_id"], pending["poll_token"])["status"], "expired")

    def test_only_literal_true_negotiates_durable_requests(self):
        for advertised in (None, False, 1, "true", {}, []):
            with self.subTest(advertised=advertised):
                self.advertisement = advertised
                pending = self.begin(complete_on_approval=True)
                self.assertNotIn("durable_pairing_approval", self.request(pending)["capabilities"])
                self.assertEqual(pending["pairing_expires_at"], self.now + PAIRING_TTL_SECONDS)
        self.advertisement = True
        pending = self.begin(complete_on_approval=True)
        request = self.request(pending)
        self.assertIn("durable_pairing_approval", request["capabilities"])
        # Host parsing verifies the actual request signature covering opt-in.
        self.assertEqual(self.store._normalize_pairing_payload(request)[0], request)
        self.assertEqual(pending["pairing_expires_at"], 0)

    def test_wait_days_restart_approve_poll_and_activate_without_new_consent(self):
        pending = self.begin(complete_on_approval=True)
        request = self.request(pending)
        self.now += 8 * 24 * 60 * 60
        self.store = SecurePeerStore(self.root / "host", "isolated-host-001", "isolated-hub-001", clock=lambda: self.now)
        self.client = self.restart()
        self.assertEqual(self.client.expire_pending_pairings(), 0)
        incoming = self.store.list_pairings(team_id="isolated-team-001", status="pending")
        self.assertEqual([row["pairing_id"] for row in incoming], [pending["pairing_id"]])
        snapshot = self.client.auto_completion_snapshot(pending["connection_id"])
        self.assertEqual((snapshot["state"], snapshot["deadline"]), ("pending", None))
        self.assertTrue(snapshot["connection"]["complete_on_approval"])
        self.assertIsNone(self.client.list_auto_completion_candidates()[0]["auto_completion_deadline"])
        self.assertEqual(self.client.auto_completion_info(pending["connection_id"]), {"state": "pending", "deadline": None})
        self.approve(pending)
        approved = self.client.poll_pairing(pending["connection_id"])
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(self.request(approved), request)
        active = self.activate(approved)
        self.assertTrue(active["active"])
        self.assertEqual(self.client.auto_completion_state(active["connection_id"]), "completed")
        self.assertEqual(self.activate(approved)["connection_id"], active["connection_id"])

    def test_lost_committed_post_retries_exact_signed_request_after_legacy_retention(self):
        captured = {}
        def lose(response):
            captured.update(response)
            self.post_hook = None
            raise SecurePeerError("transport_failed", "Isolated lost acknowledgment", 502)
        self.post_hook = lose
        with self.assertRaises(SecurePeerError):
            self.begin(complete_on_approval=True)
        with closing(self.client._connect()) as db:
            attempt = dict(db.execute("SELECT * FROM client_pairing_attempts").fetchone())
            self.assertEqual(db.execute("SELECT expires_at FROM client_join_intents").fetchone()[0], 0)
        self.now += PAIRING_ATTEMPT_RETENTION_SECONDS + 1
        self.client = self.restart()
        recovered = self.client.recover_pairing_attempts()
        self.assertEqual(recovered["recovered"], [attempt["connection_id"]])
        self.assertEqual((recovered["retired"], recovered["remaining"]), (0, 0))
        current = self.client.get_connection(attempt["connection_id"])
        self.assertEqual(current["pairing_id"], captured["pairing_id"])
        self.assertEqual(self.request(current), json.loads(attempt["request_json"]))
        self.assertEqual(len(self.store.list_pairings()), 1)
        self.assertEqual(current["pairing_expires_at"], 0)
        self.assertTrue(current["complete_on_approval"])

    def test_cancel_after_days_is_terminal_across_restart_and_host_replay(self):
        pending = self.begin(complete_on_approval=True)
        request = self.request(pending)
        self.now += 3 * 24 * 60 * 60
        result = self.client.cancel_pairing(pending["connection_id"], str(uuid.uuid4()))
        self.assertEqual(result["status"], "cancelled")
        self.client = self.restart()
        self.assertEqual(self.client.list_auto_completion_candidates(), [])
        self.assertEqual(self.client.auto_completion_state(pending["connection_id"]), "cancelled")
        self.assertEqual(self.store.submit_pairing(request)["status"], "cancelled")
        self.assertEqual(list(self.client.keys_dir.iterdir()), [])
        with self.assertRaises(SecurePeerError):
            self.approve(pending)

    def test_reject_after_days_stays_terminal(self):
        pending = self.begin(complete_on_approval=True)
        self.now += 3 * 24 * 60 * 60
        self.store.reject_pairing(pending["pairing_id"], "isolated-owner-001", "Explicit rejection",
            expected_peer_server_identity=self.client.server_identity,
            expected_transcript_hash=pending["transcript_hash"], idempotency_key=str(uuid.uuid4()))
        rejected = self.client.poll_pairing(pending["connection_id"])
        self.assertEqual(rejected["status"], "rejected")
        self.assertFalse(rejected["complete_on_approval"])
        self.assertEqual(self.client.list_auto_completion_candidates(), [])

    def test_legacy_and_durable_pending_coexist_without_reviving_expired(self):
        self.advertisement = None
        old = self.begin(complete_on_approval=True)
        self.now += PAIRING_TTL_SECONDS + 1
        self.assertEqual(self.client.get_connection(old["connection_id"])["status"], "expired")
        self.advertisement = True
        new = self.begin(complete_on_approval=True)
        self.now += 3 * 24 * 60 * 60
        self.assertEqual(self.client.get_connection(old["connection_id"])["status"], "expired")
        self.assertEqual(self.client.get_connection(new["connection_id"])["status"], "pending")
        self.assertEqual([row["pairing_id"] for row in self.store.list_pairings(status="pending")], [new["pairing_id"]])

    def test_unsigned_zero_expiry_from_legacy_host_is_rejected_initially_and_on_poll(self):
        self.advertisement = None
        self.post_hook = lambda response: response.update(expires_at=0)
        with self.assertRaises(SecurePeerError):
            self.begin(complete_on_approval=True)
        self.post_hook = None
        pending = self.begin(complete_on_approval=True)
        self.poll_hook = lambda response: response.update(expires_at=0)
        with self.assertRaises(SecurePeerError) as caught:
            self.client.poll_pairing(pending["connection_id"])
        self.assertEqual(caught.exception.code, "remote_invalid")
        self.assertGreater(self.client.get_connection(pending["connection_id"])["pairing_expires_at"], 0)

    def test_durable_flag_does_not_bypass_configured_scope_and_capability_policy(self):
        pending = self.begin()
        request = self.request(pending)
        self.assertTrue(self.client._pairing_request_matches_configured_policy(request))
        restricted = SecurePeerClient(self.root / "restricted", "restricted-guest", "Restricted",
            clock=lambda: self.now, pairing_capabilities=("cert_renewal", "teamspace"))
        self.assertFalse(restricted._pairing_request_matches_configured_policy(request))
        narrower = {**request, "capabilities": ["cert_renewal", "durable_pairing_approval", "teamspace"]}
        self.assertTrue(restricted._pairing_request_matches_configured_policy(narrower))
        self.assertFalse(restricted._pairing_request_matches_configured_policy({
            **narrower, "requested_scopes": ["teamspace.read", "cross_chat.request_reply"]}))
        self.assertFalse(restricted._pairing_request_matches_configured_policy({
            **narrower, "capabilities": narrower["capabilities"] + ["unknown"]}))

    def test_new_join_and_host_role_pause_cancel_original_durable_consent(self):
        old = self.begin(complete_on_approval=True)
        current = self.begin(complete_on_approval=True)
        self.assertEqual(self.client.auto_completion_state(old["connection_id"]), "cancelled")
        self.approve(old)
        old_approved = self.client.poll_pairing(old["connection_id"])
        with self.assertRaises(SecurePeerError):
            self.activate(old_approved)
        self.client.cancel_auto_completions()
        self.approve(current)
        approved = self.client.poll_pairing(current["connection_id"])
        with self.assertRaises(SecurePeerError):
            self.activate(approved)
        self.assertEqual(self.client.list_auto_completion_candidates(), [])

    # Re-run the existing exact safety races under negotiated durable consent,
    # in addition to the unchanged legacy variants in the original module.
    test_cancel_during_activation_health_is_terminal_and_retryable = legacy.SecurePeerAutoJoinTests.test_cancel_during_activation_health_is_terminal_and_retryable
    test_cancel_fences_consent_before_waiting_for_stalled_poll = legacy.SecurePeerAutoJoinTests.test_cancel_fences_consent_before_waiting_for_stalled_poll
    test_host_pause_without_active_member_and_new_join_supersede_consent = legacy.SecurePeerAutoJoinTests.test_host_pause_without_active_member_and_new_join_supersede_consent
    test_manual_binding_change_and_aba_invalidate_pending_join = legacy.SecurePeerAutoJoinTests.test_manual_binding_change_and_aba_invalidate_pending_join
    test_revocation_and_forgetting_keep_terminal_intent_unusable = legacy.SecurePeerAutoJoinTests.test_revocation_and_forgetting_keep_terminal_intent_unusable
    test_exact_identity_mismatch_rejects_activation_before_health = legacy.SecurePeerAutoJoinTests.test_exact_identity_mismatch_rejects_activation_before_health

    def test_durable_optin_is_signed_and_cannot_be_added_to_legacy_request(self):
        request = build_pairing_request(Ed25519PrivateKey.generate(), server_identity="signed-peer",
            display_name="Signed peer", host_ca_fingerprint=self.store.ca_fingerprint,
            created_at=self.now, requested_scopes=["teamspace.read"])
        request["capabilities"] = sorted([*request["capabilities"], "durable_pairing_approval"])
        with self.assertRaises(SecurePeerError) as caught:
            self.store.submit_pairing(request)
        self.assertEqual(caught.exception.code, "signature_invalid")
        self.assertEqual(self.store.list_pairings(), [])

    def test_negotiation_does_not_relax_fresh_request_timestamp_validation(self):
        request = build_pairing_request(Ed25519PrivateKey.generate(), server_identity="late-peer",
            display_name="Late peer", host_ca_fingerprint=self.store.ca_fingerprint,
            created_at=self.now, capabilities=("cert_renewal", "durable_pairing_approval", "teamspace"),
            requested_scopes=["teamspace.read"])
        self.now += 8 * 24 * 60 * 60
        with self.assertRaises(SecurePeerError) as caught:
            self.store.submit_pairing(request)
        self.assertEqual(caught.exception.code, "pairing_expired")
        self.assertEqual(self.store.list_pairings(), [])

    def test_shared_nat_can_submit_more_than_sixteen_pending_without_losing_global_bound(self):
        for index in range(17):
            request = build_pairing_request(Ed25519PrivateKey.generate(),
                server_identity=f"nat-peer-{index}", display_name=f"NAT peer {index}",
                host_ca_fingerprint=self.store.ca_fingerprint, created_at=self.now,
                capabilities=("cert_renewal", "durable_pairing_approval", "teamspace"),
                requested_scopes=["teamspace.read"])
            self.store.submit_pairing(request, source_ip="192.0.2.55", source_port=40000 + index)
        self.assertEqual(len(self.store.list_pairings(status="pending")), 17)
        with mock.patch("agentsdock_team_hub.secure_peer.PAIRING_STATUS_LIMIT", 17):
            request = build_pairing_request(Ed25519PrivateKey.generate(), server_identity="one-too-many",
                display_name="Capacity", host_ca_fingerprint=self.store.ca_fingerprint,
                created_at=self.now, requested_scopes=["teamspace.read"])
            with self.assertRaises(SecurePeerError) as caught:
                self.store.submit_pairing(request, source_ip="192.0.2.56", source_port=41000)
        self.assertEqual(caught.exception.code, "pairing_capacity")


if __name__ == "__main__":
    unittest.main()
