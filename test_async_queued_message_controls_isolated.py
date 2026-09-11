"""Real queue helpers and temporary SQLite only; no server/provider startup."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from contextlib import contextmanager, suppress
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_async_route_transport_isolated import HTTPException, envelope, PAIR


SOURCE = Path(__file__).with_name("agent_server.py")
NAMES = {
    "CrossChatStore", "is_async_route_message", "async_route_conversation_fields",
    "async_route_queue_fields", "async_message_target_fields", "async_queued_message_record",
    "async_queued_message_body_fields", "update_async_queued_message", "public_queued_turn",
    "queued_turns_snapshot", "cross_chat_lifecycle_fields", "public_cross_chat_envelope",
    "prepare_steered_turn", "cross_chat_delivery_prompt", "cross_chat_delivery_header",
    "cross_chat_delivery_origin", "cross_chat_provider_prompt_kind", "cross_chat_relay_content_prompt",
    "_run_queued_turn_now_once", "queued_turn_run_metadata",
    "join_task_despite_caller_cancellation",
}


def namespace():
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in ast.parse(SOURCE.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in NAMES:
            node = deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
    ns = {
        "asyncio": asyncio, "deque": deque, "contextmanager": contextmanager, "suppress": suppress,
        "hashlib": hashlib, "sqlite3": sqlite3, "threading": threading, "time": time,
        "HTTPException": HTTPException, "now_iso": lambda: "2026-09-10T20:00:00Z",
        "PROVIDER_CROSS_CHAT_LEGACY_RATE_RETENTION_SECONDS": 3600,
        "PROVIDER_CROSS_CHAT_ROUTE_PAIR_ID_RE": re.compile(r"pair_[0-9a-f]{32}"),
        "CROSS_CHAT_HANDOFF_BODY_MAX_CHARS": 100000,
        "LOCAL_CROSS_CHAT_DELIVERY_PURPOSE": "cross_chat_handoff_delivery",
        "SECURE_PEER_DELIVERY_PURPOSE": "secure_peer_handoff_delivery",
        "CROSS_CHAT_DELIVERY_PURPOSES": {"cross_chat_handoff_delivery", "secure_peer_handoff_delivery"},
        "STORE": SimpleNamespace(sessions={"a": {"title": "Sender"}, "b": {"title": "Recipient", "backend": "claude"}}),
        "sanitized_provider_route_label": lambda value: str(value or "Untitled chat"),
        "cross_chat_counterpart_label": lambda value, **kwargs: str(value),
        "QUEUE_LOCK": asyncio.Lock(), "ACTIVE_LOCK": asyncio.Lock(),
        "QUEUED_TURNS": {}, "RUN_NOW_TURNS": {}, "ACTIVE": {}, "CURRENT_TURNS": {},
        "STEERING_SESSIONS": set(), "STEERING_WAIT_TASKS": {}, "BUSY_SESSIONS": set(),
        "DEFAULT_BACKEND": "claude", "BACKEND_CLAUDE": "claude", "BACKEND_CODEX": "codex",
        "CODEX_TRANSPORT_APP_SERVER": "app_server", "CLAUDE_TRANSPORT_SDK": "sdk",
        "reject_promoted_queue_mutation": Mock(), "managed_server_update_blocker": lambda: None,
        "managed_server_update_admission_blocker": lambda: None,
        "provider_cross_chat_delivery_pair_is_live": lambda record: True,
        "wait_for_queue_recovery_admission": AsyncMock(), "append_durable_event": AsyncMock(),
        "stop_cleanup_in_progress": lambda sid: False, "codex_goal_followup_requires_native": lambda *args: False,
        "force_send_conflict_detail": lambda sid, qid, **kwargs: kwargs,
        "stop_turn": AsyncMock(return_value={"stopped": False}),
        "schedule_steered_turn_slot_waiter": Mock(), "schedule_next_queued_turn": Mock(),
        "normalize_steering_lineage": lambda value: value or [],
        "normalized_provider_cross_chat_route_snapshot": lambda value: value or [],
        "team_mail_grants": SimpleNamespace(snapshot=lambda value: value or []),
        "turn_steering_lineage": lambda value: [],
        "join_task_despite_caller_cancellation": lambda task: task,
        "NonNativeForceSendRequiresLifecycleLock": type("NeedsLock", (Exception,), {}),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "<async-queue-source>", "exec"), ns)
    return ns


async def setup_fixture(directory):
    ns = namespace()
    store = ns["CrossChatStore"](Path(directory) / "cross-chat.sqlite")
    await store.initialize()
    record = envelope(status="queued", queued_id="queued_message", queue_position=2, created_at="2026-09-10T20:00:00Z", updated_at="2026-09-10T20:00:00Z")
    with store._transaction() as connection:
        connection.execute(
            """INSERT INTO cross_chat_envelopes
               (id,kind,source_session_id,source_run_id,target_session_id,action,body,idempotency_key,
                authorization_kind,authorization_route_id,authorization_pair_id,status,queued_id,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            tuple(record[key] for key in ("id", "kind", "source_session_id", "source_run_id", "target_session_id", "action", "body"))
            + ("qa-message",) + tuple(record[key] for key in ("authorization_kind", "authorization_route_id", "authorization_pair_id", "status", "queued_id", "created_at", "updated_at")),
        )
    ns["CROSS_CHAT"] = store
    row = {
        **ns["async_route_conversation_fields"](record), **ns["async_message_target_fields"](record),
        "queued_id": "queued_message", "cross_chat_envelope_id": record["id"],
        "purpose": "cross_chat_handoff_delivery", "prompt": ns["cross_chat_delivery_prompt"](record, "Sender"),
        "display_prompt": "Prepared message", "created_at": record["created_at"], "_durable": True,
    }
    ns["QUEUED_TURNS"]["b"] = deque([row])
    return ns, record, row


