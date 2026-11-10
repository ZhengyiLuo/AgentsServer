"""Explicitly approved external mailbox principals. No sessions or provider runs.

The owner provisions a private config out of band; an absent, malformed, or
revoked config grants nothing. HTTP callers cannot provision or change policy.
Ledger functions execute in the caller's existing cross-chat transaction.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

import chat_mailbox

MAX_CONFIG_BYTES = 128 * 1024
MAX_TEXT_CHARS = 16000
PEER_RE = re.compile(r"extpeer_[0-9a-f]{32}\Z")
SESSION_RE = re.compile(r"sess_[A-Za-z0-9_-]{1,100}\Z")


class PeerUnauthorized(ValueError):
    pass


class PeerConflict(ValueError):
    pass


def _text(value, *, maximum=16000):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("Invalid external peer text")
    value.encode("utf-8", "strict")
    return value


def validate_peer(raw: dict) -> dict:
    expected = {"peer_id", "display_name", "target_session_id", "credential_sha256",
                "revision", "pair_id", "route_id", "inbound_route_id", "enabled", "policy"}
    if not isinstance(raw, dict) or set(raw) != expected or type(raw["enabled"]) is not bool:
        raise ValueError("Invalid external peer config")
    for key, pattern in (("peer_id", PEER_RE), ("target_session_id", SESSION_RE),
                         ("credential_sha256", re.compile(r"[0-9a-f]{64}\Z")),
                         ("revision", re.compile(r"rev_[0-9a-f]{32}\Z")),
                         ("pair_id", re.compile(r"pair_[0-9a-f]{32}\Z")),
                         ("route_id", re.compile(r"route_[0-9a-f]{32}\Z")),
                         ("inbound_route_id", re.compile(r"route_[0-9a-f]{32}\Z"))):
        if not isinstance(raw[key], str) or not pattern.fullmatch(raw[key]):
            raise ValueError("Invalid external peer identity")
    if raw["route_id"] == raw["inbound_route_id"]:
        raise ValueError("External route members must differ")
    if not _text(raw["display_name"], maximum=80).isprintable():
        raise ValueError("Invalid external peer label")
    policy = raw["policy"]
    if not isinstance(policy, dict) or set(policy) != {"approval_id", "approved_at", "delegated_by", "source_user_instruction"}:
        raise ValueError("External peer requires explicit owner approval")
    for key in ("approval_id", "approved_at", "delegated_by"):
        if not _text(policy[key], maximum=160).isprintable():
            raise ValueError("Invalid external peer approval identity")
    _text(policy["source_user_instruction"], maximum=16000)
    return {**raw, "policy": dict(policy)}


def load_peers(path: Path | None) -> list[dict]:
    """Bounded, owner-private, no-follow reads; malformed policy fails closed."""
    if path is None:
        return []
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or not 0 < info.st_size <= MAX_CONFIG_BYTES):
                return []
            data = json.loads(stream.read(MAX_CONFIG_BYTES + 1))
        if not isinstance(data, dict) or set(data) != {"version", "peers"} or data["version"] != 1:
            return []
        if not isinstance(data["peers"], list) or len(data["peers"]) > 8:
            return []
        peers = [validate_peer(raw) for raw in data["peers"]]
        for key in ("peer_id", "credential_sha256", "pair_id", "route_id", "inbound_route_id"):
            if len({peer[key] for peer in peers}) != len(peers):
                return []
        if len({p[k] for p in peers for k in ("route_id", "inbound_route_id")}) != 2 * len(peers):
            return []
        return peers
    except (OSError, ValueError, UnicodeError, TypeError, RecursionError):
        return []


def authenticate(path: Path | None, credential: str) -> dict:
    if not isinstance(credential, str) or not 32 <= len(credential) <= 512 or not credential.isascii():
        raise PeerUnauthorized("External peer credential is invalid")
    digest = hashlib.sha256(credential.encode()).hexdigest()
    matches = [peer for peer in load_peers(path)
               if peer["enabled"] and hmac.compare_digest(peer["credential_sha256"], digest)]
    if len(matches) != 1:
        raise PeerUnauthorized("External peer credential is invalid")
    return matches[0]


def principal(path: Path | None, peer_id: str) -> dict | None:
    return next((peer for peer in load_peers(path) if peer["peer_id"] == peer_id and peer["enabled"]), None)


def route(peer: dict) -> dict:
    return {"route_id": peer["route_id"], "revision": peer["revision"],
            "alias": "external-" + peer["peer_id"][8:20], "target_session_id": peer["peer_id"],
            "target_kind": "external_peer", "actions": ["instruction"],
            "pair_id": peer["pair_id"], "paired_route_id": peer["inbound_route_id"],
            "created_at": peer["policy"]["approved_at"], "updated_at": peer["policy"]["approved_at"]}


def routes(path: Path | None, session_id: str) -> list[dict]:
    return [route(peer) for peer in load_peers(path)
            if peer["enabled"] and peer["target_session_id"] == session_id]


def live_route(path: Path | None, session_id: str, issued: dict) -> dict | None:
    peer = principal(path, str(issued.get("target_session_id") or ""))
    if peer is None or peer["target_session_id"] != session_id:
        return None
    current = route(peer)
    return current if all(issued.get(key) == value for key, value in current.items()) else None


def _admission_provenance(peer: dict, admission_id: str) -> dict:
    return {"version": 2, "origin_kind": "external_peer", "external_peer_id": peer["peer_id"],
            "source": peer["display_name"], "delegated_by": peer["policy"]["delegated_by"],
            "target_session_id": peer["target_session_id"], "external_admission_id": admission_id,
            "reference_action": "external_peer_policy", "policy_revision": peer["revision"],
            "approval_id": peer["policy"]["approval_id"], "approved_at": peer["policy"]["approved_at"],
            "source_user_instruction": peer["policy"]["source_user_instruction"]}


def accept(connection: sqlite3.Connection, path: Path, credential: str,
           expected_peer: dict, *, text: str, idempotency_key: str, now: str) -> tuple[dict, bool]:
    if not connection.in_transaction:
        raise RuntimeError("External admission requires the caller's transaction")
    peer = authenticate(path, credential)
    if peer != expected_peer:
        raise PeerUnauthorized("External peer policy changed")
    _text(text)
    if len(text.encode()) > 64 * 1024:
        raise ValueError("External message is too large")
    if (not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128
            or not idempotency_key.isascii() or not idempotency_key.isprintable()):
        raise ValueError("Invalid external idempotency key")
    # External admission has no provider run. A separately namespaced durable
    # request identity avoids inventing one to satisfy the old envelope index.
    key = "external:" + hashlib.sha256(json.dumps(
        [peer["peer_id"], peer["revision"], idempotency_key], separators=(",", ":")).encode()).hexdigest()
    prior = connection.execute("SELECT * FROM cross_chat_envelopes WHERE source_run_id='' AND idempotency_key=?", (key,)).fetchone()
    if prior is not None:
        prior = dict(prior)
        if (prior["body"] != text or prior["source_session_id"] != peer["peer_id"]
                or prior["target_session_id"] != peer["target_session_id"]
                or prior["authorization_pair_id"] != peer["pair_id"]):
            raise PeerConflict("Idempotency key already used for another message")
        return prior, False
    message_id = "handoff_" + uuid.uuid4().hex
    admission_id = "extadmission_" + uuid.uuid4().hex
    connection.execute("""INSERT INTO cross_chat_envelopes
        (id,kind,source_session_id,source_run_id,target_session_id,action,body,idempotency_key,
         authorization_kind,authorization_route_id,authorization_pair_id,status,created_at,updated_at,delivery_mode)
        VALUES(?,'instruction',?,'',?,'instruction',?,?,'configured_route',?,?,'stored',?,?,'mailbox')""",
        (message_id, peer["peer_id"], peer["target_session_id"], text, key,
         peer["inbound_route_id"], peer["pair_id"], now, now))
    connection.execute("INSERT INTO external_chat_admissions(message_id,admission_id,provenance_json) VALUES(?,?,?)",
                       (message_id, admission_id, json.dumps(_admission_provenance(peer, admission_id), ensure_ascii=False)))
    chat_mailbox.store_message(connection, message_id, now=now)
    return dict(connection.execute("SELECT * FROM cross_chat_envelopes WHERE id=?", (message_id,)).fetchone()), True


def _cursor(peer: dict, sequence: int) -> str:
    data = json.dumps([peer["peer_id"], peer["revision"], peer["pair_id"], sequence], separators=(",", ":")).encode()
    signature = hmac.new(bytes.fromhex(peer["credential_sha256"]), data, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(data + signature).decode().rstrip("=")


def _sequence(peer: dict, cursor: str) -> int:
    if not cursor:
        return 0
    try:
        if not isinstance(cursor, str) or len(cursor) > 1024 or not re.fullmatch(r"[A-Za-z0-9_-]+", cursor):
            raise ValueError()
        decoded = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if base64.urlsafe_b64encode(decoded).decode().rstrip("=") != cursor:
            raise ValueError()
        data, signature = decoded[:-32], decoded[-32:]
        expected = hmac.new(bytes.fromhex(peer["credential_sha256"]), data, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError()
        value = json.loads(data)
        if (not isinstance(value, list) or len(value) != 4
                or value[:3] != [peer["peer_id"], peer["revision"], peer["pair_id"]]
                or type(value[3]) is not int or not 0 <= value[3] <= (1 << 63) - 1):
            raise ValueError()
        return value[3]
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid external reply cursor") from None


def replies(connection: sqlite3.Connection, path: Path, credential: str, expected_peer: dict,
            *, since: str = "", limit: int = 25) -> dict:
    peer = authenticate(path, credential)
    if peer != expected_peer:
        raise PeerUnauthorized("External peer policy changed")
    sequence = _sequence(peer, since)
    page = chat_mailbox.list_messages(connection, peer["peer_id"], peer["target_session_id"],
                                      [peer["pair_id"]], after_seq=sequence, limit=limit)
    messages = page["messages"]
    return {"messages": [{"message_id": row["message_id"], "text": row["body"],
                           "created_at": row["created_at"], "reply_to": row["in_reply_to_message_id"]}
                          for row in messages],
            "next_cursor": _cursor(peer, messages[-1]["mailbox_seq"] if messages else sequence),
            "has_more": bool(page["has_more"])}
