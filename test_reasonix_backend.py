import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_server


class ReasonixCommandTests(unittest.TestCase):
    def test_build_cmd_print_mode_with_model_and_prompt_last(self):
        sess = {"model": "deepseek-flash"}
        cmd = agent_server.build_reasonix_cmd("s1", sess, "do the thing")
        self.assertEqual(cmd[1:6], ["run", "-p", "--output-format", "stream-json", "--permission-mode"])
        self.assertEqual(cmd[6], "bypassPermissions")
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "deepseek-flash")
        self.assertEqual(cmd[-1], "do the thing")
        self.assertNotIn("--resume", cmd)

    def test_build_cmd_resumes_by_machine_session_id(self):
        cmd = agent_server.build_reasonix_cmd("s1", {}, "again", provider_id="20260920-040919-x")
        self.assertEqual(cmd[cmd.index("--resume") + 1], "20260920-040919-x")
        self.assertEqual(cmd[-1], "again")

    def test_redaction_hides_prompt(self):
        cmd = agent_server.build_reasonix_cmd("s1", {"model": "m"}, "secret prompt text")
        redacted = agent_server.redacted_provider_argv(cmd, agent_server.BACKEND_REASONIX)
        self.assertNotIn("secret prompt text", redacted)
        self.assertIn("<prompt>", redacted)


class ReasonixProbeTests(unittest.TestCase):
    def _runtime_command_mock(self, doctor_stdout, returncode=0):
        def fake_runtime_command(cmd):
            result = subprocess.CompletedProcess(cmd, returncode, stdout=doctor_stdout, stderr="")
            return result
        return fake_runtime_command

    def test_probe_ready_when_provider_key_present(self):
        doctor = json.dumps({
            "version": "v1.33.0",
            "default_model": "deepseek-flash",
            "providers": [
                {"name": "deepseek-flash", "key_present": True, "models": ["deepseek-v4-flash"]},
                {"name": "deepseek-pro", "key_present": True, "models": ["deepseek-v4-pro"]},
            ],
        })
        with patch.object(agent_server, "runtime_command", side_effect=self._runtime_command_mock(doctor)):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_REASONIX)
        self.assertEqual(diagnostic["status"], "ready")
        self.assertTrue(diagnostic["authenticated"])

    def test_probe_unauthenticated_when_no_key_present(self):
        doctor = json.dumps({
            "default_model": "deepseek-flash",
            "providers": [{"name": "deepseek-flash", "key_present": False, "models": ["m"]}],
        })
        with patch.object(agent_server, "runtime_command", side_effect=self._runtime_command_mock(doctor)):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_REASONIX)
        self.assertEqual(diagnostic["status"], "unauthenticated")
        self.assertFalse(diagnostic["authenticated"])

    def test_catalog_uses_provider_names_as_models(self):
        doctor = json.dumps({
            "default_model": "deepseek-flash",
            "providers": [
                {"name": "deepseek-flash", "models": ["deepseek-v4-flash"]},
                {"name": "deepseek-pro", "models": ["deepseek-v4-pro"]},
            ],
        })
        with patch.object(agent_server, "runtime_command", side_effect=self._runtime_command_mock(doctor)):
            catalog = agent_server.discover_reasonix_catalog()
        values = [m["value"] for m in catalog["models"]]
        self.assertIn("deepseek-flash", values)
        self.assertIn("deepseek-pro", values)
        self.assertEqual(catalog["default_model"], "deepseek-flash")


class ReasonixSessionIdentityTests(unittest.TestCase):
    def test_identity_key_registered(self):
        self.assertEqual(
            agent_server.PROVIDER_IDENTITY_KEYS[agent_server.BACKEND_REASONIX],
            "reasonix_session_id",
        )
        self.assertIn(agent_server.BACKEND_REASONIX, agent_server.VALID_BACKENDS)

    def test_public_session_exposes_reasonix_identity(self):
        sess = {
            "id": "s1",
            "backend": "reasonix",
            "reasonix_session_id": "20260920-040919-x",
        }
        public = agent_server.public_session(sess)
        self.assertEqual(public.get("reasonix_session_id"), "20260920-040919-x")

    def test_standalone_context_clears_reasonix_identity(self):
        sess = {"id": "s1", "backend": "reasonix", "reasonix_session_id": "x"}
        isolated = agent_server.standalone_provider_session(sess)
        self.assertIsNone(isolated["reasonix_session_id"])
        self.assertEqual(sess["reasonix_session_id"], "x")


if __name__ == "__main__":
    unittest.main()
