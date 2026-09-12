"""Private, immutable snapshots for explicitly shared, read-only conversations.

This module does not discover sessions, read transcripts, authenticate requests,
or choose a public origin. The caller must project and review the user/assistant
text before calling ``create_share``. Only the creation result contains the raw
bearer token; reopening or listing the store cannot recover a share URL.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time
from typing import Any, Callable, Iterator


MAX_MESSAGE_TEXT_BYTES = 256 * 1024
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_RENDER_BYTES = 16 * 1024 * 1024
MAX_TITLE_CHARACTERS = 256
MAX_LIST_LIMIT = 100
MAX_UNIX_TIMESTAMP = 253402300799
DEFAULT_TITLE = "Shared conversation"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
SHARE_ID_PATTERN = re.compile(r"share_[a-f0-9]{32}\Z", re.ASCII)


class PublicChatShareValidationError(ValueError):
    """Input is not an already-projected, bounded public conversation."""


class PublicChatShareUnavailable(LookupError):
    """Use the same public 404 response for every unavailable share."""

    def __init__(self) -> None:
        super().__init__("Shared conversation is unavailable.")


def _timestamp(value: Any, label: str) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value <= MAX_UNIX_TIMESTAMP
        or not math.isfinite(value)
    ):
        raise PublicChatShareValidationError(f"{label} must be finite Unix seconds.")
    return value


def _utf8_size(value: str, label: str, limit: int) -> int:
    # Check character count first to avoid encoding a grossly oversized input.
    if len(value) > limit:
        raise PublicChatShareValidationError(f"{label} is too large.")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PublicChatShareValidationError(f"{label} must be valid UTF-8.") from exc
    if length > limit:
        raise PublicChatShareValidationError(f"{label} is too large.")
    return length


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PublicChatShareValidationError("session_id must be a nonempty string.")
    _utf8_size(value, "session_id", 1024)
    return value


def _snapshot(messages: Any, title: Any, created_at: Any) -> tuple[dict[str, Any], bytes]:
    if title is None:
        title = DEFAULT_TITLE
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARACTERS:
        raise PublicChatShareValidationError("title must contain 1 to 256 characters.")
    total_bytes = _utf8_size(title, "title", MAX_TITLE_CHARACTERS * 4)
    _timestamp(created_at, "created_at")
    if not isinstance(messages, list) or not messages:
        raise PublicChatShareValidationError("messages must contain at least one item.")
    serialized_bytes = len(json.dumps({"title": title, "created_at": created_at, "messages": []},
        ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "text", "timestamp"}:
            raise PublicChatShareValidationError("Messages may contain only role, text, and timestamp.")
        role = message.get("role")
        text = message.get("text")
        if role not in ("user", "assistant") or not isinstance(text, str):
            raise PublicChatShareValidationError("Each message needs a user/assistant role and text.")
        total_bytes += _utf8_size(text, "message text", MAX_MESSAGE_TEXT_BYTES)
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise PublicChatShareValidationError("Snapshot is too large.")
        item = {"role": role, "text": text}
        if "timestamp" in message:
            item["timestamp"] = _timestamp(message["timestamp"], "message timestamp")
        serialized_bytes += len(json.dumps(item, ensure_ascii=False, separators=(",", ":"),
            allow_nan=False).encode("utf-8")) + bool(projected)
        if serialized_bytes > MAX_SNAPSHOT_BYTES:
            raise PublicChatShareValidationError("Snapshot is too large.")
        projected.append(item)
    snapshot = {"title": title, "created_at": created_at, "messages": projected}
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise PublicChatShareValidationError("Snapshot is too large.")
    return snapshot, encoded


class PublicChatShareStore:
    """A dedicated owner-only directory containing a durable SQLite database.

    A fresh SQLite connection is used per operation, so instances can be shared
    between request threads. Snapshot rows and revocations are append-only.
    Revocation blocks subsequent lookups; it cannot retract bytes already sent
    by a request that read the snapshot before the revocation committed.
    """

    def __init__(self, storage_root: str | os.PathLike[str], *, now: Callable[[], float] = time.time):
        root = Path(storage_root)
        if not root.is_absolute():
            raise PublicChatShareValidationError("storage_root must be an explicit absolute directory.")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.storage_root = root
        self.database_path = root / "snapshots.sqlite3"
        self._now = now
        self._check_directory()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.database_path, flags, 0o600)
        except FileExistsError:
            # Inspect existing paths before opening them: a FIFO or device must
            # not be opened, and an existing unsafe file must not be chmodded.
            self._check_database_stat(self.database_path.lstat())
        else:
            try:
                self._check_database_stat(os.fstat(descriptor))
            finally:
                os.close(descriptor)
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)

    @staticmethod
    def _create_tables(connection, suffix=""):
        connection.execute(
                f"""CREATE TABLE IF NOT EXISTS public_chat_shares{suffix} (
                    share_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    token_hash BLOB NOT NULL UNIQUE CHECK(length(token_hash) = 32),
                    title TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL,
                    message_count INTEGER NOT NULL CHECK(message_count >= 1),
                    snapshot_json BLOB NOT NULL CHECK(length(snapshot_json) <= 2097152),
                    snapshot_sha256 BLOB NOT NULL CHECK(length(snapshot_sha256) = 32)
                )"""
            )
        connection.execute(
                f"""CREATE TABLE IF NOT EXISTS public_chat_share_revocations{suffix} (
                    share_id TEXT PRIMARY KEY REFERENCES public_chat_shares{suffix}(share_id),
                    revoked_at REAL NOT NULL
                )"""
            )

    @classmethod
    def _ensure_schema(cls, connection):
        """Authenticated-write-only, atomic preservation of immutable v1 rows."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise OSError("Unsupported public chat share database version.")
        if version == 1:
            cls._create_tables(connection, "_v2")
            connection.execute("INSERT INTO public_chat_shares_v2 SELECT * FROM public_chat_shares")
            connection.execute("INSERT INTO public_chat_share_revocations_v2 SELECT * FROM public_chat_share_revocations")
            connection.execute("DROP TABLE public_chat_share_revocations")
            connection.execute("DROP TABLE public_chat_shares")
            connection.execute("ALTER TABLE public_chat_shares_v2 RENAME TO public_chat_shares")
            connection.execute("ALTER TABLE public_chat_share_revocations_v2 RENAME TO public_chat_share_revocations")
        else:
            cls._create_tables(connection)
        connection.execute(
                "CREATE INDEX IF NOT EXISTS public_chat_shares_session ON public_chat_shares(session_id, created_at DESC)"
            )
        for table in ("public_chat_shares", "public_chat_share_revocations"):
            for operation in ("UPDATE", "DELETE"):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} "
                    f"BEFORE {operation} ON {table} BEGIN "
                    "SELECT RAISE(ABORT, 'Public chat shares are immutable'); END"
                )
        connection.execute("PRAGMA user_version = 2")

    @classmethod
    def open_existing(
        cls, storage_root: str | os.PathLike[str], *, now: Callable[[], float] = time.time,
    ) -> "PublicChatShareStore":
        """Open an initialized store without creating paths or writing schema.

        Used by cold public viewers after restart. A damaged/missing/unknown
        schema is not repaired by an anonymous request; only authenticated
        creation may initialize a new store.
        """
        root = Path(storage_root)
        if not root.is_absolute():
            raise PublicChatShareValidationError("storage_root must be an explicit absolute directory.")
        instance = cls.__new__(cls)
        instance.storage_root = root
        instance.database_path = root / "snapshots.sqlite3"
        instance._now = now
        with instance._connection() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (1, 2):
                raise OSError("Unsupported public chat share database version.")
        return instance

    def _check_directory(self) -> None:
        info = self.storage_root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise OSError("Public chat share storage must be a private, owned, non-symlink directory.")

    @staticmethod
    def _check_database_stat(info: os.stat_result) -> None:
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o077
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
        ):
            raise OSError("Public chat share database must be a private, owned, single-link regular file.")

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        self._check_directory()
        self._check_database_stat(self.database_path.lstat())
        # The checked database must already exist; SQLite must not create an
        # alternate path. The private directory also protects rollback journals.
        mode = "rw" if write else "ro"
        connection = sqlite3.connect(f"{self.database_path.as_uri()}?mode={mode}", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            if write:
                connection.execute("PRAGMA synchronous = FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _metadata(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in (
            "share_id", "session_id", "title", "created_at", "expires_at", "revoked_at", "message_count"
        )}

    def create_share(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        *,
        title: str | None = None,
        expires_at: int | float | None = None,
    ) -> dict[str, Any]:
        session_id = _session_id(session_id)
        created_at = _timestamp(self._now(), "current time")
        if expires_at is not None and _timestamp(expires_at, "expires_at") <= created_at:
            raise PublicChatShareValidationError("expires_at must be in the future.")
        snapshot, encoded = _snapshot(messages, title, created_at)
        token = secrets.token_urlsafe(32)
        share_id = "share_" + secrets.token_hex(16)
        token_hash = hashlib.sha256(token.encode("ascii")).digest()
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            # An anonymous view may already have cached a read-only v1 store.
            self._ensure_schema(connection)
            connection.execute(
                """INSERT INTO public_chat_shares
                    (share_id, session_id, token_hash, title, created_at, expires_at,
                     message_count, snapshot_json, snapshot_sha256)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (share_id, session_id, token_hash, snapshot["title"], created_at, expires_at,
                 len(snapshot["messages"]), encoded, hashlib.sha256(encoded).digest()),
            )
        return {
            "share_id": share_id, "session_id": session_id, "title": snapshot["title"],
            "created_at": created_at, "expires_at": expires_at, "revoked_at": None,
            "message_count": len(snapshot["messages"]), "token": token,
        }

    def list_shares(self, session_id: str, *, limit: int = MAX_LIST_LIMIT) -> list[dict[str, Any]]:
        session_id = _session_id(session_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise PublicChatShareValidationError("limit must be an integer between 1 and 100.")
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT s.share_id, s.session_id, s.title, s.created_at, s.expires_at,
                          s.message_count, r.revoked_at
                   FROM public_chat_shares AS s
                   LEFT JOIN public_chat_share_revocations AS r USING (share_id)
                   WHERE s.session_id = ? ORDER BY s.created_at DESC, s.share_id DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [self._metadata(row) for row in rows]

    def revoke_share(self, share_id: str, *, session_id: str) -> bool:
        session_id = _session_id(session_id)
        if not isinstance(share_id, str) or SHARE_ID_PATTERN.fullmatch(share_id) is None:
            return False
        revoked_at = _timestamp(self._now(), "current time")
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM public_chat_shares WHERE share_id = ? AND session_id = ?",
                (share_id, session_id),
            ).fetchone()
            if exists is None:
                return False
            connection.execute(
                "INSERT OR IGNORE INTO public_chat_share_revocations (share_id, revoked_at) VALUES (?, ?)",
                (share_id, revoked_at),
            )
        return True

    def get_snapshot(self, token: str) -> dict[str, Any]:
        # Invalid input is rejected before storage I/O, without echoing a token.
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            raise PublicChatShareUnavailable()
        token_hash = hashlib.sha256(token.encode("ascii")).digest()
        current_time = _timestamp(self._now(), "current time")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT s.snapshot_json, s.snapshot_sha256 FROM public_chat_shares AS s
                   WHERE s.token_hash = ? AND (s.expires_at IS NULL OR s.expires_at > ?)
                   AND NOT EXISTS (SELECT 1 FROM public_chat_share_revocations AS r
                                   WHERE r.share_id = s.share_id)""",
                (token_hash, current_time),
            ).fetchone()
        if row is None:
            raise PublicChatShareUnavailable()
        encoded = row["snapshot_json"]
        digest = row["snapshot_sha256"]
        if (
            not isinstance(encoded, bytes) or len(encoded) > MAX_SNAPSHOT_BYTES
            or not isinstance(digest, bytes)
            or not hmac.compare_digest(hashlib.sha256(encoded).digest(), digest)
        ):
            raise PublicChatShareUnavailable()
        try:
            decoded = json.loads(encoded)
            if not isinstance(decoded, dict) or set(decoded) != {"title", "created_at", "messages"}:
                raise PublicChatShareUnavailable()
            snapshot, _ = _snapshot(decoded["messages"], decoded["title"], decoded["created_at"])
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise PublicChatShareUnavailable() from exc
        return snapshot


