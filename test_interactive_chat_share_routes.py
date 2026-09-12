"""In-process synthetic HTTP/stream checks; no monolith, provider or live data."""
import asyncio
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from interactive_chat_share_routes import create_interactive_chat_share_router, COOKIE
from interactive_chat_shares import InteractiveChatShareStore, csrf_token
from public_chat_transcript import PublicTranscriptError
from interactive_chat_controls import ChatControlError
import interactive_chat_share_web as web


class InteractiveShareRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="interactive-share-http-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "shares"
        self.origin = "https://share.example.test"
        self.sessions = {"chat-one"}
        self.load = mock.AsyncMock(return_value={"revision": "1", "messages": [], "busy": False})
        self.submit = mock.AsyncMock(return_value={"accepted": True, "queued": True})
        self.save = mock.AsyncMock(return_value="private-file-reference")
        self.wait = mock.AsyncMock(return_value=False)
        self.control = mock.AsyncMock(return_value={"accepted": True, "result": {"ok": True}})
        def authorize(request):
            if request.headers.get("x-agentsdock-token") != "synthetic-native-admin" or request.headers.get("origin") or request.headers.get("cookie"):
                raise HTTPException(403, "Native administration required")
        self.router = create_interactive_chat_share_router(storage_root=self.root, authorize=authorize,
            session_exists=lambda session: session in self.sessions, public_base_url=lambda: self.origin,
            load_transcript=self.load, submit_prompt=self.submit, save_upload=self.save, wait_for_change=self.wait,
            chat_control=self.control)
        app = FastAPI()
        app.include_router(self.router)
        self.client = TestClient(app, base_url=self.origin, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.admin = "/api/admin/interactive-chat-shares/chat-one"
        self.admin_headers = {"X-AgentsDock-Token": "synthetic-native-admin"}

    def create(self):
        response = self.client.post(self.admin, headers=self.admin_headers, json={"confirmed_interactive": True})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def redeem(self, share):
        path, invite = share["path"].split("#invite=")
        response = self.client.post(path + "/redeem", headers={"Origin": self.origin}, json={"invitation_token": invite})
        self.assertEqual(response.status_code, 200, response.text)
        return path, {"Origin": self.origin, "X-Chat-CSRF": response.json()["csrf"]}

    def test_scanner_get_never_redeems_and_cookie_is_secure_scoped(self):
        share = self.create()
        path = share["path"].split("#")[0]
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.head(path).status_code, 200)
        self.assertEqual(self.client.get(path + "/state").status_code, 404)
        response = self.client.post(path + "/redeem", headers={"Origin": self.origin}, json={"invitation_token": share["path"].split("#invite=")[1]})
        cookie = response.headers["set-cookie"]
        for flag in ("Secure", "HttpOnly", "SameSite=strict", "Path=" + path):
            self.assertIn(flag, cookie)
        self.assertEqual(self.client.post(path + "/redeem", headers={"Origin": self.origin}, json={"invitation_token": share["path"].split("#invite=")[1]}).status_code, 404)
        self.assertEqual(self.client.get(path + "/state").json()["messages"], [])
        self.assertNotIn("session_id", self.client.get(path + "/state").text)

    def test_prompt_origin_csrf_allowlist_and_exact_durable_retry(self):
        path, headers = self.redeem(self.create())
        payload = {"prompt": "Synthetic question", "upload_ids": [], "request_id": "request_0000000001"}
        for wrong in ({}, {**headers, "Origin": "https://other.example.test"}, {"Origin": self.origin}):
            self.assertEqual(self.client.post(path + "/prompts", headers=wrong, json=payload).status_code, 403)
        for forbidden in ("session_id", "backend", "file_ids", "references", "purpose"):
            self.assertEqual(self.client.post(path + "/prompts", headers=headers, json={**payload, forbidden: "not-authorized"}).status_code, 400)
        first = self.client.post(path + "/prompts", headers=headers, json=payload)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(self.client.post(path + "/prompts", headers=headers, json=payload).json(), first.json())
        self.assertEqual(self.client.post(path + "/prompts", headers=headers, json={**payload, "prompt": "Changed"}).status_code, 409)
        self.submit.assert_awaited_once()
        self.assertEqual(self.submit.call_args.args[0], "chat-one")
        self.assertEqual(self.submit.call_args.args[2:], ("Synthetic question", [], "request_0000000001"))

    def test_cross_site_invitation_navigation_only_exposes_static_shell(self):
        share = self.create()
        path = share["path"].split("#")[0]
        navigation = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
        for method in ("GET", "HEAD"):
            response = self.client.request(method, path, headers=navigation)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("set-cookie", response.headers)
        self.assertEqual(self.client.get(path, headers={**navigation, "Sec-Fetch-Dest": "iframe"}).status_code, 403)
        self.assertEqual(self.client.get(path + "/state", headers=navigation).status_code, 403)
        self.assertEqual(self.client.post(path + "/redeem", headers={**navigation, "Origin": self.origin},
            json={"invitation_token": share["path"].split("#invite=")[1]}).status_code, 403)
        self.load.assert_not_called()
        self.redeem(share)  # Cross-site probes did not consume the invitation.

    def test_callback_errors_are_private_and_indeterminate_work_is_not_replayed(self):
        path, headers = self.redeem(self.create())
        self.submit.side_effect = HTTPException(400, "private internal filesystem detail")
        payload = {"prompt": "Question", "request_id": "request_0000000002"}
        first = self.client.post(path + "/prompts", headers=headers, json=payload)
        self.assertEqual(first.status_code, 503)
        self.assertNotIn("private internal", first.text)
        self.assertEqual(self.client.post(path + "/prompts", headers=headers, json=payload).status_code, 409)
        self.submit.assert_awaited_once()
        self.load.side_effect = PublicTranscriptError("private source path")
        response = self.client.get(path + "/state")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private source", response.text)

    def test_exact_queued_message_id_survives_acceptance_and_durable_retry(self):
        path, headers = self.redeem(self.create())
        self.submit.return_value = {"accepted": True, "queued": True, "queued_id": "queued_synthetic_001"}
        payload = {"prompt": "Queued message", "request_id": "request_queued_001"}
        first = self.client.post(path + "/prompts", headers=headers, json=payload)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["queued_id"], "queued_synthetic_001")
        self.assertEqual(self.client.post(path + "/prompts", headers=headers, json=payload).json(), first.json())
        self.submit.assert_awaited_once()

    def test_upload_response_has_no_private_reference_and_cross_share_attach_denied(self):
        first = self.create()
        second = self.create()
        path, headers = self.redeem(first)
        response = self.client.post(path + "/uploads", headers={**headers, "Content-Type": "text/plain", "X-Chat-Filename": "note.txt"}, content=b"Safe synthetic file")
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(set(response.json()), {"id", "name", "media_type", "byte_size"})
        self.assertNotIn("private-file", response.text)
        other, other_headers = self.redeem(second)
        response = self.client.post(other + "/prompts", headers=other_headers, json={"prompt": "Question", "request_id": "request_0000000003", "upload_ids": [response.json()["id"]]})
        self.assertEqual(response.status_code, 404)
        self.submit.assert_not_called()

    def test_failed_upload_keeps_charged_reservation_and_deleted_chat_denies(self):
        share = self.create()
        path, headers = self.redeem(share)
        self.save.side_effect = RuntimeError("private upload path")
        response = self.client.post(path + "/uploads", headers={**headers, "Content-Type": "text/plain", "X-Chat-Filename": "note.txt"}, content=b"bytes")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private upload", response.text)
        with InteractiveChatShareStore.open_existing(self.root)._connection() as db:
            self.assertEqual(tuple(db.execute("SELECT byte_size,private_ref FROM interactive_uploads").fetchone()), (5, None))
        self.sessions.clear()
        self.assertEqual(self.client.get(path + "/state").status_code, 404)

    def test_stream_keepalive_does_not_load_transcript_and_revision_signal_does(self):
        share = self.create()
        path, _ = self.redeem(share)
        cookie = next(cookie.value for cookie in self.client.cookies.jar if cookie.name == COOKIE)
        request = SimpleNamespace(base_url=self.origin + "/", url=SimpleNamespace(query=""),
            headers={"cookie": COOKIE + "=" + cookie}, is_disconnected=mock.AsyncMock(return_value=False))
        endpoint = next(route.endpoint for route in self.router.routes if route.path.endswith("/{share_id}/events"))
        self.wait.side_effect = [False, True]
        async def consume():
            response = await endpoint(share["id"], request)
            iterator = response.body_iterator
            self.assertIn("event: state", await anext(iterator))
            self.assertEqual(await anext(iterator), ": keepalive\n\n")
            self.assertEqual(self.load.await_count, 1)
            with mock.patch("interactive_chat_share_routes.asyncio.sleep", new=mock.AsyncMock()):
                self.assertIn("event: state", await anext(iterator))
            self.assertEqual(self.load.await_count, 2)
            await iterator.aclose()
        asyncio.run(consume())

    def test_stream_disconnect_before_body_starts_releases_admission(self):
        share = self.create()
        self.redeem(share)
        cookie = next(cookie.value for cookie in self.client.cookies.jar if cookie.name == COOKIE)
        request = SimpleNamespace(base_url=self.origin + "/", url=SimpleNamespace(query=""),
            headers={"cookie": COOKIE + "=" + cookie}, is_disconnected=mock.AsyncMock(return_value=False))
        endpoint = next(route.endpoint for route in self.router.routes if route.path.endswith("/{share_id}/events"))
        async def disconnect():
            response = await endpoint(share["id"], request)
            def leases():
                return inspect.getclosurevars(type(response).__call__).nonlocals["streams"]
            self.assertEqual(leases(), 0)  # Merely creating a response owns no slot.
            async def send(message):
                self.assertEqual(message["type"], "http.response.start")
                self.assertEqual(leases(), 1)
                raise OSError("Synthetic connection closed before response headers")
            with self.assertRaises(ClientDisconnect):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, mock.AsyncMock(), send)
            self.assertEqual(leases(), 0)
            await response.body_iterator.aclose()
        asyncio.run(disconnect())
        self.load.assert_not_awaited()
        self.wait.assert_not_awaited()

    def test_upload_disconnect_joins_committed_save_and_retains_accounting(self):
        share = self.create()
        self.redeem(share)
        cookie = next(cookie.value for cookie in self.client.cookies.jar if cookie.name == COOKIE)
        async def chunks():
            yield b"safe bytes"
        request = SimpleNamespace(base_url=self.origin + "/", url=SimpleNamespace(query=""), stream=chunks,
            headers={"cookie": COOKIE + "=" + cookie, "origin": self.origin, "x-chat-csrf": csrf_token(cookie),
                "x-chat-filename": "note.txt", "content-type": "text/plain"})
        endpoint = next(route.endpoint for route in self.router.routes if route.path.endswith("/{share_id}/uploads"))
        original = InteractiveChatShareStore.complete_upload
        async def exercise():
            entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()
            async def save(*_):
                entered.set()
                await release.wait()
                return "private-committed-file"
            def finish(*args):
                original(*args)
                loop.call_soon_threadsafe(completed.set)
            self.save.side_effect = save
            with mock.patch.object(InteractiveChatShareStore, "complete_upload", finish):
                task = asyncio.create_task(endpoint(share["id"], request))
                await asyncio.wait_for(entered.wait(), 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                release.set()
                await asyncio.wait_for(completed.wait(), 2)
                await asyncio.sleep(0)
        asyncio.run(exercise())
        with InteractiveChatShareStore.open_existing(self.root)._connection() as db:
            self.assertEqual(tuple(db.execute("SELECT byte_size,private_ref FROM interactive_uploads").fetchone()), (10, "private-committed-file"))

    def test_native_state_preserves_exact_events_and_rejects_foreign_chat(self):
        path, _ = self.redeem(self.create())
        native = {"revision": "native-1", "session": {"id": "chat-one"}, "events": [
            {"id": "native-event-one", "session_id": "chat-one", "seq": 9, "type": "assistant_text", "text": "Answer"}],
            "queue": [], "active": False, "goal": None, "jobs": [], "codex_runtime": {}, "claude_runtime": {},
            "health": {}, "runtime_catalog": {}, "hasMoreEvents": True, "nextTimelineBefore": 9, "eventsTotal": 9}
        self.load.return_value = native
        response = self.client.get(path + "/state")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["events"], native["events"])
        self.assertNotIn("messages", response.json())
        self.load.return_value = {**native, "session": {"id": "other-chat"}}
        self.assertEqual(self.client.get(path + "/state").status_code, 503)
        self.load.return_value = {**native, "events": [{"session_id": "other-chat"}]}
        self.assertEqual(self.client.get(path + "/state").status_code, 503)

    def test_control_receipt_result_and_trusted_scope_replay_without_duplicate(self):
        share = self.create()
        path, headers = self.redeem(share)
        payload = {"action": "turn.steer", "payload": {"prompt": "New direction"}, "request_id": "control_000000001"}
        first = self.client.post(path + "/controls", headers=headers, json=payload)
        self.assertEqual(first.status_code, 202, first.text)
        self.assertEqual(first.json()["result"], {"ok": True})
        self.assertEqual(self.client.post(path + "/controls", headers=headers, json=payload).json(), first.json())
        self.control.assert_awaited_once_with("chat-one", "turn.steer", {"prompt": "New direction"},
            share_id=share["id"], request_id=payload["request_id"])
        self.assertEqual(self.client.post(path + "/controls", headers=headers,
            json={**payload, "action": "turn.stop", "payload": {}}).status_code, 409)

    def test_control_typed_denial_is_durable_but_unknown_callback_error_is_indeterminate(self):
        path, headers = self.redeem(self.create())
        self.control.side_effect = ChatControlError("forbidden")
        payload = {"action": "goal.delete", "payload": {}, "request_id": "control_000000002"}
        first = self.client.post(path + "/controls", headers=headers, json=payload)
        self.assertEqual(first.status_code, 403)
        self.assertEqual(self.client.post(path + "/controls", headers=headers, json=payload).json(), first.json())
        self.control.assert_awaited_once()
        self.control.side_effect = HTTPException(409, "private path after possible write")
        payload["request_id"] = "control_000000003"
        first = self.client.post(path + "/controls", headers=headers, json=payload)
        self.assertEqual(first.status_code, 503)
        self.assertNotIn("private path", first.text)
        self.assertEqual(self.client.post(path + "/controls", headers=headers, json=payload).status_code, 409)
        self.assertEqual(self.control.await_count, 2)

    def test_explicit_native_reads_have_no_write_ledger_or_file_proxy(self):
        path, headers = self.redeem(self.create())
        self.control.return_value = {"events": [], "has_more": False}
        for action in ("timeline.older", "timeline.around", "timeline.trace", "timeline.index", "jobs.runs", "runtime.catalog", "handoffs.get"):
            response = self.client.post(path + "/controls", headers=headers,
                json={"action": action, "payload": {"id": "handoff-synthetic"} if action == "handoffs.get" else {}, "request_id": "read_request_0001"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["result"], self.control.return_value)
            self.assertEqual(self.control.call_args.kwargs, {})
        with InteractiveChatShareStore.open_existing(self.root)._connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM interactive_submissions").fetchone()[0], 0)
        for action in ("files.read", "files.list", "terminal.execute", "admin.call", "http.proxy", []):
            response = self.client.post(path + "/controls", headers=headers,
                json={"action": action, "payload": {}, "request_id": "read_request_0001"})
            self.assertEqual(response.status_code, 400)

    def test_assets_are_exact_generated_keys_not_filesystem_routes(self):
        assets = {"native-entry.js": ("text/javascript", b"export const native = true;"),
            "fonts/example.woff2": ("font/woff2", b"synthetic-font")}
        with mock.patch.object(web, "ASSETS", assets, create=True), mock.patch.object(Path, "read_bytes", side_effect=AssertionError("No guest asset filesystem lookup")):
            response = self.client.get("/interactive-chat/assets/native-entry.js")
            self.assertEqual((response.status_code, response.content), (200, assets["native-entry.js"][1]))
            self.assertEqual(self.client.head("/interactive-chat/assets/fonts/example.woff2").status_code, 200)
            for path in ("unknown.js", "files/private.txt", "%2e%2e%2fprivate.txt"):
                self.assertEqual(self.client.get("/interactive-chat/assets/" + path).status_code, 404)
        self.assertNotIn("'unsafe-inline'", response.headers["content-security-policy"].split("script-src")[1].split(";")[0])


if __name__ == "__main__":
    unittest.main()
