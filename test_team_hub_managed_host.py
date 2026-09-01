from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from agentsdock_team_hub.cli import main as cli_main
from agentsdock_team_hub.service import create_app
from agentsdock_team_hub.store import HubStore


HOST_A = "server-host-a-12345678"
HOST_B = "server-host-b-12345678"


class ManagedHostTests(unittest.TestCase):
    @staticmethod
    def downgrade_database_to_schema5(database_path: Path) -> None:
        connection = sqlite3.connect(database_path, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("BEGIN IMMEDIATE")
            for trigger in (
                "network_agents_limit_per_server",
                "network_bulletin_body_limit_on_insert",
                "network_bulletin_body_limit_on_update",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            for table in (
                "network_passive_requests",
                "network_deliveries",
                "network_mailbox_items",
                "network_boards",
                "network_peer_bindings",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("DELETE FROM schema_migrations WHERE version > 5")
            connection.execute("PRAGMA user_version = 5")
            connection.execute("COMMIT")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    @staticmethod
    def downgrade_database_to_schema4(
        database_path: Path,
        *,
        update_version_ledger: bool = True,
    ) -> None:
        connection = sqlite3.connect(database_path, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TRIGGER network_agents_limit_per_server")
            connection.execute("DROP TRIGGER network_bulletin_body_limit_on_insert")
            connection.execute("DROP TRIGGER network_bulletin_body_limit_on_update")
            for table in (
                "network_passive_requests",
                "network_deliveries",
                "network_mailbox_items",
                "network_boards",
                "network_peer_bindings",
            ):
                connection.execute(f"DROP TABLE {table}")
            for trigger in (
                "bootstrap_delegation_is_immutable",
                "bootstrap_delegations_cannot_be_deleted",
                "bootstrap_delegation_matches_claim_expiry",
                "bootstrap_delegation_matches_hub",
            ):
                connection.execute(f"DROP TRIGGER {trigger}")
            connection.execute("DROP TABLE bootstrap_delegations")
            if update_version_ledger:
                connection.execute("DELETE FROM schema_migrations WHERE version > 4")
                connection.execute("PRAGMA user_version = 4")
            connection.execute("COMMIT")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()

    @classmethod
    def make_schema4_snapshot(
        cls,
        store: HubStore,
        *,
        operation_id: str,
    ) -> Path:
        snapshot = store.maintenance_snapshot_and_fence(
            "server-update",
            operation_id=operation_id,
        )
        snapshot_database = snapshot / "team-hub.sqlite3"
        cls.downgrade_database_to_schema4(snapshot_database)
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["schema_version"] = 4
        manifest["database_sha256"] = hashlib.sha256(
            snapshot_database.read_bytes()
        ).hexdigest()
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
        )
        cls.downgrade_database_to_schema4(store.database_path)
        return snapshot

    def test_concurrent_first_bind_has_one_winner_and_foreign_copy_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"

            def bind(identity: str) -> tuple[str, str]:
                try:
                    store = HubStore(data_dir, managed_host_identity=identity)
                    return "accepted", store.hub_id
                except RuntimeError:
                    return "rejected", ""

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(bind, (HOST_A, HOST_B)))
            self.assertEqual(sorted(result[0] for result in results), ["accepted", "rejected"])
            winner = HOST_A if results[0][0] == "accepted" else HOST_B
            winner_store = HubStore(data_dir, managed_host_identity=winner)

            connection = winner_store.connect()
            try:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                binding = connection.execute(
                    "SELECT hub_id, server_identity FROM managed_host_bindings"
                ).fetchone()
                self.assertEqual(binding["hub_id"], winner_store.hub_id)
                self.assertEqual(binding["server_identity"], winner)
            finally:
                connection.close()

            copied = Path(temporary) / "copied-hub"
            shutil.copytree(data_dir, copied)
            before = {
                path.relative_to(copied).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in copied.rglob("*")
                if path.is_file()
            }
            foreign = HOST_B if winner == HOST_A else HOST_A
            with self.assertRaisesRegex(RuntimeError, "different AgentsServer"):
                HubStore(copied, managed_host_identity=foreign)
            after = {
                path.relative_to(copied).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in copied.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

            with self.assertRaisesRegex(RuntimeError, "cannot be served standalone"):
                create_app(data_dir)
            control = HubStore(data_dir, allow_bound_control=True)
            self.assertEqual(control.hub_id, winner_store.hub_id)

    def test_snapshot_is_verified_bounded_and_preserves_active_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            store = HubStore(data_dir, managed_host_identity=HOST_A)
            bootstrap_proof = store.bootstrap_proof_path.read_bytes()
            signing_key = store.signing_key_path.read_bytes()

            first = store.maintenance_snapshot("pre-update", keep=3)
            manifest = json.loads((first / "manifest.json").read_text())
            self.assertEqual(manifest["hub_id"], store.hub_id)
            self.assertEqual(manifest["host_server_identity"], HOST_A)
            self.assertEqual(
                (first / "access-token-signing.key").read_bytes(), signing_key
            )
            self.assertEqual(
                (first / "proofs" / "bootstrap-owner.proof").read_bytes(),
                bootstrap_proof,
            )
            backup = sqlite3.connect(first / "team-hub.sqlite3")
            try:
                self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                self.assertEqual(
                    backup.execute(
                        "SELECT server_identity FROM managed_host_bindings"
                    ).fetchone()[0],
                    HOST_A,
                )
            finally:
                backup.close()
            self.assertEqual(store.bootstrap_proof_path.read_bytes(), bootstrap_proof)

            proof = bootstrap_proof.decode("ascii").strip()
            store.bootstrap(proof, "owner@example.com", "Owner", "Owner Mac")
            recovery_path = store.issue_device_recovery(
                "owner@example.com", "Recovered Mac"
            )
            recovery_bytes = recovery_path.read_bytes()
            latest = store.maintenance_snapshot("pre-restart", keep=3)
            self.assertEqual(
                (latest / "proofs" / recovery_path.name).read_bytes(),
                recovery_bytes,
            )
            for index in range(3):
                store.maintenance_snapshot(f"bounded-{index}", keep=3)
            generations = list((data_dir / "maintenance-backups").glob("snapshot_*"))
            self.assertEqual(len(generations), 3)

    def test_offline_restore_verifies_identity_and_restores_db_key_and_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            store = HubStore(data_dir, managed_host_identity=HOST_A)
            hub_id = store.hub_id
            proof_bytes = store.bootstrap_proof_path.read_bytes()
            key_bytes = store.signing_key_path.read_bytes()
            snapshot = store.maintenance_snapshot_and_fence(
                "server-update",
                operation_id="update-restore",
            )
            snapshot_database = (snapshot / "team-hub.sqlite3").read_bytes()
            live_before_verify = {
                path.name: path.read_bytes()
                for path in (
                    store.database_path,
                    store.signing_key_path,
                    store.bootstrap_proof_path,
                    store.maintenance_fence_path,
                )
            }
            HubStore.verify_maintenance_snapshot(
                data_dir,
                snapshot,
                expected_host_identity=HOST_A,
                expected_hub_id=hub_id,
                expected_operation_id="update-restore",
            )
            with mock.patch("sys.stdout"):
                self.assertEqual(
                    cli_main(
                        [
                            "verify-snapshot",
                            "--data-dir",
                            str(data_dir),
                            "--snapshot",
                            str(snapshot),
                            "--expected-host-identity",
                            HOST_A,
                            "--expected-hub-id",
                            hub_id,
                            "--expected-operation-id",
                            "update-restore",
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (
                        store.database_path,
                        store.signing_key_path,
                        store.bootstrap_proof_path,
                        store.maintenance_fence_path,
                    )
                },
                live_before_verify,
            )

            store.bootstrap(
                proof_bytes.decode("ascii").strip(),
                "owner@example.com",
                "Owner",
                "Original device",
            )
            self.assertFalse(store.bootstrap_proof_path.exists())
            live_before_rejected_restore = (data_dir / "team-hub.sqlite3").read_bytes()
            marker_before_rejected_restore = store.maintenance_fence_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                HubStore.restore_maintenance_snapshot(
                    data_dir,
                    snapshot,
                    expected_host_identity=HOST_A,
                    expected_hub_id=hub_id,
                    expected_operation_id="update-stale",
                )
            self.assertEqual(
                (data_dir / "team-hub.sqlite3").read_bytes(),
                live_before_rejected_restore,
            )
            self.assertEqual(
                store.maintenance_fence_path.read_bytes(),
                marker_before_rejected_restore,
            )
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                HubStore.restore_maintenance_snapshot(
                    data_dir,
                    snapshot,
                    expected_host_identity=HOST_A,
                    expected_hub_id="hub_wrongidentity123456",
                    expected_operation_id="update-restore",
                )
            self.assertEqual(
                (data_dir / "team-hub.sqlite3").read_bytes(),
                live_before_rejected_restore,
            )

            HubStore.restore_maintenance_snapshot(
                data_dir,
                snapshot,
                expected_host_identity=HOST_A,
                expected_hub_id=hub_id,
                expected_operation_id="update-restore",
            )
            self.assertEqual((data_dir / "team-hub.sqlite3").read_bytes(), snapshot_database)
            self.assertEqual(store.signing_key_path.read_bytes(), key_bytes)
            self.assertEqual(store.bootstrap_proof_path.read_bytes(), proof_bytes)
            self.assertFalse((data_dir / "team-hub.sqlite3-wal").exists())
            self.assertFalse((data_dir / "team-hub.sqlite3-shm").exists())
            self.assertFalse(store.maintenance_fence_path.exists())
            restored = HubStore(data_dir, managed_host_identity=HOST_A)
            self.assertEqual(restored.hub_id, hub_id)
            self.assertTrue(restored.health()["bootstrap_required"])

    def test_schema4_snapshot_verifies_restores_exactly_then_migrates_safely(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            store = HubStore(data_dir, managed_host_identity=HOST_A)
            hub_id = store.hub_id
            operation_id = "update-schema4-restore"
            snapshot = self.make_schema4_snapshot(
                store,
                operation_id=operation_id,
            )
            expected_database = (snapshot / "team-hub.sqlite3").read_bytes()
            expected_key = (snapshot / "access-token-signing.key").read_bytes()
            expected_proof = (
                snapshot / "proofs" / "bootstrap-owner.proof"
            ).read_bytes()

            HubStore.verify_maintenance_snapshot(
                data_dir,
                snapshot,
                expected_host_identity=HOST_A,
                expected_hub_id=hub_id,
                expected_operation_id=operation_id,
            )
            store.database_path.write_bytes(b"candidate-mutated database\n")
            store.signing_key_path.write_bytes(b"candidate-mutated signing key\n")
            store.bootstrap_proof_path.write_bytes(b"candidate-mutated proof\n")

            HubStore.restore_maintenance_snapshot(
                data_dir,
                snapshot,
                expected_host_identity=HOST_A,
                expected_hub_id=hub_id,
                expected_operation_id=operation_id,
            )
            self.assertEqual(store.database_path.read_bytes(), expected_database)
            self.assertEqual(store.signing_key_path.read_bytes(), expected_key)
            self.assertEqual(store.bootstrap_proof_path.read_bytes(), expected_proof)
            self.assertFalse(store.maintenance_fence_path.exists())

            legacy = sqlite3.connect(
                f"file:{store.database_path}?mode=ro&immutable=1",
                uri=True,
            )
            try:
                self.assertEqual(legacy.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(
                    legacy.execute(
                        "SELECT hub_id, server_identity FROM managed_host_bindings"
                    ).fetchone(),
                    (hub_id, HOST_A),
                )
                self.assertIsNone(
                    legacy.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'bootstrap_delegations'"
                    ).fetchone()
                )
            finally:
                legacy.close()

            migrated = HubStore(data_dir, managed_host_identity=HOST_A)
            self.assertEqual(migrated.hub_id, hub_id)
            self.assertEqual(migrated.signing_key_path.read_bytes(), expected_key)
            self.assertEqual(migrated.bootstrap_proof_path.read_bytes(), expected_proof)
            connection = migrated.connect()
            try:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 7)
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM bootstrap_delegations"
                    ).fetchone()[0],
                    0,
                )
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' AND name = 'network_mailbox_items'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_schema5_bulletin_slug_collisions_upgrade_without_hijacking(self) -> None:
        collisions = {
            "live-shared": {
                "kind": "board",
                "visibility": "team",
                "archived": False,
            },
            "archived": {"kind": "board", "visibility": "team", "archived": True},
            "private": {"kind": "board", "visibility": "private", "archived": False},
            "wrong-kind": {
                "kind": "announcements",
                "visibility": "team",
                "archived": False,
            },
        }
        for label, collision in collisions.items():
            with self.subTest(collision=label), tempfile.TemporaryDirectory() as temporary:
                data_dir = Path(temporary) / "hub"
                store = HubStore(data_dir, managed_host_identity=HOST_A)
                proof = store.bootstrap_proof_path.read_text().strip()
                bootstrap = store.bootstrap(
                    proof,
                    f"owner-{label}@example.com",
                    "Owner",
                    "Owner Mac",
                )
                owner = store.verify_access(bootstrap["access_token"])
                team_id = bootstrap["teams"][0]["id"]
                preserved_post = store.create_network_bulletin_post(
                    owner,
                    team_id,
                    {
                        "body": f"preserve pre-V6 {label}",
                        "body_format": "plain",
                        "reply_to_post_id": None,
                        "idempotency_key": f"preserve-pre-v6-{label}",
                    },
                )["post"]
                connection = store.connect()
                try:
                    old_board = connection.execute(
                        """
                        SELECT c.* FROM network_boards AS b
                        JOIN channels AS c
                          ON c.team_id=b.team_id AND c.id=b.channel_id
                        WHERE b.team_id=?
                        """,
                        (team_id,),
                    ).fetchone()
                    assert old_board is not None
                    archived_at = (
                        int(old_board["created_at"])
                        if collision["archived"]
                        else None
                    )
                    connection.execute(
                        """
                        UPDATE channels
                        SET kind=?,visibility=?,archived_at=?,updated_at=updated_at+1
                        WHERE id=?
                        """,
                        (
                            collision["kind"],
                            collision["visibility"],
                            archived_at,
                            old_board["id"],
                        ),
                    )
                    old_board_id = str(old_board["id"])
                finally:
                    connection.close()

                self.downgrade_database_to_schema5(store.database_path)
                migrated = HubStore(data_dir, managed_host_identity=HOST_A)
                connection = migrated.connect()
                try:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0],
                        7,
                    )
                    preserved = connection.execute(
                        "SELECT * FROM channels WHERE id=?", (old_board_id,)
                    ).fetchone()
                    self.assertIsNotNone(preserved)
                    assert preserved is not None
                    self.assertEqual(preserved["slug"], "agentsdock-bulletin")
                    self.assertEqual(preserved["kind"], collision["kind"])
                    self.assertEqual(preserved["visibility"], collision["visibility"])
                    self.assertEqual(
                        preserved["archived_at"] is not None,
                        collision["archived"],
                    )
                    replacement = connection.execute(
                        """
                        SELECT c.* FROM network_boards AS b
                        JOIN channels AS c
                          ON c.team_id=b.team_id AND c.id=b.channel_id
                        WHERE b.team_id=?
                        """,
                        (team_id,),
                    ).fetchone()
                    self.assertIsNotNone(replacement)
                    assert replacement is not None
                    self.assertNotEqual(replacement["id"], old_board_id)
                    self.assertEqual(replacement["slug"], "agentsdock-bulletin-v1")
                    self.assertEqual(replacement["kind"], "board")
                    self.assertEqual(replacement["visibility"], "team")
                    self.assertIsNone(replacement["archived_at"])
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM messages WHERE id=? AND channel_id=?",
                            (preserved_post["id"], old_board_id),
                        ).fetchone()
                    )
                finally:
                    connection.close()
                bulletin = migrated.list_network_bulletin(
                    owner,
                    team_id,
                    after_sequence=0,
                    limit=100,
                )
                self.assertEqual(bulletin["posts"], [])

    def test_unavailable_bulletin_binding_is_repaired_without_hijacking(self) -> None:
        for mode in ("archived", "missing"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                data_dir = Path(temporary) / "hub"
                store = HubStore(data_dir, managed_host_identity=HOST_A)
                proof = store.bootstrap_proof_path.read_text().strip()
                bootstrap = store.bootstrap(
                    proof,
                    f"owner-{mode}@example.com",
                    "Owner",
                    "Owner Mac",
                )
                owner = store.verify_access(bootstrap["access_token"])
                team_id = bootstrap["teams"][0]["id"]
                occupied_fallback = store.create_channel(
                    owner,
                    team_id,
                    {
                        "kind": "board",
                        "visibility": "team",
                        "slug": "agentsdock-bulletin-v1",
                        "display_name": "User fallback board",
                        "participant_principal_ids": [],
                        "idempotency_key": f"fallback-board-{mode}",
                    },
                )["channel"]
                connection = store.connect()
                try:
                    old_board = connection.execute(
                        """
                        SELECT c.* FROM network_boards AS b
                        JOIN channels AS c
                          ON c.team_id=b.team_id AND c.id=b.channel_id
                        WHERE b.team_id=?
                        """,
                        (team_id,),
                    ).fetchone()
                    assert old_board is not None
                    old_board_id = str(old_board["id"])
                    if mode == "archived":
                        connection.execute(
                            "UPDATE channels SET archived_at=?,updated_at=updated_at+1 "
                            "WHERE id=?",
                            (int(old_board["created_at"]), old_board_id),
                        )
                finally:
                    connection.close()
                if mode == "missing":
                    connection = sqlite3.connect(
                        store.database_path, isolation_level=None
                    )
                    try:
                        connection.execute("PRAGMA foreign_keys=OFF")
                        connection.execute("BEGIN IMMEDIATE")
                        connection.execute(
                            "DELETE FROM channel_acl_entries WHERE channel_id=?",
                            (old_board_id,),
                        )
                        connection.execute(
                            "DELETE FROM messages WHERE channel_id=?",
                            (old_board_id,),
                        )
                        connection.execute(
                            "DELETE FROM channels WHERE id=?", (old_board_id,)
                        )
                        connection.execute("COMMIT")
                    finally:
                        connection.close()

                repaired = HubStore(data_dir, managed_host_identity=HOST_A)
                connection = repaired.connect()
                try:
                    replacement = connection.execute(
                        """
                        SELECT c.* FROM network_boards AS b
                        JOIN channels AS c
                          ON c.team_id=b.team_id AND c.id=b.channel_id
                        WHERE b.team_id=?
                        """,
                        (team_id,),
                    ).fetchone()
                    self.assertIsNotNone(replacement)
                    assert replacement is not None
                    self.assertNotEqual(replacement["id"], old_board_id)
                    self.assertEqual(
                        replacement["slug"],
                        (
                            "agentsdock-bulletin-v1-2"
                            if mode == "archived"
                            else "agentsdock-bulletin"
                        ),
                    )
                    self.assertEqual(replacement["kind"], "board")
                    self.assertEqual(replacement["visibility"], "team")
                    self.assertIsNone(replacement["archived_at"])
                    self.assertIsNotNone(
                        connection.execute(
                            "SELECT 1 FROM channels WHERE id=? AND slug=?",
                            (
                                occupied_fallback["id"],
                                "agentsdock-bulletin-v1",
                            ),
                        ).fetchone()
                    )
                    if mode == "archived":
                        archived = connection.execute(
                            "SELECT archived_at FROM channels WHERE id=?",
                            (old_board_id,),
                        ).fetchone()
                        self.assertIsNotNone(archived)
                        assert archived is not None
                        self.assertIsNotNone(archived["archived_at"])
                    else:
                        self.assertIsNone(
                            connection.execute(
                                "SELECT 1 FROM channels WHERE id=?",
                                (old_board_id,),
                            ).fetchone()
                        )
                    self.assertEqual(
                        connection.execute("PRAGMA foreign_key_check").fetchall(),
                        [],
                    )
                finally:
                    connection.close()

    def test_schema5_snapshot_does_not_fall_back_when_delegation_schema_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            store = HubStore(data_dir, managed_host_identity=HOST_A)
            operation_id = "update-schema5-strict"
            snapshot = store.maintenance_snapshot_and_fence(
                "server-update",
                operation_id=operation_id,
            )
            snapshot_database = snapshot / "team-hub.sqlite3"
            self.downgrade_database_to_schema4(
                snapshot_database,
                update_version_ledger=False,
            )
            manifest_path = snapshot / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["database_sha256"] = hashlib.sha256(
                snapshot_database.read_bytes()
            ).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "snapshot bootstrap proof schema is invalid",
            ):
                HubStore.verify_maintenance_snapshot(
                    data_dir,
                    snapshot,
                    expected_host_identity=HOST_A,
                    expected_hub_id=store.hub_id,
                    expected_operation_id=operation_id,
                )

    def test_preflight_sees_a_binding_present_only_in_live_wal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            standalone = HubStore(data_dir)
            keeper = standalone.connect()
            try:
                keeper.execute("PRAGMA wal_autocheckpoint = 0")
                keeper.execute(
                    """
                    INSERT INTO managed_host_bindings(
                        singleton, hub_id, server_identity, created_at
                    ) VALUES (1, ?, ?, 1)
                    """,
                    (standalone.hub_id, HOST_A),
                )
                wal = data_dir / "team-hub.sqlite3-wal"
                self.assertTrue(wal.is_file())
                immutable = sqlite3.connect(
                    f"file:{standalone.database_path}?mode=ro&immutable=1",
                    uri=True,
                )
                try:
                    self.assertIsNone(
                        immutable.execute(
                            "SELECT server_identity FROM managed_host_bindings"
                        ).fetchone()
                    )
                finally:
                    immutable.close()
                with self.assertRaisesRegex(RuntimeError, "different AgentsServer"):
                    HubStore(data_dir, managed_host_identity=HOST_B)
            finally:
                keeper.close()

    def test_snapshot_verification_fails_before_live_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            store = HubStore(data_dir, managed_host_identity=HOST_A)
            snapshot = store.maintenance_snapshot_and_fence(
                "server-update",
                operation_id="update-verify",
            )
            live_before = {
                path.name: path.read_bytes()
                for path in (
                    store.database_path,
                    store.signing_key_path,
                    store.bootstrap_proof_path,
                    store.maintenance_fence_path,
                )
            }
            snapshot_database = snapshot / "team-hub.sqlite3"
            snapshot_database.write_bytes(snapshot_database.read_bytes() + b"tampered")
            with self.assertRaisesRegex(RuntimeError, "digest is invalid"):
                HubStore.verify_maintenance_snapshot(
                    data_dir,
                    snapshot,
                    expected_host_identity=HOST_A,
                    expected_hub_id=store.hub_id,
                    expected_operation_id="update-verify",
                )
            self.assertEqual(
                {
                    path.name: path.read_bytes()
                    for path in (
                        store.database_path,
                        store.signing_key_path,
                        store.bootstrap_proof_path,
                        store.maintenance_fence_path,
                    )
                },
                live_before,
            )

    def test_update_fence_blocks_local_recovery_until_exact_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            store = HubStore(data_dir, managed_host_identity=HOST_A)
            proof = store.bootstrap_proof_path.read_text().strip()
            store.bootstrap(
                proof,
                "owner@example.com",
                "Owner",
                "Owner Mac",
            )
            snapshot = store.maintenance_snapshot_and_fence(
                "server-update",
                operation_id="update-one",
            )
            with mock.patch("sys.stderr"):
                denied = cli_main(
                    [
                        "device-recovery",
                        "--data-dir",
                        str(data_dir),
                        "--email",
                        "owner@example.com",
                        "--device-label",
                        "Replacement Mac",
                    ]
                )
            self.assertEqual(denied, 2)
            connection = store.connect()
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT count(*) FROM owner_recovery_claims"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()
            self.assertTrue(
                store.clear_maintenance_fence(
                    expected_reason="server-update",
                    expected_operation_id="update-one",
                    expected_snapshot=snapshot,
                )
            )
            with mock.patch("sys.stdout"):
                allowed = cli_main(
                    [
                        "device-recovery",
                        "--data-dir",
                        str(data_dir),
                        "--email",
                        "owner@example.com",
                        "--device-label",
                        "Replacement Mac",
                    ]
                )
            self.assertEqual(allowed, 0)

    def test_maintenance_fence_clear_is_bound_to_operation_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            store = HubStore(data_dir, managed_host_identity=HOST_A)
            snapshot = store.maintenance_snapshot_and_fence(
                "server-update",
                operation_id="update-new",
            )
            marker_before = store.maintenance_fence()
            with self.assertRaisesRegex(RuntimeError, "operation does not match"):
                store.clear_maintenance_fence(
                    expected_reason="server-update",
                    expected_operation_id="update-old",
                    expected_snapshot=snapshot,
                )
            self.assertEqual(store.maintenance_fence(), marker_before)
            with self.assertRaisesRegex(RuntimeError, "snapshot does not match"):
                store.clear_maintenance_fence(
                    expected_reason="server-update",
                    expected_operation_id="update-new",
                    expected_snapshot=snapshot.with_name("snapshot_wrong"),
                )
            self.assertEqual(store.maintenance_fence(), marker_before)
            self.assertTrue(
                store.clear_maintenance_fence(
                    expected_reason="server-update",
                    expected_operation_id="update-new",
                    expected_snapshot=snapshot,
                )
            )

    def test_standalone_listener_and_embedded_host_share_one_runtime_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "hub"
            lease = HubStore.acquire_managed_runtime_lease(data_dir)
            try:
                with mock.patch("sys.stderr"):
                    self.assertEqual(
                        cli_main(["serve", "--data-dir", str(data_dir)]),
                        2,
                    )
            finally:
                HubStore.release_managed_runtime_lease(lease)

            embedded_result: list[str] = []

            def try_embedded(*_args, **_kwargs) -> None:
                try:
                    lease_fd = HubStore.acquire_managed_runtime_lease(data_dir)
                except RuntimeError:
                    embedded_result.append("rejected")
                else:
                    embedded_result.append("accepted")
                    HubStore.release_managed_runtime_lease(lease_fd)

            with mock.patch("agentsdock_team_hub.cli.uvicorn.run", side_effect=try_embedded), \
                 mock.patch("sys.stdout"):
                self.assertEqual(
                    cli_main(["serve", "--data-dir", str(data_dir)]),
                    0,
                )
            self.assertEqual(embedded_result, ["rejected"])


if __name__ == "__main__":
    unittest.main()
