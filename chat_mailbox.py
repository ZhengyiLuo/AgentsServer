"""Passive same-server inbox metadata on the existing cross-chat SQLite ledger.

Call mutations in the caller's BEGIN IMMEDIATE transaction and current pair
authorization lock. This module never commits, opens a database, starts a turn,
reads credentials, sends a reply, or interprets message text as permission.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections.abc import Iterable


MAX_PAGE_ITEMS = 25
MAX_PAGE_BYTES = 112 * 1024
_PAIR = re.compile(r"pair_[0-9a-f]{32}\Z")


class MailboxConflict(ValueError):
    pass


def _transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("Mailbox mutations require a caller-owned transaction")


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 240 or not value.isprintable():
        raise ValueError("Invalid mailbox identifier")
    return value


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PAGE_ITEMS:
        raise ValueError("Mailbox page limit must be between 1 and 25")
    return value


def _cursor(value: int) -> int:
    if type(value) is not int or not 0 <= value <= (1 << 63) - 1:
        raise ValueError("Invalid mailbox cursor")
    return value


def _pairs(values: Iterable[str]) -> str:
    if isinstance(values, (str, bytes)):
        raise ValueError("Expected current authorized pairs")
    clean = set(values)
    if any(not isinstance(value, str) or not _PAIR.fullmatch(value) for value in clean):
        raise ValueError("Invalid authorized pair")
    return json.dumps(sorted(clean), separators=(",", ":"))


def initialize(connection: sqlite3.Connection) -> None:
    """Add mailbox tables without altering existing envelopes or their bodies."""
    statements = (
        """CREATE TABLE IF NOT EXISTS chat_mailbox_reads (
            id TEXT PRIMARY KEY, target_session_id TEXT NOT NULL,
            source_session_id TEXT NOT NULL, reader_run_id TEXT NOT NULL,
            request_id TEXT NOT NULL, snapshot_seq INTEGER NOT NULL,
            page_limit INTEGER NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(target_session_id, reader_run_id, request_id))""",
        """CREATE TABLE IF NOT EXISTS chat_mailbox_messages (
            mailbox_seq INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE REFERENCES cross_chat_envelopes(id) ON DELETE CASCADE,
            source_session_id TEXT NOT NULL, target_session_id TEXT NOT NULL,
            pair_id TEXT NOT NULL, stored_at TEXT NOT NULL,
            in_reply_to_message_id TEXT, read_id TEXT REFERENCES chat_mailbox_reads(id),
            read_at TEXT, excluded_at TEXT, excluded_reason TEXT,
            read_event_published INTEGER NOT NULL DEFAULT 0 CHECK(read_event_published IN (0,1)))""",
        """CREATE TABLE IF NOT EXISTS chat_mailbox_read_pages (
            read_id TEXT NOT NULL REFERENCES chat_mailbox_reads(id) ON DELETE CASCADE,
            after_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL,
            has_more INTEGER NOT NULL CHECK(has_more IN (0,1)), message_ids_json TEXT NOT NULL,
            PRIMARY KEY(read_id, after_seq))""",
        """CREATE INDEX IF NOT EXISTS chat_mailbox_sender_order
            ON chat_mailbox_messages(target_session_id, source_session_id, mailbox_seq)""",
        """CREATE INDEX IF NOT EXISTS chat_mailbox_unread_order
            ON chat_mailbox_messages(target_session_id, read_at, mailbox_seq)""",
        """CREATE INDEX IF NOT EXISTS chat_mailbox_read_outbox
            ON chat_mailbox_messages(read_event_published, mailbox_seq)
            WHERE read_at IS NOT NULL AND excluded_at IS NULL""",
        """CREATE INDEX IF NOT EXISTS chat_mailbox_read_request
            ON chat_mailbox_reads(target_session_id, request_id)""",
    )
    for statement in statements:
        connection.execute(statement)


_JOIN = "chat_mailbox_messages m JOIN cross_chat_envelopes e ON e.id=m.message_id"
_VALID = """m.excluded_at IS NULL AND e.status='stored' AND e.delivery_mode='mailbox'
    AND e.kind='instruction' AND e.authorization_kind='configured_route'
    AND e.source_session_id=m.source_session_id AND e.target_session_id=m.target_session_id
    AND e.authorization_pair_id=m.pair_id"""
_COLUMNS = """m.*, e.body AS original_body, e.target_body, e.message_revision, e.created_at"""


def _message(row: sqlite3.Row) -> dict:
    edited = row["message_revision"] > 0 and isinstance(row["target_body"], str)
    return {
        "message_id": row["message_id"], "mailbox_seq": row["mailbox_seq"],
        "source_session_id": row["source_session_id"], "target_session_id": row["target_session_id"],
        "conversation_id": row["pair_id"],
        "body": row["target_body"] if edited else row["original_body"],
        "created_at": row["created_at"], "stored_at": row["stored_at"],
        "in_reply_to_message_id": row["in_reply_to_message_id"],
        "read_id": row["read_id"], "read_at": row["read_at"],
        "message_revision": row["message_revision"] if edited else 0,
        "message_edited_by_user": edited,
    }


def store_message(connection: sqlite3.Connection, message_id: str, *, now: str,
                  in_reply_to_message_id: str | None = None) -> dict:
    """Register one already-authorized stored envelope in the same transaction."""
    _transaction(connection)
    record = connection.execute("SELECT * FROM cross_chat_envelopes WHERE id=?",
                                (_identifier(message_id),)).fetchone()
    if (record is None or record["status"] != "stored" or record["delivery_mode"] != "mailbox"
            or record["kind"] != "instruction" or record["authorization_kind"] != "configured_route"
            or not _PAIR.fullmatch(record["authorization_pair_id"] or "")
            or record["source_session_id"] == record["target_session_id"]
            or record["target_run_id"]):
        raise MailboxConflict("Envelope is not an unlaunched mailbox message")
    parent = record["reply_to_message_id"]
    if in_reply_to_message_id is not None and in_reply_to_message_id != parent:
        raise MailboxConflict("Reply relationship does not match the stored envelope")
    if parent:
        prior = connection.execute("SELECT * FROM cross_chat_envelopes WHERE id=?", (parent,)).fetchone()
        prior_mail = connection.execute("SELECT excluded_at FROM chat_mailbox_messages WHERE message_id=?",
                                         (parent,)).fetchone()
        if (prior is None or prior["source_session_id"] != record["target_session_id"]
                or prior["target_session_id"] != record["source_session_id"]
                or prior["authorization_pair_id"] != record["authorization_pair_id"]
                or prior["status"] in ("failed", "cancelled")
                or (prior_mail is not None and prior_mail["excluded_at"] is not None)):
            raise MailboxConflict("Reply must reference an available exact peer message")
    connection.execute("""INSERT OR IGNORE INTO chat_mailbox_messages
        (message_id,source_session_id,target_session_id,pair_id,stored_at,in_reply_to_message_id)
        VALUES(?,?,?,?,?,?)""", (message_id, record["source_session_id"], record["target_session_id"],
                                record["authorization_pair_id"], now, parent))
    row = connection.execute(f"SELECT {_COLUMNS} FROM {_JOIN} WHERE m.message_id=?", (message_id,)).fetchone()
    if (row["source_session_id"] != record["source_session_id"]
            or row["target_session_id"] != record["target_session_id"]
            or row["pair_id"] != record["authorization_pair_id"]
            or row["in_reply_to_message_id"] != parent):
        raise MailboxConflict("Stored mailbox identity changed")
    return _message(row)


def _scope(target: str, source: str | None, pairs: Iterable[str]) -> tuple[str, list]:
    clause = f"{_VALID} AND m.target_session_id=? AND m.pair_id IN (SELECT value FROM json_each(?))"
    args = [_identifier(target), _pairs(pairs)]
    if source:
        clause += " AND m.source_session_id=?"
        args.append(_identifier(source))
    return clause, args


def list_senders(connection: sqlite3.Connection, target_session_id: str, allowed_pair_ids: Iterable[str],
                 *, after_sender: str = "", limit: int = MAX_PAGE_ITEMS, unread_only: bool = False) -> dict:
    """Read grouped metadata only; opening the mailbox never marks anything read."""
    clause, args = _scope(target_session_id, None, allowed_pair_ids)
    if after_sender:
        _identifier(after_sender)
    if unread_only:
        clause += " AND m.read_at IS NULL"
    rows = connection.execute(f"""SELECT m.source_session_id, COUNT(*) AS message_count,
        SUM(CASE WHEN m.read_at IS NULL THEN 1 ELSE 0 END) AS unread_count,
        MAX(m.mailbox_seq) AS latest_seq, MAX(e.created_at) AS latest_created_at
        FROM {_JOIN} WHERE {clause} AND m.source_session_id>?
        GROUP BY m.source_session_id ORDER BY m.source_session_id LIMIT ?""",
        (*args, after_sender, _limit(limit) + 1)).fetchall()
    more = len(rows) > limit
    selected = [dict(row) for row in rows[:limit]]
    return {"senders": selected, "has_more": more,
            "next_after_sender": selected[-1]["source_session_id"] if more else None}


def _page(connection: sqlite3.Connection, clause: str, args: list, limit: int,
          read_metadata: dict | None = None) -> tuple[list[dict], bool]:
    messages, used = [], 8192  # reserve for public titles, receipt and reply-route metadata
    rows = connection.execute(f"SELECT {_COLUMNS} FROM {_JOIN} WHERE {clause} ORDER BY m.mailbox_seq LIMIT ?",
                              (*args, limit + 1))
    for row in rows:
        message = _message(row)
        if read_metadata:
            message.update(read_metadata)
        size = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if len(messages) == limit or used + size > MAX_PAGE_BYTES:
            if not messages:
                raise MailboxConflict("Message exceeds the bounded mailbox response; no message was read")
            return messages, True
        messages.append(message)
        used += size
    return messages, False


def list_messages(connection: sqlite3.Connection, target_session_id: str, source_session_id: str | None,
                  allowed_pair_ids: Iterable[str], *, after_seq: int = 0, limit: int = MAX_PAGE_ITEMS,
                  unread_only: bool = False) -> dict:
    clause, args = _scope(target_session_id, source_session_id, allowed_pair_ids)
    clause += " AND m.mailbox_seq>?"
    args.append(_cursor(after_seq))
    if unread_only:
        clause += " AND m.read_at IS NULL"
    messages, more = _page(connection, clause, args, _limit(limit))
    return {"messages": messages, "has_more": more,
            "next_after_seq": messages[-1]["mailbox_seq"] if more else None}


def read_sender(connection: sqlite3.Connection, *, target_session_id: str, source_session_id: str,
                reader_run_id: str, request_id: str, allowed_pair_ids: Iterable[str], now: str,
                after_seq: int = 0, limit: int = MAX_PAGE_ITEMS) -> dict:
    """Atomically read one page of a fixed unread snapshot, with retry receipts.

    Arrivals after snapshot_seq remain unread. A retry only returns its original
    page IDs across fresh authorized runs, subject to current authorization and
    cancellation/deletion state. The first reader run is attribution, not authority.
    Read means included in this tool response, not proof of model understanding.
    """
    _transaction(connection)
    _identifier(source_session_id)
    _identifier(reader_run_id)
    _identifier(request_id)
    _cursor(after_seq)
    _limit(limit)
    clause, args = _scope(target_session_id, source_session_id, allowed_pair_ids)
    batches = connection.execute("""SELECT * FROM chat_mailbox_reads
        WHERE target_session_id=? AND request_id=? LIMIT 2""",
        (target_session_id, request_id)).fetchall()
    if len(batches) > 1:
        # Older versions permitted this key once per run. Never guess which
        # historical snapshot a caller intended, or consume another batch.
        raise MailboxConflict("Read request matches multiple historical snapshots")
    batch = batches[0] if batches else None
    if batch is None:
        if after_seq:
            raise MailboxConflict("A mailbox read must start at its first page")
        high_water = connection.execute("""SELECT COALESCE(MAX(mailbox_seq),0) FROM chat_mailbox_messages
            WHERE target_session_id=? AND source_session_id=?""", (target_session_id, source_session_id)).fetchone()[0]
        read_id = "mailread_" + uuid.uuid4().hex
        connection.execute("""INSERT INTO chat_mailbox_reads
            (id,target_session_id,source_session_id,reader_run_id,request_id,snapshot_seq,page_limit,created_at)
            VALUES(?,?,?,?,?,?,?,?)""", (read_id, target_session_id, source_session_id, reader_run_id,
                                       request_id, high_water, limit, now))
        batch = connection.execute("SELECT * FROM chat_mailbox_reads WHERE id=?", (read_id,)).fetchone()
    if batch["source_session_id"] != source_session_id or batch["page_limit"] != limit:
        raise MailboxConflict("Read request was already used with different parameters")
    page = connection.execute("SELECT * FROM chat_mailbox_read_pages WHERE read_id=? AND after_seq=?",
                              (batch["id"], after_seq)).fetchone()
    replayed = page is not None
    if page is None:
        if after_seq and connection.execute("""SELECT 1 FROM chat_mailbox_read_pages
            WHERE read_id=? AND end_seq=? AND has_more=1""", (batch["id"], after_seq)).fetchone() is None:
            raise MailboxConflict("Cursor does not belong to this read snapshot")
        messages, more = _page(connection, clause + " AND m.read_at IS NULL AND m.mailbox_seq>? AND m.mailbox_seq<=?",
                               [*args, after_seq, batch["snapshot_seq"]], limit,
                               {"read_id": batch["id"], "read_at": now})
        ids = [message["message_id"] for message in messages]
        end = messages[-1]["mailbox_seq"] if messages else after_seq
        connection.execute("""INSERT INTO chat_mailbox_read_pages
            (read_id,after_seq,end_seq,has_more,message_ids_json) VALUES(?,?,?,?,?)""",
            (batch["id"], after_seq, end, int(more), json.dumps(ids, separators=(",", ":"))))
        for message_id in ids:
            connection.execute("""UPDATE chat_mailbox_messages SET read_id=?,read_at=?
                WHERE message_id=? AND read_at IS NULL""", (batch["id"], now, message_id))
        page = connection.execute("SELECT * FROM chat_mailbox_read_pages WHERE read_id=? AND after_seq=?",
                                  (batch["id"], after_seq)).fetchone()
    ids = json.loads(page["message_ids_json"])
    messages, _ = _page(connection, clause + " AND m.message_id IN (SELECT value FROM json_each(?))",
                         [*args, json.dumps(ids)], limit) if ids else ([], False)
    return {"read_id": batch["id"], "snapshot_seq": batch["snapshot_seq"], "messages": messages,
            "next_after_seq": page["end_seq"] if page["has_more"] else None,
            "has_more": bool(page["has_more"]), "unavailable_count": len(ids) - len(messages), "replayed": replayed}


def exclude_message(connection: sqlite3.Connection, message_id: str, *, target_session_id: str,
                    reason: str, now: str) -> bool:
    _transaction(connection)
    if reason not in ("cancelled", "deleted", "revoked"):
        raise ValueError("Invalid mailbox exclusion reason")
    return connection.execute("""UPDATE chat_mailbox_messages SET excluded_at=?,excluded_reason=?
        WHERE message_id=? AND target_session_id=? AND excluded_at IS NULL""",
        (now, reason, _identifier(message_id), _identifier(target_session_id))).rowcount == 1


def cancel_message(connection: sqlite3.Connection, message_id: str, *, now: str) -> bool:
    """Cancel an unread stored message atomically; a completed read wins the race."""
    _transaction(connection)
    record = connection.execute(f"""SELECT m.read_at,m.excluded_at,m.excluded_reason,
        e.status,e.delivery_mode FROM {_JOIN} WHERE m.message_id=?""", (_identifier(message_id),)).fetchone()
    if record is None:
        return False
    if record["status"] == "cancelled" and record["excluded_reason"] == "cancelled":
        return False
    if (record["status"] != "stored" or record["delivery_mode"] != "mailbox"
            or record["excluded_at"] is not None or record["read_at"] is not None):
        raise MailboxConflict("Message is no longer unread and cancellable")
    connection.execute("""UPDATE chat_mailbox_messages SET excluded_at=?,excluded_reason='cancelled'
        WHERE message_id=?""", (now, message_id))
    connection.execute("""UPDATE cross_chat_envelopes SET status='cancelled',lifecycle_status='',updated_at=?
        WHERE id=?""", (now, message_id))
    return True


def exclude_pair(connection: sqlite3.Connection, pair_id: str, *, now: str) -> int:
    _transaction(connection)
    _pairs([pair_id])
    return connection.execute("""UPDATE chat_mailbox_messages SET excluded_at=?,excluded_reason='revoked'
        WHERE pair_id=? AND excluded_at IS NULL""", (now, pair_id)).rowcount


def exclude_session(connection: sqlite3.Connection, session_id: str, *, now: str) -> int:
    _transaction(connection)
    _identifier(session_id)
    return connection.execute("""UPDATE chat_mailbox_messages SET excluded_at=?,excluded_reason='deleted'
        WHERE (source_session_id=? OR target_session_id=?) AND excluded_at IS NULL""",
        (now, session_id, session_id)).rowcount


def pending_read_events(connection: sqlite3.Connection, *, limit: int = MAX_PAGE_ITEMS) -> list[dict]:
    rows, _ = _page(connection, _VALID + " AND m.read_at IS NOT NULL AND m.read_event_published=0", [], _limit(limit))
    return rows


def unread_targets(connection: sqlite3.Connection) -> list[str]:
    """Startup recovery metadata only; never fetch bodies or launch recipients."""
    return [row[0] for row in connection.execute(f"""SELECT DISTINCT m.target_session_id FROM {_JOIN}
        WHERE {_VALID} AND m.read_at IS NULL ORDER BY m.target_session_id""")]


def mark_read_event_published(connection: sqlite3.Connection, message_id: str) -> bool:
    _transaction(connection)
    return connection.execute("""UPDATE chat_mailbox_messages SET read_event_published=1
        WHERE message_id=? AND read_at IS NOT NULL AND read_event_published=0""",
        (_identifier(message_id),)).rowcount == 1
