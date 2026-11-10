"""Exact scheduled external grants, without importing the app or live state."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from typing import Any, Literal
import unittest

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

import external_chat_peers as peers
import test_provider_chat_pairs_isolated as pairs


class HTTPException(Exception):
    def __init__(self, status_code, detail, headers=None):
        self.status_code, self.detail, self.headers = status_code, detail, headers


NAMES = {
    "ChatReference", "AgentJobChatRouteSelection", "ScheduledJobChatReferenceRepairRequired",
    "canonical_secure_peer_uuid", "chat_reference_dict", "chat_reference_dicts",
    "utf16_length", "utf16_slice", "chat_reference_marker", "chat_reference_marker_candidates",
    "canonical_chat_reference", "collapse_legacy_dual_local_references",
    "chat_reference_has_token_boundaries", "validate_chat_references",
    "provider_safe_scheduled_chat_reference_detail", "validate_scheduled_job_chat_references",
    "provider_job_chat_references", "live_scheduled_external_peer_route",
}
NODES = [deepcopy(node) for node in pairs.TREE.body
         if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in NAMES]
CODE = compile(ast.fix_missing_locations(ast.Module(body=NODES, type_ignores=[])),
               "<isolated-external-job-routes>", "exec", dont_inherit=True)


class ExternalJobRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = pairs.ChatPairTests()
        await fixture.asyncSetUp()
        self.ns = fixture.namespace
        self.ns["STORE"].sessions = {"sess_" + key: {**value, "id": "sess_" + key}
                                      for key, value in self.ns["STORE"].sessions.items()}
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = Path(self.temporary.name) / "external-chat-peers.json"
        self.peer = {
            "peer_id": "extpeer_" + "1" * 32, "display_name": "Synthetic Duet 📱",
            "target_session_id": "sess_a", "credential_sha256": "9" * 64,
            "revision": "rev_" + "2" * 32, "pair_id": "pair_" + "3" * 32,
            "route_id": "route_" + "4" * 32, "inbound_route_id": "route_" + "5" * 32,
            "enabled": True,
            "policy": {"approval_id": "synthetic-owner-approval",
                       "approved_at": "2026-11-06T00:00:00Z", "delegated_by": "Synthetic owner",
                       "source_user_instruction": "Send synthetic scheduled updates to this test mailbox."},
        }
        self.save()
        self.ns.update(
            Any=Any, Literal=Literal, BaseModel=BaseModel, Field=Field,
            field_validator=field_validator, model_validator=model_validator,
            HTTPException=HTTPException, external_chat_peers=peers,
            EXTERNAL_CHAT_PEERS_CONFIG=self.config,
            CROSS_CHAT_HANDOFFS_V2_CLIENT_CAPABILITY="cross_chat_handoffs_v2",
            AGENT_CROSS_CHAT_ROUTES_CLIENT_CAPABILITY="agent_cross_chat_routes_v2",
        )
        exec(CODE, self.ns)
        self.route = peers.route(self.peer)
        self.capability = {"provider_route_grants": {self.route["route_id"]: self.route}}
        self.prompt = "Before 🧪 @Synthetic Duet 📱 send the synthetic update"

    def save(self):
        self.config.write_text(json.dumps({"version": 1, "peers": [self.peer, *getattr(self, "other_peers", [])]}))
        self.config.chmod(0o600)

    def references(self, prompt=None, source="sess_a", action="instruction"):
        selection = self.ns["AgentJobChatRouteSelection"](route_id=self.route["route_id"], action=action)
        return self.ns["provider_job_chat_references"](
            self.capability, source, prompt or self.prompt, [selection])

    def validate(self, references, source="sess_a", prompt=None):
        return self.ns["validate_scheduled_job_chat_references"](
            source, prompt or self.prompt, references, redact_target_detail=True)

    def project(self, references, source="sess_a"):
        return self.ns["provider_cross_chat_route_snapshot_for_authority"](
            self.ns["provider_cross_chat_routes"](self.ns["STORE"].sessions[source]),
            references, source_session_id=source, per_job_reference_routes=True)

    async def test_conversion_pins_exact_pair_and_unicode_marker_without_native_target(self):
        reference, = self.references()
        self.assertNotIn(self.peer["peer_id"], self.ns["STORE"].sessions)
        self.assertEqual(reference.target_kind, "external_peer")
        self.assertEqual(reference.session_id, self.peer["peer_id"])
        self.assertEqual(reference.target_route_id, self.peer["route_id"])
        self.assertEqual(reference.target_route_revision, self.peer["revision"])
        self.assertEqual(reference.target_pair_id, self.peer["pair_id"])
        self.assertEqual(reference.target_paired_route_id, self.peer["inbound_route_id"])
        self.assertEqual((reference.action, reference.route_action), ("route", "instruction"))
        self.assertEqual(self.ns["utf16_slice"](
            self.prompt, reference.source_text_start, reference.source_text_end), "@Synthetic Duet 📱")
        stored = self.ns["chat_reference_dict"](reference)
        self.assertEqual(self.validate([stored]), [reference])
        self.assertEqual(self.project([reference]), [self.route])
        # A second enabled owner-provisioned pair must never leak into this job.
        self.other_peers = [{**self.peer, "peer_id": "extpeer_" + "6" * 32,
                             "credential_sha256": "6" * 64, "pair_id": "pair_" + "6" * 32,
                             "route_id": "route_" + "7" * 32, "inbound_route_id": "route_" + "8" * 32}]
        self.save()
        self.assertEqual(len(self.ns["provider_cross_chat_routes"](self.ns["STORE"].sessions["sess_a"])), 2)
        self.assertEqual(self.project([reference]), [self.route])
        # Even a source with a live external pair gets no ambient scheduled grant.
        self.assertEqual(self.project([]), [])

    async def test_identity_changes_and_revocation_never_regrant_saved_job(self):
        reference, = self.references()
        original = deepcopy(self.peer)
        changes = {
            "enabled": False, "target_session_id": "sess_b", "revision": "rev_" + "a" * 32,
            "pair_id": "pair_" + "a" * 32, "route_id": "route_" + "a" * 32,
            "inbound_route_id": "route_" + "a" * 32,
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                self.peer = {**original, field: value}
                self.save()
                with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]):
                    self.validate([reference])
                self.assertEqual(self.project([reference]), [])
        self.peer = original
        self.save()
        source = self.ns["STORE"].sessions["sess_a"]
        for field, value in (("archived", True), ("_revoked_provider_cross_chat_route_ids", [self.route["route_id"]])):
            with self.subTest(source_field=field):
                source[field] = value
                with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]):
                    self.validate([reference])
                self.assertEqual(self.project([reference]), [])
                source.pop(field)
        for fence in ("DELETING_SESSIONS", "DELETED_SESSION_TOMBSTONES"):
            with self.subTest(fence=fence):
                self.ns[fence].add("sess_a")
                with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]):
                    self.validate([reference])
                self.assertEqual(self.project([reference]), [])
                self.ns[fence].remove("sess_a")
        with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]):
            self.validate([reference], source="sess_b")
        self.assertEqual(self.project([reference], source="sess_b"), [])

    async def test_plain_turns_missing_mentions_and_action_escalation_are_denied(self):
        reference, = self.references()
        with self.assertRaises(HTTPException) as error:
            self.ns["validate_chat_references"]("sess_a", self.prompt, [reference])
        self.assertEqual(error.exception.status_code, 400)
        for prompt in ("No selected recipient", self.prompt.replace("@Synthetic", "@@Synthetic"),
                       self.prompt.replace("@Synthetic", "x@Synthetic")):
            with self.subTest(prompt=prompt), self.assertRaises(HTTPException):
                self.references(prompt=prompt)
        with self.assertRaises(HTTPException) as error:
            self.references(action="request_reply")
        self.assertEqual(error.exception.status_code, 403)
        with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]):
            self.validate([reference], prompt=self.prompt.replace("@Synthetic", "@Other"))
        double_marker = reference.model_copy(update={"source_text_end": reference.source_text_end + 1})
        with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]):
            self.validate([double_marker], prompt=self.prompt.replace("@Synthetic", "@@Synthetic"))
        with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]):
            self.validate([reference, reference])

    async def test_external_discriminator_rejects_incomplete_or_mixed_shapes(self):
        reference, = self.references()
        raw = self.ns["chat_reference_dict"](reference)
        changes = {
            "target_pair_id": None, "target_paired_route_id": None, "target_route_revision": None,
            "target_route_id": "bad", "session_id": "native-chat", "route_action": "request_reply",
            "action": "instruction", "grant_intent": True, "target_server_identity": "server-identity",
            "target_connection_id": "some-connection", "target_kind": None,
        }
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.ns["ChatReference"].model_validate({**raw, field: value})
        with self.assertRaises(ValidationError):
            self.ns["ChatReference"].model_validate({**raw, "target_paired_route_id": raw["target_route_id"]})

    async def test_secure_peer_jobs_remain_rejected(self):
        route_id = "12345678-1234-4234-8234-123456789012"
        reference = self.ns["ChatReference"](
            session_id=route_id, display_title_snapshot="Remote", source_text_start=0,
            source_text_end=7, action="instruction", target_kind="secure_peer",
            target_server_identity="synthetic-server", target_connection_id=route_id,
            target_route_id=route_id, target_route_revision="rev_" + "a" * 32)
        with self.assertRaises(self.ns["ScheduledJobChatReferenceRepairRequired"]) as error:
            self.validate([reference], prompt="@Remote inspect")
        self.assertEqual(error.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
