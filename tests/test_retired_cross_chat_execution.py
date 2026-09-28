"""Retired direct-turn routes cannot restart a model or discard user work."""

import unittest
from collections import deque
from unittest.mock import AsyncMock, Mock, patch

import agent_server
from tests import test_cross_chat_handoffs as fixtures


class RetiredCrossChatExecutionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.CrossChatStoreTests.asyncSetUp
    asyncTearDown = fixtures.CrossChatStoreTests.asyncTearDown
    create_exchange = fixtures.CrossChatStoreTests.create_exchange

    async def test_failure_notice_is_metadata_only_and_preserves_failure(self):
        exchange, leg = await self.create_exchange("notice_only")
        exchange, leg = await agent_server.CROSS_CHAT.finish_exchange_leg(
            leg["id"], status="failed", error_code="target_stopped", error="Recipient stopped",
        )
        provider = AsyncMock()
        with (
            patch.object(agent_server, "start_turn_durably", provider),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            for _ in range(2):
                await agent_server.maybe_deliver_cross_chat_exchange_failure_status(
                    exchange, failed_session_id="target", failed_leg=leg,
                )
        status_legs = [leg for leg in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
                       if leg["kind"] == "status"]
        self.assertEqual(len(status_legs), 1)
        self.assertEqual(status_legs[0]["status"], "delivered")
        self.assertEqual(status_legs[0]["error_code"], "target_stopped")
        self.assertEqual((await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["status"], "failed")
        self.assertFalse(agent_server.QUEUED_TURNS)
        provider.assert_not_awaited()

    async def test_restart_retires_status_behind_paused_user_without_provider(self):
        exchange, failed_leg = await self.create_exchange("persisted_status")
        exchange, _ = await agent_server.CROSS_CHAT.finish_exchange_leg(
            failed_leg["id"], status="failed", error_code="target_failed", error="Recipient failed",
        )
        leg, _ = await agent_server.CROSS_CHAT.create_exchange_status_leg(
            exchange_id=exchange["id"], source_session_id="target", target_session_id="source",
            body="A failure notification, not a user instruction", error_code="target_failed",
        )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"], expected={"registered"}, status="queued", queued_id="queued_status", queue_position=2,
        )
        # Re-open the persistent store and reconstruct the old queue event as startup does.
        agent_server.CROSS_CHAT = agent_server.CrossChatStore(self.root / "cross-chat.sqlite3")
        await agent_server.CROSS_CHAT.initialize()
        user = {"queued_id": "queued_user", "prompt": "My actual message", "_paused_after_stop": True}
        status = agent_server.queued_turn_from_event({
            "type": "turn_queued", "queued_id": "queued_status", "prompt": "Cross-chat status",
            "request_prompt": "INTERNAL STATUS INSTRUCTION MUST NEVER EXECUTE",
            "purpose": "cross_chat_handoff_delivery", "source_session_id": "target", "target_session_id": "source",
            "cross_chat_exchange_id": exchange["id"], "cross_chat_exchange_leg_id": leg["id"],
            "cross_chat_exchange_status": True,
        }, agent_server.STORE.sessions["source"], 2)
        agent_server.QUEUED_TURNS["source"] = deque([user, status])
        provider = AsyncMock()
        durable = AsyncMock()
        with (
            patch.object(agent_server, "start_turn_durably", provider),
            patch.object(agent_server, "append_durable_event", durable),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        self.assertEqual(list(agent_server.QUEUED_TURNS["source"]), [user])
        self.assertEqual((await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"]))["status"], "delivered")
        self.assertTrue(any(call.args[1] == "turn_unqueued" and call.args[2]["queued_id"] == "queued_status"
                            for call in durable.await_args_list))
        provider.assert_not_awaited()

    async def _assert_legacy_request_promotion_cancels_exact_message(self, *, run_now=False):
        exchange, leg = await self.create_exchange("retired_request")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"], expected={"registered"}, status="queued", queued_id="queued_legacy",
        )
        legacy = {
            "queued_id": "queued_legacy", "prompt": "OLD SOURCE INSTRUCTION MUST NOT EXECUTE",
            "purpose": "cross_chat_handoff_delivery", "source_session_id": "source", "target_session_id": "target",
            "cross_chat_exchange_id": exchange["id"], "cross_chat_exchange_leg_id": leg["id"],
            "client_capabilities": [], "_durable": True,
        }
        user = {"queued_id": "queued_user", "prompt": "My next message", "_paused_after_stop": True}
        agent_server.QUEUED_TURNS["target"] = deque([user] if run_now else [legacy, user])
        if run_now:
            agent_server.RUN_NOW_TURNS["target"] = legacy
        provider_preparation = Mock(side_effect=AssertionError("provider admission reached"))
        durable = AsyncMock()
        with (
            patch.object(agent_server, "schedule_cross_chat_exchange_failure_status_after_unlock", Mock()),
            patch.object(agent_server, "EXPLICIT_STOP_OPERATIONS", {}),
            patch.object(agent_server, "STEERING_SESSIONS", set()),
            patch.object(agent_server, "validate_session_file_ids", provider_preparation),
            patch.object(agent_server, "append_durable_event", durable),
            patch.object(agent_server, "append_event", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server._start_next_queued_turn_locked("target", admission_backend=None)
        retired = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertEqual(retired["status"], "cancelled")
        self.assertEqual(retired["error_code"], "legacy_route_disabled")
        self.assertEqual(retired["body"], "Please investigate")
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [user])
        provider_preparation.assert_not_called()
        self.assertTrue(any(call.args[1] == "turn_unqueued" and call.args[2]["queued_id"] == "queued_legacy"
                            for call in durable.await_args_list))

    async def test_legacy_request_queue_promotion_cancels_exact_message(self):
        await self._assert_legacy_request_promotion_cancels_exact_message()

    async def test_run_now_legacy_request_is_durably_removed_without_provider(self):
        await self._assert_legacy_request_promotion_cancels_exact_message(run_now=True)
        self.assertNotIn("target", agent_server.RUN_NOW_TURNS)

    async def test_direct_legacy_envelope_recovery_preserves_body_and_user_queue(self):
        record, _ = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_retired", source_session_id="source", source_run_id="run_source",
            target_session_id="target", body="Keep this authored message", idempotency_key="retired",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="queued", queued_id="queued_envelope",
        )
        legacy = {"queued_id": "queued_envelope", "cross_chat_envelope_id": record["id"]}
        user = {"queued_id": "queued_user", "prompt": "Keep this user message", "_paused_after_stop": True}
        agent_server.QUEUED_TURNS["target"] = deque([user, legacy])
        provider = AsyncMock()
        with (
            patch.object(agent_server, "start_turn_durably", provider),
            patch.object(agent_server, "append_durable_event", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_handoffs()
        retired = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(retired["status"], "cancelled")
        self.assertIn("legacy_route_disabled", retired["error"])
        self.assertEqual(retired["body"], "Keep this authored message")
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [user])
        provider.assert_not_awaited()

    async def test_mailbox_record_is_not_retired_or_submitted_to_provider(self):
        record = {"id": "mail_current", "delivery_mode": "mailbox", "body": "Current mail"}
        with (
            patch.object(agent_server.CROSS_CHAT, "update", AsyncMock()) as update,
            patch.object(agent_server, "start_turn_durably", AsyncMock()) as provider,
        ):
            self.assertEqual(await agent_server.submit_cross_chat_delivery(record), record)
        update.assert_not_awaited()
        provider.assert_not_awaited()

    async def test_queued_user_admission_ignores_retired_obligations(self):
        class ProviderLaunchReached(Exception):
            pass

        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="old_user_exchange", requester_session_id="target",
            authorization_source_run_id="queued_user", responder_session_id="source",
            max_legs=6, expires_at="2099-01-01T00:00:00Z",
        )
        await agent_server.CROSS_CHAT.update_exchange(
            "old_user_exchange", expected={"waiting_request"}, status="cancelled",
            error_code="legacy_route_disabled",
        )
        await agent_server.CROSS_CHAT.create_final_obligation(
            envelope_id="old_user_final", source_session_id="target", source_run_id="queued_user",
            target_session_id="source", idempotency_key="old_user_final",
        )
        await agent_server.CROSS_CHAT.update(
            "old_user_final", expected={"waiting_source"}, status="cancelled",
        )
        request = agent_server.TurnRequest(prompt="Continue my real queued work")
        durable = AsyncMock(side_effect=lambda _sid, kind, payload: {"type": kind, **payload})
        with (
            patch.object(agent_server, "managed_server_update_admission_blocker", return_value=None),
            patch.object(agent_server, "turn_start_blocker", AsyncMock(return_value=None)),
            patch.object(agent_server, "ensure_runtime_available", AsyncMock(return_value={"backend": "claude", "status": "ready"})),
            patch.object(agent_server.STORE, "mark_backend_started", AsyncMock(return_value=agent_server.STORE.sessions["target"])),
            patch.object(agent_server, "codex_manifest_path", return_value=self.root / "manifest.json"),
            patch.object(agent_server, "build_turn_provider_prompt", return_value=request.prompt),
            patch.object(agent_server, "issue_cross_chat_capability", AsyncMock(return_value=None)),
            patch.object(agent_server, "provider_authority_runtime_env", AsyncMock(return_value={})),
            patch.object(agent_server, "append_durable_event", durable),
            patch.object(agent_server, "append_event", AsyncMock()),
            patch.object(agent_server, "scrub_tmux_global_secret_environment", side_effect=ProviderLaunchReached),
            patch.object(agent_server.CROSS_CHAT, "rebind_source_run", AsyncMock()) as rebind_final,
            patch.object(agent_server.CROSS_CHAT, "rebind_exchange_source_run", AsyncMock()) as rebind_exchange,
        ):
            with self.assertRaises(ProviderLaunchReached):
                await agent_server._start_turn_locked(
                    "target", request, queue_if_busy=False, queued_id="queued_user",
                    accepted_obligation_ids=["old_user_final"], accepted_exchange_ids=["old_user_exchange"],
                    accepted_provider_route_snapshot=[],
                )
        admission = next(call.args[2] for call in durable.await_args_list if call.args[1] == "turn_started")
        self.assertEqual(admission["prompt"], request.prompt)
        self.assertEqual(admission["queued_id"], "queued_user")
        self.assertEqual(admission["cross_chat_obligation_ids"], [])
        self.assertEqual(admission["cross_chat_exchange_ids"], [])
        rebind_final.assert_not_awaited()
        rebind_exchange.assert_not_awaited()

    async def test_legacy_success_does_not_create_automatic_reply(self):
        exchange, leg = await self.create_exchange("no_auto_reply")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"], expected={"registered"}, status="running", target_run_id="old_target_run",
        )
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server.CROSS_CHAT, "commit_exchange_response", AsyncMock()) as reply,
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "type": "turn_finished", "run_id": "old_target_run", "exchange_id": exchange["id"],
                "exchange_leg_id": leg["id"], "result_text": "Completed before retirement", "exit_code": 0,
            })
        reply.assert_not_awaited()
        self.assertEqual(len(await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])), 1)
        self.assertEqual((await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"]))["status"], "delivered")
        self.assertEqual((await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["status"], "cancelled")

    async def test_legacy_final_obligation_does_not_copy_source_answer(self):
        await agent_server.CROSS_CHAT.create_final_obligation(
            envelope_id="old_final", source_session_id="source", source_run_id="old_source_run",
            target_session_id="target", idempotency_key="old_final",
        )
        with patch.object(agent_server, "append_cross_chat_terminal_lifecycle", AsyncMock()):
            await agent_server.finalize_cross_chat_source_obligations({
                "type": "turn_finished", "run_id": "old_source_run", "exit_code": 0,
                "result_text": "This final must not be automatically forwarded",
            })
        retired = await agent_server.CROSS_CHAT.get("old_final")
        self.assertEqual(retired["status"], "cancelled")
        self.assertFalse(retired["body"])
