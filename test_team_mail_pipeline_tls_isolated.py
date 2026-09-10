"""Source Mail pipeline acceptance, private AF_UNIX sockets and temp state only.

Actual HubStore, adapter, issued mTLS credentials, HTTP gateway/client, Member
runtime and local ASGI handler execute. Only listener/TCP dialing and the ASGI
socket object are substituted. One approved client descriptor is seeded from
the actually issued certificate; activation and capability health are real.
No monolith, TCP ports, DNS, provider, desktop UI or production state is used.
Run only through public_chat_share_safe_tests.py.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import urlencode
import uuid

from starlette.websockets import WebSocketDisconnect

from agentsdock_team_hub.mail_hints import MailArrival, MailHintCapacity, MailHintClosed
from agentsdock_team_hub.secure_peer import SecurePeerError
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from agentsdock_team_hub.store import HubError, HubStore
from secure_peer_runtime import SecurePeerRuntime
from team_mail_websocket import MAIL_WEBSOCKET_PROTOCOL, serve_team_mail_hints
import test_peer_mail_hint_tls_isolated as tls


class MailPipelineTLSAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mail-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.hub = HubStore(self.root / "hub", managed_host_identity="server_tls_fixture")
        self.addCleanup(self.hub.mail_hint_broker.close)
        self.hub.bootstrap_managed_network("Isolated pipeline")
        self.host = self.hub.managed_server_claims()
        self.team = self.host.team_id
        self.adapter = SecurePeerHubAdapter(self.hub)
        self.enterContext(mock.patch.object(tls, "TEAM", self.team))
        self.enterContext(mock.patch.object(tls, "HUB", self.hub.hub_id))
        self.tls = tls.PeerMailHintTLSAcceptanceTests()

        def subscribe(peer, previous):
            lease = self.adapter.subscribe_team_mail_hints(peer, previous)
            self.tls.leases.append(lease)
            return lease

        self.tls.subscribe = subscribe
        self.tls.setUp()
        self.addCleanup(self.tls.doCleanups)
        self.adapter.provision_peer({"peer_id": self.tls.peer.peer_id,
            "peer_server_identity": self.tls.peer.peer_server_identity,
            "team_id": self.team}, display_name="Isolated peer")
        self.adapter.record_peer_heartbeat(self.tls.peer.peer_id, self.team)
        self.tls.gateway.mail_hint_snapshot = self.adapter.team_mail_hint_snapshot
        self.tls.gateway.forwarder = self.adapter.forward
        self.tls.gateway.resource_team_resolver = self.adapter.resource_team
        self.tls.gateway.peer_heartbeat = lambda peer: self.adapter.record_peer_heartbeat(peer.peer_id, peer.team_id)
        self.dials = 0

        def private_dial(connection):
            self.assertEqual((connection.host, connection.port), (tls.HOST, 17857))
            self.dials += 1
            server_raw, client_raw = socket.socketpair()
            self.tls.sockets.extend((server_raw, client_raw))
            self.tls.server.process_request(server_raw, (tls.SOURCE, 40000))
            client_raw.settimeout(connection.timeout)
            connection.sock = connection._context.wrap_socket(client_raw, server_hostname=connection.host)

        self.enterContext(mock.patch.object(tls.HTTPS_CONNECTION, "connect", private_dial))
        self.runtime = SecurePeerRuntime(self.root / "member", server_identity="peer_tls_fixture",
            server_instance_id="isolated-mail-pipeline", mail_hints_enabled=True)
        self.addCleanup(self.runtime.shutdown)
        self.client = self.runtime.client
        self.connection_id = str(uuid.uuid4())
        row = {**self.tls.row, "connection_id": self.connection_id, "status": "approved",
            "pairing_id": self.tls.peer.pairing_id, "pairing_request_id": str(uuid.uuid4()),
            "poll_token": "isolated-unused-pairing-token", "peer_id": self.tls.peer.peer_id,
            "host_server_identity": "server_tls_fixture", "transcript_hash": "isolated-approved-binding",
            "sas_json": "[]", "requested_scopes_json": '["teamspace.read"]',
            "scopes_json": '["teamspace.read"]',
            "certificate_fingerprint": self.tls.peer.certificate_fingerprint,
            "created_at": int(time.time()), "updated_at": int(time.time())}
        with closing(self.client._connect()) as connection:
            connection.execute(f"INSERT INTO client_connections ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()))
        active = self.client.set_active_connection(self.connection_id, expected_current=None)
        self.assertTrue(active["active"])
        self.assertTrue(self.client.mail_hint_capability(self.connection_id, self.tls.peer.certificate_fingerprint))
        self.assertTrue(self.runtime.team_mail_hint_capability()["enabled"])
        self.recipient = self.adapter.team_mail_hint_snapshot(self.tls.peer)["cursor"]["recipient_server_id"]

    async def connect_socket(self, previous=None):
        frames, closed = [], []
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put(json.dumps({"version": 1, "team_id": self.team, "previous_cursor": previous}))

        class Socket:
            query_params = {}
            async def accept(self, **kwargs): pass
            async def receive_text(self):
                value = await incoming.get()
                if value is None:
                    raise WebSocketDisconnect()
                return value
            async def send_text(self, raw):
                frame = json.loads(raw)
                frames.append(frame)
                await outgoing.put(frame)
            async def close(self, code): closed.append(code)

        task = asyncio.create_task(serve_team_mail_hints(Socket(), self.runtime,
            server_identity="peer_tls_fixture", authorized=lambda: True, protocols=[MAIL_WEBSOCKET_PROTOCOL]))
        async def disconnect():
            if not task.done():
                await incoming.put(None)
                await asyncio.wait_for(task, 5)
        self.addAsyncCleanup(disconnect)
        return frames, outgoing, disconnect

    def assert_frame(self, frame, kind, message=None):
        self.assertEqual(set(frame), {"type", "server_identity", "hub_id", "stream_id", "cursor"})
        self.assertEqual(frame["type"], kind)
        self.assertEqual(frame["server_identity"], "peer_tls_fixture")
        self.assertEqual(frame["hub_id"], self.hub.hub_id)
        self.assertRegex(frame["stream_id"], r"\A[0-9a-f]{32}\Z")
        cursor = frame["cursor"]
        self.assertEqual(set(cursor), {"version", "team_id", "recipient_server_id", "through_sequence", "arrival_id", "reset"})
        self.assertEqual(MailArrival.from_dict(cursor).mailbox, (self.team, self.recipient))
        self.assertIs(type(cursor["reset"]), bool)
        if message is not None:
            self.assertEqual((cursor["through_sequence"], cursor["arrival_id"]), (message["sequence"], message["id"]))

    def commit(self, **extra):
        return self.hub.create_team_message(self.host, self.team, {"kind": "message",
            "title": "Secret title stays out of hints", "body": "Private body stays out of hints",
            "recipients": [{"kind": "server", "id": self.recipient}],
            "idempotency_key": "pipeline-" + uuid.uuid4().hex, **extra})["message"]

    async def test_committed_recipient_mail_crosses_real_tls_runtime_and_local_socket(self):
        with mock.patch.object(self.hub, "list_team_messages", side_effect=AssertionError("passive Inbox read")), \
             mock.patch.object(self.hub, "record_team_message_receipt", side_effect=AssertionError("passive receipt")):
            frames, outgoing, disconnect = await self.connect_socket()
            snapshot = await asyncio.wait_for(outgoing.get(), 5)
            self.assert_frame(snapshot, "snapshot")
            self.assertTrue(snapshot["cursor"]["reset"])
            self.assertEqual(snapshot["cursor"]["through_sequence"], 0)
            self.assertEqual((self.runtime._host_in_flight, self.runtime._peer_in_flight), (0, 0))
            self.assertEqual(self.adapter._in_flight, {})
            self.assertEqual(self.tls.server._workers, set())
            self.assertTrue(self.tls.server._worker_slots.acquire(blocking=False))
            self.tls.server._worker_slots.release()
            before = self.dials
            with mock.patch.object(self.hub, "connect", wraps=self.hub.connect) as hub_connect, \
                 mock.patch.object(self.client, "_connect", wraps=self.client._connect) as peer_connect:
                await asyncio.sleep(.04)
                hub_connect.assert_not_called()
                peer_connect.assert_not_called()
            self.assertEqual(self.dials, before)
            ordinary = self.commit()
            self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint", ordinary)
            broadcast = self.commit(recipients=[{"kind": "all_servers"}])
            self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint", broadcast)
            self.commit(recipients=[{"kind": "all"}])
            own_host = self.hub.team_mail_arrival_snapshot(self.host, self.team)["recipient_server_id"]
            self.commit(recipients=[{"kind": "server", "id": own_host}])
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(outgoing.get(), .05)
            self.assertEqual(self.dials, before, "mail commits must not trigger request or poll lanes")
            self.assertEqual(len(frames), 3)
            self.assertNotIn("Secret title", json.dumps(frames))
            self.assertNotIn("Private body", json.dumps(frames))
            retained = MailArrival.from_dict(frames[-1]["cursor"]).as_dict()
            await disconnect()
            self.tls.assert_settled()
            offline = self.commit()
            reopened, outgoing, disconnect = await self.connect_socket(retained)
            restored = await asyncio.wait_for(outgoing.get(), 5)
            self.assert_frame(restored, "snapshot", offline)
            self.assertFalse(restored["cursor"]["reset"])
            self.assertNotEqual(restored["stream_id"], snapshot["stream_id"])
            await disconnect()
            self.tls.assert_settled()

        # The sole Inbox request is an explicit fresh page, never a hint effect.
        with mock.patch.object(self.hub, "list_team_messages", wraps=self.hub.list_team_messages) as inbox:
            response = self.client.proxy(self.connection_id, "GET", f"/v1/teams/{self.team}/network/messages",
                query=urlencode({"box": "inbox", "include_mailbox_coverage": "1", "limit": "1"}))
            self.assertEqual(response.status, 200)
            inbox.assert_called_once()
        page = json.loads(response.body)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["mailbox_coverage"], MailArrival(self.team, self.recipient,
            ordinary["sequence"], ordinary["id"]).as_dict())
        self.assertLess(page["mailbox_coverage"]["through_sequence"], restored["cursor"]["through_sequence"])
        golden = Path.cwd() / "mail-pipeline-golden.json"
        golden.write_text(json.dumps({"frames": frames + reopened, "coverage": page["mailbox_coverage"]}, indent=2) + "\n")
        print("Mail pipeline metadata golden:", golden, flush=True)

    async def test_actual_tls_pre_header_adapter_denials_preserve_status_and_capacity(self):
        for method in (self.adapter.team_mail_hint_snapshot, self.adapter.subscribe_team_mail_hints):
            with self.subTest(method=method.__name__), mock.patch.object(self.adapter, "_claims",
                    side_effect=HubError("forbidden", "Fixture revoked membership", 403)):
                with self.assertRaises(SecurePeerError) as rejected:
                    method(self.tls.peer)
                self.assertEqual((rejected.exception.code, rejected.exception.status_code), ("forbidden", 403))
        with mock.patch.object(self.adapter, "_admit", side_effect=HubError("rate_limited", "Fixture capacity", 429)):
            with self.assertRaises(SecurePeerError) as rejected:
                self.client.team_mail_hint_snapshot(self.connection_id)
            self.assertEqual(rejected.exception.status_code, 429)
        for failure, status in ((HubError("forbidden", "Fixture revoked", 403), 403),
                (MailHintClosed("Fixture authority changed"), 403), (MailHintCapacity("Fixture full"), 429)):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(self.hub,
                    "subscribe_team_mail_arrivals", side_effect=failure):
                with self.assertRaises(SecurePeerError) as rejected:
                    self.client.open_mail_hint_stream(self.connection_id)
                self.assertEqual(rejected.exception.status_code, status)
                self.assertEqual(self.adapter._mail_leases, {})
        self.adapter._revoking.add(self.tls.peer.peer_id)
        for operation in (self.client.team_mail_hint_snapshot, self.client.open_mail_hint_stream):
            with self.assertRaises(SecurePeerError) as rejected:
                operation(self.connection_id)
            self.assertEqual(rejected.exception.status_code, 403)
        self.tls.assert_settled()
