"""Summary transport tests without importing the server or opening live state."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock


class ReasoningPlaintextExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name == "codex_app_server_reasoning_plaintext")
        namespace = {"Any": Any}
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<reasoning-plaintext>", "exec"), namespace)
        cls.extract = staticmethod(namespace["codex_app_server_reasoning_plaintext"])

    def test_native_completed_content_string_array_preserves_all_sections(self):
        # Native ItemCompletedNotification / ReasoningThreadItem schema:
        # content and summary are independently exposed arrays of strings.
        self.assertEqual(self.extract({"type": "reasoning", "id": "native-completed",
            "summary": ["Short summary"], "content": ["First full section.", "Second full section."],
            "encrypted_content": "must-not-be-read"}), "First full section.\nSecond full section.")

    def test_existing_plaintext_forms_and_responses_blocks_remain_supported(self):
        for payload, expected in [
            ({"text": "Legacy text"}, "Legacy text"),
            ({"text": ["First", "Second"]}, "First\nSecond"),
            ({"content": [{"type": "reasoning_text", "text": "Responses block"}]}, "Responses block"),
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(self.extract(payload), expected)

    def test_summary_encrypted_and_non_reasoning_blocks_are_not_plaintext(self):
        self.assertEqual(self.extract({"summary": ["Summary only"], "encrypted_content": "must-not-be-read",
            "content": [{"type": "output_text", "text": "Not reasoning"},
                {"type": "reasoning_text", "text": None}, None, 42]}), "")


class ReasoningSummaryStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        names = {"reasoning_summary_stream_snapshot", "broadcast_reasoning_summary_stream",
            "flush_reasoning_summary_stream", "update_reasoning_summary_stream",
            "reasoning_summary_stream_item", "clear_reasoning_summary_stream",
            "persist_reasoning_summary", "finish_reasoning_summary_stream"}
        tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in names]
        locks = {}
        self.broadcast = AsyncMock()
        self.ns = dict(Any=Any, asyncio=asyncio, time=SimpleNamespace(monotonic=lambda: 100.0),
            REASONING_SUMMARY_STREAMS={}, REASONING_SUMMARY_STREAM_REVISION=0,
            REASONING_SUMMARY_STREAM_PENDING={}, REASONING_SUMMARY_STREAM_LAST_SENT={},
            EVENT_SEQ_CACHE={"chat": 10}, SERVER_INSTANCE_ID="owned-instance", BACKEND_CODEX="codex",
            DELETING_SESSIONS=set(), DELETED_SESSION_TOMBSTONES=set(),
            event_delivery_lock=lambda sid: locks.setdefault(sid, asyncio.Lock()),
            now_iso=lambda: "2026-09-20T00:00:00Z", logger=Mock(),
            HUB=SimpleNamespace(broadcast=self.broadcast), append_event=AsyncMock(),
            join_task_despite_caller_cancellation=lambda task: task)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<summary-transport>", "exec"), self.ns)

    async def asyncTearDown(self):
        tasks = list(self.ns["REASONING_SUMMARY_STREAM_PENDING"].values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_immediate_first_text_coalesced_sections_and_stable_tool_anchor(self):
        update = self.ns["update_reasoning_summary_stream"]
        await update("chat", "run", "thought", {"summaryIndex": 0, "delta": "First"})
        first = self.broadcast.await_args.args[1]
        self.assertEqual(first["items"][0]["text"], "First")
        self.assertEqual(first["items"][0]["after_seq"], 10)
        self.assertNotIn("seq", first)
        self.ns["EVENT_SEQ_CACHE"]["chat"] = 11  # A tool arrived after thinking began.
        await update("chat", "run", "thought", {"summaryIndex": 1})
        await update("chat", "run", "thought", {"summaryIndex": 1, "delta": "Second"})
        await update("chat", "run", "thought", {"summaryIndex": 0, "delta": " section"})
        self.assertEqual(self.broadcast.await_count, 1)
        await asyncio.wait_for(asyncio.gather(*self.ns["REASONING_SUMMARY_STREAM_PENDING"].values()), 1)
        latest = self.broadcast.await_args.args[1]
        self.assertEqual(latest["items"][0]["text"], "First section\nSecond")
        self.assertEqual(latest["items"][0]["after_seq"], 10)
        self.assertGreater(latest["revision"], first["revision"])

    async def test_completion_or_cancel_clears_pending_snapshot_without_revival(self):
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": "Visible"})
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": " pending"})
        self.assertTrue(self.ns["REASONING_SUMMARY_STREAM_PENDING"])
        await self.ns["clear_reasoning_summary_stream"]("chat", "run", "thought")
        self.assertEqual(self.broadcast.await_args.args[1]["items"], [])
        self.assertFalse(self.ns["REASONING_SUMMARY_STREAM_PENDING"])
        self.assertFalse(self.ns["REASONING_SUMMARY_STREAMS"])
        reconnect = self.ns["reasoning_summary_stream_snapshot"]("chat")
        self.assertEqual(reconnect["items"], [])
        self.assertGreater(reconnect["revision"], self.broadcast.await_args.args[1]["revision"])

    async def test_live_snapshot_bound_does_not_truncate_completed_fallback(self):
        text = "x" * 1_000_001
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": text})
        item = self.broadcast.await_args.args[1]["items"][0]
        self.assertEqual(len(item["text"]), 1_000_000)
        self.assertTrue(item["text_truncated"])
        self.assertEqual(self.ns["reasoning_summary_stream_item"]("chat", "run", "thought")["text"], text)

    async def test_large_plaintext_does_not_displace_compact_summary_from_snapshot(self):
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": "x" * 1_000_001}, phase="reasoning")
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": "Summary"})
        items = self.ns["reasoning_summary_stream_snapshot"]("chat")["items"]
        self.assertEqual((items[0]["phase"], items[0]["text"]), ("summary", "Summary"))
        self.assertEqual(sum(len(item["text"]) for item in items), 1_000_000)
        self.assertTrue(items[1]["text_truncated"])

    async def test_partial_summary_is_retained_once_on_interruption(self):
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": "Already visible"})
        completed = set()
        await self.ns["finish_reasoning_summary_stream"]("chat", "run", completed)
        await self.ns["finish_reasoning_summary_stream"]("chat", "run", completed)
        self.ns["append_event"].assert_awaited_once()
        payload = self.ns["append_event"].await_args.args[2]
        self.assertEqual((payload["text"], payload["partial"], payload["reasoning_after_seq"]), ("Already visible", True, 10))
        self.assertEqual(completed, {"thought"})
        self.assertFalse(self.ns["REASONING_SUMMARY_STREAMS"])

    async def test_cancel_after_completed_write_joins_identity_update_without_partial_duplicate(self):
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": "Earlier"})
        written, release = asyncio.Event(), asyncio.Event()
        async def append(*args):
            written.set()
            await release.wait()
        self.ns["append_event"].side_effect = append
        completed = set()
        task = asyncio.create_task(self.ns["persist_reasoning_summary"]("chat", {
            "run_id": "run", "item_id": "thought", "text": "Authoritative"}, completed))
        await written.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.ns["finish_reasoning_summary_stream"]("chat", "run", completed)
        self.ns["append_event"].assert_awaited_once()
        self.assertEqual(completed, {"thought"})
        self.assertFalse(self.ns["REASONING_SUMMARY_STREAMS"])

    async def test_partial_storage_failure_does_not_block_stream_cleanup(self):
        await self.ns["update_reasoning_summary_stream"]("chat", "run", "thought", {"delta": "Visible"})
        self.ns["append_event"].side_effect = OSError("disk full")
        await self.ns["finish_reasoning_summary_stream"]("chat", "run", set())
        self.ns["logger"].exception.assert_called_once()
        self.assertFalse(self.ns["REASONING_SUMMARY_STREAMS"])
        self.assertFalse(self.ns["REASONING_SUMMARY_STREAM_PENDING"])

    async def test_plaintext_and_summary_share_native_item_without_replacing_or_deduplicating_each_other(self):
        update = self.ns["update_reasoning_summary_stream"]
        await update("chat", "run", "thought", {"delta": "Summary"})
        self.ns["EVENT_SEQ_CACHE"]["chat"] = 11
        await update("chat", "run", "thought", {"contentIndex": 1, "delta": "Second section"}, phase="reasoning")
        await update("chat", "run", "thought", {"contentIndex": 0, "delta": "First section"}, phase="reasoning")
        snapshot = self.ns["reasoning_summary_stream_snapshot"]("chat")
        self.assertEqual([(item["phase"], item["text"], item["after_seq"]) for item in snapshot["items"]], [
            ("summary", "Summary", 10), ("reasoning", "First section\nSecond section", 11)])
        completed = set()
        await self.ns["persist_reasoning_summary"]("chat", {
            "run_id": "run", "item_id": "thought", "phase": "summary", "text": "Final summary"}, completed)
        self.assertEqual(len(self.ns["reasoning_summary_stream_snapshot"]("chat")["items"]), 1)
        await self.ns["finish_reasoning_summary_stream"]("chat", "run", completed)
        await self.ns["finish_reasoning_summary_stream"]("chat", "run", completed)
        events = [call.args[2] for call in self.ns["append_event"].await_args_list]
        self.assertEqual(len(events), 2)
        self.assertEqual([call.args[1] for call in self.ns["append_event"].await_args_list], ["reasoning_summary", "reasoning_text"])
        self.assertEqual(events[1]["phase"], "reasoning")
        self.assertEqual(events[1]["text"], "First section\nSecond section")
        self.assertTrue(events[1]["partial"])
        self.assertEqual(completed, {"thought", ("reasoning", "thought")})
