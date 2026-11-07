"""Creation retry/configuration regressions using the real pair transaction."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from tests import test_provider_chat_pairs_isolated as pairs


FUNCTIONS = {"agent_chat_create_settings", "create_provider_chat_locked", "create_provider_chat"}
NODES = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
for original in pairs.TREE.body:
    if isinstance(original, (ast.FunctionDef, ast.AsyncFunctionDef)) and original.name in FUNCTIONS:
        node = deepcopy(original)
        node.decorator_list = []
        NODES.append(node)
CODE = compile(ast.fix_missing_locations(ast.Module(body=NODES, type_ignores=[])),
               "<agent-chat-creation>", "exec")


def request(**overrides):
    values = dict(title="Research", folder=None, cwd=None, backend=None, model=None,
                  effort=None, provider_connection=None, idempotency_key="create-once")
    values.update(overrides)
    return SimpleNamespace(**values, model_dump=lambda **_: {
        key: value for key, value in values.items() if key != "idempotency_key" and value is not None
    })


class AgentChatCreationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pairs = pairs.ChatPairTests()
        await self.pairs.asyncSetUp()
        self.ns = self.pairs.namespace
        self.store = self.pairs.store
        self.source = self.store.sessions["a"]
        self.source.update(folder="Research", cwd="/workspace", model="custom-model", effort="high",
                           codex_provider="custom", codex_provider_binding="saved-endpoint",
                           codex_provider_revision="saved-revision", subagent_limit=7,
                           codex_thread_id="native-thread", codex_goal={"status": "active"})
        self.capability = {"source_run_id": "run_one", "actions": {"agent_chats"},
                           "provider_route_grants": {}, "provider_runtime_env": {}}
        self.creations = []

        async def create(settings, *, runtime_defaults, agent_creation):
            session = {**vars(settings), "id": "created", "_agent_chat_creation": agent_creation,
                       "provider_cross_chat_routes": [], "provider_cross_chat_route_audit": []}
            self.creations.append((settings, runtime_defaults))
            self.store.sessions["created"] = session
            return session

        async def append(sid, event_type, payload):
            self.pairs.events.setdefault(sid, []).append({"type": event_type, **payload})

        self.store.create = create
        self.ns.update(json=json, hashlib=hashlib, BACKEND_CODEX="codex",
                       CreateSessionRequest=lambda **values: SimpleNamespace(**values),
                       append_durable_event=append, validate_provider_runtime_env=lambda value: value,
                       provider_cross_chat_route_projection=lambda sid, route: {"route_id": route["route_id"]},
                       authorize_provider_action=AsyncMock(return_value=self.capability),
                       provider_route_capability_source=AsyncMock(return_value="a"),
                       provider_capability_header=lambda _: "test-token",
                       CROSS_CHAT_CAPABILITY_LOCK=asyncio.Lock(),
                       CROSS_CHAT_CAPABILITIES={hashlib.sha256(b"test-token").hexdigest(): self.capability})
        exec(CODE, self.ns)

    async def create(self, req=None):
        return await self.ns["create_provider_chat"](req or request(), object())

    async def test_creation_retry_across_runs_and_live_reciprocal_route(self):
        result = await self.create()
        self.assertTrue(result["created"])
        self.assertEqual(result["route"]["mode"], "async_route_v1")
        self.assertIn(result["route"]["route_id"], self.capability["provider_route_grants"])
        self.assertTrue(self.capability["async_route_v1"])
        self.assertEqual(len(self.pairs.routes("created")), 1)
        settings, defaults = self.creations[0]
        self.assertEqual(settings.model, "custom-model")
        self.assertIs(defaults, self.source)
        self.assertFalse(hasattr(settings, "codex_thread_id"))
        self.assertFalse(hasattr(settings, "codex_goal"))
        self.capability["source_run_id"] = "run_retry"
        retry = await self.create()
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["session"]["id"], result["session"]["id"])
        self.assertEqual(len(self.creations), 1)
        with self.assertRaises(pairs.HTTPException) as caught:
            await self.create(request(title="Different"))
        self.assertEqual(caught.exception.status_code, 409)

    async def test_failed_acceptance_retains_chat_and_retry_repairs_pair(self):
        append = self.ns["append_durable_event"]
        self.ns["append_durable_event"] = AsyncMock(side_effect=OSError("disk unavailable"))
        with self.assertRaises(OSError):
            await self.create()
        self.assertIn("created", self.store.sessions)
        self.assertEqual(self.pairs.routes("a"), [])
        self.assertEqual(self.pairs.routes("created"), [])
        self.ns["append_durable_event"] = append
        retry = await self.create()
        self.assertTrue(retry["duplicate"])
        self.assertEqual(len(self.creations), 1)
        self.assertEqual(len(self.pairs.routes("created")), 1)

    async def test_retry_after_pair_revocation_does_not_regrant_access(self):
        await self.create()
        self.source["provider_cross_chat_routes"] = []
        self.store.sessions["created"]["provider_cross_chat_routes"] = []
        with self.assertRaises(pairs.HTTPException) as caught:
            await self.create()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.creations), 1)
        self.assertEqual(self.pairs.routes("a"), [])
        self.assertEqual(self.pairs.routes("created"), [])

    async def test_denied_create_cannot_mutate_store_or_routes(self):
        self.ns["authorize_provider_action"] = AsyncMock(
            side_effect=pairs.HTTPException(403, "provider action was not authorized"),
        )
        with self.assertRaises(pairs.HTTPException) as caught:
            await self.create()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.creations, [])
        self.assertNotIn("created", self.store.sessions)
        self.assertEqual(self.pairs.routes("a"), [])

    async def test_backend_switch_uses_backend_defaults(self):
        settings, runtime_defaults = self.ns["agent_chat_create_settings"](
            request(backend="claude"), self.source)
        self.assertIsNone(runtime_defaults)
        self.assertIsNone(settings.model)
        self.assertEqual(settings.provider_connection, "default")
        self.assertFalse(hasattr(settings, "subagent_limit"))
        self.assertFalse(hasattr(settings, "codex_approval_policy"))
        self.assertEqual(settings.folder, "Research")
        general, _ = self.ns["agent_chat_create_settings"](request(folder=""), self.source)
        self.assertEqual(general.folder, "")  # STORE.create maps explicit empty to General.

    async def test_cancellation_finishes_the_same_creation(self):
        started, release = asyncio.Event(), asyncio.Event()
        append = self.ns["append_durable_event"]

        async def slow_append(*args):
            started.set()
            await release.wait()
            return await append(*args)

        self.ns["append_durable_event"] = slow_append
        task = asyncio.create_task(self.create())
        await started.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue((await self.create())["duplicate"])
        self.assertEqual(len(self.creations), 1)


if __name__ == "__main__":
    unittest.main()
