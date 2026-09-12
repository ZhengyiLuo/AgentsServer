"""Synthetic invitation, cookie, upload and submission ledger tests."""
import concurrent.futures
from pathlib import Path
import tempfile
import unittest

from interactive_chat_shares import InteractiveChatShareStore, Unavailable, Conflict, MAX_SHARE_UPLOAD_BYTES


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

    def test_atomic_one_use_distinct_browser_capability_hashes_only(self):
        def attempt(_):
            try:
                return self.redeem()
            except Unavailable:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tokens = [token for token in pool.map(attempt, range(2)) if token]
        self.assertEqual(len(tokens), 1)
        self.assertNotEqual(tokens[0], self.share["invitation_token"])
        self.assertEqual(self.store.authenticate(self.share["id"], tokens[0])["session_id"], "chat-one")
        with self.assertRaises(Unavailable):
            self.store.authenticate(self.share["id"], self.share["invitation_token"])
        data = self.store.database_path.read_bytes()
        for token in (tokens[0], self.share["invitation_token"]):
            self.assertNotIn(token.encode(), data)
        listed = self.store.list_shares("chat-one")
        self.assertNotIn("invitation_token", listed[0])
        self.assertNotIn("session_id", listed[0])

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


if __name__ == "__main__":
    unittest.main()
