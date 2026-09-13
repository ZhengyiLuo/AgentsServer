"""Invitation -> approval -> automatic connection using real service/storage.

Run only with public_chat_share_safe_tests.py. The real SecurePeerRuntime,
SecurePeerClient, SecurePeerStore, HubStore and SecurePeerHubAdapter execute.
Only SecurePeerGateway's listener and SecurePeerClient._request are replaced:
the memory exchange calls real pairing/certificate authorization/heartbeat/Hub
forwarding methods. TLS handshake, HTTP framing and GUI are NOT exercised.
No monolith import, sockets, listener, providers, processes or production state.
"""
import asyncio
from collections import Counter
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from agentsdock_team_hub.secure_peer import (
    PAIRING_TOKEN_HEADER, SecurePeerClient, SecurePeerError, sanitize_proxy_request,
)
from agentsdock_team_hub.security import canonical_json
from agentsdock_team_hub.store import HubError, HubStore
from secure_peer_runtime import SecurePeerRuntime


HOST = "invitation-qa-host"
GUEST = "invitation-qa-guest"
HOST_IP = "192.0.2.10"  # Documentation-only address; never contacted.
SCOPES = ["teamspace.read", "teamspace.write"]


class NoSocketGateway:
    """Allow real host configuration/link generation without creating a socket."""
    def __init__(self, store, host, port, **callbacks):
        self.store, self.address = store, (host, port)
        self.callbacks = callbacks
        self.started = False
        store.configure_listener_identity(host)

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def refresh_listener_identity(self):
        return self.store.configure_listener_identity(self.address[0])


class InvitationJourneyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="invitation-real-state-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.enterContext(mock.patch("secure_peer_runtime.SecurePeerGateway", NoSocketGateway))
        self.enterContext(mock.patch.object(SecurePeerClient, "_request", side_effect=self.exchange))
        self.calls = Counter()
        self.forwarded = 0
        self.presented_certificate = None
        self.hub = HubStore(self.root / "hub", managed_host_identity=HOST,
                            managed_server_instance_id="host-instance",
                            managed_host_display_name="Invitation host")
        bootstrap = self.hub.bootstrap(
            (self.root / "hub/bootstrap-owner.proof").read_text().strip(),
            "owner@invitation.example", "Invitation owner", "Test host",
        )
        self.owner = self.hub.verify_access(bootstrap["access_token"])
        self.team_id = bootstrap["teams"][0]["id"]
        self.host = SecurePeerRuntime(self.root / "host", server_identity=HOST,
                                      server_instance_id="host-instance", display_name="Invitation host")
        self.addCleanup(self.host.shutdown)
        self.host.attach_host_hub(hub_id=self.hub.hub_id,
                                 hub_data_dir=self.root / "hub", hub_store=self.hub)
        self.host_status = self.host.configure_host(
            enabled=True, advertised_host=HOST_IP, listen_port=7851,
        )
        self.member = self.new_member()
        self.store = self.host._host_store
        self.assertIsNotNone(self.store)
        self.leaf = self.store._server_certificate.public_bytes(serialization.Encoding.DER)

    def new_member(self):
        member = SecurePeerRuntime(self.root / "guest", server_identity=GUEST,
                                   server_instance_id="guest-instance", display_name="Guest workstation")
        self.addCleanup(member.shutdown)
        return member

    def member_certificate(self):
        connections = self.member.client.list_connections()
        self.assertEqual(len(connections), 1)
        row = self.member.client._connection_row(connections[0]["connection_id"])
        return x509.load_pem_x509_certificate(
            Path(row["certificate_path"]).read_bytes(),
        ).public_bytes(serialization.Encoding.DER)

    def exchange(self, host, port, method, path, **kwargs):
        self.assertEqual((host, port), (HOST_IP, 7851))
        self.calls[(method, path)] += 1
        if (method, path) == ("GET", "/v1/health"):
            value = self.store.public_health()
        elif (method, path) == ("POST", "/v1/pairings"):
            value = self.store.submit_pairing(kwargs["body"])
        elif method == "GET" and path.startswith("/v1/pairings/"):
            value = self.store.poll_pairing(path.rsplit("/", 1)[1], kwargs["headers"][PAIRING_TOKEN_HEADER])
        elif method == "POST" and path.startswith("/v1/pairings/") and path.endswith("/cancel"):
            value = self.store.cancel_pairing(path.split("/")[-2],
                                              kwargs["headers"][PAIRING_TOKEN_HEADER],
                                              kwargs["body"]["idempotency_key"])
        else:
            certificate = self.presented_certificate or self.member_certificate()
            if (method, path) == ("GET", "/v1/peer/status"):
                peer = self.store.authorize_peer_self_revocation(certificate)
                value = self.store.peer_revocation_status(peer)
            else:
                # Authorize the real issued certificate before heartbeat or
                # forwarding. A wrong/revoked cert must not reach Hub state.
                peer = self.store.authenticate_peer(certificate)
                self.store.record_peer_heartbeat(peer.peer_id)
                self.host._record_authenticated_peer_heartbeat(peer)
                if (method, path) == ("GET", "/v1/peer/health"):
                    value = {
                        "ok": True, "peer_id": peer.peer_id, "team_id": peer.team_id,
                        "host_server_identity": HOST, "hub_id": self.hub.hub_id,
                        "host_ca_fingerprint": self.store.ca_fingerprint,
                        "certificate_fingerprint": peer.certificate_fingerprint,
                        "certificate_expires_at": peer.certificate_expires_at,
                        "peer_display_name": peer.peer_display_name,
                        "remote_route_delivery_available": False,
                    }
                elif path.startswith("/v1/hub/"):
                    target = urlsplit(path.removeprefix("/v1/hub"))
                    allowed = {
                        ("GET", "/v1/health"), ("GET", "/v1/peer-session"),
                        ("GET", f"/v1/teams/{self.team_id}/network"),
                        ("POST", f"/v1/teams/{self.team_id}/network/server-profile"),
                    }
                    self.assertIn((method, target.path), allowed, "unexpected invitation network work")
                    body = kwargs.get("body") or b""
                    if isinstance(body, dict):
                        body = canonical_json(body)
                    request = sanitize_proxy_request(peer, method, target.path, target.query,
                                                     tuple((kwargs.get("headers") or {}).items()), body)
                    self.forwarded += 1
                    response = self.host._forward_peer_request(request)
                    return response.status, list(response.headers), response.body, self.leaf
                else:
                    self.fail(f"Unexpected in-memory request: {method} {path}")
        return 200, [("Content-Type", "application/json")], canonical_json(value), self.leaf

    def begin(self):
        link = urlsplit(self.host_status["host"]["pairing_link"])
        self.assertEqual((link.scheme, link.netloc, link.path), ("agentsdock", "secure-peer", "/join"))
        query = parse_qs(link.query, strict_parsing=True)
        self.assertEqual(set(query), {"host", "port", "fingerprint"})
        self.assertTrue(all(len(values) == 1 for values in query.values()))
        return self.member.begin_pairing(
            host=query["host"][0], port=int(query["port"][0]),
            expected_ca_fingerprint=query["fingerprint"][0], request_id=str(uuid.uuid4()),
            display_name="Guest workstation", requested_scopes=SCOPES, complete_on_approval=True,
        )

    def approve(self, pending, *, idempotency_key=None):
        incoming = next(row for row in self.host.list_pairings(team_id=None, status="pending_approval")["pairings"]
                        if row["id"] == pending["id"])
        self.assertEqual(incoming["peer_server_identity"], GUEST)
        self.assertEqual(incoming["transcript_hash"], pending["transcript_hash"])
        self.assertEqual(incoming["sas_words"], pending["sas_words"])
        self.approval = {
            "pairing_id": pending["id"], "team_id": self.team_id, "scopes": SCOPES,
            "approved_by": self.owner.principal_id, "expected_peer_server_identity": GUEST,
            "expected_transcript_hash": pending["transcript_hash"],
            "idempotency_key": idempotency_key or str(uuid.uuid4()),
        }
        return self.host.approve_pairing(**self.approval)["pairing"]

    def guest_rows(self):
        return [row for row in self.hub.get_network(self.owner, self.team_id)["servers"]
                if row["server_identity"] == GUEST]

    def proxy(self, path):
        connection = self.member.status()["active_connection_id"]
        response = self.member.proxy(connection, "GET", path, query="", headers=None, body=None)
        self.assertEqual(response.status, 200)
        return json.loads(response.body)

    async def test_invite_approve_automatic_completion_and_host_visibility(self):
        pending = self.begin()
        self.assertTrue(pending["complete_on_approval"])
        self.assertEqual(self.guest_rows(), [])
        self.assertIsNone(self.member.status()["active_connection_id"])
        observer = asyncio.create_task(self.member.wait_pairing_completion(
            pending["id"], expected_transcript_hash=pending["transcript_hash"],
        ))
        self.addAsyncCleanup(self.cancel_observer, observer)
        for _ in range(30):
            if self.member._completion_waiters:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.member._completion_waiters)
        requests_before_approval = self.calls.copy()
        self.approve(pending)
        self.assertFalse(observer.done())
        self.assertEqual(self.calls, requests_before_approval)
        approved_node = self.guest_rows()[0]
        self.assertEqual(approved_node["display_name"], "Guest workstation")
        self.assertEqual(approved_node["status"], "offline")
        # This is the existing service-owned pass, not a UI approval poll or
        # explicit activate call. Real activation validates real peer health.
        result = self.member.maintenance_once()
        self.assertTrue(result["active"], result)
        self.assertTrue(result["healthy"], result)
        receipt = await asyncio.wait_for(observer, 1)
        self.assertEqual(receipt["completion_state"], "completed")
        self.assertEqual(receipt["pairing"]["id"], pending["id"])
        self.assertEqual(receipt["pairing"]["transcript_hash"], pending["transcript_hash"])
        self.assertEqual(self.member._completion_waiters, {})
        online = self.guest_rows()
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["id"], approved_node["id"])
        self.assertEqual(online[0]["status"], "active")
        incoming = self.host.list_peers(team_id=self.team_id)["peers"]
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]["transport_state"], "online")
        self.assertEqual(receipt["pairing"]["transport_state"], "online")
        session = self.proxy("/v1/peer-session")
        self.assertEqual(session["principal"]["kind"], "service")
        self.assertIsNone(session["principal"]["email"])
        self.assertEqual([team["id"] for team in session["teams"]], [self.team_id])
        self.assertEqual(self.proxy(f"/v1/teams/{self.team_id}/network")["network"]["hub_id"], self.hub.hub_id)

    @staticmethod
    async def cancel_observer(task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_maintenance_approval_replay_and_restart_do_not_duplicate_nodes(self):
        pending = self.begin()
        approved = self.approve(pending)
        self.assertTrue(self.member.maintenance_once()["active"])
        connection_id = self.member.status()["active_connection_id"]
        node_id = self.guest_rows()[0]["id"]
        calls = self.calls.copy()
        for _ in range(2):
            self.assertTrue(self.member.maintenance_once()["active"])
            replay = self.host.approve_pairing(**{**self.approval, "idempotency_key": str(uuid.uuid4())})
            self.assertEqual(replay["pairing"]["connection_id"], approved["connection_id"])
        self.member.shutdown()
        self.member = self.new_member()
        receipt = await self.member.wait_pairing_completion(pending["id"], expected_transcript_hash=pending["transcript_hash"])
        self.assertEqual(receipt["completion_state"], "completed")
        self.assertEqual(self.member.status()["active_connection_id"], connection_id)
        self.assertTrue(self.member.maintenance_once()["active"])
        self.assertEqual([row["id"] for row in self.guest_rows()], [node_id])
        for key in calls:
            if key[1].startswith("/v1/pairings"):
                self.assertEqual(self.calls[key], calls[key], key)

    async def test_wrong_certificate_never_activates_or_marks_host_node_online(self):
        pending = self.begin()
        self.approve(pending)
        self.presented_certificate = self.leaf  # Server-auth cert, not guest client-auth cert.
        self.assertFalse(self.member.maintenance_once()["active"])
        self.assertIsNone(self.member.status()["active_connection_id"])
        self.assertEqual(self.forwarded, 0)
        self.assertEqual(self.guest_rows()[0]["status"], "offline")
        self.assertEqual(self.member.client.auto_completion_state(pending["connection_id"]), "pending")
        self.presented_certificate = None
        self.assertTrue(self.member.maintenance_once()["active"])
        self.assertEqual(len(self.guest_rows()), 1)

    async def test_revoked_certificate_cannot_reenter_hub_and_guest_retires_binding(self):
        pending = self.begin()
        approved = self.approve(pending)
        self.assertTrue(self.member.maintenance_once()["active"])
        certificate = self.member_certificate()
        self.host.revoke_peer(peer_id=approved["connection_id"], team_id=self.team_id,
                              expected_certificate_fingerprint=approved["certificate_fingerprint"],
                              idempotency_key=str(uuid.uuid4()), revoked_by=self.owner.principal_id)
        self.assertEqual(self.guest_rows(), [])
        with self.assertRaises(SecurePeerError) as denied:
            self.store.authenticate_peer(certificate)
        self.assertEqual(denied.exception.code, "peer_revoked")
        before = self.forwarded
        self.assertFalse(self.member.maintenance_once()["active"])
        self.assertEqual(self.forwarded, before)
        self.assertIsNone(self.member.status()["active_connection_id"])
        self.assertEqual(self.member.client.list_auto_completion_candidates(), [])

    async def test_cancelled_invitation_never_becomes_a_member(self):
        pending = self.begin()
        self.member.cancel_pairing(pending["id"], idempotency_key=str(uuid.uuid4()))
        calls = self.calls.copy()
        self.assertFalse(self.member.maintenance_once()["active"])
        receipt = await self.member.wait_pairing_completion(pending["id"], expected_transcript_hash=pending["transcript_hash"])
        self.assertEqual(receipt["completion_state"], "cancelled")
        self.assertEqual(self.guest_rows(), [])
        self.assertEqual(self.calls, calls)
        with self.assertRaises(HubError):
            self.host.approve_pairing(pairing_id=pending["id"], team_id=self.team_id, scopes=SCOPES,
                                     approved_by=self.owner.principal_id, expected_peer_server_identity=GUEST,
                                     expected_transcript_hash=pending["transcript_hash"], idempotency_key=str(uuid.uuid4()))


if __name__ == "__main__":
    unittest.main()
