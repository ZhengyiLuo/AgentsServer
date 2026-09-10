"""Bounded, source-proven projection repair for old Claude metadata imports.

No transcript discovery, polling, durable mutation, or server imports. A caller
explicitly prepares one session; subsequent per-event checks use memory only.
Uncheckpointed imports and ambiguous user quotations are intentionally retained.
"""
from __future__ import annotations

from collections import OrderedDict
from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
from types import MappingProxyType
from typing import Callable

from claude_history_provenance import ClaudeInterruptionTracker


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


class _Oversized(_Unproven):
    pass


def _regular_stamp(path: Path) -> tuple[int, int, int, int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise _Unproven()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _stamp(path: Path) -> tuple[int, int, int, int]:
    value = _regular_stamp(path)
    if value[2] > MAX_BYTES:
        raise _Oversized()
    return value


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
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


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
    interruptions: tuple[tuple[tuple[int, str, str], str], ...] = ()
    companions: frozenset[tuple[str, int, str]] = frozenset()
    interruption_index: MappingProxyType = field(init=False, repr=False)
    cache_signature: frozenset = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "interruption_index", MappingProxyType(dict(self.interruptions)))
        object.__setattr__(self, "cache_signature", self.targets | frozenset(
            ("interruption", key, origin) for key, origin in self.interruptions)
            | frozenset(("companion", *key) for key in self.companions))

    def signature(self) -> frozenset:
        return self.cache_signature


def _timestamp(value) -> float | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _steer_intervals(events: list[dict], provider_id: str) -> list[tuple[float, float]]:
    """Only a complete native handoff can establish why a provider was interrupted."""
    queues, finished, starts = {}, {}, {}
    for event in events:
        kind = event.get("type")
        if kind == "turn_queue_run_now":
            key = event.get("interrupted_run_id")
            destination = queues
        elif kind in ("turn_finished", "turn_stopped"):
            key = event.get("run_id")
            destination = finished
        else:
            key = event.get("steer_interrupted_run_id")
            destination = starts
        if isinstance(key, str) and key and not key.startswith("import_"):
            destination.setdefault(key, []).append(event)
    intervals = []
    for run, queued in queues.items():
        ended, following = finished.get(run, ()), starts.get(run, ())
        if len(queued) != 1 or len(ended) != 1 or len(following) != 1:
            continue
        queue, terminal, start = queued[0], ended[0], following[0]
        queued_id = queue.get("queued_id")
        next_run = start.get("run_id")
        terminal_proven = (
            terminal.get("type") == "turn_finished"
            and terminal.get("stopped") is True
            and terminal.get("exit_code") is None
        ) or (
            terminal.get("type") == "turn_stopped"
            and terminal.get("native_steer") is True
            and terminal.get("superseded_by_run_id") == next_run
        )
        if (
            not terminal_proven
            or terminal.get("provider_session_id") != provider_id
            or not isinstance(queued_id, str) or not queued_id
            or start.get("queued_id") != queued_id
            or not isinstance(next_run, str) or not next_run or next_run == run
            or next_run.startswith("import_")
            or any(event.get("provider_session_id") not in (None, "", provider_id)
                   for event in (queue, start))
        ):
            continue
        times = [_timestamp(event.get("ts")) for event in (queue, terminal, start)]
        if any(value is None for value in times):
            continue
        # An interruption must be within five seconds of every corroborating
        # record, not merely near an unrelated stop elsewhere in this chat.
        lower, upper = max(times) - 5.0, min(times) + 5.0
        if lower <= upper:
            intervals.append((lower, upper))
    return intervals


def _interruption_origin(origin: dict, provider_id: str, intervals) -> dict | None:
    if (
        not isinstance(origin, dict) or origin.get("provider") != "claude"
        or origin.get("kind") != "interruption"
        or origin.get("session_id") != provider_id
        or not isinstance(origin.get("event_id"), str)
    ):
        return None
    timestamp = _timestamp(origin.get("timestamp"))
    if timestamp is None:
        return None
    result = {key: origin[key] for key in (
        "provider", "kind", "event_id", "session_id", "timestamp",
        "parent_event_id", "prompt_id",
    ) if key in origin}
    result["cause"] = "steer" if any(low <= timestamp <= high for low, high in intervals) else "unknown"
    return result


