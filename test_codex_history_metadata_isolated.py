"""Forward Codex import metadata; AST-only helpers, no server startup or providers."""
from __future__ import annotations

import ast
from collections import defaultdict
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock

from test_codex_goal_history_isolated import SOURCE, load_projection, source_user


STAMP = "2026-09-10T05:03:16.123456789-07:00"


def assistant(text="Public progress", *, shape="response_item", phase=None, timestamp=None):
    payload = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    if shape == "event_msg":
        payload = {"type": "agent_message", "message": text}
    return {"type": shape, "timestamp": timestamp, "payload": {**payload, "phase": phase}}


def projection():
    ns = load_projection()
    names = {
        "normalized_history_sync_cursor", "load_provider_history_with_cursor",
        "history_dedup_key", "history_timeline_message_keys", "is_native_goal_steer_event",
        "reconcile_cursor_history_items", "unsynced_history_items",
        "append_durable_event_batch_sync", "append_imported_events_sync",
    }
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    assert {node.name for node in selected} == names
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), *selected], type_ignores=[]))
    ns.update({
        "os": os, "defaultdict": defaultdict,
        "HISTORY_SYNC_CURSOR_VERSION": 1, "HISTORY_SYNC_EVENT_SCAN_LIMIT": 400,
        "MAX_WORKSPACE_PATH_CHARS": 4096, "MAX_LOCAL_TRANSCRIPT_BYTES": 100_000_000,
        "MAX_LOCAL_TRANSCRIPT_LINE_BYTES": 4 * 1024 * 1024,
        "event_files_belong_to_session": lambda event, chat: event.get("session_id") == chat,
    })
    exec(compile(module, str(SOURCE), "exec"), ns)
    return ns


class CodexHistoryMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = projection()

    def parse(self, events, limit=400):
        return self.ns["parse_codex_history_events"](events, limit)

    def delta(self, events, *, previous="", context=None, limit=400):
        self.ns["bounded_jsonl_records_range"] = Mock(return_value=iter(
            (event, (index + 1) * 10) for index, event in enumerate(events)
        ))
        return self.ns["parse_provider_history_delta"](
            Path("unused"), "codex", 0, len(events) * 10, limit=limit,
            expected_stat={}, previous_last_item_digest=previous, codex_phase_context=context,
        )

    def test_explicit_public_phase_and_aware_timestamp_preserved_without_source_mutation(self):
        for shape in ("response_item", "event_msg"):
            for phase in ("commentary", "final_answer"):
                event = assistant(shape=shape, phase=phase, timestamp=STAMP)
                event["payload"].update(authority="private", provider_turn_id="private")
                before = json.dumps(event, sort_keys=True)
                self.assertEqual(self.parse([event]), [{"kind": "assistant", "text": "Public progress", "phase": phase, "ts": STAMP}])
                self.assertEqual(json.dumps(event, sort_keys=True), before)

    def test_malformed_or_naive_time_drops_only_time_and_never_guesses_phase(self):
        for timestamp in (None, 42, "yesterday", "2026-09-10", "2026-09-10T05:03:16", "2026-02-30T05:03:16Z", "2026-09-10T05:03:16+24:00", "2026-09-10T05:03:16+00:99"):
            self.assertEqual(self.parse([assistant(phase="commentary", timestamp=timestamp)])[0],
                             {"kind": "assistant", "text": "Public progress", "phase": "commentary"})
        for phase in (None, "analysis", "final", [], {"phase": "commentary"}):
            item = self.parse([assistant(phase=phase, timestamp=STAMP)])[0]
            self.assertNotIn("phase", item)
            self.assertEqual(item["ts"], STAMP)

    def test_user_provenance_and_private_reasoning_are_unchanged(self):
        user = source_user("A genuine quote", kinds=("user.text",))
        user.update(timestamp=STAMP)
        user["payload"]["phase"] = "commentary"
        self.assertEqual(self.parse([user]), [{"kind": "user", "text": "A genuine quote", "provider_user_authored": True, "ts": STAMP}])
        self.assertEqual(self.parse([{"type": "response_item", "timestamp": STAMP, "payload": {
            "type": "reasoning", "phase": "commentary", "summary": [{"text": "private"}],
        }}]), [])

    def test_adjacent_duplicate_enrichment_and_no_downgrade_in_full_and_delta(self):
        events = [assistant(shape="event_msg"), assistant(phase="commentary", timestamp=STAMP),
                  assistant(timestamp="2026-09-11T00:00:00Z"), assistant(phase="analysis")]
        expected = [{"kind": "assistant", "text": "Public progress", "phase": "commentary", "ts": STAMP}]
        self.assertEqual(self.parse(events), expected)
        context = {}
        items, offset, digest, blocked = self.delta(events, context=context, limit=1)
        self.assertEqual(items, expected)
        self.assertEqual((offset, blocked, context), (40, False, {"phase": "commentary"}))
        self.assertEqual(digest, self.ns["history_item_cursor_digest"]({"kind": "assistant", "text": "Public progress"}))

    def test_known_distinct_phases_never_coalesce_and_cap_stops_before_final(self):
        events = [assistant(phase="commentary", timestamp=STAMP), assistant(phase="final_answer", timestamp=STAMP)]
        self.assertEqual([item["phase"] for item in self.parse(events)], ["commentary", "final_answer"])
        self.assertEqual([item["phase"] for item in self.delta(events)[0]], ["commentary", "final_answer"])
        context = {}
        items, offset, _, blocked = self.delta(events, context=context, limit=1)
        self.assertEqual((len(items), offset, blocked, context), (1, 10, True, {"phase": "commentary"}))

    def test_cursor_boundary_keeps_known_phase_distinction_and_old_cursor_dedup(self):
        commentary = self.parse([assistant(phase="commentary")])[0]
        digest = self.ns["history_item_cursor_digest"](commentary)
        final = assistant(phase="final_answer", timestamp=STAMP)
        context = {"phase": "commentary"}
        items, _, _, _ = self.delta([final], previous=digest, context=context)
        self.assertEqual(items[0]["phase"], "final_answer")
        self.assertEqual(context, {"phase": "final_answer"})
        self.assertEqual(self.delta([final], previous=digest, context=context)[0], [])
        # Missing legacy context never guesses what an already committed row was.
        self.assertEqual(self.delta([final], previous=digest, context={})[0], [])
        legacy_context = {}
        items, _, _, _ = self.delta([
            assistant(phase="commentary"), final,
        ], previous=digest, context=legacy_context)
        self.assertEqual([item["phase"] for item in items], ["final_answer"])
        self.assertEqual(legacy_context, {"phase": "final_answer"})

    def test_loader_cursor_reopen_keeps_phase_without_changing_digest(self):
        session = {"id": "chat", "backend": "codex", "codex_thread_id": "thread"}
        snapshot = {"source_offset": 10, "source_digest": "a" * 64, "source_dev": 1,
                    "source_ino": 2, "source_mtime_ns": 3, "expected_stat": {}}
        self.ns["provider_history_path"] = Mock(return_value=Path("/exact/transcript.jsonl"))
        self.ns["provider_history_source_snapshot"] = Mock(return_value=(snapshot, False))
        self.ns["bounded_jsonl_events_range"] = Mock(return_value=iter([assistant(phase="commentary", timestamp=STAMP)]))
        _, items, cursor, _ = self.ns["load_provider_history_with_cursor"](session, 400, None)
        self.assertEqual(cursor["codex_last_item_phase"], "commentary")
        session["_history_sync_cursor"] = cursor
        reopened = self.ns["normalized_history_sync_cursor"](session)
        self.assertEqual(reopened["codex_last_item_phase"], "commentary")
        self.assertEqual(reopened["last_item_digest"], self.ns["history_item_cursor_digest"](items[0]))
        snapshot = {**snapshot, "source_offset": 20}
        self.ns["provider_history_source_snapshot"].return_value = (snapshot, True)
        self.ns["bounded_jsonl_records_range"] = Mock(return_value=iter([(assistant(phase="final_answer", timestamp=STAMP), 20)]))
        _, items, cursor, continued = self.ns["load_provider_history_with_cursor"](session, 400, reopened)
        self.assertTrue(continued)
        self.assertEqual((items[0]["phase"], cursor["codex_last_item_phase"]), ("final_answer", "final_answer"))
        self.ns["bounded_jsonl_records_range"] = Mock(return_value=iter([]))
        self.assertEqual(self.ns["load_provider_history_with_cursor"](session, 400, cursor)[1], [])

    async def test_both_import_paths_emit_commentary_progress_final_answer_and_original_time(self):
        items = self.parse([assistant(phase="commentary", timestamp=STAMP), assistant("Done", phase="final_answer", timestamp=STAMP)])
        session = {"id": "chat", "backend": "codex", "codex_thread_id": "thread"}
        for name, sink in (("append_imported_history", "append_durable_event_batch"),
                           ("append_staged_imported_history", "append_imported_events")):
            self.ns[sink] = AsyncMock(side_effect=lambda chat, batch: [{"seq": i + 1} for i in range(len(batch))] if sink == "append_durable_event_batch" else len(batch))
            await self.ns[name](session, Path("/exact/transcript.jsonl"), items)
            batch = self.ns[sink].call_args.args[1]
            self.assertEqual([kind for kind, _ in batch], ["history_imported", "reasoning_summary", "assistant_text", "turn_finished"])
            for (kind, event), phase in zip(batch[1:3], ("commentary", "final_answer")):
                self.assertEqual((event["phase"], event["ts"], event["imported"]), (phase, STAMP, True))
                self.assertNotIn("provider_origin", event)
                self.assertNotIn("provider_turn_id", event)
            self.assertEqual(batch[-1][1]["ts"], STAMP)

    async def test_old_source_times_do_not_extend_synthetic_work_until_import_day(self):
        user = source_user("Real request", kinds=("user.text",))
        user["timestamp"] = "2026-09-01T12:00:00Z"
        items = self.parse([
            user,
            assistant(phase="commentary", timestamp="2026-09-01T12:00:01Z"),
            assistant("Done", phase="final_answer", timestamp="2026-09-01T12:00:05Z"),
        ])
        session = {"id": "chat", "backend": "codex", "codex_thread_id": "thread"}
        for name, sink in (("append_imported_history", "append_durable_event_batch"),
                           ("append_staged_imported_history", "append_imported_events")):
            self.ns[sink] = AsyncMock(side_effect=lambda chat, batch: [{"seq": i + 1} for i in range(len(batch))] if sink == "append_durable_event_batch" else len(batch))
            await self.ns[name](session, Path("/exact/transcript.jsonl"), items)
            batch = self.ns[sink].call_args.args[1]
            self.assertEqual([event.get("ts") for _, event in batch[1:]], [
                "2026-09-01T12:00:00Z", "2026-09-01T12:00:01Z",
                "2026-09-01T12:00:05Z", "2026-09-01T12:00:05Z",
            ])
            with tempfile.TemporaryDirectory() as directory:
                persisted = self.ns["append_durable_event_batch_sync"](Path(directory) / "events.jsonl", "chat", 1, batch)
                start = self.ns["datetime"].fromisoformat(persisted[1]["ts"])
                end = self.ns["datetime"].fromisoformat(persisted[-1]["ts"])
                self.assertEqual((end - start).total_seconds(), 5)

    async def test_unknown_source_times_keep_existing_fallback_and_user_duplicate_can_enrich_time(self):
        missing = source_user("Real request", kinds=None)
        richer = source_user("Real request", kinds=("user.text",))
        richer["timestamp"] = STAMP
        items = self.parse([missing, richer])
        self.assertEqual(items, [{"kind": "user", "text": "Real request", "provider_user_authored": True, "ts": STAMP}])
        self.ns["append_imported_events"] = AsyncMock(side_effect=lambda chat, batch: len(batch))
        await self.ns["append_staged_imported_history"](
            {"id": "chat", "backend": "codex", "codex_thread_id": "thread"}, Path("unused"), self.parse([missing, assistant()]),
        )
        batch = self.ns["append_imported_events"].call_args.args[1]
        self.assertTrue(all("ts" not in event for _, event in batch))

    def test_ledger_matching_counts_only_explicit_public_codex_commentary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            self.ns["events_path"] = lambda chat: path
            events = [
                ("reasoning_summary", {"backend": "codex", "phase": "commentary", "text": "Public progress"}),
                ("reasoning_summary", {"backend": "codex", "text": "private"}),
                ("reasoning_summary", {"backend": "codex", "phase": "commentary", "text": "Imported progress", "imported": True}),
            ]
            self.ns["append_durable_event_batch_sync"](path, "chat", 1, events)
            self.ns["last_event_seq_from_file"] = lambda _: 3
            native = self.parse([assistant()])
            fresh, through = self.ns["reconcile_cursor_history_items"]("chat", native, timeline_after_seq=0, timeline_through_seq=3)
            self.assertEqual((fresh, through), ([], 3))
            imported = self.parse([assistant("Imported progress", phase="commentary")])
            self.assertEqual(self.ns["unsynced_history_items"]("chat", imported, timeline_through_seq=3), [])
            keys, _, _ = self.ns["history_timeline_message_keys"]("chat", timeline_after_seq=0, timeline_through_seq=3, tail=False, include_imported=False)
        self.assertEqual(keys, [(1, self.ns["history_dedup_key"]("assistant", "Public progress"))])

    def test_persistence_preserves_aware_timestamp_instead_of_import_time(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("append_durable_event_batch_sync", "append_imported_events_sync"):
                path = Path(directory) / f"{name}.jsonl"
                self.ns[name](path, "chat", 1, [
                    ("reasoning_summary", {"phase": "commentary", "ts": STAMP, "imported": True, "text": "Progress"}),
                ])
                self.assertEqual(json.loads(path.read_text())["ts"], STAMP)


if __name__ == "__main__":
    unittest.main()