_STYLE = """html{color-scheme:light dark}body{margin:0;background:#f6f7f9;color:#20242b;font:16px/1.65 system-ui,-apple-system,sans-serif}main{max-width:800px;margin:0 auto;padding:40px 24px}h1{font-size:1.8rem;line-height:1.25;overflow-wrap:anywhere}header{margin-bottom:32px}.note,time{color:#626a76;font-size:.85rem}.message{border:1px solid #dde1e7;border-radius:12px;padding:20px 24px;margin:20px 0;background:#fff}.message h2{font-size:.9rem;margin:0 0 12px}.text{white-space:pre-wrap;overflow-wrap:anywhere;tab-size:4}.user{border-left:4px solid #6576c7}footer{margin-top:32px}@media(prefers-color-scheme:dark){body{background:#14161a;color:#e5e8ee}.message{background:#1e2127;border-color:#373d48}.user{border-left-color:#929fea}.note,time{color:#a7afbc}}"""
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode("utf-8")).digest()).decode("ascii")


def public_chat_share_headers() -> dict[str, str]:
    """Use these headers for both successful viewer pages and unavailable pages."""
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'none'; "
            f"style-src 'sha256-{_STYLE_HASH}'; "
            "img-src 'none'; connect-src 'none'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'; sandbox"
        ),
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    }


