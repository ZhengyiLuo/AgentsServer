import os
import subprocess
import tempfile
import unittest
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
INSTALLER = ROOT / "install.sh"


class InstallerContractTests(unittest.TestCase):
    def test_shell_syntax_is_valid(self):
        result = subprocess.run(
            ["bash", "-n", str(INSTALLER)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_installer_preserves_state_and_emits_private_result(self):
        source = INSTALLER.read_text()
        self.assertIn('STATE_ROOT="${ZENITHBOT_AGENT_DIR:-$HOME/.zenithbot-agent}"', source)
        self.assertIn("AGENTSDOCK_SETUP_RESULT=", source)
        self.assertIn("ZENITHDOCK_AGENT_TOKEN", source)
        self.assertIn("AGENTS_SERVER_ADMIN_TOKEN", source)
        self.assertIn('RELEASES_ROOT="$INSTALL_ROOT/releases"', source)
        self.assertIn('PREVIOUS_LINK="$INSTALL_ROOT/previous"', source)
        self.assertIn('REPLACED_DIR="$RELEASES_ROOT/$RELEASE_VERSION-replaced-', source)
        self.assertLess(source.index('OLD_TARGET=""'), source.index('mv "$STAGE_DIR" "$RELEASE_DIR"'))
        self.assertIn("rolling back", source)
        self.assertIsNone(re.search(r"(?m)^\s*sudo\b", source))

    def test_help_does_not_modify_the_machine(self):
        result = subprocess.run(
            ["bash", str(INSTALLER), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Installs or updates AgentsServer", result.stdout)

    def test_same_version_reinstall_keeps_the_replaced_runtime_for_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            fake_bin = root / "bin"
            install_root = root / "install"
            fake_bin.mkdir()
            home.mkdir()
            self.write_executable(fake_bin / "uname", "#!/bin/sh\necho Linux\n")
            self.write_executable(fake_bin / "systemctl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "curl", "#!/bin/sh\nexit 0\n")
            self.write_executable(fake_bin / "uv", """#!/bin/sh
project=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--project" ]; then project="$2"; shift 2; else shift; fi
done
mkdir -p "$project/.venv/bin"
printf '#!/bin/sh\nexit 0\n' > "$project/.venv/bin/python"
chmod 755 "$project/.venv/bin/python"
""")
            environment = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{fake_bin}:/usr/bin:/bin",
                "AGENTS_SERVER_INSTALL_DIR": str(install_root),
                "AGENTS_SERVER_CONFIG_DIR": str(root / "config"),
                "ZENITHBOT_AGENT_DIR": str(root / "state"),
            }

            for attempt in range(2):
                result = subprocess.run(
                    ["bash", str(INSTALLER), "--port", "17850", "--non-interactive"],
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                if attempt == 0:
                    (install_root / "current" / "old-runtime-marker").write_text("old\n")

            previous = install_root / "previous"
            current = install_root / "current"
            self.assertTrue(previous.is_symlink())
            self.assertTrue((previous / "old-runtime-marker").is_file())
            self.assertFalse((current / "old-runtime-marker").exists())
            self.assertNotEqual(previous.resolve(), current.resolve())

    @staticmethod
    def write_executable(path: Path, source: str):
        path.write_text(source)
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
