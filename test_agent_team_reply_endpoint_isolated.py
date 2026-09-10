"""Actual reply request/endpoint seams with fake authority and no server import."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace
from typing import Any, Literal
import unittest
from unittest import mock

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

from agentsdock_team_hub.store import HubError, HubStore
from agentsdock_team_hub.secure_peer import SecurePeerError


ROUTE = "team_" + "a" * 32
PARENT = "tmsg_parent_0001"


def extract_endpoint():
    tree = ast.parse((Path(__file__).parent / "agent_server.py").read_text())
    names = {"AgentTeamSendRequest", "send_provider_team_message"}
    constants = {"PROVIDER_TEAM_ROUTE_ID_RE", "PROVIDER_TEAM_BODY_MAX_BYTES", "PROVIDER_TEAM_ATTACHMENT_LIMIT", "PROVIDER_TEAM_SEND_LIMIT"}
    selected = []
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            selected.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in constants for target in node.targets):
            selected.append(node)
    namespace = {
        "__name__": __name__, "Any": Any, "Literal": Literal,
        "BaseModel": BaseModel, "Field": Field, "model_validator": model_validator,
        "HubError": HubError, "HubStore": HubStore, "SecurePeerError": SecurePeerError,
        "HTTPException": HTTPException, "hashlib": hashlib, "json": json, "re": re,
    }
    prefix = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix, *selected], type_ignores=[])),
        "<isolated-team-reply-endpoint>", "exec"), namespace)
    namespace["AgentTeamSendRequest"].model_rebuild(_types_namespace=namespace)
    return namespace


class TeamReplyEndpointTests(unittest.TestCase):
    def setUp(self):
        self.namespace = extract_endpoint()
        self.reference = {
            "kind": "recipient", "recipient_kind": "server", "team_id": "team_exact_001",
            "target_id": "node_sender_001", "display_name_snapshot": "Sender",
        }
        self.capability = {
            "team_authority_generation": "exact-frozen-generation",
            "actions": {"team_read", "team_send"}, "team_routes": {ROUTE: self.reference},
        }
        self.live = {
            **self.capability, "server_identity": "isolated-server", "source_session_id": "isolated-session",
            "source_run_id": "isolated-run", "native_transition_nonce": "exact-nonce", "expires_at": 2000,
        }
        self.runtime = mock.Mock()
        self.runtime.team_send_message.return_value = {
            "message": {"id": "tmsg_reply_001", "attachments": [], "recipients": []},
        }
        self.runtime.team_authorized_write.side_effect = lambda _generation, operation, *args, **kwargs: operation(*args, **kwargs)
        self.authorize = mock.AsyncMock(return_value=("token-hash", "isolated-session", self.capability))
        self.attached = mock.Mock(return_value=True)
        self.events = mock.AsyncMock()
        self.request = object()
        self.namespace.update(
            asyncio=SimpleNamespace(
                to_thread=mock.AsyncMock(side_effect=lambda operation, *args, **kwargs: operation(*args, **kwargs)),
                CancelledError=asyncio.CancelledError,
            ),
            time=SimpleNamespace(time=lambda: 1000),
            CROSS_CHAT_CAPABILITY_LOCK=asyncio.Lock(), CROSS_CHAT_CAPABILITIES={"token-hash": self.live},
            SECURE_PEER_RUNTIME=self.runtime, provider_team_capability=self.authorize,
            provider_team_attachment_paths=mock.Mock(side_effect=lambda paths: paths),
            server_identity=lambda: "isolated-server", expire_provider_route_authority=mock.Mock(),
            provider_capability_is_attached_to_live_run=self.attached,
            STORE=SimpleNamespace(sessions={"isolated-session": {"backend": "isolated-backend"}}),
            DEFAULT_BACKEND="unused-default", record_team_message_sent_event=self.events,
            logger=mock.Mock(), provider_team_error=lambda exc: HTTPException(status_code=409, detail=str(exc)),
        )

    def send(self, *, route=ROUTE, **overrides):
        request = self.namespace["AgentTeamSendRequest"](**{
            "body": "Reply body", "in_reply_to_message_id": PARENT,
            "idempotency_key": "reply-idempotency-key", **overrides,
        })
        return asyncio.run(self.namespace["send_provider_team_message"](route, request, self.request))

    def assert_no_runtime_call(self):
        self.runtime.team_authorized_write.assert_not_called()
        self.runtime.team_send_message.assert_not_called()
        self.events.assert_not_called()

    def test_parent_model_dump_uses_existing_exact_generation_authorized_write(self):
        receipt = self.send()
        self.authorize.assert_awaited_once_with(self.request, "team_send")
        self.attached.assert_called_once_with("isolated-session", "isolated-run", "exact-nonce", allow_native_transition=False)
        call = self.runtime.team_authorized_write.call_args
        self.assertEqual(call.args, ("exact-frozen-generation", self.runtime.team_send_message, self.reference))
        self.assertEqual(call.kwargs["payload"], self.namespace["AgentTeamSendRequest"](
            body="Reply body", in_reply_to_message_id=PARENT, idempotency_key="reply-idempotency-key",
        ).model_dump())
        self.assertEqual(call.kwargs["attachment_paths"], [])
        self.assertEqual(call.kwargs["provenance"], {
            "via": "agent", "chat_id": "isolated-session", "run_id": "isolated-run", "backend": "isolated-backend",
        })
        self.assertTrue(receipt["accepted"])
        self.assertFalse(receipt["duplicate"])
        self.assertEqual(receipt["route_id"], ROUTE)
        self.assertNotIn("in_reply_to_message_id", receipt)
        self.assertEqual(self.live["team_send_count"], 1)

    def test_live_read_only_action_cannot_send_a_reply(self):
        self.live["actions"] = {"team_read"}
        with self.assertRaises(HTTPException) as error:
            self.send()
        self.assertEqual(error.exception.status_code, 403)
        self.assert_no_runtime_call()
        self.assertNotIn("team_send_count", self.live)

    def test_unknown_or_malformed_route_has_no_runtime_effect(self):
        for route in ("not-a-route", "team_" + "b" * 32):
            with self.subTest(route=route), self.assertRaises(HTTPException) as error:
                self.send(route=route)
            self.assertEqual(error.exception.status_code, 404)
        self.assert_no_runtime_call()

    def test_invalid_capability_and_disappeared_live_capability_have_no_runtime_effect(self):
        self.authorize.side_effect = HTTPException(status_code=403, detail="invalid capability")
        with self.assertRaises(HTTPException) as error:
            self.send()
        self.assertEqual(error.exception.status_code, 403)
        self.authorize.side_effect = None
        self.namespace["CROSS_CHAT_CAPABILITIES"].clear()
        with self.assertRaises(HTTPException) as error:
            self.send()
        self.assertEqual(error.exception.status_code, 403)
        self.assert_no_runtime_call()

    def test_changed_binding_or_expired_turn_cannot_gain_reply_authority(self):
        for field, value in (("server_identity", "different-server"), ("source_session_id", "different-session"), ("expires_at", 1000)):
            original = self.live[field]
            with self.subTest(field=field), self.assertRaises(HTTPException) as error:
                self.live[field] = value
                self.send()
            self.assertEqual(error.exception.status_code, 403)
            self.live[field] = original
        self.attached.return_value = False
        with self.assertRaises(HTTPException) as error:
            self.send()
        self.assertEqual(error.exception.status_code, 403)
        self.assert_no_runtime_call()

    def test_same_parent_replays_changed_parent_conflicts_and_route_remains_one_use(self):
        receipt = self.send()
        self.assertEqual(self.send(), {**receipt, "duplicate": True})
        with self.assertRaises(HTTPException) as error:
            self.send(in_reply_to_message_id="tmsg_other_parent")
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("idempotency key", error.exception.detail)
        with self.assertRaises(HTTPException) as error:
            self.send(idempotency_key="different-reply-key")
        self.assertEqual(error.exception.status_code, 409)
        self.assertIn("already used once", error.exception.detail)
        self.runtime.team_authorized_write.assert_called_once()
        self.runtime.team_send_message.assert_called_once()
        self.events.assert_awaited_once()
        self.assertEqual(self.live["team_send_count"], 1)

    def test_request_model_rejects_malformed_parent_before_endpoint(self):
        for parent in ("", "short", "tmsg/parent", "tmsg_é001", "x" * 241):
            with self.subTest(parent=parent), self.assertRaises(ValidationError):
                self.send(in_reply_to_message_id=parent)
        self.authorize.assert_not_called()
        self.assert_no_runtime_call()


if __name__ == "__main__":
    unittest.main()
