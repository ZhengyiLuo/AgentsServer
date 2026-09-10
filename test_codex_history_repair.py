"""Pure, temporary-file tests; never import or start the AgentsServer runtime."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_history_repair import CodexGoalHistoryRepairCache


PROVIDER = "01a084e2-bbf4-7543-82e2-a40e3e65737e"


def user(text, kind="goal.internal_context"):
    return {"type": "response_item", "payload": {"type": "message", "role": "user",
        "content": [{"type": "input_text", "text": text}],
        "internal_chat_message_metadata_passthrough": {"content_item_kinds": [kind]}}}


def normalize(event):
    payload = event.get("payload") or {}
    if payload.get("role") != "user":
        return None
    return "\n".join(block["text"] for block in payload.get("content", [])).strip()


def classify(event):
    if normalize(event) is None:
        return None
    kinds = (event["payload"].get("internal_chat_message_metadata_passthrough") or {}).get("content_item_kinds")
    return "goal" if kinds == ["goal.internal_context"] else "human" if kinds == ["user.text"] else "unknown"


class CodexHistoryRepairTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / f"rollout-2026-09-08T23-37-26-{PROVIDER}.jsonl"
        self.events = self.root / "events.jsonl"
        self.cache = CodexGoalHistoryRepairCache()

    def fixture(self, source_items=None, prompts=None, mutate_batch=None, mutate_target=None):
        items = [{"type": "session_meta", "payload": {"id": PROVIDER}}, *(source_items or [user("goal")])]
        raw = b"".join((json.dumps(item) + "\n").encode() for item in items)
        self.source.write_bytes(raw)
        stamp = self.source.stat()
        batch = {"type": "history_imported", "seq": 1, "session_id": "chat", "run_id": "import_fixture",
            "backend": "codex", "provider_session_id": PROVIDER, "source_path": str(self.source),
            "_history_sync_checkpoint": {"version": 1, "previous_present": False,
                "previous_source_offset": 0, "previous_source_digest": "", "cursor": {
                    "version": 1, "backend": "codex", "provider_session_id": PROVIDER,
                    "source_path": str(self.source), "source_dev": stamp.st_dev, "source_ino": stamp.st_ino,
                    "source_offset": len(raw), "source_digest": hashlib.sha256(raw).hexdigest()}}}
        if mutate_batch:
            mutate_batch(batch)
        targets = [{"type": "turn_started", "seq": index + 2, "session_id": "chat",
            "id": f"event-{index}", "run_id": "import_fixture", "backend": "codex",
            "imported": True, "prompt": text} for index, text in enumerate(prompts or ["goal"])]
        if mutate_target:
            mutate_target(targets[0])
        terminal = {"type": "turn_finished", "seq": len(targets) + 2, "session_id": "chat",
                    "run_id": "import_fixture", "backend": "codex", "imported": True}
        self.events.write_text("".join(json.dumps(event) + "\n" for event in [batch, *targets, terminal]))
        return targets

    def prepare(self, source=None):
        return self.cache.prepare("chat", PROVIDER, self.events,
            self.source if source is None else source, self.root, normalize, classify)

    def histories(self, entries):
        """Several exact checkpoint sources in one temporary chat ledger."""
        ledger, sources, targets = [], {}, {}
        for index, (provider, content) in enumerate(entries):
            source = self.root / f"rollout-history-{provider}.jsonl"
            records = [{"type": "session_meta", "payload": {"id": provider}}, *content]
            raw = b"".join((json.dumps(record) + "\n").encode() for record in records)
            source.write_bytes(raw)
            stamp = source.stat()
            run = "import_" + provider.replace("-", "")
            seq = 3 * index + 1
            batch = {"type": "history_imported", "seq": seq, "session_id": "chat",
                "run_id": run, "backend": "codex", "provider_session_id": provider,
                "source_path": str(source), "_history_sync_checkpoint": {
                    "version": 1, "previous_present": False, "previous_source_offset": 0,
                    "previous_source_digest": "", "cursor": {"version": 1, "backend": "codex",
                        "provider_session_id": provider, "source_path": str(source),
                        "source_dev": stamp.st_dev, "source_ino": stamp.st_ino,
                        "source_offset": len(raw), "source_digest": hashlib.sha256(raw).hexdigest()}}}
            target = {"type": "turn_started", "id": "event-" + provider, "seq": seq + 1,
                "session_id": "chat", "run_id": run, "backend": "codex", "imported": True,
                "prompt": normalize(content[0])}
            ledger.extend([batch, target, {"type": "turn_finished", "seq": seq + 2,
                "session_id": "chat", "run_id": run, "backend": "codex", "imported": True}])
            sources[provider], targets[provider] = source, target
        self.events.write_text("".join(json.dumps(record) + "\n" for record in ledger))
        return sources, targets

    def prepare_current(self, provider, source=None):
        return self.cache.prepare("chat", provider, self.events, source,
            self.root, normalize, classify)

    def test_checkpoint_proves_repeated_runtime_messages_without_modifying_logs(self):
        targets = self.fixture([user("goal"), user("goal")], ["goal", "goal"])
        before = self.source.read_bytes(), self.events.read_bytes()
        self.assertTrue(self.prepare())
        self.assertTrue(all(self.cache.is_hidden("chat", target) for target in targets))
        self.assertEqual(before, (self.source.read_bytes(), self.events.read_bytes()))

    def test_unknown_or_human_identical_text_stays_visible(self):
        for kind in ("user.text", "unknown"):
            with self.subTest(kind=kind):
                self.cache.forget("chat")
                target = self.fixture([user("goal"), user("goal", kind)])[0]
                self.prepare()
                self.assertFalse(self.cache.is_hidden("chat", target))

    def test_only_exact_session_run_sequence_and_text_are_suppressed(self):
        target = self.fixture()[0]
        self.prepare()
        for field, value in (("session_id", "other"), ("run_id", "import_other"), ("seq", 8),
                             ("prompt", "other"), ("provider_user_authored", True), ("imported", False)):
            self.assertFalse(self.cache.is_hidden("chat", {**target, field: value}))
        self.assertFalse(self.cache.is_hidden("other", target))

    def test_missing_checkpoint_and_tampered_digest_fail_visible(self):
        for mutate in (lambda event: event.pop("_history_sync_checkpoint"),
                       lambda event: event["_history_sync_checkpoint"]["cursor"].update(source_digest="0" * 64),
                       lambda event: event["_history_sync_checkpoint"]["cursor"].update(source_ino=0)):
            self.cache.forget("chat")
            target = self.fixture(mutate_batch=mutate)[0]
            self.prepare()
            self.assertFalse(self.cache.is_hidden("chat", target))

    def test_incomplete_batch_or_duplicate_import_target_is_not_proven(self):
        target = self.fixture(prompts=["goal", "goal"])[0]
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat", target))
        self.cache.forget("chat")
        target = self.fixture()[0]
        lines = self.events.read_text().splitlines()
        self.events.write_text("\n".join(lines[:-1]) + "\n")
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat", target))

    def test_wrong_provider_header_never_proves_renamed_rollout(self):
        target = self.fixture()[0]
        text = self.source.read_text().replace(PROVIDER, "00000000-0000-0000-0000-000000000000")
        self.source.write_text(text)
        events = [json.loads(line) for line in self.events.read_text().splitlines()]
        events[0]["_history_sync_checkpoint"]["cursor"]["source_digest"] = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.events.write_text("".join(json.dumps(event) + "\n" for event in events))
        self.prepare()
        self.assertFalse(self.cache.is_hidden("chat", target))

    def test_source_outside_configured_root_and_symlink_are_rejected(self):
        target = self.fixture()[0]
        other_root = self.root / "other"
        other_root.mkdir()
        self.cache.prepare("chat", PROVIDER, self.events, self.source, other_root, normalize, classify)
        self.assertFalse(self.cache.is_hidden("chat", target))
        self.cache.forget("chat")
        alias = self.root / f"other-{PROVIDER}.jsonl"
        alias.symlink_to(self.source)
        self.cache.prepare("chat", PROVIDER, self.events, alias, self.root, normalize, classify)
        self.assertFalse(self.cache.is_hidden("chat", target))

    def test_positive_and_negative_preparation_are_cached_without_hot_path_io(self):
        target = self.fixture()[0]
        self.prepare()
        with patch("codex_history_repair._prove", side_effect=AssertionError("repeated source scan")):
            self.assertFalse(self.prepare())
            self.assertTrue(self.cache.is_prepared("chat", PROVIDER))
            self.assertTrue(self.cache.is_hidden("chat", target))
            self.assertTrue(self.cache.signature("chat"))
        self.cache.forget("chat")
        self.cache.prepare("chat", PROVIDER, self.events, None, self.root, normalize, classify)
        with patch("codex_history_repair._prove", side_effect=AssertionError("repeated missing-source scan")):
            self.assertFalse(self.prepare())
            self.assertTrue(self.cache.is_prepared("chat", PROVIDER))
            self.assertFalse(self.cache.is_hidden("chat", target))

    def test_forget_during_preparation_prevents_resurrection(self):
        self.fixture()
        from codex_history_repair import _prove
        def prepare_and_forget(*args):
            proof = _prove(*args)
            self.cache.forget("chat")
            return proof
        with patch("codex_history_repair._prove", side_effect=prepare_and_forget):
            self.prepare()
        self.assertFalse(self.cache.is_prepared("chat", PROVIDER))

    def test_provider_rotation_retains_exact_historical_proofs_without_hiding_new_users(self):
        target = self.fixture()[0]
        self.prepare()
        signature = self.cache.signature("chat")
        new_provider = "11111111-2222-3333-4444-555555555555"
        self.assertFalse(self.cache.prepare("chat", new_provider, self.events, None,
            self.root, normalize, classify))
        self.assertTrue(self.cache.is_prepared("chat", new_provider))
        self.assertTrue(self.cache.is_hidden("chat", target))
        self.assertFalse(self.cache.is_hidden("chat", {**target, "run_id": "import_new"}))
        self.assertEqual(signature, self.cache.signature("chat"))
        self.cache.forget("chat")
        self.assertFalse(self.cache.is_hidden("chat", target))

    def test_cold_rotated_chat_repairs_two_prior_recorded_sources_without_discovery(self):
        previous = "11111111-2222-3333-4444-555555555555"
        current = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        sources, targets = self.histories([(PROVIDER, [user("First goal")]), (previous, [user("Second goal")])])
        from codex_history_repair import _records
        opened = []
        def records(path, stamp):
            opened.append(path)
            yield from _records(path, stamp)
        with patch("codex_history_repair._records", side_effect=records), \
                patch.object(Path, "glob", side_effect=AssertionError("unexpected root discovery")), \
                patch.object(Path, "rglob", side_effect=AssertionError("unexpected root discovery")):
            self.assertTrue(self.prepare_current(current))
        self.assertEqual(opened, [self.events, sources[previous].resolve(), sources[PROVIDER].resolve()])
        self.assertTrue(all(self.cache.is_hidden("chat", target) for target in targets.values()))
        with patch("codex_history_repair._records", side_effect=AssertionError("hot-path I/O")):
            self.assertFalse(self.prepare_current(current))
            self.assertTrue(all(self.cache.is_hidden("chat", target) for target in targets.values()))

    def test_prior_path_cap_keeps_only_two_newest_prior_groups_eligible(self):
        oldest = "11111111-1111-1111-1111-111111111111"
        recent = "22222222-2222-2222-2222-222222222222"
        current = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        sources, targets = self.histories([
            (oldest, [user("Old")]), (recent, [user("Recent")]), (PROVIDER, [user("Newest")]),
            (current, [user("Current")]),
        ])
        from codex_history_repair import _records
        with patch("codex_history_repair._records", wraps=_records) as reads:
            self.prepare_current(current, sources[current])
        self.assertEqual([call.args[0] for call in reads.call_args_list],
            [self.events, sources[current].resolve(), sources[PROVIDER].resolve(), sources[recent].resolve()])
        self.assertFalse(self.cache.is_hidden("chat", targets[oldest]))
        for provider in (recent, PROVIDER, current):
            self.assertTrue(self.cache.is_hidden("chat", targets[provider]))

    def test_aggregate_byte_budget_does_not_open_sources_beyond_the_cap(self):
        previous = "11111111-2222-3333-4444-555555555555"
        current = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        sources, targets = self.histories([
            (PROVIDER, [user("Oldest")]), (previous, [user("Recent")]), (current, [user("Current")]),
        ])
        budget = sources[current].stat().st_size + sources[previous].stat().st_size
        from codex_history_repair import _records
        with patch("codex_history_repair.MAX_AGGREGATE_SOURCE_BYTES", budget), \
                patch("codex_history_repair._records", wraps=_records) as reads:
            self.prepare_current(current, sources[current])
        self.assertEqual([call.args[0] for call in reads.call_args_list],
            [self.events, sources[current].resolve(), sources[previous].resolve()])
        self.assertTrue(self.cache.is_hidden("chat", targets[current]))
        self.assertTrue(self.cache.is_hidden("chat", targets[previous]))
        self.assertFalse(self.cache.is_hidden("chat", targets[PROVIDER]))

    def test_aggregate_record_budget_commits_only_completely_scanned_sources(self):
        current = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        sources, targets = self.histories([(PROVIDER, [user("Previous")]), (current, [user("Current")])])
        from codex_history_repair import _records
        source_records = []
        def records(path, stamp):
            for record in _records(path, stamp):
                if path != self.events:
                    source_records.append(path)
                yield record
        with patch("codex_history_repair.MAX_AGGREGATE_SOURCE_RECORDS", 3), \
                patch("codex_history_repair._records", side_effect=records):
            self.prepare_current(current, sources[current])
        self.assertEqual(len(source_records), 3)
        self.assertTrue(self.cache.is_hidden("chat", targets[current]))
        self.assertFalse(self.cache.is_hidden("chat", targets[PROVIDER]))

    def test_prior_unknown_or_human_quotation_blocks_only_its_source(self):
        current = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        for kind in ("user.text", "unknown"):
            with self.subTest(kind=kind):
                self.cache.forget("chat")
                sources, targets = self.histories([
                    (PROVIDER, [user("Prior goal"), user("Prior goal", kind)]),
                    (current, [user("Current goal")]),
                ])
                self.prepare_current(current, sources[current])
                self.assertFalse(self.cache.is_hidden("chat", targets[PROVIDER]))
                self.assertTrue(self.cache.is_hidden("chat", targets[current]))

    def test_prior_checkpoint_header_and_path_proof_are_still_required(self):
        current = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        for mutation in ("digest", "header", "symlink", "foreign_chat", "missing_terminal"):
            with self.subTest(mutation=mutation):
                self.cache.forget("chat")
                sources, targets = self.histories([(PROVIDER, [user("Prior")]), (current, [user("Current")])])
                ledger = [json.loads(line) for line in self.events.read_text().splitlines()]
                cursor = ledger[0]["_history_sync_checkpoint"]["cursor"]
                if mutation == "digest":
                    cursor["source_digest"] = "0" * 64
                elif mutation == "header":
                    content = sources[PROVIDER].read_text().replace(PROVIDER, current)
                    sources[PROVIDER].write_text(content)
                    cursor["source_digest"] = hashlib.sha256(sources[PROVIDER].read_bytes()).hexdigest()
                elif mutation == "symlink":
                    alias = self.root / f"alias-{PROVIDER}.jsonl"
                    alias.symlink_to(sources[PROVIDER])
                    ledger[0]["source_path"] = cursor["source_path"] = str(alias)
                elif mutation == "foreign_chat":
                    for record in ledger[:3]:
                        record["session_id"] = "other"
                else:
                    ledger.pop(2)
                self.events.write_text("".join(json.dumps(record) + "\n" for record in ledger))
                self.prepare_current(current, sources[current])
                self.assertFalse(self.cache.is_hidden("chat", targets[PROVIDER]))
                self.assertTrue(self.cache.is_hidden("chat", targets[current]))


if __name__ == "__main__":
    unittest.main()
