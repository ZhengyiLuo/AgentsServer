"""Bounded HTTP video streaming from an already authorized, pinned descriptor.

This module does not resolve paths, read a share ledger, or open server state.
The caller securely opens and validates a regular file and transfers exclusive
ownership of its descriptor to SharedVideoResponse, including on init failure.
"""
from __future__ import annotations

import os
import re
import stat
import threading
import time
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.responses import Response, StreamingResponse


VIDEO_CONTENT_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/ogg"})
IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif", "image/bmp"})
VIDEO_CHUNK_BYTES = 64 * 1024
REAUTHORIZE_INTERVAL_SECONDS = 1.0
_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)", re.IGNORECASE)
_SECURITY_HEADERS = {
    "cache-control": "no-store",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-robots-tag": "noindex, nofollow, noarchive",
    "cross-origin-resource-policy": "same-origin",
}


class SharedVideoUnavailable(RuntimeError):
    """Abort an already started response without exposing internal details."""


class VideoStreamAdmission:
    """A bounded, nonwaiting lease shared by both video route families."""

    def __init__(self, limit=8):
        if type(limit) is not int or limit < 1:
            raise ValueError("Invalid video stream limit")
        self.limit = limit
        self._active = 0
        self._lock = threading.Lock()

    def acquire(self):
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self):
        with self._lock:
            self._active -= 1


_ADMISSION = VideoStreamAdmission()


def _byte_range(request, byte_size):
    """Return (status, start, length), including strict single-range rejection."""
    values = request.headers.getlist("range")
    if not values:
        return 200, 0, byte_size
    if len(values) != 1 or len(values[0]) > 256:
        return 416, 0, 0
    match = _RANGE.fullmatch(values[0].strip())
    if match is None or not any(match.groups()) or byte_size == 0:
        return 416, 0, 0
    first, last = match.groups()
    if first:
        start = int(first)
        end = min(int(last), byte_size - 1) if last else byte_size - 1
        if start >= byte_size or end < start:
            return 416, 0, 0
    else:
        suffix = int(last)
        if suffix == 0:
            return 416, 0, 0
        start, end = max(0, byte_size - suffix), byte_size - 1
    return 206, start, end - start + 1


def _file_revision(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_uid, info.st_mode, info.st_nlink)


