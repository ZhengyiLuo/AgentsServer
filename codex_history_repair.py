"""Read-only, checkpoint-proven repair of legacy Codex goal-context imports.

Preparation is explicit, bounded and once per requested chat/provider. Event
projection is memory-only. Missing, changing or ambiguous evidence stays visible.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
import hashlib
import hmac
from pathlib import Path
import re
import threading
from typing import Callable

from claude_history_repair import (
    MAX_EVENTS_BYTES, MAX_KEYS, MAX_SESSIONS, MAX_TARGETS,
    _DIGEST, _Unproven, _records, _stamp, _text_key,
)


MAX_PRIOR_SOURCE_PATHS = 2
MAX_AGGREGATE_SOURCE_BYTES = 96 * 1024 * 1024
MAX_AGGREGATE_SOURCE_RECORDS = 100_000
_PROVIDER_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


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
