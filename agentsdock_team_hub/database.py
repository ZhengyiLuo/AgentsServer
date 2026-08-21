"""SQLite connection and append-only migration support for Team Hub."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib import resources
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Iterator


MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_(?P<name>[a-z0-9_]+)\.sql$")
MIGRATION_PACKAGE = "agentsdock_team_hub.migrations"


class MigrationError(RuntimeError):
    """Raised when the on-disk schema and embedded migration chain disagree."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    source: str
    sha256: str


def _embedded_migrations() -> tuple[Migration, ...]:
    discovered: list[Migration] = []
    root = resources.files(MIGRATION_PACKAGE)
    for entry in root.iterdir():
        match = MIGRATION_NAME.fullmatch(entry.name)
        if match is None:
            continue
        source = entry.read_text(encoding="utf-8")
        discovered.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                source=source,
                sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
            )
        )
    discovered.sort(key=lambda item: item.version)
    expected = list(range(1, len(discovered) + 1))
    actual = [item.version for item in discovered]
    if not discovered or actual != expected:
        raise MigrationError(
            f"Team Hub migrations must be contiguous from 0001; found {actual}"
        )
    return tuple(discovered)


MIGRATIONS = _embedded_migrations()
LATEST_SCHEMA_VERSION = MIGRATIONS[-1].version


_INITIALIZATION_LOCKS_GUARD = threading.Lock()
_INITIALIZATION_LOCKS: dict[str, threading.Lock] = {}


def _initialization_lock(database: str) -> threading.Lock:
    """Serialize first-open WAL setup and migration within this process.

    SQLite's busy timeout does not reliably cover a simultaneous
    ``PRAGMA journal_mode=WAL`` on a brand-new database. A process lock avoids
    that common desktop/service race; the bounded retry below also covers a
    second Team Hub process opening the same file at the same time.
    """

    with _INITIALIZATION_LOCKS_GUARD:
        return _INITIALIZATION_LOCKS.setdefault(database, threading.Lock())


def _retry_locked(operation, *, timeout_seconds: float = 8.0):
    deadline = time.monotonic() + timeout_seconds
    delay = 0.01
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message and "busy" not in message:
                raise
            if time.monotonic() >= deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def _statements(source: str) -> Iterator[str]:
    """Split trusted migration SQL without breaking trigger bodies."""

    pending = ""
    for line in source.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            pending = ""
            if statement:
                yield statement
    if pending.strip():
        raise MigrationError("migration ends with an incomplete SQL statement")


def _configure(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise MigrationError("SQLite foreign-key enforcement could not be enabled")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA secure_delete = ON")


def _open_secure_database_file(target: Path) -> int:
    """Open a regular owner-only file before SQLite can create or mutate it."""

    if os.name == "nt":
        raise MigrationError(
            "Team Hub database ACL hardening is not implemented for Windows hosts"
        )
    parent = target.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise PermissionError(
            "Team Hub database directory must be an owner-controlled real directory"
        )
    parent_mode = stat.S_IMODE(parent.st_mode)
    if parent_mode & 0o022:
        raise PermissionError("Team Hub database directory must not be group/world writable")
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError("Team Hub database must be a regular file")
        if opened.st_nlink != 1:
            raise PermissionError("Team Hub database must not be hard-linked")
        os.fchmod(descriptor, 0o600)
        if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise PermissionError("Team Hub database permissions are not owner-only")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_database(path: str | os.PathLike[str] = ":memory:") -> sqlite3.Connection:
    """Open a hardened SQLite connection and apply every embedded migration."""

    database = os.fspath(path)
    secure_descriptor: int | None = None
    if database != ":memory:":
        target = Path(database).expanduser()
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        secure_descriptor = _open_secure_database_file(target)
        database = str(target)
    try:
        connection = sqlite3.connect(database, isolation_level=None, timeout=5.0)
        if secure_descriptor is not None:
            opened = os.fstat(secure_descriptor)
            linked = os.stat(database, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino):
                connection.close()
                raise PermissionError("Team Hub database path changed while opening")
            os.close(secure_descriptor)
            secure_descriptor = None
        _configure(connection)
        if database == ":memory:":
            apply_migrations(connection)
        else:
            with _initialization_lock(os.path.realpath(database)):
                _retry_locked(lambda: connection.execute("PRAGMA journal_mode = WAL"))
                _retry_locked(lambda: apply_migrations(connection))
        return connection
    except BaseException:
        if secure_descriptor is not None:
            os.close(secure_descriptor)
        if "connection" in locals():
            connection.close()
        raise


def apply_migrations(connection: sqlite3.Connection) -> int:
    """Apply and checksum-verify the immutable migration chain."""

    if connection.in_transaction:
        raise MigrationError("migrations require an autocommit connection")
    _configure(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(length(sha256) = 64),
            applied_at INTEGER NOT NULL
        )
        """
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        # Read the ledger only after taking the writer lock. A second process
        # opening the same fresh/outdated database then observes the first
        # process's committed migration rather than replaying stale decisions.
        applied = {
            int(row["version"]): (str(row["name"]), str(row["sha256"]))
            for row in connection.execute(
                "SELECT version, name, sha256 FROM schema_migrations ORDER BY version"
            )
        }
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        applied_version = max(applied, default=0)
        known_versions = {item.version for item in MIGRATIONS}
        unknown_versions = sorted(set(applied).difference(known_versions))
        if unknown_versions or user_version > LATEST_SCHEMA_VERSION:
            raise MigrationError(
                "database schema is newer than this Team Hub build "
                f"(ledger={unknown_versions}, user_version={user_version})"
            )
        if user_version != applied_version:
            raise MigrationError(
                "SQLite user_version does not match the migration ledger "
                f"({user_version} != {applied_version})"
            )

        for migration in MIGRATIONS:
            recorded = applied.get(migration.version)
            if recorded is not None:
                if recorded != (migration.name, migration.sha256):
                    raise MigrationError(
                        f"migration {migration.version:04d}_{migration.name} checksum changed"
                    )
                continue
            if migration.version != applied_version + 1:
                raise MigrationError(
                    f"cannot apply migration {migration.version}; "
                    f"expected {applied_version + 1}"
                )
            for statement in _statements(migration.source):
                normalized = statement.lstrip().upper()
                if normalized.startswith(("BEGIN", "COMMIT", "ROLLBACK")):
                    raise MigrationError(
                        f"migration {migration.version} contains transaction control"
                    )
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, sha256, applied_at)
                VALUES (?, ?, ?, CAST(strftime('%s', 'now') AS INTEGER))
                """,
                (migration.version, migration.name, migration.sha256),
            )
            connection.execute(f"PRAGMA user_version = {migration.version}")
            applied_version = migration.version
        connection.execute("COMMIT")
        return applied_version
    except BaseException:
        connection.execute("ROLLBACK")
        raise
