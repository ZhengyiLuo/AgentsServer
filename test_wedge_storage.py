"""Regression tests for event-loop blocking writes and unbounded logs.

Covers the coalesced sessions.json writer, the rotating server log, the
uvicorn access-log poll filter, and the repeated-message filter.
"""

import asyncio
import json
import logging
import os
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class CoalescedSessionSaveTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._temp.name) / "state"
        self.sessions_file = self.state_dir / "sessions.json"
        self._patches = [
            patch.object(agent_server, "STATE_DIR", self.state_dir),
            patch.object(agent_server, "SESSIONS_FILE", self.sessions_file),
            patch.object(agent_server, "FILES_ROOT", self.state_dir / "files"),
            patch.object(agent_server, "CODE_DIFFS_ROOT", self.state_dir / "diffs"),
            patch.object(
                agent_server,
                "CROSS_CHAT_AUTHORITY_ROOT",
                self.state_dir / "authority",
            ),
        ]
        for item in self._patches:
            item.start()
        self.addCleanup(self._temp.cleanup)
        for item in reversed(self._patches):
            self.addCleanup(item.stop)

    async def asyncTearDown(self) -> None:
        # Never leave a writer task behind on the test loop.
        for store in getattr(self, "_stores", []):
            await store.flush_pending_save()

    def make_store(self) -> "agent_server.SessionStore":
        store = agent_server.SessionStore()
        self._stores = getattr(self, "_stores", [])
        self._stores.append(store)
        return store

    async def test_many_background_marks_produce_one_write_on_flush(self) -> None:
        store = self.make_store()
        for index in range(200):
            store.sessions[f"sess_{index}"] = {"id": f"sess_{index}", "n": index}
            await store.save(flush=False)
        # Nothing has hit disk yet: marks only schedule the debounced writer.
        self.assertFalse(self.sessions_file.exists())
        self.assertEqual(store.save_write_count, 0)

        await store.save()

        self.assertEqual(store.save_write_count, 1)
        self.assertEqual(json.loads(self.sessions_file.read_text()), store.sessions)

    async def test_background_marks_land_within_debounce_window(self) -> None:
        store = self.make_store()
        store.sessions["a"] = {"id": "a"}
        with (
            patch.object(agent_server, "SESSION_STORE_SAVE_DEBOUNCE_SECONDS", 0.02),
            patch.object(agent_server, "SESSION_STORE_SAVE_MAX_LATENCY_SECONDS", 0.1),
        ):
            await store.save(flush=False)
            store.sessions["b"] = {"id": "b"}
            await store.save(flush=False)
            for _ in range(50):
                if store.save_write_count:
                    break
                await asyncio.sleep(0.01)
        self.assertEqual(store.save_write_count, 1)
        self.assertEqual(
            set(json.loads(self.sessions_file.read_text())),
            {"a", "b"},
        )

    async def test_awaited_save_is_immediate_and_coalesces_concurrent_callers(self) -> None:
        store = self.make_store()
        writes: list[str] = []
        real_write = agent_server.write_sessions_json_text

        def traced_write(path: Path, text: str, *, durable: bool) -> None:
            writes.append(text)
            real_write(path, text, durable=durable)

        with patch.object(agent_server, "write_sessions_json_text", traced_write):
            async def mutate_and_save(index: int) -> None:
                store.sessions[f"s{index}"] = {"id": f"s{index}"}
                await store.save()

            await asyncio.gather(*(mutate_and_save(i) for i in range(25)))

        # At most two writes: one in flight when the burst started, one
        # covering everything marked while it ran.
        self.assertLessEqual(len(writes), 2)
        self.assertEqual(json.loads(writes[-1]), store.sessions)
        self.assertEqual(json.loads(self.sessions_file.read_text()), store.sessions)

    async def test_file_content_is_compact_and_round_trips_through_load(self) -> None:
        store = self.make_store()
        store.sessions = {
            "sess_x": {
                "id": "sess_x",
                "title": "Ünïcode ✓",
                "nested": {"a": [1, 2, {"b": None}], "flag": True},
                "sort_order": 1000,
            }
        }
        await store.save()
        raw = self.sessions_file.read_text(encoding="utf-8")
        self.assertNotIn("\n", raw)
        self.assertEqual(json.loads(raw), store.sessions)

        reloaded = agent_server.SessionStore()
        self._stores.append(reloaded)
        await reloaded.load()
        self.assertEqual(reloaded.sessions["sess_x"]["title"], "Ünïcode ✓")
        self.assertEqual(reloaded.sessions["sess_x"]["nested"], store.sessions["sess_x"]["nested"])

    async def test_durable_save_fsyncs_and_write_errors_propagate(self) -> None:
        store = self.make_store()
        store.sessions = {"a": {"id": "a"}}
        fsyncs: list[int] = []
        real_fsync = agent_server.os.fsync

        def traced_fsync(descriptor: int) -> None:
            fsyncs.append(descriptor)
            real_fsync(descriptor)

        with patch.object(agent_server.os, "fsync", side_effect=traced_fsync):
            await store.save(durable=True)
        self.assertEqual(len(fsyncs), 2)  # file, then directory
        self.assertEqual(json.loads(self.sessions_file.read_text()), store.sessions)

        def failing_write(path: Path, text: str, *, durable: bool) -> None:
            raise OSError("disk full")

        with patch.object(agent_server, "write_sessions_json_text", failing_write):
            with self.assertRaises(OSError):
                await store.save()

    async def test_flush_pending_save_joins_background_write(self) -> None:
        store = self.make_store()
        store.sessions = {"a": {"id": "a"}}
        await store.save(flush=False)
        self.assertFalse(self.sessions_file.exists())
        await store.flush_pending_save()
        self.assertTrue(self.sessions_file.exists())
        self.assertEqual(store.save_write_count, 1)
        self.assertIsNone(store._save_task)

    async def test_cancelled_writer_persists_pending_marks_synchronously(self) -> None:
        store = self.make_store()
        store.sessions = {"a": {"id": "a"}}
        await store.save(flush=False)
        task = store._save_task
        self.assertIsNotNone(task)
        pending = store._pending_save
        self.assertIsNotNone(pending)
        await asyncio.sleep(0)  # let the worker enter its debounce wait
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self.assertEqual(json.loads(self.sessions_file.read_text()), store.sessions)
        self.assertIsNone(store._save_task)
        # The fallback write succeeded, so a caller awaiting it sees no error.
        self.assertTrue(pending.done.is_set())
        self.assertIsNone(pending.error)

    async def test_event_metadata_update_does_not_block_on_disk(self) -> None:
        store = self.make_store()
        store.sessions = {"chat": {"id": "chat", "backend": "claude"}}
        blocked = asyncio.Event()

        def slow_write(path: Path, text: str, *, durable: bool) -> None:
            blocked.set()
            raise OSError("never reached in this test")

        with (
            patch.object(agent_server, "STORE", store),
            patch.object(agent_server, "write_sessions_json_text", slow_write),
        ):
            event = {
                "seq": 7,
                "type": "assistant_text",
                "ts": "2026-09-04T00:00:00Z",
                "text": "hi",
            }
            await asyncio.wait_for(
                agent_server.update_session_event_metadata("chat", event),
                timeout=0.05,
            )
            self.assertEqual(store.sessions["chat"]["latest_event_seq"], 7)
            self.assertFalse(blocked.is_set())
            # Failures of the coalesced write are logged, never raised into
            # the event path.
            await store.flush_pending_save()
            self.assertTrue(blocked.is_set())


