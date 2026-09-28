import asyncio
import json
import os
import tempfile
import time
import unittest
from collections import OrderedDict, deque
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

import agent_server


class CrossChatStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.original_cross_chat = agent_server.CROSS_CHAT
        self.original_authority_root = agent_server.CROSS_CHAT_AUTHORITY_ROOT
        self.original_sessions = agent_server.STORE.sessions
        self.original_current_turns = agent_server.CURRENT_TURNS
        self.original_queued_turns = agent_server.QUEUED_TURNS
        self.original_agent_token = agent_server.AGENT_TOKEN
        self.original_agent_relay_enabled = (
            agent_server.SECURE_PEER_AGENT_RELAY_ENABLED
        )
        self.original_busy_sessions = set(agent_server.BUSY_SESSIONS)
        self.original_run_now_turns = agent_server.RUN_NOW_TURNS
        self.original_queue_start_tasks = agent_server.QUEUE_START_TASKS
        self.original_cross_chat_event_type_cache = agent_server.CROSS_CHAT_EVENT_TYPE_CACHE
        self.original_session_lifecycle_locks = agent_server.SESSION_LIFECYCLE_LOCKS
        self.original_cross_chat_lifecycle_locks = (
            agent_server.CROSS_CHAT_LIFECYCLE_LOCKS
        )
        self.original_cross_chat_lifecycle_lock_refcounts = (
            agent_server.CROSS_CHAT_LIFECYCLE_LOCK_REFCOUNTS
        )
        self.original_cross_chat_exchange_locks = (
            agent_server.CROSS_CHAT_EXCHANGE_LOCKS
        )
        self.original_cross_chat_exchange_lock_refcounts = (
            agent_server.CROSS_CHAT_EXCHANGE_LOCK_REFCOUNTS
        )
        self.original_exchange_leg_admission_locks = (
            agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_LOCKS
        )
        self.original_exchange_leg_admission_owners = (
            agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_OWNERS
        )
        self.original_exchange_leg_admission_refcounts = (
            agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_REFCOUNTS
        )
        self.original_delivery_admission_locks = (
            agent_server.CROSS_CHAT_DELIVERY_ADMISSION_LOCKS
        )
        self.original_delivery_admission_owners = (
            agent_server.CROSS_CHAT_DELIVERY_ADMISSION_OWNERS
        )
        self.original_delivery_admission_refcounts = (
            agent_server.CROSS_CHAT_DELIVERY_ADMISSION_REFCOUNTS
        )
        self.original_live_lease_locks = agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS
        self.original_live_lease_lock_refcounts = (
            agent_server.CROSS_CHAT_LIVE_LEASE_LOCK_REFCOUNTS
        )
        self.original_live_response_waiters = agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS
        agent_server.AGENT_TOKEN = "test-admin-token"
        # Retain explicit coverage of the legacy relay protocol while normal
        # AgentsServer production keeps it disabled.
        agent_server.SECURE_PEER_AGENT_RELAY_ENABLED = True
        agent_server.CROSS_CHAT = agent_server.CrossChatStore(self.root / "cross-chat.sqlite3")
        await agent_server.CROSS_CHAT.initialize()
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.root / "authority"
        agent_server.STORE.sessions = {
            "source": {"id": "source", "title": "Source", "backend": "codex"},
            "target": {"id": "target", "title": "Target", "backend": "claude"},
        }
        agent_server.CURRENT_TURNS = {}
        agent_server.QUEUED_TURNS = {}
        agent_server.RUN_NOW_TURNS = {}
        agent_server.QUEUE_START_TASKS = {}
        agent_server.CROSS_CHAT_EVENT_TYPE_CACHE = OrderedDict()
        agent_server.SESSION_LIFECYCLE_LOCKS = {}
        agent_server.CROSS_CHAT_LIFECYCLE_LOCKS = {}
        agent_server.CROSS_CHAT_LIFECYCLE_LOCK_REFCOUNTS = {}
        agent_server.CROSS_CHAT_EXCHANGE_LOCKS = {}
        agent_server.CROSS_CHAT_EXCHANGE_LOCK_REFCOUNTS = {}
        agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_LOCKS = {}
        agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_OWNERS = {}
        agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_REFCOUNTS = {}
        agent_server.CROSS_CHAT_DELIVERY_ADMISSION_LOCKS = {}
        agent_server.CROSS_CHAT_DELIVERY_ADMISSION_OWNERS = {}
        agent_server.CROSS_CHAT_DELIVERY_ADMISSION_REFCOUNTS = {}
        agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS = {}
        agent_server.CROSS_CHAT_LIVE_LEASE_LOCK_REFCOUNTS = {}
        agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS = {}
        agent_server.BUSY_SESSIONS.clear()
        agent_server.CROSS_CHAT_CAPABILITIES.clear()

    async def asyncTearDown(self) -> None:
        agent_server.CROSS_CHAT_CAPABILITIES.clear()
        agent_server.CROSS_CHAT = self.original_cross_chat
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.original_authority_root
        agent_server.STORE.sessions = self.original_sessions
        agent_server.CURRENT_TURNS = self.original_current_turns
        agent_server.QUEUED_TURNS = self.original_queued_turns
        agent_server.RUN_NOW_TURNS = self.original_run_now_turns
        agent_server.QUEUE_START_TASKS = self.original_queue_start_tasks
        agent_server.CROSS_CHAT_EVENT_TYPE_CACHE = self.original_cross_chat_event_type_cache
        agent_server.SESSION_LIFECYCLE_LOCKS = self.original_session_lifecycle_locks
        agent_server.CROSS_CHAT_LIFECYCLE_LOCKS = (
            self.original_cross_chat_lifecycle_locks
        )
        agent_server.CROSS_CHAT_LIFECYCLE_LOCK_REFCOUNTS = (
            self.original_cross_chat_lifecycle_lock_refcounts
        )
        agent_server.CROSS_CHAT_EXCHANGE_LOCKS = (
            self.original_cross_chat_exchange_locks
        )
        agent_server.CROSS_CHAT_EXCHANGE_LOCK_REFCOUNTS = (
            self.original_cross_chat_exchange_lock_refcounts
        )
        agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_LOCKS = (
            self.original_exchange_leg_admission_locks
        )
        agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_OWNERS = (
            self.original_exchange_leg_admission_owners
        )
        agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_REFCOUNTS = (
            self.original_exchange_leg_admission_refcounts
        )
        agent_server.CROSS_CHAT_DELIVERY_ADMISSION_LOCKS = (
            self.original_delivery_admission_locks
        )
        agent_server.CROSS_CHAT_DELIVERY_ADMISSION_OWNERS = (
            self.original_delivery_admission_owners
        )
        agent_server.CROSS_CHAT_DELIVERY_ADMISSION_REFCOUNTS = (
            self.original_delivery_admission_refcounts
        )
        agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS = self.original_live_lease_locks
        agent_server.CROSS_CHAT_LIVE_LEASE_LOCK_REFCOUNTS = (
            self.original_live_lease_lock_refcounts
        )
        agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS = self.original_live_response_waiters
        agent_server.BUSY_SESSIONS.clear()
        agent_server.BUSY_SESSIONS.update(self.original_busy_sessions)
        agent_server.AGENT_TOKEN = self.original_agent_token
        agent_server.SECURE_PEER_AGENT_RELAY_ENABLED = (
            self.original_agent_relay_enabled
        )
        self.temporary.cleanup()

    async def create_exchange(
        self,
        exchange_id: str,
        *,
        source_run_id: str = "run_source",
    ) -> tuple[dict, dict]:
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id=exchange_id,
            requester_session_id="source",
            authorization_source_run_id=source_run_id,
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, leg, created = await agent_server.CROSS_CHAT.create_initial_exchange_leg(
            exchange_id=exchange_id,
            source_session_id="source",
            source_run_id=source_run_id,
            target_session_id="target",
            body="Please investigate",
            idempotency_key=f"ask:{exchange_id}",
        )
        self.assertTrue(created)
        return exchange, leg

    async def admit_exchange_test_start(
        self,
        session_id: str,
        request: agent_server.TurnRequest,
        *,
        run_id: str,
    ) -> dict:
        self.assertEqual(session_id, "target")
        agent_server.CURRENT_TURNS[session_id] = {
            "run_id": run_id,
            "cross_chat_exchange_id": request.cross_chat_exchange_id,
            "cross_chat_exchange_leg_id": request.cross_chat_exchange_leg_id,
        }
        admitted = await agent_server.admit_cross_chat_delivery_run(
            None,
            exchange_leg_id=request.cross_chat_exchange_leg_id,
            queued_id=None,
            run_id=run_id,
        )
        self.assertIsNotNone(admitted)
        return {"queued": False}

    @staticmethod
    def direct_grant_handle(
        authority_path: Path,
        *,
        target_session_id: str = "target",
        action: str = "instruction",
    ) -> str:
        token = json.loads(authority_path.read_text())["provider_capability"]
        token_hash = agent_server.hashlib.sha256(token.encode()).hexdigest()
        capability = agent_server.CROSS_CHAT_CAPABILITIES[token_hash]
        handles = [
            grant_id
            for grant_id, grant in capability["provider_direct_grants"].items()
            if grant == {
                "target_session_id": target_session_id,
                "action": action,
            }
        ]
        if len(handles) != 1:
            raise AssertionError("expected one exact provider direct grant")
        return handles[0]

    async def issue_live_waiter_owner(
        self,
        session_id: str,
        run_id: str,
    ) -> str:
        agent_server.CURRENT_TURNS[session_id] = {"run_id": run_id}
        authority = await agent_server.issue_cross_chat_capability(
            session_id,
            run_id,
            [],
            actions={"publish"},
        )
        return json.loads(authority.read_text())["provider_capability"]

    async def create_live_waiter(
        self,
        exchange_id: str,
        source_run_id: str,
    ) -> tuple[dict, dict, dict]:
        source_token = await self.issue_live_waiter_owner(
            "source",
            source_run_id,
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id=exchange_id,
            requester_session_id="source",
            authorization_source_run_id=source_run_id,
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, inbound, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id=exchange_id,
                source_session_id="source",
                source_run_id=source_run_id,
                target_session_id="target",
                body="Wait for this answer",
                idempotency_key=f"live:{exchange_id}",
                live_response_lease=True,
            )
        )
        async with agent_server.cross_chat_live_lease_lock(exchange_id):
            waiter = await agent_server.register_cross_chat_live_waiter_locked(
                exchange,
                inbound,
                owner_session_id="source",
                owner_run_id=source_run_id,
                capability_token=source_token,
            )
        return exchange, inbound, waiter

    async def deliver_terminal_live_answer(
        self,
        exchange: dict,
        inbound: dict,
        waiter: dict,
        *,
        body: str,
    ) -> tuple[dict, dict]:
        target_run_id = f"run_target_{exchange['id']}"
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id=target_run_id,
        )
        async with agent_server.cross_chat_live_lease_lock(exchange["id"]):
            exchange, outbound, _created = (
                await agent_server.CROSS_CHAT.commit_exchange_response(
                    exchange_id=exchange["id"],
                    inbound_leg_id=inbound["id"],
                    source_session_id="target",
                    source_run_id=target_run_id,
                    body=body,
                    request_response=False,
                    idempotency_key=f"answer:{exchange['id']}",
                    automatic=False,
                )
            )
            exchange, outbound, next_waiter = (
                await agent_server.deliver_cross_chat_live_response_locked(
                    exchange,
                    outbound,
                )
            )
        self.assertIsNone(next_waiter)
        self.assertTrue(waiter["future"].done())
        return exchange, outbound

    async def assert_retired_direct_authority(self, *, response=False, stale=False, repeat=1):
        """New runs get no local one-use authority; stale grants cannot revive it."""
        reference = agent_server.ChatReference(session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=7, action="instruction")
        exchange, leg = await self.create_exchange("retired_authority")
        path = await agent_server.issue_cross_chat_capability(
            "source", "run_authority", [reference],
            actions={"publish", "cross_chat_instruction", "cross_chat_response"},
            exchange_response_grants={(exchange["id"], leg["id"])})
        token = json.loads(path.read_text())["provider_capability"]
        capability = agent_server.CROSS_CHAT_CAPABILITIES[agent_server.hashlib.sha256(token.encode()).hexdigest()]
        self.assertFalse(capability["grants"])
        self.assertFalse(capability["provider_direct_grants"])
        self.assertFalse(capability["exchange_response_grants"])
        self.assertFalse({"cross_chat_instruction", "cross_chat_request_reply", "cross_chat_response"} & capability["actions"])
        self.assertNotIn(token, repr(agent_server.CROSS_CHAT_CAPABILITIES))
        agent_server.CURRENT_TURNS["source"] = {"run_id": "run_authority"}
        handle = "grant_" + "1" * 64
        if stale:
            # Simulate an authority already issued by the pre-retirement worker.
            capability["actions"].update({"cross_chat_instruction", "cross_chat_response"})
            capability["grants"].add(("target", "instruction"))
            capability["provider_direct_grants"][handle] = {"target_session_id": "target", "action": "instruction"}
            capability["exchange_response_grants"].add((exchange["id"], leg["id"]))
        create = AsyncMock(side_effect=AssertionError("retired route reached SQLite creation"))
        reply = AsyncMock(side_effect=AssertionError("retired route created a reply"))
        with patch.object(agent_server.CROSS_CHAT, "create_instruction", create), patch.object(agent_server.CROSS_CHAT, "commit_exchange_response", reply):
            for i in range(repeat):
                with self.assertRaises(HTTPException) as raised:
                    if response:
                        await agent_server.create_authorized_cross_chat_exchange_response(token, exchange["id"],
                            agent_server.CrossChatExchangeResponseRequest(inbound_leg_id=leg["id"], body="No automatic contact",
                                idempotency_key=f"retry-key-{i}", request_response=False))
                    else:
                        await agent_server.create_authorized_cross_chat_instruction(token,
                            agent_server.CrossChatHandoffRequest(target_session_id=handle, body="No direct turn",
                                idempotency_key=f"retry-key-{i}"))
                self.assertEqual(raised.exception.status_code, 410 if stale else 403)
        create.assert_not_awaited()
        reply.assert_not_awaited()
        self.assertFalse(capability.get("consumed"))
        self.assertEqual(len(await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])), 1)
        await agent_server.revoke_cross_chat_capability("run_authority")
        self.assertFalse(path.exists())

    async def assert_retired_direct_delivery(self, *, state="ready", backend="claude", reconcile=False, current_owner=False):
        agent_server.STORE.sessions["target"]["backend"] = backend
        record, _ = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="old_direct", source_session_id="source", source_run_id="run_old_source",
            target_session_id="target", body="Preserve this historical body", idempotency_key="old_direct")
        queued_id = "queued_old" if state == "queued" else None
        record = await agent_server.CROSS_CHAT.update(record["id"], expected={"ready"}, status=state,
            queued_id=queued_id, target_run_id="run_old_target" if state == "running" else None)
        user = {"queued_id": "queued_user", "prompt": "Preserve ordinary work", "_paused_after_stop": True}
        agent_server.QUEUED_TURNS["target"] = deque([user])
        if queued_id:
            agent_server.QUEUED_TURNS["target"].append({"queued_id": queued_id, "cross_chat_envelope_id": record["id"]})
        if current_owner:
            agent_server.CURRENT_TURNS["target"] = {"run_id": "run_old_target", "cross_chat_envelope_id": record["id"]}
        provider = AsyncMock(side_effect=AssertionError("retired delivery launched a provider"))
        with (patch.object(agent_server, "start_turn_durably", provider),
              patch.object(agent_server, "append_durable_event", AsyncMock()),
              patch.object(agent_server, "append_cross_chat_terminal_lifecycle", AsyncMock())):
            for _ in range(2):
                if reconcile:
                    await agent_server.reconcile_cross_chat_handoffs()
                else:
                    await agent_server.submit_cross_chat_delivery(record)
        retired = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(retired["status"], state if current_owner else "cancelled")
        self.assertEqual(retired["body"], record["body"])
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [user])
        if not current_owner:
            self.assertIn("legacy_route_disabled", retired["error"])
        provider.assert_not_awaited()

    async def assert_retired_exchange_delivery(self, *, state="registered", reconcile=False, live=False, current_owner=False, turnover=False):
        if live:
            exchange, leg, waiter = await self.create_live_waiter("old_exchange", "run_old_source")
        else:
            exchange, leg = await self.create_exchange("old_exchange")
            waiter = None
        queued_id = "queued_old" if state == "queued" else None
        leg = await agent_server.CROSS_CHAT.update_exchange_leg(leg["id"], expected={"registered"}, status=state,
            queued_id=queued_id, target_run_id="run_old_target" if state == "running" else None)
        user = {"queued_id": "queued_user", "prompt": "Preserve ordinary work", "_paused_after_stop": True}
        agent_server.QUEUED_TURNS["target"] = deque([user])
        if queued_id:
            agent_server.QUEUED_TURNS["target"].append({"queued_id": queued_id,
                "cross_chat_exchange_id": exchange["id"], "cross_chat_exchange_leg_id": leg["id"]})
        if current_owner:
            agent_server.CURRENT_TURNS["target"] = {"run_id": "run_old_target", "cross_chat_exchange_leg_id": leg["id"]}
        if turnover:
            agent_server.CURRENT_TURNS["source"] = {"run_id": "run_new_source"}
        provider = AsyncMock(side_effect=AssertionError("retired exchange launched a provider"))
        with (patch.object(agent_server, "start_turn_durably", provider),
              patch.object(agent_server, "append_durable_event", AsyncMock()),
              patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
              patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock())):
            for _ in range(2):
                if reconcile:
                    await agent_server.reconcile_cross_chat_exchanges()
                else:
                    await agent_server.submit_cross_chat_exchange_leg(exchange, leg)
        retired = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertEqual(retired["status"], state if current_owner else "cancelled")
        self.assertEqual(retired["body"], leg["body"])
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [user])
        if not current_owner:
            self.assertEqual(retired["error_code"], "legacy_route_disabled")
        provider.assert_not_awaited()
        if waiter is not None:
            self.assertTrue(waiter["future"].done())
            with self.assertRaises(HTTPException) as raised:
                await agent_server.await_cross_chat_live_waiter(exchange, waiter, timeout_seconds=1)
            self.assertEqual(raised.exception.status_code, 410)

    async def assert_retired_admission(self, *, exchange=False, backend="claude", jobs="full"):
        agent_server.STORE.sessions["target"].update(backend=backend, provider_jobs_access=jobs)
        kwargs = dict(prompt="Preserved historic request", purpose="cross_chat_handoff_delivery",
            source_session_id="source", target_session_id="target")
        if exchange:
            parent, leg = await self.create_exchange("old_admission")
            kwargs.update(cross_chat_exchange_id=parent["id"], cross_chat_exchange_leg_id=leg["id"])
        else:
            record, _ = await agent_server.CROSS_CHAT.create_instruction(envelope_id="old_admission",
                source_session_id="source", source_run_id="run_source", target_session_id="target",
                body="Preserved historic request", idempotency_key="old_admission")
            kwargs["cross_chat_envelope_id"] = record["id"]
        issue = AsyncMock(side_effect=AssertionError("retired request minted provider authority"))
        with (patch.object(agent_server, "managed_server_update_admission_blocker", return_value=None),
              patch.object(agent_server, "turn_start_blocker", AsyncMock(return_value=None)),
              patch.object(agent_server, "ensure_runtime_available", AsyncMock()),
              patch.object(agent_server, "issue_cross_chat_capability", issue),
              patch.object(agent_server, "append_cross_chat_terminal_lifecycle", AsyncMock()),
              patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
              patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock())):
            with self.assertRaises(HTTPException) as raised:
                await agent_server._start_turn_locked("target", agent_server.TurnRequest(**kwargs), queue_if_busy=False)
        self.assertEqual(raised.exception.status_code, 410)
        issue.assert_not_awaited()
        self.assertNotIn("target", agent_server.BUSY_SESSIONS)

    async def assert_no_automatic_exchange_reply(self, *, live=False, size=20, turnover=False, archived=False):
        if live:
            exchange, leg, waiter = await self.create_live_waiter("old_final", "run_source")
        else:
            exchange, leg = await self.create_exchange(f"old_final_{size}")
            waiter = None
        await agent_server.CROSS_CHAT.update_exchange_leg(leg["id"], expected={"registered"},
            status="running", target_run_id="run_target")
        if turnover:
            agent_server.CURRENT_TURNS["source"] = {"run_id": "run_successor"}
        if archived:
            agent_server.STORE.sessions["source"]["archived"] = True
        reply = AsyncMock(side_effect=AssertionError("automatic reply created"))
        provider = AsyncMock(side_effect=AssertionError("automatic reply launched provider"))
        with (patch.object(agent_server.CROSS_CHAT, "commit_exchange_response", reply),
              patch.object(agent_server, "start_turn_durably", provider),
              patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
              patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock())):
            for _ in range(2):
                await agent_server.finalize_cross_chat_exchange_run({"type": "turn_finished", "run_id": "run_target",
                    "exchange_id": exchange["id"], "exchange_leg_id": leg["id"], "result_text": "x"*size, "exit_code": 0})
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["body"], leg["body"])
        self.assertEqual(legs[0]["status"], "delivered")
        self.assertEqual((await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["status"], "cancelled")
        reply.assert_not_awaited()
        provider.assert_not_awaited()
        if waiter is not None:
            self.assertTrue(waiter["future"].done())
            with self.assertRaises(HTTPException) as raised:
                await agent_server.await_cross_chat_live_waiter(exchange, waiter, timeout_seconds=1)
            self.assertEqual(raised.exception.status_code, 410)

    async def test_retirement_preserves_already_running_direct_owner(self) -> None:
        await self.assert_retired_direct_delivery(state="running", current_owner=True)
        self.assertEqual(agent_server.CURRENT_TURNS["target"]["run_id"], "run_old_target")

    async def test_retirement_preserves_already_running_exchange_owner(self) -> None:
        await self.assert_retired_exchange_delivery(state="running", current_owner=True)
        self.assertEqual(agent_server.CURRENT_TURNS["target"]["run_id"], "run_old_target")

    async def test_retired_exchange_tombstone_failure_preserves_queue_then_exact_retry(self) -> None:
        exchange, leg = await self.create_exchange("retire_write_failure")
        leg = await agent_server.CROSS_CHAT.update_exchange_leg(leg["id"], expected={"registered"},
            status="queued", queued_id="queued_old")
        user = {"queued_id": "queued_user", "prompt": "Preserve my message"}
        old = {"queued_id": "queued_old", "cross_chat_exchange_leg_id": leg["id"]}
        agent_server.QUEUED_TURNS["target"] = deque([user, old])
        provider = AsyncMock(side_effect=AssertionError("retired queue launched provider"))
        append = AsyncMock(side_effect=OSError("fixture disk failure"))
        with (patch.object(agent_server, "start_turn_durably", provider),
              patch.object(agent_server, "append_durable_event", append),
              patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
              patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock())):
            with self.assertRaises(OSError):
                await agent_server.submit_cross_chat_exchange_leg(exchange, leg)
            self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [user, old])
            self.assertEqual((await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"]))["status"], "cancelled")
            append.side_effect = None
            await agent_server.submit_cross_chat_exchange_leg(exchange, leg)
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [user])
        provider.assert_not_awaited()

    async def test_instruction_idempotency_rejects_payload_change(self) -> None:
        first, created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_one",
            source_session_id="source",
            source_run_id="run_one",
            target_session_id="target",
            body="Do the check",
            idempotency_key="stable-key",
        )
        self.assertTrue(created)
        replay, replay_created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_two",
            source_session_id="source",
            source_run_id="run_one",
            target_session_id="target",
            body="Do the check",
            idempotency_key="stable-key",
        )
        self.assertFalse(replay_created)
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(HTTPException) as raised:
            await agent_server.CROSS_CHAT.create_instruction(
                envelope_id="handoff_three",
                source_session_id="source",
                source_run_id="run_one",
                target_session_id="target",
                body="Changed payload",
                idempotency_key="stable-key",
            )
        self.assertEqual(raised.exception.status_code, 409)

    async def test_new_local_reference_does_not_issue_direct_authority(self) -> None:
        await self.assert_retired_direct_authority(repeat=2)

    async def test_new_direct_route_cannot_return_a_success_receipt(self) -> None:
        await self.assert_retired_direct_authority()

    async def test_new_exchange_response_cannot_return_a_success_receipt(self) -> None:
        await self.assert_retired_direct_authority(response=True)

    async def test_stale_direct_grant_rejects_before_reserving_idempotency_key(self) -> None:
        await self.assert_retired_direct_authority(stale=True, repeat=2)

    async def test_local_final_reference_never_registers_an_obligation(self) -> None:
        reference = agent_server.ChatReference(session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=7, action="final_result")
        for _ in range(2):
            self.assertEqual(await agent_server.register_final_result_obligations("source", "run_final", [reference]), [])
        self.assertEqual(await agent_server.CROSS_CHAT.for_source_run("run_final"), [])

    async def test_retired_final_reference_never_enters_sqlite_or_a_cancellation_wait(self) -> None:
        reference = agent_server.ChatReference(session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=7, action="final_result")
        with patch.object(agent_server.CROSS_CHAT, "create_final_obligation", AsyncMock(side_effect=AssertionError("retired registration reached SQLite"))) as create:
            self.assertEqual(await asyncio.wait_for(agent_server.register_final_result_obligations("source", "run_final", [reference]), 1), [])
        create.assert_not_awaited()

    async def test_stale_direct_route_never_reaches_submission(self) -> None:
        await self.assert_retired_direct_authority(stale=True)

    async def test_retried_stale_direct_route_never_creates_a_record(self) -> None:
        await self.assert_retired_direct_authority(stale=True, repeat=3)

    async def test_request_cancel_waits_for_exchange_response_acceptance(self) -> None:
        request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", b"test-capability")
            ],
            "client": ("127.0.0.1", 1234),
        })
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_accept(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return ({"_secure_peer": True}, {}, True)

        with patch.object(
            agent_server,
            "create_authorized_cross_chat_exchange_response",
            side_effect=delayed_accept,
        ):
            task = asyncio.create_task(
                agent_server.submit_authorized_cross_chat_exchange_response(
                    "exchange-cancel",
                    agent_server.CrossChatExchangeResponseRequest(
                        inbound_leg_id="leg-inbound",
                        body="settle this response",
                        idempotency_key="response-cancel-key",
                    ),
                    request,
                )
            )
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_chat_reference_validation_rejects_self_archive_and_overlap(self) -> None:
        self_ref = agent_server.ChatReference(
            session_id="source", display_title_snapshot="Source",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        with self.assertRaises(HTTPException):
            agent_server.validate_chat_references("source", "@", [self_ref])
        agent_server.STORE.sessions["target"]["archived"] = True
        archived = agent_server.ChatReference(
            session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        with self.assertRaises(HTTPException):
            agent_server.validate_chat_references("source", "@", [archived])

    async def test_public_turn_cannot_spoof_scheduled_job_reference_authority(
        self,
    ) -> None:
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=0,
            source_text_end=7,
            action="instruction",
        )
        with patch.object(
            agent_server,
            "initial_provider_cross_chat_route_snapshot",
            return_value=[],
        ):
            with self.assertRaisesRegex(HTTPException, "internal turns") as raised:
                await agent_server._start_turn_locked(
                    "source",
                    agent_server.TurnRequest(
                        prompt="@Target do work",
                        purpose="scheduled_job",
                        chat_references=[reference],
                    ),
                    queue_if_busy=False,
                )
        self.assertEqual(raised.exception.status_code, 400)

    def test_queue_event_round_trip_keeps_structured_references(self) -> None:
        event = {
            "type": "turn_queued",
            "queued_id": "queued_one",
            "prompt": "ask @Target",
            "request_prompt": "ask @Target",
            "chat_references": [{
                "session_id": "target",
                "display_title_snapshot": "Target",
                "source_text_start": 4,
                "source_text_end": 11,
                "action": "instruction",
            }],
            "cross_chat_obligation_ids": ["handoff_one"],
            "ts": "2026-08-10T00:00:00Z",
        }
        item = agent_server.queued_turn_from_event(event, {"backend": "codex"}, 1)
        self.assertEqual(item["chat_references"][0]["session_id"], "target")
        self.assertEqual(item["cross_chat_obligation_ids"], ["handoff_one"])

    async def test_provider_capability_is_scoped_and_legacy_admin_tokens_are_removed(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_scope", [reference]
        )
        payload = json.loads(authority_path.read_text())
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_scope"}}
        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/agent/sessions/source/jobs",
            "headers": [(b"x-agentsdock-provider-capability", payload["provider_capability"].encode())],
            "query_string": b"",
            "scheme": "http",
            "server": ("127.0.0.1", 7850),
            "client": ("127.0.0.1", 43210),
        })
        allowed = await agent_server.authorize_provider_action(
            request, action="jobs", session_id="source"
        )
        self.assertEqual(allowed["source_run_id"], "run_scope")
        with self.assertRaises(HTTPException):
            await agent_server.authorize_provider_action(
                request, action="jobs", session_id="target"
            )

        with patch.dict(os.environ, {
            "AGENTSDOCK_AGENT_TOKEN": "admin-a",
            "ZENITHDOCK_AGENT_TOKEN": "admin-b",
            "ZENITHBOT_AGENT_TOKEN": "admin-c",
        }, clear=False):
            for environment in (
                agent_server.runner_env(),
                agent_server.agent_runner_env("source"),
                agent_server.codex_app_server_env(),
            ):
                self.assertNotIn("AGENTSDOCK_AGENT_TOKEN", environment)
                self.assertNotIn("ZENITHDOCK_AGENT_TOKEN", environment)
                self.assertNotIn("ZENITHBOT_AGENT_TOKEN", environment)

    def test_chat_reference_span_is_exact_utf16_and_surrogate_safe(self) -> None:
        prompt = "😀 ask @Target now"
        start = agent_server.utf16_length("😀 ask ")
        end = start + agent_server.utf16_length("@Target")
        reference = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=start,
            source_text_end=end,
            action="instruction",
        )
        with patch.object(
            agent_server,
            "CLAUDE_TRANSPORT",
            agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
        ):
            self.assertEqual(
                agent_server.validate_chat_references("source", prompt, [reference]),
                [reference],
            )
            mismatched = reference.model_copy(
                update={"display_title_snapshot": "Wrong"}
            )
            with self.assertRaises(HTTPException):
                agent_server.validate_chat_references("source", prompt, [mismatched])
            split_surrogate = reference.model_copy(
                update={"source_text_start": 1, "source_text_end": end}
            )
            with self.assertRaises(HTTPException) as raised:
                agent_server.validate_chat_references(
                    "source", prompt, [split_surrogate]
                )
            self.assertIn("Unicode character", str(raised.exception.detail))

            for attached_prompt in (
                "😀 ask @Target2 now",
                "😀 askx@Target now",
            ):
                with self.assertRaises(HTTPException) as attached:
                    agent_server.validate_chat_references(
                        "source", attached_prompt, [reference]
                    )
                self.assertIn("not delimited", str(attached.exception.detail))

            ambiguous_title = reference.model_copy(
                update={
                    "display_title_snapshot": "@Target",
                    "source_text_end": end + 1,
                }
            )
            with self.assertRaises(HTTPException) as ambiguous:
                agent_server.validate_chat_references(
                    "source", "😀 ask @@Target now", [ambiguous_title]
                )
            self.assertIn("cannot begin with @", str(ambiguous.exception.detail))

        with self.assertRaisesRegex(ValueError, "cannot begin with @"):
            agent_server.ChatReference(
                session_id="target",
                display_title_snapshot="@Target",
                source_text_start=start,
                source_text_end=end + 1,
                action="direct_message",
            )

    def test_request_reply_requires_additive_v2_client_capability(self) -> None:
        prompt = "ask @Target"
        instruction = agent_server.ChatReference(
            session_id="target",
            display_title_snapshot="Target",
            source_text_start=4,
            source_text_end=11,
            action="instruction",
        )
        request_reply = instruction.model_copy(update={"action": "request_reply"})
        with (
            patch.object(
                agent_server,
                "CODEX_TRANSPORT",
                agent_server.CODEX_TRANSPORT_APP_SERVER,
            ),
            patch.object(
                agent_server,
                "CLAUDE_TRANSPORT",
                agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
            ),
        ):
            self.assertEqual(
                agent_server.validate_chat_references(
                    "source",
                    prompt,
                    [instruction],
                    [agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY],
                ),
                [instruction],
            )
            with self.assertRaises(HTTPException) as raised:
                agent_server.validate_chat_references(
                    "source",
                    prompt,
                    [request_reply],
                    [agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY],
                )
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("cross_chat_handoffs_v2", str(raised.exception.detail))
            self.assertEqual(
                agent_server.validate_chat_references(
                    "source",
                    prompt,
                    [request_reply],
                    [
                        agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY,
                        agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
                    ],
                ),
                [request_reply],
            )

    async def test_queue_edit_requires_and_durably_preserves_v2_capability(self) -> None:
        prompt = "ask @Target"
        instruction = {
            "session_id": "target",
            "display_title_snapshot": "Target",
            "source_text_start": 4,
            "source_text_end": 11,
            "action": "instruction",
        }
        request_reply = agent_server.ChatReference(
            **{**instruction, "action": "request_reply"}
        )
        original_event = {
            "type": "turn_queued",
            "queued_id": "queued_v2_upgrade",
            "prompt": prompt,
            "request_prompt": prompt,
            "chat_references": [instruction],
            "client_capabilities": [
                agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY
            ],
            "ts": "2026-08-10T00:00:00Z",
        }
        item = agent_server.queued_turn_from_event(
            original_event,
            agent_server.STORE.sessions["source"],
            1,
        )
        agent_server.QUEUED_TURNS = {"source": deque([item])}
        with (
            patch.object(agent_server, "managed_server_update_blocker", return_value=None),
            patch.object(
                agent_server,
                "CODEX_TRANSPORT",
                agent_server.CODEX_TRANSPORT_APP_SERVER,
            ),
            patch.object(
                agent_server,
                "CLAUDE_TRANSPORT",
                agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
            ),
            patch.object(agent_server, "append_durable_event", AsyncMock()) as append,
        ):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.update_queued_turn(
                    "source",
                    "queued_v2_upgrade",
                    agent_server.UpdateQueuedTurnRequest(
                        chat_references=[request_reply],
                    ),
                )
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(item["chat_references"][0]["action"], "instruction")

            response = await agent_server.update_queued_turn(
                "source",
                "queued_v2_upgrade",
                agent_server.UpdateQueuedTurnRequest(
                    client_capabilities=[
                        agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY,
                        agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
                    ],
                    chat_references=[request_reply],
                ),
            )
            updated = response["item"]
            with self.assertRaises(HTTPException) as downgrade:
                await agent_server.update_queued_turn(
                    "source",
                    "queued_v2_upgrade",
                    agent_server.UpdateQueuedTurnRequest(
                        client_capabilities=[
                            agent_server.CROSS_CHAT_HANDOFFS_V1_CLIENT_CAPABILITY,
                        ],
                    ),
                )
            self.assertEqual(downgrade.exception.status_code, 400)
        self.assertEqual(updated["chat_references"][0]["action"], "request_reply")
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            updated["client_capabilities"],
        )
        update_payload = append.await_args.args[2]
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            update_payload["client_capabilities"],
        )

        event_file = self.root / "queued-v2-recovery.jsonl"
        event_file.write_text(
            json.dumps(original_event) + "\n"
            + json.dumps({"type": "turn_queue_updated", **update_payload}) + "\n"
        )
        with patch.object(agent_server, "events_path", return_value=event_file):
            recovered = agent_server.scan_queued_turns_from_events([
                ("source", agent_server.STORE.sessions["source"]),
            ])["source"][0]
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            recovered["client_capabilities"],
        )

        agent_server.QUEUED_TURNS = {"source": deque([recovered])}
        start = AsyncMock(return_value={"ok": True})
        with patch.object(agent_server, "_start_turn_locked", start):
            await agent_server.start_next_queued_turn("source")
        promoted_request = start.await_args.args[1]
        self.assertIn(
            agent_server.CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY,
            promoted_request.client_capabilities,
        )

    def test_capability_ttl_supports_overnight_live_turns(self) -> None:
        self.assertGreaterEqual(agent_server.CROSS_CHAT_CAPABILITY_TTL_SECONDS, 7 * 24 * 60 * 60)

    def test_queue_update_recovery_replaces_cross_chat_grants(self) -> None:
        path = self.root / "events.jsonl"
        events = [
            {
                "type": "turn_queued", "queued_id": "queued_one",
                "prompt": "old", "chat_references": [{"session_id": "old"}],
                "cross_chat_obligation_ids": ["handoff_old"],
            },
            {
                "type": "turn_queue_updated", "queued_id": "queued_one",
                "prompt": "new", "chat_references": [{"session_id": "target"}],
                "cross_chat_obligation_ids": ["handoff_new"],
            },
        ]
        path.write_text("".join(json.dumps(event) + "\n" for event in events))
        with patch.object(agent_server, "events_path", return_value=path):
            recovered = agent_server.scan_queued_turns_from_events([
                ("source", {"backend": "codex"}),
            ])
        self.assertEqual(recovered["source"][0]["chat_references"], [{"session_id": "target"}])
        self.assertEqual(recovered["source"][0]["cross_chat_obligation_ids"], ["handoff_new"])

    async def test_restart_binds_submitting_ledger_before_queue_schedule(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_restart_bind",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="recover me",
            idempotency_key="restart-bind-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="submitting"
        )
        recovered_item = {
            "queued_id": "queued_restart_bind",
            "prompt": "relay",
            "purpose": "cross_chat_handoff_delivery",
            "source_session_id": "source",
            "target_session_id": "target",
            "cross_chat_envelope_id": record["id"],
            "position": 1,
            "_durable": True,
            "_paused_after_stop": False,
        }
        with patch.object(
            agent_server,
            "scan_queued_turns_from_events",
            return_value={"target": [recovered_item]},
        ), patch.object(
            agent_server,
            "schedule_next_queued_turn",
        ) as schedule:
            agent_server.initialize_queue_recovery()
            rebuilt, scheduled = await agent_server.recover_queued_turns_after_start()
        self.assertEqual((rebuilt, scheduled), (1, 1))
        bound = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(bound["status"], "queued")
        self.assertEqual(bound["queued_id"], "queued_restart_bind")
        schedule.assert_called_once_with("target")

    async def test_stale_nonterminal_lifecycle_cannot_follow_terminal_ledger(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_stale_queued",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="race",
            idempotency_key="stale-queued-key",
        )
        queued = await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"},
            status="queued", queued_id="queued_stale",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"queued"}, status="cancelled"
        )
        with patch.object(
            agent_server,
            "append_durable_event",
            new_callable=AsyncMock,
        ) as append:
            await agent_server.append_cross_chat_lifecycle(
                queued,
                "cross_chat_handoff_queued",
                "queued",
                "Queued",
            )
        append.assert_not_awaited()

    def test_cross_chat_lifecycle_is_visible_and_bumps_activity(self) -> None:
        self.assertTrue(agent_server.is_agent_visible_event("cross_chat_handoff_received", {}))
        self.assertTrue(agent_server.should_bump_session_updated_at("cross_chat_handoff_started", {}))

    async def test_live_capability_survives_stale_ttl_and_terminal_revoke_wins(self) -> None:
        authority_path = await agent_server.issue_cross_chat_capability(
            "source", "run_overnight", []
        )
        payload = json.loads(authority_path.read_text())
        token = payload["provider_capability"]
        token_hash = agent_server.hashlib.sha256(token.encode()).hexdigest()
        agent_server.CROSS_CHAT_CAPABILITIES[token_hash]["expires_at"] = time.time() - 3600
        agent_server.CURRENT_TURNS = {"source": {"run_id": "run_overnight"}}
        request = Request({
            "type": "http", "method": "GET",
            "path": "/api/agent/sessions/source/jobs",
            "headers": [(b"x-agentsdock-provider-capability", token.encode())],
            "query_string": b"", "scheme": "http",
            "server": ("127.0.0.1", 7850), "client": ("127.0.0.1", 43210),
        })
        authorized = await agent_server.authorize_provider_action(
            request, action="jobs", session_id="source"
        )
        self.assertEqual(authorized["source_run_id"], "run_overnight")
        await agent_server.revoke_cross_chat_capability("run_overnight")
        with self.assertRaises(HTTPException):
            await agent_server.authorize_provider_action(
                request, action="jobs", session_id="source"
            )

    async def test_concurrent_idempotent_instruction_create_is_serialized_off_loop(self) -> None:
        async def create(envelope_id: str):
            return await agent_server.CROSS_CHAT.create_instruction(
                envelope_id=envelope_id,
                source_session_id="source",
                source_run_id="run_concurrent",
                target_session_id="target",
                body="same body",
                idempotency_key="same-key",
            )

        with patch.object(
            agent_server.asyncio,
            "to_thread",
            wraps=agent_server.asyncio.to_thread,
        ) as to_thread:
            first, second = await agent_server.asyncio.gather(
                create("handoff_concurrent_a"),
                create("handoff_concurrent_b"),
            )
        self.assertGreaterEqual(to_thread.await_count, 2)
        self.assertEqual(first[0]["id"], second[0]["id"])
        self.assertEqual(sorted((first[1], second[1])), [False, True])

    async def test_late_success_cannot_override_failed_delivery(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_late",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="work",
            idempotency_key="late-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="failed", error="target deleted"
        )
        append = AsyncMock()
        with (
            patch.object(agent_server, "append_durable_event", append),
            patch.object(agent_server, "cross_chat_event_exists", return_value=False),
        ):
            await agent_server.finish_cross_chat_delivery({
                "cross_chat_envelope_id": record["id"],
                "run_id": "run_target",
                "result_text": "late success",
                "exit_code": 0,
            })
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "failed")
        self.assertNotIn(
            "cross_chat_handoff_delivered",
            [call.args[1] for call in append.await_args_list],
        )

    async def test_normal_queue_item_cannot_move_across_delivery(self) -> None:
        agent_server.QUEUED_TURNS["target"] = deque([
            {"queued_id": "delivery", "purpose": "cross_chat_handoff_delivery"},
            {"queued_id": "normal", "purpose": None},
        ])
        with patch.object(agent_server, "managed_server_update_blocker", return_value=None):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.move_queued_turn(
                    "target",
                    "normal",
                    agent_server.MoveQueuedTurnRequest(direction="up"),
                )
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["target"]],
            ["delivery", "normal"],
        )

    async def test_force_send_overtakes_pending_delivery_without_removing_it(self) -> None:
        agent_server.QUEUED_TURNS["target"] = deque([
            {"queued_id": "delivery", "purpose": "cross_chat_handoff_delivery"},
            {"queued_id": "normal", "purpose": None, "prompt": "Send this now"},
        ])
        # The mocked waiter cannot clear its steering fence after admission.
        with patch.object(agent_server, "STEERING_SESSIONS", set()), \
                patch.object(agent_server, "managed_server_update_admission_blocker", return_value=None), \
                patch.object(agent_server, "stop_turn", new_callable=AsyncMock, return_value={"stopped": False}), \
                patch.object(agent_server, "append_durable_event", new_callable=AsyncMock), \
                patch.object(agent_server, "schedule_steered_turn_slot_waiter"):
            result = await agent_server._run_queued_turn_now_once("target", "normal")
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["target"]],
            ["delivery"],
        )
        self.assertEqual(agent_server.RUN_NOW_TURNS["target"]["queued_id"], "normal")

    async def test_explicit_stop_never_hides_and_pauses_internal_delivery(self) -> None:
        delivery = {
            "queued_id": "queued_delivery_after_stop",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": "handoff_after_stop",
            "_durable": True,
            "_paused_after_stop": False,
        }
        agent_server.QUEUED_TURNS["target"] = deque([delivery])
        with patch.object(
            agent_server,
            "append_durable_event",
            new_callable=AsyncMock,
        ) as append, patch.object(
            agent_server,
            "start_internal_delivery_after_explicit_stop",
            new_callable=AsyncMock,
        ) as wake:
            paused = await agent_server.pause_queued_turns_after_explicit_stop(
                "target"
            )
            await asyncio.sleep(0)
        self.assertEqual(paused, 0)
        self.assertFalse(delivery["_paused_after_stop"])
        append.assert_not_awaited()
        wake.assert_awaited_once_with("target")

    async def test_stopped_slot_wakes_hidden_delivery_after_busy_releases(self) -> None:
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_delivery_wakeup",
            "purpose": "cross_chat_handoff_delivery",
            "_paused_after_stop": False,
        }])
        agent_server.BUSY_SESSIONS.add("target")
        with patch.object(
            agent_server,
            "start_next_queued_turn",
            new_callable=AsyncMock,
        ) as start:
            wake = asyncio.create_task(
                agent_server.start_internal_delivery_after_explicit_stop("target")
            )
            await asyncio.sleep(0.06)
            start.assert_not_awaited()
            agent_server.BUSY_SESSIONS.discard("target")
            await asyncio.wait_for(wake, 0.5)
        start.assert_awaited_once_with("target")

    async def test_terminal_lifecycle_outbox_is_mirrored_once_concurrently(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_outbox",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="audit me",
            idempotency_key="outbox-key",
        )
        terminal = await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="delivered"
        )
        emitted: set[tuple[str, str, str]] = set()

        async def exists(
            session_id: str,
            envelope_id: str,
            event_type: str,
            *,
            full_scan: bool = False,
        ) -> bool:
            return (session_id, envelope_id, event_type) in emitted

        async def append(session_id: str, event_type: str, payload: dict):
            await asyncio.sleep(0)
            emitted.add((session_id, payload["handoff_id"], event_type))
            return {"type": event_type, **payload}

        with (
            patch.object(agent_server, "cross_chat_event_exists_async", side_effect=exists),
            patch.object(agent_server, "append_durable_event", side_effect=append),
        ):
            await asyncio.gather(
                agent_server.append_cross_chat_terminal_lifecycle(terminal, "done"),
                agent_server.append_cross_chat_terminal_lifecycle(terminal, "done"),
            )
            await agent_server.append_cross_chat_terminal_lifecycle(terminal, "done")
        self.assertEqual(len(emitted), 2)
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["lifecycle_status"], "delivered")

    async def test_live_lifecycle_uses_primed_cache_without_full_history_scan(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_live_cache",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="cache me",
            idempotency_key="live-cache-key",
        )
        agent_server.prime_cross_chat_event_cache(record)
        scans: list[bool] = []

        def scan(
            _session_id: str,
            _envelope_id: str,
            *,
            full_scan: bool = False,
        ) -> list[dict]:
            scans.append(full_scan)
            return []

        with (
            patch.object(agent_server, "cross_chat_events", side_effect=scan),
            patch.object(
                agent_server,
                "append_durable_event",
                new_callable=AsyncMock,
            ) as append,
        ):
            await agent_server.append_cross_chat_event_once(
                "source",
                record,
                "cross_chat_handoff_registered",
                "ready",
                "registered",
            )
            await agent_server.append_cross_chat_event_once(
                "source",
                record,
                "cross_chat_handoff_registered",
                "ready",
                "registered",
            )

        self.assertEqual(scans, [])
        append.assert_awaited_once()

    async def test_restart_flushes_terminal_lifecycle_outbox_exactly_once(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_restart_outbox",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="restart",
            idempotency_key="restart-outbox-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="failed", error="crash gap"
        )
        emitted: set[tuple[str, str, str]] = set()
        scan_modes: list[bool] = []

        async def exists(
            session_id: str,
            envelope_id: str,
            event_type: str,
            *,
            full_scan: bool = False,
        ) -> bool:
            scan_modes.append(full_scan)
            return (session_id, envelope_id, event_type) in emitted

        async def append(session_id: str, event_type: str, payload: dict):
            emitted.add((session_id, payload["handoff_id"], event_type))
            return {"type": event_type, **payload}

        with (
            patch.object(agent_server, "cross_chat_event_exists_async", side_effect=exists),
            patch.object(agent_server, "append_durable_event", side_effect=append),
        ):
            await agent_server.reconcile_cross_chat_handoffs()
            await agent_server.reconcile_cross_chat_handoffs()
        self.assertEqual(len(emitted), 2)
        self.assertTrue(scan_modes)
        self.assertTrue(all(scan_modes))
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["lifecycle_status"], "failed")

    async def test_busy_delivery_is_ledger_bound_before_queue_is_schedulable(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_queue_bind",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="queued",
            idempotency_key="queue-bind-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="submitting"
        )
        agent_server.BUSY_SESSIONS.add("target")
        request = agent_server.TurnRequest(
            prompt="relay",
            display_prompt="relay",
            purpose="cross_chat_handoff_delivery",
            source_session_id="source",
            target_session_id="target",
            cross_chat_envelope_id=record["id"],
        )
        with patch.object(
            agent_server,
            "append_durable_event",
            new_callable=AsyncMock,
            return_value={"type": "turn_queued"},
        ):
            result = await agent_server.enqueue_turn(
                "target", request, agent_server.STORE.sessions["target"]
            )
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "queued")
        self.assertEqual(refreshed["queued_id"], result["queued_id"])
        self.assertEqual(refreshed["queue_position"], 1)

    async def test_retired_delivery_cannot_acquire_destination_jobs_authority(self) -> None:
        for jobs in ("full", "read_only", "blocked"):
            with self.subTest(jobs=jobs):
                await self.assert_retired_admission(jobs=jobs)

    async def test_retired_exchange_cannot_acquire_provider_authority(self) -> None:
        await self.assert_retired_admission(exchange=True)

    async def test_queued_cancel_removes_target_and_mirrors_terminal_lifecycle(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_cancel",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="cancel",
            idempotency_key="cancel-target-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"}, status="queued", queued_id="queued_cancel"
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_cancel",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": record["id"],
        }])
        terminal = AsyncMock()
        with (
            patch.object(agent_server, "append_durable_event", new_callable=AsyncMock),
            patch.object(agent_server, "append_cross_chat_terminal_lifecycle", terminal),
        ):
            cancelled = await agent_server.cancel_queued_cross_chat_handoff(record["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertNotIn("target", agent_server.QUEUED_TURNS)
        terminal.assert_awaited_once()

    async def test_cancel_after_durable_unqueue_finishes_ledger_and_lifecycle(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_cancel_join",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="cancel safely",
            idempotency_key="cancel-join-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"],
            expected={"ready"},
            status="queued",
            queued_id="queued_cancel_join",
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_cancel_join",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": record["id"],
        }])
        entered = asyncio.Event()
        release = asyncio.Event()
        original_update = agent_server.CROSS_CHAT.update
        emitted: list[tuple[str, str]] = []

        async def delayed_update(envelope_id: str, **kwargs):
            if kwargs.get("status") == "cancelled":
                entered.set()
                await release.wait()
            return await original_update(envelope_id, **kwargs)

        async def exists(
            session_id: str,
            _envelope_id: str,
            event_type: str,
            *,
            full_scan: bool = False,
        ) -> bool:
            return (session_id, event_type) in emitted

        async def append(session_id: str, event_type: str, payload: dict):
            if payload.get("handoff_id"):
                emitted.append((session_id, event_type))
            return {"type": event_type, **payload}

        with (
            patch.object(agent_server.CROSS_CHAT, "update", side_effect=delayed_update),
            patch.object(agent_server, "cross_chat_event_exists_async", side_effect=exists),
            patch.object(agent_server, "append_durable_event", side_effect=append),
        ):
            task = asyncio.create_task(
                agent_server.cancel_queued_cross_chat_handoff(record["id"])
            )
            await entered.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "cancelled")
        self.assertNotIn("target", agent_server.QUEUED_TURNS)
        self.assertCountEqual(
            emitted,
            [
                ("source", "cross_chat_handoff_cancelled"),
                ("target", "cross_chat_handoff_cancelled"),
            ],
        )

    async def test_exact_delivery_skip_preserves_fifo_during_update_drain(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_exact_skip",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="skip me",
            idempotency_key="exact-skip-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"],
            expected={"ready"},
            status="queued",
            queued_id="queued_exact_skip",
        )
        agent_server.QUEUED_TURNS["target"] = deque([
            {"queued_id": "queued_before"},
            {
                "queued_id": "queued_exact_skip",
                "purpose": agent_server.LOCAL_CROSS_CHAT_DELIVERY_PURPOSE,
                "cross_chat_envelope_id": record["id"],
                "source_session_id": "source",
                "target_session_id": "target",
            },
            {"queued_id": "queued_after"},
        ])
        append = AsyncMock(return_value={})
        lifecycle = AsyncMock()
        with (
            patch.object(
                agent_server,
                "managed_server_update_blocker",
                side_effect=AssertionError(
                    "skip must remain available while draining"
                ),
            ),
            patch.object(agent_server, "append_durable_event", append),
            patch.object(
                agent_server,
                "append_cross_chat_terminal_lifecycle",
                lifecycle,
            ),
        ):
            result = await agent_server.post_skip_queued_cross_chat_delivery(
                "target",
                "queued_exact_skip",
                agent_server.SkipQueuedCrossChatDeliveryRequest(
                    cross_chat_envelope_id=record["id"],
                ),
            )

        self.assertTrue(result["skipped"])
        self.assertEqual(result["remaining"], 2)
        self.assertEqual(
            [item["queued_id"] for item in agent_server.QUEUED_TURNS["target"]],
            ["queued_before", "queued_after"],
        )
        self.assertEqual(
            (await agent_server.CROSS_CHAT.get(record["id"]))["status"],
            "cancelled",
        )
        self.assertEqual(append.await_args.args[:2], ("target", "turn_unqueued"))
        lifecycle.assert_awaited_once()

    async def test_exact_delivery_skip_keeps_terminal_cas_after_tombstone_failure(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_skip_tombstone_failure",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="skip despite disk failure",
            idempotency_key="skip-tombstone-failure-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"],
            expected={"ready"},
            status="queued",
            queued_id="queued_skip_tombstone_failure",
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_skip_tombstone_failure",
            "purpose": agent_server.LOCAL_CROSS_CHAT_DELIVERY_PURPOSE,
            "cross_chat_envelope_id": record["id"],
        }])

        with patch.object(
            agent_server,
            "append_durable_event",
            AsyncMock(side_effect=OSError("disk unavailable")),
        ):
            with self.assertRaises(OSError):
                await agent_server.skip_queued_cross_chat_delivery(
                    "target",
                    "queued_skip_tombstone_failure",
                    agent_server.SkipQueuedCrossChatDeliveryRequest(
                        cross_chat_envelope_id=record["id"],
                    ),
                )

        self.assertNotIn("target", agent_server.QUEUED_TURNS)
        self.assertEqual(
            (await agent_server.CROSS_CHAT.get(record["id"]))["status"],
            "cancelled",
        )

    async def test_exact_delivery_skip_rejects_stale_identity_or_owner(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_exact_conflict",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="keep me",
            idempotency_key="exact-conflict-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"],
            expected={"ready"},
            status="queued",
            queued_id="queued_exact_conflict",
        )
        delivery = {
            "queued_id": "queued_exact_conflict",
            "purpose": agent_server.LOCAL_CROSS_CHAT_DELIVERY_PURPOSE,
            "cross_chat_envelope_id": record["id"],
        }
        request = agent_server.SkipQueuedCrossChatDeliveryRequest(
            cross_chat_envelope_id=record["id"],
        )
        agent_server.QUEUED_TURNS["target"] = deque([delivery])

        with self.assertRaises(HTTPException) as mismatch:
            await agent_server.skip_queued_cross_chat_delivery(
                "target",
                "queued_exact_conflict",
                agent_server.SkipQueuedCrossChatDeliveryRequest(
                    cross_chat_envelope_id="handoff_stale_card",
                ),
            )
        self.assertEqual(mismatch.exception.status_code, 409)

        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"queued"}, status="running"
        )
        with self.assertRaises(HTTPException) as ledger_race:
            await agent_server.skip_queued_cross_chat_delivery(
                "target", "queued_exact_conflict", request
            )
        self.assertEqual(ledger_race.exception.status_code, 409)
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [delivery])

        agent_server.QUEUED_TURNS.pop("target")
        with self.assertRaises(HTTPException) as promoted:
            await agent_server.skip_queued_cross_chat_delivery(
                "target", "queued_exact_conflict", request
            )
        self.assertEqual(promoted.exception.status_code, 409)

    async def test_exact_delivery_skip_wakes_live_exchange_waiter(self) -> None:
        source_token = await self.issue_live_waiter_owner(
            "source",
            "run_live_skip",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_live_skip",
            requester_session_id="source",
            authorization_source_run_id="run_live_skip",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, leg, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_live_skip",
                source_session_id="source",
                source_run_id="run_live_skip",
                target_session_id="target",
                body="Please answer",
                idempotency_key="live-skip-request",
                live_response_lease=True,
            )
        )
        async with agent_server.cross_chat_live_lease_lock(exchange["id"]):
            waiter = await agent_server.register_cross_chat_live_waiter_locked(
                exchange,
                leg,
                owner_session_id="source",
                owner_run_id="run_live_skip",
                capability_token=source_token,
            )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"],
            expected={"registered"},
            status="queued",
            queued_id="queued_live_skip",
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_live_skip",
            "purpose": agent_server.LOCAL_CROSS_CHAT_DELIVERY_PURPOSE,
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": leg["id"],
            "source_session_id": "source",
            "target_session_id": "target",
        }])

        with (
            patch.object(
                agent_server,
                "append_durable_event",
                AsyncMock(return_value={}),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_terminal_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_terminal_lifecycle",
                AsyncMock(),
            ),
        ):
            await agent_server.skip_queued_cross_chat_delivery(
                "target",
                "queued_live_skip",
                agent_server.SkipQueuedCrossChatDeliveryRequest(
                    cross_chat_exchange_id=exchange["id"],
                    cross_chat_exchange_leg_id=leg["id"],
                ),
            )

        waiter_result = await asyncio.wait_for(waiter["future"], timeout=1)
        self.assertFalse(waiter_result["ok"])
        self.assertEqual(waiter_result["error_code"], "cancelled_by_user")
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "cancelled")
        self.assertEqual(
            (await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"]))["status"],
            "cancelled",
        )

    async def test_exact_delivery_skip_status_leg_preserves_terminal_exchange(self) -> None:
        exchange, parent = await self.create_exchange("exchange_status_skip")
        await agent_server.CROSS_CHAT.finish_exchange_leg(
            parent["id"],
            status="failed",
            error_code="target_failed",
            error="original target failure",
        )
        status_leg, _created = await agent_server.CROSS_CHAT.create_exchange_status_leg(
            exchange_id=exchange["id"],
            source_session_id="target",
            target_session_id="source",
            body="Target failed",
            error_code="target_failed",
        )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            status_leg["id"],
            expected={"registered"},
            status="queued",
            queued_id="queued_status_skip",
        )
        agent_server.QUEUED_TURNS["source"] = deque([{
            "queued_id": "queued_status_skip",
            "purpose": agent_server.LOCAL_CROSS_CHAT_DELIVERY_PURPOSE,
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": status_leg["id"],
            "cross_chat_exchange_status": True,
            "source_session_id": "target",
            "target_session_id": "source",
        }])
        status_lifecycle = AsyncMock()
        with (
            patch.object(
                agent_server,
                "append_durable_event",
                AsyncMock(return_value={}),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_terminal_lifecycle",
                status_lifecycle,
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_terminal_lifecycle",
                AsyncMock(
                    side_effect=AssertionError(
                        "status skip must not rewrite exchange lifecycle"
                    )
                ),
            ),
        ):
            await agent_server.skip_queued_cross_chat_delivery(
                "source",
                "queued_status_skip",
                agent_server.SkipQueuedCrossChatDeliveryRequest(
                    cross_chat_exchange_id=exchange["id"],
                    cross_chat_exchange_leg_id=status_leg["id"],
                ),
            )

        refreshed_exchange = await agent_server.CROSS_CHAT.get_exchange(
            exchange["id"]
        )
        refreshed_status = await agent_server.CROSS_CHAT.get_exchange_leg(
            status_leg["id"]
        )
        self.assertEqual(refreshed_exchange["status"], "failed")
        self.assertEqual(refreshed_exchange["error"], "original target failure")
        self.assertEqual(refreshed_status["status"], "cancelled")
        self.assertEqual(refreshed_status["response_state"], "closed")
        status_lifecycle.assert_awaited_once()

    async def test_source_deletion_does_not_reclassify_admitted_target_run(self) -> None:
        record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_running_delete_source",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="already admitted",
            idempotency_key="running-delete-key",
        )
        await agent_server.CROSS_CHAT.update(
            record["id"], expected={"ready"},
            status="running", target_run_id="run_target",
        )
        count = await agent_server.terminalize_cross_chat_session_deletion("source")
        self.assertEqual(count, 0)
        refreshed = await agent_server.CROSS_CHAT.get(record["id"])
        self.assertEqual(refreshed["status"], "running")

    async def test_archive_cancels_paused_target_delivery_and_source_obligation(self) -> None:
        target_record, _created = await agent_server.CROSS_CHAT.create_instruction(
            envelope_id="handoff_archive_target",
            source_session_id="source",
            source_run_id="run_source",
            target_session_id="target",
            body="queued target",
            idempotency_key="archive-target-key",
        )
        await agent_server.CROSS_CHAT.update(
            target_record["id"], expected={"ready"},
            status="queued", queued_id="queued_target",
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_target",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": target_record["id"],
            "_paused_after_stop": True,
        }])
        source_obligation = await agent_server.CROSS_CHAT.create_final_obligation(
            envelope_id="handoff_archive_source",
            source_session_id="source",
            source_run_id="queued_source",
            target_session_id="target",
            idempotency_key="archive-source-key",
        )
        agent_server.QUEUED_TURNS["source"] = deque([{
            "queued_id": "queued_source",
            "prompt": "send @Target",
            "file_ids": [],
            "chat_references": [{
                "session_id": "target",
                "display_title_snapshot": "Target",
                "source_text_start": 5,
                "source_text_end": 12,
                "action": "final_result",
            }],
            "cross_chat_obligation_ids": [source_obligation["id"]],
            "_durable": True,
        }])
        with (
            patch.object(agent_server, "append_durable_event", new_callable=AsyncMock),
            patch.object(agent_server, "append_cross_chat_terminal_lifecycle", new_callable=AsyncMock),
        ):
            await agent_server.terminalize_archived_cross_chat_session("target")
            await agent_server.terminalize_archived_cross_chat_session("source")
        target_after = await agent_server.CROSS_CHAT.get(target_record["id"])
        source_after = await agent_server.CROSS_CHAT.get(source_obligation["id"])
        self.assertEqual(target_after["status"], "cancelled")
        self.assertEqual(source_after["status"], "cancelled")
        self.assertNotIn("target", agent_server.QUEUED_TURNS)
        self.assertEqual(
            agent_server.QUEUED_TURNS["source"][0]["cross_chat_obligation_ids"],
            [],
        )

    async def test_repeated_legacy_direct_retirement_never_starts_a_turn(self) -> None:
        await self.assert_retired_direct_delivery()

    async def test_provider_admission_cas_loses_to_revocation_for_direct_and_queued(self) -> None:
        for suffix, initial, queued_id in (
            ("direct", "submitting", None),
            ("queued", "queued", "queued_before"),
        ):
            record, _created = await agent_server.CROSS_CHAT.create_instruction(
                envelope_id=f"handoff_admit_{suffix}",
                source_session_id="source",
                source_run_id=f"run_source_{suffix}",
                target_session_id="target",
                body=suffix,
                idempotency_key=f"admit-key-{suffix}",
            )
            await agent_server.CROSS_CHAT.update(
                record["id"], expected={"ready"},
                status=initial, queued_id=queued_id,
            )
            await agent_server.CROSS_CHAT.update(
                record["id"], expected={initial}, status="cancelled"
            )
            admitted = await agent_server.admit_cross_chat_delivery_run(
                record["id"], queued_id=queued_id, run_id=f"run_target_{suffix}"
            )
            self.assertIsNone(admitted)
            refreshed = await agent_server.CROSS_CHAT.get(record["id"])
            self.assertEqual(refreshed["status"], "cancelled")

    async def test_legacy_direct_retirement_does_not_depend_on_target_transport(self) -> None:
        await self.assert_retired_direct_delivery(backend="codex")

    async def test_legacy_cursor_direct_delivery_retires_without_headless_launch(self) -> None:
        await self.assert_retired_direct_delivery(backend="cursor")

    async def test_retired_cursor_delivery_rejects_before_provider_admission(self) -> None:
        await self.assert_retired_admission(backend="cursor")

    def test_target_delivery_capabilities_require_native_or_ready_runtimes(self) -> None:
        with patch.object(
            agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_APP_SERVER
        ):
            self.assertEqual(
                agent_server.cross_chat_delivery_client_capabilities({"backend": "codex"}),
                [agent_server.CODEX_INTERACTIVE_CLIENT_CAPABILITY],
            )
        with patch.object(
            agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_EXEC
        ):
            with self.assertRaises(HTTPException):
                agent_server.cross_chat_delivery_client_capabilities({"backend": "codex"})
        with (
            patch.object(
                agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_AGENT_SDK
            ),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=True),
        ):
            self.assertEqual(
                agent_server.cross_chat_delivery_client_capabilities({"backend": "claude"}),
                [agent_server.CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY],
            )
        with patch.object(
            agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_PRINT
        ):
            with self.assertRaises(HTTPException):
                agent_server.cross_chat_delivery_client_capabilities({"backend": "claude"})

        with patch.dict(
            agent_server.RUNTIME_DIAGNOSTICS,
            {
                agent_server.BACKEND_CURSOR: {
                    "backend": agent_server.BACKEND_CURSOR,
                    "status": "ready",
                    "available": True,
                    "installed": True,
                    "authenticated": True,
                    "_executable": "/test/bin/agent",
                }
            },
            clear=True,
        ):
            self.assertEqual(
                agent_server.cross_chat_delivery_client_capabilities(
                    {"backend": "cursor"}
                ),
                [],
            )
        for status in ("unknown", "missing", "unauthenticated", "error"):
            with self.subTest(cursor_status=status), patch.dict(
                agent_server.RUNTIME_DIAGNOSTICS,
                {
                    agent_server.BACKEND_CURSOR: {
                        "backend": agent_server.BACKEND_CURSOR,
                        "status": status,
                    }
                },
                clear=True,
            ):
                with self.assertRaisesRegex(
                    HTTPException,
                    "compatible authenticated headless Cursor CLI",
                ):
                    agent_server.cross_chat_delivery_client_capabilities(
                        {"backend": "cursor"}
                    )

    def test_handoff_capability_advertises_only_admitted_target_backends(self) -> None:
        with (
            patch.object(agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_EXEC),
            patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_PRINT),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=False),
            patch.dict(agent_server.RUNTIME_DIAGNOSTICS, {}, clear=True),
        ):
            unavailable = agent_server.cross_chat_handoffs_capability()
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["supported_target_backends"], [])

        with (
            patch.object(
                agent_server,
                "CODEX_TRANSPORT",
                agent_server.CODEX_TRANSPORT_APP_SERVER,
            ),
            patch.object(
                agent_server,
                "CLAUDE_TRANSPORT",
                agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
            ),
            patch.object(agent_server, "claude_sdk_dependency_available", return_value=True),
            patch.dict(
                agent_server.RUNTIME_DIAGNOSTICS,
                {
                    agent_server.BACKEND_CURSOR: {
                        "backend": agent_server.BACKEND_CURSOR,
                        "status": "ready",
                        "available": True,
                        "installed": True,
                        "authenticated": True,
                        "_executable": "/test/bin/agent",
                    }
                },
                clear=True,
            ),
        ):
            available = agent_server.cross_chat_handoffs_capability()
        self.assertTrue(available["available"])
        self.assertEqual(
            available["supported_target_backends"],
            [
                agent_server.BACKEND_CODEX,
                agent_server.BACKEND_CLAUDE,
                agent_server.BACKEND_CURSOR,
            ],
        )
        self.assertEqual(
            available["required_target_transports"][agent_server.BACKEND_CURSOR],
            "headless-stream-json",
        )

    def test_agent_helper_bypass_allowlist_covers_exact_registered_routes(self) -> None:
        registered = []
        for route in agent_server.app.routes:
            path = str(getattr(route, "path", ""))
            if not path.startswith("/api/agent/"):
                continue
            for method in set(getattr(route, "methods", set())) - {"HEAD", "OPTIONS"}:
                registered.append((method, path))
                sample = (
                    path.replace("{session_id}", "sess")
                    .replace("{job_id}", "job")
                    .replace(
                        "{route_id}",
                        (
                            "mail_0123456789abcdef0123456789abcdef"
                            if path.startswith("/api/agent/team-mail/")
                            else (
                                "team_0123456789abcdef0123456789abcdef"
                                if path.startswith("/api/agent/team/")
                                else "route_0123456789abcdef0123456789abcdef"
                            )
                        ),
                    )
                )
                if path == agent_server.CODEX_PROVIDER_MCP_PATH:
                    # The process-private MCP has its own exact transport
                    # secret and body gate; it must not inherit helper-header
                    # bypass semantics.
                    self.assertFalse(
                        agent_server.is_agent_helper_route(method, sample)
                    )
                else:
                    self.assertTrue(
                        agent_server.is_agent_helper_route(method, sample)
                    )
        self.assertTrue(registered)
        self.assertFalse(
            agent_server.is_agent_helper_route("POST", "/api/agent/future-route")
        )

    async def test_internal_target_admission_has_safe_fifo_queue_projection(self) -> None:
        internal = {
            "id": "internal-start",
            "seq": 2,
            "session_id": "target",
            "type": "turn_started",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_envelope_id": "handoff_hidden",
            "prompt": "Handoff from Source",
        }
        self.assertFalse(agent_server.is_client_visible_event(internal))
        self.assertTrue(agent_server.is_client_visible_event({
            **internal,
            "purpose": None,
        }))
        self.assertIsNone(agent_server.history_search_event_record(internal))
        self.assertTrue(agent_server.is_client_visible_event({
            **internal,
            "type": "assistant_text",
            "text": "Target answer",
        }))
        async with agent_server.QUEUE_LOCK:
            agent_server.QUEUED_TURNS["target"] = agent_server.deque([
                {
                    "queued_id": "queued_secure_peer",
                    "prompt": "opaque remote envelope",
                    "display_prompt": "Encrypted message from Secret Studio",
                    "file_ids": ["remote-file-secret"],
                    "source_session_id": "sha256:secret-peer-identity",
                    "target_session_id": "target",
                    "secure_peer_envelope_id": "secure-envelope-secret",
                    "chat_references": [{
                        "session_id": "secret",
                        "display_title_snapshot": "Secret",
                        "source_text_start": 0,
                        "source_text_end": 1,
                        "action": "route",
                    }],
                    "team_references": [{
                        "kind": "recipient",
                        "recipient_kind": "human",
                        "team_id": "team-secret",
                        "target_id": "secret",
                        "display_name_snapshot": "Secret",
                    }],
                    "purpose": "secure_peer_handoff_delivery",
                },
                {
                    "queued_id": "queued_internal",
                    "prompt": "private provider wrapper",
                    "display_prompt": "Agent-authored same-server handoff",
                    "purpose": "cross_chat_handoff_delivery",
                    "cross_chat_envelope_id": "handoff_hidden",
                },
                {
                    "queued_id": "queued_user",
                    "prompt": "visible",
                    "purpose": None,
                },
                {
                    "queued_id": "queued_exchange_internal",
                    "prompt": "internal exchange",
                    "purpose": "cross_chat_handoff_delivery",
                    "cross_chat_exchange_id": "exchange_hidden",
                    "cross_chat_exchange_leg_id": "leg_hidden",
                },
            ])
        snapshot = await agent_server.queued_turns_snapshot("target")
        self.assertEqual(
            [item["queued_id"] for item in snapshot],
            [
                "queued_secure_peer",
                "queued_internal",
                "queued_user",
                "queued_exchange_internal",
            ],
        )
        self.assertEqual([item["position"] for item in snapshot], [1, 2, 3, 4])
        self.assertEqual(snapshot[0]["prompt"], "Incoming secure-peer delivery")
        self.assertEqual(
            snapshot[0]["display_prompt"],
            "Incoming secure-peer delivery",
        )
        self.assertEqual(snapshot[0]["file_ids"], [])
        self.assertEqual(snapshot[0]["chat_references"], [])
        self.assertEqual(snapshot[0]["team_references"], [])
        self.assertIsNone(snapshot[0]["source_session_id"])
        self.assertIsNone(snapshot[0]["target_session_id"])
        self.assertNotIn("secure_peer_envelope_id", snapshot[0])
        self.assertEqual(
            snapshot[1]["prompt"],
            "Agent-authored same-server handoff",
        )
        self.assertEqual(snapshot[3]["prompt"], "Incoming cross-chat message")
        self.assertNotIn("opaque remote envelope", repr(snapshot))
        self.assertNotIn("Secret Studio", repr(snapshot))
        self.assertNotIn("remote-file-secret", repr(snapshot))
        self.assertNotIn("secret-peer-identity", repr(snapshot))
        self.assertNotIn("secure-envelope-secret", repr(snapshot))
        self.assertNotIn("private provider wrapper", repr(snapshot))
        self.assertNotIn("internal exchange", repr(snapshot))

        event_file = self.root / "client-events.jsonl"
        events = [
            {
                "id": "normal-start",
                "seq": 1,
                "session_id": "target",
                "type": "turn_started",
                "prompt": "Visible user prompt",
            },
            internal,
            {
                "id": "internal-reasoning",
                "seq": 3,
                "session_id": "target",
                "type": "reasoning_summary",
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_envelope_id": "handoff_hidden",
                "text": "Visible target reasoning",
            },
            {
                "id": "internal-answer",
                "seq": 4,
                "session_id": "target",
                "type": "assistant_text",
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_envelope_id": "handoff_hidden",
                "text": "Visible target answer",
            },
            {
                **internal,
                "id": "hidden-newest-start",
                "seq": 5,
            },
        ]
        event_file.write_text(
            "".join(json.dumps(event, separators=(",", ":")) + "\n" for event in events),
            encoding="utf-8",
        )
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(
                agent_server,
                "reconcile_idle_queue_session_from_snapshot",
                new_callable=AsyncMock,
            ),
        ):
            page = agent_server.read_client_events_page("target", limit=2)
            self.assertEqual([event["seq"] for event in page[0]], [1, 3])
            self.assertEqual(page[1], 5)
            self.assertEqual(page[2:], (3, 0, 1))
            tail = agent_server.read_client_events_page("target", limit=2, tail=True)
            self.assertEqual([event["seq"] for event in tail[0]], [3, 4])
            self.assertEqual(tail[1], 5)
            self.assertEqual(tail[2:], (3, 1, 0))
            visible = agent_server.read_visible_events_page("target", limit=10)
            self.assertEqual([event["seq"] for event in visible[0]], [1, 3, 4])
            self.assertEqual(visible[2:], (3, 0, 0))
            catchup = agent_server.read_event_catchup_batch(
                "target", after=0, through=5, limit=10
            )
            self.assertEqual([event["seq"] for event in catchup[0]], [1, 3, 4])
            response = await agent_server.get_session(
                "target", limit=2, tail=False
            )
            self.assertEqual([event["seq"] for event in response["events"]], [1, 3])
            self.assertEqual(response["latest_seq"], 5)
            self.assertEqual(response["event_count"], 3)
            self.assertEqual(response["events_omitted_after"], 1)

        broadcast = AsyncMock()
        with (
            patch.object(agent_server, "ensure_dirs"),
            patch.object(agent_server, "events_path", return_value=self.root / "live.jsonl"),
            patch.object(agent_server, "next_event_seq", AsyncMock(side_effect=[1, 2])),
            patch.object(agent_server, "update_session_event_metadata", AsyncMock()),
            patch.object(agent_server.HUB, "broadcast", broadcast),
        ):
            await agent_server.append_event("target", "turn_started", {
                key: value for key, value in internal.items()
                if key not in {"id", "seq", "session_id", "type"}
            })
            await agent_server.append_event("target", "assistant_text", {
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_envelope_id": "handoff_hidden",
                "text": "Visible target answer",
            })
        broadcast.assert_awaited_once()
        self.assertEqual(broadcast.await_args.args[1]["type"], "assistant_text")

        exchange_internal = {
            **internal,
            "cross_chat_envelope_id": None,
            "cross_chat_exchange_id": "exchange_hidden",
            "cross_chat_exchange_leg_id": "leg_hidden",
            "exchange_id": "exchange_hidden",
            "exchange_leg_id": "leg_hidden",
        }
        self.assertFalse(agent_server.is_client_visible_event(exchange_internal))
        self.assertIsNone(
            agent_server.history_search_event_record(exchange_internal)
        )
        self.assertTrue(agent_server.is_client_visible_event({
            **exchange_internal,
            "type": "reasoning_summary",
            "text": "Visible exchange reasoning",
        }))
        self.assertTrue(agent_server.is_client_visible_event({
            **exchange_internal,
            "type": "assistant_text",
            "text": "Visible exchange answer",
        }))

        exchange_file = self.root / "exchange-client-events.jsonl"
        exchange_events = [
            {
                "id": "exchange-normal",
                "seq": 1,
                "session_id": "target",
                "type": "assistant_text",
                "text": "ordinary",
            },
            {**exchange_internal, "id": "exchange-start", "seq": 2},
            {
                "id": "exchange-lifecycle",
                "seq": 3,
                "session_id": "target",
                "type": "cross_chat_exchange_leg_started",
                "exchange_id": "exchange_hidden",
                "exchange_leg_id": "leg_hidden",
                "exchange_status": "active",
                "exchange_leg_status": "running",
                "message": "Working in this chat.",
            },
            {
                **exchange_internal,
                "id": "exchange-reasoning",
                "seq": 4,
                "type": "reasoning_summary",
                "text": "Visible exchange reasoning",
            },
            {
                **exchange_internal,
                "id": "exchange-answer",
                "seq": 5,
                "type": "assistant_text",
                "text": "Visible exchange answer",
            },
            {
                **exchange_internal,
                "id": "exchange-queue",
                "seq": 6,
                "type": "turn_queued",
            },
        ]
        exchange_file.write_text(
            "".join(json.dumps(event) + "\n" for event in exchange_events),
            encoding="utf-8",
        )
        with (
            patch.object(agent_server, "events_path", return_value=exchange_file),
            patch.object(
                agent_server,
                "TIMELINE_INDEX_CACHE",
                agent_server.OrderedDict(),
            ),
        ):
            default_page = agent_server.read_client_events_page(
                "target", limit=10
            )
            visible_page = agent_server.read_visible_events_page(
                "target", limit=10
            )
            catchup_page = agent_server.read_event_catchup_batch(
                "target", after=0, through=6, limit=10
            )
            semantic_page = agent_server.read_semantic_timeline_page(
                "target", limit=10, tail=True
            )
        self.assertEqual(
            [event["seq"] for event in default_page[0]],
            [1, 3, 4, 5],
        )
        self.assertEqual(
            [event["seq"] for event in visible_page[0]],
            [1, 3, 4, 5],
        )
        self.assertEqual(
            [event["seq"] for event in catchup_page[0]],
            [1, 3, 4, 5],
        )
        semantic_types = [event["type"] for event in semantic_page["events"]]
        self.assertNotIn("turn_started", semantic_types)
        self.assertNotIn("turn_queued", semantic_types)
        self.assertIn("reasoning_summary", semantic_types)
        self.assertIn("assistant_text", semantic_types)

        exchange_broadcast = AsyncMock()
        with (
            patch.object(agent_server, "ensure_dirs"),
            patch.object(
                agent_server,
                "events_path",
                return_value=self.root / "exchange-live.jsonl",
            ),
            patch.object(agent_server, "next_event_seq", AsyncMock(side_effect=[1, 2])),
            patch.object(agent_server, "update_session_event_metadata", AsyncMock()),
            patch.object(agent_server.HUB, "broadcast", exchange_broadcast),
        ):
            await agent_server.append_event("target", "turn_started", {
                key: value for key, value in exchange_internal.items()
                if key not in {"id", "seq", "session_id", "type"}
            })
            await agent_server.append_event("target", "assistant_text", {
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_exchange_id": "exchange_hidden",
                "cross_chat_exchange_leg_id": "leg_hidden",
                "text": "Visible exchange answer",
            })
        exchange_broadcast.assert_awaited_once()
        self.assertEqual(
            exchange_broadcast.await_args.args[1]["type"],
            "assistant_text",
        )

        batch_broadcast = AsyncMock()
        with (
            patch.object(agent_server, "ensure_dirs"),
            patch.object(
                agent_server,
                "events_path",
                return_value=self.root / "exchange-live-batch.jsonl",
            ),
            patch.object(agent_server, "next_event_seq", AsyncMock(return_value=1)),
            patch.object(agent_server, "update_session_event_metadata", AsyncMock()),
            patch.object(agent_server.HUB, "broadcast", batch_broadcast),
        ):
            await agent_server.append_durable_event_batch("target", [
                (
                    "turn_queued",
                    {
                        "purpose": "cross_chat_handoff_delivery",
                        "cross_chat_exchange_id": "exchange_hidden",
                        "cross_chat_exchange_leg_id": "leg_hidden",
                        "prompt": "Synthetic exchange queue row",
                    },
                ),
                (
                    "assistant_text",
                    {
                        "purpose": "cross_chat_handoff_delivery",
                        "cross_chat_exchange_id": "exchange_hidden",
                        "cross_chat_exchange_leg_id": "leg_hidden",
                        "text": "Visible exchange batch answer",
                    },
                ),
            ])
        batch_broadcast.assert_awaited_once()
        self.assertEqual(
            batch_broadcast.await_args.args[1]["type"],
            "assistant_text",
        )

    async def test_ordinary_turn_rejects_reserved_cross_chat_envelope(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            await agent_server._start_turn_locked(
                "target",
                agent_server.TurnRequest(
                    prompt="Ordinary prompt",
                    cross_chat_envelope_id="forged-envelope",
                ),
                queue_if_busy=False,
            )
        self.assertEqual(raised.exception.status_code, 400)

    def test_unauthenticated_server_rejects_cross_chat_references(self) -> None:
        reference = agent_server.ChatReference(
            session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=1, action="instruction",
        )
        agent_server.AGENT_TOKEN = ""
        with self.assertRaises(HTTPException) as raised:
            agent_server.validate_chat_references("source", "@", [reference])
        self.assertEqual(raised.exception.status_code, 503)

    def test_semantic_pagination_groups_handoff_lifecycle_and_hides_synthetic_prompt(self) -> None:
        event_file = self.root / "timeline.jsonl"
        events = [
            {"id": "e1", "seq": 1, "type": "cross_chat_handoff_received", "handoff_id": "handoff_group", "cross_chat_envelope_id": "handoff_group", "handoff_status": "received", "handoff_action": "instruction", "message": "Received"},
            {"id": "e2", "seq": 2, "type": "cross_chat_handoff_queued", "handoff_id": "handoff_group", "cross_chat_envelope_id": "handoff_group", "handoff_status": "queued", "handoff_action": "instruction", "message": "Queued"},
            {"id": "e3", "seq": 3, "type": "turn_started", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "cross_chat_envelope_id": "handoff_group", "prompt": "synthetic relay"},
            {"id": "e4", "seq": 4, "type": "turn_finished", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "cross_chat_envelope_id": "handoff_group", "result_text": "done", "exit_code": 0},
            {"id": "e5", "seq": 5, "type": "cross_chat_handoff_delivered", "handoff_id": "handoff_group", "cross_chat_envelope_id": "handoff_group", "handoff_status": "delivered", "handoff_action": "instruction", "message": "Delivered"},
        ]
        event_file.write_text("".join(json.dumps(event) + "\n" for event in events))
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            page = agent_server.read_semantic_timeline_page("target", limit=10, tail=True)
        self.assertEqual(page["semantic_item_count"], 1)
        self.assertNotIn("turn_started", [event["type"] for event in page["events"]])
        self.assertIn("cross_chat_handoff_delivered", [event["type"] for event in page["events"]])

        event_file.write_text(json.dumps(events[2]) + "\n")
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            orphan_page = agent_server.read_semantic_timeline_page(
                "target", limit=10, tail=True
            )
        self.assertEqual(orphan_page["semantic_item_count"], 0)
        self.assertEqual(orphan_page["events"], [])

    def test_semantic_pagination_hides_internal_exchange_status_run(self) -> None:
        event_file = self.root / "status-leg-timeline.jsonl"
        status_turn = {
            "run_id": "status-run",
            "purpose": agent_server.LOCAL_CROSS_CHAT_DELIVERY_PURPOSE,
            "cross_chat_exchange_id": "exchange_status",
            "cross_chat_exchange_leg_id": "status-leg",
        }
        marked_status_turn = {
            **status_turn,
            "cross_chat_exchange_status": True,
        }
        with patch.dict(
            agent_server.RUN_METADATA,
            {"status-run": marked_status_turn},
            clear=True,
        ):
            inherited = agent_server.inherit_internal_status_run_metadata({
                "run_id": "status-run",
                "backend": "claude",
            })
        self.assertTrue(inherited["cross_chat_exchange_status"])
        self.assertEqual(inherited["cross_chat_exchange_leg_id"], "status-leg")
        events = [
            {"id": "summary", "seq": 1, "type": "cross_chat_exchange_failed", "exchange_id": "exchange_status", "exchange_status": "failed", "message": "Exchange failed"},
            {"id": "status-registered", "seq": 2, "type": "cross_chat_exchange_leg_registered", "exchange_id": "exchange_status", "exchange_leg_id": "status-leg", "exchange_leg_kind": "status", "exchange_leg_status": "registered"},
            {"id": "status-started", "seq": 3, "type": "turn_started", "prompt": "Internal status prompt", **marked_status_turn},
            {"id": "status-process", "seq": 4, "type": "process_started", "run_id": "status-run", "backend": "claude"},
            {"id": "status-provider", "seq": 5, "type": "provider_session", "run_id": "status-run", "backend": "claude"},
            {"id": "status-reasoning", "seq": 6, "type": "reasoning_summary", "run_id": "status-run", "text": "Processing status"},
            {"id": "status-tool", "seq": 7, "type": "tool_started", "run_id": "status-run", "tool": {"name": "Internal"}},
            {"id": "status-output", "seq": 8, "type": "assistant_text", "text": "Acknowledged", **marked_status_turn},
            {"id": "status-finished", "seq": 9, "type": "turn_finished", "result_text": "Acknowledged", "exit_code": 0, **marked_status_turn},
            {"id": "status-delivered", "seq": 10, "type": "cross_chat_exchange_leg_delivered", "exchange_id": "exchange_status", "exchange_leg_id": "status-leg", "cross_chat_exchange_status": True, "exchange_leg_status": "delivered"},
            {"id": "turn-started", "seq": 11, "type": "turn_started", "run_id": "ordinary", "prompt": "Still visible"},
            {"id": "turn-finished", "seq": 12, "type": "turn_finished", "run_id": "ordinary", "result_text": "Done", "exit_code": 0},
        ]
        event_file.write_text(
            "".join(json.dumps(event) + "\n" for event in events)
        )
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(
                agent_server,
                "TIMELINE_INDEX_CACHE",
                agent_server.OrderedDict(),
            ),
        ):
            page = agent_server.read_semantic_timeline_page(
                "target", limit=2, tail=True
            )

        self.assertEqual(page["semantic_total"], 2)
        self.assertEqual(page["semantic_item_count"], 2)
        self.assertEqual(page["semantic_omitted_before"], 0)
        event_types = [event["type"] for event in page["events"]]
        self.assertIn("cross_chat_exchange_failed", event_types)
        self.assertIn("turn_finished", event_types)
        self.assertFalse(any(
            event_type.startswith("cross_chat_exchange_leg_")
            for event_type in event_types
        ))
        self.assertNotIn("status-run", {
            str(event.get("run_id") or "") for event in page["events"]
        })

    def test_source_handoff_lifecycle_does_not_hijack_source_run(self) -> None:
        event_file = self.root / "source-timeline.jsonl"
        events = [
            {"id": "s1", "seq": 1, "type": "turn_started", "run_id": "run_source", "prompt": "work"},
            {"id": "h1", "seq": 2, "type": "cross_chat_handoff_registered", "run_id": "run_source", "handoff_id": "handoff_a", "cross_chat_envelope_id": "handoff_a", "handoff_status": "registered", "handoff_action": "final_result", "message": "A"},
            {"id": "h2", "seq": 3, "type": "cross_chat_handoff_registered", "run_id": "run_source", "handoff_id": "handoff_b", "cross_chat_envelope_id": "handoff_b", "handoff_status": "registered", "handoff_action": "final_result", "message": "B"},
            {"id": "s2", "seq": 4, "type": "reasoning_summary", "run_id": "run_source", "text": "thinking"},
            {"id": "s3", "seq": 5, "type": "turn_finished", "run_id": "run_source", "result_text": "source answer", "exit_code": 0},
        ]
        event_file.write_text("".join(json.dumps(event) + "\n" for event in events))
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            page = agent_server.read_semantic_timeline_page("source", limit=10, tail=True)
        self.assertEqual(page["semantic_item_count"], 3)
        source_finished = [
            event for event in page["events"]
            if event.get("type") == "turn_finished" and event.get("run_id") == "run_source"
        ]
        self.assertEqual(len(source_finished), 1)

    def test_cross_chat_reload_keeps_target_trace_tools_and_artifacts(self) -> None:
        event_file = self.root / "target-trace-timeline.jsonl"
        base = {
            "cross_chat_envelope_id": "handoff_trace",
            "handoff_id": "handoff_trace",
        }
        events = [
            {"id": "c1", "seq": 1, "type": "cross_chat_handoff_started", "handoff_status": "running", "handoff_action": "instruction", "message": "Started", **base},
            {"id": "c2", "seq": 2, "type": "reasoning_summary", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "text": "reason", **base},
            {"id": "c3", "seq": 3, "type": "tool_finished", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "tool_name": "shell", "output": "ok", **base},
            {"id": "c4", "seq": 4, "type": "artifact_created", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "artifact": {"id": "artifact"}, **base},
            {"id": "c5", "seq": 5, "type": "turn_finished", "run_id": "run_target", "purpose": "cross_chat_handoff_delivery", "result_text": "done", "exit_code": 0, **base},
            {"id": "c6", "seq": 6, "type": "cross_chat_handoff_delivered", "handoff_status": "delivered", "handoff_action": "instruction", "message": "Delivered", **base},
        ]
        event_file.write_text("".join(json.dumps(event) + "\n" for event in events))
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "TIMELINE_INDEX_CACHE", agent_server.OrderedDict()),
        ):
            page = agent_server.read_semantic_timeline_page("target", limit=20, tail=True)
        types = {event["type"] for event in page["events"]}
        self.assertIn("reasoning_summary", types)
        self.assertIn("tool_finished", types)
        self.assertIn("artifact_created", types)
        self.assertIn("turn_finished", types)

    async def test_legacy_registered_exchange_retires_before_durable_provider_claim(self) -> None:
        await self.assert_retired_exchange_delivery(reconcile=True)

    async def test_bare_exchange_turn_reservation_is_not_a_live_run(self) -> None:
        _exchange, leg = await self.create_exchange("exchange_bare_reservation")
        agent_server.CURRENT_TURNS["target"] = {
            "run_id": None,
            "cross_chat_exchange_leg_id": leg["id"],
        }
        self.assertIsNone(
            await agent_server.live_cross_chat_exchange_leg_state(
                "target",
                leg["id"],
            )
        )

        agent_server.CURRENT_TURNS["target"]["run_id"] = "run_reserved"
        self.assertEqual(
            await agent_server.live_cross_chat_exchange_leg_state(
                "target",
                leg["id"],
            ),
            {"status": "running", "target_run_id": "run_reserved"},
        )

    async def test_legacy_exchange_submission_never_claims_a_provider(self) -> None:
        await self.assert_retired_exchange_delivery(state="submitting")

    async def test_legacy_submitting_exchange_retires_on_reconcile(self) -> None:
        await self.assert_retired_exchange_delivery(state="submitting", reconcile=True)

    async def test_legacy_registered_exchange_repeated_submission_is_terminal(self) -> None:
        await self.assert_retired_exchange_delivery()

    async def test_legacy_queued_exchange_is_retired_without_provider_promotion(self) -> None:
        await self.assert_retired_exchange_delivery(state="queued", reconcile=True)

    async def test_admission_lock_registries_retire_after_waiter_cancellation(
        self,
    ) -> None:
        registries = (
            (
                "direct_registry_cleanup",
                agent_server.cross_chat_delivery_admission,
                agent_server.CROSS_CHAT_DELIVERY_ADMISSION_LOCKS,
                agent_server.CROSS_CHAT_DELIVERY_ADMISSION_OWNERS,
                agent_server.CROSS_CHAT_DELIVERY_ADMISSION_REFCOUNTS,
            ),
            (
                "exchange_registry_cleanup",
                agent_server.cross_chat_exchange_leg_admission,
                agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_LOCKS,
                agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_OWNERS,
                agent_server.CROSS_CHAT_EXCHANGE_LEG_ADMISSION_REFCOUNTS,
            ),
        )
        for key, admission, locks, owners, refcounts in registries:
            entered = asyncio.Event()
            release = asyncio.Event()

            async def hold_owner() -> None:
                async with admission(key):
                    entered.set()
                    await release.wait()

            async def wait_for_owner() -> None:
                async with admission(key):
                    self.fail("cancelled admission waiter acquired the fence")

            owner = asyncio.create_task(hold_owner())
            waiter: asyncio.Task | None = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                retained_lock = locks[key]
                self.assertIs(owners.get(key), owner)
                self.assertEqual(refcounts.get(key), 1)

                waiter = asyncio.create_task(wait_for_owner())
                await asyncio.sleep(0)
                self.assertEqual(refcounts.get(key), 2)
                self.assertIs(locks.get(key), retained_lock)

                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertEqual(refcounts.get(key), 1)
                self.assertIs(locks.get(key), retained_lock)
                self.assertIs(owners.get(key), owner)

                release.set()
                await asyncio.wait_for(owner, timeout=1)
                self.assertNotIn(key, locks)
                self.assertNotIn(key, owners)
                self.assertNotIn(key, refcounts)
            finally:
                release.set()
                pending = [
                    task
                    for task in (owner, waiter)
                    if task is not None and not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    async def test_cross_chat_keyed_locks_retire_after_waiter_cancellation(
        self,
    ) -> None:
        registries = (
            (
                "lifecycle_registry_cleanup",
                agent_server.cross_chat_lifecycle_lock,
                agent_server.CROSS_CHAT_LIFECYCLE_LOCKS,
                agent_server.CROSS_CHAT_LIFECYCLE_LOCK_REFCOUNTS,
            ),
            (
                "exchange_registry_cleanup",
                agent_server.cross_chat_exchange_lock,
                agent_server.CROSS_CHAT_EXCHANGE_LOCKS,
                agent_server.CROSS_CHAT_EXCHANGE_LOCK_REFCOUNTS,
            ),
            (
                "live_lease_registry_cleanup",
                agent_server.cross_chat_live_lease_lock,
                agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS,
                agent_server.CROSS_CHAT_LIVE_LEASE_LOCK_REFCOUNTS,
            ),
        )
        for key, keyed_lock, locks, refcounts in registries:
            entered = asyncio.Event()
            release = asyncio.Event()

            async def hold_owner() -> None:
                async with keyed_lock(key):
                    entered.set()
                    await release.wait()

            async def wait_for_owner() -> None:
                async with keyed_lock(key):
                    self.fail("cancelled keyed-lock waiter acquired the fence")

            owner = asyncio.create_task(hold_owner())
            waiter: asyncio.Task | None = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                retained_lock = locks[key]
                self.assertEqual(refcounts.get(key), 1)

                waiter = asyncio.create_task(wait_for_owner())
                await asyncio.sleep(0)
                self.assertEqual(refcounts.get(key), 2)
                self.assertIs(locks.get(key), retained_lock)

                waiter.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiter
                self.assertEqual(refcounts.get(key), 1)
                self.assertIs(locks.get(key), retained_lock)

                release.set()
                await asyncio.wait_for(owner, timeout=1)
                self.assertNotIn(key, locks)
                self.assertNotIn(key, refcounts)
            finally:
                release.set()
                pending = [
                    task
                    for task in (owner, waiter)
                    if task is not None and not task.done()
                ]
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    async def test_legacy_queued_exchange_repeated_submit_preserves_adjacent_user(self) -> None:
        await self.assert_retired_exchange_delivery(state="queued")

    async def test_legacy_unowned_running_exchange_retires_on_restart(self) -> None:
        await self.assert_retired_exchange_delivery(state="running", reconcile=True)

    async def test_legacy_direct_queued_owner_is_retired_without_promotion(self) -> None:
        await self.assert_retired_direct_delivery(state="queued", reconcile=True)

    async def test_legacy_direct_unowned_running_snapshot_retires_without_resurrection(self) -> None:
        await self.assert_retired_direct_delivery(state="running", reconcile=True)

    async def test_legacy_direct_submitting_snapshot_retires_before_reservation(self) -> None:
        await self.assert_retired_direct_delivery(state="submitting", reconcile=True)

    async def test_secure_peer_queue_promotion_owner_fences_reconcile(
        self,
    ) -> None:
        envelope_id = "secure_peer_promotion_owner"
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_secure_peer_promotion",
            "prompt": "Encrypted delivery",
            "file_ids": [],
            "purpose": "secure_peer_handoff_delivery",
            "source_session_id": "peer-server",
            "target_session_id": "target",
            "secure_peer_envelope_id": envelope_id,
            "client_capabilities": [],
        }])
        promotion_entered = asyncio.Event()
        release_promotion = asyncio.Event()
        promotion: asyncio.Task | None = None
        record = {
            "envelope_id": envelope_id,
            "target_chat_id": "target",
            "state": "queued",
            "queued_id": "queued_secure_peer_promotion",
        }

        async def pause_start(*_args, **_kwargs):
            promotion_entered.set()
            await release_promotion.wait()
            return {"queued": False}

        try:
            with (
                patch.object(
                    agent_server,
                    "reconcile_idle_queue_session",
                    AsyncMock(),
                ),
                patch.object(
                    agent_server,
                    "_start_turn_locked",
                    AsyncMock(side_effect=pause_start),
                ),
                patch.object(
                    agent_server.SECURE_PEER_RUNTIME,
                    "recover_prepared_deliveries",
                    return_value=[],
                ),
                patch.object(
                    agent_server.SECURE_PEER_RUNTIME,
                    "recoverable_deliveries",
                    return_value=[record],
                ),
                patch.object(
                    agent_server.SECURE_PEER_RUNTIME,
                    "finish_delivery",
                ) as finish,
            ):
                promotion = asyncio.create_task(
                    agent_server.start_next_queued_turn("target")
                )
                await asyncio.wait_for(promotion_entered.wait(), timeout=1)
                self.assertNotIn("target", agent_server.QUEUED_TURNS)

                recovered = await asyncio.wait_for(
                    agent_server.reconcile_secure_peer_deliveries(),
                    timeout=1,
                )
                self.assertEqual(recovered, 0)
                terminal_recovered = await asyncio.wait_for(
                    agent_server.reconcile_secure_peer_terminal_orphans(),
                    timeout=1,
                )
                self.assertEqual(terminal_recovered, 0)
                finish.assert_not_called()
                self.assertFalse(promotion.done())

                release_promotion.set()
                await asyncio.wait_for(promotion, timeout=1)
        finally:
            release_promotion.set()
            if promotion is not None and not promotion.done():
                promotion.cancel()
                await asyncio.gather(promotion, return_exceptions=True)

    async def test_exchange_recovery_preserves_historical_explicit_child_without_execution(self) -> None:
        exchange, parent = await self.create_exchange("exchange_explicit_restart")
        parent = await agent_server.CROSS_CHAT.update_exchange_leg(
            parent["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_target",
        )
        exchange, child, created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=parent["id"],
            source_session_id="target",
            source_run_id="run_target",
            body="Can you clarify?",
            request_response=True,
            idempotency_key="explicit-child",
            automatic=False,
        )
        self.assertTrue(created)
        provider = AsyncMock(side_effect=AssertionError("retired child started provider"))
        with (
            patch.object(agent_server, "live_cross_chat_exchange_leg_state", AsyncMock(return_value=None)),
            patch.object(agent_server, "cross_chat_exchange_events", return_value=[]),
            patch.object(agent_server, "start_turn_durably", provider),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        parent = await agent_server.CROSS_CHAT.get_exchange_leg(parent["id"])
        exchange = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(parent["status"], "cancelled")
        self.assertEqual(parent["response_state"], "explicit_committed")
        self.assertEqual(exchange["status"], "cancelled")
        self.assertEqual(exchange["active_leg_id"], child["id"])
        child_after = await agent_server.CROSS_CHAT.get_exchange_leg(child["id"])
        self.assertEqual(child_after["status"], "cancelled")
        self.assertEqual(child_after["body"], "Can you clarify?")
        self.assertEqual(parent["body"], "Please investigate")
        provider.assert_not_awaited()

    async def test_exchange_recovery_does_not_copy_final_answer_after_delivered_commit(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_auto_restart")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_target",
        )
        await agent_server.CROSS_CHAT.finish_exchange_leg(
            inbound["id"], status="delivered"
        )
        terminal = {
            "type": "turn_finished",
            "run_id": "run_target",
            "purpose": "cross_chat_handoff_delivery",
            "exchange_id": exchange["id"],
            "exchange_leg_id": inbound["id"],
            "result_text": "Recovered answer",
            "exit_code": 0,
            "stopped": False,
        }

        provider = AsyncMock(side_effect=AssertionError("recovered final started provider"))
        with (
            patch.object(agent_server, "cross_chat_exchange_events", return_value=[terminal]),
            patch.object(agent_server, "start_turn_durably", provider),
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["status"], "delivered")
        self.assertEqual(legs[0]["body"], "Please investigate")
        self.assertNotEqual(legs[0]["response_state"], "automatic_committed")
        self.assertEqual((await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["status"], "cancelled")
        provider.assert_not_awaited()

    async def test_exchange_failure_status_wakes_current_waiting_sender_in_both_directions(self) -> None:
        async def capture_direction(exchange, failed_leg):
            with (
                patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
                patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
            ):
                await agent_server.maybe_deliver_cross_chat_exchange_failure_status(
                    exchange,
                    failed_session_id=failed_leg["target_session_id"],
                    failed_leg=failed_leg,
                )
            return next(
                leg for leg in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
                if leg["kind"] == "status"
            )

        first_exchange, first_leg = await self.create_exchange("exchange_status_forward")
        failed = await agent_server.CROSS_CHAT.finish_exchange_leg(
            first_leg["id"],
            status="failed",
            error_code="target_failed",
            error="B failed",
        )
        first_status = await capture_direction(*failed)
        self.assertEqual(
            (first_status["source_session_id"], first_status["target_session_id"]),
            ("target", "source"),
        )

        reverse_exchange, reverse_parent = await self.create_exchange("exchange_status_reverse")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            reverse_parent["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_target_reverse",
        )
        reverse_exchange, reverse_leg, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=reverse_exchange["id"],
            inbound_leg_id=reverse_parent["id"],
            source_session_id="target",
            source_run_id="run_target_reverse",
            body="A follow-up question",
            request_response=True,
            idempotency_key="reverse-follow-up",
            automatic=False,
        )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            reverse_leg["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_source_reverse",
        )
        failed = await agent_server.CROSS_CHAT.finish_exchange_leg(
            reverse_leg["id"],
            status="failed",
            error_code="target_failed",
            error="A failed",
        )
        reverse_status = await capture_direction(*failed)
        self.assertEqual(
            (reverse_status["source_session_id"], reverse_status["target_session_id"]),
            ("source", "target"),
        )

    async def test_exchange_failure_status_is_metadata_even_when_provider_capacity_is_full(self) -> None:
        exchange, failed_leg = await self.create_exchange(
            "exchange_status_transient_retry"
        )
        exchange, failed_leg = await agent_server.CROSS_CHAT.finish_exchange_leg(
            failed_leg["id"],
            status="failed",
            error_code="target_failed",
            error="target failed before answering",
        )
        provider = AsyncMock(side_effect=agent_server.TransientAdmissionWait(status_code=503, detail="capacity"))
        with (
            patch.object(agent_server, "start_turn_durably", provider),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            for _ in range(2):
                await agent_server.maybe_deliver_cross_chat_exchange_failure_status(
                    exchange, failed_session_id=failed_leg["target_session_id"], failed_leg=failed_leg)
            status_legs = [item for item in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
                           if item["kind"] == "status"]
            self.assertEqual(len(status_legs), 1)
            await agent_server.reconcile_cross_chat_exchange_leg(status_legs[0])
        status_leg = await agent_server.CROSS_CHAT.get_exchange_leg(status_legs[0]["id"])
        self.assertEqual(status_leg["status"], "delivered")
        self.assertEqual(status_leg["error_code"], "target_failed")
        self.assertIsNone(status_leg["queued_id"])
        self.assertEqual((await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["status"], "failed")
        provider.assert_not_awaited()

    async def test_exchange_reconcile_creates_missing_failure_status_outbox(self) -> None:
        exchange, leg = await self.create_exchange("exchange_status_outbox")
        exchange, leg = await agent_server.CROSS_CHAT.finish_exchange_leg(
            leg["id"],
            status="failed",
            error_code="target_failed",
            error="crashed before status wake",
        )
        self.assertFalse(any(
            item["kind"] == "status"
            for item in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        ))
        order = []

        async def leg_lifecycle(*_args, **_kwargs):
            order.append("leg")

        async def exchange_lifecycle(*_args, **_kwargs):
            order.append("exchange")

        async def submit_status(*_args, **_kwargs):
            order.append("wake")

        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock(side_effect=leg_lifecycle)),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock(side_effect=exchange_lifecycle)),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock(side_effect=submit_status)),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
            await agent_server.reconcile_cross_chat_exchanges()
        status_legs = [
            item for item in await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
            if item["kind"] == "status"
        ]
        self.assertEqual(len(status_legs), 1)
        self.assertEqual(status_legs[0]["target_session_id"], "source")
        self.assertLess(order.index("exchange"), order.index("wake"))

    async def test_exchange_cancel_route_returns_leg_bodies(self) -> None:
        exchange, _leg = await self.create_exchange("exchange_cancel_body")
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            response = await agent_server.post_cancel_cross_chat_exchange(exchange["id"])
        self.assertEqual(response["exchange"]["legs"][0]["body"], "Please investigate")

    async def test_retired_exchange_cannot_create_followup_or_terminal_budget_fallback(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_budget_fallback")
        for ordinal in range(2, 6):
            source_session = inbound["target_session_id"]
            source_run = f"run_budget_{ordinal}"
            await agent_server.CROSS_CHAT.update_exchange_leg(
                inbound["id"],
                expected={"registered"},
                status="running",
                target_run_id=source_run,
            )
            exchange, inbound, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
                exchange_id=exchange["id"],
                inbound_leg_id=inbound["id"],
                source_session_id=source_session,
                source_run_id=source_run,
                body=f"Follow-up {ordinal}",
                request_response=True,
                idempotency_key=f"budget-{ordinal}",
                automatic=False,
            )
        self.assertEqual(exchange["used_legs"], 5)
        response_run = "run_budget_final"
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id=response_run,
        )
        authority_path = await agent_server.issue_cross_chat_capability(
            inbound["target_session_id"],
            response_run,
            [],
            actions={"cross_chat_response"},
            exchange_response_grants={(exchange["id"], inbound["id"])},
        )
        token = json.loads(authority_path.read_text())["provider_capability"]
        agent_server.CURRENT_TURNS = {
            inbound["target_session_id"]: {"run_id": response_run}
        }
        with self.assertRaises(HTTPException) as raised:
            await agent_server.create_authorized_cross_chat_exchange_response(
                token,
                exchange["id"],
                agent_server.CrossChatExchangeResponseRequest(
                    inbound_leg_id=inbound["id"],
                    body="One more question",
                    request_response=True,
                    idempotency_key="budget-too-large",
                ),
            )
        self.assertEqual(raised.exception.status_code, 403)
        with self.assertRaises(HTTPException) as terminal_rejected:
            await agent_server.create_authorized_cross_chat_exchange_response(
                token, exchange["id"], agent_server.CrossChatExchangeResponseRequest(
                    inbound_leg_id=inbound["id"], body="Final answer instead", request_response=False,
                    idempotency_key="budget-terminal-fallback"))
        self.assertEqual(terminal_rejected.exception.status_code, 403)
        after = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(after["used_legs"], 5)
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual(len(legs), 5)
        self.assertEqual(legs[-1]["body"], "Follow-up 5")

    async def test_exchange_expiry_is_committed_before_initial_and_response_410(self) -> None:
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_expired_unsent",
            requester_session_id="source",
            authorization_source_run_id="run_expired_unsent",
            responder_session_id="target",
            max_legs=6,
            expires_at="2000-01-01T00:00:00Z",
        )
        with self.assertRaises(HTTPException) as initial_error:
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_expired_unsent",
                source_session_id="source",
                source_run_id="run_expired_unsent",
                target_session_id="target",
                body="too late",
                idempotency_key="expired-unsent",
            )
        self.assertEqual(initial_error.exception.status_code, 410)
        unsent = await agent_server.CROSS_CHAT.get_exchange("exchange_expired_unsent")
        self.assertEqual(unsent["status"], "expired")

        exchange, inbound = await self.create_exchange("exchange_expired_response")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_expired_response",
        )
        with patch.object(agent_server, "now_iso", return_value="2100-01-01T00:00:00Z"):
            with self.assertRaises(HTTPException) as response_error:
                await agent_server.CROSS_CHAT.commit_exchange_response(
                    exchange_id=exchange["id"],
                    inbound_leg_id=inbound["id"],
                    source_session_id="target",
                    source_run_id="run_expired_response",
                    body="too late too",
                    request_response=False,
                    idempotency_key="expired-response",
                    automatic=False,
                )
        self.assertEqual(response_error.exception.status_code, 410)
        active = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(active["status"], "expired")
        pending_exchanges, _pending_legs = await agent_server.CROSS_CHAT.pending_exchange_lifecycle()
        self.assertEqual(
            {item["id"] for item in pending_exchanges},
            {"exchange_expired_unsent", "exchange_expired_response"},
        )

    async def test_attached_live_exchange_is_not_time_expired(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_live_past_nominal_expiry",
            "run_live_past_nominal_expiry_source",
        )
        inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_live_past_nominal_expiry_target",
        )
        self.assertIsNotNone(inbound)

        with patch.object(
            agent_server,
            "now_iso",
            return_value="2100-01-01T00:00:00Z",
        ):
            expired = await agent_server.CROSS_CHAT.expirable_exchanges(
                "2100-01-01T00:00:00Z"
            )
            exchange, response, created = (
                await agent_server.CROSS_CHAT.commit_exchange_response(
                    exchange_id=exchange["id"],
                    inbound_leg_id=inbound["id"],
                    source_session_id="target",
                    source_run_id="run_live_past_nominal_expiry_target",
                    body="The live owner is still attached",
                    request_response=False,
                    idempotency_key="live-answer-after-nominal-expiry",
                    automatic=False,
                )
            )

        self.assertNotIn(exchange["id"], {item["id"] for item in expired})
        self.assertTrue(created)
        self.assertEqual(response["kind"], "reply")
        self.assertEqual(exchange["status"], "active")
        self.assertTrue(bool(exchange["live_response_lease"]))
        self.assertFalse(waiter["future"].done())

    async def test_explicit_stop_cancels_queued_exchange_before_capability_revoke(
        self,
    ) -> None:
        source_run_id = "run_stopped_before_target_promotion"
        exchange, leg = await self.create_exchange(
            "exchange_stopped_before_target_promotion",
            source_run_id=source_run_id,
        )
        leg = await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"],
            expected={"registered"},
            status="queued",
            queued_id="queued_stopped_before_target_promotion",
            queue_position=1,
        )
        self.assertIsNotNone(leg)
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_stopped_before_target_promotion",
            "purpose": "cross_chat_handoff_delivery",
            "source_session_id": "source",
            "target_session_id": "target",
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": leg["id"],
        }])
        agent_server.BUSY_SESSIONS.add("source")
        agent_server.CURRENT_TURNS["source"] = {"run_id": source_run_id}
        revoke_observed_after_cancel = False

        async def observe_revoke(run_id: str) -> None:
            nonlocal revoke_observed_after_cancel
            self.assertEqual(run_id, source_run_id)
            self.assertNotIn("target", agent_server.QUEUED_TURNS)
            durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
            self.assertEqual(durable["status"], "cancelled")
            revoke_observed_after_cancel = True

        with (
            patch.object(agent_server, "ACTIVE", {}),
            patch.object(agent_server, "STOP_REQUESTS", set()),
            patch.object(agent_server, "STOPPED_RUNS", set()),
            patch.object(agent_server, "STOP_CONFIRM_TIMEOUT_SECONDS", 0),
            patch.object(agent_server, "append_durable_event", AsyncMock()),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_terminal_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_terminal_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "revoke_cross_chat_capability",
                AsyncMock(side_effect=observe_revoke),
            ),
            patch.object(
                agent_server,
                "cancel_codex_interactions",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "cancel_claude_interactions",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "release_turn_slot",
                AsyncMock(return_value=True),
            ),
        ):
            stopped = await agent_server.stop_turn(
                "source",
                emit_event=False,
                schedule_queue=False,
                cascade_codex_subagents=False,
                cascade_claude_subagents=False,
                hard_terminalize_on_timeout=False,
            )

        self.assertTrue(stopped["stopped"])
        self.assertTrue(revoke_observed_after_cancel)
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertEqual(durable_leg["status"], "cancelled")
        self.assertEqual(durable_leg["error_code"], "cancelled_by_user")

    async def test_stop_exchange_cleanup_is_noop_before_ledger_initialization(
        self,
    ) -> None:
        uninitialized = agent_server.CrossChatStore(
            self.root / "uninitialized-cross-chat.sqlite3"
        )
        with patch.object(agent_server, "CROSS_CHAT", uninitialized):
            cancelled = (
                await agent_server.cancel_cross_chat_exchanges_for_stopped_source_run(
                    "run_without_lifespan"
                )
            )

        self.assertEqual(cancelled, 0)
        self.assertFalse(uninitialized.path.exists())

    async def test_stop_exchange_cleanup_propagates_initialized_ledger_failure(
        self,
    ) -> None:
        query = AsyncMock(
            side_effect=agent_server.sqlite3.OperationalError("ledger unavailable")
        )
        with patch.object(
            agent_server.CROSS_CHAT,
            "exchanges_for_authorization_run",
            query,
        ):
            with self.assertRaisesRegex(
                agent_server.sqlite3.OperationalError,
                "ledger unavailable",
            ):
                await agent_server.cancel_cross_chat_exchanges_for_stopped_source_run(
                    "run_initialized_ledger_failure"
                )

        query.assert_awaited_once_with("run_initialized_ledger_failure")

    async def test_stopped_terminal_repairs_exchange_cancel_if_stop_path_was_lost(
        self,
    ) -> None:
        source_run_id = "run_stopped_terminal_repair"
        exchange, leg = await self.create_exchange(
            "exchange_stopped_terminal_repair",
            source_run_id=source_run_id,
        )
        with (
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_terminal_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_terminal_lifecycle",
                AsyncMock(),
            ),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "type": "turn_stopped",
                "run_id": source_run_id,
                "result_text": "",
                "exit_code": 130,
            })

        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertEqual(durable["status"], "cancelled")
        self.assertEqual(durable["error_code"], "cancelled_by_user")
        self.assertEqual(durable_leg["status"], "cancelled")

    async def test_exchange_late_terminal_after_user_cancel_never_wakes_or_replies(self) -> None:
        exchange, leg = await self.create_exchange("exchange_cancel_late_failure")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"], expected={"registered"}, status="running", target_run_id="run_late_failure"
        )
        await agent_server.CROSS_CHAT.cancel_exchange(exchange["id"])
        wake = AsyncMock()
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "maybe_deliver_cross_chat_exchange_failure_status", wake),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_late_failure",
                "purpose": "cross_chat_handoff_delivery",
                "exchange_id": exchange["id"],
                "exchange_leg_id": leg["id"],
                "result_text": "",
                "exit_code": 1,
                "stopped": True,
            })
        wake.assert_not_awaited()
        cancelled = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(len(await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])), 1)

        exchange, leg = await self.create_exchange("exchange_cancel_late_success")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"], expected={"registered"}, status="running", target_run_id="run_late_success"
        )
        await agent_server.CROSS_CHAT.cancel_exchange(exchange["id"])
        terminal_lifecycle = AsyncMock()
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", terminal_lifecycle),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_late_success",
                "purpose": "cross_chat_handoff_delivery",
                "exchange_id": exchange["id"],
                "exchange_leg_id": leg["id"],
                "result_text": "Late answer",
                "exit_code": 0,
                "stopped": False,
            })
        self.assertIn("cancelled", terminal_lifecycle.await_args.args[1].lower())
        self.assertEqual(len(await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])), 1)

    async def test_participant_cleanup_cannot_override_completed_exchange(self) -> None:
        exchange, parent = await self.create_exchange("exchange_delete_loses")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            parent["id"], expected={"registered"}, status="running", target_run_id="run_delete_parent"
        )
        exchange, answer, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=parent["id"],
            source_session_id="target",
            source_run_id="run_delete_parent",
            body="Done",
            request_response=False,
            idempotency_key="delete-answer",
            automatic=False,
        )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            answer["id"], expected={"registered"}, status="running", target_run_id="run_delete_answer"
        )
        await agent_server.CROSS_CHAT.finish_exchange_leg(answer["id"], status="delivered")
        stale = {**exchange, "status": "active", "active_leg_id": answer["id"]}
        schedule = AsyncMock()
        with (
            patch.object(agent_server.CROSS_CHAT, "nonterminal_exchanges_for_session", AsyncMock(return_value=[stale])),
            patch.object(agent_server, "schedule_cross_chat_exchange_failure_status_after_unlock", schedule),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            count = await agent_server.terminalize_cross_chat_exchanges_for_session(
                "source", archived=False
            )
        self.assertEqual(count, 0)
        schedule.assert_not_called()
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "completed")
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            expired = await agent_server.fail_cross_chat_exchange(
                exchange["id"],
                leg_id=answer["id"],
                leg_status="expired",
                error_code="expired",
                error="stale expiry snapshot",
            )
        self.assertIsNone(expired)
        self.assertEqual(
            (await agent_server.CROSS_CHAT.get_exchange(exchange["id"]))["status"],
            "completed",
        )

    async def test_reconcile_removes_cancelled_hidden_queue_owner_only(self) -> None:
        exchange, leg = await self.create_exchange("exchange_cancel_queue_crash")
        leg = await agent_server.CROSS_CHAT.update_exchange_leg(
            leg["id"],
            expected={"registered"},
            status="queued",
            queued_id="queued_hidden_exchange",
            queue_position=2,
        )
        await agent_server.CROSS_CHAT.cancel_exchange(exchange["id"])
        paused = {"queued_id": "queued_user", "prompt": "later", "_paused_after_stop": True}
        hidden = {
            "queued_id": "queued_hidden_exchange",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": leg["id"],
            "source_session_id": "source",
            "target_session_id": "target",
        }
        agent_server.QUEUED_TURNS = {"target": deque([paused, hidden])}
        with (
            patch.object(agent_server, "append_durable_event", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        self.assertEqual(list(agent_server.QUEUED_TURNS["target"]), [paused])
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertIsNone(durable_leg["queued_id"])

    async def test_reconcile_retires_legacy_request_for_durably_unqueued_source(self) -> None:
        exchange = await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_unqueue_crash",
            requester_session_id="source",
            authorization_source_run_id="queued_source_exchange",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        event_file = self.root / "source-unqueue.jsonl"
        event_file.write_text(json.dumps({
            "type": "turn_unqueued",
            "queued_id": "queued_source_exchange",
            "cross_chat_exchange_ids": [exchange["id"]],
        }) + "\n")
        with (
            patch.object(agent_server, "events_path", return_value=event_file),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "cancelled")
        self.assertEqual(durable["error_code"], "legacy_route_disabled")

    async def test_reconcile_retires_exchange_removed_by_durable_queue_edit(self) -> None:
        old_exchange = await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_edit_old",
            requester_session_id="source",
            authorization_source_run_id="queued_edit_source",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        new_exchange = await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_edit_new",
            requester_session_id="source",
            authorization_source_run_id="queued_edit_source",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        agent_server.QUEUED_TURNS = {"source": deque([{
            "queued_id": "queued_edit_source",
            "cross_chat_exchange_ids": [new_exchange["id"]],
            "_durable": True,
        }])}
        with patch.object(
            agent_server,
            "append_cross_chat_exchange_terminal_lifecycle",
            AsyncMock(),
        ):
            await agent_server.reconcile_cross_chat_exchanges()
        old_exchange = await agent_server.CROSS_CHAT.get_exchange(old_exchange["id"])
        new_exchange = await agent_server.CROSS_CHAT.get_exchange(new_exchange["id"])
        self.assertEqual(old_exchange["status"], "cancelled")
        self.assertEqual(old_exchange["error_code"], "legacy_route_disabled")
        self.assertEqual(new_exchange["status"], "cancelled")
        self.assertEqual(new_exchange["error_code"], "legacy_route_disabled")
        self.assertEqual(agent_server.QUEUED_TURNS["source"][0]["queued_id"], "queued_edit_source")

    async def test_failure_status_wake_waits_for_target_lifecycle_unlock(self) -> None:
        exchange, leg = await self.create_exchange("exchange_deferred_status_lock")
        lock = agent_server.session_lifecycle_lock("target")
        await lock.acquire()
        deliver = AsyncMock()
        try:
            with patch.object(
                agent_server,
                "maybe_deliver_cross_chat_exchange_failure_status",
                deliver,
            ):
                agent_server.schedule_cross_chat_exchange_failure_status_after_unlock(
                    exchange,
                    failed_session_id="target",
                    failed_leg=leg,
                )
                await asyncio.sleep(0)
                deliver.assert_not_awaited()
                lock.release()
                for _ in range(20):
                    if deliver.await_count:
                        break
                    await asyncio.sleep(0)
                deliver.assert_awaited_once()
        finally:
            if lock.locked():
                lock.release()

    def test_exchange_capability_v14_mailbox_only_contract_is_exact(self) -> None:
        with (
            patch.object(agent_server, "CODEX_TRANSPORT", agent_server.CODEX_TRANSPORT_APP_SERVER),
            patch.object(agent_server, "CLAUDE_TRANSPORT", agent_server.CLAUDE_TRANSPORT_AGENT_SDK),
        ):
            capability = agent_server.cross_chat_handoffs_capability()
        self.assertTrue(capability["available"])
        self.assertEqual(capability["version"], 14)
        self.assertEqual(
            capability["actions"],
            [
                "route",
                "instruction",
                "request_reply",
            ],
        )
        self.assertEqual(capability["default_action"], "route")
        self.assertFalse(capability["features"]["direct_message_mentions"])
        self.assertTrue(capability["features"]["route_mentions"])
        self.assertTrue(capability["features"]["route_hint_mentions"])
        self.assertTrue(capability["features"]["durable_route_grants"])
        self.assertTrue(capability["features"]["agent_cross_chat_routes"])
        self.assertFalse(
            capability["features"]["configured_route_async_request_reply"]
        )
        self.assertFalse(
            capability["features"]["configured_route_live_request_reply"]
        )
        self.assertEqual(
            capability["features"]["configured_route_request_reply_default"],
            "mailbox",
        )
        self.assertFalse(
            capability["features"]["live_same_server_request_reply"]
        )
        self.assertFalse(
            capability["features"]["live_wait_timeout_async_fallback"]
        )
        self.assertFalse(
            capability["features"]["live_wait_restart_async_fallback"]
        )
        self.assertTrue(
            capability["features"]["exact_queued_delivery_skip"]
        )
        self.assertFalse(capability["features"]["secure_peer_fifo_barriers"])
        self.assertFalse(capability["features"]["secure_peer_agent_relay"])
        self.assertEqual(
            capability["features"]["cross_server_delivery"],
            "team_network_inbox_only",
        )
        self.assertFalse(
            capability["features"]["agent_ambient_local_handoffs"]
        )
        self.assertNotIn("instruction_reply_once", capability["features"])
        self.assertNotIn("instruction_reply_policy", capability["features"])
        self.assertEqual(
            capability["ambient_local_handoffs"]["policy"],
            "default_deny",
        )
        self.assertEqual(
            capability["ambient_local_handoffs"]["scope"],
            "explicit_source_grants",
        )
        self.assertFalse(
            capability["ambient_local_handoffs"]["setup_required"]
        )
        self.assertFalse(capability["ambient_local_handoffs"]["enabled"])
        self.assertEqual(
            capability["agent_routes"]["client_capability"],
            agent_server.AGENT_CROSS_CHAT_ROUTES_CLIENT_CAPABILITY,
        )
        self.assertIsNone(capability["agent_routes"]["max_routes_per_chat"])
        self.assertFalse(capability["agent_routes"]["transcript_access"])
        self.assertEqual(capability["agent_routes"]["policy"], "default_deny")
        self.assertTrue(capability["agent_routes"]["same_server_only"])
        self.assertTrue(capability["agent_routes"]["durable"])
        self.assertTrue(capability["agent_routes"]["directional"])
        self.assertTrue(
            capability["agent_routes"]["revoke_requires_revision"]
        )
        self.assertFalse(capability["agent_routes"]["instruction_reply_once"])
        self.assertEqual(
            capability["agent_routes"]["instruction_reply_policy"],
            "explicit_mailbox_message",
        )
        self.assertEqual(
            capability["live_request_reply"],
            {"available": False, "followup_supported": False},
        )

    def test_live_response_timeout_wire_value_is_only_a_clamped_heartbeat(self) -> None:
        with patch.object(
            agent_server,
            "PROVIDER_CROSS_CHAT_LIVE_HEARTBEAT_SECONDS",
            60,
        ):
            self.assertEqual(
                agent_server.cross_chat_live_heartbeat_seconds(None),
                60.0,
            )
            self.assertEqual(
                agent_server.cross_chat_live_heartbeat_seconds(20),
                20.0,
            )
            self.assertEqual(
                agent_server.cross_chat_live_heartbeat_seconds(1),
                1.0,
            )
            self.assertEqual(
                agent_server.cross_chat_live_heartbeat_seconds(3600),
                60.0,
            )

    async def test_only_pending_exact_live_owner_pauses_provider_watchdog(self) -> None:
        exchange, _inbound, waiter = await self.create_live_waiter(
            "exchange_live_watchdog_owner",
            "run_live_watchdog_owner",
        )

        self.assertTrue(
            agent_server.provider_run_owns_pending_cross_chat_live_wait(
                "source",
                "run_live_watchdog_owner",
            )
        )
        self.assertFalse(
            agent_server.provider_run_owns_pending_cross_chat_live_wait(
                "source",
                "run_different",
            )
        )
        self.assertFalse(
            agent_server.provider_run_owns_pending_cross_chat_live_wait(
                "target",
                "run_live_watchdog_owner",
            )
        )

        waiter["last_observer_at_monotonic"] = (
            time.monotonic()
            - agent_server.PROVIDER_CROSS_CHAT_LIVE_OBSERVER_GRACE_SECONDS
            - 1
        )
        self.assertFalse(
            agent_server.provider_run_owns_pending_cross_chat_live_wait(
                "source",
                "run_live_watchdog_owner",
            )
        )
        waiter["observers"].add("observer_attached")
        self.assertTrue(
            agent_server.provider_run_owns_pending_cross_chat_live_wait(
                "source",
                "run_live_watchdog_owner",
            )
        )
        waiter["observers"].clear()

        waiter["future"].set_result({
            "ok": True,
            "exchange_id": exchange["id"],
            "inbound_leg_id": "leg_answer",
            "body": "done",
            "request_response": False,
        })
        self.assertFalse(
            agent_server.provider_run_owns_pending_cross_chat_live_wait(
                "source",
                "run_live_watchdog_owner",
            )
        )

    async def test_authenticated_live_get_replays_refresh_watchdog_liveness(self) -> None:
        token = await self.issue_live_waiter_owner(
            "source",
            "run_live_heartbeat_owner",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_live_heartbeat_owner",
            requester_session_id="source",
            authorization_source_run_id="run_live_heartbeat_owner",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, inbound, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_live_heartbeat_owner",
                source_session_id="source",
                source_run_id="run_live_heartbeat_owner",
                target_session_id="target",
                body="Wait across several heartbeat responses",
                idempotency_key="live-heartbeat-owner",
                live_response_lease=True,
            )
        )
        async with agent_server.cross_chat_live_lease_lock(exchange["id"]):
            waiter = await agent_server.register_cross_chat_live_waiter_locked(
                exchange,
                inbound,
                owner_session_id="source",
                owner_run_id="run_live_heartbeat_owner",
                capability_token=token,
            )

        stale = (
            time.monotonic()
            - agent_server.PROVIDER_CROSS_CHAT_LIVE_OBSERVER_GRACE_SECONDS
            - 1
        )
        for _heartbeat in range(3):
            waiter["last_observer_at_monotonic"] = stale
            self.assertFalse(
                agent_server.provider_run_owns_pending_cross_chat_live_wait(
                    "source",
                    "run_live_heartbeat_owner",
                )
            )
            replay_exchange, replay_waiter = (
                await agent_server.authorized_cross_chat_live_waiter(
                    token,
                    exchange_id=exchange["id"],
                    inbound_leg_id=inbound["id"],
                    lease_id=waiter["lease_id"],
                )
            )
            self.assertEqual(replay_exchange["id"], exchange["id"])
            self.assertIs(replay_waiter, waiter)
            self.assertTrue(
                agent_server.provider_run_owns_pending_cross_chat_live_wait(
                    "source",
                    "run_live_heartbeat_owner",
                )
            )

    def test_secure_peer_request_authority_requires_async_response_mode(self) -> None:
        reference = agent_server.ChatReference(
            session_id="22222222-2222-4222-8222-222222222222",
            display_title_snapshot="Remote",
            source_text_start=0,
            source_text_end=7,
            action="request_reply",
            target_kind="secure_peer",
            target_server_identity="peer_" + "a" * 64,
            target_connection_id="11111111-1111-4111-8111-111111111111",
            target_route_id="22222222-2222-4222-8222-222222222222",
            target_route_revision="rev_" + "d" * 32,
        )
        authority_copy = agent_server.cross_chat_provider_authority_block(
            [reference],
            self.root / "secure-request-authority.json",
            "source",
            {"secure_peer_request_reply"},
        )
        self.assertIn("secure peer server=", authority_copy)
        self.assertIn("use --async-response", authority_copy)
        self.assertIn(
            "For a secure-peer action=request_reply, use `ask --target OPAQUE_HANDLE --message TEXT --async-response`",
            authority_copy,
        )

    async def test_local_request_reply_reference_never_mints_exchange_generations(self) -> None:
        reference = agent_server.ChatReference(session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=7, action="request_reply")
        with patch.object(agent_server.CROSS_CHAT, "create_exchange_obligation", AsyncMock(side_effect=AssertionError("retired exchange registered"))) as create:
            for run in ("run_one", "run_two"):
                self.assertEqual(await agent_server.register_request_reply_exchanges("source", run, [reference]), [])
        create.assert_not_awaited()
        self.assertEqual(await agent_server.CROSS_CHAT.recoverable_exchanges(), [])
        self.assertFalse(agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS)

    async def test_exchange_explicit_and_automatic_response_cas_both_orderings(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_explicit_wins")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_explicit_wins"
        )
        _exchange, explicit, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=inbound["id"],
            source_session_id="target",
            source_run_id="run_explicit_wins",
            body="Explicit answer",
            request_response=False,
            idempotency_key="explicit-wins-key",
            automatic=False,
        )
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_explicit_wins",
                "exchange_id": exchange["id"],
                "exchange_leg_id": inbound["id"],
                "result_text": "Automatic should lose",
                "exit_code": 0,
                "stopped": False,
            })
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual([leg["id"] for leg in legs], [inbound["id"], explicit["id"]])

        exchange, inbound = await self.create_exchange("exchange_auto_wins")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_auto_wins"
        )
        with (
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", AsyncMock()),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()),
        ):
            await agent_server.finalize_cross_chat_exchange_run({
                "run_id": "run_auto_wins",
                "exchange_id": exchange["id"],
                "exchange_leg_id": inbound["id"],
                "result_text": "Automatic answer",
                "exit_code": 0,
                "stopped": False,
            })
        with self.assertRaises(HTTPException) as raised:
            await agent_server.CROSS_CHAT.commit_exchange_response(
                exchange_id=exchange["id"],
                inbound_leg_id=inbound["id"],
                source_session_id="target",
                source_run_id="run_auto_wins",
                body="Late explicit",
                request_response=False,
                idempotency_key="late-explicit-key",
                automatic=False,
            )
        self.assertEqual(raised.exception.detail, "response_already_committed")

    async def test_local_request_reply_pipeline_does_not_create_exchange_or_return_turn(self) -> None:
        reference = agent_server.ChatReference(session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=7, action="request_reply")
        with patch.object(agent_server.CROSS_CHAT, "create_exchange_obligation", AsyncMock(side_effect=AssertionError("retired exchange registered"))) as create:
            for run in ("run_one", "run_two"):
                self.assertEqual(await agent_server.register_request_reply_exchanges("source", run, [reference]), [])
        create.assert_not_awaited()
        self.assertEqual(await agent_server.CROSS_CHAT.recoverable_exchanges(), [])
        self.assertFalse(agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS)

    async def test_local_request_reply_has_no_unsent_placeholder_to_recover(self) -> None:
        reference = agent_server.ChatReference(session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=7, action="request_reply")
        with patch.object(agent_server.CROSS_CHAT, "create_exchange_obligation", AsyncMock(side_effect=AssertionError("retired exchange registered"))) as create:
            for run in ("run_one", "run_two"):
                self.assertEqual(await agent_server.register_request_reply_exchanges("source", run, [reference]), [])
        create.assert_not_awaited()
        self.assertEqual(await agent_server.CROSS_CHAT.recoverable_exchanges(), [])
        self.assertFalse(agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS)

    async def test_exchange_explicit_response_vs_failed_terminal_both_orderings(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_explicit_before_failure")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_explicit_failure"
        )
        exchange, child, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=inbound["id"],
            source_session_id="target",
            source_run_id="run_explicit_failure",
            body="Continue anyway",
            request_response=True,
            idempotency_key="explicit-before-failure",
            automatic=False,
        )
        exchange, inbound = await agent_server.CROSS_CHAT.finish_exchange_leg(
            inbound["id"],
            status="failed",
            error_code="target_stopped",
            error="provider stopped",
            preserve_committed_response=True,
        )
        self.assertEqual(inbound["status"], "delivered")
        self.assertEqual(exchange["status"], "active")
        self.assertEqual(exchange["active_leg_id"], child["id"])

        exchange, inbound = await self.create_exchange("exchange_failure_before_explicit")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_failure_first"
        )
        await agent_server.CROSS_CHAT.finish_exchange_leg(
            inbound["id"],
            status="failed",
            error_code="target_stopped",
            error="provider stopped",
            preserve_committed_response=True,
        )
        with self.assertRaises(HTTPException) as raised:
            await agent_server.CROSS_CHAT.commit_exchange_response(
                exchange_id=exchange["id"],
                inbound_leg_id=inbound["id"],
                source_session_id="target",
                source_run_id="run_failure_first",
                body="Too late",
                request_response=True,
                idempotency_key="explicit-after-failure",
                automatic=False,
            )
        self.assertEqual(raised.exception.detail, "response_already_committed")

    async def test_terminal_reply_can_receive_explicit_follow_up(self) -> None:
        exchange, inbound = await self.create_exchange("exchange_follow_terminal_reply")
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"], expected={"registered"}, status="running", target_run_id="run_reply"
        )
        exchange, reply, _created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=inbound["id"],
            source_session_id="target",
            source_run_id="run_reply",
            body="Initial answer",
            request_response=False,
            idempotency_key="initial-answer-key",
            automatic=False,
        )
        self.assertFalse(bool(reply["expects_reply"]))
        await agent_server.CROSS_CHAT.update_exchange_leg(
            reply["id"], expected={"registered"}, status="running", target_run_id="run_follow"
        )
        exchange, follow_up, created = await agent_server.CROSS_CHAT.commit_exchange_response(
            exchange_id=exchange["id"],
            inbound_leg_id=reply["id"],
            source_session_id="source",
            source_run_id="run_follow",
            body="Please clarify one thing",
            request_response=True,
            idempotency_key="follow-up-key",
            automatic=False,
        )
        self.assertTrue(created)
        self.assertTrue(bool(follow_up["expects_reply"]))
        self.assertEqual(follow_up["target_session_id"], "target")

    async def test_status_leg_is_non_budget_and_has_no_response_semantics(self) -> None:
        exchange, leg = await self.create_exchange("exchange_status_budget")
        before = int(exchange["used_legs"])
        status, created = await agent_server.CROSS_CHAT.create_exchange_status_leg(
            exchange_id=exchange["id"],
            source_session_id="target",
            target_session_id="source",
            body="Target failed",
            error_code="target_failed",
        )
        self.assertTrue(created)
        current = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(current["used_legs"], before)
        self.assertEqual(status["ordinal"], 0)
        self.assertFalse(bool(status["expects_reply"]))
        self.assertEqual(status["response_state"], "closed")

    async def test_legacy_completion_never_copies_final_body_at_either_size_limit(self) -> None:
        for size in (agent_server.CROSS_CHAT_EXCHANGE_BODY_MAX_CHARS, agent_server.CROSS_CHAT_EXCHANGE_BODY_MAX_CHARS + 1):
            with self.subTest(size=size):
                await self.assert_no_automatic_exchange_reply(size=size)

    async def test_archived_target_discards_queued_exchange_status_leg(self) -> None:
        agent_server.STORE.sessions["target"]["archived"] = True
        item = {
            "queued_id": "queued_status_archived",
            "prompt": "status",
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_exchange_id": "exchange_status_archived",
            "cross_chat_exchange_leg_id": "leg_status_archived",
            "cross_chat_exchange_status": True,
            "source_session_id": "source",
            "target_session_id": "target",
            "_durable": True,
            "_paused_after_stop": False,
        }
        agent_server.QUEUED_TURNS = {"target": deque([item])}
        discard = AsyncMock()
        with (
            patch.object(
                agent_server,
                "_start_turn_locked",
                AsyncMock(side_effect=HTTPException(status_code=409, detail="chat is archived")),
            ),
            patch.object(agent_server, "terminally_discard_queued_turn", discard),
        ):
            await agent_server.start_next_queued_turn("target")
        discard.assert_awaited_once_with("target", item, "chat is archived")

    async def test_retired_auto_return_does_not_contact_archived_requester(self) -> None:
        await self.assert_no_automatic_exchange_reply(archived=True)

    async def test_local_request_reply_references_do_not_create_live_waiters(self) -> None:
        reference = agent_server.ChatReference(session_id="target", display_title_snapshot="Target",
            source_text_start=0, source_text_end=7, action="request_reply")
        with patch.object(agent_server.CROSS_CHAT, "create_exchange_obligation", AsyncMock(side_effect=AssertionError("retired exchange registered"))) as create:
            for run in ("run_one", "run_two"):
                self.assertEqual(await agent_server.register_request_reply_exchanges("source", run, [reference]), [])
        create.assert_not_awaited()
        self.assertEqual(await agent_server.CROSS_CHAT.recoverable_exchanges(), [])
        self.assertFalse(agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS)

    async def test_retired_live_answer_completes_waiter_with_terminal_error(self) -> None:
        await self.assert_no_automatic_exchange_reply(live=True)

    async def test_completed_live_answer_is_not_blocked_by_disconnect_cleanup(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_live_cleanup_answer", "run_live_cleanup_answer",
        )
        started = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        watcher_finished = asyncio.Event()

        class CancellationResistantRequest:
            async def receive(self) -> dict:
                started.set()
                try:
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        # Model a disconnect transport swallowing cancellation.
                        # A saved reply must not depend on its acknowledgement.
                        cancelled.set()
                        await release.wait()
                    return {"type": "http.disconnect"}
                finally:
                    watcher_finished.set()

            async def is_disconnected(self) -> bool:
                await self.receive()
                return True

        live_get = asyncio.create_task(agent_server.await_cross_chat_live_waiter(
            exchange, waiter, timeout_seconds=10,
            request=CancellationResistantRequest(),
        ))
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            await self.deliver_terminal_live_answer(
                exchange, inbound, waiter, body="Already saved; return it now",
            )
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            done, _pending = await asyncio.wait({live_get}, timeout=0.5)
            self.assertIn(live_get, done, "saved answer hung in watcher cleanup")
            self.assertEqual(live_get.result()["body"], "Already saved; return it now")
            self.assertEqual(waiter["observers"], set())
            self.assertIs(
                agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS[
                    (exchange["id"], inbound["id"])
                ], waiter,
            )
            replay = await agent_server.await_cross_chat_live_waiter(
                exchange, waiter, timeout_seconds=1,
            )
            self.assertEqual(replay, live_get.result())
        finally:
            release.set()
            await asyncio.gather(live_get, return_exceptions=True)
            await asyncio.wait_for(watcher_finished.wait(), timeout=1)

    async def test_live_cleanup_preserves_heartbeat_and_caller_cancellation(self) -> None:
        for outcome in ("heartbeat", "cancel", "cancel_after_answer"):
            with self.subTest(outcome=outcome):
                exchange, inbound, waiter = await self.create_live_waiter(
                    f"exchange_cleanup_{outcome}", f"run_cleanup_{outcome}",
                )
                started = asyncio.Event()
                cancellation_seen = asyncio.Event()
                release = asyncio.Event()
                watcher_finished = asyncio.Event()

                class CancellationResistantRequest:
                    async def receive(self) -> dict:
                        started.set()
                        try:
                            try:
                                await release.wait()
                            except asyncio.CancelledError:
                                cancellation_seen.set()
                                await release.wait()
                            return {"type": "http.disconnect"}
                        finally:
                            watcher_finished.set()

                with patch.object(
                    agent_server, "cross_chat_live_heartbeat_seconds",
                    return_value=0.01 if outcome == "heartbeat" else 10,
                ):
                    live_get = asyncio.create_task(
                        agent_server.await_cross_chat_live_waiter(
                            exchange, waiter, timeout_seconds=10,
                            request=CancellationResistantRequest(),
                        )
                    )
                    try:
                        await asyncio.wait_for(started.wait(), timeout=1)
                        if outcome == "cancel_after_answer":
                            await self.deliver_terminal_live_answer(
                                exchange, inbound, waiter, body="Saved before disconnect",
                            )
                        elif outcome == "cancel":
                            live_get.cancel()
                        await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
                        if outcome == "cancel_after_answer":
                            live_get.cancel()
                        done, _pending = await asyncio.wait({live_get}, timeout=0.5)
                        self.assertIn(live_get, done, "watcher cleanup did not settle")
                        if outcome == "heartbeat":
                            self.assertTrue(live_get.result()["pending"])
                        else:
                            self.assertTrue(live_get.cancelled())
                        self.assertEqual(waiter["observers"], set())
                        self.assertFalse(waiter["future"].cancelled())
                        self.assertIn(
                            (exchange["id"], inbound["id"]),
                            agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS,
                        )
                    finally:
                        release.set()
                        await asyncio.gather(live_get, return_exceptions=True)
                        await asyncio.wait_for(watcher_finished.wait(), timeout=1)

                if not waiter["future"].done():
                    await self.deliver_terminal_live_answer(
                        exchange, inbound, waiter, body="Saved after disconnect",
                    )
                # All outcomes preserve the shared answer for the exact retry.
                replay = await agent_server.await_cross_chat_live_waiter(
                    exchange, waiter, timeout_seconds=1,
                )
                self.assertIn("Saved", replay["body"])

    async def test_live_get_uses_asgi_disconnect_event_and_completed_replay_skips_it(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_asgi_disconnect", "run_asgi_disconnect",
        )
        messages = asyncio.Queue()
        messages.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        listening = asyncio.Event()
        receives = 0

        async def receive() -> dict:
            nonlocal receives
            receives += 1
            if receives == 2:
                listening.set()
            return await messages.get()

        request = Request({"type": "http", "method": "GET"}, receive=receive)
        with patch.object(request, "is_disconnected", AsyncMock()) as poll:
            live_get = asyncio.create_task(agent_server.await_cross_chat_live_waiter(
                exchange, waiter, timeout_seconds=10, request=request,
            ))
            try:
                await asyncio.wait_for(listening.wait(), timeout=1)
                await self.deliver_terminal_live_answer(
                    exchange, inbound, waiter, body="ASGI reply",
                )
                # If both events are ready, the completed answer wins.
                messages.put_nowait({"type": "http.disconnect"})
                result = await asyncio.wait_for(live_get, timeout=1)
                self.assertEqual(result["body"], "ASGI reply")
                replay = await agent_server.await_cross_chat_live_waiter(
                    exchange, waiter, timeout_seconds=1, request=request,
                )
                self.assertEqual(replay, result)
                self.assertEqual(receives, 2)
                poll.assert_not_awaited()
                self.assertEqual(waiter["observers"], set())
            finally:
                if not live_get.done():
                    live_get.cancel()
                await asyncio.gather(live_get, return_exceptions=True)

    async def test_live_receive_failure_is_not_a_pending_heartbeat(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_receive_failure", "run_receive_failure",
        )
        request = Request(
            {"type": "http", "method": "GET"},
            receive=AsyncMock(side_effect=ConnectionError("transport unavailable")),
        )
        with self.assertRaises(HTTPException) as raised:
            await agent_server.await_cross_chat_live_waiter(
                exchange, waiter, timeout_seconds=1, request=request,
            )
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(waiter["observers"], set())
        self.assertFalse(waiter["future"].done())
        await self.deliver_terminal_live_answer(
            exchange, inbound, waiter, body="Recovered from transport error",
        )
        result = await agent_server.await_cross_chat_live_waiter(
            exchange, waiter, timeout_seconds=1, request=request,
        )
        self.assertEqual(result["body"], "Recovered from transport error")

    async def test_disconnected_get_does_not_downgrade_attached_retry(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_live_get_retry",
            "run_live_get_retry_source",
        )
        disconnect_seen = asyncio.Event()

        class DisconnectedRequest:
            async def receive(self) -> dict:
                disconnect_seen.set()
                return {"type": "http.disconnect"}

        disconnected_get = asyncio.create_task(
            agent_server.await_cross_chat_live_waiter(
                exchange,
                waiter,
                timeout_seconds=10,
                request=DisconnectedRequest(),
            )
        )
        await asyncio.wait_for(disconnect_seen.wait(), timeout=1)
        disconnected_result = await asyncio.gather(
            disconnected_get,
            return_exceptions=True,
        )
        self.assertIs(
            agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS[
                (exchange["id"], inbound["id"])
            ],
            waiter,
        )
        self.assertFalse(waiter["future"].done())
        replayed_get = asyncio.create_task(
            agent_server.await_cross_chat_live_waiter(
                exchange,
                waiter,
                timeout_seconds=10,
            )
        )
        exchange, _outbound = await self.deliver_terminal_live_answer(
            exchange,
            inbound,
            waiter,
            body="Answer survived the GET retry",
        )
        replayed_result = await asyncio.wait_for(replayed_get, timeout=1)

        self.assertEqual(replayed_result["body"], "Answer survived the GET retry")
        self.assertIsInstance(disconnected_result[0], HTTPException)
        self.assertEqual(disconnected_result[0].status_code, 499)
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "completed")
        self.assertTrue(bool(durable["live_response_lease"]))

    async def test_one_heartbeat_keeps_other_live_get_attached(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_live_two_observers",
            "run_live_two_observers_source",
        )

        async def wait_for_observer_count(expected: int) -> None:
            for _attempt in range(1000):
                if len(waiter.get("observers") or set()) == expected:
                    return
                await asyncio.sleep(0.001)
            self.fail(f"live waiter did not reach {expected} observers")

        with patch.object(
            agent_server,
            "cross_chat_live_heartbeat_seconds",
            side_effect=lambda requested: 0.02 if requested == 1 else 10.0,
        ):
            long_get = asyncio.create_task(
                agent_server.await_cross_chat_live_waiter(
                    exchange,
                    waiter,
                    timeout_seconds=10,
                )
            )
            await wait_for_observer_count(1)
            short_get = asyncio.create_task(
                agent_server.await_cross_chat_live_waiter(
                    exchange,
                    waiter,
                    timeout_seconds=1,
                )
            )
            await wait_for_observer_count(2)
            short_result = await short_get
            self.assertTrue(short_result["pending"])
            self.assertEqual(short_result["inbound_leg_id"], inbound["id"])
            durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
            self.assertTrue(bool(durable["live_response_lease"]))
            self.assertIs(
                agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS[
                    (exchange["id"], inbound["id"])
                ],
                waiter,
            )
            exchange, _outbound = await self.deliver_terminal_live_answer(
                exchange,
                inbound,
                waiter,
                body="The remaining observer received this",
            )
            long_result = await asyncio.wait_for(long_get, timeout=1)

        self.assertEqual(
            long_result["body"],
            "The remaining observer received this",
        )
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "completed")

    async def test_retired_live_completion_cannot_downgrade_to_async_delivery(self) -> None:
        await self.assert_no_automatic_exchange_reply(live=True, archived=True)

    async def test_route_live_request_replays_after_async_downgrade(self) -> None:
        values = {
            "exchange_id": "exchange_route_live_replay",
            "leg_id": "leg_route_live_replay",
            "requester_session_id": "source",
            "authorization_source_run_id": "run_route_live_replay",
            "responder_session_id": "target",
            "body": "Route question",
            "idempotency_key": "route-live-replay-key",
            "max_legs": 6,
            "expires_at": "2099-01-01T00:00:00Z",
            "authorization_route_id": "route_" + "a" * 32,
            "initial_action": "request_reply",
            "source_user_instruction": "Ask Target",
            "live_response_lease": True,
        }
        exchange, leg, created = (
            await agent_server.CROSS_CHAT.create_route_exchange_request(**values)
        )
        self.assertTrue(created)
        downgraded, changed = await agent_server.CROSS_CHAT.downgrade_live_exchange(
            exchange["id"],
            active_leg_id=leg["id"],
            expected_instance_id=str(exchange["live_response_instance_id"]),
        )
        self.assertTrue(changed)
        self.assertFalse(bool(downgraded["live_response_lease"]))
        replay_exchange, replay_leg, replay_created = (
            await agent_server.CROSS_CHAT.create_route_exchange_request(**values)
        )
        self.assertFalse(replay_created)
        self.assertEqual(replay_leg["id"], leg["id"])
        self.assertTrue(bool(replay_exchange["live_response_requested"]))
        self.assertFalse(bool(replay_exchange["live_response_lease"]))
        with self.assertRaises(HTTPException) as changed_payload:
            await agent_server.CROSS_CHAT.create_route_exchange_request(
                **{**values, "body": "Changed route question"}
            )
        self.assertEqual(changed_payload.exception.status_code, 409)

    async def test_exact_live_handoff_post_replay_returns_deferred_receipt(self) -> None:
        exchange = {
            "id": "exchange_deferred_post_replay",
            "status": "active",
            "authorization_source_run_id": "run_deferred_post_replay",
            "live_response_requested": 1,
            "live_response_lease": 0,
        }
        leg = {
            "id": "leg_deferred_post_replay",
            "status": "running",
            "source_session_id": "source",
            "source_run_id": "run_deferred_post_replay",
        }
        request = Request({
            "type": "http",
            "headers": [(b"x-agentsdock-provider-capability", b"capability")],
            "client": ("127.0.0.1", 1234),
        })
        with (
            patch.object(
                agent_server,
                "create_authorized_cross_chat_instruction",
                AsyncMock(return_value=({"exchange": exchange, "leg": leg}, False)),
            ),
            patch.object(agent_server, "append_cross_chat_exchange_registered", AsyncMock()),
            patch.object(agent_server, "append_cross_chat_exchange_leg_lifecycle", AsyncMock()),
            patch.object(agent_server, "submit_cross_chat_exchange_leg", AsyncMock()) as submit,
        ):
            receipt = await agent_server.submit_authorized_cross_chat_handoff(
                agent_server.CrossChatHandoffRequest(
                    target_session_id="grant_" + "a" * 64,
                    action="request_reply",
                    body="Replay the exact question",
                    idempotency_key="deferred-post-replay-key",
                    wait_for_response=True,
                    response_timeout_seconds=75,
                ),
                request,
            )
        self.assertEqual(receipt["exchange_id"], exchange["id"])
        self.assertEqual(receipt["inbound_leg_id"], leg["id"])
        self.assertTrue(receipt["deferred"])
        self.assertEqual(receipt["delivery"], "asynchronous")
        submit.assert_not_awaited()

    async def test_direct_live_503_preserves_waiter_and_exact_replay(self) -> None:
        token = await self.issue_live_waiter_owner(
            "source",
            "run_direct_live_503",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_direct_live_503",
            requester_session_id="source",
            authorization_source_run_id="run_direct_live_503",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, leg, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_direct_live_503",
                source_session_id="source",
                source_run_id="run_direct_live_503",
                target_session_id="target",
                body="Retry this direct live request",
                idempotency_key="direct-live-503-key",
                live_response_lease=True,
            )
        )
        request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode()),
            ],
            "client": ("127.0.0.1", 1234),
        })
        captured_waiters = []
        original_register = (
            agent_server.register_or_replay_cross_chat_live_waiter_locked
        )

        async def replay_create(*_args, **_kwargs):
            return ({
                "exchange": await agent_server.CROSS_CHAT.get_exchange(
                    exchange["id"]
                ),
                "leg": await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"]),
            }, False)

        async def capture_waiter(*args, **kwargs):
            waiter = await original_register(*args, **kwargs)
            captured_waiters.append(waiter)
            return waiter

        unavailable = HTTPException(
            status_code=503,
            detail="target admission is temporarily unavailable",
        )
        with (
            patch.object(
                agent_server,
                "create_authorized_cross_chat_instruction",
                side_effect=replay_create,
            ),
            patch.object(
                agent_server,
                "register_or_replay_cross_chat_live_waiter_locked",
                side_effect=capture_waiter,
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_registered",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "submit_cross_chat_exchange_leg",
                AsyncMock(side_effect=unavailable),
            ) as submit,
            patch.object(
                agent_server,
                "schedule_cross_chat_exchange_leg_retry",
                Mock(),
            ) as schedule,
        ):
            req = agent_server.CrossChatHandoffRequest(
                target_session_id="grant_" + "a" * 64,
                action="request_reply",
                body="Retry this direct live request",
                idempotency_key="direct-live-503-key",
                wait_for_response=True,
                response_timeout_seconds=75,
            )
            first = await agent_server.submit_authorized_cross_chat_handoff(
                req,
                request,
            )
            replay = await agent_server.submit_authorized_cross_chat_handoff(
                req,
                request,
            )

        self.assertEqual(first, replay)
        self.assertNotIn("deferred", first)
        self.assertEqual(first["exchange_id"], exchange["id"])
        self.assertEqual(first["inbound_leg_id"], leg["id"])
        self.assertEqual(
            first["live_response_lease_id"],
            replay["live_response_lease_id"],
        )
        self.assertEqual(submit.await_count, 2)
        self.assertEqual(schedule.call_count, 2)
        self.assertEqual(len(captured_waiters), 2)
        self.assertIs(captured_waiters[0], captured_waiters[1])
        self.assertFalse(captured_waiters[0]["future"].done())
        self.assertIs(
            agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS[
                (exchange["id"], leg["id"])
            ],
            captured_waiters[0],
        )
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "active")
        self.assertTrue(bool(durable["live_response_lease"]))
        self.assertTrue(bool(durable["live_response_requested"]))
        self.assertEqual(
            len(await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])),
            1,
        )

    async def test_legacy_live_route_restart_retires_instead_of_retrying_provider(self) -> None:
        await self.assert_retired_exchange_delivery(state="running", reconcile=True, live=True)

    async def test_cancelled_direct_live_post_preserves_replayable_live_leg(self) -> None:
        token = await self.issue_live_waiter_owner(
            "source",
            "run_direct_live_cancel",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_direct_live_cancel",
            requester_session_id="source",
            authorization_source_run_id="run_direct_live_cancel",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, leg, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_direct_live_cancel",
                source_session_id="source",
                source_run_id="run_direct_live_cancel",
                target_session_id="target",
                body="Preserve this cancelled live POST",
                idempotency_key="direct-live-cancel-key",
                live_response_lease=True,
            )
        )
        request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode()),
            ],
            "client": ("127.0.0.1", 1234),
        })
        entered = asyncio.Event()
        release = asyncio.Event()
        captured_waiters = []
        original_register = (
            agent_server.register_or_replay_cross_chat_live_waiter_locked
        )

        async def replay_create(*_args, **_kwargs):
            return ({
                "exchange": await agent_server.CROSS_CHAT.get_exchange(
                    exchange["id"]
                ),
                "leg": await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"]),
            }, False)

        async def capture_waiter(*args, **kwargs):
            waiter = await original_register(*args, **kwargs)
            captured_waiters.append(waiter)
            return waiter

        async def delayed_submit(current_exchange, current_leg):
            entered.set()
            await release.wait()
            return current_exchange, current_leg

        with (
            patch.object(
                agent_server,
                "create_authorized_cross_chat_instruction",
                side_effect=replay_create,
            ),
            patch.object(
                agent_server,
                "register_or_replay_cross_chat_live_waiter_locked",
                side_effect=capture_waiter,
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_registered",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "submit_cross_chat_exchange_leg",
                side_effect=delayed_submit,
            ),
            patch.object(
                agent_server,
                "schedule_cross_chat_exchange_leg_retry",
                Mock(),
            ) as schedule,
        ):
            task = asyncio.create_task(
                agent_server.submit_authorized_cross_chat_handoff(
                    agent_server.CrossChatHandoffRequest(
                        target_session_id="grant_" + "c" * 64,
                        action="request_reply",
                        body="Preserve this cancelled live POST",
                        idempotency_key="direct-live-cancel-key",
                        wait_for_response=True,
                        response_timeout_seconds=75,
                    ),
                    request,
                )
            )
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        self.assertEqual(len(captured_waiters), 1)
        self.assertFalse(captured_waiters[0]["future"].done())
        self.assertIs(
            agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS[
                (exchange["id"], leg["id"])
            ],
            captured_waiters[0],
        )
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "active")
        self.assertTrue(bool(durable["live_response_lease"]))
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertEqual(durable_leg["status"], "registered")
        schedule.assert_called_once_with(leg["id"])

    async def test_retired_live_response_is_terminal_not_a_heartbeat(self) -> None:
        await self.assert_no_automatic_exchange_reply(live=True, size=40)

    async def test_legacy_queued_live_waiter_retires_without_a_get_request(self) -> None:
        await self.assert_retired_exchange_delivery(state="queued", reconcile=True, live=True)

    async def test_legacy_live_initial_leg_does_not_admit_provider_on_retirement(self) -> None:
        await self.assert_retired_exchange_delivery(live=True)

    async def test_periodic_reconciliation_retires_registered_live_initial_leg(self) -> None:
        await self.assert_retired_exchange_delivery(state="submitting", reconcile=True, live=True)

    async def test_legacy_registered_live_waiter_retires_with_terminal_result(self) -> None:
        await self.assert_retired_exchange_delivery(reconcile=True, live=True)

    async def test_revoking_completed_waiter_does_not_downgrade_fresh_followup(self) -> None:
        source_token = await self.issue_live_waiter_owner(
            "source",
            "run_live_expired_old_source",
        )
        target_token = await self.issue_live_waiter_owner(
            "target",
            "run_live_fresh_target",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_live_expired_old_waiter",
            requester_session_id="source",
            authorization_source_run_id="run_live_expired_old_source",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, inbound, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_live_expired_old_waiter",
                source_session_id="source",
                source_run_id="run_live_expired_old_source",
                target_session_id="target",
                body="First live request",
                idempotency_key="live-expired-old-request",
                live_response_lease=True,
            )
        )
        async with agent_server.cross_chat_live_lease_lock(exchange["id"]):
            old_waiter = await agent_server.register_cross_chat_live_waiter_locked(
                exchange,
                inbound,
                owner_session_id="source",
                owner_run_id="run_live_expired_old_source",
                capability_token=source_token,
                timeout_seconds=10,
            )
            inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
                inbound["id"],
                expected={"registered"},
                status="running",
                target_run_id="run_live_fresh_target",
            )
            self.assertIsNotNone(inbound)
            exchange, followup, _created = (
                await agent_server.CROSS_CHAT.commit_exchange_response(
                    exchange_id=exchange["id"],
                    inbound_leg_id=inbound["id"],
                    source_session_id="target",
                    source_run_id="run_live_fresh_target",
                    body="Answer plus follow-up",
                    request_response=True,
                    idempotency_key="live-expired-old-followup",
                    automatic=False,
                )
            )
            exchange, followup, fresh_waiter = (
                await agent_server.deliver_cross_chat_live_response_locked(
                    exchange,
                    followup,
                    response_capability_token=target_token,
                    response_timeout_seconds=10,
                )
            )
            self.assertIsNotNone(fresh_waiter)
        await agent_server.revoke_cross_chat_capability(
            "run_live_expired_old_source"
        )

        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "active")
        self.assertTrue(bool(durable["live_response_lease"]))
        self.assertEqual(durable["active_leg_id"], followup["id"])
        self.assertNotIn(
            (exchange["id"], inbound["id"]),
            agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS,
        )
        self.assertTrue(old_waiter["future"].done())
        self.assertIs(
            agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS[
                (exchange["id"], followup["id"])
            ],
            fresh_waiter,
        )
        self.assertFalse(fresh_waiter["future"].done())

    async def test_live_lease_lock_identity_survives_waiter_handoff_and_retires(
        self,
    ) -> None:
        exchange_id = "exchange_lock_handoff"
        owner_entered = asyncio.Event()
        release_owner = asyncio.Event()
        acquired = asyncio.Event()
        release_queued = asyncio.Event()

        async def initial_owner() -> None:
            async with agent_server.cross_chat_live_lease_lock(exchange_id):
                owner_entered.set()
                await release_owner.wait()

        async def queued_owner() -> None:
            async with agent_server.cross_chat_live_lease_lock(exchange_id):
                acquired.set()
                await release_queued.wait()

        initial = asyncio.create_task(initial_owner())
        await asyncio.wait_for(owner_entered.wait(), timeout=1)
        retained_lock = agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS[exchange_id]
        queued = asyncio.create_task(queued_owner())
        await asyncio.sleep(0)
        self.assertEqual(
            agent_server.CROSS_CHAT_LIVE_LEASE_LOCK_REFCOUNTS[exchange_id],
            2,
        )
        release_owner.set()
        await asyncio.wait_for(initial, timeout=1)
        await asyncio.wait_for(acquired.wait(), timeout=1)
        self.assertIs(
            agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS[exchange_id],
            retained_lock,
        )
        self.assertEqual(
            agent_server.CROSS_CHAT_LIVE_LEASE_LOCK_REFCOUNTS[exchange_id],
            1,
        )
        release_queued.set()
        await asyncio.wait_for(queued, timeout=1)
        self.assertNotIn(exchange_id, agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS)
        self.assertNotIn(
            exchange_id,
            agent_server.CROSS_CHAT_LIVE_LEASE_LOCK_REFCOUNTS,
        )

    async def test_shutdown_waiter_settlement_serializes_on_live_lease_lock(self) -> None:
        exchange_id = "exchange_shutdown_lock"
        inbound_leg_id = "leg_shutdown_lock"
        waiter = {
            "future": asyncio.get_running_loop().create_future(),
        }
        agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS[
            (exchange_id, inbound_leg_id)
        ] = waiter
        lock_held = asyncio.Event()
        release_lock = asyncio.Event()

        async def hold_live_lease_lock() -> None:
            async with agent_server.cross_chat_live_lease_lock(exchange_id):
                lock_held.set()
                await release_lock.wait()

        lock_holder = asyncio.create_task(hold_live_lease_lock())
        await asyncio.wait_for(lock_held.wait(), timeout=1)
        settlement = asyncio.create_task(
            agent_server.settle_cross_chat_live_waiters_for_shutdown()
        )
        await asyncio.sleep(0)
        self.assertFalse(settlement.done())
        self.assertFalse(waiter["future"].done())

        release_lock.set()
        await asyncio.wait_for(lock_holder, timeout=1)
        await asyncio.wait_for(settlement, timeout=1)
        self.assertEqual(agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS, {})
        self.assertEqual(
            waiter["future"].result()["error_code"],
            "server_shutdown",
        )
        self.assertNotIn(
            exchange_id,
            agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS,
        )

    async def test_invalid_live_wait_path_does_not_retain_keyed_lock(self) -> None:
        token = await self.issue_live_waiter_owner(
            "source",
            "run_invalid_live_wait_path",
        )
        before = dict(agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS)
        with self.assertRaises(HTTPException) as raised:
            await agent_server.authorized_cross_chat_live_waiter(
                token,
                exchange_id="exchange_" + "a" * 32,
                inbound_leg_id="leg_" + "b" * 32,
                lease_id="lease_" + "c" * 32,
            )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(agent_server.CROSS_CHAT_LIVE_LEASE_LOCKS, before)

    async def test_capability_revocation_downgrades_its_exact_live_waiter(self) -> None:
        source_token = await self.issue_live_waiter_owner(
            "source",
            "run_live_revoked",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_live_revoked",
            requester_session_id="source",
            authorization_source_run_id="run_live_revoked",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, inbound, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_live_revoked",
                source_session_id="source",
                source_run_id="run_live_revoked",
                target_session_id="target",
                body="Revoke this waiter",
                idempotency_key="live-revoked",
                live_response_lease=True,
            )
        )
        async with agent_server.cross_chat_live_lease_lock(exchange["id"]):
            waiter = await agent_server.register_cross_chat_live_waiter_locked(
                exchange,
                inbound,
                owner_session_id="source",
                owner_run_id="run_live_revoked",
                capability_token=source_token,
            )
        await agent_server.revoke_cross_chat_capability("run_live_revoked")
        self.assertTrue(waiter["future"].result()["ok"])
        self.assertTrue(waiter["future"].result()["deferred"])
        self.assertEqual(
            waiter["future"].result()["delivery"],
            "asynchronous",
        )
        self.assertEqual(agent_server.CROSS_CHAT_LIVE_RESPONSE_WAITERS, {})
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(inbound["id"])
        self.assertEqual(durable["status"], "active")
        self.assertFalse(bool(durable["live_response_lease"]))
        self.assertEqual(durable_leg["status"], "registered")

    async def test_revocation_between_durable_acceptance_and_waiter_publication_defers(self) -> None:
        token = await self.issue_live_waiter_owner(
            "source",
            "run_acceptance_waiter_gap",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_acceptance_waiter_gap",
            requester_session_id="source",
            authorization_source_run_id="run_acceptance_waiter_gap",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, leg, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_acceptance_waiter_gap",
                source_session_id="source",
                source_run_id="run_acceptance_waiter_gap",
                target_session_id="target",
                body="Preserve this accepted request",
                idempotency_key="acceptance-waiter-gap",
                live_response_lease=True,
            )
        )
        request = Request({
            "type": "http",
            "headers": [
                (b"x-agentsdock-provider-capability", token.encode()),
            ],
            "client": ("127.0.0.1", 1234),
        })

        async def accepted_then_revoked(*_args, **_kwargs):
            await agent_server.revoke_cross_chat_capability(
                "run_acceptance_waiter_gap"
            )
            return {"exchange": exchange, "leg": leg}, True

        submit = AsyncMock(side_effect=lambda current_exchange, current_leg: (
            current_exchange,
            current_leg,
        ))
        with (
            patch.object(
                agent_server,
                "create_authorized_cross_chat_instruction",
                side_effect=accepted_then_revoked,
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_registered",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "submit_cross_chat_exchange_leg",
                submit,
            ),
        ):
            receipt = await agent_server.submit_authorized_cross_chat_handoff(
                agent_server.CrossChatHandoffRequest(
                    target_session_id="grant_" + "a" * 64,
                    action="request_reply",
                    body="Preserve this accepted request",
                    idempotency_key="acceptance-waiter-gap",
                    wait_for_response=True,
                ),
                request,
            )

        self.assertTrue(receipt["deferred"])
        submit.assert_awaited_once()
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(leg["id"])
        self.assertEqual(durable["status"], "active")
        self.assertIsNone(durable["error_code"])
        self.assertFalse(bool(durable["live_response_lease"]))
        self.assertEqual(durable_leg["status"], "registered")

    async def test_source_revocation_preserves_queued_exchange_delivery(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_live_revoked_queued",
            "run_live_revoked_queued",
        )
        inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="submitting",
        )
        self.assertIsNotNone(inbound)
        inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"submitting"},
            status="queued",
            queued_id="queued_live_revoked_exact",
            queue_position=1,
        )
        self.assertIsNotNone(inbound)
        unrelated = {
            "queued_id": "queued_unrelated",
            "purpose": None,
        }
        agent_server.QUEUED_TURNS["target"] = deque([
            unrelated,
            {
                "queued_id": "queued_live_revoked_exact",
                "purpose": "cross_chat_handoff_delivery",
                "source_session_id": "source",
                "target_session_id": "target",
                "cross_chat_exchange_id": exchange["id"],
                "cross_chat_exchange_leg_id": inbound["id"],
            },
        ])
        with patch.object(
            agent_server,
            "append_durable_event",
            AsyncMock(),
        ) as append:
            await agent_server.revoke_cross_chat_capability(
                "run_live_revoked_queued"
            )

        self.assertTrue(waiter["future"].done())
        self.assertTrue(waiter["future"].result()["deferred"])
        self.assertEqual(
            list(agent_server.QUEUED_TURNS["target"]),
            [
                unrelated,
                {
                    "queued_id": "queued_live_revoked_exact",
                    "purpose": "cross_chat_handoff_delivery",
                    "source_session_id": "source",
                    "target_session_id": "target",
                    "cross_chat_exchange_id": exchange["id"],
                    "cross_chat_exchange_leg_id": inbound["id"],
                },
            ],
        )
        append.assert_not_awaited()
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(inbound["id"])
        self.assertEqual(durable["status"], "active")
        self.assertFalse(bool(durable["live_response_lease"]))
        self.assertEqual(durable_leg["status"], "queued")

    async def test_source_revocation_preserves_queue_owner_bound_during_downgrade(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_live_revoke_queue_race",
            "run_live_revoke_queue_race",
        )
        agent_server.QUEUED_TURNS["target"] = deque([{
            "queued_id": "queued_live_revoke_queue_race",
            "purpose": "cross_chat_handoff_delivery",
            "source_session_id": "source",
            "target_session_id": "target",
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": inbound["id"],
        }])
        original_downgrade = agent_server.CROSS_CHAT.downgrade_live_exchange

        async def bind_queue_owner_then_downgrade(*args, **kwargs):
            claimed = await agent_server.CROSS_CHAT.update_exchange_leg(
                inbound["id"],
                expected={"registered"},
                status="submitting",
            )
            self.assertIsNotNone(claimed)
            queued = await agent_server.CROSS_CHAT.update_exchange_leg(
                inbound["id"],
                expected={"submitting"},
                status="queued",
                queued_id="queued_live_revoke_queue_race",
                queue_position=1,
            )
            self.assertIsNotNone(queued)
            return await original_downgrade(*args, **kwargs)

        with patch.object(
            agent_server.CROSS_CHAT,
            "downgrade_live_exchange",
            side_effect=bind_queue_owner_then_downgrade,
        ):
            await agent_server.revoke_cross_chat_capability(
                "run_live_revoke_queue_race"
            )

        self.assertTrue(waiter["future"].result()["deferred"])
        self.assertEqual(
            agent_server.QUEUED_TURNS["target"][0]["queued_id"],
            "queued_live_revoke_queue_race",
        )
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        durable_leg = await agent_server.CROSS_CHAT.get_exchange_leg(inbound["id"])
        self.assertEqual(durable["status"], "active")
        self.assertFalse(bool(durable["live_response_lease"]))
        self.assertEqual(durable_leg["status"], "queued")
        self.assertEqual(
            durable_leg["queued_id"],
            "queued_live_revoke_queue_race",
        )

    async def test_retired_target_completion_does_not_send_to_successor_turn(self) -> None:
        await self.assert_no_automatic_exchange_reply(turnover=True)

    async def test_legacy_queue_retirement_preserves_user_work_after_source_turnover(self) -> None:
        await self.assert_retired_exchange_delivery(state="queued", reconcile=True, turnover=True)

    async def test_targeted_stop_run_guard_cannot_stop_a_promoted_successor(self) -> None:
        active_successor = {
            "run_id": "run_user_successor",
            "purpose": None,
            "cross_chat_exchange_id": None,
            "cross_chat_exchange_leg_id": None,
        }
        current_successor = dict(active_successor)
        with (
            patch.object(agent_server, "ACTIVE", {"target": active_successor}),
            patch.object(agent_server, "CURRENT_TURNS", {"target": current_successor}),
        ):
            agent_server.BUSY_SESSIONS.add("target")
            try:
                result = await agent_server.stop_turn(
                    "target",
                    expected_run_id="run_cancelled_delivery",
                    pause_queued_turns_on_stop=False,
                )
            finally:
                agent_server.BUSY_SESSIONS.discard("target")
        self.assertFalse(result["stopped"])
        self.assertTrue(result["superseded"])
        self.assertNotIn("stop_requested", active_successor)
        self.assertNotIn("stop_requested", current_successor)

    async def test_exact_exchange_target_stop_interrupts_codex_and_claude_owner(self) -> None:
        for backend, transport in (
            (
                agent_server.BACKEND_CODEX,
                agent_server.CODEX_TRANSPORT_APP_SERVER,
            ),
            (
                agent_server.BACKEND_CLAUDE,
                agent_server.CLAUDE_TRANSPORT_AGENT_SDK,
            ),
        ):
            with self.subTest(backend=backend):
                run_id = f"run_exact_interrupt_{backend}"
                exchange_id = f"exchange_exact_interrupt_{backend}"
                leg_id = f"leg_exact_interrupt_{backend}"
                provider = Mock()
                provider.interrupt = AsyncMock(return_value=True)
                active = {
                    "run_id": run_id,
                    "backend": backend,
                    "transport": transport,
                    "provider_turn_ready": True,
                    "provider_session_id": f"provider_{backend}",
                    "cross_chat_exchange_id": exchange_id,
                    "cross_chat_exchange_leg_id": leg_id,
                }
                if backend == agent_server.BACKEND_CODEX:
                    provider.turn_id = f"turn_{backend}"
                    active["codex_app_server_turn"] = provider
                else:
                    active["claude_sdk_run"] = provider
                    active["claude_permissions_open"] = True
                current = {
                    "run_id": run_id,
                    "purpose": "cross_chat_handoff_delivery",
                    "cross_chat_exchange_id": exchange_id,
                    "cross_chat_exchange_leg_id": leg_id,
                }
                agent_server.STORE.sessions["target"]["backend"] = backend
                with (
                    patch.object(agent_server, "ACTIVE", {"target": active}),
                    patch.object(agent_server, "CURRENT_TURNS", {"target": current}),
                    patch.object(agent_server, "RUN_METADATA", {run_id: dict(current)}),
                    patch.object(agent_server, "BUSY_SESSIONS", {"target"}),
                    patch.object(agent_server, "STOPPED_RUNS", set()),
                    patch.object(agent_server, "STOP_REQUESTS", set()),
                    patch.object(agent_server, "revoke_cross_chat_capability", AsyncMock()),
                    patch.object(agent_server, "cancel_codex_interactions", AsyncMock()),
                    patch.object(agent_server, "cancel_claude_interactions", AsyncMock()),
                    patch.object(
                        agent_server,
                        "pause_active_codex_goal_for_stop",
                        AsyncMock(return_value=(True, False, None)),
                    ),
                    patch.object(
                        agent_server,
                        "stop_idle_claude_background_subagents_bounded",
                        AsyncMock(return_value={
                            "fence_committed": True,
                            "descendants": 0,
                            "requested": [],
                            "interrupted": [],
                            "pending": [],
                            "errors": [],
                        }),
                    ),
                    patch.object(agent_server, "append_event", AsyncMock()),
                    patch.object(agent_server, "schedule_next_queued_turn"),
                    patch.object(agent_server, "STOP_CONFIRM_TIMEOUT_SECONDS", 0.01),
                ):
                    stopped = await agent_server.stop_exact_cross_chat_exchange_target_run(
                        exchange_id,
                        {
                            "id": leg_id,
                            "target_session_id": "target",
                            "target_run_id": run_id,
                        },
                    )
                self.assertTrue(stopped)
                provider.interrupt.assert_awaited_once()

    async def test_cancel_during_queue_promotion_never_stops_the_successor(self) -> None:
        exchange, inbound = await self.create_exchange(
            "exchange_cancel_promotion_successor"
        )
        queued_id = "queued_cancel_promotion"
        inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="queued",
            queued_id=queued_id,
            queue_position=1,
        )
        self.assertIsNotNone(inbound)
        inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"queued"},
            status="running",
            target_run_id="run_cancelled_delivery",
        )
        self.assertIsNotNone(inbound)
        successor = {
            "run_id": "run_user_successor",
            "purpose": None,
        }
        stop = AsyncMock(return_value={
            "ok": True,
            "stopped": True,
            "pending": False,
        })
        with (
            patch.object(agent_server, "ACTIVE", {"target": dict(successor)}),
            patch.object(agent_server, "CURRENT_TURNS", {"target": dict(successor)}),
            patch.object(agent_server, "stop_turn", stop),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", AsyncMock()),
        ):
            cancelled = await agent_server.cancel_cross_chat_exchange(
                exchange["id"]
            )
        self.assertEqual(cancelled["status"], "cancelled")
        stop.assert_not_awaited()

    async def test_secure_peer_ask_rejects_live_wait_before_outbound_side_effect(self) -> None:
        token = "secure-live-wait-token"
        token_hash = agent_server.hashlib.sha256(token.encode()).hexdigest()
        handle = "secure-peer-handle"
        agent_server.CURRENT_TURNS["source"] = {"run_id": "run_secure_wait"}
        agent_server.CROSS_CHAT_CAPABILITIES[token_hash] = {
            "server_identity": agent_server.server_identity(),
            "source_session_id": "source",
            "source_run_id": "run_secure_wait",
            "source_user_instruction": "Ask the peer",
            "native_transition_nonce": "",
            "actions": {"secure_peer_request_reply"},
            "secure_peer_grants": {
                (handle, "request_reply"): {"opaque": "snapshot"},
            },
            "consumed": {},
        }
        prepare = Mock()
        with patch.object(
            agent_server.SECURE_PEER_RUNTIME,
            "prepare_outbound_handoff",
            prepare,
        ):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.create_authorized_cross_chat_instruction(
                    token,
                    agent_server.CrossChatHandoffRequest(
                        target_session_id=handle,
                        action="request_reply",
                        body="Wait for the peer",
                        idempotency_key="secure-live-wait",
                        wait_for_response=True,
                        response_timeout_seconds=75,
                    ),
                )
        self.assertEqual(raised.exception.status_code, 400)
        prepare.assert_not_called()
        self.assertEqual(
            agent_server.CROSS_CHAT_CAPABILITIES[token_hash]["consumed"],
            {},
        )

    async def test_new_followup_rejects_before_response_commit(self) -> None:
        await self.assert_retired_direct_authority(response=True, repeat=2)

    async def test_stale_followup_cannot_downgrade_into_new_async_delivery(self) -> None:
        await self.assert_retired_direct_authority(response=True, stale=True)

    async def test_retry_stale_followup_never_commits_next_leg(self) -> None:
        await self.assert_retired_direct_authority(response=True, stale=True, repeat=2)

    async def test_stale_followup_cannot_restore_live_reply_lease(self) -> None:
        await self.assert_retired_direct_authority(response=True, stale=True, repeat=3)

    async def test_recovered_stale_live_lease_falls_back_to_async_queue(self) -> None:
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_live_stale_queue",
            requester_session_id="source",
            authorization_source_run_id="run_live_stale_queue",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, inbound, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_live_stale_queue",
                source_session_id="source",
                source_run_id="run_live_stale_queue",
                target_session_id="target",
                body="Deliver asynchronously after restart",
                idempotency_key="live-stale-queue",
                live_response_lease=True,
            )
        )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="queued",
            queued_id="queued_live_stale",
            queue_position=1,
        )
        recovered_item = {
            "queued_id": "queued_live_stale",
            "prompt": "stale live relay",
            "purpose": "cross_chat_handoff_delivery",
            "source_session_id": "source",
            "target_session_id": "target",
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": inbound["id"],
            "position": 1,
            "_durable": True,
            "_paused_after_stop": False,
        }
        with (
            patch.object(agent_server, "SERVER_INSTANCE_ID", "new-instance"),
            patch.object(
                agent_server,
                "scan_queued_turns_from_events",
                return_value={"target": [recovered_item]},
            ),
            patch.object(agent_server, "append_durable_event", AsyncMock()),
            patch.object(agent_server, "schedule_next_queued_turn") as schedule,
        ):
            agent_server.initialize_queue_recovery()
            rebuilt, scheduled = await agent_server.recover_queued_turns_after_start()
        self.assertEqual((rebuilt, scheduled), (1, 1))
        schedule.assert_called_once_with("target")
        self.assertEqual(
            agent_server.QUEUED_TURNS["target"][0]["queued_id"],
            "queued_live_stale",
        )
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "active")
        self.assertFalse(bool(durable["live_response_lease"]))

    async def test_live_delivery_wins_or_cancel_serializes_after_future_release(self) -> None:
        source_token = await self.issue_live_waiter_owner(
            "source",
            "run_live_cancel_source",
        )
        await agent_server.CROSS_CHAT.create_exchange_obligation(
            exchange_id="exchange_live_cancel_race",
            requester_session_id="source",
            authorization_source_run_id="run_live_cancel_source",
            responder_session_id="target",
            max_legs=6,
            expires_at="2099-01-01T00:00:00Z",
        )
        exchange, inbound, _created = (
            await agent_server.CROSS_CHAT.create_initial_exchange_leg(
                exchange_id="exchange_live_cancel_race",
                source_session_id="source",
                source_run_id="run_live_cancel_source",
                target_session_id="target",
                body="Race cancellation",
                idempotency_key="live-cancel-race",
                live_response_lease=True,
            )
        )
        async with agent_server.cross_chat_live_lease_lock(exchange["id"]):
            waiter = await agent_server.register_cross_chat_live_waiter_locked(
                exchange,
                inbound,
                owner_session_id="source",
                owner_run_id="run_live_cancel_source",
                capability_token=source_token,
            )
        await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id="run_live_cancel_target",
        )
        exchange, outbound, _created = (
            await agent_server.CROSS_CHAT.commit_exchange_response(
                exchange_id=exchange["id"],
                inbound_leg_id=inbound["id"],
                source_session_id="target",
                source_run_id="run_live_cancel_target",
                body="Committed answer",
                request_response=False,
                idempotency_key="live-cancel-answer",
                automatic=False,
            )
        )
        real_finish = agent_server.CROSS_CHAT.finish_exchange_leg
        durable_finished = asyncio.Event()
        release_finish = asyncio.Event()

        async def pause_after_finish(*args, **kwargs):
            result = await real_finish(*args, **kwargs)
            if args[0] == outbound["id"]:
                durable_finished.set()
                await release_finish.wait()
            return result

        async def deliver():
            async with agent_server.cross_chat_live_lease_lock(exchange["id"]):
                return await agent_server.deliver_cross_chat_live_response_locked(
                    exchange,
                    outbound,
                )

        leg_lifecycle = AsyncMock()
        exchange_lifecycle = AsyncMock()
        failure_status = AsyncMock()
        with (
            patch.object(agent_server.CROSS_CHAT, "finish_exchange_leg", side_effect=pause_after_finish),
            patch.object(agent_server, "append_cross_chat_exchange_leg_terminal_lifecycle", leg_lifecycle),
            patch.object(agent_server, "append_cross_chat_exchange_terminal_lifecycle", exchange_lifecycle),
            patch.object(agent_server, "maybe_deliver_cross_chat_exchange_failure_status", failure_status),
        ):
            delivery_task = asyncio.create_task(deliver())
            await asyncio.wait_for(durable_finished.wait(), timeout=1)
            cancel_task = asyncio.create_task(
                agent_server.cancel_cross_chat_exchange(exchange["id"])
            )
            await asyncio.sleep(0)
            self.assertFalse(cancel_task.done())
            release_finish.set()
            await asyncio.wait_for(delivery_task, timeout=1)
            await asyncio.wait_for(cancel_task, timeout=1)
        leg_lifecycle.assert_not_awaited()
        exchange_lifecycle.assert_not_awaited()
        failure_status.assert_not_awaited()
        self.assertTrue(waiter["future"].result()["ok"])
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(durable["status"], "completed")
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual([leg["kind"] for leg in legs], ["request", "reply"])

    async def test_explicit_cancel_after_async_downgrade_stops_exact_target(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_cancel_after_async_downgrade",
            "run_cancel_after_async_downgrade_source",
        )
        target_run_id = "run_cancel_after_async_downgrade_target"
        inbound = await agent_server.CROSS_CHAT.update_exchange_leg(
            inbound["id"],
            expected={"registered"},
            status="running",
            target_run_id=target_run_id,
        )
        self.assertIsNotNone(inbound)
        agent_server.CURRENT_TURNS["target"] = {
            "run_id": target_run_id,
            "purpose": "cross_chat_handoff_delivery",
            "cross_chat_exchange_id": exchange["id"],
            "cross_chat_exchange_leg_id": inbound["id"],
        }

        await agent_server.revoke_cross_chat_capability(
            "run_cancel_after_async_downgrade_source"
        )
        downgraded = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        self.assertEqual(downgraded["status"], "active")
        self.assertFalse(bool(downgraded["live_response_lease"]))
        self.assertTrue(waiter["future"].result()["deferred"])

        stop = AsyncMock(return_value={
            "ok": True,
            "stopped": True,
            "pending": False,
        })
        with (
            patch.object(agent_server, "stop_turn", stop),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_terminal_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_terminal_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "maybe_deliver_cross_chat_exchange_failure_status",
                AsyncMock(),
            ) as failure_status,
        ):
            cancelled = await agent_server.cancel_cross_chat_exchange(
                exchange["id"]
            )
            await agent_server.finalize_cross_chat_terminal({
                "type": "turn_stopped",
                "run_id": target_run_id,
                "purpose": "cross_chat_handoff_delivery",
                "cross_chat_exchange_id": exchange["id"],
                "cross_chat_exchange_leg_id": inbound["id"],
                "result_text": "",
                "exit_code": 130,
                "stopped": True,
            })

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["error_code"], "cancelled_by_user")
        stop.assert_awaited_once_with(
            "target",
            expected_run_id=target_run_id,
            pause_queued_turns_on_stop=False,
        )
        failure_status.assert_not_awaited()
        durable = await agent_server.CROSS_CHAT.get_exchange(exchange["id"])
        legs = await agent_server.CROSS_CHAT.exchange_legs(exchange["id"])
        self.assertEqual(durable["status"], "cancelled")
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]["status"], "failed")
        self.assertEqual(legs[0]["response_state"], "closed")

    async def test_explicit_cancel_wakes_an_indefinite_live_waiter(self) -> None:
        exchange, inbound, waiter = await self.create_live_waiter(
            "exchange_live_explicit_cancel",
            "run_live_explicit_cancel_source",
        )

        with (
            patch.object(
                agent_server,
                "append_cross_chat_exchange_leg_terminal_lifecycle",
                AsyncMock(),
            ),
            patch.object(
                agent_server,
                "append_cross_chat_exchange_terminal_lifecycle",
                AsyncMock(),
            ),
        ):
            cancelled = await agent_server.cancel_cross_chat_exchange(
                exchange["id"]
            )

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["error_code"], "cancelled_by_user")
        self.assertTrue(waiter["future"].done())
        self.assertEqual(
            waiter["future"].result()["error_code"],
            "cancelled_by_user",
        )
        with self.assertRaises(HTTPException) as closed:
            await agent_server.await_cross_chat_live_waiter(
                cancelled,
                waiter,
                timeout_seconds=1,
            )
        self.assertEqual(closed.exception.status_code, 410)
        self.assertIn(
            "cancelled by the user",
            str(closed.exception.detail),
        )

    async def test_retired_live_completion_has_no_restart_async_answer(self) -> None:
        await self.assert_no_automatic_exchange_reply(live=True, turnover=True)


if __name__ == "__main__":
    unittest.main()
