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

from shared_chat_videos import normalize_shared_chat_videos


# Pagination targets, not quotas: one complete message may exceed a page target.
TARGET_SNAPSHOT_PAGE_BYTES = 2 * 1024 * 1024
TARGET_SNAPSHOT_PAGE_MESSAGES = 100
MAX_TITLE_CHARACTERS = 256
MAX_LIST_LIMIT = 100
MAX_UNIX_TIMESTAMP = 253402300799
DEFAULT_TITLE = "Shared conversation"
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)
SHARE_ID_PATTERN = re.compile(r"share_[a-f0-9]{32}\Z", re.ASCII)


class PublicChatShareValidationError(ValueError):
    """Input is not an already-projected public conversation."""


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


def _utf8_size(value: str, label: str, limit: int | None = None) -> int:
    # Check character count first to avoid encoding a grossly oversized input.
    if limit is not None and len(value) > limit:
        raise PublicChatShareValidationError(f"{label} is too large.")
    try:
        length = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PublicChatShareValidationError(f"{label} must be valid UTF-8.") from exc
    if limit is not None and length > limit:
        raise PublicChatShareValidationError(f"{label} is too large.")
    return length


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise PublicChatShareValidationError("session_id must be a nonempty string.")
    _utf8_size(value, "session_id", 1024)
    return value


def _projected_message(message: Any) -> dict[str, Any]:
    if not isinstance(message, dict) or set(message) - {"role", "text", "timestamp", "videos"}:
        raise PublicChatShareValidationError("Messages may contain only role, text, timestamp, and videos.")
    role = message.get("role")
    text = message.get("text")
    if role not in ("user", "assistant") or not isinstance(text, str):
        raise PublicChatShareValidationError("Each message needs a user/assistant role and text.")
    _utf8_size(text, "message text")
    item = {"role": role, "text": text}
    if "videos" in message:
        try:
            videos = normalize_shared_chat_videos(message["videos"])
        except ValueError as exc:
            raise PublicChatShareValidationError("Invalid shared video metadata.") from exc
        if videos:
            item["videos"] = videos
    if "timestamp" in message:
        item["timestamp"] = _timestamp(message["timestamp"], "message timestamp")
    return item


