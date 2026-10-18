"""Bounded-memory projection of native JSONL, with exact raw-byte receipts.

Opaque media/tool fields stay in the provider-owned file. This reader validates
all JSON, including skipped strings, but only materializes history metadata and
text. Bounds apply to retained JSON/work, never to opaque payload/file bytes.
"""
from __future__ import annotations

import codecs
import json
import re
import time
from dataclasses import dataclass
from typing import BinaryIO, Callable

CHUNK_BYTES = 64 * 1024
MAX_RETAINED_BYTES = 32 * 1024 * 1024
MAX_DEPTH = 96
MAX_NODES = 250_000
MAX_KEY_BYTES = 4096
MAX_SCALAR_BYTES = 1024
SCAN_SECONDS = 60.0
_SPECIAL = re.compile(rb'["\\\x00-\x1f]')
_NUMBER = re.compile(rb'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z')
_HEX = frozenset(b'0123456789abcdefABCDEF')


class TranscriptBudgetExceeded(ValueError):
    """Never treat exhausted work/memory as an ignorable malformed record."""

    def at_record(self, number: int, start: int):
        # A delta scan starts inside the file; do not call this a file line or
        # a user-visible chat message number. Never include transcript content.
        return TranscriptBudgetExceeded(
            f'{self} Record {number:,} in this scan (source byte offset {start:,}).')


class InvalidRecord(ValueError):
    pass


def scan_check(deadline: float, cancelled: Callable[[], bool] | None = None,
               *, budget_seconds: float | None = None):
    if cancelled is not None and cancelled():
        raise TranscriptBudgetExceeded('transcript scan cancelled')
    if time.monotonic() >= deadline:
        limit = f' ({budget_seconds:g} seconds)' if budget_seconds is not None else ''
        raise TranscriptBudgetExceeded(
            f'Transcript scan exceeded its processing time budget{limit}. '
            'Reading stopped before completion; this is a processing timeout, not a file-size limit.')


def byte_amount(size: int) -> str:
    mib = f' ({size / (1024 * 1024):.2f} MiB)' if size >= 1024 * 1024 else ''
    return f'{size:,} bytes{mib}'


def opaque_path(path: tuple) -> bool:
    # These fields never feed text_from_content, native user/assistant matching,
    # ownership/turn metadata or goal/wake classification. Do not skip arbitrary
    # `content`, `text`, `message`, thinking or unknown fields.
    if path == ('payload', 'output'):
        return True  # Codex function/custom tool output, not assistant text.
    for prefix in (('message', 'content'), ('payload', 'content')):
        if len(path) > 3 and path[:2] == prefix and isinstance(path[2], int):
            tail = path[3:]
            if prefix == ('message', 'content') and tail in (('content',), ('input',)):
                return True  # Claude tool result/body and tool invocation arguments.
            if tail in (('source', 'data'), ('image_url',), ('image_url', 'url')):
                return True
    return False


@dataclass(frozen=True)
class RawRecord:
    stream: BinaryIO
    start: int
    end: int
    complete: bool
    blank: bool
    malformed: bool
    deadline: float
    cancelled: Callable[[], bool] | None = None
    number: int = 1
    budget_seconds: float | None = None

    def __len__(self):
        return self.end - self.start

    def update_hash(self, digest):
        # Restore the parser's stream position; this also works on Windows,
        # where pread is unavailable. Each iterator owns this file handle.
        saved = self.stream.tell()
        try:
            self.stream.seek(self.start)
            remaining = len(self)
            while remaining:
                scan_check(self.deadline, self.cancelled, budget_seconds=self.budget_seconds)
                chunk = self.stream.read(min(CHUNK_BYTES, remaining))
                if not chunk:
                    raise ValueError('transcript was truncated during record hashing')
                digest.update(chunk)
                remaining -= len(chunk)
        except TranscriptBudgetExceeded as error:
            raise error.at_record(self.number, self.start) from error
        finally:
            self.stream.seek(saved)



