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
from pathlib import Path
import re
import threading
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
    if (runtime_kind not in ("subagent_notification", "turn_aborted")
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
        or not isinstance(event.get("id"), str) or not event["id"]
        or not isinstance(body, str) or not body or len(body) > 4 * 1024 * 1024
        or not isinstance(event.get("ts"), str)
        or event.get("source_text_sha256") is not None and not isinstance(event.get("source_text_sha256"), str)):
        return None
    # Human authorship does not make a duplicate a second human message.
    # Retain the original authorship flag; only this exact ledger copy is aliased.
    return (event["seq"], event["run_id"], event["id"], event["type"], kind, _text_key(body), event["ts"],
            _runtime_human_provenance(event), event.get("source_text_sha256"))


@dataclass(frozen=True)
class _NativeProof:
    provider_id: str
    targets: dict = field(default_factory=dict)


def _prove_native_replays(session_id: str, provider_id: str, events: Path, source: Path | None,
                          root: Path, parse_item: Callable[[dict], dict | None]) -> _NativeProof:
    if not _PROVIDER_ID.fullmatch(provider_id):
        raise _Unproven()
    stamp = _stamp(events)
    if stamp[2] > MAX_EVENTS_BYTES:
        raise _Unproven()
    batches, terminals, candidates, native, owners = {}, {}, [], {}, {}
    for event, _offset, _line in _records(events, stamp):
        if event.get("session_id") not in (None, "", session_id):
            continue
        run = event.get("run_id")
        if not isinstance(run, str) or not run:
            continue
        if run.startswith("import_"):
            if event.get("type") == "history_imported" and event.get("backend") == "codex":
                if run in batches:
                    raise _Unproven()
                batches[run] = event
            elif event.get("type") == "turn_finished" and event.get("imported") is True and event.get("backend") == "codex":
                terminals.setdefault(run, []).append(event.get("seq"))
            else:
                target = _replay_target(event)
                if target:
                    candidates.append(target)
        elif event.get("imported") is not True and event.get("backend") in (None, "codex"):
            if event.get("type") == "turn_finished" and event.get("backend") == "codex" and event.get("transport") == "app-server":
                thread, turn = event.get("provider_thread_id"), event.get("provider_turn_id")
                if isinstance(thread, str) and _PROVIDER_ID.fullmatch(thread) and isinstance(turn, str) and 0 < len(turn) <= 256:
                    owners.setdefault((thread, turn), set()).add(run)
            kind = "user" if event.get("type") == "turn_started" else "assistant" if event.get("type") in ("assistant_text", "reasoning_summary", "turn_finished") else None
            body = event.get("prompt") if kind == "user" else event.get("result_text") if event.get("type") == "turn_finished" else event.get("text")
            if kind and isinstance(body, str) and body and len(body) <= 4 * 1024 * 1024:
                native.setdefault((run, kind, _text_key(body)), []).append(event)
        if len(candidates) + len(batches) + len(native) + len(owners) > MAX_KEYS:
            raise _Unproven()
    if not candidates:
        return _NativeProof(provider_id)
    groups = {}
    for run, batch in batches.items():
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
    budget = _SourceBudget(MAX_AGGREGATE_SOURCE_BYTES, MAX_AGGREGATE_SOURCE_RECORDS)
    proofs = {}
    for thread, path in selected:
        try:
            proofs.update(_prove_native_source(thread, Path(path), root, groups[(thread, path)], candidates, native, owners, parse_item, budget))
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            continue
    return _NativeProof(provider_id, proofs)


def _prove_native_source(thread: str, source: Path, root: Path, batches: dict, candidates: list,
                         native: dict, owners: dict, parse_item: Callable, budget: _SourceBudget) -> dict:
    if (not source.is_absolute() or source.is_symlink() or source.suffix != ".jsonl"
        or not (source.stem == thread or source.stem.endswith("-" + thread))):
        raise _Unproven()
    source = source.resolve(strict=True)
    source.relative_to(root.resolve(strict=True))
    stamp = _stamp(source)
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
            continue
        wanted.setdefault(end, set()).add(expected)
        if start:
            wanted.setdefault(start, set()).add(previous)
        eligible[run] = (first, last, start, end, expected, previous)
    if not eligible:
        return {}
    budget.reserve(stamp[2])
    digest, verified, canonical, occurrences = hashlib.sha256(), set(), {}, {}
    allowed_header_owners, header_parents = {thread}, {}
    context_turn = None
    for record, offset, line in _records(source, stamp):
        budget.consume_record()
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
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context" or record.get("type") == "event_msg" and payload.get("type") == "task_started":
            context_turn = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else None
        item = parse_item(record)
        if not item or item.get("kind") not in ("user", "assistant") or not isinstance(item.get("text"), str):
            continue
        origin = codex_public_item_origin(record, thread)
        runtime_kind = item.get("provider_runtime_context")
        if (origin and runtime_kind in ("subagent_notification", "turn_aborted")
                and item.get("provider_user_authored") is not True
                and (item.get("provider_origin") or {}).get("kind") == runtime_kind):
            origin = {**origin, "kind": runtime_kind}
        turn = origin["turn_id"] if origin else context_turn
        timestamp = record.get("timestamp")
        if not isinstance(turn, str) or not isinstance(timestamp, str):
            continue
        key = (turn, item["kind"], _text_key(item["text"]), item.get("source_text_sha256"))
        if origin:
            canonical.setdefault(key, {})[origin["event_id"]] = origin
        occurrences.setdefault((item["kind"], key[2], timestamp, key[3]), []).append((offset, key))
        if len(canonical) + len(occurrences) > MAX_KEYS:
            raise _Unproven()
    proofs = {}
    for target in candidates:
        seq, run, _id, _type, kind, body_key, timestamp, _human, source_hash = target
        batch = eligible.get(run)
        if not batch:
            continue
        first, last, start, end, expected, previous = batch
        if not (first < seq < last and (end, expected) in verified and (start == 0 or (start, previous) in verified)):
            continue
        matches = {key for offset, key in occurrences.get((kind, body_key, timestamp, source_hash), []) if start < offset <= end}
        if len(matches) != 1:
            continue
        key = next(iter(matches))
        source_ids, owned_runs = canonical.get(key, {}), owners.get((thread, key[0]), set())
        if len(source_ids) != 1 or source_hash is not None:
            # Truncated source hashes need a separately retained native full-body
            # hash. Until one exists, the bounded preview cannot prove equality.
            continue
        source_origin = next(iter(source_ids.values()))
        if source_origin.get("kind") in ("subagent_notification", "turn_aborted"):
            if kind == "user" and not _human:
                proofs[target] = {**source_origin, "source_text_sha256": body_key}
                if len(proofs) > MAX_TARGETS:
                    raise _Unproven()
            continue
        if len(owned_runs) != 1:
            continue
        native_matches = native.get((next(iter(owned_runs)), kind, body_key), [])
        native_matches = [event for event in native_matches if type(event.get("seq")) is int and event["seq"] < first and isinstance(event.get("id"), str)]
        if not native_matches:
            continue
        representative = min(native_matches, key=lambda event: event["seq"])
        proofs[target] = {**next(iter(source_ids.values())), "native_event_id": representative["id"], "source_text_sha256": body_key}
        if len(proofs) > MAX_TARGETS:
            raise _Unproven()
    return proofs


