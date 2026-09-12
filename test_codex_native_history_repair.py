"""Checkpoint/native ownership proof using temporary files and AST public parsers only."""
import ast
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

from codex_history_repair import CodexNativeHistoryRepairCache, filter_native_codex_history_items, _native_assistant_text
from test_codex_goal_history_isolated import load_projection

PROVIDER = "11111111-2222-3333-4444-555555555555"


class CodexNativeHistoryRepairTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = self.root / "events.jsonl"
        self.source = self.root / f"rollout-{PROVIDER}.jsonl"
        self.cache = CodexNativeHistoryRepairCache()
        self.parse = load_projection()["codex_history_event_item"]
        self.raw, self.native = [], []
        for number, (prompt, answer) in enumerate((("Genuine human request", "Original answer"), ("Scheduled input", "Scheduled report")), 1):
            turn, run = f"turn-{number}", f"native-{number}"
            for role, text in (("user", prompt), ("assistant", answer)):
                self.raw.append({"type": "response_item", "timestamp": f"2026-09-11T12:0{number}:0{role == 'assistant' and 1 or 0}.123Z",
                    "payload": {"type": "message", "role": role, "id": f"item-{number}-{role}",
                        "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn, "content_item_kinds": ["user.text"] if role == "user" else []}}})
            fields = {"job_id": "schedule", "purpose": "scheduled_job"} if number == 2 else {}
            self.native.extend([
                {"seq": number * 3 - 2, "id": f"native-input-{number}", "run_id": run, "type": "turn_started", "prompt": prompt, **fields},
                {"seq": number * 3 - 1, "id": f"native-answer-{number}", "run_id": run, "type": "assistant_text", "text": answer, **fields},
                {"seq": number * 3, "id": f"native-end-{number}", "run_id": run, "type": "turn_finished", "backend": "codex",
                    "transport": "app-server", "provider_thread_id": PROVIDER, "provider_turn_id": turn, "result_text": answer, **fields}])
        self.fixture()

    def fixture(self, mutate_checkpoint=None):
        source = [{"type": "session_meta", "payload": {"id": PROVIDER}}, *self.raw]
        raw = b"".join((json.dumps(row) + "\n").encode() for row in source)
        self.source.write_bytes(raw)
        stat = self.source.stat()
        checkpoint = {"version": 1, "previous_present": False, "previous_source_offset": 0, "previous_source_digest": "", "cursor": {
            "version": 1, "backend": "codex", "provider_session_id": PROVIDER, "source_path": str(self.source),
            "source_dev": stat.st_dev, "source_ino": stat.st_ino, "source_offset": len(raw), "source_digest": hashlib.sha256(raw).hexdigest()}}
        if mutate_checkpoint:
            mutate_checkpoint(checkpoint)
        self.imports = [{"seq": index + 101, "id": f"import-{index}", "run_id": "import_fixture", "backend": "codex", "imported": True,
            "type": "turn_started" if row["payload"]["role"] == "user" else "assistant_text", "ts": row["timestamp"],
            "provider_user_authored": row["payload"]["role"] == "user", "provider_history_sanitized": True,
            "prompt" if row["payload"]["role"] == "user" else "text": row["payload"]["content"][0]["text"]} for index, row in enumerate(self.raw)]
        rows = [*self.native, {"seq": 100, "type": "history_imported", "run_id": "import_fixture", "backend": "codex",
            "provider_session_id": PROVIDER, "source_path": str(self.source), "_history_sync_checkpoint": checkpoint}, *self.imports,
            {"seq": 200, "type": "turn_finished", "run_id": "import_fixture", "backend": "codex", "imported": True}]
        self.events.write_text("".join(json.dumps({"session_id": "chat", **row}) + "\n" for row in rows))

    def prepare(self):
        self.cache.prepare("chat", PROVIDER, self.events, self.source, self.root, self.parse)

    def test_exact_human_and_scheduled_copies_are_hidden_originals_unchanged(self):
        before = self.events.read_bytes(), self.source.read_bytes()
        self.prepare()
        projected = [self.cache.project_event("chat", row) for row in self.imports]
        self.assertTrue(all(projected))
        self.assertTrue(projected[0]["provider_user_authored"])
        self.assertEqual(projected[0]["prompt"], "")
        self.assertEqual(projected[2]["provider_origin"]["turn_id"], "turn-2")
        self.assertTrue(all(self.cache.project_event("chat", row) is None for row in self.native))
        self.assertEqual(before, (self.events.read_bytes(), self.source.read_bytes()))

    def test_unowned_same_text_different_turn_and_changed_import_stay_visible(self):
        self.raw.append({**self.raw[0], "timestamp": "2026-09-11T12:09:00Z", "payload": {**self.raw[0]["payload"],
            "id": "other-source-item", "internal_chat_message_metadata_passthrough": {"turn_id": "unowned-turn", "content_item_kinds": ["user.text"]}}})
        self.fixture(); self.prepare()
        self.assertIsNone(self.cache.project_event("chat", self.imports[-1]))
        self.assertIsNone(self.cache.project_event("chat", {**self.imports[0], "prompt": "Changed genuine text"}))
        self.assertIsNone(self.cache.project_event("different-chat", self.imports[0]))

    def test_tampered_checkpoint_and_wrong_terminal_thread_fail_visible(self):
        self.fixture(lambda value: value["cursor"].update(source_digest="0" * 64)); self.prepare()
        self.assertFalse(self.cache.signature("chat"))
        self.cache.forget("chat")
        for event in self.native:
            if event["type"] == "turn_finished":
                event["provider_thread_id"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        self.fixture(); self.prepare()
        self.assertFalse(self.cache.signature("chat"))

    def test_forward_verified_items_keep_unowned_message_and_no_source_rescan(self):
        items = [self.parse(row) for row in self.raw]
        extra = {**items[0], "provider_origin": {**items[0]["provider_origin"], "turn_id": "other-turn"}}
        result = filter_native_codex_history_items("chat", PROVIDER, self.events, [*items, extra])
        self.assertEqual(result[-1], extra)
        self.assertEqual([item["text"] for item in result[:-1]], ["", ""])
        self.assertTrue(all(item["provider_history_repair"] == "source_proven_native_replay" for item in result[:-1]))
        self.cache.prepare("chat", PROVIDER, self.events, None, self.root, self.parse)
        self.cache.forget("chat")
        self.prepare()
        self.assertEqual(len(self.cache.signature("chat")), 4)

    def test_assistant_normalization_matches_actual_native_cleaner(self):
        tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        selected = [node for node in tree.body if (
            isinstance(node, ast.FunctionDef) and node.name == "clean_assistant_text"
        ) or (isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "LEADING_DECORATION_RE" for target in node.targets
        ))]
        self.assertEqual(len(selected), 2)
        namespace = {"re": re}
        exec(compile(ast.Module(body=selected, type_ignores=[]), "native-cleaner", "exec"), namespace)
        for text in ("✅ Scheduled report", "  :white_check_mark: Report\n⚠️ Detail", "  Unchanged text  ", "Text ✅ remains", "✅"):
            self.assertEqual(_native_assistant_text(text), namespace["clean_assistant_text"](text))

    def test_same_item_decorated_scheduled_assistant_repair_and_import_filter(self):
        self.raw[3]["payload"]["content"][0]["text"] = "✅ Scheduled report"
        self.native[4]["item_id"] = self.raw[3]["payload"]["id"]
        self.fixture()
        before = self.events.read_bytes(), self.source.read_bytes()
        self.prepare()
        projected = self.cache.project_event("chat", self.imports[3])
        self.assertEqual(projected["text"], "")
        self.assertEqual(projected["provider_origin"]["native_event_id"], self.native[4]["id"])
        self.assertEqual(projected["provider_origin"]["source_text_sha256"], hashlib.sha256("✅ Scheduled report".encode()).hexdigest())
        self.assertEqual(projected["ts"], self.imports[3]["ts"])
        items = [self.parse(row) for row in self.raw]
        self.assertEqual(len(filter_native_codex_history_items("chat", PROVIDER, self.events, items)), 2)
        self.assertTrue(all(self.cache.project_event("chat", row) is None for row in self.native))
        self.assertEqual(before, (self.events.read_bytes(), self.source.read_bytes()))

    def test_decorated_assistant_requires_same_public_item_and_complete_body(self):
        self.raw[3]["payload"]["content"][0]["text"] = "✅ Scheduled report"
        for item_id, event_type, text in (
            (None, "assistant_text", "Scheduled report"),
            ("different-item", "assistant_text", "Scheduled report"),
            ("item-2-assistant", "reasoning_summary", "Scheduled report"),
            ("item-2-assistant", "assistant_text", "Scheduled report changed"),
        ):
            with self.subTest(item_id=item_id, event_type=event_type, text=text):
                self.native[4].update(item_id=item_id, type=event_type, text=text)
                self.fixture(); self.cache.forget("chat"); self.prepare()
                self.assertIsNone(self.cache.project_event("chat", self.imports[3]))
                item = self.parse(self.raw[3])
                self.assertEqual(filter_native_codex_history_items("chat", PROVIDER, self.events, [item]), [item])

    def test_user_decorations_are_not_assistant_normalization_credits(self):
        self.raw[2]["payload"]["content"][0]["text"] = "✅ Scheduled input"
        self.native[3]["item_id"] = self.raw[2]["payload"]["id"]
        self.fixture(); self.prepare()
        self.assertIsNone(self.cache.project_event("chat", self.imports[2]))
        item = self.parse(self.raw[2])
        self.assertEqual(filter_native_codex_history_items("chat", PROVIDER, self.events, [item]), [item])


if __name__ == "__main__":
    unittest.main()
