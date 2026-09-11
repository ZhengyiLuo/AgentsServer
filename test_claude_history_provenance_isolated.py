"""Source-provenance checks with AST-selected helpers and mocked persistence."""

from __future__ import annotations

import ast
import asyncio
from collections import defaultdict, deque
from datetime import datetime
import io
import hashlib
import hmac
import json
from pathlib import Path
import re
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid
from claude_history_provenance import ClaudeInterruptionTracker, normalize_claude_interruption_context
from codex_history_repair import CodexNativeHistoryRepairCache, filter_native_codex_history_items


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "compact_import_text", "is_import_boilerplate", "text_from_content", "message_text",
    "normalized_history_provider_origin", "normalized_history_item", "add_history_item",
    "codex_history_assistant_metadata",
    "normalized_history_import_limit", "claude_history_event_item", "append_claude_history_event",
    "parse_claude_history_events", "parse_provider_history_delta", "history_item_cursor_digest",
    "history_dedup_key", "reconcile_cursor_history_items", "unsynced_history_items",
    "append_imported_history", "append_staged_imported_history", "imported_history_terminal_event",
    "seed_claude_interruption_context", "normalized_history_sync_cursor", "load_provider_history_with_cursor",
    "should_bump_session_updated_at", "is_agent_visible_event",
    "bounded_jsonl_events", "bounded_jsonl_events_range",
}
EVENT_ID = "12345678-1234-4234-8234-123456789abc"
SESSION_ID = "23456789-2345-4345-8345-23456789abcd"
PARENT_ID = "3456789a-3456-4456-8456-3456789abcde"
PROMPT_ID = "456789ab-4567-4567-8567-456789abcdef"
MARKER_ID = "56789abc-5678-4678-8678-56789abcdef0"
TIMESTAMP = "2026-09-09T10:11:12.123-07:00"


def origin(**extra) -> dict:
    return {
        "provider": "claude", "event_id": EVENT_ID, "session_id": SESSION_ID,
        "timestamp": TIMESTAMP, "parent_event_id": PARENT_ID, "prompt_id": PROMPT_ID,
        **extra,
    }


def source_event(kind="user", text="Real conversation", **extra) -> dict:
    return {
        "type": kind, "uuid": EVENT_ID, "sessionId": SESSION_ID,
        "timestamp": TIMESTAMP, "parentUuid": PARENT_ID, "promptId": PROMPT_ID,
        "message": {"role": kind, "content": text}, **extra,
    }


def interruption_chain() -> list[dict]:
    return [
        source_event(),
        source_event("assistant", uuid=PARENT_ID, parentUuid=EVENT_ID, message={"role": "assistant", "content": [{"type": "tool_use", "id": "tool-1"}]}),
        source_event(uuid=MARKER_ID, parentUuid=PARENT_ID, message={"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user for tool use]"}]}),
    ]


