"""Public snapshot routes tested in-process with temporary state and explicit auth."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import public_chat_share_routes as routes
from public_chat_shares import PublicChatShareStore, PublicChatShareValidationError
from public_chat_transcript import read_public_transcript
from shared_chat_videos import SharedVideoUnavailable


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
        self.load = mock.Mock(side_effect=lambda session, boundary, **options: read_public_transcript(
            self.events, lambda event: event, through_bytes=boundary, **options))
        self.open_video = mock.AsyncMock()
        self.store_factory = mock.Mock(side_effect=lambda root: PublicChatShareStore(root, now=lambda: self.clock))
        self.store_factory.open_existing = mock.Mock(side_effect=lambda root: PublicChatShareStore.open_existing(root, now=lambda: self.clock))
        patcher = mock.patch.object(routes, "PublicChatShareStore", self.store_factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(routes.create_public_chat_share_router(
            storage_root=self.storage, authorize=self.authorize, session_exists=self.exists,
            load_transcript=self.load, public_base_url=lambda: self.base, open_video=self.open_video,
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

    def unlock(self, share, *, token=None, remember=False):
        return self.client.post(share["path"] + "/unlock", headers={"Origin": str(self.client.base_url).rstrip("/")},
            data={"access_token": share["access_token"] if token is None else token, **({"remember": "1"} if remember else {})},
            follow_redirects=False)

    def create_video_share(self):
        self.video_bytes = b"0123456789video-contents"
        self.video_path = self.root / "published.mp4"
        self.video_path.write_bytes(self.video_bytes)
        self.video = {"id": "video_ZmlsZQ." + "a" * 64, "filename": "published.mp4",
                      "content_type": "video/mp4", "size": len(self.video_bytes)}
        self.events.write_text(json.dumps({"type": "artifact_created", "shared_videos": [self.video]}) + "\n")
        self.video_fds = []
        async def open_video(session_id, handle):
            self.assertEqual(session_id, "chat-one")
            self.assertEqual(handle, self.video["id"])
            descriptor = os.open(self.video_path, os.O_RDONLY)
            self.video_fds.append(descriptor)
            return {"file_fd": descriptor, **{key: self.video[key] for key in ("size", "filename", "content_type")}}
        self.open_video.side_effect = open_video
        return self.create()

    def assert_video_fds_closed(self):
        for descriptor in self.video_fds:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_video_cookie_and_bearer_paths_play_seek_and_head_without_native_credentials(self):
        share = self.create_video_share()
        path = share["path"] + "/media/0/0/0"
        self.assertEqual(self.client.get(path).status_code, 404)
        self.open_video.assert_not_called()
        self.assertEqual(self.unlock(share).status_code, 303)
        before = self.load.call_count
        for target in (path, "/share/" + share["access_token"] + "/media/0/0/0"):
            full = self.client.get(target)
            self.assertEqual(full.status_code, 200, full.text)
            self.assertEqual(full.content, self.video_bytes)
            self.assertEqual(full.headers["content-type"], "video/mp4")
            self.assertEqual(full.headers["cache-control"], "no-store")
            self.assertEqual(full.headers["accept-ranges"], "bytes")
            self.assertEqual(full.headers["x-content-type-options"], "nosniff")
            for value, expected, content_range in (
                ("bytes=2-5", self.video_bytes[2:6], f"bytes 2-5/{len(self.video_bytes)}"),
                ("bytes=10-", self.video_bytes[10:], f"bytes 10-{len(self.video_bytes)-1}/{len(self.video_bytes)}"),
                ("bytes=-5", self.video_bytes[-5:], f"bytes {len(self.video_bytes)-5}-{len(self.video_bytes)-1}/{len(self.video_bytes)}"),
            ):
                response = self.client.get(target, headers={"Range": value})
                self.assertEqual(response.status_code, 206, response.text)
                self.assertEqual(response.content, expected)
                self.assertEqual(response.headers["content-range"], content_range)
                self.assertEqual(response.headers["content-length"], str(len(expected)))
            head = self.client.head(target)
            self.assertEqual(head.status_code, 200)
            self.assertEqual(head.content, b"")
            self.assertEqual(head.headers["content-length"], str(len(self.video_bytes)))
            ranged_head = self.client.head(target, headers={"Range": "bytes=0-2"})
            self.assertEqual(ranged_head.status_code, 206)
            self.assertEqual(ranged_head.headers["content-length"], "3")
            self.assertEqual(ranged_head.content, b"")
        self.assertEqual(self.load.call_count, before, "Playback must not rescan the source chat")
        self.assert_video_fds_closed()

    def test_video_access_is_exact_share_page_and_not_an_arbitrary_file_route(self):
        share = self.create_video_share()
        other = self.create()
        self.unlock(share)
        path = share["path"] + "/media/0/0/0"
        for target, headers in (
            (other["path"] + "/media/0/0/0", {}),
            (share["path"] + "/media/1/0/0", {}),
            (share["path"] + "/media/0/0/1", {}),
            (share["path"] + "/media/0/-1/0", {}),
            (share["path"] + "/media/0/0/private.mp4", {}),
            (path + "?file=/private/path", {}),
            (path, {"Origin": "https://attacker.test"}),
            (path, {"Sec-Fetch-Site": "cross-site"}),
            (path, {"Sec-Fetch-Site": "same-site"}),
            (path, {"Cookie": routes.HTTP_SNAPSHOT_COOKIE + "=" + share["access_token"] + "; "
                + routes.HTTP_SNAPSHOT_COOKIE + "=" + share["access_token"]}),
        ):
            with self.subTest(target=target, headers=headers):
                response = self.client.get(target, headers=headers)
                self.assertEqual(response.status_code, 404, response.text)
                self.assertNotIn(self.video["id"], response.text)
        self.open_video.assert_not_called()
        self.assertEqual(self.client.delete(self.admin + "/" + share["share_id"], headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get("/share/" + share["access_token"] + "/media/0/0/0").status_code, 404)
        self.open_video.assert_not_called()

    def test_video_invalid_range_and_changed_registry_metadata_release_file_descriptor(self):
        share = self.create_video_share()
        self.unlock(share)
        path = share["path"] + "/media/0/0/0"
        for value in ("bytes=9999-", "bytes=4-2", "bytes=0-1,4-5", "bytes=-0", "bananas"):
            with self.subTest(value=value):
                response = self.client.get(path, headers={"Range": value})
                self.assertEqual(response.status_code, 416, response.text)
                self.assertEqual(response.headers["content-range"], f"bytes */{len(self.video_bytes)}")
        self.assert_video_fds_closed()
        original_open = self.open_video.side_effect
        async def changed(session_id, handle):
            value = await original_open(session_id, handle)
            return {**value, "size": value["size"] + 1}
        self.open_video.side_effect = changed
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assert_video_fds_closed()

    def test_video_rechecks_revocation_after_native_open_before_returning_any_bytes(self):
        share = self.create_video_share()
        self.unlock(share)
        original_open = self.open_video.side_effect
        async def revoked(session_id, handle):
            value = await original_open(session_id, handle)
            PublicChatShareStore(self.storage, now=lambda: self.clock).revoke_share(share["share_id"], session_id="chat-one")
            return value
        self.open_video.side_effect = revoked
        response = self.client.get(share["path"] + "/media/0/0/0")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertNotEqual(response.content, self.video_bytes)
        self.assert_video_fds_closed()

    def test_missing_registry_video_has_uniform_unavailable_response_without_native_details(self):
        share = self.create_video_share()
        self.unlock(share)
        self.open_video.side_effect = SharedVideoUnavailable()
        response = self.client.get(share["path"] + "/media/0/0/0")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.text, "Shared video unavailable.")
        self.assertNotIn(self.video["id"], response.text)
        self.assertNotIn("chat-one", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_common_url_requires_separate_token_and_remembers_only_scoped_http_cookie(self):
        share = self.create(title="Private snapshot title")
        self.assertEqual(share["path"], "/shared-chat/" + share["share_id"])
        self.assertNotIn(share["access_token"], share["path"])
        self.assertNotIn(share["access_token"], share["url"])
        self.assertTrue(share["token_url"].endswith("/share/" + share["access_token"]))
        for method in ("GET", "HEAD"):
            response = self.client.request(method, share["path"])
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("Private snapshot title", response.text)
            self.assertNotIn("Reviewed question", response.text)
        gate = self.client.get(share["path"])
        self.assertIn('name="access_token"', gate.text)
        self.assertIn("form-action 'self'", gate.headers["content-security-policy"])
        self.assertEqual(gate.headers["referrer-policy"], "same-origin")
        self.assertNotIn("<script", gate.text)
        entered = self.unlock(share, remember=True)
        self.assertEqual(entered.status_code, 303, entered.text)
        self.assertEqual(entered.headers["location"], share["path"] + "?page=0#conversation-end")
        cookie = entered.headers["set-cookie"]
        self.assertTrue(cookie.startswith(routes.HTTP_SNAPSHOT_COOKIE + "="))
        for flag in ("HttpOnly", "SameSite=strict", "Path=" + share["path"], "Max-Age=2592000"):
            self.assertIn(flag, cookie)
        self.assertNotIn("Secure", cookie)
        self.assertNotIn("Domain=", cookie)
        viewed = self.client.get(share["path"])
        self.assertIn("Private snapshot title", viewed.text)
        self.assertIn("Reviewed question", viewed.text)
        self.assertIn("form-action 'none'", viewed.headers["content-security-policy"])
        self.assertEqual(viewed.headers["referrer-policy"], "no-referrer")
        self.assertNotIn(share["access_token"], viewed.text)
        listing = self.client.get(self.admin, headers=self.auth)
        self.assertNotIn(share["access_token"], listing.text)
        self.assertNotIn("access_token", listing.text)

    def test_https_cookie_and_common_url_do_not_accept_other_share_token_or_duplicate_cookies(self):
        self.client.base_url = self.base
        first = self.create(title="First private title")
        second = self.create(title="Second private title")
        wrong = self.unlock(second, token=first["access_token"])
        self.assertEqual(wrong.status_code, 403)
        self.assertNotIn(first["access_token"], wrong.text)
        self.assertNotIn("Second private title", wrong.text)
        accepted = self.unlock(first)
        self.assertEqual(accepted.status_code, 303)
        self.assertTrue(accepted.headers["set-cookie"].startswith(routes.SNAPSHOT_COOKIE + "="))
        self.assertIn("Secure", accepted.headers["set-cookie"])
        self.assertNotIn("Max-Age", accepted.headers["set-cookie"])
        cookie = f'{routes.SNAPSHOT_COOKIE}={first["access_token"]}'
        self.assertNotIn("Second private title", self.client.get(second["path"], headers={"Cookie": cookie}).text)
        self.assertNotIn("First private title", self.client.get(first["path"], headers={"Cookie": cookie + "; " + cookie}).text)
        self.assertIn("First private title", self.client.get(first["path"]).text)

    def test_unlock_rejects_cross_origin_malformed_and_oversized_posts_without_leaking_token(self):
        share = self.create(title="Hidden title")
        origin = str(self.client.base_url).rstrip("/")
        payload = {"access_token": share["access_token"]}
        for headers in ({}, {"Origin": "null"}, {"Origin": "https://other.example.test"},
                        {"Origin": origin, "Sec-Fetch-Site": "cross-site"}):
            response = self.client.post(share["path"] + "/unlock", headers=headers, data=payload)
            self.assertEqual(response.status_code, 403)
            self.assertNotIn("Hidden title", response.text)
            self.assertNotIn(share["access_token"], response.text)
            self.assertNotIn("set-cookie", response.headers)
        for body in ("access_token=bad", "access_token=" + share["access_token"] + "&access_token=" + share["access_token"],
                     "access_token=" + share["access_token"] + "&unexpected=1"):
            response = self.client.post(share["path"] + "/unlock", content=body,
                headers={"Origin": origin, "Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(response.status_code, 403)
            self.assertNotIn(share["access_token"], response.text)
        self.assertEqual(self.client.post(share["path"] + "/unlock", headers={"Origin": origin}, json=payload).status_code, 415)
        self.assertEqual(self.client.post(share["path"] + "/unlock", headers={"Origin": origin,
            "Content-Type": "application/x-www-form-urlencoded"}, content="x" * 1025).status_code, 413)

    def test_cold_guessed_common_url_or_unlock_never_initializes_storage(self):
        path = "/shared-chat/share_" + "0" * 32
        self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get(path, headers={"Cookie": routes.HTTP_SNAPSHOT_COOKIE + "=" + "A" * 43}).status_code, 200)
        response = self.client.post(path + "/unlock", headers={"Origin": "http://testserver"}, data={"access_token": "A" * 43})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(self.storage.exists())
        self.store_factory.assert_not_called()
        self.load.assert_not_called()

    def test_revocation_and_expiry_deny_every_remembered_snapshot_read(self):
        revoked = self.create(title="Revoked private title")
        expired = self.create(title="Expired private title", expires_at=1001)
        self.assertEqual(self.unlock(revoked).status_code, 303)
        self.assertEqual(self.unlock(expired).status_code, 303)
        self.client.delete(self.admin + "/" + revoked["share_id"], headers=self.auth)
        self.clock = 1002
        for share in (revoked, expired):
            page = self.client.get(share["path"] + "?page=0")
            self.assertNotIn(share["title"], page.text)
            self.assertIn('name="access_token"', page.text)
            self.assertEqual(self.unlock(share).status_code, 403)
            self.assertEqual(self.client.get(share["token_url"]).status_code, 404)

    def test_large_streamed_creation_opens_latest_page_at_end_and_pages_back(self):
        def stream(session, boundary, *, message_sink):
            for index in range(450):
                message_sink({"role": "user", "text": f"Message {index}: " + "x" * 5120})
            return {"messages": [], "message_count": 450, "digest": "a" * 64, "through_bytes": 100}
        self.load.side_effect = stream
        response = self.client.post(self.admin, headers=self.auth, json={"confirmed_public": True})
        self.assertEqual(response.status_code, 201, response.text)
        share = response.json()
        self.assertEqual(share["message_count"], 450)
        self.assertEqual(self.load.call_count, 1)
        unlocked = self.unlock(share)
        self.assertEqual(unlocked.headers["location"], share["path"] + "?page=4#conversation-end")
        last = self.client.get(share["path"])
        self.assertIn("Message 449:", last.text)
        self.assertNotIn("Message 0:", last.text)
        self.assertIn("450 messages", last.text)
        self.assertIn('?page=3#conversation-end', last.text)
        self.assertNotIn(share["access_token"], last.text)
        first = self.client.get(share["path"] + "?page=0")
        self.assertIn("Message 0:", first.text)
        self.assertNotIn("Message 449:", first.text)
        bearer = self.client.get(share["token_url"], follow_redirects=False)
        self.assertTrue(bearer.headers["location"].endswith("?page=4#conversation-end"))
        for query in ("?page=-1", "?page=0&page=1", "?access_token=" + share["access_token"]):
            self.assertEqual(self.client.get(share["path"] + query).status_code, 404)

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

    def test_publish_requires_explicit_boolean_confirmation_and_paired_review_fields(self):
        preview = self.preview()
        valid = {"through_bytes": preview["through_bytes"], "digest": preview["digest"]}
        for value in ({}, {**valid}, {**valid, "confirmed_public": False},
                      {**valid, "confirmed_public": 1}, {**valid, "confirmed_public": "true"},
                      {"confirmed_public": True, "through_bytes": preview["through_bytes"]},
                      {"confirmed_public": True, "digest": preview["digest"]},
                      {**valid, "confirmed_public": True, "through_bytes": True},
                      {**valid, "confirmed_public": True, "digest": "invalid"},
                      {**valid, "confirmed_public": True, "unexpected": "option"}):
            with self.subTest(value=value):
                response = self.client.post(self.admin, headers=self.auth, json=value)
                self.assertEqual(response.status_code, 400, response.text)
        self.store_factory.assert_not_called()
        self.assertEqual(self.load.call_count, 1)

    def test_confirmed_only_snapshot_loads_once_and_returns_direct_http_link(self):
        self.base = ""
        self.client.base_url = "http://192.0.2.42:8080"
        response = self.client.post(self.admin, headers=self.auth, json={"confirmed_public": True})
        self.assertEqual(response.status_code, 201, response.text)
        share = response.json()
        self.assertEqual(share["url"], "http://192.0.2.42:8080" + share["path"])
        self.load.assert_called_once_with("chat-one", None, message_sink=mock.ANY)
        self.store_factory.assert_called_once_with(self.storage)
        self.events.write_text(json.dumps({"type": "turn_started", "prompt": "Later private message"}) + "\n")
        page = self.client.get(share["token_url"])
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Reviewed question", page.text)
        self.assertNotIn("Later private message", page.text)
        self.load.assert_called_once_with("chat-one", None, message_sink=mock.ANY)

    def test_explicit_link_origin_overrides_configuration_and_preserves_https_fallback(self):
        share = self.create(base_url="http://192.0.2.42:8080/")
        self.assertEqual(share["url"], "http://192.0.2.42:8080" + share["path"])
        configured = self.create()
        self.assertEqual(configured["url"], self.base + configured["path"])

    def test_create_over_one_connection_returns_token_gated_lan_snapshot(self):
        management_origin = "https://connection.example.test"
        lan_origin = "http://192.0.2.42:7850"
        self.client.base_url = management_origin
        share = self.create(base_url=lan_origin + "/", title="LAN snapshot")
        self.assertEqual(share["url"], lan_origin + share["path"])
        self.assertEqual(share["token_url"], lan_origin + "/share/" + share["access_token"])
        self.assertNotIn(share["access_token"], share["url"])
        self.assertEqual(self.client.base_url.host, "connection.example.test")

        with TestClient(self.client.app, base_url=lan_origin) as lan:
            gate = lan.get(share["path"])
            self.assertEqual(gate.status_code, 200)
            self.assertIn('name="access_token"', gate.text)
            self.assertNotIn("Reviewed question", gate.text)
            self.assertNotIn("LAN snapshot", gate.text)
            denied = lan.post(share["path"] + "/unlock", headers={"Origin": management_origin},
                data={"access_token": share["access_token"]}, follow_redirects=False)
            self.assertEqual(denied.status_code, 403)
            self.assertNotIn("set-cookie", denied.headers)
            entered = lan.post(share["path"] + "/unlock", headers={"Origin": lan_origin},
                data={"access_token": share["access_token"]}, follow_redirects=False)
            self.assertEqual(entered.status_code, 303, entered.text)
            cookie = next(item for item in lan.cookies.jar if item.name == routes.HTTP_SNAPSHOT_COOKIE)
            self.assertFalse(cookie.secure)
            self.assertFalse(cookie.domain_specified)
            self.assertEqual(cookie.path, share["path"])
            self.assertIn("HttpOnly", entered.headers["set-cookie"])
            self.assertIn("SameSite=strict", entered.headers["set-cookie"])
            page = lan.get(entered.headers["location"])
            self.assertEqual(page.status_code, 200)
            self.assertIn("Reviewed question", page.text)
            self.assertNotIn(share["access_token"], page.text)
            self.assertEqual(lan.get(share["token_url"]).status_code, 200)
            # Snapshot links are bearer-token gated, not persisted-origin
            # capabilities; do not accidentally impose interactive semantics.
            self.assertNotIn("Reviewed question", self.client.get(share["path"]).text)
            self.assertEqual(self.client.delete(self.admin + "/" + share["share_id"],
                headers=self.auth).status_code, 200)
            self.assertNotIn("Reviewed question", lan.get(share["path"]).text)
            self.assertEqual(lan.get(share["token_url"]).status_code, 404)

    def test_invalid_explicit_origins_fail_before_loading_or_persisting(self):
        for base in (None, "", False, "ftp://example.test", "http://example.test/private", "http://@example.test",
                     "http://user:password@example.test", "http://example.test?token=value"):
            with self.subTest(base=base):
                response = self.client.post(self.admin, headers=self.auth,
                    json={"confirmed_public": True, "base_url": base})
                self.assertEqual(response.status_code, 400, response.text)
        self.load.assert_not_called()
        self.store_factory.assert_not_called()

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
        public = self.client.get(share["token_url"])
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
        response = self.client.get(share["token_url"])
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
        self.assertEqual(PublicChatShareStore.open_existing(self.storage, now=lambda: self.clock).list_shares("chat-one"), [])

    def test_invalid_public_origin_fails_before_persisting_share(self):
        preview = self.preview()
        self.base = "ftp://unsupported.example.test"
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
            revoked["token_url"], expired["token_url"], missing, "/share/malformed", missing + "?format=json",
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
        self.assertNotIn(share["access_token"], listing.text)
        wrong = self.client.delete("/api/admin/chat-shares/other/" + share["share_id"], headers=self.auth)
        self.assertEqual(wrong.status_code, 404)
        self.assertEqual(self.client.get(share["token_url"]).status_code, 200)
        self.assertEqual(self.client.delete(self.admin + "/" + share["share_id"], headers=self.auth).status_code, 200)
        self.assertEqual(self.client.get(share["token_url"]).status_code, 404)

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
            response = self.client.get(share["token_url"])
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
        self.load.side_effect = lambda session, boundary, **options: read_public_transcript(
            self.events, lambda event: {**event, "prompt": "New unreviewed text"}, through_bytes=boundary, **options)
        response = self.client.post(self.admin, headers=self.auth, json={
            "confirmed_public": True, "through_bytes": preview["through_bytes"], "digest": preview["digest"],
        })
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(PublicChatShareStore.open_existing(self.storage, now=lambda: self.clock).list_shares("chat-one"), [])


class PublicShareURLTests(unittest.TestCase):
    def test_http_and_https_origins_generate_public_urls(self):
        self.assertIsNone(routes.public_share_url("", "token"))
        for base in ("https://share.example.test", "http://192.0.2.42:8080", "http://[2001:db8::42]:8080"):
            self.assertEqual(routes.public_share_url(base + "/", "token"), base + "/share/token")
        for base in ("ftp://share.example.test", "https://user:password@share.example.test",
                     "https://share.example.test/path", "https://share.example.test?query=yes",
                     "https://share.example.test#fragment", "https://share.example.test /", "//share.example.test",
                     "http://example.test:99999", "http://example.test\\path", "http://",
                     "http://@example.test", "http://:@example.test", "http://[fe80::1%25en0]"):
            with self.subTest(base=base), self.assertRaises(PublicChatShareValidationError):
                routes.public_share_url(base, "token")

    def test_origins_follow_browser_case_and_default_port_normalization(self):
        for base, expected in (("http://192.0.2.42:80/", "http://192.0.2.42"),
                               ("HTTPS://SHARE.EXAMPLE.TEST:443/", "https://share.example.test"),
                               ("http://SHARE.EXAMPLE.TEST:8080", "http://share.example.test:8080"),
                               ("http://[2001:0db8:0:0:0:0:0:42]:80", "http://[2001:db8::42]")):
            with self.subTest(base=base):
                self.assertEqual(routes.public_share_url(base, "token"), expected + "/share/token")

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
