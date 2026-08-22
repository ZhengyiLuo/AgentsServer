from pathlib import Path
import os
import tempfile
import threading
import unittest
import uuid
from unittest import mock

from agentsdock_team_hub.secure_peer import PAIRING_STATUS_LIMIT, SecurePeerError
from secure_peer_runtime import SecurePeerRuntime


class SecurePeerRuntimeTests(unittest.TestCase):
    @staticmethod
    def incoming_pairing(status: str, created_at: int) -> dict:
        return {
            "pairing_id": str(uuid.uuid4()),
            "peer_server_identity": f"peer_{created_at}",
            "peer_display_name": f"Peer {created_at}",
            "transcript_hash": f"transcript-{created_at}",
            "sas_words": ["amber", "beacon", "cedar", "delta"],
            "status": status,
            "created_at": created_at,
            "expires_at": 2_000_000_000,
            "team_id": None,
            "scopes": [],
            "requested_scopes": ["teamspace.read"],
            "source_ip": "192.0.2.10",
            "source_endpoint": "192.0.2.10:50000",
            "peer_public_key_fingerprint": "sha256:" + "b" * 64,
        }

    @staticmethod
    def outgoing_pairing(created_at: int) -> dict:
        return {
            "connection_id": str(uuid.uuid4()),
            "pairing_id": str(uuid.uuid4()),
            "host_ip": "192.0.2.20",
            "port": 7851,
            "status": "connected",
            "active": True,
            "host_server_identity": "remote_server",
            "host_display_name": "Remote server",
            "host_ca_fingerprint": "sha256:" + "c" * 64,
            "transcript_hash": "outgoing-transcript",
            "sas_words": ["echo", "forest", "globe", "harbor"],
            "requested_scopes": ["teamspace.read"],
            "scopes": ["teamspace.read"],
            "created_at": created_at,
            "updated_at": created_at,
        }

    def test_disabled_host_status_does_not_advertise_live_certificate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )

            class DormantHost:
                ca_fingerprint = "sha256:" + "a" * 64
                server_certificate_expires_at = 2_000_000_000

                @staticmethod
                def list_pairings(*, team_id=None, status=None):
                    del team_id, status
                    return []

                @staticmethod
                def list_peers(*, team_id=None):
                    del team_id
                    return []

            runtime._host_store = DormantHost()
            runtime._config = {
                **runtime._config,
                "enabled": False,
                "advertised_host": None,
            }
            with mock.patch.object(
                runtime, "remote_route_delivery_available", return_value=False
            ):
                status = runtime.status()

            self.assertTrue(status["host"]["available"])
            self.assertFalse(status["host"]["enabled"])
            self.assertIsNone(status["host"]["pairing_link"])
            self.assertIsNone(status["host"]["certificate_expires_at"])
            runtime.mark_host_unavailable("Peer\nerror\x7fdetail")
            projected = runtime.status()["host"]["error"]
            self.assertNotIn("\n", projected)
            self.assertNotIn("\x7f", projected)
            runtime.shutdown()

    def test_status_preserves_511_pending_plus_one_outgoing_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            pending = [
                self.incoming_pairing("pending", index + 1)
                for index in range(PAIRING_STATUS_LIMIT - 1)
            ]
            outgoing = self.outgoing_pairing(PAIRING_STATUS_LIMIT + 1)

            class Host:
                ca_fingerprint = "sha256:" + "a" * 64
                server_certificate_expires_at = 2_000_000_000
                hub_id = "team-hub-test"

                @staticmethod
                def list_pairings(*, team_id=None, status=None):
                    del team_id, status
                    return pending

                @staticmethod
                def list_peers(*, team_id=None):
                    del team_id
                    return []

            class Client:
                @staticmethod
                def list_connections():
                    return [outgoing]

            runtime._host_store = Host()
            runtime.client = Client()
            with mock.patch.object(
                runtime, "remote_route_delivery_available", return_value=False
            ):
                status = runtime.status()
            self.assertEqual(len(status["pairings"]), PAIRING_STATUS_LIMIT)
            self.assertEqual(
                sum(
                    item["status"] == "pending_approval"
                    for item in status["pairings"]
                ),
                PAIRING_STATUS_LIMIT - 1,
            )
            self.assertIn(
                outgoing["pairing_id"],
                {item["id"] for item in status["pairings"]},
            )
            runtime.shutdown()

    def test_status_caps_terminal_history_after_preserving_actionable_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            pending = self.incoming_pairing("pending", 10_000)
            terminal = [
                self.incoming_pairing("rejected", index + 1)
                for index in range(PAIRING_STATUS_LIMIT + 88)
            ]
            outgoing = self.outgoing_pairing(10_001)

            class Host:
                ca_fingerprint = "sha256:" + "a" * 64
                server_certificate_expires_at = 2_000_000_000
                hub_id = "team-hub-test"

                @staticmethod
                def list_pairings(*, team_id=None, status=None):
                    del team_id, status
                    return [pending, *terminal]

                @staticmethod
                def list_peers(*, team_id=None):
                    del team_id
                    return []

            class Client:
                @staticmethod
                def list_connections():
                    return [outgoing]

            runtime._host_store = Host()
            runtime.client = Client()
            with mock.patch.object(
                runtime, "remote_route_delivery_available", return_value=False
            ):
                status = runtime.status()
            pairing_ids = {item["id"] for item in status["pairings"]}
            self.assertEqual(len(status["pairings"]), PAIRING_STATUS_LIMIT)
            self.assertIn(pending["pairing_id"], pairing_ids)
            self.assertIn(outgoing["pairing_id"], pairing_ids)
            self.assertIn(terminal[-1]["pairing_id"], pairing_ids)
            self.assertNotIn(terminal[0]["pairing_id"], pairing_ids)
            runtime.shutdown()

    def test_client_submit_uses_atomic_local_route_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            self.assertTrue(runtime.state_available())
            self.assertIsNone(runtime.state_error_code())
            connection_id = str(uuid.uuid4())
            source_route_id = str(uuid.uuid4())
            target_route_id = str(uuid.uuid4())
            request_id = str(uuid.uuid4())
            revision = "rev_" + "a" * 32
            expires_at = 2_000_000_000
            calls: list[dict] = []

            class Client:
                def submit_envelope(self, *_args, **_kwargs):
                    raise AssertionError("raw relay submit must not be used")

                def submit_envelope_from_published_route(
                    self, connection, **kwargs
                ):
                    calls.append({"connection": connection, **kwargs})
                    return {
                        "envelope_id": str(uuid.uuid4()),
                        "exchange_id": str(uuid.uuid4()),
                        "status": "queued",
                        "used_legs": 1,
                        "max_legs": 6,
                        "expires_at": expires_at,
                    }

            runtime.client = Client()
            snapshot = {
                "role": "client",
                "connection_id": connection_id,
                "source_server_identity": "server_identity_test",
                "source_chat_id": "chat-source",
                "source_route_id": source_route_id,
                "source_route_revision": revision,
                "target_server_identity": "server_remote",
                "target_route_id": target_route_id,
                "target_route_revision": "rev_" + "b" * 32,
                "action": "instruction",
            }
            published = {
                "connection_id": connection_id,
                "chat_id": "chat-source",
                "route_id": source_route_id,
                "revision": revision,
                "status": "active",
                "actions": ["instruction"],
            }
            with (
                mock.patch.object(
                    runtime, "remote_route_delivery_available", return_value=True
                ),
                mock.patch.object(runtime, "_client_delivery_ready", return_value=True),
                mock.patch.object(
                    runtime,
                    "_client_connection",
                    return_value={"connection_id": connection_id},
                ),
                mock.patch.object(
                    runtime, "_published_routes", return_value=[published]
                ),
            ):
                response = runtime.submit_remote_handoff(
                    snapshot,
                    body="hello",
                    action="instruction",
                    request_id=request_id,
                    expires_at=expires_at,
                    expected_used_legs=1,
                )
            self.assertEqual(response["used_legs"], 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["connection"], connection_id)
            self.assertEqual(calls[0]["source_route_id"], source_route_id)
            self.assertEqual(calls[0]["source_route_revision"], revision)
            self.assertEqual(calls[0]["source_chat_id"], "chat-source")
            self.assertEqual(calls[0]["action"], "instruction")
            runtime.shutdown()

    def test_corrupt_optional_state_is_quarantined_without_blocking_server(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "secure-peers"
            root.mkdir(mode=0o700)
            config = root / "host-config.json"
            config.write_text("not-json", encoding="utf-8")
            os.chmod(config, 0o600)

            runtime = SecurePeerRuntime(
                root,
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            status = runtime.status()
            self.assertFalse(runtime.state_available())
            self.assertEqual(
                runtime.state_error_code(), "secure_peer_state_unavailable"
            )
            self.assertFalse(status["host"]["available"])
            self.assertFalse(status["host"]["enabled"])
            self.assertIsNone(status["active_connection_id"])
            self.assertIn("safety validation", status["host"]["error"])
            self.assertFalse(runtime.remote_route_delivery_available())
            with self.assertRaises(SecurePeerError) as raised:
                runtime.begin_pairing(
                    host="192.0.2.10",
                    port=7851,
                    expected_ca_fingerprint=None,
                    request_id="52e36f23-50ff-42c7-aec8-269e0419cb06",
                    display_name="Peer",
                    requested_scopes=["teamspace.read"],
                )
            self.assertEqual(raised.exception.code, "secure_peer_state_unavailable")
            runtime.shutdown()

    def test_pending_outbound_fences_route_chat_and_connection_retirement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            connection_id = str(uuid.uuid4())
            route_id = str(uuid.uuid4())
            revision = "rev_" + "a" * 32
            snapshot = {
                "version": 1,
                "role": "client",
                "connection_id": connection_id,
                "source_server_identity": "server_identity_test",
                "source_chat_id": "chat-source",
                "source_route_id": route_id,
                "source_route_revision": revision,
                "target_server_identity": "server_remote",
                "target_route_id": str(uuid.uuid4()),
                "target_route_revision": "rev_" + "b" * 32,
                "action": "instruction",
            }
            route = {
                "connection_id": connection_id,
                "chat_id": "chat-source",
                "route_id": route_id,
                "revision": revision,
                "status": "active",
                "actions": ["instruction"],
            }
            with mock.patch.object(runtime, "_published_routes", return_value=[route]):
                runtime.prepare_outbound_handoff(
                    request_id=str(uuid.uuid4()),
                    source_session_id="chat-source",
                    source_run_id="run-source",
                    snapshot=snapshot,
                    body="deliver me",
                    action="instruction",
                    expires_at=2_000_000_000,
                )
            with self.assertRaises(SecurePeerError) as chat_blocked:
                runtime.revoke_routes_for_chat("chat-source")
            self.assertEqual(chat_blocked.exception.code, "outbound_handoff_pending")
            with self.assertRaises(SecurePeerError) as route_blocked:
                runtime.revoke_route(
                    route_id=route_id,
                    expected_connection_id=connection_id,
                    expected_revision=revision,
                    idempotency_key=str(uuid.uuid4()),
                )
            self.assertEqual(route_blocked.exception.code, "outbound_handoff_pending")
            with self.assertRaises(SecurePeerError) as connection_blocked:
                runtime.deactivate_connection(
                    connection_id,
                    expected_host_server_identity="server_remote",
                    expected_hub_id="hub_remote",
                )
            self.assertEqual(
                connection_blocked.exception.code,
                "connection_delivery_pending",
            )
            replacement_id = str(uuid.uuid4())
            with (
                mock.patch.object(
                    runtime,
                    "_outgoing_for_pairing",
                    return_value={
                        "connection_id": replacement_id,
                        "host_server_identity": "server-replacement",
                        "hub_id": "hub-replacement",
                    },
                ),
                mock.patch.object(
                    runtime.client,
                    "list_connections",
                    return_value=[
                        {"connection_id": connection_id, "active": True}
                    ],
                ),
                mock.patch.object(
                    runtime.client,
                    "set_active_connection",
                ) as set_active,
            ):
                with self.assertRaises(SecurePeerError) as switch_blocked:
                    runtime.activate_pairing(
                        str(uuid.uuid4()),
                        expected_connection_id=replacement_id,
                        expected_host_server_identity="server-replacement",
                        expected_hub_id="hub-replacement",
                    )
                self.assertEqual(
                    switch_blocked.exception.code,
                    "active_connection_conflict",
                )
                set_active.assert_not_called()
            runtime.shutdown()

    def test_begin_pairing_resumes_matching_persisted_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            outgoing = self.outgoing_pairing(123)
            with mock.patch.object(
                runtime.client,
                "begin_pairing",
                return_value=outgoing,
            ) as begin:
                result = runtime.begin_pairing(
                    host="192.0.2.20",
                    port=7851,
                    expected_ca_fingerprint=None,
                    request_id=str(uuid.uuid4()),
                    display_name="Test server",
                    requested_scopes=["teamspace.read"],
                )
            self.assertEqual(result["connection_id"], outgoing["connection_id"])
            self.assertTrue(begin.call_args.kwargs["resume_matching"])
            runtime.shutdown()

    def test_publishing_route_can_be_resolved_for_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            connection_id = str(uuid.uuid4())
            route_id = str(uuid.uuid4())
            revision = "rev_" + "8" * 32
            with mock.patch.object(
                runtime,
                "_published_routes",
                return_value=[{
                    "route_id": route_id,
                    "connection_id": connection_id,
                    "revision": revision,
                    "chat_id": "chat-publishing",
                    "status": "publishing",
                }],
            ):
                self.assertEqual(
                    runtime.route_local_chat(
                        route_id=route_id,
                        expected_connection_id=connection_id,
                        expected_revision=revision,
                    ),
                    "chat-publishing",
                )
            runtime.shutdown()

    def test_client_claim_prepare_linearizes_deactivate_and_forget(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            connection_id = str(uuid.uuid4())
            entered = threading.Event()
            release = threading.Event()
            envelope = {
                "envelope_id": str(uuid.uuid4()),
                "request_id": str(uuid.uuid4()),
                "team_id": str(uuid.uuid4()),
                "source_peer_id": str(uuid.uuid4()),
                "source_server_identity": "server_remote",
                "source_route_id": str(uuid.uuid4()),
                "source_route_revision": "rev_" + "1" * 32,
                "target_peer_id": None,
                "target_server_identity": "server_identity_test",
                "target_route_id": str(uuid.uuid4()),
                "target_route_revision": "rev_" + "2" * 32,
                "action": "instruction",
                "kind": "instruction",
                "exchange_id": str(uuid.uuid4()),
                "parent_envelope_id": None,
                "parent_leg": None,
                "used_legs": 1,
                "max_legs": 6,
                "expires_at": 2_000_000_000,
                "body": {"message": "deliver"},
            }

            def claim(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(5))
                return {"lease_token": "lease-token", "envelopes": [envelope]}

            claim_result: list[dict] = []
            retirement_errors: list[BaseException] = []
            with (
                mock.patch.object(
                    runtime, "remote_route_delivery_available", return_value=True
                ),
                mock.patch.object(
                    runtime.client,
                    "list_connections",
                    return_value=[{"connection_id": connection_id, "active": True}],
                ),
                mock.patch.object(runtime.client, "claim_inbox", side_effect=claim),
                mock.patch.object(
                    runtime,
                    "_resolve_claim_target",
                    return_value=("target-chat", envelope["team_id"]),
                ),
            ):
                claim_thread = threading.Thread(
                    target=lambda: claim_result.extend(
                        runtime.claim_deliveries_once(limit=1)
                    )
                )

                def deactivate() -> None:
                    try:
                        runtime.deactivate_connection(
                            connection_id,
                            expected_host_server_identity="server_remote",
                            expected_hub_id="hub_remote",
                        )
                    except BaseException as exc:
                        retirement_errors.append(exc)

                retirement_thread = threading.Thread(target=deactivate)
                claim_thread.start()
                self.assertTrue(entered.wait(5))
                retirement_thread.start()
                self.assertTrue(retirement_thread.is_alive())
                release.set()
                claim_thread.join(5)
                retirement_thread.join(5)

            self.assertEqual(len(claim_result), 1)
            self.assertEqual(len(retirement_errors), 1)
            self.assertIsInstance(retirement_errors[0], SecurePeerError)
            self.assertEqual(
                retirement_errors[0].code,
                "connection_delivery_pending",
            )
            with self.assertRaises(SecurePeerError) as forgetting:
                runtime.forget_connection(
                    connection_id,
                    expected_host_server_identity="server_remote",
                    expected_hub_id="hub_remote",
                    expected_certificate_fingerprint="sha256:" + "f" * 64,
                )
            self.assertEqual(forgetting.exception.code, "connection_delivery_pending")
            runtime.shutdown()

    def test_expired_prepared_delivery_is_terminalized_without_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = SecurePeerRuntime(
                Path(temporary) / "secure-peers",
                server_identity="server_identity_test",
                server_instance_id="server_instance_test",
                display_name="Test server",
            )
            envelope_id = "env_expired_prepared"
            runtime.delivery_ledger.prepare(
                {
                    "envelope_id": envelope_id,
                    "request_id": str(uuid.uuid4()),
                    "team_id": "team-test",
                    "source_peer_id": str(uuid.uuid4()),
                    "source_server_identity": "server-source",
                    "source_route_id": str(uuid.uuid4()),
                    "source_route_revision": "rev_" + "1" * 32,
                    "target_peer_id": None,
                    "target_server_identity": "server_identity_test",
                    "target_route_id": str(uuid.uuid4()),
                    "target_route_revision": "rev_" + "2" * 32,
                    "action": "instruction",
                    "kind": "instruction",
                    "exchange_id": str(uuid.uuid4()),
                    "parent_envelope_id": None,
                    "parent_leg": None,
                    "used_legs": 1,
                    "max_legs": 6,
                    "expires_at": 1,
                    "body": {"message": "expired"},
                },
                transport_role="client",
                connection_id=str(uuid.uuid4()),
                lease_token="lease." + "a" * 43,
                target_chat_id="chat-target",
            )
            self.assertEqual(runtime.recover_prepared_deliveries(), [])
            self.assertEqual(runtime.delivery(envelope_id)["state"], "failed")
            runtime.shutdown()


if __name__ == "__main__":
    unittest.main()
