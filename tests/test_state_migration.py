import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


class StateMigrationTests(unittest.TestCase):
    def test_default_legacy_directory_moves_to_agentsdock(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            legacy = home / ".zenithbot-agent"
            legacy.mkdir()
            (legacy / "sessions.json").write_text('{"kept": true}\n')
            with patch.dict(os.environ, {}, clear=True), patch.object(agent_server.Path, "home", return_value=home):
                resolved = agent_server.resolve_state_dir()

            canonical = home / ".agentsdock"
            self.assertEqual(resolved, canonical)
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(legacy.resolve(), canonical.resolve())
            self.assertEqual((canonical / "sessions.json").read_text(), '{"kept": true}\n')

    def test_agentsdock_setting_wins_over_legacy_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            canonical = root / "canonical"
            legacy = root / "legacy"
            with patch.dict(os.environ, {
                "AGENTSDOCK_STATE_DIR": str(canonical),
                "ZENITHBOT_AGENT_DIR": str(legacy),
            }, clear=True):
                self.assertEqual(agent_server.resolve_state_dir(), canonical)

    def test_legacy_custom_state_override_remains_supported(self):
        with tempfile.TemporaryDirectory() as temporary:
            custom = Path(temporary) / "custom-state"
            with patch.dict(os.environ, {"ZENITHBOT_AGENT_DIR": str(custom)}, clear=True):
                self.assertEqual(agent_server.resolve_state_dir(), custom)


if __name__ == "__main__":
    unittest.main()