def _native_control(event: dict, session_id: str) -> dict | None:
    if not (
        event.get("session_id") == session_id and event.get("backend") == "claude"
        and event.get("imported") is not True
        and event.get("type") in ("turn_queue_run_now", "turn_finished", "turn_stopped", "turn_started")
    ):
        return None
    return {key: event[key] for key in (
        "type", "ts", "run_id", "queued_id", "interrupted_run_id",
        "steer_interrupted_run_id", "provider_session_id", "stopped", "exit_code",
        "native_steer", "superseded_by_run_id",
    ) if key in event}


def enrich_interruption_origins(events_path: Path, provider_id: str, origins: list[dict],
                                *, session_id: str) -> list[dict]:
    """Explicit import-worker enrichment; bounded native-log read, no source discovery.

    Valid origins retain their order and are copied with unknown cause on any
    read/proof failure. Invalid entries become empty dictionaries; an oversized
    input returns an empty list. Callers must not replace an origin with either.
    """
    if not isinstance(origins, list) or len(origins) > MAX_TARGETS:
        return []
    unknown = [_interruption_origin(origin, provider_id, ()) or {} for origin in origins]
    if not any(unknown) or not isinstance(session_id, str) or not session_id:
        return unknown
    try:
        stamp = _stamp(events_path)
        if stamp[2] > MAX_EVENTS_BYTES:
            raise _Unproven()
        controls = []
        for event, _offset, _line in _records(events_path, stamp):
            control = _native_control(event, session_id)
            if control is not None:
                controls.append(control)
                if len(controls) > MAX_TARGETS:
                    raise _Unproven()
        intervals = _steer_intervals(controls, provider_id)
        return [_interruption_origin(origin, provider_id, intervals) or {} for origin in unknown]
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return unknown


