"""Synthetic API-key lifecycle; never read or modify the user's Cursor login."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi import HTTPException
import cursor_api_key
import provider_connections as connections
from side_questions import SideQuestionError

INPUT = {"base_url": cursor_api_key.ENDPOINT, "protocol": "cursor", "auth_header": "bearer",
         "api_key": "synthetic-cursor-key", "model": None, "expected_revision": 0}


class CursorKeyTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_is_read_only_bounded_and_overrides_native_auth(self):
        run = AsyncMock(side_effect=["2026.09.26-dd393fe", "Available models\nauto - Auto (default)\nsonnet - Sonnet\n"])
        original = {"CURSOR_AUTH_TOKEN": "wrong-account", "AGENT_CLI_CREDENTIAL_STORE": "file",
                    "CURSOR_API_ENDPOINT": "https://untrusted.invalid", "PATH": "/fixture"}
        with patch.object(cursor_api_key, "run_isolated_command", run):
            result = await cursor_api_key.catalog(INPUT, executable="/fixture/agent", env=original)
        self.assertEqual(result["discovery_status"], "ready")
        self.assertEqual(len(result["models"]), 2)
        call = run.call_args_list[1]
        self.assertEqual(call.args[0], ["/fixture/agent", "--list-models"])
        self.assertEqual(call.kwargs["env"]["CURSOR_API_KEY"], INPUT["api_key"])
        self.assertEqual(call.kwargs["env"]["AGENT_CLI_CREDENTIAL_STORE"], "memory")
        self.assertEqual(call.kwargs["env"]["CURSOR_AUTH_TOKEN"], "")
        self.assertEqual(call.kwargs["env"]["DIRENV_DISABLE"], "1")
        self.assertEqual(call.kwargs["env"]["CURSOR_API_ENDPOINT"], cursor_api_key.ENDPOINT)
        self.assertEqual(original["CURSOR_AUTH_TOKEN"], "wrong-account")
        self.assertNotIn(INPUT["api_key"], repr(call.args))
        self.assertEqual(call.kwargs["prompt"], "")
        self.assertLessEqual(call.kwargs["timeout"], 20)

    async def test_unsupported_build_never_attempts_key_exchange(self):
        for version in ["2026.09.10-old", "unknown"]:
            with patch.object(cursor_api_key, "run_isolated_command", AsyncMock(return_value=version)) as run:
                self.assertTrue((await cursor_api_key.catalog(INPUT, executable="/fixture/agent", env={}))["cli_update_required"])
                self.assertEqual(run.call_count, 1)

    async def test_failure_never_uses_native_auth_or_reflects_output(self):
        with patch.object(cursor_api_key, "run_isolated_command", AsyncMock(side_effect=["2026.09.26-fixture", SideQuestionError(503, INPUT["api_key"])])) as run:
            result = await cursor_api_key.catalog(INPUT, executable="/fixture/agent", env={})
        self.assertEqual(result, {"models": [], "discovery_status": "unavailable"})
        self.assertEqual(run.call_count, 2)

    def test_binding_forget_instance_isolation_and_missing_key_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = connections.ConnectionStore(Path(temporary) / "instance-one")
            other = connections.ConnectionStore(Path(temporary) / "instance-two")
            saved = store.write("cursor", 0, INPUT, "verified")
            self.assertEqual(saved["model"], "auto")
            self.assertNotIn(INPUT["api_key"], repr(saved))
            session = {"backend": "cursor", "provider_connection": "custom"}
            bound = store.bind(session)
            session["provider_connection_revision"] = bound["credential_id"]
            env = store.cursor_overrides(session)
            self.assertEqual(env["AGENT_CLI_CREDENTIAL_STORE"], "memory")
            store.write("cursor", 1, None)
            self.assertFalse(store.catalog("cursor")["available"])
            self.assertFalse(other.public("cursor")["configured"])
            with self.assertRaisesRegex(HTTPException, "forgotten"): store.cursor_overrides(session)
            other.write("cursor", 0, INPUT, "verified")
            other_session = {"backend": "cursor", "provider_connection": "custom"}
            other_session["provider_connection_revision"] = other.bind(other_session)["credential_id"]
            self.assertEqual(other.cursor_overrides(other_session), env)
            store.write("cursor", 2, {**INPUT, "expected_revision": 2}, "verified")
            with self.assertRaisesRegex(HTTPException, "forgotten"):
                connections.ConnectionStore(store.root).cursor_overrides(session)
            self.assertEqual(store.cursor_overrides({"backend": "cursor"}), {})
            with self.assertRaises(HTTPException): store.cursor_overrides({**session, "provider_connection_revision": None})
            self.assertEqual(store.redact(session, INPUT["api_key"]), "<api-key>")

    def test_cursor_cannot_be_redirected_to_an_arbitrary_gateway(self):
        for patch_value in [{"base_url": "https://gateway.invalid"}, {"protocol": "responses"}, {"auth_header": "x-api-key"}]:
            with self.assertRaises(HTTPException): connections.selection("cursor", {**INPUT, **patch_value})
