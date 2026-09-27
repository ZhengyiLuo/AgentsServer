"""Real MCP route/transport with synthetic providers and no server-state import."""
import ast
import asyncio
from collections import OrderedDict
from contextlib import suppress
import hashlib
import hmac
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import httpx

from codex_app_server import CodexAppServerManager
from tests.test_codex_app_server import FakeProcessFactory, wait_until


class CodexProviderMCPContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mcp-continuation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.session_id, self.thread_id = "synthetic-chat", "synthetic-thread"
        self.run_id, self.turn_id = "run_synthetic", "native-continuation"
        self.factory = FakeProcessFactory()
        self.factory.process.responders.update({
            "thread/start": lambda _: {"thread": {"id": self.thread_id}},
            "turn/start": lambda _: {"turn": {"id": self.turn_id}},
        })
        self.manager = CodexAppServerManager("synthetic-unused", cwd=str(self.root),
            env_factory=dict, process_factory=self.factory, request_timeout=1)
        self.addAsyncCleanup(self.manager.close)
        await self.manager.start_thread({})
        self.turn = await self.manager.start_turn(self.thread_id,
            [{"type": "text", "text": "Synthetic continuation"}], overrides={})
        self.addAsyncCleanup(self.turn.close)
        self.executor = AsyncMock(return_value=("synthetic publish receipt", False))
        self.app = FastAPI()
        ready = asyncio.Event()
        ready.set()
        self.active = {self.session_id: {
            "run_id": self.run_id, "backend": "codex", "transport": "app-server",
            "provider_thread_id": self.thread_id, "provider_turn_id": self.turn_id,
            "provider_turn_ready": True, "provider_tools_ready": ready,
        }}
        self.sessions = {self.session_id: {"id": self.session_id, "backend": "codex",
            "codex_thread_id": self.thread_id}}
        self.current = {self.session_id: {"run_id": self.run_id}}
        self.capabilities = {"synthetic-capability": {
            "source_session_id": self.session_id, "source_run_id": self.run_id,
            "authority_path": str(self.root / (self.run_id + "-" + "a" * 32 + ".json")),
            "provider_runtime_env": {},
        }}
        self.ns = dict(Any=Any, Path=Path, asyncio=asyncio, re=re, hmac=hmac,
            hashlib=hashlib, json=json, suppress=suppress, app=self.app,
            Request=Request, Response=Response, JSONResponse=JSONResponse,
            HTTPException=HTTPException, ProviderToolError=ValueError,
            CODEX_PROVIDER_MCP_PATH="/synthetic-provider-mcp",
            CODEX_PROVIDER_MCP_PROOF_KEY=b"synthetic-proof-key", SERVER_INSTANCE_ID="synthetic-instance",
            BACKEND_CODEX="codex", BACKEND_CLAUDE="claude",
            CODEX_TRANSPORT_APP_SERVER="app-server", CLAUDE_TRANSPORT_AGENT_SDK="agent-sdk",
            ACTIVE=self.active, CURRENT_TURNS=self.current, STORE=SimpleNamespace(sessions=self.sessions),
            BUSY_SESSIONS={self.session_id}, DELETING_SESSIONS=set(), DELETED_SESSION_TOMBSTONES=set(),
            STOPPED_RUNS=set(), ACTIVE_LOCK=asyncio.Lock(), CROSS_CHAT_CAPABILITY_LOCK=asyncio.Lock(),
            CROSS_CHAT_CAPABILITIES=self.capabilities, CROSS_CHAT_AUTHORITY_ROOT=self.root,
            PROVIDER_TOOL_REPLAY_LOCK=asyncio.Lock(), PROVIDER_TOOL_REPLAY=OrderedDict(),
            PROVIDER_TOOL_REPLAY_TOMBSTONES=OrderedDict(), PROVIDER_TOOL_REPLAY_LIMIT=64,
            PROVIDER_TOOL_REPLAY_TOMBSTONE_LIMIT=64,
            existing_codex_app_server_manager=lambda _: self.manager,
            session_references_codex_thread=lambda session, thread: session.get("codex_thread_id") == thread,
            validate_provider_tool_input=lambda value: (value["helper"], value["arguments"], ""),
            validate_provider_runtime_env=lambda value: dict(value), execute_provider_tool=self.executor)
        names = {"codex_provider_mcp", "codex_provider_mcp_jsonrpc_error", "codex_provider_mcp_run_proof",
            "provider_tool_active_matches", "provider_tool_capability_snapshot", "provider_tool_authority_path",
            "provider_capability_is_attached_to_live_run", "execute_provider_tool_once",
            "codex_native_mailbox_owner_matches"}
        tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
        nodes = [node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.assertEqual({node.name for node in nodes}, names)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "<isolated-mcp-route>", "exec"), self.ns)

        @self.app.middleware("http")
        async def owned_test_transport(request, call_next):
            # Production transport authentication has separate security tests.
            # This fixture supplies only its exact owned synthetic connection.
            request.state.codex_provider_mcp_authenticated = request.headers.get("x-synthetic-secret") == "owned"
            return await call_next(request)

        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
            base_url="http://127.0.0.1", headers={"x-synthetic-secret": "owned"})
        self.addAsyncCleanup(self.client.aclose)

    def payload(self, *, supplied=False):
        metadata = {"thread_id": self.thread_id, "turn_id": self.turn_id}
        if supplied:
            metadata.update(agentsdock_run_id=self.run_id,
                agentsdock_run_proof=self.ns["codex_provider_mcp_run_proof"](
                    self.session_id, self.thread_id, self.run_id))
        return {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "run", "arguments": {"helper": "publish", "arguments": ["/synthetic/video.mp4"]},
            "_meta": {"callId": "native-tool-call", "x-codex-turn-metadata": metadata}}}

    async def invoke(self, payload=None):
        response = await self.client.post("/synthetic-provider-mcp", json=payload or self.payload())
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def test_native_continuation_without_optional_metadata_publishes_once(self):
        # The native transport owns this exact turn, but no client run fields
        # accompanied the continuation's turn/start request or its MCP call.
        sent = next(message for message in self.factory.process.messages if message.get("method") == "turn/start")
        self.assertNotIn("responsesapiClientMetadata", sent["params"])
        first = await self.invoke()
        self.assertEqual(first["result"]["content"][0]["text"], "synthetic publish receipt")
        self.assertFalse(first["result"]["isError"])
        self.assertEqual(await self.invoke(), first)
        for location in ("responsesapi_client_metadata", "responsesapiClientMetadata"):
            for empty in (None, {}):
                payload = self.payload()
                payload["params"]["_meta"]["x-codex-turn-metadata"][location] = empty
                self.assertEqual(await self.invoke(payload), first)
        self.executor.assert_awaited_once()
        args, kwargs = self.executor.await_args
        self.assertEqual(args[:2], (self.session_id, self.run_id))
        self.assertEqual(kwargs["provider_turn_id"], self.turn_id)
        self.assertEqual(kwargs["provider_thread_id"], self.thread_id)

    async def test_continuation_has_no_goal_status_or_internal_run_prefix_requirement(self):
        other_run = "owned_internal_run"
        self.active[self.session_id]["run_id"] = other_run
        self.current[self.session_id]["run_id"] = other_run
        self.capabilities["synthetic-capability"].update(source_run_id=other_run,
            authority_path=str(self.root / (other_run + "-" + "a" * 32 + ".json")))
        self.assertFalse((await self.invoke())["result"]["isError"])
        self.assertEqual(self.executor.await_args.args[1], other_run)

    async def test_retained_native_goal_uses_existing_authority_without_managed_turn_handle(self):
        self.factory.process.feed({"method": "turn/completed", "params": {
            "threadId": self.thread_id, "turn": {"id": self.turn_id, "status": "completed"}}})
        await wait_until(lambda: self.turn._completed)
        await self.turn.close()
        self.assertIsNone(self.manager.active_turn(self.thread_id))
        self.active[self.session_id].update(codex_native_operation=True,
            codex_native_operation_kind="goal_resume", codex_control_reservation_id=self.run_id)
        self.current[self.session_id]["codex_control_reservation_id"] = self.run_id
        self.sessions[self.session_id]["codex_goal"] = {"status": "active"}
        self.assertFalse((await self.invoke())["result"]["isError"])

    async def test_supplied_metadata_remains_validated_and_never_downgrades(self):
        cases = [({"agentsdock_run_id": self.run_id}, "Incomplete turn metadata"),
            ({"agentsdock_run_proof": "0" * 64}, "Incomplete turn metadata"),
            ({"agentsdock_run_id": "", "agentsdock_run_proof": ""}, "Incomplete turn metadata"),
            ({"agentsdock_run_id": self.run_id, "agentsdock_run_proof": "0" * 64}, "Invalid turn proof"),
            ({"agentsdock_run_id": "run_previous", "agentsdock_run_proof": "0" * 64}, "Stale turn metadata")]
        for fields, expected in cases:
            for location in (None, "responsesapi_client_metadata", "responsesapiClientMetadata"):
                with self.subTest(fields=tuple(fields), location=location):
                    payload = self.payload()
                    metadata = payload["params"]["_meta"]["x-codex-turn-metadata"]
                    if location:
                        metadata[location] = fields
                    else:
                        metadata.update(fields)
                    self.assertEqual((await self.invoke(payload))["error"]["message"], expected)
        self.executor.assert_not_awaited()
        self.assertFalse((await self.invoke(self.payload(supplied=True)))["result"]["isError"])

    async def test_malformed_metadata_containers_and_shadowed_fields_do_not_downgrade(self):
        for location in ("responsesapi_client_metadata", "responsesapiClientMetadata"):
            for value in ("invalid", [], 1, False):
                payload = self.payload()
                payload["params"]["_meta"]["x-codex-turn-metadata"][location] = value
                self.assertEqual((await self.invoke(payload))["error"]["message"], "Incomplete turn metadata")
        payload = self.payload()
        metadata = payload["params"]["_meta"]["x-codex-turn-metadata"]
        metadata.update(responsesapi_client_metadata={}, agentsdock_run_proof="invalid")
        self.assertEqual((await self.invoke(payload))["error"]["message"], "Incomplete turn metadata")
        self.executor.assert_not_awaited()

    async def test_owner_change_during_authority_lookup_cannot_publish_or_replay(self):
        lock = self.ns["CROSS_CHAT_CAPABILITY_LOCK"]
        await lock.acquire()
        pending = asyncio.create_task(self.invoke())
        try:
            await wait_until(lambda: bool(lock._waiters))
            self.active[self.session_id]["run_id"] = "run_replacement"
            self.current[self.session_id]["run_id"] = "run_replacement"
        finally:
            lock.release()
        response = await pending
        self.assertTrue(response["result"]["isError"])
        self.executor.assert_not_awaited()
        self.assertEqual(self.ns["PROVIDER_TOOL_REPLAY"], {})

    async def test_current_native_identity_and_unambiguous_run_are_required(self):
        original = dict(self.active[self.session_id])
        for changes in ({"provider_turn_id": "previous-native-turn"},
                {"provider_thread_id": "another-thread"}, {"stop_requested": True},
                {"provider_turn_ready": False}, {"run_id": "run_other"}):
            with self.subTest(changes=changes):
                self.active[self.session_id] = {**original, **changes}
                self.assertEqual((await self.invoke())["error"]["message"], "Stale turn metadata")
        self.active[self.session_id] = original
        self.active["other-chat"] = dict(original)
        self.sessions["other-chat"] = dict(self.sessions[self.session_id])
        self.current["other-chat"] = dict(self.current[self.session_id])
        self.ns["BUSY_SESSIONS"].add("other-chat")
        self.assertEqual((await self.invoke())["error"]["message"], "Stale turn metadata")
        self.executor.assert_not_awaited()

    async def test_existing_native_manager_and_authority_checks_are_not_bypassed(self):
        self.capabilities.clear()
        response = await self.invoke()
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "provider authority is not active")
        self.executor.assert_not_awaited()
        self.active[self.session_id]["provider_turn_id"] = "unowned-native-turn"
        payload = self.payload()
        payload["params"]["_meta"]["x-codex-turn-metadata"]["turn_id"] = "unowned-native-turn"
        response = await self.invoke(payload)
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "provider tool turn is stale")
        self.executor.assert_not_awaited()

    async def test_missing_native_metadata_and_subagent_markers_remain_rejected(self):
        for field in ("thread_id", "turn_id"):
            payload = self.payload()
            del payload["params"]["_meta"]["x-codex-turn-metadata"][field]
            self.assertEqual((await self.invoke(payload))["error"]["message"], "Incomplete turn metadata")
        payload = self.payload()
        del payload["params"]["_meta"]["callId"]
        self.assertEqual((await self.invoke(payload))["error"]["message"], "Incomplete turn metadata")
        payload = self.payload()
        payload["params"]["_meta"]["x-codex-turn-metadata"]["parent_thread_id"] = "another-thread"
        self.assertEqual((await self.invoke(payload))["error"]["message"], "Subagent tool calls are forbidden")
        self.executor.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
