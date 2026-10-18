import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest

from fastapi import FastAPI, HTTPException
import httpx
import gemini_runner


class GeminiPermissions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        gemini_runner.PERMISSIONS.clear()
        self.events = []
        async def append(sid, kind, payload): self.events.append((sid, kind, payload))
        @asynccontextmanager
        async def lock(sid): yield
        def auth(request):
            if request.headers.get("X-AgentsDock-Token") != "fixture" or request.headers.get("Origin"):
                raise HTTPException(403)
        self.api = SimpleNamespace(STORE=SimpleNamespace(sessions={"s": {"backend": "gemini"}, "other": {"backend": "gemini"}}),
            ACTIVE={"s": {"run_id": "r", "backend": "gemini"}}, DELETING_SESSIONS=set(), STOPPED_RUNS=set(),
            append_event=append, now_iso=lambda: "2026-10-15T00:00:00Z", bounded_codex_interaction_value=lambda x: x,
            require_native_admin_control=auth, session_lifecycle_lock=lock, ensure_session_not_deleting=lambda s: None)
        app = FastAPI(); app.include_router(gemini_runner.router(self.api))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test", headers={"X-AgentsDock-Token": "fixture"})

    async def asyncTearDown(self):
        await self.client.aclose()
        gemini_runner.PERMISSIONS.clear()

    async def pending(self):
        self.task = asyncio.create_task(gemini_runner.ask_permission(self.api, "s", "r", {
            "sessionId": "native", "toolCall": {"toolCallId": "call-1", "title": "Write test"},
            "options": [{"kind": "allow_once", "optionId": "once"}, {"kind": "reject_once", "optionId": "no"},
                        {"kind": "allow_always", "optionId": "always"}]}))
        self.addAsyncCleanup(self.cancel_task)
        await asyncio.sleep(0)
        return next(iter(gemini_runner.PERMISSIONS))

    async def cancel_task(self):
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def test_native_approval_only_once_and_wrong_chat_rejected(self):
        key = await self.pending()
        public = (await self.client.get("/api/sessions/s/gemini/runtime")).json()["pending_interactions"][0]
        self.assertEqual(public["params"]["availableDecisions"], ["accept", "decline"])
        response = {"response": {"decision": "accept"}}
        self.assertEqual((await self.client.post(f"/api/sessions/other/gemini/interactions/{key}/resolve", json=response)).status_code, 409)
        self.assertEqual((await self.client.post(f"/api/sessions/s/gemini/interactions/{key}/resolve", headers={"Origin": "https://untrusted.test"}, json=response)).status_code, 403)
        self.assertEqual((await self.client.post(f"/api/sessions/s/gemini/interactions/{key}/resolve", json=response)).status_code, 200)
        self.assertEqual(await self.task, {"outcome": {"outcome": "selected", "optionId": "once"}})
        self.assertFalse(gemini_runner.PERMISSIONS)

    async def test_stopped_or_replaced_run_cannot_approve(self):
        key = await self.pending()
        self.api.ACTIVE["s"]["run_id"] = "next"
        response = await self.client.post(f"/api/sessions/s/gemini/interactions/{key}/resolve", json={"response": {"decision": "accept"}})
        self.assertEqual(response.status_code, 409)
        self.assertEqual((await self.client.get("/api/sessions/s/gemini/runtime")).json()["pending_interactions"], [])

    async def test_request_validation_is_bounded_and_no_persistent_allow(self):
        key = await self.pending()
        path = f"/api/sessions/s/gemini/interactions/{key}/resolve"
        self.assertEqual((await self.client.post(path, content=b"x" * 4097)).status_code, 413)
        self.assertEqual((await self.client.post(path, content=b"{")).status_code, 400)
        self.assertEqual((await self.client.post(path, json={"response": {"decision": "acceptForSession"}})).status_code, 400)
        self.assertEqual((await self.client.post(path, json={"response": {"decision": []}})).status_code, 400)
