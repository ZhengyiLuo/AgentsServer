"""OpenCode detection with the minimal PATH inherited by service managers."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


class OpenCodeRuntimeResolutionTests(unittest.TestCase):
    def test_compatibility_probe_requires_native_file_support(self):
        help_text = (
            "opencode run [message..]\n"
            " --format --dir --session --fork --model --agent\n"
        )
        result = subprocess.CompletedProcess(
            ["/test/opencode", "run", "--help"],
            0,
            stdout="",
            stderr=help_text,
        )
        with patch.object(agent_server, "runtime_command", return_value=result):
            compatible, missing, error = agent_server.opencode_cli_compatibility(
                "/test/opencode"
            )
        self.assertFalse(compatible)
        self.assertEqual(missing, ("--file",))
        self.assertEqual(error, "required headless flags are unavailable")

    def test_compatibility_probe_rejects_unvalidated_opencode_version(self):
        help_result = subprocess.CompletedProcess(
            ["/test/opencode", "run", "--help"],
            0,
            stdout=(
                "opencode run [message..]\n"
                " --format --dir --session --fork --model --agent --file\n"
            ),
            stderr="",
        )
        version_result = subprocess.CompletedProcess(
            ["/test/opencode", "--version"],
            0,
            stdout="1.18.30\n",
            stderr="",
        )
        with patch.object(
            agent_server,
            "runtime_command",
            side_effect=[help_result, version_result],
        ):
            compatible, missing, error = agent_server.opencode_cli_compatibility(
                "/test/opencode"
            )
        self.assertFalse(compatible)
        self.assertEqual(missing, ())
        self.assertIn("unsupported OpenCode CLI version '1.18.30'", error)
        self.assertIn("requires exactly 1.18.29", error)

    def test_compatibility_probe_accepts_validated_opencode_version(self):
        help_result = subprocess.CompletedProcess(
            ["/test/opencode", "run", "--help"],
            0,
            stdout=(
                "opencode run [message..]\n"
                " --format --dir --session --fork --model --agent --file\n"
            ),
            stderr="",
        )
        version_result = subprocess.CompletedProcess(
            ["/test/opencode", "--version"],
            0,
            stdout="1.18.29\n",
            stderr="",
        )
        with patch.object(
            agent_server,
            "runtime_command",
            side_effect=[help_result, version_result],
        ):
            compatible, missing, error = agent_server.opencode_cli_compatibility(
                "/test/opencode"
            )
        self.assertTrue(compatible)
        self.assertEqual(missing, ())
        self.assertEqual(error, "")

    def test_missing_runtime_during_catalog_refresh_degrades_without_raising(self):
        with patch.object(agent_server, "resolve_opencode_executable", return_value=None):
            catalog = agent_server.discover_opencode_catalog()
        self.assertEqual(catalog["models"], [])
        self.assertIn("failed", catalog["model_source"])

    def test_success_retains_compatibility_probed_executable(self):
        with patch.dict(agent_server.RUNTIME_DIAGNOSTICS, {
            "opencode": {"_executable": "/test/opencode", "version": "test"},
        }), patch.object(agent_server, "store_runtime_diagnostic") as store:
            agent_server.record_runtime_success("opencode")
        self.assertEqual(store.call_args.args[0]["_executable"], "/test/opencode")

    def test_default_probe_finds_official_install_outside_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / ".opencode" / "bin" / "opencode"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
            with patch.object(agent_server, "OPENCODE_BIN", "opencode"), \
                 patch.object(agent_server, "OPENCODE_BIN_OVERRIDE", ""), \
                 patch.object(agent_server.Path, "home", return_value=root), \
                 patch.object(agent_server, "runner_env", return_value={"PATH": str(root)}), \
                 patch.object(agent_server, "opencode_cli_compatibility", return_value=(True, (), "")):
                self.assertEqual(agent_server.resolve_opencode_executable(), str(binary.resolve()))

    def test_explicit_override_does_not_fall_back_to_another_install(self):
        with patch.object(agent_server, "OPENCODE_BIN", "/missing/chosen/opencode"), \
             patch.object(agent_server, "OPENCODE_BIN_OVERRIDE", "/missing/chosen/opencode"), \
             patch.object(agent_server, "runner_env", return_value={"PATH": "/usr/bin:/bin"}):
            self.assertEqual(agent_server.opencode_executable_candidates(), ("/missing/chosen/opencode",))
            self.assertIsNone(agent_server.resolve_opencode_executable())


class OpenCodeAdmissionResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_ready_cache_is_backfilled_before_admission(self):
        with patch.object(agent_server, "runtime_diagnostic", return_value={"backend": "opencode", "status": "ready"}), \
             patch.object(agent_server, "resolve_opencode_executable", return_value="/test/opencode"), \
             patch.object(agent_server, "store_runtime_diagnostic"):
            diagnostic = await agent_server.ensure_runtime_available("opencode")
        self.assertEqual(diagnostic["_executable"], "/test/opencode")


if __name__ == "__main__":
    unittest.main()
