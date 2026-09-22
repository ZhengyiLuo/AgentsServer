"""Real legacy entry points must not mutate an opt-in execution layout."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent


def shell_function(source, name):
    ending = r"^EXECUTION_LAYOUT_CHECK\n\}" if name == "refuse_remote_execution_layout" else r"^\}"
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?{ending}", source)
    if match is None:
        raise AssertionError(f"Missing function {name}")
    return match.group()


class LegacyExecutionLayoutGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="execution-legacy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.install = self.home / "managed server"
        self.install.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.operations = self.root / "unexpected-operations"
        self.environment = {
            **os.environ, "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "AGENTS_SERVER_INSTALL_DIR": str(self.install),
            "AGENTS_SERVER_CONFIG_DIR": str(self.home / "config"),
            "AGENTSDOCK_STATE_DIR": str(self.home / "state"),
            "AGENTSDOCK_REMOTE_HOST": "fixture.invalid",
            "AGENTSDOCK_REMOTE_APP_DIR": str(self.install / "current"),
            "AGENTSDOCK_AGENT_TOKEN": "fixture_" + "x" * 40,
            "OPERATIONS_LOG": str(self.operations),
        }
        for name in ("launchctl", "systemctl", "uv", "curl", "scp", "rsync", "security"):
            self.stub(name, '#!/bin/bash\nprintf "%s\\n" "$0 $*" >> "$OPERATIONS_LOG"\nexit 79\n')
        # Execute only the new read-only SSH guard locally. Any subsequent
        # health/deployment operation is an unexpected boundary crossing.
        self.stub("ssh", '#!/bin/bash\n'
                  'if [[ "$#" == 2 && "$2" == "bash -s -- "* ]]; then\n'
                  '  exec /bin/bash -c "$2"\nfi\n'
                  'printf "%s\\n" "$*" >> "$OPERATIONS_LOG"\nexit 79\n')

    def stub(self, name, source):
        path = self.bin / name
        path.write_text(source)
        path.chmod(0o755)

    def marker(self, name, kind):
        path = self.install / name
        if kind == "directory":
            path.mkdir()
        elif kind == "symlink":
            path.symlink_to(self.install / "missing-target")
        else:
            path.write_text("preserve this marker exactly\n")
        return path

    def run_script(self, name, *arguments):
        return subprocess.run(["/bin/bash", str(ROOT / name), *arguments],
                              env=self.environment, capture_output=True, text=True, timeout=10)

    def test_install_and_uninstall_refuse_each_marker_before_side_effects(self):
        for script in ("install.sh", "uninstall.sh"):
            for name in ("execution-layout.json", ".execution-transaction"):
                for kind in ("file", "directory", "symlink"):
                    with self.subTest(script=script, marker=name, kind=kind):
                        marker = self.marker(name, kind)
                        before = marker.lstat()
                        contents = (os.readlink(marker) if kind == "symlink" else
                                    marker.read_bytes() if kind == "file" else None)
                        try:
                            result = self.run_script(script, *(["--yes"] if script == "uninstall.sh" else []))
                            self.assertNotEqual(result.returncode, 0, result.stdout)
                            if script == "install.sh":
                                # A valid installed layout is now supported;
                                # these malformed files/links remain refusals.
                                self.assertRegex(result.stderr, "execution (configuration|activation|layout)|execution-layout.json|separate gateway/execution layout")
                            else:
                                self.assertIn("separate gateway/execution layout", result.stderr)
                            after = marker.lstat()
                            # Read-only inspection may update access time. Keep
                            # checking identity, metadata and contents exactly.
                            for field in ("st_dev", "st_ino", "st_mode", "st_nlink",
                                          "st_uid", "st_gid", "st_size",
                                          "st_mtime_ns", "st_ctime_ns"):
                                self.assertEqual(getattr(after, field), getattr(before, field), field)
                            if kind == "symlink":
                                self.assertEqual(os.readlink(marker), contents)
                            elif kind == "file":
                                self.assertEqual(marker.read_bytes(), contents)
                            self.assertEqual(set(self.install.iterdir()), {marker})
                            self.assertFalse((self.home / "config").exists())
                            self.assertFalse((self.home / "state").exists())
                            self.assertFalse(self.operations.exists())
                        finally:
                            marker.rmdir() if kind == "directory" else marker.unlink()

    def test_deploy_refuses_managed_current_and_direct_release_paths(self):
        release = self.install / "releases" / "1.2.3-beta.4"
        release.mkdir(parents=True)
        current = self.install / "current"
        current.symlink_to(release)
        external = self.home / "runtime-alias"
        external.symlink_to(release)
        for name in ("execution-layout.json", ".execution-transaction"):
            for kind in ("file", "directory", "symlink"):
                marker = self.marker(name, kind)
                for target in (current, release, external):
                    with self.subTest(marker=name, kind=kind, target=target.name):
                        self.environment["AGENTSDOCK_REMOTE_APP_DIR"] = str(target)
                        result = self.run_script("deploy.sh")
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn("does not support the separate gateway/execution layout", result.stderr)
                        self.assertFalse(self.operations.exists())
                        self.assertTrue(current.is_symlink())
                        self.assertEqual(list(release.iterdir()), [])
                marker.rmdir() if kind == "directory" else marker.unlink()

    def test_ordinary_layout_passes_the_local_and_remote_guards(self):
        for script, function in (("install.sh", "refuse_execution_layout"),
                                 ("uninstall.sh", "refuse_execution_layout"),
                                 ("deploy.sh", "refuse_remote_execution_layout")):
            with self.subTest(script=script):
                source = shell_function((ROOT / script).read_text(), function)
                result = subprocess.run(["/bin/bash", "-c", source + "\n" + function],
                    env={**self.environment, "INSTALL_ROOT": str(self.install),
                         "REMOTE_HOST": "fixture.invalid", "REMOTE_SERVER_DIR": str(self.install / "current")},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.operations.exists())
                self.assertEqual(list(self.install.iterdir()), [])

    def test_install_lock_recheck_refuses_new_execution_marker_before_other_checks(self):
        source = (ROOT / "install.sh").read_text()
        for marker in ("execution-layout.json", ".execution-transaction"):
            path = self.marker(marker, "file")
            script = (shell_function(source, "refuse_execution_layout") + "\n"
                      + shell_function(source, "validate_exclusive_install_state")
                      + '\nvalidate_fresh_install_state() { echo unexpected >> "$OPERATIONS_LOG"; }\n'
                      + 'validate_exclusive_install_state\n')
            result = subprocess.run(["/bin/bash", "-c", script],
                env={**self.environment, "INSTALL_ROOT": str(self.install)},
                capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, "execution (configuration|activation|layout)|separate gateway/execution layout")
            self.assertFalse(self.operations.exists())
            path.unlink()

    def test_uninstall_lock_recheck_preserves_marker_created_during_lock_acquisition(self):
        self.stub("mkdir", '#!/bin/bash\n/bin/mkdir "$@" || exit $?\n'
                  'if [[ "${!#}" == "$AGENTS_SERVER_INSTALL_DIR/.install-lock" ]]; then\n'
                  '  printf preserve > "$AGENTS_SERVER_INSTALL_DIR/execution-layout.json"\nfi\n')
        result = self.run_script("uninstall.sh", "--yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("separate gateway/execution layout", result.stderr)
        self.assertEqual((self.install / "execution-layout.json").read_text(), "preserve")
        self.assertFalse((self.install / ".install-lock").exists())
        self.assertFalse(self.operations.exists())


if __name__ == "__main__":
    unittest.main()
