"""Read-only, signed references to attachments already published in one chat.

No workspace paths, file discovery, registry writes, or file copies. Capabilities
pin the registered copy's metadata and inode revision; changed files fail closed.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import stat

VIDEO_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime", "video/ogg"})
VIDEO_ID = re.compile(r"video_[A-Za-z0-9_-]+\.[a-f0-9]{64}\Z")
SHARED_FILE_ID = re.compile(r"shared_file_[A-Za-z0-9_-]+\.[a-f0-9]{64}\Z")
CONTENT_TYPE = re.compile(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+\Z")
FILE_ID = re.compile(r"(?:file|art)_[a-f0-9]{16,32}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
EXTENSIONS = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
              ".mov": "video/quicktime", ".ogv": "video/ogg", ".ogg": "video/ogg"}


class SharedVideoUnavailable(ValueError):
    def __init__(self):
        super().__init__("Shared video is unavailable")


def _normalize_shared_chat_files(value, *, videos):
    if not isinstance(value, list):
        raise ValueError("Invalid shared videos")
    output, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "filename", "content_type", "size"}:
            raise ValueError("Invalid shared video")
        identity, name = item["id"], item["filename"]
        pattern = VIDEO_ID if videos else SHARED_FILE_ID
        if (not isinstance(identity, str) or len(identity) > 1024 or pattern.fullmatch(identity) is None
                or identity in seen or not isinstance(name, str) or not 1 <= len(name) <= 255
                or name in {".", ".."} or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)
                or len(name.encode("utf-8")) > 1024 or not isinstance(item["content_type"], str)
                or (item["content_type"] not in VIDEO_TYPES if videos else CONTENT_TYPE.fullmatch(item["content_type"]) is None)
                or type(item["size"]) is not int or not (1 if videos else 0) <= item["size"] <= (1 << 53) - 1):
            raise ValueError("Invalid shared video")
        output.append(dict(item)); seen.add(identity)
    return output


def normalize_shared_chat_videos(value):
    return _normalize_shared_chat_files(value, videos=True)


def normalize_shared_chat_files(value):
    return _normalize_shared_chat_files(value, videos=False)


def _key(secret, *, videos=True):
    if not isinstance(secret, str) or not secret:
        raise SharedVideoUnavailable()
    return hmac.new(secret.encode("utf-8"), b"agentsdock-chat-video-v1" if videos else b"agentsdock-chat-file-v1", hashlib.sha256).digest()


def _regular(info, owner):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != owner:
        raise SharedVideoUnavailable()


def _open_registered(root, session_id, file_id, *, legacy_owner=False, videos=True):
    """Open through pinned directories; return an owned FD, never reopen a path."""
    if (not isinstance(session_id, str) or SESSION_ID.fullmatch(session_id) is None
            or not isinstance(file_id, str) or FILE_ID.fullmatch(file_id) is None):
        raise SharedVideoUnavailable()
    root = Path(root)
    if not root.is_absolute() or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise SharedVideoUnavailable()
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = directory_fd = meta_fd = video_fd = None
    try:
        root_fd = os.open(root, directory_flags)
        owner = os.fstat(root_fd).st_uid
        directory_fd = os.open(file_id, directory_flags, dir_fd=root_fd)
        if os.fstat(directory_fd).st_uid != owner:
            raise SharedVideoUnavailable()
        meta_fd = os.open("meta.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        before = os.fstat(meta_fd)
        _regular(before, owner)
        if not 0 < before.st_size <= 64 * 1024:
            raise SharedVideoUnavailable()
        raw = os.read(meta_fd, 64 * 1024 + 1)
        after = os.fstat(meta_fd)
        if len(raw) != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns):
            raise SharedVideoUnavailable()
        meta = json.loads(raw)
        if not isinstance(meta, dict) or meta.get("id") != file_id:
            raise SharedVideoUnavailable()
        if meta.get("session_id") != session_id and not (legacy_owner and meta.get("session_id") in (None, "")):
            raise SharedVideoUnavailable()
        name = meta.get("filename")
        if (not isinstance(name, str) or not 1 <= len(name) <= 255 or name in {".", "..", "meta.json"}
                or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)
                or len(name.encode("utf-8")) > 1024 or meta.get("path") != str(root / file_id / name)):
            raise SharedVideoUnavailable()
        guessed = EXTENSIONS.get(Path(name).suffix.lower()) if videos else mimetypes.guess_type(name)[0]
        mime = str(meta.get("content_type") or "").split(";", 1)[0].strip().lower()
        if mime in {"", "application/octet-stream", "binary/octet-stream"}:
            mime = guessed or "application/octet-stream"
        if (videos and (mime not in VIDEO_TYPES or mime != guessed)) or CONTENT_TYPE.fullmatch(mime) is None:
            raise SharedVideoUnavailable()
        video_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        info = os.fstat(video_fd)
        _regular(info, owner)
        if type(meta.get("size")) is not int or meta["size"] != info.st_size or not (1 if videos else 0) <= info.st_size <= (1 << 53) - 1:
            raise SharedVideoUnavailable()
        revision = hashlib.sha256(json.dumps([name, mime, meta.get("session_id"), info.st_dev, info.st_ino,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_uid, info.st_mode],
            ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
        result = {"file_fd": video_fd, "filename": name, "content_type": mime, "size": info.st_size, "revision": revision,
                  "file_revision": (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                                    info.st_uid, info.st_mode, info.st_nlink)}
        video_fd = None
        return result
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise SharedVideoUnavailable() from None
    finally:
        for fd in (video_fd, meta_fd, directory_fd, root_fd):
            if fd is not None:
                os.close(fd)


def _shared_chat_descriptor(root, secret, session_id, file_id, *, legacy_owner=False, videos=True):
    key = _key(secret, videos=videos)
    opened = _open_registered(root, session_id, file_id, legacy_owner=legacy_owner, videos=videos)
    try:
        payload = json.dumps([1, session_id, file_id, opened["revision"]], separators=(",", ":")).encode("ascii")
        encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
        identity = ("video_" if videos else "shared_file_") + encoded + "." + hmac.new(key, encoded.encode("ascii"), hashlib.sha256).hexdigest()
        return _normalize_shared_chat_files([{key: opened[key] for key in ("filename", "content_type", "size")} | {"id": identity}], videos=videos)[0]
    finally:
        os.close(opened["file_fd"])


def shared_chat_video_descriptor(root, secret, session_id, file_id, *, legacy_owner=False):
    return _shared_chat_descriptor(root, secret, session_id, file_id, legacy_owner=legacy_owner)


def shared_chat_file_descriptor(root, secret, session_id, file_id, *, legacy_owner=False):
    return _shared_chat_descriptor(root, secret, session_id, file_id, legacy_owner=legacy_owner, videos=False)


def _open_shared_chat_file(root, secret, session_id, identity, *, videos):
    opened = None
    try:
        pattern = VIDEO_ID if videos else SHARED_FILE_ID
        if not isinstance(identity, str) or len(identity) > 1024 or pattern.fullmatch(identity) is None:
            raise SharedVideoUnavailable()
        encoded, signature = identity[len("video_" if videos else "shared_file_"):].split(".")
        expected = hmac.new(_key(secret, videos=videos), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise SharedVideoUnavailable()
        payload = json.loads(base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True))
        if (not isinstance(payload, list) or len(payload) != 4 or type(payload[0]) is not int or payload[0] != 1
                or payload[1] != session_id or not isinstance(payload[3], str)):
            raise SharedVideoUnavailable()
        opened = _open_registered(root, session_id, payload[2], legacy_owner=True, videos=videos)
        if not hmac.compare_digest(opened.pop("revision"), payload[3]):
            raise SharedVideoUnavailable()
        result, opened = opened, None
        return result
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise SharedVideoUnavailable() from None
    finally:
        if opened is not None:
            os.close(opened["file_fd"])


def open_shared_chat_video(root, secret, session_id, identity):
    return _open_shared_chat_file(root, secret, session_id, identity, videos=True)


def open_shared_chat_file(root, secret, session_id, identity):
    return _open_shared_chat_file(root, secret, session_id, identity, videos=False)