class _RecordParser:
    def __init__(self, stream, end, deadline, cancelled, project, maximum, budget_seconds, retained_fields=None, max_nodes=-1, max_depth=None):
        self.stream, self.end = stream, end
        self.deadline, self.cancelled, self.project = deadline, cancelled, project
        self.buffer, self.index, self.final, self.complete = b'', 0, False, False
        self.retained, self.nodes, self.maximum = 0, 0, maximum
        self.budget_seconds = budget_seconds
        self.retained_fields = retained_fields
        self.max_nodes = MAX_NODES if max_nodes == -1 else max_nodes
        self.max_depth = MAX_DEPTH if max_depth is None else max_depth

    def fill(self):
        if self.index < len(self.buffer):
            return True
        if self.final:
            return False
        scan_check(self.deadline, self.cancelled, budget_seconds=self.budget_seconds)
        remaining = self.end - self.stream.tell()
        self.buffer = self.stream.readline(min(CHUNK_BYTES, remaining)) if remaining > 0 else b''
        self.index = 0
        self.complete = self.buffer.endswith(b'\n')
        self.final = self.complete or not self.buffer or self.stream.tell() >= self.end
        return bool(self.buffer)

    def peek(self):
        return self.buffer[self.index] if self.fill() else None

    def take(self):
        value = self.peek()
        if value is None:
            raise InvalidRecord('incomplete JSON record')
        self.index += 1
        return value

    def space(self):
        while self.peek() in (9, 10, 13, 32):
            self.index += 1

    def reserve(self, size):
        self.retained += size
        if self.retained > self.maximum:
            raise TranscriptBudgetExceeded(
                'Single transcript record retained text/metadata exceeds its import budget: '
                f'observed at least {byte_amount(self.retained)}; limit {byte_amount(self.maximum)}. '
                'This includes accounting overhead, not the total conversation file size.')

    def check_key_size(self, size):
        if size > MAX_KEY_BYTES:
            raise TranscriptBudgetExceeded(
                'Single transcript record contains an oversized JSON field name: '
                f'observed at least {byte_amount(size)}; limit {byte_amount(MAX_KEY_BYTES)}.')

    def string(self, retain, key=False):
        if self.take() != 34:
            raise InvalidRecord('JSON string expected')
        parts = bytearray(b'"') if retain else None
        decoder = codecs.getincrementaldecoder('utf-8')()
        size = 0
        while self.fill():
            match = _SPECIAL.search(self.buffer, self.index)
            stop = match.start() if match else len(self.buffer)
            piece = self.buffer[self.index:stop]
            decoder.decode(piece, final=False)
            if retain:
                self.reserve(len(piece)); parts.extend(piece)
                size += len(piece)
                if key:
                    self.check_key_size(size)
            self.index = stop
            if match is None:
                continue
            special = self.take()
            if special == 34:
                decoder.decode(b'', final=True)
                if retain:
                    parts.append(34)
                    return json.loads(parts)
                return None
            if special != 92:
                raise InvalidRecord('control character in JSON string')
            # Escapes are ASCII boundaries: a partial UTF-8 codepoint here is
            # invalid, including in a discarded field.
            decoder.decode(b'', final=True)
            decoder = codecs.getincrementaldecoder('utf-8')()
            escape = self.take()
            raw = bytes((92, escape))
            if escape == 117:
                suffix = bytes(self.take() for _ in range(4))
                if any(value not in _HEX for value in suffix):
                    raise InvalidRecord('invalid JSON Unicode escape')
                raw += suffix
            elif escape not in b'"\\/bfnrt':
                raise InvalidRecord('invalid JSON escape')
            if retain:
                self.reserve(len(raw)); parts.extend(raw)
                size += len(raw)
                if key:
                    self.check_key_size(size)
        raise InvalidRecord('unterminated JSON string')

    def value(self, path=(), retain=True, depth=0):
        self.nodes += 1
        if depth > self.max_depth:
            raise TranscriptBudgetExceeded(
                'Single transcript record exceeds the JSON nesting limit: '
                f'observed depth {depth:,}; limit {self.max_depth:,}.')
        if self.max_nodes is not None and self.nodes > self.max_nodes:
            raise TranscriptBudgetExceeded(
                'Single transcript record exceeds the JSON value-count limit: '
                f'observed at least {self.nodes:,} values; limit {self.max_nodes:,}.')
        self.space()
        if path and self.retained_fields is not None and path[0] not in self.retained_fields:
            retain = False
        if self.project and opaque_path(path):
            retain = False
        if retain:
            self.reserve(64)  # Bound object overhead as well as string bytes.
        first = self.peek()
        if first == 34:
            return self.string(retain)
        if first in (123, 91):
            self.take()
            obj = first == 123
            result = ({} if obj else []) if retain else None
            close = 125 if obj else 93
            self.space()
            if self.peek() == close:
                self.take(); return result
            index = 0
            while True:
                if obj:
                    self.space()
                    # Explicit metadata projection does not need keys inside
                    # discarded subtrees. Do not charge transient key bytes as
                    # retained metadata (native import behavior is unchanged).
                    keep_key = retain or self.retained_fields is None or self.project
                    key = self.string(keep_key, key=True)
                    self.space()
                    if self.take() != 58:
                        raise InvalidRecord('JSON colon expected')
                else:
                    key = index
                value = self.value(path+(key,), retain, depth+1)
                if retain:
                    if obj: result[key] = value
                    else: result.append(value)
                self.space()
                delimiter = self.take()
                if delimiter == close:
                    return result
                if delimiter != 44:
                    raise InvalidRecord('JSON comma expected')
                index += 1
        token = bytearray()
        while self.peek() is not None and self.peek() not in b' \r\n\t,]}':
            token.append(self.take())
            if len(token) > MAX_SCALAR_BYTES:
                raise TranscriptBudgetExceeded(
                    'Single transcript record contains an oversized JSON scalar (number or literal): '
                    f'observed at least {byte_amount(len(token))}; limit {byte_amount(MAX_SCALAR_BYTES)}.')
        raw = bytes(token)
        if raw not in (b'true', b'false', b'null', b'NaN', b'Infinity', b'-Infinity') and not _NUMBER.fullmatch(raw):
            raise InvalidRecord('invalid JSON scalar')
        return json.loads(raw) if retain else None

    def drain(self):
        while self.fill():
            self.index = len(self.buffer)


