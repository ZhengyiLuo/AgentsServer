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
        start = source.index("validate_fresh_install_state() {")
        self.function = source[start:source.index("\nvalidate_fresh_install_state ||", start)]
        self.values = {
            "FRESH_INSTALL_ONLY": "true", "OS_NAME": "Linux",
            "SERVICE_NAME": "agents-server", "LABEL": "com.agentsdock.server",
            "INSTALL_ROOT": str(self.root / "install"),
            "RELEASES_ROOT": str(self.root / "install/releases"),
            "STAGE_DIR": str(self.root / "install/releases/.stage-owned"),
            "STAGE_DIR_DEVICE": "", "STAGE_DIR_INODE": "",
            "CURRENT_LINK": str(self.root / "install/current"),
            "PREVIOUS_LINK": str(self.root / "install/previous"),
            "CONFIG_ROOT": str(self.root / "config"),
            "STATE_ROOT": str(self.root / "state"),
            "LEGACY_STATE_ROOT": str(self.root / "legacy-state"),
            "SYSTEMD_SERVICE_FILE": str(self.root / "service"),
            "LEGACY_SERVICE_FILE": str(self.root / "legacy-service"),
            "PLIST": str(self.root / "service.plist"),
        }

    def run_guard(self, *, loaded=False, unavailable=False, between=""):
        assignments = "\n".join(f"{k}={shlex.quote(v)}" for k, v in self.values.items())
        script = assignments + "\n" + self.function + "\n"
        script += 'systemctl() { printf "%s\\n" ' + ("loaded" if loaded else "not-found") + '; }\n'
        script += 'launchctl() { printf "Could not find service\\n" >&2; return ' + ("0" if loaded else "1") + '; }\n'
        if unavailable:
            script += 'systemctl() { return 1; }; launchctl() { return 1; };\n'
        script += "validate_fresh_install_state || exit $?\n"
        if between:
            script += between + "\nvalidate_fresh_install_state\n"
        return subprocess.run(["/bin/bash", "-u", "-c", script], text=True, capture_output=True)

    def test_empty_target_is_allowed(self):
        self.assertEqual(self.run_guard().returncode, 0)

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

    def test_state_appearing_between_checks_is_rejected(self):
        state = Path(self.values["STATE_ROOT"])
        result = self.run_guard(between=f"mkdir {shlex.quote(str(state))}")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(state.is_dir())

    def test_ordinary_managed_update_is_not_blocked_by_fresh_guard(self):
        Path(self.values["STATE_ROOT"]).mkdir()
        self.values["FRESH_INSTALL_ONLY"] = "false"
        self.assertEqual(self.run_guard(loaded=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
