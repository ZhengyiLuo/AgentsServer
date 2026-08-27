import argparse
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

import agentsdock_chats


class AgentsDockChatsCLITests(unittest.TestCase):
    def test_post_retries_only_the_native_promotion_window(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"ok": True}).encode("utf-8")

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                if len(self.requests) == 1:
                    raise urllib.error.HTTPError(
                        request.full_url,
                        409,
                        "Conflict",
                        {},
                        io.BytesIO(json.dumps({
                            "detail": (
                                "agent chat access is waiting for turn "
                                "promotion"
                            ),
                        }).encode("utf-8")),
                    )
                return FakeResponse()

        opener = FakeOpener()
        payload = {
            "body": "hello",
            "idempotency_key": "stable-key",
        }
        with (
            patch.object(
                agentsdock_chats,
                "environment",
                return_value="http://127.0.0.1:7850",
            ),
            patch.object(
                agentsdock_chats.urllib.request,
                "build_opener",
                return_value=opener,
            ),
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            result = agentsdock_chats.post_json(
                "/api/agent/cross-chat/routes/route/handoffs",
                payload,
                "capability",
            )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0][0], opener.requests[1][0])
        self.assertEqual(
            json.loads(opener.requests[0][0].data.decode("utf-8")),
            payload,
        )
        sleep.assert_called_once_with(0.05)

    def test_list_uses_capability_scoped_route_endpoint(self) -> None:
        args = argparse.Namespace(authority_file="authority.json")
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(
                agentsdock_chats,
                "get_json",
                return_value={"routes": [], "max_handoffs_per_run": 4},
            ) as get,
        ):
            result = agentsdock_chats.list_routes(args)
        self.assertEqual(result["routes"], [])
        get.assert_called_once_with(
            "/api/agent/cross-chat/routes",
            "capability",
        )

    def test_ask_uses_request_reply_wire_and_stable_retry_key(self) -> None:
        handle = "grant_" + "a" * 64
        args = argparse.Namespace(
            authority_file="authority.json",
            target=handle,
            message="  investigate this  ",
            idempotency_key=None,
        )
        calls = []

        def post(path, payload, capability):
            calls.append((path, payload, capability))
            return {
                "ok": True,
                "action": "request_reply",
                "accepted": True,
            }

        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", side_effect=post),
        ):
            first = agentsdock_chats.ask(args)
            second = agentsdock_chats.ask(args)
        self.assertEqual(first, second)
        self.assertEqual(calls[0][0], "/api/agent/cross-chat/handoffs")
        self.assertEqual(calls[0][1]["action"], "request_reply")
        self.assertEqual(calls[0][1]["body"], "investigate this")
        self.assertEqual(calls[0][1]["target_session_id"], handle)
        self.assertEqual(calls[0][1]["idempotency_key"], calls[1][1]["idempotency_key"])

    def test_direct_send_rejects_receipt_with_internal_identifiers(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            target="grant_" + "b" * 64,
            message="check",
            idempotency_key=None,
        )
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(
                agentsdock_chats,
                "post_json",
                return_value={
                    "ok": True,
                    "action": "instruction",
                    "accepted": True,
                    "target_session_id": "must-not-leak",
                },
            ),
        ):
            with self.assertRaises(agentsdock_chats.ChatsCLIError):
                agentsdock_chats.send(args)

    def test_respond_has_no_target_and_request_response_changes_stable_key(self) -> None:
        base = dict(
            authority_file="authority.json",
            exchange="exchange_one",
            inbound_leg="leg_one",
            message="answer",
            idempotency_key=None,
        )
        payloads = []

        def post(_path, payload, _capability):
            payloads.append(payload)
            return {"ok": True, "action": "response", "accepted": True}

        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", side_effect=post),
        ):
            agentsdock_chats.respond(argparse.Namespace(**base, request_response=False))
            agentsdock_chats.respond(argparse.Namespace(**base, request_response=True))
        self.assertNotIn("target_session_id", payloads[0])
        self.assertEqual(payloads[0]["inbound_leg_id"], "leg_one")
        self.assertFalse(payloads[0]["request_response"])
        self.assertTrue(payloads[1]["request_response"])
        self.assertNotEqual(payloads[0]["idempotency_key"], payloads[1]["idempotency_key"])

    def test_respond_accepts_only_strict_configured_route_receipt(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            exchange="exchange_private",
            inbound_leg="leg_private",
            message="answer",
            request_response=False,
            idempotency_key="configured-response-key",
        )
        receipt = {"ok": True, "action": "response", "accepted": True}
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", return_value=receipt),
        ):
            self.assertEqual(agentsdock_chats.respond(args), receipt)

        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(
                agentsdock_chats,
                "post_json",
                return_value={**receipt, "exchange": {"id": "must-not-leak"}},
            ),
        ):
            with self.assertRaises(agentsdock_chats.ChatsCLIError):
                agentsdock_chats.respond(args)

    def test_route_send_uses_opaque_route_path_and_no_target(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            route="route_0123456789abcdef0123456789abcdef",
            target=None,
            message="update mobile",
            idempotency_key=None,
        )
        calls = []

        def post(path, payload, capability):
            calls.append((path, payload, capability))
            return {
                "ok": True,
                "route_id": args.route,
                "action": "instruction",
                "accepted": True,
            }

        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", side_effect=post),
        ):
            result = agentsdock_chats.send(args)
        self.assertTrue(result["accepted"])
        self.assertEqual(
            calls[0][0],
            f"/api/agent/cross-chat/routes/{args.route}/handoffs",
        )
        self.assertNotIn("target_session_id", calls[0][1])

    def test_send_parser_requires_exactly_one_route_or_target(self) -> None:
        cli = agentsdock_chats.parser()
        with self.assertRaises(SystemExit):
            cli.parse_args([
                "--authority-file", "authority.json", "send",
                "--message", "hello",
            ])
        with self.assertRaises(SystemExit):
            cli.parse_args([
                "--authority-file", "authority.json", "send",
                "--route", "route_one", "--target", "sess_one",
                "--message", "hello",
            ])

    def test_ask_and_respond_reject_whitespace_messages(self) -> None:
        for handler, args in (
            (
                agentsdock_chats.ask,
                argparse.Namespace(
                    authority_file="authority.json", target="target",
                    message="  ", idempotency_key=None,
                ),
            ),
            (
                agentsdock_chats.respond,
                argparse.Namespace(
                    authority_file="authority.json", exchange="exchange",
                    inbound_leg="leg", message="\n", request_response=False,
                    idempotency_key=None,
                ),
            ),
        ):
            with patch.object(agentsdock_chats, "authority", return_value="capability"):
                with self.assertRaises(agentsdock_chats.ChatsCLIError):
                    handler(args)


if __name__ == "__main__":
    unittest.main()
