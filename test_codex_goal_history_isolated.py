"""Goal-history projections: allowlisted AST helpers, no server import/startup."""
from __future__ import annotations

import ast
from collections import deque
from contextlib import suppress
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
import uuid
from codex_history_repair import codex_public_item_origin, filter_native_codex_history_items


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "compact_import_text", "is_import_boilerplate", "text_from_content",
    "normalized_history_provider_origin", "normalized_history_item",
    "normalized_history_import_limit", "codex_user_item_has_human_provenance",
    "is_codex_goal_runtime_user_item", "codex_history_user_record",
    "is_codex_subagent_notification_user_item",
    "codex_runtime_user_item_kind",
    "codex_history_user_item", "codex_history_event_item", "append_codex_history_event",
    "codex_history_assistant_metadata", "codex_history_assistant_item", "merge_codex_history_duplicate",
    "codex_history_user_event_item",
    "parse_codex_history_events", "codex_transcript_preview", "history_item_cursor_digest",
    "history_dedup_key",
    "parse_provider_history_delta", "project_legacy_imported_provider_event",
    "project_provider_history_event_for_egress", "client_safe_event",
    "append_imported_history", "append_staged_imported_history",
    "imported_history_terminal_event", "is_agent_visible_event",
    "should_bump_session_updated_at", "update_session_event_metadata",
    "clear_imported_active_runs", "prepare_codex_goal_history_repair",
    "sync_history_search_index", "history_search_event_record",
    "is_native_goal_steer_event",
}
GOAL = (
    '<codex_internal_context source="goal">\n'
    'Continue working toward the active thread goal.\n'
    'This is runtime continuation metadata, not a new human message.\n'
    '<objective>Finish the existing work</objective>\n'
    '</codex_internal_context>'
)


def source_user(text=GOAL, *, kinds=("goal.internal_context",), shape="response_item", **fields):
    payload = {"type": "message", "role": "user", "id": "msg_fixture",
               "content": [{"type": "input_text", "text": text}], **fields}
    if shape == "event_msg":
        payload.update(type="user_message", message=text)
        payload.pop("content")
    if kinds is not None:
        payload["internal_chat_message_metadata_passthrough"] = {
            "content_item_kinds": list(kinds), "turn_id": "turn_fixture",
            "create_time": "2026-09-10T05:03:16Z",
        }
    return {"type": shape, "payload": payload}


def imported(text=GOAL, **extra):
    return {"type": "turn_started", "seq": 2, "session_id": "chat",
            "run_id": "import_fixture", "backend": "codex", "imported": True,
            "provider_history_sanitized": True, "prompt": text, **extra}


