"""An admitted update must finish a previous rollback without losing its fence."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import test_installer as fixtures
from agentsdock_team_hub.store import HubStore


class InstallerUpdateSuccessionTests(unittest.TestCase):
    def make_pending_rollback(self, root: Path, origin: str):
        helper = fixtures.InstallerContractTests()
        home, fake_bin, install_root, environment = helper.fake_linux_preinstall_environment(root)
        helper.write_exact_health_uv(fake_bin)
        helper.write_json_health_curl(fake_bin)
        helper.write_event_systemctl(fake_bin)
        helper.write_running_systemd_service(home)
        if origin == "prepared":
            uv = fake_bin / "uv"
            uv.write_text(uv.read_text().replace(
                '  *"-m activation_transaction "*|*"/activation_transaction.py "*)',
                '  *"-m activation_transaction "*|*"/activation_transaction.py "*)\n'
                '    case "$*" in *"--phase linking "*)\n'
                '      [ "${FAKE_FAIL_BEFORE_LINK:-false}" != "true" ] || exit 42 ;;\n'
                '    esac',
            ))
        identity = "server_successive_update_12345678"
        hub_data = root / "state" / "team-hub"
        store, snapshot = helper.prepare_real_managed_hub_fixture(
            install_root, hub_data, operation_id="update_previous_12345678",
            server_identity=identity, schema_version=5,
        )
        (install_root / "current" / "VERSION").write_text("1.0.3\n")
        (root / "config").mkdir()
        helper.write_private_file(root / "config" / "env",
            "AGENTSDOCK_AGENT_TOKEN=fixture_only_token_abcdefghijklmnopqrstuvwxyz\n"
            "AGENTSDOCK_AGENT_PORT=7850\nAGENTSDOCK_TEAM_HUB_MODE=host\n")
        environment.update(
            FAKE_EVENT_LOG=str(root / "events.log"),
            FAKE_REAL_CANDIDATE_TEAM_HUB_CONTROL="true",
            FAKE_FAIL_BEFORE_LINK="true" if origin == "prepared" else "false",
            FAKE_HEALTH_VERSION="9.9.9-wrong", FAKE_HEALTH_VERSION_AFTER_RESTORE="1.0.3",
            FAKE_TEAM_HUB_ID_AFTER_RESTORE="hub_failed_health_12345678",
            FAKE_SERVER_IDENTITY=identity, FAKE_TEAM_HUB_ID=store.hub_id,
            FAKE_TEAM_HUB_MODE="host", FAKE_TEAM_HUB_MODE_BEFORE_RESTART="host",
            FAKE_TEAM_HUB_TRANSPORT="loopback", FAKE_TEAM_HUB_TRANSPORT_AFTER_RESTORE="loopback",
            FAKE_TEAM_HUB_URL_AFTER_RESTORE="", REAL_PYTHON=sys.executable,
            AGENTS_SERVER_HEALTH_CHECK_ATTEMPTS="1",
        )
        first = subprocess.run(helper.managed_installer_command(
            identity, store.hub_id, snapshot, hub_data, "update_previous_12345678"),
            env=environment, capture_output=True, text=True, check=False)
        self.assertNotEqual(first.returncode, 0, first.stdout)
        journal = install_root / ".activation-transaction" / "manifest.json"
        value = json.loads(journal.read_text())
        self.assertEqual(value["phase"], "rolled-back", first.stderr)
        self.assertEqual(value["rollback_from"], origin, first.stderr)
        self.assertFalse(store.maintenance_fence_path.exists(), first.stderr)
        self.assertEqual((install_root / "current" / "VERSION").read_text().strip(), "1.0.3")
        environment["FAKE_FAIL_BEFORE_LINK"] = "false"
        environment["FAKE_TEAM_HUB_ID_AFTER_RESTORE"] = store.hub_id
        # Follow actual installed links. Only native services/HTTP are fake;
        # activation journals, SQLite snapshots and Hub controls run normally.
        curl = fake_bin / "curl"
        curl.write_text(curl.read_text().replace(
            'health_version="$FAKE_HEALTH_VERSION"',
            'health_version="$(cat "$AGENTS_SERVER_INSTALL_DIR/current/VERSION")"',
        ).replace('health_version="${FAKE_HEALTH_VERSION_AFTER_RESTORE:-$health_version}"', ':'))
        new_store = HubStore(hub_data, managed_host_identity=identity)
        new_snapshot = new_store.maintenance_snapshot_and_fence(
            "server-update", operation_id="update_incoming_12345678")
        command = helper.managed_installer_command(
            identity, store.hub_id, new_snapshot, hub_data, "update_incoming_12345678")
        return install_root, environment, store, journal, command

    def test_new_update_continues_after_previous_terminal_rollback(self):
        for origin in ("prepared", "candidate-starting"):
            with self.subTest(origin=origin), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                install_root, environment, store, journal, command = self.make_pending_rollback(root, origin)
                signing_key = store.signing_key_path.read_bytes()
                bootstrap_proof = store.bootstrap_proof_path.read_bytes()
                result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Continuing the requested AgentsServer", result.stdout)
                self.assertFalse(journal.exists())
                self.assertFalse(store.maintenance_fence_path.exists())
                self.assertFalse((install_root / ".install-lock").exists())
                self.assertEqual((install_root / "current" / "VERSION").read_text().strip(),
                                 fixtures.InstallerContractTests.release_version())
                self.assertEqual(store.signing_key_path.read_bytes(), signing_key)
                self.assertEqual(store.bootstrap_proof_path.read_bytes(), bootstrap_proof)
                self.assertEqual(HubStore(store.data_dir, managed_host_identity=store.managed_host_identity).hub_id,
                                 store.hub_id)

    def test_failed_previous_recovery_releases_only_unstarted_incoming_fence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            install_root, environment, store, journal, command = self.make_pending_rollback(root, "prepared")
            environment["FAKE_TEAM_HUB_ID"] = "hub_failed_recovery_12345678"
            database = store.database_path.read_bytes()
            result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(journal.exists())
            self.assertFalse(store.maintenance_fence_path.exists(), result.stderr)
            self.assertEqual(store.database_path.read_bytes(), database)
            self.assertEqual((install_root / "current" / "VERSION").read_text().strip(), "1.0.3")

    def test_nonterminal_previous_activation_does_not_orphan_incoming_fence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            install_root, environment, store, journal, command = self.make_pending_rollback(root, "prepared")
            # Crash after old files were restored but before rolled-back was
            # recorded: it still belongs to the existing recovery operation.
            value = json.loads(journal.read_text())
            value["phase"] = "rolling-back"
            journal.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
            before = (root / "events.log").read_text().splitlines()
            database = store.database_path.read_bytes()
            result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 75, result.stderr)
            self.assertIn("previous activation is still recovering", result.stderr)
            self.assertEqual(json.loads(journal.read_text()), value)
            self.assertFalse(store.maintenance_fence_path.exists(), result.stderr)
            self.assertEqual(store.database_path.read_bytes(), database)
            self.assertEqual((install_root / "current" / "VERSION").read_text().strip(), "1.0.3")
            after = (root / "events.log").read_text().splitlines()[len(before):]
            self.assertEqual([line for line in after if line.startswith("systemctl:")],
                             ["systemctl:--user show-environment"])

    def test_invalid_previous_journal_releases_unstarted_incoming_fence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            install_root, environment, store, journal, command = self.make_pending_rollback(root, "prepared")
            journal.write_text("{invalid old journal\n")
            before = (root / "events.log").read_text().splitlines()
            database = store.database_path.read_bytes()
            result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not be verified for recovery", result.stderr)
            self.assertEqual(journal.read_text(), "{invalid old journal\n")
            self.assertFalse(store.maintenance_fence_path.exists(), result.stderr)
            self.assertEqual(store.database_path.read_bytes(), database)
            self.assertEqual((install_root / "current" / "VERSION").read_text().strip(), "1.0.3")
            after = (root / "events.log").read_text().splitlines()[len(before):]
            self.assertEqual([line for line in after if line.startswith("systemctl:")],
                             ["systemctl:--user show-environment"])


if __name__ == "__main__":
    unittest.main()