class PollingAccessLogFilterTests(unittest.TestCase):
    def record(self, method: str, path: str, status: int, *, args: bool = True) -> logging.LogRecord:
        if args:
            return logging.LogRecord(
                "uvicorn.access",
                logging.INFO,
                __file__,
                1,
                '%s - "%s %s HTTP/%s" %d',
                ("127.0.0.1:1234", method, path, "1.1", status),
                None,
            )
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            f'127.0.0.1:1234 - "{method} {path} HTTP/1.1" {status}',
            None,
            None,
        )

    def test_drops_successful_polls_and_keeps_everything_else(self) -> None:
        log_filter = agent_server.PollingAccessLogFilter()
        dropped = [
            ("GET", "/api/health", 200),
            ("GET", "/api/jobs", 200),
            ("GET", "/api/sessions?summary=true", 200),
            ("GET", "/api/sessions?summary=true&limit=50", 200),
            ("GET", "/api/sessions/sess_abc/codex/runtime", 200),
            ("GET", "/api/sessions/sess_abc/claude/runtime", 200),
            (
                "GET",
                "/api/team-hub-secure/0f7f4c8e-1111-2222-3333-444444444444/v1/teams/team-1/network/mailbox",
                200,
            ),
            (
                "GET",
                "/api/team-hub-secure/conn/v1/teams/team-1/network/mailbox?after=5",
                200,
            ),
        ]
        for method, path, status in dropped:
            with self.subTest(path=path):
                self.assertFalse(log_filter.filter(self.record(method, path, status)))
                self.assertFalse(
                    log_filter.filter(self.record(method, path, status, args=False))
                )
        kept = [
            ("GET", "/api/health", 500),
            ("GET", "/api/health", 401),
            ("GET", "/api/jobs", 503),
            ("POST", "/api/health", 200),
            ("GET", "/api/sessions", 200),
            ("GET", "/api/sessions?summary=false", 200),
            ("GET", "/api/sessions/sess_abc", 200),
            ("GET", "/api/sessions/sess_abc/events", 200),
            ("POST", "/api/sessions/sess_abc/codex/runtime", 200),
            ("GET", "/api/team-hub-secure/conn/v1/teams/team-1/network/messages", 200),
            ("POST", "/api/team-hub-secure/conn/v1/teams/team-1/network/mailbox", 200),
        ]
        for method, path, status in kept:
            with self.subTest(path=path, status=status):
                self.assertTrue(log_filter.filter(self.record(method, path, status)))
                self.assertTrue(
                    log_filter.filter(self.record(method, path, status, args=False))
                )

    def test_unrelated_records_pass_through(self) -> None:
        log_filter = agent_server.PollingAccessLogFilter()
        record = logging.LogRecord(
            "uvicorn.access", logging.INFO, __file__, 1, "startup complete", None, None
        )
        self.assertTrue(log_filter.filter(record))

    def test_redacts_websocket_query_tokens_before_logging(self) -> None:
        log_filter = agent_server.PollingAccessLogFilter()
        secret = "admin-secret-that-must-not-reach-disk"
        paths = [
            f"/api/sessions/chat/events?after=5&token={secret}&visible=true",
            f"/api/sessions/chat/events?after=5&%74o%6Ben={secret}&visible=true",
        ]
        websocket_formats = [
            ('%s - "WebSocket %s" [accepted]', ("127.0.0.1:1234",)),
            ('%s - "WebSocket %s" 403', ("127.0.0.1:1234",)),
            ('%s - "WebSocket %s" %d', ("127.0.0.1:1234", 4401)),
        ]
        for path in paths:
            for message, surrounding_args in websocket_formats:
                with self.subTest(path=path, message=message):
                    args = (
                        surrounding_args[0],
                        path,
                        *surrounding_args[1:],
                    )
                    record = logging.LogRecord(
                        "uvicorn.error",
                        logging.INFO,
                        __file__,
                        1,
                        message,
                        args,
                        None,
                    )
                    self.assertTrue(log_filter.filter(record))
                    rendered = record.getMessage()
                    self.assertNotIn(secret, rendered)
                    self.assertIn("<redacted>", rendered)
                    self.assertIn("visible=true", rendered)

        path = f"/api/sessions/chat/events?after=5&token={secret}&visible=true"
        for uses_args in (True, False):
            with self.subTest(args=uses_args):
                record = self.record("GET", path, 101, args=uses_args)
                self.assertTrue(log_filter.filter(record))
                rendered = record.getMessage()
                self.assertNotIn(secret, rendered)
                self.assertIn("token=<redacted>", rendered)
                self.assertIn("visible=true", rendered)


class RepeatedMessageFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = [1000.0]
        self.logger = logging.getLogger(f"test-repeat-{id(self)}")
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.handler = _ListHandler()
        self.logger.addHandler(self.handler)
        self.filter = agent_server.RepeatedMessageFilter(60.0, clock=lambda: self.clock[0])
        self.logger.addFilter(self.filter)
        self.addCleanup(self.logger.removeHandler, self.handler)
        self.addCleanup(self.logger.removeFilter, self.filter)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.handler.records]

    def test_identical_warnings_are_suppressed_then_summarized(self) -> None:
        for _ in range(5):
            self.logger.warning("codex app-server unavailable: %s", "connection refused")
            self.clock[0] += 1.0
        self.assertEqual(
            self.messages(),
            ["codex app-server unavailable: connection refused"],
        )

        self.logger.error("claude runtime probe failed")
        self.assertEqual(
            self.messages(),
            [
                "codex app-server unavailable: connection refused",
                "codex app-server unavailable: connection refused (suppressed 4 repeats)",
                "claude runtime probe failed",
            ],
        )
        self.assertEqual(self.handler.records[1].levelno, logging.WARNING)
        self.assertEqual(self.handler.records[2].levelno, logging.ERROR)

    def test_same_message_after_window_emits_again_with_summary(self) -> None:
        self.logger.warning("stuck")
        self.logger.warning("stuck")
        self.clock[0] += 61.0
        self.logger.warning("stuck")
        self.assertEqual(
            self.messages(),
            ["stuck", "stuck (suppressed 1 repeats)", "stuck"],
        )

    def test_info_and_distinct_messages_are_never_suppressed(self) -> None:
        for index in range(3):
            self.logger.info("poll %d", index)
            self.logger.info("poll %d", index)
        self.logger.warning("a")
        self.logger.warning("b")
        self.logger.warning("a")
        self.assertEqual(len(self.handler.records), 9)
        self.assertEqual(self.messages()[-3:], ["a", "b", "a"])

    def test_different_levels_with_same_text_are_distinct(self) -> None:
        self.logger.warning("same text")
        self.logger.error("same text")
        self.assertEqual(self.messages(), ["same text", "same text"])


