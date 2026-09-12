"""Actual shared-chat adapters with isolated native seams, never server startup."""
import ast
import asyncio
from contextlib import asynccontextmanager
import json
import time
from types import MethodType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException
from fastapi.responses import Response
from interactive_chat_controls import ChatControlError, InteractiveChatControls
from interactive_chat_native import shared_events, shared_native_value, shared_session
from test_interactive_chat_controls import native_models
from test_interactive_chat_integration_isolated import load_glue


def load_native_glue():
    namespace, tree = load_glue()
    names = {"interactive_chat_native_page", "interactive_chat_native_snapshot",
             "steer_interactive_chat_prompt", "run_interactive_chat_job", "control_interactive_chat"}
    nodes = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    job_store = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "JobStore")
    manual = next(node for node in job_store.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "request_manual_run")
    namespace.update(native_models())
    namespace.update(asyncio=asyncio, time=time, json=json, Response=Response,
        shared_events=shared_events, shared_native_value=shared_native_value, shared_session=shared_session,
        ChatControlError=ChatControlError, InteractiveChatControls=InteractiveChatControls)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[*nodes, manual], type_ignores=[])), "<isolated-native-chat-adapters>", "exec"), namespace)
    return namespace


class InteractiveChatNativeGlueTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.native = load_native_glue()

    def setUp(self):
        self.share = "interactive_" + "a" * 32
        self.request = "request_0000000001"
        self.lock = asyncio.Lock()
        self.row = {"id": "event-one", "session_id": "chat-one", "seq": 1,
                    "type": "reasoning_summary", "phase": "commentary", "text": "Progress",
                    "ts": "2026-09-12T00:00:00.123Z", "run_id": "native-run"}
        self.native.update({
            "interactive_chat_session_available": lambda sid: sid == "chat-one",
            "STORE": SimpleNamespace(sessions={"chat-one": {"id": "chat-one", "title": "Shared", "backend": "codex", "cwd": "/private/owner"}}),
            "BUSY_SESSIONS": {"chat-one"}, "ACTIVE": {},
            "read_semantic_timeline_page": Mock(return_value={"events": [self.row, {"id": "file-one", "session_id": "chat-one", "type": "file_uploaded", "seq": 2}], "semantic_total": 2, "semantic_omitted_before": 1, "next_semantic_before": 1}),
            "public_session": lambda value: dict(value), "DEFAULT_BACKEND": "codex",
            "BACKEND_CODEX": "codex", "BACKEND_CLAUDE": "claude",
            "CODEX_TRANSPORT": "app-server", "CODEX_TRANSPORT_EXEC": "exec",
            "CLAUDE_TRANSPORT": "sdk", "CLAUDE_TRANSPORT_PRINT": "print",
            "CODEX_INTERACTIVE_CLIENT_CAPABILITY": "codex-test", "CLAUDE_SDK_INTERACTIVE_CLIENT_CAPABILITY": "claude-test",
            "CODEX_GOALS_ENABLED": True, "CODEX_DEFAULT_APPROVAL_POLICY": "on-request",
            "CODEX_DEFAULT_SANDBOX_MODE": "workspace-write", "CODEX_DEFAULT_PERMISSION_PROFILE": None,
            "CODEX_DEFAULT_APPROVALS_REVIEWER": "user", "CLAUDE_PERMISSION_MODE_OPTIONS": ("default",),
            "CLAUDE_STOP_FENCE_SESSIONS": set(), "effective_claude_permission_mode": lambda _: "default",
            "session_provider_id": lambda _: "synthetic-native-thread", "claude_provider_id_for_session": lambda _: None,
            "CODEX_PENDING_INTERACTIONS_LOCK": asyncio.Lock(), "CLAUDE_PENDING_INTERACTIONS_LOCK": asyncio.Lock(),
            "CODEX_PENDING_INTERACTIONS": {"own": {"id": "approval-own", "session_id": "chat-one", "params": {"headers": {"token": "private"}}}, "foreign": {"id": "approval-foreign", "session_id": "chat-two"}},
            "CLAUDE_PENDING_INTERACTIONS": {}, "public_codex_interaction": lambda row: dict(row), "public_claude_interaction": lambda row: dict(row),
            "get_codex_goal": AsyncMock(return_value={"enabled": True, "goal": None}),
            "queued_turns_snapshot": AsyncMock(return_value=[{"queued_id": "queued-own", "prompt": "Waiting", "file_ids": ["private-file"]}]),
            "strip_agentsdock_generated_user_text": lambda text, **kwargs: text,
            "list_session_jobs": AsyncMock(return_value={"jobs": []}),
            "session_lifecycle_lock": lambda sid: self.lock, "ensure_session_not_deleting": Mock(),
            "INTERACTIVE_CHAT_LIVE": SimpleNamespace(notify=Mock()),
            "INTERACTIVE_CHAT_CATALOG": None, "INTERACTIVE_CHAT_CATALOG_LOCK": asyncio.Lock(),
            "runtime_catalog": AsyncMock(return_value={"backends": {"codex": {"models": [], "cwd": "/private"}}}),
        })
        for name in ("post_turn", "post_run_queued_turn_now", "stop_turn_endpoint", "patch_queued_turn", "delete_queued_turn",
                     "post_move_queued_turn", "update_session", "put_codex_goal", "delete_codex_goal", "create_session_job",
                     "update_session_job", "delete_session_job", "post_codex_interaction_response", "post_claude_interaction_response"):
            self.native[name] = AsyncMock(return_value={"ok": True})

    async def test_indexed_page_preserves_identity_and_rejects_foreign_rows(self):
        result = await self.native["interactive_chat_native_page"]("chat-one", semantic_before=20, limit=25)
        self.assertEqual(result["events"], [self.row])
        self.assertTrue(result["semantic_paging"])
        self.native["read_semantic_timeline_page"].assert_called_once_with("chat-one", semantic_before=20, limit=25)
        self.native["read_semantic_timeline_page"].return_value = {"events": [{**self.row, "session_id": "chat-two"}]}
        with self.assertRaises(ValueError): await self.native["interactive_chat_native_page"]("chat-one")

    async def test_native_snapshot_is_one_chat_without_runtime_probe_or_private_file_fields(self):
        result = await self.native["interactive_chat_native_snapshot"]("chat-one")
        self.assertEqual(result["events"], [self.row])
        self.assertNotIn("cwd", result["session"])
        self.assertEqual(result["queue"][0]["queued_id"], "queued-own")
        self.assertEqual(result["queue"][0]["file_ids"], [])
        self.assertEqual([row["id"] for row in result["codex_runtime"]["pending_interactions"]], ["approval-own"])
        self.assertNotIn("headers", result["codex_runtime"]["pending_interactions"][0]["params"])
        self.assertFalse(result["health"]["capabilities"]["workspace_files"]["available"])
        self.native["runtime_catalog"].assert_not_awaited()
        self.native["list_session_jobs"].assert_awaited_once_with("chat-one")

    async def test_control_uses_exact_native_model_and_rejects_paths_before_mutation(self):
        self.native["patch_queued_turn"].return_value = {"ok": True, "file_ids": ["private"]}
        result = await self.native["control_interactive_chat"]("chat-one", "queue.edit", {"id": "queued-own", "prompt": "Edited", "expected_message_revision": 3})
        self.assertEqual(result, {"accepted": True, "result": {"ok": True}})
        sid, qid, request = self.native["patch_queued_turn"].await_args.args
        self.assertEqual((sid, qid), ("chat-one", "queued-own"))
        self.assertEqual(request.model_dump(exclude_unset=True), {"prompt": "Edited", "expected_message_revision": 3})
        with self.assertRaises(ChatControlError) as denied:
            await self.native["control_interactive_chat"]("chat-one", "settings.update", {"cwd": "/other"})
        self.assertEqual(denied.exception.code, "invalid_request")
        self.native["update_session"].assert_not_awaited()
        with self.assertRaises(HTTPException): await self.native["control_interactive_chat"]("chat-two", "turn.stop", {})
        self.native["stop_turn_endpoint"].assert_not_awaited()

    async def test_steer_preserves_trusted_author_and_promotes_only_receipted_queue_id(self):
        self.native["post_turn"].return_value = {"queued": True, "queued_id": "queued-exact"}
        await self.native["steer_interactive_chat_prompt"]("chat-one", "A human clarification", share_id=self.share, request_id=self.request)
        sid, request = self.native["post_turn"].await_args.args
        self.assertEqual(sid, "chat-one")
        self.assertEqual(request.shared_chat_metadata, {"shared_chat_id": self.share, "shared_chat_request_id": self.request, "author_label": "Collaborator"})
        self.assertEqual(request.chat_references, [])
        sid, qid, request = self.native["post_run_queued_turn_now"].await_args.args
        self.assertEqual((sid, qid), ("chat-one", "queued-exact"))
        self.assertTrue(request.accept_deferred_queue_response)
        self.native["stop_turn_endpoint"].assert_not_awaited()
        for receipt in ({"queued": True}, {}, {"queued": "false"}):
            self.native["post_turn"].return_value = receipt
            self.native["post_run_queued_turn_now"].reset_mock()
            with self.subTest(receipt=receipt), self.assertRaises(HTTPException):
                await self.native["steer_interactive_chat_prompt"]("chat-one", "Text", share_id=self.share, request_id=self.request)
            self.native["post_run_queued_turn_now"].assert_not_awaited()

    async def test_job_run_releases_lifecycle_lock_before_normal_turn_admission(self):
        async def dispatch(_jid):
            async with self.lock:  # Actual _start_job_run calls start_turn, which takes this lock.
                return {"queued": False, "run_id": "synthetic-run"}
        jobs = SimpleNamespace(jobs={"job-own": {"id": "job-own", "session_id": "chat-one"}}, _lock=asyncio.Lock(),
            save=AsyncMock(), _dispatch_pending_manual_run=dispatch, pause_for_session=AsyncMock())
        jobs.request_manual_run = MethodType(self.native["request_manual_run"], jobs)
        self.native.update(JOBS=jobs, now_iso=lambda: "2026-09-12T00:00:00Z", new_job_revision=lambda: "revision",
                           event_job=lambda row: dict(row), append_event=AsyncMock())
        result = await asyncio.wait_for(self.native["run_interactive_chat_job"]("chat-one", "job-own"), 1)
        self.assertEqual(result["run_id"], "synthetic-run")
        jobs.save.assert_awaited_once()
        with self.assertRaises(HTTPException):
            await jobs.request_manual_run("job-own", expected_session_id="chat-two")
        self.assertEqual(jobs.save.await_count, 1)
        @asynccontextmanager
        async def changed_owner():
            jobs.jobs["job-own"] = {"id": "job-own", "session_id": "chat-two"}
            yield
        jobs._lock = changed_owner()
        with self.assertRaises(HTTPException):
            await jobs.request_manual_run("job-own", expected_session_id="chat-one")
        self.assertEqual(jobs.save.await_count, 1)

    async def test_explicit_catalog_read_is_cached_and_browser_cannot_select_other_chat(self):
        for _ in range(2):
            result = await self.native["control_interactive_chat"]("chat-one", "runtime.catalog", {})
            self.assertEqual(result, {"backends": {"codex": {"models": []}}})
        self.native["runtime_catalog"].assert_awaited_once_with()
        with self.assertRaises(ChatControlError):
            await self.native["control_interactive_chat"]("chat-one", "timeline.older", {"session_id": "chat-two"})
        self.native["read_semantic_timeline_page"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
