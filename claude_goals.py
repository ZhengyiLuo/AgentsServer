"""Read native Claude goal attachments without interpreting conversation text.

Call refresh from the existing provider-history/activity path. This module owns
no watcher, timer, provider command, or transcript mutation.
"""
from __future__ import annotations

from collections import deque, OrderedDict
from datetime import datetime
import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID


MAX_GOAL_RECORD_BYTES = 128 * 1024
MAX_GOAL_SCAN_BYTES = 4 * 1024 * 1024


def is_claude_synthetic_no_response(event: Any) -> bool:
    if not isinstance(event, dict) or event.get("type") != "assistant" or event.get("isSidechain") is True:
        return False
    message = event.get("message")
    return bool(isinstance(message, dict) and message.get("model") == "<synthetic>"
                and message.get("role") == "assistant"
                and message.get("content") == [{"type": "text", "text": "No response requested."}]
                and isinstance(message.get("usage"), dict)
                and message["usage"].get("output_tokens") == 0)


class ClaudeGoalHistoryNormalizer:
    """Normalize only commands linked to native goal attachments, never quotes."""
    _command = re.compile(r"\A<command-name>/goal</command-name>\s*<command-message>goal</command-message>\s*<command-args>(.*)</command-args>\Z", re.DOTALL)

    def __init__(self):
        self._parents: OrderedDict[str, dict] = OrderedDict()

    def seed(self, path: Path, offset: int) -> None:
        if offset <= 0:
            return
        try:
            with path.open("rb") as stream:
                start = max(0, offset - 65536)
                stream.seek(start)
                region = stream.read(offset - start)
        except OSError:
            return
        if start:
            region = region.partition(b"\n")[2]
        for line in region.splitlines():
            try:
                self.consume(json.loads(line))
            except (ValueError, RecursionError):
                continue

    def consume(self, event: Any) -> Any:
        if not isinstance(event, dict) or event.get("isSidechain") is True:
            return event
        parent = self._parents.get(event.get("parentUuid"))
        same_session = bool(parent and parent.get("sessionId") == event.get("sessionId"))
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        attachment = event.get("attachment")
        if (isinstance(attachment, dict) and attachment.get("type") == "goal_status"
                and attachment.get("sentinel") is True and type(attachment.get("met")) is bool):
            self._remember(event)
        elif (same_session and event.get("type") == "user" and event.get("isMeta") is True
              and isinstance(content, str) and content.startswith("<local-command-caveat>")
              and parent.get("attachment", {}).get("met") is True):
            self._remember({**event, "attachment": parent["attachment"]})
        elif (same_session and event.get("type") == "user" and isinstance(message, dict)
              and message.get("role") == "user" and isinstance(content, str)):
            match = self._command.fullmatch(content)
            native = parent.get("attachment", {})
            if match and ((native.get("met") is False and match[1] == native.get("condition"))
                          or (native.get("met") is True and match[1] == "clear"
                              and parent.get("isMeta") is True
                              and parent.get("promptId") == event.get("promptId"))):
                return {**event, "message": {**message, "content": "/goal " + match[1]}}
        return event

    def _remember(self, event: dict) -> None:
        identity = event.get("uuid")
        if not isinstance(identity, str) or not isinstance(event.get("sessionId"), str):
            return
        native = event.get("attachment", {})
        condition = native.get("condition")
        if not isinstance(condition, str) or len(condition) > 16_384:
            return
        self._parents[identity] = {key: event.get(key) for key in ("sessionId", "promptId", "isMeta")}
        self._parents[identity]["attachment"] = {
            "met": native.get("met"), "condition": condition,
        }
        while len(self._parents) > 32:
            self._parents.popitem(last=False)


def _timestamp_ms(value: Any) -> int | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return int(parsed.timestamp() * 1000) if parsed.tzinfo else None
    except (ValueError, OverflowError, OSError):
        return None


