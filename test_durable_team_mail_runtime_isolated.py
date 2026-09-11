"""Exact durable mail identity checks; no runtime startup, network or mail."""
from __future__ import annotations

import ast
from collections.abc import Mapping
import hashlib
import hmac
import json
from pathlib import Path
import re
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import quote


class PeerError(Exception):
    def __init__(self, code, message, status_code):
        super().__init__(message)
        self.code, self.status_code = code, status_code


def runtime_type():
    path = Path(__file__).with_name("secure_peer_runtime.py")
    source = next(node for node in ast.parse(path.read_text()).body if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    names = {"_team_network_server", "_durable_server_binding", "resolve_durable_server_reference",
             "validate_durable_server_reference", "resolve_team_references", "team_realm", "team_send_message",
             "team_authorized_write", "team_authority_generation", "_team_authority_generation_locked",
             "_team_authority_digest", "_require_team_authority_generation_locked"}
    methods = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=methods)
    namespace = {"Mapping": Mapping, "SecurePeerError": PeerError, "HubError": PeerError,
                 "re": re, "quote": quote, "Path": Path, "hashlib": hashlib, "hmac": hmac,
                 "canonical_json": lambda value: json.dumps(value, sort_keys=True, separators=(",", ":")).encode()}
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), selected], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace["Runtime"]


class DurableMailRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.Runtime = runtime_type()

    def setUp(self):
        self.runtime = self.Runtime()
        self.runtime._guard = threading.RLock()
        self.runtime._outbound_guard = threading.RLock()
        self.runtime._team_authority_epoch = "first-runtime-epoch"
        self.realm = {"realm": "secure_peer", "team_id": "team-fixture", "hub_id": "hub-fixture", "connection_id": "fresh-connection", "can_write": True}
        self.server = {"id": "node-fixture", "server_identity": "server-fixture", "display_name": "Original name", "status": "active", "mail_route_lifecycle_id": "a" * 64}
        self.runtime.team_realms = Mock(side_effect=lambda: [dict(self.realm)])
        self.runtime._team_hub_get = Mock(side_effect=lambda *_args, **_kwargs: {"server": dict(self.server)})
        self.runtime._team_hub_post = Mock(return_value={"ok": True})
        self.runtime._team_upload_attachment = Mock(return_value="attachment-fixture")
        self.reference = {"kind": "recipient", "recipient_kind": "server", "team_id": "team-fixture", "target_id": "node-fixture", "display_name_snapshot": "Original name"}

    def resolved(self):
        return self.runtime.resolve_team_references([self.reference])[0]

    def send(self, reference):
        return self.runtime.team_send_message(reference, payload={"body": "Synthetic fixture"}, attachment_paths=["unused-file"], idempotency_key="fixture-key", provenance={})

    def test_initial_label_validation_then_identity_only_rename_resolution(self):
        reference = self.resolved()
        binding = reference["durable_server_binding"]
        self.assertEqual(set(binding), {"version", "team_id", "hub_id", "target_id", "server_identity", "lifecycle_id"})
        self.server["display_name"] = "Renamed server"
        self.assertEqual(self.runtime.resolve_durable_server_reference(binding)["display_name_snapshot"], "Renamed server")
        with self.assertRaises(PeerError):
            self.resolved()

    def test_changed_or_missing_exact_identity_fails_closed(self):
        binding = self.resolved()["durable_server_binding"]
        for owner, key, value in ((self.realm, "hub_id", "replacement-hub"), (self.realm, "team_id", "other-team"),
                                  (self.server, "id", "replacement-node"), (self.server, "server_identity", "replacement-server"),
                                  (self.server, "mail_route_lifecycle_id", "b" * 64), (self.server, "status", "revoked"),
                                  (self.server, "mail_route_lifecycle_id", None)):
            with self.subTest(key=key, value=value):
                previous = owner[key]
                owner[key] = value
                with self.assertRaises(PeerError):
                    self.runtime.resolve_durable_server_reference(binding)
                owner[key] = previous
        self.runtime._team_hub_get.side_effect = PeerError("not_found", "gone", 404)
        with self.assertRaises(PeerError):
            self.runtime.resolve_durable_server_reference(binding)

    def test_send_rechecks_before_upload_and_binds_transaction_expectation(self):
        reference = self.resolved()
        self.server["mail_route_lifecycle_id"] = "b" * 64
        with self.assertRaises(PeerError):
            self.send(reference)
        self.runtime._team_upload_attachment.assert_not_called()
        self.runtime._team_hub_post.assert_not_called()
        self.server["mail_route_lifecycle_id"] = "a" * 64
        self.send(reference)
        self.assertEqual(self.runtime._team_hub_post.call_args.args[2]["recipients"], [
            {"kind": "server", "id": "node-fixture", "mail_route_lifecycle_id": "a" * 64}])

    def test_old_hub_keeps_one_use_route_but_cannot_resolve_durable_grant(self):
        binding = self.resolved()["durable_server_binding"]
        self.server.pop("mail_route_lifecycle_id")
        self.assertEqual(self.resolved(), self.reference)
        self.send(self.reference)
        self.assertEqual(self.runtime._team_hub_post.call_args.args[2]["recipients"], [{"kind": "server", "id": "node-fixture"}])
        with self.assertRaises(PeerError):
            self.runtime.resolve_durable_server_reference(binding)

    def test_restart_requires_fresh_authority_but_keeps_identity_grant(self):
        reference = self.resolved()
        previous_generation = self.runtime.team_authority_generation()
        self.runtime._team_authority_epoch = "replacement-runtime-epoch"
        self.realm["connection_id"] = "replacement-live-connection"
        self.assertEqual(self.runtime.resolve_durable_server_reference(reference["durable_server_binding"]), reference)
        with self.assertRaises(PeerError):
            self.runtime.team_authorized_write(previous_generation, self.send, reference)
        self.runtime._team_hub_post.assert_not_called()
        current_generation = self.runtime.team_authority_generation()
        self.runtime.team_authorized_write(current_generation, self.send, reference)
        self.runtime._team_hub_post.assert_called_once()

    def test_host_uses_exact_live_projection_and_rejects_cross_recipient_binding(self):
        self.realm["realm"] = "host"
        store = SimpleNamespace(hub_id="hub-fixture", local_agent_mail_claims=Mock(return_value="fixture-claims"), get_network_server=Mock(return_value={"server": self.server}))
        self.runtime._hub_store = store
        reference = self.resolved()
        store.get_network_server.assert_called_once_with("fixture-claims", "team-fixture", "node-fixture")
        self.runtime._team_hub_get.assert_not_called()
        with self.assertRaises(PeerError):
            self.send({**reference, "target_id": "other-node"})
        self.runtime._team_upload_attachment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
