"""Actual Mail store/adapter/runtime coordinator and ASGI handler in memory.

Run through public_chat_share_safe_tests.py. Only the peer socket is replaced
by an adapter-backed blocking stream; no monolith, TLS, listener or provider.
"""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest import mock
from urllib.parse import urlencode

from agentsdock_team_hub.mail_hints import MailArrival, MailHintBroker, MailHintClosed
from agentsdock_team_hub.mail_hint_streams import MailHintLease, owned_mail_snapshot
from agentsdock_team_hub.secure_peer import PeerAuthorization, SecurePeerError, sanitize_proxy_request
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from team_mail_runtime import RuntimeMailHints
from team_mail_websocket import MAIL_WEBSOCKET_PROTOCOL, _MailSocketWriter, serve_team_mail_hints
from secure_peer_runtime import SecurePeerRuntime
import test_team_mail_hints_isolated as fixtures


class LeaseTests(unittest.TestCase):
    def lease(self, expires=None, clock=time.time):
        broker = MailHintBroker()
        sub = broker.subscribe("team_test", "server_test")
        cursor = MailArrival("team_test", "server_test", 0, None)
        authorize = mock.Mock()
        lease = MailHintLease(sub, cursor.as_dict(reset=True), hub_id="hub_test",
            authorize=authorize, expires_at=expires, clock=clock)
        self.addCleanup(lease.close)
        return broker, lease, authorize

    def test_idle_has_no_authority_queries_and_close_wakes_reader(self):
        _broker, lease, authorize = self.lease()
        result = []
        started = threading.Event()
        reader = threading.Thread(target=lambda: (started.set(), result.append(lease.take())), daemon=True)
        reader.start()
        self.assertTrue(started.wait(1))
        self.assertFalse(threading.Event().wait(.02))
        authorize.assert_not_called()
        lease.close()
        reader.join(1)
        self.assertFalse(reader.is_alive())
        self.assertEqual(result, [None])

    def test_expiry_no_idle_query_and_pending_coalesces(self):
        broker, lease, authorize = self.lease(expires=11, clock=lambda: 10)
        for seq in range(1, 101):
            broker.publish(MailArrival("team_test", "server_test", seq, f"tmsg_{seq:032x}"))
        self.assertEqual(lease.take(.1).through_sequence, 100)
        authorize.assert_not_called()
        lease._clock = lambda: 11
        self.assertIsNone(lease.take())
        self.assertTrue(lease.closed)

    def test_close_aborts_and_drains_actual_writer_then_forbids_send(self):
        _broker, lease, _authorize = self.lease()
        began, release, settled = threading.Event(), threading.Event(), threading.Event()
        def write(_cursor):
            began.set()
            self.assertTrue(release.wait(1))
            settled.set()
        lease.set_aborter(release.set)
        writer = threading.Thread(target=lambda: lease.write(write, lease.snapshot), daemon=True)
        writer.start()
        self.assertTrue(began.wait(1))
        lease.close()
        self.assertTrue(settled.is_set())
        writer.join(1)
        with self.assertRaises(MailHintClosed):
            lease.write(lambda _cursor: self.fail("late send"), lease.snapshot)


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TeamMailArrivalStoreTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.adapter = SecurePeerHubAdapter(self.f.store)
        self.peer = PeerAuthorization(self.f.peer.peer_id, "pairing-fixture", "mail-hint-peer-recipient",
            self.f.team, frozenset({"teamspace.read", "teamspace.write"}), "fingerprint-fixture",
            int(time.time()) + 3600, "recipient")

    def test_adapter_passive_budget_and_revoke_do_not_hold_request_slots(self):
        one = self.adapter.subscribe_team_mail_hints(self.peer)
        two = self.adapter.subscribe_team_mail_hints(self.peer)
        self.assertEqual(self.adapter._in_flight, {})
        with self.assertRaises(Exception):
            self.adapter.subscribe_team_mail_hints(self.peer)
        self.f.send()
        self.assertGreater(one.take(.5).through_sequence, 0)
        self.adapter.revoke_peer(peer_id=self.peer.peer_id, team_id=self.peer.team_id)
        self.assertTrue(one.closed)
        self.assertTrue(two.closed)
        self.assertEqual(self.adapter._mail_leases, {})
        self.assertEqual(self.adapter._in_flight, {})

    def test_foreign_retained_recipient_resets_without_reading_old_anchor(self):
        old = MailArrival(self.f.team, "server_old", 999, "tmsg_" + "f" * 32).as_dict()
        with mock.patch.object(self.f.store, "_team_mail_anchor_matches", side_effect=AssertionError("old mailbox read")):
            snapshot, retained = owned_mail_snapshot(self.f.store, self.f.peer, self.f.team, old)
        self.assertIsNone(retained)
        self.assertTrue(snapshot["reset"])
        self.assertEqual(snapshot["recipient_server_id"], self.f.address)
        with self.assertRaises(ValueError):
            owned_mail_snapshot(self.f.store, self.f.peer, self.f.team, {**old, "team_id": "team_other"})

    def test_actual_secure_query_and_adapter_forward_fresh_capped_and_unproven_pages(self):
        first, second = self.f.send(), self.f.send()
        path = f"/v1/teams/{self.f.team}/network/messages"
        def page(**query):
            request = sanitize_proxy_request(self.peer, "GET", path,
                urlencode({"box": "inbox", "include_mailbox_coverage": "1", **query}), (), b"")
            result = self.adapter.forward(request)
            self.assertEqual(result.status, 200)
            return json.loads(result.body)
        limited = page(limit=1)
        self.assertTrue(limited["has_more"])
        self.assertEqual(limited["mailbox_coverage"]["arrival_id"], first["id"])
        complete = page(after_sequence=first["sequence"], after_arrival_id=first["id"])
        self.assertFalse(complete["has_more"])
        self.assertEqual(complete["mailbox_coverage"]["arrival_id"], second["id"])
        self.assertNotIn("mailbox_coverage", page(after_sequence=first["sequence"]))
        self.assertNotIn("mailbox_coverage", page(after_sequence=first["sequence"], after_arrival_id="tmsg_" + "f" * 32))
        self.assertNotIn("mailbox_coverage", page(unread="1"))

    def member(self):
        test = self
        active = {"connection_id": "connection_fixture", "status": "connected", "scopes": ["teamspace.read"],
            "team_id": self.f.team, "hub_id": self.f.store.hub_id, "host_server_identity": "mail-hint-host",
            "certificate_fingerprint": "fingerprint-fixture", "certificate_expires_at": int(time.time()) + 3600}
        realm = {**active, "realm": "secure_peer"}
        class Stream:
            def __init__(self):
                self.lease = test.adapter.subscribe_team_mail_hints(test.peer)
                self.first = True
            def read(self):
                if self.first:
                    self.first = False
                    return {"type": "snapshot", "hub_id": self.lease.hub_id, "cursor": self.lease.snapshot}
                item = self.lease.take()
                return None if item is None else {"type": "hint", "hub_id": self.lease.hub_id, "cursor": item.as_dict(reset=False)}
            def close(self):
                self.lease.close()
        client = SimpleNamespace(open_mail_hint_stream=mock.Mock(side_effect=lambda _id: Stream()),
            mail_hint_capability=mock.Mock(return_value=True),
            team_mail_hint_snapshot=mock.Mock(side_effect=lambda _id, previous: test.adapter.team_mail_hint_snapshot(test.peer, previous)))
        runtime = SimpleNamespace(client=client, _completion_closing=False, _team_authority_epoch="epoch-fixture",
            _host_role_active=False, team_realms=lambda: [realm], _require_active_proxy_connection=lambda _id: active)
        manager = RuntimeMailHints(runtime, enabled=True)
        self.addCleanup(manager.invalidate)
        return manager, client, runtime

    def test_member_single_upstream_each_anchor_proven_no_idle_reads_or_inbox(self):
        manager, client, _runtime = self.member()
        one = manager.subscribe(self.f.team)
        two = manager.subscribe(self.f.team, MailArrival.from_dict(one.snapshot).as_dict())
        self.assertEqual(client.open_mail_hint_stream.call_count, 1)
        self.assertEqual(client.team_mail_hint_snapshot.call_count, 2)
        with mock.patch.object(self.f.store, "connect", wraps=self.f.store.connect) as connect:
            threading.Event().wait(.03)
            connect.assert_not_called()
        sent = self.f.send()
        self.assertEqual(one.take(1).through_sequence, sent["sequence"])
        self.assertEqual(two.take(1).through_sequence, sent["sequence"])
        feed = manager.member
        one.close()
        self.assertFalse(feed.closed)
        two.close()
        feed.thread.join(1)
        self.assertTrue(feed.closed)
        self.assertFalse(feed.thread.is_alive())
        self.assertEqual(self.adapter._in_flight, {})

    def test_member_upstream_failure_and_role_change_retire_exact_cohort(self):
        manager, client, runtime = self.member()
        lease = manager.subscribe(self.f.team)
        feed = manager.member
        feed.close()
        self.assertIsNone(lease.take(.5))
        lease = manager.subscribe(self.f.team)
        self.assertEqual(client.open_mail_hint_stream.call_count, 2)
        runtime._team_authority_epoch = "new-role"
        manager.invalidate()
        self.assertTrue(lease.closed)
        self.assertIsNone(manager.member)

    def test_member_authentication_failure_is_terminal_not_reconnect_polling(self):
        manager, client, _runtime = self.member()
        client.open_mail_hint_stream.side_effect = SecurePeerError("forbidden", "Private peer detail", 403)
        with self.assertRaises(MailHintClosed) as rejected:
            manager.subscribe(self.f.team)
        self.assertEqual(rejected.exception.status_code, 403)
        self.assertNotIn("Private peer detail", str(rejected.exception))
        self.assertEqual(client.open_mail_hint_stream.call_count, 1)
        self.assertEqual(client.team_mail_hint_snapshot.call_count, 0)
        self.assertEqual(manager.leases, set())

    def test_old_host_missing_lane_is_terminal_and_makes_no_snapshot_probe(self):
        manager, client, _runtime = self.member()
        client.open_mail_hint_stream.side_effect = SecurePeerError("not_found", "Old host", 404)
        with self.assertRaises(MailHintClosed) as rejected:
            manager.subscribe(self.f.team)
        self.assertEqual(rejected.exception.status_code, 501)
        self.assertEqual(client.open_mail_hint_stream.call_count, 1)
        self.assertEqual(client.team_mail_hint_snapshot.call_count, 0)

    def test_default_disabled_does_not_discover_or_start(self):
        runtime = mock.Mock()
        manager = RuntimeMailHints(runtime)
        capability = manager.capability()
        self.assertFalse(capability["enabled"])
        self.assertIsNone(capability["mailbox"])
        self.assertEqual(set(capability), {"enabled", "version", "websocket_path", "websocket_protocol", "mailbox_coverage", "mailbox"})
        with self.assertRaises(MailHintClosed):
            manager.subscribe("team_example")
        runtime.assert_not_called()
        runtime.team_realms.assert_not_called()

    def test_unnegotiated_member_never_starts_worker_or_probes_endpoint(self):
        manager, client, runtime = self.member()
        original_realms = runtime.team_realms
        runtime.team_realms = mock.Mock(side_effect=original_realms)
        for value in (False, None, 1, "true"):
            with self.subTest(value=value), mock.patch("team_mail_runtime.threading.Thread") as thread:
                client.mail_hint_capability.return_value = value
                self.assertFalse(manager.capability()["enabled"])
                self.assertIsNone(manager.capability()["mailbox"])
                with self.assertRaises(MailHintClosed) as rejected:
                    manager.subscribe(self.f.team)
                self.assertEqual(rejected.exception.status_code, 501)
                thread.assert_not_called()
                client.open_mail_hint_stream.assert_not_called()
                client.team_mail_hint_snapshot.assert_not_called()
                client.mail_hint_capability.assert_called_with("connection_fixture", "fingerprint-fixture")
        before = runtime.team_realms.call_count
        manager.capability()
        manager.capability()
        self.assertEqual(runtime.team_realms.call_count, before)
        client.mail_hint_capability.return_value = True
        self.assertTrue(manager.capability()["enabled"])
        self.assertEqual(runtime.team_realms.call_count, before)

    def test_expired_negotiation_hides_cached_descriptor_and_fences_send(self):
        manager, client, _runtime = self.member()
        lease = manager.subscribe(self.f.team)
        self.assertTrue(manager.capability()["enabled"])
        client.mail_hint_capability.return_value = False
        self.assertFalse(manager.capability()["enabled"])
        with self.assertRaises(MailHintClosed) as rejected:
            lease.write(lambda _cursor: self.fail("unnegotiated write"), lease.snapshot)
        self.assertEqual(rejected.exception.status_code, 501)
        lease.close()

    def host_runtime(self):
        runtime = SecurePeerRuntime(Path(self.f.store.data_dir).parent / "runtime",
            server_identity="mail-hint-host", server_instance_id="mail-host-fixture", mail_hints_enabled=True)
        self.addCleanup(runtime.shutdown)
        runtime.attach_host_hub(hub_id=self.f.store.hub_id, hub_data_dir=self.f.store.data_dir, hub_store=self.f.store)
        return runtime

    def test_actual_host_runtime_delivers_and_maintenance_does_not_wait_on_stream(self):
        runtime = self.host_runtime()
        lease = runtime.subscribe_team_mail_hints(self.f.team)
        self.assertEqual((runtime._host_in_flight, runtime._peer_in_flight), (0, 0))
        target = lease.snapshot["recipient_server_id"]
        result = self.f.store.create_team_message(self.f.peer, self.f.team,
            self.f.payload(recipients=[{"kind": "server", "id": target}]))
        self.assertEqual(lease.take(.5).through_sequence, result["message"]["sequence"])
        runtime.close_host_admission()
        self.assertTrue(lease.closed)
        runtime.reopen_host_admission()
        replacement = runtime.subscribe_team_mail_hints(self.f.team)
        self.assertFalse(replacement.closed)
        replacement.close()

    def test_snapshot_straddling_close_and_reopen_cannot_register_old_generation(self):
        runtime = self.host_runtime()
        original = self.f.store.subscribe_team_mail_arrivals
        def raced(*args, **kwargs):
            result = original(*args, **kwargs)
            runtime.close_host_admission()
            runtime.reopen_host_admission()
            return result
        with mock.patch.object(self.f.store, "subscribe_team_mail_arrivals", side_effect=raced):
            with self.assertRaises(MailHintClosed):
                runtime.subscribe_team_mail_hints(self.f.team)
        self.assertEqual(runtime._mail_hints.leases, set())
        self.assertEqual(self.f.store.mail_hint_broker._count, 0)


class WebsocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_resistant_asgi_writer_keeps_lease_fence_until_task_settles(self):
        loop = asyncio.get_running_loop()
        writer = _MailSocketWriter(loop, timeout=.02)
        began, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        worker_done, close_done = asyncio.Event(), asyncio.Event()
        broker = MailHintBroker()
        sub = broker.subscribe("team_fixture", "node_fixture")
        lease = MailHintLease(sub, MailArrival("team_fixture", "node_fixture", 0, None).as_dict(reset=True),
            hub_id="hub_fixture", authorize=lambda: None, expires_at=None)
        lease.set_aborter(writer.cancel)

        async def resistant_send():
            began.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()

        def consume():
            try:
                lease.write(lambda _cursor: writer.send(resistant_send), lease.snapshot)
            finally:
                loop.call_soon_threadsafe(worker_done.set)
        def close():
            lease.close()
            loop.call_soon_threadsafe(close_done.set)
        worker = threading.Thread(target=consume, daemon=True)
        worker.start()
        await asyncio.wait_for(began.wait(), 1)
        await asyncio.wait_for(cancelled.wait(), 1)
        closer = threading.Thread(target=close, daemon=True)
        closer.start()
        try:
            await asyncio.sleep(.03)
            self.assertFalse(worker_done.is_set())
            self.assertFalse(close_done.is_set())
            self.assertTrue(worker.is_alive())
        finally:
            release.set()
        await asyncio.wait_for(worker_done.wait(), 1)
        await asyncio.wait_for(close_done.wait(), 1)
        worker.join(.1)
        closer.join(.1)

    async def test_task_cancelled_before_first_step_still_releases_dedicated_writer(self):
        loop = asyncio.get_running_loop()
        writer = _MailSocketWriter(loop, timeout=.1)
        writer.cancel()
        done = asyncio.Event()
        entered, errors = [], []
        async def send():
            entered.append(True)
        def run():
            try:
                writer.send(send)
            except BaseException as exc:
                errors.append(type(exc))
            finally:
                loop.call_soon_threadsafe(done.set)
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        await asyncio.wait_for(done.wait(), 1)
        worker.join(.1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(entered, [])
        self.assertEqual(errors, [asyncio.CancelledError])

    async def test_actual_handler_snapshot_hint_disconnect_and_no_idle_database_reads(self):
        fixture = RuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        manager, _client, _runtime = fixture.member()
        runtime = SimpleNamespace(team_mail_hint_capability=manager.capability, subscribe_team_mail_hints=manager.subscribe)
        frames, closed = [], []
        incoming = asyncio.Queue()
        changed = asyncio.Event()
        await incoming.put(json.dumps({"version": 1, "team_id": fixture.f.team, "previous_cursor": None}))
        class Socket:
            query_params = {}
            async def accept(self, **kwargs): pass
            async def receive_text(self): return await incoming.get()
            async def send_text(self, value):
                frames.append(json.loads(value))
                changed.set()
            async def close(self, code): closed.append(code)
        task = asyncio.create_task(serve_team_mail_hints(Socket(), runtime,
            server_identity="member-fixture", authorized=lambda: True, protocols=[MAIL_WEBSOCKET_PROTOCOL]))
        self.addAsyncCleanup(self.stop_task, task, incoming)
        await asyncio.wait_for(changed.wait(), 3)
        self.assertEqual(frames[0]["type"], "snapshot")
        self.assertEqual(set(frames[0]), {"type", "server_identity", "hub_id", "stream_id", "cursor"})
        self.assertEqual(len(frames[0]["stream_id"]), 32)
        changed.clear()
        with mock.patch.object(fixture.f.store, "connect", wraps=fixture.f.store.connect) as connect:
            await asyncio.sleep(.03)
            connect.assert_not_called()
        fixture.f.send()
        await asyncio.wait_for(changed.wait(), 3)
        self.assertEqual(frames[-1]["type"], "hint")
        self.assertNotIn("Private body", json.dumps(frames))
        self.assertFalse(frames[-1]["cursor"]["reset"])
        await incoming.put("extra command forbidden")
        await asyncio.wait_for(task, 3)
        self.assertIn(1008, closed)
        self.assertEqual(manager.leases, set())

    async def stop_task(self, task, incoming):
        if not task.done():
            await incoming.put("stop")
            await asyncio.wait_for(task, 3)
