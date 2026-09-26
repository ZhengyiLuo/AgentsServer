"""Pure store/view tests: no server imports, transcript discovery, or home access."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
import base64
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from public_chat_shares import (
    TARGET_SNAPSHOT_PAGE_BYTES,
    PublicChatShareStore,
    PublicChatShareUnavailable,
    PublicChatShareValidationError,
    public_chat_share_headers,
    render_public_chat_html,
)


class _ParsedHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.attributes = []
        self.text = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)

    def handle_data(self, data):
        self.text.append(data)


class PublicChatShareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="public-chat-shares-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "shares"
        self.clock = 1800000000.25
        self.store = PublicChatShareStore(self.root, now=lambda: self.clock)
        self.messages = [
            {"role": "user", "text": "Question\n\nwith a paragraph", "timestamp": 1799999900},
            {"role": "assistant", "text": "Answer\n    code()\n\tmore code"},
        ]

    def create(self, **kwargs):
        return self.store.create_share("private-session-123", self.messages, **kwargs)

    def test_video_descriptors_are_frozen_scoped_and_not_exposed_in_rendered_urls(self):
        video = {"id": "video_ZmlsZQ." + "a" * 64, "filename": 'demo "clip".mp4',
                 "content_type": "video/mp4", "size": 10}
        original = dict(video)
        self.messages[1]["videos"] = [video]
        share = self.create()
        video["filename"] = "changed.mp4"
        self.assertEqual(self.store.get_snapshot_video(share["token"], share_id=share["share_id"],
            page=0, message_index=1, video_index=0), {"session_id": share["session_id"], "video": original})
        page = self.store.get_snapshot_page(share["token"])
        self.assertNotIn("session_id", page)
        path = "/shared-chat/" + share["share_id"]
        rendered = render_public_chat_html(page["snapshot"], navigation_base=path).decode()
        self.assertIn(f'src="{path}/media/0/1/0"', rendered)
        self.assertIn('controls preload="metadata" playsinline', rendered)
        self.assertIn("demo &quot;clip&quot;.mp4", rendered)
        self.assertNotIn(original["id"], rendered)
        self.assertNotIn(share["token"], rendered)
        self.assertNotIn(share["session_id"], rendered)
        self.assertIn("media-src 'self'", public_chat_share_headers()["Content-Security-Policy"])
        self.assertNotIn("<script", rendered)
        self.assertIn('src="/share/' + share["token"] + '/media/0/1/0"', render_public_chat_html(
            page["snapshot"], navigation_base="/share/" + share["token"]).decode())

    def test_video_access_checks_exact_frozen_page_and_current_revocation_or_expiry(self):
        video = {"id": "video_ZmlsZQ." + "a" * 64, "filename": "demo.webm",
                 "content_type": "video/webm", "size": 10}
        def emit(sink):
            for index in range(101):
                sink({"role": "assistant", "text": str(index), **({"videos": [video]} if index == 100 else {})})
        share = self.store.create_streamed_share("chat-video", emit, expires_at=self.clock + 10)
        self.assertEqual(self.store.get_snapshot_video(share["token"], page=1, message_index=0,
            video_index=0)["video"], video)
        for options in ({"page": 0, "message_index": 0, "video_index": 0},
                        {"page": 1, "message_index": 0, "video_index": 1},
                        {"page": 1, "message_index": -1, "video_index": 0},
                        {"page": 1, "message_index": True, "video_index": 0},
                        {"page": 2, "message_index": 0, "video_index": 0}):
            with self.subTest(options=options), self.assertRaises(PublicChatShareUnavailable):
                self.store.get_snapshot_video(share["token"], **options)
        self.store.authorize_access(share["token"], share_id=share["share_id"])
        with self.assertRaises(PublicChatShareUnavailable):
            self.store.authorize_access(share["token"], share_id="share_" + "0" * 32)
        self.clock += 10
        with self.assertRaises(PublicChatShareUnavailable):
            self.store.authorize_access(share["token"])
        with self.assertRaises(PublicChatShareUnavailable):
            self.store.get_snapshot_video(share["token"], page=1, message_index=0, video_index=0)
        self.clock -= 10
        self.store.revoke_share(share["share_id"], session_id="chat-video")
        with self.assertRaises(PublicChatShareUnavailable):
            self.store.authorize_access(share["token"])

    def test_invalid_video_descriptors_never_persist_or_expand_legacy_snapshots(self):
        legacy = self.create()
        video = {"id": "video_ZmlsZQ." + "a" * 64, "filename": "demo.mp4",
                 "content_type": "video/mp4", "size": 10}
        for change in ({"id": "/api/files/private"}, {"filename": "../demo.mp4"},
                       {"content_type": "text/html"}, {"size": True}, {"size": 0},
                       {"path": "/private/demo.mp4"}):
            with self.subTest(change=change), self.assertRaises(PublicChatShareValidationError):
                self.store.create_share("chat-one", [{"role": "assistant", "text": "", "videos": [{**video, **change}]}])
        self.messages[0]["videos"] = [video]
        self.assertNotIn("videos", self.store.get_snapshot(legacy["token"])["messages"][0])
        with self.assertRaises(PublicChatShareUnavailable):
            self.store.get_snapshot_video(legacy["token"], page=0, message_index=0, video_index=0)
        self.assertEqual(self.store.list_shares("chat-one"), [])

    def test_snapshot_is_durable_private_and_detached_from_inputs(self):
        result = self.create(title="My conversation")
        self.assertEqual(len(base64.urlsafe_b64decode(result["token"] + "=")), 32)
        self.messages[0]["text"] = "Changed source after sharing"
        reopened = PublicChatShareStore(self.root, now=lambda: self.clock)
        snapshot = reopened.get_snapshot(result["token"])
        self.assertEqual(set(snapshot), {"title", "created_at", "messages"})
        self.assertEqual(snapshot["title"], "My conversation")
        self.assertEqual(snapshot["messages"][0]["text"], "Question\n\nwith a paragraph")
        self.assertNotIn("private-session-123", json.dumps(snapshot))
        self.assertNotIn(result["share_id"], json.dumps(snapshot))
        snapshot["messages"][0]["text"] = "Changed view"
        self.assertNotEqual(reopened.get_snapshot(result["token"])["messages"][0]["text"], "Changed view")
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.database_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(result["token"].encode(), self.store.database_path.read_bytes())
        with sqlite3.connect(self.store.database_path) as connection:
            row = connection.execute("SELECT token_hash, snapshot_json FROM public_chat_shares").fetchone()
        self.assertEqual(row[0], hashlib.sha256(result["token"].encode()).digest())
        self.assertNotIn(b"private-session-123", row[1])

    def test_streamed_snapshot_has_no_whole_chat_ceiling_and_defaults_to_latest_page(self):
        def emit_messages(emit):
            for index in range(320):
                emit({"role": "assistant", "text": f"Message {index}: " + "x" * 8192})
        share = self.store.create_streamed_share("chat-one", emit_messages, title="Large snapshot")
        self.assertEqual(share["message_count"], 320)
        reopened = PublicChatShareStore.open_existing(self.root, now=lambda: self.clock)
        latest = reopened.get_snapshot_page(share["token"], share_id=share["share_id"])
        self.assertEqual((latest["page"], latest["page_count"], latest["message_count"]), (3, 4, 320))
        self.assertTrue(latest["snapshot"]["messages"][-1]["text"].startswith("Message 319:"))
        count = 0
        for page in range(latest["page_count"]):
            value = reopened.get_snapshot_page(share["token"], share_id=share["share_id"], page=page)
            self.assertLessEqual(len(value["snapshot"]["messages"]), 100)
            self.assertLessEqual(len(json.dumps(value["snapshot"]).encode()), TARGET_SNAPSHOT_PAGE_BYTES)
            count += len(value["snapshot"]["messages"])
        self.assertEqual(count, 320)
        with self.assertRaises(PublicChatShareUnavailable):
            reopened.get_snapshot_page(share["token"], share_id="share_" + "0" * 32)
        with self.assertRaises(PublicChatShareUnavailable):
            reopened.get_snapshot_page(share["token"], page=4)
        with reopened._connection(write=True) as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE public_chat_share_pages SET page_index=99")
        self.store.revoke_share(share["share_id"], session_id="chat-one")
        for page in (0, 3):
            with self.assertRaises(PublicChatShareUnavailable):
                reopened.get_snapshot_page(share["token"], page=page)

    def test_streamed_capture_rolls_back_pages_and_capability_on_late_failure(self):
        def interrupted(emit):
            for index in range(205):
                emit({"role": "user", "text": str(index)})
            raise ValueError("Synthetic source failure after completed pages")
        with self.assertRaisesRegex(ValueError, "Synthetic"):
            self.store.create_streamed_share("chat-one", interrupted)
        self.assertEqual(self.store.list_shares("chat-one"), [])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM public_chat_share_pages").fetchone()[0], 0)

    def test_legacy_snapshot_reads_without_creating_page_schema(self):
        share = self.create()
        with self.store._connection(write=True) as db:
            db.execute("DROP TABLE public_chat_share_pages")
            db.execute("PRAGMA user_version=2")
        before = self.store.database_path.read_bytes()
        legacy = PublicChatShareStore.open_existing(self.root, now=lambda: self.clock)
        value = legacy.get_snapshot_page(share["token"], share_id=share["share_id"])
        self.assertEqual((value["page"], value["page_count"]), (0, 1))
        self.assertEqual(value["snapshot"]["messages"], self.messages)
        self.assertEqual(self.store.database_path.read_bytes(), before)

    def test_exact_share_id_is_required_when_using_a_common_url(self):
        first = self.create()
        second = self.create()
        self.assertEqual(self.store.get_snapshot(first["token"], share_id=first["share_id"])["messages"], self.messages)
        for share_id in (second["share_id"], "bad", "share_" + "0" * 32):
            with self.assertRaises(PublicChatShareUnavailable):
                self.store.get_snapshot(first["token"], share_id=share_id)

    def test_list_is_session_scoped_bounded_and_cannot_recover_tokens(self):
        first = self.create()
        self.clock += 1
        second = self.create()
        self.store.create_share("another-session", self.messages)
        listed = self.store.list_shares("private-session-123")
        self.assertEqual([item["share_id"] for item in listed], [second["share_id"], first["share_id"]])
        self.assertEqual(len(self.store.list_shares("private-session-123", limit=1)), 1)
        self.assertEqual(self.store.list_shares("unknown-session"), [])
        self.assertTrue(all("token" not in item and "messages" not in item for item in listed))
        for invalid in (0, 101, -1, True, 1.0, "1"):
            with self.subTest(limit=invalid), self.assertRaises(PublicChatShareValidationError):
                self.store.list_shares("private-session-123", limit=invalid)

    def test_revocation_is_scoped_idempotent_durable_and_keeps_snapshot(self):
        result = self.create()
        self.assertFalse(self.store.revoke_share(result["share_id"], session_id="wrong-session"))
        self.assertEqual(self.store.get_snapshot(result["token"])["messages"], self.messages)
        self.clock += 5
        self.assertTrue(self.store.revoke_share(result["share_id"], session_id=result["session_id"]))
        revoked_at = self.clock
        self.clock += 5
        self.assertTrue(self.store.revoke_share(result["share_id"], session_id=result["session_id"]))
        reopened = PublicChatShareStore(self.root, now=lambda: self.clock)
        with self.assertRaises(PublicChatShareUnavailable):
            reopened.get_snapshot(result["token"])
        self.assertEqual(reopened.list_shares(result["session_id"])[0]["revoked_at"], revoked_at)
        self.assertFalse(reopened.revoke_share("../not-a-share", session_id=result["session_id"]))
        with sqlite3.connect(self.store.database_path) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM public_chat_shares").fetchone()[0], 1)
            for table in ("public_chat_shares", "public_chat_share_revocations"):
                with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(f"DELETE FROM {table}")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE public_chat_shares SET title = 'changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE public_chat_share_revocations SET revoked_at = 0")

    def test_expiry_at_exact_boundary_is_unavailable(self):
        result = self.create(expires_at=self.clock + 10)
        self.clock += 9.99
        self.store.get_snapshot(result["token"])
        self.clock = result["expires_at"]
        with self.assertRaises(PublicChatShareUnavailable):
            self.store.get_snapshot(result["token"])
        self.assertIsNone(self.store.list_shares(result["session_id"])[0]["revoked_at"])

    def test_all_unavailable_tokens_have_the_same_error(self):
        revoked = self.create()
        self.store.revoke_share(revoked["share_id"], session_id=revoked["session_id"])
        expired = self.create(expires_at=self.clock + 1)
        self.clock += 1
        errors = []
        for token in ("", "x" * 1000000, "../" + "x" * 40, "é" * 43, "x" * 43, revoked["token"], expired["token"], None):
            with self.subTest(token_length=len(token) if token is not None else None):
                try:
                    self.store.get_snapshot(token)
                except PublicChatShareUnavailable as exc:
                    errors.append(str(exc))
                else:
                    self.fail("unavailable token returned a snapshot")
        self.assertEqual(set(errors), {"Shared conversation is unavailable."})
        with patch.object(self.store, "_connection", side_effect=AssertionError("must not read storage")):
            with self.assertRaises(PublicChatShareUnavailable):
                self.store.get_snapshot("invalid")

    def test_rejects_unprojected_messages_and_bad_types(self):
        invalid = [
            [], tuple(self.messages), [{"role": "system", "text": "private"}],
            [{"role": "tool", "text": "private"}], [{"role": "assistant", "text": "a", "tool_calls": []}],
            [{"role": "user", "text": "a", "session_id": "secret"}],
            [{"role": "user", "text": 1}], [{"role": "user"}], [None],
            [{"role": ["user"], "text": "a"}], [{"role": "user", "text": "\ud800"}],
        ]
        for messages in invalid:
            with self.subTest(messages=messages), self.assertRaises(PublicChatShareValidationError):
                self.store.create_share("session", messages)
        for title in ("", " ", "x" * 257, 4, "\ud800"):
            with self.subTest(title=repr(title)), self.assertRaises(PublicChatShareValidationError):
                self.create(title=title)
        for session_id in ("", None, "a\x00b", "x" * 1025):
            with self.subTest(session_id=session_id), self.assertRaises(PublicChatShareValidationError):
                self.store.create_share(session_id, self.messages)

    def test_rejects_invalid_expiry_and_message_timestamps(self):
        bad_timestamps = (True, False, -1, float("nan"), float("inf"), "tomorrow", {}, 253402300800)
        for value in bad_timestamps + (self.clock, self.clock - 1):
            with self.subTest(expiry=value), self.assertRaises(PublicChatShareValidationError):
                self.create(expires_at=value)
        for value in bad_timestamps + (None,):
            with self.subTest(timestamp=value), self.assertRaises(PublicChatShareValidationError):
                self.store.create_share("session", [{"role": "user", "text": "a", "timestamp": value}])

    def test_large_message_utf8_and_json_escaping_roundtrip_without_size_quota(self):
        for messages in (
            [{"role": "user", "text": "x" * (3 * 1024 * 1024)}],
            [{"role": "user", "text": "😀" * (700 * 1024)}],
            [{"role": "user", "text": "\x01" * (400 * 1024)}],
            [{"role": "user", "text": "x" * (300 * 1024)}] * 9,
        ):
            with self.subTest(count=len(messages), length=len(messages[0]["text"])):
                share = self.store.create_share("session", messages)
                snapshot = self.store.get_snapshot(share["token"])
                self.assertEqual(snapshot["messages"], messages)
                self.assertGreater(len(json.dumps(snapshot).encode()), 2 * 1024 * 1024)

    def test_streamed_oversized_message_gets_its_own_complete_page(self):
        long_text = "    Large genuine report with unicode 中文\n" * 90_000
        messages = [{"role": "user", "text": "Before the long report"},
                    {"role": "assistant", "text": long_text, "timestamp": self.clock},
                    {"role": "user", "text": "After the long report"}]
        self.assertGreater(len(long_text.encode()), TARGET_SNAPSHOT_PAGE_BYTES)
        def emit(sink):
            for message in messages:
                sink(message)
        share = self.store.create_streamed_share("chat-one", emit)
        self.assertEqual(share["message_count"], 3)
        reopened = PublicChatShareStore.open_existing(self.root, now=lambda: self.clock)
        restored = []
        for index, expected in enumerate(messages):
            page = reopened.get_snapshot_page(share["token"], share_id=share["share_id"], page=index)
            self.assertEqual((page["page"], page["page_count"], page["message_count"]), (index, 3, 3))
            self.assertEqual(page["snapshot"]["messages"], [expected])
            restored.extend(page["snapshot"]["messages"])
        self.assertEqual(restored, messages)
        self.store.revoke_share(share["share_id"], session_id="chat-one")
        with self.assertRaises(PublicChatShareUnavailable):
            reopened.get_snapshot_page(share["token"], page=1)

    def test_large_first_streamed_message_and_later_invalid_message_roll_back(self):
        long_text = "x" * (3 * 1024 * 1024)
        def emit(sink):
            sink({"role": "user", "text": long_text})
            sink({"role": "assistant", "text": "Valid next page"})
            sink({"role": "assistant", "text": long_text, "tool_output": "Private"})
        with self.assertRaises(PublicChatShareValidationError):
            self.store.create_streamed_share("chat-one", emit)
        self.assertEqual(self.store.list_shares("chat-one"), [])
        with self.store._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM public_chat_share_pages").fetchone()[0], 0)

    def test_escaped_long_message_is_readable_past_old_render_limit(self):
        text = "&" * (4 * 1024 * 1024) + "<script>never execute</script>"
        share = self.store.create_streamed_share("chat-one", lambda sink: sink({"role": "user", "text": text}))
        snapshot = self.store.get_snapshot(share["token"])
        rendered = render_public_chat_html(snapshot)
        self.assertGreater(len(rendered), 16 * 1024 * 1024)
        self.assertEqual(rendered.count(b"&amp;"), 4 * 1024 * 1024)
        self.assertIn(b"&lt;script&gt;never execute&lt;/script&gt;", rendered)
        self.assertNotIn(b"<script>", rendered)

    def test_v1_upgrade_preserves_snapshots_revocations_and_rolls_back_failure(self):
        active = self.create()
        revoked = self.create()
        self.store.revoke_share(revoked["share_id"], session_id="private-session-123")
        with self.store._connection() as db:
            legacy_sql = "\n".join(db.iterdump()).replace(
                "CHECK(message_count >= 1)", "CHECK(message_count BETWEEN 1 AND 1000)")
        legacy_root = Path(self.temporary.name) / "legacy"
        legacy_root.mkdir(mode=0o700)
        legacy_path = legacy_root / "snapshots.sqlite3"
        legacy_path.touch(mode=0o600)
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.executescript(legacy_sql)
            db.execute("PRAGMA user_version=1")
        before = legacy_path.read_bytes()
        old = PublicChatShareStore.open_existing(legacy_root, now=lambda: self.clock)
        self.assertEqual(old.get_snapshot(active["token"])["messages"], self.messages)
        self.assertEqual(legacy_path.read_bytes(), before)  # Anonymous cold read never migrates.
        messages = [{"role": "assistant", "text": f"Synthetic result {index}"} for index in range(2504)]
        connect = old._connection
        @contextmanager
        def interrupted_connection(*, write=False):
            with connect(write=write) as db:
                class Interrupted:
                    def execute(self, sql, *args):
                        if sql.startswith("ALTER TABLE public_chat_shares_expanded"):
                            raise sqlite3.OperationalError("database or disk is full")
                        return db.execute(sql, *args)
                yield Interrupted()
        with patch.object(old, "_connection", interrupted_connection):
            with self.assertRaises(sqlite3.OperationalError):
                old.create_share("session", messages)
        with old._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual("\n".join(db.iterdump()), legacy_sql)
        self.assertEqual(old.get_snapshot(active["token"])["messages"], self.messages)
        with self.assertRaises(PublicChatShareUnavailable):
            old.get_snapshot(revoked["token"])
        large = old.create_share("session", messages)  # Cached v1 instance upgrades on this authenticated write.
        self.assertEqual(large["message_count"], 2504)
        self.assertEqual(old.get_snapshot(large["token"])["messages"], messages)
        self.assertEqual(old.get_snapshot(active["token"])["messages"], self.messages)
        with self.assertRaises(PublicChatShareUnavailable):
            old.get_snapshot(revoked["token"])
        with old._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 6)
        with self.assertRaises(sqlite3.IntegrityError), old._connection(write=True) as db:
            db.execute("DELETE FROM public_chat_shares WHERE share_id=?", (active["share_id"],))

    def test_existing_v3_size_constraints_migrate_atomically_without_version_gate(self):
        def emit(sink):
            for index in range(105):
                sink({"role": "assistant", "text": f"Original paragraph {index}"})
        active = self.store.create_streamed_share("legacy-chat", emit)
        revoked = self.create()
        self.store.revoke_share(revoked["share_id"], session_id=revoked["session_id"])
        expected_pages = [self.store.get_snapshot_page(active["token"], page=index) for index in (0, 1)]
        with self.store._connection() as db:
            original_rows = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                             for table in ("public_chat_shares", "public_chat_share_pages", "public_chat_share_revocations")}
            legacy_sql = "\n".join(db.iterdump()).replace("snapshot_json BLOB NOT NULL,",
                "snapshot_json BLOB NOT NULL CHECK(length(snapshot_json) <= 2097152),")
        self.assertEqual(legacy_sql.count("CHECK(length(snapshot_json) <= 2097152)"), 2)
        legacy_root = Path(self.temporary.name) / "legacy-v3"
        legacy_root.mkdir(mode=0o700)
        legacy_path = legacy_root / "snapshots.sqlite3"
        legacy_path.touch(mode=0o600)
        with closing(sqlite3.connect(legacy_path)) as db, db:
            db.executescript(legacy_sql)
            db.execute("PRAGMA user_version=3")
        before = legacy_path.read_bytes()
        old = PublicChatShareStore.open_existing(legacy_root, now=lambda: self.clock)
        self.assertEqual(old.get_snapshot_page(active["token"], page=1), expected_pages[1])
        self.assertEqual(legacy_path.read_bytes(), before)
        connect = old._connection
        @contextmanager
        def interrupted_connection(*, write=False):
            with connect(write=write) as db:
                class Interrupted:
                    def execute(self, sql, *args):
                        if sql.startswith("ALTER TABLE public_chat_share_pages_expanded"):
                            raise sqlite3.OperationalError("Synthetic migration write failure")
                        return db.execute(sql, *args)
                yield Interrupted()
        text = "Large migrated message 中文\n" * 100_000
        with patch.object(old, "_connection", interrupted_connection):
            with self.assertRaisesRegex(sqlite3.OperationalError, "Synthetic"):
                old.create_streamed_share("new-chat", lambda sink: sink({"role": "user", "text": text}))
        with old._connection() as db:
            self.assertEqual("\n".join(db.iterdump()), legacy_sql)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(old.get_snapshot_page(active["token"], page=1), expected_pages[1])
        created = old.create_streamed_share("new-chat", lambda sink: sink({"role": "user", "text": text}))
        self.assertEqual(old.get_snapshot(created["token"])["messages"], [{"role": "user", "text": text}])
        for index in (0, 1):
            self.assertEqual(old.get_snapshot_page(active["token"], page=index), expected_pages[index])
        with self.assertRaises(PublicChatShareUnavailable):
            old.get_snapshot(revoked["token"])
        with old._connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger'").fetchone()[0], 6)
            for table, rows in original_rows.items():
                current = [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                self.assertTrue(all(row in current for row in rows), table)
            self.assertNotIn("CHECK(length(snapshot_json)", "\n".join(db.iterdump()))
        with self.assertRaises(sqlite3.IntegrityError), old._connection(write=True) as db:
            db.execute("DELETE FROM public_chat_share_pages WHERE share_id=?", (active["share_id"],))

    def test_html_escapes_plaintext_and_has_no_interactive_or_remote_content(self):
        literal = '<script>alert(1)</script>\n<img src="https://example.invalid/a" onerror="x()">\n[link](https://example.invalid/)\n```html\n<b>code</b>\n```\n  spaces\tand & < > "quotes"'
        result = self.store.create_share("session", [{"role": "user", "text": literal}], title="<iframe> & title")
        page = render_public_chat_html(self.store.get_snapshot(result["token"])).decode()
        parsed = _ParsedHTML()
        parsed.feed(page)
        self.assertIn(literal, parsed.text)
        self.assertIn("<iframe> & title", parsed.text)
        self.assertTrue(set(parsed.tags).isdisjoint({"script", "iframe", "img", "a", "form", "input", "button", "object", "embed", "link"}))
        self.assertFalse(any(name.lower().startswith("on") or name in ("href", "src") for name, _ in parsed.attributes))
        self.assertIn("white-space:pre-wrap", page)
        self.assertIn("Read-only conversation snapshot", page)
        self.assertNotIn(result["token"], page)
        self.assertNotIn(result["share_id"], page)
        headers = public_chat_share_headers()
        css = page.split("<style>", 1)[1].split("</style>", 1)[0]
        style_hash = base64.b64encode(hashlib.sha256(css.encode()).digest()).decode()
        self.assertIn(f"style-src 'sha256-{style_hash}'", headers["Content-Security-Policy"])
        for directive in ("default-src 'none'", "script-src 'none'", "connect-src 'none'", "form-action 'none'", "frame-ancestors 'none'", "sandbox"):
            self.assertIn(directive, headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", headers["Content-Security-Policy"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")

    def test_renderer_revalidates_snapshot(self):
        result = self.create()
        snapshot = self.store.get_snapshot(result["token"])
        snapshot["session_id"] = "private"
        with self.assertRaises(PublicChatShareValidationError):
            render_public_chat_html(snapshot)

    def test_assistant_markdown_has_readable_structure_and_literal_code(self):
        message = """## A practical plan

