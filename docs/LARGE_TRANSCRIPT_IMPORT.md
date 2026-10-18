# Large native transcript records

Claude/Codex native history import and synchronization use a chunked JSONL
projection. Source file size and an opaque media/tool field's size no longer
have the former **256 MiB file / 32 MiB raw record** admission caps. A reported
record number is relative to the current scan, not a chat turn or model context.

## What is streamed

The reader validates JSON in 64 KiB chunks. Claude image/document Base64,
image URLs, tool-result bodies/tool arguments, and Codex image/tool-output
payloads are not materialized for the text history projection. They remain
unchanged in the native provider file. Text, message type, timestamps, tool
identity, flags, and ownership/turn metadata remain available to the existing
history normalizers. Unknown fields and public message text are not silently
truncated to make an import succeed.

This does not add image rendering or copy media into AgentsDock attachments.
Native resume still refers to the original provider session and its history.

Cursor snapshots hash fixed-size chunks without accumulating an unfinished
record. Proof readers hash original byte ranges, not the projected JSON; source
identity, exact offsets, hashes and final file-stability checks remain required.
Import, delta sync and native fork-boundary reads use the shared reader. All
runtime package/installer inventories include it.

## Remaining resource protections

This is bounded-memory processing, not a promise to accept every possible file:

- Retained text/metadata per record has a 32 MiB accounting budget, including
  a conservative object overhead charge. It is not a total process RSS limit.
- JSON depth, node count, key/scalar sizes and record count are bounded.
- Each streaming scan has a 60-second work deadline; native Codex proof retains
  its stricter existing deadline/cancellation checks.
- Displayed text, message count, retained proof keys and private event-ledger
  budgets remain bounded. Legacy goal-proof aggregate budgets remain unchanged.
- Budget exhaustion raises an explicit failure; it is never handled as an
  ignorable malformed line or a successful partial import. Malformed native
  lines retain their prior skip/lineage semantics.

The server does not enforce one file-size threshold for the optimized import
path. A huge textual message, pathological JSON, excessively many records or a
very slow source can still exhaust a specific processing/proof budget.

## Import failure messages

Streaming budget failures identify the exact protection: retained text/metadata
in one record, JSON field/scalar size, nesting depth, JSON value count, scan
record count, or processing timeout. Size/count failures include the observed
amount and configured limit. The observed amount is a lower bound because
reading stops at the threshold; it is not the full offending record's size.
The 32 MiB retained budget includes accounting overhead, not just text bytes or
the entire conversation's disk size. Default scan timeouts name the 60-second
budget; externally owned deadlines do not invent a duration.

Failures include the record's ordinal within the scan and its starting byte
offset in the original file. This remains accurate for incremental scans that
start in the middle of a file. Messages never include conversation text or JSON
field names. The import API returns the reason through its existing `error`
field, used by the desktop import result and resume-by-ID error display. Failed
imports do not report partial success or alter the provider transcript.

## Verification

Disposable fixtures cover records larger than 32 MiB, files larger than 256 MiB,
partial lines, serialized large offsets, repeat synchronization, exact raw-byte
hashes, malformed skipped payloads, cancellation, and provenance filtering.
See `tests/test_transcript_reader.py`, `tests/test_large_transcript_import.py`,
and the Claude/Codex history repair suites.

A macOS isolated-reader measurement found approximately 281 MiB peak RSS for
the former whole-line JSON approach on a 64 MiB image record, versus 23 MiB for
the streaming reader. A 300 MiB image record used approximately 24 MiB. These
figures include the small reader process, not a running AgentsServer, and are
fixture-specific measurements rather than universal memory guarantees.
