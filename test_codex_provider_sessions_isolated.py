"""Per-chat provider boundaries using production AST and owned synthetic state."""
from __future__ import annotations
import ast
import asyncio
from contextlib import suppress
import copy
import json
import re
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
from typing import Any, Literal
import unittest
from unittest.mock import AsyncMock, Mock
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
import codex_provider
import test_codex_subagent_config_isolated as config_fixture

SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {"preview_session_runtime_update", "session_backend_locked", "public_session",
    "effective_opencode_permission_mode", "ensure_opencode_permission_mode_update_allowed",
    "session_subagent_limit_control", "validate_session_subagent_limit",
    "record_codex_subagent_limit_application",
    "create_session", "update_session", "ensure_backend_update_allowed", "codex_runtime_settings", "_fork_session_locked"}
MODELS = {"CreateSessionRequest", "UpdateSessionRequest"}
tree = ast.parse(SOURCE.read_text())
nodes = []
for node in tree.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
        node.decorator_list = []
        nodes.append(node)
    elif isinstance(node, ast.ClassDef) and node.name in MODELS:
        nodes.append(node)
    elif isinstance(node, ast.ClassDef) and node.name == "SessionStore":
        node.body = [method for method in node.body if isinstance(method, ast.AsyncFunctionDef) and method.name in {"create", "update"}]
        nodes.append(node)
