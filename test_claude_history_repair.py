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
        "HISTORY_SEARCH_REPAIR_DIRTY": set(),
        "HISTORY_SEARCH_DIRTY": set(),
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
        legacy.pop("isCompactSummary", None)
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

    def test_compaction_summary_requires_source_metadata_not_matching_wording(self):
        self.fixture([user_event("Generated wrapper", isCompactSummary=True), user_event("Real question")])
        self.assertTrue(self.prepare())
        projected = load_server_repair(self.cache)["project_provider_history_event_for_egress"](self.wrapper, "chat-1")
        self.assertEqual(projected["prompt"], "")
        self.assertEqual(projected["provider_history_repair"], "source_proven_import")
        self.cache = repair.ClaudeMetadataRepairCache()
        self.fixture([user_event("Generated wrapper", isCompactSummary=True), user_event("Generated wrapper")])
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


class ClaudeInterruptionRepairTests(unittest.TestCase):
    PROVIDER = "23456789-2345-4345-8345-23456789abcd"
    PROMPT = "456789ab-4567-4567-8567-456789abcdef"
    MARKER = "[Request interrupted by user for tool use]"
    TIME = "2026-09-09T03:16:54.515Z"

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / (self.PROVIDER + ".jsonl")
        self.events = self.root / "events.jsonl"
        self.cache = repair.ClaudeMetadataRepairCache()

    def raw(self, number, text="Real question", **extra):
        return {
            "type": "user", "uuid": f"12345678-1234-4234-8234-{number:012d}",
            "sessionId": self.PROVIDER, "timestamp": self.TIME,
            "promptId": self.PROMPT,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
            **extra,
        }

    def source_rows(self):
        user = self.raw(1)
        assistant = self.raw(2, type="assistant", parentUuid=user["uuid"],
                             message={"role": "assistant", "content": [{"type": "tool_use", "id": "tool-one"}]})
        attachment = self.raw(3, type="attachment", parentUuid=assistant["uuid"], message=None)
        marker = self.raw(4, self.MARKER, parentUuid=attachment["uuid"])
        return [user, assistant, attachment, marker]

    @staticmethod
    def normalize(event):
        # Reproduce the text-only legacy importer, not the new interruption kind.
        content = (event.get("message") or {}).get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            return "\n".join(part["text"] for part in content
                             if isinstance(part, dict) and part.get("type") == "text"
                             and isinstance(part.get("text"), str)).strip()
        return None

    def checkpoint(self, records, start, end, run="import_one", seq=18525):
        previous, through = encode(records[:start]), encode(records[:end])
        info = self.source.stat()
        return {
            "type": "history_imported", "session_id": "chat-1", "backend": "claude",
            "provider_session_id": self.PROVIDER, "source_path": str(self.source),
            "seq": seq, "run_id": run,
            "_history_sync_checkpoint": {
                "version": 1, "previous_present": bool(start),
                "previous_source_offset": len(previous),
                "previous_source_digest": hashlib.sha256(previous).hexdigest() if start else "",
                "cursor": {
                    "version": 1, "backend": "claude", "provider_session_id": self.PROVIDER,
                    "source_path": str(self.source), "source_dev": info.st_dev, "source_ino": info.st_ino,
                    "source_offset": len(through), "source_digest": hashlib.sha256(through).hexdigest(),
                },
            },
        }

    def fixture(self, records=None, start=3, native=()):
        records = self.source_rows() if records is None else records
        self.source.write_bytes(encode(records))
        self.target = {
            "id": "stable-ui-event", "seq": 18526, "run_id": "import_one",
            "type": "turn_started", "session_id": "chat-1", "backend": "claude",
            "imported": True, "provider_history_sanitized": True,
            "ts": "2026-09-09T03:17:00Z", "prompt": self.MARKER,
        }
        self.rows = [*native, self.checkpoint(records, start, len(records)), self.target, {
            "type": "turn_finished", "seq": 18527, "run_id": "import_one",
            "session_id": "chat-1", "backend": "claude", "imported": True,
        }]
        self.events.write_bytes(encode(self.rows))
        return records

    def prepare(self):
        return self.cache.prepare("chat-1", self.PROVIDER, self.events, self.root, self.normalize)

    def correction(self):
        return self.cache.project_interruption("chat-1", self.target)

    def native_steer(self):
        common = {"session_id": "chat-1", "backend": "claude", "ts": self.TIME}
        return [
            {**common, "type": "turn_queue_run_now", "queued_id": "queue-one", "interrupted_run_id": "run-old"},
            {**common, "type": "turn_finished", "run_id": "run-old", "stopped": True,
             "exit_code": None, "provider_session_id": self.PROVIDER},
            {**common, "type": "turn_started", "run_id": "run-next", "queued_id": "queue-one",
             "steer_interrupted_run_id": "run-old"},
        ]

    def test_source_proven_marker_corrects_stable_ui_row_and_keeps_source_identity(self):
        records = self.fixture()
        original = dict(self.target)
        self.assertTrue(self.prepare())
        projected = self.correction()
        for field in ("id", "seq", "run_id", "session_id", "imported"):
            self.assertEqual(projected[field], original[field])
        self.assertEqual(projected["type"], "provider_interruption")
        self.assertEqual(projected["ts"], self.TIME)
        self.assertEqual(projected["provider_origin"], {
            "provider": "claude", "kind": "interruption", "event_id": records[-1]["uuid"],
            "session_id": self.PROVIDER, "timestamp": self.TIME,
            "parent_event_id": records[-1]["parentUuid"], "prompt_id": self.PROMPT,
            "cause": "unknown",
        })
        self.assertNotIn("prompt", projected)
        self.assertNotIn("text", projected)
        self.assertFalse(self.cache.is_hidden("chat-1", self.target))
        self.assertEqual(self.target, original)
        helpers = load_server_repair(self.cache)
        self.assertEqual(helpers["project_provider_history_event_for_egress"](self.target, "chat-1"), projected)
        for companion in (self.rows[0], self.rows[-1]):
            self.assertEqual(helpers["project_provider_history_event_for_egress"](companion, "chat-1"),
                             {**companion, "imported": True, "metadata_only": True})
            self.assertNotIn("metadata_only", companion)

    def test_genuine_marker_in_another_batch_is_preserved_without_blocking_proof(self):
        human = self.raw(8, self.MARKER, promptId="56789abc-5678-4678-8678-56789abcdef0")
        records = self.fixture([human, *self.source_rows()], start=4)
        quote = {**self.target, "id": "real-quote", "seq": 11, "run_id": "import_quote"}
        self.rows[:0] = [self.checkpoint(records, 0, 1, "import_quote", 10), quote, {
            "type": "turn_finished", "seq": 12, "run_id": "import_quote",
            "session_id": "chat-1", "backend": "claude", "imported": True,
        }]
        self.events.write_bytes(encode(self.rows))
        self.prepare()
        self.assertIsNotNone(self.correction())
        self.assertIsNone(self.cache.project_interruption("chat-1", quote))
        self.assertFalse(self.cache.is_hidden("chat-1", quote))

    def test_genuine_same_marker_inside_batch_blocks_correction(self):
        records = self.source_rows()
        records.append(self.raw(5, self.MARKER, parentUuid=records[-1]["uuid"],
                                promptId="56789abc-5678-4678-8678-56789abcdef0"))
        self.fixture(records)
        self.prepare()
        self.assertIsNone(self.correction())
        self.assertFalse(self.cache.is_hidden("chat-1", self.target))

    def test_duplicate_proven_marker_or_durable_target_is_ambiguous(self):
        for duplicate in ("source", "target"):
            with self.subTest(duplicate=duplicate):
                self.cache = repair.ClaudeMetadataRepairCache()
                records = self.source_rows()
                if duplicate == "source":
                    records.append(self.raw(5, self.MARKER, isMeta=True))
                self.fixture(records)
                if duplicate == "target":
                    self.rows.insert(-1, {**self.target, "id": "other-ui-event"})
                    self.events.write_bytes(encode(self.rows))
                self.prepare()
                self.assertIsNone(self.correction())
                self.assertFalse(self.cache.is_hidden("chat-1", self.target))

    def test_missing_anchor_changed_prompt_parent_provider_or_multiblock_is_not_proof(self):
        for changed in ("no_anchor", "prompt", "parent", "provider", "multi_block", "time"):
            with self.subTest(changed=changed):
                self.cache = repair.ClaudeMetadataRepairCache()
                records = self.source_rows()
                if changed == "no_anchor":
                    records = records[-1:]
                elif changed == "prompt":
                    records[-1]["promptId"] = "56789abc-5678-4678-8678-56789abcdef0"
                elif changed == "parent":
                    records[-1]["parentUuid"] = records[0]["uuid"]
                elif changed == "provider":
                    records[-1]["sessionId"] = "56789abc-5678-4678-8678-56789abcdef0"
                elif changed == "multi_block":
                    records[-1]["message"]["content"].append({"type": "text", "text": "My quote"})
                else:
                    records[-1]["timestamp"] = "yesterday"
                self.fixture(records, start=0)
                self.prepare()
                self.assertIsNone(self.correction())

    def test_steer_requires_all_three_nearby_matching_native_events(self):
        self.fixture(native=self.native_steer())
        self.prepare()
        self.assertEqual(self.correction()["provider_origin"]["cause"], "steer")
        changes = [(index, None, None) for index in range(3)] + [
            (1, "stopped", False), (1, "exit_code", 1), (1, "provider_session_id", "foreign"),
            (2, "queued_id", "foreign"), (2, "steer_interrupted_run_id", "foreign"),
            (0, "backend", "codex"), (0, "session_id", "foreign"),
            (2, "ts", "2026-09-09T03:17:54.515Z"), (2, "ts", "not-a-time"),
        ]
        for index, field, value in changes:
            with self.subTest(index=index, field=field):
                self.cache = repair.ClaudeMetadataRepairCache()
                native = self.native_steer()
                if field is None:
                    native.pop(index)
                else:
                    native[index][field] = value
                self.fixture(native=native)
                self.prepare()
                self.assertEqual(self.correction()["provider_origin"]["cause"], "unknown")

    def test_checkpoint_bounds_and_negative_admission_remain_fail_visible(self):
        for bound in ("MAX_EVENTS_BYTES", "MAX_BYTES", "MAX_LINE_BYTES", "MAX_RECORDS", "MAX_KEYS", "MAX_TARGETS"):
            with self.subTest(bound=bound):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.fixture()
                with patch.object(repair, bound, 0):
                    self.prepare()
                with patch.object(repair, "_stamp", side_effect=AssertionError("negative cache rescan")):
                    self.assertFalse(self.prepare())
                    self.assertIsNone(self.correction())
        self.cache = repair.ClaudeMetadataRepairCache()
        self.fixture()
        self.rows[0]["_history_sync_checkpoint"]["cursor"]["source_digest"] = "0" * 64
        self.events.write_bytes(encode(self.rows))
        self.prepare()
        self.assertIsNone(self.correction())

    def test_append_and_event_egress_do_not_restat_or_rescan_and_origins_are_copied(self):
        self.fixture()
        self.prepare()
        signature = self.cache.signature("chat-1")
        self.assertTrue(signature)
        with self.source.open("ab") as stream:
            stream.write(encode([self.raw(6, "New user message")]))
        with self.events.open("ab") as stream:
            stream.write(encode([{"type": "assistant_text", "text": "Live output"}]))
        with patch.object(repair, "_stamp", side_effect=AssertionError("unexpected stat")), \
                patch.object(repair, "_records", side_effect=AssertionError("unexpected source scan")):
            self.assertFalse(self.prepare())
            projected = self.correction()
            projected["provider_origin"]["cause"] = "stop"
            self.assertEqual(self.correction()["provider_origin"]["cause"], "unknown")
            self.assertEqual(self.cache.signature("chat-1"), signature)
            self.assertTrue(self.cache.project_event("chat-1", self.rows[0])["metadata_only"])
            for change in ({"seq": 18528}, {"run_id": "import_other"}, {"session_id": "foreign"},
                           {"backend": "codex"}, {"prompt": "Different"}, {"imported": False}):
                self.assertIsNone(self.cache.project_interruption("chat-1", {**self.target, **change}))
        self.cache.forget("chat-1")
        self.assertFalse(self.cache.signature("chat-1"))
        self.assertIsNone(self.correction())


    def test_mixed_or_ambiguous_batches_do_not_mark_companions_metadata_only(self):
        for kind in ("assistant_text", "tool_use", "unknown", "turn_started", "turn_finished"):
            with self.subTest(kind=kind):
                self.cache = repair.ClaudeMetadataRepairCache()
                self.fixture()
                extra = {**self.target, "type": kind, "seq": 18527, "prompt": "Real user text", "text": "Output"}
                self.rows[-1]["seq"] = 18528
                self.rows.insert(-1, extra)
                self.events.write_bytes(encode(self.rows))
                self.prepare()
                self.assertIsNotNone(self.correction())
                self.assertIsNone(self.cache.project_event("chat-1", self.rows[0]))
                self.assertIsNone(self.cache.project_event("chat-1", self.rows[-1]))

    def test_companion_correction_requires_exact_cached_identity(self):
        self.fixture()
        self.prepare()
        for companion in (self.rows[0], self.rows[-1]):
            for changed in ({"seq": 42}, {"run_id": "run-native"}, {"backend": "codex"},
                            {"session_id": "foreign"}, {"provider_session_id": "foreign"}):
                self.assertIsNone(self.cache.project_event("chat-1", {**companion, **changed}))
        self.assertIsNone(self.cache.project_event("chat-1", {**self.rows[-1], "imported": False}))

    def test_fresh_enrichment_reads_only_native_events_and_copies_allowlisted_origins(self):
        origin = {"provider": "claude", "kind": "interruption", "event_id": self.raw(4)["uuid"],
                  "session_id": self.PROVIDER, "timestamp": self.TIME, "cause": "stop", "private": "omit"}
        self.events.write_bytes(encode(self.native_steer()))
        records = repair._records

        def native_only(path, stamp):
            self.assertEqual(path, self.events)
            return records(path, stamp)

        with patch.object(repair, "_records", side_effect=native_only):
            result = repair.enrich_interruption_origins(self.events, self.PROVIDER, [origin], session_id="chat-1")
        self.assertEqual(result, [{key: value for key, value in {**origin, "cause": "steer"}.items() if key != "private"}])
        self.assertEqual(origin["cause"], "stop")
        self.assertFalse(self.source.exists(), "enrichment never needs a provider transcript")

    def test_fresh_enrichment_failure_bounds_and_empty_inputs_fail_unknown_without_retries(self):
        origin = {"provider": "claude", "kind": "interruption", "event_id": self.raw(4)["uuid"],
                  "session_id": self.PROVIDER, "timestamp": self.TIME, "cause": "steer"}
        result = repair.enrich_interruption_origins(self.events, self.PROVIDER, [origin], session_id="chat-1")
        self.assertEqual(result[0]["cause"], "unknown")
        self.events.write_bytes(encode(self.native_steer()))
        for bound in ("MAX_EVENTS_BYTES", "MAX_LINE_BYTES", "MAX_RECORDS"):
            with self.subTest(bound=bound), patch.object(repair, bound, 1):
                result = repair.enrich_interruption_origins(self.events, self.PROVIDER, [origin], session_id="chat-1")
                self.assertEqual(result[0]["cause"], "unknown")
        with patch.object(repair, "_stamp", side_effect=AssertionError("empty/invalid origins caused I/O")):
            self.assertEqual(repair.enrich_interruption_origins(self.events, self.PROVIDER, [], session_id="chat-1"), [])
            self.assertEqual(repair.enrich_interruption_origins(self.events, self.PROVIDER, [{}], session_id="chat-1"), [{}])
            with patch.object(repair, "MAX_TARGETS", 0):
                self.assertEqual(repair.enrich_interruption_origins(self.events, self.PROVIDER, [origin], session_id="chat-1"), [])


if __name__ == "__main__":
    unittest.main()