def _snapshot(messages: Any, title: Any, created_at: Any) -> tuple[dict[str, Any], bytes]:
    if title is None:
        title = DEFAULT_TITLE
    if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE_CHARACTERS:
        raise PublicChatShareValidationError("title must contain 1 to 256 characters.")
    _utf8_size(title, "title", MAX_TITLE_CHARACTERS * 4)
    _timestamp(created_at, "created_at")
    if not isinstance(messages, list) or not messages:
        raise PublicChatShareValidationError("messages must contain at least one item.")
    projected = [_projected_message(message) for message in messages]
    snapshot = {"title": title, "created_at": created_at, "messages": projected}
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
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
                    snapshot_json BLOB NOT NULL,
                    snapshot_sha256 BLOB NOT NULL CHECK(length(snapshot_sha256) = 32)
                )"""
            )
        connection.execute(
                f"""CREATE TABLE IF NOT EXISTS public_chat_share_revocations{suffix} (
                    share_id TEXT PRIMARY KEY REFERENCES public_chat_shares{suffix}(share_id),
                    revoked_at REAL NOT NULL
                )"""
            )
        connection.execute(f"""CREATE TABLE IF NOT EXISTS public_chat_share_pages{suffix} (
            share_id TEXT NOT NULL REFERENCES public_chat_shares{suffix}(share_id) DEFERRABLE INITIALLY DEFERRED,
            page_index INTEGER NOT NULL CHECK(page_index >= 1),
            snapshot_json BLOB NOT NULL,
            snapshot_sha256 BLOB NOT NULL CHECK(length(snapshot_sha256) = 32),
            PRIMARY KEY(share_id, page_index))""")

    @classmethod
    def _ensure_schema(cls, connection):
        """Authenticated-write-only, atomic preservation of existing snapshots."""
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2, 3):
            raise OSError("Unsupported public chat share database version.")
        # Only the old SQL size constraints change; columns and persisted JSON
        # are identical. Keep schema version 3 so a previous server can reopen
        # its existing shares after rollback instead of refusing the database.
        bounded_schema = any(re.search(r"CHECK\s*\(\s*length\s*\(\s*snapshot_json\s*\)", row[0] or "", re.IGNORECASE)
            for row in connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name IN ('public_chat_shares','public_chat_share_pages')"))
        if version == 1 or bounded_schema:
            cls._create_tables(connection, "_expanded")
            connection.execute("INSERT INTO public_chat_shares_expanded SELECT * FROM public_chat_shares")
            connection.execute("INSERT INTO public_chat_share_revocations_expanded SELECT * FROM public_chat_share_revocations")
            has_pages = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_chat_share_pages'").fetchone()
            if has_pages:
                connection.execute("INSERT INTO public_chat_share_pages_expanded SELECT * FROM public_chat_share_pages")
                connection.execute("DROP TABLE public_chat_share_pages")
            connection.execute("DROP TABLE public_chat_share_revocations")
            connection.execute("DROP TABLE public_chat_shares")
            connection.execute("ALTER TABLE public_chat_shares_expanded RENAME TO public_chat_shares")
            connection.execute("ALTER TABLE public_chat_share_revocations_expanded RENAME TO public_chat_share_revocations")
            connection.execute("ALTER TABLE public_chat_share_pages_expanded RENAME TO public_chat_share_pages")
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
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS public_chat_share_pages_no_{operation.lower()} "
                f"BEFORE {operation} ON public_chat_share_pages BEGIN "
                "SELECT RAISE(ABORT, 'Public chat shares are immutable'); END")
        connection.execute("PRAGMA user_version = 3")

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
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (1, 2, 3):
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

    def create_streamed_share(self, session_id: str, load_messages: Callable, *, title=None, expires_at=None) -> dict[str, Any]:
        """Capture complete messages in paginated snapshots atomically.

        The loader must emit projected messages synchronously and raise before
        returning if a reviewed digest changed. No whole-chat list/JSON is kept.
        Existing snapshots retain their original first-page representation.
        A message larger than the page target is stored intact on its own page.
        """
        session_id = _session_id(session_id)
        created_at = _timestamp(self._now(), "current time")
        if expires_at is not None and _timestamp(expires_at, "expires_at") <= created_at:
            raise PublicChatShareValidationError("expires_at must be in the future.")
        # Validate metadata before starting the transcript scan.
        seed, _ = _snapshot([{"role": "user", "text": ""}], title, created_at)
        title = seed["title"]
        token, share_id = secrets.token_urlsafe(32), "share_" + secrets.token_hex(16)
        first_page = None
        page: list[dict[str, Any]] = []
        page_bytes = 0
        page_index = 0
        total = 0
        overhead = len(json.dumps({"title": title, "created_at": created_at, "messages": []},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
        with self._connection(write=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_schema(connection)

            def flush() -> None:
                nonlocal first_page, page_index, page_bytes
                if not page:
                    return
                _, encoded = _snapshot(page, title, created_at)
                if page_index == 0:
                    first_page = encoded
                else:
                    connection.execute("INSERT INTO public_chat_share_pages VALUES(?,?,?,?)",
                        (share_id, page_index, encoded, hashlib.sha256(encoded).digest()))
                page_index += 1
                page.clear()
                page_bytes = 0

            def emit(message) -> None:
                nonlocal total, page_bytes
                item = _projected_message(message)
                size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
                if page and (len(page) >= TARGET_SNAPSHOT_PAGE_MESSAGES or overhead + page_bytes + size + len(page) > TARGET_SNAPSHOT_PAGE_BYTES):
                    flush()
                page.append(item)
                page_bytes += size
                total += 1

            load_messages(emit)
            flush()
            if first_page is None:
                raise PublicChatShareValidationError("messages must contain at least one item.")
            connection.execute("""INSERT INTO public_chat_shares
                (share_id,session_id,token_hash,title,created_at,expires_at,message_count,snapshot_json,snapshot_sha256)
                VALUES(?,?,?,?,?,?,?,?,?)""", (share_id, session_id, hashlib.sha256(token.encode("ascii")).digest(),
                    title, created_at, expires_at, total, first_page, hashlib.sha256(first_page).digest()))
        return {"share_id": share_id, "session_id": session_id, "title": title, "created_at": created_at,
            "expires_at": expires_at, "revoked_at": None, "message_count": total, "token": token}

    def get_snapshot(self, token: str, *, share_id: str | None = None) -> dict[str, Any]:
        return self.get_snapshot_page(token, share_id=share_id, page=0)["snapshot"]

    def get_snapshot_page(self, token: str, *, share_id: str | None = None, page: int | None = None) -> dict[str, Any]:
        value = self._get_snapshot_page(token, share_id=share_id, page=page)
        value.pop("session_id")
        return value

    def _get_snapshot_page(self, token: str, *, share_id: str | None = None, page: int | None = None) -> dict[str, Any]:
        # Invalid input is rejected before storage I/O, without echoing a token.
        if not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None:
            raise PublicChatShareUnavailable()
        if share_id is not None and (not isinstance(share_id, str) or SHARE_ID_PATTERN.fullmatch(share_id) is None):
            raise PublicChatShareUnavailable()
        if page is not None and (type(page) is not int or not 0 <= page <= 2**31 - 1):
            raise PublicChatShareUnavailable()
        token_hash = hashlib.sha256(token.encode("ascii")).digest()
        current_time = _timestamp(self._now(), "current time")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT s.share_id, s.session_id, s.message_count, s.snapshot_json, s.snapshot_sha256 FROM public_chat_shares AS s
                   WHERE s.token_hash = ? AND (? IS NULL OR s.share_id = ?)
                   AND (s.expires_at IS NULL OR s.expires_at > ?)
                   AND NOT EXISTS (SELECT 1 FROM public_chat_share_revocations AS r
                                   WHERE r.share_id = s.share_id)""",
                (token_hash, share_id, share_id, current_time),
            ).fetchone()
            if row is None:
                raise PublicChatShareUnavailable()
            session_id = row["session_id"]
            message_count = row["message_count"]
            has_pages = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='public_chat_share_pages'").fetchone()
            page_count = 1 + (connection.execute("SELECT count(*) FROM public_chat_share_pages WHERE share_id=?", (row["share_id"],)).fetchone()[0] if has_pages else 0)
            page = page_count - 1 if page is None else page
            if page >= page_count:
                raise PublicChatShareUnavailable()
            if page:
                row = connection.execute("SELECT snapshot_json,snapshot_sha256 FROM public_chat_share_pages WHERE share_id=? AND page_index=?", (row["share_id"], page)).fetchone()
                if row is None:
                    raise PublicChatShareUnavailable()
        encoded = row["snapshot_json"]
        digest = row["snapshot_sha256"]
        if (
            not isinstance(encoded, bytes)
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
        return {"snapshot": snapshot, "page": page, "page_count": page_count, "message_count": message_count,
                "session_id": session_id}

    def get_snapshot_video(self, token: str, *, page: int, message_index: int, video_index: int,
                           share_id: str | None = None) -> dict[str, Any]:
        """Resolve only a descriptor already frozen in this exact bounded page."""
        if any(type(index) is not int or not 0 <= index <= 2**31 - 1
               for index in (page, message_index, video_index)):
            raise PublicChatShareUnavailable()
        value = self._get_snapshot_page(token, share_id=share_id, page=page)
        try:
            video = value["snapshot"]["messages"][message_index]["videos"][video_index]
        except (IndexError, KeyError):
            raise PublicChatShareUnavailable() from None
        return {"session_id": value["session_id"], "video": video}

    def authorize_access(self, token: str, *, share_id: str | None = None) -> None:
        """Cheap read-only stream recheck; never reload or render a transcript page."""
        if (not isinstance(token, str) or TOKEN_PATTERN.fullmatch(token) is None
                or share_id is not None and (not isinstance(share_id, str)
                    or SHARE_ID_PATTERN.fullmatch(share_id) is None)):
            raise PublicChatShareUnavailable()
        token_hash = hashlib.sha256(token.encode("ascii")).digest()
        current_time = _timestamp(self._now(), "current time")
        with self._connection() as connection:
            row = connection.execute(
                """SELECT 1 FROM public_chat_shares AS s
                   WHERE s.token_hash = ? AND (? IS NULL OR s.share_id = ?)
                   AND (s.expires_at IS NULL OR s.expires_at > ?)
                   AND NOT EXISTS (SELECT 1 FROM public_chat_share_revocations AS r
                                   WHERE r.share_id = s.share_id)""",
                (token_hash, share_id, share_id, current_time),
            ).fetchone()
        if row is None:
            raise PublicChatShareUnavailable()


_STYLE = """html{color-scheme:light dark;--page:#f7f8fa;--surface:#fff;--ink:#222831;--muted:#69727e;--line:#e4e7eb;--green:#287346;--user:#e6f3e9;--user-line:#d1e7d8;--code:#f3f5f7;--mark:#ecf5ee}
*{box-sizing:border-box}body{margin:0;background:var(--page);color:var(--ink);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;-webkit-font-smoothing:antialiased}::selection{background:#9cceb1;color:#14261c}
.masthead{height:76px;border-bottom:1px solid var(--line);background:var(--surface)}.masthead-inner{max-width:1008px;height:100%;margin:0 auto;padding:0 32px;display:flex;align-items:center;justify-content:space-between;gap:16px}.brand{display:flex;align-items:center;gap:10px;font-size:15px;font-weight:650;letter-spacing:-.35px}.brand-mark{width:26px;height:26px;border-radius:8px;background:var(--green);color:#fff;display:grid;place-items:center;font-size:17px;font-weight:750;line-height:1}.masthead-label{color:var(--muted);font-size:12px}
main{max-width:864px;margin:0 auto;padding:54px 32px 36px}.conversation-header{padding-bottom:30px;border-bottom:1px solid var(--line);margin-bottom:32px}.eyebrow{display:flex;align-items:center;gap:12px;margin-bottom:16px;color:var(--muted);font-size:11px;font-weight:650;letter-spacing:1.4px;text-transform:uppercase}.view-badge{display:inline-flex;align-items:center;gap:6px;padding:3px 9px;border:1px solid var(--user-line);border-radius:6px;background:var(--mark);color:var(--green);font-size:11px;line-height:1.5;letter-spacing:0;text-transform:none}.view-badge:before{content:"";width:5px;height:5px;border-radius:50%;background:currentColor}h1{margin:0;font-size:34px;line-height:1.2;font-weight:650;letter-spacing:-1.15px;overflow-wrap:anywhere}.description{margin:13px 0 17px;color:var(--muted);font-size:14px}.metadata{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;color:var(--muted);font-size:12px}.metadata-separator{opacity:.5}
.transcript{display:flex;flex-direction:column;gap:28px}.message{min-width:0}.message-header{display:flex;align-items:center;gap:8px;margin-bottom:9px}.avatar{display:grid;place-items:center;width:25px;height:25px;border-radius:8px;background:var(--surface);border:1px solid var(--line);color:var(--muted);font-size:11px;font-weight:700}.message-label{margin:0;font-size:12px;font-weight:650;letter-spacing:.05px}.message time{font-size:11px;color:var(--muted);margin-left:3px}.message-body{padding:21px 24px;border:1px solid var(--line);border-radius:4px 16px 16px 16px;background:var(--surface);overflow-wrap:anywhere;min-width:0}.user{width:88%;align-self:flex-end}.user .message-header{justify-content:flex-end}.user .avatar{background:var(--mark);border-color:var(--user-line);color:var(--green)}.user .message-body{background:var(--user);border-color:var(--user-line);border-radius:16px 4px 16px 16px}.plain-text{white-space:pre-wrap;tab-size:4}
.markdown>:first-child{margin-top:0}.markdown>:last-child{margin-bottom:0}.markdown p{margin:0 0 16px;white-space:pre-wrap}.markdown h2,.markdown h3,.markdown h4,.markdown h5,.markdown h6{margin:24px 0 10px;font-weight:650;line-height:1.4;letter-spacing:-.25px}.markdown h2{font-size:21px}.markdown h3{font-size:18px}.markdown h4,.markdown h5,.markdown h6{font-size:16px}.markdown ul,.markdown ol{padding-left:23px;margin:10px 0 18px}.markdown li{padding-left:3px;margin:5px 0;white-space:pre-wrap}.markdown li::marker{color:var(--muted)}.markdown blockquote{margin:18px 0;padding:3px 0 3px 17px;border-left:3px solid var(--user-line);color:var(--muted);white-space:pre-wrap}.markdown code{font:12.5px/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);border-radius:4px;padding:2px 5px}.code-block{margin:18px 0;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--code)}.code-language{border-bottom:1px solid var(--line);padding:7px 14px;font-size:11px;color:var(--muted)}.code-block pre{margin:0;padding:14px 16px;overflow:auto;tab-size:4;white-space:pre;overscroll-behavior-x:contain}.code-block code{padding:0;border-radius:0;font-size:12px;background:transparent}.table-wrap{max-width:100%;overflow:auto;margin:18px 0;border:1px solid var(--line);border-radius:9px;overscroll-behavior-x:contain}.markdown table{width:100%;border-collapse:collapse;font-size:13px;line-height:1.55}.markdown th,.markdown td{padding:11px 13px;text-align:left;border-bottom:1px solid var(--line);min-width:90px;overflow-wrap:anywhere}.markdown th{background:var(--code);font-weight:600}.markdown tbody tr:last-child td{border-bottom:0}.markdown hr{border:0;border-top:1px solid var(--line);margin:24px 0}
.conversation-footer{margin-top:38px;padding-top:23px;border-top:1px solid var(--line);text-align:center;color:var(--muted);font-size:12px}.conversation-footer p{margin:0 0 4px}.footer-brand{font-size:11px;opacity:.8}
.unlock-main{max-width:520px;padding-top:76px}.unlock-card{padding:30px;border:1px solid var(--line);border-radius:18px;background:var(--surface)}.unlock-card h1{font-size:27px;letter-spacing:-.7px}.unlock-card label{display:block;font-size:13px;font-weight:600;margin:20px 0 7px}.unlock-card input[type=password]{width:100%;min-width:0;padding:12px;border:1px solid var(--line);border-radius:9px;background:var(--page);color:var(--ink);font:14px ui-monospace,SFMono-Regular,Menlo,monospace}.unlock-card input:focus-visible,.unlock-card button:focus-visible{outline:2px solid var(--green);outline-offset:3px}.unlock-card .remember{display:flex;gap:8px;align-items:center;margin:16px 0;color:var(--muted);font-size:12px;font-weight:400}.remember input{accent-color:var(--green)}.unlock-card button{width:100%;padding:12px 16px;border:1px solid var(--user-line);border-radius:9px;background:var(--user);color:var(--green);font:600 14px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;cursor:pointer}.unlock-error{font-size:13px;color:#bd4444}.unlock-help{margin:18px 0 0;font-size:12px;color:var(--muted)}
.pagination{display:flex;justify-content:space-between;align-items:center;gap:12px;margin:22px 0;font-size:12px;color:var(--muted)}.pagination a{display:inline-block;border:1px solid var(--line);border-radius:8px;padding:7px 12px;color:var(--green);background:var(--surface);text-decoration:none}.pagination a:hover{background:var(--mark)}.pagination a:focus-visible{outline:2px solid var(--green);outline-offset:3px}.pagination span{flex:1;text-align:center}
@media(prefers-color-scheme:dark){html{--page:#141619;--surface:#1b1e22;--ink:#e7e9ed;--muted:#9da5b0;--line:#2b3037;--green:#8bd5a5;--user:#213b2c;--user-line:#31513c;--code:#171a1e;--mark:#22372b}.brand-mark{background:#397f51;color:#f4fff7}}
@media(max-width:600px){.masthead{height:62px}.masthead-inner{padding:0 20px}.masthead-label{font-size:11px}main{padding:32px 18px 28px}h1{font-size:27px;letter-spacing:-.8px}.conversation-header{padding-bottom:24px;margin-bottom:25px}.eyebrow{margin-bottom:13px;font-size:10px}.description{font-size:13px}.metadata{font-size:11px;gap:5px 7px}.transcript{gap:24px}.user{width:94%}.message-body{padding:17px 18px;font-size:14px}.message time{font-size:10px}.markdown h2{font-size:19px}.markdown h3{font-size:17px}.code-block pre{padding:12px}.markdown th,.markdown td{padding:9px 11px}.conversation-footer{font-size:11px}}
@media print{html{color-scheme:light;--page:#fff;--surface:#fff;--ink:#111;--muted:#555;--line:#ddd;--user:#f0f7f2;--user-line:#ddd;--code:#f5f5f5;--mark:#f0f7f2;--green:#286b43}body{font-size:11pt}.masthead{height:48px}main{max-width:none;padding:24px 0}.message-body{break-inside:avoid}.code-block pre{white-space:pre-wrap;overflow-wrap:anywhere}.table-wrap{overflow:visible}.conversation-footer{margin-top:24px}}
"""
_STYLE += "\n.shared-video{margin:16px 0 0;white-space:normal}.shared-video video{display:block;width:100%;max-height:65vh;background:#111;border-radius:10px}.shared-video figcaption,.video-caption{margin-top:7px;color:var(--muted);font-size:12px;overflow-wrap:anywhere}"
_STYLE_HASH = base64.b64encode(hashlib.sha256(_STYLE.encode("utf-8")).digest()).decode("ascii")


# A deliberately small formatting vocabulary. Every text fragment is escaped;
# raw HTML, links, images, embeds, and arbitrary attributes are never emitted.
# Keeping this local also avoids a Markdown dependency in the server runtime.
_INLINE_MARKDOWN = re.compile(r"`([^`\n]+)`|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+)$")
_LIST_ITEM = re.compile(r"^ {0,3}(?:([-+*])|([0-9]{1,9})[.)])\s+(.+)$")
_TABLE_DIVIDER = re.compile(r"^:?-{3,}:?$")


def _render_inline_text(text: str) -> str:
    parts = []
    position = 0
    for match in _INLINE_MARKDOWN.finditer(text):
        parts.append(html.escape(text[position:match.start()], quote=True))
        for index, tag in ((1, "code"), (2, "strong"), (3, "em")):
            if match[index] is not None:
                parts.append(f"<{tag}>{html.escape(match[index], quote=True)}</{tag}>")
                break
        position = match.end()
    parts.append(html.escape(text[position:], quote=True))
    return "".join(parts)


def _table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _render_message_markdown(text: str) -> str:
    """Format common assistant Markdown without interpreting HTML or URLs."""
    lines = text.splitlines()
    parts: list[str] = []
    paragraph: list[str] = []
    index = 0

    def flush_paragraph() -> None:
        if paragraph:
            parts.append(f'<p>{_render_inline_text(chr(10).join(paragraph))}</p>')
            paragraph.clear()

    while index < len(lines):
        line = lines[index]
        fence = _FENCE.match(line)
        heading = _HEADING.match(line)
        item = _LIST_ITEM.match(line)
        if not line.strip():
            flush_paragraph()
            index += 1
            continue
        if fence:
            flush_paragraph()
            marker = fence[1]
            label = html.escape(fence[2].strip(), quote=True)
            code = []
            index += 1
            while index < len(lines):
                closing = lines[index].strip()
                if len(closing) >= len(marker) and set(closing) == {marker[0]}:
                    index += 1
                    break
                code.append(lines[index])
                index += 1
            parts.append('<div class="code-block">')
            if label:
                parts.append(f'<div class="code-language">{label}</div>')
            parts.append(f'<pre><code>{html.escape(chr(10).join(code), quote=True)}</code></pre></div>')
            continue
        if heading:
            flush_paragraph()
            level = min(len(heading[1]) + 1, 6)
            parts.append(f"<h{level}>{_render_inline_text(heading[2])}</h{level}>")
        elif line.strip() in ("---", "***", "___"):
            flush_paragraph()
            parts.append("<hr>")
        elif item:
            flush_paragraph()
            tag = "ul" if item[1] else "ol"
            start = f' start="{int(item[2])}"' if item[2] and item[2] != "1" else ""
            parts.append(f"<{tag}{start}>")
            while index < len(lines):
                match = _LIST_ITEM.match(lines[index])
                if not match or ("ul" if match[1] else "ol") != tag:
                    break
                parts.append(f"<li>{_render_inline_text(match[3])}</li>")
                index += 1
            parts.append(f"</{tag}>")
            continue
        elif line.lstrip().startswith("> "):
            flush_paragraph()
            quote = []
            while index < len(lines) and lines[index].lstrip().startswith("> "):
                quote.append(lines[index].lstrip()[2:])
                index += 1
            parts.append(f'<blockquote>{_render_inline_text(chr(10).join(quote))}</blockquote>')
            continue
        elif "|" in line and index + 1 < len(lines) and "|" in lines[index + 1]:
            columns = _table_cells(line)
            dividers = _table_cells(lines[index + 1])
            if len(columns) == len(dividers) and 1 < len(columns) <= 64 and all(
                _TABLE_DIVIDER.fullmatch(cell) for cell in dividers
            ):
                flush_paragraph()
                parts.append('<div class="table-wrap" role="region" aria-label="Conversation table" tabindex="0"><table><thead><tr>')
                parts.extend(f'<th scope="col">{_render_inline_text(cell)}</th>' for cell in columns)
                parts.append("</tr></thead><tbody>")
                index += 2
                while index < len(lines) and "|" in lines[index]:
                    cells = _table_cells(lines[index])
                    if len(cells) != len(columns):
                        break
                    parts.append("<tr>" + "".join(f"<td>{_render_inline_text(cell)}</td>" for cell in cells) + "</tr>")
                    index += 1
                parts.append("</tbody></table></div>")
                continue
            paragraph.append(line)
        else:
            paragraph.append(line)
        index += 1
    flush_paragraph()
    return "".join(parts)


def public_chat_share_headers(*, allow_unlock_form: bool = False) -> dict[str, str]:
    """Use these headers for both successful viewer pages and unavailable pages."""
    return {
        "Content-Type": "text/html; charset=utf-8",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'none'; "
            f"style-src 'sha256-{_STYLE_HASH}'; "
            "img-src 'none'; media-src 'self'; connect-src 'none'; base-uri 'none'; "
            + ("form-action 'self'; frame-ancestors 'none'; sandbox allow-forms allow-same-origin"
               if allow_unlock_form else "form-action 'none'; frame-ancestors 'none'; sandbox allow-same-origin")
        ),
        "Cache-Control": "no-store",
        # A native navigation form under no-referrer submits Origin: null in
        # Chromium. Keep an origin on this token-free, same-origin entry page
        # so strict CSRF validation works; transcript pages disclose no referrer.
        "Referrer-Policy": "same-origin" if allow_unlock_form else "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    }


def render_public_chat_unlock_html(share_id: str, *, invalid_token: bool = False) -> bytes:
    """A generic token prompt; it never reads or discloses snapshot metadata."""
    if not isinstance(share_id, str) or SHARE_ID_PATTERN.fullmatch(share_id) is None:
        raise PublicChatShareUnavailable()
    error = '<p class="unlock-error" role="alert">The token is invalid or this shared chat is no longer available.</p>' if invalid_token else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        f'<title>Open shared chat · AgentsDock</title><style>{_STYLE}</style></head><body>'
        '<div class="masthead"><div class="masthead-inner"><div class="brand">'
        '<span class="brand-mark" aria-hidden="true">A</span>AgentsDock</div>'
        '<span class="masthead-label">Shared conversations</span></div></div>'
        '<main class="unlock-main"><section class="unlock-card"><div class="eyebrow">Shared conversation'
        '<span class="view-badge">View only</span></div><h1>Open your shared chat</h1>'
        '<p class="description">Enter the access token provided by the person who shared this conversation.</p>'
        f'{error}<form method="post" action="/shared-chat/{share_id}/unlock">'
        '<label for="access-token">Access token</label>'
        '<input id="access-token" name="access_token" type="password" required minlength="43" maxlength="43" '
        'pattern="[A-Za-z0-9_-]{43}" autocomplete="off" autocapitalize="none" spellcheck="false">'
        '<label class="remember"><input type="checkbox" name="remember" value="1">Remember in this browser for 30 days</label>'
        '<button type="submit">Open shared chat</button></form>'
        '<p class="unlock-help">This opens a saved, read-only conversation.</p></section>'
        '<footer class="conversation-footer"><p>Shared with AgentsDock</p></footer></main></body></html>'
    ).encode("utf-8")


