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
