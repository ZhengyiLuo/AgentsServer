"""Deferral/cursor boundary tests using AST-only adapters and fake persistence."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from pathlib import Path
import re
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from codex_history_repair import CodexNativeHistoryProofUnavailable


SOURCE = (Path(__file__).resolve().parents[1] / "agent_server.py")
PROVIDER = "11111111-2222-3333-4444-555555555555"
FUNCTIONS = {
    "sync_provider_history", "import_session_history", "append_imported_history",
    "append_staged_imported_history", "filter_codex_history_for_import",
    "normalized_history_sync_cursor", "history_sync_checkpoint", "persist_history_sync_cursor",
    "history_cursor_matches_source_stamp", "imported_history_terminal_event",
    "prepare_codex_native_history_repair",
}


def load_deferral_glue():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    nodes = [node for node in tree.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS]
    assert {node.name for node in nodes} == FUNCTIONS
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    namespace = dict(asyncio=asyncio, threading=threading, re=re, uuid=uuid, Path=Path,
                     CodexNativeHistoryProofUnavailable=CodexNativeHistoryProofUnavailable)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[])),
                 "<isolated-codex-history-deferral>", "exec"), namespace)
    return namespace


class CodexHistoryDeferralTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.functions = load_deferral_glue()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="codex-import-deferral-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / f"rollout-{PROVIDER}.jsonl"
        self.cursor = {
            "version": 1, "backend": "codex", "provider_session_id": PROVIDER,
            "source_path": str(self.source), "source_dev": 1, "source_ino": 2,
            "source_size": 100, "source_offset": 100, "source_mtime_ns": 3,
            "source_digest": "a" * 64, "last_item_digest": "b" * 64,
            "timeline_seq": 5, "checkpoint_seq": 5,
        }
        self.next_cursor = {**self.cursor, "source_size": 200, "source_offset": 200,
                            "source_digest": "c" * 64, "source_caught_up": True}
        self.session = {"id": "chat", "backend": "codex", "codex_thread_id": PROVIDER,
                        "_history_sync_cursor": deepcopy(self.cursor)}
        self.items = [{"kind": "assistant", "text": "Candidate provider replay"}]
        self.cache = SimpleNamespace(forget=Mock(), is_prepared=Mock(return_value=False), prepare=Mock())
        self.store = SimpleNamespace(sessions={"chat": self.session}, _lock=asyncio.Lock(), save=AsyncMock())
        self.ns = self.functions
        self.ns.update(
            STORE=self.store, logger=Mock(), BACKEND_CODEX="codex", BACKEND_CLAUDE="claude", BACKEND_CURSOR="cursor", DEFAULT_BACKEND="codex",
            HISTORY_SYNC_CURSOR_VERSION=1, HISTORY_SYNC_CHECKPOINT_VERSION=1,
            MAX_WORKSPACE_PATH_CHARS=4096, MAX_LOCAL_TRANSCRIPT_BYTES=1 << 40,
            session_provider_id=lambda session: session.get("codex_thread_id"),
            provider_session_identifier=lambda identity: identity,
            provider_history_source_stamp=Mock(return_value=[str(self.source), 200, 4, 1, 2]),
            load_provider_history_with_cursor=Mock(side_effect=lambda *args, **kwargs:
                (self.source, deepcopy(self.items), deepcopy(self.next_cursor), True)),
            committed_history_sync_checkpoint=Mock(return_value=None),
            last_event_seq_from_file=Mock(return_value=10),
            reconcile_cursor_history_items=Mock(side_effect=lambda sid, items, **kwargs: (items, 10)),
            unsynced_history_items=Mock(side_effect=lambda sid, items, **kwargs: items),
            events_path=lambda session: self.root / "events.jsonl", read_events=Mock(return_value=[]),
            append_event=AsyncMock(), append_imported_events=AsyncMock(side_effect=lambda sid, specs: len(specs)),
            append_durable_event_batch=AsyncMock(side_effect=lambda sid, specs: [{"seq": 11 + index} for index in range(len(specs))]),
            filter_native_codex_history_items=Mock(side_effect=CodexNativeHistoryProofUnavailable("Incomplete proof")),
            CODEX_NATIVE_HISTORY_REPAIR_CACHE=self.cache,
            normalized_history_provider_origin=lambda value: value,
            codex_history_assistant_metadata=lambda value: {}, concise_error_message=str,
            CODEX_SESSIONS_ROOT=self.root, find_codex_history=Mock(return_value=self.source),
            HISTORY_SEARCH_REPAIR_DIRTY=set(), HISTORY_SEARCH_DIRTY=set(),
        )

    def assert_no_persistence(self):
        self.ns["append_event"].assert_not_awaited()
        self.ns["append_durable_event_batch"].assert_not_awaited()
        self.ns["append_imported_events"].assert_not_awaited()
        self.store.save.assert_not_awaited()
        self.cache.forget.assert_not_called()
        self.assertEqual(self.session["_history_sync_cursor"], self.cursor)

    async def test_normal_sync_defers_before_any_event_or_cursor_commit(self):
        result = await self.ns["sync_provider_history"](dict(self.session))
        self.assertEqual(result["imported"], 0)
        self.assertIs(result["deferred"], True)
        self.assertEqual(result["reason"], "native_history_proof_unavailable")
        self.assert_no_persistence()
        self.assertEqual(self.items, [{"kind": "assistant", "text": "Candidate provider replay"}])

    async def test_explicit_import_defers_without_failure_marker_or_cursor_commit(self):
        result = await self.ns["import_session_history"](dict(self.session), force=False)
        self.assertEqual(result["imported"], 0)
        self.assertIs(result["deferred"], True)
        self.assertEqual(result["reason"], "native_history_proof_unavailable")
        self.assert_no_persistence()

    async def test_initial_sync_does_not_create_cursor_when_proof_is_incomplete(self):
        self.session.pop("_history_sync_cursor")
        for name in ["sync_provider_history", "import_session_history"]:
            with self.subTest(name=name):
                result = await self.ns[name](dict(self.session))
                self.assertIs(result["deferred"], True)
                self.assertNotIn("_history_sync_cursor", self.session)
                self.store.save.assert_not_awaited()
                self.ns["append_event"].assert_not_awaited()
                self.ns["append_durable_event_batch"].assert_not_awaited()

    async def test_explicit_retry_after_transient_failure_commits_from_original_cursor(self):
        before = deepcopy(self.session)
        deferred = await self.ns["import_session_history"](dict(self.session))
        self.assertIs(deferred["deferred"], True)
        self.assertEqual(self.session, before)
        self.ns["filter_native_codex_history_items"].side_effect = lambda sid, pid, path, items, **kwargs: items
        result = await self.ns["import_session_history"](dict(self.session))
        self.assertEqual(result["imported"], 1)
        self.assertFalse(result.get("deferred"))
        self.ns["append_event"].assert_not_awaited()
        self.ns["append_durable_event_batch"].assert_awaited_once()
        self.store.save.assert_awaited_once()
        self.assertEqual(self.session["_history_sync_cursor"]["source_offset"], 200)
        checkpoint = self.ns["append_durable_event_batch"].await_args.args[1][0][1]["_history_sync_checkpoint"]
        self.assertEqual(checkpoint["previous_source_offset"], self.cursor["source_offset"])
        self.assertEqual(checkpoint["previous_source_digest"], self.cursor["source_digest"])

    async def test_direct_append_incomplete_proof_raises_before_building_durable_batch(self):
        with self.assertRaises(CodexNativeHistoryProofUnavailable):
            await self.ns["append_imported_history"](dict(self.session), self.source, self.items,
                sync_checkpoint={"version": 1})
        self.assert_no_persistence()

    async def test_new_staged_import_without_native_checkpoint_keeps_existing_flow(self):
        result = await self.ns["append_staged_imported_history"](dict(self.session), self.source, self.items)
        self.assertEqual(result["imported"], 1)
        self.ns["filter_native_codex_history_items"].assert_not_called()
        self.ns["append_imported_events"].assert_awaited_once()
        self.ns["append_durable_event_batch"].assert_not_awaited()
        self.store.save.assert_not_awaited()
        self.assertEqual(self.session["_history_sync_cursor"], self.cursor)

    async def test_worker_cancellation_signals_read_only_filter_and_commits_nothing(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        cancellation = []
        def filter_items(sid, pid, path, items, *, cancelled=None, **kwargs):
            entered.set()
            try:
                if not release.wait(3):
                    raise AssertionError("Synthetic proof worker was not released")
                cancellation.append(cancelled())
                raise CodexNativeHistoryProofUnavailable("Cancelled proof")
            finally:
                finished.set()
        self.ns["filter_native_codex_history_items"].side_effect = filter_items
        task = asyncio.create_task(self.ns["sync_provider_history"](dict(self.session)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            release.set()
        self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        self.assertEqual(cancellation, [True])
        self.assert_no_persistence()

    def test_incomplete_cache_preparation_dirties_no_indexes_and_next_request_retries(self):
        self.cache.prepare.side_effect = [CodexNativeHistoryProofUnavailable("Incomplete proof"), True]
        self.ns["prepare_codex_native_history_repair"]("chat")
        self.assertEqual(self.ns["HISTORY_SEARCH_REPAIR_DIRTY"], set())
        self.assertEqual(self.ns["HISTORY_SEARCH_DIRTY"], set())
        self.cache.forget.assert_not_called()
        self.ns["prepare_codex_native_history_repair"]("chat")
        self.assertEqual(self.cache.prepare.call_count, 2)
        self.assertEqual(self.ns["HISTORY_SEARCH_REPAIR_DIRTY"], {"chat"})
        self.assertEqual(self.ns["HISTORY_SEARCH_DIRTY"], {"chat"})


if __name__ == "__main__":
    unittest.main()
