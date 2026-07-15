import subprocess
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


if __name__ == "__main__":
    unittest.main()