def _counter(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


class ClaudeGoalProjection:
    """One provider session's latest goal and bounded incremental file cursor.

    A native set sentinel establishes the goal. Evaluator and clear records
    update that exact condition; unrelated sessions/sidechains cannot establish
    or finish it. A fork starts empty until its own native set evidence arrives.
    No failure, pause, or success is inferred from a result/assistant message.
    """

    def __init__(self, provider_session_id: str):
        self.reset(provider_session_id)

    def reset(self, provider_session_id: str) -> None:
        self.provider_session_id = str(provider_session_id)
        self.goal: dict[str, Any] | None = None
        self.offset = 0
        self.caught_up = False
        self._identity: tuple[str, int, int] | None = None
        self._tail = b""
        self._anchor = b""
        self._discard_line = False
        self._seen: deque[str] = deque(maxlen=256)
        self._last_timestamp = -1

    def consume(self, event: Any) -> bool:
        """Fold one native JSONL record; return whether visible goal changed."""
        if not isinstance(event, dict) or (
            event.get("type") != "attachment"
            or event.get("sessionId") != self.provider_session_id
            or event.get("isSidechain") is not False
        ):
            return False
        attachment = event.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "goal_status":
            return False
        event_id = event.get("uuid")
        if not isinstance(event_id, str) or len(event_id) != 36:
            return False
        try:
            UUID(event_id)
        except ValueError:
            return False
        timestamp = _timestamp_ms(event.get("timestamp"))
        condition = attachment.get("condition")
        met = attachment.get("met")
        sentinel = attachment.get("sentinel", False)
        if (
            timestamp is None or timestamp < self._last_timestamp
            or event_id in self._seen
            or not isinstance(condition, str) or not condition.strip()
            or len(condition) > 16_384
            or type(met) is not bool or type(sentinel) is not bool
        ):
            return False
        previous = self.goal
        if sentinel and not met:
            goal = {"condition": condition, "status": "active", "set_at": timestamp}
        else:
            if previous is None or previous["condition"] != condition:
                return False
            if not sentinel and previous["status"] != "active":
                return False
            goal = dict(previous)
            goal["status"] = "cleared" if sentinel else "achieved" if met else "active"
            if sentinel:
                for field in ("iterations", "duration_ms", "tokens"):
                    goal.pop(field, None)
            if not sentinel:
                reason = attachment.get("reason")
                if isinstance(reason, str) and reason:
                    goal["last_reason"] = reason[:8192]
                # Resuming the native session can reset its counters. The
                # number of historical evaluator rows is not a native total.
                for native, public in (("iterations", "iterations"), ("durationMs", "duration_ms"), ("tokens", "tokens")):
                    value = _counter(attachment.get(native))
                    if value is not None:
                        goal[public] = value
                    else:
                        goal.pop(public, None)
        self._seen.append(event_id)
        self._last_timestamp = timestamp
        self.goal = goal
        return goal != previous

    def refresh(
        self, path: str | Path, *, provider_session_id: str | None = None,
        max_bytes: int = MAX_GOAL_SCAN_BYTES,
    ) -> bool:
        """Read at most max_bytes; later activity can continue an unfinished scan.

        Partial lines are retained, oversized lines skipped, and replacement,
        truncation or provider changes reset both projection and cursor. A
        temporarily missing/unreadable file keeps the last observed native state.
        The caller should run file I/O off the server's event loop.
        """
        previous = self.goal
        if provider_session_id is not None and provider_session_id != self.provider_session_id:
            self.reset(provider_session_id)
        budget = max(1, min(int(max_bytes), MAX_GOAL_SCAN_BYTES))
        try:
            with Path(path).open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (str(Path(path).absolute()), stat.st_dev, stat.st_ino)
                stream.seek(max(0, self.offset - len(self._anchor)))
                anchor_matches = stream.read(len(self._anchor)) == self._anchor
                if self._identity != identity or stat.st_size < self.offset or not anchor_matches:
                    self.reset(self.provider_session_id)
                    self._identity = identity
                stream.seek(self.offset)
                chunk = stream.read(budget)
                self.offset += len(chunk)
                self._anchor = (self._anchor + chunk)[-64:]
                self.caught_up = self.offset >= stat.st_size
        except OSError:
            self.caught_up = False
            return previous != self.goal
        lines = (self._tail + chunk).split(b"\n")
        self._tail = lines.pop()
        for line in lines:
            if self._discard_line:
                self._discard_line = False
                continue
            if len(line) > MAX_GOAL_RECORD_BYTES or b'"goal_status"' not in line:
                continue
            try:
                self.consume(json.loads(line))
            except (ValueError, RecursionError):
                continue
        if len(self._tail) > MAX_GOAL_RECORD_BYTES:
            self._tail = b""
            self._discard_line = True
        return previous != self.goal
