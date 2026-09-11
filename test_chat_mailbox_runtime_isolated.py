"""Actual mailbox handlers/ledger on synthetic SQLite, never import the server."""
from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
from copy import deepcopy
import hashlib
import hmac
import logging
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import chat_mailbox


TREE = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
ROUTE = "route_" + "a" * 32
RETURN_ROUTE = "route_" + "b" * 32
PAIR = "pair_" + "c" * 32
NOW = "2026-01-01T00:00:00Z"
FUNCTIONS = {
    "reserve_async_provider_route_message", "submit_provider_route_handoff",
    "chat_mailbox_pairs", "public_chat_mailbox_message", "publish_chat_mailbox_message",
    "publish_chat_mailbox_read", "refresh_chat_mailbox_pending", "take_chat_mailbox_hint",
    "maybe_notify_chat_mailbox_codex", "get_provider_chat_mailbox", "read_provider_chat_mailbox",
    "delete_chat_mailbox_message",
    "provider_capability_is_attached_to_live_run", "codex_native_mailbox_owner_matches",
}
METHODS = {"__init__", "_locked_call", "_call", "_connect", "_transaction", "initialize", "_row",
           "create_instruction", "get", "update", "mailbox_call", "mailbox_envelopes"}


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


def isolated_source():
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    found = set()
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
            copied = deepcopy(node)
            copied.decorator_list = []
            nodes.append(copied)
            found.add(node.name)
    if found != FUNCTIONS:
        raise AssertionError("Required mailbox source changed")
    ledger = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "CrossChatStore")
    selected = [deepcopy(node) for node in ledger.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in METHODS]
    selected += [deepcopy(node) for node in ledger.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id == "TERMINAL_STATUSES" for target in node.targets)]
    nodes.append(ast.ClassDef(name="Ledger", bases=[], keywords=[], decorator_list=[], body=selected))
    namespace = dict(asyncio=asyncio, contextmanager=contextmanager, suppress=suppress,
                     hashlib=hashlib, hmac=hmac, sqlite3=sqlite3, threading=threading, time=time,
                     chat_mailbox=chat_mailbox, HTTPException=HTTPException,
                     now_iso=lambda: NOW, logger=logging.getLogger("isolated-mailbox"),
                     PROVIDER_CROSS_CHAT_LEGACY_RATE_RETENTION_SECONDS=86400,
                     PROVIDER_CROSS_CHAT_ROUTE_ID_RE=re.compile(r"route_[0-9a-f]{32}"),
                     PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE=re.compile(r"pair_[0-9a-f]{32}"),
                     validated_cross_chat_source_user_instruction=lambda value: value,
                     sanitized_provider_route_label=lambda value: str(value or "Untitled chat"),
                     CODEX_TRANSPORT_APP_SERVER="app-server")
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated-mailbox-runtime>", "exec"), namespace)
    return namespace


class ChatMailboxRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_mailbox_routes_have_exact_bounded_provider_header_entry_points(self):
        names = {"agent_helper_route_body_limit", "is_agent_helper_route"}
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        nodes.extend(deepcopy(node) for node in TREE.body if
            isinstance(node, ast.FunctionDef) and node.name in names or
            isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
            and node.target.id == "AGENT_HELPER_ROUTE_RULES")
        namespace = {"re": re}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     "<isolated-mailbox-entry-points>", "exec"), namespace)
        limit = namespace["agent_helper_route_body_limit"]
        self.assertEqual(limit("GET", "/api/agent/cross-chat/inbox"), 0)
        self.assertEqual(limit("POST", "/api/agent/cross-chat/inbox/read"), 16 * 1024)
        for method, path in (("POST", "/api/agent/cross-chat/inbox"),
                             ("GET", "/api/agent/cross-chat/inbox/read"),
                             ("POST", "/api/agent/cross-chat/inbox/read/extra"),
                             ("POST", "/api/agent/cross-chat/future")):
            self.assertIsNone(limit(method, path))

    async def asyncSetUp(self):
        self.ns = isolated_source()
        temporary = tempfile.TemporaryDirectory(prefix="isolated-mailbox-")
        self.addCleanup(temporary.cleanup)
        self.ledger = self.ns["Ledger"](Path(temporary.name) / "cross-chat.sqlite3")
        await self.ledger.initialize()
        self.source = {"route_id": ROUTE, "pair_id": PAIR, "target_session_id": "recipient", "actions": ["instruction"]}
        self.reverse = {"route_id": RETURN_ROUTE, "pair_id": PAIR, "target_session_id": "sender", "actions": ["instruction"]}
        self.routes = {"sender": [self.source], "recipient": [self.reverse]}
        self.capabilities = {
            "sender": {"source_run_id": "sender-run", "async_route_v1": True, "provider_route_grants": {ROUTE: self.source}},
            "recipient": {"source_run_id": "recipient-run", "async_route_v1": True, "provider_route_grants": {RETURN_ROUTE: self.reverse}},
        }
        self.ns.update({
            "CROSS_CHAT": self.ledger,
            "STORE": SimpleNamespace(_lock=asyncio.Lock(), sessions={
                "sender": {"title": "Synthetic sender"},
                "recipient": {"title": "Synthetic recipient", "codex_goal": {"status": "active", "objective": "Synthetic work"}},
            }),
            "ACTIVE": {"sender": {"run_id": "sender-run"}},
            "CURRENT_TURNS": {"sender": {"run_id": "sender-run"}},
            "BUSY_SESSIONS": {"sender"}, "STOPPED_RUNS": set(), "DELETING_SESSIONS": set(),
            "QUEUED_TURNS": {}, "CHAT_MAILBOX_PENDING": set(),
            "session_lifecycle_lock": self.lifecycle_lock,
            "provider_route_capability_source": AsyncMock(side_effect=lambda request: request.owner),
            "authorize_provider_action": AsyncMock(side_effect=lambda request, **kw: deepcopy(self.capabilities[request.owner])),
            "provider_cross_chat_routes": lambda row: self.routes["recipient" if row["title"] == "Synthetic recipient" else "sender"],
            "live_provider_cross_chat_route": lambda owner, issued: next((dict(route) for route in self.routes[owner] if route == issued), None),
            "provider_cross_chat_route_projection": lambda owner, route: dict(route),
            "provider_cross_chat_route_availability": lambda *_args: (True, None),
            "cross_chat_delivery_client_capabilities": lambda *_args: [],
            "provider_cross_chat_route_body_exceeds_limit": lambda body: len(body) > 16000,
            "prime_cross_chat_event_cache": Mock(), "append_cross_chat_event_once": AsyncMock(),
            "generic_provider_route_delivery_error": lambda: HTTPException(409, "delivery failed"),
            "join_task_despite_caller_cancellation": lambda task: task,
        })
        self.traps = {}
        for name in ("submit_cross_chat_delivery", "submit_cross_chat_exchange_leg", "reserve_provider_route_handoff",
                     "_start_turn_locked", "start_turn", "queue_turn", "_run_queued_turn_now", "stop_turn", "pause_codex_goal"):
            self.traps[name] = AsyncMock(side_effect=AssertionError("Mailbox must not execute recipient work"))
            self.ns[name] = self.traps[name]
        self.ledger.create_route_exchange_request = self.traps["submit_cross_chat_exchange_leg"]

    @asynccontextmanager
    async def lifecycle_lock(self, *_args):
        yield

    def set_recipient(self, kind="goal"):
        self.ns["ACTIVE"].pop("recipient", None)
        self.ns["CURRENT_TURNS"].pop("recipient", None)
        self.ns["BUSY_SESSIONS"].discard("recipient")
        if kind != "idle":
            self.ns["ACTIVE"]["recipient"] = {"run_id": "recipient-run", "transport": "app-server",
                "provider_thread_id": "synthetic-thread", "provider_turn_id": "synthetic-turn"}
            self.ns["CURRENT_TURNS"]["recipient"] = {"run_id": "recipient-run"}
            self.ns["BUSY_SESSIONS"].add("recipient")
        self.ns["STORE"].sessions["recipient"]["codex_goal"]["status"] = "active" if kind == "goal" else "paused"

    def work_snapshot(self):
        return deepcopy((self.ns["STORE"].sessions, self.ns["ACTIVE"], self.ns["CURRENT_TURNS"],
                         self.ns["BUSY_SESSIONS"], self.ns["QUEUED_TURNS"], self.ns["STOPPED_RUNS"]))

    async def send(self, key="message-one"):
        req = SimpleNamespace(mode="async_route_v1", action="instruction", artifact_grants=[],
                              body="Exact synthetic peer message.", idempotency_key=key,
                              wait_for_response=False, response_timeout_seconds=None, reply_to_message_id=None)
        return await self.ns["submit_provider_route_handoff"](ROUTE, req, SimpleNamespace(owner="sender"))

    async def read(self, request_id="stable-read-request"):
        req = SimpleNamespace(source_session_id="sender", request_id=request_id, after_seq=0, limit=25)
        return await self.ns["read_provider_chat_mailbox"](req, SimpleNamespace(owner="recipient"))

    def assert_no_execution(self):
        for trap in self.traps.values():
            trap.assert_not_awaited()

    async def test_actual_send_is_passive_for_idle_busy_and_goal_recipients(self):
        for kind in ("idle", "busy", "goal"):
            with self.subTest(recipient=kind):
                self.set_recipient(kind)
                before = self.work_snapshot()
                receipt = await self.send("message-" + kind)
                self.assertEqual((receipt["state"], receipt["delivery_mode"], receipt["execution_started"]), ("unread", "mailbox", False))
                record = await self.ledger.get(receipt["message_id"])
                self.assertEqual((record["status"], record["queued_id"], record["target_run_id"]), ("stored", None, None))
                self.assertEqual(self.work_snapshot(), before)
                self.assertIn("recipient", self.ns["CHAT_MAILBOX_PENDING"])
        self.assert_no_execution()

    async def test_actual_duplicate_send_has_one_sqlite_effect(self):
        self.set_recipient()
        first, repeated = await self.send(), await self.send()
        self.assertFalse(first["duplicate"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(first["message_id"], repeated["message_id"])
        with self.ledger._transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cross_chat_envelopes").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM chat_mailbox_messages").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM cross_chat_exchanges").fetchone()[0], 0)
        self.assert_no_execution()

    async def test_actual_read_preserves_goal_owner_and_replays_same_claim(self):
        self.set_recipient()
        receipt = await self.send()
        before = self.work_snapshot()
        first, replay = await self.read(), await self.read()
        self.assertEqual(first["messages"], replay["messages"])
        self.assertEqual(first["messages"][0]["message_id"], receipt["message_id"])
        self.assertEqual(first["messages"][0]["body"], "Exact synthetic peer message.")
        self.assertEqual(first["reply_routes"][0]["route_id"], RETURN_ROUTE)
        self.assertFalse(first["automatic_reply"])
        self.assertEqual(self.work_snapshot(), before)
        self.assertNotIn("recipient", self.ns["CHAT_MAILBOX_PENDING"])
        self.assert_no_execution()
        self.assertTrue(all(call.kwargs["action"] == "agent_cross_chat_routes"
                            for call in self.ns["authorize_provider_action"].call_args_list))

    async def test_pending_hint_coalesces_until_drained_or_new_logical_owner(self):
        self.set_recipient()
        await self.send()
        take = self.ns["take_chat_mailbox_hint"]
        text = take("recipient", "recipient-run")
        self.assertIsInstance(text, str)
        self.assertNotIn("Exact synthetic peer message", text)
        self.assertLess(len(text.encode()), 2048)
        self.assertIsNone(take("recipient", "recipient-run"))
        await self.send("message-two")
        self.assertIsNone(take("recipient", "recipient-run"))
        await self.read()
        await self.send("message-three")
        self.assertIsInstance(take("recipient", "recipient-run"), str)
        self.ns["ACTIVE"]["recipient"] = {"run_id": "new-run"}
        self.ns["CURRENT_TURNS"]["recipient"] = {"run_id": "new-run"}
        self.assertIsNone(take("recipient", "recipient-run"))
        self.assertIsInstance(take("recipient", "new-run"), str)
        self.assert_no_execution()

    async def test_read_publication_failure_and_send_replay_preserve_current_mailbox_state(self):
        self.set_recipient()
        first = await self.send()
        before = self.work_snapshot()
        publication = self.ns["append_cross_chat_event_once"]
        def fail_read(_owner, _record, kind, *_args, **_kwargs):
            if kind == "chat_conversation_message_read":
                raise OSError("Synthetic event publication failure")
        publication.side_effect = fail_read
        with self.assertRaises(OSError):
            await self.read()
        pending = await self.ledger.mailbox_call("pending_read_events")
        self.assertEqual([row["message_id"] for row in pending], [first["message_id"]])
        publication.side_effect = None
        replay = await self.read()
        self.assertEqual([row["message_id"] for row in replay["messages"]], [first["message_id"]])
        self.assertEqual(await self.ledger.mailbox_call("pending_read_events"), [])
        repeated = await self.send()
        self.assertEqual((repeated["message_id"], repeated["state"], repeated["duplicate"]),
                         (first["message_id"], "read", True))
        await self.ns["delete_chat_mailbox_message"]("recipient", first["message_id"])
        deleted = await self.send()
        self.assertEqual((deleted["message_id"], deleted["state"], deleted["execution_started"]),
                         (first["message_id"], "deleted", False))
        self.assertTrue(deleted["duplicate"])
        self.assertEqual((await self.read())["messages"], [])
        self.assertEqual(self.work_snapshot(), before)
        self.assert_no_execution()

    async def test_stale_mailbox_publication_cannot_resurrect_cancelled_message(self):
        self.set_recipient()
        first = await self.send()
        record = await self.ledger.get(first["message_id"])
        await self.ledger.mailbox_call("cancel_message", first["message_id"], now=NOW)
        publication = self.ns["append_cross_chat_event_once"]
        publication.reset_mock()
        state = await self.ns["publish_chat_mailbox_message"](record)
        self.assertEqual(state, "cancelled")
        self.assertEqual({call.args[2] for call in publication.call_args_list}, {"chat_conversation_message_cancelled"})
        self.assertEqual((await self.read())["messages"], [])
        self.assertEqual((await self.ledger.get(first["message_id"]))["status"], "cancelled")
        self.assert_no_execution()

    async def test_native_goal_hint_uses_native_owner_without_ordinary_transport_turn(self):
        self.set_recipient()
        await self.send()
        active = self.ns["ACTIVE"]["recipient"]
        active.update(purpose="codex_goal_resume", codex_native_operation=True,
                      codex_native_operation_kind="goal_resume", codex_control_reservation_id="synthetic-reservation",
                      provider_turn_ready=True, codex_goal_handoff_closed=False)
        self.ns["CURRENT_TURNS"]["recipient"].update(purpose="codex_goal_resume",
            codex_control_reservation_id="synthetic-reservation")
        generation = 7
        manager = SimpleNamespace(ready=True, generation=generation, active_turn=lambda _thread: None)
        async def inject(thread, items, *, expected_generation, before_send, timeout):
            self.assertEqual(thread, "synthetic-thread")
            self.assertEqual(expected_generation, generation)
            self.assertTrue(before_send())
            self.assertEqual(items[0]["role"], "developer")
            self.assertNotIn("Exact synthetic peer message", items[0]["content"][0]["text"])
        manager.inject_items_guarded = AsyncMock(side_effect=inject)
        self.ns["CODEX_APP_SERVER_MANAGER"] = manager
        await self.ns["maybe_notify_chat_mailbox_codex"]("recipient")
        manager.inject_items_guarded.assert_awaited_once()
        await self.ns["maybe_notify_chat_mailbox_codex"]("recipient")
        manager.inject_items_guarded.assert_awaited_once()
        self.assertEqual(self.ns["STORE"].sessions["recipient"]["codex_goal"]["status"], "active")
        self.assertIs(self.ns["ACTIVE"]["recipient"], active)
        self.assert_no_execution()
        guard = manager.inject_items_guarded.call_args.kwargs["before_send"]
        for patch in ({"provider_turn_ready": False}, {"stop_requested": True},
                      {"codex_goal_handoff_closed": True}, {"codex_control_reservation_id": "different"}):
            saved = dict(active)
            active.update(patch)
            self.assertFalse(guard())
            active.clear()
            active.update(saved)
        self.ns["STORE"].sessions["recipient"]["codex_goal"]["status"] = "paused"
        self.assertFalse(guard())


if __name__ == "__main__":
    unittest.main()
