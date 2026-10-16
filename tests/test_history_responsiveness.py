"""Regression coverage for chat admission during history maintenance.

Run with AGENTSDOCK_STATE_DIR / provider roots set to disposable directories.
No provider executable or production state is used.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import AsyncMock, patch

import agent_server as server
from codex_history_repair import CodexNativeHistoryRepairCache, CodexNativeHistoryProofUnavailable


class HistoryResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sid = "chat_history_responsiveness"
        self.pid = str(uuid.uuid4())
        self.source = self.root / f"rollout-test-{self.pid}.jsonl"
        self.source.write_text(json.dumps({"type": "session_meta", "payload": {"id": self.pid}}) + "\n")
        self.events = self.root / "events.jsonl"
        self.events.write_text("")
        self.sess = {"id": self.sid, "backend": "codex", "codex_thread_id": self.pid}
        self.enterContext(patch.object(server.STORE, "sessions", {self.sid: self.sess}))
        self.enterContext(patch.object(server.STORE, "save", AsyncMock()))
        self.enterContext(patch.multiple(server,
            events_path=lambda _: self.events, CODEX_SESSIONS_ROOT=self.root,
            CODEX_ROLLOUT_PATH_CACHE={}, CODEX_ROLLOUT_PATH_MISSES={},
            HISTORY_SYNC_SCHEDULED=set(), HISTORY_MAINTENANCE_STATE={}, HISTORY_MAINTENANCE_TASKS={},
            HISTORY_MAINTENANCE_SLOTS=asyncio.Semaphore(2),
            SESSION_LIFECYCLE_LOCKS={}, SESSION_TURN_TASKS={}, ACTIVE={}, BUSY_SESSIONS=set(),
            SERVER_MAINTENANCE_SESSIONS=set(), DELETING_SESSIONS=set(), DELETED_SESSION_TOMBSTONES=set(),
            QUEUE_RECOVERY_TASK=None, QUEUE_RECOVERY_READY=set(), TURN_RECEIPT_TASKS={},
        ))

    async def test_slow_history_preparation_does_not_hold_send_lock(self):
        entered = threading.Event()
        release = threading.Event()
        def load(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test preparation timed out")
            return self.source, [], None, False
        with patch.object(server, "load_provider_history_with_cursor", load), \
                patch.object(server, "unsynced_history_items", return_value=[]), \
                patch.object(server, "reconcile_idle_queue_session", AsyncMock()), \
                patch.object(server, "_start_turn_locked", AsyncMock(return_value={"run_id": "accepted"})):
            task = asyncio.create_task(server.sync_provider_history(dict(self.sess), background=True))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                result = await asyncio.wait_for(server.start_turn(self.sid, server.TurnRequest(prompt="hello")), .25)
                self.assertEqual(result["run_id"], "accepted")
            finally:
                release.set()
                await task

    async def test_commit_rejects_timeline_cursor_provider_and_busy_changes(self):
        stamp = server.history_file_stamp(self.events)
        source = server.provider_history_source_stamp(self.sess)
        changes = [lambda: self.events.write_text('{}\n'),
                   lambda: self.sess.update(codex_thread_id=str(uuid.uuid4())),
                   lambda: server.BUSY_SESSIONS.add(self.sid)]
        for change in changes:
            self.events.write_text("")
            self.sess["codex_thread_id"] = self.pid
            server.BUSY_SESSIONS.clear()
            stamp = server.history_file_stamp(self.events)
            before = dict(self.sess)
            change()
            with self.assertRaises(server.HistoryPreparationStale):
                async with server.history_commit_guard(before, None, stamp, source, True):
                    self.fail("stale plan reached commit")

    async def test_saved_path_lookup_ignores_import_size_limit(self):
        # A small configured limit reproduces the size boundary without IO load.
        _, _, cursor, _ = server.load_provider_history_with_cursor(self.sess, None, None)
        self.sess["_history_sync_cursor"] = cursor
        with patch.object(server, "bounded_jsonl_paths", side_effect=AssertionError("unexpected directory scan")):
            self.assertEqual(server.provider_history_path(self.sess), self.source)

    async def test_legacy_filename_discovery_and_changed_cursor_guard(self):
        renamed = self.source.with_name("renamed-transcript.jsonl")
        self.source.rename(renamed)
        self.assertEqual(server.find_codex_history(self.pid), renamed)
        before = dict(self.sess)
        stamp = server.history_file_stamp(self.events)
        _, _, cursor, _ = server.load_provider_history_with_cursor(self.sess, None, None)
        self.sess["_history_sync_cursor"] = cursor
        with self.assertRaises(server.HistoryPreparationStale):
            async with server.history_commit_guard(before, None, stamp, None, True):
                self.fail("changed cursor reached commit")

    async def test_batches_keep_all_messages_across_byte_and_record_limits(self):
        with self.source.open("a") as stream:
            for index in range(30):
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": f"message {index}"}}) + "\n")
        cursor = None
        items = []
        with patch.object(server, "HISTORY_SYNC_BATCH_BYTES", 500), \
                patch.object(server, "MAX_LOCAL_TRANSCRIPT_SCAN_LINES", 3):
            for _ in range(50):
                _, batch, cursor, _ = server.load_provider_history_with_cursor(self.sess, 2, cursor)
                items.extend(item["text"] for item in batch)
                if cursor["source_caught_up"]:
                    break
        self.assertEqual(items, [f"message {i}" for i in range(30)])
        self.assertEqual(cursor["source_offset"], self.source.stat().st_size)

    async def test_initial_batches_do_not_duplicate_preexisting_imported_history(self):
        self.events.write_text("".join(json.dumps({"seq": i + 1, "session_id": self.sid,
            "type": "turn_started", "run_id": "import_old", "imported": True,
            "backend": "codex", "prompt": f"message {i}"}) + "\n" for i in range(5)))
        with self.source.open("a") as stream:
            for i in range(10):
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": f"message {i}"}}) + "\n")
        imported = []
        async def append(sess, path, items, **kwargs):
            imported.extend(item["text"] for item in items)
            return {"imported": len(items), "timeline_seq": 5}
        with patch.object(server, "HISTORY_SYNC_BATCH_BYTES", 500), \
                patch.object(server, "append_imported_history", append):
            for _ in range(20):
                result = await server.sync_provider_history(dict(self.sess), limit=2)
                if not result.get("pending"):
                    break
        self.assertEqual(imported, [f"message {i}" for i in range(5, 10)])
        self.assertEqual(self.sess["_history_sync_cursor"]["source_offset"], self.source.stat().st_size)

    async def test_large_compaction_record_continues_without_skipping_next_message(self):
        _, _, cursor, _ = server.load_provider_history_with_cursor(self.sess, None, None)
        with self.source.open("a") as stream:
            stream.write(json.dumps({"type": "compacted", "payload": {
                "replacement_history": [{"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "x" * (5 * 1024 * 1024)}]}]}}) + "\n")
            stream.write(json.dumps({"type": "event_msg", "payload": {
                "type": "user_message", "message": "after compaction"}}) + "\n")
        _, items, next_cursor, continued = server.load_provider_history_with_cursor(self.sess, None, cursor)
        self.assertTrue(continued)
        self.assertEqual([item["text"] for item in items], ["after compaction"])
        self.assertEqual(next_cursor["source_offset"], self.source.stat().st_size)
        # Verification of a previously consumed large record must also work.
        _, items, _, continued = server.load_provider_history_with_cursor(self.sess, None, next_cursor)
        self.assertTrue(continued)
        self.assertEqual(items, [])

    @unittest.skipUnless(os.environ.get("AGENTSDOCK_LARGE_HISTORY_TEST"), "opt-in 330 MiB fixture")
    async def test_real_330_mib_rollout_continues_past_old_limit(self):
        digest = hashlib.sha256()
        header = self.source.read_bytes()
        block = (json.dumps({"type": "tool_output", "padding": "x" * (1024 * 1024 - 128)}) + "\n").encode()
        with self.source.open("wb") as stream:
            stream.write(header)
            digest.update(header)
            for _ in range(331):
                stream.write(block)
                digest.update(block)
        stamp = self.source.stat()
        cursor = {"version": 1, "backend": "codex", "provider_session_id": self.pid,
                  "source_path": str(self.source), "source_dev": stamp.st_dev, "source_ino": stamp.st_ino,
                  "source_size": stamp.st_size, "source_offset": stamp.st_size,
                  "source_mtime_ns": stamp.st_mtime_ns, "source_digest": digest.hexdigest(),
                  "last_item_digest": "", "timeline_seq": 0, "checkpoint_seq": 0}
        self.sess["_history_sync_cursor"] = cursor
        self.assertIsNotNone(server.normalized_history_sync_cursor(self.sess))
        with self.source.open("a") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "user_message", "message": "after the old limit"}}) + "\n")
        started = time.monotonic()
        with patch.object(server, "bounded_jsonl_paths", side_effect=AssertionError("directory scan")):
            _, items, next_cursor, continued = server.load_provider_history_with_cursor(self.sess, 10, cursor)
        self.assertTrue(continued)
        self.assertEqual([item["text"] for item in items], ["after the old limit"])
        self.assertEqual(next_cursor["source_offset"], self.source.stat().st_size)
        print(f"330 MiB suffix import: {time.monotonic() - started:.3f}s; directory scans=0")

    async def test_unchanged_failure_backoff_survives_state_reload(self):
        with patch.object(server, "sync_provider_history", AsyncMock(side_effect=ValueError("malformed source"))):
            await server.run_provider_history_sync(self.sid)
        server.HISTORY_MAINTENANCE_STATE.clear()
        with patch.object(server, "run_provider_history_sync", AsyncMock()) as worker:
            for _ in range(5):
                server.schedule_provider_history_sync(self.sess)
            worker.assert_not_called()

    async def test_backlog_progress_does_not_wait_for_legacy_display_repair(self):
        with patch.object(server, "sync_provider_history", AsyncMock(return_value={"pending": True})), \
                patch.object(server, "prepare_provider_history_metadata_repair") as repair, \
                patch.object(server, "SERVER_SHUTTING_DOWN", True):
            await server.run_provider_history_sync(self.sid)
        repair.assert_not_called()
        self.assertEqual(server.HISTORY_MAINTENANCE_STATE[self.sid]["status"], "pending")

    async def test_explicit_retry_clears_backoff_without_bypassing_busy_guard(self):
        server.HISTORY_MAINTENANCE_STATE[self.sid] = {"deterministic": True, "retry_at": time.time() + 100}
        server.BUSY_SESSIONS.add(self.sid)
        with self.assertRaises(server.HTTPException) as error:
            await server.retry_history_maintenance(self.sid)
        self.assertEqual(error.exception.status_code, 409)
        self.assertTrue(server.HISTORY_MAINTENANCE_STATE[self.sid]["deterministic"])
        server.BUSY_SESSIONS.clear()
        with patch.object(server, "schedule_provider_history_sync") as schedule:
            await server.retry_history_maintenance(self.sid)
        schedule.assert_called_once_with(self.sess)
        self.assertEqual(server.HISTORY_MAINTENANCE_STATE[self.sid], {})

    async def test_queue_checkpoint_replays_only_suffix_and_preserves_pause(self):
        queued = {"type": "turn_queued", "queued_id": "queued_test", "prompt": "hello"}
        self.events.write_text(json.dumps(queued) + "\n" + json.dumps({"type": "turn_queue_paused", "queued_ids": ["queued_test"]}) + "\n")
        first = server.scan_queued_turns_from_events([(self.sid, self.sess)])
        self.assertTrue(first[self.sid][0]["_paused_after_stop"])
        offset = self.events.stat().st_size
        with self.events.open("a") as stream:
            stream.write(json.dumps({"type": "turn_started", "queued_id": "queued_test"}) + "\n")
        observed = []
        original = server.queued_event_lines
        def records(path, **kwargs):
            observed.append(kwargs["scan"]["offset"])
            return original(path, **kwargs)
        with patch.object(server, "queued_event_lines", records):
            self.assertEqual(server.scan_queued_turns_from_events([(self.sid, self.sess)]), {})
        self.assertEqual(observed, [offset])
        # A replacement must ignore the old checkpoint even if it is longer.
        replacement = self.root / "replacement"
        replacement.write_text(json.dumps(queued) + "\n")
        replacement.replace(self.events)
        self.assertIn(self.sid, server.scan_queued_turns_from_events([(self.sid, self.sess)]))

    async def test_recovered_chat_can_send_while_other_chat_recovery_waits(self):
        pending = asyncio.create_task(asyncio.sleep(10))
        try:
            with patch.object(server, "QUEUE_RECOVERY_TASK", pending):
                server.QUEUE_RECOVERY_READY.add(self.sid)
                await asyncio.wait_for(server.wait_for_queue_recovery_admission(self.sid), .1)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_native_repair_budget_includes_waiting_for_another_chat(self):
        cache = CodexNativeHistoryRepairCache()
        cache._prepare_lock.acquire()
        try:
            with self.assertRaises(CodexNativeHistoryProofUnavailable):
                await asyncio.wait_for(asyncio.to_thread(cache.prepare, self.sid, self.pid,
                    self.events, self.source, self.root, lambda _: None,
                    deadline=time.monotonic() + .1), .5)
        finally:
            cache._prepare_lock.release()

    async def test_retry_after_lost_response_starts_once(self):
        gate = asyncio.Event()
        async def accept(*args):
            await gate.wait()
            return {"run_id": "run_receipt", "queued": False}
        request = server.TurnRequest(prompt="hello", client_request_id="request_one")
        with patch.object(server, "post_turn_without_receipt", AsyncMock(side_effect=accept)) as start:
            first = asyncio.create_task(server.post_turn(self.sid, request))
            while not server.TURN_RECEIPT_TASKS:
                await asyncio.sleep(0)
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            retry = asyncio.create_task(server.post_turn(self.sid, request))
            gate.set()
            self.assertEqual((await retry)["run_id"], "run_receipt")
            await asyncio.sleep(0)
            self.assertEqual((await server.post_turn(self.sid, request))["run_id"], "run_receipt")
            self.assertEqual(start.await_count, 1)
            with self.assertRaises(server.HTTPException) as error:
                await server.post_turn(self.sid, server.TurnRequest(prompt="different", client_request_id="request_one"))
            self.assertEqual(error.exception.status_code, 409)

    async def test_receipt_recovers_crash_after_durable_acceptance(self):
        request = server.TurnRequest(prompt="hello", client_request_id="request_crash")
        fingerprint = hashlib.sha256(json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        value = {"request_id": request.client_request_id, "fingerprint": fingerprint,
                 "status": "pending", "journal": server.history_file_stamp(self.events)}
        server.write_turn_receipt(server.turn_receipt_path(self.sid, request.client_request_id), value)
        self.events.write_text(json.dumps({"type": "turn_started", "client_request_id": request.client_request_id, "run_id": "run_before_crash"}) + "\n")
        with patch.object(server, "post_turn_without_receipt", AsyncMock()) as start:
            result = await server.post_turn(self.sid, request)
            self.assertEqual(result["run_id"], "run_before_crash")
            start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
