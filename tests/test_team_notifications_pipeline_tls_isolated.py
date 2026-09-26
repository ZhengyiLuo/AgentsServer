"""V2 Hub-to-mTLS-to-Member-to-WebSocket acceptance using private socketpairs.

Run through public_chat_share_safe_tests.py. Actual credentials, authenticated
health negotiation, HTTP gateway/client, durable store, broker, runtime and
ASGI handler execute. No TCP listener, DNS, monolith or production state.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest import mock
import uuid

from starlette.websockets import WebSocketDisconnect

from agentsdock_team_hub.notification_hints import NotificationCursor
from team_mail_websocket import NOTIFICATION_WEBSOCKET_PROTOCOL, serve_team_mail_hints
from tests import test_team_mail_pipeline_tls_isolated as pipeline


class NotificationPipelineTLSAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    # Reuse only setup and helpers, without rerunning inherited v1 test methods.
    commit = pipeline.MailPipelineTLSAcceptanceTests.commit

    def setUp(self):
        pipeline.MailPipelineTLSAcceptanceTests.setUp(self)
        self.addCleanup(self.hub.notification_broker.close)
        self.assertFalse(self.runtime.team_notification_hint_capability()["enabled"])

        def subscribe(peer, previous):
            lease = self.adapter.subscribe_team_mail_hints(peer, previous, version=2)
            self.tls.leases.append(lease)
            return lease

        self.tls.gateway.notification_hint_subscriber = subscribe
        self.tls.gateway.notification_hint_snapshot = lambda peer, previous: self.adapter.team_mail_hint_snapshot(
            peer, previous, version=2)
        health = self.client.peer_health(self.connection_id)
        self.assertIs(health["mail_hints_available"], True)
        self.assertIs(health["mail_hints_v2_available"], True)
        self.assertTrue(self.client.notification_hint_capability(
            self.connection_id, self.tls.peer.certificate_fingerprint))
        capability = self.runtime.team_notification_hint_capability()
        self.assertTrue(capability["enabled"])
        self.assertEqual(capability["version"], 2)
        self.assertEqual(capability["websocket_path"], "/api/team-mail-hints/events")
        self.assertEqual(capability["websocket_protocol"], NOTIFICATION_WEBSOCKET_PROTOCOL)

    async def connect_notifications(self, previous=None):
        frames, closed = [], []
        incoming, outgoing = asyncio.Queue(), asyncio.Queue()
        await incoming.put(json.dumps({"version": 2, "team_id": self.team, "previous_cursor": previous}))
        owner = self

        class Socket:
            query_params = {}
            async def accept(self, **kwargs):
                owner.assertEqual(kwargs, {"subprotocol": NOTIFICATION_WEBSOCKET_PROTOCOL})
            async def receive_text(self):
                value = await incoming.get()
                if value is None:
                    raise WebSocketDisconnect()
                return value
            async def send_text(self, raw):
                frame = json.loads(raw)
                frames.append(frame)
                await outgoing.put(frame)
            async def close(self, code):
                closed.append(code)

        task = asyncio.create_task(serve_team_mail_hints(Socket(), self.runtime,
            server_identity="peer_tls_fixture", authorized=lambda: True,
            protocols=[NOTIFICATION_WEBSOCKET_PROTOCOL]))

        async def disconnect():
            if not task.done():
                await incoming.put(None)
                await asyncio.wait_for(task, 5)

        self.addAsyncCleanup(disconnect)
        return frames, outgoing, disconnect

    def assert_frame(self, frame, kind):
        self.assertEqual(set(frame), {"type", "server_identity", "hub_id", "stream_id", "cursor"})
        self.assertEqual(frame["type"], kind)
        self.assertEqual(frame["server_identity"], "peer_tls_fixture")
        self.assertEqual(frame["hub_id"], self.hub.hub_id)
        self.assertRegex(frame["stream_id"], r"\A[0-9a-f]{32}\Z")
        cursor = NotificationCursor.from_dict(frame["cursor"])
        self.assertEqual(cursor.mailbox, (self.team, self.recipient))
        if kind == "hint":
            self.assertFalse(cursor.mail_reset)
            self.assertFalse(cursor.bulletin_reset)
        return cursor

    def revise(self, message, version):
        return self.hub.revise_team_message(self.host, self.team, message["id"], {
            "body": f"Private revised body version {version + 1}", "body_format": "markdown",
            "expected_version": version, "idempotency_key": "revision-" + uuid.uuid4().hex})

    async def assert_quiet(self):
        before = self.dials
        with mock.patch.object(self.hub, "connect", wraps=self.hub.connect) as hub_connect, \
             mock.patch.object(self.client, "_connect", wraps=self.client._connect) as member_connect, \
             mock.patch.object(self.tls.store, "_connect", wraps=self.tls.store._connect) as host_peer_connect:
            await asyncio.sleep(.04)
            hub_connect.assert_not_called()
            member_connect.assert_not_called()
            host_peer_connect.assert_not_called()
        self.assertEqual(self.dials, before)

    async def test_bulletin_revision_mail_delete_and_durable_reconnect_cross_actual_tls(self):
        with mock.patch.object(self.hub, "list_team_messages", side_effect=AssertionError("passive Inbox read")), \
             mock.patch.object(self.hub, "get_team_message", side_effect=AssertionError("passive body read")), \
             mock.patch.object(self.hub, "record_team_message_receipt", side_effect=AssertionError("passive receipt")):
            frames, outgoing, disconnect = await self.connect_notifications()
            initial = await asyncio.wait_for(outgoing.get(), 5)
            baseline = self.assert_frame(initial, "snapshot")
            self.assertEqual((baseline.mail.through_sequence, baseline.bulletin.through_sequence), (0, 0))
            self.assertTrue(baseline.mail_reset)
            self.assertTrue(baseline.bulletin_reset)
            self.assertEqual((self.runtime._host_in_flight, self.runtime._peer_in_flight), (0, 0))
            self.assertEqual(self.adapter._in_flight, {})
            self.assertEqual(self.tls.server._workers, set())
            self.assertTrue(self.tls.server._worker_slots.acquire(blocking=False))
            self.tls.server._worker_slots.release()
            await self.assert_quiet()
            dials = self.dials

            bulletin = self.commit(recipients=[{"kind": "all"}])
            created = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint")
            self.assertEqual(created.mail, baseline.mail)
            self.assertEqual((created.bulletin.message_id, created.bulletin.change_kind,
                              created.bulletin.message_version), (bulletin["id"], "created", 1))

            self.revise(bulletin, 1)
            revised = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint")
            self.assertEqual(revised.mail, baseline.mail)
            self.assertEqual((revised.bulletin.message_id, revised.bulletin.change_kind,
                              revised.bulletin.message_version), (bulletin["id"], "revised", 2))
            self.assertGreater(revised.bulletin.through_sequence, created.bulletin.through_sequence)
            self.assertNotEqual(revised.bulletin.change_id, created.bulletin.change_id)

            direct = self.commit()
            delivered = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint")
            self.assertEqual((delivered.mail.through_sequence, delivered.mail.arrival_id),
                             (direct["sequence"], direct["id"]))
            self.assertEqual(delivered.bulletin, revised.bulletin)
            self.assertEqual(self.dials, dials, "commits must not open requests or polling lanes")
            await self.assert_quiet()
            self.assertTrue(outgoing.empty())
            retained = delivered.as_dict()
            await disconnect()
            self.tls.assert_settled()

            self.revise(bulletin, 2)
            offline_mail = self.commit()
            reopened, outgoing, disconnect = await self.connect_notifications(retained)
            recovered_frame = await asyncio.wait_for(outgoing.get(), 5)
            recovered = self.assert_frame(recovered_frame, "snapshot")
            self.assertFalse(recovered.mail_reset)
            self.assertFalse(recovered.bulletin_reset)
            self.assertEqual(recovered.mail.arrival_id, offline_mail["id"])
            self.assertEqual((recovered.bulletin.change_kind, recovered.bulletin.message_version), ("revised", 3))
            self.assertNotEqual(initial["stream_id"], recovered_frame["stream_id"])
            await self.assert_quiet()
            self.assertEqual(len(reopened), 1, "reconnect must emit one current snapshot without replayed hints")

            self.hub.delete_team_message(self.host, self.team, bulletin["id"],
                {"idempotency_key": "delete-" + uuid.uuid4().hex})
            deleted = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint")
            self.assertEqual(deleted.mail, recovered.mail)
            self.assertEqual((deleted.bulletin.message_id, deleted.bulletin.change_kind,
                              deleted.bulletin.message_version), (bulletin["id"], "deleted", 3))
            await disconnect()
            self.tls.assert_settled()

            restored_anchor = deleted.as_dict()
            restored_anchor["bulletin"]["change_id"] = "bchg_" + "f" * 32
            restored, outgoing, disconnect = await self.connect_notifications(restored_anchor)
            reset = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "snapshot")
            self.assertFalse(reset.mail_reset)
            self.assertTrue(reset.bulletin_reset)
            self.assertEqual(reset.mail, deleted.mail)
            self.assertEqual(reset.bulletin, deleted.bulletin)
            await self.assert_quiet()
            self.assertEqual(len(restored), 1)
            await disconnect()
            self.tls.assert_settled()

        all_frames = frames + reopened + restored
        self.assertNotIn("Secret title", json.dumps(all_frames))
        self.assertNotIn("Private body", json.dumps(all_frames))
        self.assertNotIn("Private revised body", json.dumps(all_frames))
        self.assertEqual(self.runtime._mail_hints.leases, set())
        self.assertEqual(self.adapter._mail_leases, {})

    async def test_v1_and_v2_sockets_share_one_actual_tls_upstream(self):
        legacy_frames, legacy_outgoing, legacy_disconnect = await pipeline.MailPipelineTLSAcceptanceTests.connect_socket(self)
        legacy_snapshot = await asyncio.wait_for(legacy_outgoing.get(), 5)
        pipeline.MailPipelineTLSAcceptanceTests.assert_frame(self, legacy_snapshot, "snapshot")
        self.assertEqual(self.runtime._mail_hints.member.version, 2)
        dials = self.dials
        frames, outgoing, disconnect = await self.connect_notifications()
        self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "snapshot")
        self.assertEqual(self.dials, dials + 1, "the second desktop needs only its finite cursor proof")
        self.assertEqual(len(self.tls.gateway._mail_streams), 1)
        self.assertEqual(len(self.tls.leases), 1)
        await self.assert_quiet()

        self.commit(recipients=[{"kind": "all"}])
        bulletin = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint")
        self.assertEqual(bulletin.mail.through_sequence, 0)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(legacy_outgoing.get(), .04)
        self.assertEqual(len(legacy_frames), 1)

        direct = self.commit()
        legacy_hint = await asyncio.wait_for(legacy_outgoing.get(), 5)
        pipeline.MailPipelineTLSAcceptanceTests.assert_frame(self, legacy_hint, "hint", direct)
        combined = self.assert_frame(await asyncio.wait_for(outgoing.get(), 5), "hint")
        self.assertEqual(combined.mail.arrival_id, direct["id"])
        self.assertEqual(combined.bulletin, bulletin.bulletin)
        self.assertEqual(self.dials, dials + 1)
        await legacy_disconnect()
        self.assertEqual(len(self.tls.gateway._mail_streams), 1)
        self.assertFalse(self.runtime._mail_hints.member.closed)
        await disconnect()
        self.tls.assert_settled()
        self.assertEqual(len(frames), 3)