def render_public_chat_html(snapshot: dict[str, Any], *, page: int = 0, page_count: int = 1,
                            message_count: int | None = None, navigation_base: str | None = None) -> bytes:
    """Render a static transcript with escaped, presentation-only Markdown."""
    if not isinstance(snapshot, dict) or set(snapshot) != {"title", "created_at", "messages"}:
        raise PublicChatShareValidationError("Invalid public snapshot fields.")
    snapshot, _ = _snapshot(snapshot["messages"], snapshot["title"], snapshot["created_at"])
    title = html.escape(snapshot["title"], quote=True)
    created_at = datetime.fromtimestamp(snapshot["created_at"], timezone.utc)
    created = created_at.strftime("%b %d, %Y · %H:%M UTC")
    if (type(page) is not int or type(page_count) is not int or not 0 <= page < page_count
            or message_count is not None and (type(message_count) is not int or message_count < len(snapshot["messages"]))):
        raise PublicChatShareValidationError("Invalid snapshot page.")
    if navigation_base is not None and re.fullmatch(r"/(?:shared-chat/share_[a-f0-9]{32}|share/[A-Za-z0-9_-]{43})", navigation_base) is None:
        raise PublicChatShareValidationError("Invalid snapshot navigation.")
    navigation = ""
    if navigation_base is not None and page_count > 1:
        older = f'<a href="{navigation_base}?page={page - 1}#conversation-end" rel="prev">← Older messages</a>' if page else ""
        newer = f'<a href="{navigation_base}?page={page + 1}#conversation-end" rel="next">Newer messages →</a>' if page + 1 < page_count else ""
        navigation = f'<nav class="pagination" aria-label="Conversation pages">{older}<span>Page {page + 1:,} of {page_count:,}</span>{newer}</nav>'
    count = len(snapshot["messages"]) if message_count is None else message_count
    count_label = "message" if count == 1 else "messages"
    parts = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta name="robots" content="noindex,nofollow,noarchive">',
        f"<title>{title} · AgentsDock</title><style>{_STYLE}</style></head><body>",
        '<div class="masthead"><div class="masthead-inner"><div class="brand">',
        '<span class="brand-mark" aria-hidden="true">A</span>AgentsDock</div>',
        '<span class="masthead-label">Shared conversations</span></div></div><main>',
        '<header class="conversation-header"><div class="eyebrow">Conversation',
        '<span class="view-badge">View only</span></div>',
        f'<h1>{title}</h1><p class="description">A saved conversation, shared for reading.</p>',
        f'<div class="metadata"><span>Shared <time datetime="{created_at.isoformat()}">{created}</time></span>',
        f'<span class="metadata-separator" aria-hidden="true">·</span><span>{count:,} {count_label}</span>',
        f'</div>{navigation}</header><div class="transcript" aria-label="Read-only conversation snapshot">',
    ]
    for index, message in enumerate(snapshot["messages"]):
        role = message["role"]
        label = "User" if role == "user" else "Assistant"
        parts.append(f'<section class="message {role}" aria-labelledby="message-{index}"><div class="message-header">')
        parts.append(f'<span class="avatar" aria-hidden="true">{label[0]}</span><h2 class="message-label" id="message-{index}">{label}</h2>')
        if "timestamp" in message:
            timestamp = datetime.fromtimestamp(message["timestamp"], timezone.utc)
            stamp = timestamp.strftime("%b %d · %H:%M UTC")
            parts.append(f'<time datetime="{timestamp.isoformat()}">{stamp}</time>')
        parts.append('</div>')
        content = (html.escape(message["text"], quote=True) if role == "user"
                   else _render_message_markdown(message["text"]))
        parts.append(f'<div class="message-body {"plain-text" if role == "user" else "markdown"}">{content}')
        for video_index, video in enumerate(message.get("videos", [])):
            filename = html.escape(video["filename"], quote=True)
            if navigation_base is None:
                parts.append(f'<p class="video-caption">Video: {filename}</p>')
                continue
            source = f"{navigation_base}/media/{page}/{index}/{video_index}"
            parts.append(f'<figure class="shared-video"><video controls preload="metadata" playsinline '
                         f'aria-label="{filename}" src="{source}">Your browser cannot play this video.</video>'
                         f'<figcaption>{filename}</figcaption></figure>')
        parts.append('</div>')
        parts.append('</section>')
    parts.append(f'</div>{navigation}<footer class="conversation-footer"><p>This is a saved copy. Replies and updates stay in the original chat.</p><p class="footer-brand">Shared with AgentsDock</p></footer><div id="conversation-end"></div></main></body></html>')
    return "".join(parts).encode("utf-8")
