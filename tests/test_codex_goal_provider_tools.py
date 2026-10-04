"""Native goal MCP requests through HTTP, real authority and helper execution.

The native notification/manager fixture is synthetic; this is not OAuth or
model-generation acceptance. No provider or user installation is contacted.
"""
import asyncio
from collections import OrderedDict
from contextlib import ExitStack
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
import agent_server as server


class CodexGoalProviderToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.sid, self.thread, self.turn, self.run = "goal-chat", "native-thread", "goal-turn-2", "run_goal"
        self.session = {"id": self.sid, "backend": "codex", "codex_thread_id": self.thread,
                        "codex_goal": {"status": "active", "objective": "Run fixture"}}
        self.active = {"backend": "codex", "transport": server.CODEX_TRANSPORT_APP_SERVER,
                       "run_id": self.run, "provider_thread_id": self.thread,
                       "provider_turn_id": self.turn, "provider_turn_ready": True,
                       "codex_native_operation": True, "codex_native_operation_kind": "goal_resume",
                       "codex_control_reservation_id": self.run}
        for name, value in {
            "ACTIVE": {self.sid: self.active}, "BUSY_SESSIONS": {self.sid},
            "CURRENT_TURNS": {self.sid: {"run_id": self.run, "codex_control_reservation_id": self.run}},
            "STOPPED_RUNS": set(), "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
            "CROSS_CHAT_CAPABILITIES": {}, "CROSS_CHAT_AUTHORITY_ROOT": self.root / "authority",
            "PROVIDER_TOOL_REPLAY": OrderedDict(), "PROVIDER_TOOL_REPLAY_TOMBSTONES": OrderedDict(),
            "ACTIVE_LOCK": asyncio.Lock(), "CROSS_CHAT_CAPABILITY_LOCK": asyncio.Lock(),
            "PROVIDER_TOOL_REPLAY_LOCK": asyncio.Lock(), "UNSAFE_HTTP_MUTATION_ADMISSION_LOCK": asyncio.Lock(),
            "UNSAFE_HTTP_MUTATION_TASKS": {}, "AGENT_TOKEN": "isolated-test-token",
            "CODEX_PROVIDER_MCP_HEADER_SECRET": "isolated-mcp-secret",
        }.items():
            self.stack.enter_context(patch.object(server, name, value))
        self.stack.enter_context(patch.object(server.STORE, "sessions", {self.sid: self.session}))
        self.manager = SimpleNamespace(ready=True, active_turn=lambda _: None)
        self.stack.enter_context(patch.object(server, "existing_codex_app_server_manager", return_value=self.manager))
        for name in ("managed_server_restart_blocks_work", "managed_server_update_blocks_work",
                     "managed_server_force_update_is_pending"):
            self.stack.enter_context(patch.object(server, name, return_value=False))
        self.authority = await server.issue_cross_chat_capability(
            self.sid, self.run, [], actions={"publish"}, async_route_v1=True,
        )
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1:7850")
        self.addAsyncCleanup(self.client.aclose)

    async def call(self, *, meta=None, call_id="native-call", secret="isolated-mcp-secret"):
        response = await self.client.post(server.CODEX_PROVIDER_MCP_PATH, headers={server.CODEX_PROVIDER_MCP_HEADER_NAME: secret}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                "name": "run", "arguments": {"helper": "publish", "arguments": ["--help"]},
                "_meta": {"callId": call_id, "x-codex-turn-metadata": {
                    "thread_id": self.thread, "turn_id": self.turn, **(meta or {}),
                }},
            },
        })
        return response

    async def test_automatic_goal_continuation_runs_real_helper_without_client_metadata(self):
        first = (await self.call()).json()
        self.assertFalse(first["result"]["isError"], first)
        self.assertIn("usage:", first["result"]["content"][0]["text"])
        replay = (await self.call()).json()
        self.assertEqual(first, replay)
        self.turn = "goal-turn-3"
        self.active["provider_turn_id"] = self.turn
        third = (await self.call(call_id="third-call")).json()
        self.assertFalse(third["result"]["isError"], third)

    async def test_explicit_resume_operation_uses_its_own_authority(self):
        await server.revoke_cross_chat_capability(self.run)
        self.run = "run_goal_explicit"
        self.active.update(run_id=self.run, codex_control_reservation_id="resume-reservation")
        server.CURRENT_TURNS[self.sid] = {"run_id": self.run, "codex_control_reservation_id": "resume-reservation"}
        await server.issue_cross_chat_capability(self.sid, self.run, [], actions={"publish"})
        result = (await self.call()).json()
        self.assertFalse(result["result"]["isError"], result)

    async def test_missing_proof_does_not_authorize_an_ordinary_turn(self):
        self.active["codex_native_operation_kind"] = None
        result = (await self.call()).json()
        self.assertIn("error", result)

    async def test_artifact_route_accepts_live_goal_and_rejects_control_or_stopped_work(self):
        run_id, _ = await server.active_artifact_publication_run(self.sid)
        self.assertEqual(run_id, self.run)
        for changed in ({"codex_native_operation_kind": "compaction"},
                        {"codex_native_operation_kind": "review"},
                        {"stop_requested": True}, {"provider_turn_ready": False}):
            with self.subTest(changed=changed), patch.dict(self.active, changed):
                with self.assertRaises(server.HTTPException):
                    await server.active_artifact_publication_run(self.sid)

    async def test_stale_turn_stopped_owner_and_forged_proof_never_run_helper(self):
        executor = AsyncMock(return_value=("should never run", False))
        cases = [
            ({"turn_id": "old-turn"}, {}), ({"thread_id": "other-thread"}, {}),
            ({"parent_thread_id": "parent"}, {}), ({"agentsdock_run_id": "run_other"}, {}),
            ({"agentsdock_run_id": "run_goal", "agentsdock_run_proof": "0" * 64}, {}),
            ({}, {"stop_requested": True}), ({}, {"provider_turn_ready": False}),
            ({}, {"codex_goal_handoff_closed": True}),
        ]
        with patch.object(server, "execute_provider_tool_once", executor):
            for metadata, active_patch in cases:
                with self.subTest(metadata=metadata, active_patch=active_patch), patch.dict(self.active, active_patch):
                    self.assertIn("error", (await self.call(meta=metadata)).json())
        executor.assert_not_awaited()

    async def test_revoked_capability_and_disconnected_manager_cannot_replay(self):
        self.assertFalse((await self.call()).json()["result"]["isError"])
        self.manager.ready = False
        self.assertTrue((await self.call()).json()["result"]["isError"])
        self.manager.ready = True
        await server.revoke_cross_chat_capability(self.run)
        self.assertTrue((await self.call()).json()["result"]["isError"])

    async def test_duplicate_thread_owner_and_missing_transport_auth_are_rejected(self):
        server.STORE.sessions["neighbor"] = dict(self.session, id="neighbor")
        self.assertIn("error", (await self.call()).json())
        self.assertEqual((await self.call(secret="wrong-secret")).status_code, 403)

    async def test_resume_issues_authority_before_native_goal_can_start(self):
        await server.revoke_cross_chat_capability(self.run)
        self.session["codex_goal"]["status"] = "paused"
        self.active["codex_control_reservation_id"] = "reserved"
        server.CURRENT_TURNS[self.sid]["codex_control_reservation_id"] = "reserved"
        observed = []

        async def start_native_goal(*args, **kwargs):
            self.session["codex_goal"]["status"] = "active"
            run_id = self.active["run_id"]
            self.assertTrue(run_id.startswith("run_goal_"))
            self.assertEqual(server.CURRENT_TURNS[self.sid]["run_id"], run_id)
            self.assertEqual(len(server.CROSS_CHAT_CAPABILITIES), 1)
            observed.append((await self.call()).json())
            return self.session["codex_goal"]

        manager = SimpleNamespace(subscribe_thread=Mock(return_value=SimpleNamespace(close=Mock())),
                                  set_thread_goal=AsyncMock(side_effect=start_native_goal))
        consumer = asyncio.create_task(asyncio.sleep(0))
        with ExitStack() as stack:
            for name, value in {
                "CODEX_GOALS_ENABLED": True, "QUEUED_TURNS": {}, "RUN_NOW_TURNS": {},
                "QUEUE_START_TASKS": {}, "STEERING_SESSIONS": set(), "QUEUE_LOCK": asyncio.Lock(),
                "wait_for_queue_recovery_admission": AsyncMock(),
                "acquire_codex_control_thread": AsyncMock(return_value=(manager, self.thread, {"_codex_control_reservation_id": "reserved"})),
                "turn_start_blocker": AsyncMock(return_value=None), "append_event": AsyncMock(),
                "start_codex_goal_resume_consumer": Mock(return_value=consumer),
            }.items():
                stack.enter_context(patch.object(server, name, value))
            stack.enter_context(patch.object(server.STORE, "save", AsyncMock()))
            result = await server._put_codex_goal_locked(self.sid, server.CodexGoalRequest(status="active"))
        await consumer
        self.assertEqual(result["goal"]["status"], "active")
        self.assertFalse(observed[0]["result"]["isError"], observed)
