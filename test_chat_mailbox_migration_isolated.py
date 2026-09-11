"""Startup backlog migration on synthetic SQLite/events; never import the server."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from test_chat_mailbox_runtime_isolated import isolated_source, TREE, PAIR, ROUTE, NOW


def migration_source():
    ns = isolated_source()
    names = {"queued_event_lines", "record_mailbox_migration_evidence", "scan_queued_turns_from_events",
             "migrate_unstarted_chat_mailbox_backlog", "recover_queued_turns_after_start", "is_async_route_message"}
    ledger = next(node for node in TREE.body if isinstance(node, ast.ClassDef) and node.name == "CrossChatStore")
    methods = {"pending_mailbox_migration_candidates", "migrate_pending_mailbox_message"}
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    nodes += [deepcopy(node) for node in TREE.body
              if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    nodes += [deepcopy(node) for node in ledger.body
              if isinstance(node, ast.AsyncFunctionDef) and node.name in methods]
    ns.update(json=json, deque=deque)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated-mailbox-migration>", "exec"), ns)
    for name in methods:
        setattr(ns["Ledger"], name, ns[name])
    return ns


class ChatMailboxMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mailbox-migration-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ns = migration_source()
        self.ledger = self.ns["Ledger"](self.root / "ledger.sqlite")
        await self.ledger.initialize()
        self.ns.update({
            "CROSS_CHAT": self.ledger,
            "STORE": SimpleNamespace(_lock=asyncio.Lock(), sessions={"sender": {}, "recipient": {}}),
            "DELETING_SESSIONS": set(), "QUEUE_LOCK": asyncio.Lock(), "QUEUED_TURNS": {},
            "events_path": lambda sid: self.root / (sid + ".jsonl"),
            "queued_turn_from_event": lambda event, _sess, _position: dict(event),
            "provider_cross_chat_delivery_pair_is_live": lambda record: record["authorization_pair_id"] == PAIR,
            "append_durable_event": AsyncMock(), "schedule_next_queued_turn": Mock(),
            "bind_recovered_cross_chat_queue_item": AsyncMock(side_effect=lambda _sid, item: item),
            "append_cross_chat_terminal_lifecycle": AsyncMock(),
            "CrossChatStore": self.ns["Ledger"],
        })
        (self.root / "sender.jsonl").write_text("")
        (self.root / "recipient.jsonl").write_text("")

    async def envelope(self, name="message", status="queued", **overrides):
        record, _ = await self.ledger.create_instruction(
            envelope_id=name, source_session_id="sender", source_run_id="source-run-" + name,
            target_session_id="recipient", body="Original synthetic body", idempotency_key=name,
            authorization_kind="configured_route", authorization_route_id=ROUTE,
            authorization_pair_id=PAIR,
        )
        values = {"status": status, "queued_id": "queue-" + name if status == "queued" else None, **overrides}
        with self.ledger._transaction() as connection:
            connection.execute("UPDATE cross_chat_envelopes SET " + ",".join(key + "=?" for key in values)
                               + " WHERE id=?", (*values.values(), name))
        return await self.ledger.get(name)

    def queue_event(self, record, **overrides):
        return {"type": "turn_queued", "queued_id": record["queued_id"],
                "cross_chat_envelope_id": record["id"], "purpose": "cross_chat_handoff_delivery",
                "prompt": "Synthetic delivery", **overrides}

    def events(self, records):
        (self.root / "recipient.jsonl").write_text("".join(json.dumps(event) + "\n" for event in records))

    async def test_exact_pending_and_ready_become_mail_before_admission_preserving_edits(self):
        queued = await self.envelope(target_body="Recipient edit", message_revision=2)
        await self.envelope("ready", status="ready")
        self.events([self.queue_event(queued)])
        self.assertEqual(await self.ns["recover_queued_turns_after_start"](), (0, 0))
        for name in ("message", "ready"):
            record = await self.ledger.get(name)
            self.assertEqual((record["status"], record["delivery_mode"], record["created_at"]), ("stored", "mailbox", NOW))
        mailbox = await self.ledger.mailbox_call("list_messages", "recipient", None, [PAIR])
        self.assertEqual(mailbox["messages"][0]["body"], "Recipient edit")
        self.assertEqual(mailbox["messages"][0]["message_revision"], 2)
        self.assertEqual(mailbox["messages"][0]["stored_at"], NOW)
        self.assertEqual((await self.ledger.get("message"))["body"], "Original synthetic body")
        self.ns["schedule_next_queued_turn"].assert_not_called()
        self.ns["bind_recovered_cross_chat_queue_item"].assert_not_awaited()
        self.assertEqual(self.ns["append_durable_event"].await_args.args[1], "turn_unqueued")

    async def test_restart_after_commit_before_queue_removal_never_discards_or_executes_mail(self):
        record = await self.envelope()
        await self.ledger.migrate_pending_mailbox_message(record)
        self.assertEqual((await self.ledger.get("message"))["lifecycle_status"], "mailbox_migration_pending")
        self.events([self.queue_event(record)])
        for _ in range(2):
            self.assertEqual(await self.ns["recover_queued_turns_after_start"](), (0, 0))
            self.assertEqual((await self.ledger.get("message"))["status"], "stored")
            self.assertEqual((await self.ledger.get("message"))["lifecycle_status"], "mailbox_migration_pending")
        self.ns["append_cross_chat_terminal_lifecycle"].assert_not_awaited()
        self.ns["schedule_next_queued_turn"].assert_not_called()

    async def test_started_without_queue_id_fenced_legacy_and_secure_owners_are_unchanged(self):
        started = await self.envelope("started")
        fenced = await self.envelope("fenced")
        legacy = await self.envelope("legacy", authorization_pair_id="")
        secure = await self.envelope("secure")
        running = await self.envelope("running", status="running", target_run_id="owned-run")
        self.events([
            {"type": "turn_started", "cross_chat_envelope_id": started["id"], "run_id": "older-native-run"},
            self.queue_event(started), self.queue_event(fenced),
            self.queue_event(fenced, type="turn_queue_delivery_fenced"),
            self.queue_event(legacy), self.queue_event(secure, secure_peer_envelope_id="secure-envelope"),
        ])
        candidates = await self.ledger.pending_mailbox_migration_candidates()
        proof = {record["id"]: {"target_session_id": "recipient"} for record in candidates}
        recovered = self.ns["scan_queued_turns_from_events"]([("recipient", {})], mailbox_evidence=proof)
        self.assertTrue(proof["started"]["started_or_terminal"])
        self.assertEqual(await self.ns["migrate_unstarted_chat_mailbox_backlog"](candidates, proof, recovered), 0)
        for record in (started, fenced, legacy, secure, running):
            self.assertEqual((await self.ledger.get(record["id"]))["status"], record["status"])

    async def test_incomplete_proof_revoked_pair_and_changed_owner_do_not_migrate(self):
        record = await self.envelope()
        self.events([self.queue_event(record)])
        candidates = await self.ledger.pending_mailbox_migration_candidates()
        proof = {record["id"]: {"target_session_id": "recipient"}}
        recovered = self.ns["scan_queued_turns_from_events"]([("recipient", {})], mailbox_evidence=proof)
        proof[record["id"]]["complete"] = False
        self.assertEqual(await self.ns["migrate_unstarted_chat_mailbox_backlog"](candidates, proof, recovered), 0)
        proof[record["id"]]["complete"] = True
        self.ns["provider_cross_chat_delivery_pair_is_live"] = lambda _: False
        self.assertEqual(await self.ns["migrate_unstarted_chat_mailbox_backlog"](candidates, proof, recovered), 0)
        with self.ledger._transaction() as connection:
            connection.execute("UPDATE cross_chat_envelopes SET status='running',target_run_id='native-owner' WHERE id=?", (record["id"],))
        self.assertIsNone(await self.ledger.migrate_pending_mailbox_message(record))

    async def test_partial_event_line_and_uninitialized_ledger_are_not_migration_proof(self):
        unopened = self.ns["Ledger"](self.root / "not-opened.sqlite")
        self.assertEqual(await unopened.pending_mailbox_migration_candidates(), [])
        self.assertFalse(unopened.path.exists())
        record = await self.envelope()
        path = self.root / "recipient.jsonl"
        path.write_text(json.dumps(self.queue_event(record)))
        proof = {record["id"]: {"target_session_id": "recipient"}}
        recovered = self.ns["scan_queued_turns_from_events"]([("recipient", {})], mailbox_evidence=proof)
        self.assertFalse(proof[record["id"]]["complete"])
        self.assertEqual(await self.ns["migrate_unstarted_chat_mailbox_backlog"]([record], proof, recovered), 0)


if __name__ == "__main__":
    unittest.main()
