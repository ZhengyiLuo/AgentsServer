import json
import subprocess
import unittest
from unittest.mock import patch

from fastapi import HTTPException

import agent_server


def completed(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=args, returncode=returncode, stdout=stdout, stderr=stderr)


class RuntimeDiagnosticTests(unittest.TestCase):
    def setUp(self) -> None:
        with agent_server.RUNTIME_DIAGNOSTICS_LOCK:
            agent_server.RUNTIME_DIAGNOSTICS.clear()

    def test_missing_runtime_is_explicit(self) -> None:
        with patch.object(agent_server.shutil, "which", return_value=None):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CLAUDE)
        self.assertEqual(diagnostic["status"], "missing")
        self.assertFalse(diagnostic["available"])
        self.assertIn("Install Claude Code", diagnostic["action"])

    def test_claude_ready_probe_does_not_expose_identity(self) -> None:
        responses = [
            completed(["claude", "--version"], stdout="2.3.4 (Claude Code)\n"),
            completed(
                ["claude", "auth", "status", "--json"],
                stdout=json.dumps({"loggedIn": True, "email": "private@example.com", "organizationName": "Secret"}),
            ),
        ]
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/claude"), patch.object(
            agent_server, "runtime_command", side_effect=responses
        ):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CLAUDE)
        self.assertEqual(diagnostic["status"], "ready")
        self.assertEqual(diagnostic["version"], "2.3.4 (Claude Code)")
        self.assertNotIn("private@example.com", json.dumps(diagnostic))
        self.assertNotIn("Secret", json.dumps(diagnostic))

    def test_codex_auth_failure_is_actionable(self) -> None:
        responses = [
            completed(["codex", "--version"], stdout="codex-cli 1.2.3\n"),
            completed(["codex", "login", "status"], returncode=1, stderr="Not logged in"),
        ]
        with patch.object(agent_server.shutil, "which", return_value="/usr/local/bin/codex"), patch.object(
            agent_server, "runtime_command", side_effect=responses
        ):
            diagnostic = agent_server.probe_runtime(agent_server.BACKEND_CODEX)
        self.assertEqual(diagnostic["status"], "unauthenticated")
        self.assertIn("codex login", diagnostic["action"])

    def test_transient_provider_failure_keeps_cli_ready(self) -> None:
        agent_server.store_runtime_diagnostic(agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CLAUDE,
            "ready",
            installed=True,
            authenticated=True,
            version="2.3.4",
        ))
        agent_server.record_runtime_failure(agent_server.BACKEND_CLAUDE, "529 overloaded")
        snapshot = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CLAUDE]
        self.assertEqual(snapshot["status"], "ready")
        self.assertIsNotNone(snapshot["last_error"])
        self.assertNotIn("checked_at_epoch", snapshot)

    def test_provider_thread_not_found_does_not_mark_cli_missing(self) -> None:
        agent_server.store_runtime_diagnostic(agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CODEX,
            "ready",
            installed=True,
            authenticated=True,
            version="1.2.3",
        ))
        agent_server.record_runtime_failure(
            agent_server.BACKEND_CODEX,
            "No conversation found with session ID: external-thread",
        )
        diagnostic = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CODEX]
        self.assertEqual(diagnostic["status"], "ready")
        self.assertTrue(diagnostic["installed"])

    def test_spawn_failure_marks_cli_missing(self) -> None:
        agent_server.record_runtime_failure(
            agent_server.BACKEND_CODEX,
            FileNotFoundError(2, "No such file or directory", "codex"),
            spawn_failure=True,
        )
        diagnostic = agent_server.runtime_diagnostics_snapshot()[agent_server.BACKEND_CODEX]
        self.assertEqual(diagnostic["status"], "missing")
        self.assertFalse(diagnostic["installed"])


class RuntimePreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_runtime_fails_before_launch(self) -> None:
        diagnostic = agent_server.runtime_diagnostic_payload(
            agent_server.BACKEND_CODEX,
            "unauthenticated",
            installed=True,
            authenticated=False,
        )
        with patch.object(agent_server, "runtime_diagnostic", return_value=diagnostic):
            with self.assertRaises(HTTPException) as raised:
                await agent_server.ensure_runtime_available(agent_server.BACKEND_CODEX)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.detail["code"], "runtime_unavailable")
        self.assertEqual(raised.exception.detail["backend"], "codex")


if __name__ == "__main__":
    unittest.main()
