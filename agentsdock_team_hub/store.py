"""Transactional application service for the runnable Team Hub V1 API."""

from __future__ import annotations

from dataclasses import dataclass
import base64
import binascii
from contextlib import contextmanager, suppress
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import stat
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - Team Hub storage is Unix-only in V1.
    fcntl = None  # type: ignore[assignment]

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .auth import (
    AuthenticationError,
    AuthorizationError,
    _bounded_text,
    _canonical_ed25519_public_key,
    _email,
    _id,
    _identity,
    _require_team_role,
    _token_digest,
    _ttl,
    _write_transaction,
    bootstrap_personal_team,
    issue_invitation,
    issue_node_enrollment,
    redeem_invitation,
)
from .database import LATEST_SCHEMA_VERSION, MIGRATIONS, open_database
from .security import (
    ACCESS_TOKEN_TTL_SECONDS,
    BOOTSTRAP_PROOF_TTL_SECONDS,
    SESSION_TTL_SECONDS,
    AccessTokenSigner,
    TokenError,
    canonical_fingerprint,
    canonical_json,
    create_secret_file,
    ensure_private_directory,
    load_or_create_signing_key,
    now_seconds,
    opaque_secret,
    read_secret_file,
    token_hash,
)


NODE_CHALLENGE_TTL_SECONDS = 2 * 60
RECOVERY_PROOF_TTL_SECONDS = 10 * 60
TAILNET_BOOTSTRAP_PROOF_TTL_SECONDS = 5 * 60
MAX_BOOTSTRAP_DELEGATION_LEDGER_ROWS = 256
MAX_NETWORK_AGENTS_PER_SERVER = 256
MAX_NETWORK_BODY_BYTES = 8_192
MAX_NETWORK_PAGE_ITEMS = 100
MAX_SECURE_PEER_BINDING_LOOKUP_IDS = 512
SECURE_PEER_HEARTBEAT_WRITE_INTERVAL_SECONDS = 15
# Secure-peer responses are hard-capped at 2 MiB. Keep paged network payloads
# below that transport ceiling, including JSON escaping and envelope fields.
MAX_NETWORK_PAGE_RESPONSE_BYTES = 1_900_000
LOCAL_CONTROL_PRINCIPAL_ID = "service_local_control"


class HubError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class AccessClaims:
    principal_id: str
    session_id: str
    jti: str
    expires_at: int
    auth_kind: str = "human"
    team_id: str | None = None
    scopes: frozenset[str] = frozenset()
    peer_id: str | None = None


