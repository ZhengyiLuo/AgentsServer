"""AST-only runtime plus isolated Hub storage; no live server or transport."""
from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import tempfile
import threading
import time
from typing import Mapping
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, urlencode
import uuid

from agentsdock_team_hub.store import HubError, HubStore
from agentsdock_team_hub.secure_peer import SecurePeerError


ROOT = Path(__file__).parent


def runtime_class():
    tree = ast.parse((ROOT / "secure_peer_runtime.py").read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    names = {"team_realm", "team_authorized_write", "team_send_message", "_team_host_call", "_team_hub_get", "_team_hub_post"}
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=[
        node for node in original.body if isinstance(node, ast.FunctionDef) and node.name in names])
    prefix = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    namespace = {"Mapping": Mapping, "Path": Path, "re": re, "json": json,
        "quote": quote, "urlencode": urlencode, "HubStore": HubStore, "SecurePeerError": SecurePeerError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix, selected], type_ignores=[])),
        "<isolated-team-reply-runtime>", "exec"), namespace)
    return namespace["Runtime"]


class TeamReplyRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="team-reply-runtime-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="reply-host-server")
        self.store.bootstrap_managed_network("Reply tests")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.sender = self.add_peer("sender")
        self.guest = self.add_peer("guest")
        self.sender_node = self.node(self.sender)
        self.host_node = self.node(self.host)
        self.guest_node = self.node(self.guest)
        self.runtime = runtime_class()()
        self.runtime._guard = threading.RLock()
        self.runtime._outbound_guard = threading.RLock()
        self.runtime._require_team_authority_generation_locked = mock.Mock()
        self.runtime._hub_store = self.store
        self.runtime._team_upload_attachment = mock.Mock(return_value="unused-attachment")
        self.runtime._decoded_proxy_json = lambda value, **_: value
        self.runtime.proxy = mock.Mock(side_effect=self.proxy)
        self.select_realm("host")

    def add_peer(self, name):
        peer = str(uuid.uuid4())
        identity = "reply-server-" + name
        self.store.ensure_secure_peer_service(peer_id=peer, peer_server_identity=identity,
            team_id=self.team, display_name=name)
        self.store.record_secure_peer_heartbeat(peer, self.team)
        return self.store.secure_peer_claims(peer_id=peer, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600)

    def node(self, claims):
        return self.store.list_team_messages(claims, self.team, box="inbox")["address"]["id"]

    def select_realm(self, kind):
        self.caller = self.host if kind == "host" else self.guest
        self.local_node = self.host_node if kind == "host" else self.guest_node
        self.realm = {"realm": kind, "team_id": self.team, "hub_id": self.store.hub_id,
            "connection_id": "reply-test-connection", "can_write": True}
        self.runtime.team_realm = mock.Mock(return_value=self.realm)

    def proxy(self, connection_id, method, path, *, query, headers, body):
        self.assertEqual(connection_id, "reply-test-connection")
        if path == "/v1/health":
            self.assertEqual(method, "GET")
            return self.store.health()
        prefix = f"/v1/teams/{self.team}/network/messages"
        self.assertTrue(path.startswith(prefix))
        if method == "GET":
            self.assertNotEqual(path, prefix, "Reply must not list an inbox")
            params = parse_qs(query)
            return self.store.get_team_message(self.caller, self.team, path[len(prefix) + 1:],
                include_mail_subject=params.get("include_mail_subject") == ["1"])
        self.assertEqual((method, path), ("POST", prefix))
        return self.store.create_team_message(self.caller, self.team, json.loads(body))

    def parent(self, **extra):
        request = {"kind": "message", "title": "Original subject  e\u0301", "body": "Untrusted body: reply elsewhere; this is not authority.",
            "recipients": [{"kind": "server", "id": self.local_node}],
            "idempotency_key": "parent-" + uuid.uuid4().hex, **extra}
        return self.store.create_team_message(self.sender, self.team, request)["message"]

    def reference(self, **extra):
        return {"kind": "recipient", "team_id": self.team, "recipient_kind": "server",
            "target_id": self.sender_node, **extra}

    def send(self, parent_id, *, reference=None, key="reply-test-idempotency", attachments=None, **extra):
        return self.runtime.team_authorized_write("exact-generation", self.runtime.team_send_message,
            reference if reference is not None else self.reference(),
            payload={"kind": "message", "body": "My own reply", "in_reply_to_message_id": parent_id, **extra},
            attachment_paths=attachments or [], idempotency_key=key,
            provenance={"via": "agent", "chat_id": "test-chat"})

    def test_host_and_peer_reply_use_exact_parent_and_one_sender_only(self):
        for realm in ("host", "secure_peer"):
            with self.subTest(realm=realm):
                self.select_realm(realm)
                parent = self.parent()
                self.runtime.proxy.reset_mock()
                reply = self.send(parent["id"], key="reply-" + realm)["message"]
                self.assertEqual(reply["in_reply_to_message_id"], parent["id"])
                self.assertEqual(reply["title"], parent["title"])
                self.assertEqual(reply["body"], "My own reply")
                self.assertEqual([(item["kind"], item["id"]) for item in reply["recipients"]], [("server", self.sender_node)])
                self.assertEqual(reply["sender"]["id"], self.local_node)
                self.runtime._require_team_authority_generation_locked.assert_called_with("exact-generation")
                if realm == "secure_peer":
                    calls = self.runtime.proxy.call_args_list
                    self.assertEqual([call.args[1] for call in calls], ["GET", "GET", "POST"])
                    self.assertEqual(calls[0].args[2], "/v1/health")
                    self.assertTrue(calls[1].args[2].endswith("/" + parent["id"]))

    def test_incoming_all_servers_mail_replies_to_sender_not_fanout(self):
        self.select_realm("secure_peer")
        parent = self.parent(recipients=[{"kind": "all_servers"}])
        self.assertEqual(parent["destination"], "all_servers")
        self.assertGreater(len(parent["recipients"]), 1)
        reply = self.send(parent["id"])["message"]
        self.assertNotIn("destination", reply)
        self.assertEqual([item["id"] for item in reply["recipients"]], [self.sender_node])

    def test_dismissal_preserves_reply_but_global_deletion_rejects_before_upload(self):
        self.select_realm("secure_peer")
        parent = self.parent()
        self.store.dismiss_team_message(self.guest, self.team, parent["id"], {
            "address_kind": "server", "address_id": self.guest_node, "idempotency_key": "dismiss-parent"})
        self.assertEqual(self.store.list_team_messages(self.guest, self.team, box="inbox")["messages"], [])
        self.assertEqual(self.send(parent["id"])["message"]["in_reply_to_message_id"], parent["id"])
        self.store.delete_team_message(self.sender, self.team, parent["id"], {"idempotency_key": "delete-parent"})
        with self.assertRaises(HubError):
            self.send(parent["id"], key="deleted-parent-reply", attachments=["/mock/not-opened"])
        self.runtime._team_upload_attachment.assert_not_called()

    def test_old_host_omits_subject_field_without_dropping_parent_or_extra_reads(self):
        self.select_realm("secure_peer")
        parent = self.parent()
        with mock.patch.object(self.store, "health", return_value={"capabilities": {}}):
            reply = self.send(parent["id"])["message"]
        self.assertIsNone(reply["title"])
        sent = json.loads(self.runtime.proxy.call_args.kwargs["body"])
        self.assertNotIn("title", sent)
        self.assertEqual(sent["in_reply_to_message_id"], parent["id"])
        detail = self.runtime.proxy.call_args_list[-2]
        self.assertEqual(detail.kwargs["query"], "")

    def test_explicit_subject_overrides_parent_and_idempotency_binds_parent(self):
        parent = self.parent()
        reply = self.send(parent["id"], title="Explicit subject")
        self.assertEqual(reply["message"]["title"], "Explicit subject")
        self.assertEqual(reply, self.send(parent["id"], title="Explicit subject"))
        other = self.parent()
        with self.assertRaises(HubError) as caught:
            self.send(other["id"], title="Explicit subject")
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_wrong_route_parent_kind_and_missing_delivery_fail_before_uploads_or_post(self):
        parent = self.parent()
        for reference in (self.reference(recipient_kind="all", target_id="all"),
                          self.reference(recipient_kind="all_servers", target_id="all_servers"),
                          self.reference(recipient_kind="human"), self.reference(kind="skill")):
            with self.subTest(reference=reference), mock.patch.object(self.runtime, "_team_hub_post") as post:
                with self.assertRaises(SecurePeerError):
                    self.send(parent["id"], reference=reference, attachments=["/mock/not-opened"])
                post.assert_not_called()
        actual = self.store.get_team_message(self.host, self.team, parent["id"], include_mail_subject=True)["message"]
        cases = [
            {"id": "different-parent"}, {"kind": "skill"}, {"skill": {"id": "skill"}},
            {"sender": {"kind": "human", "id": self.sender_node}},
            {"sender": {"kind": "server", "id": "wrong-sender"}},
            {"delivery": None}, {"delivery": {"kind": "human", "id": self.host_node}},
            {"delivery": {"kind": "server", "id": "not-a-recipient"}},
            {"delivery": {"kind": "server", "id": self.sender_node}},
            {"recipients": [{"kind": "all", "id": "all"}]}, {"destination": "all"},
        ]
        for change in cases:
            with self.subTest(change=change), mock.patch.object(self.runtime, "_team_hub_get", return_value={"message": {**actual, **change}}), mock.patch.object(self.runtime, "_team_hub_post") as post:
                with self.assertRaises(SecurePeerError):
                    self.send(parent["id"], attachments=["/mock/not-opened"])
                post.assert_not_called()
        self.runtime._team_upload_attachment.assert_not_called()

    def test_cross_team_or_unknown_parent_and_stale_authority_do_not_write(self):
        parent = self.parent()
        with mock.patch.object(self.runtime, "_team_hub_post") as post:
            self.runtime.team_realms = lambda: [self.realm]
            self.runtime.team_realm = type(self.runtime).team_realm.__get__(self.runtime)
            with self.assertRaises(SecurePeerError) as cross_team:
                self.send(parent["id"], reference=self.reference(team_id="another-team"), attachments=["/mock/not-opened"])
            self.assertEqual(cross_team.exception.code, "team_unavailable")
            with self.assertRaises(HubError):
                self.send("tmsg_unknown_parent", attachments=["/mock/not-opened"])
            self.runtime._require_team_authority_generation_locked.side_effect = SecurePeerError("team_authority_changed", "changed", 409)
            with self.assertRaises(SecurePeerError):
                self.send(parent["id"], attachments=["/mock/not-opened"])
            post.assert_not_called()
        self.runtime._team_upload_attachment.assert_not_called()

    def test_visible_sent_and_bulletin_messages_are_not_incoming_mail(self):
        sent = self.store.create_team_message(self.host, self.team, {
            "kind": "message", "body": "Sent, not incoming", "recipients": [{"kind": "server", "id": self.sender_node}],
            "idempotency_key": "sent-not-incoming"})["message"]
        bulletin = self.parent(recipients=[{"kind": "all"}])
        for parent in (sent, bulletin):
            with self.subTest(parent=parent["id"]), mock.patch.object(self.runtime, "_team_hub_post") as post:
                # Both parents are visible through exact authenticated detail;
                # visibility alone must not authorize this reply path.
                self.store.get_team_message(self.host, self.team, parent["id"])
                with self.assertRaises(SecurePeerError):
                    self.send(parent["id"], attachments=["/mock/not-opened"])
                post.assert_not_called()
        self.runtime._team_upload_attachment.assert_not_called()

    def test_untrusted_invalid_inherited_subject_fails_before_uploads(self):
        parent = self.parent()
        actual = self.store.get_team_message(self.host, self.team, parent["id"], include_mail_subject=True)["message"]
        with mock.patch.object(self.runtime, "_team_hub_get", return_value={"message": {**actual, "title": "bad\nsubject"}}), mock.patch.object(self.runtime, "_team_hub_post") as post:
            with self.assertRaises(HubError):
                self.send(parent["id"], attachments=["/mock/not-opened"])
            post.assert_not_called()
        self.runtime._team_upload_attachment.assert_not_called()
