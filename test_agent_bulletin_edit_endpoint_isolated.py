"""Bulletin edits use existing live route fences; no server import or network."""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import unittest
from unittest import mock

from fastapi import HTTPException
from pydantic import ValidationError

import test_agent_team_reply_endpoint_isolated as endpoint_helpers
import test_team_mail_subject_helper_isolated as read_helpers


MESSAGE = "tmsg_bulletin_original"


class BulletinEditEndpointTests(unittest.TestCase):
    def setUp(self):
        self.fixture = endpoint_helpers.TeamReplyEndpointTests()
        self.fixture.setUp()
        self.fixture.reference.update(recipient_kind="all")
        self.fixture.runtime.team_send_message.return_value = {"message": {
            "id": MESSAGE, "kind": "message", "body": "Corrected bulletin",
            "revision": {"version": 2}, "attachments": [{"id": "original-attachment"}],
            "recipients": [{"kind": "all"}],
        }}

    def edit(self, **changes):
        return self.fixture.send(**{
            "kind": "bulletin_edit", "message_id": MESSAGE, "expected_version": 1,
            "in_reply_to_message_id": None, "body": "Corrected bulletin", **changes,
        })

    def test_exact_revision_receipt_and_retry_do_not_create_another_sent_event(self):
        receipt = self.edit()
        self.assertEqual(receipt["message_id"], MESSAGE)
        self.assertEqual(receipt["version"], 2)
        self.assertTrue(receipt["edited"])
        self.assertEqual(receipt["kind"], "bulletin_edit")
        self.assertEqual(receipt["attachments"], 1)
        self.assertEqual(self.edit(), {**receipt, "duplicate": True})
        self.fixture.runtime.team_authorized_write.assert_called_once()
        call = self.fixture.runtime.team_send_message.call_args
        self.assertEqual(call.kwargs["payload"]["message_id"], MESSAGE)
        self.assertEqual(call.kwargs["payload"]["expected_version"], 1)
        self.fixture.events.assert_not_called()

    def test_private_mail_broadcast_mail_and_skill_routes_cannot_edit_bulletin(self):
        for kind, recipient in (("recipient", "server"), ("recipient", "all_servers"), ("skill", "all")):
            with self.subTest(kind=kind, recipient=recipient):
                self.fixture.reference.update(kind=kind, recipient_kind=recipient)
                with self.assertRaises(HTTPException) as error:
                    self.edit()
                self.assertEqual(error.exception.status_code, 409)
                self.fixture.assert_no_runtime_call()

    def test_edit_requires_current_write_authority_and_live_owner(self):
        self.fixture.live["actions"] = {"team_read"}
        with self.assertRaises(HTTPException) as error:
            self.edit()
        self.assertEqual(error.exception.status_code, 403)
        self.fixture.live["actions"] = {"team_read", "team_send"}
        self.fixture.attached.return_value = False
        with self.assertRaises(HTTPException) as error:
            self.edit()
        self.assertEqual(error.exception.status_code, 403)
        self.fixture.assert_no_runtime_call()

    def test_model_requires_exact_identity_version_and_body_only_edit(self):
        for fields in ({"message_id": None}, {"message_id": "bad/id"},
                       {"expected_version": None}, {"expected_version": 0},
                       {"expected_version": True}, {"expected_version": "1"},
                       {"title": "Replacement"}, {"attachments": ["/unused"]},
                       {"skill": {}}, {"in_reply_to_message_id": "tmsg_other_message"}):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                self.edit(**fields)
        for kind in ("message", "skill"):
            with self.assertRaises(ValidationError):
                self.edit(kind=kind)
        self.fixture.assert_no_runtime_call()

    def test_hub_error_never_falls_back_to_new_post(self):
        self.fixture.runtime.team_send_message.side_effect = HTTPException(409, "Reload the changed post")
        with self.assertRaises(HTTPException) as error:
            self.edit()
        self.assertEqual(error.exception.status_code, 409)
        self.fixture.runtime.team_send_message.assert_called_once()
        self.fixture.events.assert_not_called()
        self.assertEqual(self.fixture.live["team_send_count"], 0)

    def test_invalid_revision_receipt_is_not_reported_as_accepted(self):
        for changes in ({"id": "tmsg_another_message"}, {"revision": {"version": 1}},
                        {"revision": {"version": True}}, {"kind": "skill"}):
            with self.subTest(changes=changes):
                self.setUp()
                self.fixture.runtime.team_send_message.return_value["message"].update(changes)
                with self.assertRaises(HTTPException) as error:
                    self.edit()
                self.assertEqual(error.exception.status_code, 502)
                self.fixture.events.assert_not_called()

    def test_old_request_kind_contract_rejects_edit_instead_of_posting(self):
        # The explicit new discriminator is essential: extra optional fields
        # alone would be ignored by older Pydantic models and create a post.
        namespace = endpoint_helpers.extract_endpoint()
        source = 'class LegacyRequest(BaseModel):\n kind: Literal["message", "skill"] = "message"\n body: str\n'
        exec(compile(ast.parse(source), "legacy-message-discriminator", "exec"), namespace)
        with self.assertRaises(ValidationError):
            namespace["LegacyRequest"](kind="bulletin_edit", body="Correction", message_id=MESSAGE, expected_version=1)

    def test_read_revision_option_retains_existing_read_only_fence(self):
        namespace = read_helpers.extracted()
        runtime = mock.Mock()
        runtime.team_authorized_read.return_value = {"message": {"id": MESSAGE, "revision": {"version": 1}}}
        authorize = mock.AsyncMock(return_value=("synthetic", "chat", {"team_authority_generation": "generation"}))
        namespace.update(SECURE_PEER_RUNTIME=runtime, provider_team_capability=authorize)
        asyncio.run(namespace["get_provider_team_message"](MESSAGE, object(), include_revision=True))
        call = runtime.team_authorized_read.call_args
        self.assertTrue(call.kwargs["include_revision"])
        self.assertEqual(call.args[0], "generation")
        self.assertEqual(authorize.call_args.args[1], "team_read")

    def test_actual_cli_endpoint_and_runtime_revise_one_synthetic_hub_post(self):
        import agentsdock_team as cli
        import test_agent_bulletin_edit_helper_runtime_isolated as runtime_helpers

        runtime_fixture = runtime_helpers.BulletinEditRuntimeTests()
        runtime_fixture.setUp()
        self.addCleanup(runtime_fixture.doCleanups)
        original = runtime_fixture.create()
        self.fixture.reference["team_id"] = runtime_fixture.team
        self.fixture.runtime.team_send_message.side_effect = runtime_fixture.runtime.team_send_message

        def forward(method, path, _capability, payload, **_options):
            self.assertEqual(method, "POST")
            self.assertEqual(path, f"/api/agent/team/routes/{endpoint_helpers.ROUTE}")
            request = self.fixture.namespace["AgentTeamSendRequest"](**payload)
            return asyncio.run(self.fixture.namespace["send_provider_team_message"](
                endpoint_helpers.ROUTE, request, self.fixture.request))

        args = cli.parser().parse_args(["edit", original["id"], "--route", endpoint_helpers.ROUTE, "--expected-version", "1"])
        with mock.patch.object(cli, "_provider_authority", return_value=("synthetic", "chat")), \
                mock.patch.object(cli, "_read_body", return_value="One revised post, not two"), \
                mock.patch.object(cli, "_request_json", side_effect=forward):
            receipt = args.handler(args)
            self.assertEqual(args.handler(args), {**receipt, "duplicate": True})
        self.assertEqual((receipt["message_id"], receipt["version"], receipt["attachments"]), (original["id"], 2, 0))
        visible = runtime_fixture.store.list_team_messages(runtime_fixture.host, runtime_fixture.team, box="feed")["messages"]
        self.assertEqual([item["id"] for item in visible], [original["id"]])
        history = runtime_fixture.store.list_team_message_revisions(runtime_fixture.host, runtime_fixture.team, original["id"])
        self.assertEqual([item["version"] for item in history["versions"]], [2, 1])
        self.fixture.events.assert_not_called()


if __name__ == "__main__":
    unittest.main()
