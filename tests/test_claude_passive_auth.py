"""Claude installation checks must never renew or invalidate native credentials."""

import asyncio
import subprocess
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import httpx

import agent_server as server


class PassiveClaudeAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(server.RUNTIME_DIAGNOSTICS, {}, clear=True))
        self.enterContext(patch.dict(server.RUNTIME_DIAGNOSTIC_GENERATIONS, {}, clear=True))
        token = server.RUNTIME_CATALOG_DEADLINE.set(None)
        self.addCleanup(server.RUNTIME_CATALOG_DEADLINE.reset, token)
        self.enterContext(patch.object(server.shutil, "which", return_value="/fixture/claude"))
        self.command = self.enterContext(patch.object(server, "runtime_command", side_effect=self.version))

    @staticmethod
    def version(command, **_kwargs):
        if command != ["/fixture/claude", "--version"]:
            raise AssertionError(f"unexpected Claude command: {command!r}")
        return subprocess.CompletedProcess(command, 0, "2.1.283 (Claude Code)", "")

    async def test_unknown_authentication_can_start_native_run(self):
        diagnostic = await server.ensure_runtime_available("claude")
        self.assertEqual(diagnostic["status"], "unknown")
        self.assertTrue(diagnostic["installed"])
        self.assertIsNone(diagnostic["authenticated"])
        self.assertIsNone(diagnostic["action"])
        self.command.assert_called_once()

    async def test_http_catalog_readiness_never_runs_auth_status_and_preserves_native_evidence(self):
        self.enterContext(patch.object(server, "AGENT_TOKEN", "passive-auth-test-token"))
        self.enterContext(patch.object(server, "VALID_BACKENDS", {"claude"}))
        self.enterContext(patch.object(server, "parse_claude_help_catalog", return_value={
            "models": [{"value": "sonnet", "label": "Sonnet"}], "efforts": [],
        }))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                base_url="http://127.0.0.1", headers={"X-AgentsDock-Token": "passive-auth-test-token"}) as client:
            first = await client.get("/api/runtime/catalog?refresh=true")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json()["backends"]["claude"]["diagnostic"]["status"], "unknown")
            server.record_runtime_failure("claude", "Not logged in")
            rejected = await client.get("/api/runtime/catalog?refresh=true")
            self.assertEqual(rejected.status_code, 200)
            self.assertEqual(rejected.json()["backends"]["claude"]["diagnostic"]["status"], "unauthenticated")
            server.record_runtime_success("claude")
            recovered = await client.get("/api/runtime/catalog?refresh=true")
            self.assertEqual(recovered.status_code, 200)
            diagnostic = recovered.json()["backends"]["claude"]["diagnostic"]
            self.assertEqual(diagnostic["status"], "ready")
            self.assertNotIn("_installation_checked_at_epoch", diagnostic)
        self.assertEqual(self.command.call_count, 3)

    async def test_native_auth_failure_remains_visible_but_does_not_block_retry(self):
        server.record_runtime_failure("claude", "Failed to authenticate: OAuth session expired and could not be refreshed")
        failed = dict(server.RUNTIME_DIAGNOSTICS["claude"])
        self.assertEqual(failed["status"], "unauthenticated")
        self.assertIn("claude auth login", failed["action"])
        refreshed = server.runtime_diagnostic("claude", force=True)
        for key in ("status", "authenticated", "checked_at", "last_error", "last_error_at"):
            self.assertEqual(refreshed[key], failed[key])
        retried = await server.ensure_runtime_available("claude")
        self.assertEqual(retried["status"], "unauthenticated")
        # The normal native retry, not an auth-status subprocess, observes login.
        server.record_runtime_success("claude")
        success = await server.ensure_runtime_available("claude")
        self.assertEqual(success["status"], "ready")
        self.assertTrue(success["authenticated"])
        self.assertIsNone(success["last_error"])
        self.command.assert_called_once()

    async def test_native_success_timestamp_survives_expired_installation_refresh(self):
        with patch.object(server.time, "time", return_value=100.0):
            server.record_runtime_success("claude")
        observed = dict(server.RUNTIME_DIAGNOSTICS["claude"])
        with patch.object(server.time, "time", return_value=1000.0):
            refreshed = server.runtime_diagnostic("claude")
            await server.ensure_runtime_available("claude")
        self.assertEqual(refreshed["checked_at"], observed["checked_at"])
        self.assertEqual(refreshed["checked_at_epoch"], 100.0)
        self.assertEqual(refreshed["version"], "2.1.283 (Claude Code)")
        self.assertEqual(refreshed["status"], "ready")
        self.command.assert_called_once()
        self.assertNotIn("_installation_checked_at_epoch", server.public_runtime_diagnostic(refreshed))

    async def test_missing_or_broken_cli_still_blocks_admission(self):
        for status, installed in (("missing", False), ("error", True)):
            with self.subTest(status=status):
                diagnostic = server.runtime_diagnostic_payload("claude", status, installed=installed, authenticated=None)
                with patch.object(server, "runtime_diagnostic", return_value=diagnostic):
                    with self.assertRaises(HTTPException) as error:
                        await server.ensure_runtime_available("claude")
                self.assertEqual(error.exception.status_code, 503)
                self.assertNotIn("auth login", str(error.exception.detail))

    def test_forced_recheck_cannot_replace_newer_native_result(self):
        for success in (False, True):
            with self.subTest(success=success):
                def delayed_installation_check(_backend):
                    if success:
                        server.record_runtime_success("claude")
                    else:
                        server.record_runtime_failure("claude", "Not logged in")
                    return server.runtime_diagnostic_payload("claude", "unknown", installed=True, authenticated=None)

                with patch.object(server, "probe_runtime", side_effect=delayed_installation_check):
                    result = server.runtime_diagnostic("claude", force=True)
                self.assertEqual(result["status"], "ready" if success else "unauthenticated")

    def test_network_timeout_is_not_a_login_failure(self):
        server.record_runtime_failure("claude", "request timed out while connecting")
        result = server.runtime_diagnostic("claude", force=True)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["authenticated"])
        self.assertIsNone(result["action"])

    async def test_concurrent_manual_and_admission_checks_never_start_auth_commands(self):
        await asyncio.gather(*[
            asyncio.to_thread(server.runtime_diagnostic, "claude", force=True)
            for _ in range(4)
        ], server.ensure_runtime_available("claude"))
        self.assertTrue(self.command.called)
        self.assertTrue(all(call.args[0] == ["/fixture/claude", "--version"] for call in self.command.call_args_list))
