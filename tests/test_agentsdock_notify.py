import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import agentsdock_notify


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class NotificationCLITests(unittest.TestCase):
    def test_retry_preserves_idempotency_and_chat_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            authority = Path(directory) / "authority.json"
            authority.write_text(json.dumps({
                "provider_capability": "run-secret", "source_session_id": "sess/demo",
            }), encoding="utf-8")
            authority.chmod(0o600)
            requests = []

            def open_response(request, timeout):
                self.assertEqual(timeout, 30)
                requests.append(request)
                if len(requests) == 1:
                    raise urllib.error.URLError("response lost")
                return FakeResponse({
                    "ok": True, "chat_id": "sess/demo", "level": "important",
                    "event_id": "event-1", "event_seq": 4, "duplicate": True,
                })

            with (
                patch.dict("os.environ", {
                    "AGENTSDOCK_SERVER_URL": "http://127.0.0.1:7850",
                    "AGENTSDOCK_CHAT_ID": "sess/demo",
                }, clear=True),
                patch.object(agentsdock_notify.urllib.request, "build_opener", return_value=type(
                    "Opener", (), {"open": staticmethod(open_response)},
                )()) as build_opener,
            ):
                receipt = agentsdock_notify.send_notification(
                    "important", "Review the result", authority_file=str(authority),
                    chat_id="sess/demo", request_id="notice.cli.1",
                )
                with self.assertRaises(agentsdock_notify.EmergencyCLIError):
                    agentsdock_notify.send_notification(
                        "important", "Wrong chat", authority_file=str(authority),
                        chat_id="other", request_id="notice.cli.2",
                    )

            self.assertTrue(receipt["duplicate"])
            self.assertEqual(len(requests), 2)
            self.assertEqual(requests[0].data, requests[1].data)
            self.assertEqual(requests[0].get_header("X-agentsdock-provider-capability"), "run-secret")
            self.assertEqual(json.loads(requests[0].data)["level"], "important")
            self.assertEqual(len(build_opener.call_args.args), 2)

    def test_invalid_level_and_receipt_fail_closed(self):
        with self.assertRaises(agentsdock_notify.EmergencyCLIError):
            agentsdock_notify.send_notification("urgent", "Message")
