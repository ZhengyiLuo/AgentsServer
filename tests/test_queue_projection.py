"""Crash/restart checks for disposable per-chat queue checkpoints."""
from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import agent_server as server
import queue_projection as projection


class QueueProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="queue-projection-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "events.jsonl"
        self.session = {"id": "chat", "backend": "codex", "model": "model"}
        self.seq = 0
        store = patch.object(server.STORE, "sessions", {"chat": self.session})
        store.start()
        self.addCleanup(store.stop)
        self.addCleanup(projection._MEMORY.clear)

    def event(self, kind, qid=None, **fields):
        self.seq += 1
        result = {"seq": self.seq, "ts": "2026-09-28T01:00:00Z", "type": kind, **fields}
        if qid:
            result["queued_id"] = qid
        return result

    def queued(self, qid, **fields):
        return self.event("turn_queued", qid, prompt="Message " + qid, **fields)

    def append(self, *events, hooked=False):
        offset = self.path.stat().st_size if self.path.exists() else 0
        with self.path.open("ab") as output:
            for event in events:
                output.write(json.dumps(event).encode() + b"\n")
            output.flush()
            os.fsync(output.fileno())
        if hooked:
            server.record_queue_projection_append(self.path, "chat", offset, list(events))
        return offset

    def read(self):
        return projection.read_projection(self.path,
            lambda state, event: server.apply_queue_projection_event(state, event, self.session),
            context=server.queue_projection_context(self.session))

    def restart(self):
        projection._MEMORY.clear()

    def test_warm_restart_no_transcript_scan_and_private_independent_snapshot(self):
        self.append(self.queued("a"), self.queued("b"), hooked=True)
        self.restart()
        with patch.object(projection, "_scan_lines", side_effect=AssertionError("full scan")):
            state = self.read()
        self.assertEqual(state["order"], ["a", "b"])
        self.assertEqual(projection.projection_path(self.path).stat().st_mode & 0o777, 0o600)
        state["pending"]["a"]["prompt"] = "changed copy"
        self.assertEqual(self.read()["pending"]["a"]["prompt"], "Message a")

    def test_crash_gap_replays_only_suffix(self):
        self.append(self.event("assistant_text", text="x" * 100000), self.queued("a"), hooked=True)
        boundary = self.path.stat().st_size
        self.append(self.event("turn_started", "a"), self.queued("b"))
        self.restart()
        offsets = []
        original = projection._scan_lines
        def observed(source, limit):
            offsets.append(source.tell())
            yield from original(source, limit)
        with patch.object(projection, "_scan_lines", observed):
            state = self.read()
        self.assertEqual(offsets, [boundary])
        self.assertEqual(state["order"], ["b"])

    def test_every_transition_matches_cold_authoritative_replay(self):
        transitions = [self.queued("a"), self.queued("b"), self.queued("c"),
            self.event("turn_queue_updated", "a", request_prompt="edited", file_ids=["file"]),
            self.event("turn_queue_reordered", positions=[{"queued_id": "c", "position": 1}, {"queued_id": "a", "position": 2}, {"queued_id": "b", "position": 3}]),
            self.event("turn_queue_paused", queued_ids=["a", "b"]),
            self.event("turn_queue_run_now", "b", superseded_queued_ids=["c"], interrupted_run_id="r", steering_lineage=[]),
            self.event("turn_started", "b"), self.event("turn_unqueued", "a"), self.queued("d"),
            self.event("turn_unqueued", "d"),
            self.event("turn_queue_delivery_fenced", "d", prompt="Message d", position=1),
            self.event("turn_queue_run_now", "d", native_goal_steer=True),
            self.event("turn_steered", "d", run_id="r", backend="codex", purpose="codex_goal_resume", native_steer=True, native_goal_steer=True, provider_user_authored=True)]
        for event in transitions:
            with self.subTest(event=event["type"]):
                self.append(event, hooked=True)
                warm = self.read()
                self.restart()
                projection.projection_path(self.path).unlink()
                cold = self.read()
                self.assertEqual(warm["pending"], cold["pending"])
                self.assertEqual(warm["order"], cold["order"])
        self.assertEqual(cold["pending"], {})
        self.assertNotIn("Message d", projection.projection_path(self.path).read_text())

    def test_stop_unknown_prefix_survives_restart_and_skips_internal_delivery(self):
        self.append(self.queued("user"), self.queued("mail", purpose="cross_chat_handoff_delivery"))
        self.append(self.event("turn_queue_paused", all_pending=True, queued_ids=[]))
        state = self.read()
        self.assertTrue(state["pending"]["user"]["_paused_after_stop"])
        self.assertFalse(state["pending"]["mail"]["_paused_after_stop"])
        self.restart()
        self.assertTrue(self.read()["pending"]["user"]["_paused_after_stop"])
        self.append(self.event("turn_queue_run_now", "user"), hooked=True)
        self.restart()
        self.assertFalse(self.read()["pending"]["user"]["_paused_after_stop"])

    def test_invalid_checkpoints_rebuild(self):
        for mode in ["corrupt", "checksum", "missing", "replaced", "truncated", "rewritten"]:
            with self.subTest(mode=mode):
                self.path.write_text("")
                self.restart()
                self.append(self.queued("a"), hooked=True)
                cache = projection.projection_path(self.path)
                if mode == "corrupt":
                    cache.write_text("{")
                elif mode == "checksum":
                    envelope = json.loads(cache.read_bytes())
                    envelope["state"]["order"] = []
                    cache.write_text(json.dumps(envelope))
                elif mode == "missing":
                    cache.unlink()
                elif mode == "replaced":
                    replacement = self.path.with_suffix(".replacement")
                    replacement.write_bytes(self.path.read_bytes().replace(b'"a"', b'"b"'))
                    replacement.replace(self.path)
                elif mode == "truncated":
                    self.path.write_text("")
                elif mode == "rewritten":
                    self.path.write_bytes(self.path.read_bytes().replace(b'"a"', b'"b"'))
                self.restart()
                with patch.object(projection, "_scan_lines", wraps=projection._scan_lines) as scan:
                    state = self.read()
                self.assertEqual(state["order"], [] if mode == "truncated" else ["b"] if mode in {"replaced", "rewritten"} else ["a"])
                if mode != "truncated":
                    self.assertEqual(scan.call_count, 1)

    def test_cache_failure_does_not_fail_authoritative_commit(self):
        with patch.object(projection, "_save", side_effect=OSError("cache unavailable")):
            result = server.append_durable_event_batch_sync(self.path, "chat", 1, [("turn_queued", {"queued_id": "a", "prompt": "Durable user prompt"})])
        self.assertEqual(result[0]["queued_id"], "a")
        self.restart()
        self.assertEqual(self.read()["pending"]["a"]["prompt"], "Durable user prompt")

    def test_partial_tail_does_not_advance_checkpoint(self):
        self.append(self.queued("a"), hooked=True)
        saved = projection.projection_path(self.path).read_bytes()
        with self.path.open("ab") as output:
            output.write(b'{"type":"turn_unqueued","queued_id":"a"')
        self.restart()
        self.assertEqual(self.read()["order"], ["a"])
        self.assertEqual(projection.projection_path(self.path).read_bytes(), saved)
        server.repair_event_log_tail(self.path)
        self.append(self.event("turn_unqueued", "a"))
        self.restart()
        self.assertEqual(self.read()["order"], [])

    def test_mailbox_proof_without_transcript_bodies(self):
        self.append(self.queued("a", cross_chat_envelope_id="envelope"),
                    self.event("turn_started", "a", cross_chat_envelope_id="envelope"),
                    self.event("turn_finished", cross_chat_envelope_id="envelope", text="private transcript"), hooked=True)
        self.restart()
        with patch.object(projection, "_scan_lines", side_effect=AssertionError("full scan")):
            state = self.read()
        self.assertTrue(state["complete"])
        self.assertTrue(state["mailbox"]["envelope"]["started_or_terminal"])
        self.assertNotIn("private transcript", projection.projection_path(self.path).read_text())

    def test_bootstrap_does_not_block_append_or_publish_incomplete_evidence(self):
        self.append(self.queued("a"))
        entered, release = threading.Event(), threading.Event()
        original = projection._scan_lines
        def paused(source, limit):
            entered.set()
            self.assertTrue(release.wait(5))
            yield from original(source, limit)
        result = []
        with patch.object(projection, "_scan_lines", paused):
            thread = threading.Thread(target=lambda: result.append(self.read()))
            thread.start()
            self.assertTrue(entered.wait(5))
            self.append(self.event("turn_queue_paused", all_pending=True), hooked=True)
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(result[0]["complete"])
        self.restart()
        self.assertTrue(self.read()["pending"]["a"]["_paused_after_stop"])

    def test_delayed_cancelled_hook_cannot_claim_a_later_append(self):
        first = self.queued("a")
        self.append(first)
        boundary = self.path.stat().st_size
        # The first writer's worker starts only after cancellation released
        # the event lock and a second writer has appended another queue row.
        self.append(self.queued("b"))
        server.record_queue_projection_append(self.path, "chat", 0, [first], boundary)
        checkpoint = json.loads(projection.projection_path(self.path).read_bytes())["state"]
        self.assertEqual(checkpoint["offset"], boundary)
        self.restart()
        self.assertEqual(self.read()["order"], ["a", "b"])

    def test_skipped_hook_gap_advances_bounded_prefix_until_current(self):
        self.append(self.queued("a"), hooked=True)
        # Simulate appends whose hooks lost the nonblocking scanner lock.
        for _ in range(8):
            self.append(self.event("assistant_text", text="x" * 200))
        self.append(self.event("turn_unqueued", "a"))
        previous = json.loads(projection.projection_path(self.path).read_bytes())["state"]["offset"]
        with patch.object(projection, "CHECKPOINT_BYTES", 512):
            for _ in range(20):
                self.append(self.event("assistant_text", text="tail"), hooked=True)
                checkpoint = json.loads(projection.projection_path(self.path).read_bytes())["state"]
                self.assertGreater(checkpoint["offset"], previous)
                previous = checkpoint["offset"]
                if checkpoint["offset"] == self.path.stat().st_size:
                    break
            else:
                self.fail("bounded catch-up did not reach the live append")
        self.restart()
        with patch.object(projection, "_scan_lines", side_effect=AssertionError("full scan")):
            self.assertEqual(self.read()["order"], [])

    def test_empty_queue_context_changes_do_not_bootstrap_history(self):
        self.append(self.queued("a"), self.event("turn_started", "a"), hooked=True)
        self.restart()
        self.session["model"] = "new model"
        with patch.object(projection, "_scan_lines", side_effect=AssertionError("full scan")):
            self.assertEqual(self.read()["order"], [])

    def test_atomic_cache_replace_failure_retains_previous_committed_checkpoint(self):
        self.append(self.queued("a"), hooked=True)
        original = projection.projection_path(self.path).read_bytes()
        with patch.object(projection.os, "replace", side_effect=OSError("interrupted replace")):
            self.append(self.event("turn_unqueued", "a"), hooked=True)
        self.assertEqual(projection.projection_path(self.path).read_bytes(), original)
        self.restart()
        self.assertEqual(self.read()["order"], [])
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_legacy_append_never_scans_and_streaming_checkpoints_amortized(self):
        self.append(self.event("assistant_text", text="old history"))
        with patch.object(projection, "_scan_lines", side_effect=AssertionError("legacy append scanned")):
            self.append(self.queued("a"), hooked=True)
        self.assertFalse(projection.projection_path(self.path).exists())
        self.read()
        with patch.object(projection, "_save", wraps=projection._save) as save, patch.object(projection, "CHECKPOINT_BYTES", 1024):
            for _ in range(10):
                self.append(self.event("assistant_text", text="x" * 100), hooked=True)
        self.assertLess(save.call_count, 4)
        self.assertGreaterEqual(save.call_count, 1)


if __name__ == "__main__":
    unittest.main()
