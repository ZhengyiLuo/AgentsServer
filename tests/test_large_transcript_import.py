"""Real bounded readers over disposable native transcripts; no provider calls."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import agent_server as server
import claude_history_repair as claude
import codex_history_repair as codex


MIB = 1024 * 1024
PROVIDER = "11111111-2222-3333-4444-555555555555"


def encoded(record):
    return (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def message(backend, text):
    if backend == "claude":
        return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}
    return {"type": "event_msg", "payload": {"type": "user_message", "message": text}}


def large_record(backend):
    if backend == "claude":
        return {"type": "user", "message": {"content": [
            {"type": "text", "text": "Image caption 中文"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * (5 * MIB)}},
        ]}}
    return {"type": "response_item", "payload": {"type": "function_call_output", "output": "x" * (5 * MIB)}}


class LargeTranscriptImportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="large-transcript-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / f"{PROVIDER}.jsonl"

    def stamp(self):
        value = self.path.stat()
        return {name: int(getattr(value, name)) for name in ("st_dev", "st_ino", "st_size", "st_mtime_ns")}

    def test_readers_agree_on_retained_memory_and_proof_text_budgets(self):
        self.assertEqual(server.MAX_LOCAL_TRANSCRIPT_LINE_BYTES, 32 * MIB)
        self.assertEqual(claude.MAX_LINE_BYTES, server.MAX_LOCAL_TRANSCRIPT_LINE_BYTES)
        self.assertEqual(codex.NATIVE_PROOF_LINE_BYTES, server.MAX_LOCAL_TRANSCRIPT_LINE_BYTES)
        self.assertEqual(claude.MAX_TEXT_CHARS, 4 * MIB)
        self.assertEqual(codex.NATIVE_PROOF_TEXT_CHARS, 4 * MIB)

    def test_image_and_tool_records_above_old_limit_preserve_surrounding_messages(self):
        for backend in ("claude", "codex"):
            with self.subTest(backend=backend):
                raw = b"".join(encoded(record) for record in (
                    message(backend, "Before"), large_record(backend), message(backend, "After"),
                ))
                self.path.write_bytes(raw)
                parser = server.parse_claude_history if backend == "claude" else server.parse_codex_history
                items = parser(self.path, None)
                self.assertEqual([item["text"] for item in items],
                    ["Before", "Image caption 中文", "After"] if backend == "claude" else ["Before", "After"])
                self.assertEqual(self.path.read_bytes(), raw)

    def test_large_record_survives_serialized_cursor_and_followup_without_replay(self):
        for backend in ("claude", "codex"):
            with self.subTest(backend=backend):
                sess = {"id": f"test-{backend}", "backend": backend,
                    "claude_session_id" if backend == "claude" else "codex_thread_id": PROVIDER}
                self.path.write_bytes(encoded(message(backend, "Before")))
                with patch.object(server, "provider_history_path", return_value=self.path):
                    _, first, cursor, _ = server.load_provider_history_with_cursor(sess, None, None)
                    with self.path.open("ab") as stream:
                        stream.write(encoded(large_record(backend)))
                        stream.write(encoded(message(backend, "After")))
                    _, delta, current, continued = server.load_provider_history_with_cursor(sess, None, json.loads(json.dumps(cursor)))
                    _, repeated, final, _ = server.load_provider_history_with_cursor(sess, None, json.loads(json.dumps(current)))
                self.assertEqual([item["text"] for item in first], ["Before"])
                self.assertEqual([item["text"] for item in delta],
                    ["Image caption 中文", "After"] if backend == "claude" else ["After"])
                self.assertTrue(continued)
                self.assertEqual(repeated, [])
                self.assertEqual(final["source_offset"], self.path.stat().st_size)
                self.assertEqual(final["source_digest"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_partial_large_record_is_consumed_only_when_completed(self):
        raw = encoded(large_record("codex"))
        self.path.write_bytes(raw[:-1])
        incomplete, _ = server.provider_history_source_snapshot(self.path, None)
        self.assertEqual(incomplete["source_offset"], 0)
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        complete, _ = server.provider_history_source_snapshot(self.path, None)
        self.assertEqual(complete["source_offset"], len(raw))
        self.assertEqual(complete["source_digest"], hashlib.sha256(raw).hexdigest())

    def test_64_mib_media_record_is_accepted_by_every_native_reader(self):
        prefix, suffix = b'{"payload":{"output":"', b'"}}\n'
        with self.path.open("wb") as stream:
            stream.write(prefix)
            for _ in range(64):
                stream.write(b"x" * MIB)
            stream.write(suffix)
        size = self.path.stat().st_size
        self.assertEqual(sum(1 for _ in server.bounded_jsonl_events(self.path)), 1)
        self.assertEqual(sum(1 for _ in server.bounded_jsonl_records_range(self.path, 0, size, expected_stat=self.stamp())), 1)
        self.assertEqual(sum(1 for _ in claude._records(self.path, claude._regular_stamp(self.path))), 1)
        self.assertEqual(sum(1 for _ in codex._native_records(self.path, codex._native_stamp(self.path), codex._NativeReadBudget(), provider_source=True)), 1)
        snapshot, _ = server.provider_history_source_snapshot(self.path, None)
        self.assertEqual(snapshot["source_offset"], size)
        with self.path.open("rb") as stream:
            self.assertEqual(snapshot["source_digest"], hashlib.file_digest(stream, "sha256").hexdigest())

    def test_retained_text_budget_fails_instead_of_silently_losing_messages(self):
        self.path.write_bytes(encoded(message("claude", "x" * 4096)))
        with patch.object(server, "MAX_LOCAL_TRANSCRIPT_LINE_BYTES", 1024):
            with self.assertRaisesRegex(ValueError, "retained text/metadata"):
                list(server.bounded_jsonl_events(self.path))

    def test_file_above_old_total_cap_and_large_serialized_cursor_sync(self):
        self.path.write_bytes(encoded(message("codex", "Before")))
        sess = {"id": "large-cursor", "backend": "codex", "codex_thread_id": PROVIDER}
        with patch.object(server, "provider_history_path", return_value=self.path):
            _, first, cursor, _ = server.load_provider_history_with_cursor(sess, None, None)
            with self.path.open("ab") as stream:
                stream.write(b'{"payload":{"output":"')
                for _ in range(260):
                    stream.write(b"x" * MIB)
                stream.write(b'"}}\n')
                stream.write(encoded(message("codex", "After")))
            _, delta, current, continued = server.load_provider_history_with_cursor(sess, None, cursor)
            self.assertEqual([item["text"] for item in first], ["Before"])
            self.assertEqual([item["text"] for item in delta], ["After"])
            self.assertTrue(continued)
            restored = server.normalized_history_sync_cursor({**sess, "_history_sync_cursor": json.loads(json.dumps(current))})
            self.assertIsNotNone(restored)
            _, repeated, final, _ = server.load_provider_history_with_cursor(sess, None, restored)
            self.assertEqual(repeated, [])
            self.assertEqual(final["source_offset"], self.path.stat().st_size)
            with self.path.open("rb") as stream:
                self.assertEqual(final["source_digest"], hashlib.file_digest(stream, "sha256").hexdigest())

    def test_native_proof_keeps_distinction_between_bad_json_and_nonobject_json(self):
        self.path.write_bytes(b'not-json\n')
        self.assertEqual(list(codex._native_records(self.path, codex._native_stamp(self.path),
            codex._NativeReadBudget(), provider_source=True))[0][0], {})
        for raw in (b'[]\n', b'null\n', b'42\n'):
            self.path.write_bytes(raw)
            with self.assertRaises(codex._Unproven):
                list(codex._native_records(self.path, codex._native_stamp(self.path),
                    codex._NativeReadBudget(), provider_source=True))


class ImportLimitErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_parser_error_reaches_import_response_without_creating_chat(self):
        for backend in ('claude', 'codex'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f'{PROVIDER}.jsonl'
                before = encoded(message(backend, 'Before'))
                raw = before + encoded(message(backend, 'PRIVATE-CONTENT' + 'x' * 4096))
                path.write_bytes(raw)
                candidate = {'provider_session_id': PROVIDER, 'backend': backend,
                             'cwd': directory, 'label': 'Disposable oversized record'}
                parser = server.parse_claude_history if backend == 'claude' else server.parse_codex_history
                def history(*_args):
                    return path, parser(path, None)
                with patch.object(server.STORE, 'sessions', {}), \
                     patch.object(server, 'other_local_instance_provider_keys', return_value=set()), \
                     patch.object(server, 'local_session_candidates', return_value=[candidate]), \
                     patch.object(server, 'provider_history', side_effect=history), \
                     patch.object(server, 'MAX_LOCAL_TRANSCRIPT_LINE_BYTES', 1024), \
                     patch.object(server.STORE, 'create', AsyncMock()) as create:
                    response = await server.bulk_import_sessions(server.BulkImportSessionsRequest(items=[
                        server.BulkImportSessionItem(provider_session_id=PROVIDER, backend=backend, cwd=directory)]))
                result = response['results'][0]
                self.assertFalse(result['ok'])
                self.assertEqual(result['code'], 'import_failed')
                self.assertIsNone(result['session_id'])
                self.assertEqual(result['imported'], 0)
                for part in ('retained text/metadata', 'observed at least', 'limit 1,024 bytes',
                             f'Record 2 in this scan (source byte offset {len(before)})'):
                    self.assertIn(part, result['error'])
                self.assertNotIn('PRIVATE-CONTENT', result['error'])
                self.assertEqual(path.read_bytes(), raw)
                create.assert_not_awaited()
