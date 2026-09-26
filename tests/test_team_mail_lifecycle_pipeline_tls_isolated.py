"""Cross-host Mail lifecycle over real mTLS/HTTP on private socketpairs only.

Run through public_chat_share_safe_tests.py. Production imports, providers,
TCP listeners, DNS and production state remain forbidden by that runner.
"""
from contextlib import closing
import asyncio
import io
import json
import os
import time
import unittest
from unittest import mock
import uuid

from agentsdock_team_hub.secure_peer import SecurePeerError
from agentsdock_team_hub.store import HubError
import agentsdock_team as team_cli
from tests import test_team_notifications_pipeline_tls_isolated as notifications


class MailLifecyclePipelineTLSAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    requested_scopes = ["teamspace.read", "teamspace.write"]
    commit = notifications.NotificationPipelineTLSAcceptanceTests.commit
    connect_notifications = notifications.NotificationPipelineTLSAcceptanceTests.connect_notifications
    assert_frame = notifications.NotificationPipelineTLSAcceptanceTests.assert_frame
    assert_quiet = notifications.NotificationPipelineTLSAcceptanceTests.assert_quiet

    def setUp(self):
        notifications.NotificationPipelineTLSAcceptanceTests.setUp(self)
        self.assertEqual(self.tls.peer.scopes, frozenset(self.requested_scopes))
        self.host_recipient = self.hub.team_mail_arrival_snapshot(self.host, self.team)["recipient_server_id"]
        self.path = f"/v1/teams/{self.team}/network/messages"
        self.delivery_validator = mock.Mock(side_effect=AssertionError("passive Team Mail attempted agent delivery"))
        self.runtime.set_delivery_target_validator(self.delivery_validator)
        self.addCleanup(self.delivery_validator.assert_not_called)
        self.delivery_claims = self.enterContext(mock.patch.object(self.runtime, "claim_deliveries_once",
            side_effect=AssertionError("passive Team Mail claimed a provider delivery")))
        self.addCleanup(self.delivery_claims.assert_not_called)

    def proxy(self, method, path=None, *, body=None, query="", status=200):
        response = self.runtime.proxy(self.connection_id, method, path or self.path,
            query=query, headers={"content-type": "application/json"} if body is not None else None,
            body=json.dumps(body).encode() if body is not None else None)
        self.assertEqual(response.status, status, response.body)
        return json.loads(response.body)

    def member_request(self, *, parent=None, key=None, body="Member reply body"):
        return {"kind": "message", "title": "Exact parent conversation", "body": body,
            "recipients": [{"kind": "server", "id": self.host_recipient}],
            "in_reply_to_message_id": parent, "idempotency_key": key or "lifecycle-" + uuid.uuid4().hex}

    def host_subscription(self):
        sub, _snapshot = self.hub.subscribe_team_mail_arrivals(self.host, self.team)
        self.addCleanup(sub.close)
        return sub

    def deactivate(self):
        self.runtime.deactivate_connection(self.connection_id,
            expected_host_server_identity="server_tls_fixture", expected_hub_id=self.hub.hub_id)
        self.assertFalse(self.client.get_connection(self.connection_id)["active"])

    def reactivate(self):
        self.runtime.activate_pairing(self.tls.peer.pairing_id,
            expected_connection_id=self.connection_id, expected_host_server_identity="server_tls_fixture",
            expected_hub_id=self.hub.hub_id)
        self.assertTrue(self.client.get_connection(self.connection_id)["active"])

    async def test_host_member_send_reply_and_thread_use_exact_durable_parent(self):
        frames, outgoing, disconnect = await self.connect_notifications()
        baseline = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "snapshot")
        host_subscription = self.host_subscription()
        original = self.commit(title="Original host subject", body="Host request")
        received = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint")
        self.assertEqual(received.mail.arrival_id, original["id"])
        self.assertEqual(received.bulletin, baseline.bulletin)
        inbox = self.proxy("GET", query="box=inbox&include_mailbox_coverage=1")
        self.assertEqual([item["id"] for item in inbox["messages"]], [original["id"]])

        reply = self.proxy("POST", body=self.member_request(parent=original["id"]))["message"]
        self.assertEqual(reply["in_reply_to_message_id"], original["id"])
        self.assertEqual(host_subscription.take(1).arrival_id, reply["id"])
        self.assertEqual(self.hub.get_team_message(self.host, self.team, reply["id"])["message"]["body"], "Member reply body")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(outgoing.get(), .04)

        followup = self.commit(body="Host follow-up", in_reply_to_message_id=reply["id"])
        self.assertEqual(self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint").mail.arrival_id, followup["id"])
        unrelated = self.proxy("POST", body=self.member_request())["message"]
        thread = self.proxy("GET", f"{self.path}/{reply['id']}/thread", query="limit=25")
        self.assertEqual(thread["root_message_id"], original["id"])
        self.assertEqual([item["id"] for item in thread["messages"]], [original["id"], reply["id"], followup["id"]])
        self.assertNotIn(unrelated["id"], json.dumps(thread))
        host_thread = self.hub.get_team_message_thread(self.host, self.team, original["id"])
        shared_fields = lambda item: (item["id"], item["in_reply_to_message_id"], item["body"])
        self.assertEqual(list(map(shared_fields, host_thread["messages"])), list(map(shared_fields, thread["messages"])))
        self.assertEqual(len(frames), 3)
        await self.assert_quiet()
        await disconnect()
        self.tls.assert_settled()

    async def test_committed_lost_ack_retry_and_offline_mail_survive_reconnect_once(self):
        _frames, outgoing, disconnect = await self.connect_notifications()
        retained = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "snapshot").as_dict()
        host_subscription = self.host_subscription()
        request = self.member_request(key="lost-ack-stable-key")
        actual_request = self.client._request
        committed = []

        def lose_ack(host, port, method, path, **kwargs):
            response = actual_request(host, port, method, path, **kwargs)
            if method == "POST" and path == "/v1/hub" + self.path:
                self.assertEqual(response[0], 200)
                committed.append(json.loads(response[2])["message"])
                # The real TLS request has committed; suppress only the
                # acknowledgement at the client boundary to model its loss.
                raise SecurePeerError("transport_failed", "Fixture lost committed acknowledgement", 502)
            return response

        with mock.patch.object(self.client, "_request", side_effect=lose_ack):
            with self.assertRaises(SecurePeerError) as lost:
                self.proxy("POST", body=request)
        self.assertEqual(lost.exception.code, "transport_failed")
        self.assertEqual(len(committed), 1)
        self.assertEqual(host_subscription.take(1).arrival_id, committed[0]["id"])
        lifecycle = self.hub.get_network_server(self.host, self.team, self.recipient)["server"]["mail_route_lifecycle_id"]
        await asyncio.to_thread(self.deactivate)
        await asyncio.sleep(.03)
        await disconnect()
        self.tls.assert_settled()
        before = self.dials
        with self.assertRaises(SecurePeerError):
            self.proxy("POST", body=request)
        self.assertEqual(self.dials, before, "deactivated member must be rejected before transport")

        self.assertGreaterEqual(self.adapter.expire_peer_leases(int(time.time()) + 1), 1)
        offline = self.commit(recipients=[{"kind": "server", "id": self.recipient,
                                           "mail_route_lifecycle_id": lifecycle}])
        self.assertEqual(self.hub.get_network_server(self.host, self.team, self.recipient)["server"]["mail_route_lifecycle_id"], lifecycle)
        self.reactivate()
        replay = self.proxy("POST", body=request)["message"]
        self.assertEqual(replay, committed[0])
        self.assertIsNone(host_subscription.take(0))
        with closing(self.hub.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=? AND id=?",
                (self.team, replay["id"])).fetchone()[0], 1)
        reopened, outgoing, disconnect = await self.connect_notifications(retained)
        current = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "snapshot")
        self.assertEqual(current.mail.arrival_id, offline["id"])
        self.assertFalse(current.mail_reset)
        self.assertFalse(current.bulletin_reset)
        inbox = self.proxy("GET", query="box=inbox&include_mailbox_coverage=1")
        self.assertEqual([item["id"] for item in inbox["messages"]], [offline["id"]])
        await self.assert_quiet()
        self.assertEqual(len(reopened), 1, "offline backlog is one snapshot, without duplicate live arrivals")
        await disconnect()
        self.tls.assert_settled()

    async def test_revocation_closes_idle_stream_and_rejects_old_certificate_and_recipient(self):
        frames, outgoing, disconnect = await self.connect_notifications()
        self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "snapshot")
        previous = self.proxy("POST", body=self.member_request())["message"]
        lifecycle = self.hub.get_network_server(self.host, self.team, self.recipient)["server"]["mail_route_lifecycle_id"]
        lease = next(iter(self.runtime._mail_hints.leases))
        loop, automatically_closed = asyncio.get_running_loop(), asyncio.Event()
        retired = lease._on_close
        def observed_close():
            retired()
            loop.call_soon_threadsafe(automatically_closed.set)
        lease._on_close = observed_close
        revoked = self.tls.store.revoke_peer(self.tls.peer.peer_id, self.team,
            self.tls.peer.certificate_fingerprint, str(uuid.uuid4()), "owner_fixture")
        self.assertEqual(revoked["status"], "revoked")
        self.adapter.revoke_peer(peer_id=self.tls.peer.peer_id, team_id=self.team)
        await asyncio.wait_for(automatically_closed.wait(), 2)
        self.assertTrue(lease.closed)
        self.assertEqual(self.runtime._mail_hints.leases, set())
        self.tls.assert_settled()
        await disconnect()
        self.assertEqual(len(frames), 1)
        with self.assertRaises(SecurePeerError) as rejected:
            self.proxy("POST", body=self.member_request())
        self.assertEqual((rejected.exception.code, rejected.exception.status_code), ("peer_revoked", 401))
        self.assertFalse(self.client.get_connection(self.connection_id)["active"])
        with self.assertRaises(HubError) as stale:
            self.commit(recipients=[{"kind": "server", "id": self.recipient,
                                     "mail_route_lifecycle_id": lifecycle}])
        self.assertEqual(stale.exception.code, "mail_route_changed")
        self.assertEqual(self.hub.get_team_message(self.host, self.team, previous["id"])["message"]["id"], previous["id"])
        with closing(self.hub.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM team_messages WHERE team_id=?", (self.team,)).fetchone()[0], 1)

    async def test_real_helper_authority_and_stdin_reply_reaches_host_over_tls(self):
        parent = self.commit(body="Incoming mail asks for an explicit reply", title="Preserved parent subject")
        host_subscription = self.host_subscription()
        authority = self.root / "provider-authority.json"
        capability, session, route = "isolated-team-reply-capability", "isolated-helper-session", "team_frozen_host_route"
        authority.write_text(json.dumps({"provider_capability": capability, "source_session_id": session}))
        authority.chmod(0o600)
        reference = {"kind": "recipient", "team_id": self.team,
                     "recipient_kind": "server", "target_id": self.host_recipient}
        generation = self.runtime.team_authority_generation()
        receipts = []

        def local_http_boundary(method, path, credential, payload, *, timeout):
            # The monolith HTTP seam alone is substituted. CLI file/stdin
            # validation, realm generation, exact parent authorization, TLS
            # transport and durable store all execute their real code.
            self.assertEqual((method, path, credential, timeout),
                ("POST", f"/api/agent/team/routes/{route}", capability, 900.0))
            result = self.runtime.team_authorized_write(generation, self.runtime.team_send_message,
                reference, payload=payload, attachment_paths=payload["attachments"],
                idempotency_key=payload["idempotency_key"], provenance={"via": "agent", "chat_id": session})
            message = result["message"]
            receipts.append(message)
            return {"ok": True, "route_id": route, "message_id": message["id"], "kind": "message",
                    "accepted": True, "duplicate": False, "attachments": []}

        args = team_cli.parser().parse_args(["--authority-file", str(authority), "reply", parent["id"], "--route", route])
        body = "Line one from stdin.\n\nLine two keeps Unicode: café."
        with mock.patch.dict(os.environ, {"AGENTSDOCK_PROVIDER_AUTHORITY_FILE": str(authority), "AGENTSDOCK_CHAT_ID": session}), \
             mock.patch.object(team_cli, "_request_json", side_effect=local_http_boundary) as local_request:
            with mock.patch.object(team_cli.sys, "stdin", io.StringIO(body + "\n")):
                accepted = args.handler(args)
            local_request.assert_called_once()
            message = receipts[0]
            self.assertEqual(accepted["message_id"], message["id"])
            self.assertEqual((message["body"], message["in_reply_to_message_id"], message["title"]),
                (body, parent["id"], parent["title"]))
            self.assertEqual([item["id"] for item in message["recipients"]], [self.host_recipient])
            self.assertEqual(host_subscription.take(1).arrival_id, message["id"])
            before = self.dials
            with mock.patch.dict(os.environ, {"AGENTSDOCK_CHAT_ID": "another-session"}), \
                 mock.patch.object(team_cli.sys, "stdin", io.StringIO("Forbidden reply")):
                with self.assertRaisesRegex(team_cli.TeamCLIError, "does not match"):
                    args.handler(args)
            self.assertEqual(local_request.call_count, 1)
            self.assertEqual(self.dials, before)
        self.assertEqual(self.hub.get_team_message(self.host, self.team, receipts[0]["id"])["message"]["body"], body)
