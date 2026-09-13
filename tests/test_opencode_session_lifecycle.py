"""HTTP lifecycle regressions for imported/resumed OpenCode sessions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import agent_server


PROVIDER_SESSION_ID = "ses_f86f2df67ffep5CKpMOXIyR44R"


class OpenCodeSessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.cwd = self.root / "workspace"
        self.cwd.mkdir()

        self.previous = {
            "state_dir": agent_server.STATE_DIR,
            "sessions_file": agent_server.SESSIONS_FILE,
            "files_root": agent_server.FILES_ROOT,
            "code_diffs_root": agent_server.CODE_DIFFS_ROOT,
            "cross_chat_authority_root": agent_server.CROSS_CHAT_AUTHORITY_ROOT,
            "sessions": agent_server.STORE.sessions,
            "busy_sessions": agent_server.BUSY_SESSIONS,
            "active": agent_server.ACTIVE,
        }
        agent_server.STATE_DIR = self.root / "state"
        agent_server.SESSIONS_FILE = agent_server.STATE_DIR / "sessions.json"
        agent_server.FILES_ROOT = agent_server.STATE_DIR / "files"
        agent_server.CODE_DIFFS_ROOT = agent_server.STATE_DIR / "code_diffs"
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = (
            agent_server.STATE_DIR / "cross_chat_authority"
        )
        agent_server.STORE.sessions = {}
        agent_server.BUSY_SESSIONS = set()
        agent_server.ACTIVE = {}

        self.auth_patch = patch.object(agent_server, "AGENT_TOKEN", "test-token")
        self.auth_patch.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=agent_server.app),
            base_url="http://test",
            headers={"x-agentsdock-token": "test-token"},
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.auth_patch.stop()
        agent_server.STATE_DIR = self.previous["state_dir"]
        agent_server.SESSIONS_FILE = self.previous["sessions_file"]
        agent_server.FILES_ROOT = self.previous["files_root"]
        agent_server.CODE_DIFFS_ROOT = self.previous["code_diffs_root"]
        agent_server.CROSS_CHAT_AUTHORITY_ROOT = self.previous[
            "cross_chat_authority_root"
        ]
        agent_server.STORE.sessions = self.previous["sessions"]
        agent_server.BUSY_SESSIONS = self.previous["busy_sessions"]
        agent_server.ACTIVE = self.previous["active"]
        self.temporary.cleanup()

    async def test_generic_provider_id_binds_resumable_opencode_session(self) -> None:
        response = await self.client.post(
            "/api/sessions",
            json={
                "backend": "opencode",
                "cwd": str(self.cwd),
                "provider_session_id": PROVIDER_SESSION_ID,
                "import_history": False,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()["session"]
        self.assertEqual(session["session_id"], PROVIDER_SESSION_ID)
        self.assertEqual(session["opencode_session_id"], PROVIDER_SESSION_ID)
        self.assertTrue(session["backend_locked"])
        stored = agent_server.STORE.sessions[session["id"]]
        self.assertEqual(stored["opencode_session_cwd"], str(self.cwd))

        blocked = await self.client.patch(
            f"/api/sessions/{session['id']}",
            json={"backend": "cursor"},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)

    async def test_explicit_opencode_id_uses_the_same_resume_binding(self) -> None:
        response = await self.client.post(
            "/api/sessions",
            json={
                "backend": "opencode",
                "cwd": str(self.cwd),
                "opencode_session_id": PROVIDER_SESSION_ID,
                "import_history": False,
            },
        )

        self.assertEqual(response.status_code, 200, response.text)
        session = response.json()["session"]
        self.assertEqual(session["session_id"], PROVIDER_SESSION_ID)
        self.assertEqual(session["opencode_session_id"], PROVIDER_SESSION_ID)
        self.assertTrue(session["backend_locked"])

    async def test_invalid_opencode_resume_id_is_rejected(self) -> None:
        response = await self.client.post(
            "/api/sessions",
            json={
                "backend": "opencode",
                "cwd": str(self.cwd),
                "provider_session_id": "not a provider id",
                "import_history": False,
            },
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(agent_server.STORE.sessions, {})

    async def test_idle_fork_preserves_mode_and_seeds_bounded_memory(self) -> None:
        created = await self.client.post(
            "/api/sessions",
            json={
                "backend": "opencode",
                "cwd": str(self.cwd),
                "opencode_permission_mode": "plan",
            },
        )
        self.assertEqual(created.status_code, 200, created.text)
        parent_id = created.json()["session"]["id"]
        await agent_server.append_event(
            parent_id,
            "turn_started",
            {"run_id": "run-parent", "prompt": "Remember the blue number."},
        )
        await agent_server.append_event(
            parent_id,
            "turn_finished",
            {
                "run_id": "run-parent",
                "backend": "opencode",
                "exit_code": 0,
                "result_text": "The blue number is 417.",
                "is_error": False,
            },
        )

        response = await self.client.post(
            f"/api/sessions/{parent_id}/fork",
            json={"title": "OpenCode fork"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        child = response.json()["session"]
        self.assertEqual(child["backend"], "opencode")
        self.assertEqual(child["opencode_permission_mode"], "plan")
        self.assertIsNone(child["opencode_session_id"])
        self.assertTrue(child["memory_forked"])
        self.assertFalse(child["memory_seed_used"])
        stored = agent_server.STORE.sessions[child["id"]]
        self.assertIn("The blue number is 417.", stored["memory_seed"])
        self.assertIn("OpenCode does not expose", stored["memory_fork_reason"])

    async def test_running_opencode_fork_remains_fail_closed(self) -> None:
        created = await self.client.post(
            "/api/sessions",
            json={"backend": "opencode", "cwd": str(self.cwd)},
        )
        self.assertEqual(created.status_code, 200, created.text)
        parent_id = created.json()["session"]["id"]
        agent_server.BUSY_SESSIONS.add(parent_id)

        response = await self.client.post(
            f"/api/sessions/{parent_id}/fork",
            json={},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("cannot fork a running chat safely", response.text)
        self.assertEqual(len(agent_server.STORE.sessions), 1)


if __name__ == "__main__":
    unittest.main()
