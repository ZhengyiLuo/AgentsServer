"""Disposable queue checkpoints over authoritative append-only event logs.

A checkpoint is never an ownership record: missing, stale or corrupt state is
rebuilt from events. Writers advance only an exact byte boundary, after the log
append commits; a crash between the two writes leaves a suffix to replay. Legacy
logs bootstrap in the caller's recovery worker, never on an append path.

Like the transcript sequence index, this cache assumes append-only history.
Controlled rewrites must call invalidate(); inode changes, truncation, same-size
rewrites and changed prefix/boundary samples are also detected independently.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, Iterator

VERSION = 1
CHECKPOINT_BYTES = 8 * 1024 * 1024
FINGERPRINT_BYTES = 4096
_LOCKS: dict[str, threading.Lock] = {}
_MEMORY: dict[str, dict[str, Any]] = {}
Reducer = Callable[[dict[str, Any], dict[str, Any]], None]


def projection_path(path: Path) -> Path:
    return path.with_suffix(".queue.json")


def invalidate(path: Path) -> None:
    _MEMORY.pop(str(path), None)
    try:
        projection_path(path).unlink()
    except FileNotFoundError:
        pass


def _lock(path: Path) -> threading.Lock:
    return _LOCKS.setdefault(str(path), threading.Lock())


def _encoded(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _empty(context: dict[str, Any]) -> dict[str, Any]:
    return {"version": VERSION, "offset": 0, "context": context,
            "pending": {}, "order": [], "mailbox": {}, "complete": True}


def _fingerprints(source: Any, offset: int) -> list[str]:
    position = source.tell()
    try:
        source.seek(0)
        head = source.read(min(offset, FINGERPRINT_BYTES))
        source.seek(max(0, offset - FINGERPRINT_BYTES))
        tail = source.read(min(offset, FINGERPRINT_BYTES))
        return [hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()]
    finally:
        source.seek(position)


def _valid(state: Any, source: Any, stamp: os.stat_result,
           context: dict[str, Any]) -> bool:
    if not isinstance(state, dict) or state.get("version") != VERSION:
        return False
    if state.get("context") != context and state.get("pending"):
        return False
    offset = state.get("offset")
    if (type(offset) is not int or offset < 0 or offset > stamp.st_size
            or state.get("device") != stamp.st_dev or state.get("inode") != stamp.st_ino
            or not isinstance(state.get("pending"), dict) or not isinstance(state.get("order"), list)
            or not isinstance(state.get("mailbox"), dict) or type(state.get("complete")) is not bool):
        return False
    if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in state["pending"].items()):
        return False
    if any(not isinstance(key, str) for key in state["order"]) or len(set(state["order"])) != len(state["order"]):
        return False
    if any(not isinstance(key, str) or not isinstance(value, dict) for key, value in state["mailbox"].items()):
        return False
    # A same-size rewrite is not an append, even when its first/last blocks match.
    if offset == stamp.st_size and state.get("mtime_ns") != stamp.st_mtime_ns:
        return False
    return state.get("fingerprints") == _fingerprints(source, offset)


def _load(path: Path, source: Any, stamp: os.stat_result,
          context: dict[str, Any], *, clone: bool = True) -> dict[str, Any] | None:
    memory = _MEMORY.get(str(path))
    if memory is not None and _valid(memory, source, stamp, context):
        state = copy.deepcopy(memory) if clone else memory
        state["context"] = context
        return state
    try:
        envelope = json.loads(projection_path(path).read_bytes())
        state = envelope["state"]
        if envelope["sha256"] != hashlib.sha256(_encoded(state)).hexdigest():
            return None
        if _valid(state, source, stamp, context):
            state["context"] = context
            return state
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _save(path: Path, state: dict[str, Any]) -> None:
    """Atomic private replacement; publication is optional for correctness."""
    destination = projection_path(path)
    payload = _encoded({"state": state, "sha256": hashlib.sha256(_encoded(state)).hexdigest()})
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", suffix=".tmp", dir=destination.parent)
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _stamp(state: dict[str, Any], source: Any, stamp: os.stat_result, offset: int) -> None:
    state.update(offset=offset, device=stamp.st_dev, inode=stamp.st_ino,
                 mtime_ns=stamp.st_mtime_ns, fingerprints=_fingerprints(source, offset))


def _scan_lines(source: Any, limit: int) -> Iterator[tuple[int, bytes]]:
    offset = source.tell()
    while offset < limit:
        raw = source.readline(limit - offset)
        if not raw:
            break
        offset += len(raw)
        yield offset, raw


def _candidate(raw: bytes) -> bool:
    return (b'"queued_id"' in raw or b'"turn_queue_paused"' in raw
            or b'"cross_chat_envelope_id"' in raw)


def read_projection(path: Path, reducer: Reducer,
                    *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return an isolated queue snapshot, replaying only an uncached suffix."""
    context = context or {}
    with _lock(path), path.open("rb") as source:
        before = os.fstat(source.fileno())
        loaded = _load(path, source, before, context)
        state = loaded or _empty(context)
        initial_offset = state.get("persisted_offset", -1)
        source.seek(state["offset"])
        complete_boundary = True
        if state["offset"] < before.st_size:
            for offset, raw in _scan_lines(source, before.st_size):
                if not raw.endswith(b"\n"):
                    complete_boundary = False
                    state["complete"] = False
                if not _candidate(raw):
                    continue
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                    if not isinstance(event, dict):
                        raise ValueError("event must be an object")
                except (ValueError, TypeError):
                    state["complete"] = False
                    continue
                reducer(state, event)
        after = os.fstat(source.fileno())
        current = path.stat()
        unchanged = ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                     == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                     == (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns))
        # Concurrent appends are harmless: retain the exact prefix, then replay
        # the gap on the next read. Rewrites/truncation require a fresh snapshot.
        same_file = (before.st_dev, before.st_ino) == (current.st_dev, current.st_ino)
        append_only = (same_file and after.st_size >= before.st_size
                       and (after.st_size != before.st_size or after.st_mtime_ns == before.st_mtime_ns))
        if not append_only:
            _MEMORY.pop(str(path), None)
            raise OSError("event log changed during queue recovery")
        if complete_boundary:
            _stamp(state, source, before, before.st_size)
            state["persisted_offset"] = state["offset"]
            _MEMORY[str(path)] = copy.deepcopy(state)
            if loaded is None or initial_offset != state["offset"]:
                try:
                    _save(path, state)
                except OSError:
                    pass
        else:
            _MEMORY.pop(str(path), None)
        result = copy.deepcopy(state)
        result["complete"] = bool(result["complete"] and unchanged)
        return result


