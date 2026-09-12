"""Explicit Bulletin edits through the CLI and AST-only authenticated runtime.

No server import, provider execution, or live transport. Hub storage is synthetic.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import time
from typing import Mapping
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, urlencode, urlsplit
import uuid

import agentsdock_team as cli
from agentsdock_team_hub.secure_peer import SecurePeerError
from agentsdock_team_hub.store import HubError, HubStore


MESSAGE = "tmsg_bulletin_001"
ROUTE = "team_bulletin_route"


def _runtime_class():
    tree = ast.parse(Path(__file__).with_name("secure_peer_runtime.py").read_text())
    source = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    names = {"team_send_message", "team_get_message", "_team_hub_get", "_team_hub_post", "_team_host_call_admitted"}
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=[
        node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names
    ])
    prefix = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    namespace = {"Mapping": Mapping, "Path": Path, "re": re, "json": json,
        "quote": quote, "urlencode": urlencode, "SecurePeerError": SecurePeerError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix, selected], type_ignores=[])),
        "<isolated-bulletin-edit-runtime>", "exec"), namespace)
    return namespace["Runtime"]


Runtime = _runtime_class()


class BulletinEditHelperTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.object(cli, "_provider_authority", return_value=("synthetic-capability", "synthetic-chat")))
        self.body = self.enterContext(mock.patch.object(cli, "_read_body", return_value="Revised Bulletin body"))
        self.receipt = {"ok": True, "route_id": ROUTE, "message_id": MESSAGE, "kind": "bulletin_edit",
            "accepted": True, "duplicate": False, "attachments": 0, "edited": True, "version": 2}
        self.request = self.enterContext(mock.patch.object(cli, "_request_json", return_value=self.receipt))

    def execute(self, *args):
        parsed = cli.parser().parse_args(list(args))
        return parsed.handler(parsed)

    def edit(self, message=MESSAGE, version=1):
        return self.execute("edit", message, "--route", ROUTE, "--expected-version", str(version))

    def test_edit_posts_exact_id_body_version_once_and_uses_stable_operation_key(self):
        self.assertEqual(self.edit(), self.receipt)
        self.request.assert_called_once()
        self.assertEqual(self.request.call_args.args[:3], ("POST", f"/api/agent/team/routes/{ROUTE}", "synthetic-capability"))
        first = dict(self.request.call_args.args[3])
        self.assertEqual(set(first), {"kind", "message_id", "expected_version", "body", "body_format", "idempotency_key"})
        self.assertEqual(first["kind"], "bulletin_edit")
        self.assertEqual((first["message_id"], first["expected_version"], first["body"]), (MESSAGE, 1, "Revised Bulletin body"))
        self.edit()
        self.assertEqual(self.request.call_args.args[3], first)
        keys = {first["idempotency_key"]}
        for message, version, body in (("tmsg_bulletin_002", 1, "Revised Bulletin body"),
            (MESSAGE, 2, "Revised Bulletin body"), (MESSAGE, 1, "Revised Bulletin body, different tail")):
            self.request.return_value = {**self.receipt, "message_id": message, "version": version + 1}
            self.body.return_value = body
            self.edit(message, version)
            keys.add(self.request.call_args.args[3]["idempotency_key"])
        self.assertEqual(len(keys), 4)

    def test_old_server_and_conflicts_are_errors_without_creation_fallback(self):
        for message in ("422 unsupported kind bulletin_edit", "409 version_conflict", "404 not_found"):
            self.request.reset_mock()
            self.request.side_effect = cli.TeamCLIError(message)
            with self.subTest(message=message), self.assertRaisesRegex(cli.TeamCLIError, message):
                self.edit()
            self.request.assert_called_once()
            self.assertEqual(self.request.call_args.args[3]["kind"], "bulletin_edit")

    def test_invalid_id_version_or_replacement_options_fail_before_transport(self):
        for message, version in (("not/an/id", 1), (MESSAGE, 0), (MESSAGE, -1)):
            with self.subTest(message=message, version=version), self.assertRaises(cli.TeamCLIError):
                self.edit(message, version)
        for option in ("--title", "--attach", "--skill-slug", "--in-reply-to"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.execute("edit", MESSAGE, "--route", ROUTE, "--expected-version", "1", option, "synthetic-value")
        self.body.assert_not_called()
        self.request.assert_not_called()

    def test_edit_receipt_requires_exact_operation_id_and_next_integer_version(self):
        for change in ({"message_id": "tmsg_other_001"}, {"kind": "message"}, {"edited": False},
            {"version": 1}, {"version": True}, {"attachments": None}, {"accepted": False}):
            self.request.return_value = {**self.receipt, **change}
            with self.subTest(change=change), self.assertRaisesRegex(cli.TeamCLIError, "invalid Team Network edit receipt"):
                self.edit()

    def test_revision_read_is_opt_in_and_help_preserves_body_only_semantics(self):
        self.execute("read", MESSAGE)
        self.assertNotIn("include_revision", self.request.call_args.args[1])
        self.execute("read", MESSAGE, "--include-revision")
        self.assertEqual(parse_qs(urlsplit(self.request.call_args.args[1]).query)["include_revision"], ["1"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit):
            cli.parser().parse_args(["edit", "--help"])
        self.assertIn("--include-revision", output.getvalue())
        self.assertIn("attachments, and skill data are preserved", output.getvalue())


class BulletinEditRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bulletin-edit-runtime-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="synthetic-bulletin-host")
        self.store.bootstrap_managed_network("Bulletin edit fixture")
        self.team = self.store.managed_server_claims().team_id
        self.host = self.store.local_agent_mail_claims(self.team)
        peer = str(uuid.uuid4())
        self.store.ensure_secure_peer_service(peer_id=peer, peer_server_identity="synthetic-bulletin-peer",
            team_id=self.team, display_name="Bulletin peer")
        self.store.record_secure_peer_heartbeat(peer, self.team)
        self.peer = self.store.secure_peer_claims(peer_id=peer, peer_server_identity="synthetic-bulletin-peer",
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}), expires_at=int(time.time()) + 3600)
        self.runtime = Runtime()
        self.runtime._team_upload_attachment = mock.Mock(side_effect=AssertionError("Edit must not upload"))
        self.runtime._decoded_proxy_json = lambda value, **_: value
        self.runtime._team_host_call = lambda realm, method, path, query, body: self.runtime._team_host_call_admitted(
            self.store, realm, method, path, query, body)
        self.runtime.proxy = mock.Mock(side_effect=self.proxy)
        self.select("host")

    def select(self, realm):
        self.caller = self.host if realm == "host" else self.peer
        self.realm = {"realm": realm, "team_id": self.team, "hub_id": self.store.hub_id,
            "can_write": True, "connection_id": "synthetic-connection"}
        self.runtime.team_realm = mock.Mock(return_value=self.realm)

    def proxy(self, connection_id, method, path, *, query, headers, body):
        self.assertEqual(connection_id, "synthetic-connection")
        prefix = f"/v1/teams/{self.team}/network/messages/"
        self.assertTrue(path.startswith(prefix))
        suffix = path[len(prefix):]
        if method == "GET":
            return self.store.get_team_message(self.caller, self.team, suffix,
                include_revision=parse_qs(query).get("include_revision") == ["1"])
        self.assertEqual(method, "POST")
        self.assertTrue(suffix.endswith("/revisions"), "No create-message fallback")
        return self.store.revise_team_message(self.caller, self.team, suffix[:-len("/revisions")], json.loads(body))

    def create(self, claims=None, **extra):
        return self.store.create_team_message(claims or self.caller, self.team, {
            "kind": "message", "body": "Original Bulletin body", "recipients": [{"kind": "all"}],
            "idempotency_key": "create-" + uuid.uuid4().hex, **extra,
        })["message"]

    def edit(self, message_id, *, reference=None, key="edit-fixture", attachment_paths=None, **extra):
        return self.runtime.team_send_message(reference or {
            "kind": "recipient", "recipient_kind": "all", "team_id": self.team,
        }, payload={"kind": "bulletin_edit", "message_id": message_id, "expected_version": 1,
            "body": "Revised Bulletin body", "body_format": "markdown", **extra},
            attachment_paths=attachment_paths or [], idempotency_key=key, provenance={"via": "agent"})

    def test_host_and_peer_edit_same_id_one_post_and_keep_version_history(self):
        for realm in ("host", "secure_peer"):
            with self.subTest(realm=realm):
                self.select(realm)
                original = self.create()
                before = self.store.list_team_messages(self.caller, self.team, box="feed")["messages"]
                self.runtime.proxy.reset_mock()
                revised = self.edit(original["id"], key="edit-" + realm)["message"]
                self.assertEqual((revised["id"], revised["body"], revised["revision"]["version"]),
                    (original["id"], "Revised Bulletin body", 2))
                self.assertEqual(revised["attachments"], original["attachments"])
                after = self.store.list_team_messages(self.caller, self.team, box="feed")["messages"]
                self.assertEqual({item["id"] for item in before}, {item["id"] for item in after})
                history = self.store.list_team_message_revisions(self.caller, self.team, original["id"])
                self.assertEqual([item["version"] for item in history["versions"]], [2, 1])
                self.assertEqual(self.edit(original["id"], key="edit-" + realm)["message"], revised)
                if realm == "secure_peer":
                    self.assertEqual(len(self.runtime.proxy.call_args_list), 2)
                    outbound = json.loads(self.runtime.proxy.call_args.kwargs["body"])
                    self.assertEqual(set(outbound), {"body", "body_format", "expected_version", "idempotency_key"})

    def test_hub_author_only_unchanged_stale_missing_and_skill_errors_propagate(self):
        original = self.create()
        cases = ((self.peer, {}, "forbidden", 403), (self.host, {"body": original["body"]}, "unchanged", 409),
            (self.host, {"expected_version": 2}, "version_conflict", 409))
        for caller, changes, code, status in cases:
            self.select("host" if caller is self.host else "secure_peer")
            with self.subTest(code=code), self.assertRaises(HubError) as error:
                self.edit(original["id"], **changes)
            self.assertEqual((error.exception.code, error.exception.status_code), (code, status))
        self.select("host")
        with self.assertRaises(HubError) as missing:
            self.edit("tmsg_missing_001")
        self.assertEqual(missing.exception.status_code, 404)
        skill = self.create(kind="skill", title="Fixture skill", skill={"slug": "fixture-skill"})
        with self.assertRaises(HubError) as error:
            self.edit(skill["id"])
        self.assertEqual(error.exception.code, "skill_version_required")
        self.runtime._team_upload_attachment.assert_not_called()

    def test_forbidden_routes_or_payloads_fail_before_transport(self):
        for reference in ({"kind": "recipient", "recipient_kind": "server"},
            {"kind": "recipient", "recipient_kind": "all_servers"}, {"kind": "skill", "recipient_kind": "all"}):
            with self.subTest(reference=reference), self.assertRaises(SecurePeerError):
                self.edit(MESSAGE, reference=reference)
        for change in ({"title": "new title"}, {"attachments": ["unused"]}, {"skill": {}},
            {"in_reply_to_message_id": MESSAGE}, {"attachment_paths": ["/synthetic/not-opened"]},
            {"expected_version": True}, {"expected_version": 0}):
            with self.subTest(change=change), self.assertRaises(SecurePeerError):
                self.edit(MESSAGE, **change)
        self.realm["can_write"] = False
        with self.assertRaises(SecurePeerError) as error:
            self.edit(MESSAGE)
        self.assertEqual(error.exception.status_code, 403)
        self.runtime.proxy.assert_not_called()
        self.runtime._team_upload_attachment.assert_not_called()

    def test_revision_discovery_is_opt_in_for_host_and_peer(self):
        for realm in ("host", "secure_peer"):
            self.select(realm)
            original = self.create()
            legacy = self.runtime.team_get_message(original["id"])["message"]
            self.assertNotIn("revision", legacy)
            current = self.runtime.team_get_message(original["id"], include_revision=True)["message"]
            self.assertEqual(current["revision"]["version"], 1)
        with self.assertRaises(SecurePeerError):
            self.runtime.team_get_message(MESSAGE, include_revision="true")

    def test_cli_endpoint_runtime_store_chain_returns_actual_edit_receipt(self):
        import test_agent_team_reply_endpoint_isolated as endpoint_helpers

        fixture = endpoint_helpers.TeamReplyEndpointTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.reference.update(kind="recipient", recipient_kind="all", team_id=self.team)
        fixture.runtime.team_send_message.side_effect = self.runtime.team_send_message
        original = self.create()

        def request(method, path, capability, payload, **_kwargs):
            self.assertEqual((method, path), ("POST", f"/api/agent/team/routes/{endpoint_helpers.ROUTE}"))
            model = fixture.namespace["AgentTeamSendRequest"](**payload)
            return asyncio.run(fixture.namespace["send_provider_team_message"](endpoint_helpers.ROUTE, model, fixture.request))

        with mock.patch.object(cli, "_provider_authority", return_value=("synthetic-capability", "synthetic-chat")), \
            mock.patch.object(cli, "_read_body", return_value="Actual helper revision"), \
            mock.patch.object(cli, "_request_json", side_effect=request) as transport:
            args = cli.parser().parse_args(["edit", original["id"], "--route", endpoint_helpers.ROUTE, "--expected-version", "1"])
            result = args.handler(args)
        self.assertEqual((result["message_id"], result["version"], result["attachments"]), (original["id"], 2, 0))
        self.assertTrue(result["edited"])
        transport.assert_called_once()
        fixture.events.assert_not_called()
        stored = self.store.get_team_message(self.host, self.team, original["id"], include_revision=True)["message"]
        self.assertEqual(stored["body"], "Actual helper revision")


if __name__ == "__main__":
    unittest.main()
