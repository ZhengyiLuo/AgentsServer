import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import agentsdock_emergency


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class EmergencyCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def authority_file(
        self,
        chat_id: str = "sess/demo",
        *,
        mode: int = 0o600,
    ) -> str:
        path = self.root / f"authority-{len(list(self.root.iterdir()))}.json"
        path.write_text(json.dumps({
            "provider_capability": "provider-secret",
            "source_session_id": chat_id,
        }), encoding="utf-8")
        path.chmod(mode)
        return str(path)

    def environment(
        self,
        chat_id: str = "sess/demo",
        *,
        server_url: str = "http://127.0.0.1:7850",
    ) -> dict[str, str]:
        return {
            "AGENTSDOCK_SERVER_URL": server_url,
            "AGENTSDOCK_CHAT_ID": chat_id,
            "HTTP_PROXY": "http://proxy.invalid:8888",
            "NO_PROXY": "",
        }

    @staticmethod
    def receipt(chat_id: str = "sess/demo") -> dict:
        return {
            "ok": True,
            "chat_id": chat_id,
            "alert": {
                "id": "emergency_" + "a" * 32,
                "status": "active",
                "severity": "critical",
                "message": "Immediate user attention is required.",
                "raised_at": "2026-08-25T12:00:00Z",
            },
            "event_id": "evt_alert",
            "event_seq": 42,
            "duplicate": False,
            "unacknowledged_emergency_count": 1,
        }

    def test_lost_response_retries_same_request_without_proxy_or_redirect(self) -> None:
        requests = []

        def open_response(request, timeout):
            self.assertEqual(timeout, 30)
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.URLError("response lost")
            return FakeResponse(self.receipt())

        authority_file = self.authority_file()
        with (
            patch.dict("os.environ", self.environment(), clear=True),
            patch.object(
                agentsdock_emergency.urllib.request,
                "build_opener",
                return_value=type(
                    "Opener",
                    (),
                    {"open": staticmethod(open_response)},
                )(),
            ) as build_opener,
        ):
            result = agentsdock_emergency.raise_alert(
                "  Immediate   user attention is required.  ",
                authority_file=authority_file,
                chat_id="sess/demo",
                request_id="request.cli.0001",
            )

        self.assertEqual(result, self.receipt())
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].data, requests[1].data)
        body = json.loads(requests[0].data)
        self.assertEqual(body, {
            "request_id": "request.cli.0001",
            "message": "Immediate user attention is required.",
        })
        self.assertEqual(
            requests[0].full_url,
            "http://127.0.0.1:7850/api/agent/sessions/sess%2Fdemo/emergency-alerts",
        )
        first_headers = {
            key.lower(): value for key, value in requests[0].header_items()
        }
        second_headers = {
            key.lower(): value for key, value in requests[1].header_items()
        }
        self.assertEqual(
            first_headers["x-agentsdock-provider-capability"],
            "provider-secret",
        )
        self.assertNotIn("authorization", first_headers)
        self.assertNotIn("x-agentsdock-emergency-retry", first_headers)
        self.assertEqual(second_headers["x-agentsdock-emergency-retry"], "1")
        handlers = build_opener.call_args.args
        self.assertIsInstance(handlers[0], agentsdock_emergency.urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], agentsdock_emergency.NoRedirectHandler)

    def test_unicode_controls_are_normalized_before_send_and_receipt_validation(self) -> None:
        requests = []
        expected_message = "Urgent data loss now"

        def open_response(request, timeout):
            requests.append(request)
            receipt = self.receipt()
            receipt["alert"]["message"] = expected_message
            return FakeResponse(receipt)

        with (
            patch.dict("os.environ", self.environment(), clear=True),
            patch.object(
                agentsdock_emergency.urllib.request,
                "build_opener",
                return_value=type(
                    "Opener",
                    (),
                    {"open": staticmethod(open_response)},
                )(),
            ),
        ):
            result = agentsdock_emergency.raise_alert(
                "Urgent\u200bdata\nloss\tnow",
                authority_file=self.authority_file(),
                request_id="request.controls.0001",
            )

        self.assertEqual(result["alert"]["message"], expected_message)
        self.assertEqual(
            json.loads(requests[0].data)["message"],
            expected_message,
        )

    def test_remote_server_is_rejected_before_authority_can_be_sent(self) -> None:
        authority_file = self.authority_file()
        with (
            patch.dict(
                "os.environ",
                self.environment(server_url="http://10.0.0.8:7850"),
                clear=True,
            ),
            patch.object(
                agentsdock_emergency.urllib.request,
                "build_opener",
            ) as build_opener,
        ):
            with self.assertRaisesRegex(
                agentsdock_emergency.EmergencyCLIError,
                "loopback",
            ):
                agentsdock_emergency.raise_alert(
                    "Do not send the capability remotely.",
                    authority_file=authority_file,
                    request_id="request.remote.0001",
                )
        build_opener.assert_not_called()

    def test_chat_scope_and_private_authority_mode_are_required(self) -> None:
        private = self.authority_file("source")
        with patch.dict("os.environ", self.environment("neighbor"), clear=True):
            with self.assertRaisesRegex(
                agentsdock_emergency.EmergencyCLIError,
                "does not match",
            ):
                agentsdock_emergency.raise_alert(
                    "A foreign chat must not be alerted.",
                    authority_file=private,
                    request_id="request.scope.0001",
                )

        unsafe = self.authority_file("source", mode=0o644)
        with patch.dict("os.environ", self.environment("source"), clear=True):
            with self.assertRaisesRegex(
                agentsdock_emergency.EmergencyCLIError,
                "permissions are unsafe",
            ):
                agentsdock_emergency.raise_alert(
                    "Unsafe authority files must be rejected.",
                    authority_file=unsafe,
                    request_id="request.mode.0001",
                )

    def test_invalid_receipt_is_rejected_after_bounded_retry(self) -> None:
        requests = []

        def open_response(request, timeout):
            requests.append(request)
            return FakeResponse({
                **self.receipt(),
                "chat_id": "another-chat",
            })

        with (
            patch.dict("os.environ", self.environment(), clear=True),
            patch.object(
                agentsdock_emergency.urllib.request,
                "build_opener",
                return_value=type(
                    "Opener",
                    (),
                    {"open": staticmethod(open_response)},
                )(),
            ),
        ):
            with self.assertRaisesRegex(
                agentsdock_emergency.EmergencyCLIError,
                "invalid emergency receipt",
            ):
                agentsdock_emergency.raise_alert(
                    "The response must match the authority chat.",
                    authority_file=self.authority_file(),
                    request_id="request.receipt.0001",
                )
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