def request(body="Recipient revised body", revision=0, **overrides):
    return SimpleNamespace(prompt=body, expected_message_revision=revision, file_ids=None,
                           client_capabilities=None, chat_references=None, team_references=None, **overrides)


async def public_fixture():
    with tempfile.TemporaryDirectory(prefix="async-queue-qa-") as directory:
        ns, record, row = await setup_fixture(directory)
        before = ns["public_queued_turn"]("b", row, 1)
        receipt = await ns["update_async_queued_message"]("b", "queued_message", request())
        changed = await ns["CROSS_CHAT"].get(record["id"])
        return {
            "origin": "actual-source-extracted-queue-and-sqlite", "before": before,
            "editReceipt": receipt, "after": receipt["item"],
            "detail": ns["public_cross_chat_envelope"](changed, include_body=True),
            "recipientLifecycle": ns["cross_chat_lifecycle_fields"](changed, "running", session_id="b"),
            "senderLifecycle": ns["cross_chat_lifecycle_fields"](changed, "running", session_id="a"),
            "promotionReceipt": await ns["_run_queued_turn_now_once"]("b", "queued_message"),
        }


class AsyncQueuedMessageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="async-queue-test-")
        self.addCleanup(self.temp.cleanup)
        self.ns, self.record, self.row = await setup_fixture(self.temp.name)

    async def test_exact_edit_preserves_sender_and_routing_and_rejects_stale_revision(self):
        ns = self.ns
        receipt = await ns["update_async_queued_message"]("b", "queued_message", request())
        self.assertEqual(receipt["item"]["message_revision"], 1)
        saved = await ns["CROSS_CHAT"].get(self.record["id"])
        self.assertEqual(saved["body"], "Prepared message")
        self.assertEqual(saved["target_body"], "Recipient revised body")
        self.assertEqual(saved["authorization_pair_id"], PAIR)
        self.assertIn("recipient user edited", self.row["prompt"])
        self.assertNotIn("Prepared message", self.row["prompt"])
        with self.assertRaises(HTTPException) as raised:
            await ns["update_async_queued_message"]("b", "queued_message", request())
        self.assertEqual(raised.exception.status_code, 409)
        req = request(revision=1)
        req.chat_references = []
        with self.assertRaises(HTTPException) as raised:
            await ns["update_async_queued_message"]("b", "queued_message", req)
        self.assertEqual(raised.exception.status_code, 400)
        events = ns["append_durable_event"].await_args_list
        self.assertEqual([call.args[1] for call in events], ["turn_queue_updated", "chat_conversation_message_queued"])
        self.assertTrue(all(call.args[0] == "b" for call in events))

    async def test_bounded_public_preview_full_body_and_recipient_only_lifecycle(self):
        body = "Safe revised paragraph. " * 300
        await self.ns["update_async_queued_message"]("b", "queued_message", request(body))
        dto = self.ns["public_queued_turn"]("b", self.row, 1)
        self.assertEqual(dto["message_body"], body)
        self.assertEqual(len(dto["prompt"]), 4096)
        self.assertEqual(dto["display_prompt"], dto["prompt"])
        saved = await self.ns["CROSS_CHAT"].get(self.record["id"])
        for phase in ("queued", "running", "delivered"):
            target = self.ns["cross_chat_lifecycle_fields"](saved, phase, session_id="b")
            source = self.ns["cross_chat_lifecycle_fields"](saved, phase, session_id="a")
            self.assertEqual(target["handoff_preview"], body[:4096])
            self.assertEqual(target["handoff_body_chars"], len(body))
            self.assertTrue(target["handoff_body_truncated"])
            self.assertEqual(target["message_revision"], 1)
            self.assertEqual(source["handoff_preview"], "Prepared message")
            self.assertNotIn("message_edited_by_user", source)

    async def test_edit_append_failure_keeps_committed_override_and_restart_recovers_once(self):
        self.ns["append_durable_event"].side_effect = OSError("fixture disk failure")
        with self.assertRaises(OSError):
            await self.ns["update_async_queued_message"]("b", "queued_message", request())
        self.assertEqual(self.row["message_revision"], 1)
        # Simulate restoring an older queue event after that partial append.
        self.row.update(message_body="Prepared message", message_revision=0, message_edited_by_user=False)
        self.row.pop("_async_body_verified", None)
        store = self.ns["CROSS_CHAT"]
        store.get = AsyncMock(wraps=store.get)
        restored = await self.ns["queued_turns_snapshot"]("b")
        await self.ns["queued_turns_snapshot"]("b")
        self.assertEqual(restored[0]["message_body"], "Recipient revised body")
        self.assertEqual(store.get.await_count, 1)

    async def test_starting_owner_and_revocation_fail_closed(self):
        self.ns["provider_cross_chat_delivery_pair_is_live"] = lambda record: False
        with self.assertRaises(HTTPException):
            await self.ns["update_async_queued_message"]("b", "queued_message", request())
        self.ns["provider_cross_chat_delivery_pair_is_live"] = lambda record: True
        await self.ns["CROSS_CHAT"].update(self.record["id"], status="running", target_run_id="qa-started")
        with self.assertRaises(HTTPException):
            await self.ns["update_async_queued_message"]("b", "queued_message", request())
        self.assertEqual((await self.ns["CROSS_CHAT"].get(self.record["id"]))["message_revision"], 0)

    async def test_cancelled_edit_waits_for_committed_queue_reflection(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def append(*args):
            entered.set()
            await release.wait()
        self.ns["append_durable_event"].side_effect = append
        task = asyncio.create_task(self.ns["update_async_queued_message"]("b", "queued_message", request()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        self.assertTrue(self.ns["QUEUE_LOCK"].locked())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.ns["QUEUE_LOCK"].locked())
        self.assertEqual(self.row["message_revision"], 1)
        self.assertEqual((await self.ns["CROSS_CHAT"].get(self.record["id"]))["target_body"], "Recipient revised body")

    async def test_plain_send_now_overtakes_but_selecting_legacy_still_rejects(self):
        legacy = {"queued_id": "earlier_legacy", "purpose": "cross_chat_handoff_delivery", "prompt": "legacy"}
        plain = {"queued_id": "plain", "prompt": "Send now", "purpose": None}
        self.ns["QUEUED_TURNS"]["b"] = deque([legacy, plain])
        with self.assertRaises(HTTPException):
            await self.ns["_run_queued_turn_now_once"]("b", "earlier_legacy")
        result = await self.ns["_run_queued_turn_now_once"]("b", "plain")
        self.assertTrue(result["ok"])
        self.assertEqual(list(self.ns["QUEUED_TURNS"]["b"]), [legacy])

    async def test_send_now_overtakes_pending_types_without_rewriting_or_native_steering(self):
        before = [
            {"queued_id": "earlier_legacy", "purpose": "cross_chat_handoff_delivery", "prompt": "legacy"},
            {"queued_id": "earlier_peer", "purpose": "secure_peer_handoff_delivery", "prompt": "peer"},
            {"queued_id": "earlier_job", "purpose": "scheduled_job", "prompt": "job"},
        ]
        self.ns["QUEUED_TURNS"]["b"] = deque([*deepcopy(before), self.row])
        self.ns["ACTIVE"]["b"] = {"provider_turn_ready": True, "native_steer_queue": asyncio.Queue()}
        receipt = await self.ns["_run_queued_turn_now_once"]("b", "queued_message")
        self.assertTrue(receipt["ok"])
        self.assertEqual(list(self.ns["QUEUED_TURNS"]["b"]), before)
        promoted = self.ns["RUN_NOW_TURNS"]["b"]
        self.assertEqual(promoted["cross_chat_envelope_id"], self.record["id"])
        self.assertEqual(promoted["purpose"], "cross_chat_handoff_delivery")
        self.assertFalse(promoted["replays_interrupted_message"])
        self.assertEqual(self.ns["stop_turn"].await_count, 1)
        self.assertTrue(self.ns["ACTIVE"]["b"]["native_steer_queue"].empty())


if __name__ == "__main__":
    unittest.main()