def jsonl_records(stream: BinaryIO, *, end: int, project=True, strict=False,
                  require_newline=True, max_records=250_000, deadline=None,
                  cancelled=None, max_retained_bytes=MAX_RETAINED_BYTES, retained_fields=None,
                  max_nodes=-1, max_depth=None):
    """Yield (projected object or None, ending offset, RawRecord).

    Malformed provider lines may be represented by None, as before. Resource
    exhaustion always raises, so callers never commit a partial-success cursor.
    A fixed end excludes concurrent appends; callers own file identity checks.
    Native-import work budgets remain the default. Durable metadata-only scans
    can supply separate structural limits without imposing import caps on logs.
    """
    # Externally supplied deadlines can belong to a larger operation. Do not
    # invent a duration for them; the caller can provide its own timeout detail.
    budget_seconds = SCAN_SECONDS if deadline is None else None
    deadline = time.monotonic() + SCAN_SECONDS if deadline is None else deadline
    count = 0
    while stream.tell() < end:
        count += 1
        start = stream.tell()
        if count > max_records:
            raise TranscriptBudgetExceeded(
                'Transcript scan exceeds the record-count limit: '
                f'observed at least {count:,} records; limit {max_records:,}. '
                'This counts JSONL records, not chat messages.').at_record(count, start)
        parser = _RecordParser(stream, end, deadline, cancelled, project, max_retained_bytes, budget_seconds, retained_fields, max_nodes, max_depth)
        blank = False
        malformed = False
        try:
            try:
                parser.space()
                blank = parser.peek() is None
                value = None if blank else parser.value()
                parser.space()
                if parser.peek() is not None:
                    raise InvalidRecord('trailing JSON content')
            except (InvalidRecord, UnicodeDecodeError, json.JSONDecodeError) as error:
                if strict:
                    raise ValueError('malformed transcript record') from error
                parser.drain()
                malformed = True
                value = None
        except TranscriptBudgetExceeded as error:
            raise error.at_record(count, start) from error
        stop = stream.tell()
        if stop <= start:
            raise ValueError('transcript was truncated during reading')
        if require_newline and not parser.complete:
            raise ValueError('transcript cursor does not end on a complete line')
        raw = RawRecord(stream, start, stop, parser.complete, blank, malformed or blank,
                        deadline, cancelled, count, budget_seconds)
        if strict and not isinstance(value, dict):
            raise ValueError('transcript object expected')
        yield value if isinstance(value, dict) else None, stop, raw