class ConfigureServerLoggingTests(unittest.TestCase):
    def test_installs_rotating_file_and_filters_with_env_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            root = logging.getLogger(f"test-root-{id(self)}")
            root.propagate = False
            access = logging.getLogger(f"test-access-{id(self)}")
            access.propagate = False
            error = logging.getLogger(f"test-error-{id(self)}")
            error.propagate = False
            server = logging.getLogger(f"test-server-{id(self)}")
            server.propagate = False
            with patch.dict(
                os.environ,
                {"AGENTSDOCK_LOG_MAX_BYTES": "65536", "AGENTSDOCK_LOG_BACKUPS": "2"},
            ):
                installed = agent_server.configure_server_logging(
                    state_dir,
                    root_logger=root,
                    access_logger=access,
                    error_logger=error,
                    server_logger=server,
                )
            try:
                self.assertIsNotNone(installed.file_handler)
                assert installed.file_handler is not None
                self.assertEqual(installed.file_handler.maxBytes, 65536)
                self.assertEqual(installed.file_handler.backupCount, 2)
                self.assertEqual(
                    Path(installed.file_handler.baseFilename),
                    state_dir / "logs" / "agents-server.log",
                )
                self.assertIn(installed.access_filter, access.filters)
                self.assertIn(installed.access_filter, error.filters)
                self.assertIn(installed.repeat_filter, server.filters)
                self.assertEqual(len(root.handlers), 2)
                self.assertIsInstance(installed.stream_handler, logging.StreamHandler)

                with patch.object(installed.stream_handler, "emit"):
                    root.warning("rotating file receives this line")
                installed.file_handler.flush()
                text = (state_dir / "logs" / "agents-server.log").read_text()
                self.assertIn("[WARNING]", text)
                self.assertIn("rotating file receives this line", text)
            finally:
                for handler in list(root.handlers):
                    root.removeHandler(handler)
                    handler.close()
                access.removeFilter(installed.access_filter)
                error.removeFilter(installed.access_filter)
                server.removeFilter(installed.repeat_filter)

    def test_rotation_settings_fall_back_on_invalid_values(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENTSDOCK_LOG_MAX_BYTES": "not-a-number", "AGENTSDOCK_LOG_BACKUPS": "-4"},
        ):
            max_bytes, backups = agent_server.server_log_rotation_settings()
        self.assertEqual(max_bytes, agent_server.SERVER_LOG_DEFAULT_MAX_BYTES)
        self.assertEqual(backups, 0)

    def test_main_routes_uvicorn_logging_through_root_handlers(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(app: object, **kwargs: object) -> None:
            captured.update(kwargs)

        with (
            patch.object(agent_server, "configure_server_logging") as configure,
            patch.object(agent_server.uvicorn, "run", fake_run),
            patch.object(agent_server.sys, "argv", ["agent_server.py"]),
        ):
            self.assertEqual(agent_server.main(), 0)
        configure.assert_called_once_with(agent_server.STATE_DIR)
        self.assertIn("log_config", captured)
        self.assertIsNone(captured["log_config"])


def _write_events(path: Path, count: int, *, session_id: str = "chat", pad: int = 0) -> None:
    with path.open("w", encoding="utf-8") as output:
        for seq in range(1, count + 1):
            event_type = "raw_event" if seq % 5 == 0 else "assistant_text"
            output.write(json.dumps({
                "seq": seq,
                "id": f"event-{seq}",
                "session_id": session_id,
                "type": event_type,
                "ts": "2026-09-04T00:00:00Z",
                "text": f"event {seq} " + ("x" * pad),
            }, separators=(",", ":")) + "\n")


def _full_scan(session_id: str, **kwargs: object) -> tuple[list[dict[str, object]], int, bool]:
    with patch.object(agent_server, "event_index_resume_offset", return_value=0):
        return agent_server.read_event_catchup_batch(session_id, **kwargs)  # type: ignore[arg-type]


class EventCatchupIndexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.path = Path(self._temp.name) / "events.jsonl"
        self.index_path = agent_server.events_index_path(self.path)
        self.assertEqual(self.index_path.name, "events.idx")

    def line_offset(self, seq: int) -> int:
        offset = 0
        with self.path.open("rb") as source:
            for raw_line in source:
                if json.loads(raw_line)["seq"] == seq:
                    return offset
                offset += len(raw_line)
        raise AssertionError(f"seq {seq} not found")

    def test_resume_uses_indexed_offset_and_matches_full_scan(self) -> None:
        _write_events(self.path, 3000, pad=120)
        self.assertGreater(self.path.stat().st_size, agent_server.EVENT_INDEX_MIN_FILE_BYTES)
        self.assertFalse(self.index_path.exists())
        with (
            patch.object(agent_server, "events_path", return_value=self.path),
            patch.object(agent_server, "fork_internal_run_ids", return_value=set()),
        ):
            for after in (0, 1, 511, 512, 513, 1500, 2047, 2048, 2999, 3000):
                with self.subTest(after=after):
                    expected = _full_scan(
                        "chat", after=after, through=3000, limit=5000, visible=True
                    )
                    actual = agent_server.read_event_catchup_batch(
                        "chat", after=after, through=3000, limit=5000, visible=True
                    )
                    self.assertEqual(actual, expected)
            # The first miss rebuilt the index lazily from the transcript.
            self.assertTrue(self.index_path.exists())
            entries = agent_server.read_event_index(self.path)
            self.assertEqual(
                entries,
                [(seq, self.line_offset(seq)) for seq in (512, 1024, 1536, 2048, 2560)],
            )
            self.assertEqual(
                agent_server.event_index_resume_offset(self.path, 1500),
                self.line_offset(1024),
            )
            self.assertEqual(agent_server.event_index_resume_offset(self.path, 511), 0)
            self.assertEqual(agent_server.event_index_resume_offset(self.path, 0), 0)

            # Paging continues from the returned byte offset exactly as before.
            first, offset, exhausted = agent_server.read_event_catchup_batch(
                "chat", after=1500, through=3000, limit=400, visible=True
            )
            self.assertFalse(exhausted)
            rest, _, exhausted = agent_server.read_event_catchup_batch(
                "chat", after=first[-1]["seq"], through=3000, offset=offset, limit=5000, visible=True
            )
            self.assertTrue(exhausted)
            self.assertEqual(
                [event["seq"] for event in first + rest],
                [seq for seq in range(1501, 3001) if seq % 5],
            )

    def test_stale_index_falls_back_without_skipping_events(self) -> None:
        _write_events(self.path, 2000, pad=120)
        with (
            patch.object(agent_server, "events_path", return_value=self.path),
            patch.object(agent_server, "fork_internal_run_ids", return_value=set()),
        ):
            agent_server.rebuild_event_index(self.path)
            self.assertIsNotNone(agent_server.read_event_index(self.path))
            # Rewrite the transcript in place with longer lines: same inode,
            # every checkpoint now points into the middle of some other line.
            _write_events(self.path, 2000, pad=200)
            self.assertEqual(
                agent_server.read_event_catchup_batch(
                    "chat", after=1300, through=2000, limit=5000, visible=True
                ),
                _full_scan("chat", after=1300, through=2000, limit=5000, visible=True),
            )
            # ...and the index was rebuilt against the new content.
            self.assertEqual(
                agent_server.event_index_resume_offset(self.path, 1300),
                self.line_offset(1024),
            )

            # Wrong inode (transcript replaced by a copy) is rejected outright.
            replacement = self.path.with_name("replacement.jsonl")
            replacement.write_bytes(self.path.read_bytes())
            os.replace(replacement, self.path)
            self.assertIsNone(agent_server.read_event_index(self.path))
            self.assertEqual(
                agent_server.read_event_catchup_batch(
                    "chat", after=700, through=2000, limit=5000, visible=True
                ),
                _full_scan("chat", after=700, through=2000, limit=5000, visible=True),
            )

            # Corrupt index bytes are ignored, not raised.
            self.index_path.write_text("{not json", encoding="utf-8")
            self.assertIsNone(agent_server.read_event_index(self.path))
            self.assertEqual(
                agent_server.read_event_catchup_batch(
                    "chat", after=700, through=2000, limit=5000, visible=True
                ),
                _full_scan("chat", after=700, through=2000, limit=5000, visible=True),
            )

    def test_small_transcripts_are_not_indexed(self) -> None:
        _write_events(self.path, 600)
        self.assertLess(self.path.stat().st_size, agent_server.EVENT_INDEX_MIN_FILE_BYTES)
        with (
            patch.object(agent_server, "events_path", return_value=self.path),
            patch.object(agent_server, "fork_internal_run_ids", return_value=set()),
        ):
            events, _, _ = agent_server.read_event_catchup_batch(
                "chat", after=550, through=600, limit=500, visible=True
            )
        self.assertEqual(len(events), 40)
        self.assertFalse(self.index_path.exists())

    async def test_append_event_records_stride_checkpoints(self) -> None:
        session_id = "index-chat"
        agent_server.EVENT_SEQ_CACHE.pop(session_id, None)
        agent_server.EVENT_DELIVERY_LOCKS.pop(session_id, None)
        self.addCleanup(agent_server.EVENT_SEQ_CACHE.pop, session_id, None)
        self.addCleanup(agent_server.EVENT_DELIVERY_LOCKS.pop, session_id, None)
        # Pre-seed 1023 events so the very next append lands on a boundary.
        _write_events(self.path, 1023, session_id=session_id)
        expected_offset = self.path.stat().st_size
        with (
            patch.dict(agent_server.STORE.sessions, {session_id: {"id": session_id}}),
            patch.object(agent_server, "ensure_dirs"),
            patch.object(agent_server, "events_path", return_value=self.path),
            patch.object(agent_server, "update_session_event_metadata", new=AsyncMock()),
            patch.object(agent_server.HUB, "broadcast", new=AsyncMock()),
        ):
            event = await agent_server.append_event(session_id, "assistant_text", {"text": "boundary"})
            self.assertEqual(event["seq"], 1024)
            self.assertEqual(agent_server.read_event_index(self.path), [(1024, expected_offset)])
            await agent_server.append_event(session_id, "assistant_text", {"text": "after"})
            self.assertEqual(agent_server.read_event_index(self.path), [(1024, expected_offset)])
            # The last line is byte-identical to the historical text-mode writer.
            last_line = self.path.read_bytes().splitlines()[-1]
            self.assertEqual(json.loads(last_line)["seq"], 1025)
            self.assertEqual(last_line, json.dumps(json.loads(last_line), separators=(",", ":")).encode())

            with patch.object(agent_server, "fork_internal_run_ids", return_value=set()):
                self.assertEqual(
                    agent_server.read_event_catchup_batch(
                        session_id, after=1024, through=1025, limit=10, visible=True
                    )[0],
                    _full_scan(session_id, after=1024, through=1025, limit=10, visible=True)[0],
                )
                self.assertEqual(
                    agent_server.event_index_resume_offset(self.path, 1024),
                    expected_offset,
                )


if __name__ == "__main__":
    unittest.main()
