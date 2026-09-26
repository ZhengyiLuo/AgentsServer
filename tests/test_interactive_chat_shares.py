"""Synthetic invitation, cookie, upload and submission ledger tests."""
import concurrent.futures
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from interactive_chat_shares import InteractiveChatShareStore, Unavailable, Conflict, token_hash


class InteractiveShareStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="interactive-share-store-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "shares"
        self.now = 1000
        self.store = InteractiveChatShareStore(self.root, now=lambda: self.now)
        self.share = self.store.create_share("chat-one", expires_at=2000)

    def redeem(self, share=None):
        share = share or self.share
        return self.store.redeem(share["id"], share["invitation_token"])

    def test_shared_token_admits_multiple_browsers_and_persists_hashes_only(self):
        def attempt(_):
            try:
                return self.redeem()
            except Unavailable:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tokens = [token for token in pool.map(attempt, range(2)) if token]
        self.assertEqual(len(tokens), 2)
        self.assertEqual(tokens[0], self.share["invitation_token"])
        self.assertEqual(self.store.authenticate(self.share["id"], tokens[0])["session_id"], "chat-one")
        self.assertEqual(self.store.authenticate(self.share["id"], tokens[1])["session_id"], "chat-one")
        data = self.store.database_path.read_bytes()
        for token in (tokens[0], self.share["invitation_token"]):
            self.assertNotIn(token.encode(), data)
        listed = self.store.list_shares("chat-one")
        self.assertNotIn("invitation_token", listed[0])
        self.assertNotIn("session_id", listed[0])

    def test_public_origin_is_durable_and_does_not_expose_capabilities(self):
        origin = "http://192.0.2.42:8080"
        share = self.store.create_share("chat-one", public_origin=origin)
        reopened = InteractiveChatShareStore.open_existing(self.root, now=lambda: self.now)
        self.assertEqual(reopened.share_origin(share["id"]), origin)
        self.assertEqual(reopened.share_origin(self.share["id"]), "")
        listed = reopened.list_shares("chat-one")
        for row in listed:
            self.assertNotIn("invitation_token", row)
            self.assertNotIn("browser_hash", row)
        with self.assertRaises(Unavailable):
            reopened.share_origin("interactive_" + "0" * 32)

    def test_legacy_origin_migration_is_admin_only_and_preserves_active_and_revoked_grants(self):
        active_token = self.redeem()
        revoked = self.store.create_share("chat-one")
        revoked_token = self.redeem(revoked)
        self.store.revoke_share(revoked["id"], session_id="chat-one")
        with self.store._connection(write=True) as db:
            db.execute("ALTER TABLE interactive_shares DROP COLUMN public_origin")
        before = self.store.database_path.read_bytes()
        with mock.patch("public_chat_shares.sqlite3.connect", wraps=sqlite3.connect) as connect:
            legacy = InteractiveChatShareStore.open_existing(self.root, now=lambda: self.now)
            self.assertEqual(legacy.share_origin(self.share["id"]), "")
            self.assertEqual(legacy.authenticate(self.share["id"], active_token)["session_id"], "chat-one")
            with self.assertRaises(Unavailable):
                legacy.authenticate(revoked["id"], revoked_token)
        self.assertTrue(all(call.args[0].endswith("?mode=ro") for call in connect.call_args_list))
        self.assertEqual(self.store.database_path.read_bytes(), before)
        upgraded = InteractiveChatShareStore(self.root, now=lambda: self.now)
        self.assertEqual(upgraded.share_origin(self.share["id"]), "")
        self.assertEqual(upgraded.authenticate(self.share["id"], active_token)["session_id"], "chat-one")
        with self.assertRaises(Unavailable):
            upgraded.authenticate(revoked["id"], revoked_token)
        fresh = upgraded.create_share("chat-one", public_origin="https://share.example.test")
        self.assertEqual(upgraded.share_origin(fresh["id"]), "https://share.example.test")
        with upgraded._connection() as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT count(*) FROM interactive_shares").fetchone()[0], 3)

    def test_expiry_revocation_and_exact_chat_management(self):
        token = self.redeem()
        self.assertFalse(self.store.revoke_share(self.share["id"], session_id="other-chat"))
        self.now = 2000
        with self.assertRaises(Unavailable):
            self.store.authenticate(self.share["id"], token)
        self.now = 1001
        self.assertTrue(self.store.revoke_share(self.share["id"], session_id="chat-one"))
        with self.assertRaises(Unavailable):
            self.store.authenticate(self.share["id"], token)

    def test_reusable_token_preserves_legacy_browser_cookie_and_revokes_every_browser(self):
        legacy_cookie = "L" * 43
        with self.store._connection(write=True) as db:
            db.execute("UPDATE interactive_shares SET browser_hash=?,redeemed_at=? WHERE id=?",
                (token_hash(legacy_cookie), self.now, self.share["id"]))
        current_cookie = self.redeem()
        self.assertEqual(self.store.authenticate(self.share["id"], legacy_cookie)["session_id"], "chat-one")
        self.assertEqual(self.store.authenticate(self.share["id"], current_cookie)["session_id"], "chat-one")
        other = self.store.create_share("chat-one")
        with self.assertRaises(Unavailable):
            self.store.redeem(other["id"], self.share["invitation_token"])
        self.store.revoke_share(self.share["id"], session_id="chat-one")
        for token in (legacy_cookie, current_cookie):
            with self.assertRaises(Unavailable):
                self.store.authenticate(self.share["id"], token)
        with self.assertRaises(Unavailable):
            self.redeem()

    def test_uploads_belong_to_exact_share_and_unknown_writes_remain_charged(self):
        token = self.redeem()
        upload = self.store.reserve_upload(self.share["id"], token, name="note.txt", media_type="text/plain", byte_size=5)
        self.store.complete_upload(self.share["id"], token, upload, "private-file-reference")
        self.assertEqual(self.store.upload_refs(self.share["id"], token, [upload]), ["private-file-reference"])
        other = self.store.create_share("chat-one")
        with self.assertRaises(Unavailable):
            self.store.upload_refs(other["id"], self.redeem(other), [upload])
        # Incomplete reservations stay durable across restart; no refund merely
        # because the save callback's outcome could not be observed.
        pending = self.store.reserve_upload(self.share["id"], token, name="pending.txt", media_type="text/plain", byte_size=7)
        reopened = InteractiveChatShareStore.open_existing(self.root, now=lambda: self.now)
        with reopened._connection() as db:
            row = db.execute("SELECT byte_size,private_ref FROM interactive_uploads WHERE id=?", (pending,)).fetchone()
        self.assertEqual(tuple(row), (7, None))
        with self.assertRaises(Unavailable):
            reopened.upload_refs(self.share["id"], token, [pending])

    def test_stable_request_replays_only_exact_accepted_receipt_across_restart(self):
        token = self.redeem()
        self.assertIsNone(self.store.reserve_submission(self.share["id"], token, "request-one", "Prompt", []))
        reopened = InteractiveChatShareStore.open_existing(self.root, now=lambda: self.now)
        with self.assertRaisesRegex(Conflict, "indeterminate"):
            reopened.reserve_submission(self.share["id"], token, "request-one", "Prompt", [])
        receipt = {"accepted": True, "queued": True, "request_id": "request-one"}
        reopened.accept_submission(self.share["id"], "request-one", receipt)
        self.assertEqual(self.store.reserve_submission(self.share["id"], token, "request-one", "Prompt", []), receipt)
        with self.assertRaisesRegex(Conflict, "different"):
            reopened.reserve_submission(self.share["id"], token, "request-one", "Prompt", [], operation="control:turn.stop")
        with self.assertRaisesRegex(Conflict, "different"):
            reopened.reserve_submission(self.share["id"], token, "request-one", "Changed", [])

    def test_normal_uploads_have_no_separate_share_size_lifetime_or_count_cap(self):
        token = self.redeem()
        uploads = []
        for index in range(9):
            upload = self.store.reserve_upload(self.share["id"], token,
                name=f"file-{index}.bin", media_type="application/octet-stream", byte_size=9 * 1024 * 1024)
            self.store.complete_upload(self.share["id"], token, upload, f"file-{index}")
            uploads.append(upload)
        self.assertEqual(self.store.upload_refs(self.share["id"], token, uploads), [f"file-{index}" for index in range(9)])
        self.store.reserve_upload(self.share["id"], token, name="empty.txt", media_type="text/plain", byte_size=0)


if __name__ == "__main__":
    unittest.main()
