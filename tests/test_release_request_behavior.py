import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time

import agent_server
import update_runner
from fastapi import HTTPException
from fastapi.testclient import TestClient


class ReleaseRequestBehaviorTests(unittest.TestCase):
    def test_rate_limit_does_not_trigger_release_page_scraping(self):
        error = HTTPError(update_runner.RELEASES_API_URL, 429, "Too Many Requests", {"Retry-After": "120"}, None)
        with patch.object(update_runner, "download_bytes", side_effect=error) as download:
            with self.assertRaises(Exception) as raised:
                update_runner.check_release(Path("unused.pem"))
        self.assertEqual(download.call_count, 1, "A throttled request must not trigger more requests")
        self.assertEqual(getattr(raised.exception, "retry_after_seconds", None), 120)
        self.assertIn("GitHub", str(raised.exception))

    def test_rate_limit_honors_http_date_and_primary_reset(self):
        now = time.time()
        error = HTTPError("https://api.github.com/", 403, "Forbidden", {
            "Retry-After": formatdate(now + 120, usegmt=True),
            "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(now + 240),
        }, None)
        with patch.object(update_runner.time, "time", return_value=now):
            self.assertEqual(update_runner.release_rate_limit_delay(error), 240)

    def test_plain_forbidden_is_not_mislabeled_as_rate_limit(self):
        error = HTTPError("https://api.github.com/", 403, "Forbidden", {}, None)
        self.assertIsNone(update_runner.release_rate_limit_delay(error))

    def test_pinned_asset_rate_limit_has_same_retry_information(self):
        error = HTTPError(update_runner.release_manifest_url("1.0.5"), 429, "Too Many Requests", {}, None)
        with patch.object(update_runner, "download_bytes", side_effect=error) as download:
            with self.assertRaises(update_runner.ReleaseRateLimitedError) as raised:
                update_runner.check_release(Path("unused.pem"), expected_version="1.0.5")
        self.assertEqual(download.call_count, 1)
        self.assertEqual(raised.exception.retry_after_seconds, 60)


class ReleaseRequestCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.key = Path(self.temporary.name) / "release-public-key.pem"
        self.key.touch()
        self.key_patch = patch.object(agent_server, "SERVER_UPDATE_PUBLIC_KEY", self.key)
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        self.manifest = {"version": "1.0.5", "track": "stable", "archive": {"name": "server.tgz"}}
        for name, value in (("_RELEASE_CHECK_CACHE", {}), ("_RELEASE_CHECK_TASKS", {}), ("_RELEASE_CHECK_RETRY_AT", 0.0)):
            change = patch.object(agent_server, name, value)
            change.start()
            self.addCleanup(change.stop)

    async def test_repeated_checks_reuse_recent_verified_result(self):
        with patch.object(agent_server, "check_release", return_value=self.manifest) as check:
            first = await agent_server.signed_release_manifest()
            first["archive"]["name"] = "modified-by-caller"
            second = await agent_server.signed_release_manifest()
        self.assertEqual(check.call_count, 1)
        self.assertEqual(second["archive"]["name"], "server.tgz")

    async def test_rate_limit_is_shared_across_tracks_and_refresh_until_retry_after(self):
        with patch.object(agent_server, "check_release", side_effect=[
            update_runner.ReleaseRateLimitedError(120), self.manifest,
        ]) as check:
            for track, refresh in (("stable", False), ("beta", False), ("stable", True)):
                with self.assertRaises(HTTPException) as raised:
                    await agent_server.signed_release_manifest(track, refresh=refresh)
                self.assertEqual(raised.exception.status_code, 429)
                self.assertEqual(raised.exception.detail["code"], "server_update_release_rate_limited")
                self.assertIn("No server restart", raised.exception.detail["action"])
                self.assertIn("Retry-After", raised.exception.headers)
            self.assertEqual(check.call_count, 1)
            agent_server._RELEASE_CHECK_RETRY_AT = 0
            self.assertEqual((await agent_server.signed_release_manifest())["version"], "1.0.5")
            self.assertEqual(check.call_count, 2)

    async def test_native_authenticated_route_surfaces_real_http_rate_limit_without_repeating_request(self):
        requests = []
        class RateLimitedHost(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                self.send_response(429)
                self.send_header("Retry-After", "120")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), RateLimitedHost)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(update_runner, "RELEASES_API_URL", f"http://127.0.0.1:{upstream.server_port}/releases"), \
                 patch.object(agent_server, "AGENT_TOKEN", "isolated-release-check"), \
                 patch.object(agent_server, "server_identity", return_value="release-test-server"), \
                 patch.object(agent_server, "SERVER_INSTANCE_ID", "release-test-instance"), \
                 patch.object(agent_server, "SERVER_UPDATE_STATUS_FILE", self.key.parent / "update.json"), \
                 patch.object(agent_server, "SERVER_RESTART_STATUS_FILE", self.key.parent / "restart.json"):
                client = TestClient(agent_server.app)
                try:
                    for track in ("stable", "stable", "beta"):
                        response = client.post("/api/admin/update/check", headers={
                            "X-AgentsDock-Token": "isolated-release-check",
                        }, json={"track": track, "expected_server_identity": "release-test-server",
                                 "expected_server_instance_id": "release-test-instance"})
                        self.assertEqual(response.status_code, 429, response.text)
                        detail = response.json()["detail"]
                        self.assertEqual(detail["code"], "server_update_release_rate_limited")
                        self.assertTrue(detail["retryable"])
                        self.assertIn("No server restart", detail["action"])
                        self.assertGreater(int(response.headers["Retry-After"]), 0)
                        self.assertIn(
                            f"Try again in {detail['retry_after_seconds']} seconds.",
                            detail["message"],
                        )
                finally:
                    client.close()
            self.assertEqual(requests, ["/releases"])
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

    async def test_fresh_check_discovers_withdrawal_and_discards_cached_release(self):
        with patch.object(agent_server, "check_release", side_effect=[
            self.manifest, update_runner.ReleaseUnavailableError("Release withdrawn"),
            {**self.manifest, "version": "1.0.3"},
        ]) as check:
            await agent_server.signed_release_manifest()
            with self.assertRaises(HTTPException) as raised:
                await agent_server.signed_release_manifest(refresh=True)
            self.assertEqual(raised.exception.status_code, 404)
            self.assertEqual((await agent_server.signed_release_manifest())["version"], "1.0.3")
            self.assertEqual(check.call_count, 3)
    async def test_concurrent_checks_share_request_and_canceling_one_does_not_cancel_others(self):
        started = threading.Event()
        finish = threading.Event()
        def fetch(*args):
            started.set()
            if not finish.wait(5):
                raise RuntimeError("test release fetch timed out")
            return self.manifest
        with patch.object(agent_server, "check_release", side_effect=fetch) as check:
            first = asyncio.create_task(agent_server.signed_release_manifest())
            self.assertTrue(await asyncio.to_thread(started.wait, 2))
            second = asyncio.create_task(agent_server.signed_release_manifest())
            await asyncio.sleep(0)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            finish.set()
            self.assertEqual((await second)["version"], "1.0.5")
            self.assertEqual((await agent_server.signed_release_manifest())["version"], "1.0.5")
        self.assertEqual(check.call_count, 1)