def load_projection():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS]
    assert {node.name for node in selected} == FUNCTIONS
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), *selected], type_ignores=[]))
    namespace = {
        "re": re, "datetime": datetime, "hashlib": hashlib, "hmac": hmac,
        "json": json, "deque": deque, "uuid": uuid, "suppress": suppress, "Path": Path,
        "MAX_IMPORTED_TEXT_CHARS": 100_000, "MAX_IMPORT_MESSAGES": 400,
        "CODEX_TRANSCRIPT_SCAN_LINES": 1000, "CODEX_APP_SERVER_TOOL_OUTPUT_MAX_CHARS": 100_000,
        "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude", "DEFAULT_BACKEND": "claude",
        "TIMELINE_INDEX_CODEX_COMPACTION_TYPES": set(),
        "TIMELINE_INDEX_JOB_TYPES": set(),
        "HISTORY_SEARCH_REPAIR_DIRTY": set(), "HISTORY_SEARCH_DIRTY": set(),
        "HISTORY_SEARCH_EVENT_TYPES": {"turn_started", "assistant_text"},
        "HISTORY_SEARCH_LINE_MARKERS": (b'"type":"turn_started"', b'"type":"assistant_text"'),
        "is_fork_internal_event": lambda *_: False,
        "is_client_visible_event": lambda *_: True,
        "timeline_index_is_error": lambda *_: False,
        "timeline_search_event_text": lambda event: str(event.get("prompt") or event.get("text") or ""),
        "timeline_search_value": lambda value: str(value or ""),
        "TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD": "_agentsdock_imported_prompt_hidden",
        "CLAUDE_METADATA_REPAIR_CACHE": SimpleNamespace(project_event=lambda *_: None, is_hidden=lambda *_: False),
        "CODEX_GOAL_HISTORY_REPAIR_CACHE": SimpleNamespace(is_hidden=Mock(return_value=False)),
        "CODEX_NATIVE_HISTORY_REPAIR_CACHE": SimpleNamespace(project_event=lambda *_: None, forget=lambda *_: None),
        "codex_public_item_origin": codex_public_item_origin,
        "filter_native_codex_history_items": filter_native_codex_history_items,
        "strip_agentsdock_generated_user_text": lambda text, **kwargs: text,
        "strip_all_legacy_agentsdock_provider_authority_suffixes": lambda text, **kwargs: text,
        "session_provider_id": lambda session: session.get("codex_thread_id"),
        "logger": Mock(), "now_iso": lambda: "2026-09-10T00:00:00Z",
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


class GoalHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ns = load_projection()

    def test_typed_runtime_input_is_omitted_without_mutating_source(self):
        for shape in ("response_item", "event_msg"):
            event = source_user(shape=shape)
            before = json.dumps(event, sort_keys=True)
            self.assertIsNone(self.ns["codex_history_event_item"](event))
            self.assertEqual(json.dumps(event, sort_keys=True), before)

    def test_missing_mixed_or_malformed_provenance_keeps_the_entire_text(self):
        for kinds in (None, (), ("user.text",), ("user.text", "goal.internal_context"),
                      ("environments.environment_context",), ("unknown",), (42,)):
            with self.subTest(kinds=kinds):
                item = self.ns["codex_history_event_item"](source_user(kinds=kinds))
                self.assertEqual(item["text"], GOAL)
        for metadata in (None, "goal.internal_context", {}, {"content_item_kinds": "goal.internal_context"}):
            event = source_user()
            event["payload"]["internal_chat_message_metadata_passthrough"] = metadata
            self.assertEqual(self.ns["codex_history_event_item"](event)["text"], GOAL)

    def test_client_authorship_wins_even_over_conflicting_runtime_metadata(self):
        for fields in ({"clientId": "human"}, {"clientUserMessageId": "human"},
                       {"client_id": "human"}, {"client_user_message_id": "human"},
                       {"provider_user_authored": True}, {"origin": {"kind": "human"}},
                       {"provider_origin": {"provider": "codex", "kind": "user"}}):
            item = self.ns["codex_history_event_item"](source_user(**fields))
            self.assertEqual(item, {"kind": "user", "text": GOAL, "provider_user_authored": True})

    def test_partial_quoted_and_non_goal_text_is_never_removed(self):
        for text in ("Explain this:\n" + GOAL, GOAL + "\nMy question", "```xml\n" + GOAL + "\n```",
                     GOAL.replace('source="goal"', 'source="memory"'),
                     GOAL.replace("</codex_internal_context>", ""),
                     GOAL.replace("<objective>Finish the existing work</objective>", "<objective></objective>"),
                     GOAL.replace("<objective>", "<objective><objective>"),
                     GOAL.replace("Continue working toward the active thread goal.", "A quoted example.")):
            self.assertEqual(self.ns["codex_history_event_item"](source_user(text))["text"], text.strip())
        self.assertIsNone(self.ns["codex_history_event_item"](source_user(GOAL.replace('"goal"', "'goal'"))))

    def test_assistant_wrapper_is_preserved_and_runtime_does_not_consume_limit(self):
        assistant = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": GOAL}]}}
        events = [source_user("Real request", kinds=("user.text",)), source_user(), assistant]
        result = self.ns["parse_codex_history_events"](events, 2)
        self.assertEqual([item["kind"] for item in result], ["user", "assistant"])
        self.assertEqual(result[-1]["text"], GOAL)
        self.assertTrue(result[0]["provider_user_authored"])

    def test_dedup_keeps_positive_human_provenance(self):
        events = [source_user("Real", kinds=None), source_user("Real", kinds=("user.text",))]
        self.assertEqual(self.ns["parse_codex_history_events"](events, 2), [
            {"kind": "user", "text": "Real", "provider_user_authored": True},
        ])

    def test_preview_uses_the_same_provenance_classifier(self):
        self.ns["bounded_jsonl_events"] = Mock(return_value=iter([
            source_user(), source_user("Real preview", kinds=("user.text",)),
        ]))
        self.assertEqual(self.ns["codex_transcript_preview"](Path("unused")), "Real preview")

    def test_delta_advances_across_runtime_only_input_without_visible_items(self):
        self.ns["bounded_jsonl_records_range"] = Mock(return_value=iter([(source_user(), 10)]))
        result = self.ns["parse_provider_history_delta"](
            Path("unused"), "codex", 0, 10, limit=1, expected_stat={}, previous_last_item_digest="previous",
        )
        self.assertEqual(result, ([], 10, "previous", False))

    def test_delta_limit_preserves_the_next_real_message_and_runtime_offsets(self):
        self.ns["bounded_jsonl_records_range"] = Mock(return_value=iter([
            (source_user("First", kinds=("user.text",)), 10), (source_user(), 20),
            (source_user("Next", kinds=("user.text",)), 30),
        ]))
        items, offset, digest, blocked = self.ns["parse_provider_history_delta"](
            Path("unused"), "codex", 0, 30, limit=1, expected_stat={}, previous_last_item_digest="",
        )
        self.assertEqual([item["text"] for item in items], ["First"])
        self.assertEqual(offset, 20)
        self.assertTrue(blocked)
        self.assertEqual(digest, self.ns["history_item_cursor_digest"](items[0]))

    def test_old_import_requires_source_proof_and_projected_boundary_is_idempotent(self):
        event = imported()
        self.assertIs(self.ns["project_legacy_imported_provider_event"](event, "chat"), event)
        self.ns["CODEX_GOAL_HISTORY_REPAIR_CACHE"].is_hidden.return_value = True
        projected = self.ns["project_legacy_imported_provider_event"](event, "chat")
        self.assertEqual(projected["prompt"], "")
        self.assertTrue(projected["_agentsdock_imported_prompt_hidden"])
        self.assertEqual(projected["provider_runtime_context"], "goal")
        self.assertTrue(projected["metadata_only"])
        egress = self.ns["client_safe_event"](event)
        self.assertNotIn("_agentsdock_imported_prompt_hidden", egress)
        self.assertEqual(self.ns["client_safe_event"](egress), egress)
        self.assertEqual(event["prompt"], GOAL)

    def test_legacy_projection_never_changes_real_users_or_assistant_output(self):
        self.ns["CODEX_GOAL_HISTORY_REPAIR_CACHE"].is_hidden.return_value = True
        for change in ({"imported": False}, {"run_id": "run_real"}, {"backend": "cursor"},
                       {"session_id": "other"}, {"type": "assistant_text", "text": GOAL},
                       {"provider_user_authored": True}):
            event = imported(**change)
            self.assertIs(self.ns["project_legacy_imported_provider_event"](event, "chat"), event)

    async def test_both_import_paths_retain_human_authorship_and_close_replay(self):
        items = [self.ns["codex_history_event_item"](source_user(kinds=("user.text",)))]
        session = {"id": "chat", "backend": "codex", "codex_thread_id": "thread"}
        self.ns["append_durable_event_batch"] = AsyncMock(side_effect=lambda _id, events: [
            {"seq": index} for index, _event in enumerate(events, 1)
        ])
        self.ns["append_imported_events"] = AsyncMock(side_effect=lambda _id, events: len(events))
        for name, sink in (("append_imported_history", "append_durable_event_batch"),
                           ("append_staged_imported_history", "append_imported_events")):
            await self.ns[name](session, Path("unused"), items)
            events = self.ns[sink].await_args.args[1]
            self.assertTrue(events[1][1]["provider_user_authored"])
            self.assertEqual(events[-1][0], "turn_finished")
            self.assertFalse(self.ns["is_agent_visible_event"](*events[-1]))

    async def test_reopened_import_does_not_create_running_or_unread_state(self):
        session = {"id": "chat", "latest_agent_event_seq": 1}
        self.ns["STORE"] = SimpleNamespace(sessions={"chat": session}, save=AsyncMock())
        event = imported()
        await self.ns["update_session_event_metadata"]("chat", event)
        self.assertNotIn("active_run", session)
        self.assertEqual(session["latest_agent_event_seq"], 1)
        session["active_run"] = {"run_id": "import_old"}
        self.assertEqual(self.ns["clear_imported_active_runs"](), 1)
        self.assertNotIn("active_run", session)

    async def test_accepted_goal_followup_does_not_replace_owner_or_make_unread_output(self):
        from test_goal_followup_projection_isolated import followup

        owner = {"run_id": "goal-owner", "purpose": "codex_goal_resume", "backend": "codex"}
        goal = {"status": "active", "objective": "Keep working", "tokensUsed": 900}
        session = {"id": "chat", "latest_agent_event_seq": 1, "active_run": owner, "codex_goal": goal}
        self.ns["STORE"] = SimpleNamespace(sessions={"chat": session}, save=AsyncMock())
        await self.ns["update_session_event_metadata"]("chat", followup(3))
        self.assertIs(session["active_run"], owner)
        self.assertIs(session["codex_goal"], goal)
        self.assertEqual(session["latest_agent_event_seq"], 1)

    def test_goal_followup_is_searchable_as_human_input(self):
        from test_goal_followup_projection_isolated import followup

        self.ns["HISTORY_SEARCH_EVENT_TYPES"].add("turn_steered")
        row = followup(3)
        self.assertEqual(self.ns["history_search_event_record"](row), ("user", row["prompt"]))
        self.assertIsNone(self.ns["history_search_event_record"]({**row, "provider_user_authored": False}))

    def test_prepared_cache_avoids_provider_discovery_on_reopen(self):
        self.ns["STORE"] = SimpleNamespace(sessions={"chat": {
            "backend": "codex", "codex_thread_id": "thread",
        }})
        self.ns["provider_session_identifier"] = lambda value: value
        self.ns["CODEX_GOAL_HISTORY_REPAIR_CACHE"] = SimpleNamespace(is_prepared=Mock(return_value=True))
        self.ns["find_codex_history"] = Mock(side_effect=AssertionError("unexpected provider discovery"))
        self.ns["prepare_codex_goal_history_repair"]("chat")
        self.ns["find_codex_history"].assert_not_called()

    def test_repair_callbacks_retain_legacy_text_and_classify_typed_authorship(self):
        self.ns["STORE"] = SimpleNamespace(sessions={"chat": {
            "backend": "codex", "codex_thread_id": "thread",
        }})
        cache = SimpleNamespace(is_prepared=Mock(return_value=False), prepare=Mock(return_value=False))
        self.ns.update({
            "CODEX_GOAL_HISTORY_REPAIR_CACHE": cache,
            "provider_session_identifier": lambda value: value,
            "normalized_history_sync_cursor": lambda session: {"source_path": "/unused/rollout.jsonl"},
            "events_path": lambda session: Path("/unused/events.jsonl"),
            "CODEX_SESSIONS_ROOT": Path("/unused"),
        })
        self.ns["prepare_codex_goal_history_repair"]("chat")
        normalize, classify = cache.prepare.call_args.args[-2:]
        for kinds, expected in ((("goal.internal_context",), "goal"), (("user.text",), "human"), (None, "unknown")):
            event = source_user(kinds=kinds)
            self.assertEqual(normalize(event), GOAL)
            self.assertEqual(classify(event), expected)
        non_user = {"type": "response_item", "payload": {"type": "message", "role": "assistant"}}
        self.assertIsNone(normalize(non_user))
        self.assertIsNone(classify(non_user))

    def test_new_source_proof_rebuilds_only_the_stale_search_projection(self):
        with tempfile.TemporaryDirectory(prefix="codex-goal-search-") as directory:
            path = Path(directory) / "events.jsonl"
            rows = [imported(), {"type": "assistant_text", "session_id": "chat",
                    "seq": 3, "text": "Actual answer", "run_id": "import_fixture", "imported": True}]
            path.write_text("".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows))
            connection = sqlite3.connect(":memory:")
            self.addCleanup(connection.close)
            connection.executescript(
                "CREATE TABLE history_search(text,session_id,event_id,seq,ts,role);"
                "CREATE TABLE history_search_sessions(session_id PRIMARY KEY,active);"
                "CREATE TABLE history_search_state(session_id PRIMARY KEY,inode,offset,mtime_ns);"
            )
            stamp = path.stat()
            connection.execute("INSERT INTO history_search VALUES (?,?,?,?,?,?)", (GOAL, "chat", "2", 2, None, "user"))
            connection.execute("INSERT INTO history_search_state VALUES (?,?,?,?)", ("chat", stamp.st_ino, stamp.st_size, stamp.st_mtime_ns))
            connection.commit()
            self.ns.update({
                "events_path": lambda _session: path, "fork_internal_run_ids": lambda _session: set(),
                "event_files_belong_to_session": lambda *_: True,
                "prepare_codex_goal_history_repair": Mock(),
            })
            self.ns["CODEX_GOAL_HISTORY_REPAIR_CACHE"].is_hidden.side_effect = lambda _session, event: event.get("prompt") == GOAL
            self.assertEqual(self.ns["sync_history_search_index"](connection, {"chat"}, {"chat"}), (0, 0))
            self.ns["prepare_codex_goal_history_repair"].assert_not_called()
            self.ns["HISTORY_SEARCH_REPAIR_DIRTY"].add("chat")
            self.assertEqual(self.ns["sync_history_search_index"](connection, {"chat"}, {"chat"}), (1, 1))
            self.assertEqual(connection.execute("SELECT text FROM history_search").fetchall(), [("Actual answer",)])
            self.assertNotIn("chat", self.ns["HISTORY_SEARCH_REPAIR_DIRTY"])
            self.ns["prepare_codex_goal_history_repair"].assert_called_once_with("chat")


if __name__ == "__main__":
    unittest.main()
