import argparse
import http.client
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import agentsdock_chats


class AgentsDockChatsCLITests(unittest.TestCase):
    def test_inbox_lists_one_page_without_claim_or_send(self) -> None:
        args = agentsdock_chats.parser().parse_args(["inbox", "--cursor", "synthetic-previous-sender"])
        page = {"senders": [{"source_session_id": "synthetic-sender", "pending_count": 2}], "next_cursor": None}
        with patch.object(agentsdock_chats, "authority", return_value="capability"), \
                patch.object(agentsdock_chats, "get_json", return_value=page) as get, \
                patch.object(agentsdock_chats, "post_json") as post:
            self.assertEqual(args.handler(args), page)
        get.assert_called_once_with("/api/agent/cross-chat/inbox?cursor=synthetic-previous-sender", "capability")
        post.assert_not_called()

    def test_read_sends_exact_stable_receipt_and_preserves_returned_messages(self) -> None:
        args = agentsdock_chats.parser().parse_args([
            "read", "--sender", "synthetic-sender", "--request-id", "stable-read-request", "--cursor", "42",
        ])
        response = {"messages": [{"message_id": "synthetic-message", "body": "Exact received body"}],
                    "return_route": {"route_id": "synthetic-route"}, "next_cursor": None}
        with patch.object(agentsdock_chats, "authority", return_value="capability"), \
                patch.object(agentsdock_chats, "post_json", return_value=response) as post, \
                patch.object(agentsdock_chats, "get_json") as get:
            self.assertEqual(args.handler(args), response)
            self.assertEqual(args.handler(args), response)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0], post.call_args_list[1])
        self.assertEqual(post.call_args.args, ("/api/agent/cross-chat/inbox/read", {
            "source_session_id": "synthetic-sender", "request_id": "stable-read-request", "after_seq": 42, "limit": 25,
        }, "capability"))
        get.assert_not_called()

    def test_inbox_read_parser_and_identity_bounds(self) -> None:
        from contextlib import redirect_stderr
        for arguments in (["read", "--sender", "synthetic"],
                          ["read", "--sender", "synthetic", "--request-id", "stable-request", "--cursor", "-1"],
                          ["read", "--sender", "synthetic", "--request-id", "stable-request", "--limit", "26"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                agentsdock_chats.parser().parse_args(arguments)
        for sender, request_id in (("", "stable-request"), ("x" * 129, "stable-request"), ("synthetic", "short")):
            args = argparse.Namespace(sender=sender, request_id=request_id, cursor=0, limit=25, authority_file=None)
            with patch.object(agentsdock_chats, "authority") as auth, self.assertRaises(agentsdock_chats.ChatsCLIError):
                agentsdock_chats.read_inbox(args)
            auth.assert_not_called()

    def test_async_reply_parent_is_explicit_and_part_of_default_idempotency(self) -> None:
        route = "route_" + "a" * 32
        parents = ["handoff_" + "b" * 32, "handoff_" + "c" * 32]
        receipt = {"ok": True, "route_id": route, "action": "instruction", "accepted": True,
                   "mode": "async_route_v1", "message_id": "handoff_" + "d" * 32, "duplicate": False}
        for verb in ("send", "ask"):
            with self.subTest(verb=verb), \
                    patch.object(agentsdock_chats, "authority", return_value="capability"), \
                    patch.object(agentsdock_chats, "get_json", return_value={"routes": [
                        {"route_id": route, "mode": "async_route_v1", "available": True}]}) as get, \
                    patch.object(agentsdock_chats, "post_json", return_value=receipt) as post:
                for parent in [parents[0], parents[1], parents[0]]:
                    args = agentsdock_chats.parser().parse_args([verb, "--route", route, "--message", "Same response", "--reply-to", parent])
                    self.assertEqual(args.handler(args), receipt)
                payloads = [call.args[1] for call in post.call_args_list]
                self.assertEqual([payload["reply_to_message_id"] for payload in payloads], [parents[0], parents[1], parents[0]])
                self.assertNotEqual(payloads[0]["idempotency_key"], payloads[1]["idempotency_key"])
                self.assertEqual(payloads[0]["idempotency_key"], payloads[2]["idempotency_key"])
                self.assertTrue(all("wait_for_response" not in payload for payload in payloads))
                self.assertEqual(get.call_count, 3)

    def test_reply_parent_rejects_legacy_before_post(self) -> None:
        route = "route_" + "a" * 32
        args = agentsdock_chats.parser().parse_args(["send", "--route", route, "--message", "Reply",
                                                    "--reply-to", "handoff_" + "b" * 32])
        with patch.object(agentsdock_chats, "authority", return_value="capability"), \
                patch.object(agentsdock_chats, "get_json", return_value={"routes": [{"route_id": route, "available": True}]}), \
                patch.object(agentsdock_chats, "post_json") as post, self.assertRaisesRegex(agentsdock_chats.ChatsCLIError, "legacy"):
            args.handler(args)
        post.assert_not_called()

    def test_async_mailbox_receipt_is_additive_and_never_claims_execution_started(self) -> None:
        route = "route_" + "a" * 32
        base = {"ok": True, "route_id": route, "action": "instruction", "accepted": True,
                "mode": "async_route_v1", "message_id": "handoff_" + "b" * 32, "duplicate": False}
        args = agentsdock_chats.parser().parse_args(["send", "--route", route, "--message", "Hello", "--mode", "async_route_v1"])
        mailbox = {**base, "delivery_mode": "mailbox", "state": "unread", "execution_started": False}
        with patch.object(agentsdock_chats, "authority", return_value="capability"), \
                patch.object(agentsdock_chats, "get_json", return_value={"routes": [
                    {"route_id": route, "available": True, "mode": "async_route_v1"}]}):
            for receipt in (base, mailbox, *[{**mailbox, "state": state, "duplicate": True}
                                           for state in ("read", "cancelled", "deleted")]):
                with patch.object(agentsdock_chats, "post_json", return_value=receipt):
                    self.assertEqual(args.handler(args), receipt)
            for receipt in ({**mailbox, "execution_started": True}, {**base, "delivery_mode": "mailbox"},
                            {**mailbox, "state": "running"}):
                with patch.object(agentsdock_chats, "post_json", return_value=receipt), self.assertRaises(agentsdock_chats.ChatsCLIError):
                    args.handler(args)

    def test_helper_uses_only_the_canonical_provider_capability_header(self) -> None:
        self.assertEqual(
            agentsdock_chats.provider_headers("live-capability"),
            {
                "Accept": "application/json",
                "X-AgentsDock-Provider-Capability": "live-capability",
            },
        )

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

    def test_post_replays_identical_idempotent_request_after_lost_response(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"ok": True, "accepted": True}).encode("utf-8")

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                if len(self.requests) == 1:
                    raise urllib.error.URLError(TimeoutError("response lost"))
                return FakeResponse()

        opener = FakeOpener()
        payload = {
            "body": "hello",
            "idempotency_key": "stable-key",
        }
        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=opener),
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            result = agentsdock_chats.post_json(
                "/api/agent/cross-chat/handoffs",
                payload,
                "capability",
            )

        self.assertEqual(result, {"ok": True, "accepted": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0][0], opener.requests[1][0])
        self.assertEqual(opener.requests[0][0].data, opener.requests[1][0].data)
        sleep.assert_called_once_with(0.1)

    def test_post_replays_identical_request_after_truncated_success_body(self) -> None:
        class FakeResponse:
            def __init__(self, truncated: bool) -> None:
                self.truncated = truncated

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                if self.truncated:
                    raise http.client.IncompleteRead(b'{"ok":true')
                return json.dumps({"ok": True, "accepted": True}).encode("utf-8")

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                return FakeResponse(len(self.requests) == 1)

        opener = FakeOpener()
        payload = {"body": "hello", "idempotency_key": "stable-key"}
        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=opener),
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            result = agentsdock_chats.post_json(
                "/api/agent/cross-chat/handoffs",
                payload,
                "capability",
            )

        self.assertEqual(result, {"ok": True, "accepted": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0][0], opener.requests[1][0])
        self.assertEqual(opener.requests[0][0].data, opener.requests[1][0].data)
        sleep.assert_called_once_with(0.1)

    def test_post_replays_identical_request_after_truncated_error_body(self) -> None:
        class TruncatedHTTPError(urllib.error.HTTPError):
            def read(self):
                raise http.client.IncompleteRead(b'{"detail":')

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"ok": True, "accepted": True}).encode("utf-8")

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                if len(self.requests) == 1:
                    raise TruncatedHTTPError(
                        request.full_url,
                        409,
                        "Conflict",
                        {},
                        None,
                    )
                return FakeResponse()

        opener = FakeOpener()
        payload = {"body": "hello", "idempotency_key": "stable-key"}
        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=opener),
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            result = agentsdock_chats.post_json(
                "/api/agent/cross-chat/handoffs",
                payload,
                "capability",
            )

        self.assertEqual(result, {"ok": True, "accepted": True})
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0][0], opener.requests[1][0])
        sleep.assert_called_once_with(0.1)

    def test_post_reports_ambiguous_commit_without_inviting_reworded_retry(self) -> None:
        class FakeOpener:
            def open(self, _request, timeout):
                raise urllib.error.URLError(TimeoutError("response lost"))

        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=FakeOpener()),
            patch.object(agentsdock_chats.time, "sleep"),
        ):
            with self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                "do not resend it with different wording",
            ):
                agentsdock_chats.post_json(
                    "/api/agent/cross-chat/handoffs",
                    {"body": "hello", "idempotency_key": "stable-key"},
                    "capability",
                )

    def test_get_replays_identical_live_lease_after_lost_response(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"ok": True, "body": "answer"}).encode("utf-8")

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                if len(self.requests) == 1:
                    raise urllib.error.URLError(ConnectionResetError("response lost"))
                return FakeResponse()

        opener = FakeOpener()
        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=opener),
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            result = agentsdock_chats.get_json(
                "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response?lease_id=lease",
                "capability",
                timeout=90,
            )

        self.assertEqual(result, {"ok": True, "body": "answer"})
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0][0], opener.requests[1][0])
        self.assertEqual(opener.requests[0][0].full_url, opener.requests[1][0].full_url)
        sleep.assert_called_once_with(0.1)

    def test_get_replays_identical_live_lease_after_truncated_json(self) -> None:
        class FakeResponse:
            def __init__(self, body: bytes) -> None:
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return self.body

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                if len(self.requests) == 1:
                    return FakeResponse(b'{"ok":true')
                return FakeResponse(json.dumps({
                    "ok": True,
                    "body": "answer",
                }).encode("utf-8"))

        opener = FakeOpener()
        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=opener),
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            result = agentsdock_chats.get_json(
                "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response?lease_id=lease",
                "capability",
                timeout=90,
            )

        self.assertEqual(result, {"ok": True, "body": "answer"})
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0][0], opener.requests[1][0])
        sleep.assert_called_once_with(0.1)

    def test_get_replays_identical_live_lease_after_truncated_error_body(self) -> None:
        class TruncatedHTTPError(urllib.error.HTTPError):
            def read(self):
                raise http.client.IncompleteRead(b'{"detail":')

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return json.dumps({"ok": True, "body": "answer"}).encode("utf-8")

        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                if len(self.requests) == 1:
                    raise TruncatedHTTPError(
                        request.full_url,
                        409,
                        "Conflict",
                        {},
                        None,
                    )
                return FakeResponse()

        opener = FakeOpener()
        with (
            patch.object(agentsdock_chats, "environment", return_value="http://127.0.0.1:7850"),
            patch.object(agentsdock_chats.urllib.request, "build_opener", return_value=opener),
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            result = agentsdock_chats.get_json(
                "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response?lease_id=lease",
                "capability",
                timeout=90,
            )

        self.assertEqual(result, {"ok": True, "body": "answer"})
        self.assertEqual(len(opener.requests), 2)
        self.assertIs(opener.requests[0][0], opener.requests[1][0])
        sleep.assert_called_once_with(0.1)

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

    def test_ask_returns_resume_receipt_then_wait_observes_one_slice(self) -> None:
        exchange_id = "exchange_" + "1" * 32
        question_leg_id = "leg_" + "2" * 32
        answer_leg_id = "leg_" + "3" * 32
        post = Mock(return_value={
            "ok": True,
            "action": "request_reply",
            "accepted": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": question_leg_id,
            "live_response_lease_id": "lease_" + "a" * 32,
        })
        get = Mock(return_value={
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": answer_leg_id,
            "body": "Peer answer",
            "request_response": False,
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
            patch.object(agentsdock_chats, "get_json", get),
        ):
            pending = agentsdock_chats.ask(args)
            get.assert_not_called()
            self.assertTrue(pending["pending"])
            self.assertEqual(
                pending["live_response_lease_id"],
                "lease_" + "a" * 32,
            )
            result = agentsdock_chats.wait(argparse.Namespace(
                authority_file="authority.json",
                exchange=pending["exchange_id"],
                inbound_leg=pending["inbound_leg_id"],
                lease=pending["live_response_lease_id"],
                timeout_seconds=75,
            ))
        self.assertEqual(result["body"], "Peer answer")
        self.assertEqual(result["inbound_leg_id"], answer_leg_id)
        wait_path = get.call_args.args[0]
        self.assertIn(
            f"{exchange_id}/legs/{question_leg_id}/live-response?",
            wait_path,
        )
        self.assertIn("lease_id=lease_", wait_path)
        self.assertEqual(get.call_args.kwargs["timeout"], 30)
        self.assertTrue(get.call_args.kwargs["live_slice"])
        self.assertIn("timeout_seconds=20", wait_path)

    def test_route_ask_waits_on_the_exact_live_lease(self) -> None:
        route = "route_" + "a" * 32
        exchange_id = "exchange_" + "4" * 32
        question_leg_id = "leg_" + "5" * 32
        answer_leg_id = "leg_" + "6" * 32
        receipt = {
            "ok": True,
            "route_id": route,
            "action": "request_reply",
            "accepted": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": question_leg_id,
            "live_response_lease_id": "lease_" + "b" * 32,
        }
        post = Mock(return_value=receipt)
        get = Mock(return_value={
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": answer_leg_id,
            "body": "Route answer",
            "request_response": False,
        })
        args = argparse.Namespace(
            authority_file="authority.json",
            route=route,
            target=None,
            message="Slow question",
            idempotency_key=None,
            timeout_seconds=75,
            async_response=False,
        )
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats, "get_json", get),
        ):
            result = agentsdock_chats.ask(args)

        self.assertTrue(result["pending"])
        self.assertEqual(result["exchange_id"], exchange_id)
        self.assertEqual(result["inbound_leg_id"], question_leg_id)
        payload = post.call_args.args[1]
        self.assertTrue(payload["wait_for_response"])
        self.assertEqual(payload["response_timeout_seconds"], 20)
        get.assert_not_called()

    def test_route_ask_accepts_minimal_receipt_only_in_explicit_async_mode(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            route="route_" + "a" * 32,
            target=None,
            message="Question",
            idempotency_key=None,
            timeout_seconds=75,
            async_response=True,
        )
        receipt = {
            "ok": True,
            "route_id": args.route,
            "action": "request_reply",
            "accepted": True,
        }
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(
                agentsdock_chats,
                "post_json",
                return_value=receipt,
            ),
        ):
            self.assertEqual(agentsdock_chats.ask(args), receipt)

    def test_live_wait_returns_resumable_receipt_after_one_pending_slice(self) -> None:
        exchange_id = "exchange_" + "7" * 32
        question_leg_id = "leg_" + "8" * 32
        lease_id = "lease_" + "d" * 32
        receipt = {
            "exchange_id": exchange_id,
            "inbound_leg_id": question_leg_id,
            "live_response_lease_id": lease_id,
        }
        pending = {
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": question_leg_id,
            "pending": True,
        }
        get = Mock(return_value=pending)
        with patch.object(agentsdock_chats, "get_json", get):
            result = agentsdock_chats.await_live_response(
                receipt,
                "capability",
                20,
            )

        self.assertEqual(result, {**pending, "live_response_lease_id": lease_id})
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["timeout"], 30)
        self.assertTrue(get.call_args.kwargs["live_slice"])

    def test_live_wait_transport_loss_returns_distinct_resumable_error(self) -> None:
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
                3600,
            )

        self.assertEqual(result, {
            "ok": False,
            "exchange_id": exchange_id,
            "inbound_leg_id": inbound_leg_id,
            "live_response_lease_id": lease_id,
            "transport_error": True,
            "retryable": True,
            "message": (
                "AgentsServer did not confirm the live-response state because "
                "the transport was interrupted. Retry the existing wait "
                f"exactly with --exchange {exchange_id} "
                f"--inbound-leg {inbound_leg_id} --lease {lease_id}; "
                "do not resend the ask or change its wording."
            ),
        })
        self.assertNotIn("pending", result)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs["timeout"], 30)
        self.assertTrue(get.call_args.kwargs["live_slice"])

    def test_cli_transport_receipt_prints_exact_lease_and_exits_nonzero(self) -> None:
        exchange_id = "exchange_" + "4" * 32
        inbound_leg_id = "leg_" + "5" * 32
        lease_id = "lease_" + "6" * 32
        stdout = io.StringIO()
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(
                agentsdock_chats,
                "get_json",
                side_effect=agentsdock_chats.LiveWaitRetryable(
                    "response socket closed",
                ),
            ),
            patch.object(agentsdock_chats.sys, "stdout", stdout),
        ):
            exit_code = agentsdock_chats.main([
                "--authority-file",
                "authority.json",
                "wait",
                "--exchange",
                exchange_id,
                "--inbound-leg",
                inbound_leg_id,
                "--lease",
                lease_id,
            ])

        receipt = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertFalse(receipt["ok"])
        self.assertTrue(receipt["transport_error"])
        self.assertTrue(receipt["retryable"])
        self.assertNotIn("pending", receipt)
        self.assertEqual(receipt["exchange_id"], exchange_id)
        self.assertEqual(receipt["inbound_leg_id"], inbound_leg_id)
        self.assertEqual(receipt["live_response_lease_id"], lease_id)
        self.assertIn(
            f"--exchange {exchange_id} --inbound-leg {inbound_leg_id} "
            f"--lease {lease_id}",
            receipt["message"],
        )
        self.assertIn("do not resend the ask", receipt["message"])

    def test_same_lease_retry_recovers_an_already_delivered_answer(self) -> None:
        exchange_id = "exchange_" + "7" * 32
        inbound_leg_id = "leg_" + "8" * 32
        lease_id = "lease_" + "9" * 32
        answer_leg_id = "leg_" + "a" * 32
        args = argparse.Namespace(
            authority_file="authority.json",
            exchange=exchange_id,
            inbound_leg=inbound_leg_id,
            lease=lease_id,
            timeout_seconds=20,
        )
        answer = {
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": answer_leg_id,
            "body": "The reply was already committed.",
            "request_response": False,
        }
        get = Mock(side_effect=[
            agentsdock_chats.LiveWaitRetryable("disconnect cleanup stalled"),
            answer,
        ])
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "get_json", get),
        ):
            transport_receipt = agentsdock_chats.wait(args)
            recovered = agentsdock_chats.wait(args)

        self.assertTrue(transport_receipt["transport_error"])
        self.assertNotIn("pending", transport_receipt)
        self.assertEqual(recovered, answer)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].args[0], get.call_args_list[1].args[0])
        self.assertIn(f"lease_id={lease_id}", get.call_args_list[0].args[0])

    def test_wait_rejects_noncanonical_resume_identifiers_before_http(self) -> None:
        args = argparse.Namespace(
            authority_file="authority.json",
            exchange="../../admin",
            inbound_leg="leg_" + "2" * 32,
            lease="lease_" + "3" * 32,
            timeout_seconds=10,
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

    def test_cli_replays_bounded_wait_slices_past_old_limit_then_answers(self) -> None:
        route = "route_" + "e" * 32
        exchange_id = "exchange_" + "9" * 32
        question_leg_id = "leg_" + "a" * 32
        answer_leg_id = "leg_" + "b" * 32
        lease_id = "lease_" + "f" * 32
        post = Mock(return_value={
            "ok": True,
            "route_id": route,
            "action": "request_reply",
            "accepted": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": question_leg_id,
            "live_response_lease_id": lease_id,
        })
        pending = {
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": question_leg_id,
            "pending": True,
        }
        answer = {
            "ok": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": answer_leg_id,
            "body": "Answer after more than the old timeout",
            "request_response": False,
        }
        # Each wait command observes one second and exits normally. Seventy-six
        # pending slices cross the old 75-second semantic cap before the same
        # lease returns its answer, without one shell call becoming long-lived.
        get = Mock(side_effect=[pending] * 76 + [answer])
        stdout = io.StringIO()
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats, "get_json", get),
            patch.object(agentsdock_chats.sys, "stdout", stdout),
        ):
            self.assertEqual(agentsdock_chats.main([
                "--authority-file",
                "authority.json",
                "ask",
                "--route",
                route,
                "--message",
                "Wait as long as necessary",
                "--timeout-seconds",
                "1",
            ]), 0)
            receipt = json.loads(stdout.getvalue())
            self.assertTrue(receipt["pending"])
            for _index in range(77):
                stdout.seek(0)
                stdout.truncate(0)
                self.assertEqual(agentsdock_chats.main([
                    "--authority-file",
                    "authority.json",
                    "wait",
                    "--exchange",
                    receipt["exchange_id"],
                    "--inbound-leg",
                    receipt["inbound_leg_id"],
                    "--lease",
                    receipt["live_response_lease_id"],
                    "--timeout-seconds",
                    "1",
                ]), 0)
                receipt = json.loads(stdout.getvalue())

        self.assertEqual(receipt["body"], answer["body"])
        post.assert_called_once()
        self.assertEqual(post.call_args.args[1]["response_timeout_seconds"], 1)
        self.assertEqual(get.call_count, 77)
        self.assertEqual(
            {call.args[0] for call in get.call_args_list},
            {get.call_args_list[0].args[0]},
        )
        self.assertTrue(all(
            "timeout_seconds=1" in call.args[0]
            and call.kwargs["timeout"] == 11
            and call.kwargs["live_slice"] is True
            for call in get.call_args_list
        ))

    def test_cli_explicit_410_cancel_is_terminal(self) -> None:
        route = "route_" + "a" * 32
        exchange_id = "exchange_" + "c" * 32
        question_leg_id = "leg_" + "d" * 32
        lease_id = "lease_" + "b" * 32
        post = Mock(return_value={
            "ok": True,
            "route_id": route,
            "action": "request_reply",
            "accepted": True,
            "exchange_id": exchange_id,
            "inbound_leg_id": question_leg_id,
            "live_response_lease_id": lease_id,
        })
        get = Mock(side_effect=agentsdock_chats.ChatsCLIError(
            "server rejected request (410): cancelled_by_user"
        ))
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats, "get_json", get),
            patch.object(agentsdock_chats.sys, "stdout", stdout),
            patch.object(agentsdock_chats.sys, "stderr", stderr),
        ):
            self.assertEqual(agentsdock_chats.main([
                "--authority-file",
                "authority.json",
                "ask",
                "--route",
                route,
                "--message",
                "Wait until cancel",
                "--timeout-seconds",
                "1",
            ]), 0)
            pending_receipt = json.loads(stdout.getvalue())
            exit_code = agentsdock_chats.main([
                "--authority-file",
                "authority.json",
                "wait",
                "--exchange",
                pending_receipt["exchange_id"],
                "--inbound-leg",
                pending_receipt["inbound_leg_id"],
                "--lease",
                pending_receipt["live_response_lease_id"],
                "--timeout-seconds",
                "1",
            ])

        self.assertEqual(exit_code, 2)
        self.assertIn("cancelled_by_user", stderr.getvalue())
        get.assert_called_once()

    def test_live_get_transport_failure_ends_slice_after_one_boundary(self) -> None:
        class FakeOpener:
            def __init__(self) -> None:
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
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
        ):
            with self.assertRaises(agentsdock_chats.LiveWaitRetryable):
                agentsdock_chats.get_json(
                    "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response",
                    "capability",
                    timeout=15,
                    live_slice=True,
                )

        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0][1], 15)
        sleep.assert_not_called()

    def test_live_get_gateway_failure_is_resumable_not_terminal(self) -> None:
        class FakeOpener:
            def open(self, request, timeout):
                raise urllib.error.HTTPError(
                    request.full_url,
                    503,
                    "Service Unavailable",
                    {},
                    io.BytesIO(b'{"detail":"restarting"}'),
                )

        with (
            patch.object(
                agentsdock_chats,
                "environment",
                return_value="http://127.0.0.1:7850",
            ),
            patch.object(
                agentsdock_chats.urllib.request,
                "build_opener",
                return_value=FakeOpener(),
            ),
            self.assertRaises(agentsdock_chats.LiveWaitRetryable),
        ):
            agentsdock_chats.get_json(
                "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response",
                "capability",
                live_slice=True,
            )

    def test_live_get_malformed_success_never_multiplies_slice_budget(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b"not-json"

        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                self.timeout = timeout
                return FakeResponse()

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
            self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                "invalid live-response body",
            ),
        ):
            agentsdock_chats.get_json(
                "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response",
                "capability",
                timeout=30,
                live_slice=True,
            )

        self.assertEqual(opener.calls, 1)
        self.assertEqual(opener.timeout, 30)
        sleep.assert_not_called()

    def test_live_get_treats_explicit_cancel_as_terminal(self) -> None:
        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
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
            patch.object(agentsdock_chats.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                r"\(410\): cancelled_by_user",
            ):
                agentsdock_chats.get_json(
                    "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response",
                    "capability",
                    live_slice=True,
                )

        self.assertEqual(opener.calls, 1)
        sleep.assert_not_called()

    def test_live_get_does_not_retry_malformed_success_forever(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b'{"ok":'

        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, _request, timeout):
                self.calls += 1
                return FakeResponse()

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
        ):
            with self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                "invalid live-response body",
            ):
                agentsdock_chats.get_json(
                    "/api/agent/cross-chat/exchanges/ex/legs/leg/live-response",
                    "capability",
                    live_slice=True,
                )

        self.assertEqual(opener.calls, 1)
        sleep.assert_not_called()

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

    def test_list_cursor_is_explicit_and_preserves_next_page(self) -> None:
        cursor = "route_" + "a" * 32
        following = "route_" + "b" * 32
        args = agentsdock_chats.parser().parse_args(["list", "--cursor", cursor])
        page = {"routes": [], "next_cursor": following, "max_handoffs_per_run": None}
        with patch.object(agentsdock_chats, "authority", return_value="capability"), \
                patch.object(agentsdock_chats, "get_json", return_value=page) as get:
            self.assertEqual(args.handler(args), page)
        get.assert_called_once_with(f"/api/agent/cross-chat/routes?cursor={cursor}", "capability")

    def test_list_rejects_ignored_or_nonadvancing_cursor(self) -> None:
        cursor = "route_" + "a" * 32
        args = argparse.Namespace(authority_file=None, cursor=cursor)
        for response in ({"routes": []}, {"routes": [], "next_cursor": cursor},
                         {"routes": [], "next_cursor": "bad"}):
            with self.subTest(response=response), \
                    patch.object(agentsdock_chats, "authority", return_value="capability"), \
                    patch.object(agentsdock_chats, "get_json", return_value=response), \
                    self.assertRaises(agentsdock_chats.ChatsCLIError):
                agentsdock_chats.list_routes(args)

    def test_exact_mode_lookup_accepts_old_whole_list_and_new_exact_response(self) -> None:
        route_id = "route_" + "b" * 32
        target = {"route_id": route_id, "available": True, "mode": "async_route_v1"}
        for response in ({"routes": [{"route_id": "unrelated"}, target]},
                         {"routes": [target], "next_cursor": None}):
            with self.subTest(response=response), \
                    patch.object(agentsdock_chats, "get_json", return_value=response) as get:
                self.assertEqual(agentsdock_chats.negotiated_route_mode("capability", route_id), "async_route_v1")
            get.assert_called_once_with(f"/api/agent/cross-chat/routes?route_id={route_id}", "capability")
        with patch.object(agentsdock_chats, "get_json", return_value={"routes": []}), \
                self.assertRaisesRegex(agentsdock_chats.ChatsCLIError, "unavailable"):
            agentsdock_chats.negotiated_route_mode("capability", route_id)

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

    def test_followup_pending_receipt_preserves_exact_resumable_lease(self) -> None:
        exchange_id = "exchange_" + "1" * 32
        inbound_leg_id = "leg_" + "2" * 32
        followup_leg_id = "leg_" + "3" * 32
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
                    "inbound_leg_id": followup_leg_id,
                    "live_response_lease_id": lease_id,
                },
            ),
            patch.object(
                agentsdock_chats,
                "get_json",
                return_value={
                    "ok": True,
                    "exchange_id": exchange_id,
                    "inbound_leg_id": followup_leg_id,
                    "pending": True,
                },
            ) as get,
        ):
            result = agentsdock_chats.respond(args)

        self.assertTrue(result["accepted"])
        self.assertTrue(result["pending"])
        self.assertEqual(result["live_response_lease_id"], lease_id)
        self.assertEqual(result["inbound_leg_id"], followup_leg_id)
        get.assert_not_called()

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

    def test_followup_accepts_legacy_async_recovery_receipt(self) -> None:
        exchange_id = "exchange_" + "e" * 32
        answer_leg_id = "leg_" + "e" * 32
        followup_leg_id = "leg_" + "f" * 32
        args = argparse.Namespace(
            authority_file="authority.json",
            exchange=exchange_id,
            inbound_leg=answer_leg_id,
            message="One more question",
            request_response=True,
            async_response=False,
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
                    "action": "response",
                    "accepted": True,
                    "exchange_id": exchange_id,
                    "inbound_leg_id": followup_leg_id,
                    "live_response_lease_id": "lease_" + "c" * 32,
                },
            ),
            patch.object(
                agentsdock_chats,
                "get_json",
                return_value={
                    "ok": True,
                    "exchange_id": exchange_id,
                    "inbound_leg_id": followup_leg_id,
                    "deferred": True,
                    "delivery": "asynchronous",
                    "message": "The answer will be delivered asynchronously.",
                },
            ),
        ):
            pending = agentsdock_chats.respond(args)
            self.assertTrue(pending["pending"])
            result = agentsdock_chats.wait(argparse.Namespace(
                authority_file="authority.json",
                exchange=pending["exchange_id"],
                inbound_leg=pending["inbound_leg_id"],
                lease=pending["live_response_lease_id"],
                timeout_seconds=75,
            ))

        self.assertTrue(pending["accepted"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["delivery"], "asynchronous")

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

    def test_authority_uses_matching_bounded_provider_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            authority_path = Path(temporary) / "authority.json"
            authority_path.write_text(json.dumps({
                "provider_capability": "provider-secret",
                "source_session_id": "sess/source",
            }), encoding="utf-8")
            authority_path.chmod(0o600)
            with patch.dict(os.environ, {
                "AGENTSDOCK_PROVIDER_AUTHORITY_FILE": str(authority_path),
                "AGENTSDOCK_CHAT_ID": "sess/source",
            }, clear=True):
                self.assertEqual(
                    agentsdock_chats.authority(None),
                    "provider-secret",
                )

    def test_authority_rejects_explicit_override_and_ambient_chat_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = []
            for index in range(2):
                path = Path(temporary) / f"authority-{index}.json"
                path.write_text(json.dumps({
                    "provider_capability": f"provider-{index}",
                    "source_session_id": "sess/source",
                }), encoding="utf-8")
                path.chmod(0o600)
                paths.append(path)
            with patch.dict(os.environ, {
                "AGENTSDOCK_PROVIDER_AUTHORITY_FILE": str(paths[0]),
                "AGENTSDOCK_CHAT_ID": "sess/source",
            }, clear=True):
                with self.assertRaisesRegex(
                    agentsdock_chats.ChatsCLIError,
                    "conflicts with the live provider authority",
                ):
                    agentsdock_chats.authority(str(paths[1]))
            with patch.dict(os.environ, {
                "AGENTSDOCK_PROVIDER_AUTHORITY_FILE": str(paths[0]),
                "AGENTSDOCK_CHAT_ID": "sess/other",
            }, clear=True):
                with self.assertRaisesRegex(
                    agentsdock_chats.ChatsCLIError,
                    "does not match the authority file",
                ):
                    agentsdock_chats.authority(None)

    def test_non_loopback_origin_requires_matching_authority_and_runtime_bindings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            authority_path = Path(temporary) / "authority.json"
            authority_path.write_text(json.dumps({
                "provider_capability": "provider-secret",
                "source_session_id": "sess/source",
                "provider_server_origin": "http://[fd00::10]:7850",
            }), encoding="utf-8")
            authority_path.chmod(0o600)
            environment = {
                "AGENTSDOCK_PROVIDER_AUTHORITY_FILE": str(authority_path),
                "AGENTSDOCK_PROVIDER_SERVER_ORIGIN": "http://[fd00:0::10]:7850/",
                "AGENTSDOCK_SERVER_URL": "http://[fd00::10]:7850",
            }
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(
                    agentsdock_chats.environment(),
                    "http://[fd00::10]:7850",
                )
            environment["AGENTSDOCK_PROVIDER_SERVER_ORIGIN"] = (
                "http://192.0.2.20:7850"
            )
            with patch.dict(os.environ, environment, clear=True):
                with self.assertRaisesRegex(
                    agentsdock_chats.ChatsCLIError,
                    "conflicts with the live provider origin",
                ):
                    agentsdock_chats.environment()

    def test_target_index_resolves_only_matching_live_handle(self) -> None:
        target = "grant_" + "a" * 32
        post = Mock(return_value={
            "ok": True,
            "action": "instruction",
            "accepted": True,
        })
        with (
            patch.dict(os.environ, {
                "AGENTSDOCK_CROSS_CHAT_HANDLE_COUNT": "1",
                "AGENTSDOCK_CROSS_CHAT_HANDLE_1": target,
                "AGENTSDOCK_CROSS_CHAT_HANDLE_1_ACTION": "instruction",
                "AGENTSDOCK_CROSS_CHAT_HANDLE_1_ASYNC": "0",
            }, clear=True),
            patch.object(
                agentsdock_chats,
                "_authority_path",
                return_value=Path("authority.json"),
            ),
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats.sys, "stdout", io.StringIO()),
        ):
            self.assertEqual(agentsdock_chats.main([
                "send", "--target-index", "1", "--message", "hello",
            ]), 0)

        path, payload, _capability = post.call_args.args
        self.assertEqual(path, "/api/agent/cross-chat/handoffs")
        self.assertEqual(payload["target_session_id"], target)
        self.assertNotIn("target_index", payload)

    def test_target_index_derives_secure_peer_async_mode_and_rejects_override(self) -> None:
        target = "secure_" + "b" * 32
        post = Mock(return_value={
            "ok": True,
            "action": "request_reply",
            "accepted": True,
        })
        environment = {
            "AGENTSDOCK_CROSS_CHAT_HANDLE_COUNT": "1",
            "AGENTSDOCK_CROSS_CHAT_HANDLE_1": target,
            "AGENTSDOCK_CROSS_CHAT_HANDLE_1_ACTION": "request_reply",
            "AGENTSDOCK_CROSS_CHAT_HANDLE_1_ASYNC": "1",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            patch.object(
                agentsdock_chats,
                "_authority_path",
                return_value=Path("authority.json"),
            ),
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats.sys, "stdout", io.StringIO()),
        ):
            self.assertEqual(agentsdock_chats.main([
                "ask", "--target-index", "1", "--message", "hello",
            ]), 0)
            self.assertEqual(agentsdock_chats.main([
                "ask", "--target-index", "1", "--message", "hello",
                "--async-response",
            ]), 2)

        payload = post.call_args.args[1]
        self.assertNotIn("wait_for_response", payload)
        self.assertEqual(post.call_count, 1)

    def test_target_index_fails_closed_for_missing_or_wrong_action_grant(self) -> None:
        for environment in (
            {},
            {
                "AGENTSDOCK_CROSS_CHAT_HANDLE_COUNT": "1",
                "AGENTSDOCK_CROSS_CHAT_HANDLE_1": "grant_" + "c" * 32,
                "AGENTSDOCK_CROSS_CHAT_HANDLE_1_ACTION": "request_reply",
                "AGENTSDOCK_CROSS_CHAT_HANDLE_1_ASYNC": "0",
            },
            {
                "AGENTSDOCK_CROSS_CHAT_HANDLE_COUNT": "999",
            },
        ):
            with self.subTest(environment=environment), patch.dict(
                os.environ, environment, clear=True
            ):
                with self.assertRaises(agentsdock_chats.ChatsCLIError):
                    agentsdock_chats.provider_handle(1, "instruction")

    def test_respond_current_resolves_reply_and_followup_mode_from_environment(self) -> None:
        exchange_id = "exchange_" + "d" * 32
        inbound_leg_id = "leg_" + "e" * 32
        post = Mock(return_value={
            "ok": True,
            "action": "response",
            "accepted": True,
        })
        with (
            patch.dict(os.environ, {
                "AGENTSDOCK_CROSS_CHAT_RESPONSE_EXCHANGE_ID": exchange_id,
                "AGENTSDOCK_CROSS_CHAT_RESPONSE_INBOUND_LEG_ID": inbound_leg_id,
                "AGENTSDOCK_CROSS_CHAT_RESPONSE_FOLLOWUP": "allowed-async",
            }, clear=True),
            patch.object(
                agentsdock_chats,
                "_authority_path",
                return_value=Path("authority.json"),
            ),
            patch.object(agentsdock_chats, "authority", return_value="capability"),
            patch.object(agentsdock_chats, "post_json", post),
            patch.object(agentsdock_chats.sys, "stdout", io.StringIO()),
        ):
            self.assertEqual(agentsdock_chats.main([
                "respond-current", "--message", "reply", "--request-response",
            ]), 0)

        path, payload, _capability = post.call_args.args
        self.assertIn(exchange_id, path)
        self.assertEqual(payload["inbound_leg_id"], inbound_leg_id)
        self.assertTrue(payload["request_response"])
        self.assertNotIn("wait_for_response", payload)

    def test_respond_current_fails_closed_without_grant_or_followup(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                "reply grant is unavailable",
            ):
                agentsdock_chats.respond_current(argparse.Namespace(
                    authority_file=None,
                    message="reply",
                    request_response=False,
                    idempotency_key=None,
                    timeout_seconds=20,
                ))
        with patch.dict(os.environ, {
            "AGENTSDOCK_CROSS_CHAT_RESPONSE_EXCHANGE_ID": "exchange_" + "f" * 32,
            "AGENTSDOCK_CROSS_CHAT_RESPONSE_INBOUND_LEG_ID": "leg_" + "a" * 32,
            "AGENTSDOCK_CROSS_CHAT_RESPONSE_FOLLOWUP": "none",
        }, clear=True):
            with self.assertRaisesRegex(
                agentsdock_chats.ChatsCLIError,
                "no follow-up grant",
            ):
                agentsdock_chats.respond_current(argparse.Namespace(
                    authority_file=None,
                    message="reply",
                    request_response=True,
                    idempotency_key=None,
                    timeout_seconds=20,
                ))


if __name__ == "__main__":
    unittest.main()
