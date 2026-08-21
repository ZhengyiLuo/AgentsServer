"""Fail-closed Team Hub bootstrap, invitation, and node-enrollment primitives."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import binascii
import hashlib
import hmac
import re
import secrets
import sqlite3
import struct
import time
from typing import Iterator


INVITATION_MAX_TTL_SECONDS = 24 * 60 * 60
NODE_ENROLLMENT_MAX_TTL_SECONDS = 15 * 60
MIN_TTL_SECONDS = 30
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
IDENTITY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{7,239}$")


class AuthenticationError(RuntimeError):
    """Raised for an invalid, expired, revoked, or already-used credential."""


class AuthorizationError(RuntimeError):
    """Raised when a principal lacks the required active team role."""


@dataclass(frozen=True)
class IssuedSecret:
    """A one-time secret returned only at issuance; only its hash is persisted."""

    id: str
    token: str = field(repr=False, compare=False)
    expires_at: int


@dataclass(frozen=True)
class BootstrapResult:
    human_principal_id: str
    team_id: str
    membership_id: str
    created: bool


@dataclass(frozen=True)
class EnrollmentResult:
    team_id: str
    node_id: str
    principal_id: str
    credential_id: str


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(16)}"


def _timestamp(now: int | None) -> int:
    value = int(time.time()) if now is None else int(now)
    if value < 0:
        raise ValueError("timestamp must be non-negative")
    return value


def _bounded_text(value: str, field: str, minimum: int, maximum: int) -> str:
    normalized = value.strip()
    if not minimum <= len(normalized) <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum} characters")
    return normalized


def _email(value: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) > 320 or EMAIL_PATTERN.fullmatch(normalized) is None:
        raise ValueError("email is invalid")
    return normalized


def _identity(value: str) -> str:
    normalized = value.strip()
    if IDENTITY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("server identity is invalid")
    return normalized


def _ttl(value: int, maximum: int) -> int:
    ttl = int(value)
    if not MIN_TTL_SECONDS <= ttl <= maximum:
        raise ValueError(f"ttl_seconds must be between {MIN_TTL_SECONDS} and {maximum}")
    return ttl


def _token_digest(token: str) -> bytes:
    if not 16 <= len(token) <= 512:
        # Still hash a fixed value before failing so malformed tokens do not take a
        # uniquely cheap path at the credential comparison boundary.
        hashlib.sha256(b"invalid-team-hub-token").digest()
        raise AuthenticationError("credential is invalid or unavailable")
    return hashlib.sha256(token.encode("utf-8")).digest()


def _new_secret() -> tuple[str, bytes]:
    token = secrets.token_urlsafe(32)
    return token, hashlib.sha256(token.encode("ascii")).digest()


def _canonical_ed25519_public_key(public_material: str) -> tuple[str, bytes]:
    """Parse one OpenSSH Ed25519 public key and remove its optional comment."""

    value = _bounded_text(public_material, "public_material", 32, 16384)
    if "PRIVATE KEY" in value.upper():
        raise ValueError("private key material is not accepted")
    parts = value.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise ValueError("ed25519 credential must be an OpenSSH public key")
    try:
        wire = base64.b64decode(parts[1].encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("ed25519 public key encoding is invalid") from exc

    def read_field(offset: int) -> tuple[bytes, int]:
        if len(wire) - offset < 4:
            raise ValueError("ed25519 public key encoding is truncated")
        length = struct.unpack(">I", wire[offset : offset + 4])[0]
        start = offset + 4
        end = start + length
        if end > len(wire):
            raise ValueError("ed25519 public key encoding is truncated")
        return wire[start:end], end

    algorithm, offset = read_field(0)
    key_bytes, offset = read_field(offset)
    if algorithm != b"ssh-ed25519" or len(key_bytes) != 32 or offset != len(wire):
        raise ValueError("ed25519 public key encoding is invalid")
    encoded = base64.b64encode(wire).decode("ascii")
    return f"ssh-ed25519 {encoded}", hashlib.sha256(wire).digest()


@contextmanager
def _write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Use an immediate transaction, nesting safely inside caller transactions."""

    if connection.in_transaction:
        savepoint = f"team_hub_{secrets.token_hex(8)}"
        connection.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        except BaseException:
            connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        return

    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _require_team_role(
    connection: sqlite3.Connection,
    team_id: str,
    principal_id: str,
    allowed_roles: tuple[str, ...],
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT m.id, m.role
        FROM memberships AS m
        JOIN principals AS p ON p.id = m.principal_id
        WHERE m.team_id = ?
          AND m.principal_id = ?
          AND m.status = 'active'
          AND p.status = 'active'
        """,
        (team_id, principal_id),
    ).fetchone()
    if row is None or row["role"] not in allowed_roles:
        raise AuthorizationError("active team role does not permit this operation")
    return row


def bootstrap_personal_team(
    connection: sqlite3.Connection,
    email: str,
    display_name: str,
    *,
    now: int | None = None,
) -> BootstrapResult:
    """Idempotently create one human principal and one personal team."""

    email_normalized = _email(email)
    name = _bounded_text(display_name, "display_name", 1, 160)
    timestamp = _timestamp(now)
    with _write_transaction(connection):
        existing = connection.execute(
            """
            SELECT h.principal_id, t.id AS team_id, m.id AS membership_id
            FROM human_accounts AS h
            JOIN principals AS p ON p.id = h.principal_id
            JOIN teams AS t ON t.personal_owner_principal_id = h.principal_id
            JOIN memberships AS m
              ON m.team_id = t.id AND m.principal_id = h.principal_id
            WHERE h.email_normalized = ?
              AND t.kind = 'personal'
              AND m.role = 'owner'
              AND m.status = 'active'
              AND p.status = 'active'
            """,
            (email_normalized,),
        ).fetchone()
        if existing is not None:
            return BootstrapResult(
                human_principal_id=existing["principal_id"],
                team_id=existing["team_id"],
                membership_id=existing["membership_id"],
                created=False,
            )

        orphan = connection.execute(
            "SELECT principal_id FROM human_accounts WHERE email_normalized = ?",
            (email_normalized,),
        ).fetchone()
        if orphan is not None:
            raise AuthenticationError("existing account has no valid personal team")

        principal_id = _id("human")
        team_id = _id("team")
        membership_id = _id("membership")
        team_display_name = f"{name[:153]}'s Team"
        connection.execute(
            """
            INSERT INTO principals(id, kind, display_name, created_at, updated_at)
            VALUES (?, 'human', ?, ?, ?)
            """,
            (principal_id, name, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO human_accounts(principal_id, email_normalized, created_at)
            VALUES (?, ?, ?)
            """,
            (principal_id, email_normalized, timestamp),
        )
        connection.execute(
            """
            INSERT INTO teams(
                id, kind, slug, display_name, personal_owner_principal_id,
                created_by_principal_id, created_at, updated_at
            ) VALUES (?, 'personal', ?, ?, ?, ?, ?, ?)
            """,
            (
                team_id,
                f"personal-{team_id[-24:]}",
                team_display_name,
                principal_id,
                principal_id,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO memberships(
                id, team_id, principal_id, role, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'owner', 'active', ?, ?)
            """,
            (membership_id, team_id, principal_id, timestamp, timestamp),
        )
        return BootstrapResult(principal_id, team_id, membership_id, True)


def record_legacy_server_binding(
    connection: sqlite3.Connection,
    team_id: str,
    server_identity: str,
    bound_by_principal_id: str,
    *,
    now: int | None = None,
) -> str:
    """Bind a stable legacy server identity without importing its bearer secret."""

    identity = _identity(server_identity)
    timestamp = _timestamp(now)
    with _write_transaction(connection):
        _require_team_role(
            connection, team_id, bound_by_principal_id, ("owner", "admin")
        )
        existing = connection.execute(
            "SELECT id, team_id FROM legacy_server_bindings WHERE server_identity = ?",
            (identity,),
        ).fetchone()
        if existing is not None:
            if not hmac.compare_digest(str(existing["team_id"]), team_id):
                raise AuthorizationError("server identity is already bound to another team")
            return str(existing["id"])
        enrolled_node = connection.execute(
            "SELECT id, team_id FROM nodes WHERE server_identity = ?", (identity,)
        ).fetchone()
        if enrolled_node is not None and not hmac.compare_digest(
            str(enrolled_node["team_id"]), team_id
        ):
            raise AuthorizationError("server identity is enrolled in another team")
        binding_id = _id("legacy_binding")
        connection.execute(
            """
            INSERT INTO legacy_server_bindings(
                id, team_id, server_identity, node_id, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                binding_id,
                team_id,
                identity,
                enrolled_node["id"] if enrolled_node is not None else None,
                timestamp,
            ),
        )
        return binding_id


def issue_invitation(
    connection: sqlite3.Connection,
    team_id: str,
    issued_by_principal_id: str,
    role: str,
    *,
    invitee_email: str,
    ttl_seconds: int = 15 * 60,
    now: int | None = None,
) -> IssuedSecret:
    """Issue a one-time, short-lived invitation as an owner or admin."""

    if role not in ("admin", "member", "guest"):
        raise ValueError("invitation role must be admin, member, or guest")
    email_normalized = _email(invitee_email)
    timestamp = _timestamp(now)
    expires_at = timestamp + _ttl(ttl_seconds, INVITATION_MAX_TTL_SECONDS)
    token, token_hash = _new_secret()
    invitation_id = _id("invite")
    with _write_transaction(connection):
        _require_team_role(
            connection, team_id, issued_by_principal_id, ("owner", "admin")
        )
        connection.execute(
            """
            INSERT INTO invitations(
                id, team_id, token_hash, invitee_email_normalized, role,
                issued_by_principal_id, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                invitation_id,
                team_id,
                token_hash,
                email_normalized,
                role,
                issued_by_principal_id,
                expires_at,
                timestamp,
            ),
        )
    return IssuedSecret(invitation_id, token, expires_at)


def redeem_invitation(
    connection: sqlite3.Connection,
    token: str,
    human_principal_id: str,
    *,
    now: int | None = None,
) -> str:
    """Atomically consume an invitation and create its exact membership."""

    token_hash = _token_digest(token)
    timestamp = _timestamp(now)
    with _write_transaction(connection):
        invitation = connection.execute(
            """
            SELECT i.id, i.team_id, i.token_hash, i.invitee_email_normalized,
                   i.role, i.issued_by_principal_id, i.expires_at,
                   i.redeemed_at, i.revoked_at
            FROM invitations AS i
            JOIN memberships AS issuer
              ON issuer.team_id = i.team_id
             AND issuer.principal_id = i.issued_by_principal_id
             AND issuer.status = 'active'
             AND issuer.role IN ('owner', 'admin')
            JOIN principals AS issuer_principal
              ON issuer_principal.id = issuer.principal_id
             AND issuer_principal.status = 'active'
            WHERE i.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if invitation is None or not hmac.compare_digest(invitation["token_hash"], token_hash):
            raise AuthenticationError("credential is invalid or unavailable")
        if (
            invitation["redeemed_at"] is not None
            or invitation["revoked_at"] is not None
            or int(invitation["expires_at"]) <= timestamp
        ):
            raise AuthenticationError("credential is invalid or unavailable")
        human = connection.execute(
            """
            SELECT h.email_normalized
            FROM human_accounts AS h
            JOIN principals AS p ON p.id = h.principal_id
            WHERE h.principal_id = ? AND p.status = 'active'
            """,
            (human_principal_id,),
        ).fetchone()
        if human is None:
            raise AuthenticationError("human account is unavailable")
        bound_email = invitation["invitee_email_normalized"]
        if bound_email is None or str(bound_email) != str(human["email_normalized"]):
            raise AuthenticationError("credential is invalid or unavailable")

        existing = connection.execute(
            """
            SELECT id, role, status FROM memberships
            WHERE team_id = ? AND principal_id = ?
            """,
            (invitation["team_id"], human_principal_id),
        ).fetchone()
        if existing is not None:
            if existing["status"] != "active" or existing["role"] != invitation["role"]:
                raise AuthorizationError("existing membership conflicts with invitation")
            membership_id = str(existing["id"])
        else:
            membership_id = _id("membership")
            connection.execute(
                """
                INSERT INTO memberships(
                    id, team_id, principal_id, role, status,
                    invited_by_principal_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    membership_id,
                    invitation["team_id"],
                    human_principal_id,
                    invitation["role"],
                    invitation["issued_by_principal_id"],
                    timestamp,
                    timestamp,
                ),
            )
        changed = connection.execute(
            """
            UPDATE invitations
            SET redeemed_at = ?, redeemed_by_principal_id = ?
            WHERE id = ? AND redeemed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
            """,
            (timestamp, human_principal_id, invitation["id"], timestamp),
        ).rowcount
        if changed != 1:
            raise AuthenticationError("credential is invalid or unavailable")
        return membership_id


def issue_node_enrollment(
    connection: sqlite3.Connection,
    team_id: str,
    issued_by_principal_id: str,
    *,
    ttl_seconds: int = 5 * 60,
    now: int | None = None,
) -> IssuedSecret:
    """Issue a distinct one-time node enrollment credential."""

    timestamp = _timestamp(now)
    expires_at = timestamp + _ttl(ttl_seconds, NODE_ENROLLMENT_MAX_TTL_SECONDS)
    token, token_hash = _new_secret()
    grant_id = _id("node_grant")
    with _write_transaction(connection):
        _require_team_role(
            connection, team_id, issued_by_principal_id, ("owner", "admin")
        )
        connection.execute(
            """
            INSERT INTO node_enrollment_grants(
                id, team_id, token_hash, issued_by_principal_id,
                created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (grant_id, team_id, token_hash, issued_by_principal_id, timestamp, expires_at),
        )
    return IssuedSecret(grant_id, token, expires_at)


def redeem_node_enrollment(
    connection: sqlite3.Connection,
    token: str,
    server_identity: str,
    display_name: str,
    credential_kind: str,
    public_material: str,
    *,
    now: int | None = None,
) -> EnrollmentResult:
    """Atomically consume a grant and enroll a public node credential."""

    token_hash = _token_digest(token)
    identity = _identity(server_identity)
    name = _bounded_text(display_name, "display_name", 1, 160)
    if credential_kind != "ed25519":
        raise ValueError("credential_kind must be ed25519")
    public_key, fingerprint = _canonical_ed25519_public_key(public_material)
    timestamp = _timestamp(now)

    with _write_transaction(connection):
        grant = connection.execute(
            """
            SELECT grant.id, grant.team_id, grant.token_hash, grant.expires_at,
                   grant.consumed_at, grant.revoked_at
            FROM node_enrollment_grants AS grant
            JOIN memberships AS issuer
              ON issuer.team_id = grant.team_id
             AND issuer.principal_id = grant.issued_by_principal_id
             AND issuer.status = 'active'
             AND issuer.role IN ('owner', 'admin')
            JOIN principals AS issuer_principal
              ON issuer_principal.id = issuer.principal_id
             AND issuer_principal.status = 'active'
            WHERE grant.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if grant is None or not hmac.compare_digest(grant["token_hash"], token_hash):
            raise AuthenticationError("credential is invalid or unavailable")
        if (
            grant["consumed_at"] is not None
            or grant["revoked_at"] is not None
            or int(grant["expires_at"]) <= timestamp
        ):
            raise AuthenticationError("credential is invalid or unavailable")

        principal_id = _id("node_principal")
        node_id = _id("node")
        credential_id = _id("node_credential")
        team_id = str(grant["team_id"])
        binding = connection.execute(
            """
            SELECT team_id, node_id FROM legacy_server_bindings
            WHERE server_identity = ?
            """,
            (identity,),
        ).fetchone()
        if binding is not None and (
            not hmac.compare_digest(str(binding["team_id"]), team_id)
            or binding["node_id"] is not None
        ):
            raise AuthorizationError("server identity is unavailable for enrollment")
        connection.execute(
            """
            INSERT INTO principals(
                id, kind, scope_team_id, display_name, created_at, updated_at
            ) VALUES (?, 'node', ?, ?, ?, ?)
            """,
            (principal_id, team_id, name, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO nodes(
                id, team_id, principal_id, server_identity,
                display_name, enrolled_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (node_id, team_id, principal_id, identity, name, timestamp),
        )
        connection.execute(
            """
            INSERT INTO node_credentials(
                id, team_id, node_id, credential_kind,
                public_material, fingerprint_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                credential_id,
                team_id,
                node_id,
                credential_kind,
                public_key,
                fingerprint,
                timestamp,
            ),
        )
        if binding is not None:
            connection.execute(
                """
                UPDATE legacy_server_bindings SET node_id = ?
                WHERE team_id = ? AND server_identity = ? AND node_id IS NULL
                """,
                (node_id, team_id, identity),
            )
        changed = connection.execute(
            """
            UPDATE node_enrollment_grants
            SET consumed_at = ?, consumed_by_node_id = ?
            WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
            """,
            (timestamp, node_id, grant["id"], timestamp),
        ).rowcount
        if changed != 1:
            raise AuthenticationError("credential is invalid or unavailable")
        return EnrollmentResult(team_id, node_id, principal_id, credential_id)
