"""Real temporary Hub/runtime objects, no listener/provider/monolith; guarded QA only."""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
import uuid

from agentsdock_team_hub.secure_peer import SecurePeerError
from agentsdock_team_hub.store import HubStore
from secure_peer_runtime import SecurePeerRuntime


class HostProviderAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="host-provider-admission-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = HubStore(self.root / "hub", managed_host_identity="host-admission-server")
        self.store.bootstrap_managed_network("Host admission test")
        self.claims = self.store.managed_server_claims()
        self.team = self.claims.team_id
        self.runtime = self.new_runtime("runtime")
        self.attach(self.runtime)
        self.realm = self.runtime.team_realm(self.team)
        self.path = f"/v1/teams/{self.team}/network/messages"
        self.assertFalse(self.runtime._peer_accepting)
        self.assertIsNone(self.runtime._gateway)
        self.assertFalse(self.runtime._host_admission_closed)

    def new_runtime(self, name):
        runtime = SecurePeerRuntime(self.root / name, server_identity="host-admission-server",
            server_instance_id="host-admission-instance", display_name="Test Host")
        self.assertFalse(runtime._config["enabled"])
        self.addCleanup(runtime.shutdown)
        return runtime

    def attach(self, runtime):
        runtime.attach_host_hub(hub_id=self.store.hub_id, hub_data_dir=self.store.data_dir, hub_store=self.store)

    def payload(self):
        return {"kind": "message", "body": "Passive host mail", "recipients": [{"kind": "all_servers"}],
            "idempotency_key": "host-admit-" + uuid.uuid4().hex}

    def post(self, runtime=None):
        return (runtime or self.runtime)._team_host_call(self.realm, "POST", self.path, {}, self.payload())

    def messages(self):
        with closing(self.store.connect()) as db:
            return db.execute("SELECT COUNT(*) FROM team_messages").fetchone()[0]

    def start(self, operation):
        result = {"started": threading.Event(), "done": threading.Event()}
        def work():
            result["started"].set()
            try:
                result["value"] = operation()
            except BaseException as exc:
                result["error"] = exc
            finally:
                result["done"].set()
        thread = threading.Thread(target=work, daemon=True)
        result["thread"] = thread
        thread.start()
        self.assertTrue(result["started"].wait(2))
        self.addCleanup(thread.join, 2)
        return result

    def settled(self, result):
        self.assertTrue(result["done"].wait(3), "worker did not settle")
        result["thread"].join(1)
        self.assertFalse(result["thread"].is_alive())
        return result

    def wait_closed(self, runtime=None):
        runtime = runtime or self.runtime
        with runtime._peer_admission:
            self.assertTrue(runtime._peer_admission.wait_for(lambda: runtime._host_admission_closed, timeout=2))

    def blocked(self, method):
        entered, release = threading.Event(), threading.Event()
        original = getattr(self.store, method)
        def operation(*args, **kwargs):
            entered.set()
            if not release.wait(3): raise AssertionError("test worker release timed out")
            return original(*args, **kwargs)
        self.addCleanup(release.set)
        return entered, release, mock.patch.object(self.store, method, side_effect=operation)

    def test_local_only_host_accepts_then_close_rejects_before_claims_and_reopens(self):
        self.post()
        self.runtime.close_host_admission()
        with mock.patch.object(self.store, "local_agent_mail_claims", side_effect=AssertionError("claims after close")):
            for method in ("GET", "POST", "DELETE"):
                with self.subTest(method=method), self.assertRaises(SecurePeerError) as closed:
                    self.runtime._team_host_call(self.realm, method, self.path, {}, self.payload())
                self.assertEqual(closed.exception.code, "hub_maintenance")
        self.assertEqual(self.messages(), 1)
        self.assertEqual(self.runtime._host_in_flight, 0)
        self.runtime.reopen_host_admission()
        self.assertFalse(self.runtime._peer_accepting)
        self.post()
        self.assertEqual(self.messages(), 2)

    def test_drain_waits_for_admitted_write_and_get_setup_before_releasing(self):
        for method, call in (
            ("create_team_message", self.post),
            ("local_agent_mail_claims", lambda: self.runtime._team_host_call(self.realm, "GET", self.path, {}, None)),
        ):
            self.runtime.reopen_host_admission()
            entered, release, patch = self.blocked(method)
            with patch:
                work = self.start(call)
                self.assertTrue(entered.wait(2))
                drain = self.start(self.runtime.close_host_admission)
                self.wait_closed()
                self.assertFalse(drain["done"].is_set())
                self.assertEqual(self.runtime._host_in_flight, 1)
                release.set()
                self.assertNotIn("error", self.settled(work))
                self.assertNotIn("error", self.settled(drain))
            self.assertEqual(self.runtime._host_in_flight, 0)

    def test_queued_authorized_write_observes_close_after_outbound_lock_wait(self):
        generation = self.runtime.team_authority_generation()
        with self.runtime._outbound_guard:
            worker = self.start(lambda: self.runtime.team_authorized_write(generation, self.post))
            self.runtime.close_host_admission()
        result = self.settled(worker)
        self.assertIsInstance(result.get("error"), SecurePeerError)
        self.assertEqual(result["error"].code, "hub_maintenance")
        self.assertEqual(self.messages(), 0)
        self.runtime.reopen_host_admission()
        self.runtime.team_authorized_write(generation, self.post)
        self.assertEqual(self.messages(), 1)

    def test_durable_fence_blocks_before_claims_even_if_memory_admission_is_open(self):
        self.store.maintenance_snapshot_and_fence("server-update", operation_id="host-admission-test")
        with mock.patch.object(self.store, "local_agent_mail_claims", side_effect=AssertionError("claims past fence")):
            for method in ("GET", "POST"):
                with self.subTest(method=method), self.assertRaises(SecurePeerError) as fenced:
                    self.runtime._team_host_call(self.realm, method, self.path, {}, self.payload())
                self.assertEqual(fenced.exception.code, "hub_maintenance")
        self.assertEqual(self.runtime._host_in_flight, 0)
        self.assertEqual(self.messages(), 0)

    def test_nested_write_reuses_exact_control_lease_and_settles_both_counts(self):
        observed = []
        original = self.store.create_team_message
        def create(*args, **kwargs):
            observed.append(self.runtime._host_in_flight)
            return original(*args, **kwargs)
        def outer():
            with self.runtime._host_store_operation(self.realm, write=True):
                self.assertEqual(self.runtime._host_in_flight, 1)
                self.post()
                self.assertEqual(self.runtime._host_in_flight, 1)
                with self.assertRaises(SecurePeerError):
                    with self.runtime._host_store_operation({**self.realm, "hub_id": "other"}, write=True): pass
        with mock.patch.object(self.store, "create_team_message", side_effect=create):
            result = self.settled(self.start(outer))
        self.assertNotIn("error", result)
        self.assertEqual(observed, [2])
        self.assertEqual(self.runtime._host_in_flight, 0)
        self.assertEqual(self.messages(), 1)

    def test_worker_caller_cancellation_cannot_release_drain_before_commit_settles(self):
        async def scenario():
            entered, release, patch = self.blocked("create_team_message")
            with patch:
                work = asyncio.create_task(asyncio.to_thread(self.post))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                work.cancel()
                with self.assertRaises(asyncio.CancelledError): await work
                drain = asyncio.create_task(asyncio.to_thread(self.runtime.close_host_admission))
                await asyncio.to_thread(self.wait_closed)
                self.assertFalse(drain.done())
                self.assertEqual(self.runtime._host_in_flight, 1)
                release.set()
                await asyncio.wait_for(drain, 3)
        asyncio.run(scenario())
        self.assertEqual(self.runtime._host_in_flight, 0)
        self.assertEqual(self.messages(), 1)

    def test_legacy_host_mail_counts_writes_and_preserves_stale_route_errors(self):
        recipient = self.store.get_network(self.claims, self.team)["servers"][0]["id"]
        profile = {**self.realm, "destination_kind": "server", "destination_id": recipient}
        for kind, method in (("message", "create_network_mailbox_item"), ("request", "create_network_request")):
            self.runtime.reopen_host_admission()
            def send():
                return self.runtime.send_agent_mail(profile, kind=kind, message="Legacy passive mail",
                    idempotency_key="legacy-" + uuid.uuid4().hex)
            entered, release, patch = self.blocked(method)
            with patch:
                work = self.start(send)
                self.assertTrue(entered.wait(2))
                drain = self.start(self.runtime.close_host_admission)
                self.wait_closed()
                self.assertFalse(drain["done"].is_set())
                release.set()
                self.assertNotIn("error", self.settled(work))
                self.assertNotIn("error", self.settled(drain))
            with mock.patch.object(self.store, method, side_effect=AssertionError("legacy write after close")):
                with self.assertRaises(SecurePeerError) as closed: send()
                self.assertEqual(closed.exception.code, "hub_maintenance")
        with self.assertRaises(SecurePeerError) as stale:
            self.runtime.send_agent_mail({**profile, "hub_id": "wrong"}, kind="message", message="wrong", idempotency_key="wrong-route")
        self.assertEqual(stale.exception.code, "team_mail_route_changed")

    def test_attachment_close_after_declaration_or_between_chunks_never_reports_completion(self):
        for close_after_first_chunk in (False, True):
            self.runtime.reopen_host_admission()
            data = b"bounded synthetic attachment"
            with tempfile.TemporaryFile(dir=self.root) as source:
                source.write(data)
                source.flush()
                descriptor = source.fileno()
                original_get = self.runtime._team_hub_get
                original_post = self.runtime._team_hub_post
                original_read = os.read
                uploaded = []
                reads = 0
                def post(*args, **kwargs):
                    result = original_post(*args, **kwargs)
                    uploaded.append(result["attachment"]["id"])
                    return {**result, "chunk_bytes": 4}
                def get(*args, **kwargs):
                    result = original_get(*args, **kwargs)
                    if not close_after_first_chunk: self.runtime.close_host_admission()
                    return result
                def read(fd, amount):
                    nonlocal reads
                    if fd == descriptor:
                        reads += 1
                        if close_after_first_chunk and reads == 2: self.runtime.close_host_admission()
                    return original_read(fd, amount)
                with mock.patch.object(self.runtime, "_team_hub_post", side_effect=post), \
                     mock.patch.object(self.runtime, "_team_hub_get", side_effect=get), \
                     mock.patch("secure_peer_runtime.os.read", side_effect=read):
                    with self.assertRaises(SecurePeerError) as closed:
                        self.runtime._team_upload_attachment_descriptor(self.realm, Path("synthetic.txt"), descriptor,
                            os.fstat(descriptor), hashlib.sha256(data).hexdigest(),
                            idempotency_key="attachment-" + uuid.uuid4().hex)
                self.assertEqual(closed.exception.code, "hub_maintenance")
                self.assertEqual(self.runtime._host_in_flight, 0)
                with closing(self.store.connect()) as db:
                    row = db.execute("SELECT state,received_bytes FROM team_attachments WHERE id=?", (uploaded[0],)).fetchone()
                self.assertEqual(row["state"], "uploading")
                self.assertEqual(row["received_bytes"], 4 if close_after_first_chunk else 0)

    def test_listener_disabled_or_failed_does_not_strand_local_host(self):
        self.runtime.configure_host(enabled=False, advertised_host=None, listen_port=7851)
        self.post()
        gateway = mock.Mock()
        gateway.start.side_effect = RuntimeError("synthetic listener failure")
        with mock.patch("secure_peer_runtime.SecurePeerGateway", return_value=gateway):
            with self.assertRaisesRegex(RuntimeError, "synthetic listener failure"):
                self.runtime.configure_host(enabled=True, advertised_host="192.0.2.44", listen_port=7851)
        self.assertIsNone(self.runtime._gateway)
        self.assertFalse(self.runtime._host_admission_closed)
        self.post()

    def test_listener_control_cannot_reopen_preexisting_later_or_shutdown_closure(self):
        self.runtime.close_host_admission()
        self.runtime.configure_host(enabled=False, advertised_host=None, listen_port=7851)
        self.assertTrue(self.runtime._host_admission_closed)
        self.runtime.reopen_host_admission()
        original = self.runtime._write_config
        def persist(value):
            original(value)
            self.runtime.close_host_admission()  # A later maintenance intent wins.
        with mock.patch.object(self.runtime, "_write_config", side_effect=persist):
            self.runtime.configure_host(enabled=False, advertised_host=None, listen_port=7851)
        self.assertTrue(self.runtime._host_admission_closed)
        self.runtime.shutdown()
        self.runtime.reopen_host_admission()
        self.assertTrue(self.runtime._host_admission_closed)

    def test_concurrent_listener_controls_serialize_closure_ownership(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        original = self.runtime._write_config
        calls = 0
        def persist(value):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                if not release.wait(3): raise AssertionError("configuration release timed out")
            original(value)
        configure = lambda: self.runtime.configure_host(enabled=False, advertised_host=None, listen_port=7851)
        with mock.patch.object(self.runtime, "_write_config", side_effect=persist):
            first = self.start(configure)
            self.assertTrue(entered.wait(2))
            second = self.start(configure)
            release.set()
            self.assertNotIn("error", self.settled(first))
            self.assertNotIn("error", self.settled(second))
        self.assertEqual(calls, 2)
        self.assertFalse(self.runtime._host_admission_closed)
        self.post()

    def test_background_initial_attachment_retry_cannot_reopen_closed_gate(self):
        runtime = self.new_runtime("retry-runtime")
        with mock.patch.object(self.store, "provision_local_agent_mail", side_effect=RuntimeError("initial projection failed")):
            with self.assertRaises(RuntimeError): self.attach(runtime)
        self.assertIsNone(runtime._hub_store)
        runtime.close_host_admission()
        self.assertTrue(runtime.retry_host_attachment())
        self.assertTrue(runtime._host_admission_closed)
        with self.assertRaises(SecurePeerError): self.post(runtime)
        runtime.reopen_host_admission()
        self.post(runtime)


if __name__ == "__main__":
    unittest.main()