def _prove(session_id: str, provider_id: str, events: Path, root: Path,
           normalize_user: Callable[[dict], str | None], events_stamp,
           normalize_full_user: Callable[[dict], str | None] | None = None) -> _Proof:
    empty = _Proof(provider_id, events_stamp, None, None, frozenset())
    batches = {}
    candidates = []
    terminals = {}
    run_rows = {}
    terminal_counts = {}
    native_events = []
    scheduled_starts, scheduled_ends, candidate_origins = {}, {}, {}
    for event, _offset, _line in _records(events, events_stamp):
        if event.get("session_id") not in (None, "", session_id):
            continue
        control = _native_control(event, session_id)
        if control is not None:
            native_events.append(control)
            if len(native_events) > MAX_TARGETS:
                raise _Unproven()
        run = event.get("run_id")
        if (
            isinstance(run, str) and run and not run.startswith("import_")
            and event.get("backend") == "claude" and event.get("imported") is not True
        ):
            if (event.get("type") == "turn_started"
                    and event.get("purpose") == "scheduled_job" and event.get("job_id")
                    and isinstance(event.get("prompt"), str)):
                scheduled_starts.setdefault(run, []).append((
                    _text_key(" ".join(event["prompt"].split())), _timestamp(event.get("ts")),
                ))
            elif event.get("type") == "turn_finished":
                scheduled_ends.setdefault(run, []).append(_timestamp(event.get("ts")))
            if len(scheduled_starts) + len(scheduled_ends) > MAX_TARGETS:
                raise _Unproven()
        if not isinstance(run, str) or not run.startswith("import_"):
            continue
        run_rows[run] = run_rows.get(run, 0) + 1
        if len(run_rows) > MAX_TARGETS:
            raise _Unproven()
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
                terminal_counts[run] = terminal_counts.get(run, 0) + 1
        else:
            target = _target(event)
            if target:
                candidates.append(target)
                origin = event.get("provider_origin")
                if isinstance(origin, dict) and origin.get("provider") == "claude":
                    candidate_origins[target] = (
                        origin.get("event_id"), origin.get("session_id"), origin.get("timestamp"),
                    )
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
    human_offsets = {}
    interruptions = {}
    interruption_count = 0
    tracker = ClaudeInterruptionTracker()
    steer_intervals = _steer_intervals(native_events, provider_id)
    scheduled_ranges = {}
    for run, starts in scheduled_starts.items():
        ends = scheduled_ends.get(run, ())
        if len(starts) == len(ends) == 1:
            key, start_time = starts[0]
            end_time = ends[0]
            if start_time is not None and end_time is not None and start_time <= end_time:
                scheduled_ranges.setdefault(key, []).append((run, start_time, end_time))
    scheduled_sources, scheduled_counts = {}, {}
    for event, offset, line in _records(source, source_stamp):
        digest.update(line)
        for wanted in prefix_digests.get(offset, ()):
            if hmac.compare_digest(digest.hexdigest(), wanted):
                verified.add((offset, wanted))
        origin = tracker.consume(event)
        if event.get("type") != "user":
            continue
        text = normalize_user(event)
        if not isinstance(text, str) or not text:
            continue
        key = _text_key(text)
        if (normalize_full_user is not None and scheduled_ranges
                and event.get("sessionId") == provider_id and event.get("isMeta") is not True
                and event.get("isSidechain") is not True):
            full_text = normalize_full_user(event)
            source_time = _timestamp(event.get("timestamp"))
            source_identity = (event.get("uuid"), provider_id, event.get("timestamp"))
            if isinstance(full_text, str) and isinstance(source_identity[0], str) and source_time is not None:
                matches = [run for run, start_time, end_time in scheduled_ranges.get(
                    _text_key(" ".join(full_text.split())), (),
                ) if start_time <= source_time <= end_time]
                if len(matches) == 1:
                    run = matches[0]
                    scheduled_counts[run] = scheduled_counts.get(run, 0) + 1
                    scheduled_sources.setdefault((source_identity, key), []).append((offset, run))
        origin = _interruption_origin(origin, provider_id, steer_intervals)
        if origin is not None:
            interruptions.setdefault(key, []).append((offset, origin))
            interruption_count += 1
            if interruption_count > MAX_TARGETS:
                raise _Unproven()
        if event.get("isMeta") is True or event.get("isCompactSummary") is True or event.get("isSidechain") is True:
            metadata.setdefault(key, []).append(offset)
        else:
            # Preserve the existing global ambiguity rule for isMeta repairs.
            humans.add(key)
            if origin is None:
                human_offsets.setdefault(key, []).append(offset)
        if len(metadata) + len(humans) + len(interruptions) > MAX_KEYS:
            raise _Unproven()
    targets = set()
    corrected = []
    candidate_counts = {}
    candidate_origin_counts = {}
    run_candidate_counts = {}
    for candidate in candidates:
        _seq, run, key = candidate
        candidate_counts[(run, key)] = candidate_counts.get((run, key), 0) + 1
        origin_key = (run, candidate_origins.get(candidate))
        candidate_origin_counts[origin_key] = candidate_origin_counts.get(origin_key, 0) + 1
        run_candidate_counts[run] = run_candidate_counts.get(run, 0) + 1
    for target in candidates:
        seq, run, key = target
        batch = eligible_batches.get(run)
        if batch is None:
            continue
        first_seq, last_seq, start, end, expected, previous = batch
        if not (
            first_seq < seq < last_seq
            and (end, expected) in verified
            and (start == 0 or (start, previous) in verified)
        ):
            continue
        # A scheduled wake is ordinary provider user input, not isMeta. Prove
        # its complete text, native occurrence interval, exact source identity
        # and original import checkpoint. A shared trimmed prefix is not proof.
        scheduled = scheduled_sources.get((candidate_origins.get(target), key), ())
        if (len(scheduled) == 1 and candidate_origin_counts[(run, candidate_origins.get(target))] == 1
                and start < scheduled[0][0] <= end
                and scheduled_counts.get(scheduled[0][1]) == 1):
            targets.add(target)
            continue
        proven = [(offset, origin) for offset, origin in interruptions.get(key, ())
                  if start < offset <= end]
        ordinary = human_offsets.get(key, ())
        if (
            len(proven) == 1 and candidate_counts[(run, key)] == 1
            and bisect_right(ordinary, end) - bisect_right(ordinary, start) == 0
        ):
            corrected.append((target, json.dumps(proven[0][1], sort_keys=True, separators=(",", ":"))))
            continue
        # An ambiguous interruption is never demoted to a hidden metadata row.
        if proven:
            continue
        offsets = metadata.get(key, ())
        if key not in humans and bisect_right(offsets, end) - bisect_right(offsets, start) == 1:
            targets.add(target)
    corrected_counts = {}
    for (_seq, run, _key), _origin in corrected:
        corrected_counts[run] = corrected_counts.get(run, 0) + 1
    for _seq, run, _key in targets:
        corrected_counts[run] = corrected_counts.get(run, 0) + 1
    companions = set()
    for run, count in corrected_counts.items():
        # Only a complete marker-only batch is lifecycle-neutral. Any genuine
        # user, assistant/tool/unknown row, duplicate terminal or unproven target
        # leaves both import companions unchanged.
        if (count == run_candidate_counts[run] and run_rows[run] == count + 2
                and terminal_counts.get(run) == 1):
            companions.add(("history_imported", eligible_batches[run][0], run))
            companions.add(("turn_finished", eligible_batches[run][1], run))
    return _Proof(provider_id, events_stamp, source, source_stamp, frozenset(targets),
                  tuple(corrected), frozenset(companions))