def load_projection() -> dict:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS]
    if {node.name for node in selected} != FUNCTIONS:
        raise AssertionError("Isolated provenance helper allowlist is incomplete")
    module = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
        *selected,
    ], type_ignores=[]))
    namespace = {
        "re": re, "datetime": datetime, "json": json, "uuid": uuid, "asyncio": asyncio,
        "filter_native_codex_history_items": filter_native_codex_history_items,
        "CODEX_NATIVE_HISTORY_REPAIR_CACHE": CodexNativeHistoryRepairCache(),
        "hashlib": hashlib, "hmac": hmac, "deque": deque, "defaultdict": defaultdict,
        "ClaudeInterruptionTracker": ClaudeInterruptionTracker,
        "normalize_claude_interruption_context": normalize_claude_interruption_context,
        "MAX_LOCAL_TRANSCRIPT_SCAN_LINES": 10_000, "MAX_LOCAL_TRANSCRIPT_BYTES": 100_000_000,
        "MAX_LOCAL_TRANSCRIPT_LINE_BYTES": 4 * 1024 * 1024, "stat": stat,
        "MAX_WORKSPACE_PATH_CHARS": 4096, "HISTORY_SYNC_CURSOR_VERSION": 1,
        "MAX_IMPORTED_TEXT_CHARS": 100_000, "MAX_IMPORT_MESSAGES": 400,
        "BACKEND_CLAUDE": "claude", "BACKEND_CODEX": "codex", "DEFAULT_BACKEND": "claude",
        "logger": Mock(), "is_claude_task_notification_history_event": Mock(return_value=False),
        "strip_agentsdock_generated_user_text": Mock(side_effect=lambda text, **_kwargs: text),
        "session_provider_id": lambda session: session.get("claude_session_id"),
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class ClaudeHistoryProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.projection = load_projection()

    def test_normal_messages_keep_only_allowlisted_source_fields(self) -> None:
        for kind in ("user", "assistant"):
            event = source_event(kind, cwd="/private/path", context="private", grants=["private"], origin={"secret": True})
            before = json.dumps(event, sort_keys=True)
            item = self.projection["claude_history_event_item"](event)
            self.assertEqual(item, {"kind": kind, "text": "Real conversation", "provider_origin": origin()})
            self.assertEqual(json.dumps(event, sort_keys=True), before)

    def test_invalid_timestamps_drop_only_timestamp(self) -> None:
        for timestamp in (None, 123, "yesterday", "2026-09-09", "2026-09-09T10:11:12", "2026-02-30T10:11:12Z", "2026-09-09T10:11:12+00:99", "2026-09-09T10:11:12+24:00"):
            with self.subTest(timestamp=timestamp):
                result = self.projection["normalized_history_provider_origin"](origin(timestamp=timestamp))
                self.assertEqual(result, {key: value for key, value in origin().items() if key != "timestamp"})

    def test_valid_timestamp_is_preserved_without_reformatting(self) -> None:
        for timestamp in (TIMESTAMP, "2026-09-09T17:11:12Z", "2026-09-09T17:11:12.123456789+00:00"):
            self.assertEqual(self.projection["normalized_history_provider_origin"](origin(timestamp=timestamp))["timestamp"], timestamp)

    def test_invalid_identifiers_and_unrelated_fields_are_independently_omitted(self) -> None:
        result = self.projection["normalized_history_provider_origin"](origin(
            event_id="not-a-uuid", session_id="/private/context", parent_event_id=123,
            prompt_id={"path": "private"}, cwd="private", grants=["private"], kind="user", cause="stop",
        ))
        self.assertEqual(result, {"provider": "claude", "timestamp": TIMESTAMP})
        self.assertIsNone(self.projection["normalized_history_provider_origin"]({"provider": "claude", "grants": ["private"]}))
        self.assertIsNone(self.projection["normalized_history_provider_origin"](origin(provider="codex")))

    def test_kind_text_only_callers_and_adjacent_dedup_are_unchanged(self) -> None:
        normalize = self.projection["normalized_history_item"]
        self.assertEqual(normalize("user", "  Plain text  "), {"kind": "user", "text": "Plain text"})
        items = []
        self.projection["add_history_item"](items, "assistant", "Reply", provider_origin=origin())
        self.projection["add_history_item"](items, "assistant", "Reply", provider_origin=origin(event_id=PARENT_ID))
        self.assertEqual(items, [{"kind": "assistant", "text": "Reply", "provider_origin": origin()}])
        digest = self.projection["history_item_cursor_digest"]
        self.assertEqual(digest(items[0]), digest({"kind": "assistant", "text": "Reply"}))

    def test_full_parser_retains_first_duplicate_provenance(self) -> None:
        events = [source_event(), source_event(uuid=PARENT_ID), source_event("assistant", "Reply", uuid=PROMPT_ID)]
        items = self.projection["parse_claude_history_events"](events, 2)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["provider_origin"], origin())
        self.assertEqual(items[1]["provider_origin"]["event_id"], PROMPT_ID)

    def test_delta_retains_provenance_and_keeps_kind_text_cursor_dedup(self) -> None:
        first = self.projection["claude_history_event_item"](source_event())
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter([
            (source_event(uuid=PARENT_ID), 10),
            (source_event("assistant", "Reply", uuid=PROMPT_ID), 20),
        ]))
        items, offset, digest, blocked = self.projection["parse_provider_history_delta"](
            Path("unused.jsonl"), "claude", 0, 20, limit=2, expected_stat={},
            previous_last_item_digest=self.projection["history_item_cursor_digest"](first),
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["provider_origin"]["event_id"], PROMPT_ID)
        self.assertEqual(offset, 20)
        self.assertFalse(blocked)
        self.assertEqual(digest, self.projection["history_item_cursor_digest"]({"kind": "assistant", "text": "Reply"}))

    def test_content_reconciliation_retains_unmatched_item_metadata(self) -> None:
        items = [self.projection["claude_history_event_item"](source_event()), self.projection["claude_history_event_item"](source_event("assistant", "Reply"))]
        self.projection["history_timeline_message_keys"] = Mock(return_value=([
            (7, self.projection["history_dedup_key"]("user", "Real conversation")),
        ], True, False))
        fresh, consumed = self.projection["reconcile_cursor_history_items"]("chat", items, timeline_after_seq=1, timeline_through_seq=8)
        self.assertEqual(fresh, [items[1]])
        self.assertIs(fresh[0], items[1])
        self.assertEqual(consumed, 8)
        self.assertEqual(self.projection["unsynced_history_items"]("chat", items, timeline_through_seq=8), [items[1]])

    def test_interruption_requires_complete_source_identity_and_aware_time(self) -> None:
        normalize = self.projection["normalized_history_item"]
        valid = origin(kind="interruption", cause="steer")
        self.assertEqual(normalize("interruption", "marker", provider_origin=valid)["provider_origin"], valid)
        for missing in ("event_id", "session_id", "timestamp"):
            malformed = {key: value for key, value in valid.items() if key != missing}
            self.assertIsNone(normalize("interruption", "marker", provider_origin=malformed))
        self.assertEqual(self.projection["normalized_history_provider_origin"](origin(kind="interruption", cause="invalid"))["cause"], "unknown")

    def test_full_parser_consumes_hidden_tool_records_and_keeps_proven_marker_lifecycle(self) -> None:
        context = {}
        items = self.projection["parse_claude_history_events"](interruption_chain(), 1, interruption_context=context)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "interruption")
        self.assertEqual(items[0]["provider_origin"]["event_id"], MARKER_ID)
        self.assertEqual(context["last_event_id"], MARKER_ID)
        self.assertEqual(context["anchor_event_id"], EVENT_ID)
        for marker in ("[Request interrupted by user]", "[Request interrupted by user for tool use]"):
            event = source_event(message={"role": "user", "content": [{"type": "text", "text": marker}]})
            self.assertEqual(self.projection["parse_claude_history_events"]([event], 1)[0]["kind"], "user")
            self.assertEqual(self.projection["parse_claude_history_events"]([{**event, "isMeta": True}], 1)[0]["kind"], "interruption")

    def test_delta_restores_context_before_a_message_blocked_by_the_cursor_cap(self) -> None:
        context = {"version": 1}
        chain = interruption_chain()
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter(zip(chain, (10, 20, 30))))
        items, offset, digest, blocked = self.projection["parse_provider_history_delta"](
            Path("unused.jsonl"), "claude", 0, 30, limit=1, expected_stat={}, previous_last_item_digest="", interruption_context=context,
        )
        self.assertEqual([item["kind"] for item in items], ["user"])
        self.assertEqual(offset, 20)
        self.assertTrue(blocked)
        self.assertEqual(context["last_event_id"], PARENT_ID)
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter([(chain[2], 30)]))
        items, offset, _digest, blocked = self.projection["parse_provider_history_delta"](
            Path("unused.jsonl"), "claude", 20, 30, limit=1, expected_stat={}, previous_last_item_digest=digest, interruption_context=context,
        )
        self.assertEqual([item["kind"] for item in items], ["interruption"])
        self.assertEqual(offset, 30)
        self.assertFalse(blocked)
        self.assertEqual(context["last_event_id"], MARKER_ID)

    def test_failed_delta_validation_never_exports_uncommitted_context(self) -> None:
        context = {"version": 1}
        def records(*_args, **_kwargs):
            yield source_event(), 10
            raise ValueError("transcript changed")
        self.projection["bounded_jsonl_records_range"] = records
        with self.assertRaisesRegex(ValueError, "changed"):
            self.projection["parse_provider_history_delta"](Path("unused"), "claude", 0, 10, limit=1, expected_stat={}, previous_last_item_digest="", interruption_context=context)
        self.assertEqual(context, {"version": 1})

    def test_cursor_context_is_allowlisted_bound_and_preserves_missing_vs_initialized(self) -> None:
        raw = {"version": 1, "backend": "claude", "provider_session_id": SESSION_ID,
               "source_path": "unused.jsonl", "source_dev": 1, "source_ino": 2,
               "source_size": 30, "source_offset": 30, "source_mtime_ns": 4,
               "source_digest": "a" * 64, "last_item_digest": "", "timeline_seq": 0}
        session = {"backend": "claude", "claude_session_id": SESSION_ID, "_history_sync_cursor": raw}
        self.assertNotIn("claude_interruption_context", self.projection["normalized_history_sync_cursor"](session))
        context = {"version": 1, "session_id": SESSION_ID, "prompt_id": PROMPT_ID, "anchor_event_id": EVENT_ID, "last_event_id": PARENT_ID}
        raw["claude_interruption_context"] = {**context, "path": "private", "prompt": "private"}
        self.assertEqual(self.projection["normalized_history_sync_cursor"](session)["claude_interruption_context"], context)
        raw["claude_interruption_context"]["session_id"] = PARENT_ID
        self.assertEqual(self.projection["normalized_history_sync_cursor"](session)["claude_interruption_context"], {"version": 1})

    def test_legacy_seed_is_bounded_stable_and_never_reads_past_consumed_offset(self) -> None:
        chain = interruption_chain()
        prefix = b"x" * (2 * 1024 * 1024) + b"\n"
        consumed = prefix + b"\n".join(json.dumps(event).encode() for event in chain[:2]) + b"\n"
        data = consumed + json.dumps(chain[2]).encode() + b"\n"
        reads = []
        class Stream(io.BytesIO):
            def fileno(self):
                return 91
            def read(self, size=-1):
                reads.append((self.tell(), size))
                return super().read(size)
        path = Mock()
        stamp = {"st_dev": 1, "st_ino": 2, "st_size": len(data), "st_mtime_ns": 4}
        source_stat = {**stamp, "st_mode": stat.S_IFREG}
        fake_os = SimpleNamespace(
            O_RDONLY=0, O_NOFOLLOW=1, O_CLOEXEC=2, O_NONBLOCK=4,
            open=Mock(return_value=91), fdopen=Mock(side_effect=lambda *_args: Stream(data)),
            fstat=Mock(return_value=SimpleNamespace(**source_stat)),
        )
        self.projection["os"] = fake_os
        context = self.projection["seed_claude_interruption_context"](path, len(consumed), expected_stat=stamp, provider_session_id=SESSION_ID)
        self.assertEqual(context["last_event_id"], PARENT_ID)
        self.assertEqual(reads, [(len(consumed) - 2 * 1024 * 1024, 2 * 1024 * 1024)])
        fake_os.open.assert_called_once_with(path, 7)
        self.projection["os"].fstat.side_effect = [SimpleNamespace(**source_stat), SimpleNamespace(**{**source_stat, "st_mtime_ns": 5})]
        with self.assertRaisesRegex(ValueError, "changed"):
            self.projection["seed_claude_interruption_context"](path, len(consumed), expected_stat=stamp, provider_session_id=SESSION_ID)

    def test_metadata_only_companions_and_interruption_do_not_bump_activity(self) -> None:
        bump = self.projection["should_bump_session_updated_at"]
        for kind in ("history_imported", "turn_finished", "provider_interruption"):
            self.assertFalse(bump(kind, {"metadata_only": True, "imported": True, "backend": "claude", "run_id": "import_test"}))
        self.assertFalse(bump("provider_interruption", {}))
        self.assertTrue(bump("history_imported", {}))
        self.assertTrue(bump("turn_started", {"prompt": "Real user"}))
        self.assertTrue(bump("turn_started", {"prompt": "Real user", "metadata_only": True}))
        self.assertTrue(bump("history_imported", {"metadata_only": True, "imported": True, "backend": "codex", "run_id": "import_test"}))

    def test_distinct_interruption_ids_survive_adjacent_and_cursor_dedup(self) -> None:
        make = self.projection["normalized_history_item"]
        first = make("interruption", "same marker", provider_origin=origin(kind="interruption", cause="unknown"))
        second = make("interruption", "same marker", provider_origin=origin(kind="interruption", cause="unknown", event_id=MARKER_ID))
        items = []
        for item in (first, second, second):
            self.projection["add_history_item"](items, item["kind"], item["text"], provider_origin=item["provider_origin"])
        self.assertEqual(items, [first, second])
        digest = self.projection["history_item_cursor_digest"]
        self.assertNotEqual(digest(first), digest(second))
        self.assertEqual(digest(second), digest({**second, "text": "another exact marker representation"}))
        marker = interruption_chain()[-1]
        marker["isMeta"] = True
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter([(marker, 10)]))
        parsed, offset, _digest, blocked = self.projection["parse_provider_history_delta"](
            Path("unused"), "claude", 0, 10, limit=1, expected_stat={}, previous_last_item_digest=digest(first),
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(offset, 10)
        self.assertFalse(blocked)

    def test_invalid_gaps_break_lineage_in_full_delta_and_seed(self) -> None:
        chain = interruption_chain()
        events = [*chain[:2], None, chain[2]]
        self.assertEqual(self.projection["parse_claude_history_events"](events, 10)[-1]["kind"], "user")
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter(zip(events, (10, 20, 30, 40))))
        parsed, _offset, _digest, _blocked = self.projection["parse_provider_history_delta"](
            Path("unused"), "claude", 0, 40, limit=10, expected_stat={}, previous_last_item_digest="",
        )
        self.assertEqual(parsed[-1]["kind"], "user")
        region = b"\n".join(json.dumps(event).encode() for event in chain[:2]) + b"\n{invalid-json}\n"
        class Stream(io.BytesIO):
            def fileno(self):
                return 91
        stamp = {"st_dev": 1, "st_ino": 2, "st_size": len(region), "st_mtime_ns": 4}
        self.projection["os"] = SimpleNamespace(O_RDONLY=0, O_NOFOLLOW=1, O_CLOEXEC=2, O_NONBLOCK=4,
            open=Mock(return_value=91), fdopen=Mock(side_effect=lambda *_args: Stream(region)),
            fstat=Mock(return_value=SimpleNamespace(**stamp, st_mode=stat.S_IFREG)))
        self.assertEqual(self.projection["seed_claude_interruption_context"](Path("unused"), len(region), expected_stat=stamp, provider_session_id=SESSION_ID), {"version": 1})

    def test_bounded_generators_preserve_invalid_only_when_opted_in(self) -> None:
        data = b'{"type":"user"}\n{invalid}\n[]\n\n'
        path = Mock()
        path.stat.return_value = SimpleNamespace(st_size=len(data))
        path.open.side_effect = lambda *_args: io.BytesIO(data)
        self.assertEqual(list(self.projection["bounded_jsonl_events"](path)), [{"type": "user"}])
        self.assertEqual(list(self.projection["bounded_jsonl_events"](path, preserve_invalid=True)), [{"type": "user"}, None, None])
        for preserve, expected in ((False, [{"type": "user"}]), (True, [{"type": "user"}, None])):
            self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter([({"type": "user"}, 10), (None, 20)]))
            self.assertEqual(list(self.projection["bounded_jsonl_events_range"](path, 0, 20, expected_stat={}, preserve_invalid=preserve)), expected)

    def test_loader_seeds_legacy_context_once_and_enriches_only_parsed_interruptions(self) -> None:
        chain = interruption_chain()
        context = {"version": 1, "session_id": SESSION_ID, "prompt_id": PROMPT_ID, "anchor_event_id": EVENT_ID, "last_event_id": PARENT_ID}
        self.projection["provider_history_path"] = Mock(return_value=Path("unused.jsonl"))
        snapshot = {"source_dev": 1, "source_ino": 2, "source_mtime_ns": 4, "source_offset": 30, "source_digest": "a" * 64, "expected_stat": {}}
        self.projection["provider_history_source_snapshot"] = Mock(return_value=(snapshot, True))
        self.projection["seed_claude_interruption_context"] = Mock(return_value=dict(context))
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter([(chain[2], 30)]))
        self.projection["events_path"] = Mock(return_value=Path("unused-events.jsonl"))
        enrich = Mock(side_effect=lambda _path, _provider, origins, **_kwargs: [{**value, "cause": "steer"} for value in origins])
        self.projection["enrich_interruption_origins"] = enrich
        session = {"id": "app-chat", "backend": "claude", "claude_session_id": SESSION_ID}
        _path, items, cursor, continued = self.projection["load_provider_history_with_cursor"](session, 1, {"source_offset": 20})
        self.assertTrue(continued)
        self.assertEqual(items[0]["kind"], "interruption")
        self.assertEqual(items[0]["provider_origin"]["cause"], "steer")
        self.assertEqual(cursor["claude_interruption_context"]["last_event_id"], MARKER_ID)
        self.projection["seed_claude_interruption_context"].assert_called_once()
        enrich.assert_called_once()
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter([]))
        _path, items, next_cursor, _continued = self.projection["load_provider_history_with_cursor"](session, 1, cursor)
        self.assertEqual(items, [])
        self.assertEqual(next_cursor["claude_interruption_context"], cursor["claude_interruption_context"])
        self.projection["seed_claude_interruption_context"].assert_called_once()
        enrich.assert_called_once()
        self.projection["provider_history_source_snapshot"].return_value = (snapshot, False)
        self.projection["bounded_jsonl_events_range"] = Mock(return_value=iter([chain[0]]))
        self.projection["load_provider_history_with_cursor"](session, 1, None)
        self.projection["strip_agentsdock_generated_user_text"].assert_called_with("Real conversation", expected_session_id="app-chat", provider_history=True)
        enrich.assert_called_once()


class ImportedHistoryProvenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.projection = load_projection()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.projection["events_path"] = lambda _session_id: Path(temporary.name) / "events.jsonl"
        self.session = {"id": "app-chat", "backend": "claude", "claude_session_id": SESSION_ID}

        async def durable(_session_id, specifications):
            return [{"seq": index + 1, "ts": "IMPORT-TIME", **payload} for index, (_kind, payload) in enumerate(specifications)]

        self.projection["append_durable_event_batch"] = AsyncMock(side_effect=durable)
        self.projection["append_imported_events"] = AsyncMock(side_effect=lambda _session, specs: len(specs))

    async def append(self, name, items, backend="claude"):
        session = {**self.session, "backend": backend}
        kwargs = {"sync_checkpoint": {"test": "checkpoint"}} if name == "append_imported_history" else {}
        result = await self.projection[name](session, Path("unused.jsonl"), items, **kwargs)
        sink = self.projection["append_durable_event_batch" if name == "append_imported_history" else "append_imported_events"]
        return result, sink.await_args.args[1]

    async def test_both_append_paths_preserve_source_timestamps_without_overriding_internal_ids(self) -> None:
        items = [self.projection["claude_history_event_item"](source_event(kind, kind)) for kind in ("user", "assistant")]
        for name in ("append_imported_history", "append_staged_imported_history"):
            with self.subTest(name=name):
                result, specifications = await self.append(name, items)
                self.assertEqual(result["imported"], 2)
                self.assertEqual([kind for kind, _payload in specifications], ["history_imported", "turn_started", "assistant_text", "turn_finished"])
                for _kind, payload in specifications[1:3]:
                    self.assertEqual(payload["ts"], TIMESTAMP)
                    self.assertEqual(payload["provider_origin"], origin())
                    self.assertNotIn("id", payload)
                    self.assertNotIn("session_id", payload)
                for _kind, payload in (specifications[0], specifications[-1]):
                    self.assertNotIn("provider_origin", payload)
                    self.assertNotIn("metadata_only", payload)
                self.assertNotIn("ts", specifications[0][1])
                self.assertEqual(specifications[-1][1]["ts"], TIMESTAMP)
                if name == "append_imported_history":
                    self.assertEqual(specifications[0][1]["_history_sync_checkpoint"], {"test": "checkpoint"})

    async def test_old_source_times_do_not_extend_import_completion_until_import_day(self) -> None:
        items = [self.projection["claude_history_event_item"](source_event(kind, kind, timestamp=timestamp))
                 for kind, timestamp in (("user", "2026-09-01T12:00:00Z"), ("assistant", "2026-09-01T12:00:05Z"))]
        for name in ("append_imported_history", "append_staged_imported_history"):
            _result, specifications = await self.append(name, items)
            events = [{"type": kind, "ts": "2026-09-10T00:00:00Z", **payload} for kind, payload in specifications]
            self.assertEqual([event["ts"] for event in events[1:]], [
                "2026-09-01T12:00:00Z", "2026-09-01T12:00:05Z", "2026-09-01T12:00:05Z",
            ])
            start = datetime.fromisoformat(events[1]["ts"])
            end = datetime.fromisoformat(events[-1]["ts"])
            self.assertEqual((end - start).total_seconds(), 5)
            self.assertTrue(events[-1]["imported"])

    async def test_invalid_timestamp_keeps_identity_and_leaves_import_time_fallback(self) -> None:
        item = {"kind": "user", "text": "Real", "provider_origin": origin(timestamp="bad", grants=["private"]), "ts": "UNTRUSTED"}
        for name in ("append_imported_history", "append_staged_imported_history"):
            _result, specifications = await self.append(name, [item])
            payload = specifications[1][1]
            self.assertNotIn("ts", payload)
            self.assertNotIn("ts", specifications[-1][1])
            self.assertEqual(payload["provider_origin"], {key: value for key, value in origin().items() if key != "timestamp"})
            _result, legacy = await self.append(name, [{"kind": "user", "text": "Plain"}], backend="codex")
            self.assertNotIn("provider_origin", legacy[1][1])

    async def test_interruption_batch_is_metadata_only_and_never_a_user_prompt(self) -> None:
        item = {"kind": "interruption", "text": "PRIVATE MARKER TEXT", "provider_origin": origin(kind="interruption", cause="stop")}
        for name in ("append_imported_history", "append_staged_imported_history"):
            result, specifications = await self.append(name, [item])
            self.assertEqual(result["imported"], 1)
            self.assertEqual([kind for kind, _payload in specifications], ["history_imported", "provider_interruption", "turn_finished"])
            self.assertTrue(specifications[0][1]["metadata_only"])
            self.assertTrue(specifications[0][1]["imported"])
            self.assertTrue(specifications[-1][1]["metadata_only"])
            payload = specifications[1][1]
            self.assertEqual(payload["ts"], TIMESTAMP)
            self.assertEqual(payload["provider_origin"], item["provider_origin"])
            self.assertNotIn("prompt", payload)
            self.assertNotIn("text", payload)
            self.assertNotIn("PRIVATE MARKER TEXT", json.dumps(specifications))

    async def test_malformed_interruption_is_skipped_and_mixed_batches_keep_message_semantics(self) -> None:
        invalid = {"kind": "interruption", "text": "marker", "provider_origin": origin(kind="interruption", timestamp="bad")}
        for name in ("append_imported_history", "append_staged_imported_history"):
            result, specifications = await self.append(name, [invalid])
            self.assertEqual(result["imported"], 0)
            self.assertEqual([kind for kind, _payload in specifications], ["history_imported", "turn_finished"])
            self.assertTrue(specifications[0][1]["metadata_only"])
            valid = {**invalid, "provider_origin": origin(kind="interruption", cause="unknown")}
            _result, mixed = await self.append(name, [valid, {"kind": "user", "text": "Real user"}])
            self.assertNotIn("metadata_only", mixed[0][1])
            self.assertNotIn("metadata_only", mixed[-1][1])
            self.assertEqual([kind for kind, _payload in mixed], ["history_imported", "provider_interruption", "turn_started", "turn_finished"])


if __name__ == "__main__":
    unittest.main()
