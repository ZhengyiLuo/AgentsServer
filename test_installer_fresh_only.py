"""Exercise the real fresh-install admission check without service mutations."""

from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


class FreshInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        source = (Path(__file__).with_name("install.sh")).read_text()
        start = source.index("fresh_install_scaffold_is_empty() {")
        self.function = source[start:source.index("\nvalidate_fresh_install_state ||", start)]
        migration_start = source.index("migrate_legacy_state() {")
        self.migration_function = source[migration_start:source.index("\n}\n", migration_start) + 3]
        self.values = {
            "FRESH_INSTALL_ONLY": "true", "OS_NAME": "Linux",
            "SERVICE_NAME": "agents-server", "LEGACY_SERVICE_NAME": "zenithbot-agent", "LABEL": "com.agentsdock.server",
            "INSTALL_ROOT": str(self.root / "install"),
            "RELEASES_ROOT": str(self.root / "install/releases"),
            "STAGE_DIR": str(self.root / "install/releases/.stage-owned"),
            "STAGE_DIR_DEVICE": "", "STAGE_DIR_INODE": "",
            "CURRENT_LINK": str(self.root / "install/current"),
            "PREVIOUS_LINK": str(self.root / "install/previous"),
            "CONFIG_ROOT": str(self.root / "config"),
            "STATE_ROOT": str(self.root / "state"),
            "DEFAULT_STATE_GUARD": str(self.root / "state"),
            "LEGACY_STATE_ROOT": str(self.root / "legacy-state"),
            "SYSTEMD_SERVICE_FILE": str(self.root / "service"),
            "LEGACY_SERVICE_FILE": str(self.root / "legacy-service"),
            "PLIST": str(self.root / "service.plist"),
        }

    def run_guard(self, *, loaded=False, unavailable=False, legacy_loaded=False, between=""):
        assignments = "\n".join(f"{k}={shlex.quote(v)}" for k, v in self.values.items())
        script = assignments + "\n" + self.function + "\n"
        script += 'systemctl() { printf "%s\\n" ' + ("loaded" if loaded else "not-found") + '; }\n'
        if legacy_loaded:
            script += 'systemctl() { if [[ "$3" = zenithbot-agent.service ]]; then printf "loaded\\n"; else printf "not-found\\n"; fi; }\n'
        script += 'launchctl() { printf "Could not find service\\n" >&2; return ' + ("0" if loaded else "1") + '; }\n'
        if unavailable:
            script += 'systemctl() { return 1; }; launchctl() { return 1; };\n'
        script += "validate_fresh_install_state || exit $?\n"
        if between:
            script += between + "\nvalidate_fresh_install_state\n"
        return subprocess.run(["/bin/bash", "-u", "-c", script], text=True, capture_output=True)

    def test_empty_target_is_allowed(self):
        self.assertEqual(self.run_guard().returncode, 0)

    def test_preactivation_failure_can_retry_known_empty_scaffolding_without_cleanup_or_legacy_alias(self):
        config = Path(self.values["CONFIG_ROOT"])
        state = Path(self.values["STATE_ROOT"])
        releases = Path(self.values["RELEASES_ROOT"])
        create = "mkdir -p " + " ".join(shlex.quote(str(p)) for p in (config, state / "admin", releases))
        failed = self.run_guard(between=create + "\nexit 73")
        self.assertEqual(failed.returncode, 73)
        paths = (config, state, state / "admin", releases)
        identities = {p: p.stat().st_ino for p in paths}
        result = self.run_guard(between=self.migration_function + "\nmigrate_legacy_state")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({p: p.stat().st_ino for p in paths}, identities)
        self.assertFalse(Path(self.values["LEGACY_STATE_ROOT"]).is_symlink())
        self.assertEqual(list(config.iterdir()), [])
        self.assertEqual(list(state.iterdir()), [state / "admin"])
        self.assertEqual(list((state / "admin").iterdir()), [])

    def test_empty_config_state_and_admin_are_safe_only_with_owned_searchable_nonwritable_directories(self):
        config = Path(self.values["CONFIG_ROOT"])
        state = Path(self.values["STATE_ROOT"])
        config.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        self.assertEqual(self.run_guard().returncode, 0)
        admin = state / "admin"
        admin.mkdir(mode=0o700)
        self.assertEqual(self.run_guard().returncode, 0)
        for path in (config, state, admin):
            for mode in (0o770, 0o707, 0o300):
                with self.subTest(path=path.name, mode=oct(mode)):
                    path.chmod(mode)
                    try:
                        self.assertNotEqual(self.run_guard().returncode, 0)
                    finally:
                        path.chmod(0o700)

    def test_config_state_and_admin_links_files_hidden_entries_and_unknown_directories_are_rejected(self):
        config = Path(self.values["CONFIG_ROOT"])
        state = Path(self.values["STATE_ROOT"])
        for parent in (config, state):
            parent.symlink_to(self.root / "missing-target")
            self.assertNotEqual(self.run_guard().returncode, 0)
            self.assertTrue(parent.is_symlink())
            parent.unlink()
            parent.mkdir(mode=0o700)
        admin = state / "admin"
        for target in (config / "env", config / ".lock", state / "server-identity", state / ".lock", admin):
            with self.subTest(target=target):
                target.write_bytes(b"existing-data")
                self.assertNotEqual(self.run_guard().returncode, 0)
                self.assertEqual(target.read_bytes(), b"existing-data")
                target.unlink()
        for target in (config / "admin", state / "chats", state / ".lock"):
            target.mkdir()
            self.assertNotEqual(self.run_guard().returncode, 0)
            self.assertTrue(target.is_dir())
            target.rmdir()
        admin.symlink_to(config)
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertTrue(admin.is_symlink())
        admin.unlink()
        admin.mkdir()
        for target in (admin / "pending-update", admin / ".lock"):
            target.write_bytes(b"existing-data")
            self.assertNotEqual(self.run_guard().returncode, 0)
            self.assertEqual(target.read_bytes(), b"existing-data")
            target.unlink()

    def test_existing_state_configuration_and_service_are_preserved(self):
        for field in ("STATE_ROOT", "CONFIG_ROOT", "LEGACY_STATE_ROOT", "SYSTEMD_SERVICE_FILE", "PLIST"):
            with self.subTest(field=field):
                path = Path(self.values[field]); path.write_bytes(b"existing-data")
                result = self.run_guard()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(path.read_bytes(), b"existing-data")
                path.unlink()

    def test_dangling_current_link_is_not_a_fresh_install(self):
        target = Path(self.values["CURRENT_LINK"])
        target.parent.mkdir(); target.symlink_to(self.root / "missing-release")
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.assertTrue(target.is_symlink())

    def test_existing_release_is_rejected(self):
        release = Path(self.values["RELEASES_ROOT"]) / "older"
        release.mkdir(parents=True)
        self.assertNotEqual(self.run_guard().returncode, 0)

    def test_only_owned_stage_and_lock_are_allowed(self):
        stage = Path(self.values["STAGE_DIR"]); stage.mkdir(parents=True)
        (Path(self.values["INSTALL_ROOT"]) / ".install-lock").mkdir()
        self.assertNotEqual(self.run_guard().returncode, 0)
        self.values["STAGE_DIR_DEVICE"] = str(stage.stat().st_dev)
        self.values["STAGE_DIR_INODE"] = str(stage.stat().st_ino)
        self.assertEqual(self.run_guard().returncode, 0)
        self.values["STAGE_DIR_INODE"] = str(stage.stat().st_ino + 1)
        self.assertNotEqual(self.run_guard().returncode, 0)

    def test_unknown_service_state_is_not_treated_as_absence(self):
        for platform in ("Linux", "Darwin"):
            with self.subTest(platform=platform):
                self.values["OS_NAME"] = platform
                self.assertEqual(self.run_guard().returncode, 0)
                self.assertNotEqual(self.run_guard(unavailable=True).returncode, 0)

    def test_service_registration_without_file_is_rejected(self):
        for platform in ("Linux", "Darwin"):
            with self.subTest(platform=platform):
                self.values["OS_NAME"] = platform
                self.assertNotEqual(self.run_guard(loaded=True).returncode, 0)

    def test_loaded_legacy_service_without_a_file_is_rejected_before_and_after_admission(self):
        self.assertNotEqual(self.run_guard(legacy_loaded=True).returncode, 0)
        result = self.run_guard(between='systemctl() { if [[ "$3" = zenithbot-agent.service ]]; then printf "loaded\\n"; else printf "not-found\\n"; fi; }')
        self.assertNotEqual(result.returncode, 0)

    def test_state_appearing_between_checks_is_rejected(self):
        state = Path(self.values["STATE_ROOT"])
        result = self.run_guard(between=f"mkdir {shlex.quote(str(state))}\nprintf existing-server > {shlex.quote(str(state / 'server-identity'))}")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((state / "server-identity").read_text(), "existing-server")

    def test_credentials_appearing_after_scaffold_admission_are_rejected_on_the_repeated_check(self):
        config = Path(self.values["CONFIG_ROOT"])
        config.mkdir(mode=0o700)
        state = Path(self.values["STATE_ROOT"])
        (state / "admin").mkdir(parents=True, mode=0o700)
        for file in (config / "env", state / "admin" / "pending-update"):
            with self.subTest(file=file):
                result = self.run_guard(between=f"printf existing-secret > {shlex.quote(str(file))}")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(file.read_text(), "existing-secret")
                file.unlink()

    def test_ordinary_managed_update_is_not_blocked_by_fresh_guard(self):
        Path(self.values["STATE_ROOT"]).mkdir()
        self.values["FRESH_INSTALL_ONLY"] = "false"
        self.assertEqual(self.run_guard(loaded=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
