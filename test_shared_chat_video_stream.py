"""Synthetic descriptor/ASGI checks; no server import, listeners, or live state."""
import asyncio
import gc
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from starlette.requests import ClientDisconnect, Request

from shared_chat_video_stream import (
    SharedVideoResponse, SharedVideoUnavailable, VideoStreamAdmission,
    VIDEO_CHUNK_BYTES, VIDEO_CONTENT_TYPES,
)


class SharedVideoResponseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="shared-video-stream-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "private-owner-video.mp4"
        self.path.write_bytes(b"0123456789")
        self.admission = VideoStreamAdmission(limit=1)

    def response(self, *, data=None, method="GET", ranges=(), **kwargs):
        if data is not None:
            self.path.write_bytes(data)
        fd = os.open(self.path, os.O_RDONLY)
        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                 "http_version": "1.1", "method": method, "path": "/synthetic/video",
                 "headers": [(b"range", item.encode("latin1")) for item in ranges]}
        response = SharedVideoResponse(fd, byte_size=os.fstat(fd).st_size,
            content_type="video/mp4", filename="clip.mp4", request=Request(scope),
            admission=self.admission, **kwargs)
        self.addCleanup(response.close)
        return response, fd, scope

    def assert_closed(self, fd):
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertEqual(self.admission._active, 0)

    async def collect(self, response, scope, *, send=None, receive=None):
        messages = []
        async def sender(message):
            messages.append(message)
            if send is not None:
                await send(message)
        async def pending():
            await asyncio.Future()
        await response(scope, receive or pending, sender)
        return messages

    @staticmethod
    def headers(messages):
        return {key.decode("latin1"): value.decode("latin1") for key, value in messages[0]["headers"]}

    @staticmethod
    def body(messages):
        return b"".join(item.get("body", b"") for item in messages)

    async def test_full_file_and_required_private_media_headers(self):
        response, fd, scope = self.response(extra_headers={
            "Content-Security-Policy": "default-src 'none'; media-src 'self'",
            "Cache-Control": "public", "Content-Length": "999", "Content-Type": "text/html",
            "Transfer-Encoding": "chunked", "ETag": "owner-metadata"})
        self.assertEqual(self.admission._active, 0)
        messages = await self.collect(response, scope)
        self.assertEqual(messages[0]["status"], 200)
        self.assertEqual(self.body(messages), b"0123456789")
        headers = self.headers(messages)
        self.assertEqual(headers["content-length"], "10")
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["accept-ranges"], "bytes")
        self.assertIn("media-src 'self'", headers["content-security-policy"])
        self.assertNotIn("transfer-encoding", headers)
        self.assertNotIn("etag", headers)
        self.assertNotIn(str(self.path), repr(messages))
        self.assert_closed(fd)

    async def test_single_closed_open_suffix_and_clamped_ranges(self):
        cases = [
            ("bytes=2-5", b"2345", "bytes 2-5/10"),
            ("bytes=6-", b"6789", "bytes 6-9/10"),
            ("bytes=-3", b"789", "bytes 7-9/10"),
            ("bytes=-30", b"0123456789", "bytes 0-9/10"),
            ("bytes=7-999", b"789", "bytes 7-9/10"),
            ("bytes=0-0", b"0", "bytes 0-0/10"),
            (" BYTES=01-02 ", b"12", "bytes 1-2/10"),
        ]
        for value, body, content_range in cases:
            with self.subTest(value=value):
                response, fd, scope = self.response(ranges=[value])
                os.lseek(fd, 5, os.SEEK_SET)
                messages = await self.collect(response, scope)
                self.assertEqual(messages[0]["status"], 206)
                self.assertEqual(self.body(messages), body)
                self.assertEqual(self.headers(messages)["content-range"], content_range)
                self.assertEqual(self.headers(messages)["content-length"], str(len(body)))
                self.assert_closed(fd)

    async def test_head_has_get_headers_and_never_reads_file(self):
        for value, status, size in [(None, 200, "10"), ("bytes=-3", 206, "3"), ("bytes=99-", 416, "0")]:
            with self.subTest(value=value):
                authorize = mock.AsyncMock(return_value=True)
                response, fd, scope = self.response(method="HEAD", ranges=[value] if value else [], reauthorize=authorize)
                with mock.patch.object(response, "_read", side_effect=AssertionError("HEAD read")):
                    messages = await self.collect(response, scope)
                self.assertEqual(messages[0]["status"], status)
                self.assertEqual(self.headers(messages)["content-length"], size)
                self.assertEqual(self.body(messages), b"")
                authorize.assert_awaited_once()
                self.assert_closed(fd)

    async def test_invalid_unsatisfiable_and_multiple_ranges(self):
        values = ["", "bytes=", "bytes=-", "bytes=-0", "bytes=10-", "bytes=99-100", "bytes=4-3",
                  "bytes=0-1,3-4", "bytes=1 -2", "items=1-2", "bytes=+1-2", "bytes=1--2",
                  "bytes=1.0-2", "bytes=1-2\r\nInjected: x", "bytes=" + "0" * 260 + "-1"]
        for ranges in [[value] for value in values] + [["bytes=0-1", "bytes=3-4"]]:
            with self.subTest(ranges=ranges):
                response, fd, scope = self.response(ranges=ranges)
                with mock.patch.object(response, "_read", side_effect=AssertionError("invalid range read")):
                    messages = await self.collect(response, scope)
                self.assertEqual(messages[0]["status"], 416)
                self.assertEqual(self.headers(messages)["content-range"], "bytes */10")
                self.assertEqual(self.headers(messages)["content-length"], "0")
                self.assertEqual(self.body(messages), b"")
                self.assert_closed(fd)

    async def test_empty_file_and_empty_file_ranges(self):
        for ranges in [[], ["bytes=0-0"], ["bytes=0-"], ["bytes=-1"]]:
            with self.subTest(ranges=ranges):
                response, fd, scope = self.response(data=b"", ranges=ranges)
                messages = await self.collect(response, scope)
                self.assertEqual(messages[0]["status"], 416 if ranges else 200)
                self.assertEqual(self.headers(messages)["content-length"], "0")
                self.assertEqual(self.body(messages), b"")
                if ranges:
                    self.assertEqual(self.headers(messages)["content-range"], "bytes */0")
                self.assert_closed(fd)

    async def test_chunks_are_bounded_and_exactly_cover_requested_bytes(self):
        data = b"v" * (3 * VIDEO_CHUNK_BYTES + 19)
        response, fd, scope = self.response(data=data, ranges=["bytes=7-"])
        with mock.patch.object(response, "_read", wraps=response._read) as reader:
            messages = await self.collect(response, scope)
        self.assertEqual(self.body(messages), data[7:])
        self.assertEqual(reader.call_count, 4)
        self.assertTrue(all(call.args[0] <= VIDEO_CHUNK_BYTES for call in reader.call_args_list))
        self.assertEqual([call.args[1] for call in reader.call_args_list],
                         [7 + index * VIDEO_CHUNK_BYTES for index in range(4)])
        self.assertTrue(all(len(message.get("body", b"")) <= VIDEO_CHUNK_BYTES for message in messages))
        self.assert_closed(fd)

    async def test_replaced_path_keeps_original_descriptor_pinned(self):
        response, fd, scope = self.response()
        self.path.rename(self.path.with_suffix(".old"))
        self.path.write_bytes(b"SECRET NEW PATH CONTENT")
        messages = await self.collect(response, scope)
        # Renaming may update the pinned inode's ctime. Either reject that
        # changed revision or serve its original bytes, never the new path.
        self.assertIn(messages[0]["status"], {200, 404})
        self.assertIn(self.body(messages), {b"", b"0123456789"})
        self.assert_closed(fd)

    async def test_in_place_mutation_before_headers_is_empty_404(self):
        for method in ["GET", "HEAD"]:
            response, fd, scope = self.response(data=b"original!!", method=method)
            self.path.write_bytes(b"new secret")
            messages = await self.collect(response, scope)
            self.assertEqual(messages[0]["status"], 404)
            self.assertEqual(self.body(messages), b"")
            self.assertNotIn("content-disposition", self.headers(messages))
            self.assert_closed(fd)

    async def test_in_place_mutation_between_chunks_never_sends_new_bytes(self):
        response, fd, scope = self.response(data=b"x" * (2 * VIDEO_CHUNK_BYTES))
        original = self.path.stat()
        bodies = []
        async def send(message):
            if message.get("body"):
                bodies.append(message["body"])
                self.path.write_bytes(b"s" * (2 * VIDEO_CHUNK_BYTES))
                # Restoring mtime cannot defeat the ctime revision check.
                os.utime(self.path, ns=(original.st_atime_ns, original.st_mtime_ns))
        with self.assertRaises(SharedVideoUnavailable):
            await self.collect(response, scope, send=send)
        self.assertEqual(bodies, [b"x" * VIDEO_CHUNK_BYTES])
        self.assert_closed(fd)

    async def test_mutation_during_worker_read_discards_its_chunk(self):
        response, fd, scope = self.response()
        original_pread = os.pread
        def read_then_mutate(descriptor, size, offset):
            chunk = original_pread(descriptor, size, offset)
            self.path.write_bytes(b"new secret")
            return chunk
        messages = []
        async def send(message):
            messages.append(message)
        with mock.patch("shared_chat_video_stream.os.pread", side_effect=read_then_mutate):
            with self.assertRaises(SharedVideoUnavailable):
                await self.collect(response, scope, send=send)
        self.assertEqual([message["type"] for message in messages], ["http.response.start"])
        self.assert_closed(fd)

    def test_resolver_revision_rejects_mutation_before_constructor(self):
        fd = os.open(self.path, os.O_RDONLY)
        info = os.fstat(fd)
        revision = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                    info.st_ctime_ns, info.st_uid, info.st_mode, info.st_nlink)
        self.path.write_bytes(b"new secret")
        with self.assertRaises(ValueError):
            SharedVideoResponse(fd, byte_size=10, content_type="video/mp4", filename="clip.mp4",
                request=Request({"type": "http", "method": "GET", "headers": []}), file_revision=revision)
        self.assert_closed(fd)

    async def test_denied_or_failed_authorization_before_headers_is_empty_404(self):
        for callback in [mock.AsyncMock(return_value=False), mock.AsyncMock(side_effect=RuntimeError(str(self.path)))]:
            with self.subTest(callback=callback):
                response, fd, scope = self.response(ranges=["bytes=99-"], reauthorize=callback)
                messages = await self.collect(response, scope)
                self.assertEqual(messages[0]["status"], 404)
                self.assertEqual(self.body(messages), b"")
                self.assertNotIn("content-range", self.headers(messages))
                self.assertNotIn("content-disposition", self.headers(messages))
                self.assertNotIn(str(self.path), repr(messages))
                self.assert_closed(fd)

    async def test_revocation_rechecks_on_consumption_once_per_second(self):
        clock = [0.0]
        callback = mock.AsyncMock(side_effect=lambda: clock[0] < 1.0)
        response, fd, scope = self.response(data=b"x" * (4 * VIDEO_CHUNK_BYTES), reauthorize=callback)
        bodies = []
        async def send(message):
            if message.get("body"):
                bodies.append(message["body"])
                clock[0] = [0.2, 0.9, 1.1][len(bodies) - 1]
        with mock.patch("shared_chat_video_stream.time", SimpleNamespace(monotonic=lambda: clock[0])):
            with self.assertRaises(SharedVideoUnavailable):
                await self.collect(response, scope, send=send)
        self.assertEqual(len(bodies), 3)
        self.assertEqual(callback.await_count, 2)
        self.assert_closed(fd)

    async def test_slow_read_rechecks_before_sending_its_chunk(self):
        clock = [0.0]
        callback = mock.AsyncMock(side_effect=lambda: clock[0] < 1.0)
        response, fd, scope = self.response(reauthorize=callback)
        original_read = response._read
        def read(size, offset):
            clock[0] = 1.1
            return original_read(size, offset)
        messages = []
        async def send(message):
            messages.append(message)
        with mock.patch("shared_chat_video_stream.time", SimpleNamespace(monotonic=lambda: clock[0])), \
                mock.patch.object(response, "_read", side_effect=read):
            with self.assertRaises(SharedVideoUnavailable):
                await self.collect(response, scope, send=send)
        self.assertEqual([item["type"] for item in messages], ["http.response.start"])
        self.assertEqual(callback.await_count, 2)
        self.assert_closed(fd)

    async def test_header_and_body_send_errors_release_descriptor_and_lease(self):
        for target in ["http.response.start", "http.response.body"]:
            with self.subTest(target=target):
                response, fd, scope = self.response()
                async def send(message):
                    self.assertEqual(self.admission._active, 1)
                    if message["type"] == target:
                        raise OSError("disconnected")
                with self.assertRaises(ClientDisconnect):
                    await self.collect(response, scope, send=send)
                self.assert_closed(fd)

    async def test_cancellation_before_headers_and_during_headers_or_body(self):
        for target in ["authorization", "http.response.start", "http.response.body"]:
            with self.subTest(target=target):
                entered = asyncio.Event()
                async def block():
                    entered.set()
                    await asyncio.Future()
                async def send(message):
                    if message["type"] == target:
                        await block()
                response, fd, scope = self.response(reauthorize=block if target == "authorization" else None)
                task = asyncio.create_task(self.collect(response, scope, send=send))
                await asyncio.wait_for(entered.wait(), 2)
                self.assertEqual(self.admission._active, 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assert_closed(fd)

    async def test_legacy_asgi_disconnect_during_headers_closes_fd(self):
        response, fd, scope = self.response()
        scope["asgi"]["spec_version"] = "2.3"
        entered = asyncio.Event()
        async def send(message):
            entered.set()
            await asyncio.Future()
        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}
        await asyncio.wait_for(self.collect(response, scope, send=send, receive=receive), 2)
        self.assert_closed(fd)

    async def test_cancelled_read_cannot_use_reassigned_descriptor(self):
        response, fd, scope = self.response()
        entered, released, finished = threading.Event(), threading.Event(), threading.Event()
        read_fd = []
        original_pread = os.pread
        def slow_pread(descriptor, size, offset):
            read_fd.append(descriptor)
            entered.set()
            try:
                if not released.wait(2):
                    raise AssertionError("worker was not released")
                return original_pread(descriptor, size, offset)
            finally:
                finished.set()
        with mock.patch("shared_chat_video_stream.os.pread", side_effect=slow_pread):
            task = asyncio.create_task(self.collect(response, scope))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assert_closed(fd)
                self.assertNotEqual(read_fd, [fd])
            finally:
                released.set()
                self.assertTrue(await asyncio.to_thread(finished.wait, 2))
        # A delayed worker that has not duped yet observes a closed response.
        with self.assertRaises(SharedVideoUnavailable):
            response._read(1, 0)

    async def test_admission_exhaustion_closes_without_reading_or_reauth(self):
        self.assertTrue(self.admission.acquire())
        authorize = mock.AsyncMock(return_value=True)
        response, fd, scope = self.response(reauthorize=authorize)
        try:
            messages = await self.collect(response, scope)
            self.assertEqual(messages[0]["status"], 503)
            self.assertEqual(self.body(messages), b"")
            self.assertEqual(self.admission._active, 1)
            with self.assertRaises(OSError):
                os.fstat(fd)
            authorize.assert_not_awaited()
        finally:
            self.admission.release()
        self.assert_closed(fd)

    async def test_read_failure_and_unexpected_eof_abort_without_private_details(self):
        for effect in [OSError(str(self.path)), b""]:
            with self.subTest(effect=effect):
                response, fd, scope = self.response()
                replacement = mock.Mock(side_effect=effect) if isinstance(effect, Exception) else mock.Mock(return_value=effect)
                with mock.patch.object(response, "_read", replacement):
                    with self.assertRaises(SharedVideoUnavailable) as caught:
                        await self.collect(response, scope)
                self.assertEqual(str(caught.exception), "Shared video unavailable")
                self.assert_closed(fd)

    async def test_explicit_close_is_idempotent_for_unused_responses(self):
        response, fd, scope = self.response()
        response.close()
        response.close()
        self.assert_closed(fd)
        with self.assertRaises(SharedVideoUnavailable):
            await self.collect(response, scope)
        self.assert_closed(fd)

    def test_constructor_failure_closes_owned_fd(self):
        request = Request({"type": "http", "method": "GET", "headers": []})
        for changes in [{"content_type": "text/html"}, {"content_type": "video/svg+xml"},
                        {"content_type": "video/mp4; codecs=avc1"}, {"byte_size": 99},
                        {"filename": str(self.path)}, {"filename": "..\\secret.mp4"},
                        {"filename": "clip.mp4\r\nInjected: yes"}, {"filename": ""}]:
            with self.subTest(changes=changes):
                fd = os.open(self.path, os.O_RDONLY)
                options = dict(byte_size=10, content_type="video/mp4", filename="clip.mp4", request=request)
                options.update(changes)
                with self.assertRaises(ValueError):
                    SharedVideoResponse(fd, **options)
                self.assert_closed(fd)

    def test_nonregular_descriptor_is_rejected_and_closed(self):
        fd = os.open(self.path.parent, os.O_RDONLY)
        with self.assertRaises(ValueError):
            SharedVideoResponse(fd, byte_size=os.fstat(fd).st_size, content_type="video/mp4",
                filename="clip.mp4", request=Request({"type": "http", "method": "GET", "headers": []}))
        self.assert_closed(fd)

    def test_unused_response_finalizer_closes_descriptor(self):
        fd = os.open(self.path, os.O_RDONLY)
        response = SharedVideoResponse(fd, byte_size=10, content_type="video/mp4", filename="clip.mp4",
            request=Request({"type": "http", "method": "GET", "headers": []}), admission=self.admission)
        del response
        gc.collect()
        self.assert_closed(fd)

    async def test_allowlisted_mime_and_unicode_filename_are_header_safe(self):
        for content_type in VIDEO_CONTENT_TYPES:
            fd = os.open(self.path, os.O_RDONLY)
            scope = {"type": "http", "method": "HEAD", "headers": [], "asgi": {"spec_version": "2.4"}}
            response = SharedVideoResponse(fd, byte_size=10, content_type=content_type,
                filename='A "movie" \u732b.mp4', request=Request(scope), admission=self.admission)
            messages = await self.collect(response, scope)
            headers = self.headers(messages)
            self.assertEqual(headers["content-type"], content_type)
            self.assertEqual(headers["content-disposition"], "inline; filename*=UTF-8''A%20%22movie%22%20%E7%8C%AB.mp4")
            self.assert_closed(fd)

    async def test_non_get_head_method_is_empty_405(self):
        response, fd, scope = self.response(method="POST")
        messages = await self.collect(response, scope)
        self.assertEqual(messages[0]["status"], 405)
        self.assertEqual(self.headers(messages)["allow"], "GET, HEAD")
        self.assertEqual(self.body(messages), b"")
        self.assert_closed(fd)


if __name__ == "__main__":
    unittest.main()
