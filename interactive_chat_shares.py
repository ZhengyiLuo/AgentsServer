"""Durable single-use invitations and browser capabilities for one exact chat.

This ledger grants no provider or native API authority. Raw invitation/browser
tokens are returned once and never persisted. Upload references stay private.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time

from public_chat_shares import (
    PublicChatShareStore, PublicChatShareUnavailable, PublicChatShareValidationError,
    TOKEN_PATTERN, _timestamp, _utf8_size,
)

Unavailable = PublicChatShareUnavailable
ValidationError = PublicChatShareValidationError
SHARE_ID = re.compile(r"interactive_[a-f0-9]{32}\Z")
UPLOAD_ID = re.compile(r"upload_[a-f0-9]{32}\Z")
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_SHARE_UPLOAD_BYTES = 64 * 1024 * 1024
MAX_PROMPT_BYTES = 64 * 1024


class Conflict(ValueError):
    """A stable request ID already names different or indeterminate work."""


def token_hash(token):
    if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
        raise Unavailable()
    return hashlib.sha256(token.encode("ascii")).digest()


def csrf_token(browser_token):
    token_hash(browser_token)
    return hmac.new(browser_token.encode("ascii"), b"interactive-chat-csrf-v1", hashlib.sha256).hexdigest()


class InteractiveChatShareStore(PublicChatShareStore):
    """Reuse the existing owner-only SQLite connection checks, not snapshot data."""

    def __init__(self, storage_root, *, now=time.time):
        self.storage_root = Path(storage_root)
        if not self.storage_root.is_absolute():
            raise ValidationError("Share storage must be an absolute private directory")
        self.storage_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database_path = self.storage_root / "interactive.sqlite3"
        self._now = now
        self._check_directory()
        try:
            fd = os.open(self.database_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        except FileExistsError:
            self._check_database_stat(self.database_path.lstat())
        else:
            os.close(fd)
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise OSError("Unsupported interactive share database version")
            db.execute("""CREATE TABLE IF NOT EXISTS interactive_shares(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL,title TEXT NOT NULL,
                created_at REAL NOT NULL,expires_at REAL,revoked_at REAL,redeemed_at REAL,
                invite_hash BLOB NOT NULL UNIQUE,browser_hash BLOB UNIQUE)""")
            db.execute("CREATE INDEX IF NOT EXISTS interactive_share_session ON interactive_shares(session_id,created_at DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS interactive_uploads(
                id TEXT PRIMARY KEY,share_id TEXT NOT NULL REFERENCES interactive_shares(id),
                name TEXT NOT NULL,media_type TEXT NOT NULL,byte_size INTEGER NOT NULL,
                private_ref TEXT,created_at REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS interactive_upload_share ON interactive_uploads(share_id)")
            db.execute("""CREATE TABLE IF NOT EXISTS interactive_submissions(
                share_id TEXT NOT NULL REFERENCES interactive_shares(id),request_id TEXT NOT NULL,
                fingerprint BLOB NOT NULL,receipt_json TEXT,created_at REAL NOT NULL,
                PRIMARY KEY(share_id,request_id))""")
            db.execute("PRAGMA user_version=1")

    @classmethod
    def open_existing(cls, storage_root, *, now=time.time):
        instance = cls.__new__(cls)
        instance.storage_root = Path(storage_root)
        instance.database_path = instance.storage_root / "interactive.sqlite3"
        instance._now = now
        with instance._connection() as db:
            if db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise OSError("Unsupported interactive share database version")
        return instance

    @staticmethod
    def metadata(row):
        return {key: row[key] for key in ("id", "title", "created_at", "expires_at", "revoked_at", "redeemed_at")}

    def create_share(self, session_id, *, title=None, expires_at=None):
        if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id) is None:
            raise ValidationError("Invalid chat")
        title = "Shared conversation" if title is None else title
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            raise ValidationError("Title must contain 1 to 256 characters")
        _utf8_size(title, "Title", 1024)
        now = _timestamp(self._now(), "current time")
        if expires_at is not None and _timestamp(expires_at, "expires_at") <= now:
            raise ValidationError("Expiry must be in the future")
        invite = secrets.token_urlsafe(32)
        share_id = "interactive_" + secrets.token_hex(16)
        with self._connection(write=True) as db:
            db.execute("INSERT INTO interactive_shares(id,session_id,title,created_at,expires_at,invite_hash) VALUES(?,?,?,?,?,?)",
                (share_id, session_id, title, now, expires_at, token_hash(invite)))
            row = db.execute("SELECT * FROM interactive_shares WHERE id=?", (share_id,)).fetchone()
        return {**self.metadata(row), "invitation_token": invite}

    def list_shares(self, session_id):
        with self._connection() as db:
            return [self.metadata(row) for row in db.execute(
                "SELECT * FROM interactive_shares WHERE session_id=? ORDER BY created_at DESC,id DESC LIMIT 100", (session_id,))]

    def revoke_share(self, share_id, *, session_id):
        with self._connection(write=True) as db:
            return db.execute("UPDATE interactive_shares SET revoked_at=COALESCE(revoked_at,?) WHERE id=? AND session_id=?",
                (self._now(), share_id, session_id)).rowcount == 1

    def redeem(self, share_id, invite):
        digest = token_hash(invite)
        browser_token = secrets.token_urlsafe(32)
        now = self._now()
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("""UPDATE interactive_shares SET browser_hash=?,redeemed_at=?
                WHERE id=? AND invite_hash=? AND redeemed_at IS NULL AND revoked_at IS NULL
                AND (expires_at IS NULL OR expires_at>?)""", (token_hash(browser_token), now, share_id, digest, now))
            if updated.rowcount != 1:
                raise Unavailable()
        return browser_token

    def _authorized(self, db, share_id, browser_token):
        digest = token_hash(browser_token)
        row = db.execute("""SELECT * FROM interactive_shares WHERE id=? AND browser_hash=?
            AND redeemed_at IS NOT NULL AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at>?)""",
            (share_id, digest, self._now())).fetchone()
        if row is None:
            raise Unavailable()
        return row

    def authenticate(self, share_id, browser_token):
        with self._connection() as db:
            row = self._authorized(db, share_id, browser_token)
            return {**self.metadata(row), "session_id": row["session_id"]}

    def reserve_upload(self, share_id, browser_token, *, name, media_type, byte_size):
        if (not isinstance(name, str) or not 1 <= len(name) <= 160 or name in {".", ".."}
                or any(ord(c) < 32 or c in "/\\" for c in name)):
            raise ValidationError("Use a simple filename without directories")
        _utf8_size(name, "Filename", 640)
        if not isinstance(media_type, str) or re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", media_type) is None:
            raise ValidationError("Invalid upload media type")
        if type(byte_size) is not int or not 1 <= byte_size <= MAX_UPLOAD_BYTES:
            raise ValidationError("Files must contain 1 byte to 8 MiB")
        upload_id = "upload_" + secrets.token_hex(16)
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorized(db, share_id, browser_token)
            used = db.execute("SELECT COALESCE(SUM(byte_size),0) FROM interactive_uploads WHERE share_id=?", (share_id,)).fetchone()[0]
            if used + byte_size > MAX_SHARE_UPLOAD_BYTES:
                raise ValidationError("This share has reached its 64 MiB upload storage bound")
            db.execute("INSERT INTO interactive_uploads VALUES(?,?,?,?,?,?,?)",
                (upload_id, share_id, name, media_type, byte_size, None, self._now()))
        return upload_id

    def complete_upload(self, share_id, browser_token, upload_id, private_ref):
        if not isinstance(private_ref, str) or not private_ref or len(private_ref) > 1024:
            raise ValidationError("Upload was not accepted")
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorized(db, share_id, browser_token)
            if db.execute("UPDATE interactive_uploads SET private_ref=? WHERE id=? AND share_id=? AND private_ref IS NULL",
                (private_ref, upload_id, share_id)).rowcount != 1:
                raise Unavailable()

    def abandon_upload(self, share_id, upload_id):
        with self._connection(write=True) as db:
            db.execute("DELETE FROM interactive_uploads WHERE id=? AND share_id=? AND private_ref IS NULL", (upload_id, share_id))

    def upload_refs(self, share_id, browser_token, upload_ids):
        if (not isinstance(upload_ids, list) or len(upload_ids) > 4 or len(set(str(i) for i in upload_ids)) != len(upload_ids)
                or any(not isinstance(i, str) or UPLOAD_ID.fullmatch(i) is None for i in upload_ids)):
            raise ValidationError("Select at most four distinct uploads from this share")
        with self._connection() as db:
            self._authorized(db, share_id, browser_token)
            refs = []
            for upload_id in upload_ids:
                row = db.execute("SELECT private_ref FROM interactive_uploads WHERE id=? AND share_id=? AND private_ref IS NOT NULL",
                    (upload_id, share_id)).fetchone()
                if row is None:
                    raise Unavailable()
                refs.append(row["private_ref"])
        return refs

    def reserve_submission(self, share_id, browser_token, request_id, prompt, refs, *, operation="prompt"):
        fingerprint = hashlib.sha256(json.dumps([operation, prompt, refs], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).digest()
        with self._connection(write=True) as db:
            db.execute("BEGIN IMMEDIATE")
            self._authorized(db, share_id, browser_token)
            row = db.execute("SELECT fingerprint,receipt_json FROM interactive_submissions WHERE share_id=? AND request_id=?",
                (share_id, request_id)).fetchone()
            if row is not None:
                if not hmac.compare_digest(bytes(row["fingerprint"]), fingerprint):
                    raise Conflict("This request ID was already used for a different message")
                if row["receipt_json"] is None:
                    raise Conflict("Message acceptance is indeterminate. Inspect the chat before sending another message; this request will not be retried")
                return json.loads(row["receipt_json"])
            db.execute("INSERT INTO interactive_submissions VALUES(?,?,?,?,?)",
                (share_id, request_id, fingerprint, None, self._now()))
        return None

    def accept_submission(self, share_id, request_id, receipt):
        # Persist an acceptance fact even if expiry occurred during the callback.
        # A failure leaves the existing pending receipt indeterminate, not retryable.
        encoded = json.dumps(receipt, separators=(",", ":"), allow_nan=False)
        with self._connection(write=True) as db:
            if db.execute("UPDATE interactive_submissions SET receipt_json=? WHERE share_id=? AND request_id=? AND receipt_json IS NULL",
                (encoded, share_id, request_id)).rowcount != 1:
                raise Conflict("Message receipt could not be recorded")
