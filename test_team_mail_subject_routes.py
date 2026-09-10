"""Mail-subject query forwarding with ASGI/mocked transports and isolated state."""

import ast
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from typing import Mapping
from urllib.parse import parse_qs, quote, urlencode
import uuid

from fastapi.testclient import TestClient

from agentsdock_team_hub.secure_peer import (
    PeerAuthorization,
    ProxyRequest,
    SecurePeerError,
    sanitize_proxy_request,
)
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from agentsdock_team_hub.service import create_app
from agentsdock_team_hub.store import HubStore


REPO = Path(__file__).resolve().parent
MESSAGE_ID = "message_subjects_001"
REVISION_BODY = {
    "body": "Revised body, unchanged subject",
    "body_format": "markdown",
    "expected_version": 1,
    "idempotency_key": "subject-revision-request",
}


class MailSubjectServiceRoutesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mail-subject-routes-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = create_app(self.root, allowed_hosts={"localhost"})
        self.client = self.enterContext(TestClient(
            self.app, base_url="http://localhost", client=("127.0.0.1", 41000)
        ))
        proof = (self.root / "bootstrap-owner.proof").read_text().strip()
        result = self.client.post(
            "/v1/bootstrap/redeem",
            headers={"X-Team-Hub-Bootstrap-Proof": proof},
            json={"email": "subject-tests@example.com", "display_name": "Subject tests", "device_label": "Isolated tests"},
        )
        self.assertEqual(result.status_code, 200, result.text)
        owner = result.json()
        self.team_id = owner["teams"][0]["id"]
        self.auth = {"Authorization": f"Bearer {owner['access_token']}"}
        self.base = f"/v1/teams/{self.team_id}/network/messages"

    def test_three_service_routes_forward_opt_in_and_default_false(self):
        cases = (
            ("GET", self.base, "list_team_messages"),
            ("GET", f"{self.base}/{MESSAGE_ID}", "get_team_message"),
            ("POST", f"{self.base}/{MESSAGE_ID}/revisions", "revise_team_message"),
        )
        for method, path, name in cases:
            for query, expected in (("", False), ("?include_mail_subject=true", True), ("?include_mail_subject=false", False)):
                with self.subTest(method=method, path=path, query=query):
                    with mock.patch(f"agentsdock_team_hub.service.HubStore.{name}", return_value={"ok": True}) as forwarded:
                        response = self.client.request(
                            method, path + query, headers=self.auth,
                            **({"json": REVISION_BODY} if method == "POST" else {}),
                        )
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIs(forwarded.call_args.kwargs["include_mail_subject"], expected)
                    if method == "POST":
                        self.assertNotIn("title", forwarded.call_args.args[-1])

    def test_service_keeps_revision_selection_and_authentication(self):
        with mock.patch("agentsdock_team_hub.service.HubStore.get_team_message", return_value={"ok": True}) as forwarded:
            response = self.client.get(
                f"{self.base}/{MESSAGE_ID}?include_revision=true&include_mail_subject=true", headers=self.auth
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIs(forwarded.call_args.kwargs["include_revision"], True)
            self.assertIs(forwarded.call_args.kwargs["include_mail_subject"], True)
            unauthorized = self.client.get(f"{self.base}/{MESSAGE_ID}?include_mail_subject=true")
            self.assertEqual(unauthorized.status_code, 401, unauthorized.text)
            self.assertEqual(forwarded.call_count, 1)
        with mock.patch("agentsdock_team_hub.service.HubStore.list_team_message_revisions", return_value={"ok": True}) as history:
            response = self.client.get(
                f"{self.base}/{MESSAGE_ID}/revisions?version=2", headers=self.auth
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(history.call_args.kwargs["version"], 2)
        self.assertNotIn("include_mail_subject", history.call_args.kwargs)

    def test_message_subject_is_trimmed_before_length_validation_without_changing_skills(self):
        body = {
            "kind": "message", "title": "  " + "é" * 160 + "  ", "body": "Mail body",
            "recipients": [{"kind": "all"}], "idempotency_key": "subject-create-request",
        }
        with mock.patch("agentsdock_team_hub.service.HubStore.create_team_message", return_value={"ok": True}) as create:
            response = self.client.post(self.base, headers=self.auth, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(create.call_args.args[-1]["title"], "é" * 160)
            for invalid in ("", " ", "x" * 161, "\tSubject", "Subject\n", "Line\rbreak", "Nul\x00", "Line\u2028break", "Para\u2029break"):
                create.reset_mock()
                response = self.client.post(self.base, headers=self.auth, json={**body, "title": invalid})
                self.assertEqual(response.status_code, 422, (invalid, response.text))
                create.assert_not_called()
            response = self.client.post(self.base, headers=self.auth, json={**body, "kind": "skill"})
            self.assertEqual(response.status_code, 422, response.text)
            response = self.client.post(self.base, headers=self.auth, json={**body, "kind": "skill", "title": " Skill title "})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(create.call_args.args[-1]["title"], " Skill title ")


class MailSubjectPeerRoutesTests(unittest.TestCase):
    def setUp(self):
        self.peer = PeerAuthorization(
            str(uuid.uuid4()), str(uuid.uuid4()), "subject-peer-server", "subject-team-001",
            frozenset({"teamspace.read", "teamspace.write"}), "sha256:" + "a" * 64,
            int(time.time()) + 600, "Subject peer",
        )
        self.store = mock.Mock()
        self.adapter = SecurePeerHubAdapter(self.store)
        self.base = f"/v1/teams/{self.peer.team_id}/network/messages"
        self.enterContext(mock.patch.object(self.adapter, "_claims", return_value=object()))

    def request(self, method, path, query, body=b""):
        headers = (("content-type", "application/json"),) if method == "POST" else ()
        return sanitize_proxy_request(self.peer, method, path, query, headers, body)

    def test_gateway_and_adapter_forward_only_supported_opt_in_routes(self):
        import json

        cases = (
            ("GET", self.base, "list_team_messages", "box=inbox&include_revision=1&"),
            ("GET", f"{self.base}/{MESSAGE_ID}", "get_team_message", "include_revision=true&"),
            ("POST", f"{self.base}/{MESSAGE_ID}/revisions", "revise_team_message", ""),
        )
        for method, path, name, prefix in cases:
            for suffix, expected in (("include_mail_subject=true", True), ("include_mail_subject=0", False), ("", False)):
                with self.subTest(method=method, path=path, suffix=suffix):
                    forwarded = getattr(self.store, name)
                    forwarded.reset_mock()
                    forwarded.return_value = {"ok": True}
                    query = prefix + suffix if suffix else prefix.rstrip("&")
                    response = self.adapter.forward(self.request(
                        method, path, query,
                        json.dumps(REVISION_BODY).encode() if method == "POST" else b"",
                    ))
                    self.assertEqual(response.status, 200, response.body)
                    self.assertIs(forwarded.call_args.kwargs["include_mail_subject"], expected)

    def test_subject_flag_does_not_expand_legacy_mailbox_or_mutation_queries(self):
        denied = (
            ("GET", self.base.replace("/messages", "/mailbox"), "address_kind=server&address_id=server_subjects_001&include_mail_subject=true"),
            ("POST", self.base, "include_mail_subject=true"),
            ("DELETE", f"{self.base}/{MESSAGE_ID}", "include_mail_subject=true"),
            ("POST", f"{self.base}/{MESSAGE_ID}/revisions", "version=2&include_mail_subject=true"),
            ("GET", f"{self.base}/{MESSAGE_ID}/revisions", "include_mail_subject=true"),
            ("GET", f"{self.base}/{MESSAGE_ID}/revisions", "include_revision=true&include_mail_subject=true"),
        )
        for method, path, query in denied:
            with self.subTest(method=method, path=path, query=query):
                with self.assertRaises(SecurePeerError):
                    self.request(method, path, query, b"{}" if method in {"POST", "DELETE"} else b"")

    def test_duplicate_unknown_and_noncanonical_flags_fail_before_store_calls(self):
        for query in (
            "include_mail_subject=true&include_mail_subject=false",
            "include_mail_subject=yes",
            "include_mail_subject=TRUE",
            "include_mail_subject=",
            "include_mail_subject=true&unknown=1",
        ):
            with self.subTest(query=query):
                with self.assertRaises(SecurePeerError):
                    self.request("GET", f"{self.base}/{MESSAGE_ID}", query)
                response = self.adapter.forward(ProxyRequest(
                    "GET", f"{self.base}/{MESSAGE_ID}", query, (), b"", self.peer
                ))
                self.assertEqual(response.status, 422, response.body)
        self.store.get_team_message.assert_not_called()


def isolated_runtime():
    tree = ast.parse((REPO / "secure_peer_runtime.py").read_text())
    source = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    names = {"_team_host_call", "_team_hub_get", "team_list_messages", "team_get_message", "team_send_message"}
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=[
        node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names
    ])
    module = ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), selected,
    ], type_ignores=[])
    namespace = {"quote": quote, "urlencode": urlencode, "SecurePeerError": SecurePeerError, "Mapping": Mapping, "Path": Path}
    exec(compile(ast.fix_missing_locations(module), "<isolated-mail-subject-runtime>", "exec"), namespace)
    return namespace["Runtime"]


