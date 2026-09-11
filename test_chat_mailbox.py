"""Offline mailbox tests on a synthetic copy of the envelope schema."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import chat_mailbox as mailbox


PAIR = "pair_" + "1" * 32
OTHER_PAIR = "pair_" + "2" * 32
NOW = "2026-01-01T00:00:00Z"


class ChatMailboxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "ledger.sqlite3"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.addCleanup(self.connection.close)
        self.connection.execute("""CREATE TABLE cross_chat_envelopes (
            id TEXT PRIMARY KEY, kind TEXT, source_session_id TEXT, source_run_id TEXT,
            target_session_id TEXT, action TEXT, body TEXT, authorization_kind TEXT,
            authorization_pair_id TEXT, status TEXT, target_run_id TEXT,
            target_body TEXT, message_revision INTEGER DEFAULT 0, created_at TEXT,
            delivery_mode TEXT, reply_to_message_id TEXT, lifecycle_status TEXT DEFAULT '', updated_at TEXT)""")
        mailbox.initialize(self.connection)

    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def store(self, message_id, *, body="Synthetic message.", source="sender", target="recipient",
              pair=PAIR, parent=None, mode="mailbox", status="stored"):
        self.connection.execute("""INSERT INTO cross_chat_envelopes
            (id,kind,source_session_id,source_run_id,target_session_id,action,body,
             authorization_kind,authorization_pair_id,status,created_at,delivery_mode,reply_to_message_id)
            VALUES(?,'instruction',?,'example-run',?,'instruction',?,'configured_route',?,?,?,?,?)""",
            (message_id, source, target, body, pair, status, NOW, mode, parent))
        return mailbox.store_message(self.connection, message_id, now=NOW)

    def read(self, request="example-read", **patch):
        args = dict(target_session_id="recipient", source_session_id="sender",
                    reader_run_id="reader-run", request_id=request, allowed_pair_ids=[PAIR], now=NOW)
        args.update(patch)
        return mailbox.read_sender(self.connection, **args)

    def test_storage_and_ui_reads_are_passive_and_preserve_identity(self):
        with self.transaction():
            original = self.store("message-one", body="First exact body.")
            duplicate = mailbox.store_message(self.connection, "message-one", now="later")
            self.store("other-sender", source="another-sender", pair=OTHER_PAIR)
        self.assertEqual(original, duplicate)
        groups = mailbox.list_senders(self.connection, "recipient", [PAIR, OTHER_PAIR])
        self.assertEqual([row["unread_count"] for row in groups["senders"]], [1, 1])
        page = mailbox.list_messages(self.connection, "recipient", None, [PAIR, OTHER_PAIR])
        self.assertEqual([row["message_id"] for row in page["messages"]], ["message-one", "other-sender"])
        self.assertTrue(all(row["read_at"] is None for row in page["messages"]))
        row = self.connection.execute("SELECT * FROM cross_chat_envelopes WHERE id='message-one'").fetchone()
        self.assertEqual((row["body"], row["status"], row["created_at"], row["target_run_id"]),
                         ("First exact body.", "stored", NOW, None))

    def test_snapshot_pages_retries_and_later_arrivals_stay_unread(self):
        with self.transaction():
            for index in range(3):
                self.store(f"message-{index}")
        with self.transaction():
            first = self.read(limit=2)
        self.assertEqual([row["message_id"] for row in first["messages"]], ["message-0", "message-1"])
        self.assertTrue(first["has_more"])
        with self.transaction():
            late = self.store("later-message")
            repeated = self.read(limit=2)
            second = self.read(limit=2, after_seq=first["next_after_seq"])
        self.assertEqual(repeated["messages"], first["messages"])
        self.assertTrue(repeated["replayed"])
        self.assertEqual([row["message_id"] for row in second["messages"]], ["message-2"])
        self.assertFalse(second["has_more"])
        self.assertGreater(late["mailbox_seq"], first["snapshot_seq"])
        unread = mailbox.list_messages(self.connection, "recipient", "sender", [PAIR], unread_only=True)
        self.assertEqual([row["message_id"] for row in unread["messages"]], ["later-message"])
        with self.transaction():
            third = self.read("new-read")
        self.assertEqual([row["message_id"] for row in third["messages"]], ["later-message"])

    def test_cancel_delete_revoke_are_rechecked_on_receipt_replay(self):
        with self.transaction():
            self.store("cancelled-message")
            self.store("deleted-message")
            self.store("remaining-message")
            first = self.read()
        with self.transaction():
            self.assertFalse(mailbox.exclude_message(self.connection, "cancelled-message", target_session_id="other",
                                                      reason="cancelled", now=NOW))
            self.assertTrue(mailbox.exclude_message(self.connection, "cancelled-message", target_session_id="recipient",
                                                     reason="cancelled", now=NOW))
            self.connection.execute("DELETE FROM cross_chat_envelopes WHERE id='deleted-message'")
            repeated = self.read()
            revoked = self.read(allowed_pair_ids=[])
        self.assertEqual(first["snapshot_seq"], repeated["snapshot_seq"])
        self.assertEqual([row["message_id"] for row in repeated["messages"]], ["remaining-message"])
        self.assertEqual(repeated["unavailable_count"], 2)
        self.assertEqual(revoked["messages"], [])
        self.assertEqual(revoked["unavailable_count"], 3)
        with self.transaction():
            self.assertEqual(mailbox.exclude_pair(self.connection, PAIR, now=NOW), 1)
        self.assertEqual(mailbox.list_senders(self.connection, "recipient", [PAIR])["senders"], [])

    def test_read_receipt_survives_reopen_and_outbox_is_idempotent(self):
        with self.transaction():
            self.store("durable-message")
            first = self.read()
        self.connection.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        with self.transaction():
            repeated = self.read()
        self.assertEqual(first["messages"], repeated["messages"])
        self.assertEqual([row["message_id"] for row in mailbox.pending_read_events(self.connection)], ["durable-message"])
        with self.transaction():
            self.assertTrue(mailbox.mark_read_event_published(self.connection, "durable-message"))
            self.assertFalse(mailbox.mark_read_event_published(self.connection, "durable-message"))
        self.assertEqual(mailbox.pending_read_events(self.connection), [])

    def test_response_bytes_bound_pages_without_truncation_or_consuming_unsent_items(self):
        body = "X" * 60_000
        with self.transaction():
            self.store("large-one", body=body)
            self.store("large-two", body=body)
            page = self.read()
        self.assertEqual([row["body"] for row in page["messages"]], [body])
        self.assertTrue(page["has_more"])
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=True).encode()), mailbox.MAX_PAGE_BYTES)
        unread = mailbox.list_messages(self.connection, "recipient", None, [PAIR], unread_only=True)
        self.assertEqual([row["message_id"] for row in unread["messages"]], ["large-two"])

    def test_reply_identity_foreign_scope_and_invalid_snapshot_cursor_fail_closed(self):
        with self.transaction():
            self.store("original", source="recipient", target="sender")
            reply = self.store("reply", parent="original")
            self.assertEqual(reply["in_reply_to_message_id"], "original")
            with self.assertRaises(mailbox.MailboxConflict):
                self.store("wrong-peer", source="other", parent="original")
            page = self.read(limit=1)
            with self.assertRaises(mailbox.MailboxConflict):
                self.read(limit=1, source_session_id="other")
            with self.assertRaises(mailbox.MailboxConflict):
                self.read(limit=1, after_seq=999)
        self.assertEqual(mailbox.list_messages(self.connection, "other-recipient", None, [PAIR])["messages"], [])
        self.assertEqual(page["messages"][0]["message_id"], "reply")
        with self.assertRaises(RuntimeError):
            self.read("without-transaction")

    def test_rollback_does_not_consume_and_session_exclusion_keeps_body(self):
        with self.transaction():
            self.store("rollback-message")
        self.connection.execute("BEGIN IMMEDIATE")
        self.read()
        self.connection.rollback()
        self.assertIsNone(mailbox.list_messages(self.connection, "recipient", None, [PAIR])["messages"][0]["read_at"])
        with self.transaction():
            self.assertEqual(mailbox.exclude_session(self.connection, "sender", now=NOW), 1)
        self.assertEqual(mailbox.list_messages(self.connection, "recipient", None, [PAIR])["messages"], [])
        self.assertEqual(self.connection.execute("SELECT body FROM cross_chat_envelopes").fetchone()[0], "Synthetic message.")

    def test_unread_groups_and_startup_targets_clear_after_explicit_read(self):
        with self.transaction():
            self.store("unread-message")
        self.assertEqual(mailbox.unread_targets(self.connection), ["recipient"])
        with self.transaction():
            self.read()
        self.assertEqual(mailbox.unread_targets(self.connection), [])
        self.assertEqual(mailbox.list_senders(self.connection, "recipient", [PAIR], unread_only=True)["senders"], [])
        self.assertEqual(mailbox.list_senders(self.connection, "recipient", [PAIR])["senders"][0]["unread_count"], 0)

    def test_unicode_and_control_character_messages_fit_without_truncation(self):
        for body in ("😀" * 16_000, "\x01" * 16_000):
            with self.subTest(body_kind="unicode" if body[0] == "😀" else "control"):
                message_id = "unicode-message" if body[0] == "😀" else "control-message"
                with self.transaction():
                    self.store(message_id, body=body)
                    page = self.read(message_id)
                self.assertEqual(page["messages"][0]["body"], body)
                self.assertLess(len(json.dumps(page, ensure_ascii=False).encode("utf-8")), 128 * 1024)
        with self.assertRaises(ValueError):
            mailbox.list_messages(self.connection, "recipient", None, [PAIR], after_seq=1 << 63)

    def test_cancel_and_read_have_one_atomic_winner_and_deleted_parent_is_not_reusable(self):
        with self.transaction():
            self.store("cancel-before-read")
            self.assertTrue(mailbox.cancel_message(self.connection, "cancel-before-read", now=NOW))
            self.assertFalse(mailbox.cancel_message(self.connection, "cancel-before-read", now=NOW))
            self.assertEqual(self.read()["messages"], [])
            self.store("read-before-cancel")
            self.read("next-read")
            with self.assertRaises(mailbox.MailboxConflict):
                mailbox.cancel_message(self.connection, "read-before-cancel", now=NOW)
            self.store("deleted-parent", source="recipient", target="sender")
            mailbox.exclude_message(self.connection, "deleted-parent", target_session_id="sender", reason="deleted", now=NOW)
            with self.assertRaises(mailbox.MailboxConflict):
                self.store("reply-to-deleted", parent="deleted-parent")
        self.assertEqual(self.connection.execute("SELECT status FROM cross_chat_envelopes WHERE id='cancel-before-read'").fetchone()[0], "cancelled")


if __name__ == "__main__":
    unittest.main()