def render_public_chat_html(snapshot: dict[str, Any]) -> bytes:
    """Render escaped plaintext only: no markdown HTML, links, tools, or actions."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"title", "created_at", "messages"}:
        raise PublicChatShareValidationError("Invalid public snapshot fields.")
    snapshot, _ = _snapshot(snapshot["messages"], snapshot["title"], snapshot["created_at"])
    title = html.escape(snapshot["title"], quote=True)
    created = datetime.fromtimestamp(snapshot["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="robots" content="noindex,nofollow,noarchive">',
        f"<title>{title}</title><style>{_STYLE}</style></head><body><main>",
        f"<header><h1>{title}</h1><p class=\"note\">Read-only conversation snapshot · {created}</p></header>",
    ]
    for message in snapshot["messages"]:
        role = message["role"]
        label = "User" if role == "user" else "Assistant"
        parts.append(f'<section class="message {role}"><h2>{label}</h2>')
        if "timestamp" in message:
            stamp = datetime.fromtimestamp(message["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            parts.append(f"<time>{stamp}</time>")
        parts.append(f'<div class="text">{html.escape(message["text"], quote=True)}</div></section>')
    parts.append('<footer class="note">This shared snapshot cannot access or continue the original conversation.</footer></main></body></html>')
    rendered = "".join(parts).encode("utf-8")
    if len(rendered) > MAX_RENDER_BYTES:
        raise PublicChatShareValidationError("Rendered snapshot is too large.")
    return rendered
