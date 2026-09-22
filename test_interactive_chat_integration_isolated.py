"""Allowlisted production callback tests. Never import/start AgentsServer."""
from __future__ import annotations

import ast
import io
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException, UploadFile
from pydantic import BaseModel, Field, model_validator
from starlette.datastructures import Headers

from interactive_chat_projection import IncrementalChatTranscript
from public_chat_transcript import PublicTranscriptError, make_public_event_projector


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "public_chat_share_session_exists", "interactive_chat_session_available",
    "interactive_chat_reader", "interactive_chat_public_state",
    "submit_interactive_chat_prompt", "save_interactive_chat_upload",
    "session_dir", "events_path",
}


def load_glue():
    tree = ast.parse(SOURCE.read_text(), filename=str(SOURCE))
    selected = [node for node in tree.body if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS
        or isinstance(node, ast.ClassDef) and node.name == "TurnRequest"
    )]
    assert {node.name for node in selected} == FUNCTIONS | {"TurnRequest"}
    namespace = dict(re=re, io=io, Any=Any, HTTPException=HTTPException,
        UploadFile=UploadFile, Headers=Headers, BaseModel=BaseModel, Field=Field,
        model_validator=model_validator, SkillSelection=Any, ChatReference=Any, TeamReference=Any,
        IncrementalChatTranscript=IncrementalChatTranscript,
        PublicTranscriptError=PublicTranscriptError, make_public_event_projector=make_public_event_projector,
        routed_references_match_visible_prompt=lambda *args: True)
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[])), str(SOURCE), "exec"), namespace)
    namespace["TurnRequest"].model_rebuild(_types_namespace=namespace)
    return namespace, tree


class InteractiveChatGlueTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.glue, cls.tree = load_glue()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="interactive-chat-glue-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / "sessions" / "chat-one").mkdir(parents=True)
        self.share = "interactive_" + "a" * 32
        self.request_id = "browser_request_0001"
        self.glue.update({
            "STATE_DIR": root, "AGENT_TOKEN": "synthetic-native-admin",
            "STORE": SimpleNamespace(sessions={"chat-one": {"id": "chat-one"}}),
            "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
            "BUSY_SESSIONS": set(), "ACTIVE": {}, "QUEUED_TURNS": {},
            "is_client_visible_event": lambda event: True,
            "event_files_belong_to_session": lambda event, sid: event.get("session_id", sid) == sid,
            "project_provider_history_event_for_egress": lambda event, sid: event,
            "strip_agentsdock_generated_user_text": lambda text, **kwargs: text,
            "FORK_INTERNAL_PURPOSES": set(),
            "validate_session_file_ids": Mock(side_effect=lambda sid, ids: ids),
            "post_turn": AsyncMock(return_value={"queued": True, "queued_id": "queued_exact", "private": "not-for-guests"}),
            "upload_file": AsyncMock(return_value={"file": {"id": "file_synthetic", "path": "/private/owner/file"}}),
        })

    async def test_guest_submission_constructs_only_exact_chat_prompt_uploads_and_attribution(self):
        receipt = await self.glue["submit_interactive_chat_prompt"](
            "chat-one", self.share, "An ordinary guest prompt", ["file_synthetic"], self.request_id)
        self.assertEqual(receipt, {"accepted": True, "queued": True, "request_id": self.request_id, "queued_id": "queued_exact"})
        callback = self.glue["post_turn"]
        callback.assert_awaited_once()
        sid, req = callback.await_args.args
        self.assertEqual(sid, "chat-one")
        self.assertEqual(req.prompt, "An ordinary guest prompt")
        self.assertEqual(req.file_ids, ["file_synthetic"])
        self.assertEqual(req.shared_chat_metadata, {"shared_chat_id": self.share,
            "shared_chat_request_id": self.request_id, "author_label": "Collaborator"})
        for field in ("backend", "model", "effort", "purpose", "job_id", "skill_selection",
                      "display_prompt", "source_session_id", "cross_chat_envelope_id"):
            self.assertIsNone(getattr(req, field), field)
        self.assertEqual(req.chat_references, [])
        self.assertEqual(req.team_references, [])

    async def test_missing_chat_auth_disable_and_foreign_file_fail_before_send(self):
        callback = self.glue["submit_interactive_chat_prompt"]
        for sid in ("missing", "../chat-one"):
            with self.assertRaises(HTTPException):
                await callback(sid, self.share, "hello", [], self.request_id)
        self.glue["AGENT_TOKEN"] = ""
        with self.assertRaises(HTTPException):
            await callback("chat-one", self.share, "hello", [], self.request_id)
        self.glue["AGENT_TOKEN"] = "synthetic"
        self.glue["validate_session_file_ids"].side_effect = HTTPException(404, "foreign file")
        with self.assertRaises(HTTPException):
            await callback("chat-one", self.share, "hello", ["foreign"], self.request_id)
        self.glue["post_turn"].assert_not_awaited()

    async def test_callback_never_invents_acceptance_on_invalid_owner_receipt(self):
        self.glue["post_turn"].return_value = {"other": True}
        with self.assertRaises(HTTPException):
            await self.glue["submit_interactive_chat_prompt"]("chat-one", self.share, "hello", [], self.request_id)

    async def test_upload_reuses_atomic_owner_pipeline_and_returns_only_opaque_id(self):
        result = await self.glue["save_interactive_chat_upload"](
            "chat-one", self.share, "note.txt", "text/plain", b"synthetic upload")
        self.assertEqual(result, "file_synthetic")
        sid, file = self.glue["upload_file"].await_args.args
        self.assertEqual(sid, "chat-one")
        self.assertEqual(file.filename, "note.txt")
        self.assertEqual(file.content_type, "text/plain")
        self.assertTrue(file.file.closed)

    def test_ordinary_queue_projection_excludes_private_envelopes_controls_and_paths(self):
        self.glue["BUSY_SESSIONS"].add("chat-one")
        self.glue["QUEUED_TURNS"] = {"chat-one": [
            {"prompt": "visible", "_durable": True, "path": "/private", "file_ids": ["file_owner"]},
            {"prompt": "uncommitted", "_durable": False},
            {"prompt": "internal envelope", "purpose": "cross_chat_handoff_delivery", "_durable": True},
        ]}
        self.assertEqual(self.glue["interactive_chat_public_state"]("chat-one"), {
            "busy": True, "queued": [{"role": "user", "text": "visible", "pending": True}]})
        self.assertEqual(self.glue["interactive_chat_public_state"]("other"), {"busy": False, "queued": []})

    def test_reader_starts_empty_and_rejects_linked_directory(self):
        reader = self.glue["interactive_chat_reader"]("chat-one")
        self.assertEqual(reader.load()["messages"], [])
        root = self.glue["STATE_DIR"]
        (root / "elsewhere").mkdir()
        (root / "sessions" / "linked").symlink_to(root / "elsewhere", target_is_directory=True)
        self.glue["STORE"].sessions["linked"] = {"id": "linked"}
        with self.assertRaises(PublicTranscriptError):
            self.glue["interactive_chat_reader"]("linked")

    def test_guest_metadata_survives_durable_queue_and_owner_admission(self):
        source = SOURCE.read_text()
        for name in ("enqueue_turn", "_start_next_queued_turn_locked", "queued_turn_from_event", "_start_turn_locked"):
            fn = next(node for node in self.tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name)
            segment = ast.get_source_segment(source, fn)
            self.assertTrue("shared_chat_metadata" in segment or "shared_chat_request_id" in segment, name)
        self.assertIn('or getattr(req, "shared_chat_id", None) is not None', source)

    def test_native_auth_is_not_replaced_by_the_guest_cookie(self):
        source = SOURCE.read_text()
        for name in ("request_authorized", "websocket_authorized"):
            fn = next(node for node in self.tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
            self.assertNotIn("cookie", ast.get_source_segment(source, fn).lower())
        middleware = next(node for node in self.tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "require_agent_token")
        text = ast.get_source_segment(source, middleware)
        self.assertIn('interactive_chat_guest_route = request.url.path.startswith("/interactive-chat/")', text)
        self.assertIn('request.url.path.startswith("/api/admin/interactive-chat-shares/")', text)
        self.assertIn('request_route_parses_body(request) or interactive_chat_guest_route', text)


if __name__ == "__main__":
    unittest.main()
