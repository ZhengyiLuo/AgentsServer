"""Credential fixtures and generation routing; never a real login/model turn."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException
import codex_auth
from tests import test_codex_binary_refresh_isolated as binary


def jwt(claims):
    return "fixture." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".fixture"


class NativeLoginRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        self.native = self.root / "native"
        self.native.mkdir()
        self.env = {"HOME": str(self.root), "CODEX_HOME": str(self.native)}
        self.claims = {"sub": "synthetic-user", "auth_time": 100, "iat": 100, "exp": 200,
                       "https://api.openai.com/auth": {"chatgpt_account_id": "synthetic-account"}}
        self.auth = {"auth_mode": "chatgpt", "tokens": {"id_token": jwt(self.claims),
                     "account_id": "synthetic-account", "access_token": "synthetic-access",
                     "refresh_token": "synthetic-refresh"}, "last_refresh": "before"}

    def write(self):
        (self.native / "auth.json").write_text(json.dumps(self.auth))

    def revision(self):
        return codex_auth.native_login_revision(self.env, cwd=str(self.root))

    def test_same_account_login_changes_but_token_refresh_and_touch_do_not(self):
        self.write()
        original = self.revision()
        self.assertIsNotNone(original)
        self.auth["tokens"].update(access_token="new-access", refresh_token="new-refresh")
        self.auth["last_refresh"] = "after"
        self.claims.update(iat=150, exp=250)
        self.auth["tokens"]["id_token"] = jwt(self.claims)
        self.write()
        os.utime(self.native / "auth.json", None)
        self.assertEqual(original, self.revision())
        self.claims["auth_time"] = 150
        self.auth["tokens"]["id_token"] = jwt(self.claims)
        self.write()
        self.assertNotEqual(original, self.revision())

    def test_account_switch_without_auth_time_is_detected(self):
        self.claims.pop("auth_time")
        self.auth["tokens"]["id_token"] = jwt(self.claims)
        self.write()
        original = self.revision()
        self.auth["tokens"]["account_id"] = "other-account"
        self.write()
        self.assertNotEqual(original, self.revision())

    def test_api_key_change_and_same_content_rewrite(self):
        self.auth = {"OPENAI_API_KEY": "synthetic-key-one"}
        self.write()
        original = self.revision()
        self.write()
        self.assertEqual(original, self.revision())
        self.auth["OPENAI_API_KEY"] = "synthetic-key-two"
        self.write()
        self.assertNotEqual(original, self.revision())
        self.assertEqual(repr(original), "LoginRevision()")

    def test_missing_malformed_oversized_and_non_regular_are_unknown(self):
        path = self.native / "auth.json"
        self.assertIsNone(self.revision())
        for contents in ("{", "[]", "{}", "x" * (codex_auth._MAX_AUTH_BYTES + 1)):
            path.write_text(contents)
            self.assertIsNone(self.revision())
        path.unlink()
        os.mkfifo(path)
        self.assertIsNone(self.revision())

    def test_keyring_auto_ephemeral_and_bad_config_do_not_read_leftover_file(self):
        self.write()
        for contents in ('cli_auth_credentials_store = "keyring"',
                         'cli_auth_credentials_store = "auto"',
                         'cli_auth_credentials_store = "ephemeral"', '['):
            (self.native / "config.toml").write_text(contents)
            self.assertIsNone(self.revision())

    def test_child_home_relative_codex_home_and_environment_auth(self):
        self.write()
        original = self.revision()
        self.env["CODEX_HOME"] = "native"
        self.assertEqual(original, self.revision())
        self.env["CODEX_HOME"] = "~/native"
        self.assertEqual(original, self.revision())
        self.env["OPENAI_API_KEY"] = "synthetic-env-key"
        self.assertIsNone(self.revision())

    def test_explicit_handoff_skips_process_only_login_or_broken_config(self):
        for contents, expected in (('cli_auth_credentials_store = "ephemeral"', False),
                                   ('[', False), ('cli_auth_credentials_store = "keyring"', True)):
            (self.native / "config.toml").write_text(contents)
            self.assertEqual(codex_auth.native_login_handoff_supported(self.env, cwd=str(self.root)), expected)


class LoginHandoffTests(binary.BinaryRefreshTests):
    def setUp(self):
        super().setUp()
        self.login = codex_auth.LoginRevision(b"synthetic-first-login")
        self.ns["codex_auth"].native_login_revision = lambda *_args, **_kwargs: self.login

    async def relogin(self):
        self.login = codex_auth.LoginRevision(b"synthetic-second-login")
        await self.ns["refresh_codex_app_server_login"]()

    async def preflight(self, session_id):
        # Match the production request task registration and lifecycle lock.
        current = asyncio.current_task()
        tasks = self.ns["SESSION_TURN_TASKS"].setdefault(session_id, set())
        tasks.add(current)
        try:
            async with self.ns["session_lifecycle_lock"](session_id):
                await self.ns["prepare_codex_login_turn"](self.ns["STORE"].sessions[session_id])
        finally:
            tasks.discard(current)

    async def test_new_login_keeps_running_work_and_routes_new_chat_to_fresh_manager(self):
        old = await self.manager("old")
        thread = self.load("old", old)
        old.client._turns_by_thread[thread] = SimpleNamespace(_completed=False)
        self.ns["BUSY_SESSIONS"].add("old")
        await self.relogin()
        new = await self.manager("new")
        self.assertIsNot(new, old)
        self.assertIs(self.ns["existing_codex_app_server_manager_for_thread"](thread), old)
        await self.drain()
        self.assertFalse(old.closed)
        self.assertFalse(new.closed)
        with self.assertRaises(HTTPException) as error:
            await self.manager("old")
        self.assertEqual(error.exception.status_code, 409)

    async def test_idle_preflight_releases_same_thread_before_next_turn(self):
        old = await self.manager("idle")
        thread = self.load("idle", old)
        await self.relogin()
        await self.preflight("idle")
        new = await self.manager("idle")
        self.assertIsNot(new, old)
        self.assertEqual(self.ns["STORE"].sessions["idle"]["codex_thread_id"], thread)
        self.ns["evict_codex_app_server_thread"].assert_awaited_once_with(old, thread, reinsert_on_failure=True)

    async def test_busy_queue_waits_then_promotion_uses_same_identity(self):
        old = await self.manager("queued")
        thread = self.load("queued", old)
        self.ns["BUSY_SESSIONS"].add("queued")
        await self.relogin()
        await self.preflight("queued")
        self.assertTrue(old.is_thread_loaded(thread))
        self.ns["BUSY_SESSIONS"].clear()
        await self.preflight("queued")
        self.assertIsNot(await self.manager("queued"), old)

    async def test_pending_nonturn_request_blocks_unsubscribe_and_process_close(self):
        old = await self.manager("idle")
        self.load("idle", old)
        old.client._pending[1] = ("thread/resume", object(), None)
        await self.relogin()
        with self.assertRaises(HTTPException):
            await self.preflight("idle")
        await self.drain()
        self.assertFalse(old.closed)
        self.ns["evict_codex_app_server_thread"].assert_not_awaited()
        old.client._pending.clear()
        await self.preflight("idle")
        self.assertIsNot(await self.manager("idle"), old)

    async def test_close_rechecks_after_start_lock_wait(self):
        old = await self.manager("idle")
        await self.relogin()
        waiting = asyncio.Event()
        lock = asyncio.Lock()
        await lock.acquire()
        class ObservedLock:
            async def __aenter__(self):
                waiting.set()
                await lock.acquire()
            async def __aexit__(self, *_args):
                lock.release()
        old.client._start_lock = ObservedLock()
        draining = asyncio.create_task(self.drain())
        await asyncio.wait_for(waiting.wait(), 1)
        old.client._turns_by_thread["late"] = SimpleNamespace(_completed=False)
        old.client._pending[1] = ("thread/resume", object(), None)
        lock.release()
        await draining
        self.assertFalse(old.closed)

    async def test_caller_before_request_gap_prevents_handoff(self):
        old = await self.manager("idle")
        self.load("idle", old)
        borrowed, release = asyncio.Event(), asyncio.Event()
        async def borrow():
            await self.ns["codex_app_server_manager"](self.ns["STORE"].sessions["idle"])
            borrowed.set()
            await release.wait()
        task = asyncio.create_task(borrow())
        await borrowed.wait()
        try:
            await self.relogin()
            with self.assertRaises(HTTPException):
                await self.preflight("idle")
            self.ns["evict_codex_app_server_thread"].assert_not_awaited()
        finally:
            release.set()
            await task
        await self.preflight("idle")

    async def test_goal_terminal_side_chat_and_subagent_keep_owner(self):
        old = await self.manager("idle")
        self.load("idle", old)
        await self.relogin()
        for kind in ("goal", "terminal", "side", "subagent"):
            old.goal = {"status": "active"} if kind == "goal" else None
            old.terminals = [{"id": "background"}] if kind == "terminal" else []
            self.ns["SIDE_QUESTIONS"].active_session_ids = lambda: {"idle"} if kind == "side" else set()
            self.ns["codex_session_has_live_subagents"] = lambda _sid: kind == "subagent"
            with self.subTest(kind=kind), self.assertRaises(HTTPException):
                await self.preflight("idle")
            self.assertFalse(old.closed)

    async def test_cancelled_handoff_preserves_owner_and_releases_maintenance_fence(self):
        old = await self.manager("idle")
        self.load("idle", old)
        await self.relogin()
        entered = asyncio.Event()
        async def slow(_thread):
            entered.set()
            await asyncio.Event().wait()
        old.get_thread_goal.side_effect = slow
        task = asyncio.create_task(self.preflight("idle"))
        await entered.wait()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertIs(self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"]["idle"], old)
        self.assertFalse(self.ns["SERVER_MAINTENANCE_SESSIONS"])
        self.assertFalse(old.closed)

    async def test_concurrent_detection_retires_once_and_failed_probe_does_not(self):
        old = await self.manager("idle")
        self.login = None
        await self.ns["refresh_codex_app_server_login"]()
        self.assertIs(self.ns["CODEX_APP_SERVER_MANAGER"], old)
        self.login = codex_auth.LoginRevision(b"changed")
        await asyncio.gather(*[self.ns["refresh_codex_app_server_login"]() for _ in range(5)])
        self.assertEqual(self.ns["CODEX_RETIRED_APP_SERVER_MANAGERS"], [old])

    async def test_explicit_recheck_handles_opaque_store_without_touching_custom_manager(self):
        self.login = None
        old = await self.manager("idle")
        custom = binary.Manager()
        custom._agentsdock_provider_revision = "custom-revision"
        self.ns["CODEX_CUSTOM_APP_SERVER_MANAGERS"]["custom-revision"] = custom
        await self.ns["refresh_codex_app_server_login"](request_handoff=True)
        self.assertIs(self.ns["CODEX_CUSTOM_APP_SERVER_MANAGERS"]["custom-revision"], custom)
        self.assertFalse(custom.closed)
        self.assertIn(old, self.ns["CODEX_RETIRED_APP_SERVER_MANAGERS"])

    async def test_failed_replacement_does_not_reassign_stale_owner(self):
        old = await self.manager("idle")
        self.load("idle", old)
        await self.relogin()
        await self.preflight("idle")
        new = await self.manager("idle")
        new.start = AsyncMock(side_effect=RuntimeError("fixture spawn failure"))
        with self.assertRaises(RuntimeError):
            await new.start()
        self.assertIs(self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"]["idle"], new)
        self.assertIsNot(new, old)

    async def test_shutdown_does_not_start_detection_or_replace_manager(self):
        old = await self.manager("idle")
        self.ns["CODEX_MANAGER_CLOSING"] = True
        await self.relogin()
        self.assertIs(self.ns["CODEX_APP_SERVER_MANAGER"], old)

    async def test_explicit_recheck_preserves_unsupported_process_only_login(self):
        old = await self.manager("idle")
        self.ns["codex_auth"].native_login_handoff_supported = lambda *_args, **_kwargs: False
        await self.ns["refresh_codex_app_server_login"](request_handoff=True)
        self.assertIs(self.ns["CODEX_APP_SERVER_MANAGER"], old)
        self.assertFalse(old.closed)

    async def test_login_also_fences_an_older_binary_generation(self):
        old = await self.manager("idle")
        self.load("idle", old)
        await self.upgrade()
        await self.manager("new")
        await self.relogin()
        self.assertTrue(old._agentsdock_login_superseded)
        await self.preflight("idle")
        self.assertIsNot(await self.manager("idle"), old)

    async def test_unsubscribe_without_proven_release_keeps_owner(self):
        old = await self.manager("idle")
        self.load("idle", old)
        await self.relogin()
        self.ns["evict_codex_app_server_thread"] = AsyncMock(return_value=True)
        with self.assertRaises(HTTPException):
            await self.preflight("idle")
        self.assertIs(self.ns["CODEX_SESSION_APP_SERVER_MANAGERS"]["idle"], old)