class CodexNativeHistoryRepairCache(CodexGoalHistoryRepairCache):
    """An explicit history boundary prepares proof; per-event projection does no IO."""
    def prepare(self, session_id: str, provider_id: str, events_path: Path, source_path: Path | None,
                root: Path, parse_item: Callable[[dict], dict | None]) -> bool:
        with self._prepare_lock:
            with self._lock:
                previous = self._proofs.get(session_id)
                if previous and previous.provider_id == provider_id:
                    return False
                self._preparing, self._cancelled = session_id, False
            try:
                proof = _prove_native_replays(session_id, provider_id, events_path, source_path, root, parse_item)
            except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                proof = _NativeProof(provider_id)
            with self._lock:
                self._preparing = None
                if self._cancelled:
                    return False
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
        if origin.get("kind") in ("subagent_notification", "turn_aborted"):
            return {**event, "prompt": "", "metadata_only": True,
                    "provider_runtime_context": origin["kind"], "provider_origin": origin,
                    "_agentsdock_imported_prompt_hidden": True}
        return {**event, "prompt" if event["type"] == "turn_started" else "text": "",
                "metadata_only": True, "provider_history_repair": "source_proven_native_replay", "provider_origin": origin,
                **({"_agentsdock_imported_prompt_hidden": True} if event["type"] == "turn_started" else {})}


def filter_native_codex_history_items(session_id: str, provider_id: str, events: Path, items: list[dict]) -> list[dict]:
    """An existing verified import boundary can omit exact completed native copies.

    This reads only the bounded native ledger, never reopens a provider source.
    The caller supplies items parsed from its verified cursor range.
    """
    try:
        if not _PROVIDER_ID.fullmatch(provider_id):
            return items
        stamp = _stamp(events)
        if stamp[2] > MAX_EVENTS_BYTES:
            return items
        owners, native, native_count = {}, {}, 0
        for event, _offset, _line in _records(events, stamp):
            run = event.get("run_id")
            if (event.get("session_id") not in (None, "", session_id) or event.get("imported") is True
                or event.get("backend") not in (None, "codex") or not isinstance(run, str) or run.startswith("import_")):
                continue
            if (event.get("type") == "turn_finished" and event.get("backend") == "codex"
                and event.get("transport") == "app-server" and event.get("provider_thread_id") == provider_id
                and isinstance(event.get("provider_turn_id"), str)):
                owners.setdefault(event["provider_turn_id"], set()).add(run)
            kind = "user" if event.get("type") == "turn_started" else "assistant" if event.get("type") in ("assistant_text", "reasoning_summary", "turn_finished") else None
            body = event.get("prompt") if kind == "user" else event.get("result_text") if event.get("type") == "turn_finished" else event.get("text")
            if kind and isinstance(body, str) and body:
                keys = native.setdefault(run, {})
                key = (kind, _text_key(body))
                native_count += key not in keys
                if isinstance(event.get("id"), str):
                    keys.setdefault(key, event["id"])
            if native_count + len(owners) > MAX_KEYS:
                return items
        result = []
        for item in items:
            origin = item.get("provider_origin")
            runs = owners.get(origin.get("turn_id"), set()) if isinstance(origin, dict) else set()
            known = (isinstance(origin, dict) and origin.get("provider") == "codex"
                     and origin.get("kind") == item.get("kind") and isinstance(origin.get("event_id"), str)
                     and origin.get("session_id", provider_id) == provider_id and len(runs) == 1
                     and item.get("source_text_sha256") is None and isinstance(item.get("text"), str)
                     and (item["kind"], _text_key(item["text"])) in native.get(next(iter(runs)), {}))
            if not known:
                result.append(item)
            elif item["kind"] == "user":
                # Retain a silent exact input boundary: a later unmatched
                # answer must not become the answer to an earlier real input.
                key = (item["kind"], _text_key(item["text"]))
                result.append({**item, "text": "", "metadata_only": True,
                    "provider_history_repair": "source_proven_native_replay",
                    "provider_origin": {**origin, "session_id": provider_id,
                        "native_event_id": native[next(iter(runs))][key], "source_text_sha256": key[1]}})
        return result
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return items
