"""Pure store/view tests: no server imports, transcript discovery, or home access."""

from concurrent.futures import ThreadPoolExecutor
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
    MAX_MESSAGES,
    MAX_MESSAGE_TEXT_BYTES,
    MAX_SNAPSHOT_BYTES,
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

    def test_message_and_snapshot_bounds_include_utf8_and_json_escaping(self):
        for messages in (
            [{"role": "user", "text": ""}] * (MAX_MESSAGES + 1),
            [{"role": "user", "text": "x" * (MAX_MESSAGE_TEXT_BYTES + 1)}],
            [{"role": "user", "text": "😀" * (MAX_MESSAGE_TEXT_BYTES // 4 + 1)}],
            [{"role": "user", "text": "x" * MAX_MESSAGE_TEXT_BYTES}] * 9,
            [{"role": "user", "text": "\x01" * MAX_MESSAGE_TEXT_BYTES}] * 2,
        ):
            with self.subTest(count=len(messages)), self.assertRaises(PublicChatShareValidationError):
                self.store.create_share("session", messages)
        near_max = self.store.create_share("session", [{"role": "user", "text": "&" * MAX_MESSAGE_TEXT_BYTES}] * 7)
        snapshot = self.store.get_snapshot(near_max["token"])
        self.assertLess(len(json.dumps(snapshot).encode()), MAX_SNAPSHOT_BYTES)
        self.assertGreater(len(render_public_chat_html(snapshot)), MAX_SNAPSHOT_BYTES)

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

    def test_renderer_revalidates_snapshot_and_enforces_output_bound(self):
        result = self.create()
        snapshot = self.store.get_snapshot(result["token"])
        with patch("public_chat_shares.MAX_RENDER_BYTES", 32):
            with self.assertRaises(PublicChatShareValidationError):
                render_public_chat_html(snapshot)
        snapshot["session_id"] = "private"
        with self.assertRaises(PublicChatShareValidationError):
            render_public_chat_html(snapshot)

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