def record_append(path: Path, start_offset: int, events: list[dict[str, Any]],
                  reducer: Reducer, *, context: dict[str, Any] | None = None,
                  end_offset: int | None = None) -> None:
    """Advance a known prefix; never scan legacy history or block its scanner.

    The caller serializes authoritative appends for this chat. Queue transitions
    checkpoint immediately; token/trace traffic checkpoints every 8 MiB, bounding
    restart replay without fsyncing a sidecar for every streaming update.
    """
    lock = _lock(path)
    if not lock.acquire(blocking=False):
        return
    try:
        with path.open("rb") as source:
            stamp = os.fstat(source.fileno())
            committed_end = stamp.st_size if end_offset is None else end_offset
            if not start_offset <= committed_end <= stamp.st_size:
                return
            state = _load(path, source, stamp, context or {}, clone=False)
            if state is None:
                if start_offset != 0:
                    return
                state = _empty(context or {})
            if state["offset"] > start_offset:
                return
            relevant = False
            if state["offset"] < start_offset:
                # A concurrent recovery reader may have skipped an append
                # hook. Catch up a bounded prefix so that one skipped hook
                # cannot leave a busy chat's checkpoint stale for days. Finish
                # the last record whole (memory remains bounded to one row).
                source.seek(state["offset"])
                boundary = state["offset"]
                budget_end = min(start_offset, boundary + CHECKPOINT_BYTES)
                while boundary < budget_end:
                    raw = source.readline(start_offset - boundary)
                    if not raw or not raw.endswith(b"\n"):
                        _MEMORY.pop(str(path), None)
                        return
                    boundary += len(raw)
                    if _candidate(raw):
                        try:
                            event = json.loads(raw.decode("utf-8", "replace"))
                            if not isinstance(event, dict):
                                raise ValueError("event must be an object")
                        except (ValueError, TypeError):
                            state["complete"] = False
                            continue
                        reducer(state, event)
                _stamp(state, source, stamp, boundary)
                relevant = True
                if boundary < start_offset:
                    state["persisted_offset"] = boundary
                    _save(path, state)
                    _MEMORY[str(path)] = state
                    return
            for event in events:
                if (event.get("queued_id") or event.get("type") == "turn_queue_paused"
                        or event.get("cross_chat_envelope_id") or event.get("type") == "turn_queue_reordered"):
                    reducer(state, event)
                    relevant = True
                elif event.get("type") in {"turn_finished", "turn_stopped"}:
                    relevant = True
            # A cancelled async caller can release its delivery lock while
            # this worker is still running. Claim only its supplied byte range,
            # never a later append visible through fstat by the time we run.
            _stamp(state, source, stamp, committed_end)
            if relevant or committed_end - state.get("persisted_offset", 0) >= CHECKPOINT_BYTES:
                state["persisted_offset"] = committed_end
                _save(path, state)
            _MEMORY[str(path)] = state
    except Exception:
        # The append is already authoritative. Cache failure must not turn a
        # successful enqueue/start into a retry and duplicate execution.
        _MEMORY.pop(str(path), None)
    finally:
        lock.release()
