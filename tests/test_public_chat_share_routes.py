"""Public snapshot routes tested in-process with temporary state and explicit auth."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import public_chat_share_routes as routes
from public_chat_shares import PublicChatShareStore, PublicChatShareValidationError
from public_chat_transcript import read_public_transcript


class PublicChatShareRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = self.root / "public-snapshots"
        self.events = self.root / "events.jsonl"
        self.events.write_text(json.dumps({"type": "turn_started", "prompt": "Reviewed question"}) + "\n")
        self.clock = 1000
        self.sessions = {"chat-one"}
        self.base = "https://share.example.test"
        self.authorize = mock.Mock(side_effect=self.require_auth)
        self.exists = mock.Mock(side_effect=lambda session: session in self.sessions)
        self.load = mock.Mock(side_effect=lambda session, boundary: read_public_transcript(
            self.events, lambda event: event, through_bytes=boundary))
        self.store_factory = mock.Mock(side_effect=lambda root: PublicChatShareStore(root, now=lambda: self.clock))
        self.store_factory.open_existing = mock.Mock(side_effect=lambda root: PublicChatShareStore.open_existing(root, now=lambda: self.clock))
        patcher = mock.patch.object(routes, "PublicChatShareStore", self.store_factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(routes.create_public_chat_share_router(
            storage_root=self.storage, authorize=self.authorize, session_exists=self.exists,
            load_transcript=self.load, public_base_url=lambda: self.base,
        ))
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.admin = "/api/admin/chat-shares/chat-one"
        self.auth = {"Authorization": "Bearer isolated-management-only"}

    @staticmethod
    def require_auth(request):
        if request.headers.get("authorization") != "Bearer isolated-management-only":
            raise HTTPException(401, "Authentication required")

    def preview(self):
        response = self.client.post(self.admin + "/preview", headers=self.auth, json={})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def create(self, preview=None, **options):
        preview = preview or self.preview()
        response = self.client.post(self.admin, headers=self.auth, json={
            "confirmed_public": True, "through_bytes": preview["through_bytes"],
            "digest": preview["digest"], **options,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_preview_has_warning_and_creates_no_public_capability(self):
        response = self.client.post(self.admin + "/preview", headers=self.auth, json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json()["messages"], [{"role": "user", "text": "Reviewed question"}])
        self.assertIn("Anyone with this link", response.json()["warning"])
        self.assertIn("New messages are not added", response.json()["warning"])
        self.assertNotIn("path", response.json())
        self.assertFalse(self.storage.exists())
        self.store_factory.assert_not_called()

    def test_publish_requires_explicit_boolean_confirmation_and_valid_preview(self):
        preview = self.preview()
        valid = {"through_bytes": preview["through_bytes"], "digest": preview["digest"]}
        for value in ({}, {**valid}, {**valid, "confirmed_public": False},
                      {**valid, "confirmed_public": 1}, {**valid, "confirmed_public": "true"},
                      {"confirmed_public": True}, {**valid, "confirmed_public": True, "through_bytes": True},
                      {**valid, "confirmed_public": True, "digest": "invalid"},
                      {**valid, "confirmed_public": True, "unexpected": "option"}):
            with self.subTest(value=value):
                response = self.client.post(self.admin, headers=self.auth, json=value)
                self.assertEqual(response.status_code, 400, response.text)
        self.store_factory.assert_not_called()
        self.assertEqual(self.load.call_count, 1)

    def test_authentication_denial_precedes_every_management_callback(self):
        for method, suffix in (("POST", "/preview"), ("POST", ""), ("GET", ""), ("DELETE", "/share_fake")):
            with self.subTest(method=method, suffix=suffix):
                response = self.client.request(method, self.admin + suffix, json={})
                self.assertEqual(response.status_code, 401, response.text)
        self.exists.assert_not_called()
        self.load.assert_not_called()
        self.store_factory.assert_not_called()
        self.assertFalse(self.storage.exists())

    def test_broken_auth_callback_cannot_publish_or_read_private_state(self):
        self.authorize.side_effect = RuntimeError("authentication unavailable")
        response = self.client.post(self.admin + "/preview", headers=self.auth, json={})
        self.assertEqual(response.status_code, 500)
        self.exists.assert_not_called()
        self.load.assert_not_called()
        self.store_factory.assert_not_called()

    def test_nonexistent_session_and_invalid_session_id_do_not_load_or_create(self):
        for session in ("missing", "chat%20one"):
            response = self.client.post("/api/admin/chat-shares/" + session + "/preview", headers=self.auth, json={})
            self.assertEqual(response.status_code, 404, response.text)
        self.load.assert_not_called()
        self.store_factory.assert_not_called()

    def test_reviewed_prefix_is_immutable_and_public_link_is_not_api_authority(self):
        preview = self.preview()
        with self.events.open("a") as stream:
            stream.write(json.dumps({"type": "assistant_text", "text": "NEW PRIVATE MESSAGE"}) + "\n")
        share = self.create(preview)
        self.assertEqual(share["url"], self.base + share["path"])
        self.assertNotIn("token", share)
        self.assertNotIn("session_id", share)
        auth_count, load_count = self.authorize.call_count, self.load.call_count
        self.events.unlink()
        public = self.client.get(share["path"])
        self.assertEqual(public.status_code, 200, public.text)
        self.assertIn("Reviewed question", public.text)
        self.assertNotIn("NEW PRIVATE MESSAGE", public.text)
        self.assertNotIn("chat-one", public.text)
        self.assertEqual(self.authorize.call_count, auth_count)
        self.assertEqual(self.load.call_count, load_count)
        for header in ({}, {"Authorization": "Bearer " + share["path"].rsplit("/", 1)[1]}):
            denied = self.client.get(self.admin, headers=header)
            self.assertEqual(denied.status_code, 401)
        for path in (share["path"] + "/api", share["path"] + "/events", "/api/sessions/chat-one"):
            self.assertEqual(self.client.get(path).status_code, 404)
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.client.request(method, share["path"], json={}).status_code, 405)
        self.assertEqual(self.client.head(share["path"]).content, b"")

    def test_public_html_is_isolated_no_store_and_escapes_untrusted_content(self):
        self.events.write_text(json.dumps({"type": "turn_started", "prompt": '<script>alert(1)</script> ![x](https://remote.invalid/image)'}) + "\n")
        share = self.create(title="<b>Shared title</b>")
        response = self.client.get(share["path"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("no-store", response.headers["cache-control"])
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        self.assertIn("noindex", response.headers["x-robots-tag"])
        self.assertNotIn("<script>", response.text)
        self.assertNotIn("<img", response.text)
        self.assertIn("&lt;script&gt;", response.text)
        self.assertNotIn("<b>Shared title</b>", response.text)

    def test_changed_prefix_digest_fails_before_persisting_share(self):
        preview = self.preview()
        self.events.write_bytes(self.events.read_bytes().replace(b"Reviewed", b"Replaced"))
        response = self.client.post(self.admin, headers=self.auth, json={
            "confirmed_public": True, "through_bytes": preview["through_bytes"], "digest": preview["digest"],
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("preview it again", response.json()["detail"])
        self.store_factory.assert_not_called()

    def test_invalid_public_origin_fails_before_persisting_share(self):
        preview = self.preview()
        self.base = "http://insecure.example.test"
        response = self.client.post(self.admin, headers=self.auth, json={
            "confirmed_public": True, "through_bytes": preview["through_bytes"], "digest": preview["digest"],
        })
        self.assertEqual(response.status_code, 400, response.text)
        self.store_factory.assert_not_called()

    def test_revoked_missing_expired_and_malformed_public_links_are_uniform(self):
        revoked = self.create()
        expired = self.create(expires_at=1001)
        result = self.client.delete(self.admin + "/" + revoked["share_id"], headers=self.auth)
        self.assertEqual(result.json(), {"revoked": True})
        self.clock = 1002
        missing = "/share/" + "A" * 43
        responses = [self.client.get(path) for path in (
            revoked["path"], expired["path"], missing, "/share/malformed", missing + "?format=json",
        )]
        for response in responses:
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.content, responses[0].content)
            self.assertEqual(response.headers.get("cache-control"), responses[0].headers.get("cache-control"))
        self.assertEqual(responses[0].text, "Shared conversation unavailable.")

    def test_listing_and_revocation_remain_authenticated_and_session_scoped_after_chat_deletion(self):
        share = self.create()
        self.sessions.clear()
        listing = self.client.get(self.admin, headers=self.auth)
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertEqual(listing.json()["shares"][0]["share_id"], share["share_id"])
        self.assertNotIn(share["path"].rsplit("/", 1)[1], listing.text)
        wrong = self.client.delete("/api/admin/chat-shares/other/" + share["share_id"], headers=self.auth)
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(self.client.get(share["path"]).status_code, 200)
        self.assertEqual(self.client.delete(self.admin + "/" + share["share_id"], headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get(share["path"]).status_code, 404)

    def test_malformed_and_oversized_management_bodies_are_bounded(self):
        for content, content_type, expected in ((b"{}", "text/plain", 415), (b"bad-json", "application/json", 400),
                                                 (b"[]", "application/json", 400), (b" " * 8193, "application/json", 413),
                                                 (b'{"option":true}', "application/json", 400)):
            with self.subTest(expected=expected, content_type=content_type):
                response = self.client.post(self.admin + "/preview", content=content,
                                            headers={**self.auth, "Content-Type": content_type})
                self.assertEqual(response.status_code, expected, response.text)
        self.load.assert_not_called()
        self.store_factory.assert_not_called()

    def test_excessively_nested_management_json_is_a_client_error(self):
        content = b'{"option":' + b'[' * 1200 + b'0' + b']' * 1200 + b'}'
        response = self.client.post(self.admin + "/preview", content=content,
                                    headers={**self.auth, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400, response.text)
        self.load.assert_not_called()
        self.store_factory.assert_not_called()

    def test_anonymous_unknown_token_does_not_initialize_any_storage(self):
        response = self.client.get("/share/" + "A" * 43)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertFalse(self.storage.exists())
        self.store_factory.assert_not_called()
        self.load.assert_not_called()

    def test_store_is_cached_across_creation_listing_and_public_views(self):
        share = self.create()
        self.client.get(share["path"])
        self.client.get(share["path"])
        self.client.get(self.admin, headers=self.auth)
        self.create()
        self.assertEqual(self.store_factory.call_count, 1)

    def test_viewer_unexpected_failure_keeps_security_headers_and_hides_details(self):
        share = self.create()
        with mock.patch.object(routes, "render_public_chat_html", side_effect=RuntimeError("private internal detail")):
            response = self.client.get(share["path"])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertIn("sandbox", response.headers["content-security-policy"])
        self.assertNotIn("private internal detail", response.text)

    def test_recursive_management_decoder_failure_is_a_client_error(self):
        with mock.patch.object(routes.json, "loads", side_effect=RecursionError):
            response = self.client.post(self.admin + "/preview", content=b"{}",
                                        headers={**self.auth, "Content-Type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.load.assert_not_called()

    def test_invalid_list_and_revoke_session_ids_are_bounded(self):
        for method, suffix in (("GET", ""), ("DELETE", "/share_fake")):
            response = self.client.request(method, "/api/admin/chat-shares/" + "x" * 129 + suffix, headers=self.auth)
            self.assertEqual(response.status_code, 404)
        self.store_factory.assert_not_called()

    def test_cold_public_view_uses_read_only_open_existing_and_then_caches(self):
        existing = PublicChatShareStore(self.storage, now=lambda: self.clock)
        share = existing.create_share("chat-one", [{"role": "user", "text": "Existing snapshot"}])
        for _ in range(2):
            response = self.client.get("/share/" + share["token"])
            self.assertEqual(response.status_code, 200, response.text)
        self.store_factory.assert_not_called()
        self.store_factory.open_existing.assert_called_once_with(self.storage)

    def test_projection_change_after_review_rejects_creation_even_without_source_change(self):
        preview = self.preview()
        self.load.side_effect = lambda session, boundary: read_public_transcript(
            self.events, lambda event: {**event, "prompt": "New unreviewed text"}, through_bytes=boundary)
        response = self.client.post(self.admin, headers=self.auth, json={
            "confirmed_public": True, "through_bytes": preview["through_bytes"], "digest": preview["digest"],
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.store_factory.assert_not_called()


class PublicShareURLTests(unittest.TestCase):
    def test_only_explicit_https_origins_generate_public_urls(self):
        self.assertIsNone(routes.public_share_url("", "token"))
        self.assertEqual(routes.public_share_url("https://share.example.test/", "token"), "https://share.example.test/share/token")
        for base in ("http://share.example.test", "https://user:password@share.example.test",
                     "https://share.example.test/path", "https://share.example.test?query=yes",
                     "https://share.example.test#fragment", "https://share.example.test /", "//share.example.test"):
            with self.subTest(base=base), self.assertRaises(PublicChatShareValidationError):
                routes.public_share_url(base, "token")

    def test_log_redaction_removes_capability_without_redacting_unrelated_text(self):
        token = "Abc123_-" * 5 + "xyz"
        self.assertEqual(routes.redact_public_share_path("GET /share/" + token + " HTTP/1.1"),
                         "GET /share/<redacted> HTTP/1.1")
        self.assertEqual(routes.redact_public_share_path("ordinary /api/status"), "ordinary /api/status")

    def test_log_redaction_masks_percent_encoded_and_invalid_token_segments(self):
        for token in ("%41" * 43, "%5fA%2D" * 15, "invalid%broken&token"):
            with self.subTest(token=token):
                self.assertEqual(routes.redact_public_share_path("GET /share/" + token + '?x=1 HTTP/1.1'),
                                 'GET /share/<redacted>?x=1 HTTP/1.1')


if __name__ == "__main__":
    unittest.main()
