"""AST-only route retirement tests; ledger data stays in in-memory SQLite."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from contextlib import suppress
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import AsyncMock


ROOT = Path(__file__).parent
TREE = ast.parse((ROOT / "agent_server.py").read_text())
LEDGER_METHODS = {
    "_row", "get", "update", "nonterminal_for_session", "get_exchange",
    "get_exchange_leg", "cancel_exchange", "nonterminal_exchanges_for_session",
}
LEDGER_CLASS = next(
    node for node in TREE.body
    if isinstance(node, ast.ClassDef) and node.name == "CrossChatStore"
)
EXTRACTED_LEDGER = ast.ClassDef(
    name="IsolatedLedger", bases=[], keywords=[], decorator_list=[],
    body=[
        node for node in LEDGER_CLASS.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in LEDGER_METHODS
    ],
)
HELPER = next(
    node for node in TREE.body
    if isinstance(node, ast.AsyncFunctionDef)
    and node.name == "retire_revoked_provider_route_deliveries"
)
CODE = compile(ast.fix_missing_locations(ast.Module(body=[
    ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
    EXTRACTED_LEDGER,
    HELPER,
], type_ignores=[])), "<isolated-route-retirement>", "exec")


class ProviderRouteRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE cross_chat_envelopes (
                id TEXT PRIMARY KEY, source_session_id TEXT, target_session_id TEXT,
                authorization_kind TEXT, authorization_route_id TEXT, status TEXT,
                queued_id TEXT, target_run_id TEXT, lifecycle_status TEXT DEFAULT '',
                error TEXT, created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
            );
            CREATE TABLE cross_chat_exchanges (
                id TEXT PRIMARY KEY, requester_session_id TEXT, responder_session_id TEXT,
                authorization_kind TEXT, authorization_route_id TEXT, status TEXT,
                active_leg_id TEXT, lifecycle_status TEXT DEFAULT '', error_code TEXT,
                live_response_lease INTEGER DEFAULT 0, error TEXT,
                created_at TEXT DEFAULT '', updated_at TEXT DEFAULT ''
            );
            CREATE TABLE cross_chat_exchange_legs (
                id TEXT PRIMARY KEY, exchange_id TEXT, source_session_id TEXT,
                target_session_id TEXT, queued_id TEXT, target_run_id TEXT, kind TEXT,
                status TEXT, response_state TEXT DEFAULT 'open', error_code TEXT,
                error TEXT, lifecycle_status TEXT DEFAULT '', updated_at TEXT DEFAULT ''
            );
        """)
        self.queues = {}
        self.run_now = {}
        self.promotions = {}
        self.lease_locks = {}
        self.events = AsyncMock()
        self.envelope_lifecycle = AsyncMock()
        self.exchange_lifecycle = AsyncMock()
        self.leg_lifecycle = AsyncMock()
        self.waiter_failure = AsyncMock()
        self.namespace = {
            "asyncio": asyncio, "deque": deque, "suppress": suppress,
            "now_iso": lambda: "2026-09-10T00:00:00Z",
            "CROSS_CHAT_EXCHANGE_TERMINAL_STATUSES": {"completed", "failed", "cancelled", "expired"},
            "QUEUE_LOCK": asyncio.Lock(), "QUEUED_TURNS": self.queues,
            "RUN_NOW_TURNS": self.run_now,
            "queue_promotion_owner_locked": self.promotions.get,
            "cross_chat_live_lease_lock": lambda key: self.lease_locks.setdefault(key, asyncio.Lock()),
            "append_durable_event": self.events,
            "append_cross_chat_terminal_lifecycle": self.envelope_lifecycle,
            "append_cross_chat_exchange_terminal_lifecycle": self.exchange_lifecycle,
            "append_cross_chat_exchange_leg_terminal_lifecycle": self.leg_lifecycle,
            "settle_cross_chat_live_waiter_failure": self.waiter_failure,
            "join_task_despite_caller_cancellation": self.join_completion,
        }
        exec(CODE, self.namespace)
        self.ledger = self.namespace["IsolatedLedger"]()
        self.ledger.TERMINAL_STATUSES = {"delivered", "failed", "cancelled"}
        self.ledger._initialized = True
        self.ledger._transaction = lambda: self.connection
        self.ledger._call = self.call_operation
        self.namespace["CROSS_CHAT"] = self.ledger
        self.retire = self.namespace["retire_revoked_provider_route_deliveries"]

    @staticmethod
    async def call_operation(operation):
        return operation()

    @staticmethod
    async def join_completion(task):
        return await task

    def insert(self, table, record):
        columns = ",".join(record)
        placeholders = ",".join("?" for _ in record)
        self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", list(record.values())
        )
        self.connection.commit()
        return record

    def envelope(self, identifier, status="ready", **extra):
        return self.insert("cross_chat_envelopes", {
            "id": identifier, "source_session_id": "source", "target_session_id": "target",
            "authorization_kind": "configured_route", "authorization_route_id": "revoked",
            "status": status, **extra,
        })

    def exchange(self, identifier, leg_status=None, **extra):
        leg_id = identifier + "-leg" if leg_status else None
        exchange = self.insert("cross_chat_exchanges", {
            "id": identifier, "requester_session_id": "source", "responder_session_id": "target",
            "authorization_kind": "configured_route", "authorization_route_id": "revoked",
            "status": "active" if leg_status else "waiting_request", "active_leg_id": leg_id,
            **extra,
        })
        leg = None
        if leg_status:
            leg = self.insert("cross_chat_exchange_legs", {
                "id": leg_id, "exchange_id": identifier, "source_session_id": "source",
                "target_session_id": "target", "kind": "request", "status": leg_status,
                "queued_id": identifier + "-queue" if leg_status == "queued" else None,
            })
        return exchange, leg

    @staticmethod
    def queue_item(record, exchange_id=None):
        return {
            "queued_id": record["queued_id"],
            "cross_chat_envelope_id": None if exchange_id else record["id"],
            "cross_chat_exchange_id": exchange_id,
            "cross_chat_exchange_leg_id": record["id"] if exchange_id else None,
        }

    async def test_only_exact_route_source_and_configured_authorization_are_retired(self):
        unsent = ["waiting_admission", "waiting_source", "ready", "submitting", "queued"]
        for status in unsent:
            self.envelope(status, status)
        self.envelope("running", "running", target_run_id="live-run")
        self.envelope("other-route", authorization_route_id="other")
        self.envelope("explicit", authorization_kind="explicit_prompt")
        self.envelope("incoming", source_session_id="other", target_session_id="source")

        await self.retire("source", "revoked")

        for status in unsent:
            self.assertEqual((await self.ledger.get(status))["status"], "cancelled")
        for identifier in ["other-route", "explicit", "incoming"]:
            self.assertEqual((await self.ledger.get(identifier))["status"], "ready")
        running = await self.ledger.get("running")
        self.assertEqual((running["status"], running["target_run_id"]), ("running", "live-run"))
        self.assertEqual(self.envelope_lifecycle.await_count, len(unsent))

    async def test_queue_removal_requires_exact_delivery_identity(self):
        envelope = self.envelope("queued-envelope", "queued", queued_id="same-queue-id")
        exact = self.queue_item(envelope)
        collision = {**exact, "cross_chat_envelope_id": "another-envelope"}
        mixed = {**exact, "cross_chat_exchange_id": "another-exchange"}
        self.queues["target"] = deque([collision, exact, mixed])

        async def assert_durable_before_removal(*args):
            self.assertEqual((await self.ledger.get(envelope["id"]))["status"], "cancelled")
            self.assertEqual(list(self.queues["target"]), [collision, mixed])

        self.events.side_effect = assert_durable_before_removal
        await self.retire("source", "revoked")
        self.assertEqual(list(self.queues["target"]), [collision, mixed])
        self.events.assert_awaited_once()

    async def test_run_now_and_popped_promotions_lose_the_final_admission_cas(self):
        run_now = self.envelope("run-now", "queued", queued_id="q-now", target_session_id="now-target")
        popped = self.envelope("popped", "queued", queued_id="q-pop", target_session_id="pop-target")
        self.run_now["now-target"] = self.queue_item(run_now)
        self.promotions["pop-target"] = self.queue_item(popped)

        await self.retire("source", "revoked")

        self.assertNotIn("now-target", self.run_now)
        self.assertEqual(self.events.await_count, 2)
        self.assertIsNone(await self.ledger.update("popped", expected={"queued"}, status="running"))
        self.assertEqual(self.promotions["pop-target"], self.queue_item(popped))

    async def test_running_envelope_winning_before_retirement_is_preserved(self):
        self.envelope("race", "submitting")
        update = self.ledger.update

        async def admit_first(identifier, **changes):
            await update(identifier, expected={"submitting"}, status="running", target_run_id="winner")
            return await update(identifier, **changes)

        self.ledger.update = admit_first
        await self.retire("source", "revoked")
        self.assertEqual((await self.ledger.get("race"))["status"], "running")
        self.envelope_lifecycle.assert_not_awaited()

    async def test_unsent_exchanges_retire_and_running_leg_response_state_is_preserved(self):
        self.exchange("waiting")
        for status in ["registered", "submitting", "queued", "running"]:
            exchange, leg = self.exchange(status, status)
            if status == "queued":
                self.queues["target"] = deque([self.queue_item(leg, exchange["id"])])
        self.exchange("other-route", "registered", authorization_route_id="other")
        self.exchange("other-owner", "registered", requester_session_id="other", responder_session_id="source")
        self.exchange("explicit", "registered", authorization_kind="explicit_prompt")

        await self.retire("source", "revoked")

        for identifier in ["waiting", "registered", "submitting", "queued"]:
            self.assertEqual((await self.ledger.get_exchange(identifier))["status"], "cancelled")
        running = await self.ledger.get_exchange_leg("running-leg")
        self.assertEqual((running["status"], running["response_state"]), ("running", "open"))
        for identifier in ["running", "other-route", "other-owner", "explicit"]:
            self.assertEqual((await self.ledger.get_exchange(identifier))["status"], "active")
        self.assertNotIn("target", self.queues)
        self.assertEqual(self.exchange_lifecycle.await_count, 4)
        self.assertEqual(self.leg_lifecycle.await_count, 3)
        self.assertEqual(self.waiter_failure.await_count, 4)

    async def test_reverse_leg_uses_immutable_requester_authorization(self):
        exchange, leg = self.exchange("reverse", "queued")
        self.connection.execute(
            "UPDATE cross_chat_exchange_legs SET source_session_id='target', target_session_id='source' WHERE id=?",
            (leg["id"],),
        )
        leg["source_session_id"], leg["target_session_id"] = "target", "source"
        exact = self.queue_item(leg, exchange["id"])
        collision = {**exact, "cross_chat_exchange_id": "other-exchange"}
        self.queues["source"] = deque([exact, collision])

        await self.retire("source", "revoked")

        self.assertEqual((await self.ledger.get_exchange_leg(leg["id"]))["status"], "cancelled")
        self.assertEqual(list(self.queues["source"]), [collision])

    async def test_exchange_admission_winning_shared_fence_preserves_running_leg(self):
        self.exchange("race", "submitting")
        lock = self.namespace["cross_chat_live_lease_lock"]("race")
        await lock.acquire()
        retirement = asyncio.create_task(self.retire("source", "revoked"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.connection.execute("UPDATE cross_chat_exchange_legs SET status='running' WHERE id='race-leg'")
        self.connection.commit()
        lock.release()
        await retirement
        self.assertEqual((await self.ledger.get_exchange("race"))["status"], "active")
        self.assertEqual((await self.ledger.get_exchange_leg("race-leg"))["response_state"], "open")
        self.exchange_lifecycle.assert_not_awaited()

    async def test_caller_cancellation_finishes_queue_and_lifecycle_cleanup(self):
        record = self.envelope("cancelled-caller", "queued", queued_id="q-cancel")
        self.queues["target"] = deque([self.queue_item(record)])
        entered, release = asyncio.Event(), asyncio.Event()

        async def pause_event(*args):
            entered.set()
            await release.wait()

        self.events.side_effect = pause_event
        retirement = asyncio.create_task(self.retire("source", "revoked"))
        await entered.wait()
        retirement.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await retirement
        self.assertEqual((await self.ledger.get(record["id"]))["status"], "cancelled")
        self.assertNotIn("target", self.queues)
        self.envelope_lifecycle.assert_awaited_once()

    async def test_failed_queue_tombstone_never_restores_terminal_delivery(self):
        record = self.envelope("failed-event", "queued", queued_id="q-failed")
        self.queues["target"] = deque([self.queue_item(record)])
        self.events.side_effect = OSError("isolated append failure")
        with self.assertRaisesRegex(OSError, "isolated append failure"):
            await self.retire("source", "revoked")
        terminal = await self.ledger.get(record["id"])
        self.assertEqual((terminal["status"], terminal["lifecycle_status"]), ("cancelled", ""))
        self.assertNotIn("target", self.queues)
        self.assertIsNone(await self.ledger.update(record["id"], expected={"queued"}, status="running"))

    async def test_uninitialized_ledger_and_empty_identity_are_noops(self):
        self.envelope("pending")
        await self.retire("", "revoked")
        await self.retire("source", "")
        self.ledger._initialized = False
        await self.retire("source", "revoked")
        self.assertEqual((await self.ledger.get("pending"))["status"], "ready")
        self.envelope_lifecycle.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