assert {node.name for node in nodes} == FUNCTIONS | MODELS | {"SessionStore"}
CODE = compile(ast.fix_missing_locations(ast.Module(body=[ast.ImportFrom(module="__future__",
    names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])), str(SOURCE), "exec")


def make_namespace(root: Path):
    """Reusable native UI fixture: real routes/store methods, synthetic helpers."""
    locks = {}
    ns = {"Any": Any, "Literal": Literal, "BaseModel": BaseModel, "Field": Field,
        "cancel_generated_session_title": Mock(),
        "Path": Path, "asyncio": asyncio, "suppress": suppress, "uuid": uuid, "json": json, "re": re,
        "RUNTIME_DIAGNOSTICS": {"claude": {"version": "2.1.277"}},
        "RUNTIME_DIAGNOSTICS_LOCK": threading.RLock(),
        "SERVER_INSTANCE_ID": "owned-server-instance",
        "existing_codex_app_server_manager": lambda session: None,
        "HTTPException": HTTPException, "codex_provider": codex_provider,
        "MAX_SESSION_SYSTEM_PROMPT_CHARS": 10000, "DEFAULT_BACKEND": "codex",
        "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude", "BACKEND_CURSOR": "cursor",
        "BACKEND_OPENCODE": "opencode",
        "VALID_BACKENDS": {"codex", "claude", "cursor", "opencode"}, "DEFAULT_CWD": str(root),
        "CODEX_TRANSPORT": "app-server", "CODEX_TRANSPORT_EXEC": "exec",
        "CODEX_PROVIDER_STORE": codex_provider.ProviderStore(root / "providers"),
        "normalize_runtime_effort_for_model": lambda backend, model, effort, **kwargs: effort,
        "normalize_runtime_effort": lambda backend, effort, **kwargs: effort,
        "ensure_codex_thread_not_pending_fork_cleanup": lambda value: None,
        "ensure_dirs": lambda session: None, "now_iso": lambda: "2026-09-18T00:00:00Z",
        "clean_session_system_prompt": lambda value: value, "append_event": AsyncMock(),
        "CLAUDE_DEFAULT_PERMISSION_MODE": "default", "CLAUDE_PERMISSION_MODES": {"default"},
        "CURSOR_DEFAULT_PERMISSION_MODE": "default", "CURSOR_PERMISSION_MODES": ["default"],
        "OPENCODE_DEFAULT_PERMISSION_MODE": "default", "OPENCODE_PERMISSION_MODES": ["default", "full_access", "plan"],
        "CODEX_DEFAULT_APPROVAL_POLICY": "never", "CODEX_APPROVAL_POLICIES": {"never"},
        "CODEX_DEFAULT_SANDBOX_MODE": "read-only", "CODEX_SANDBOX_MODES": {"read-only"},
        "CODEX_DEFAULT_PERMISSION_PROFILE": None, "CODEX_DEFAULT_APPROVALS_REVIEWER": "user",
        "CODEX_APPROVAL_REVIEWERS": {"user"}, "PROVIDER_JOBS_ACCESS_DEFAULT": "full",
        "PROVIDER_JOBS_ACCESS_MODE_SET": {"full", "read_only", "blocked"},
        "session_section_key": lambda session: ("folder", session.get("folder", "General")),
        "emergency_summary": lambda session: (None, 0), "effective_provider_jobs_access": lambda session: "full",
        "CLAUDE_STOP_FENCE_SESSIONS": set(), "codex_goal_time_budget_is_exhausted": lambda session: False,
        "HISTORY_SEARCH_DIRTY": set(), "logger": Mock(), "concise_error_message": lambda value: "safe fixture error",
        "session_provider_id": lambda session: session.get("session_id"), "import_session_history": AsyncMock(),
        "ensure_session_not_initializing": lambda session: None,
        "SESSION_LIFECYCLE_UPDATE_FIELDS": {"backend", "codex_provider", "model", "effort", "archived"},
        "session_lifecycle_lock": lambda session: locks.setdefault(session, asyncio.Lock()),
        "ensure_claude_permission_mode_update_allowed": AsyncMock(),
        "ACTIVE_LOCK": asyncio.Lock(), "ACTIVE": {}, "BUSY_SESSIONS": set(), "SESSION_TURN_TASKS": {},
        "session_codex_thread_id": lambda session: session.get("codex_thread_id") or session.get("session_id") or "",
        "codex_user_config_defaults": lambda: ("normal-model", "high", "fast"),
        "CODEX_DEFAULT_MODEL": "normal-model", "CODEX_DEFAULT_EFFORT": "high",
        "clamp_codex_runtime_effort": lambda model, effort: effort,
        "codex_default_service_tier": lambda model: "fast",
        "CODEX_SETTINGS_FILE": root / "codex-settings.json", "CODEX_NONINTERACTIVE_APPROVAL_POLICY": "never",
        "CODEX_PROVIDER_MCP_NAME": "fixture", "codex_provider_mcp_config": lambda: {},
        "codex_app_server_service_tier": lambda value: value,
        "codex_thread_instructions": lambda *args: "synthetic instructions", "codex_goals_cli_args": lambda: [],
        "CODEX_BIN": "unused-codex"}
    exec(config_fixture.code, ns)
    exec(CODE, ns)
    for name in MODELS:
        ns[name].model_rebuild(_types_namespace=ns)
    for name, model in (("create_session", "CreateSessionRequest"), ("update_session", "UpdateSessionRequest")):
        ns[name].__annotations__["req"] = ns[model]
    store = ns["SessionStore"]()
    store.sessions = {}
    store._lock = asyncio.Lock()
    store.top_order_for_section = lambda *args, **kwargs: 1000
    async def save():
        (root / "sessions.json").write_text(json.dumps(store.sessions))
    store.save = AsyncMock(side_effect=save)
    async def persist_restored_state(**kwargs):
        await save()
    store.persist_restored_state = AsyncMock(side_effect=persist_restored_state)
    ns["STORE"] = store
    return ns


class PerChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="provider-sessions-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ns = make_namespace(self.root)
        self.selection = {"base_url": "https://gateway.example.invalid/v1", "model": "openai/openai/gpt-6-astra", "api_key": "synthetic-key"}
        self.ns["CODEX_PROVIDER_STORE"].save(self.selection)

    async def create(self, **kwargs):
        return (await self.ns["create_session"](self.ns["CreateSessionRequest"](**kwargs)))["session"]

    def advertise_efforts(self, model, *efforts):
        self.ns["CODEX_PROVIDER_STORE"].cache_catalog(self.selection, {
            "models": [{"value": model, "label": model}],
            "model_capabilities": {model: {"reasoning_efforts": list(efforts)}}})

    async def test_creates_persists_and_exposes_both_providers_without_replacing_normal_model(self):
        normal = await self.create(backend="codex", model="ordinary-model")
        custom = await self.create(backend="codex", codex_provider="custom")
        self.assertEqual(normal["codex_provider"], "default")
        self.assertEqual(normal["model"], "ordinary-model")
        self.assertEqual(custom["codex_provider"], "custom")
        self.assertEqual(custom["model"], self.selection["model"])
        persisted = json.loads((self.root / "sessions.json").read_text())
        self.assertEqual(persisted[custom["id"]]["codex_provider"], "custom")
        self.assertEqual(self.ns["public_session"](persisted[custom["id"]], summary=True)["codex_provider"], "custom")

    async def test_changes_empty_chat_provider_with_implicit_model_reset_and_freezes_started_chat(self):
        normal = await self.create(model="normal-model", effort="high")
        request = self.ns["UpdateSessionRequest"](codex_provider="custom")
        custom = (await self.ns["update_session"](normal["id"], request))["session"]
        self.assertEqual(custom["model"], self.selection["model"])
        self.assertIsNone(custom["effort"])
        switched = (await self.ns["update_session"](normal["id"], self.ns["UpdateSessionRequest"](codex_provider="default")))["session"]
        self.assertEqual(switched["codex_provider"], "default")
        self.assertIsNone(switched["model"])
        await self.ns["update_session"](normal["id"], request)
        self.ns["STORE"].sessions[normal["id"]]["backend_locked"] = True
        with self.assertRaises(HTTPException) as caught:
            await self.ns["update_session"](normal["id"], self.ns["UpdateSessionRequest"](codex_provider="default"))
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.ns["STORE"].sessions[normal["id"]]["codex_provider"], "custom")

    async def test_active_first_turn_reservation_fences_provider_change_before_native_id(self):
        normal = await self.create()
        self.ns["BUSY_SESSIONS"].add(normal["id"])
        with self.assertRaises(HTTPException) as caught:
            await self.ns["update_session"](normal["id"], self.ns["UpdateSessionRequest"](codex_provider="custom"))
        self.assertEqual(caught.exception.status_code, 409)

    async def test_model_provider_params_and_history_binding_are_session_specific(self):
        normal = self.ns["STORE"].sessions[(await self.create())["id"]]
        custom = self.ns["STORE"].sessions[(await self.create(codex_provider="custom"))["id"]]
        normal_params = self.ns["codex_thread_params"](normal, str(self.root))
        custom_params = self.ns["codex_thread_params"](custom, str(self.root))
        self.assertNotIn("modelProvider", normal_params)
        self.assertEqual(normal_params["model"], "normal-model")
        self.assertEqual(custom_params["modelProvider"], codex_provider.PROVIDER_ID)
        self.assertEqual(custom_params["model"], self.selection["model"])
        self.assertEqual(custom_params["config"]["model_reasoning_summary"], "none")
        self.assertNotIn("model_reasoning_summary", normal_params["config"])
        store = self.ns["CODEX_PROVIDER_STORE"]
        store.cache_model_capability(store.for_session(custom, include_key=True), {"reasoning_summary_supported": True})
        self.assertEqual(self.ns["codex_thread_params"](custom, str(self.root))["config"]["model_reasoning_summary"], "auto")
        store.record_thread("custom-thread", self.selection)
        custom["codex_thread_id"] = "custom-thread"
        self.ns["codex_runtime_settings"](custom)
        with self.assertRaises(HTTPException):
            self.ns["codex_runtime_settings"]({**custom, "codex_provider": "default"})
        normal["codex_thread_id"] = "normal-thread"
        self.ns["codex_runtime_settings"](normal)
        with self.assertRaises(HTTPException):
            self.ns["codex_runtime_settings"]({**normal, "codex_provider": "custom", "model": self.selection["model"]})

    async def test_custom_unavailable_wrong_backend_and_invalid_model_fail_without_state(self):
        for kwargs in [{"backend": "claude", "codex_provider": "custom"},
                {"codex_provider": "custom", "model": "invalid model"}]:
            with self.assertRaises(HTTPException):
                await self.create(**kwargs)
        self.ns["CODEX_PROVIDER_STORE"].reset()
        with self.assertRaises(HTTPException):
            await self.create(codex_provider="custom")
        self.assertEqual(self.ns["STORE"].sessions, {})
        self.assertEqual((await self.create())["codex_provider"], "default")

    async def test_custom_effort_is_independent_of_the_normal_account_default_model(self):
        self.ns["normalize_runtime_effort_for_model"] = Mock(side_effect=AssertionError("normal account model limits must not apply"))
        self.advertise_efforts("gateway/custom", "ultra")
        custom = await self.create(codex_provider="custom", model="gateway/custom", effort="ultra")
        self.assertEqual((custom["model"], custom["effort"]), ("gateway/custom", "ultra"))

    async def test_custom_unknown_model_clears_stale_high_while_normal_keeps_its_default(self):
        normal = self.ns["STORE"].sessions[(await self.create())["id"]]
        custom = self.ns["STORE"].sessions[(await self.create(codex_provider="custom", model="unknown/model", effort="high"))["id"]]
        self.assertIsNone(custom["effort"])
        custom["effort"] = "high"  # A saved chat from the previous universal High catalog.
        self.assertEqual(self.ns["codex_runtime_settings"](custom), ("unknown/model", "", ""))
        changed = await self.ns["update_session"](custom["id"], self.ns["UpdateSessionRequest"](model="another/unknown"))
        self.assertIsNone(changed["session"]["effort"])
        self.assertIsNone(json.loads((self.root / "sessions.json").read_text())[custom["id"]]["effort"])
        self.assertEqual(self.ns["codex_runtime_settings"](normal), ("normal-model", "high", "fast"))

    def test_registration_isolates_custom_auth_and_leaves_caller_environment_unchanged(self):
        args = codex_provider.registration_args(self.selection)
        self.assertFalse(any(value.startswith(("model=", "model_provider=")) for value in args))
        self.assertNotIn(self.selection["api_key"], str(args))
        original = {"OPENAI_API_KEY": "normal-key", "CODEX_API_KEY": "normal-codex-key"}
        env = codex_provider.registration_environment(original, self.selection)
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("CODEX_API_KEY", env)
        self.assertEqual(original["OPENAI_API_KEY"], "normal-key")
        self.assertEqual(env[codex_provider.ENV_KEY], self.selection["api_key"])

    async def test_provider_update_save_failure_restores_prior_routing(self):
        normal = await self.create(model="normal-model")
        self.ns["STORE"].save.side_effect = OSError("synthetic disk failure")
        self.ns["STORE"].persist_restored_state = AsyncMock()
        with self.assertRaises(OSError):
            await self.ns["STORE"].update(normal["id"], {"codex_provider": "custom"})
        current = self.ns["STORE"].sessions[normal["id"]]
        self.assertEqual(current["codex_provider"], "default")
        self.assertEqual(current["model"], "normal-model")
        self.ns["STORE"].persist_restored_state.assert_awaited_once_with(durable=True)

    async def test_changed_endpoint_preserves_existing_chat_revision_and_model_controls(self):
        self.advertise_efforts("new/custom-model", "ultra")
        custom = await self.create(codex_provider="custom")
        self.ns["CODEX_PROVIDER_STORE"].save({**self.selection, "base_url": "https://other.example.invalid/v1"})
        current = self.ns["STORE"].sessions[custom["id"]]
        current["backend_locked"] = True
        self.assertIsNone(current["codex_thread_id"])
        self.assertEqual(self.ns["codex_runtime_settings"](current), (self.selection["model"], "", ""))
        self.assertEqual(self.ns["CODEX_PROVIDER_STORE"].for_session(current)["base_url"], self.selection["base_url"])
        changed = (await self.ns["update_session"](custom["id"], self.ns["UpdateSessionRequest"](model="new/custom-model", effort="ultra")))["session"]
        self.assertEqual((changed["model"], changed["effort"]), ("new/custom-model", "ultra"))
        self.assertEqual(self.ns["codex_runtime_settings"](current), ("new/custom-model", "ultra", ""))
        self.assertEqual(len(self.ns["STORE"].sessions), 1)
        self.assertNotIn("codex_provider_binding", custom)
        self.assertNotIn("codex_provider_revision", custom)

    async def test_internal_fork_creation_retains_parent_provider_after_reset(self):
        self.advertise_efforts("parent-model", "ultra")
        parent = await self.create(codex_provider="custom", model="parent-model", effort="ultra")
        source = self.ns["STORE"].sessions[parent["id"]]
        original_revision = source["codex_provider_revision"]
        self.ns["CODEX_PROVIDER_STORE"].reset()
        public = self.ns["public_session"](source, summary=True)
        self.assertTrue(public["codex_provider_catalog"]["available"])
        self.assertNotIn(self.selection["api_key"], json.dumps(public))
        child = await self.ns["STORE"].create(self.ns["CreateSessionRequest"](
            codex_provider="custom", model="parent-model", effort="ultra"),
            parent_id=parent["id"], initializing_fork=True)
        self.assertEqual(child["codex_provider_revision"], original_revision)
        self.assertEqual(self.ns["codex_runtime_settings"](child), ("parent-model", "ultra", ""))

    def test_custom_native_results_do_not_change_default_diagnostic_or_fall_back_to_exec(self):
        runner = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "run_codex_app_server")
        failure = next(node for node in ast.walk(runner) if isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare) and "codex_provider.session_choice" in ast.unparse(node.test)
            and any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "record_runtime_failure" for statement in node.body for item in ast.walk(statement)))
        success = next(node for node in ast.walk(runner) if isinstance(node, ast.If)
            and isinstance(node.test, ast.BoolOp) and "codex_provider.session_choice" in ast.unparse(node.test)
            and any(isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "record_runtime_success" for statement in node.body for item in ast.walk(statement)))
        fallback = next(node for node in ast.walk(runner) if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "can_fallback" for target in node.targets))
        statements = [copy.deepcopy(failure), copy.deepcopy(success), copy.deepcopy(fallback)]
        code = compile(ast.fix_missing_locations(ast.Module(body=statements, type_ignores=[])), str(SOURCE), "exec")
        for provider in ("default", "custom"):
            ns = {"sess": {"codex_provider": provider}, "codex_provider": codex_provider, "BACKEND_CODEX": "codex",
                "terminal_error": "synthetic", "stopped": False, "record_runtime_failure": Mock(), "record_runtime_success": Mock(),
                "allow_exec_fallback": True, "provider_command": None, "stop_requested": False,
                "turn_start_attempted": False, "safe_pre_accept_failure": True}
            exec(code, ns)
            self.assertEqual(ns["record_runtime_failure"].call_count, int(provider == "default"))
            self.assertEqual(ns["record_runtime_success"].call_count, int(provider == "default"))
            self.assertEqual(ns["can_fallback"], provider == "default")
