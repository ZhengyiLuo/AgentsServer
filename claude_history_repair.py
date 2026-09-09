"""Bounded, source-proven projection repair for old Claude metadata imports.

No transcript discovery, polling, durable mutation, or server imports. A caller
explicitly prepares one session; subsequent per-event checks use memory only.
Uncheckpointed imports and ambiguous user quotations are intentionally retained.
"""
from __future__ import annotations

from collections import OrderedDict
from bisect import bisect_right
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Callable


MAX_BYTES = 96 * 1024 * 1024
MAX_EVENTS_BYTES = 32 * 1024 * 1024
MAX_LINE_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 100_000
MAX_KEYS = 20_000
MAX_TARGETS = 4_000
MAX_SESSIONS = 24
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class _Unproven(ValueError):
    pass


def _stamp(path: Path) -> tuple[int, int, int, int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or value.st_size > MAX_BYTES:
        raise _Unproven()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _records(path: Path, expected: tuple[int, int, int, int]):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != expected:
            raise _Unproven()
        count = 0
        offset = 0
        while offset < expected[2]:
            line = stream.readline(min(MAX_LINE_BYTES + 1, expected[2] - offset))
            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                raise _Unproven()
            count += 1
            if count > MAX_RECORDS:
                raise _Unproven()
            offset += len(line)
            event = json.loads(line)
            if not isinstance(event, dict):
                raise _Unproven()
            yield event, offset, line
        final = os.fstat(stream.fileno())
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != expected:
            raise _Unproven()
    if _stamp(path) != expected:
        raise _Unproven()


def _text_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _target(event: dict) -> tuple[int, str, str] | None:
    seq = event.get("seq")
    run = event.get("run_id")
    prompt = event.get("prompt")
    if (
        event.get("type") != "turn_started" or event.get("imported") is not True
        or event.get("backend") != "claude" or type(seq) is not int or seq <= 0
        or not isinstance(run, str) or not run.startswith("import_")
        or not isinstance(prompt, str) or len(prompt) > MAX_LINE_BYTES
    ):
        return None
    return seq, run, _text_key(prompt)


@dataclass(frozen=True)
class _Proof:
    provider_id: str
    events_stamp: tuple[int, int, int, int]
    source: Path | None
    source_stamp: tuple[int, int, int, int] | None
    targets: frozenset[tuple[int, str, str]]


def _prove(session_id: str, provider_id: str, events: Path, root: Path,
           normalize_user: Callable[[dict], str | None], events_stamp) -> _Proof:
    empty = _Proof(provider_id, events_stamp, None, None, frozenset())
    batches = {}
    candidates = []
    terminals = {}
    for event, _offset, _line in _records(events, events_stamp):
        if event.get("session_id") not in (None, "", session_id):
            continue
        run = event.get("run_id")
        if not isinstance(run, str) or not run.startswith("import_"):
            continue
        if event.get("type") == "history_imported":
            checkpoint = event.get("_history_sync_checkpoint")
            cursor = checkpoint.get("cursor") if isinstance(checkpoint, dict) else None
            if (
                event.get("backend") == "claude"
                and event.get("provider_session_id") == provider_id
                and isinstance(cursor, dict) and cursor.get("version") == 1
                and cursor.get("backend") == "claude"
                and cursor.get("provider_session_id") == provider_id
                and event.get("source_path") == cursor.get("source_path")
                and checkpoint.get("version") == 1
                and type(event.get("seq")) is int
            ):
                if run in batches:
                    raise _Unproven()
                batches[run] = (event["seq"], checkpoint)
        elif event.get("type") == "turn_finished" and event.get("imported") is True:
            if event.get("backend") == "claude" and type(event.get("seq")) is int:
                terminals[run] = event["seq"]
        else:
            target = _target(event)
            if target:
                candidates.append(target)
        if max(len(batches), len(candidates), len(terminals)) > MAX_TARGETS:
            raise _Unproven()
    if not batches or not candidates:
        return empty
    paths = {batch[1]["cursor"].get("source_path") for batch in batches.values()}
    if len(paths) != 1:
        raise _Unproven()
    raw_path = paths.pop()
    if not isinstance(raw_path, str):
        raise _Unproven()
    source = Path(raw_path)
    if not source.is_absolute() or source.suffix != ".jsonl" or source.stem != provider_id:
        raise _Unproven()
    if source.is_symlink():
        raise _Unproven()
    source = source.resolve(strict=True)
    source.relative_to(root.resolve(strict=True))
    source_stamp = _stamp(source)
    prefix_digests = {}
    eligible_batches = {}
    for run, (seq, checkpoint) in batches.items():
        cursor = checkpoint["cursor"]
        end = cursor.get("source_offset")
        start = checkpoint.get("previous_source_offset")
        expected_digest = cursor.get("source_digest")
        previous_digest = checkpoint.get("previous_source_digest")
        if (
            type(start) is not int or type(end) is not int
            or not 0 <= start < end <= source_stamp[2]
            or (cursor.get("source_dev"), cursor.get("source_ino")) != source_stamp[:2]
            or not isinstance(expected_digest, str) or not _DIGEST.fullmatch(expected_digest)
            or type(checkpoint.get("previous_present")) is not bool
            or (start > 0 and checkpoint.get("previous_present") is not True)
            or (start == 0 and previous_digest != "")
            or (start > 0 and (not isinstance(previous_digest, str) or not _DIGEST.fullmatch(previous_digest)))
            or terminals.get(run, 0) <= seq
        ):
            continue
        prefix_digests.setdefault(end, set()).add(expected_digest)
        if start:
            prefix_digests.setdefault(start, set()).add(previous_digest)
        eligible_batches[run] = (seq, terminals[run], start, end, expected_digest, previous_digest)
    if not eligible_batches:
        return _Proof(provider_id, events_stamp, source, source_stamp, frozenset())
    digest = hashlib.sha256()
    verified = set()
    metadata = {}
    humans = set()
    for event, offset, line in _records(source, source_stamp):
        digest.update(line)
        for wanted in prefix_digests.get(offset, ()):
            if hmac.compare_digest(digest.hexdigest(), wanted):
                verified.add((offset, wanted))
        if event.get("type") != "user":
            continue
        text = normalize_user(event)
        if not isinstance(text, str) or not text:
            continue
        key = _text_key(text)
        if event.get("isMeta") is True:
            metadata.setdefault(key, []).append(offset)
        else:
            humans.add(key)
        if len(metadata) + len(humans) > MAX_KEYS:
            raise _Unproven()
    targets = set()
    for target in candidates:
        seq, run, key = target
        batch = eligible_batches.get(run)
        if batch is None or key in humans:
            continue
        first_seq, last_seq, start, end, expected, previous = batch
        offsets = metadata.get(key, ())
        if (
            first_seq < seq < last_seq
            # Repeated use in another batch is fine; this original source
            # interval must identify exactly one metadata occurrence.
            and bisect_right(offsets, end) - bisect_right(offsets, start) == 1
            and (end, expected) in verified
            and (start == 0 or (start, previous) in verified)
        ):
            targets.add(target)
    return _Proof(provider_id, events_stamp, source, source_stamp, frozenset(targets))


class ClaudeMetadataRepairCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Serialize explicit preparation without blocking per-event lookups
        # (which can execute on the event loop) behind any filesystem work.
        self._prepare_lock = threading.Lock()
        self._proofs: OrderedDict[str, _Proof] = OrderedDict()
        self._preparing_session: str | None = None
        self._preparation_cancelled = False

    def prepare(self, session_id: str, provider_id: str, events: Path, root: Path,
                normalize_user: Callable[[dict], str | None]) -> bool:
        """Prepare only this requested session; report a changed suppression map."""
        with self._lock:
            previous = self._proofs.get(session_id)
            if previous and previous.provider_id == provider_id:
                self._proofs.move_to_end(session_id)
                return False
        with self._prepare_lock:
            with self._lock:
                previous = self._proofs.get(session_id)
                if previous and previous.provider_id == provider_id:
                    # A verified historical target remains a fact when either
                    # append-only log grows. Do not turn ordinary chat refreshes
                    # into repeated source scans. New imports already filter
                    # metadata; negative admissions are cached as well.
                    self._proofs.move_to_end(session_id)
                    return False
                self._preparing_session = session_id
                self._preparation_cancelled = False
            try:
                stamp = _stamp(events)
                if stamp[2] > MAX_EVENTS_BYTES:
                    raise _Unproven()
                with self._lock:
                    self._proofs.pop(session_id, None)
                proof = _prove(session_id, provider_id, events, root, normalize_user, stamp)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                # Fail visible, including incomplete or oversized files. A
                # failed admission must not retry on every page/socket read.
                proof = _Proof(provider_id, (0, 0, 0, 0), None, None, frozenset())
            with self._lock:
                cancelled = self._preparation_cancelled
                self._preparing_session = None
                if not cancelled:
                    self._proofs[session_id] = proof
                    self._proofs.move_to_end(session_id)
                    while len(self._proofs) > MAX_SESSIONS:
                        self._proofs.popitem(last=False)
                else:
                    return bool(previous and previous.targets)
            return bool((previous.targets if previous else frozenset()) != proof.targets)

    def signature(self, session_id: str) -> frozenset[tuple[int, str, str]]:
        """Memory-only cache identity: repaired timeline indexes cannot go stale."""
        with self._lock:
            proof = self._proofs.get(session_id)
            return proof.targets if proof else frozenset()

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._proofs.pop(session_id, None)
            if self._preparing_session == session_id:
                self._preparation_cancelled = True

    def is_hidden(self, session_id: str, event: dict) -> bool:
        """Memory-only per-event lookup; missing or evicted proof stays visible."""
        with self._lock:
            proof = self._proofs.get(session_id)
        if not proof or not proof.targets:
            return False
        try:
            target = _target(event)
        except (ValueError, TypeError):
            return False
        return target in proof.targets
