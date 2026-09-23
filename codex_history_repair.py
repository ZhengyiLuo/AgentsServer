"""Read-only, checkpoint-proven repair of Codex runtime and native replay imports.

Preparation is explicit, bounded and once per requested chat/provider. Event
projection is memory-only. Missing, changing or ambiguous evidence stays visible.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import Callable
from datetime import datetime

from claude_history_repair import (
    MAX_EVENTS_BYTES, MAX_KEYS, MAX_SESSIONS, MAX_TARGETS,
    _DIGEST, _Unproven, _records, _stamp, _text_key,
)


MAX_PRIOR_SOURCE_PATHS = 2
MAX_AGGREGATE_SOURCE_BYTES = 96 * 1024 * 1024
MAX_AGGREGATE_SOURCE_RECORDS = 100_000
MAX_FORK_META_HEADERS = 32
_PROVIDER_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_NATIVE_LEADING_DECORATION_RE = re.compile(
    r"(?m)^[ \t]*(?:(?::[A-Za-z0-9_+\-]+:|[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?)[ \t]*)+"
)
NATIVE_PROOF_LINE_BYTES = 4 * 1024 * 1024
NATIVE_PROOF_SECONDS = 30.0
_COMPACTION_PREFIX = (
    "Another language model started to solve this problem and produced a summary of its thinking process. "
    "You also have access to the state of the tools that were used by that language model. "
    "Use this to build on the work that has already been done and avoid duplicating work. "
    "Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:\n"
)


class CodexCompactionSummaryTracker:
    """Match one native compaction response, never classify assistant wording.

    Some endpoints persist the compactor's response as an ordinary assistant
    item. Only its immediately following usage receipt and typed replacement
    history can prove that item belongs to the compaction, not the conversation.
    """
    def __init__(self, provider_id: str | None = None):
        self.provider_id = provider_id
        self.pending = None

    def observe(self, record: dict) -> dict | None:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            self.pending = None
            return None
        kind = record.get("type")
        if kind == "response_item":
            self.pending = None
            origin = codex_public_item_origin(record, self.provider_id or "")
            metadata = payload.get("internal_chat_message_metadata_passthrough")
            content = payload.get("content")
            if (origin and origin["kind"] == "assistant" and payload.get("phase") == "final_answer"
                and isinstance(metadata, dict) and metadata.get("content_item_kinds") == ["unknown"]
                and isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                and content[0].get("type") == "output_text" and isinstance(content[0].get("text"), str)
                and 0 < len(content[0]["text"]) <= NATIVE_PROOF_LINE_BYTES):
                self.pending = {"origin": origin, "text": content[0]["text"], "response_id": None}
            return None
        pending = self.pending
        if pending is None:
            return None
        if kind == "event_msg" and payload.get("type") == "token_count":
            return None
        if kind == "token_usage_record":
            response = payload.get("response_id")
            thread = payload.get("thread_id")
            if (pending["response_id"] is None and isinstance(thread, str) and _PROVIDER_ID.fullmatch(thread)
                and (self.provider_id is None or thread == self.provider_id)
                and payload.get("turn_id") == pending["origin"]["turn_id"]
                and isinstance(response, str) and 0 < len(response) <= 4096):
                pending["response_id"] = response
                pending["thread_id"] = thread
                return None
        self.pending = None
        if kind != "compacted" or pending["response_id"] is None:
            return None
        latest = payload.get("latest_token_usage_record")
        replacement = payload.get("replacement_history")
        if not isinstance(latest, dict) or not isinstance(replacement, list) or not replacement:
            return None
        summary = replacement[-1]
        if not isinstance(summary, dict):
            return None
        metadata = summary.get("internal_chat_message_metadata_passthrough")
        text = _COMPACTION_PREFIX + pending["text"]
        start, end = _delivery_timestamp(pending["origin"]["timestamp"]), _delivery_timestamp(record.get("timestamp"))
        if (payload.get("compaction_response_id") != pending["response_id"]
            or latest.get("response_id") != pending["response_id"] or latest.get("thread_id") != pending["thread_id"]
            or latest.get("turn_id") != pending["origin"]["turn_id"]
            or start is None or end is None or end < start or payload.get("message") != text
            or summary.get("type") != "message" or summary.get("role") != "user"
            or not isinstance(metadata, dict) or metadata.get("turn_id") != pending["origin"]["turn_id"]
            or metadata.get("content_item_kinds") != ["compaction.summary"]
            or summary.get("content") != [{"type": "input_text", "text": text}]):
            return None
        return {**pending["origin"], "kind": "compaction_summary", "source_text_sha256": _text_key(pending["text"])}

    def discard_summary(self, items, record: dict | None) -> bool:
        """Use the existing parse pass; incomplete ranges keep their last item."""
        if record is None:
            self.pending = None
            return False
        proof = self.observe(record)
        if proof is None or not items:
            return False
        item = items[-1]
        origin = item.get("provider_origin")
        if (item.get("kind") == "assistant" and isinstance(origin, dict)
            and all(origin.get(key) == proof.get(key) for key in ("event_id", "turn_id", "timestamp"))):
            items.pop()
            return True
        return False


class CodexNativeHistoryProofUnavailable(ValueError):
    """Incomplete evidence must defer import, not become a new human message."""


@dataclass
class _NativeReadBudget:
    cancelled: Callable[[], bool] | None = None
    deadline: float | None = None

    def __post_init__(self) -> None:
        if self.deadline is None:
            self.deadline = time.monotonic() + NATIVE_PROOF_SECONDS

    def check(self) -> None:
        if ((self.cancelled is not None and self.cancelled())
                or time.monotonic() >= self.deadline):
            raise CodexNativeHistoryProofUnavailable("Codex history proof was cancelled or exceeded its work budget")


def _native_stamp(path: Path) -> tuple[int, int, int, int]:
    """Pin a regular file without a total-size cutoff or following symlinks."""
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        raise _Unproven()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _native_records(path: Path, expected: tuple[int, int, int, int], budget: _NativeReadBudget,
                    *, end: int | None = None):
    """Stream one fixed prefix; tool volume does not consume retained-key budget.

    A source proof needs only the prefix ending at its newest checkpoint. Never
    parse or hash a later unrelated source tail. Cancellation is checked between
    individually bounded records, including records that carry no public text.
    """
    budget.check()
    boundary = expected[2] if end is None else end
    if type(boundary) is not int or not 0 <= boundary <= expected[2]:
        raise _Unproven()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != expected:
            raise _Unproven()
        offset = 0
        while offset < boundary:
            budget.check()
            line = stream.readline(min(NATIVE_PROOF_LINE_BYTES + 1, boundary - offset))
            if not line or len(line) > NATIVE_PROOF_LINE_BYTES or not line.endswith(b"\n"):
                raise _Unproven()
            offset += len(line)
            record = json.loads(line)
            if not isinstance(record, dict):
                raise _Unproven()
            yield record, offset, line
        info = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != expected:
            raise _Unproven()
    budget.check()
    if _native_stamp(path) != expected:
        raise _Unproven()


def _native_event_identity(event: dict) -> dict:
    """Keep bounded public identity, never retain tool bodies or message text."""
    return {key: event[key] for key in ("id", "seq", "type", "phase", "item_id")
            if key in event and (type(event[key]) is int or isinstance(event[key], str) and len(event[key]) <= 256)}


def _native_import_batch(event: dict) -> dict:
    result = {key: event.get(key) for key in ("seq", "provider_session_id", "source_path")}
    checkpoint = event.get("_history_sync_checkpoint")
    if isinstance(checkpoint, dict) and isinstance(checkpoint.get("cursor"), dict):
        result["_history_sync_checkpoint"] = {
            **{key: checkpoint.get(key) for key in (
                "version", "previous_present", "previous_source_offset", "previous_source_digest")},
            "cursor": {key: checkpoint["cursor"].get(key) for key in (
                "version", "backend", "provider_session_id", "source_path", "source_offset",
                "source_dev", "source_ino", "source_digest")},
        }
    # Unexpected huge metadata cannot make the retained compact proof unbounded.
    if len(json.dumps(result)) > 16 * 1024:
        raise _Unproven()
    return result


def _native_assistant_text(text: str) -> str:
    """Mirror native clean_assistant_text; never normalize source proof keys."""
    return _NATIVE_LEADING_DECORATION_RE.sub("", str(text or "")).strip()


def _public_assistant_item_id(event: dict) -> str | None:
    item_id = event.get("item_id")
    public = event.get("type") == "assistant_text" or (
        event.get("type") == "reasoning_summary" and event.get("phase") == "commentary"
    )
    return item_id if public and isinstance(item_id, str) and 0 < len(item_id) <= 256 else None


@dataclass
class _SourceBudget:
    bytes_remaining: int
    records_remaining: int

    def reserve(self, size: int) -> None:
        if size > self.bytes_remaining or self.records_remaining <= 0:
            raise _Unproven()
        self.bytes_remaining -= size

    def consume_record(self) -> None:
        if self.records_remaining <= 0:
            raise _Unproven()
        self.records_remaining -= 1


def _target(event: dict) -> tuple[int, str, str] | None:
    seq, run, prompt = event.get("seq"), event.get("run_id"), event.get("prompt")
    if (
        event.get("type") != "turn_started" or event.get("imported") is not True
        or event.get("backend") != "codex" or event.get("provider_user_authored") is True
        or type(seq) is not int or seq <= 0
        or not isinstance(run, str) or not run.startswith("import_")
        or not isinstance(prompt, str) or not prompt or len(prompt) > 4 * 1024 * 1024
    ):
        return None
    return seq, run, _text_key(prompt)


@dataclass(frozen=True)
class _Proof:
    provider_id: str
    targets: frozenset[tuple[int, str, str]] = frozenset()
    positions: frozenset[tuple[int, str]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", frozenset((seq, run) for seq, run, _key in self.targets))


def _prove(session_id: str, provider_id: str, events: Path, source: Path | None, root: Path,
           normalize_user: Callable[[dict], str | None],
           classify_user: Callable[[dict], str | None]) -> _Proof:
    """Inspect one ledger and only its current/two newest recorded prior sources."""
    if not _PROVIDER_ID.fullmatch(provider_id):
        raise _Unproven()
    events_stamp = _stamp(events)
    if events_stamp[2] > MAX_EVENTS_BYTES:
        raise _Unproven()
    batches, terminals, candidates = {}, {}, []
    terminal_counts = Counter()
    for event, _offset, _line in _records(events, events_stamp):
        if event.get("session_id") not in (None, "", session_id):
            continue
        run = event.get("run_id")
        if not isinstance(run, str) or not run.startswith("import_"):
            continue
        if event.get("type") == "history_imported":
            checkpoint = event.get("_history_sync_checkpoint")
            cursor = checkpoint.get("cursor") if isinstance(checkpoint, dict) else None
            batch_provider = event.get("provider_session_id")
            batch_source = event.get("source_path")
            if (
                event.get("backend") == "codex"
                and isinstance(batch_provider, str) and _PROVIDER_ID.fullmatch(batch_provider)
                and isinstance(batch_source, str) and 0 < len(batch_source) <= 4096
                and isinstance(cursor, dict) and cursor.get("version") == 1
                and cursor.get("backend") == "codex" and cursor.get("provider_session_id") == batch_provider
                and batch_source == cursor.get("source_path")
                and checkpoint.get("version") == 1 and type(event.get("seq")) is int
            ):
                if run in batches:
                    raise _Unproven()
                batches[run] = (event["seq"], checkpoint)
        elif (event.get("type") == "turn_finished" and event.get("imported") is True
              and event.get("backend") == "codex" and type(event.get("seq")) is int):
            terminals[run] = event["seq"]
            terminal_counts[run] += 1
        else:
            target = _target(event)
            if target:
                candidates.append(target)
        if max(len(batches), len(candidates), len(terminals)) > MAX_TARGETS:
            raise _Unproven()
    if not batches or not candidates:
        return _Proof(provider_id)
    candidate_runs = {run for _seq, run, _key in candidates}
    groups = {}
    for run, (seq, checkpoint) in batches.items():
        if run not in candidate_runs or terminals.get(run, 0) <= seq or terminal_counts[run] != 1:
            continue
        cursor = checkpoint["cursor"]
        key = (cursor["provider_session_id"], cursor["source_path"])
        group = groups.setdefault(key, {})
        group[run] = (seq, checkpoint)
    current_key = (provider_id, str(source)) if source is not None else None
    prior_keys = sorted(
        (key for key in groups if key[0] != provider_id),
        key=lambda key: max(seq for seq, _checkpoint in groups[key].values()),
        reverse=True,
    )[:MAX_PRIOR_SOURCE_PATHS]
    selected = ([current_key] if current_key in groups else []) + prior_keys
    budget = _SourceBudget(MAX_AGGREGATE_SOURCE_BYTES, MAX_AGGREGATE_SOURCE_RECORDS)
    targets = set()
    for source_provider, source_path in selected:
        selected_batches = groups[(source_provider, source_path)]
        selected_candidates = [target for target in candidates if target[1] in selected_batches]
        try:
            targets.update(_prove_source(
                source_provider, Path(source_path), root, selected_batches, terminals,
                selected_candidates, normalize_user, classify_user, budget,
            ))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            # A missing, changed, oversized or ambiguous prior source cannot
            # invalidate a proof independently established from another source.
            continue
    return _Proof(provider_id, frozenset(targets))


def _prove_source(provider_id: str, source: Path, root: Path,
                  batches: dict, terminals: dict, candidates: list,
                  normalize_user: Callable[[dict], str | None],
                  classify_user: Callable[[dict], str | None],
                  budget: _SourceBudget) -> set[tuple[int, str, str]]:
    if (
        not source.is_absolute() or source.suffix != ".jsonl" or source.is_symlink()
        or not (source.stem == provider_id or source.stem.endswith("-" + provider_id))
    ):
        raise _Unproven()
    canonical_source = source.resolve(strict=True)
    canonical_source.relative_to(root.resolve(strict=True))
    source_stamp = _stamp(canonical_source)
    wanted, eligible = {}, {}
    for run, (seq, checkpoint) in batches.items():
        cursor = checkpoint["cursor"]
        start, end = checkpoint.get("previous_source_offset"), cursor.get("source_offset")
        expected, previous = cursor.get("source_digest"), checkpoint.get("previous_source_digest")
        if (
            type(start) is not int or type(end) is not int or not 0 <= start < end <= source_stamp[2]
            or (cursor.get("source_dev"), cursor.get("source_ino")) != source_stamp[:2]
            or not isinstance(expected, str) or not _DIGEST.fullmatch(expected)
            or type(checkpoint.get("previous_present")) is not bool
            or (start > 0 and checkpoint.get("previous_present") is not True)
            or (start == 0 and previous != "")
            or (start > 0 and (not isinstance(previous, str) or not _DIGEST.fullmatch(previous)))
        ):
            continue
        wanted.setdefault(end, set()).add(expected)
        if start:
            wanted.setdefault(start, set()).add(previous)
        eligible[run] = (seq, terminals[run], start, end, expected, previous)
    if not eligible:
        return set()
    budget.reserve(source_stamp[2])
    digest, verified, goals, other_users = hashlib.sha256(), set(), {}, set()
    source_owner_seen, goal_count = False, 0
    for event, offset, line in _records(canonical_source, source_stamp):
        budget.consume_record()
        if budget.records_remaining == 0 and offset < source_stamp[2]:
            raise _Unproven()
        payload = event.get("payload")
        if not source_owner_seen:
            if event.get("type") != "session_meta" or not isinstance(payload, dict) or payload.get("id") != provider_id:
                raise _Unproven()
            source_owner_seen = True
        elif event.get("type") == "session_meta":
            if not isinstance(payload, dict) or payload.get("id") != provider_id:
                raise _Unproven()
        digest.update(line)
        for expected in wanted.get(offset, ()):
            if hmac.compare_digest(digest.hexdigest(), expected):
                verified.add((offset, expected))
        classification = classify_user(event)
        if classification is None:
            continue
        text = normalize_user(event)
        if not isinstance(text, str) or not text:
            continue
        key = _text_key(text)
        if classification == "goal":
            goals.setdefault(key, []).append(offset)
            goal_count += 1
            if goal_count > MAX_TARGETS:
                raise _Unproven()
        else:
            # Even an unknown user source makes identical quoted text ambiguous.
            other_users.add(key)
        if len(goals) + len(other_users) > MAX_KEYS:
            raise _Unproven()
    counts = Counter((run, key) for _seq, run, key in candidates)
    targets = set()
    for target in candidates:
        seq, run, key = target
        batch = eligible.get(run)
        if batch is None or key in other_users:
            continue
        first_seq, last_seq, start, end, expected, previous = batch
        offsets = goals.get(key, ())
        occurrences = bisect_right(offsets, end) - bisect_right(offsets, start)
        if (
            first_seq < seq < last_seq and (end, expected) in verified
            and (start == 0 or (start, previous) in verified)
            and occurrences > 0 and counts[(run, key)] == occurrences
        ):
            targets.add(target)
    return targets


class CodexGoalHistoryRepairCache:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._proofs: OrderedDict[str, _Proof] = OrderedDict()
        self._preparing: str | None = None
        self._cancelled = False

    def is_prepared(self, session_id: str, provider_id: str) -> bool:
        with self._lock:
            proof = self._proofs.get(session_id)
            return proof is not None and proof.provider_id == provider_id

    def prepare(self, session_id: str, provider_id: str, events_path: Path,
                source_path: Path | None, root: Path,
                normalize_user: Callable[[dict], str | None],
                classify_user: Callable[[dict], str | None]) -> bool:
        with self._prepare_lock:
            with self._lock:
                previous = self._proofs.get(session_id)
                if previous and previous.provider_id == provider_id:
                    self._proofs.move_to_end(session_id)
                    return False
                self._preparing, self._cancelled = session_id, False
            try:
                proof = _prove(session_id, provider_id, events_path, source_path, root,
                               normalize_user, classify_user)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                proof = _Proof(provider_id)
            with self._lock:
                self._preparing = None
                if self._cancelled:
                    return bool(previous and previous.targets)
                if previous and previous.provider_id != provider_id:
                    # Proven immutable ledger positions stay proven when this
                    # chat rotates provider threads. Fresh credentials/source
                    # discovery must not resurrect old runtime-only bubbles.
                    remaining = max(0, MAX_TARGETS - len(previous.targets))
                    added = sorted(proof.targets - previous.targets)[:remaining]
                    proof = _Proof(provider_id, previous.targets.union(added))
                self._proofs[session_id] = proof
                self._proofs.move_to_end(session_id)
                while len(self._proofs) > MAX_SESSIONS:
                    self._proofs.popitem(last=False)
            return (previous.targets if previous else frozenset()) != proof.targets

    def signature(self, session_id: str) -> frozenset:
        with self._lock:
            proof = self._proofs.get(session_id)
            return proof.targets if proof else frozenset()

    def forget(self, session_id: str) -> None:
        with self._lock:
            self._proofs.pop(session_id, None)
            if self._preparing == session_id:
                self._cancelled = True

    def is_hidden(self, session_id: str, event: dict) -> bool:
        if event.get("session_id") not in (None, "", session_id):
            return False
        with self._lock:
            proof = self._proofs.get(session_id)
        if proof is None or not proof.targets:
            return False
        try:
            if (event.get("seq"), event.get("run_id")) not in proof.positions:
                return False
            return _target(event) in proof.targets
        except (TypeError, ValueError):
            return False


def codex_public_item_origin(event: dict, provider_id: str | None = None) -> dict | None:
    """Explicit public response identity only; never infer identity from text."""
    payload = event.get("payload")
    if event.get("type") != "response_item" or not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    item_id, turn_id = payload.get("id"), metadata.get("turn_id") if isinstance(metadata, dict) else None
    timestamp, role = event.get("timestamp"), payload.get("role")
    if role not in ("user", "assistant") or not all(isinstance(value, str) and 0 < len(value) <= 256 for value in (item_id, turn_id)):
        return None
    try:
        if not isinstance(timestamp, str) or len(timestamp) > 64 or datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset() is None:
            return None
    except ValueError:
        return None
    origin = {"provider": "codex", "kind": role, "event_id": item_id, "turn_id": turn_id, "timestamp": timestamp}
    if provider_id and _PROVIDER_ID.fullmatch(provider_id):
        origin["session_id"] = provider_id
    return origin


def _runtime_human_provenance(event: dict) -> bool:
    metadata = event.get("internal_chat_message_metadata_passthrough")
    kinds = metadata.get("content_item_kinds") if isinstance(metadata, dict) else None
    return event.get("provider_user_authored") is True or isinstance(kinds, list) and "user.text" in kinds or any(
        isinstance(event.get(field), str) and event[field].strip()
        for field in ("clientUserMessageId", "clientId", "client_user_message_id", "client_id")
    ) or any(isinstance(origin, dict) and origin.get("kind") in ("human", "user", "user_input", "user-input")
             for origin in (event.get("origin"), event.get("provider_origin")))


def _persisted_runtime_marker(event: dict, session_id: str) -> bool:
    origin = event.get("provider_origin")
    runtime_kind = event.get("provider_runtime_context")
    if (runtime_kind not in ("subagent_notification", "turn_aborted", "provider_notice")
        or event.get("metadata_only") is not True or event.get("backend") != "codex"
        or event.get("imported") is not True or not isinstance(event.get("run_id"), str)
        or not event["run_id"].startswith("import_") or event.get("session_id") != session_id
        or not isinstance(event.get("id"), str) or not 0 < len(event["id"].strip()) <= 256
        or type(event.get("seq")) is not int or event["seq"] <= 0
        or event.get("type") != "turn_started" or event.get("prompt") != ""
        or _runtime_human_provenance(event) or not isinstance(origin, dict)
        or origin.get("provider") != "codex" or origin.get("kind") != runtime_kind
        or not all(isinstance(origin.get(key), str) and 0 < len(origin[key].strip()) <= 256
                   for key in ("event_id", "session_id", "turn_id"))
        or not isinstance(origin.get("source_text_sha256"), str) or not _DIGEST.fullmatch(origin["source_text_sha256"])):
        return False
    timestamp = origin.get("timestamp")
    if (not isinstance(timestamp, str) or timestamp != event.get("ts") or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)", timestamp)):
        return False
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset() is not None
    except ValueError:
        return False


def _replay_target(event: dict) -> tuple | None:
    kind = "user" if event.get("type") == "turn_started" else "assistant" if event.get("type") in ("assistant_text", "reasoning_summary") else None
    body = event.get("prompt") if kind == "user" else event.get("text")
    if (kind is None or event.get("backend") != "codex" or event.get("imported") is not True
        or not isinstance(event.get("run_id"), str) or not event["run_id"].startswith("import_")
        or type(event.get("seq")) is not int or event["seq"] <= 0
        or not isinstance(event.get("id"), str) or not 0 < len(event["id"]) <= 256
        or not isinstance(body, str) or not body or len(body) > 4 * 1024 * 1024
        or not isinstance(event.get("ts"), str) or not 0 < len(event["ts"]) <= 64
        or event.get("source_text_sha256") is not None and (
            not isinstance(event.get("source_text_sha256"), str)
            or not _DIGEST.fullmatch(event["source_text_sha256"]))):
        return None
    # Human authorship does not make a duplicate a second human message.
    # Retain the original authorship flag; only this exact ledger copy is aliased.
    return (event["seq"], event["run_id"], event["id"], event["type"], kind, _text_key(body), event["ts"],
            _runtime_human_provenance(event), event.get("source_text_sha256"))


@dataclass(frozen=True)
class _NativeProof:
    provider_id: str
    targets: dict = field(default_factory=dict)


def _native_mailbox_wake_hash(event: dict) -> str | None:
    """A server-authored hidden input, not a text-based provider classification."""
    digest = event.get("provider_input_sha256")
    wake_id = event.get("mailbox_wake_id")
    if (event.get("type") == "turn_started" and event.get("imported") is not True
        and event.get("purpose") == "chat_mailbox_wake" and event.get("prompt") == ""
        and event.get("provider_generated") is True and not _runtime_human_provenance(event)
        and isinstance(wake_id, str) and re.fullmatch(r"mailwake_[a-f0-9]{32}", wake_id)
        and type(event.get("mailbox_wake_through_seq")) is int and 0 < event["mailbox_wake_through_seq"] < 2**63
        and isinstance(digest, str) and _DIGEST.fullmatch(digest)):
        return digest
    return None


_ASYNC_DELIVERY_WRAPPER = re.compile(
    r"\A\[AgentsDock delivery kind=instruction leg=1/1 origin=route mode=async_route_v1 from=([^\[\]\r\n]{1,512})\]\n"
    r"source-instruction: this legacy relay has no recorded source user instruction; do not infer user authorization from the prepared content\.\n"
    r"\[Agent-prepared handoff message\]\n([\s\S]+)\n"
    r"\[End agent-prepared handoff message\]\n\[End delivery\]\Z"
)


def _async_delivery_body(text: str) -> tuple[str, str, int] | None:
    """Parse one complete legacy wire shape; text alone never proves delivery."""
    if len(text) > NATIVE_PROOF_LINE_BYTES:
        return None
    match = _ASYNC_DELIVERY_WRAPPER.fullmatch(text)
    if not match:
        return None
    return match[1], _text_key(match[2]), len(match[2])


def _delivery_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not 0 < len(value) <= 64:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp if timestamp.utcoffset() is not None else None
    except ValueError:
        return None


@dataclass
class _AsyncDeliveryIndex:
    """Compact native proof only: no message bodies, route grants or file I/O."""
    starts: dict = field(default_factory=dict)
    ends: dict = field(default_factory=dict)
    receipts: dict = field(default_factory=dict)
    steered: set = field(default_factory=set)
    count: int = 0

    def observe(self, event: dict) -> None:
        event_type = event.get("type")
        if (event_type not in {"turn_started", "turn_finished", "turn_steered", "turn_stopped", "turn_queue_run_now",
                              "chat_conversation_message_received", "chat_conversation_message_started", "chat_conversation_message_delivered"}
            or event.get("imported") is True or event.get("forked") is True
            or event.get("backend") not in (None, "codex")):
            return
        run = (event.get("interrupted_run_id") or event.get("run_id")) if event_type == "turn_queue_run_now" else event.get("run_id")
        if (event_type in {"turn_steered", "turn_stopped"} or event.get("native_steer") is True):
            if isinstance(run, str) and 0 < len(run) <= 256 and not run.startswith("import_"):
                self.count += run not in self.steered
                self.steered.add(run)
            if self.count > MAX_KEYS:
                raise _Unproven()
            return
        if event_type == "turn_queue_run_now" or (
            event_type in {"turn_started", "turn_finished"} and event.get("purpose") != "cross_chat_handoff_delivery"
        ):
            return
        envelope = event.get("cross_chat_envelope_id")
        fields = ("id", "seq", "ts", "backend", "purpose", "run_id", "transport", "exit_code", "stopped", "is_error",
            "provider_thread_id", "provider_turn_id", "cross_chat_envelope_id", "handoff_id", "message_id",
            "source_session_id", "target_session_id", "source_title", "target_run_id", "conversation_mode",
            "kind", "action", "handoff_action", "handoff_body_chars", "handoff_body_sha256",
            "message_revision", "message_edited_by_user", "exchange_id", "cross_chat_exchange_id")
        record = {key: event.get(key) for key in fields
                  if event.get(key) is None or type(event.get(key)) in (int, bool)
                  or isinstance(event.get(key), str) and len(event[key]) <= 512}
        record["malformed"] = len(record) != len(fields)
        record["type"] = event_type
        if isinstance(run, str) and 0 < len(run) <= 256 and not run.startswith("import_"):
            if event_type == "turn_started":
                body = event.get("prompt")
                if isinstance(body, str) and 0 < len(body) <= NATIVE_PROOF_LINE_BYTES and not _runtime_human_provenance(event):
                    record.update(body_key=_text_key(body), body_chars=len(body))
                self.starts.setdefault(run, []).append(record)
                self.count += 1
            elif event_type == "turn_finished":
                self.ends.setdefault(run, []).append(record)
                self.count += 1
        if (event_type in {"chat_conversation_message_received", "chat_conversation_message_started", "chat_conversation_message_delivered"}
            and isinstance(envelope, str) and 0 < len(envelope) <= 256):
            self.receipts.setdefault(envelope, []).append(record)
            self.count += 1
        if self.count > MAX_KEYS:
            raise _Unproven()

    def match(self, session_id: str, provider_id: str, run: str, origin: dict,
              body: tuple[str, str, int] | None, source_ids: set) -> dict | None:
        starts, ends = self.starts.get(run, []), self.ends.get(run, [])
        if body is None or len(starts) != 1 or len(ends) != 1 or run in self.steered:
            return None
        start, end = starts[0], ends[0]
        envelope = start.get("cross_chat_envelope_id")
        if (start.get("malformed") or end.get("malformed")
            or not isinstance(envelope, str) or not envelope or start.get("backend") != "codex"
            or start.get("conversation_mode") != "async_route_v1" or start.get("target_session_id") != session_id
            or not isinstance(start.get("source_session_id"), str) or not start["source_session_id"]
            or start["source_session_id"] == session_id
            or start.get("exchange_id") or start.get("cross_chat_exchange_id")
            or start.get("body_key") != body[1] or start.get("body_chars") != body[2]
            or start.get("message_edited_by_user") not in (None, False) or start.get("message_revision") not in (None, 0)
            or end.get("backend") != "codex" or end.get("transport") != "app-server"
            or end.get("provider_thread_id") != provider_id or end.get("provider_turn_id") != origin.get("turn_id")
            or end.get("cross_chat_envelope_id") != envelope or type(end.get("exit_code")) is not int or end["exit_code"] != 0
            or end.get("stopped") not in (None, False) or end.get("is_error") not in (None, False)
            or any(end.get(key) != start.get(key) for key in (
                "source_session_id", "target_session_id", "conversation_mode", "handoff_id", "message_id",
                "message_revision", "message_edited_by_user", "exchange_id", "cross_chat_exchange_id"))
            or source_ids != {origin.get("event_id")} or not origin.get("event_id")
            or type(start.get("seq")) is not int or type(end.get("seq")) is not int or start["seq"] >= end["seq"]
            or not isinstance(start.get("id"), str) or not start["id"]):
            return None
        times = [_delivery_timestamp(value) for value in (start.get("ts"), origin.get("timestamp"), end.get("ts"))]
        if any(value is None for value in times) or not times[0] <= times[1] <= times[2]:
            return None
        receipts = self.receipts.get(envelope, [])
        stages = set()
        for receipt in receipts:
            if (receipt.get("malformed") or receipt.get("conversation_mode") != "async_route_v1" or receipt.get("kind") != "instruction"
                or receipt.get("action") != "instruction" or receipt.get("handoff_action") != "instruction"
                or receipt.get("handoff_id") != envelope or receipt.get("message_id") != envelope
                or receipt.get("target_session_id") != session_id or receipt.get("source_session_id") != start.get("source_session_id")
                or receipt.get("source_title") != body[0] or receipt.get("handoff_body_sha256") != body[1]
                or type(receipt.get("handoff_body_chars")) is not int or receipt["handoff_body_chars"] != body[2]
                or receipt.get("message_edited_by_user") not in (None, False) or receipt.get("message_revision") not in (None, 0)
                or receipt.get("exchange_id") or receipt.get("cross_chat_exchange_id")
                or type(receipt.get("seq")) is not int):
                return None
            stage = receipt["type"]
            if stage == "chat_conversation_message_received":
                if receipt["seq"] >= start["seq"] or receipt.get("target_run_id") not in (None, run):
                    return None
            elif receipt.get("target_run_id") != run:
                return None
            elif stage == "chat_conversation_message_started" and not start["seq"] < receipt["seq"] < end["seq"]:
                return None
            elif stage == "chat_conversation_message_delivered" and receipt["seq"] <= end["seq"]:
                return None
            stages.add(stage)
        if stages != {"chat_conversation_message_received", "chat_conversation_message_started", "chat_conversation_message_delivered"}:
            return None
        return _native_event_identity(start)


def _prove_native_replays(session_id: str, provider_id: str, events: Path, source: Path | None,
                          root: Path, parse_item: Callable[[dict], dict | None],
                          budget: _NativeReadBudget) -> _NativeProof:
    if not _PROVIDER_ID.fullmatch(provider_id):
        raise _Unproven()
    stamp = _native_stamp(events)
    batches, terminals, candidates, native, owners = {}, {}, [], {}, {}
    deliveries = _AsyncDeliveryIndex()
    previous_seq, retained = 0, 0
    for event, _offset, _line in _native_records(events, stamp, budget):
        seq = event.get("seq")
        if type(seq) is not int or seq <= previous_seq:
            raise _Unproven()
        previous_seq = seq
        if event.get("session_id") not in (None, "", session_id):
            continue
        deliveries.observe(event)
        if len(candidates) + len(batches) + len(terminals) + retained + deliveries.count > MAX_KEYS:
            raise _Unproven()
        run = event.get("run_id")
        if not isinstance(run, str) or not 0 < len(run) <= 256:
            continue
        if run.startswith("import_"):
            if event.get("type") == "history_imported" and event.get("backend") == "codex":
                if run in batches:
                    raise _Unproven()
                batches[run] = _native_import_batch(event)
            elif event.get("type") == "turn_finished" and event.get("imported") is True and event.get("backend") == "codex":
                ends = terminals.setdefault(run, [])
                if len(ends) < 2:
                    ends.append(seq)
            else:
                target = _replay_target(event)
                if target:
                    candidates.append(target)
        elif event.get("imported") is not True and event.get("backend") in (None, "codex"):
            if event.get("type") == "turn_finished" and event.get("backend") == "codex" and event.get("transport") == "app-server":
                thread, turn = event.get("provider_thread_id"), event.get("provider_turn_id")
                if isinstance(thread, str) and _PROVIDER_ID.fullmatch(thread) and isinstance(turn, str) and 0 < len(turn) <= 256:
                    owned = owners.setdefault((thread, turn), set())
                    retained += run not in owned
                    owned.add(run)
            kind = "user" if event.get("type") == "turn_started" else "assistant" if event.get("type") in ("assistant_text", "reasoning_summary", "turn_finished") else None
            body = event.get("prompt") if kind == "user" else event.get("result_text") if event.get("type") == "turn_finished" else event.get("text")
            if kind and isinstance(body, str) and body and len(body) <= 4 * 1024 * 1024:
                native.setdefault((run, kind, _text_key(body)), []).append(_native_event_identity(event))
                retained += 1
            elif kind == "user" and (wake_hash := _native_mailbox_wake_hash(event)):
                native.setdefault((run, kind, wake_hash), []).append(_native_event_identity(event))
                retained += 1
        if len(candidates) + len(batches) + len(terminals) + retained + deliveries.count > MAX_KEYS:
            raise _Unproven()
    if not candidates:
        return _NativeProof(provider_id)
    groups = {}
    for run, batch in batches.items():
        budget.check()
        checkpoint = batch.get("_history_sync_checkpoint")
        cursor = checkpoint.get("cursor") if isinstance(checkpoint, dict) else None
        ends = terminals.get(run, [])
        if (not isinstance(cursor, dict) or checkpoint.get("version") != 1 or cursor.get("version") != 1
            or cursor.get("backend") != "codex" or cursor.get("provider_session_id") != batch.get("provider_session_id")
            or cursor.get("source_path") != batch.get("source_path") or len(ends) != 1
            or type(ends[0]) is not int or type(batch.get("seq")) is not int or ends[0] <= batch["seq"]):
            continue
        key = (batch.get("provider_session_id"), batch.get("source_path"))
        if not all(isinstance(value, str) for value in key) or not _PROVIDER_ID.fullmatch(key[0]):
            continue
        groups.setdefault(key, {})[run] = (batch["seq"], ends[0], checkpoint)
    current = (provider_id, str(source)) if source else None
    selected = ([current] if current in groups else []) + sorted(
        (key for key in groups if key != current), key=lambda key: max(row[0] for row in groups[key].values()), reverse=True,
    )[:MAX_PRIOR_SOURCE_PATHS]
    proofs = {}
    for thread, path in selected:
        proofs.update(_prove_native_source(thread, Path(path), root, groups[(thread, path)], candidates, native, owners, parse_item, budget,
                                          session_id=session_id, deliveries=deliveries))
    budget.check()
    return _NativeProof(provider_id, proofs)


def _prove_native_source(thread: str, source: Path, root: Path, batches: dict, candidates: list,
                         native: dict, owners: dict, parse_item: Callable, budget: _NativeReadBudget,
                         *, session_id: str, deliveries: _AsyncDeliveryIndex,
                         require_verified_checkpoint: bool = False) -> dict:
    if (not source.is_absolute() or source.is_symlink() or source.suffix != ".jsonl"
        or not (source.stem == thread or source.stem.endswith("-" + thread))):
        raise _Unproven()
    source = source.resolve(strict=True)
    source.relative_to(root.resolve(strict=True))
    stamp = _native_stamp(source)
    wanted, eligible = {}, {}
    for run, (first, last, checkpoint) in batches.items():
        cursor = checkpoint["cursor"]
        start, end = checkpoint.get("previous_source_offset"), cursor.get("source_offset")
        previous, expected = checkpoint.get("previous_source_digest"), cursor.get("source_digest")
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= stamp[2]
            or (cursor.get("source_dev"), cursor.get("source_ino")) != stamp[:2]
            or not isinstance(expected, str) or not _DIGEST.fullmatch(expected)
            or type(checkpoint.get("previous_present")) is not bool
            or start == 0 and previous != ""
            or start > 0 and (checkpoint.get("previous_present") is not True or not isinstance(previous, str) or not _DIGEST.fullmatch(previous))):
            if require_verified_checkpoint:
                raise _Unproven()
            continue
        wanted.setdefault(end, set()).add(expected)
        if start:
            wanted.setdefault(start, set()).add(previous)
        eligible[run] = (first, last, start, end, expected, previous)
    if not eligible:
        if require_verified_checkpoint:
            raise _Unproven()
        return {}
    # Only source messages capable of proving requested imported rows need to
    # occupy memory. Tools/private reasoning still contribute to exact digests.
    candidate_keys = {(target[4], target[5], target[8]) for target in candidates
                      if target[1] in eligible}
    digest, verified, canonical, occurrences = hashlib.sha256(), set(), {}, {}
    assistant_native_keys = {}
    compactions, candidate_assistants = {}, set()
    compaction_tracker = CodexCompactionSummaryTracker(thread)
    delivery_bodies, delivery_source_ids = {}, {}
    allowed_header_owners, header_parents = {thread}, {}
    context_turn = None
    retained = 0
    for record, offset, line in _native_records(source, stamp, budget, end=max(wanted)):
        payload = record.get("payload")
        if offset == len(line) or record.get("type") == "session_meta":
            if record.get("type") != "session_meta" or not isinstance(payload, dict):
                raise _Unproven()
            owner, parent = payload.get("id"), payload.get("forked_from_id")
            if (not isinstance(owner, str) or owner not in allowed_header_owners
                or offset == len(line) and owner != thread
                or parent is not None and (not isinstance(parent, str) or not _PROVIDER_ID.fullmatch(parent))
                or owner in header_parents and header_parents[owner] != parent
                or owner not in header_parents and parent in header_parents):
                raise _Unproven()
            # Forked rollouts retain an ancestral session_meta immediately
            # after their own header. Only the declared parent chain is valid;
            # an unrelated embedded owner cannot relabel this source file.
            header_parents[owner] = parent
            if len(header_parents) > MAX_FORK_META_HEADERS:
                raise _Unproven()
            if parent is not None:
                allowed_header_owners.add(parent)
        digest.update(line)
        for expected in wanted.get(offset, ()):
            if hmac.compare_digest(digest.hexdigest(), expected):
                verified.add((offset, expected))
        compaction = compaction_tracker.observe(record)
        if compaction is not None:
            identity = (compaction["event_id"], compaction["turn_id"], compaction["timestamp"])
            if identity in candidate_assistants:
                compactions[identity] = (offset, compaction)
                retained += 1
                if retained > MAX_KEYS:
                    raise _Unproven()
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context" or record.get("type") == "event_msg" and payload.get("type") == "task_started":
            context_turn = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
        item = parse_item(record)
        if not item or item.get("kind") not in ("user", "assistant") or not isinstance(item.get("text"), str):
            continue
        origin = codex_public_item_origin(record, thread)
        runtime_kind = item.get("provider_runtime_context")
        if (origin and runtime_kind in ("subagent_notification", "turn_aborted", "provider_notice")
                and item.get("provider_user_authored") is not True
                and (item.get("provider_origin") or {}).get("kind") == runtime_kind):
            origin = {**origin, "kind": runtime_kind}
        turn = origin["turn_id"] if origin else context_turn
        timestamp = record.get("timestamp")
        if (not isinstance(turn, str) or not 0 < len(turn) <= 256
                or not isinstance(timestamp, str) or not 0 < len(timestamp) <= 64):
            continue
        key = (turn, item["kind"], _text_key(item["text"]), item.get("source_text_sha256"))
        if origin and item["kind"] == "user" and any(run in deliveries.starts for run in owners.get((thread, turn), ())):
            ids = delivery_source_ids.setdefault(turn, set())
            if len(ids) < 2:
                retained += origin["event_id"] not in ids
                ids.add(origin["event_id"])
            if retained > MAX_KEYS:
                raise _Unproven()
        if (item["kind"], key[2], key[3]) not in candidate_keys:
            continue
        if item["kind"] == "user" and (body := _async_delivery_body(item["text"])):
            delivery_bodies[key] = body
        if item["kind"] == "assistant":
            cleaned = _native_assistant_text(item["text"])
            if cleaned:
                assistant_native_keys[key] = _text_key(cleaned)
            if origin:
                candidate_assistants.add((origin["event_id"], origin["turn_id"], origin["timestamp"]))
        if origin:
            ids = canonical.setdefault(key, {})
            retained += origin["event_id"] not in ids
            ids[origin["event_id"]] = origin
        occurrences.setdefault((item["kind"], key[2], timestamp, key[3]), []).append((offset, key))
        retained += 1
        if retained > MAX_KEYS:
            raise _Unproven()
    if require_verified_checkpoint and any((offset, expected) not in verified
        for offset, expectations in wanted.items() for expected in expectations):
        raise _Unproven()
    proofs = {}
    for target in candidates:
        budget.check()
        seq, run, _id, _type, kind, body_key, timestamp, _human, source_hash = target
        batch = eligible.get(run)
        if not batch:
            continue
        first, last, start, end, expected, previous = batch
        if not (first < seq < last and (end, expected) in verified and (start == 0 or (start, previous) in verified)):
            continue
        matches = set()
        for offset, key in occurrences.get((kind, body_key, timestamp, source_hash), []):
            budget.check()
            if start < offset <= end:
                matches.add(key)
        if len(matches) != 1:
            continue
        key = next(iter(matches))
        source_ids, owned_runs = canonical.get(key, {}), owners.get((thread, key[0]), set())
        # Compaction owns a particular source item and timestamp. Another
        # genuine answer with the same body in this turn is a distinct item.
        timestamp_origins = [origin for origin in source_ids.values() if origin["timestamp"] == timestamp]
        compaction_matches = [compactions[identity] for origin in timestamp_origins
            if len(timestamp_origins) == 1
            and (identity := (origin["event_id"], origin["turn_id"], origin["timestamp"])) in compactions
            and compactions[identity][0] <= end]
        if kind == "assistant" and source_hash is None and len(compaction_matches) == 1:
            proofs[target] = compaction_matches[0][1]
            if len(proofs) > MAX_TARGETS:
                raise _Unproven()
            continue
        if len(source_ids) != 1 or source_hash is not None:
            # Truncated source hashes need a separately retained native full-body
            # hash. Until one exists, the bounded preview cannot prove equality.
            continue
        source_origin = next(iter(source_ids.values()))
        if source_origin.get("kind") in ("subagent_notification", "turn_aborted", "provider_notice"):
            if kind == "user" and not _human:
                proofs[target] = {**source_origin, "source_text_sha256": body_key}
                if len(proofs) > MAX_TARGETS:
                    raise _Unproven()
            continue
        if len(owned_runs) != 1:
            continue
        native_run = next(iter(owned_runs))
        native_matches = native.get((native_run, kind, body_key), [])
        if not native_matches and kind == "user":
            delivery = deliveries.match(session_id, thread, native_run, source_origin,
                                        delivery_bodies.get(key), delivery_source_ids.get(key[0], set()))
            if delivery is not None:
                native_matches = [delivery]
        if not native_matches and kind == "assistant":
            # Native delivery removes line-leading decorations. Only the same
            # public provider item may use that normalization; a similar body
            # in another item/turn is not replay evidence.
            native_key = assistant_native_keys.get(key)
            if native_key and native_key != body_key:
                native_matches = [event for event in native.get((native_run, kind, native_key), [])
                                  if _public_assistant_item_id(event) == source_origin["event_id"]]
        native_matches = [event for event in native_matches if type(event.get("seq")) is int and event["seq"] < first and isinstance(event.get("id"), str)]
        if not native_matches:
            continue
        representative = min(native_matches, key=lambda event: event["seq"])
        proofs[target] = {**next(iter(source_ids.values())), "native_event_id": representative["id"], "source_text_sha256": body_key}
        if len(proofs) > MAX_TARGETS:
            raise _Unproven()
    budget.check()
    return proofs


class CodexNativeHistoryRepairCache(CodexGoalHistoryRepairCache):
    """An explicit history boundary prepares proof; per-event projection does no IO."""
    def prepare(self, session_id: str, provider_id: str, events_path: Path, source_path: Path | None,
                root: Path, parse_item: Callable[[dict], dict | None], *,
                cancelled: Callable[[], bool] | None = None, deadline: float | None = None) -> bool:
        with self._prepare_lock:
            with self._lock:
                previous = self._proofs.get(session_id)
                if previous and previous.provider_id == provider_id:
                    return False
                self._preparing, self._cancelled = session_id, False
            budget = _NativeReadBudget(lambda: self._cancelled or bool(cancelled and cancelled()), deadline)
            try:
                proof = _prove_native_replays(
                    session_id, provider_id, events_path, source_path, root, parse_item,
                    budget,
                )
                budget.check()
            except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
                with self._lock:
                    self._preparing = None
                # Unavailable proof is neither a negative answer nor a prepared
                # cache entry. The explicit worker decides when to retry it.
                raise CodexNativeHistoryProofUnavailable("Codex native history proof is incomplete") from exc
            with self._lock:
                self._preparing = None
                budget.check()
                self._proofs[session_id] = proof
                self._proofs.move_to_end(session_id)
                while len(self._proofs) > MAX_SESSIONS:
                    self._proofs.popitem(last=False)
            return bool(proof.targets)

    def signature(self, session_id: str) -> frozenset:
        with self._lock:
            proof = self._proofs.get(session_id)
            return frozenset(proof.targets) if proof else frozenset()

    def project_event(self, session_id: str, event: dict) -> dict | None:
        if event.get("session_id") not in (None, "", session_id):
            return None
        if _persisted_runtime_marker(event, session_id):
            return {**event, "_agentsdock_imported_prompt_hidden": True}
        if (event.get("provider_history_repair") == "source_proven_native_replay"
            and event.get("metadata_only") is True and event.get("backend") == "codex"
            and event.get("imported") is True and str(event.get("run_id") or "").startswith("import_")
            and event.get("type") == "turn_started" and event.get("prompt") == ""
            and isinstance(event.get("provider_origin"), dict)
            and event["provider_origin"].get("provider") == "codex"
            and event["provider_origin"].get("native_event_id")):
            return {**event, "_agentsdock_imported_prompt_hidden": True}
        with self._lock:
            proof = self._proofs.get(session_id)
        if not proof:
            return None
        target = _replay_target(event)
        origin = proof.targets.get(target) if target else None
        if origin is None:
            return None
        if origin.get("kind") == "compaction_summary":
            return {**event, "text": "", "metadata_only": True,
                    "provider_history_repair": "source_proven_compaction", "provider_origin": origin}
        if origin.get("kind") in ("subagent_notification", "turn_aborted", "provider_notice"):
            return {**event, "prompt": "", "metadata_only": True,
                    "provider_runtime_context": origin["kind"], "provider_origin": origin,
                    "_agentsdock_imported_prompt_hidden": True}
        return {**event, "prompt" if event["type"] == "turn_started" else "text": "",
                "metadata_only": True, "provider_history_repair": "source_proven_native_replay", "provider_origin": origin,
                **({"_agentsdock_imported_prompt_hidden": True} if event["type"] == "turn_started" else {})}


def _prove_pending_async_deliveries(session_id: str, provider_id: str, items: list[dict],
                                    source: Path, root: Path, checkpoint: dict, parse_item: Callable,
                                    previous_seq: int, owners: dict, deliveries: _AsyncDeliveryIndex,
                                    budget: _NativeReadBudget) -> dict:
    """Reuse pinned-prefix proof before broadcast; the synthetic batch is never stored."""
    cursor = checkpoint.get("cursor")
    if (checkpoint.get("version") != 1 or not isinstance(cursor, dict) or cursor.get("version") != 1
        or cursor.get("backend") != "codex" or cursor.get("provider_session_id") != provider_id
        or cursor.get("source_path") != str(source)):
        raise _Unproven()
    run = "import_pending_native_delivery_proof"
    candidates, indexes = [], {}
    for index, item in enumerate(items):
        budget.check()
        origin = item.get("provider_origin")
        if (item.get("kind") != "user" or not isinstance(item.get("text"), str)
            or not isinstance(origin, dict) or origin.get("provider") != "codex" or origin.get("kind") != "user"
            or origin.get("session_id", provider_id) != provider_id or item.get("source_text_sha256") is not None
            or not _async_delivery_body(item["text"])):
            continue
        candidate = _replay_target({"seq": previous_seq + index + 2, "id": f"pending:{index}", "run_id": run,
            "type": "turn_started", "backend": "codex", "imported": True, "prompt": item["text"],
            "ts": origin.get("timestamp"), "provider_user_authored": _runtime_human_provenance(item)})
        if candidate is not None:
            candidates.append(candidate)
            indexes[candidate] = index
        if len(candidates) > MAX_TARGETS:
            raise _Unproven()
    if not candidates:
        return {}
    proof = _prove_native_source(provider_id, source, root,
        {run: (previous_seq + 1, previous_seq + len(items) + 2, checkpoint)}, candidates, {},
        {(provider_id, turn): runs for turn, runs in owners.items()}, parse_item, budget,
        session_id=session_id, deliveries=deliveries, require_verified_checkpoint=True)
    result = {}
    for candidate, origin in proof.items():
        index = indexes[candidate]
        item_origin = items[index]["provider_origin"]
        if all(origin.get(key) == item_origin.get(key) for key in ("provider", "kind", "event_id", "turn_id", "timestamp")):
            result[index] = origin
    return result


def filter_native_codex_history_items(session_id: str, provider_id: str, events: Path, items: list[dict], *,
                                      cancelled: Callable[[], bool] | None = None,
                                      deadline: float | None = None,
                                      source_path: Path | None = None, root: Path | None = None,
                                      sync_checkpoint: dict | None = None,
                                      parse_item: Callable[[dict], dict | None] | None = None) -> list[dict]:
    """An existing verified import boundary can omit exact completed native copies.

    Exact native bodies need only the ledger. Legacy async wrappers additionally
    require the caller's source/checkpoint/parser and one verified source-prefix
    scan: a delta range alone cannot disambiguate a later same-turn human quote.
    Missing source context leaves those wrappers visible for later read repair.
    Incomplete proof raises; the caller must defer import and its cursor commit.
    """
    if not items:
        return []
    try:
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise _Unproven()
        budget = _NativeReadBudget(cancelled, deadline)
        stamp = _native_stamp(events)
        owners, native, assistant_items, wake_keys, native_count = {}, {}, {}, set(), 0
        deliveries = _AsyncDeliveryIndex()
        prove_deliveries = (source_path is not None and root is not None
            and isinstance(sync_checkpoint, dict) and callable(parse_item)
            and any(item.get("kind") == "user" and isinstance(item.get("text"), str)
                    and _async_delivery_body(item["text"]) for item in items))
        previous_seq, owner_count = 0, 0
        for event, _offset, _line in _native_records(events, stamp, budget):
            seq = event.get("seq")
            if type(seq) is not int or seq <= previous_seq:
                raise _Unproven()
            previous_seq = seq
            run = event.get("run_id")
            if prove_deliveries and event.get("session_id") in (None, "", session_id):
                deliveries.observe(event)
                if native_count + owner_count + len(assistant_items) + deliveries.count > MAX_KEYS:
                    raise _Unproven()
            if (event.get("session_id") not in (None, "", session_id) or event.get("imported") is True
                or event.get("backend") not in (None, "codex") or not isinstance(run, str)
                or not 0 < len(run) <= 256 or run.startswith("import_")):
                continue
            if (event.get("type") == "turn_finished" and event.get("backend") == "codex"
                and event.get("transport") == "app-server" and event.get("provider_thread_id") == provider_id
                and isinstance(event.get("provider_turn_id"), str) and 0 < len(event["provider_turn_id"]) <= 256):
                owned = owners.setdefault(event["provider_turn_id"], set())
                owner_count += run not in owned
                owned.add(run)
            kind = "user" if event.get("type") == "turn_started" else "assistant" if event.get("type") in ("assistant_text", "reasoning_summary", "turn_finished") else None
            body = event.get("prompt") if kind == "user" else event.get("result_text") if event.get("type") == "turn_finished" else event.get("text")
            wake_hash = _native_mailbox_wake_hash(event) if kind == "user" else None
            if kind and (isinstance(body, str) and body or wake_hash):
                keys = native.setdefault(run, {})
                key = (kind, wake_hash or _text_key(body))
                native_count += key not in keys
                if isinstance(event.get("id"), str) and 0 < len(event["id"]) <= 256:
                    keys.setdefault(key, event["id"])
                    if wake_hash:
                        wake_keys.add((run, wake_hash))
                    item_id = _public_assistant_item_id(event)
                    if item_id is not None:
                        assistant_items[(run, item_id, key[1])] = event["id"]
            if native_count + owner_count + len(assistant_items) + deliveries.count > MAX_KEYS:
                raise _Unproven()
        delivery_proofs = _prove_pending_async_deliveries(
            session_id, provider_id, items, source_path, root, sync_checkpoint, parse_item,
            previous_seq, owners, deliveries, budget,
        ) if prove_deliveries and deliveries.starts else {}
        # A hidden wake has no public prompt body. Require a single exact source
        # item in this verified import range, in addition to native turn ownership.
        wake_source_ids = {}
        for item in items if wake_keys else ():
            budget.check()
            origin = item.get("provider_origin")
            if (item.get("kind") == "user" and isinstance(item.get("text"), str)
                and isinstance(origin, dict)):
                key = (origin.get("turn_id"), _text_key(item["text"]))
                wake_source_ids.setdefault(key, set()).add(origin.get("event_id"))
        result = []
        for index, item in enumerate(items):
            budget.check()
            origin = item.get("provider_origin")
            runs = owners.get(origin.get("turn_id"), set()) if isinstance(origin, dict) else set()
            owned = (isinstance(origin, dict) and origin.get("provider") == "codex"
                     and origin.get("kind") == item.get("kind") and isinstance(origin.get("event_id"), str)
                     and origin.get("session_id", provider_id) == provider_id and len(runs) == 1
                     and item.get("source_text_sha256") is None and isinstance(item.get("text"), str))
            known = owned and (item["kind"], _text_key(item["text"])) in native.get(next(iter(runs)), {})
            delivery = delivery_proofs.get(index) if owned else None
            known = known or delivery is not None
            if known and (next(iter(runs)), _text_key(item["text"])) in wake_keys:
                known = (bool(origin.get("event_id")) and bool(origin.get("timestamp"))
                         and len(wake_source_ids.get((origin.get("turn_id"), _text_key(item["text"])), set())) == 1)
            if owned and not known and item["kind"] == "assistant":
                cleaned = _native_assistant_text(item["text"])
                known = bool(cleaned and (next(iter(runs)), origin["event_id"], _text_key(cleaned)) in assistant_items)
            if not known:
                result.append(item)
            elif item["kind"] == "user":
                # Retain a silent exact input boundary: a later unmatched
                # answer must not become the answer to an earlier real input.
                key = (item["kind"], _text_key(item["text"]))
                result.append({**item, "text": "", "metadata_only": True,
                    "provider_history_repair": "source_proven_native_replay",
                    "provider_origin": {**origin, "session_id": provider_id,
                        "native_event_id": delivery["native_event_id"] if delivery else native[next(iter(runs))][key],
                        "source_text_sha256": key[1]}})
        budget.check()
        return result
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        raise CodexNativeHistoryProofUnavailable("Codex native import ownership proof is incomplete") from exc
