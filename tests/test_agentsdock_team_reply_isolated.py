"""Scoped Team reply CLI tests with mocked authority, stdin, and transport."""

import contextlib
import hashlib
import io
import json
import unittest
from unittest import mock

import agentsdock_team as cli


PARENT = "tmsg_parent_0001"
ROUTE = "team_frozen_sender_route"
CAPABILITY = "isolated-provider-capability"


class TeamReplyCLITests(unittest.TestCase):
    def setUp(self):
        self.authority = self.enterContext(mock.patch.object(
            cli, "_provider_authority", return_value=(CAPABILITY, "isolated-session")
        ))
        self.body = self.enterContext(mock.patch.object(cli, "_read_body", return_value="Reply body"))
        self.receipt = {
            "ok": True, "route_id": ROUTE, "message_id": "tmsg_created_reply",
            "kind": "message", "accepted": True, "duplicate": False, "attachments": [],
        }
        self.request = self.enterContext(mock.patch.object(cli, "_request_json", return_value=self.receipt))

    def execute(self, *argv):
        args = cli.parser().parse_args(list(argv))
        return args.handler(args)

    def payload(self):
        return self.request.call_args.args[3]

    def test_reply_reuses_send_route_receipt_and_stdin_without_inferred_authority(self):
        result = self.execute("reply", PARENT, "--route", ROUTE)
        self.assertIs(result, self.receipt)
        self.authority.assert_called_once_with(None)
        self.body.assert_called_once_with()
        self.request.assert_called_once()
        self.assertEqual(self.request.call_args.args[:3], (
            "POST", f"/api/agent/team/routes/{ROUTE}", CAPABILITY,
        ))
        self.assertEqual(self.request.call_args.kwargs, {"timeout": 900.0})
        self.assertEqual(self.payload()["kind"], "message")
        self.assertEqual(self.payload()["in_reply_to_message_id"], PARENT)
        self.assertEqual(self.payload()["body"], "Reply body")
        self.assertNotIn("title", self.payload())
        self.assertNotIn("recipients", self.payload())

    def test_reply_and_send_parent_option_have_identical_payload_and_idempotency(self):
        self.execute("reply", PARENT, "--route", ROUTE, "--title", " Subject ")
        reply_payload = dict(self.payload())
        self.execute("send", "--route", ROUTE, "--in-reply-to", PARENT, "--title", " Subject ")
        self.assertEqual(self.payload(), reply_payload)
        self.execute("reply", "tmsg_parent_0002", "--route", ROUTE, "--title", " Subject ")
        self.assertNotEqual(self.payload()["idempotency_key"], reply_payload["idempotency_key"])

    def test_parent_is_absent_from_ordinary_send_and_its_existing_idempotency_is_unchanged(self):
        self.execute("send", "--route", ROUTE)
        expected = {"kind": "message", "body": "Reply body", "body_format": "markdown", "attachments": []}
        stable_key = "team_cli_" + hashlib.sha256(json.dumps(
            [CAPABILITY, ROUTE, expected], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")).hexdigest()
        self.assertEqual(self.payload(), {**expected, "idempotency_key": stable_key})

    def test_reply_preserves_shared_subject_attachment_and_explicit_idempotency_options(self):
        with mock.patch.object(cli, "_attachment_paths", return_value=["/isolated/resolved-file"]) as paths:
            self.execute(
                "reply", PARENT, "--route", ROUTE, "--title", " " + "é" * 160 + " ",
                "--attach", "/isolated/mock-file", "--idempotency-key", "explicit-reply-key",
            )
        paths.assert_called_once_with(["/isolated/mock-file"])
        self.assertEqual(self.payload()["title"], "é" * 160)
        self.assertEqual(self.payload()["attachments"], ["/isolated/resolved-file"])
        self.assertEqual(self.payload()["idempotency_key"], "explicit-reply-key")

    def test_invalid_parent_ids_fail_before_body_or_request(self):
        for parent in ("", "short_7", "x" * 241, "tmsg/parent", "tmsg.parent", "tmsg_é001", " tmsg_parent", "tmsg_parent\n"):
            with self.subTest(parent=repr(parent)), self.assertRaises(cli.TeamCLIError):
                self.execute("reply", parent, "--route", ROUTE)
        self.body.assert_not_called()
        self.request.assert_not_called()

    def test_parent_id_bounds_allow_exactly_eight_and_240_safe_characters(self):
        for parent in ("tmsg_001", "tmsg_" + "A-0_" * 58 + "ABC"):
            self.execute("reply", parent, "--route", ROUTE)
            self.assertEqual(self.payload()["in_reply_to_message_id"], parent)

    def test_skill_send_cannot_add_reply_linkage(self):
        with self.assertRaisesRegex(cli.TeamCLIError, "requires --kind message"):
            self.execute("send", "--route", ROUTE, "--kind", "skill", "--in-reply-to", PARENT)
        self.body.assert_not_called()
        self.request.assert_not_called()

    def test_reply_requires_explicit_route_and_does_not_offer_skill_or_reply_all(self):
        for args in (
            ("reply", PARENT),
            ("reply", PARENT, "--route", ROUTE, "--kind", "skill"),
            ("reply", PARENT, "--route", ROUTE, "--reply-all"),
        ):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                cli.parser().parse_args(args)
            self.assertEqual(error.exception.code, 2)
        self.authority.assert_not_called()
        self.body.assert_not_called()
        self.request.assert_not_called()
        help_output = io.StringIO()
        with contextlib.redirect_stdout(help_output), self.assertRaises(SystemExit):
            cli.parser().parse_args(["reply", "--help"])
        text = help_output.getvalue()
        self.assertIn("explicit @@ sender mention", text)
        self.assertIn("no reply-all", text)
        self.assertIn("stdin", text)

    def test_reply_uses_existing_subject_validation_before_stdin(self):
        for title in ("", "  ", "x" * 161, "Subject\n", "\tSubject", "Subject\u2028line"):
            with self.subTest(title=repr(title)), self.assertRaises(cli.TeamCLIError):
                self.execute("reply", PARENT, "--route", ROUTE, "--title", title)
        self.body.assert_not_called()
        self.request.assert_not_called()

    def test_reply_keeps_existing_send_receipt_validation(self):
        for update in ({"accepted": False}, {"route_id": "different-route"}, {"kind": "skill"}, {"duplicate": 1}):
            self.request.return_value = {**self.receipt, **update}
            with self.subTest(update=update), self.assertRaisesRegex(cli.TeamCLIError, "invalid Team Network send receipt"):
                self.execute("reply", PARENT, "--route", ROUTE)


if __name__ == "__main__":
    unittest.main()
