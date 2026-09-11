import argparse
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

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

    def test_live_post_returns_lease_on_bounded_transport_timeout(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"ok": True}).encode("utf-8")

        class FakeOpener:
            timeout = None

            def open(self, _request, timeout):
                self.timeout = timeout
                return FakeResponse()

        opener = FakeOpener()
        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=opener),
        ):
            agentsdock_chats.post_json(
                "/api/agent/cross-chat/handoffs",
                {
                    "wait_for_response": True,
                    "response_timeout_seconds": 120,
                },
                "capability",
            )
        self.assertEqual(opener.timeout, 10)

    def test_ask_returns_resumable_receipt_without_blocking_provider_call(self) -> None:
        exchange_id = "exchange_" + "1" * 32
        inbound_leg_id = "leg_" + "2" * 32
        lease_id = "lease_" + "3" * 32
        post = Mock(return_value={
            "ok": True,
            "action": "request_reply",
            "accepted": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": inbound_leg_id,
            "live_response_lease_id": lease_id,
        })
        args = argparse.Namespace(
            authority_file="authority.json",
            route=None,
            target="grant_" + "b" * 64,
            message="Question",
            idempotency_key=None,
            timeout_seconds=75,
        )
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats, "get_json") as get,
        ):
            result = agentsdock_chats.ask(args)
        self.assertTrue(result["pending"])
        self.assertEqual(result["exchange_id"], exchange_id)
        self.assertEqual(result["inbound_leg_id"], inbound_leg_id)
        self.assertEqual(result["live_response_lease_id"], lease_id)
        get.assert_not_called()

    def test_ask_rejects_server_without_live_wait_support(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            route="route_" + "a" * 32,
            target=None,
            message="Question",
            idempotency_key=None,
            timeout_seconds=75,
        )
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(
                agentsdock_chats,
                "post_json",
                return_value={
                    "ok": True,
                    "route_id": args.route,
                    "action": "request_reply",
                    "accepted": True,
                },
            ),
        ):
            with self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                "does not support a live response",
            ):
                agentsdock_chats.ask(args)

    def test_secure_peer_ask_can_explicitly_use_async_response_delivery(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            route=None,
            target="route_" + "a" * 32,
            message="Question for peer server",
            idempotency_key=None,
            timeout_seconds=75,
            async_response=True,
        )
        receipt = {
            "ok": True,
            "action": "request_reply",
            "accepted": True,
        }
        post = Mock(return_value=receipt)
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
        ):
            self.assertEqual(agentsdock_chats.ask(args), receipt)
        payload = post.call_args.args[1]
        self.assertNotIn("wait_for_response", payload)
        self.assertNotIn("response_timeout_seconds", payload)

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
                "exchange_id": "exchange_live",
                "inbound_leg_id": "leg_live_answer",
                "body": "Investigation complete",
                "request_response": False,
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
        self.assertTrue(calls[0][1]["wait_for_response"])
        self.assertEqual(calls[0][1]["response_timeout_seconds"], 20)
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
            receipt = {"ok": True, "action": "response", "accepted": True}
            if payload["request_response"]:
                receipt.update({
                    "exchange_id": "exchange_one",
                    "inbound_leg_id": "leg_two",
                    "body": "follow-up answer",
                    "request_response": False,
                })
            return receipt

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
        self.assertTrue(payloads[1]["wait_for_response"])
        self.assertEqual(payloads[1]["response_timeout_seconds"], 20)
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

    def test_secure_peer_followup_can_explicitly_remain_async(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            exchange="exchange_peer",
            inbound_leg="envelope_peer",
            message="Question back to peer",
            request_response=True,
            async_response=True,
            idempotency_key=None,
            timeout_seconds=75,
        )
        receipt = {"ok": True, "action": "response", "accepted": True}
        post = Mock(return_value=receipt)
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
        ):
            self.assertEqual(agentsdock_chats.respond(args), receipt)
        payload = post.call_args.args[1]
        self.assertTrue(payload["request_response"])
        self.assertNotIn("wait_for_response", payload)
        self.assertNotIn("response_timeout_seconds", payload)

    def test_live_and_async_followups_use_distinct_retry_keys(self) -> None:
        base = dict(
            authority_file="authority.json",
            exchange="exchange_peer",
            inbound_leg="envelope_peer",
            message="Question back to peer",
            request_response=True,
            idempotency_key=None,
            timeout_seconds=75,
        )
        payloads = []

        def post(_path, payload, _capability):
            payloads.append(payload)
            if payload.get("wait_for_response"):
                return {
                    "ok": True,
                    "action": "response",
                    "accepted": True,
                    "exchange_id": "exchange_peer",
                    "inbound_leg_id": "envelope_reply",
                    "body": "Peer reply",
                    "request_response": False,
                }
            return {"ok": True, "action": "response", "accepted": True}

        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", side_effect=post),
        ):
            agentsdock_chats.respond(
                argparse.Namespace(**base, async_response=False)
            )
            agentsdock_chats.respond(
                argparse.Namespace(**base, async_response=True)
            )
        self.assertNotEqual(
            payloads[0]["idempotency_key"],
            payloads[1]["idempotency_key"],
        )

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

    def test_live_wait_returns_resumable_receipt_after_one_pending_slice(self) -> None:
        exchange_id = "exchange_" + "7" * 32
        inbound_leg_id = "leg_" + "8" * 32
        lease_id = "lease_" + "9" * 32
        pending = {
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": inbound_leg_id,
            "pending": True,
        }
        get = Mock(return_value=pending)

        with patch.object(agentsdock_chats, "get_json", get):
            result = agentsdock_chats.await_live_response(
                {
                    "exchange_id": exchange_id,
                    "inbound_leg_id": inbound_leg_id,
                    "live_response_lease_id": lease_id,
                },
                "capability",
                3600,
            )

        self.assertEqual(result, {**pending, "live_response_lease_id": lease_id})
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["timeout"], 30)
        self.assertTrue(get.call_args.kwargs["live_slice"])
        self.assertIn("timeout_seconds=20", get.call_args.args[0])

    def test_live_wait_transport_loss_preserves_exact_resumable_lease(self) -> None:
        exchange_id = "exchange_" + "1" * 32
        inbound_leg_id = "leg_" + "2" * 32
        lease_id = "lease_" + "3" * 32
        with patch.object(
            agentsdock_chats,
            "get_json",
            side_effect=agentsdock_chats.LiveWaitRetryable("proxy restarted"),
        ) as get:
            result = agentsdock_chats.await_live_response(
                {
                    "exchange_id": exchange_id,
                    "inbound_leg_id": inbound_leg_id,
                    "live_response_lease_id": lease_id,
                },
                "capability",
                20,
            )

        self.assertEqual(result, {
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": inbound_leg_id,
            "pending": True,
            "live_response_lease_id": lease_id,
        })
        get.assert_called_once()

    def test_wait_rejects_noncanonical_resume_identifier_before_http(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            exchange="../../admin",
            inbound_leg="leg_" + "2" * 32,
            lease="lease_" + "3" * 32,
            timeout_seconds=20,
        )
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "get_json") as get,
            self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                "exchange id is invalid",
            ),
        ):
            agentsdock_chats.wait(args)
        get.assert_not_called()

    def test_cli_ask_and_wait_are_separate_bounded_provider_calls(self) -> None:
        route_id = "route_" + "a" * 32
        exchange_id = "exchange_" + "b" * 32
        inbound_leg_id = "leg_" + "c" * 32
        answer_leg_id = "leg_" + "d" * 32
        lease_id = "lease_" + "e" * 32
        post = Mock(return_value={
            "ok": True,
            "route_id": route_id,
            "action": "request_reply",
            "accepted": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": inbound_leg_id,
            "live_response_lease_id": lease_id,
        })
        get = Mock(return_value={
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": answer_leg_id,
            "body": "Peer answer",
            "request_response": False,
        })
        stdout = io.StringIO()
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats, "get_json", get),
            patch.object(agentsdock_chats.sys, "stdout", stdout),
        ):
            self.assertEqual(agentsdock_chats.main([
                "--authority-file", "authority.json",
                "ask", "--route", route_id,
                "--message", "Wait for the peer",
            ]), 0)
            receipt = json.loads(stdout.getvalue())
            self.assertTrue(receipt["pending"])
            get.assert_not_called()

            stdout.seek(0)
            stdout.truncate(0)
            self.assertEqual(agentsdock_chats.main([
                "--authority-file", "authority.json",
                "wait",
                "--exchange", receipt["exchange_id"],
                "--inbound-leg", receipt["inbound_leg_id"],
                "--lease", receipt["live_response_lease_id"],
            ]), 0)

        answer = json.loads(stdout.getvalue())
        self.assertEqual(answer["body"], "Peer answer")
        post.assert_called_once()
        get.assert_called_once()

    def test_live_get_transport_failure_ends_one_slice_without_retrying(self) -> None:
        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, timeout):
                del timeout
                self.calls += 1
                raise urllib.error.URLError(ConnectionResetError("proxy reset"))

        opener = FakeOpener()
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
            self.assertRaises(agentsdock_chats.LiveWaitRetryable),
        ):
            agentsdock_chats.get_json(
                "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response",
                "capability",
                timeout=30,
                live_slice=True,
            )

        self.assertEqual(opener.calls, 1)
        sleep.assert_not_called()

    def test_live_get_explicit_cancel_is_terminal_not_pending(self) -> None:
        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                del timeout
                self.calls += 1
                raise urllib.error.HTTPError(
                    request.full_url,
                    410,
                    "Gone",
                    {},
                    io.BytesIO(b'{"detail":"cancelled_by_user"}'),
                )

        opener = FakeOpener()
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
            self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                r"\(410\): cancelled_by_user",
            ),
        ):
            agentsdock_chats.get_json(
                "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response",
                "capability",
                live_slice=True,
            )

        self.assertEqual(opener.calls, 1)

    def test_followup_returns_pending_receipt_without_waiting_inline(self) -> None:
        exchange_id = "exchange_" + "1" * 32
        inbound_leg_id = "leg_" + "2" * 32
        next_leg_id = "leg_" + "3" * 32
        lease_id = "lease_" + "4" * 32
        args = argparse.Namespace(
            authority_file="authority.json",
            exchange=exchange_id,
            inbound_leg=inbound_leg_id,
            message="One more question",
            request_response=True,
            async_response=False,
            idempotency_key=None,
            timeout_seconds=3600,
        )
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(
                agentsdock_chats,
                "post_json",
                return_value={
                    "ok": True,
                    "action": "response",
                    "accepted": True,
                    "exchange_id": exchange_id,
                    "inbound_leg_id": next_leg_id,
                    "live_response_lease_id": lease_id,
                },
            ) as post,
            patch.object(agentsdock_chats, "get_json") as get,
        ):
            result = agentsdock_chats.respond(args)

        self.assertTrue(result["pending"])
        self.assertEqual(result["live_response_lease_id"], lease_id)
        self.assertEqual(post.call_args.args[1]["response_timeout_seconds"], 20)
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
