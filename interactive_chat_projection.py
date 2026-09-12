"""Incremental, bounded text-only chat projection; no server imports or timers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import threading
from typing import Callable

from public_chat_transcript import (
    MAX_LINE_BYTES, MAX_LOG_BYTES, MAX_MESSAGE_BYTES, MAX_MESSAGES,
    MAX_RECORDS, MAX_TEXT_BYTES, PublicTranscriptError,
    _is_goal_followup, _public_timestamp,
)


class IncrementalChatTranscript:
    """Use one fresh privacy projector for the lifetime of an append-only log.

    The owner must discard this reader after a known history rewrite or privacy
    projection change. Stat checks detect replacement, truncation and same-size
    changes; no incremental reader can prove an earlier prefix was not changed
    in place while also growing without rereading that prefix.
    """

    def __init__(self, path: Path, projector: Callable[[dict], dict | None]):
        self.path = Path(path)
        self.projector = projector
        self._lock = threading.Lock()
        self._stamp = None
        self._offset = 0
        self._revision = 0
        self._records = 0
        self._total_text = 0
        self._messages: list[dict] = []
        self._outputs: dict[str, list[str]] = {}
        self._failed = False

    def _snapshot(self) -> dict:
        return {"messages": [dict(message) for message in self._messages],
                "through_bytes": self._offset, "revision": str(self._revision)}

    @staticmethod
    def _file_stamp(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def load(self) -> dict:
        """Read only new complete records; a detected error requires a new reader."""
        with self._lock:
            if self._failed:
                raise PublicTranscriptError("Chat history changed; reopen the conversation")
            try:
                return self._load()
            except Exception as exc:
                # The supplied projector has private state and cannot be rolled
                # back after a partially consumed bad page. Never reuse it.
                self._failed = True
                if isinstance(exc, PublicTranscriptError):
                    raise
                raise PublicTranscriptError("Chat history is unavailable") from exc

    def _load(self) -> dict:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            if self._stamp is None:
                return self._snapshot()
            raise PublicTranscriptError("Chat history changed; reopen the conversation") from None
        with os.fdopen(fd, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise PublicTranscriptError("Chat history is unavailable")
            stamp = self._file_stamp(initial)
            if initial.st_size > MAX_LOG_BYTES:
                raise PublicTranscriptError("Chat exceeds the 64 MiB sharing limit")
            if self._stamp is not None:
                if (stamp[:2] != self._stamp[:2] or stamp[2] < self._stamp[2]
                        or stamp[2] == self._stamp[2] and stamp[3:] != self._stamp[3:]):
                    raise PublicTranscriptError("Chat history changed; reopen the conversation")
                if stamp == self._stamp:
                    return self._snapshot()
            stream.seek(self._offset)
            while self._offset < initial.st_size:
                line = stream.readline(min(MAX_LINE_BYTES + 1, initial.st_size - self._offset))
                if len(line) > MAX_LINE_BYTES:
                    raise PublicTranscriptError("A chat record exceeds the sharing limit")
                if not line.endswith(b"\n"):
                    break  # Re-read this uncommitted tail only after it grows.
                self._offset += len(line)
                self._records += 1
                if self._records > MAX_RECORDS:
                    raise PublicTranscriptError("Chat exceeds the sharing record limit")
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise PublicTranscriptError("Chat history contains an unreadable record") from exc
                if not isinstance(raw, dict) or not isinstance(raw.get("type"), str):
                    raise PublicTranscriptError("Chat history contains an invalid event")
                self._consume(raw)
            final = os.fstat(stream.fileno())
            current = self.path.stat(follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != stamp[:2]
                    or final.st_size < initial.st_size
                    or final.st_size == initial.st_size and self._file_stamp(final)[3:] != stamp[3:]):
                raise PublicTranscriptError("Chat history changed; reopen the conversation")
            # Save the boundary actually read, not a later concurrent append.
            self._stamp = stamp
        return self._snapshot()

    def _consume(self, raw: dict) -> None:
        kind = raw["type"]
        if kind not in {"turn_started", "assistant_text", "turn_finished", "reasoning_summary"} and not _is_goal_followup(raw):
            return
        if kind == "reasoning_summary" and raw.get("phase") != "commentary":
            return
        event = self.projector(raw)
        if not event:
            return
        if not isinstance(event, dict):
            raise PublicTranscriptError("Chat history projection is invalid")
        run = str(event.get("run_id") or "")
        if kind == "turn_started" or _is_goal_followup(event):
            self._outputs[run] = []
            role, text = "user", event.get("prompt")
        else:
            role = "assistant"
            text = event.get("result_text") if kind == "turn_finished" else event.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        try:
            size = len(text.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise PublicTranscriptError("Chat history contains invalid Unicode text") from exc
        if size > MAX_MESSAGE_BYTES:
            raise PublicTranscriptError("A message exceeds the 256 KiB sharing limit")
        normalized = " ".join(text.split())
        previous = self._outputs.setdefault(run, [])
        if kind == "turn_finished" and (normalized in previous or normalized == " ".join(previous)):
            return
        if kind == "assistant_text":
            previous.append(normalized)
        self._total_text += size
        if self._total_text > MAX_TEXT_BYTES or len(self._messages) >= MAX_MESSAGES:
            raise PublicTranscriptError("Chat exceeds the 1,000-message / 2 MiB sharing limit")
        message = {"role": role, "text": text}
        timestamp = _public_timestamp(event.get("ts"))
        if timestamp is not None:
            message["timestamp"] = timestamp
        self._messages.append(message)
        self._revision = self._offset
