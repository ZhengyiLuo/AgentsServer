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
            source_user_instruction TEXT DEFAULT '', source_user_delegation_action TEXT DEFAULT '',
            delivery_mode TEXT, reply_to_message_id TEXT, lifecycle_status TEXT DEFAULT '', updated_at TEXT)""")
        mailbox.initialize(self.connection)

    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        return self.connection

    def store(self, message_id, *, body="Synthetic message.", source="sender", target="recipient",
              pair=PAIR, parent=None, mode="mailbox", status="stored", source_user_instruction=""):
        self.connection.execute("""INSERT INTO cross_chat_envelopes
            (id,kind,source_session_id,source_run_id,target_session_id,action,body,
             authorization_kind,authorization_pair_id,status,created_at,delivery_mode,reply_to_message_id,
             source_user_instruction,source_user_delegation_action)
            VALUES(?,'instruction',?,'example-run',?,'instruction',?,'configured_route',?,?,?,?,?,?,?)""",
            (message_id, source, target, body, pair, status, NOW, mode, parent, source_user_instruction, "route" if source_user_instruction else ""))
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
            repeated = self.read(reader_run_id="fresh-reader")
            revoked = self.read(reader_run_id="another-reader", allowed_pair_ids=[])
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
            repeated = self.read(reader_run_id="reconnected-reader")
        self.assertEqual(first["messages"], repeated["messages"])
        self.assertEqual(first["read_id"], repeated["read_id"])
        self.assertTrue(repeated["replayed"])
        self.assertEqual(self.connection.execute("SELECT reader_run_id FROM chat_mailbox_reads").fetchone()[0], "reader-run")
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

    def test_unicode_source_instruction_counts_toward_page_bytes_without_truncation(self):
        instruction = "Render 😀\n" * 5_000
        with self.transaction():
            for message_id in ("source-first", "source-second"):
                self.store(message_id, body="B" * 8_000, source_user_instruction=instruction)
            first = self.read()
        self.assertEqual([row["message_id"] for row in first["messages"]], ["source-first"])
        self.assertEqual(first["messages"][0]["user_delegation"]["source_user_instruction"], instruction)
        self.assertTrue(first["has_more"])
        self.assertLess(len(json.dumps(first, ensure_ascii=False).encode("utf-8")), mailbox.MAX_PAGE_BYTES)
        unread = mailbox.list_messages(self.connection, "recipient", "sender", [PAIR], unread_only=True)
        self.assertEqual([row["message_id"] for row in unread["messages"]], ["source-second"])
        with self.transaction():
            second = self.read(after_seq=first["next_after_seq"])
        self.assertEqual(second["messages"][0]["user_delegation"]["source_user_instruction"], instruction)
        self.assertFalse(second["has_more"])

    def test_read_page_that_outgrows_byte_budget_errors_without_dropping_messages(self):
        with self.transaction():
            for message_id in ("legacy-first", "legacy-second"):
                self.store(message_id, body="B" * 45_000)
            original = self.read()
            # Simulate an expanded attested projection without changing
            # receipt membership; retries must reject rather than truncate.
            self.connection.execute("UPDATE cross_chat_envelopes SET source_user_instruction=?, source_user_delegation_action='route'",
                                    ("😀" * 3_000,))
        self.assertEqual(len(original["messages"]), 2)
        with self.transaction(), self.assertRaisesRegex(mailbox.MailboxConflict, "retry was not truncated"):
            self.read(reader_run_id="reconnected-reader")
        page = self.connection.execute("SELECT message_ids_json FROM chat_mailbox_read_pages").fetchone()
        self.assertEqual(json.loads(page[0]), ["legacy-first", "legacy-second"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM chat_mailbox_reads").fetchone()[0], 1)

    def test_fresh_run_continues_original_snapshot_without_consuming_late_arrivals(self):
        with self.transaction():
            self.store("snapshot-first")
            self.store("snapshot-second")
            first = self.read(limit=1)
        with self.transaction():
            self.store("late-arrival")
            repeated = self.read(limit=1, reader_run_id="fresh-reader")
            second = self.read(limit=1, reader_run_id="fresh-reader", after_seq=first["next_after_seq"])
            with self.assertRaises(mailbox.MailboxConflict):
                self.read(limit=1, reader_run_id="fresh-reader", source_session_id="other-sender")
            with self.assertRaises(mailbox.MailboxConflict):
                self.read(limit=2, reader_run_id="fresh-reader")
        self.assertEqual(repeated["messages"], first["messages"])
        self.assertEqual(second["read_id"], first["read_id"])
        self.assertEqual([row["message_id"] for row in second["messages"]], ["snapshot-second"])
        unread = mailbox.list_messages(self.connection, "recipient", "sender", [PAIR], unread_only=True)
        self.assertEqual([row["message_id"] for row in unread["messages"]], ["late-arrival"])

    def test_ambiguous_legacy_run_scoped_request_is_rejected_without_new_claim(self):
        with self.transaction():
            self.store("already-read")
            first = self.read()
            self.connection.execute("""INSERT INTO chat_mailbox_reads
                (id,target_session_id,source_session_id,reader_run_id,request_id,snapshot_seq,page_limit,created_at)
                SELECT 'legacy-second-receipt',target_session_id,source_session_id,'legacy-other-run',
                       request_id,snapshot_seq,page_limit,created_at FROM chat_mailbox_reads WHERE id=?""",
                (first["read_id"],))
            self.store("still-unread")
        mailbox.initialize(self.connection)  # Additive index accepts existing historical duplicates.
        with self.transaction(), self.assertRaisesRegex(mailbox.MailboxConflict, "multiple historical"):
            self.read(reader_run_id="fresh-reader")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM chat_mailbox_reads").fetchone()[0], 2)
        unread = mailbox.list_messages(self.connection, "recipient", "sender", [PAIR], unread_only=True)
        self.assertEqual([row["message_id"] for row in unread["messages"]], ["still-unread"])

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

    def test_wake_coalesces_duplicate_receipts_without_reading_or_relaunching(self):
        with self.transaction():
            self.store("wake-first")
            latest = self.store("wake-second")
            self.store("unauthorized-newer", pair=OTHER_PAIR)
            claim = mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW)
            self.assertEqual(claim["through_seq"], latest["mailbox_seq"])
            self.assertNotIn("body", claim)
            mailbox.store_message(self.connection, "wake-second", now="later")
            self.assertIsNone(mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW))
            self.assertFalse(mailbox.admit_wake(self.connection, "other", claim["claim_id"], "run", [PAIR], now=NOW))
            self.assertFalse(mailbox.admit_wake(self.connection, "recipient", "wrong-claim", "run", [PAIR], now=NOW))
            self.assertTrue(mailbox.admit_wake(self.connection, "recipient", claim["claim_id"], "run", [PAIR], now=NOW))
            self.assertFalse(mailbox.admit_wake(self.connection, "recipient", claim["claim_id"], "run", [PAIR], now=NOW))
            self.assertFalse(mailbox.release_wake(self.connection, "recipient", claim["claim_id"]))
            self.assertIsNone(mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW))
        rows = self.connection.execute("SELECT read_at,read_id FROM chat_mailbox_messages").fetchall()
        self.assertTrue(all(tuple(row) == (None, None) for row in rows))
        self.assertTrue(all(tuple(row) == ("stored", None) for row in
                            self.connection.execute("SELECT status,target_run_id FROM cross_chat_envelopes")))

    def test_wake_cutoff_excludes_arrivals_during_reserved_or_admitted_attempt(self):
        with self.transaction():
            first = self.store("wake-first")
            claim = mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW)
            later = self.store("wake-later")
            self.assertIsNone(mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW))
            self.assertTrue(mailbox.admit_wake(self.connection, "recipient", claim["claim_id"], "first-run", [PAIR], now=NOW))
            self.assertEqual(claim["through_seq"], first["mailbox_seq"])
            # Runtime calls again only once idle; the ledger does not interrupt
            # or infer whether the previous run is busy from message contents.
            next_claim = mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW)
            self.assertEqual(next_claim["through_seq"], later["mailbox_seq"])
            self.assertNotEqual(next_claim["claim_id"], claim["claim_id"])
            self.assertFalse(mailbox.release_wake(self.connection, "recipient", claim["claim_id"]))
            self.assertTrue(mailbox.admit_wake(self.connection, "recipient", next_claim["claim_id"], "second-run", [PAIR], now=NOW))
            self.assertIsNone(mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW))

    def test_wake_admission_rechecks_current_route_cancel_delete_and_read(self):
        for change in ("route", "cancel", "delete", "read"):
            target = "recipient-" + change
            with self.subTest(change=change), self.transaction():
                self.store(change, target=target)
                claim = mailbox.claim_wake(self.connection, target, [PAIR], now=NOW)
                pairs = [PAIR]
                if change == "route":
                    pairs = []
                elif change == "cancel":
                    mailbox.cancel_message(self.connection, change, now=NOW)
                elif change == "delete":
                    mailbox.exclude_message(self.connection, change, target_session_id=target, reason="deleted", now=NOW)
                else:
                    self.read(target_session_id=target)
                self.assertFalse(mailbox.admit_wake(self.connection, target, claim["claim_id"], "run", pairs, now=NOW))
                self.assertTrue(mailbox.release_wake(self.connection, target, claim["claim_id"]))
                self.assertIsNone(mailbox.claim_wake(self.connection, target, pairs, now=NOW))

    def test_stop_suppresses_current_unread_cutoff_but_preserves_future_mail_and_admitted_claim(self):
        with self.transaction():
            self.store("first")
            claim = mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW)
            other = self.store("other-route", pair=OTHER_PAIR)
            cutoff = mailbox.suppress_wake(self.connection, "recipient", now=NOW)
            self.assertEqual(cutoff, other["mailbox_seq"])
            self.assertFalse(mailbox.admit_wake(self.connection, "recipient", claim["claim_id"], "run", [PAIR], now=NOW))
            self.assertIsNone(mailbox.claim_wake(self.connection, "recipient", [PAIR, OTHER_PAIR], now=NOW))
            future = self.store("after-stop")
            next_claim = mailbox.claim_wake(self.connection, "recipient", [PAIR], now=NOW)
            self.assertEqual(next_claim["through_seq"], future["mailbox_seq"])
            self.assertTrue(mailbox.admit_wake(self.connection, "recipient", next_claim["claim_id"], "future-run", [PAIR], now=NOW))
            mailbox.suppress_wake(self.connection, "recipient", now=NOW)
            state = self.connection.execute("SELECT * FROM chat_mailbox_wakes").fetchone()
            self.assertEqual((state["state"], state["claim_id"], state["run_id"]),
                             ("admitted", next_claim["claim_id"], "future-run"))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM chat_mailbox_messages WHERE read_at IS NULL").fetchone()[0], 3)

    def test_wake_recovery_releases_only_unadmitted_and_transaction_rollback_is_safe(self):
        with self.transaction():
            self.store("reserved", target="reserved-target")
            self.store("admitted", target="admitted-target")
            reserved = mailbox.claim_wake(self.connection, "reserved-target", [PAIR], now=NOW)
            admitted = mailbox.claim_wake(self.connection, "admitted-target", [PAIR], now=NOW)
            self.assertTrue(mailbox.admit_wake(self.connection, "admitted-target", admitted["claim_id"], "run", [PAIR], now=NOW))
        self.connection.close()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        with self.transaction():
            self.assertEqual(mailbox.recover_wakes(self.connection), 1)
            self.assertEqual(mailbox.recover_wakes(self.connection), 0)
            self.assertIsNone(mailbox.claim_wake(self.connection, "admitted-target", [PAIR], now=NOW))
        self.connection.execute("BEGIN IMMEDIATE")
        retry = mailbox.claim_wake(self.connection, "reserved-target", [PAIR], now=NOW)
        self.assertNotEqual(retry["claim_id"], reserved["claim_id"])
        mailbox.admit_wake(self.connection, "reserved-target", retry["claim_id"], "retry-run", [PAIR], now=NOW)
        self.connection.rollback()
        with self.transaction():
            self.assertIsNotNone(mailbox.claim_wake(self.connection, "reserved-target", [PAIR], now=NOW))
        with self.assertRaises(RuntimeError):
            mailbox.recover_wakes(self.connection)


if __name__ == "__main__":
    unittest.main()