Start with **a small change** and inspect `result < expected`.

- First task
- Second task with *emphasis*

3. Third step
4. Fourth step

> Keep this observation visible.

```python
if result < expected:
    print("<script>not markup</script>")
```

| Check | Outcome |
| :--- | ---: |
| **Desktop** | Ready |
| Mobile | Reviewing |

---

Final paragraph.
"""
        page = render_public_chat_html({"title": "Planning", "created_at": self.clock,
            "messages": [{"role": "assistant", "text": message}]}).decode()
        parsed = _ParsedHTML()
        parsed.feed(page)
        for tag in ("h3", "strong", "em", "code", "ul", "ol", "li", "blockquote", "pre", "table", "thead", "th", "tbody", "td", "hr"):
            self.assertIn(tag, parsed.tags)
        self.assertIn('<ol start="3">', page)
        self.assertIn('if result < expected:\n    print("<script>not markup</script>")', parsed.text)
        self.assertIn("Final paragraph.", parsed.text)
        self.assertNotIn("script", parsed.tags)

    def test_assistant_markdown_cannot_emit_active_html_urls_or_attributes(self):
        message = """# <img src=x onerror=alert(1)>
**<script>alert(2)</script>** and `<iframe src=x>`.
[link](javascript:alert(3)) ![pixel](https://example.invalid/pixel)
<style>body{display:none}</style><svg onload=alert(4)>
```</div><script>alert(5)</script>
<object data=x>literal code</object>
```
| <img src=x> | Header |
| --- | --- |
| <a href=https://example.invalid/> | Safe |
"""
        page = render_public_chat_html({"title": "</title><script>alert(6)</script>",
            "created_at": self.clock, "messages": [{"role": "assistant", "text": message}]}).decode()
        parsed = _ParsedHTML()
        parsed.feed(page)
        self.assertTrue(set(parsed.tags).isdisjoint({"script", "iframe", "img", "a", "form", "input", "button", "object", "embed", "link", "svg"}))
        self.assertEqual(parsed.tags.count("style"), 1)
        self.assertFalse(any(name.lower().startswith("on") or name in ("href", "src", "data", "style") for name, _ in parsed.attributes))
        self.assertIn("[link](javascript:alert(3)) ![pixel](https://example.invalid/pixel)", "".join(parsed.text))
        self.assertIn("</div><script>alert(5)</script>", parsed.text)

    def test_view_only_page_preserves_roles_order_and_optional_timestamps(self):
        page = render_public_chat_html({"title": "A saved conversation", "created_at": self.clock,
            "messages": self.messages}).decode()
        parsed = _ParsedHTML()
        parsed.feed(page)
        self.assertIn("View only", parsed.text)
        self.assertIn("2 messages", parsed.text)
        self.assertLess(page.index('class="message user"'), page.index('class="message assistant"'))
        self.assertEqual(parsed.tags.count("time"), 2)  # Shared time plus the one supplied message time.
        self.assertEqual(parsed.tags.count("h1"), 1)
        self.assertIn(("datetime", "2027-01-15T07:58:20+00:00"), parsed.attributes)
        self.assertIn("prefers-color-scheme:dark", page)
        self.assertIn("@media(max-width:600px)", page)

    def test_incomplete_markdown_and_long_heading_preserve_visible_text(self):
        message = "# " + " " * 20000 + "Heading\n\n```\n<unfinished>\nnext line"
        page = render_public_chat_html({"title": "Incomplete reply", "created_at": self.clock,
            "messages": [{"role": "assistant", "text": message}]}).decode()
        parsed = _ParsedHTML()
        parsed.feed(page)
        self.assertIn("<unfinished>\nnext line", parsed.text)
        self.assertIn("Heading", "".join(parsed.text))
        self.assertEqual(parsed.tags.count("pre"), 1)

    def test_rejects_nonprivate_or_symlink_storage_without_changing_permissions(self):
        with self.assertRaises(PublicChatShareValidationError):
            PublicChatShareStore("relative")
        unsafe = Path(self.temporary.name) / "unsafe"
        unsafe.mkdir(mode=0o755)
        unsafe.chmod(0o755)
        with self.assertRaises(OSError):
            PublicChatShareStore(unsafe)
        self.assertEqual(unsafe.stat().st_mode & 0o777, 0o755)
        linked_root = Path(self.temporary.name) / "linked-root"
        linked_root.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            PublicChatShareStore(linked_root)
        linked_db_root = Path(self.temporary.name) / "linked-db"
        linked_db_root.mkdir(mode=0o700)
        (linked_db_root / "snapshots.sqlite3").symlink_to(self.store.database_path)
        with self.assertRaises(OSError):
            PublicChatShareStore(linked_db_root)
        hardlinked_db_root = Path(self.temporary.name) / "hardlinked-db"
        hardlinked_db_root.mkdir(mode=0o700)
        os.link(self.store.database_path, hardlinked_db_root / "snapshots.sqlite3")
        with self.assertRaises(OSError):
            PublicChatShareStore(hardlinked_db_root)

    def test_parallel_first_open_create_and_revoke(self):
        parallel_root = Path(self.temporary.name) / "parallel"

        def create_and_revoke(index):
            store = PublicChatShareStore(parallel_root, now=lambda: self.clock)
            result = store.create_share("parallel-session", [{"role": "user", "text": str(index)}])
            self.assertEqual(store.get_snapshot(result["token"])["messages"][0]["text"], str(index))
            store.revoke_share(result["share_id"], session_id="parallel-session")
            return result

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(create_and_revoke, range(16)))
        self.assertEqual(len({item["token"] for item in results}), 16)
        reopened = PublicChatShareStore(parallel_root, now=lambda: self.clock)
        self.assertEqual(len(reopened.list_shares("parallel-session")), 16)
        for result in results:
            with self.assertRaises(PublicChatShareUnavailable):
                reopened.get_snapshot(result["token"])

    def test_existing_unsafe_database_is_rejected_without_chmod(self):
        self.store.database_path.chmod(0o644)
        with self.assertRaises(OSError):
            PublicChatShareStore(self.root)
        self.assertEqual(self.store.database_path.stat().st_mode & 0o777, 0o644)
        with self.assertRaises(OSError):
            self.store.list_shares("session")

    def test_corrupt_snapshot_fails_closed(self):
        result = self.create()
        with sqlite3.connect(self.store.database_path) as connection:
            # Simulate corruption without relying on or weakening the API's
            # immutable-row policy. An unchanged digest must reject new bytes.
            connection.execute("DROP TRIGGER public_chat_shares_no_update")
            connection.execute("UPDATE public_chat_shares SET snapshot_json = ?", (b"{}",))
        with self.assertRaises(PublicChatShareUnavailable):
            self.store.get_snapshot(result["token"])

    def test_open_existing_and_view_use_only_read_only_connections(self):
        result = self.create()
        previous_mtime = self.store.database_path.stat().st_mtime_ns
        self.store.database_path.chmod(0o400)
        self.root.chmod(0o500)
        try:
            with patch("public_chat_shares.sqlite3.connect", wraps=sqlite3.connect) as connect:
                with patch("public_chat_shares.os.open", side_effect=AssertionError("must not open/create paths")):
                    existing = PublicChatShareStore.open_existing(self.root, now=lambda: self.clock)
                    self.assertEqual(existing.get_snapshot(result["token"])["messages"], self.messages)
            self.assertTrue(all(call.args[0].endswith("?mode=ro") for call in connect.call_args_list))
            self.assertEqual(self.store.database_path.stat().st_mtime_ns, previous_mtime)
            self.assertEqual({item.name for item in self.root.iterdir()}, {"snapshots.sqlite3"})
        finally:
            self.root.chmod(0o700)
            self.store.database_path.chmod(0o600)

    def test_open_existing_does_not_create_missing_storage(self):
        missing = Path(self.temporary.name) / "missing"
        with self.assertRaises(OSError):
            PublicChatShareStore.open_existing(missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