class MailSubjectRuntimeRoutesTests(unittest.TestCase):
    def setUp(self):
        self.runtime = isolated_runtime()()
        self.runtime._guard = threading.RLock()
        self.store = mock.Mock(hub_id="subject-hub-001")
        self.runtime._hub_store = self.store
        self.runtime.team_realm = mock.Mock()
        self.runtime.proxy = mock.Mock(return_value={"ok": True})
        self.runtime._decoded_proxy_json = mock.Mock(side_effect=lambda response, **_: response)
        self.runtime._team_upload_attachment = mock.Mock(return_value="subject-attachment-001")
        self.runtime._team_hub_post = mock.Mock(return_value={"ok": True})

    def test_default_and_explicit_opt_in_use_existing_peer_read_without_probe(self):
        self.runtime.team_realm.return_value = {
            "realm": "secure_peer", "team_id": "subject-team-001", "connection_id": str(uuid.uuid4()),
        }
        for operation in (
            lambda **kwargs: self.runtime.team_list_messages(box="inbox", **kwargs),
            lambda **kwargs: self.runtime.team_get_message(MESSAGE_ID, **kwargs),
        ):
            self.runtime.proxy.reset_mock()
            operation()
            self.assertNotIn("include_mail_subject", parse_qs(self.runtime.proxy.call_args.kwargs["query"]))
            operation(include_mail_subject=False)
            self.assertNotIn("include_mail_subject", parse_qs(self.runtime.proxy.call_args.kwargs["query"]))
            operation(include_mail_subject=True)
            self.assertEqual(parse_qs(self.runtime.proxy.call_args.kwargs["query"])["include_mail_subject"], ["1"])
            self.assertEqual(self.runtime.proxy.call_count, 3)

    def test_host_read_receives_same_projection_choice(self):
        self.runtime.team_realm.return_value = {
            "realm": "host", "team_id": "subject-team-001", "hub_id": "subject-hub-001",
        }
        for operation, name in (
            (lambda **kwargs: self.runtime.team_list_messages(box="inbox", **kwargs), "list_team_messages"),
            (lambda **kwargs: self.runtime.team_get_message(MESSAGE_ID, **kwargs), "get_team_message"),
        ):
            method = getattr(self.store, name)
            method.return_value = {"ok": True}
            operation()
            self.assertIs(method.call_args.kwargs["include_mail_subject"], False)
            operation(include_mail_subject=True)
            self.assertIs(method.call_args.kwargs["include_mail_subject"], True)
        self.runtime.proxy.assert_not_called()

    def send(self, **payload):
        return self.runtime.team_send_message(
            {"team_id": "subject-team-001", "recipient_kind": "all"},
            payload={"kind": "message", "body": "Mail body", **payload},
            attachment_paths=["/isolated/mock-only-attachment"],
            idempotency_key="subject-send-request", provenance={},
        )

    def set_realm(self, realm):
        self.runtime.team_realm.return_value = {
            "realm": realm, "team_id": "subject-team-001", "hub_id": "subject-hub-001",
            "connection_id": "subject-connection-001", "can_write": True,
        }

    def test_supported_actual_host_capability_is_checked_once_before_upload_and_preserves_subject(self):
        with tempfile.TemporaryDirectory(prefix="mail-subject-runtime-health-") as temporary:
            health = HubStore(Path(temporary) / "hub").health()
        self.assertEqual(health["capabilities"]["team_mail_subjects_v1"], {
            "available": True, "version": 1, "max_subject_chars": 160,
        })
        self.assertNotIn("team_mail_subjects_v1", health)
        for realm in ("host", "secure_peer"):
            with self.subTest(realm=realm):
                self.set_realm(realm)
                self.store.health.return_value = health
                self.runtime.proxy.return_value = health
                calls = mock.Mock()
                calls.attach_mock(self.store.health if realm == "host" else self.runtime.proxy, "health")
                calls.attach_mock(self.runtime._team_upload_attachment, "upload")
                calls.attach_mock(self.runtime._team_hub_post, "post")
                self.send(title=" Subject retained ")
                self.assertEqual([item[0] for item in calls.mock_calls], ["health", "upload", "post"])
                self.assertEqual(self.runtime._team_hub_post.call_args.args[-1]["title"], " Subject retained ")
                if realm == "secure_peer":
                    self.assertEqual(self.runtime.proxy.call_args.args, ("subject-connection-001", "GET", "/v1/health"))
                    self.assertEqual(self.runtime.proxy.call_args.kwargs["query"], "")

    def test_missing_old_or_malformed_capability_fails_before_any_upload_or_post(self):
        valid = {"available": True, "version": 1, "max_subject_chars": 160}
        bad_health = [
            None, [], {}, {"team_mail_subjects_v1": valid},
            {"capabilities": None}, {"capabilities": []}, {"capabilities": {}},
            {"capabilities": {"team_messages_v1": {"team_mail_subjects_v1": valid}}},
        ]
        bad_health += [{"capabilities": {"team_mail_subjects_v1": value}} for value in (
            None, [], {}, {**valid, "available": 1}, {**valid, "available": False},
            {**valid, "version": True}, {**valid, "version": "1"}, {**valid, "version": 2},
            {**valid, "max_subject_chars": 160.0}, {**valid, "max_subject_chars": "160"},
        )]
        for realm in ("host", "secure_peer"):
            self.set_realm(realm)
            for health in bad_health:
                with self.subTest(realm=realm, health=health):
                    self.store.health.return_value = health
                    self.runtime.proxy.return_value = health
                    with self.assertRaises(SecurePeerError) as error:
                        self.send(title="Subject")
                    self.assertEqual(error.exception.code, "mail_subjects_unavailable")
                    self.assertEqual(error.exception.status_code, 409)
                    self.runtime._team_upload_attachment.assert_not_called()
                    self.runtime._team_hub_post.assert_not_called()

    def test_mismatched_local_hub_cannot_authorize_a_subject_write(self):
        self.set_realm("host")
        self.store.hub_id = "different-subject-hub"
        self.store.health.return_value = {"capabilities": {"team_mail_subjects_v1": {"available": True, "version": 1, "max_subject_chars": 160}}}
        with self.assertRaises(SecurePeerError) as error:
            self.send(title="Subject")
        self.assertEqual(error.exception.code, "mail_subjects_unavailable")
        self.store.health.assert_not_called()
        self.runtime.proxy.assert_not_called()
        self.runtime._team_upload_attachment.assert_not_called()
        self.runtime._team_hub_post.assert_not_called()

    def test_untitled_messages_and_skill_titles_do_not_probe_mail_capability(self):
        for realm in ("host", "secure_peer"):
            self.set_realm(realm)
            self.send()
            self.send(title=None)
            self.send(kind="skill", title="Skill title", skill={"slug": "subject-test"})
        self.store.health.assert_not_called()
        self.runtime.proxy.assert_not_called()

    def test_empty_explicit_subject_is_not_silently_dropped(self):
        self.set_realm("secure_peer")
        self.runtime.proxy.return_value = {"capabilities": {"team_mail_subjects_v1": {"available": True, "version": 1, "max_subject_chars": 160}}}
        self.send(title="")
        self.assertEqual(self.runtime._team_hub_post.call_args.args[-1]["title"], "")

    def test_non_boolean_runtime_option_is_rejected_before_any_read(self):
        with self.assertRaises(SecurePeerError):
            self.runtime.team_list_messages(box="inbox", include_mail_subject="true")
        with self.assertRaises(SecurePeerError):
            self.runtime.team_get_message(MESSAGE_ID, include_mail_subject=1)
        self.runtime.team_realm.assert_not_called()
        self.runtime.proxy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