def _bounded_records(path: Path, expected, start: int, end: int):
    """Read complete records in one fixed window, never the oversized prefix."""
    if not 0 <= start < end <= expected[2] or end - start > MAX_EVENTS_BYTES:
        raise _Unproven()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != expected:
            raise _Unproven()
        stream.seek(start)
        if start:
            stream.seek(start - 1)
            if stream.read(1) != b"\n":
                skipped = stream.readline(min(MAX_LINE_BYTES + 1, end - start))
                if len(skipped) > MAX_LINE_BYTES or not skipped.endswith(b"\n"):
                    raise _Unproven()
        count = 0
        while stream.tell() < end:
            offset = stream.tell()
            line = stream.readline(min(MAX_LINE_BYTES + 1, end - offset))
            if len(line) > MAX_LINE_BYTES or not line.endswith(b"\n"):
                raise _Unproven()
            count += 1
            if count > MAX_RECORDS:
                raise _Unproven()
            event = json.loads(line)
            if not isinstance(event, dict):
                raise _Unproven()
            yield event, stream.tell()
        final = os.fstat(stream.fileno())
        if (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != expected:
            raise _Unproven()
    if _regular_stamp(path) != expected:
        raise _Unproven()


def _prove_recent_scheduled(session_id: str, provider_id: str, events: Path, root: Path,
                            normalize_user, events_stamp, normalize_full_user) -> _Proof:
    """Exact-origin scheduled duplicates and provider metadata in recent history.

    An oversized transcript cannot establish global metadata/interruption proof.
    Scheduled input requires a complete native occurrence and full source-text
    match. Metadata requires its explicit structured source flag. Both require
    an exact, unique origin within the original checkpoint; wording is not proof.
    Both reads are bounded to 32 MiB; source growth before preparation is allowed.
    Any change during either read fails visible.
    """
    empty = _Proof(provider_id, events_stamp, None, None, frozenset())
    if normalize_full_user is None or not events_stamp[2]:
        return empty
    starts, ends, batches, terminals, rows, candidates = {}, {}, {}, {}, {}, []
    origin_counts = {}
    previous_seq = 0
    for event, _offset in _bounded_records(
        events, events_stamp, max(0, events_stamp[2] - MAX_EVENTS_BYTES), events_stamp[2],
    ):
        seq = event.get("seq")
        if type(seq) is not int or seq <= previous_seq:
            raise _Unproven()
        previous_seq = seq
        if event.get("session_id") not in (None, "", session_id):
            continue
        run = event.get("run_id")
        if not isinstance(run, str) or not run:
            continue
        if not run.startswith("import_"):
            if (event.get("backend") != "claude" or event.get("imported") is True
                    or event.get("provider_session_id") not in (None, "", provider_id)):
                continue
            if (event.get("type") == "turn_started" and event.get("purpose") == "scheduled_job"
                    and event.get("job_id") and isinstance(event.get("prompt"), str)):
                starts.setdefault(run, []).append((
                    _text_key(" ".join(event["prompt"].split())), _timestamp(event.get("ts")),
                ))
            elif event.get("type") == "turn_finished":
                ends.setdefault(run, []).append(_timestamp(event.get("ts")))
        else:
            rows[run] = rows.get(run, 0) + 1
            if event.get("type") == "history_imported":
                checkpoint = event.get("_history_sync_checkpoint")
                cursor = checkpoint.get("cursor") if isinstance(checkpoint, dict) else None
                if (event.get("backend") == "claude" and event.get("provider_session_id") == provider_id
                        and isinstance(cursor, dict) and cursor.get("version") == 1
                        and cursor.get("backend") == "claude" and cursor.get("provider_session_id") == provider_id
                        and checkpoint.get("version") == 1 and event.get("source_path") == cursor.get("source_path")):
                    batches.setdefault(run, []).append((seq, checkpoint))
            elif (event.get("type") == "turn_finished" and event.get("imported") is True
                    and event.get("backend") == "claude"):
                terminals.setdefault(run, []).append(seq)
            target = _target(event)
            if target:
                origin = event.get("provider_origin")
                identity = (origin.get("event_id"), origin.get("session_id"), origin.get("timestamp")) if isinstance(origin, dict) else ()
                if (isinstance(origin, dict) and origin.get("provider") == "claude"
                        and len(identity) == 3 and all(isinstance(value, str) and value for value in identity)
                        and identity[1] == provider_id and _timestamp(identity[2]) is not None):
                    candidates.append((target, identity))
                    origin_key = (run, identity)
                    origin_counts[origin_key] = origin_counts.get(origin_key, 0) + 1
        if (sum(map(len, (starts, ends, batches, terminals, rows))) > MAX_TARGETS
                or len(candidates) > MAX_TARGETS):
            raise _Unproven()
    if not batches or not candidates:
        return empty
    eligible = {}
    for run, checkpoints in batches.items():
        finished = terminals.get(run, ())
        if len(checkpoints) != 1 or len(finished) != 1:
            continue
        seq, checkpoint = checkpoints[0]
        cursor = checkpoint["cursor"]
        start, end = checkpoint.get("previous_source_offset"), cursor.get("source_offset")
        previous, digest = checkpoint.get("previous_source_digest"), cursor.get("source_digest")
        if (type(start) is int and type(end) is int and 0 <= start < end
                and type(checkpoint.get("previous_present")) is bool
                and ((start == 0 and previous == "") or
                     (start > 0 and checkpoint["previous_present"] is True
                      and isinstance(previous, str) and _DIGEST.fullmatch(previous)))
                and isinstance(digest, str) and _DIGEST.fullmatch(digest) and seq < finished[0]):
            eligible[run] = (seq, finished[0], start, end, cursor)
    if not eligible:
        return empty
    paths = {batch[4].get("source_path") for batch in eligible.values()}
    if len(paths) != 1:
        raise _Unproven()
    raw_path = paths.pop()
    if not isinstance(raw_path, str):
        raise _Unproven()
    source = Path(raw_path)
    if not source.is_absolute() or source.suffix != ".jsonl" or source.stem != provider_id or source.is_symlink():
        raise _Unproven()
    source = source.resolve(strict=True)
    source.relative_to(root.resolve(strict=True))
    source_stamp = _regular_stamp(source)
    eligible = {run: batch for run, batch in eligible.items()
                if (batch[4].get("source_dev"), batch[4].get("source_ino")) == source_stamp[:2]
                and batch[3] <= source_stamp[2]}
    if not eligible:
        return empty
    checkpoint_end = max(batch[3] for batch in eligible.values())
    window_start = max(0, checkpoint_end - MAX_EVENTS_BYTES)
    ranges = {}
    for run, occurrences in starts.items():
        finished = ends.get(run, ())
        if len(occurrences) == len(finished) == 1:
            key, start_time = occurrences[0]
            end_time = finished[0]
            if start_time is not None and end_time is not None and start_time <= end_time:
                ranges.setdefault(key, []).append((run, start_time, end_time))
    source_matches, occurrence_counts, identity_counts, source_metadata = {}, {}, {}, {}
    for event, offset in _bounded_records(source, source_stamp, window_start, checkpoint_end):
        if event.get("type") != "user" or event.get("sessionId") != provider_id:
            continue
        identity = (event.get("uuid"), provider_id, event.get("timestamp"))
        if not all(isinstance(value, str) and value for value in identity):
            continue
        identity_counts[identity] = identity_counts.get(identity, 0) + 1
        display_text = normalize_user(event)
        timestamp = _timestamp(identity[2])
        if not isinstance(display_text, str) or not display_text or timestamp is None:
            continue
        if event.get("isMeta") is True or event.get("isCompactSummary") is True or event.get("isSidechain") is True:
            source_metadata.setdefault((identity, _text_key(display_text)), []).append(offset)
            if len(identity_counts) + len(source_metadata) > MAX_KEYS:
                raise _Unproven()
            continue
        full_text = normalize_full_user(event)
        if not isinstance(full_text, str) or not full_text:
            continue
        matches = [run for run, start, end in ranges.get(_text_key(" ".join(full_text.split())), ())
                   if start <= timestamp <= end]
        if len(matches) == 1:
            occurrence = matches[0]
            occurrence_counts[occurrence] = occurrence_counts.get(occurrence, 0) + 1
            source_matches.setdefault((identity, _text_key(display_text)), []).append((offset, occurrence))
        if len(identity_counts) + len(source_matches) > MAX_KEYS:
            raise _Unproven()
    targets, counts = set(), {}
    for target, identity in candidates:
        seq, run, key = target
        batch, matched = eligible.get(run), source_matches.get((identity, key), ())
        if (batch is None or origin_counts[(run, identity)] != 1
                or identity_counts.get(identity) != 1):
            continue
        first, last, start, end, _cursor = batch
        metadata = source_metadata.get((identity, key), ())
        proven_metadata = len(metadata) == 1 and start < metadata[0] <= end
        proven_scheduled = (len(matched) == 1 and occurrence_counts.get(matched[0][1]) == 1
                            and start < matched[0][0] <= end)
        if first < seq < last and (proven_metadata or proven_scheduled):
            targets.add(target)
            counts[run] = counts.get(run, 0) + 1
    companions = set()
    for run, count in counts.items():
        if rows[run] == count + 2:
            companions.add(("history_imported", eligible[run][0], run))
            companions.add(("turn_finished", eligible[run][1], run))
    return _Proof(provider_id, events_stamp, source, source_stamp, frozenset(targets),
                  companions=frozenset(companions))


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
                normalize_user: Callable[[dict], str | None], *,
                normalize_full_user: Callable[[dict], str | None] | None = None) -> bool:
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
                try:
                    stamp = _stamp(events)
                except _Oversized:
                    if normalize_full_user is None:
                        raise
                    stamp = _regular_stamp(events)
                with self._lock:
                    self._proofs.pop(session_id, None)
                try:
                    if stamp[2] > MAX_EVENTS_BYTES:
                        raise _Oversized()
                    proof = _prove(session_id, provider_id, events, root, normalize_user, stamp,
                                   normalize_full_user)
                except _Oversized:
                    if normalize_full_user is None:
                        raise
                    proof = _prove_recent_scheduled(session_id, provider_id, events, root,
                                                     normalize_user, stamp, normalize_full_user)
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
                    return bool(previous and previous.signature())
            return bool((previous.signature() if previous else frozenset()) != proof.signature())

    def signature(self, session_id: str) -> frozenset:
        """Memory-only cache identity: repaired timeline indexes cannot go stale."""
        with self._lock:
            proof = self._proofs.get(session_id)
            return proof.signature() if proof else frozenset()

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

    def interruption_origin(self, session_id: str, event: dict) -> dict | None:
        """Return copied, verified provenance without filesystem access."""
        if event.get("session_id") not in (None, "", session_id):
            return None
        with self._lock:
            proof = self._proofs.get(session_id)
        if not proof or not proof.interruptions:
            return None
        try:
            target = _target(event)
        except (ValueError, TypeError):
            return None
        origin = proof.interruption_index.get(target)
        return json.loads(origin) if origin is not None else None

    def project_interruption(self, session_id: str, event: dict) -> dict | None:
        origin = self.interruption_origin(session_id, event)
        if origin is None:
            return None
        projected = dict(event)
        projected.pop("prompt", None)
        projected.pop("text", None)
        projected.pop("_agentsdock_imported_prompt_hidden", None)
        projected.update(type="provider_interruption", imported=True,
                         ts=origin["timestamp"], provider_origin=origin)
        return projected

    def project_event(self, session_id: str, event: dict) -> dict | None:
        """Memory-only correction of an exact interruption or its proven companions."""
        kind, seq, run = event.get("type"), event.get("seq"), event.get("run_id")
        if (
            kind in ("history_imported", "turn_finished") and type(seq) is int
            and isinstance(run, str) and event.get("backend") == "claude"
            and event.get("session_id") in (None, "", session_id)
            and (kind == "history_imported" or event.get("imported") is True)
        ):
            with self._lock:
                proof = self._proofs.get(session_id)
            if (proof and (kind, seq, run) in proof.companions
                    and event.get("provider_session_id") in (None, "", proof.provider_id)):
                return {**event, "imported": True, "metadata_only": True}
        return self.project_interruption(session_id, event)
