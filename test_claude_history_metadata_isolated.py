"""Claude history projection checks without importing the server runtime."""

from __future__ import annotations

import ast
from collections import deque
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import unittest
from unittest.mock import Mock
from claude_history_provenance import ClaudeInterruptionTracker


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "compact_import_text",
    "is_import_boilerplate",
    "text_from_content",
    "message_text",
    "normalized_history_item",
    "normalized_history_provider_origin",
    "add_history_item",
    "normalized_history_import_limit",
    "claude_history_event_is_task_notification",
    "is_claude_task_notification_history_event",
    "claude_history_event_item",
    "append_claude_history_event",
    "parse_claude_history_events",
    "claude_transcript_preview",
    "history_item_cursor_digest",
    "history_dedup_key",
    "parse_provider_history_delta",
}
CONSTANTS = {
    "CLAUDE_TASK_NOTIFICATION_ORIGIN",
    "CLAUDE_TASK_NOTIFICATION_PROMPT_SOURCES",
    "CLAUDE_TASK_NOTIFICATION_RE",
}


def load_projection() -> dict:
    """Compile only explicit pure helpers; no server imports or module startup."""

    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = []
    found_functions = set()
    found_constants = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            selected.append(node)
            found_functions.add(node.name)
        elif isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if names and names <= CONSTANTS:
                selected.append(node)
                found_constants.update(names)
    if found_functions != FUNCTIONS or found_constants != CONSTANTS:
        raise AssertionError("The isolated history helper allowlist is incomplete")
    annotations = ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0
    )
    module = ast.fix_missing_locations(ast.Module(body=[annotations, *selected], type_ignores=[]))
    namespace = {
        "re": re,
        "datetime": datetime,
        "ClaudeInterruptionTracker": ClaudeInterruptionTracker,
        "json": json,
        "hashlib": hashlib,
        "hmac": hmac,
        "deque": deque,
        "MAX_IMPORTED_TEXT_CHARS": 100_000,
        "MAX_IMPORT_MESSAGES": 400,
        "CLAUDE_TRANSCRIPT_CWD_LINE_BYTES": 4 * 1024 * 1024,
        "BACKEND_CLAUDE": "claude",
        "BACKEND_CODEX": "codex",
        # Authority-envelope stripping is unrelated to metadata classification.
        "strip_agentsdock_generated_user_text": Mock(side_effect=lambda text, **_kwargs: text),
    }
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace


COMMAND = "<command-message>example-skill</command-message>\n<command-name>/example-skill</command-name>"
REFERENCE = "Base directory for this skill: /example/skills/example-skill\n\n# Skill reference"


def user_event(text: str, **metadata) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}, **metadata}


class ClaudeHistoryMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.projection = load_projection()

    def setUp(self) -> None:
        self.projection["strip_agentsdock_generated_user_text"].reset_mock()

    def item(self, event: dict):
        return self.projection["claude_history_event_item"](event)

    def test_generated_command_and_skill_reference_are_not_user_turns(self) -> None:
        events = [
            user_event(COMMAND, isMeta=True),
            {
                "type": "user", "isMeta": True,
                "message": {"role": "user", "content": [{"type": "text", "text": REFERENCE}]},
            },
        ]
        for event in events:
            with self.subTest(content=event["message"]["content"]):
                before = json.dumps(event, sort_keys=True)
                self.assertIsNone(self.item(event))
                self.assertEqual(json.dumps(event, sort_keys=True), before)
        self.projection["strip_agentsdock_generated_user_text"].assert_not_called()

    def test_user_quotes_and_untyped_metadata_remain_visible(self) -> None:
        for text in (COMMAND, REFERENCE, "Please explain this wrapper:\n" + COMMAND):
            for metadata in ({}, {"isMeta": False}, {"isMeta": "true"}, {"isMeta": 1}, {"origin": {"kind": "human"}}):
                with self.subTest(text=text, metadata=metadata):
                    self.assertEqual(self.item(user_event(text, **metadata)), {"kind": "user", "text": text})

    def test_compact_summary_flag_not_summary_wording_decides_user_origin(self) -> None:
        text = "This session is being continued from a previous conversation. Summary: a real user may quote this."
        self.assertIsNone(self.item(user_event(text, isCompactSummary=True)))
        for flag in (None, False, "true", 1):
            self.assertEqual(self.item(user_event(text, isCompactSummary=flag)), {"kind": "user", "text": text})

    def test_assistant_text_and_non_user_events_are_unchanged(self) -> None:
        self.assertEqual(self.item({
            "type": "assistant", "isMeta": True,
            "message": {"content": COMMAND},
        }), {"kind": "assistant", "text": COMMAND})
        self.assertIsNone(self.item({"type": "system", "message": {"content": REFERENCE}}))

    def test_existing_task_notification_filter_remains_effective(self) -> None:
        self.assertIsNone(self.item(user_event(
            "Provider task completed", origin={"kind": "task-notification"},
        )))
        self.assertEqual(self.item(user_event(
            "Provider task completed", origin={"kind": "human"},
        )), {"kind": "user", "text": "Provider task completed"})

    def test_full_parser_omits_metadata_without_consuming_message_limit(self) -> None:
        events = [
            user_event("Actual request"),
            user_event(COMMAND, isMeta=True),
            user_event(REFERENCE, isMeta=True),
            {"type": "assistant", "message": {"content": "Actual answer"}},
        ]
        self.assertEqual(self.projection["parse_claude_history_events"](events, 2), [
            {"kind": "user", "text": "Actual request"},
            {"kind": "assistant", "text": "Actual answer"},
        ])

    def test_preview_skips_generated_skill_context(self) -> None:
        events = [user_event(COMMAND, isMeta=True), user_event(REFERENCE, isMeta=True), user_event("Real preview")]
        region = b"\n".join(json.dumps(event).encode("utf-8") for event in events) + b"\n"
        self.projection["bounded_claude_transcript_regions"] = Mock(return_value=iter([region]))
        self.assertEqual(self.projection["claude_transcript_preview"](Path("unused.jsonl")), "Real preview")

    def test_delta_consumes_metadata_and_preserves_next_unseen_user(self) -> None:
        records = [
            (user_event(COMMAND, isMeta=True), 10),
            (user_event("First request"), 20),
            (user_event(REFERENCE, isMeta=True), 30),
            (user_event("Next request"), 40),
        ]
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter(records))
        items, offset, digest, blocked = self.projection["parse_provider_history_delta"](
            Path("unused.jsonl"), "claude", 0, 40, limit=1,
            expected_stat={}, previous_last_item_digest="",
        )
        self.assertEqual(items, [{"kind": "user", "text": "First request"}])
        self.assertEqual(offset, 30)
        self.assertEqual(digest, self.projection["history_item_cursor_digest"](items[0]))
        self.assertTrue(blocked)

    def test_metadata_only_delta_advances_cursor_without_changing_digest(self) -> None:
        self.projection["bounded_jsonl_records_range"] = Mock(return_value=iter([
            (user_event(COMMAND, isMeta=True), 10),
            (user_event(REFERENCE, isMeta=True), 20),
        ]))
        self.assertEqual(self.projection["parse_provider_history_delta"](
            Path("unused.jsonl"), "claude", 0, 20, limit=1,
            expected_stat={}, previous_last_item_digest="previous",
        ), ([], 20, "previous", False))


if __name__ == "__main__":
    unittest.main()
