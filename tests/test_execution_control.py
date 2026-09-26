import asyncio
import json
import unittest
import uuid

from execution_control import ExecutionControl, ExecutionControlError, MAINTENANCE_PATH, STATUS_PATH


class ExecutionControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.token = "a" * 64
        self.instance = uuid.uuid4().hex
        self.operation = str(uuid.uuid4())
        self.calls = []
        self.status_calls = []
        self.fallback_calls = []

        async def fallback(scope, receive, send):
            self.fallback_calls.append(scope)
            await send({"type": "http.response.start", "status": 418, "headers": []})
            await send({"type": "http.response.body", "body": b"fallback"})

        async def status():
            self.status_calls.append(True)
            return {"worker_instance_id": self.instance, "busy": False}

        async def maintain(*args):
            self.calls.append(args)
            return {"worker_instance_id": self.instance, "lease_id": "lease-123", "action": args[0]}

        self.app = ExecutionControl(fallback, token=self.token, worker_instance_id=self.instance,
                                    status_callback=status, maintenance_callback=maintain)

    def body(self, **changes):
        return {"action": "acquire", "expected_worker_instance_id": self.instance,
                "operation_id": self.operation, **changes}

    async def request(self, *, path=MAINTENANCE_PATH, method="POST", body=None,
                      raw=None, headers=None, client=("127.0.0.1", 55321), query=b"", events=None):
        if raw is None:
            raw = json.dumps(self.body() if body is None else body).encode()
        if headers is None:
            headers = [(b"authorization", b"Bearer " + self.token.encode())]
            if method == "POST":
                headers += [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
        scope = {"type": "http", "path": path, "method": method, "headers": headers,
                 "client": client, "query_string": query}
        queue = asyncio.Queue()
        for message in events or [{"type": "http.request", "body": raw}]:
            queue.put_nowait(message)
        sent = []

        async def send(message):
            sent.append(message)

        await self.app(scope, queue.get, send)
        return sent[0]["status"], b"".join(item.get("body", b"") for item in sent)

    def assert_no_owner_calls(self):
        self.assertEqual(self.calls, [])
        self.assertEqual(self.status_calls, [])
        self.assertEqual(self.fallback_calls, [])

    async def test_status_and_all_maintenance_actions_pass_exact_bound_arguments(self):
        status, raw = await self.request(path=STATUS_PATH, method="GET")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["worker_instance_id"], self.instance)
        status, _ = await self.request()
        self.assertEqual(status, 200)
        self.assertEqual(self.calls[-1], ("acquire", self.operation, None, 120))
        for action, lifetime in (("renew", 30), ("seal", 300), ("release", 120)):
            status, _ = await self.request(body=self.body(action=action, lease_id="lease-123", lifetime_seconds=lifetime))
            self.assertEqual(status, 200)
            self.assertEqual(self.calls[-1], (action, self.operation, "lease-123", lifetime))

    async def test_wrong_instance_is_fenced_before_any_callback(self):
        status, raw = await self.request(body=self.body(expected_worker_instance_id="old-worker"))
        self.assertEqual(status, 409)
        self.assertNotIn(b"old-worker", raw)
        self.assert_no_owner_calls()

    async def test_duplicate_mixed_or_noncanonical_credentials_fail_before_body_read(self):
        good = (b"authorization", b"Bearer " + self.token.encode())
        for headers in ([], [good, good], [good, (b"Authorization", good[1])],
                        [(b"authorization", b"bearer " + self.token.encode())],
                        [(b"authorization", good[1] + b" ")],
                        [(b"authorization", b"Bearer " + b"b" * 64)],
                        [good, (b"x-agentsdock-token", b"other")]):
            with self.subTest(headers=[name for name, _ in headers]):
                status, raw = await self.request(headers=headers)
                self.assertIn(status, (401, 403))
                self.assertNotIn(self.token.encode(), raw)
        self.assert_no_owner_calls()

    async def test_remote_unknown_peers_queries_and_browser_proxy_headers_are_rejected(self):
        for client in (None, ("203.0.113.10", 1), ("localhost", 1), ("::ffff:203.0.113.10", 1)):
            status, _ = await self.request(client=client)
            self.assertEqual(status, 403)
        for query in (b"token=anything", b"x=1"):
            status, _ = await self.request(query=query)
            self.assertEqual(status, 403)
        for name in (b"origin", b"cookie", b"sec-fetch-site", b"sec-fetch-mode", b"forwarded",
                     b"via", b"x-forwarded-for", b"x-forwarded-proto", b"x-real-ip", b"tailscale-user-login"):
            status, _ = await self.request(headers=[(b"authorization", b"Bearer " + self.token.encode()), (name, b"")])
            self.assertEqual(status, 403)
        self.assert_no_owner_calls()

    async def test_reserved_paths_methods_do_not_fall_back_to_public_app(self):
        for path, method, expected in ((STATUS_PATH, "POST", 405), (MAINTENANCE_PATH, "GET", 405),
                                       (STATUS_PATH, "OPTIONS", 405), (STATUS_PATH + "/extra", "GET", 404),
                                       ("/api/admin/execution", "GET", 404)):
            status, _ = await self.request(path=path, method=method)
            self.assertEqual(status, expected)
        self.assert_no_owner_calls()

    async def test_noncontrol_request_uses_existing_application(self):
        status, raw = await self.request(path="/api/health", method="GET", headers=[])
        self.assertEqual((status, raw), (418, b"fallback"))
        self.assertEqual(len(self.fallback_calls), 1)
        self.assertEqual(self.calls, [])

    async def test_invalid_duplicate_or_nonfinite_json_never_reaches_owner(self):
        raw = json.dumps(self.body()).encode()
        duplicate = raw[:-1] + b',"action":"release"}'
        for payload in (duplicate, b"[]", b"null", b"{", b"\xff", b'{"action":NaN}',
                        json.dumps(self.body(unexpected=True)).encode()):
            status, _ = await self.request(raw=payload)
            self.assertIn(status, (400, 422))
        self.assert_no_owner_calls()

    async def test_invalid_operation_lease_action_and_lifetime_never_reach_owner(self):
        bodies = [self.body(operation_id=self.operation.upper()), self.body(operation_id=uuid.uuid4().hex),
                  self.body(operation_id=None), self.body(action=[]), self.body(action="delete"),
                  self.body(lease_id="not-allowed-on-acquire"), self.body(action="seal"),
                  self.body(action="renew", lease_id="bad lease"), self.body(action="release", lease_id="x" * 129)]
        bodies += [self.body(lifetime_seconds=value) for value in (29, 301, True, 120.0, "120", None)]
        for body in bodies:
            status, _ = await self.request(body=body)
            self.assertEqual(status, 422)
        self.assert_no_owner_calls()

    async def test_declared_streamed_limits_and_framing_are_enforced(self):
        auth = (b"authorization", b"Bearer " + self.token.encode())
        content_type = (b"content-type", b"application/json")
        for headers, events, expected in (
            ([auth, content_type, (b"content-length", b"4097")], None, 413),
            ([auth, content_type], [{"type": "http.request", "body": b"x" * 4097}], 413),
            ([auth, content_type, (b"content-length", b"1")], None, 400),
            ([auth, content_type, (b"content-length", b"4000")], None, 400),
            ([auth, content_type, (b"content-length", b"10"), (b"content-length", b"10")], None, 400),
            ([auth, content_type, (b"transfer-encoding", b"chunked")], None, 400),
            ([auth, content_type], [{"type": "http.disconnect"}], 400),
            ([auth], None, 415),
        ):
            status, _ = await self.request(headers=headers, events=events)
            self.assertEqual(status, expected)
        self.assert_no_owner_calls()

    async def test_fragmented_valid_body_is_parsed_once(self):
        raw = json.dumps(self.body()).encode()
        status, _ = await self.request(raw=raw, events=[
            {"type": "http.request", "body": raw[:10], "more_body": True},
            {"type": "http.request", "body": raw[10:]},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(len(self.calls), 1)

    async def test_callback_refusal_is_preserved_but_secrets_and_internal_errors_never_escape(self):
        async def busy(*args):
            raise ExecutionControlError(409, "Execution remains busy")

        self.app.maintenance_callback = busy
        status, raw = await self.request()
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(raw), {"detail": "Execution remains busy"})

        async def bad(*args):
            return {"accidental_secret": self.token}

        self.app.maintenance_callback = bad
        status, raw = await self.request()
        self.assertEqual(status, 500)
        self.assertNotIn(self.token.encode(), raw)

        async def failure(*args):
            raise RuntimeError(self.token)

        self.app.maintenance_callback = failure
        status, raw = await self.request()
        self.assertEqual(status, 500)
        self.assertNotIn(self.token.encode(), raw)

    async def test_websocket_cannot_enter_control_callbacks(self):
        sent = []

        async def receive():
            self.fail("Control WebSocket must be refused before reads")

        async def send(message):
            sent.append(message)

        await self.app({"type": "websocket", "path": STATUS_PATH}, receive, send)
        self.assertEqual(sent[0]["type"], "websocket.close")
        self.assertEqual(sent[0]["code"], 4403)
        self.assert_no_owner_calls()

    async def test_response_loss_does_not_repeat_accepted_lease_operation_or_response(self):
        raw = json.dumps(self.body()).encode()
        sent = []

        async def receive():
            return {"type": "http.request", "body": raw}

        async def send(message):
            sent.append(message)
            raise BrokenPipeError("The caller disconnected after acceptance")

        scope = {"type": "http", "path": MAINTENANCE_PATH, "method": "POST",
                 "client": ("127.0.0.1", 32100), "query_string": b"",
                 "headers": [(b"authorization", b"Bearer " + self.token.encode()),
                             (b"content-type", b"application/json"),
                             (b"content-length", str(len(raw)).encode())]}
        with self.assertRaises(BrokenPipeError):
            await self.app(scope, receive, send)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(sent), 1)


if __name__ == "__main__":
    unittest.main()