def _now(value: int | None = None) -> int:
    return now_seconds() if value is None else int(value)


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _iso8601(value: int | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _maintenance_operation_id(value: str) -> str:
    operation_id = _bounded_text(value, "operation_id", 1, 128)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", operation_id) is None:
        raise ValueError("operation_id is invalid")
    return operation_id


class HubStore:
    """One authoritative SQLite-backed Hub with per-operation connections."""

    @staticmethod
    def _open_lock_file(path: Path) -> int:
        ensure_private_directory(path.parent)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, 0o600)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
            ):
                raise PermissionError("Team Hub lock file is unsafe")
            os.fchmod(descriptor, 0o600)
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @classmethod
    def acquire_managed_runtime_lease(cls, data_dir: Path) -> int:
        """Acquire the one-process lease required by an embedded listener."""

        if fcntl is None:
            raise RuntimeError("managed Team Hub host locking is unavailable")
        root = Path(os.path.abspath(os.path.expanduser(os.fspath(data_dir))))
        # Keep the lease beside, not inside, the Hub directory so a foreign
        # copied bound database can be rejected without changing its file set.
        descriptor = cls._open_lock_file(
            root.parent / f".{root.name}.managed-host.lock"
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError("another managed Team Hub host is already active") from exc
        return descriptor

    @staticmethod
    def release_managed_runtime_lease(descriptor: int | None) -> None:
        if descriptor is None:
            return
        if fcntl is None:
            os.close(descriptor)
            return
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    @classmethod
    @contextmanager
    def maintenance_control_lock(
        cls,
        data_dir: Path,
        *,
        timeout_seconds: float = 5.0,
    ):
        """Serialize local proof control, snapshots, and offline restores."""

        if fcntl is None:
            raise RuntimeError("Team Hub local-control locking is unavailable")
        root = Path(os.path.abspath(os.path.expanduser(os.fspath(data_dir))))
        descriptor = cls._open_lock_file(root / "maintenance-control.lock")
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Team Hub local control is busy")
                    time.sleep(0.025)
            yield
        finally:
            with suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def __init__(
        self,
        data_dir: Path,
        *,
        now: int | None = None,
        managed_host_identity: str | None = None,
        managed_server_instance_id: str | None = None,
        allow_bound_control: bool = False,
    ) -> None:
        self.data_dir = Path(os.path.abspath(os.path.expanduser(os.fspath(data_dir))))
        self.database_path = self.data_dir / "team-hub.sqlite3"
        self.signing_key_path = self.data_dir / "access-token-signing.key"
        self.bootstrap_proof_path = self.data_dir / "bootstrap-owner.proof"
        self.maintenance_fence_path = self.data_dir / "maintenance-fence.json"
        self.managed_host_identity: str | None = None
        self.managed_server_instance_id = (
            _identity(managed_server_instance_id)
            if managed_server_instance_id is not None
            else None
        )
        ensure_private_directory(self.data_dir)
        self.instance_id = _id("hub_instance")
        self.hub_id = ""
        expected_host = (
            _identity(managed_host_identity)
            if managed_host_identity is not None
            else None
        )
        self._preflight_managed_host_binding(
            expected_host,
            allow_bound_control=allow_bound_control,
        )
        self._initialize(
            _now(now),
            expected_host,
            allow_bound_control=allow_bound_control,
        )
        # Host binding is checked or won transactionally before a missing key
        # is created. A foreign copied database therefore cannot mutate local
        # credential state merely by attempting activation.
        self.signer = AccessTokenSigner(load_or_create_signing_key(self.signing_key_path))

    def _preflight_managed_host_binding(
        self,
        expected_host_identity: str | None,
        *,
        allow_bound_control: bool,
    ) -> None:
        """Reject a foreign bound database before WAL setup or migrations."""

        try:
            info = self.database_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("Team Hub database must be a single-link regular file")
        if info.st_size == 0:
            return
        bound_identity = self._read_managed_binding_without_source_mutation()
        if bound_identity is None:
            return
        if expected_host_identity is not None:
            if bound_identity != expected_host_identity:
                raise RuntimeError(
                    "Team Hub database is bound to a different AgentsServer host"
                )
        elif not allow_bound_control:
            raise RuntimeError("managed Team Hub databases cannot be served standalone")

    @staticmethod
    def _copy_private_regular_file(source: Path, destination: Path) -> tuple[int, ...]:
        """Copy one stable owner-only file without following source links."""

        read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        read_flags |= getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, read_flags)
        destination_descriptor = -1
        try:
            before = os.fstat(source_descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600
            ):
                raise PermissionError(
                    "Team Hub database files must be owner-only regular files"
                )
            write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            write_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            destination_descriptor = os.open(destination, write_flags, 0o600)
            while True:
                chunk = os.read(source_descriptor, 1024 * 1024)
                if not chunk:
                    break
                offset = 0
                while offset < len(chunk):
                    offset += os.write(destination_descriptor, chunk[offset:])
            os.fsync(destination_descriptor)
            after = os.fstat(source_descriptor)
            linked = os.stat(source, follow_symlinks=False)
            signature = (
                int(before.st_dev),
                int(before.st_ino),
                int(before.st_size),
                int(before.st_mtime_ns),
            )
            if signature != (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
            ) or signature != (
                int(linked.st_dev),
                int(linked.st_ino),
                int(linked.st_size),
                int(linked.st_mtime_ns),
            ):
                raise RuntimeError("Team Hub database changed during host-binding preflight")
            return signature
        finally:
            if destination_descriptor >= 0:
                os.close(destination_descriptor)
            os.close(source_descriptor)

    def _read_managed_binding_without_source_mutation(self) -> str | None:
        """Read the main DB plus any live WAL through a private stable copy."""

        wal_path = self.database_path.with_name(self.database_path.name + "-wal")
        for _attempt in range(3):
            with tempfile.TemporaryDirectory(prefix="team-hub-binding-preflight-") as root:
                copied = Path(root) / self.database_path.name
                try:
                    database_signature = self._copy_private_regular_file(
                        self.database_path, copied
                    )
                    wal_existed = False
                    try:
                        self._copy_private_regular_file(
                            wal_path, copied.with_name(copied.name + "-wal")
                        )
                        wal_existed = True
                    except FileNotFoundError:
                        pass
                    latest_database = self.database_path.lstat()
                    latest_signature = (
                        int(latest_database.st_dev),
                        int(latest_database.st_ino),
                        int(latest_database.st_size),
                        int(latest_database.st_mtime_ns),
                    )
                    if latest_signature != database_signature:
                        continue
                    try:
                        wal_now_exists = wal_path.lstat().st_size >= 0
                    except FileNotFoundError:
                        wal_now_exists = False
                    if wal_now_exists != wal_existed:
                        continue
                    connection = sqlite3.connect(str(copied), isolation_level=None)
                    try:
                        table = connection.execute(
                            """
                            SELECT 1 FROM sqlite_master
                            WHERE type = 'table' AND name = 'managed_host_bindings'
                            """
                        ).fetchone()
                        if table is None:
                            return None
                        binding = connection.execute(
                            """
                            SELECT server_identity
                            FROM managed_host_bindings WHERE singleton = 1
                            """
                        ).fetchone()
                        return str(binding[0]) if binding is not None else None
                    finally:
                        connection.close()
                except RuntimeError:
                    continue
                except sqlite3.DatabaseError as exc:
                    raise RuntimeError(
                        "Team Hub host-binding preflight could not verify the database"
                    ) from exc
        raise RuntimeError("Team Hub host-binding preflight could not obtain a stable snapshot")

    @staticmethod
    def _sha256_private_regular_file(path: Path) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError("Team Hub snapshot file is not owner-only")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            return digest.hexdigest()
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_private_regular_file(
        path: Path,
        *,
        minimum_bytes: int = 1,
        maximum_bytes: int = 1024 * 1024,
    ) -> bytes:
        """Read a bounded owner-only file without following or racing links."""

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or not minimum_bytes <= info.st_size <= maximum_bytes
            ):
                raise PermissionError("Team Hub snapshot file is invalid")
            chunks: list[bytes] = []
            remaining = maximum_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 1024 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            value = b"".join(chunks)
            if not minimum_bytes <= len(value) <= maximum_bytes:
                raise PermissionError("Team Hub snapshot file is invalid")
            linked = os.stat(path, follow_symlinks=False)
            after = os.fstat(descriptor)
            if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
            ) or (info.st_dev, info.st_ino) != (linked.st_dev, linked.st_ino):
                raise RuntimeError("Team Hub snapshot file changed while reading")
            return value
        finally:
            os.close(descriptor)

    @staticmethod
    def _validate_private_directory_without_mutation(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700
            ):
                raise PermissionError("Team Hub snapshot directory is not owner-only")
        finally:
            os.close(descriptor)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def connect(self) -> sqlite3.Connection:
        return open_database(self.database_path)

    def _initialize(
        self,
        timestamp: int,
        expected_host_identity: str | None,
        *,
        allow_bound_control: bool,
    ) -> None:
        connection = self.connect()
        try:
            with _write_transaction(connection):
                row = connection.execute(
                    "SELECT hub_id FROM hub_metadata WHERE singleton = 1"
                ).fetchone()
                if row is None:
                    self.hub_id = _id("hub")
                    connection.execute(
                        "INSERT INTO hub_metadata(singleton, hub_id, created_at) VALUES (1, ?, ?)",
                        (self.hub_id, timestamp),
                    )
                else:
                    self.hub_id = str(row["hub_id"])
                binding = connection.execute(
                    """
                    SELECT hub_id, server_identity
                    FROM managed_host_bindings WHERE singleton = 1
                    """
                ).fetchone()
                if expected_host_identity is not None:
                    if binding is None:
                        connection.execute(
                            """
                            INSERT INTO managed_host_bindings(
                                singleton, hub_id, server_identity, created_at
                            ) VALUES (1, ?, ?, ?)
                            """,
                            (self.hub_id, expected_host_identity, timestamp),
                        )
                        self.managed_host_identity = expected_host_identity
                    elif (
                        str(binding["hub_id"]) != self.hub_id
                        or str(binding["server_identity"]) != expected_host_identity
                    ):
                        raise RuntimeError(
                            "Team Hub database is bound to a different AgentsServer host"
                        )
                    else:
                        self.managed_host_identity = expected_host_identity
                elif binding is not None:
                    if not allow_bound_control:
                        raise RuntimeError(
                            "managed Team Hub databases cannot be served standalone"
                        )
                    self.managed_host_identity = str(binding["server_identity"])
                self._validate_owner_invariants(connection)
                bootstrapped = self._is_bootstrapped(connection)
                if bootstrapped and self.managed_host_identity is not None:
                    team_owners = connection.execute(
                        """
                        SELECT t.id AS team_id,m.principal_id AS owner_principal_id
                        FROM teams AS t
                        JOIN memberships AS m
                          ON m.team_id=t.id AND m.role='owner' AND m.status='active'
                        ORDER BY t.created_at,t.id
                        """
                    ).fetchall()
                    # A managed AgentsServer identity is one logical server and
                    # cannot be silently attached to multiple team networks.
                    # Existing single-team databases are upgraded eagerly;
                    # multi-team stores bind only through an explicit peer/team
                    # approval path.
                    if len(team_owners) == 1:
                        team_owner = team_owners[0]
                        self._ensure_managed_host_node(
                            connection, team_owner["team_id"], timestamp
                        )
                        self._ensure_network_board(
                            connection,
                            team_owner["team_id"],
                            team_owner["owner_principal_id"],
                            timestamp,
                        )
                if not bootstrapped:
                    if not self._globally_empty(connection):
                        raise RuntimeError(
                            "Team Hub refuses a partial unbootstrapped identity database"
                        )
                    self._ensure_bootstrap_claim(connection, timestamp)
        finally:
            connection.close()

    @staticmethod
    def _is_bootstrapped(connection: sqlite3.Connection) -> bool:
        return bool(connection.execute("SELECT EXISTS(SELECT 1 FROM teams)").fetchone()[0])

    @staticmethod
    def _globally_empty(connection: sqlite3.Connection) -> bool:
        return all(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]) == 0
            for table in ("principals", "teams", "memberships", "device_sessions")
        )

    @staticmethod
    def _validate_owner_invariants(connection: sqlite3.Connection) -> None:
        invalid = connection.execute(
            """
            SELECT t.id
            FROM teams AS t
            LEFT JOIN memberships AS m
              ON m.team_id = t.id AND m.role = 'owner' AND m.status = 'active'
            LEFT JOIN principals AS p ON p.id = m.principal_id AND p.status = 'active'
            GROUP BY t.id
            HAVING count(m.id) <> 1 OR count(p.id) <> 1
            LIMIT 1
            """
        ).fetchone()
        if invalid is not None:
            raise RuntimeError(
                f"Team Hub refuses a team without exactly one active owner: {invalid['id']}"
            )

    def _read_local_proof(self, path: Path) -> str | None:
        try:
            value = read_secret_file(path).decode("ascii").strip()
        except (FileNotFoundError, UnicodeError, OSError, PermissionError):
            return None
        return value if 16 <= len(value) <= 512 else None

    def _ensure_bootstrap_claim(
        self,
        connection: sqlite3.Connection,
        timestamp: int,
        *,
        force_local: bool = False,
    ) -> None:
        active = connection.execute(
            """
            SELECT c.id, c.token_hash, c.expires_at,
                   d.server_instance_id AS delegated_server_instance_id
            FROM bootstrap_claims AS c
            LEFT JOIN bootstrap_delegations AS d ON d.bootstrap_claim_id = c.id
            WHERE c.consumed_at IS NULL AND c.revoked_at IS NULL AND c.expires_at > ?
            ORDER BY c.created_at DESC LIMIT 1
            """,
            (timestamp,),
        ).fetchone()
        local_proof = self._read_local_proof(self.bootstrap_proof_path)
        if (
            not force_local
            and active is not None
            and active["delegated_server_instance_id"] is not None
            and str(active["delegated_server_instance_id"])
            == self.managed_server_instance_id
            and local_proof is None
        ):
            return
        if (
            not force_local
            and active is not None
            and active["delegated_server_instance_id"] is None
            and local_proof is not None
            and hmac.compare_digest(active["token_hash"], token_hash(local_proof))
        ):
            return
        connection.execute(
            """
            UPDATE bootstrap_claims SET revoked_at = ?
            WHERE consumed_at IS NULL AND revoked_at IS NULL
            """,
            (timestamp,),
        )
        try:
            self.bootstrap_proof_path.unlink(missing_ok=True)
        except OSError as exc:
            raise PermissionError("cannot replace stale bootstrap proof") from exc
        proof, digest = opaque_secret("bootstrap")
        create_secret_file(self.bootstrap_proof_path, (proof + "\n").encode("ascii"))
        connection.execute(
            """
            INSERT INTO bootstrap_claims(
                id, token_hash, created_at, expires_at
            ) VALUES (?, ?, ?, ?)
            """,
            (_id("bootstrap_claim"), digest, timestamp, timestamp + BOOTSTRAP_PROOF_TTL_SECONDS),
        )

    def health(self) -> dict[str, Any]:
        connection = self.connect()
        try:
            bootstrapped = self._is_bootstrapped(connection)
            return {
                "ok": True,
                "service": "agentsdock-team-hub",
                "api_version": 1,
                "schema_version": LATEST_SCHEMA_VERSION,
                "hub_id": self.hub_id,
                "instance_id": self.instance_id,
                "bootstrapped": bootstrapped,
                "bootstrap_required": not bootstrapped,
                "capabilities": {
                    "team_network_v1": {
                        "available": True,
                        "version": 1,
                        "logical_servers": True,
                        "agent_registry": True,
                        "bulletin": True,
                        "mailbox": True,
                        "delivery_receipts": ["delivered", "read"],
                        "passive_requests": True,
                        "server_invites": False,
                        "skill_attachments": False,
                        "dispatch": False,
                        "max_agents_per_server": MAX_NETWORK_AGENTS_PER_SERVER,
                        "max_page_items": MAX_NETWORK_PAGE_ITEMS,
                        "max_body_bytes": MAX_NETWORK_BODY_BYTES,
                    }
                },
            }
        finally:
            connection.close()

    def maintenance_snapshot(self, reason: str, *, keep: int = 3) -> Path:
        with self.maintenance_control_lock(self.data_dir):
            return self._maintenance_snapshot_unlocked(reason, keep=keep)

    def maintenance_snapshot_and_fence(
        self,
        reason: str,
        *,
        operation_id: str,
        keep: int = 3,
    ) -> Path:
        """Create a snapshot and durably exclude local/HTTP writes afterward."""

        clean_operation_id = _maintenance_operation_id(operation_id)
        with self.maintenance_control_lock(self.data_dir):
            if self.maintenance_fence_path.exists():
                raise RuntimeError("Team Hub maintenance is already active")
            snapshot = self._maintenance_snapshot_unlocked(reason, keep=keep)
            marker = {
                "format": 1,
                "reason": _bounded_text(reason, "reason", 1, 80),
                "operation_id": clean_operation_id,
                "hub_id": self.hub_id,
                "host_server_identity": self.managed_host_identity,
                "snapshot": snapshot.name,
                "created_at": _iso8601(_now()),
            }
            create_secret_file(
                self.maintenance_fence_path,
                canonical_json(marker) + b"\n",
            )
            return snapshot

    def maintenance_fence(self) -> dict[str, Any] | None:
        try:
            raw = self._read_private_regular_file(
                self.maintenance_fence_path,
                maximum_bytes=16 * 1024,
            )
        except FileNotFoundError:
            return None
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Team Hub maintenance fence is invalid") from exc
        expected = {
            "format",
            "reason",
            "operation_id",
            "hub_id",
            "host_server_identity",
            "snapshot",
            "created_at",
        }
        if (
            not isinstance(value, dict)
            or set(value) != expected
            or value.get("format") != 1
            or value.get("hub_id") != self.hub_id
            or value.get("host_server_identity") != self.managed_host_identity
            or not isinstance(value.get("reason"), str)
            or not isinstance(value.get("operation_id"), str)
            or re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}",
                value["operation_id"],
            )
            is None
            or not isinstance(value.get("snapshot"), str)
            or re.fullmatch(r"snapshot_[A-Za-z0-9_]+", value["snapshot"]) is None
            or not isinstance(value.get("created_at"), str)
        ):
            raise RuntimeError("Team Hub maintenance fence is invalid")
        return value

    def clear_maintenance_fence(
        self,
        *,
        expected_reason: str,
        expected_operation_id: str,
        expected_snapshot: Path,
    ) -> bool:
        """Clear one exact completed/cancelled maintenance generation."""

        clean_reason = _bounded_text(expected_reason, "reason", 1, 80)
        clean_operation_id = _maintenance_operation_id(expected_operation_id)
        with self.maintenance_control_lock(self.data_dir):
            marker = self.maintenance_fence()
            if marker is None:
                return False
            if marker["reason"] != clean_reason:
                raise RuntimeError("Team Hub maintenance reason does not match")
            if marker["operation_id"] != clean_operation_id:
                raise RuntimeError("Team Hub maintenance operation does not match")
            if marker["snapshot"] != Path(expected_snapshot).name:
                raise RuntimeError("Team Hub maintenance snapshot does not match")
            self.maintenance_fence_path.unlink()
            self._fsync_directory(self.data_dir)
            return True

    @classmethod
    def _maintenance_fence_control_unlocked(
        cls,
        root: Path,
        *,
        expected_hub_id: str,
        expected_host_identity: str,
        expected_reason: str,
        expected_operation_id: str,
        expected_snapshot: Path,
    ) -> dict[str, Any] | None:
        marker_path = root / "maintenance-fence.json"
        try:
            raw = cls._read_private_regular_file(
                marker_path, maximum_bytes=16 * 1024
            )
        except FileNotFoundError:
            return None
        try:
            marker = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Team Hub maintenance fence is invalid") from exc
        expected_keys = {
            "format",
            "reason",
            "operation_id",
            "hub_id",
            "host_server_identity",
            "snapshot",
            "created_at",
        }
        if (
            not isinstance(marker, dict)
            or set(marker) != expected_keys
            or marker.get("format") != 1
            or marker.get("hub_id") != expected_hub_id
            or marker.get("host_server_identity") != expected_host_identity
            or marker.get("reason") != expected_reason
            or marker.get("operation_id") != _maintenance_operation_id(
                expected_operation_id
            )
            or marker.get("snapshot") != Path(expected_snapshot).name
        ):
            raise RuntimeError("Team Hub maintenance fence does not match")
        return marker

    @classmethod
    def maintenance_fence_matches_control(
        cls,
        data_dir: Path,
        *,
        expected_hub_id: str,
        expected_host_identity: str,
        expected_reason: str,
        expected_operation_id: str,
        expected_snapshot: Path,
    ) -> bool:
        """Check an exact fence without opening or migrating its database."""

        root = Path(os.path.abspath(os.path.expanduser(os.fspath(data_dir))))
        with cls.maintenance_control_lock(root):
            return cls._maintenance_fence_control_unlocked(
                root,
                expected_hub_id=expected_hub_id,
                expected_host_identity=expected_host_identity,
                expected_reason=expected_reason,
                expected_operation_id=expected_operation_id,
                expected_snapshot=expected_snapshot,
            ) is not None

    @classmethod
    def clear_maintenance_fence_control(
        cls,
        data_dir: Path,
        *,
        expected_hub_id: str,
        expected_host_identity: str,
        expected_reason: str,
        expected_operation_id: str,
        expected_snapshot: Path,
    ) -> bool:
        """Clear an exact fence without opening or migrating its database.

        The detached updater intentionally runs from the previous release and
        therefore may not understand a candidate's newer SQLite schema.
        """

        root = Path(os.path.abspath(os.path.expanduser(os.fspath(data_dir))))
        marker_path = root / "maintenance-fence.json"
        with cls.maintenance_control_lock(root):
            marker = cls._maintenance_fence_control_unlocked(
                root,
                expected_hub_id=expected_hub_id,
                expected_host_identity=expected_host_identity,
                expected_reason=expected_reason,
                expected_operation_id=expected_operation_id,
                expected_snapshot=expected_snapshot,
            )
            if marker is None:
                return False
            marker_path.unlink()
            cls._fsync_directory(root)
            return True

    def _maintenance_snapshot_unlocked(self, reason: str, *, keep: int = 3) -> Path:
        """Checkpoint and durably snapshot the bound Hub before replacement.

        SQLite's online backup captures the complete logical database after a
        successful WAL checkpoint. The signing key and a manifest containing
        only hashes and stable identities are written into the same private
        generation; the manifest is written last and the directory rename is
        the commit point. Existing verified generations are pruned only after
        the new generation is complete.
        """

        if self.managed_host_identity is None:
            raise RuntimeError("maintenance snapshots require a managed Hub binding")
        clean_reason = _bounded_text(reason, "reason", 1, 80)
        retained = max(1, min(int(keep), 10))
        backups = self.data_dir / "maintenance-backups"
        ensure_private_directory(backups)
        generation = f"snapshot_{time.time_ns():020d}_{secrets.token_hex(8)}"
        temporary = backups / f".{generation}.tmp"
        final = backups / generation
        ensure_private_directory(temporary)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = self.connect()
            snapshot_time = _now()
            # A delegated bootstrap proof is scoped to the live AgentsServer
            # instance. Maintenance may replace that instance, so revoke the
            # remote authority before the durable snapshot is taken. The
            # immutable delegation row remains as audit/idempotency evidence.
            with _write_transaction(source):
                source.execute(
                    """
                    UPDATE bootstrap_claims SET revoked_at = ?
                    WHERE consumed_at IS NULL AND revoked_at IS NULL
                      AND EXISTS (
                          SELECT 1 FROM bootstrap_delegations AS d
                          WHERE d.bootstrap_claim_id = bootstrap_claims.id
                      )
                    """,
                    (snapshot_time,),
                )
            checkpoint = source.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise RuntimeError("Team Hub WAL checkpoint could not drain")

            database_copy = temporary / "team-hub.sqlite3"
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(database_copy, flags, 0o600)
            os.close(descriptor)
            destination = sqlite3.connect(str(database_copy), isolation_level=None)
            destination.row_factory = sqlite3.Row
            source.backup(destination)
            integrity = str(destination.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError("Team Hub maintenance backup failed integrity verification")
            version = int(destination.execute("PRAGMA user_version").fetchone()[0])
            metadata = destination.execute(
                "SELECT hub_id FROM hub_metadata WHERE singleton = 1"
            ).fetchone()
            binding = destination.execute(
                """
                SELECT hub_id, server_identity
                FROM managed_host_bindings WHERE singleton = 1
                """
            ).fetchone()
            if (
                version != LATEST_SCHEMA_VERSION
                or metadata is None
                or str(metadata[0]) != self.hub_id
                or binding is None
                or str(binding[0]) != self.hub_id
                or str(binding[1]) != self.managed_host_identity
            ):
                raise RuntimeError("Team Hub maintenance backup identity verification failed")
            proof_rows: list[tuple[str, str, bytes]] = []
            bootstrap_claims = destination.execute(
                """
                SELECT c.id, c.token_hash FROM bootstrap_claims AS c
                LEFT JOIN bootstrap_delegations AS d
                  ON d.bootstrap_claim_id = c.id
                WHERE c.consumed_at IS NULL AND c.revoked_at IS NULL
                  AND c.expires_at > ? AND d.bootstrap_claim_id IS NULL
                ORDER BY c.created_at, c.id
                """,
                (snapshot_time,),
            ).fetchall()
            if len(bootstrap_claims) > 1:
                raise RuntimeError("Team Hub has multiple active bootstrap proofs")
            if bootstrap_claims:
                proof_rows.append(
                    (
                        "bootstrap-owner.proof",
                        str(bootstrap_claims[0]["id"]),
                        bytes(bootstrap_claims[0]["token_hash"]),
                    )
                )
            for claim in destination.execute(
                """
                SELECT id, token_hash FROM owner_recovery_claims
                WHERE consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                ORDER BY created_at, id
                """,
                (snapshot_time,),
            ):
                claim_id = str(claim["id"])
                if re.fullmatch(r"[A-Za-z0-9_]{8,240}", claim_id) is None:
                    raise RuntimeError("Team Hub recovery proof identity is invalid")
                proof_rows.append(
                    (f"{claim_id}.proof", claim_id, bytes(claim["token_hash"]))
                )
            destination.close()
            destination = None
            source.close()
            source = None
            os.chmod(database_copy, 0o600)
            with database_copy.open("rb") as stream:
                os.fsync(stream.fileno())

            key = read_secret_file(self.signing_key_path)
            key_copy = temporary / "access-token-signing.key"
            create_secret_file(key_copy, key)
            proof_manifest: list[dict[str, str]] = []
            if proof_rows:
                proof_directory = temporary / "proofs"
                ensure_private_directory(proof_directory)
                for filename, claim_id, expected_digest in proof_rows:
                    source_path = self.data_dir / filename
                    proof_bytes = read_secret_file(source_path)
                    try:
                        proof_value = proof_bytes.decode("ascii").strip()
                        actual_digest = token_hash(proof_value)
                    except (UnicodeError, TokenError) as exc:
                        raise RuntimeError(
                            "Team Hub active local proof could not be verified"
                        ) from exc
                    if not hmac.compare_digest(expected_digest, actual_digest):
                        raise RuntimeError(
                            "Team Hub active local proof does not match its claim"
                        )
                    create_secret_file(proof_directory / filename, proof_bytes)
                    proof_manifest.append(
                        {
                            "claim_id": claim_id,
                            "filename": filename,
                            "sha256": hashlib.sha256(proof_bytes).hexdigest(),
                        }
                    )
            database_digest = self._sha256_private_regular_file(database_copy)
            key_digest = hashlib.sha256(key).hexdigest()
            manifest = {
                "format": 1,
                "reason": clean_reason,
                "hub_id": self.hub_id,
                "host_server_identity": self.managed_host_identity,
                "schema_version": LATEST_SCHEMA_VERSION,
                "database_sha256": database_digest,
                "signing_key_sha256": key_digest,
                "proofs": proof_manifest,
                "created_at": _iso8601(_now()),
            }
            create_secret_file(
                temporary / "manifest.json",
                canonical_json(manifest) + b"\n",
            )
            directory_descriptor = os.open(
                temporary,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
            os.replace(temporary, final)
            backups_descriptor = os.open(
                backups,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(backups_descriptor)
            finally:
                os.close(backups_descriptor)

            generations = sorted(
                (
                    entry
                    for entry in backups.iterdir()
                    if entry.name.startswith("snapshot_")
                    and not entry.is_symlink()
                    and entry.is_dir()
                ),
                key=lambda entry: entry.name,
                reverse=True,
            )
            for expired in generations[retained:]:
                shutil.rmtree(expired)
            return final
        except BaseException:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()
            if temporary.exists() and not temporary.is_symlink():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    @classmethod
    def verify_maintenance_snapshot(
        cls,
        data_dir: Path,
        snapshot_dir: Path,
        *,
        expected_host_identity: str,
        expected_hub_id: str,
        expected_operation_id: str,
        expected_reason: str = "server-update",
    ) -> None:
        """Fully verify an exact fenced rollback generation without restoring it."""

        root = Path(os.path.abspath(os.path.expanduser(os.fspath(data_dir))))
        cls._validate_private_directory_without_mutation(root)
        with cls.maintenance_control_lock(root):
            cls._restore_maintenance_snapshot_unlocked(
                root,
                snapshot_dir,
                expected_host_identity=expected_host_identity,
                expected_hub_id=expected_hub_id,
                expected_operation_id=expected_operation_id,
                expected_reason=expected_reason,
                verify_only=True,
            )

    @classmethod
    def restore_maintenance_snapshot(
        cls,
        data_dir: Path,
        snapshot_dir: Path,
        *,
        expected_host_identity: str,
        expected_hub_id: str,
        expected_operation_id: str,
        expected_reason: str = "server-update",
    ) -> None:
        lease = cls.acquire_managed_runtime_lease(data_dir)
        try:
            with cls.maintenance_control_lock(data_dir):
                cls._restore_maintenance_snapshot_unlocked(
                    data_dir,
                    snapshot_dir,
                    expected_host_identity=expected_host_identity,
                    expected_hub_id=expected_hub_id,
                    expected_operation_id=expected_operation_id,
                    expected_reason=expected_reason,
                    verify_only=False,
                )
        finally:
            cls.release_managed_runtime_lease(lease)

    @classmethod
    def _restore_maintenance_snapshot_unlocked(
        cls,
        data_dir: Path,
        snapshot_dir: Path,
        *,
        expected_host_identity: str,
        expected_hub_id: str,
        expected_operation_id: str,
        expected_reason: str,
        verify_only: bool,
    ) -> None:
        """Verify and restore one maintenance generation while Hub is offline.

        The managed listener must be stopped before this control-plane method
        runs. Every replacement is staged on the Hub filesystem, and an
        ordinary I/O failure rolls all already-replaced files back before the
        method returns. The old service is only restarted after a successful
        return, so it never observes a partial logical restore.
        """

        root = Path(os.path.abspath(os.path.expanduser(os.fspath(data_dir))))
        snapshot = Path(
            os.path.abspath(os.path.expanduser(os.fspath(snapshot_dir)))
        )
        host_identity = _identity(expected_host_identity)
        hub_id = str(expected_hub_id).strip()
        if (
            not 8 <= len(hub_id) <= 240
            or re.fullmatch(r"[A-Za-z0-9_.:-]+", hub_id) is None
        ):
            raise ValueError("expected Hub identity is invalid")
        if verify_only:
            cls._validate_private_directory_without_mutation(root)
        else:
            ensure_private_directory(root)
        backups = root / "maintenance-backups"
        if snapshot.parent != backups or not snapshot.name.startswith("snapshot_"):
            raise PermissionError("snapshot must be a Team Hub maintenance generation")
        if cls._maintenance_fence_control_unlocked(
            root,
            expected_hub_id=hub_id,
            expected_host_identity=host_identity,
            expected_reason=expected_reason,
            expected_operation_id=expected_operation_id,
            expected_snapshot=snapshot,
        ) is None:
            raise RuntimeError("Team Hub maintenance fence is missing")
        cls._validate_private_directory_without_mutation(backups)
        cls._validate_private_directory_without_mutation(snapshot)

        manifest_bytes = cls._read_private_regular_file(
            snapshot / "manifest.json", maximum_bytes=1024 * 1024
        )
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Team Hub snapshot manifest is invalid") from exc
        required_manifest_keys = {
            "format",
            "reason",
            "hub_id",
            "host_server_identity",
            "schema_version",
            "database_sha256",
            "signing_key_sha256",
            "proofs",
            "created_at",
        }
        if not isinstance(manifest, dict) or set(manifest) != required_manifest_keys:
            raise RuntimeError("Team Hub snapshot manifest is invalid")
        schema_version = manifest.get("schema_version")
        if (
            manifest.get("format") != 1
            or manifest.get("hub_id") != hub_id
            or manifest.get("host_server_identity") != host_identity
            or manifest.get("reason") != expected_reason
            or isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or not 4 <= schema_version <= LATEST_SCHEMA_VERSION
            or not isinstance(manifest.get("reason"), str)
            or not 1 <= len(manifest["reason"]) <= 80
            or not isinstance(manifest.get("created_at"), str)
        ):
            raise RuntimeError("Team Hub snapshot identity is invalid")
        try:
            snapshot_time = int(
                datetime.fromisoformat(
                    manifest["created_at"].replace("Z", "+00:00")
                ).timestamp()
            )
        except (ValueError, OverflowError) as exc:
            raise RuntimeError("Team Hub snapshot timestamp is invalid") from exc
        if snapshot_time < 0:
            raise RuntimeError("Team Hub snapshot timestamp is invalid")
        for digest_name in ("database_sha256", "signing_key_sha256"):
            digest = manifest.get(digest_name)
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise RuntimeError("Team Hub snapshot manifest digest is invalid")

        raw_proofs = manifest.get("proofs")
        if not isinstance(raw_proofs, list) or len(raw_proofs) > 4096:
            raise RuntimeError("Team Hub snapshot proof manifest is invalid")
        proof_entries: dict[str, tuple[str, str]] = {}
        for raw in raw_proofs:
            if not isinstance(raw, dict) or set(raw) != {"claim_id", "filename", "sha256"}:
                raise RuntimeError("Team Hub snapshot proof manifest is invalid")
            claim_id = raw.get("claim_id")
            filename = raw.get("filename")
            digest = raw.get("sha256")
            if (
                not isinstance(claim_id, str)
                or re.fullmatch(r"[A-Za-z0-9_]{8,240}", claim_id) is None
                or not isinstance(filename, str)
                or filename
                not in {"bootstrap-owner.proof", f"{claim_id}.proof"}
                or filename in proof_entries
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            ):
                raise RuntimeError("Team Hub snapshot proof manifest is invalid")
            proof_entries[filename] = (claim_id, digest)

        verification_directory: tempfile.TemporaryDirectory[str] | None = None
        if verify_only:
            verification_directory = tempfile.TemporaryDirectory(
                prefix="team-hub-snapshot-verify-"
            )
            staging = Path(verification_directory.name)
            cls._validate_private_directory_without_mutation(staging)
        else:
            staging = root / f".restore-{os.getpid()}-{secrets.token_hex(8)}"
            ensure_private_directory(staging)
        installed: list[Path] = []
        moved_old: list[tuple[Path, Path]] = []
        connection: sqlite3.Connection | None = None
        try:
            staged_database = staging / "team-hub.sqlite3"
            staged_key = staging / "access-token-signing.key"
            cls._copy_private_regular_file(
                snapshot / "team-hub.sqlite3", staged_database
            )
            cls._copy_private_regular_file(
                snapshot / "access-token-signing.key", staged_key
            )
            if not hmac.compare_digest(
                cls._sha256_private_regular_file(staged_database),
                manifest["database_sha256"],
            ) or not hmac.compare_digest(
                cls._sha256_private_regular_file(staged_key),
                manifest["signing_key_sha256"],
            ):
                raise RuntimeError("Team Hub snapshot file digest is invalid")
            key_bytes = cls._read_private_regular_file(
                staged_key, minimum_bytes=32, maximum_bytes=4096
            )
            if hashlib.sha256(key_bytes).hexdigest() != manifest["signing_key_sha256"]:
                raise RuntimeError("Team Hub snapshot signing key is invalid")

            staged_proofs = staging / "proofs"
            if proof_entries:
                cls._validate_private_directory_without_mutation(snapshot / "proofs")
                ensure_private_directory(staged_proofs)
                for filename, (_claim_id, digest) in proof_entries.items():
                    destination = staged_proofs / filename
                    cls._copy_private_regular_file(
                        snapshot / "proofs" / filename, destination
                    )
                    if not hmac.compare_digest(
                        cls._sha256_private_regular_file(destination), digest
                    ):
                        raise RuntimeError("Team Hub snapshot proof digest is invalid")

            connection = sqlite3.connect(
                f"file:{staged_database}?mode=ro&immutable=1",
                uri=True,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            if str(connection.execute("PRAGMA integrity_check").fetchone()[0]) != "ok":
                raise RuntimeError("Team Hub snapshot database integrity check failed")
            if int(connection.execute("PRAGMA user_version").fetchone()[0]) != schema_version:
                raise RuntimeError("Team Hub snapshot schema version is invalid")
            migrations = connection.execute(
                "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
            ).fetchall()
            expected_migrations = MIGRATIONS[:schema_version]
            if len(migrations) != len(expected_migrations) or any(
                (
                    int(row["version"]),
                    str(row["name"]),
                    str(row["sha256"]),
                )
                != (item.version, item.name, item.sha256)
                for row, item in zip(migrations, expected_migrations)
            ):
                raise RuntimeError("Team Hub snapshot migration ledger is invalid")
            metadata = connection.execute(
                "SELECT hub_id FROM hub_metadata WHERE singleton = 1"
            ).fetchone()
            binding = connection.execute(
                "SELECT hub_id, server_identity FROM managed_host_bindings WHERE singleton = 1"
            ).fetchone()
            if (
                metadata is None
                or str(metadata["hub_id"]) != hub_id
                or binding is None
                or str(binding["hub_id"]) != hub_id
                or str(binding["server_identity"]) != host_identity
            ):
                raise RuntimeError("Team Hub snapshot host binding is invalid")

            database_proofs: dict[str, tuple[str, bytes, int]] = {}
            try:
                if schema_version >= 5:
                    # Delegated bootstrap claims were introduced by migration
                    # 0005. They never have a local proof file and must remain
                    # excluded from schema-5 snapshots. Schema 4 predates that
                    # table, so its active claim is necessarily the local proof.
                    bootstrap_proofs = connection.execute(
                        """
                        SELECT c.id, c.token_hash, c.expires_at
                        FROM bootstrap_claims AS c
                        LEFT JOIN bootstrap_delegations AS d
                          ON d.bootstrap_claim_id = c.id
                        WHERE c.consumed_at IS NULL AND c.revoked_at IS NULL
                          AND d.bootstrap_claim_id IS NULL
                        """
                    ).fetchall()
                else:
                    bootstrap_proofs = connection.execute(
                        """
                        SELECT id, token_hash, expires_at
                        FROM bootstrap_claims
                        WHERE consumed_at IS NULL AND revoked_at IS NULL
                        """
                    ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(
                    "Team Hub snapshot bootstrap proof schema is invalid"
                ) from exc
            for row in bootstrap_proofs:
                database_proofs["bootstrap-owner.proof"] = (
                    str(row["id"]), bytes(row["token_hash"]), int(row["expires_at"])
                )
            for row in connection.execute(
                """
                SELECT id, token_hash, expires_at FROM owner_recovery_claims
                WHERE consumed_at IS NULL AND revoked_at IS NULL
                """
            ):
                claim_id = str(row["id"])
                database_proofs[f"{claim_id}.proof"] = (
                    claim_id, bytes(row["token_hash"]), int(row["expires_at"])
                )
            for filename, (claim_id, _digest) in proof_entries.items():
                database_entry = database_proofs.get(filename)
                if database_entry is None or database_entry[0] != claim_id:
                    raise RuntimeError("Team Hub snapshot proof claim is invalid")
                proof_bytes = cls._read_private_regular_file(
                    staged_proofs / filename, maximum_bytes=4096
                )
                try:
                    proof_value = proof_bytes.decode("ascii").strip()
                    proof_digest = token_hash(proof_value)
                except (UnicodeError, TokenError) as exc:
                    raise RuntimeError("Team Hub snapshot proof is invalid") from exc
                if not hmac.compare_digest(proof_digest, database_entry[1]):
                    raise RuntimeError("Team Hub snapshot proof claim is invalid")
            active_at_snapshot = {
                filename
                for filename, (_claim_id, _digest, expires_at) in database_proofs.items()
                if expires_at > snapshot_time
            }
            if not active_at_snapshot.issubset(proof_entries):
                raise RuntimeError("Team Hub snapshot omits an active local proof")
            connection.close()
            connection = None

            if verify_only:
                return

            old_directory = staging / "previous"
            ensure_private_directory(old_directory)
            managed_targets = {
                root / "team-hub.sqlite3",
                root / "access-token-signing.key",
                root / "team-hub.sqlite3-wal",
                root / "team-hub.sqlite3-shm",
                root / "bootstrap-owner.proof",
                root / "maintenance-fence.json",
            }
            for entry in root.glob("*.proof"):
                if re.fullmatch(r"[A-Za-z0-9_]{8,240}\.proof", entry.name):
                    managed_targets.add(entry)
            for target in sorted(managed_targets, key=lambda item: item.name):
                try:
                    info = target.lstat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                    raise PermissionError("Team Hub live state contains an unsafe file")
                prior = old_directory / target.name
                os.replace(target, prior)
                moved_old.append((prior, target))

            replacements = [
                (staged_database, root / "team-hub.sqlite3"),
                (staged_key, root / "access-token-signing.key"),
            ]
            replacements.extend(
                (staged_proofs / filename, root / filename)
                for filename in sorted(proof_entries)
            )
            for source, target in replacements:
                os.replace(source, target)
                installed.append(target)
            cls._fsync_directory(root)
            if (
                cls._sha256_private_regular_file(root / "team-hub.sqlite3")
                != manifest["database_sha256"]
                or cls._sha256_private_regular_file(root / "access-token-signing.key")
                != manifest["signing_key_sha256"]
            ):
                raise RuntimeError("Team Hub restored state verification failed")
        except BaseException:
            if connection is not None:
                connection.close()
            for target in reversed(installed):
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
            for prior, target in reversed(moved_old):
                if prior.exists():
                    os.replace(prior, target)
            with suppress(OSError):
                cls._fsync_directory(root)
            raise
        finally:
            if verification_directory is not None:
                verification_directory.cleanup()
            elif staging.exists() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)

    def renew_bootstrap_proof(self) -> Path:
        timestamp = _now()
        connection = self.connect()
        try:
            with _write_transaction(connection):
                if self._is_bootstrapped(connection) or not self._globally_empty(connection):
                    raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 409)
                self._ensure_bootstrap_claim(
                    connection,
                    timestamp,
                    force_local=True,
                )
            return self.bootstrap_proof_path
        finally:
            connection.close()

    @staticmethod
    def _canonical_request_id(value: str) -> str:
        try:
            parsed = uuid.UUID(str(value))
            canonical = str(parsed)
        except (ValueError, AttributeError) as exc:
            raise ValueError("request_id is invalid") from exc
        if parsed.version != 4 or canonical != str(value):
            raise ValueError("request_id must be a canonical UUIDv4")
        return canonical

    def _tailnet_bootstrap_proof(self, fingerprint: bytes) -> str:
        key = read_secret_file(self.signing_key_path)
        digest = hmac.new(
            key,
            b"agentsdock-team-hub-tailnet-bootstrap-v1\0" + fingerprint,
            hashlib.sha256,
        ).digest()
        encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        return f"bootstrap_remote.{encoded}"

    def issue_tailnet_bootstrap_proof(
        self,
        *,
        request_id: str,
        server_identity: str,
        server_instance_id: str,
        hub_url: str,
        tailnet_login: str,
        recipient_email: str,
        display_name: str,
        device_label: str,
        transport: str = "tailscale_serve",
    ) -> dict[str, Any]:
        """Issue an idempotent, hash-only first-owner proof for one remote route."""

        timestamp = _now()
        clean_request_id = self._canonical_request_id(request_id)
        clean_server_identity = _identity(server_identity)
        clean_server_instance_id = _identity(server_instance_id)
        clean_hub_url = _bounded_text(hub_url, "hub_url", 16, 2048)
        clean_login = _email(tailnet_login)
        clean_recipient = _email(recipient_email)
        clean_display_name = _bounded_text(display_name, "display_name", 1, 160)
        clean_device_label = _bounded_text(device_label, "device_label", 1, 160)
        if transport not in {"tailscale_serve", "direct_ip"}:
            raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
        try:
            hub_scheme = urlsplit(clean_hub_url).scheme
        except ValueError as exc:
            raise HubError(
                "bootstrap_unavailable", "Bootstrap is unavailable", 403
            ) from exc
        if (
            transport == "tailscale_serve" and hub_scheme != "https"
        ) or (
            transport == "direct_ip" and hub_scheme != "http"
        ):
            raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
        if clean_login != clean_recipient:
            raise HubError(
                "bootstrap_identity_mismatch",
                (
                    "Bootstrap recipient does not match the verified Tailnet identity"
                    if transport == "tailscale_serve"
                    else "Bootstrap recipient does not match the confirmed direct-IP owner"
                ),
                403,
            )
        if (
            self.managed_host_identity is None
            or clean_server_identity != self.managed_host_identity
            or self.managed_server_instance_id is None
            or clean_server_instance_id != self.managed_server_instance_id
        ):
            raise HubError(
                "bootstrap_target_changed",
                "The designated Team Hub host changed before confirmation",
                409,
            )
        fingerprint = canonical_fingerprint(
            {
                "request_id": clean_request_id,
                "server_identity": clean_server_identity,
                "server_instance_id": clean_server_instance_id,
                "hub_id": self.hub_id,
                "hub_url": clean_hub_url,
                "transport": transport,
                "tailnet_login": clean_login,
                "recipient_email": clean_recipient,
                "display_name": clean_display_name,
                "device_label": clean_device_label,
            }
        )
        proof = self._tailnet_bootstrap_proof(fingerprint)
        digest = token_hash(proof)
        expires_at = timestamp + TAILNET_BOOTSTRAP_PROOF_TTL_SECONDS
        connection = self.connect()
        try:
            with _write_transaction(connection):
                if self._is_bootstrapped(connection) or not self._globally_empty(connection):
                    raise HubError(
                        "bootstrap_unavailable", "Bootstrap is unavailable", 409
                    )
                prior = connection.execute(
                    """
                    SELECT d.request_fingerprint, d.expires_at,
                           c.token_hash, c.consumed_at, c.revoked_at
                    FROM bootstrap_delegations AS d
                    JOIN bootstrap_claims AS c ON c.id = d.bootstrap_claim_id
                    WHERE d.request_id = ?
                    """,
                    (clean_request_id,),
                ).fetchone()
                if prior is not None:
                    if not hmac.compare_digest(
                        bytes(prior["request_fingerprint"]), fingerprint
                    ):
                        raise HubError(
                            "idempotency_conflict",
                            "Bootstrap request_id was already used for another request",
                            409,
                        )
                    if (
                        prior["consumed_at"] is not None
                        or prior["revoked_at"] is not None
                        or int(prior["expires_at"]) <= timestamp
                        or not hmac.compare_digest(bytes(prior["token_hash"]), digest)
                    ):
                        raise HubError(
                            "bootstrap_unavailable", "Bootstrap is unavailable", 409
                        )
                    expires_at = int(prior["expires_at"])
                else:
                    delegation_count = int(
                        connection.execute(
                            "SELECT count(*) FROM bootstrap_delegations"
                        ).fetchone()[0]
                    )
                    if delegation_count >= MAX_BOOTSTRAP_DELEGATION_LEDGER_ROWS:
                        raise HubError(
                            "bootstrap_ledger_exhausted",
                            (
                                "Remote bootstrap proof issuance is exhausted; "
                                "complete first-owner setup from the host"
                            ),
                            409,
                        )
                    competing = connection.execute(
                        """
                        SELECT d.request_id
                        FROM bootstrap_delegations AS d
                        JOIN bootstrap_claims AS c
                          ON c.id = d.bootstrap_claim_id
                        WHERE c.consumed_at IS NULL AND c.revoked_at IS NULL
                          AND c.expires_at > ?
                        LIMIT 1
                        """,
                        (timestamp,),
                    ).fetchone()
                    if competing is not None:
                        raise HubError(
                            "bootstrap_request_in_progress",
                            "Another bootstrap confirmation is still active",
                            409,
                        )
                    connection.execute(
                        """
                        UPDATE bootstrap_claims SET revoked_at = ?
                        WHERE consumed_at IS NULL AND revoked_at IS NULL
                        """,
                        (timestamp,),
                    )
                    claim_id = _id("bootstrap_claim")
                    connection.execute(
                        """
                        INSERT INTO bootstrap_claims(
                            id, token_hash, created_at, expires_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (claim_id, digest, timestamp, expires_at),
                    )
                    connection.execute(
                        """
                        INSERT INTO bootstrap_delegations(
                            bootstrap_claim_id, request_id, request_fingerprint,
                            server_identity, server_instance_id, hub_id, hub_url,
                            tailnet_login_normalized, recipient_email_normalized,
                            display_name, device_label, created_at, expires_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            claim_id,
                            clean_request_id,
                            fingerprint,
                            clean_server_identity,
                            clean_server_instance_id,
                            self.hub_id,
                            clean_hub_url,
                            clean_login,
                            clean_recipient,
                            clean_display_name,
                            clean_device_label,
                            timestamp,
                            expires_at,
                        ),
                    )
            try:
                self.bootstrap_proof_path.unlink(missing_ok=True)
            except OSError:
                pass
            return {
                "request_id": clean_request_id,
                "server_identity": clean_server_identity,
                "server_instance_id": clean_server_instance_id,
                "hub_id": self.hub_id,
                "tailnet_login": clean_login,
                "expires_at": _iso8601(expires_at),
                "bootstrap_proof": proof,
            }
        finally:
            connection.close()

    def verify_access(self, value: str) -> AccessClaims:
        try:
            payload = self.signer.verify(value)
        except TokenError as exc:
            raise HubError("authentication_required", "Authentication required", 401) from exc
        return AccessClaims(
            str(payload["sub"]), str(payload["sid"]), str(payload["jti"]), int(payload["exp"])
        )

    @staticmethod
    def _require_session(
        connection: sqlite3.Connection, claims: AccessClaims, timestamp: int
    ) -> sqlite3.Row:
        if claims.auth_kind == "secure_peer":
            if (
                claims.team_id is None
                or claims.peer_id is None
                or claims.expires_at <= timestamp
                or claims.session_id != f"secure_peer_session_{claims.peer_id}"
            ):
                raise HubError("authentication_required", "Authentication required", 401)
            row = connection.execute(
                """
                SELECT ? AS id, p.id AS human_principal_id,
                       ? AS device_label, ? AS expires_at,
                       NULL AS email_normalized, p.display_name,
                       p.kind AS principal_kind
                FROM principals AS p
                JOIN service_accounts AS s ON s.principal_id = p.id
                JOIN memberships AS m ON m.principal_id = p.id
                WHERE p.id = ? AND p.kind = 'service'
                  AND p.scope_team_id IS NULL AND p.status = 'active'
                  AND s.service_identifier = ?
                  AND m.team_id = ? AND m.role = 'automation'
                  AND m.status = 'active'
                """,
                (
                    claims.session_id,
                    "Secure paired server",
                    claims.expires_at,
                    claims.principal_id,
                    f"agentsdock.secure-peer.{claims.peer_id}",
                    claims.team_id,
                ),
            ).fetchone()
            if row is None:
                raise HubError("authentication_required", "Authentication required", 401)
            return row
        if claims.auth_kind != "human":
            raise HubError("authentication_required", "Authentication required", 401)
        row = connection.execute(
            """
            SELECT s.id, s.human_principal_id, s.device_label, s.expires_at,
                   h.email_normalized, p.display_name,
                   p.kind AS principal_kind
            FROM device_sessions AS s
            JOIN human_accounts AS h ON h.principal_id = s.human_principal_id
            JOIN principals AS p ON p.id = s.human_principal_id
            WHERE s.id = ? AND s.human_principal_id = ?
              AND s.revoked_at IS NULL AND s.expires_at > ?
              AND p.status = 'active'
              AND NOT EXISTS (
                SELECT 1 FROM access_token_revocations AS r
                WHERE r.device_session_id = s.id
                  AND r.jti_hash = ? AND r.expires_at > ?
              )
            """,
            (
                claims.session_id,
                claims.principal_id,
                timestamp,
                hashlib.sha256(claims.jti.encode("utf-8")).digest(),
                timestamp,
            ),
        ).fetchone()
        if row is None:
            raise HubError("authentication_required", "Authentication required", 401)
        return row

    def _teams_for(self, connection: sqlite3.Connection, principal_id: str) -> list[dict[str, Any]]:
        return [
            _row_dict(row)
            for row in connection.execute(
                """
                SELECT t.id, t.kind, t.slug, t.display_name, m.role, m.status
                FROM memberships AS m
                JOIN teams AS t ON t.id = m.team_id
                WHERE m.principal_id = ? AND m.status = 'active'
                ORDER BY t.display_name COLLATE NOCASE, t.id
                """,
                (principal_id,),
            )
        ]

    @staticmethod
    def _local_control_principal(
        connection: sqlite3.Connection, team_id: str, timestamp: int
    ) -> str:
        """Return the team-scoped membership for the host control-plane actor.

        Recovery proofs are issued by the owner of the Hub data directory, not
        by the human being recovered.  A stable service principal keeps that
        distinction honest in the immutable audit ledger.  It is created only
        after a team exists and in the same transaction as the operation that
        needs it, so it cannot make an empty database ineligible for bootstrap.
        """

        principal = connection.execute(
            """
            SELECT p.kind, p.scope_team_id, p.display_name, p.status,
                   s.service_identifier
            FROM principals AS p
            LEFT JOIN service_accounts AS s ON s.principal_id = p.id
            WHERE p.id = ?
            """,
            (LOCAL_CONTROL_PRINCIPAL_ID,),
        ).fetchone()
        if principal is None:
            connection.execute(
                """
                INSERT INTO principals(
                    id, kind, scope_team_id, display_name, status,
                    created_at, updated_at
                ) VALUES (?, 'service', NULL, ?, 'active', ?, ?)
                """,
                (
                    LOCAL_CONTROL_PRINCIPAL_ID,
                    "Team Hub local control",
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO service_accounts(
                    principal_id, service_identifier, created_at
                ) VALUES (?, ?, ?)
                """,
                (
                    LOCAL_CONTROL_PRINCIPAL_ID,
                    "agentsdock.team-hub.local-control",
                    timestamp,
                ),
            )
        elif (
            principal["kind"] != "service"
            or principal["scope_team_id"] is not None
            or principal["status"] != "active"
            or principal["service_identifier"] != "agentsdock.team-hub.local-control"
        ):
            raise RuntimeError("invalid Team Hub local-control service principal")

        membership = connection.execute(
            """
            SELECT role, status FROM memberships
            WHERE team_id = ? AND principal_id = ?
            """,
            (team_id, LOCAL_CONTROL_PRINCIPAL_ID),
        ).fetchone()
        if membership is None:
            connection.execute(
                """
                INSERT INTO memberships(
                    id, team_id, principal_id, role, status,
                    invited_by_principal_id, created_at, updated_at
                ) VALUES (?, ?, ?, 'automation', 'active', NULL, ?, ?)
                """,
                (
                    _id("membership"),
                    team_id,
                    LOCAL_CONTROL_PRINCIPAL_ID,
                    timestamp,
                    timestamp,
                ),
            )
        elif membership["role"] != "automation" or membership["status"] != "active":
            raise RuntimeError("invalid Team Hub local-control team membership")
        return LOCAL_CONTROL_PRINCIPAL_ID

    @staticmethod
    def _secure_peer_principal_id(peer_id: str) -> str:
        try:
            parsed = uuid.UUID(peer_id)
        except (ValueError, AttributeError) as exc:
            raise HubError("peer_unavailable", "Secure peer is unavailable", 404) from exc
        if parsed.version != 4 or str(parsed) != peer_id:
            raise HubError("peer_unavailable", "Secure peer is unavailable", 404)
        return "service_secure_peer_" + parsed.hex

    def _ensure_managed_host_node(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        timestamp: int,
    ) -> str | None:
        """Materialize the designated host as one ordinary logical server."""

        identity = self.managed_host_identity
        if identity is None:
            return None
        row = connection.execute(
            """
            SELECT n.id,n.team_id,n.principal_id,n.status,
                   p.kind AS principal_kind,p.scope_team_id,p.status AS principal_status
            FROM nodes AS n JOIN principals AS p ON p.id=n.principal_id
            WHERE n.server_identity=?
            """,
            (identity,),
        ).fetchone()
        if row is not None:
            if (
                row["team_id"] != team_id
                or row["principal_kind"] != "node"
                or row["scope_team_id"] != team_id
                or row["principal_status"] != "active"
                or row["status"] == "revoked"
            ):
                raise HubError(
                    "server_identity_conflict",
                    "Managed server identity conflicts with Team Hub state",
                    409,
                )
            if row["status"] != "active":
                connection.execute(
                    "UPDATE nodes SET status='active',last_seen_at=? WHERE id=?",
                    (timestamp, row["id"]),
                )
            return str(row["id"])
        principal_id = _id("node_principal")
        node_id = _id("node")
        label = "Team Hub host"
        connection.execute(
            """
            INSERT INTO principals(
                id,kind,scope_team_id,display_name,status,created_at,updated_at
            ) VALUES (?,'node',?,?,'active',?,?)
            """,
            (principal_id, team_id, label, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO nodes(
                id,team_id,principal_id,server_identity,display_name,status,
                enrolled_at,last_seen_at
            ) VALUES (?,?,?,?,?,'active',?,?)
            """,
            (node_id, team_id, principal_id, identity, label, timestamp, timestamp),
        )
        return node_id

    def ensure_secure_peer_service(
        self,
        *,
        peer_id: str,
        peer_server_identity: str,
        team_id: str,
        display_name: str,
    ) -> str:
        """Idempotently bind one approved mTLS peer to an automation member.

        The certificate and scope authority remain in the separate secure-peer
        store and are rechecked for every proxied request.  This row supplies
        only an auditable Team Hub author principal; it cannot authenticate on
        any ordinary HTTP Hub route.
        """

        principal_id = self._secure_peer_principal_id(peer_id)
        identity = _identity(peer_server_identity)
        label = _bounded_text(display_name, "display_name", 1, 160)
        clean_team = _identity(team_id)
        timestamp = _now()
        service_identifier = f"agentsdock.secure-peer.{peer_id}"
        changed = False
        connection = self.connect()
        try:
            with _write_transaction(connection):
                team = connection.execute(
                    "SELECT id FROM teams WHERE id = ?",
                    (clean_team,),
                ).fetchone()
                if team is None:
                    raise HubError("not_found", "Resource not found", 404)
                self._ensure_managed_host_node(connection, clean_team, timestamp)
                principal = connection.execute(
                    """
                    SELECT p.kind,p.scope_team_id,p.display_name,p.status,
                           s.service_identifier
                    FROM principals AS p
                    LEFT JOIN service_accounts AS s ON s.principal_id=p.id
                    WHERE p.id=?
                    """,
                    (principal_id,),
                ).fetchone()
                if principal is None:
                    connection.execute(
                        """
                        INSERT INTO principals(
                            id,kind,scope_team_id,display_name,status,
                            created_at,updated_at
                        ) VALUES (?,'service',NULL,?,'active',?,?)
                        """,
                        (principal_id, label, timestamp, timestamp),
                    )
                    connection.execute(
                        """
                        INSERT INTO service_accounts(
                            principal_id,service_identifier,created_at
                        ) VALUES (?,?,?)
                        """,
                        (principal_id, service_identifier, timestamp),
                    )
                    changed = True
                elif (
                    principal["kind"] != "service"
                    or principal["scope_team_id"] is not None
                    or principal["status"] != "active"
                    or principal["service_identifier"] != service_identifier
                    or principal["display_name"] != label
                ):
                    raise HubError(
                        "peer_identity_conflict",
                        "Secure peer identity conflicts with Team Hub state",
                        409,
                    )
                membership = connection.execute(
                    """
                    SELECT role,status FROM memberships
                    WHERE team_id=? AND principal_id=?
                    """,
                    (clean_team, principal_id),
                ).fetchone()
                if membership is None:
                    connection.execute(
                        """
                        INSERT INTO memberships(
                            id,team_id,principal_id,role,status,
                            invited_by_principal_id,created_at,updated_at
                        ) VALUES (?,?,?,'automation','active',NULL,?,?)
                        """,
                        (
                            _id("membership"),
                            clean_team,
                            principal_id,
                            timestamp,
                            timestamp,
                        ),
                    )
                    changed = True
                elif membership["role"] != "automation" or membership["status"] != "active":
                    raise HubError(
                        "peer_identity_conflict",
                        "Secure peer membership conflicts with Team Hub state",
                        409,
                    )
                self._ensure_network_board(
                    connection, clean_team, principal_id, timestamp
                )
                node = connection.execute(
                    """
                    SELECT n.id,n.team_id,n.principal_id,n.display_name,n.status,
                           p.kind AS principal_kind,p.scope_team_id,p.status AS principal_status
                    FROM nodes AS n
                    JOIN principals AS p ON p.id=n.principal_id
                    WHERE n.server_identity=?
                    """,
                    (identity,),
                ).fetchone()
                if node is None:
                    node_principal_id = _id("node_principal")
                    node_id = _id("node")
                    connection.execute(
                        """
                        INSERT INTO principals(
                            id,kind,scope_team_id,display_name,status,
                            created_at,updated_at
                        ) VALUES (?,'node',?,?,'active',?,?)
                        """,
                        (node_principal_id, clean_team, label, timestamp, timestamp),
                    )
                    connection.execute(
                        """
                        INSERT INTO nodes(
                            id,team_id,principal_id,server_identity,display_name,
                            status,enrolled_at,last_seen_at
                        ) VALUES (?,?,?,?,?,'offline',?,NULL)
                        """,
                        (
                            node_id,
                            clean_team,
                            node_principal_id,
                            identity,
                            label,
                            timestamp,
                        ),
                    )
                    changed = True
                else:
                    if (
                        node["team_id"] != clean_team
                        or node["principal_kind"] != "node"
                        or node["scope_team_id"] != clean_team
                        or node["principal_status"] != "active"
                        or node["status"] == "revoked"
                    ):
                        raise HubError(
                            "peer_identity_conflict",
                            "Secure peer server identity conflicts with Team Hub state",
                            409,
                        )
                    node_id = str(node["id"])
                    if node["display_name"] != label:
                        connection.execute(
                            """
                            UPDATE nodes
                            SET display_name=?
                            WHERE id=?
                            """,
                            (label, node_id),
                        )
                        connection.execute(
                            """
                            UPDATE principals SET display_name=?,updated_at=?
                            WHERE id=?
                            """,
                            (label, timestamp, node["principal_id"]),
                        )
                        changed = True
                binding = connection.execute(
                    """
                    SELECT peer_id,node_id,service_principal_id,
                           peer_server_identity,status
                    FROM network_peer_bindings WHERE peer_id=?
                    """,
                    (peer_id,),
                ).fetchone()
                active_for_node = connection.execute(
                    """
                    SELECT peer_id FROM network_peer_bindings
                    WHERE team_id=? AND node_id=? AND status='active'
                    """,
                    (clean_team, node_id),
                ).fetchone()
                if binding is None:
                    if active_for_node is not None:
                        raise HubError(
                            "peer_identity_conflict",
                            "Server already has an active secure peer connection",
                            409,
                        )
                    connection.execute(
                        """
                        INSERT INTO network_peer_bindings(
                            peer_id,team_id,node_id,service_principal_id,
                            peer_server_identity,status,created_at
                        ) VALUES (?,?,?,?,?,'active',?)
                        """,
                        (
                            peer_id,
                            clean_team,
                            node_id,
                            principal_id,
                            identity,
                            timestamp,
                        ),
                    )
                    changed = True
                elif (
                    binding["node_id"] != node_id
                    or binding["service_principal_id"] != principal_id
                    or binding["peer_server_identity"] != identity
                    or binding["status"] != "active"
                ):
                    raise HubError(
                        "peer_identity_conflict",
                        "Secure peer binding conflicts with Team Hub state",
                        409,
                    )
                # Existing public channels predate the automation ACL role.
                # Install only the shared role entry; private/direct channels
                # require an explicit principal ACL and remain invisible.
                channels = connection.execute(
                    """
                    SELECT id,kind FROM channels
                    WHERE team_id=? AND visibility='team' AND archived_at IS NULL
                    """,
                    (clean_team,),
                ).fetchall()
                for channel in channels:
                    inserted = connection.execute(
                        """
                        INSERT OR IGNORE INTO channel_acl_entries(
                            id,team_id,channel_id,subject_kind,
                            subject_principal_id,subject_role,
                            can_read,can_post,can_manage,can_dispatch,created_at
                        ) VALUES (?,?,?,'role',NULL,'automation',1,?,0,0,?)
                        """,
                        (
                            _id("channel_acl"),
                            clean_team,
                            channel["id"],
                            0 if channel["kind"] == "announcements" else 1,
                            timestamp,
                        ),
                    ).rowcount
                    changed = changed or inserted == 1
                if changed:
                    self._audit(
                        connection,
                        clean_team,
                        principal_id,
                        "secure_peer.bind",
                        "secure_peer",
                        peer_id,
                        "succeeded",
                        {"server_identity": identity, "node_id": node_id},
                        timestamp,
                    )
            return principal_id
        finally:
            connection.close()

    def active_secure_peer_binding_ids(
        self,
        peer_ids: list[str] | tuple[str, ...],
        peer_server_identity: str,
    ) -> set[str]:
        """Return exact, live trust bindings for one logical peer identity.

        Presence is deliberately not part of this lookup: an offline logical
        node still owns its active trust binding and must win restart
        reconciliation over an unbound duplicate peer record.
        """

        identity = _identity(peer_server_identity)
        if not isinstance(peer_ids, (list, tuple)):
            raise ValueError("peer_ids must be a bounded list")
        if len(peer_ids) > MAX_SECURE_PEER_BINDING_LOOKUP_IDS:
            raise ValueError("too many secure peer binding candidates")
        candidates: list[str] = []
        seen: set[str] = set()
        for value in peer_ids:
            self._secure_peer_principal_id(value)
            if value not in seen:
                candidates.append(value)
                seen.add(value)
        if not candidates:
            return set()

        placeholders = ",".join("?" for _value in candidates)
        connection = self.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT b.peer_id
                FROM network_peer_bindings AS b
                JOIN nodes AS n
                  ON n.team_id=b.team_id AND n.id=b.node_id
                JOIN principals AS np ON np.id=n.principal_id
                JOIN principals AS sp ON sp.id=b.service_principal_id
                JOIN service_accounts AS sa
                  ON sa.principal_id=b.service_principal_id
                JOIN memberships AS m
                  ON m.team_id=b.team_id
                 AND m.principal_id=b.service_principal_id
                WHERE b.peer_id IN ({placeholders})
                  AND b.peer_server_identity=?
                  AND b.status='active'
                  AND n.server_identity=b.peer_server_identity
                  AND n.status<>'revoked'
                  AND np.kind='node'
                  AND np.scope_team_id=b.team_id
                  AND np.status='active'
                  AND sp.kind='service'
                  AND sp.scope_team_id IS NULL
                  AND sp.status='active'
                  AND sa.service_identifier='agentsdock.secure-peer.' || b.peer_id
                  AND m.role='automation'
                  AND m.status='active'
                """,
                (*candidates, identity),
            ).fetchall()
            return {str(row["peer_id"]) for row in rows}
        finally:
            connection.close()

    def record_secure_peer_heartbeat(self, peer_id: str, team_id: str) -> None:
        """Mark one exactly bound logical node online and advance its lease."""

        principal_id = self._secure_peer_principal_id(peer_id)
        team = _identity(team_id)
        timestamp = _now()
        binding_query = """
                    SELECT b.node_id,n.status,n.last_seen_at
                    FROM network_peer_bindings AS b
                    JOIN nodes AS n
                      ON n.team_id=b.team_id AND n.id=b.node_id
                    JOIN principals AS np ON np.id=n.principal_id
                    JOIN principals AS sp ON sp.id=b.service_principal_id
                    JOIN service_accounts AS sa
                      ON sa.principal_id=b.service_principal_id
                    JOIN memberships AS m
                      ON m.team_id=b.team_id
                     AND m.principal_id=b.service_principal_id
                    WHERE b.peer_id=? AND b.team_id=?
                      AND b.service_principal_id=?
                      AND b.status='active'
                      AND n.server_identity=b.peer_server_identity
                      AND n.status IN ('active','offline')
                      AND np.kind='node'
                      AND np.scope_team_id=b.team_id
                      AND np.status='active'
                      AND sp.kind='service'
                      AND sp.scope_team_id IS NULL
                      AND sp.status='active'
                      AND sa.service_identifier='agentsdock.secure-peer.' || b.peer_id
                      AND m.role='automation'
                      AND m.status='active'
                    """
        connection = self.connect()
        try:
            binding = connection.execute(
                binding_query,
                (peer_id, team, principal_id),
            ).fetchone()
            if binding is None:
                raise HubError(
                    "peer_unavailable",
                    "Secure peer is unavailable",
                    404,
                )
            last_seen_at = (
                int(binding["last_seen_at"])
                if binding["last_seen_at"] is not None
                else None
            )
            if (
                binding["status"] == "active"
                and last_seen_at is not None
                and last_seen_at
                > timestamp - SECURE_PEER_HEARTBEAT_WRITE_INTERVAL_SECONDS
            ):
                return
            with _write_transaction(connection):
                binding = connection.execute(
                    binding_query,
                    (peer_id, team, principal_id),
                ).fetchone()
                if binding is None:
                    raise HubError(
                        "peer_unavailable",
                        "Secure peer is unavailable",
                        404,
                    )
                last_seen_at = (
                    int(binding["last_seen_at"])
                    if binding["last_seen_at"] is not None
                    else None
                )
                if (
                    binding["status"] == "active"
                    and last_seen_at is not None
                    and last_seen_at
                    > timestamp
                    - SECURE_PEER_HEARTBEAT_WRITE_INTERVAL_SECONDS
                ):
                    return
                changed = connection.execute(
                    """
                    UPDATE nodes
                    SET status='active',
                        last_seen_at=CASE
                            WHEN last_seen_at IS NULL OR last_seen_at < ? THEN ?
                            ELSE last_seen_at
                        END
                    WHERE team_id=? AND id=? AND status IN ('active','offline')
                    """,
                    (timestamp, timestamp, team, binding["node_id"]),
                ).rowcount
                if changed != 1:
                    raise HubError(
                        "peer_unavailable",
                        "Secure peer is unavailable",
                        404,
                    )
        finally:
            connection.close()

    def expire_secure_peer_leases(self, stale_before: int) -> int:
        """Mark stale active peer nodes offline without changing trust rows."""

        if type(stale_before) is not int or stale_before < 0:
            raise ValueError("stale_before must be a non-negative integer")
        connection = self.connect()
        try:
            with _write_transaction(connection):
                return connection.execute(
                    """
                    UPDATE nodes AS n
                    SET status='offline'
                    WHERE n.status='active'
                      AND (n.last_seen_at IS NULL OR n.last_seen_at < ?)
                      AND EXISTS (
                          SELECT 1 FROM network_peer_bindings AS b
                          WHERE b.team_id=n.team_id
                            AND b.node_id=n.id
                            AND b.peer_server_identity=n.server_identity
                            AND b.status='active'
                      )
                    """,
                    (stale_before,),
                ).rowcount
        finally:
            connection.close()

    def require_secure_peer_target_team(self, team_id: str) -> None:
        """Preflight an approval target before the peer certificate commits."""

        team = _identity(team_id)
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT id FROM teams WHERE id=?",
                (team,),
            ).fetchone()
            if row is None:
                raise HubError(
                    "team_not_found",
                    "Secure peer approval target team is unavailable",
                    404,
                )
        finally:
            connection.close()

    def secure_peer_claims(
        self,
        *,
        peer_id: str,
        peer_server_identity: str,
        team_id: str,
        scopes: frozenset[str],
        expires_at: int,
        display_name: str | None = None,
    ) -> AccessClaims:
        allowed = {
            "teamspace.read",
            "teamspace.write",
            "cross_chat.instruction",
            "cross_chat.request_reply",
        }
        if not scopes or not scopes.issubset(allowed):
            raise HubError("peer_unavailable", "Secure peer is unavailable", 403)
        # Provisioning is an explicit approval-time mutation.  Request-time
        # claims remain read-only so a paired peer cannot turn GET traffic
        # into an unbounded audit/SQLite write stream.
        principal_id = self._secure_peer_principal_id(peer_id)
        _identity(peer_server_identity)
        _identity(team_id)
        if display_name is not None:
            _bounded_text(display_name, "display_name", 1, 160)
        return AccessClaims(
            principal_id=principal_id,
            session_id=f"secure_peer_session_{peer_id}",
            jti=f"secure_peer_{peer_id}",
            expires_at=int(expires_at),
            auth_kind="secure_peer",
            team_id=team_id,
            scopes=frozenset(scopes),
            peer_id=peer_id,
        )

    def revoke_secure_peer_service(
        self,
        *,
        peer_id: str,
        team_id: str,
    ) -> None:
        principal_id = self._secure_peer_principal_id(peer_id)
        timestamp = _now()
        connection = self.connect()
        try:
            with _write_transaction(connection):
                binding = connection.execute(
                    """
                    SELECT node_id,status FROM network_peer_bindings
                    WHERE peer_id=? AND team_id=?
                    """,
                    (peer_id, team_id),
                ).fetchone()
                if binding is not None and binding["status"] == "active":
                    connection.execute(
                        """
                        UPDATE network_peer_bindings
                        SET status='revoked',revoked_at=?
                        WHERE peer_id=? AND team_id=? AND status='active'
                        """,
                        (timestamp, peer_id, team_id),
                    )
                    connection.execute(
                        """
                        UPDATE nodes SET status='offline',last_seen_at=?
                        WHERE team_id=? AND id=? AND status='active'
                        """,
                        (timestamp, team_id, binding["node_id"]),
                    )
                connection.execute(
                    """
                    UPDATE memberships SET status='revoked',updated_at=?
                    WHERE team_id=? AND principal_id=? AND status='active'
                    """,
                    (timestamp, team_id, principal_id),
                )
                connection.execute(
                    """
                    UPDATE principals SET status='revoked',updated_at=?
                    WHERE id=? AND status='active'
                    """,
                    (timestamp, principal_id),
                )
        finally:
            connection.close()

    def secure_peer_resource_team(
        self,
        resource_kind: str,
        resource_id: str,
    ) -> str | None:
        """Resolve only the small resource set accepted by the mTLS proxy."""

        if resource_kind != "channel":
            return None
        clean_id = _identity(resource_id)
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT team_id FROM channels WHERE id=? AND archived_at IS NULL",
                (clean_id,),
            ).fetchone()
            return str(row["team_id"]) if row is not None else None
        finally:
            connection.close()

    def _session_public(
        self, connection: sqlite3.Connection, session: sqlite3.Row
    ) -> dict[str, Any]:
        return {
            "session": {
                "id": session["id"],
                "device_label": session["device_label"],
                "expires_at": _iso8601(session["expires_at"]),
            },
            "principal": {
                "id": session["human_principal_id"],
                "email": session["email_normalized"],
                "display_name": session["display_name"],
                "kind": session["principal_kind"],
            },
            "teams": self._teams_for(connection, str(session["human_principal_id"])),
        }

    def _create_session(
        self,
        connection: sqlite3.Connection,
        principal_id: str,
        device_label: str,
        timestamp: int,
    ) -> tuple[sqlite3.Row, str, int]:
        label = _bounded_text(device_label, "device_label", 1, 160)
        session_id = _id("device")
        refresh, digest = opaque_secret("refresh")
        refresh_id = _id("refresh")
        expires_at = timestamp + SESSION_TTL_SECONDS
        connection.execute(
            """
            INSERT INTO device_sessions(
                id, human_principal_id, device_label, refresh_generation,
                created_at, last_seen_at, expires_at
            ) VALUES (?, ?, ?, 0, ?, ?, ?)
            """,
            (session_id, principal_id, label, timestamp, timestamp, expires_at),
        )
        connection.execute(
            """
            INSERT INTO refresh_tokens(
                id, device_session_id, token_hash, generation, created_at, expires_at
            ) VALUES (?, ?, ?, 0, ?, ?)
            """,
            (refresh_id, session_id, digest, timestamp, expires_at),
        )
        row = connection.execute(
            """
            SELECT s.id, s.human_principal_id, s.device_label, s.expires_at,
                   h.email_normalized, p.display_name,
                   p.kind AS principal_kind
            FROM device_sessions AS s
            JOIN human_accounts AS h ON h.principal_id = s.human_principal_id
            JOIN principals AS p ON p.id = s.human_principal_id
            WHERE s.id = ?
            """,
            (session_id,),
        ).fetchone()
        assert row is not None
        return row, refresh, expires_at

    def _auth_bundle(
        self,
        connection: sqlite3.Connection,
        session: sqlite3.Row,
        refresh: str,
        refresh_expires_at: int,
        timestamp: int,
    ) -> dict[str, Any]:
        access = self.signer.mint(
            str(session["human_principal_id"]), str(session["id"]), now=timestamp
        )
        return {
            "access_token": access.token,
            "token_type": "Bearer",
            "access_expires_at": _iso8601(access.expires_at),
            "refresh_token": refresh,
            "refresh_expires_at": _iso8601(refresh_expires_at),
            **self._session_public(connection, session),
        }

    def bootstrap(
        self,
        proof: str,
        email: str,
        display_name: str,
        device_label: str,
        *,
        transport: str = "loopback",
        request_id: str | None = None,
        tailnet_login: str | None = None,
        hub_url: str | None = None,
    ) -> dict[str, Any]:
        timestamp = _now()
        if transport not in {"loopback", "tailscale_serve", "direct_ip"}:
            raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
        try:
            digest = token_hash(proof)
        except TokenError as exc:
            raise HubError(
                "bootstrap_unavailable", "Bootstrap is unavailable", 403
            ) from exc
        connection = self.connect()
        try:
            with _write_transaction(connection):
                if not self._globally_empty(connection):
                    raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 409)
                claim = connection.execute(
                    """
                    SELECT c.id, c.token_hash,
                           d.request_id, d.server_identity, d.server_instance_id,
                           d.hub_id, d.hub_url, d.tailnet_login_normalized,
                           d.recipient_email_normalized, d.display_name,
                           d.device_label
                    FROM bootstrap_claims AS c
                    LEFT JOIN bootstrap_delegations AS d
                      ON d.bootstrap_claim_id = c.id
                    WHERE c.token_hash = ? AND c.consumed_at IS NULL
                      AND c.revoked_at IS NULL AND c.expires_at > ?
                    """,
                    (digest, timestamp),
                ).fetchone()
                if claim is None or not hmac.compare_digest(claim["token_hash"], digest):
                    raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
                delegated = claim["request_id"] is not None
                if transport == "loopback":
                    if delegated or request_id is not None or not proof.startswith("bootstrap."):
                        raise HubError(
                            "bootstrap_unavailable", "Bootstrap is unavailable", 403
                        )
                else:
                    try:
                        clean_request_id = self._canonical_request_id(str(request_id or ""))
                        clean_login = _email(str(tailnet_login or ""))
                        clean_email = _email(email)
                        clean_display_name = _bounded_text(
                            display_name, "display_name", 1, 160
                        )
                        clean_device_label = _bounded_text(
                            device_label, "device_label", 1, 160
                        )
                    except ValueError as exc:
                        raise HubError(
                            "bootstrap_unavailable", "Bootstrap is unavailable", 403
                        ) from exc
                    if (
                        not delegated
                        or not proof.startswith("bootstrap_remote.")
                        or clean_request_id != str(claim["request_id"])
                        or self.managed_host_identity is None
                        or str(claim["server_identity"])
                        != self.managed_host_identity
                        or self.managed_server_instance_id is None
                        or str(claim["server_instance_id"])
                        != self.managed_server_instance_id
                        or str(claim["hub_id"]) != self.hub_id
                        or str(claim["hub_url"]) != str(hub_url or "")
                        or (
                            transport == "tailscale_serve"
                            and urlsplit(str(claim["hub_url"])).scheme != "https"
                        )
                        or (
                            transport == "direct_ip"
                            and urlsplit(str(claim["hub_url"])).scheme != "http"
                        )
                        or str(claim["tailnet_login_normalized"]) != clean_login
                        or str(claim["recipient_email_normalized"]) != clean_email
                        or str(claim["display_name"]) != clean_display_name
                        or str(claim["device_label"]) != clean_device_label
                    ):
                        raise HubError(
                            "bootstrap_unavailable", "Bootstrap is unavailable", 403
                        )
                result = bootstrap_personal_team(
                    connection, email, display_name, now=timestamp
                )
                self._ensure_managed_host_node(
                    connection, result.team_id, timestamp
                )
                self._ensure_network_board(
                    connection,
                    result.team_id,
                    result.human_principal_id,
                    timestamp,
                )
                changed = connection.execute(
                    """
                    UPDATE bootstrap_claims
                    SET consumed_at = ?, consumed_by_principal_id = ?
                    WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                      AND expires_at > ?
                    """,
                    (timestamp, result.human_principal_id, claim["id"], timestamp),
                ).rowcount
                if changed != 1:
                    raise HubError("bootstrap_unavailable", "Bootstrap is unavailable", 403)
                session, refresh, refresh_exp = self._create_session(
                    connection, result.human_principal_id, device_label, timestamp
                )
                self._audit(
                    connection,
                    result.team_id,
                    result.human_principal_id,
                    "team.bootstrap",
                    "team",
                    result.team_id,
                    "succeeded",
                    {},
                    timestamp,
                )
                bundle = self._auth_bundle(connection, session, refresh, refresh_exp, timestamp)
            try:
                self.bootstrap_proof_path.unlink(missing_ok=True)
            except OSError:
                pass
            return bundle
        finally:
            connection.close()

    def session_snapshot(self, claims: AccessClaims) -> dict[str, Any]:
        timestamp = _now()
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            session = self._require_session(connection, claims, timestamp)
            response = self._session_public(connection, session)
            connection.execute("COMMIT")
            return response
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        timestamp = _now()
        try:
            digest = token_hash(refresh_token)
        except TokenError as exc:
            raise HubError("authentication_required", "Authentication required", 401) from exc
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT r.id AS refresh_id, r.device_session_id, r.token_hash,
                           r.generation, r.expires_at AS refresh_expires_at,
                           r.consumed_at, r.revoked_at,
                           s.human_principal_id, s.device_label, s.expires_at,
                           s.refresh_generation,
                           s.revoked_at AS session_revoked_at,
                           h.email_normalized, p.display_name, p.status AS principal_status
                    FROM refresh_tokens AS r
                    JOIN device_sessions AS s ON s.id = r.device_session_id
                    JOIN human_accounts AS h ON h.principal_id = s.human_principal_id
                    JOIN principals AS p ON p.id = s.human_principal_id
                    WHERE r.token_hash = ?
                    """,
                    (digest,),
                ).fetchone()
                if row is None or not hmac.compare_digest(row["token_hash"], digest):
                    raise HubError("authentication_required", "Authentication required", 401)
                if (
                    row["consumed_at"] is not None
                    or int(row["generation"]) != int(row["refresh_generation"])
                ):
                    connection.execute(
                        "UPDATE device_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                        (timestamp, row["device_session_id"]),
                    )
                    connection.execute(
                        """
                        UPDATE refresh_tokens SET revoked_at = ?
                        WHERE device_session_id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                        """,
                        (timestamp, row["device_session_id"]),
                    )
                    self._audit_principal_teams(
                        connection,
                        str(row["human_principal_id"]),
                        "session.refresh_replay",
                        "device_session",
                        str(row["device_session_id"]),
                        "denied",
                        timestamp,
                    )
                    connection.execute("COMMIT")
                    raise HubError("authentication_required", "Authentication required", 401)
                if (
                    row["revoked_at"] is not None
                    or row["session_revoked_at"] is not None
                    or row["principal_status"] != "active"
                    or int(row["refresh_expires_at"]) <= timestamp
                    or int(row["expires_at"]) <= timestamp
                ):
                    raise HubError("authentication_required", "Authentication required", 401)
                replacement, replacement_hash = opaque_secret("refresh")
                next_generation = int(row["generation"]) + 1
                replacement_id = _id("refresh")
                connection.execute(
                    """
                    INSERT INTO refresh_tokens(
                        id, device_session_id, token_hash, generation,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        replacement_id,
                        row["device_session_id"],
                        replacement_hash,
                        next_generation,
                        timestamp,
                        row["expires_at"],
                    ),
                )
                changed = connection.execute(
                    """
                    UPDATE refresh_tokens SET consumed_at = ?, replaced_by_token_id = ?
                    WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                    """,
                    (timestamp, replacement_id, row["refresh_id"]),
                ).rowcount
                if changed != 1:
                    raise HubError("authentication_required", "Authentication required", 401)
                connection.execute(
                    """
                    UPDATE device_sessions
                    SET refresh_generation = ?, last_seen_at = ?
                    WHERE id = ? AND revoked_at IS NULL
                    """,
                    (next_generation, timestamp, row["device_session_id"]),
                )
                current = connection.execute(
                    """
                    SELECT s.id, s.human_principal_id, s.device_label, s.expires_at,
                           h.email_normalized, p.display_name,
                           p.kind AS principal_kind
                    FROM device_sessions AS s
                    JOIN human_accounts AS h ON h.principal_id = s.human_principal_id
                    JOIN principals AS p ON p.id = s.human_principal_id
                    WHERE s.id = ?
                    """,
                    (row["device_session_id"],),
                ).fetchone()
                assert current is not None
                bundle = self._auth_bundle(
                    connection, current, replacement, int(row["expires_at"]), timestamp
                )
                self._audit_principal_teams(
                    connection,
                    str(row["human_principal_id"]),
                    "session.refresh",
                    "device_session",
                    str(row["device_session_id"]),
                    "succeeded",
                    timestamp,
                )
                connection.execute("COMMIT")
                return bundle
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        finally:
            connection.close()

    def revoke_session(self, claims: AccessClaims, refresh_token: str) -> dict[str, bool]:
        timestamp = _now()
        try:
            digest = token_hash(refresh_token)
        except TokenError as exc:
            raise HubError("authentication_required", "Authentication required", 401) from exc
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_session(connection, claims, timestamp)
                belongs = connection.execute(
                    "SELECT 1 FROM refresh_tokens WHERE device_session_id = ? AND token_hash = ?",
                    (claims.session_id, digest),
                ).fetchone()
                if belongs is None:
                    raise HubError("authentication_required", "Authentication required", 401)
                connection.execute(
                    "UPDATE device_sessions SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                    (timestamp, claims.session_id),
                )
                connection.execute(
                    """
                    UPDATE refresh_tokens SET revoked_at = ?
                    WHERE device_session_id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                    """,
                    (timestamp, claims.session_id),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO access_token_revocations(
                        jti_hash, device_session_id, expires_at, revoked_at, reason
                    ) VALUES (?, ?, ?, ?, 'session logout')
                    """,
                    (
                        hashlib.sha256(claims.jti.encode("utf-8")).digest(),
                        claims.session_id,
                        claims.expires_at,
                        timestamp,
                    ),
                )
                self._audit_principal_teams(
                    connection,
                    claims.principal_id,
                    "session.revoke",
                    "device_session",
                    claims.session_id,
                    "succeeded",
                    timestamp,
                )
            return {"revoked": True}
        finally:
            connection.close()

    def list_teams(self, claims: AccessClaims) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            result = {"teams": self._teams_for(connection, claims.principal_id)}
            connection.execute("COMMIT")
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_team(self, claims: AccessClaims, team_id: str) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            membership = _require_team_role(
                connection,
                team_id,
                claims.principal_id,
                ("owner", "admin", "member", "guest", "automation"),
            )
            row = connection.execute(
                "SELECT id, kind, slug, display_name FROM teams WHERE id = ?",
                (team_id,),
            ).fetchone()
            if row is None:
                raise HubError("not_found", "Resource not found", 404)
            team = _row_dict(row)
            team.update({"role": membership["role"], "status": "active"})
            connection.execute("COMMIT")
            return {"team": team}
        except (AuthorizationError, AuthenticationError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise HubError("not_found", "Resource not found", 404) from exc
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def list_members(self, claims: AccessClaims, team_id: str) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            viewer = _require_team_role(
                connection,
                team_id,
                claims.principal_id,
                ("owner", "admin", "member", "guest", "automation"),
            )
            email_projection = "h.email_normalized" if viewer["role"] in ("owner", "admin") else "NULL"
            members = [
                _row_dict(row)
                for row in connection.execute(
                    f"""
                    SELECT m.principal_id, {email_projection} AS email,
                           p.display_name, m.role, m.status
                    FROM memberships AS m
                    JOIN principals AS p ON p.id = m.principal_id
                    LEFT JOIN human_accounts AS h ON h.principal_id = m.principal_id
                    WHERE m.team_id = ? AND m.status = 'active' AND p.status = 'active'
                      AND (
                        p.kind = 'human'
                        OR (p.kind = 'service' AND p.id = ?)
                      )
                    ORDER BY p.display_name COLLATE NOCASE, m.principal_id
                    """,
                    (team_id, claims.principal_id),
                )
            ]
            connection.execute("COMMIT")
            return {"members": members}
        except (AuthorizationError, AuthenticationError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise HubError("not_found", "Resource not found", 404) from exc
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def issue_invite(
        self,
        claims: AccessClaims,
        team_id: str,
        invitee_email: str,
        role: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        timestamp = _now()
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_session(connection, claims, timestamp)
                issued = issue_invitation(
                    connection,
                    team_id,
                    claims.principal_id,
                    role,
                    invitee_email=invitee_email,
                    ttl_seconds=ttl_seconds,
                    now=timestamp,
                )
                normalized = _email(invitee_email)
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "invitation.issue",
                    "invitation",
                    issued.id,
                    "succeeded",
                    {"role": role},
                    timestamp,
                )
                return {
                    "invitation": {
                        "id": issued.id,
                        "team_id": team_id,
                        "invitee_email": normalized,
                        "role": role,
                        "expires_at": _iso8601(issued.expires_at),
                    },
                    "token": issued.token,
                }
        except AuthorizationError as exc:
            raise HubError("forbidden", "Operation is not permitted", 403) from exc
        finally:
            connection.close()

    def redeem_invite(
        self,
        invite_token: str,
        email: str,
        display_name: str,
        device_label: str,
    ) -> dict[str, Any]:
        timestamp = _now()
        normalized_email = _email(email)
        try:
            digest = _token_digest(invite_token)
        except (AuthenticationError, UnicodeError) as exc:
            raise HubError("invitation_unavailable", "Invitation is unavailable", 403) from exc
        connection = self.connect()
        try:
            with _write_transaction(connection):
                invitation = connection.execute(
                    """
                    SELECT id, team_id, invitee_email_normalized
                    FROM invitations
                    WHERE token_hash = ? AND redeemed_at IS NULL AND revoked_at IS NULL
                      AND expires_at > ?
                    """,
                    (digest, timestamp),
                ).fetchone()
                if (
                    invitation is None
                    or invitation["invitee_email_normalized"] is None
                    or str(invitation["invitee_email_normalized"]) != normalized_email
                ):
                    raise HubError("invitation_unavailable", "Invitation is unavailable", 403)
                human = connection.execute(
                    "SELECT principal_id FROM human_accounts WHERE email_normalized = ?",
                    (normalized_email,),
                ).fetchone()
                if human is not None:
                    raise HubError(
                        "invitation_requires_authentication",
                        "Sign in to accept this invitation",
                        409,
                    )
                principal_id = _id("human")
                name = _bounded_text(display_name, "display_name", 1, 160)
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
                    (principal_id, normalized_email, timestamp),
                )
                membership_id = redeem_invitation(
                    connection, invite_token, principal_id, now=timestamp
                )
                session, refresh, refresh_exp = self._create_session(
                    connection, principal_id, device_label, timestamp
                )
                self._audit(
                    connection,
                    str(invitation["team_id"]),
                    principal_id,
                    "invitation.redeem",
                    "membership",
                    membership_id,
                    "succeeded",
                    {},
                    timestamp,
                )
                return self._auth_bundle(connection, session, refresh, refresh_exp, timestamp)
        except (AuthenticationError, AuthorizationError) as exc:
            raise HubError("invitation_unavailable", "Invitation is unavailable", 403) from exc
        finally:
            connection.close()

    def accept_invite(self, claims: AccessClaims, invite_token: str) -> dict[str, Any]:
        timestamp = _now()
        try:
            digest = _token_digest(invite_token)
        except (AuthenticationError, UnicodeError) as exc:
            raise HubError("invitation_unavailable", "Invitation is unavailable", 403) from exc
        connection = self.connect()
        try:
            with _write_transaction(connection):
                session = self._require_session(connection, claims, timestamp)
                invitation = connection.execute(
                    """
                    SELECT team_id, invitee_email_normalized, role
                    FROM invitations
                    WHERE token_hash = ? AND redeemed_at IS NULL AND revoked_at IS NULL
                      AND expires_at > ?
                    """,
                    (digest, timestamp),
                ).fetchone()
                if (
                    invitation is None
                    or invitation["invitee_email_normalized"] is None
                    or str(invitation["invitee_email_normalized"])
                    != str(session["email_normalized"])
                ):
                    raise HubError("invitation_unavailable", "Invitation is unavailable", 403)
                membership_id = redeem_invitation(
                    connection, invite_token, claims.principal_id, now=timestamp
                )
                membership = connection.execute(
                    """
                    SELECT id, team_id, principal_id, role, status
                    FROM memberships WHERE id = ?
                    """,
                    (membership_id,),
                ).fetchone()
                assert membership is not None
                self._audit(
                    connection,
                    str(invitation["team_id"]),
                    claims.principal_id,
                    "invitation.accept",
                    "membership",
                    membership_id,
                    "succeeded",
                    {},
                    timestamp,
                )
                return {
                    "membership": _row_dict(membership),
                    "teams": self._teams_for(connection, claims.principal_id),
                }
        except (AuthenticationError, AuthorizationError) as exc:
            raise HubError("invitation_unavailable", "Invitation is unavailable", 403) from exc
        finally:
            connection.close()

    def issue_owner_recovery(
        self,
        email: str,
        device_label: str,
        *,
        team_id: str | None = None,
        now: int | None = None,
    ) -> Path:
        """Compatibility wrapper restricted to an active owner membership."""

        return self.issue_device_recovery(
            email,
            device_label,
            team_id=team_id,
            require_owner=True,
            now=now,
        )

    def issue_device_recovery(
        self,
        email: str,
        device_label: str,
        *,
        team_id: str | None = None,
        require_owner: bool = False,
        now: int | None = None,
    ) -> Path:
        """Issue a host-control recovery proof for one existing human principal."""

        timestamp = _now(now)
        normalized = _email(email)
        label = _bounded_text(device_label, "device_label", 1, 160)
        connection = self.connect()
        proof_path: Path | None = None
        superseded_ids: list[str] = []
        try:
            with _write_transaction(connection):
                role_clause = "AND m.role = 'owner'" if require_owner else ""
                memberships = connection.execute(
                    f"""
                    SELECT m.team_id, m.principal_id
                    FROM memberships AS m
                    JOIN human_accounts AS h ON h.principal_id = m.principal_id
                    JOIN principals AS p ON p.id = m.principal_id
                    WHERE h.email_normalized = ?
                      AND m.status = 'active' AND p.status = 'active'
                      AND (? IS NULL OR m.team_id = ?)
                      {role_clause}
                    """,
                    (normalized, team_id, team_id),
                ).fetchall()
                if len(memberships) != 1:
                    raise HubError("recovery_unavailable", "Device recovery is unavailable", 409)
                superseded_ids = [
                    str(row["id"])
                    for row in connection.execute(
                        """
                        SELECT id FROM owner_recovery_claims
                        WHERE owner_principal_id = ? AND consumed_at IS NULL
                          AND revoked_at IS NULL
                        """,
                        (memberships[0]["principal_id"],),
                    )
                ]
                connection.execute(
                    """
                    UPDATE owner_recovery_claims SET revoked_at = ?
                    WHERE owner_principal_id = ? AND consumed_at IS NULL
                      AND revoked_at IS NULL
                    """,
                    (timestamp, memberships[0]["principal_id"]),
                )
                proof, digest = opaque_secret("owner-recovery")
                claim_id = _id("owner_recovery")
                proof_path = self.data_dir / f"{claim_id}.proof"
                create_secret_file(proof_path, (proof + "\n").encode("ascii"))
                connection.execute(
                    """
                    INSERT INTO owner_recovery_claims(
                        id, team_id, owner_principal_id, token_hash,
                        device_label, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        claim_id,
                        memberships[0]["team_id"],
                        memberships[0]["principal_id"],
                        digest,
                        label,
                        timestamp,
                        timestamp + RECOVERY_PROOF_TTL_SECONDS,
                    ),
                )
                local_control_id = self._local_control_principal(
                    connection, str(memberships[0]["team_id"]), timestamp
                )
                self._audit(
                    connection,
                    str(memberships[0]["team_id"]),
                    local_control_id,
                    "device_recovery.issue",
                    "device_recovery",
                    claim_id,
                    "accepted",
                    {
                        "authority": "local_host_recovery",
                        "subject_principal_id": str(memberships[0]["principal_id"]),
                    },
                    timestamp,
                )
            for superseded_id in superseded_ids:
                try:
                    (self.data_dir / f"{superseded_id}.proof").unlink(missing_ok=True)
                except OSError:
                    pass
            return proof_path
        except BaseException:
            if proof_path is not None:
                try:
                    proof_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        finally:
            connection.close()

    def redeem_owner_recovery(self, proof: str, device_label: str) -> dict[str, Any]:
        return self.redeem_device_recovery(proof, device_label)

    def redeem_device_recovery(self, proof: str, device_label: str) -> dict[str, Any]:
        timestamp = _now()
        try:
            digest = token_hash(proof)
        except TokenError as exc:
            raise HubError("recovery_unavailable", "Device recovery is unavailable", 403) from exc
        label = _bounded_text(device_label, "device_label", 1, 160)
        connection = self.connect()
        claim_id = ""
        try:
            with _write_transaction(connection):
                claim = connection.execute(
                    """
                    SELECT c.id, c.team_id, c.owner_principal_id, c.token_hash,
                           c.device_label
                    FROM owner_recovery_claims AS c
                    JOIN memberships AS m
                      ON m.team_id = c.team_id AND m.principal_id = c.owner_principal_id
                    JOIN principals AS p ON p.id = c.owner_principal_id
                    WHERE c.token_hash = ? AND c.consumed_at IS NULL
                      AND c.revoked_at IS NULL AND c.expires_at > ?
                      AND m.status = 'active' AND p.status = 'active'
                    """,
                    (digest, timestamp),
                ).fetchone()
                if (
                    claim is None
                    or not hmac.compare_digest(claim["token_hash"], digest)
                    or str(claim["device_label"]) != label
                ):
                    raise HubError("recovery_unavailable", "Device recovery is unavailable", 403)
                prior_session_count = int(
                    connection.execute(
                        """
                        SELECT count(*) FROM device_sessions
                        WHERE human_principal_id = ? AND revoked_at IS NULL
                        """,
                        (claim["owner_principal_id"],),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    UPDATE device_sessions SET revoked_at = ?
                    WHERE human_principal_id = ? AND revoked_at IS NULL
                    """,
                    (timestamp, claim["owner_principal_id"]),
                )
                connection.execute(
                    """
                    UPDATE refresh_tokens SET revoked_at = ?
                    WHERE consumed_at IS NULL AND revoked_at IS NULL
                      AND device_session_id IN (
                        SELECT id FROM device_sessions WHERE human_principal_id = ?
                      )
                    """,
                    (timestamp, claim["owner_principal_id"]),
                )
                session, refresh, refresh_exp = self._create_session(
                    connection, str(claim["owner_principal_id"]), label, timestamp
                )
                claim_id = str(claim["id"])
                changed = connection.execute(
                    """
                    UPDATE owner_recovery_claims
                    SET consumed_at = ?, consumed_by_session_id = ?
                    WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                      AND expires_at > ?
                    """,
                    (timestamp, session["id"], claim_id, timestamp),
                ).rowcount
                if changed != 1:
                    raise HubError("recovery_unavailable", "Device recovery is unavailable", 403)
                local_control_id = self._local_control_principal(
                    connection, str(claim["team_id"]), timestamp
                )
                self._audit(
                    connection,
                    str(claim["team_id"]),
                    local_control_id,
                    "device_recovery.redeem",
                    "device_session",
                    str(session["id"]),
                    "succeeded",
                    {
                        "authority": "local_host_recovery",
                        "subject_principal_id": str(claim["owner_principal_id"]),
                        "revoked_prior_session_count": prior_session_count,
                    },
                    timestamp,
                )
                bundle = self._auth_bundle(connection, session, refresh, refresh_exp, timestamp)
            try:
                (self.data_dir / f"{claim_id}.proof").unlink(missing_ok=True)
            except OSError:
                pass
            return bundle
        finally:
            connection.close()

    def issue_node_grant(
        self,
        claims: AccessClaims,
        team_id: str,
        server_identity: str,
        display_name: str,
        public_key: str,
        ttl_seconds: int,
    ) -> dict[str, Any]:
        timestamp = _now()
        identity = _identity(server_identity)
        name = _bounded_text(display_name, "display_name", 1, 160)
        canonical_key, fingerprint = _canonical_ed25519_public_key(public_key)
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_session(connection, claims, timestamp)
                _require_team_role(
                    connection, team_id, claims.principal_id, ("owner", "admin")
                )
                collision = connection.execute(
                    """
                    SELECT 1
                    FROM node_enrollment_bindings AS b
                    JOIN node_enrollment_grants AS g ON g.id = b.grant_id
                    WHERE b.team_id = ? AND b.expected_server_identity = ?
                      AND g.consumed_at IS NULL AND g.revoked_at IS NULL
                      AND g.expires_at > ?
                    """,
                    (team_id, identity, timestamp),
                ).fetchone()
                if collision is not None:
                    raise HubError("enrollment_conflict", "A live enrollment already exists", 409)
                existing_node = connection.execute(
                    "SELECT 1 FROM nodes WHERE server_identity = ?",
                    (identity,),
                ).fetchone()
                if existing_node is not None:
                    raise HubError("enrollment_conflict", "Server identity is already enrolled", 409)
                legacy = connection.execute(
                    """
                    SELECT team_id, node_id FROM legacy_server_bindings
                    WHERE server_identity = ?
                    """,
                    (identity,),
                ).fetchone()
                if legacy is not None and (
                    str(legacy["team_id"]) != team_id or legacy["node_id"] is not None
                ):
                    raise HubError("enrollment_conflict", "Server identity is unavailable", 409)
                issued = issue_node_enrollment(
                    connection,
                    team_id,
                    claims.principal_id,
                    ttl_seconds=ttl_seconds,
                    now=timestamp,
                )
                connection.execute(
                    """
                    INSERT INTO node_enrollment_bindings(
                        grant_id, team_id, expected_server_identity,
                        expected_display_name, expected_public_material,
                        expected_public_key_fingerprint, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issued.id,
                        team_id,
                        identity,
                        name,
                        canonical_key,
                        fingerprint,
                        timestamp,
                    ),
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "node_enrollment.issue",
                    "node_enrollment",
                    issued.id,
                    "succeeded",
                    {"server_identity": identity, "fingerprint": fingerprint.hex()},
                    timestamp,
                )
                return {
                    "enrollment": {
                        "id": issued.id,
                        "team_id": team_id,
                        "server_identity": identity,
                        "display_name": name,
                        "public_key_fingerprint": fingerprint.hex(),
                        "expires_at": _iso8601(issued.expires_at),
                    },
                    "token": issued.token,
                }
        except AuthorizationError as exc:
            raise HubError("forbidden", "Operation is not permitted", 403) from exc
        finally:
            connection.close()

    def node_challenge(
        self, grant_token: str, server_identity: str, display_name: str, public_key: str
    ) -> dict[str, Any]:
        timestamp = _now()
        try:
            digest = _token_digest(grant_token)
        except (AuthenticationError, UnicodeError) as exc:
            raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403) from exc
        identity = _identity(server_identity)
        name = _bounded_text(display_name, "display_name", 1, 160)
        canonical_key, fingerprint = _canonical_ed25519_public_key(public_key)
        connection = self.connect()
        try:
            with _write_transaction(connection):
                grant = connection.execute(
                    """
                    SELECT g.id, g.team_id, g.token_hash, g.expires_at,
                           b.expected_server_identity, b.expected_display_name,
                           b.expected_public_material, b.expected_public_key_fingerprint
                    FROM node_enrollment_grants AS g
                    JOIN node_enrollment_bindings AS b
                      ON b.grant_id = g.id AND b.team_id = g.team_id
                    JOIN memberships AS issuer
                      ON issuer.team_id = g.team_id
                     AND issuer.principal_id = g.issued_by_principal_id
                    JOIN principals AS issuer_principal ON issuer_principal.id = issuer.principal_id
                    WHERE g.token_hash = ? AND g.consumed_at IS NULL
                      AND g.revoked_at IS NULL AND g.expires_at > ?
                      AND issuer.status = 'active' AND issuer.role IN ('owner', 'admin')
                      AND issuer_principal.status = 'active'
                    """,
                    (digest, timestamp),
                ).fetchone()
                if grant is None or not hmac.compare_digest(grant["token_hash"], digest):
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
                if (
                    str(grant["expected_server_identity"]) != identity
                    or str(grant["expected_display_name"]) != name
                    or str(grant["expected_public_material"]) != canonical_key
                    or not hmac.compare_digest(grant["expected_public_key_fingerprint"], fingerprint)
                ):
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
                existing = connection.execute(
                    "SELECT * FROM node_enrollment_challenges WHERE grant_id = ?",
                    (grant["id"],),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["consumed_at"] is None
                        and existing["revoked_at"] is None
                        and int(existing["expires_at"]) > timestamp
                        and hmac.compare_digest(existing["public_key_fingerprint"], fingerprint)
                    ):
                        payload = str(existing["signing_payload"])
                        payload_data = json.loads(payload.split("\n", 1)[1])
                        return {
                            "challenge_id": existing["id"],
                            "nonce": payload_data["nonce"],
                            "expires_at": _iso8601(existing["expires_at"]),
                            "signing_payload": payload,
                        }
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
                challenge_id = _id("node_challenge")
                nonce = secrets.token_urlsafe(32)
                expires_at = min(timestamp + NODE_CHALLENGE_TTL_SECONDS, int(grant["expires_at"]))
                payload_data = {
                    "challenge_id": challenge_id,
                    "grant_id": grant["id"],
                    "team_id": grant["team_id"],
                    "server_identity": identity,
                    "public_key_fingerprint": fingerprint.hex(),
                    "nonce": nonce,
                    "expires_at": expires_at,
                }
                signing_payload = (
                    "AgentsDock-Team-Hub-Node-Enrollment-v1\n"
                    + canonical_json(payload_data).decode("utf-8")
                )
                connection.execute(
                    """
                    INSERT INTO node_enrollment_challenges(
                        id, grant_id, team_id, public_material,
                        public_key_fingerprint, nonce_hash, signing_payload,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        challenge_id,
                        grant["id"],
                        grant["team_id"],
                        canonical_key,
                        fingerprint,
                        hashlib.sha256(nonce.encode("ascii")).digest(),
                        signing_payload,
                        timestamp,
                        expires_at,
                    ),
                )
                return {
                    "challenge_id": challenge_id,
                    "nonce": nonce,
                    "expires_at": _iso8601(expires_at),
                    "signing_payload": signing_payload,
                }
        finally:
            connection.close()

    def redeem_node_challenge(self, challenge_id: str, signature: str) -> dict[str, Any]:
        timestamp = _now()
        try:
            signature_bytes = base64.b64decode(signature.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403) from exc
        if len(signature_bytes) != 64:
            raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
        connection = self.connect()
        try:
            with _write_transaction(connection):
                row = connection.execute(
                    """
                    SELECT c.*, b.expected_server_identity, b.expected_display_name,
                           b.expected_public_material, b.expected_public_key_fingerprint,
                           g.token_hash, g.expires_at AS grant_expires_at,
                           g.consumed_at AS grant_consumed_at, g.revoked_at AS grant_revoked_at,
                           g.issued_by_principal_id
                    FROM node_enrollment_challenges AS c
                    JOIN node_enrollment_bindings AS b
                      ON b.grant_id = c.grant_id AND b.team_id = c.team_id
                    JOIN node_enrollment_grants AS g
                      ON g.id = c.grant_id AND g.team_id = c.team_id
                    JOIN memberships AS issuer
                      ON issuer.team_id = g.team_id
                     AND issuer.principal_id = g.issued_by_principal_id
                    JOIN principals AS issuer_principal ON issuer_principal.id = issuer.principal_id
                    WHERE c.id = ? AND c.consumed_at IS NULL AND c.revoked_at IS NULL
                      AND c.expires_at > ? AND g.expires_at > ?
                      AND g.consumed_at IS NULL AND g.revoked_at IS NULL
                      AND issuer.status = 'active' AND issuer.role IN ('owner', 'admin')
                      AND issuer_principal.status = 'active'
                    """,
                    (challenge_id, timestamp, timestamp),
                ).fetchone()
                if row is None:
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
                legacy = connection.execute(
                    """
                    SELECT team_id, node_id FROM legacy_server_bindings
                    WHERE server_identity = ?
                    """,
                    (row["expected_server_identity"],),
                ).fetchone()
                if legacy is not None and (
                    str(legacy["team_id"]) != str(row["team_id"])
                    or legacy["node_id"] is not None
                ):
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
                if (
                    str(row["public_material"]) != str(row["expected_public_material"])
                    or not hmac.compare_digest(
                        row["public_key_fingerprint"], row["expected_public_key_fingerprint"]
                    )
                ):
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
                try:
                    key = serialization.load_ssh_public_key(
                        str(row["public_material"]).encode("ascii")
                    )
                    if not isinstance(key, Ed25519PublicKey):
                        raise ValueError("not Ed25519")
                    key.verify(signature_bytes, str(row["signing_payload"]).encode("utf-8"))
                except Exception as exc:
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403) from exc
                if legacy is None:
                    connection.execute(
                        """
                        INSERT INTO legacy_server_bindings(
                            id, team_id, server_identity, node_id, created_at
                        ) VALUES (?, ?, ?, NULL, ?)
                        """,
                        (
                            _id("legacy_binding"),
                            row["team_id"],
                            row["expected_server_identity"],
                            timestamp,
                        ),
                    )
                principal_id = _id("node_principal")
                node_id = _id("node")
                credential_id = _id("node_credential")
                connection.execute(
                    """
                    INSERT INTO principals(
                        id, kind, scope_team_id, display_name, created_at, updated_at
                    ) VALUES (?, 'node', ?, ?, ?, ?)
                    """,
                    (
                        principal_id,
                        row["team_id"],
                        row["expected_display_name"],
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO nodes(
                        id, team_id, principal_id, server_identity,
                        display_name, enrolled_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        row["team_id"],
                        principal_id,
                        row["expected_server_identity"],
                        row["expected_display_name"],
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO node_credentials(
                        id, team_id, node_id, credential_kind, public_material,
                        fingerprint_sha256, created_at
                    ) VALUES (?, ?, ?, 'ed25519', ?, ?, ?)
                    """,
                    (
                        credential_id,
                        row["team_id"],
                        node_id,
                        row["public_material"],
                        row["public_key_fingerprint"],
                        timestamp,
                    ),
                )
                connection.execute(
                    """
                    UPDATE legacy_server_bindings SET node_id = ?
                    WHERE team_id = ? AND server_identity = ? AND node_id IS NULL
                    """,
                    (node_id, row["team_id"], row["expected_server_identity"]),
                )
                grant_changed = connection.execute(
                    """
                    UPDATE node_enrollment_grants
                    SET consumed_at = ?, consumed_by_node_id = ?
                    WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                      AND expires_at > ?
                    """,
                    (timestamp, node_id, row["grant_id"], timestamp),
                ).rowcount
                challenge_changed = connection.execute(
                    """
                    UPDATE node_enrollment_challenges SET consumed_at = ?
                    WHERE id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                      AND expires_at > ?
                    """,
                    (timestamp, challenge_id, timestamp),
                ).rowcount
                if grant_changed != 1 or challenge_changed != 1:
                    raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403)
                self._audit(
                    connection,
                    str(row["team_id"]),
                    principal_id,
                    "node.enroll",
                    "node",
                    node_id,
                    "succeeded",
                    {"server_identity": row["expected_server_identity"]},
                    timestamp,
                    node_id=node_id,
                )
                return {
                    "node": {
                        "id": node_id,
                        "team_id": row["team_id"],
                        "server_identity": row["expected_server_identity"],
                        "display_name": row["expected_display_name"],
                        "status": "active",
                        "enrolled_at": _iso8601(timestamp),
                        "last_seen_at": None,
                        "public_key_fingerprint": row["public_key_fingerprint"].hex(),
                    }
                }
        except sqlite3.IntegrityError as exc:
            raise HubError("enrollment_unavailable", "Enrollment is unavailable", 403) from exc
        finally:
            connection.close()

    def list_nodes(self, claims: AccessClaims, team_id: str) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            _require_team_role(
                connection,
                team_id,
                claims.principal_id,
                ("owner", "admin"),
            )
            nodes = [
                {
                    **_row_dict(row),
                    "public_key_fingerprint": row["public_key_fingerprint"].hex(),
                    "enrolled_at": _iso8601(row["enrolled_at"]),
                    "last_seen_at": _iso8601(row["last_seen_at"]),
                }
                for row in connection.execute(
                    """
                    SELECT n.id, n.team_id, n.server_identity, n.display_name,
                           n.status, n.enrolled_at, n.last_seen_at,
                           c.fingerprint_sha256 AS public_key_fingerprint
                    FROM nodes AS n
                    JOIN node_credentials AS c
                      ON c.team_id = n.team_id AND c.node_id = n.id
                     AND c.revoked_at IS NULL
                    WHERE n.team_id = ?
                    ORDER BY n.display_name COLLATE NOCASE, n.id
                    """,
                    (team_id,),
                )
            ]
            connection.execute("COMMIT")
            return {"nodes": nodes}
        except (AuthorizationError, AuthenticationError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise HubError("not_found", "Resource not found", 404) from exc
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def require_team_admin(
        self,
        claims: AccessClaims,
        team_id: str,
    ) -> dict[str, str]:
        """Prove a live owner/admin session without leaking team existence."""

        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            membership = _require_team_role(
                connection,
                team_id,
                claims.principal_id,
                ("owner", "admin"),
            )
            connection.execute("COMMIT")
            return {
                "team_id": team_id,
                "principal_id": claims.principal_id,
                "role": str(membership["role"]),
            }
        except (AuthorizationError, AuthenticationError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise HubError("not_found", "Resource not found", 404) from exc
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def require_team_owner(
        self,
        claims: AccessClaims,
        team_id: str,
    ) -> dict[str, str]:
        """Prove a live owner session without leaking team existence.

        Unassigned inbound peer-pairing requests are host-global until an
        owner deliberately binds one to a team.  Team administrators must not
        be able to inspect or claim that global queue merely by choosing a
        team id they administer.
        """

        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            membership = _require_team_role(
                connection,
                team_id,
                claims.principal_id,
                ("owner",),
            )
            connection.execute("COMMIT")
            return {
                "team_id": team_id,
                "principal_id": claims.principal_id,
                "role": str(membership["role"]),
            }
        except (AuthorizationError, AuthenticationError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise HubError("not_found", "Resource not found", 404) from exc
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _channel_permission(
        connection: sqlite3.Connection,
        channel: sqlite3.Row,
        principal_id: str,
        permission: str,
    ) -> bool:
        membership = connection.execute(
            """
            SELECT m.role
            FROM memberships AS m
            JOIN principals AS p ON p.id = m.principal_id
            WHERE m.team_id = ? AND m.principal_id = ?
              AND m.status = 'active' AND p.status = 'active'
            """,
            (channel["team_id"], principal_id),
        ).fetchone()
        if membership is None:
            return False
        if channel["kind"] == "direct":
            participants = [
                str(row["principal_id"])
                for row in connection.execute(
                    """
                    SELECT principal_id FROM channel_participants
                    WHERE channel_id = ? AND status = 'active' ORDER BY principal_id
                    """,
                    (channel["id"],),
                )
            ]
            expected_pair = (
                hashlib.sha256("\0".join(participants).encode("utf-8")).digest()
                if len(participants) == 2
                else b""
            )
            if (
                len(participants) != 2
                or principal_id not in participants
                or not hmac.compare_digest(channel["direct_pair_key"], expected_pair)
            ):
                return False
        column = {
            "read": "can_read",
            "post": "can_post",
            "manage": "can_manage",
            "dispatch": "can_dispatch",
        }.get(permission)
        if column is None:
            return False
        explicit = connection.execute(
            f"""
            SELECT {column} AS allowed FROM channel_acl_entries
            WHERE channel_id = ? AND subject_kind = 'principal'
              AND subject_principal_id = ?
            """,
            (channel["id"], principal_id),
        ).fetchone()
        if explicit is not None:
            return bool(explicit["allowed"])
        role_acl = connection.execute(
            f"""
            SELECT {column} AS allowed FROM channel_acl_entries
            WHERE channel_id = ? AND subject_kind = 'role' AND subject_role = ?
            """,
            (channel["id"], membership["role"]),
        ).fetchone()
        if role_acl is not None:
            return bool(role_acl["allowed"])
        return False

    def _channel_dict(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        principal_id: str | None = None,
    ) -> dict[str, Any]:
        participants = [
            str(item["principal_id"])
            for item in connection.execute(
                """
                SELECT principal_id FROM channel_participants
                WHERE channel_id = ? AND status = 'active'
                ORDER BY principal_id
                """,
                (row["id"],),
            )
        ]
        result = {
            "id": row["id"],
            "team_id": row["team_id"],
            "kind": row["kind"],
            "visibility": row["visibility"],
            "slug": row["slug"],
            "display_name": row["display_name"],
            "created_by_principal_id": row["created_by_principal_id"],
            "created_at": _iso8601(row["created_at"]),
            "updated_at": _iso8601(row["updated_at"]),
            "archived_at": _iso8601(row["archived_at"]),
            "participants": participants,
        }
        if principal_id is not None:
            result["permissions"] = {
                permission: self._channel_permission(
                    connection, row, principal_id, permission
                )
                for permission in ("read", "post", "manage", "dispatch")
            }
        return result

    @staticmethod
    def _idempotency_lookup(
        connection: sqlite3.Connection,
        team_id: str,
        principal_id: str,
        operation: str,
        key: str,
        fingerprint: bytes,
    ) -> dict[str, Any] | None:
        if not 8 <= len(key) <= 240:
            raise HubError("invalid_request", "idempotency_key must be 8-240 characters", 422)
        try:
            key_digest = hashlib.sha256(key.encode("utf-8")).digest()
        except UnicodeEncodeError as exc:
            raise HubError("invalid_request", "idempotency_key is invalid", 422) from exc
        row = connection.execute(
            """
            SELECT request_fingerprint, response_json
            FROM request_idempotency
            WHERE team_id = ? AND principal_id = ? AND operation = ? AND key_hash = ?
            """,
            (team_id, principal_id, operation, key_digest),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(row["request_fingerprint"], fingerprint):
            raise HubError(
                "idempotency_conflict",
                "Idempotency key was already used with a different request",
                409,
            )
        return json.loads(str(row["response_json"]))

    @staticmethod
    def _idempotency_store(
        connection: sqlite3.Connection,
        team_id: str,
        principal_id: str,
        operation: str,
        key: str,
        fingerprint: bytes,
        resource_type: str,
        resource_id: str,
        response: dict[str, Any],
        timestamp: int,
    ) -> None:
        connection.execute(
            """
            INSERT INTO request_idempotency(
                id, team_id, principal_id, operation, key_hash,
                request_fingerprint, resource_type, resource_id,
                response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _id("idempotency"),
                team_id,
                principal_id,
                operation,
                hashlib.sha256(key.encode("utf-8")).digest(),
                fingerprint,
                resource_type,
                resource_id,
                canonical_json(response).decode("utf-8"),
                timestamp,
            ),
        )

    @staticmethod
    def _require_network_scope(
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
        *,
        write: bool,
    ) -> sqlite3.Row:
        HubStore._require_session(connection, claims, _now())
        if claims.auth_kind == "secure_peer":
            expected_scope = "teamspace.write" if write else "teamspace.read"
            if claims.team_id != team_id or expected_scope not in claims.scopes:
                raise HubError("forbidden", "Operation is not permitted", 403)
            roles = ("automation",)
        else:
            roles = (
                ("owner", "admin", "member")
                if write
                else ("owner", "admin", "member", "guest")
            )
        try:
            return _require_team_role(
                connection, team_id, claims.principal_id, roles
            )
        except (AuthorizationError, AuthenticationError) as exc:
            raise HubError("not_found", "Resource not found", 404) from exc

    @staticmethod
    def _bound_network_node(
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
    ) -> sqlite3.Row:
        if claims.auth_kind != "secure_peer" or claims.peer_id is None:
            raise HubError(
                "network_peer_required",
                "A bound server connection is required",
                403,
            )
        row = connection.execute(
            """
            SELECT b.node_id,n.principal_id,n.server_identity,n.display_name,n.status
            FROM network_peer_bindings AS b
            JOIN nodes AS n ON n.team_id=b.team_id AND n.id=b.node_id
            WHERE b.peer_id=? AND b.team_id=?
              AND b.service_principal_id=? AND b.status='active'
              AND n.status='active'
            """,
            (claims.peer_id, team_id, claims.principal_id),
        ).fetchone()
        if row is None:
            raise HubError(
                "network_peer_unavailable",
                "Bound server identity is unavailable",
                403,
            )
        return row

    def _caller_network_node(
        self,
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
    ) -> sqlite3.Row:
        """Resolve the one logical server this authenticated caller controls.

        An incoming secure peer is fenced by its durable certificate binding.
        An ordinary Team Hub session acts only for this installation's
        designated managed host, never for another server listed in the team.
        """

        if claims.auth_kind == "secure_peer":
            return self._bound_network_node(connection, claims, team_id)
        try:
            _require_team_role(
                connection,
                team_id,
                claims.principal_id,
                ("owner", "admin", "member"),
            )
        except (AuthorizationError, AuthenticationError) as exc:
            raise HubError("not_found", "Resource not found", 404) from exc
        if self.managed_host_identity is None:
            raise HubError(
                "network_host_unavailable",
                "Designated host server identity is unavailable",
                409,
            )
        row = connection.execute(
            """
            SELECT n.id AS node_id,n.principal_id,n.server_identity,
                   n.display_name,n.status
            FROM nodes AS n
            JOIN principals AS p ON p.id=n.principal_id
            WHERE n.team_id=? AND n.server_identity=?
              AND n.status='active' AND p.status='active'
            """,
            (team_id, self.managed_host_identity),
        ).fetchone()
        if row is None:
            raise HubError(
                "network_host_unavailable",
                "Designated host server identity is unavailable",
                409,
            )
        return row

    @staticmethod
    def _agent_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "server_id": row["node_id"],
            "external_agent_id": row["external_agent_id"],
            "backend": row["backend"],
            "display_name": row["display_name"],
            "status": row["status"],
        }

    def get_network(
        self,
        claims: AccessClaims,
        team_id: str,
        *,
        after_server_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= MAX_NETWORK_PAGE_ITEMS:
            raise HubError("invalid_request", "Network page limit is invalid", 422)
        bounded_limit = limit
        clean_after: str | None = None
        if after_server_id is not None:
            try:
                clean_after = _identity(after_server_id)
            except (AttributeError, TypeError, ValueError) as exc:
                raise HubError(
                    "invalid_request", "Network page cursor is invalid", 422
                ) from exc
            if clean_after != after_server_id:
                raise HubError(
                    "invalid_request", "Network page cursor is invalid", 422
                )
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            membership = self._require_network_scope(
                connection, claims, team_id, write=False
            )
            team = connection.execute(
                "SELECT id,display_name FROM teams WHERE id=?",
                (team_id,),
            ).fetchone()
            if team is None:
                raise HubError("not_found", "Resource not found", 404)
            owned_node_id: str | None = None
            if claims.auth_kind == "secure_peer":
                owned_node_id = str(
                    self._bound_network_node(connection, claims, team_id)["node_id"]
                )
            elif membership["role"] in {"owner", "admin", "member"}:
                try:
                    owned_node_id = str(
                        self._caller_network_node(
                            connection, claims, team_id
                        )["node_id"]
                    )
                except HubError as exc:
                    if exc.code != "network_host_unavailable":
                        raise
            network = {
                "id": team["id"],
                "display_name": team["display_name"],
                "hub_id": self.hub_id,
            }
            server_rows = list(
                connection.execute(
                    """
                    SELECT id,server_identity,display_name,status
                    FROM nodes
                    WHERE team_id=? AND status<>'revoked' AND id>?
                    ORDER BY id
                    LIMIT ?
                    """,
                    (team_id, clean_after or "", bounded_limit + 1),
                )
            )
            candidate_rows = server_rows[:bounded_limit]
            servers: list[dict[str, Any]] = []
            agents: list[dict[str, Any]] = []
            visible_rows: list[sqlite3.Row] = []
            for row in candidate_rows:
                server = {
                    "id": row["id"],
                    "server_identity": row["server_identity"],
                    "display_name": row["display_name"],
                    "status": row["status"],
                    "is_host": bool(
                        self.managed_host_identity is not None
                        and row["server_identity"] == self.managed_host_identity
                    ),
                    "owned_by_caller": row["id"] == owned_node_id,
                }
                # Materialize at most one server's bounded agent group at a
                # time. A page that reaches its byte ceiling never loads the
                # remaining candidate groups into memory.
                group_agents = [
                    self._agent_public(agent_row)
                    for agent_row in connection.execute(
                        """
                        SELECT id,node_id,external_agent_id,backend,
                               display_name,status
                        FROM agents
                        WHERE team_id=? AND node_id=? AND status<>'retired'
                        ORDER BY id
                        """,
                        (team_id, row["id"]),
                    )
                ]
                candidate_response = {
                    "network": network,
                    "servers": [*servers, server],
                    "agents": [*agents, *group_agents],
                    "next_after_server_id": row["id"],
                    # false is one byte larger than true in canonical JSON, so
                    # it is the conservative value for the transport bound.
                    "has_more": False,
                }
                if (
                    len(canonical_json(candidate_response))
                    > MAX_NETWORK_PAGE_RESPONSE_BYTES
                ):
                    break
                servers.append(server)
                agents.extend(group_agents)
                visible_rows.append(row)
            if candidate_rows and not visible_rows:
                raise RuntimeError(
                    "one bounded network server group exceeds the page response limit"
                )
            has_more = len(visible_rows) < len(server_rows)
            response = {
                "network": network,
                "servers": servers,
                "agents": agents,
                "next_after_server_id": (
                    str(visible_rows[-1]["id"]) if visible_rows else None
                ),
                "has_more": has_more,
            }
            if len(canonical_json(response)) > MAX_NETWORK_PAGE_RESPONSE_BYTES:
                raise RuntimeError("bounded network page exceeds the response limit")
            connection.execute("COMMIT")
            return response
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def register_network_agent(
        self,
        claims: AccessClaims,
        team_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = _now()
        external_id = _bounded_text(
            request.get("external_agent_id") or "",
            "external_agent_id",
            1,
            240,
        )
        display_name = _bounded_text(
            request.get("display_name") or "", "display_name", 1, 160
        )
        backend = request.get("backend")
        if backend not in {"codex", "claude", "other"}:
            raise HubError("invalid_request", "Agent backend is invalid", 422)
        fingerprint = canonical_fingerprint(
            {
                "team_id": team_id,
                "external_agent_id": external_id,
                "backend": backend,
                "display_name": display_name,
            }
        )
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_network_scope(connection, claims, team_id, write=True)
                if claims.auth_kind == "human":
                    try:
                        _require_team_role(
                            connection,
                            team_id,
                            claims.principal_id,
                            ("owner", "admin"),
                        )
                    except (AuthorizationError, AuthenticationError) as exc:
                        raise HubError(
                            "forbidden", "Operation is not permitted", 403
                        ) from exc
                node = self._caller_network_node(connection, claims, team_id)
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.agent.register",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                if connection.execute(
                    "SELECT 1 FROM agents WHERE node_id=? AND external_agent_id=?",
                    (node["node_id"], external_id),
                ).fetchone() is not None:
                    raise HubError(
                        "agent_conflict",
                        "Agent identity is already registered",
                        409,
                    )
                agent_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM agents WHERE node_id=?",
                        (node["node_id"],),
                    ).fetchone()[0]
                )
                if agent_count >= MAX_NETWORK_AGENTS_PER_SERVER:
                    raise HubError(
                        "agent_limit_reached",
                        "Server agent limit has been reached",
                        409,
                    )
                principal_id = _id("agent_principal")
                agent_id = _id("agent")
                connection.execute(
                    """
                    INSERT INTO principals(
                        id,kind,scope_team_id,display_name,status,
                        created_at,updated_at
                    ) VALUES (?,'agent',?,?,'active',?,?)
                    """,
                    (principal_id, team_id, display_name, timestamp, timestamp),
                )
                connection.execute(
                    """
                    INSERT INTO agents(
                        id,team_id,principal_id,node_id,external_agent_id,
                        backend,display_name,status,created_at,updated_at
                    ) VALUES (?,?,?,?,?,?,?,'active',?,?)
                    """,
                    (
                        agent_id,
                        team_id,
                        principal_id,
                        node["node_id"],
                        external_id,
                        backend,
                        display_name,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    """
                    SELECT id,node_id,external_agent_id,backend,display_name,status
                    FROM agents WHERE id=?
                    """,
                    (agent_id,),
                ).fetchone()
                assert row is not None
                response = {"agent": self._agent_public(row)}
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.agent.register",
                    request["idempotency_key"],
                    fingerprint,
                    "agent",
                    agent_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.agent.register",
                    "agent",
                    agent_id,
                    "succeeded",
                    {"node_id": node["node_id"], "backend": backend},
                    timestamp,
                )
                self._outbox(
                    connection, team_id, "agent", agent_id, "network.agent.registered", timestamp
                )
                return response
        finally:
            connection.close()

    def _ensure_network_board(
        self,
        connection: sqlite3.Connection,
        team_id: str,
        creator_principal_id: str,
        timestamp: int,
    ) -> sqlite3.Row:
        binding = connection.execute(
            """
            SELECT c.*,b.channel_id AS bound_channel_id
            FROM network_boards AS b
            LEFT JOIN channels AS c
              ON c.team_id=b.team_id AND c.id=b.channel_id
            WHERE b.team_id=?
            """,
            (team_id,),
        ).fetchone()
        if (
            binding is not None
            and binding["id"] is not None
            and binding["kind"] == "board"
            and binding["visibility"] == "team"
            and binding["archived_at"] is None
        ):
            return binding
        membership = _require_team_role(
            connection,
            team_id,
            creator_principal_id,
            ("owner", "admin", "member", "automation"),
        )
        channel_id = _id("channel")
        occupied_slugs = {
            str(row["slug"])
            for row in connection.execute(
                "SELECT slug FROM channels WHERE team_id=? AND slug IS NOT NULL",
                (team_id,),
            )
        }
        primary_slug = "agentsdock-bulletin"
        fallback_slug = "agentsdock-bulletin-v1"
        if primary_slug not in occupied_slugs:
            slug = primary_slug
        elif fallback_slug not in occupied_slugs:
            slug = fallback_slug
        else:
            suffix = 2
            while f"{fallback_slug}-{suffix}" in occupied_slugs:
                suffix += 1
            slug = f"{fallback_slug}-{suffix}"
        connection.execute(
            """
            INSERT INTO channels(
                id,team_id,kind,visibility,slug,display_name,
                direct_pair_key,created_by_principal_id,
                created_at,updated_at
            ) VALUES (?,?,'board','team',?,'Bulletin',NULL,?,?,?)
            """,
            (
                channel_id,
                team_id,
                slug,
                creator_principal_id,
                timestamp,
                timestamp,
            ),
        )
        role_permissions = {
            "owner": (1, 1, 1),
            "admin": (1, 1, 1),
            "member": (1, 1, 0),
            "guest": (1, 0, 0),
            "automation": (1, 1, 0),
        }
        for role, (can_read, can_post, can_manage) in role_permissions.items():
            connection.execute(
                """
                INSERT INTO channel_acl_entries(
                    id,team_id,channel_id,subject_kind,
                    subject_principal_id,subject_role,
                    can_read,can_post,can_manage,can_dispatch,created_at
                ) VALUES (?,?,?,'role',NULL,?,?,?,?,0,?)
                """,
                (
                    _id("channel_acl"),
                    team_id,
                    channel_id,
                    role,
                    can_read,
                    can_post,
                    can_manage,
                    timestamp,
                ),
            )
        if binding is None:
            connection.execute(
                """
                INSERT INTO network_boards(team_id,channel_id,created_at)
                VALUES (?,?,?)
                """,
                (team_id, channel_id, timestamp),
            )
        else:
            connection.execute(
                "UPDATE network_boards SET channel_id=? WHERE team_id=?",
                (channel_id, team_id),
            )
        row = connection.execute(
            "SELECT * FROM channels WHERE id=?", (channel_id,)
        ).fetchone()
        assert row is not None
        self._audit(
            connection,
            team_id,
            creator_principal_id,
            "network.bulletin.create",
            "network_bulletin",
            team_id,
            "succeeded",
            {
                "creator_role": membership["role"],
                "replaced_unavailable_binding": binding is not None,
                "reserved_slug": slug,
            },
            timestamp,
        )
        return row

    @staticmethod
    def _bulletin_post_public(row: sqlite3.Row) -> dict[str, Any]:
        author_kind = "server" if row["author_node_id"] is not None else "human"
        return {
            "id": row["id"],
            "sequence": int(row["channel_sequence"]),
            "author": {
                "kind": author_kind,
                "id": (
                    row["author_node_id"]
                    if author_kind == "server"
                    else row["author_principal_id"]
                ),
                "display_name": (
                    row["author_node_display_name"]
                    if author_kind == "server"
                    else row["author_principal_display_name"]
                ),
            },
            "body_format": row["body_format"],
            "body": row["body"],
            "thread_root_post_id": row["thread_root_message_id"],
            "reply_to_post_id": row["parent_message_id"],
            "created_at": _iso8601(row["created_at"]),
        }

    @staticmethod
    def _bulletin_post_select() -> str:
        return """
            SELECT m.*,p.display_name AS author_principal_display_name,
                   b.node_id AS author_node_id,
                   n.display_name AS author_node_display_name
            FROM messages AS m
            JOIN principals AS p ON p.id=m.author_principal_id
            LEFT JOIN network_peer_bindings AS b
              ON b.service_principal_id=m.author_principal_id
             AND b.team_id=m.team_id
            LEFT JOIN nodes AS n ON n.team_id=b.team_id AND n.id=b.node_id
        """

    @staticmethod
    def _bounded_network_page(
        rows: list[sqlite3.Row],
        *,
        clean_after: int,
        bounded_limit: int,
        collection_key: str,
        sequence_column: str,
        render: Callable[[sqlite3.Row], dict[str, Any]],
    ) -> dict[str, Any]:
        visible_rows: list[sqlite3.Row] = []
        visible_items: list[dict[str, Any]] = []
        encoded_collection_bytes = 2  # JSON array brackets.
        collection_budget = MAX_NETWORK_PAGE_RESPONSE_BYTES - 1_024
        for row in rows[:bounded_limit]:
            item = render(row)
            item_bytes = len(canonical_json(item))
            separator_bytes = 1 if visible_items else 0
            if (
                encoded_collection_bytes + separator_bytes + item_bytes
                > collection_budget
            ):
                break
            encoded_collection_bytes += separator_bytes + item_bytes
            visible_rows.append(row)
            visible_items.append(item)
        if rows and not visible_rows:
            raise RuntimeError("one bounded network item exceeds the page response limit")
        response = {
            collection_key: visible_items,
            "next_after_sequence": (
                int(visible_rows[-1][sequence_column])
                if visible_rows
                else clean_after
            ),
            "has_more": len(visible_rows) < len(rows),
        }
        if len(canonical_json(response)) > MAX_NETWORK_PAGE_RESPONSE_BYTES:
            raise RuntimeError("bounded network page exceeds the response limit")
        return response

    def list_network_bulletin(
        self,
        claims: AccessClaims,
        team_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_network_scope(connection, claims, team_id, write=False)
            board = connection.execute(
                """
                SELECT c.* FROM network_boards AS b
                JOIN channels AS c ON c.team_id=b.team_id AND c.id=b.channel_id
                WHERE b.team_id=? AND c.archived_at IS NULL
                """,
                (team_id,),
            ).fetchone()
            bounded_limit = max(1, min(int(limit), MAX_NETWORK_PAGE_ITEMS))
            clean_after = max(0, int(after_sequence))
            if board is None:
                rows: list[sqlite3.Row] = []
            else:
                if not self._channel_permission(
                    connection, board, claims.principal_id, "read"
                ):
                    raise HubError("not_found", "Resource not found", 404)
                rows = connection.execute(
                    self._bulletin_post_select()
                    + """
                    WHERE m.channel_id=? AND m.channel_sequence>?
                      AND m.deleted_at IS NULL
                    ORDER BY m.channel_sequence ASC LIMIT ?
                    """,
                    (board["id"], clean_after, bounded_limit + 1),
                ).fetchall()
            response = self._bounded_network_page(
                rows,
                clean_after=clean_after,
                bounded_limit=bounded_limit,
                collection_key="posts",
                sequence_column="channel_sequence",
                render=self._bulletin_post_public,
            )
            connection.execute("COMMIT")
            return response
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def create_network_bulletin_post(
        self,
        claims: AccessClaims,
        team_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = _now()
        body = request.get("body")
        try:
            encoded = body.encode("utf-8") if isinstance(body, str) else b""
        except UnicodeEncodeError as exc:
            raise HubError("invalid_request", "Bulletin body is invalid", 422) from exc
        if not 1 <= len(encoded) <= MAX_NETWORK_BODY_BYTES:
            raise HubError("invalid_request", "Bulletin body is invalid", 422)
        body_format = request.get("body_format")
        if body_format not in {"plain", "markdown"}:
            raise HubError("invalid_request", "Bulletin body format is invalid", 422)
        reply_to = request.get("reply_to_post_id")
        fingerprint = canonical_fingerprint(
            {
                "team_id": team_id,
                "body": body,
                "body_format": body_format,
                "reply_to_post_id": reply_to,
            }
        )
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_network_scope(connection, claims, team_id, write=True)
                board = self._ensure_network_board(
                    connection, team_id, claims.principal_id, timestamp
                )
                if not self._channel_permission(
                    connection, board, claims.principal_id, "post"
                ):
                    raise HubError("forbidden", "Operation is not permitted", 403)
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.bulletin.post",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                root_id: str | None = None
                if reply_to is not None:
                    parent = connection.execute(
                        """
                        SELECT id,thread_root_message_id FROM messages
                        WHERE team_id=? AND channel_id=? AND id=?
                          AND deleted_at IS NULL
                        """,
                        (team_id, board["id"], reply_to),
                    ).fetchone()
                    if parent is None:
                        raise HubError(
                            "invalid_request", "Bulletin reply target is unavailable", 422
                        )
                    root_id = str(parent["thread_root_message_id"] or parent["id"])
                self._charge_network_peer_write(
                    connection, claims, team_id, len(encoded), timestamp
                )
                sequence = int(board["next_message_sequence"])
                message_id = _id("message")
                connection.execute(
                    "UPDATE channels SET next_message_sequence=?,updated_at=? WHERE id=?",
                    (sequence + 1, timestamp, board["id"]),
                )
                connection.execute(
                    """
                    INSERT INTO messages(
                        id,team_id,channel_id,channel_sequence,kind,
                        thread_root_message_id,parent_message_id,
                        author_principal_id,body_format,body,
                        idempotency_key,created_at
                    ) VALUES (?,?,?,?,'post',?,?,?,?,?,?,?)
                    """,
                    (
                        message_id,
                        team_id,
                        board["id"],
                        sequence,
                        root_id,
                        reply_to,
                        claims.principal_id,
                        body_format,
                        body,
                        hashlib.sha256(
                            f"{team_id}\0{claims.principal_id}\0network.bulletin.post\0{request['idempotency_key']}".encode(
                                "utf-8"
                            )
                        ).digest(),
                        timestamp,
                    ),
                )
                row = connection.execute(
                    self._bulletin_post_select() + " WHERE m.id=?",
                    (message_id,),
                ).fetchone()
                assert row is not None
                response = {"post": self._bulletin_post_public(row)}
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.bulletin.post",
                    request["idempotency_key"],
                    fingerprint,
                    "network_bulletin_post",
                    message_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.bulletin.post",
                    "network_bulletin_post",
                    message_id,
                    "succeeded",
                    {"reply": reply_to is not None},
                    timestamp,
                )
                self._outbox(
                    connection,
                    team_id,
                    "network_bulletin_post",
                    message_id,
                    "network.bulletin.posted",
                    timestamp,
                )
                return response
        finally:
            connection.close()

    @staticmethod
    def _network_item_select() -> str:
        return """
            SELECT i.*,i.id AS request_item_id,
                   d.id AS delivery_id,d.state AS delivery_state,
                   d.available_at,d.delivered_at,d.read_at,
                   sp.display_name AS sender_principal_display_name,
                   sn.server_identity AS sender_server_identity,
                   sn.display_name AS sender_server_display_name,
                   sa.display_name AS sender_agent_display_name,
                   sa.backend AS sender_agent_backend,
                   rp.display_name AS recipient_principal_display_name,
                   rn.server_identity AS recipient_server_identity,
                   rn.display_name AS recipient_server_display_name,
                   ra.display_name AS recipient_agent_display_name,
                   ra.backend AS recipient_agent_backend,
                   (SELECT pr.status FROM network_passive_requests AS pr
                    WHERE pr.team_id=i.team_id AND pr.request_item_id=i.id)
                       AS request_status,
                   (SELECT pr.expires_at FROM network_passive_requests AS pr
                    WHERE pr.team_id=i.team_id AND pr.request_item_id=i.id)
                       AS request_expires_at,
                   (SELECT pr.reply_item_id FROM network_passive_requests AS pr
                    WHERE pr.team_id=i.team_id AND pr.request_item_id=i.id)
                       AS reply_item_id
            FROM network_mailbox_items AS i
            JOIN network_deliveries AS d
              ON d.team_id=i.team_id AND d.item_id=i.id
            JOIN principals AS sp ON sp.id=i.sender_principal_id
            JOIN principals AS rp ON rp.id=i.recipient_principal_id
            LEFT JOIN nodes AS sn
              ON sn.team_id=i.team_id AND sn.id=i.sender_node_id
            LEFT JOIN agents AS sa
              ON sa.team_id=i.team_id AND sa.id=i.sender_agent_id
            LEFT JOIN nodes AS rn
              ON rn.team_id=i.team_id AND rn.id=i.recipient_node_id
            LEFT JOIN agents AS ra
              ON ra.team_id=i.team_id AND ra.id=i.recipient_agent_id
        """

    @staticmethod
    def _network_address(row: sqlite3.Row, prefix: str) -> dict[str, Any]:
        kind = str(row[f"{prefix}_kind"])
        if kind == "human":
            return {
                "kind": "human",
                "id": row[f"{prefix}_principal_id"],
                "display_name": row[f"{prefix}_principal_display_name"],
            }
        if kind == "server":
            return {
                "kind": "server",
                "id": row[f"{prefix}_node_id"],
                "server_identity": row[f"{prefix}_server_identity"],
                "display_name": row[f"{prefix}_server_display_name"],
            }
        return {
            "kind": "agent",
            "id": row[f"{prefix}_agent_id"],
            "server_id": row[f"{prefix}_node_id"],
            "backend": row[f"{prefix}_agent_backend"],
            "display_name": row[f"{prefix}_agent_display_name"],
        }

    @classmethod
    def _network_item_public(cls, row: sqlite3.Row) -> dict[str, Any]:
        kind = str(row["kind"])
        request_id = (
            row["id"]
            if kind == "request"
            else row["root_request_item_id"]
            if kind == "reply"
            else None
        )
        return {
            "id": row["id"],
            "sequence": int(row["queue_ordinal"]),
            "kind": kind,
            "from": cls._network_address(row, "sender"),
            "to": cls._network_address(row, "recipient"),
            "body_format": row["body_format"],
            "body": row["body"],
            "request_id": request_id,
            "created_at": _iso8601(row["created_at"]),
            "expires_at": _iso8601(row["expires_at"]),
        }

    @staticmethod
    def _network_delivery_public(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["delivery_id"],
            "state": row["delivery_state"],
            "available_at": _iso8601(row["available_at"]),
            "delivered_at": _iso8601(row["delivered_at"]),
            "read_at": _iso8601(row["read_at"]),
        }

    def _network_sender(
        self,
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
        from_agent_id: str | None,
    ) -> dict[str, str | None]:
        if claims.auth_kind == "human":
            if from_agent_id is not None:
                raise HubError(
                    "forbidden", "A human session cannot claim agent authorship", 403
                )
            return {
                "kind": "human",
                "principal_id": claims.principal_id,
                "node_id": None,
                "agent_id": None,
            }
        node = self._caller_network_node(connection, claims, team_id)
        if from_agent_id is None:
            return {
                "kind": "server",
                "principal_id": claims.principal_id,
                "node_id": str(node["node_id"]),
                "agent_id": None,
            }
        agent_id = _identity(from_agent_id)
        agent = connection.execute(
            """
            SELECT id FROM agents
            WHERE team_id=? AND node_id=? AND id=? AND status='active'
            """,
            (team_id, node["node_id"], agent_id),
        ).fetchone()
        if agent is None:
            raise HubError("forbidden", "Agent is not owned by this server", 403)
        return {
            "kind": "agent",
            "principal_id": claims.principal_id,
            "node_id": str(node["node_id"]),
            "agent_id": agent_id,
        }

    @staticmethod
    def _network_recipient(
        connection: sqlite3.Connection,
        team_id: str,
        target: dict[str, Any],
    ) -> dict[str, str | None]:
        kind = target.get("kind")
        identifier = _identity(str(target.get("id") or ""))
        if kind == "server":
            row = connection.execute(
                """
                SELECT id,principal_id FROM nodes
                WHERE team_id=? AND id=? AND status='active'
                """,
                (team_id, identifier),
            ).fetchone()
            if row is None:
                raise HubError("recipient_unavailable", "Recipient is unavailable", 404)
            return {
                "kind": "server",
                "principal_id": str(row["principal_id"]),
                "node_id": str(row["id"]),
                "agent_id": None,
            }
        if kind == "agent":
            row = connection.execute(
                """
                SELECT a.id,a.node_id,a.principal_id
                FROM agents AS a
                JOIN nodes AS n ON n.team_id=a.team_id AND n.id=a.node_id
                JOIN principals AS p ON p.id=a.principal_id
                WHERE a.team_id=? AND a.id=? AND a.status='active'
                  AND n.status='active' AND p.status='active'
                """,
                (team_id, identifier),
            ).fetchone()
            if row is None:
                raise HubError("recipient_unavailable", "Recipient is unavailable", 404)
            return {
                "kind": "agent",
                "principal_id": str(row["principal_id"]),
                "node_id": str(row["node_id"]),
                "agent_id": str(row["id"]),
            }
        raise HubError("invalid_request", "Recipient kind is invalid", 422)

    @staticmethod
    def _network_body(request: dict[str, Any]) -> tuple[str, str, int]:
        body = request.get("body")
        try:
            encoded = body.encode("utf-8") if isinstance(body, str) else b""
        except UnicodeEncodeError as exc:
            raise HubError("invalid_request", "Mailbox body is invalid", 422) from exc
        if not 1 <= len(encoded) <= MAX_NETWORK_BODY_BYTES:
            raise HubError("invalid_request", "Mailbox body is invalid", 422)
        body_format = request.get("body_format")
        if body_format not in {"plain", "markdown"}:
            raise HubError("invalid_request", "Mailbox body format is invalid", 422)
        return body, str(body_format), len(encoded)

    def _insert_network_item(
        self,
        connection: sqlite3.Connection,
        *,
        team_id: str,
        kind: str,
        sender: dict[str, str | None],
        recipient: dict[str, str | None],
        body: str,
        body_format: str,
        operation: str,
        idempotency_key: str,
        timestamp: int,
        root_request_item_id: str | None = None,
        expires_at: int | None = None,
    ) -> tuple[dict[str, Any], sqlite3.Row]:
        item_id = _id("network_item")
        delivery_id = _id("network_delivery")
        connection.execute(
            """
            INSERT INTO network_mailbox_items(
                id,team_id,kind,sender_kind,sender_principal_id,
                sender_node_id,sender_agent_id,
                recipient_kind,recipient_principal_id,
                recipient_node_id,recipient_agent_id,root_request_item_id,
                body_format,body,idempotency_key,created_at,expires_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item_id,
                team_id,
                kind,
                sender["kind"],
                sender["principal_id"],
                sender["node_id"],
                sender["agent_id"],
                recipient["kind"],
                recipient["principal_id"],
                recipient["node_id"],
                recipient["agent_id"],
                root_request_item_id,
                body_format,
                body,
                hashlib.sha256(
                    f"{team_id}\0{sender['principal_id']}\0{operation}\0{idempotency_key}".encode(
                        "utf-8"
                    )
                ).digest(),
                timestamp,
                expires_at,
            ),
        )
        connection.execute(
            """
            INSERT INTO network_deliveries(
                id,team_id,item_id,state,available_at
            ) VALUES (?,?,?,'available',?)
            """,
            (delivery_id, team_id, item_id, timestamp),
        )
        row = connection.execute(
            self._network_item_select() + " WHERE i.id=?",
            (item_id,),
        ).fetchone()
        assert row is not None
        response = {
            "item": self._network_item_public(row),
            "delivery": self._network_delivery_public(row),
        }
        return response, row

    def _charge_network_peer_write(
        self,
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
        body_bytes: int,
        timestamp: int,
    ) -> None:
        if claims.auth_kind != "secure_peer":
            return
        subject = f"secure-peer:{claims.peer_id or claims.principal_id}"
        self._charge_rate_bucket(
            connection,
            team_id=team_id,
            subject_key=subject,
            action="network.mailbox.count.minute",
            timestamp=timestamp,
            window_seconds=60,
            cost=1,
            limit=60,
        )
        self._charge_rate_bucket(
            connection,
            team_id=team_id,
            subject_key=subject,
            action="network.mailbox.bytes.hour",
            timestamp=timestamp,
            window_seconds=3_600,
            cost=body_bytes,
            limit=4 * 1024 * 1024,
        )

    def create_network_mailbox_item(
        self,
        claims: AccessClaims,
        team_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = _now()
        body, body_format, body_bytes = self._network_body(request)
        fingerprint = canonical_fingerprint(
            {
                "team_id": team_id,
                "to": request.get("to"),
                "from_agent_id": request.get("from_agent_id"),
                "body": body,
                "body_format": body_format,
            }
        )
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_network_scope(connection, claims, team_id, write=True)
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.mailbox.send",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                sender = self._network_sender(
                    connection, claims, team_id, request.get("from_agent_id")
                )
                recipient = self._network_recipient(
                    connection, team_id, dict(request.get("to") or {})
                )
                self._charge_network_peer_write(
                    connection, claims, team_id, body_bytes, timestamp
                )
                response, row = self._insert_network_item(
                    connection,
                    team_id=team_id,
                    kind="message",
                    sender=sender,
                    recipient=recipient,
                    body=body,
                    body_format=body_format,
                    operation="network.mailbox.send",
                    idempotency_key=request["idempotency_key"],
                    timestamp=timestamp,
                )
                item_id = str(row["id"])
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.mailbox.send",
                    request["idempotency_key"],
                    fingerprint,
                    "network_mailbox_item",
                    item_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.mailbox.send",
                    "network_mailbox_item",
                    item_id,
                    "succeeded",
                    {
                        "sender_kind": sender["kind"],
                        "recipient_kind": recipient["kind"],
                    },
                    timestamp,
                )
                self._outbox(
                    connection,
                    team_id,
                    "network_mailbox_item",
                    item_id,
                    "network.mailbox.available",
                    timestamp,
                )
                return response
        finally:
            connection.close()

    def _require_network_address_owner(
        self,
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
        address_kind: str,
        address_id: str,
    ) -> tuple[str | None, str | None]:
        identifier = _identity(address_id)
        if address_kind == "human":
            if (
                address_id != identifier
                or claims.auth_kind != "human"
                or identifier != claims.principal_id
            ):
                raise HubError("not_found", "Resource not found", 404)
            human = connection.execute(
                """
                SELECT p.id FROM principals AS p
                JOIN memberships AS m
                  ON m.team_id=? AND m.principal_id=p.id
                WHERE p.id=? AND p.kind='human' AND p.status='active'
                  AND m.status='active'
                """,
                (team_id, identifier),
            ).fetchone()
            if human is None:
                raise HubError("not_found", "Resource not found", 404)
            return None, None
        node = self._caller_network_node(connection, claims, team_id)
        if address_kind == "server":
            if identifier != node["node_id"]:
                raise HubError("not_found", "Resource not found", 404)
            return str(node["node_id"]), None
        if address_kind == "agent":
            agent = connection.execute(
                """
                SELECT id FROM agents
                WHERE team_id=? AND node_id=? AND id=? AND status='active'
                """,
                (team_id, node["node_id"], identifier),
            ).fetchone()
            if agent is None:
                raise HubError("not_found", "Resource not found", 404)
            return str(node["node_id"]), identifier
        raise HubError("invalid_request", "Mailbox address kind is invalid", 422)

    def list_network_mailbox(
        self,
        claims: AccessClaims,
        team_id: str,
        *,
        address_kind: str,
        address_id: str,
        after_sequence: int,
        limit: int,
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_network_scope(connection, claims, team_id, write=False)
            node_id, agent_id = self._require_network_address_owner(
                connection, claims, team_id, address_kind, address_id
            )
            bounded_limit = max(1, min(int(limit), MAX_NETWORK_PAGE_ITEMS))
            clean_after = max(0, int(after_sequence))
            if address_kind == "human":
                routing = (
                    "i.recipient_kind='human' AND i.recipient_principal_id=?"
                )
                routing_values = (claims.principal_id,)
            elif agent_id is None:
                routing = "i.recipient_kind='server' AND i.recipient_node_id=?"
                routing_values: tuple[Any, ...] = (node_id,)
            else:
                routing = (
                    "i.recipient_kind='agent' AND i.recipient_node_id=? "
                    "AND i.recipient_agent_id=?"
                )
                routing_values = (node_id, agent_id)
            rows = connection.execute(
                self._network_item_select()
                + f"""
                WHERE i.team_id=? AND {routing} AND i.queue_ordinal>?
                ORDER BY i.queue_ordinal ASC LIMIT ?
                """,
                (team_id, *routing_values, clean_after, bounded_limit + 1),
            ).fetchall()
            response = self._bounded_network_page(
                rows,
                clean_after=clean_after,
                bounded_limit=bounded_limit,
                collection_key="items",
                sequence_column="queue_ordinal",
                render=lambda row: {
                    "item": self._network_item_public(row),
                    "delivery": self._network_delivery_public(row),
                },
            )
            connection.execute("COMMIT")
            return response
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _network_item_participant(
        self,
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
        row: sqlite3.Row,
    ) -> bool:
        if claims.auth_kind == "human" and claims.principal_id in {
            row["sender_principal_id"],
            row["recipient_principal_id"],
        }:
            return True
        node = self._caller_network_node(connection, claims, team_id)
        return node["node_id"] in {
            row["sender_node_id"],
            row["recipient_node_id"],
        }

    def get_network_item(
        self,
        claims: AccessClaims,
        team_id: str,
        item_id: str,
    ) -> dict[str, Any]:
        clean_id = _identity(item_id)
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_network_scope(connection, claims, team_id, write=False)
            row = connection.execute(
                self._network_item_select() + " WHERE i.team_id=? AND i.id=?",
                (team_id, clean_id),
            ).fetchone()
            if row is None or not self._network_item_participant(
                connection, claims, team_id, row
            ):
                raise HubError("not_found", "Resource not found", 404)
            response = {
                "item": self._network_item_public(row),
                "delivery": self._network_delivery_public(row),
            }
            connection.execute("COMMIT")
            return response
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def record_network_delivery_receipt(
        self,
        claims: AccessClaims,
        team_id: str,
        delivery_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = _now()
        clean_id = _identity(delivery_id)
        target_state = request.get("state")
        if target_state not in {"delivered", "read"}:
            raise HubError("invalid_request", "Receipt state is invalid", 422)
        fingerprint = canonical_fingerprint(
            {"team_id": team_id, "delivery_id": clean_id, "state": target_state}
        )
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_network_scope(connection, claims, team_id, write=True)
                row = connection.execute(
                    self._network_item_select()
                    + " WHERE i.team_id=? AND d.id=?",
                    (team_id, clean_id),
                ).fetchone()
                if row is None:
                    raise HubError("not_found", "Resource not found", 404)
                if (
                    claims.auth_kind == "human"
                    and row["recipient_kind"] == "human"
                    and row["recipient_principal_id"] == claims.principal_id
                ):
                    authorized = True
                else:
                    node = self._caller_network_node(
                        connection, claims, team_id
                    )
                    authorized = row["recipient_node_id"] == node["node_id"]
                if not authorized:
                    raise HubError("not_found", "Resource not found", 404)
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.delivery.receipt",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                current = str(row["delivery_state"])
                if (
                    target_state == "delivered"
                    and current in {"delivered", "read"}
                ) or (target_state == "read" and current == "read"):
                    return {"delivery": self._network_delivery_public(row)}
                if target_state == "delivered" and current != "available":
                    raise HubError(
                        "receipt_conflict", "Receipt state is invalid", 409
                    )
                if target_state == "read" and current == "available":
                    raise HubError(
                        "receipt_conflict",
                        "Delivery must be recorded before it is read",
                        409,
                    )
                if target_state == "read" and current != "delivered":
                    raise HubError(
                        "receipt_conflict", "Receipt state is invalid", 409
                    )
                self._charge_network_peer_write(
                    connection, claims, team_id, 0, timestamp
                )
                if target_state == "delivered":
                    connection.execute(
                        """
                        UPDATE network_deliveries
                        SET state='delivered',delivered_at=? WHERE id=?
                        """,
                        (timestamp, clean_id),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE network_deliveries
                        SET state='read',read_at=? WHERE id=?
                        """,
                        (timestamp, clean_id),
                    )
                updated = connection.execute(
                    self._network_item_select()
                    + " WHERE i.team_id=? AND d.id=?",
                    (team_id, clean_id),
                ).fetchone()
                assert updated is not None
                response = {"delivery": self._network_delivery_public(updated)}
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.delivery.receipt",
                    request["idempotency_key"],
                    fingerprint,
                    "network_delivery",
                    clean_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.delivery.receipt",
                    "network_delivery",
                    clean_id,
                    "succeeded",
                    {"state": target_state},
                    timestamp,
                )
                self._outbox(
                    connection,
                    team_id,
                    "network_delivery",
                    clean_id,
                    f"network.delivery.{target_state}",
                    timestamp,
                )
                return response
        finally:
            connection.close()

    @staticmethod
    def _network_request_public(
        request_row: sqlite3.Row,
        *,
        timestamp: int,
    ) -> dict[str, Any]:
        status = str(request_row["request_status"])
        if status == "open" and int(request_row["request_expires_at"]) <= timestamp:
            status = "expired"
        return {
            "id": request_row["request_item_id"],
            "status": status,
            "expires_at": _iso8601(request_row["request_expires_at"]),
            "reply_item_id": request_row["reply_item_id"],
        }

    def create_network_request(
        self,
        claims: AccessClaims,
        team_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = _now()
        body, body_format, body_bytes = self._network_body(request)
        ttl = int(request.get("expires_in_seconds", 86_400))
        if not 60 <= ttl <= 86_400:
            raise HubError("invalid_request", "Request expiry is invalid", 422)
        expires_at = timestamp + ttl
        fingerprint = canonical_fingerprint(
            {
                "team_id": team_id,
                "to": request.get("to"),
                "from_agent_id": request.get("from_agent_id"),
                "body": body,
                "body_format": body_format,
                "expires_in_seconds": ttl,
            }
        )
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_network_scope(connection, claims, team_id, write=True)
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.request.create",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                sender = self._network_sender(
                    connection, claims, team_id, request.get("from_agent_id")
                )
                recipient = self._network_recipient(
                    connection, team_id, dict(request.get("to") or {})
                )
                self._charge_network_peer_write(
                    connection, claims, team_id, body_bytes, timestamp
                )
                response, row = self._insert_network_item(
                    connection,
                    team_id=team_id,
                    kind="request",
                    sender=sender,
                    recipient=recipient,
                    body=body,
                    body_format=body_format,
                    operation="network.request.create",
                    idempotency_key=request["idempotency_key"],
                    timestamp=timestamp,
                    expires_at=expires_at,
                )
                item_id = str(row["id"])
                connection.execute(
                    """
                    INSERT INTO network_passive_requests(
                        request_item_id,team_id,status,created_at,expires_at
                    ) VALUES (?,?,'open',?,?)
                    """,
                    (item_id, team_id, timestamp, expires_at),
                )
                request_row = connection.execute(
                    """
                    SELECT request_item_id,status AS request_status,
                           expires_at AS request_expires_at,reply_item_id
                    FROM network_passive_requests WHERE request_item_id=?
                    """,
                    (item_id,),
                ).fetchone()
                assert request_row is not None
                response["request"] = self._network_request_public(
                    request_row, timestamp=timestamp
                )
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.request.create",
                    request["idempotency_key"],
                    fingerprint,
                    "network_request",
                    item_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.request.create",
                    "network_request",
                    item_id,
                    "succeeded",
                    {
                        "sender_kind": sender["kind"],
                        "recipient_kind": recipient["kind"],
                        "expires_at": expires_at,
                        "passive": True,
                    },
                    timestamp,
                )
                self._outbox(
                    connection,
                    team_id,
                    "network_request",
                    item_id,
                    "network.request.available",
                    timestamp,
                )
                return response
        finally:
            connection.close()

    def _request_participant_row(
        self,
        connection: sqlite3.Connection,
        claims: AccessClaims,
        team_id: str,
        request_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            self._network_item_select()
            + """
            JOIN network_passive_requests AS pr
              ON pr.team_id=i.team_id AND pr.request_item_id=i.id
            WHERE i.team_id=? AND i.id=? AND i.kind='request'
            """,
            (team_id, request_id),
        ).fetchone()
        if row is None or not self._network_item_participant(
            connection, claims, team_id, row
        ):
            raise HubError("not_found", "Resource not found", 404)
        return row

    def get_network_request(
        self,
        claims: AccessClaims,
        team_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        clean_id = _identity(request_id)
        timestamp = _now()
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_network_scope(connection, claims, team_id, write=False)
            row = self._request_participant_row(
                connection, claims, team_id, clean_id
            )
            response: dict[str, Any] = {
                "item": self._network_item_public(row),
                "delivery": self._network_delivery_public(row),
                "request": self._network_request_public(row, timestamp=timestamp),
                "reply": None,
            }
            if row["reply_item_id"] is not None:
                reply = connection.execute(
                    self._network_item_select() + " WHERE i.id=?",
                    (row["reply_item_id"],),
                ).fetchone()
                if reply is None:
                    raise RuntimeError("passive request reply is missing")
                response["reply"] = {
                    "item": self._network_item_public(reply),
                    "delivery": self._network_delivery_public(reply),
                }
            connection.execute("COMMIT")
            return response
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _network_reply_recipient(
        connection: sqlite3.Connection,
        team_id: str,
        request_item: sqlite3.Row,
    ) -> dict[str, str | None]:
        sender_kind = str(request_item["sender_kind"])
        if sender_kind == "human":
            human = connection.execute(
                """
                SELECT p.id FROM principals AS p
                JOIN memberships AS m
                  ON m.team_id=? AND m.principal_id=p.id
                WHERE p.id=? AND p.kind='human' AND p.status='active'
                  AND m.status='active'
                """,
                (team_id, request_item["sender_principal_id"]),
            ).fetchone()
            if human is None:
                raise HubError(
                    "request_unavailable", "Requester is unavailable", 409
                )
            return {
                "kind": "human",
                "principal_id": str(human["id"]),
                "node_id": None,
                "agent_id": None,
            }
        if sender_kind == "server":
            node = connection.execute(
                """
                SELECT n.id,n.principal_id FROM nodes AS n
                JOIN principals AS p ON p.id=n.principal_id
                WHERE n.team_id=? AND n.id=? AND n.status='active'
                  AND p.status='active'
                """,
                (team_id, request_item["sender_node_id"]),
            ).fetchone()
            if node is None:
                raise HubError(
                    "request_unavailable", "Requester is unavailable", 409
                )
            return {
                "kind": "server",
                "principal_id": str(node["principal_id"]),
                "node_id": str(node["id"]),
                "agent_id": None,
            }
        agent = connection.execute(
            """
            SELECT a.id,a.node_id,a.principal_id FROM agents AS a
            JOIN principals AS p ON p.id=a.principal_id
            JOIN nodes AS n ON n.team_id=a.team_id AND n.id=a.node_id
            WHERE a.team_id=? AND a.id=? AND a.node_id=?
              AND a.status='active' AND p.status='active' AND n.status='active'
            """,
            (
                team_id,
                request_item["sender_agent_id"],
                request_item["sender_node_id"],
            ),
        ).fetchone()
        if agent is None:
            raise HubError("request_unavailable", "Requester is unavailable", 409)
        return {
            "kind": "agent",
            "principal_id": str(agent["principal_id"]),
            "node_id": str(agent["node_id"]),
            "agent_id": str(agent["id"]),
        }

    def create_network_request_reply(
        self,
        claims: AccessClaims,
        team_id: str,
        request_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        clean_id = _identity(request_id)
        timestamp = _now()
        body, body_format, body_bytes = self._network_body(request)
        fingerprint = canonical_fingerprint(
            {
                "team_id": team_id,
                "request_id": clean_id,
                "from_agent_id": request.get("from_agent_id"),
                "body": body,
                "body_format": body_format,
            }
        )
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_network_scope(connection, claims, team_id, write=True)
                request_item = connection.execute(
                    self._network_item_select()
                    + """
                    JOIN network_passive_requests AS pr
                      ON pr.team_id=i.team_id AND pr.request_item_id=i.id
                    WHERE i.team_id=? AND i.id=? AND i.kind='request'
                    """,
                    (team_id, clean_id),
                ).fetchone()
                if request_item is None:
                    raise HubError("not_found", "Resource not found", 404)
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.request.reply",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                if request_item["request_status"] != "open" or int(
                    request_item["request_expires_at"]
                ) <= timestamp:
                    raise HubError(
                        "request_unavailable", "Passive request is no longer open", 409
                    )
                if (
                    claims.auth_kind == "human"
                    and request_item["recipient_kind"] == "human"
                    and request_item["recipient_principal_id"]
                    == claims.principal_id
                ):
                    authorized = True
                else:
                    bound = self._caller_network_node(
                        connection, claims, team_id
                    )
                    authorized = (
                        request_item["recipient_node_id"] == bound["node_id"]
                    )
                if not authorized:
                    raise HubError("not_found", "Resource not found", 404)
                from_agent_id = request.get("from_agent_id")
                if request_item["recipient_kind"] == "agent":
                    if claims.auth_kind == "human":
                        agent_author_matches = from_agent_id is None
                    else:
                        agent_author_matches = (
                            from_agent_id == request_item["recipient_agent_id"]
                        )
                    if not agent_author_matches:
                        raise HubError(
                            "forbidden",
                            "The addressed agent must author this reply",
                            403,
                        )
                sender = self._network_sender(
                    connection, claims, team_id, from_agent_id
                )
                recipient = self._network_reply_recipient(
                    connection, team_id, request_item
                )
                self._charge_network_peer_write(
                    connection, claims, team_id, body_bytes, timestamp
                )
                response, reply_row = self._insert_network_item(
                    connection,
                    team_id=team_id,
                    kind="reply",
                    sender=sender,
                    recipient=recipient,
                    body=body,
                    body_format=body_format,
                    operation="network.request.reply",
                    idempotency_key=request["idempotency_key"],
                    timestamp=timestamp,
                    root_request_item_id=clean_id,
                )
                reply_id = str(reply_row["id"])
                changed = connection.execute(
                    """
                    UPDATE network_passive_requests
                    SET status='replied',reply_item_id=?,replied_at=?
                    WHERE request_item_id=? AND status='open'
                    """,
                    (reply_id, timestamp, clean_id),
                ).rowcount
                if changed != 1:
                    raise HubError(
                        "request_unavailable", "Passive request is no longer open", 409
                    )
                updated_request = connection.execute(
                    """
                    SELECT request_item_id,status AS request_status,
                           expires_at AS request_expires_at,reply_item_id
                    FROM network_passive_requests WHERE request_item_id=?
                    """,
                    (clean_id,),
                ).fetchone()
                assert updated_request is not None
                response["request"] = self._network_request_public(
                    updated_request, timestamp=timestamp
                )
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.request.reply",
                    request["idempotency_key"],
                    fingerprint,
                    "network_request_reply",
                    reply_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "network.request.reply",
                    "network_request",
                    clean_id,
                    "succeeded",
                    {"reply_item_id": reply_id, "passive": True},
                    timestamp,
                )
                self._outbox(
                    connection,
                    team_id,
                    "network_request_reply",
                    reply_id,
                    "network.request.replied",
                    timestamp,
                )
                return response
        finally:
            connection.close()

    def list_channels(self, claims: AccessClaims, team_id: str) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            _require_team_role(
                connection,
                team_id,
                claims.principal_id,
                ("owner", "admin", "member", "guest", "automation"),
            )
            rows = connection.execute(
                """
                SELECT * FROM channels WHERE team_id = ? AND archived_at IS NULL
                ORDER BY kind, display_name COLLATE NOCASE, id
                """,
                (team_id,),
            ).fetchall()
            channels = [
                self._channel_dict(connection, row, claims.principal_id)
                for row in rows
                if self._channel_permission(connection, row, claims.principal_id, "read")
            ]
            connection.execute("COMMIT")
            return {"channels": channels}
        except (AuthorizationError, AuthenticationError) as exc:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise HubError("not_found", "Resource not found", 404) from exc
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def create_channel(
        self,
        claims: AccessClaims,
        team_id: str,
        request: dict[str, Any],
    ) -> dict[str, Any]:
        timestamp = _now()
        request = dict(request)
        request["participant_principal_ids"] = sorted(
            set(request.get("participant_principal_ids") or [])
        )
        if request.get("kind") != "direct" and request.get("visibility") == "private":
            request["participant_principal_ids"] = sorted(
                set(request["participant_principal_ids"] + [claims.principal_id])
            )
        fingerprint = canonical_fingerprint(
            {"team_id": team_id, **{k: v for k, v in request.items() if k != "idempotency_key"}}
        )
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_session(connection, claims, timestamp)
                membership = _require_team_role(
                    connection,
                    team_id,
                    claims.principal_id,
                    ("owner", "admin", "member"),
                )
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "channel.create",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                kind = request["kind"]
                visibility = request["visibility"]
                participants = sorted(set(request.get("participant_principal_ids") or []))
                if kind == "direct":
                    if visibility != "private" or request.get("slug") is not None:
                        raise HubError("invalid_request", "Direct channels are private and have no slug", 422)
                    if len(participants) != 2 or claims.principal_id not in participants:
                        raise HubError(
                            "invalid_request",
                            "Direct channels require exactly the caller and one other participant",
                            422,
                        )
                    pair_key = hashlib.sha256("\0".join(participants).encode("utf-8")).digest()
                    slug = None
                    display_name = (
                        _bounded_text(request["display_name"], "display_name", 1, 160)
                        if request.get("display_name")
                        else None
                    )
                else:
                    if kind not in ("board", "announcements"):
                        raise HubError("invalid_request", "Unknown channel kind", 422)
                    if kind == "announcements" and membership["role"] not in ("owner", "admin"):
                        raise HubError("forbidden", "Operation is not permitted", 403)
                    slug_value = _bounded_text(request.get("slug") or "", "slug", 1, 80).lower()
                    if not all(character.isalnum() or character in "-_" for character in slug_value):
                        raise HubError("invalid_request", "slug contains unsupported characters", 422)
                    slug = slug_value
                    display_name = _bounded_text(
                        request.get("display_name") or "", "display_name", 1, 160
                    )
                    pair_key = None
                    if visibility == "private" and claims.principal_id not in participants:
                        participants.append(claims.principal_id)
                        participants.sort()
                for participant in participants:
                    valid = connection.execute(
                        """
                        SELECT 1 FROM principals AS p
                        WHERE p.id = ? AND p.status = 'active'
                          AND (
                            p.scope_team_id = ?
                            OR EXISTS (
                              SELECT 1 FROM memberships AS m
                              WHERE m.team_id = ? AND m.principal_id = p.id
                                AND m.status = 'active'
                            )
                          )
                        """,
                        (participant, team_id, team_id),
                    ).fetchone()
                    if valid is None:
                        raise HubError("invalid_request", "Channel participant is unavailable", 422)
                channel_id = _id("channel")
                connection.execute(
                    """
                    INSERT INTO channels(
                        id, team_id, kind, visibility, slug, display_name,
                        direct_pair_key, created_by_principal_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        channel_id,
                        team_id,
                        kind,
                        visibility,
                        slug,
                        display_name,
                        pair_key,
                        claims.principal_id,
                        timestamp,
                        timestamp,
                    ),
                )
                for participant in participants:
                    connection.execute(
                        """
                        INSERT INTO channel_participants(
                            team_id, channel_id, principal_id, participant_role,
                            status, joined_at, updated_at
                        ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                        """,
                        (
                            team_id,
                            channel_id,
                            participant,
                            "manager" if participant == claims.principal_id else "member",
                            timestamp,
                            timestamp,
                        ),
                    )
                if kind == "direct" or visibility == "private":
                    for participant in participants:
                        connection.execute(
                            """
                            INSERT INTO channel_acl_entries(
                                id, team_id, channel_id, subject_kind,
                                subject_principal_id, subject_role,
                                can_read, can_post, can_manage, can_dispatch, created_at
                            ) VALUES (?, ?, ?, 'principal', ?, NULL, 1, 1, ?, 0, ?)
                            """,
                            (
                                _id("channel_acl"),
                                team_id,
                                channel_id,
                                participant,
                                1 if participant == claims.principal_id else 0,
                                timestamp,
                            ),
                        )
                else:
                    role_permissions = {
                        "owner": (1, 1, 1),
                        "admin": (1, 1, 1),
                        "member": (1, 0 if kind == "announcements" else 1, 0),
                        "guest": (1, 0, 0),
                        # Secure paired servers are automation principals. The
                        # gateway separately checks the peer's live mTLS
                        # certificate and teamspace.read/write scope on every
                        # request; this ACL only makes shared channels visible.
                        "automation": (
                            1,
                            0 if kind == "announcements" else 1,
                            0,
                        ),
                    }
                    for role, (can_read, can_post, can_manage) in role_permissions.items():
                        connection.execute(
                            """
                            INSERT INTO channel_acl_entries(
                                id, team_id, channel_id, subject_kind,
                                subject_principal_id, subject_role,
                                can_read, can_post, can_manage, can_dispatch, created_at
                            ) VALUES (?, ?, ?, 'role', NULL, ?, ?, ?, ?, 0, ?)
                            """,
                            (
                                _id("channel_acl"),
                                team_id,
                                channel_id,
                                role,
                                can_read,
                                can_post,
                                can_manage,
                                timestamp,
                            ),
                        )
                row = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
                assert row is not None
                response = {
                    "channel": self._channel_dict(
                        connection, row, claims.principal_id
                    )
                }
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "channel.create",
                    request["idempotency_key"],
                    fingerprint,
                    "channel",
                    channel_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "channel.create",
                    "channel",
                    channel_id,
                    "succeeded",
                    {"kind": kind, "visibility": visibility},
                    timestamp,
                )
                self._outbox(
                    connection, team_id, "channel", channel_id, "channel.created", timestamp
                )
                return response
        except AuthorizationError as exc:
            raise HubError("forbidden", "Operation is not permitted", 403) from exc
        except sqlite3.IntegrityError as exc:
            raise HubError("conflict", "Channel already exists", 409) from exc
        finally:
            connection.close()

    @staticmethod
    def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "team_id": row["team_id"],
            "channel_id": row["channel_id"],
            "channel_sequence": row["channel_sequence"],
            "kind": row["kind"],
            "thread_root_message_id": row["thread_root_message_id"],
            "parent_message_id": row["parent_message_id"],
            "author_principal_id": row["author_principal_id"],
            "body_format": row["body_format"],
            "body": row["body"],
            "created_at": _iso8601(row["created_at"]),
            "edited_at": _iso8601(row["edited_at"]),
            "deleted_at": _iso8601(row["deleted_at"]),
        }

    def list_messages(
        self, claims: AccessClaims, channel_id: str, limit: int, before_sequence: int | None
    ) -> dict[str, Any]:
        connection = self.connect()
        try:
            connection.execute("BEGIN")
            self._require_session(connection, claims, _now())
            channel = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
            if channel is None or not self._channel_permission(
                connection, channel, claims.principal_id, "read"
            ):
                raise HubError("not_found", "Resource not found", 404)
            bounded_limit = max(1, min(int(limit), 100))
            cutoff = before_sequence if before_sequence is not None else 2**63 - 1
            rows = connection.execute(
                """
                SELECT * FROM messages
                WHERE channel_id = ? AND channel_sequence < ? AND deleted_at IS NULL
                ORDER BY channel_sequence DESC LIMIT ?
                """,
                (channel_id, cutoff, bounded_limit),
            ).fetchall()
            messages = [self._message_dict(row) for row in reversed(rows)]
            next_before = int(rows[-1]["channel_sequence"]) if len(rows) == bounded_limit else None
            connection.execute("COMMIT")
            return {"messages": messages, "next_before_sequence": next_before}
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def create_message(
        self, claims: AccessClaims, channel_id: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        timestamp = _now()
        connection = self.connect()
        try:
            with _write_transaction(connection):
                self._require_session(connection, claims, timestamp)
                channel = connection.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
                if channel is None or not self._channel_permission(
                    connection, channel, claims.principal_id, "post"
                ):
                    raise HubError("not_found", "Resource not found", 404)
                team_id = str(channel["team_id"])
                body = request["body"]
                try:
                    encoded_body = body.encode("utf-8") if isinstance(body, str) else b""
                except UnicodeEncodeError as exc:
                    raise HubError("invalid_request", "Message body is invalid", 422) from exc
                if not isinstance(body, str) or not 1 <= len(encoded_body) <= 65536:
                    raise HubError("invalid_request", "Message body is invalid", 422)
                if request.get("body_format") not in ("plain", "markdown"):
                    raise HubError("invalid_request", "Message body format is invalid", 422)
                kind = request["kind"]
                if channel["kind"] == "announcements" and kind != "announcement":
                    raise HubError("invalid_request", "Announcements channels accept announcements only", 422)
                if kind == "announcement" and channel["kind"] != "announcements":
                    raise HubError("invalid_request", "Announcements require an announcements channel", 422)
                if kind != "announcement" and kind != "post":
                    raise HubError("invalid_request", "Unknown message kind", 422)
                fingerprint = canonical_fingerprint(
                    {"channel_id": channel_id, **{k: v for k, v in request.items() if k != "idempotency_key"}}
                )
                cached = self._idempotency_lookup(
                    connection,
                    team_id,
                    claims.principal_id,
                    "message.create",
                    request["idempotency_key"],
                    fingerprint,
                )
                if cached is not None:
                    return cached
                if claims.auth_kind == "secure_peer":
                    subject = f"secure-peer:{claims.peer_id or claims.principal_id}"
                    # These durable buckets bound remote write amplification
                    # even across gateway/server restarts. Replays with the
                    # same idempotency key return above without being charged.
                    self._charge_rate_bucket(
                        connection,
                        team_id=team_id,
                        subject_key=subject,
                        action="peer.message.count.minute",
                        timestamp=timestamp,
                        window_seconds=60,
                        cost=1,
                        limit=60,
                    )
                    self._charge_rate_bucket(
                        connection,
                        team_id=team_id,
                        subject_key=subject,
                        action="peer.message.count.day",
                        timestamp=timestamp,
                        window_seconds=86_400,
                        cost=1,
                        limit=5_000,
                    )
                    self._charge_rate_bucket(
                        connection,
                        team_id=team_id,
                        subject_key=subject,
                        action="peer.message.bytes.hour",
                        timestamp=timestamp,
                        window_seconds=3_600,
                        cost=len(encoded_body),
                        limit=4 * 1024 * 1024,
                    )
                root_id = request.get("thread_root_message_id")
                parent_id = request.get("parent_message_id")
                if parent_id is not None and root_id is None:
                    raise HubError("invalid_request", "A reply requires a thread root", 422)
                if root_id is not None:
                    root = connection.execute(
                        """
                        SELECT id FROM messages
                        WHERE id = ? AND channel_id = ? AND deleted_at IS NULL
                          AND thread_root_message_id IS NULL AND parent_message_id IS NULL
                        """,
                        (root_id, channel_id),
                    ).fetchone()
                    if root is None:
                        raise HubError("invalid_request", "Thread root is unavailable", 422)
                if parent_id is not None:
                    parent = connection.execute(
                        """
                        SELECT id, thread_root_message_id FROM messages
                        WHERE id = ? AND channel_id = ? AND deleted_at IS NULL
                        """,
                        (parent_id, channel_id),
                    ).fetchone()
                    if parent is None or (
                        str(parent["id"]) != root_id
                        and str(parent["thread_root_message_id"]) != root_id
                    ):
                        raise HubError("invalid_request", "Thread parent is unavailable", 422)
                sequence = int(channel["next_message_sequence"])
                connection.execute(
                    "UPDATE channels SET next_message_sequence = ?, updated_at = ? WHERE id = ?",
                    (sequence + 1, timestamp, channel_id),
                )
                message_id = _id("message")
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, team_id, channel_id, channel_sequence, kind,
                        thread_root_message_id, parent_message_id,
                        author_principal_id, body_format, body,
                        idempotency_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        team_id,
                        channel_id,
                        sequence,
                        kind,
                        root_id,
                        parent_id,
                        claims.principal_id,
                        request["body_format"],
                        body,
                        hashlib.sha256(
                            f"{team_id}\0{claims.principal_id}\0message.create\0{request['idempotency_key']}".encode(
                                "utf-8"
                            )
                        ).digest(),
                        timestamp,
                    ),
                )
                if channel["kind"] == "direct":
                    for recipient in connection.execute(
                        """
                        SELECT principal_id FROM channel_participants
                        WHERE channel_id = ? AND status = 'active' AND principal_id <> ?
                        """,
                        (channel_id, claims.principal_id),
                    ):
                        recipient_id = _id("recipient")
                        connection.execute(
                            """
                            INSERT INTO message_recipients(
                                id, team_id, message_id, recipient_principal_id,
                                reason, delivery_key, state, created_at
                            ) VALUES (?, ?, ?, ?, 'direct', ?, 'available', ?)
                            """,
                            (
                                recipient_id,
                                team_id,
                                message_id,
                                recipient["principal_id"],
                                hashlib.sha256(f"{message_id}\0{recipient['principal_id']}".encode()).digest(),
                                timestamp,
                            ),
                        )
                row = connection.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
                assert row is not None
                response = {"message": self._message_dict(row)}
                self._idempotency_store(
                    connection,
                    team_id,
                    claims.principal_id,
                    "message.create",
                    request["idempotency_key"],
                    fingerprint,
                    "message",
                    message_id,
                    response,
                    timestamp,
                )
                self._audit(
                    connection,
                    team_id,
                    claims.principal_id,
                    "message.post",
                    "message",
                    message_id,
                    "succeeded",
                    {"channel_id": channel_id, "kind": kind},
                    timestamp,
                )
                self._outbox(
                    connection, team_id, "message", message_id, "message.created", timestamp
                )
                return response
        finally:
            connection.close()

    @staticmethod
    def _charge_rate_bucket(
        connection: sqlite3.Connection,
        *,
        team_id: str,
        subject_key: str,
        action: str,
        timestamp: int,
        window_seconds: int,
        cost: int,
        limit: int,
    ) -> None:
        if cost < 0 or limit <= 0 or window_seconds <= 0:
            raise RuntimeError("invalid durable rate bucket")
        row = connection.execute(
            """
            SELECT window_started_at, count FROM rate_limit_buckets
            WHERE team_id=? AND subject_key=? AND action=?
            """,
            (team_id, subject_key, action),
        ).fetchone()
        if row is None or int(row["window_started_at"]) + window_seconds <= timestamp:
            window_started = timestamp
            next_count = cost
        else:
            window_started = int(row["window_started_at"])
            next_count = int(row["count"]) + cost
        if next_count > limit:
            raise HubError(
                "rate_limited",
                "Secure peer write limit exceeded; retry after the current window",
                429,
            )
        connection.execute(
            """
            INSERT INTO rate_limit_buckets(
                team_id, subject_key, action, window_started_at, count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id, subject_key, action) DO UPDATE SET
                window_started_at=excluded.window_started_at,
                count=excluded.count,
                updated_at=excluded.updated_at
            """,
            (team_id, subject_key, action, window_started, next_count, timestamp),
        )

    @staticmethod
    def _outbox(
        connection: sqlite3.Connection,
        team_id: str,
        aggregate_type: str,
        aggregate_id: str,
        event_type: str,
        timestamp: int,
    ) -> None:
        effect_key = hashlib.sha256(
            f"{team_id}\0{aggregate_type}\0{aggregate_id}\0{event_type}".encode("utf-8")
        ).digest()
        connection.execute(
            """
            INSERT INTO outbox_events(
                id, team_id, aggregate_type, aggregate_id, event_type,
                metadata_json, idempotency_key, state, available_at,
                attempt_count, created_at
            ) VALUES (?, ?, ?, ?, ?, '{}', ?, 'pending', ?, 0, ?)
            """,
            (
                _id("outbox"),
                team_id,
                aggregate_type,
                aggregate_id,
                event_type,
                effect_key,
                timestamp,
                timestamp,
            ),
        )

    @classmethod
    def _audit_principal_teams(
        cls,
        connection: sqlite3.Connection,
        principal_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        timestamp: int,
    ) -> None:
        for row in connection.execute(
            """
            SELECT team_id FROM memberships
            WHERE principal_id = ? AND status = 'active' ORDER BY team_id
            """,
            (principal_id,),
        ):
            cls._audit(
                connection,
                str(row["team_id"]),
                principal_id,
                action,
                resource_type,
                resource_id,
                outcome,
                {},
                timestamp,
            )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        team_id: str,
        actor_principal_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        outcome: str,
        metadata: dict[str, Any],
        timestamp: int,
        *,
        node_id: str | None = None,
    ) -> None:
        head = connection.execute(
            "SELECT event_hash, sequence FROM audit_chain_heads WHERE team_id = ?",
            (team_id,),
        ).fetchone()
        previous_hash = head["event_hash"] if head is not None else None
        sequence = int(head["sequence"]) + 1 if head is not None else 1
        event_id = _id("audit")
        metadata_json = canonical_json(metadata).decode("utf-8")
        event_hash = hashlib.sha256(
            canonical_json(
                {
                    "id": event_id,
                    "team_id": team_id,
                    "actor_principal_id": actor_principal_id,
                    "node_id": node_id,
                    "action": action,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "outcome": outcome,
                    "metadata": metadata,
                    "previous_event_hash": previous_hash.hex() if previous_hash else None,
                    "sequence": sequence,
                    "created_at": timestamp,
                }
            )
        ).digest()
        connection.execute(
            """
            INSERT INTO audit_events(
                id, team_id, actor_principal_id, node_id, action,
                resource_type, resource_id, outcome, metadata_json,
                previous_event_hash, event_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                team_id,
                actor_principal_id,
                node_id,
                action,
                resource_type,
                resource_id,
                outcome,
                metadata_json,
                previous_hash,
                event_hash,
                timestamp,
            ),
        )
        if head is None:
            connection.execute(
                """
                INSERT INTO audit_chain_heads(team_id, event_id, event_hash, sequence, updated_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (team_id, event_id, event_hash, timestamp),
            )
        else:
            connection.execute(
                """
                UPDATE audit_chain_heads
                SET event_id = ?, event_hash = ?, sequence = ?, updated_at = ?
                WHERE team_id = ?
                """,
                (event_id, event_hash, sequence, timestamp, team_id),
            )
