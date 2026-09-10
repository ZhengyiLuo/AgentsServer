"""Isolated queue-reorder contract tests; never import the server runtime."""
from __future__ import annotations

import ast
import asyncio
from collections import deque
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from pydantic import BaseModel, Field


SOURCE = Path(__file__).with_name("agent_server.py")
DEFINITIONS = {
    "MoveQueuedTurnRequest", "queue_positions", "reject_promoted_queue_mutation", "move_queued_turn",
}


def load_queue_reorder():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [node for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in DEFINITIONS]
    if {node.name for node in selected} != DEFINITIONS:
        raise AssertionError("The isolated queue-reorder definition allowlist is incomplete")
    for node in selected:
        if node.decorator_list:
            raise AssertionError("Queue-reorder helpers must not execute module decorators")
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    namespace = {"Any": Any, "BaseModel": BaseModel, "Field": Field,
                 "HTTPException": HTTPException, "asyncio": asyncio, "deque": deque}
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class PendingQueueReorderTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.loaded = load_queue_reorder()

    def setUp(self):
        self.ns = self.loaded
        self.session = "queue-chat"
        self.recovery = AsyncMock()
        self.journal = AsyncMock()
        self.update_blocker = Mock(return_value=None)
        self.cross_chat = SimpleNamespace(update=AsyncMock(), update_exchange=AsyncMock())
        self.force_send = AsyncMock()
        self.delivery_effect = AsyncMock()
        self.run_now = {"other-chat": {"queued_id": "other-promoted"}}
        self.current = {self.session: {"run_id": "running-provider-turn"}}
        self.active = {self.session: object()}
        self.ns.update({
            "STORE": SimpleNamespace(sessions={self.session: {"id": self.session}}),
            "QUEUE_LOCK": asyncio.Lock(), "QUEUED_TURNS": {},
            "RUN_NOW_TURNS": self.run_now, "CURRENT_TURNS": self.current, "ACTIVE": self.active,
            "CROSS_CHAT_DELIVERY_PURPOSES": {"cross_chat_handoff_delivery", "secure_peer_handoff_delivery"},
            "wait_for_queue_recovery_admission": self.recovery,
            "managed_server_update_blocker": self.update_blocker,
            "append_durable_event": self.journal,
            "CROSS_CHAT": self.cross_chat,
            "force_send_queued_turn": self.force_send,
            "append_cross_chat_terminal_lifecycle": self.delivery_effect,
            "append_cross_chat_exchange_terminal_lifecycle": self.delivery_effect,
        })

    def tearDown(self):
        self.force_send.assert_not_called()
        self.delivery_effect.assert_not_called()
        self.cross_chat.update.assert_not_called()
        self.cross_chat.update_exchange.assert_not_called()
        self.assertEqual(self.current, {self.session: {"run_id": "running-provider-turn"}})
        self.assertEqual(set(self.active), {self.session})

    @staticmethod
    def row(queued_id, purpose="user"):
        return {"queued_id": queued_id, "purpose": purpose, "prompt": "Keep literal " + queued_id,
                "file_ids": ["file-" + queued_id], "chat_references": [{"target_session_id": "other"}],
                "cross_chat_envelope_id": "envelope-" + queued_id,
                "secure_peer_envelope_id": "peer-envelope-" + queued_id, "job_id": "job-" + queued_id}

    def install(self, rows):
        self.ns["QUEUED_TURNS"][self.session] = deque(rows)

    def ids(self):
        return [item["queued_id"] for item in self.ns["QUEUED_TURNS"][self.session]]

    async def move(self, queued_id, direction, expected=None):
        request = self.ns["MoveQueuedTurnRequest"](direction=direction, expected_adjacent_queued_id=expected)
        return await self.ns["move_queued_turn"](self.session, queued_id, request)

    async def test_all_pending_kinds_can_swap_with_each_other_without_changing_payload(self):
        purposes = ("user", "cross_chat_handoff_delivery", "secure_peer_handoff_delivery", "scheduled_job")
        for left in purposes:
            for right in purposes:
                with self.subTest(left=left, right=right):
                    first, second = self.row("first", left), self.row("second", right)
                    original = deepcopy([first, second])
                    self.install([first, second])
                    self.journal.reset_mock()
                    response = await self.move("second", "up", "first")
                    self.assertEqual(self.ids(), ["second", "first"])
                    self.assertEqual([first, second], original)
                    self.assertIs(self.ns["QUEUED_TURNS"][self.session][0], second)
                    positions = [{"queued_id": "second", "position": 1}, {"queued_id": "first", "position": 2}]
                    self.assertEqual(response, {"ok": True, "queued_id": "second", "positions": positions})
                    self.journal.assert_awaited_once_with(self.session, "turn_queue_reordered", {
                        "queued_id": "second", "direction": "up", "positions": positions,
                    })

    async def test_direction_down_with_expected_adjacent_identity(self):
        self.install([self.row("first", "scheduled_job"), self.row("second", "cross_chat_handoff_delivery")])
        await self.move("first", " DOWN ", "second")
        self.assertEqual(self.ids(), ["second", "first"])

    async def test_legacy_requests_keep_delivery_fence_but_can_reorder_normal_messages(self):
        for purpose in ("cross_chat_handoff_delivery", "secure_peer_handoff_delivery", "scheduled_job"):
            for rows in ([self.row("first", purpose), self.row("second")],
                         [self.row("first"), self.row("second", purpose)]):
                with self.subTest(purpose=purpose, selected=rows[0]["purpose"]):
                    self.install(rows)
                    with self.assertRaises(HTTPException) as raised:
                        await self.move("first", "down")
                    self.assertEqual(raised.exception.status_code, 409)
                    self.assertEqual(self.ids(), ["first", "second"])
                    self.journal.assert_not_called()
        self.install([self.row("first"), self.row("second")])
        await self.move("first", "down")
        self.assertEqual(self.ids(), ["second", "first"])

    async def test_stale_adjacent_identity_rejects_without_mutation_or_journal(self):
        rows = [self.row("first"), self.row("second"), self.row("third")]
        self.install(rows)
        before = self.ns["QUEUED_TURNS"][self.session]
        with self.assertRaises(HTTPException) as raised:
            await self.move("second", "up", "third")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIs(self.ns["QUEUED_TURNS"][self.session], before)
        self.assertEqual(list(before), rows)
        self.journal.assert_not_called()

    async def test_edge_with_stale_expected_neighbor_is_not_silently_accepted(self):
        self.install([self.row("first"), self.row("second")])
        with self.assertRaises(HTTPException) as raised:
            await self.move("first", "up", "no-longer-pending")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.ids(), ["first", "second"])
        self.journal.assert_not_called()

    async def test_promoted_row_is_immutable_and_never_swaps_back_into_pending(self):
        self.install([self.row("pending")])
        promoted = self.row("promoted", "scheduled_job")
        self.run_now[self.session] = promoted
        for direction in ("up", "down"):
            with self.subTest(direction=direction), self.assertRaises(HTTPException) as raised:
                await self.move("promoted", direction, "pending")
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(raised.exception.detail["code"], "queued_turn_already_promoted")
        self.assertIs(self.run_now[self.session], promoted)
        self.assertEqual(self.ids(), ["pending"])
        self.journal.assert_not_called()

    async def test_pending_edge_does_not_touch_an_independent_promoted_row(self):
        self.install([self.row("pending")])
        promoted = self.row("promoted")
        self.run_now[self.session] = promoted
        await self.move("pending", "up")
        self.assertEqual(self.ids(), ["pending"])
        self.assertIs(self.run_now[self.session], promoted)

    async def test_promoted_adjacent_identity_cannot_be_crossed_even_if_still_in_pending(self):
        promoted = self.row("promoted")
        self.install([promoted, self.row("pending")])
        self.run_now[self.session] = promoted
        with self.assertRaises(HTTPException) as raised:
            await self.move("pending", "up", "promoted")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "queued_turn_already_promoted")
        self.assertEqual(self.ids(), ["promoted", "pending"])
        self.assertIs(self.run_now[self.session], promoted)
        self.journal.assert_not_called()

    async def test_durable_append_oserror_restores_original_order_and_payload(self):
        rows = [self.row("first", "cross_chat_handoff_delivery"), self.row("second", "scheduled_job")]
        original = deepcopy(rows)
        self.install(rows)
        self.journal.side_effect = OSError("isolated journal failure")
        with self.assertRaises(OSError):
            await self.move("second", "up", "first")
        self.assertEqual(list(self.ns["QUEUED_TURNS"][self.session]), original)
        self.assertIs(self.ns["QUEUED_TURNS"][self.session][0], rows[0])
        self.assertFalse(self.ns["QUEUE_LOCK"].locked())

    async def test_cancellation_during_durable_append_rolls_back_and_releases_lock(self):
        rows = [self.row("first"), self.row("second", "secure_peer_handoff_delivery")]
        self.install(rows)
        entered = asyncio.Event()
        hold = asyncio.Event()
        async def append(*_args):
            entered.set()
            await hold.wait()
        self.journal.side_effect = append
        task = asyncio.create_task(self.move("second", "up", "first"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            hold.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(list(self.ns["QUEUED_TURNS"][self.session]), rows)
        self.assertFalse(self.ns["QUEUE_LOCK"].locked())

    async def test_simultaneous_reorders_serialize_through_durable_commit(self):
        self.install([self.row("first"), self.row("second", "scheduled_job"), self.row("third", "cross_chat_handoff_delivery")])
        entered = asyncio.Event()
        release = asyncio.Event()
        second_started = asyncio.Event()
        commits = []
        async def append(_session, _kind, data):
            if not commits:
                entered.set()
                await release.wait()
            commits.append(deepcopy(data))
        self.journal.side_effect = append
        first = asyncio.create_task(self.move("second", "up", "first"))
        async def second_move():
            second_started.set()
            return await self.move("third", "up", "first")
        second = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            second = asyncio.create_task(second_move())
            await asyncio.wait_for(second_started.wait(), timeout=1)
            self.assertFalse(second.done())
            self.assertEqual(self.journal.await_count, 1)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
        finally:
            release.set()
            tasks = [task for task in (first, second) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(self.ids(), ["second", "third", "first"])
        self.assertEqual([[position["queued_id"] for position in event["positions"]] for event in commits],
                         [["second", "first", "third"], ["second", "third", "first"]])

    async def test_serialized_second_move_rechecks_stale_neighbor_after_first_commit(self):
        self.install([self.row("first"), self.row("second"), self.row("third")])
        await self.move("second", "up", "first")
        with self.assertRaises(HTTPException) as raised:
            await self.move("third", "up", "second")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.ids(), ["second", "first", "third"])
        self.journal.assert_awaited_once()

    async def test_missing_queue_direction_and_update_admission_fail_without_effects(self):
        self.install([self.row("first")])
        for queued_id, direction, status in (("missing", "up", 404), ("first", "sideways", 400)):
            with self.subTest(queued_id=queued_id), self.assertRaises(HTTPException) as raised:
                await self.move(queued_id, direction)
            self.assertEqual(raised.exception.status_code, status)
        self.update_blocker.return_value = "Update admission closed"
        with self.assertRaises(HTTPException) as raised:
            await self.move("first", "up")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(self.ids(), ["first"])
        self.journal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
