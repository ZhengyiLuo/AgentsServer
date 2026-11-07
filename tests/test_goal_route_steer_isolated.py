"""Goal route admission/authority tests with real reference and live-route fences."""
from __future__ import annotations

import ast
import asyncio
import copy
import hashlib
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Literal
import unittest
from unittest.mock import AsyncMock

from pydantic import BaseModel, Field, field_validator, model_validator
from codex_app_server import CodexAppServerError
from tests.test_goal_followup_admission_isolated import AdmissionHTTPException


SOURCE = Path(__file__).resolve().parents[1] / "agent_server.py"
NAMES = {
    "prepare_codex_goal_route_steer", "commit_codex_goal_route_steer",
    "reserve_async_provider_route_message", "NativeSteerHandoffError", "ChatReference",
    "validate_chat_references", "utf16_length", "utf16_slice", "chat_reference_marker",
    "chat_reference_marker_candidates", "chat_reference_has_token_boundaries",
    "canonical_chat_reference", "collapse_legacy_dual_local_references",
    "provider_cross_chat_route_snapshot_for_authority", "live_provider_cross_chat_route",
    "provider_cross_chat_route_id_is_revoked", "provider_cross_chat_pair_is_live",
    "validated_cross_chat_source_user_instruction",
}
tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
nodes = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
         and node.name in NAMES]
assert {node.name for node in nodes} == NAMES
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(
    module="__future__", names=[ast.alias(name="annotations")], level=0,
), *nodes], type_ignores=[])), str(SOURCE), "exec")
del tree, nodes


class GoalRouteSteerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.routes = [{
            "route_id": f"route_{index:032x}", "revision": f"rev_{index:032x}",
            "target_session_id": target, "alias": target, "actions": ["instruction", "request_reply"],
            "pair_id": f"pair_{index:032x}", "paired_route_id": f"route_{index + 10:032x}",
        } for index, target in enumerate(("Original", "Selected", "Unmentioned"), 1)]
        self.sessions = {"chat": {"backend": "codex", "provider_cross_chat_routes": copy.deepcopy(self.routes)}}
        for route in self.routes:
            self.sessions[route["target_session_id"]] = {"backend": "claude", "provider_cross_chat_routes": [{
                **route, "route_id": route["paired_route_id"], "paired_route_id": route["route_id"],
                "target_session_id": "chat",
            }]}
        self.active = {"run_id": "run_original", "codex_native_operation_kind": "goal_resume",
                       "codex_control_reservation_id": "reservation"}
        self.current = dict(self.active)
        self.capability = {
            "server_identity": "server", "source_session_id": "chat", "source_run_id": "run_original",
            "source_user_instruction": "Original user instruction", "native_transition_nonce": "",
            "async_route_v1": True, "authority_path": "unchanged-authority",
            "expires_at": time.time() + 3600,
            "provider_route_grants": {self.routes[0]["route_id"]: copy.deepcopy(self.routes[0])},
            "user_delegation_grants": {("Original", "route")},
            "actions": {"publish", "jobs", "agent_cross_chat_routes"},
            "provider_runtime_env": {"AGENTSDOCK_PROVIDER_AUTHORITY_FILE": "unchanged-authority",
                                     "AGENTSDOCK_CROSS_CHAT_HANDLE_1": "original-handle"},
            "provider_route_consumed": {self.routes[0]["route_id"]: {"body": "Already sent"}},
            "provider_route_handoff_count": 1, "consumed": {"original-effect": True},
        }
        self.ns = {
            "asyncio": asyncio, "copy": copy, "re": re, "hashlib": hashlib, "time": time,
            "BaseModel": BaseModel, "Field": Field, "field_validator": field_validator,
            "model_validator": model_validator, "Literal": Literal, "CodexAppServerError": CodexAppServerError,
            "HTTPException": AdmissionHTTPException, "AGENT_TOKEN": "synthetic-test-token",
            "CROSS_CHAT_CAPABILITIES": {"original-token-hash": self.capability},
            "CROSS_CHAT_CAPABILITY_LOCK": asyncio.Lock(), "STORE": SimpleNamespace(sessions=self.sessions),
            "ACTIVE": {"chat": self.active}, "CURRENT_TURNS": {"chat": self.current},
            "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
            "AGENT_CROSS_CHAT_ROUTES_CLIENT_CAPABILITY": "agent_cross_chat_routes_v2",
            "CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY": "cross_chat_handoffs_v2",
            "CROSS_CHAT_SOURCE_USER_INSTRUCTION_MAX_CHARS": 100_000,
            "PROVIDER_CROSS_CHAT_ROUTE_KIND_AMBIENT": "ambient", "PROVIDER_CROSS_CHAT_ROUTE_KIND_REFERENCE": "reference",
            "PROVIDER_CROSS_CHAT_ROUTE_ACTIONS": ("instruction", "request_reply"),
            "PROVIDER_CROSS_CHAT_ROUTE_ID_RE": re.compile(r"route_[0-9a-f]{32}"),
            "provider_cross_chat_routes": lambda session: (session or {}).get("provider_cross_chat_routes", []),
            "normalized_provider_cross_chat_route_snapshot": lambda value: copy.deepcopy(value or []),
            "live_provider_chat_mailbox_route": lambda *_args: None,
            "cross_chat_delivery_client_capabilities": lambda *_args: [],
            "cross_chat_target_backend_supported": lambda _backend: True,
            "provider_cross_chat_route_availability": lambda source, target: (target in self.sessions and not self.sessions[target].get("archived"), ""),
            "validate_provider_runtime_env": lambda value: dict(value),
            "server_identity": lambda: "server",
            "provider_capability_is_attached_to_live_run": lambda session, run, *_args, **_kwargs: (
                self.ns["ACTIVE"].get(session, {}).get("run_id") == run
                and self.ns["CURRENT_TURNS"].get(session, {}).get("run_id") == run
                and not self.ns["ACTIVE"].get(session, {}).get("stop_requested")),
            "authorize_provider_action": AsyncMock(return_value=self.capability),
            "CROSS_CHAT": SimpleNamespace(get=AsyncMock(return_value=None)),
        }
        exec(CODE, self.ns)
        self.ns["ChatReference"].model_rebuild(_types_namespace=self.ns)

    def selection(self, instruction="Ask @Selected to review"):
        return {"prompt": instruction, "client_capabilities": ["agent_cross_chat_routes_v2"],
                "chat_references": [{"session_id": "Selected", "display_title_snapshot": "Selected",
                                     "source_text_start": 4, "source_text_end": 13, "action": "route", "grant_intent": True}],
                "provider_cross_chat_route_snapshot": copy.deepcopy(self.routes)}

    async def prepare(self, selected=None):
        return await self.ns["prepare_codex_goal_route_steer"]("chat", self.active["run_id"], selected or self.selection())

    async def commit(self, prepared):
        return await self.ns["commit_codex_goal_route_steer"]("chat", self.active["run_id"], prepared)

    async def reserve(self, route, key):
        return await self.ns["reserve_async_provider_route_message"](
            None, source_session_id="chat", route_id=route["route_id"], body="Review", idempotency_key=key,
        )

    async def test_prepare_changes_no_authority_and_commit_adds_only_selected_route(self):
        before = copy.deepcopy(self.capability)
        active, current = copy.deepcopy(self.active), copy.deepcopy(self.current)
        prepared = await self.prepare()
        self.assertEqual(self.capability, before)
        self.assertEqual(len(prepared["routes"]), 1)
        self.assertTrue(await self.commit(prepared))
        self.assertIs(self.ns["CROSS_CHAT_CAPABILITIES"]["original-token-hash"], self.capability)
        self.assertEqual(len(self.ns["CROSS_CHAT_CAPABILITIES"]), 1)
        self.assertEqual(self.active, active)
        self.assertEqual(self.current, current)
        self.assertEqual(set(self.capability["provider_route_grants"]), {self.routes[0]["route_id"], self.routes[1]["route_id"]})
        for field in ("source_user_instruction", "user_delegation_grants", "provider_route_consumed",
                      "provider_route_handoff_count", "consumed", "authority_path"):
            self.assertEqual(self.capability[field], before[field], field)
        self.assertEqual(self.capability["provider_runtime_env"]["AGENTSDOCK_CROSS_CHAT_HANDLE_1"], "original-handle")

    async def test_new_route_uses_selected_instruction_and_original_keeps_original_provenance(self):
        self.assertTrue(await self.commit(await self.prepare()))
        old = await self.reserve(self.routes[0], "old")
        new = await self.reserve(self.routes[1], "new")
        self.assertEqual(old["source_user_instruction"], "Original user instruction")
        self.assertEqual(new["source_user_instruction"], "Ask @Selected to review")
        self.assertEqual(new["source_user_delegation_action"], "route")
        self.ns["CROSS_CHAT"].get.return_value = {
            **new, "id": new["envelope_id"], "kind": "instruction", "delivery_mode": "mailbox",
            "authorization_kind": "configured_route", "authorization_route_id": new["route_id"],
        }
        self.assertTrue(await self.commit(await self.prepare(self.selection("Ask @Selected to retest"))))
        retry = await self.reserve(self.routes[1], "new")
        self.assertEqual(retry["source_user_instruction"], "Ask @Selected to review")
        self.ns["CROSS_CHAT"].get.return_value = None
        self.assertEqual((await self.reserve(self.routes[1], "later"))["source_user_instruction"], "Ask @Selected to retest")
        self.assertEqual(new["source_user_instruction"], "Ask @Selected to review")

    async def test_revoked_or_changed_selected_route_rejects_before_delivery(self):
        for mutation in ("revision", "revoke", "peer"):
            with self.subTest(mutation=mutation):
                selected = self.selection()
                if mutation == "revision": selected["provider_cross_chat_route_snapshot"][1]["revision"] = "stale"
                if mutation == "revoke": self.sessions["chat"]["_revoked_provider_cross_chat_route_ids"] = [self.routes[1]["route_id"]]
                if mutation == "peer": self.sessions["Selected"]["provider_cross_chat_routes"] = []
                with self.assertRaises(self.ns["NativeSteerHandoffError"]) as caught:
                    await self.prepare(selected)
                self.assertTrue(caught.exception.safe_to_requeue)

    async def test_revocation_after_ack_never_resurrects_access(self):
        prepared = await self.prepare()
        before = copy.deepcopy(self.capability)
        self.sessions["chat"]["_revoked_provider_cross_chat_route_ids"] = [self.routes[1]["route_id"]]
        self.assertFalse(await self.commit(prepared))
        self.assertEqual(self.capability, before)

    async def test_expired_authority_is_neither_prepared_nor_extended(self):
        prepared = await self.prepare()
        self.capability["expires_at"] = 0
        self.assertFalse(await self.commit(prepared))
        with self.assertRaises(self.ns["NativeSteerHandoffError"]):
            await self.prepare()

    async def test_replaced_capability_or_stopped_owner_cannot_receive_prepared_grants(self):
        for change in ("replace", "stop"):
            with self.subTest(change=change):
                prepared = await self.prepare()
                if change == "replace": self.ns["CROSS_CHAT_CAPABILITIES"]["original-token-hash"] = copy.deepcopy(self.capability)
                else: self.active["stop_requested"] = True
                self.assertFalse(await self.commit(prepared))
                self.assertNotIn(self.routes[1]["route_id"], self.capability["provider_route_grants"])
                self.ns["CROSS_CHAT_CAPABILITIES"]["original-token-hash"] = self.capability

    async def test_visible_reference_mutation_and_legacy_intent_do_not_grant_access(self):
        for kind in ("prompt", "intent", "action"):
            selected = self.selection()
            if kind == "prompt": selected["prompt"] = "Ask @Different to review"
            if kind == "intent": selected["chat_references"][0].pop("grant_intent")
            if kind == "action": selected["chat_references"][0]["action"] = "instruction"
            with self.subTest(kind=kind), self.assertRaises(self.ns["NativeSteerHandoffError"]):
                await self.prepare(selected)

    async def test_plain_input_needs_no_capability_and_resumed_owner_can_plan_lazy_issuance(self):
        self.ns["CROSS_CHAT_CAPABILITIES"].clear()
        self.assertIsNone(await self.prepare({"prompt": "ordinary follow-up"}))
        self.active["run_id"] = self.current["run_id"] = "run_codexgoal_1234567890abcdef"
        prepared = await self.prepare()
        self.assertIsNone(prepared["capability"])
        self.assertIsNone(prepared["token_hash"])
        self.assertEqual(self.ns["CROSS_CHAT_CAPABILITIES"], {})
        self.active["codex_native_operation_kind"] = None
        with self.assertRaises(self.ns["NativeSteerHandoffError"]):
            await self.prepare()


if __name__ == "__main__":
    unittest.main()
