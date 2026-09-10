"""Source-allowlisted server glue tests; never import the server runtime."""
from __future__ import annotations

import ast
import hmac
import json
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from public_chat_share_routes import create_public_chat_share_router, redact_public_share_path
from public_chat_transcript import PublicTranscriptError, make_public_event_projector, read_public_transcript


SOURCE = Path(__file__).with_name("agent_server.py")
FUNCTIONS = {
    "token_matches", "decoded_exact_header_secret", "request_exact_native_token_header_authorized",
    "privileged_native_browser_request_forbidden", "require_native_admin_control",
    "public_chat_share_session_exists", "load_public_chat_share_transcript",
    "session_dir", "events_path", "redact_access_log_token",
}


def load_glue():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    selected = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS]
    if {node.name for node in selected} != FUNCTIONS:
        raise AssertionError("Missing isolated public share integration helper")
    selected.extend(node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "ACCESS_LOG_TOKEN_QUERY_RE" for target in node.targets))
    namespace = {
        "re": re, "hmac": hmac, "HTTPException": HTTPException,
        "PublicTranscriptError": PublicTranscriptError,
        "make_public_event_projector": make_public_event_projector,
        "read_public_transcript": read_public_transcript,
        "redact_public_share_path": redact_public_share_path,
    }
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, *selected], type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace, tree


class PublicChatShareIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.glue, cls.tree = load_glue()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="public-share-glue-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        selected = self.root / "sessions" / "chat-one"
        selected.mkdir(parents=True)
        self.events = selected / "events.jsonl"
        self.events.write_text(json.dumps({"type": "turn_started", "session_id": "chat-one", "prompt": "Reviewed text"}) + "\n")
        self.project = Mock(side_effect=lambda event, session: event)
        self.strip = Mock(side_effect=lambda text, **kwargs: text)
        self.glue.update({
            "AGENT_TOKEN": "native-share-admin-only", "STATE_DIR": self.root,
            "STORE": SimpleNamespace(sessions={"chat-one": {"id": "chat-one"}}),
            "DELETING_SESSIONS": set(), "DELETED_SESSION_TOMBSTONES": set(),
            "is_client_visible_event": lambda event: True,
            "event_files_belong_to_session": lambda event, session: True,
            "project_provider_history_event_for_egress": self.project,
            "strip_agentsdock_generated_user_text": self.strip,
            "FORK_INTERNAL_PURPOSES": {"handoff_digest", "handoff_digest_delivery"},
        })

    def test_actual_native_auth_rejects_browser_url_bearer_and_ambiguous_headers(self):
        valid = (b"x-agentsdock-token", b"native-share-admin-only")
        for headers, query, expected in (
            ([valid], b"", None), ([(b"x-zenithdock-token", valid[1])], b"", None),
            ([], b"", 401), ([(b"authorization", b"Bearer " + valid[1])], b"", 401),
            ([valid], b"token=other", 401), ([valid, valid], b"", 401),
            ([valid, (b"origin", b"https://browser.invalid")], b"", 403),
            ([valid, (b"cookie", b"ambient=yes")], b"", 403),
            ([valid, (b"sec-fetch-mode", b"cors")], b"", 403),
            ([valid, (b"authorization", b"Bearer other")], b"", 401),
        ):
            with self.subTest(headers=headers, query=query):
                request = Request({"type": "http", "headers": headers, "query_string": query})
                if expected is None:
                    self.glue["require_native_admin_control"](request)
                else:
                    with self.assertRaises(HTTPException) as failure:
                        self.glue["require_native_admin_control"](request)
                    self.assertEqual(failure.exception.status_code, expected)
        self.glue["AGENT_TOKEN"] = ""
        with self.assertRaises(HTTPException) as failure:
            self.glue["require_native_admin_control"](Request({"type": "http", "headers": [], "query_string": b""}))
        self.assertEqual(failure.exception.status_code, 503)

    def test_session_registry_and_path_checks_fail_closed(self):
        exists = self.glue["public_chat_share_session_exists"]
        self.assertTrue(exists("chat-one"))
        for session in ("../chat-one", "missing", "x" * 129, None):
            self.assertFalse(exists(session))
        self.glue["DELETING_SESSIONS"].add("chat-one")
        self.assertFalse(exists("chat-one"))
        self.glue["DELETING_SESSIONS"].clear()
        self.glue["STORE"].sessions["chat-one"]["id"] = "different-id"
        self.assertFalse(exists("chat-one"))

    def test_durable_adapter_calls_existing_projection_without_discovery(self):
        result = self.glue["load_public_chat_share_transcript"]("chat-one", None)
        self.assertEqual(result["messages"], [{"role": "user", "text": "Reviewed text"}])
        self.project.assert_called_once()
        self.strip.assert_called_once_with("Reviewed text", expected_session_id="chat-one", provider_history=False)
        with self.assertRaises(PublicTranscriptError):
            self.glue["load_public_chat_share_transcript"]("../other", None)

    def test_linked_session_directory_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        (other / "events.jsonl").write_text('{"type":"turn_started","prompt":"private other"}\n')
        linked = self.root / "sessions" / "linked"
        linked.symlink_to(other, target_is_directory=True)
        self.glue["STORE"].sessions["linked"] = {"id": "linked"}
        with self.assertRaises(PublicTranscriptError):
            self.glue["load_public_chat_share_transcript"]("linked", None)

    def test_real_glue_publishes_only_after_native_authenticated_preview(self):
        app = FastAPI()
        app.include_router(create_public_chat_share_router(
            storage_root=self.root / "shares",
            authorize=self.glue["require_native_admin_control"],
            session_exists=self.glue["public_chat_share_session_exists"],
            load_transcript=self.glue["load_public_chat_share_transcript"],
            public_base_url=lambda: "",
        ))
        with TestClient(app) as client:
            base = "/api/admin/chat-shares/chat-one"
            headers = {"X-AgentsDock-Token": "native-share-admin-only"}
            self.assertEqual(client.post(base + "/preview", json={}).status_code, 401)
            self.assertFalse((self.root / "shares").exists())
            preview = client.post(base + "/preview", json={}, headers=headers).json()
            created = client.post(base, json={"confirmed_public": True, "through_bytes": preview["through_bytes"], "digest": preview["digest"]}, headers=headers)
            self.assertEqual(created.status_code, 201, created.text)
            self.assertIsNone(created.json()["url"])
            view = client.get(created.json()["path"])
            self.assertEqual(view.status_code, 200)
            self.assertIn("Reviewed text", view.text)
            self.assertNotIn("chat-one", view.text)
            self.assertEqual(client.delete(base + "/" + created.json()["share_id"], headers=headers).status_code, 200)
            self.assertEqual(client.get(created.json()["path"]).status_code, 404)

    def test_server_log_redactor_removes_public_bearer_and_query_credentials(self):
        token = "A" * 43
        output = self.glue["redact_access_log_token"]('GET /share/' + token + '?token=private-value HTTP/1.1')
        self.assertNotIn(token, output)
        self.assertNotIn("private-value", output)
        self.assertIn("/share/<redacted>", output)
        self.assertEqual(self.glue["redact_access_log_token"]("/api/status"), "/api/status")
        encoded = "%41" * 43
        self.assertEqual(self.glue["redact_access_log_token"]("GET /share/" + encoded + " HTTP/1.1"),
                         "GET /share/<redacted> HTTP/1.1")

    def test_router_registration_and_strict_middleware_class_are_present(self):
        middleware = next(node for node in self.tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "require_agent_token")
        self.assertGreaterEqual(sum(isinstance(node, ast.Name) and node.id == "public_chat_shares_admin_route" for node in ast.walk(middleware)), 3)
        registrations = [node for node in self.tree.body if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                         and isinstance(node.value.func, ast.Attribute) and node.value.func.attr == "include_router"]
        self.assertTrue(any("create_public_chat_share_router" in ast.unparse(node) for node in registrations))


if __name__ == "__main__":
    unittest.main()
