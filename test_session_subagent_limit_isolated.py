"""Per-chat native limits using production models/routes and owned local state."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from test_codex_provider_sessions_isolated import make_namespace


class SessionSubagentLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="session-subagent-limit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.ns = make_namespace(self.root)
        self.store = self.ns["STORE"]

    async def create(self, **kwargs):
        return await self.store.create(self.ns["CreateSessionRequest"](**kwargs))

    async def update(self, sid, **kwargs):
        result = await self.ns["update_session"](sid, self.ns["UpdateSessionRequest"](**kwargs))
        return result["session"]

    async def test_set_clear_persist_and_public_summary_are_chat_scoped(self):
        for backend in ("codex", "claude"):
            with self.subTest(backend=backend):
                selected = await self.create(backend=backend, subagent_limit=3)
                other = await self.create(backend=backend, subagent_limit=8)
                selected["codex_config_overrides"] = {"agents": {"default_subagent_model": "child-model"}}
                siblings = copy.deepcopy(selected["codex_config_overrides"])
                changed = await self.update(selected["id"], subagent_limit=5)
                self.assertEqual(changed["subagent_limit"], 5)
                self.assertEqual(other["subagent_limit"], 8)
                for summary in (False, True):
                    public = self.ns["public_session"](selected, summary=summary)
                    self.assertEqual(public["subagent_limit"], 5)
                    self.assertTrue(public["subagent_limit_control"]["supported"])
                    if not summary:
                        self.assertEqual(public["subagent_limit_control"]["scope"], "chat")
                cleared = await self.update(selected["id"], subagent_limit=None)
                self.assertIsNone(cleared["subagent_limit"])
                persisted = json.loads((self.root / "sessions.json").read_text())
                self.assertIsNone(persisted[selected["id"]]["subagent_limit"])
                self.assertEqual(persisted[selected["id"]]["codex_config_overrides"], siblings)
                self.assertEqual(persisted[other["id"]]["subagent_limit"], 8)

    async def test_default_creation_is_sparse_and_clear_keeps_summary_tombstone(self):
        session = await self.create()
        self.assertNotIn("subagent_limit", session)
        summary = self.ns["public_session"](session, summary=True)
        self.assertNotIn("subagent_limit", summary)
        self.assertEqual(summary["subagent_limit_control"], {"supported": True})
        self.assertIsNone(self.ns["public_session"](session)["subagent_limit"])
        default_control_payload = len(JSONResponse({"sessions": [summary] * 182}).body)
        # Other full creation fields are unrelated to this contract. Its new
        # per-row default metadata stays under the existing list's 52-byte headroom.
        baseline = {key: value for key, value in summary.items() if key != "subagent_limit_control"}
        self.assertLess(default_control_payload - len(JSONResponse({"sessions": [baseline] * 182}).body), 52 * 182)
        await self.update(session["id"], subagent_limit=3)
        await self.update(session["id"], subagent_limit=None)
        self.assertIsNone(self.ns["public_session"](session, summary=True)["subagent_limit"])
        self.assertIn("subagent_limit", json.loads((self.root / "sessions.json").read_text())[session["id"]])

    async def test_invalid_request_values_fail_before_create_or_update_mutation(self):
        session = await self.create(title="Original", subagent_limit=3)
        before = copy.deepcopy(self.store.sessions)
        app = FastAPI()
        app.post("/sessions")(self.ns["create_session"])
        app.patch("/sessions/{session_id}")(self.ns["update_session"])
        with TestClient(app) as client:
            for invalid in (True, False, 0, -1, 1.0, 1.5, "3", [], {}):
                with self.subTest(invalid=invalid):
                    self.assertEqual(client.post("/sessions", json={"subagent_limit": invalid}).status_code, 422)
                    self.assertEqual(client.patch(f"/sessions/{session['id']}", json={
                        "title": "Must not change", "subagent_limit": invalid}).status_code, 422)
                    self.assertEqual(self.store.sessions, before)
        with self.assertRaises(HTTPException) as caught:
            await self.store.update(session["id"], {"title": "Must not change", "subagent_limit": True})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.store.sessions, before)

    async def test_claude_old_or_unknown_runtime_and_unsupported_backend_cannot_silently_save(self):
        claude = await self.create(backend="claude", subagent_limit=3)
        for version in (None, "2.1.216", "unrecognized"):
            self.ns["RUNTIME_DIAGNOSTICS"]["claude"] = {"version": version}
            with self.subTest(version=version):
                before = copy.deepcopy(self.store.sessions)
                with self.assertRaises(HTTPException) as caught:
                    await self.store.update(claude["id"], {"title": "Must not change", "subagent_limit": 4})
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(self.store.sessions, before)
                self.assertFalse(self.ns["public_session"](claude, summary=True)["subagent_limit_control"]["supported"])
                with self.assertRaises(HTTPException):
                    await self.create(backend="claude", subagent_limit=4)
                self.assertEqual(self.store.sessions, before)
        await self.update(claude["id"], subagent_limit=None)
        for backend in ("cursor", "codex"):
            self.ns["CODEX_TRANSPORT"] = "exec"
            with self.assertRaises(HTTPException) as caught:
                await self.create(backend=backend, subagent_limit=3)
            self.assertEqual(caught.exception.status_code, 409)

    async def test_busy_save_does_not_wait_for_lifecycle_or_touch_native_provider(self):
        session = await self.create(subagent_limit=3)
        sid = session["id"]
        self.ns["BUSY_SESSIONS"].add(sid)
        self.ns["ACTIVE"][sid] = object()
        lock = self.ns["session_lifecycle_lock"](sid)
        async with lock:
            changed = await asyncio.wait_for(self.update(sid, subagent_limit=6), 1)
        self.assertEqual(changed["subagent_limit"], 6)
        self.assertIn(sid, self.ns["ACTIVE"])
        self.ns["ensure_claude_permission_mode_update_allowed"].assert_not_awaited()
        self.assertEqual(changed["subagent_limit_control"]["applies_to"], "new_or_reloaded_threads")

    async def test_failed_save_restores_cap_and_unrelated_settings(self):
        session = await self.create(title="Original", subagent_limit=3)
        before = copy.deepcopy(self.store.sessions)
        self.store.save.side_effect = OSError("synthetic disk failure")
        self.store.persist_restored_state = AsyncMock()
        with self.assertRaises(OSError):
            await self.store.update(session["id"], {"title": "Must not remain", "subagent_limit": None})
        self.assertEqual(self.store.sessions, before)
        self.store.persist_restored_state.assert_awaited_once_with(durable=True)

    async def test_staged_fork_retains_limit_and_copies_other_native_overrides(self):
        parent = await self.create(subagent_limit=3)
        parent["codex_config_overrides"] = {"agents": {
            "default_subagent_model": "child-model", "max_threads": 7}, "model": "parent-model"}
        child = await self.store.create(self.ns["CreateSessionRequest"](subagent_limit=parent["subagent_limit"]),
            parent_id=parent["id"], initializing_fork=True)
        self.assertEqual(child["subagent_limit"], 3)
        self.assertEqual(child["codex_config_overrides"]["agents"], {
            "default_subagent_model": "child-model", "max_concurrent_threads_per_session": 7})
        child["codex_config_overrides"]["agents"]["default_subagent_model"] = "changed"
        self.assertEqual(parent["codex_config_overrides"]["agents"]["default_subagent_model"], "child-model")

    async def test_clearing_unknown_native_default_stays_pending_until_fresh_process_applies(self):
        session = await self.create(subagent_limit=3)
        manager = SimpleNamespace(generation=1, ready=True)
        self.ns["existing_codex_app_server_manager"] = lambda session: manager
        record = self.ns["record_codex_subagent_limit_application"]
        await record(manager, session["id"], dict(session))
        cleared = await self.update(session["id"], subagent_limit=None)
        self.assertEqual(cleared["subagent_limit_control"]["applies_to"], "next_provider_process_start")
        await record(manager, session["id"], dict(session))  # unsubscribe/resume retains native process
        self.assertIn("_codex_subagent_limit_reset_pending", session)
        self.ns["SERVER_INSTANCE_ID"] = "fresh-server-instance"
        self.assertEqual(self.ns["public_session"](session)["subagent_limit_control"]["applies_to"], "new_or_reloaded_threads")
        self.ns["SERVER_INSTANCE_ID"] = "owned-server-instance"
        manager.generation += 1
        await record(manager, session["id"], dict(session))
        self.assertNotIn("_codex_subagent_limit_reset_pending", session)
        self.assertNotIn("_codex_subagent_limit_applied", session)
        persisted = json.loads((self.root / "sessions.json").read_text())[session["id"]]
        self.assertNotIn("_codex_subagent_limit_reset_pending", persisted)

    async def test_clear_during_native_start_records_captured_override_and_known_default_applies_on_reload(self):
        session = await self.create(subagent_limit=3)
        captured = dict(session)
        await self.update(session["id"], subagent_limit=None)
        manager = SimpleNamespace(generation=1)
        record = self.ns["record_codex_subagent_limit_application"]
        await record(manager, session["id"], captured)
        self.assertIn("_codex_subagent_limit_reset_pending", session)
        session["codex_config_overrides"] = {"agents": {"max_concurrent_threads_per_session": 7}}
        await self.update(session["id"], subagent_limit=None)
        self.assertNotIn("_codex_subagent_limit_reset_pending", session)
        await record(manager, session["id"], dict(session))
        self.assertNotIn("_codex_subagent_limit_reset_pending", session)
        self.assertNotIn("_codex_subagent_limit_applied", session)


if __name__ == "__main__":
    unittest.main()
