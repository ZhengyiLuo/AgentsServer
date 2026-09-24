"""Temporary-only SQLite admission/performance regressions; no server import."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from agentsdock_team_hub import database
from agentsdock_team_hub.database import (
    LATEST_SCHEMA_VERSION,
    MIGRATIONS,
    MigrationError,
    apply_migrations,
    open_database,
)


class DatabaseReadFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "hub.sqlite3"

    def initialize(self) -> None:
        open_database(self.path).close()

    def change_database(self, *statements: str) -> None:
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            for statement in statements:
                connection.execute(statement)
        finally:
            connection.close()

    def test_current_wal_open_does_not_wait_for_an_existing_writer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hub.sqlite3"
            initialized = open_database(path)
            initialized.close()
            writer = sqlite3.connect(path, isolation_level=None)
            self.addCleanup(writer.close)
            writer.execute("BEGIN IMMEDIATE")
            reader = sqlite3.connect(path, isolation_level=None)
            try:
                self.assertEqual(
                    reader.execute("PRAGMA user_version").fetchone()[0],
                    LATEST_SCHEMA_VERSION,
                )
            finally:
                reader.close()

            def open_and_read() -> tuple[int, float]:
                started = time.monotonic()
                connection = open_database(path)
                try:
                    return (
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        time.monotonic() - started,
                    )
                finally:
                    connection.close()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(open_and_read)
                try:
                    version, elapsed = future.result(timeout=1.0)
                finally:
                    writer.execute("ROLLBACK")
                self.assertEqual(version, LATEST_SCHEMA_VERSION)
                print(f"Current WAL open during held writer: {elapsed * 1000:.2f} ms")

    def test_current_wal_open_only_reads_ledger_without_initialization_lock(self) -> None:
        self.initialize()
        statements: list[str] = []
        original_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with (
            mock.patch.object(database.sqlite3, "connect", traced_connect),
            mock.patch.object(database, "apply_migrations", side_effect=AssertionError("writer migration")),
            mock.patch.object(database, "_initialization_lock", side_effect=AssertionError("initialization lock")),
        ):
            connection = open_database(self.path)
        try:
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA secure_delete").fetchone()[0], 1)
        finally:
            connection.close()
        normalized = [statement.upper() for statement in statements]
        self.assertIn("BEGIN", normalized)
        self.assertIn("ROLLBACK", normalized)
        self.assertFalse(any("BEGIN IMMEDIATE" in statement for statement in normalized))
        self.assertFalse(any("CREATE TABLE" in statement for statement in normalized))
        self.assertFalse(any("JOURNAL_MODE =" in statement for statement in normalized))
        self.assertTrue(any("ORDER BY VERSION LIMIT" in statement for statement in normalized))

    def test_every_new_connection_rechecks_checksum_and_name(self) -> None:
        for column, value in (("sha256", "0" * 64), ("name", "unexpected_name")):
            with self.subTest(column=column):
                path = self.root / f"{column}.sqlite3"
                open_database(path).close()
                connection = sqlite3.connect(path, isolation_level=None)
                connection.execute(
                    f"UPDATE schema_migrations SET {column} = ? WHERE version = 1",
                    (value,),
                )
                connection.close()
                with self.assertRaisesRegex(MigrationError, "checksum changed"):
                    open_database(path)

    def test_missing_middle_ledger_entry_is_not_current(self) -> None:
        self.initialize()
        self.change_database("DELETE FROM schema_migrations WHERE version = 1")
        with self.assertRaisesRegex(MigrationError, "cannot apply migration"):
            open_database(self.path)

    def test_newer_ledger_entry_is_not_current(self) -> None:
        self.initialize()
        self.change_database(
            "INSERT INTO schema_migrations VALUES "
            f"({LATEST_SCHEMA_VERSION + 1}, 'future', '{'0' * 64}', 1)"
        )
        with self.assertRaisesRegex(MigrationError, "newer than this Team Hub build"):
            open_database(self.path)

    def test_newer_user_version_is_rejected(self) -> None:
        self.initialize()
        self.change_database(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(MigrationError, "newer than this Team Hub build"):
            open_database(self.path)

    def test_mismatched_user_version_is_rejected(self) -> None:
        self.initialize()
        self.change_database(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION - 1}")
        with self.assertRaisesRegex(MigrationError, "does not match the migration ledger"):
            open_database(self.path)

    def test_current_ledger_and_version_share_one_read_snapshot(self) -> None:
        self.initialize()
        original_connect = sqlite3.connect
        raced = False
        path = self.path

        class SnapshotConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                nonlocal raced
                if sql == "PRAGMA user_version" and self.in_transaction and not raced:
                    # Commit a future version after the ledger SELECT. A
                    # coherent reader must still see its prior user_version.
                    raced = True
                    writer = original_connect(path, isolation_level=None)
                    try:
                        writer.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
                    finally:
                        writer.close()
                return super().execute(sql, parameters)

        def snapshot_connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=SnapshotConnection)

        with (
            mock.patch.object(database.sqlite3, "connect", snapshot_connect),
            mock.patch.object(database, "apply_migrations", side_effect=AssertionError("incoherent snapshot")),
        ):
            open_database(self.path).close()
        self.assertTrue(raced)
        with self.assertRaisesRegex(MigrationError, "newer than this Team Hub build"):
            open_database(self.path)

    def test_replaced_database_has_no_trusted_path_cache(self) -> None:
        self.initialize()
        replacement = self.root / "replacement.sqlite3"
        open_database(replacement).close()
        connection = sqlite3.connect(replacement, isolation_level=None)
        connection.execute("UPDATE schema_migrations SET sha256 = ? WHERE version = 1", ("0" * 64,))
        connection.close()
        os.replace(replacement, self.path)
        with self.assertRaisesRegex(MigrationError, "checksum changed"):
            open_database(self.path)

    def test_non_wal_current_database_uses_existing_initialization_path(self) -> None:
        self.initialize()
        self.change_database("PRAGMA journal_mode = DELETE")
        with mock.patch.object(database, "apply_migrations", wraps=apply_migrations) as migrate:
            connection = open_database(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            migrate.assert_called_once_with(connection)
        finally:
            connection.close()

    def test_outdated_wal_database_upgrades_through_existing_writer_path(self) -> None:
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, "
            "name TEXT NOT NULL, sha256 TEXT NOT NULL, applied_at INTEGER NOT NULL)"
        )
        migration = MIGRATIONS[0]
        connection.executescript(migration.source)
        connection.execute("INSERT INTO schema_migrations VALUES (?,?,?,1)",
                           (migration.version, migration.name, migration.sha256))
        connection.execute("PRAGMA user_version = 1")
        connection.close()
        with mock.patch.object(database, "apply_migrations", wraps=apply_migrations) as migrate:
            connection = open_database(self.path)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], LATEST_SCHEMA_VERSION)
            self.assertEqual(connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], len(MIGRATIONS))
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            migrate.assert_called_once_with(connection)
        finally:
            connection.close()

    def test_direct_apply_migrations_still_checks_under_writer_transaction(self) -> None:
        connection = open_database()
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        try:
            self.assertEqual(apply_migrations(connection), LATEST_SCHEMA_VERSION)
            self.assertIn("BEGIN IMMEDIATE", statements)
        finally:
            connection.close()

    def test_symlink_database_is_still_rejected_before_fast_path(self) -> None:
        self.initialize()
        link = self.root / "linked.sqlite3"
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            open_database(link)

    def test_hard_link_database_is_still_rejected_before_fast_path(self) -> None:
        self.initialize()
        link = self.root / "linked.sqlite3"
        os.link(self.path, link)
        with self.assertRaisesRegex(PermissionError, "hard-linked"):
            open_database(self.path)

    def test_database_path_swap_during_connect_is_still_rejected(self) -> None:
        self.initialize()
        replacement = self.root / "replacement.sqlite3"
        open_database(replacement).close()
        original_connect = sqlite3.connect

        def swapped_connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            os.replace(replacement, self.path)
            return connection

        with mock.patch.object(database.sqlite3, "connect", swapped_connect):
            with self.assertRaisesRegex(PermissionError, "path changed while opening"):
                open_database(self.path)

    def test_sqlite_implicit_rollback_does_not_mask_original_failure(self) -> None:
        self.initialize()
        original_connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                if sql.startswith("SELECT version, name, sha256"):
                    super().execute("ROLLBACK")
                    raise sqlite3.OperationalError("original simulated I/O error")
                return super().execute(sql, parameters)

        def failing_connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=FailingConnection)

        with mock.patch.object(database.sqlite3, "connect", failing_connect):
            with self.assertRaisesRegex(sqlite3.OperationalError, "original simulated I/O error"):
                open_database(self.path)

    def test_busy_read_probe_uses_existing_bounded_initialization_retry(self) -> None:
        self.initialize()
        original_connect = sqlite3.connect
        raced = False

        class BusyConnection(sqlite3.Connection):
            def execute(self, sql, parameters=(), /):
                nonlocal raced
                if sql == "PRAGMA journal_mode" and not raced:
                    raced = True
                    raise sqlite3.OperationalError("database is locked")
                return super().execute(sql, parameters)

        def busy_connect(*args, **kwargs):
            return original_connect(*args, **kwargs, factory=BusyConnection)

        with (
            mock.patch.object(database.sqlite3, "connect", busy_connect),
            mock.patch.object(database, "apply_migrations", wraps=apply_migrations) as migrate,
            mock.patch.object(database, "_retry_locked", wraps=database._retry_locked) as retry,
        ):
            connection = open_database(self.path)
        try:
            self.assertTrue(raced)
            migrate.assert_called_once_with(connection)
            self.assertEqual(retry.call_count, 2)
            self.assertFalse(connection.in_transaction)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