class SharedVideoResponse(StreamingResponse):
    """GET/HEAD response owning a descriptor for its complete ASGI lifecycle.

    ``request`` is a Starlette Request. ``reauthorize`` is an optional async
    zero-argument callback: None/True grants access, False or an exception denies
    it. It runs immediately before headers and, on demand while consuming the
    response, at most once per second. Denial before headers produces an empty
    404; denial after headers aborts the stream with SharedVideoUnavailable.

    Admission is acquired only when ASGI invokes the response and released on
    every exit, including header disconnects. ``close()`` explicitly disposes a
    response that will never be invoked; finalization is an additional fallback.
    Neither the caller nor any other object may close/reuse the transferred fd.
    ``file_revision`` may pin the resolver's fstat result as the tuple
    (dev, ino, size, mtime_ns, ctime_ns, uid, mode, nlink). The revision is checked
    at construction, before headers, and before/after every bounded read.
    """

    content_types = VIDEO_CONTENT_TYPES
    disposition = "inline"

    def __init__(self, fd, *, byte_size, content_type, filename, request,
                 reauthorize=None, extra_headers=None, admission=None, file_revision=None):
        self._fd = fd
        self._fd_lock = threading.Lock()
        self._called = False
        try:
            if (type(fd) is not int or fd < 0 or type(byte_size) is not int
                    or byte_size < 0 or not isinstance(content_type, str)
                    or re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", content_type) is None
                    or (self.content_types is not None and content_type not in self.content_types)):
                raise ValueError("Invalid shared video")
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != byte_size:
                raise ValueError("Invalid shared video")
            self._file_revision = _file_revision(info)
            if file_revision is not None and file_revision != self._file_revision:
                raise ValueError("Invalid shared video")
            # The caller supplies a display filename, never a path. Fail closed
            # on path/control characters even if that contract was violated.
            if (not isinstance(filename, str) or not filename or len(filename) > 255
                    or filename in {".", ".."}
                    or re.search(r"[\x00-\x1f\x7f/\\]", filename)):
                raise ValueError("Invalid shared video")
            self._method = request.method.upper()
            self._reauthorize = reauthorize
            self._last_authorized = None
            self._admission = admission if admission is not None else _ADMISSION
            self._base_headers = {str(key).lower(): value for key, value in (extra_headers or {}).items()}
            # Route CSP is retained; callers cannot replace the mandatory
            # privacy headers or representation/framing headers below.
            for key in ("content-type", "content-length", "content-range", "content-disposition",
                        "accept-ranges", "transfer-encoding", "etag", "last-modified"):
                self._base_headers.pop(key, None)
            self._base_headers.update(_SECURITY_HEADERS)
            headers = dict(self._base_headers)
            status, self._start, self._length = _byte_range(request, byte_size)
            if self._method not in {"GET", "HEAD"}:
                status, self._length = 405, 0
                headers["allow"] = "GET, HEAD"
            else:
                headers["accept-ranges"] = "bytes"
                if status == 416:
                    headers["content-range"] = f"bytes */{byte_size}"
                else:
                    headers["content-type"] = content_type
                    headers["content-disposition"] = self.disposition + "; filename*=UTF-8''" + quote(filename, safe="")
                    if status == 206:
                        headers["content-range"] = f"bytes {self._start}-{self._start + self._length - 1}/{byte_size}"
            headers["content-length"] = str(self._length)
            # stream_response owns the body; no generator owns the descriptor.
            super().__init__(iter(()), status_code=status, headers=headers)
        except BaseException:
            self.close()
            raise

    def close(self):
        """Idempotently close the owned descriptor, including unused responses."""
        with self._fd_lock:
            fd, self._fd = self._fd, None
        if type(fd) is int and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def _read(self, size, offset):
        # Cancellation may end an await while its worker still runs. Dup under
        # the close lock so a delayed worker can never read a reused descriptor.
        # The worker owns that duplicate until its one bounded read finishes.
        with self._fd_lock:
            if self._fd is None:
                raise SharedVideoUnavailable("Shared video unavailable")
            fd = os.dup(self._fd)
        try:
            if _file_revision(os.fstat(fd)) != self._file_revision:
                raise SharedVideoUnavailable("Shared video unavailable")
            chunk = os.pread(fd, size, offset)
            if _file_revision(os.fstat(fd)) != self._file_revision:
                raise SharedVideoUnavailable("Shared video unavailable")
            return chunk
        finally:
            os.close(fd)

    def _unchanged(self):
        with self._fd_lock:
            try:
                return self._fd is not None and _file_revision(os.fstat(self._fd)) == self._file_revision
            except OSError:
                return False

    async def _authorized(self, *, force=False):
        if self._reauthorize is None:
            return True
        if (not force and self._last_authorized is not None
                and time.monotonic() - self._last_authorized < REAUTHORIZE_INTERVAL_SECONDS):
            return True
        try:
            granted = await self._reauthorize()
        except Exception:
            return False
        self._last_authorized = time.monotonic()
        return granted is None or granted is True

    async def stream_response(self, send):
        if not await self._authorized(force=True):
            await self._empty_response(send, 404)
            return
        if not self._unchanged():
            await self._empty_response(send, 404)
            return
        await send({"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers})
        if self._method != "HEAD" and self.status_code in {200, 206}:
            offset, remaining = self._start, self._length
            while remaining:
                if not await self._authorized():
                    raise SharedVideoUnavailable("Shared video unavailable")
                try:
                    chunk = await run_in_threadpool(self._read, min(VIDEO_CHUNK_BYTES, remaining), offset)
                except OSError:
                    raise SharedVideoUnavailable("Shared video unavailable") from None
                if not chunk:
                    raise SharedVideoUnavailable("Shared video unavailable")
                # A slow disk read can cross the authorization interval.
                if not await self._authorized():
                    raise SharedVideoUnavailable("Shared video unavailable")
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                offset += len(chunk)
                remaining -= len(chunk)
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _empty_response(self, send, status):
        response = Response(status_code=status, headers=self._base_headers)
        await send({"type": "http.response.start", "status": status, "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def __call__(self, scope, receive, send):
        admitted = False
        try:
            if self._called or self._fd is None:
                raise SharedVideoUnavailable("Shared video unavailable")
            self._called = True
            admitted = self._admission.acquire()
            if not admitted:
                await self._empty_response(send, 503)
                return
            await super().__call__(scope, receive, send)
        finally:
            self.close()
            if admitted:
                self._admission.release()


class SharedFileResponse(SharedVideoResponse):
    """Download any registered attachment using the same descriptor lifecycle."""

    content_types = None
    disposition = "attachment"


class SharedImageResponse(SharedVideoResponse):
    """Only raster images are embedded at the shared chat's origin."""

    content_types = IMAGE_CONTENT_TYPES
