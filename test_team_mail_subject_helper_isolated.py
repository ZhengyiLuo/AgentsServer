"""Mocked helper and AST-only server seams: no live server import or I/O."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any, Literal
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

import agentsdock_team as helper
from agentsdock_team_hub.store import HubError, HubStore
from agentsdock_team_hub.secure_peer import SecurePeerError


ROOT = Path(__file__).parent


def extracted():
    tree = ast.parse((ROOT / "agent_server.py").read_text())
    names = {"AgentTeamSendRequest", "list_provider_team_messages", "get_provider_team_message"}
    selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef)) and node.name in names]
    for node in selected:
        node.decorator_list = []
    namespace = {
        "__name__": __name__, "Any": Any, "Literal": Literal,
        "BaseModel": BaseModel, "Field": Field, "model_validator": model_validator,
        "HubError": HubError, "HubStore": HubStore, "SecurePeerError": SecurePeerError,
        "PROVIDER_TEAM_BODY_MAX_BYTES": 49152, "PROVIDER_TEAM_ATTACHMENT_LIMIT": 16,
        "PROVIDER_TEAM_LIST_LIMIT": 50, "TEAM_CONTENT_NOTICE": "test notice",
        "asyncio": asyncio, "HTTPException": HTTPException,
    }
    prefix = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix, *selected], type_ignores=[])),
        "agent_server.py:isolated-subject-seams", "exec"), namespace)
    namespace["AgentTeamSendRequest"].model_rebuild(_types_namespace=namespace)
    return namespace


class MailSubjectHelperTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(helper, "_provider_authority", return_value=("fake-capability", "fake-session")))

    def test_cli_titled_message_normalizes_without_changing_body_and_stable_key(self):
        receipt = {"ok": True, "route_id": "route-1", "message_id": "message-1", "kind": "message",
            "accepted": True, "duplicate": False, "attachments": 0}
        with mock.patch.object(helper, "_read_body", return_value="Original body"), mock.patch.object(helper, "_request_json", return_value=receipt) as request:
            for title in ("  Subject  ", "Subject", "Different"):
                args = helper.parser().parse_args(["send", "--route", "route-1", "--title", title])
                self.assertEqual(helper.send(args), receipt)
            payloads = [call.args[3] for call in request.call_args_list]
        self.assertEqual(payloads[0]["title"], "Subject")
        self.assertEqual(payloads[0]["body"], "Original body")
        self.assertEqual(payloads[0]["idempotency_key"], payloads[1]["idempotency_key"])
        self.assertNotEqual(payloads[1]["idempotency_key"], payloads[2]["idempotency_key"])

    def test_cli_invalid_subject_is_rejected_before_body_or_request(self):
        for title in ("", "  ", "x" * 161, "a\tb", "a\nb", "a\u2028b", "\ud800"):
            with self.subTest(title=repr(title)), mock.patch.object(helper, "_read_body") as body, mock.patch.object(helper, "_request_json") as request:
                args = helper.parser().parse_args(["send", "--route", "route-1", "--title", title])
                with self.assertRaises(helper.TeamCLIError):
                    helper.send(args)
                body.assert_not_called()
                request.assert_not_called()

    def test_cli_read_opt_in_is_explicit_and_adds_no_request(self):
        for command in (["inbox"], ["feed"], ["sent"], ["read", "message-1"]):
            for include in (False, True):
                args = helper.parser().parse_args([*command, *(["--include-mail-subject"] if include else [])])
                with mock.patch.object(helper, "_request_json", return_value={"messages": [], "message": {}}) as request:
                    args.handler(args)
                request.assert_called_once()
                query = parse_qs(urlsplit(request.call_args.args[1]).query)
                self.assertEqual(query.get("include_mail_subject"), ["1"] if include else None)

    def test_actual_request_model_trims_only_message_subjects(self):
        model = extracted()["AgentTeamSendRequest"]
        basic = {"body": "Original", "idempotency_key": "subject-test-key"}
        self.assertEqual(model(**basic, title=" " + "📨" * 160 + " ").title, "📨" * 160)
        self.assertEqual(model(**basic, title="a  b").title, "a  b")
        for title in ("a\nb", "a\tb", "\ud800", "x" * 161):
            with self.subTest(title=repr(title)), self.assertRaises(ValidationError):
                model(**basic, title=title)
        self.assertEqual(model(**basic, kind="skill", title="a\nb").title, "a\nb")

    def test_actual_read_endpoints_preserve_authority_and_default_opt_out(self):
        namespace = extracted()
        runtime = mock.Mock()
        runtime.team_authorized_read.return_value = {"messages": []}
        authorize = mock.AsyncMock(return_value=("token", "session", {"team_authority_generation": "exact-generation"}))
        namespace.update(SECURE_PEER_RUNTIME=runtime, provider_team_capability=authorize)
        for name in ("list_provider_team_messages", "get_provider_team_message"):
            for include in (False, True):
                runtime.reset_mock()
                kwargs = {"request": object(), "team": "exact-team"}
                if name == "get_provider_team_message":
                    kwargs["message_id"] = "exact-message"
                if include:
                    kwargs["include_mail_subject"] = True
                asyncio.run(namespace[name](**kwargs))
                call = runtime.team_authorized_read.call_args
                self.assertEqual(call.args[0], "exact-generation")
                self.assertEqual(call.kwargs["team_id"], "exact-team")
                self.assertIs(call.kwargs["include_mail_subject"], include)
                self.assertEqual(authorize.call_args.args[1], "team_read")
