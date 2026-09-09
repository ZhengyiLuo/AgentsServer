"""Source-proven Claude repair: temporary fixtures and allowlisted server AST only."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import claude_history_repair as repair
from test_claude_history_metadata_isolated import load_projection, user_event


def encode(events):
    return b"".join(json.dumps(event).encode() + b"\n" for event in events)


def load_server_repair(cache):
    source = Path(__file__).with_name("agent_server.py")
    wanted = {
        "prepare_claude_history_metadata_repair",
        "project_legacy_imported_provider_event",
        "project_provider_history_event_for_egress",
    }
    nodes = [node for node in ast.parse(source.read_text()).body
             if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in nodes} == wanted
    module = ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
        module="__future__", names=[ast.alias(name="annotations")], level=0,
    ), *nodes], type_ignores=[]))
    namespace = load_projection()
    namespace.update({
        "CLAUDE_METADATA_REPAIR_CACHE": cache,
        "TIMELINE_IMPORTED_PROMPT_HIDDEN_FIELD": "_agentsdock_imported_prompt_hidden",
        "strip_all_legacy_agentsdock_provider_authority_suffixes": lambda text, **kwargs: text,
    })
    exec(compile(module, str(source), "exec"), namespace)
    return namespace


class ClaudeHistoryRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helpers = load_projection()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "provider-1.jsonl"
        self.events = self.root / "events.jsonl"
        self.cache = repair.ClaudeMetadataRepairCache()

    def normalize(self, event):
        legacy = dict(event)
        legacy.pop("isMeta", None)
        item = self.helpers["claude_history_event_item"](legacy)
        return item["text"] if item else None

    def fixture(self, source_events=None, *, previous_count=0):
        source_events = source_events or [
            user_event("Generated wrapper", isMeta=True),
            user_event("Real question"),
        ]
        raw = encode(source_events)
        self.source.write_bytes(raw)
        stat = self.source.stat()
        previous = encode(source_events[:previous_count])
        checkpoint = {
            "version": 1, "previous_present": bool(previous_count),
            "previous_source_offset": len(previous),
            "previous_source_digest": hashlib.sha256(previous).hexdigest() if previous else "",
            "cursor": {
                "version": 1, "backend": "claude", "provider_session_id": "provider-1",
                "source_path": str(self.source), "source_dev": stat.st_dev, "source_ino": stat.st_ino,
                "source_offset": len(raw), "source_digest": hashlib.sha256(raw).hexdigest(),
            },
        }
        self.wrapper = {
            "type": "turn_started", "seq": 2, "session_id": "chat-1",
            "run_id": "import_one", "backend": "claude", "imported": True,
            "provider_history_sanitized": True, "prompt": "Generated wrapper",
        }
        self.rows = [{
            "type": "history_imported", "seq": 1, "session_id": "chat-1",
            "run_id": "import_one", "backend": "claude", "provider_session_id": "provider-1",
            "source_path": str(self.source), "_history_sync_checkpoint": checkpoint,
        }, self.wrapper, {
            "type": "assistant_text", "seq": 3, "session_id": "chat-1",
            "run_id": "import_one", "backend": "claude", "imported": True, "text": "Answer",
        }, {**self.wrapper, "seq": 4, "prompt": "Real question"}, {
            "type": "turn_finished", "seq": 5, "session_id": "chat-1",
            "run_id": "import_one", "backend": "claude", "imported": True,
        }]
        self.write_events()

    def write_events(self):
        self.events.write_bytes(encode(self.rows))

    def prepare(self, **kwargs):
        return self.cache.prepare("chat-1", kwargs.get("provider", "provider-1"), self.events,
                                  kwargs.get("root", self.root), self.normalize)

    def test_exact_metadata_is_hidden_and_other_rows_are_preserved(self):
        self.fixture()
        self.assertTrue(self.prepare())
        self.assertTrue(self.cache.is_hidden("chat-1", self.wrapper))
        for changed in ({"seq": 99}, {"run_id": "import_other"}, {"prompt": "Other"},
                        {"imported": False}, {"backend": "codex"}, {"type": "assistant_text"}):
            self.assertFalse(self.cache.is_hidden("chat-1", {**self.wrapper, **changed}))
        self.assertFalse(self.cache.is_hidden("other-chat", self.wrapper))
        self.assertFalse(self.cache.is_hidden("chat-1", self.rows[3]))

    def test_any_normalized_human_match_preserves_the_user_quote(self):
        for flag in ({}, {"isMeta": False}, {"isMeta": "true"}, {"isMeta": 1}):
            with self.subTest(flag=flag):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.fixture([user_event("Generated wrapper", isMeta=True),
                              user_event(" \nGenerated wrapper\n", **flag)])
                self.prepare()
                self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_reused_metadata_is_unique_within_its_original_batch(self):
        self.fixture([user_event("Generated wrapper", isMeta=True),
                      user_event("Generated wrapper", isMeta=True)], previous_count=1)
        self.prepare()
        self.assertTrue(self.cache.is_hidden("chat-1", self.wrapper))

    def test_duplicate_metadata_within_batch_is_ambiguous(self):
        self.fixture([user_event("Generated wrapper", isMeta=True)] * 2)
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_metadata_outside_original_batch_does_not_prove_the_row(self):
        self.fixture([user_event("Generated wrapper", isMeta=True),
                      user_event("Real question")], previous_count=1)
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_uncheckpointed_and_unfinished_batches_fail_visible(self):
        for mutation in (lambda: self.rows[0].pop("_history_sync_checkpoint"),
                         lambda: self.rows.pop()):
            self.cache = repair.ClaudeMetadataRepairCache()
            self.fixture()
            mutation()
            self.write_events()
            self.prepare()
            self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_checkpoint_digest_identity_offset_and_provider_are_required(self):
        cases = [
            ("cursor", "source_digest", "0" * 64),
            ("cursor", "source_ino", -1),
            ("cursor", "source_offset", 1),
            ("cursor", "provider_session_id", "foreign"),
            ("checkpoint", "previous_present", 1),
            ("checkpoint", "previous_source_digest", "0" * 64),
        ]
        for where, field, value in cases:
            with self.subTest(field=field):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.fixture()
                checkpoint = self.rows[0]["_history_sync_checkpoint"]
                (checkpoint["cursor"] if where == "cursor" else checkpoint)[field] = value
                self.write_events()
                self.prepare()
                self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_source_content_rewrite_and_foreign_session_remain_visible(self):
        self.fixture()
        self.source.write_bytes(self.source.read_bytes().replace(b"true", b"null"))
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))
        self.cache = repair.ClaudeMetadataRepairCache()
        self.fixture()
        self.rows[0]["session_id"] = "foreign"
        self.write_events()
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_source_must_be_regular_contained_and_provider_named(self):
        self.fixture()
        outside_root = self.root / "other-root"
        outside_root.mkdir()
        self.prepare(root=outside_root)
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))
        for kind in ("symlink", "filename"):
            self.cache = repair.ClaudeMetadataRepairCache()
            self.fixture()
            alternate = self.root / ("alias.jsonl" if kind == "filename" else "link")
            if kind == "symlink":
                self.source.rename(alternate)
                self.source.symlink_to(alternate)
            else:
                self.source.rename(alternate)
                self.rows[0]["source_path"] = str(alternate)
                self.rows[0]["_history_sync_checkpoint"]["cursor"]["source_path"] = str(alternate)
                self.write_events()
            self.prepare()
            self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))
            if kind == "symlink":
                self.source.unlink()

    def test_size_line_record_and_key_bounds_fail_visible(self):
        for name, limit in (("MAX_EVENTS_BYTES", 1), ("MAX_BYTES", 1),
                            ("MAX_LINE_BYTES", 1), ("MAX_RECORDS", 1), ("MAX_KEYS", 1)):
            with self.subTest(bound=name):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.fixture()
                with patch.object(repair, name, limit):
                    self.prepare()
                self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_no_restating_or_rescanning_on_append_or_per_event_lookup(self):
        self.fixture()
        self.prepare()
        with self.events.open("ab") as stream:
            stream.write(encode([{"type": "assistant_text", "seq": 6, "text": "Live answer"}]))
        with self.source.open("ab") as stream:
            stream.write(encode([user_event("New user message")]))
        with patch.object(repair, "_stamp", side_effect=AssertionError("unexpected stat")), \
                patch.object(repair, "_records", side_effect=AssertionError("unexpected read")):
            self.assertFalse(self.prepare())
            self.assertTrue(self.cache.is_hidden("chat-1", self.wrapper))
            self.assertTrue(self.cache.signature("chat-1"))

    def test_changed_provider_does_not_reuse_the_old_proof(self):
        self.fixture()
        self.prepare()
        self.assertTrue(self.cache.is_hidden("chat-1", self.wrapper))
        self.prepare(provider="provider-2")
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_source_must_stay_stable_through_complete_scan(self):
        self.fixture()
        real_records = repair._records

        def growing_records(path, stamp):
            changed = False
            for record in real_records(path, stamp):
                yield record
                if path == self.source.resolve() and not changed:
                    changed = True
                    with self.source.open("ab") as stream:
                        stream.write(encode([user_event("Real question")]))

        with patch.object(repair, "_records", side_effect=growing_records):
            self.prepare()
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_incomplete_source_record_stays_visible_even_with_matching_digest(self):
        self.fixture()
        raw = self.source.read_bytes().rstrip(b"\n")
        self.source.write_bytes(raw)
        cursor = self.rows[0]["_history_sync_checkpoint"]["cursor"]
        cursor.update(source_offset=len(raw), source_digest=hashlib.sha256(raw).hexdigest())
        self.write_events()
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))

    def test_failed_admission_is_cached_and_lru_is_bounded(self):
        self.fixture()
        self.rows[0].pop("_history_sync_checkpoint")
        self.write_events()
        self.prepare()
        with patch.object(repair, "_stamp", side_effect=AssertionError("unexpected retry")):
            self.assertFalse(self.prepare())
        with patch.object(repair, "MAX_SESSIONS", 2):
            self.cache.prepare("chat-2", "provider-1", self.events, self.root, self.normalize)
            self.cache.prepare("chat-3", "provider-1", self.events, self.root, self.normalize)
        self.assertEqual(len(self.cache._proofs), 2)
        self.assertNotIn("chat-1", self.cache._proofs)

    def test_lookup_cached_prepare_and_forget_never_wait_for_a_scan(self):
        self.fixture()
        self.prepare()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        real_prove = repair._prove

        def blocked(*args):
            entered.set()
            if not release.wait(3):
                raise AssertionError("test scan was not released")
            return real_prove(*args)

        def quick_operations():
            self.prepare()
            self.cache.is_hidden("chat-1", self.wrapper)
            self.cache.forget("codex-chat")
            self.cache.forget("chat-2")
            finished.set()

        with patch.object(repair, "_prove", side_effect=blocked):
            worker = threading.Thread(target=lambda: self.cache.prepare(
                "chat-2", "provider-1", self.events, self.root, self.normalize))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                quick = threading.Thread(target=quick_operations)
                quick.start()
                self.assertTrue(finished.wait(1), "memory-only operations blocked behind source scan")
            finally:
                release.set()
                worker.join(3)
                if "quick" in locals():
                    quick.join(3)
        self.assertNotIn("chat-2", self.cache._proofs, "forget must cancel a pending proof swap")

    def test_server_projection_preserves_empty_boundary_answers_and_source(self):
        self.fixture()
        self.prepare()
        helpers = load_server_repair(self.cache)
        before = dict(self.wrapper)
        projected = helpers["project_legacy_imported_provider_event"](self.wrapper, "chat-1")
        self.assertEqual(projected["prompt"], "")
        self.assertTrue(projected["_agentsdock_imported_prompt_hidden"])
        egress = helpers["project_provider_history_event_for_egress"](self.wrapper, "chat-1")
        self.assertEqual(egress["type"], "turn_started")
        self.assertEqual(egress["seq"], 2)
        self.assertNotIn("_agentsdock_imported_prompt_hidden", egress)
        self.assertEqual(helpers["project_provider_history_event_for_egress"](self.rows[2], "chat-1"), self.rows[2])
        self.assertEqual(self.wrapper, before)

    def test_default_public_egress_does_not_prepare_or_read_any_source(self):
        self.fixture()
        helpers = load_server_repair(self.cache)
        with patch.object(repair, "_stamp", side_effect=AssertionError("unexpected stat")), \
                patch.object(repair, "_records", side_effect=AssertionError("unexpected source read")):
            self.assertEqual(helpers["project_provider_history_event_for_egress"](
                self.wrapper, "chat-1"), self.wrapper)

    def test_server_admission_is_scoped_and_uses_legacy_normalization(self):
        self.fixture()
        helpers = load_server_repair(self.cache)
        helpers.update({
            "STORE": SimpleNamespace(sessions={"chat-1": {"backend": "claude", "claude_session_id": "provider-1"}}),
            "provider_session_identifier": lambda value: value,
            "session_provider_id": lambda session: session.get("claude_session_id"),
            "events_path": lambda session_id: self.events,
            "CLAUDE_PROJECTS_ROOT": self.root,
        })
        helpers["prepare_claude_history_metadata_repair"]("chat-1")
        self.assertTrue(self.cache.is_hidden("chat-1", self.wrapper))
        helpers["STORE"].sessions["chat-1"]["backend"] = "codex"
        with patch.object(repair, "_stamp", side_effect=AssertionError("non-Claude source access")):
            helpers["prepare_claude_history_metadata_repair"]("chat-1")
        self.assertFalse(self.cache.is_hidden("chat-1", self.wrapper))


if __name__ == "__main__":
    unittest.main()
