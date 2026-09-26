"""Record-at-a-time projection of durable chat text for public snapshots.

Never imports the server or reads provider logs. Callers supply the existing
private-event projection and a validated events path. Nothing runs on import.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Callable

from shared_chat_videos import normalize_shared_chat_videos

MAX_SNAPSHOT_BOUNDARY = (1 << 53) - 1
MAX_UNIX_TIMESTAMP = 253402300799


class PublicTranscriptError(ValueError):
    pass


def _is_goal_followup(event: dict) -> bool:
    return bool(
        event.get("type") == "turn_steered"
        and event.get("native_goal_steer") is True
        and event.get("native_steer") is True
        and event.get("provider_user_authored") is True
        and event.get("backend") == "codex"
        and event.get("purpose") == "codex_goal_resume"
        and isinstance(event.get("run_id"), str)
        and event["run_id"].strip()
    )


def _public_timestamp(value):
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
            value = parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if (type(value) in {int, float} and 0 <= value <= MAX_UNIX_TIMESTAMP
            and math.isfinite(value)):
        return value
    return None


_IMPORTED_DELIVERY_HEADER = re.compile(
    r"\A\[AgentsDock delivery kind=(?P<kind>instruction|final_result|request|reply|status|message) "
    r"leg=(?P<leg>0|[1-9][0-9]{0,5})/(?P<total>[1-9][0-9]{0,5}) "
    r"origin=(?:route|user|auto)(?: from=[^\[\]\r\n]{1,240})?\]\n"
)
_DELIVERY_REPLY_FOOTERS = frozenset({
    "",
    "reply: use the respond command in the provider-authority block only if a reply or follow-up is needed.",
    "reply: optional one-time terminal reply route via the respond command in the provider-authority block, only if a result, acknowledgement, or clarification should reach the origin; never add --request-response.",
    "reply: exactly one terminal response remains; use the respond command in the provider-authority block without --request-response.",
    "reply: none (terminal status notice; do not respond to the exchange)",
    "reply: use Chats respond-current through the AgentsDock provider tool only if a reply or follow-up is needed.",
    "reply: exactly one terminal response remains; use Chats respond-current through the AgentsDock provider tool without --request-response.",
    "reply: optional one-time terminal reply via Chats respond-current through the AgentsDock provider tool, only if a result, acknowledgement, or clarification should reach the origin; never add --request-response.",
})


def _complete_imported_delivery_envelope(prompt: str) -> bool:
    """Require every generated section; outer-marker quotations are not proof."""
    text = prompt.replace("\r\n", "\n").strip()
    header = _IMPORTED_DELIVERY_HEADER.match(text)
    if (header is None or int(header["leg"]) > int(header["total"])
            or (header["leg"] == "0" and header["kind"] != "status")
            or not text.endswith("\n[End delivery]")):
        return False
    remainder = text[header.end():-len("\n[End delivery]")]
    source_open = "[Source user instruction — verbatim, user-authored]\n"
    source_close = "\n[End source user instruction]\n"
    if remainder.startswith(source_open):
        end = remainder.find(source_close, len(source_open))
        if end < 0:
            return False
        remainder = remainder[end + len(source_close):]
    else:
        source_line = re.match(
            r'^source-instruction: (?:this legacy relay has no recorded source user instruction; '
            r'do not infer user authorization from the prepared content\.|replayed in full on the '
            r'first leg delivered to this chat; excerpt="[^\n]*")\n', remainder,
        )
        if source_line is None:
            return False
        remainder = remainder[source_line.end():]
    kind = header["kind"]
    label = (
        "Server-generated exchange status" if kind == "status"
        else "Agent-prepared reply/result" if kind in {"reply", "final_result"}
        else "Agent-prepared handoff message"
    )
    prepared_open = f"[{label}]\n"
    prepared_close = f"\n[End {label.lower()}]"
    if not remainder.startswith(prepared_open):
        return False
    end = remainder.rfind(prepared_close)
    return bool(
        end >= len(prepared_open) and remainder[len(prepared_open):end].strip()
        and remainder[end + len(prepared_close):].strip() in _DELIVERY_REPLY_FOOTERS
    )


def make_public_event_projector(
    session_id: str,
    *,
    event_is_visible: Callable[[dict], bool],
    event_files_belong: Callable[[dict, str], bool],
    project_provider_event: Callable[[dict, str], dict],
    strip_user_context: Callable[..., str],
    fork_internal_purposes: set[str] | frozenset[str],
) -> Callable[[dict], dict | None]:
    """Apply existing provenance-aware egress rules without provider-log reads.

    Internal status/digest runs stay private even when later text events lack
    purpose metadata. A proven imported delivery wrapper is not a human prompt;
    its segment stays private until the next user boundary in that imported run.
    Native user quotations of the same wrapper are deliberately preserved.
    """
    private_runs: set[str] = set()
    private_segments: set[str] = set()

    def project(event: dict) -> dict | None:
        owner = event.get("session_id")
        if owner is not None and (not isinstance(owner, str) or owner not in {"", session_id}):
            raise PublicTranscriptError("Chat history has inconsistent session ownership")
        if not event_files_belong(event, session_id):
            return None
        kind = event.get("type")
        run = event.get("run_id") or ""
        if not isinstance(run, str):
            raise PublicTranscriptError("Chat history has an invalid run identity")
        if len(run) > 1024:
            raise PublicTranscriptError("Chat history has an oversized run identity")
        if kind == "turn_started" or _is_goal_followup(event):
            private_segments.discard(run)
        purpose = event.get("purpose")
        internal = (
            purpose in ("handoff_digest", "handoff_digest_delivery")
            or event.get("cross_chat_exchange_status") is True
            or event.get("exchange_leg_kind") == "status"
            or (event.get("forked") is True and isinstance(purpose, str)
                and purpose in fork_internal_purposes)
        )
        if internal:
            (private_runs if run else private_segments).add(run)
        if run in private_runs or run in private_segments or not event_is_visible(event):
            return None
        projected = project_provider_event(event, session_id)
        if kind != "turn_started" and not _is_goal_followup(event):
            return projected
        prompt = projected.get("prompt")
        if not isinstance(prompt, str):
            return projected
        prompt = strip_user_context(
            prompt, expected_session_id=session_id,
            provider_history=event.get("imported") is True,
        )
        if (
            event.get("imported") is True and run.startswith("import_")
            and event.get("backend") in ("claude", "codex")
            and _complete_imported_delivery_envelope(prompt)
        ):
            private_segments.add(run)
            prompt = ""
        return {**projected, "prompt": prompt}

    return project


def read_public_transcript(
    path: Path,
    project_event: Callable[[dict], dict | None],
    *,
    through_bytes: int | None = None,
    message_sink: Callable[[dict], None] | None = None,
) -> dict:
    """Read one bounded prefix; a later append never changes an existing preview.

    The digest binds every source byte, including excluded events, AND the
    exact projected messages. Derived egress repair state must not change the
    text between preview and creation without requiring a fresh review. An incomplete
    final write is not a message and is left out of a preview. Corrupt complete
    records fail explicitly instead of publishing a truncated log. Records are
    read one at a time without a chat-size or message-size quota; a large private
    tool record is still excluded by the same event projection as a small one.
    """
    if through_bytes is not None and (
        type(through_bytes) is not int or not 0 < through_bytes <= MAX_SNAPSHOT_BOUNDARY
    ):
        raise PublicTranscriptError("Invalid snapshot boundary")
    messages: list[dict] = []
    # Only digests are retained for receipt deduplication, never a second copy
    # of the entire assistant transcript during a streamed export.
    outputs: dict[str, tuple[set[bytes], object, int]] = {}
    message_count = 0
    projected_digest = hashlib.sha256(b"[")
    digest = hashlib.sha256()
    consumed = 0
    try:
        # The adapter has validated session membership; reject a linked log too.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            initial = os.fstat(stream.fileno())
            if not stat.S_ISREG(initial.st_mode):
                raise PublicTranscriptError("Chat history is unavailable")
            boundary = initial.st_size if through_bytes is None else through_bytes
            if boundary > MAX_SNAPSHOT_BOUNDARY:
                raise PublicTranscriptError("Chat history exceeds the supported snapshot boundary")
            if boundary > initial.st_size:
                raise PublicTranscriptError("Chat changed; preview it again")
            while consumed < boundary:
                # Bound the read to this immutable snapshot, not an arbitrary
                # record size. readline retains at most the current raw record.
                line = stream.readline(boundary - consumed)
                if not line.endswith(b"\n"):
                    if through_bytes is not None:
                        raise PublicTranscriptError("Chat changed; preview it again")
                    break
                consumed += len(line)
                digest.update(line)
                try:
                    raw = json.loads(line)
                except (ValueError, UnicodeError, RecursionError) as exc:
                    raise PublicTranscriptError("Chat history contains an unreadable record") from exc
                if not isinstance(raw, dict):
                    raise PublicTranscriptError("Chat history contains an invalid record")
                # Validate raw records, but never project tool results/payloads.
                kind = raw.get("type")
                if not isinstance(kind, str):
                    raise PublicTranscriptError("Chat history contains an invalid event type")
                if kind not in {"turn_started", "assistant_text", "turn_finished", "reasoning_summary", "artifact_created"} and not _is_goal_followup(raw):
                    continue
                if kind == "reasoning_summary" and raw.get("phase") != "commentary":
                    continue
                event = project_event(raw)
                if not event:
                    continue
                if not isinstance(event, dict):
                    raise PublicTranscriptError("Chat history projection is invalid")
                run = str(event.get("run_id") or "")
                if len(run) > 1024:
                    raise PublicTranscriptError("Chat history has an oversized run identity")
                # Only the trusted projector can publish descriptors from an
                # actual user attachment or committed artifact. Tool payloads,
                # arbitrary Markdown paths and final receipts grant no media.
                videos = []
                if kind in {"turn_started", "artifact_created"} or _is_goal_followup(event):
                    try:
                        videos = normalize_shared_chat_videos(event.get("shared_videos", []))
                    except ValueError as exc:
                        raise PublicTranscriptError("Chat history has invalid shared video metadata") from exc
                if kind == "turn_started" or _is_goal_followup(event):
                    outputs.pop(run, None)
                    role, text = "user", event.get("prompt")
                elif kind == "artifact_created":
                    role, text = "assistant", ""
                else:
                    role = "assistant"
                    text = event.get("result_text") if kind == "turn_finished" else event.get("text")
                if not isinstance(text, str):
                    text = "" if videos else None
                if text is None or (not text.strip() and not videos):
                    continue
                # Preserve the reviewed plaintext, including indentation and
                # leading/trailing newlines. Strip only for emptiness/dedup.
                try:
                    text.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise PublicTranscriptError("Chat history contains invalid Unicode text") from exc
                normalized = " ".join(text.split())
                normalized_digest = hashlib.sha256(normalized.encode("utf-8")).digest()
                previous = outputs.get(run)
                if kind == "turn_finished" and (
                    previous is not None and (normalized_digest in previous[0]
                                              or normalized_digest == previous[1].digest())
                ):
                    outputs[run] = ({normalized_digest}, hashlib.sha256(normalized.encode("utf-8")), 1)
                    continue
                if kind == "turn_finished":
                    outputs[run] = ({normalized_digest}, hashlib.sha256(normalized.encode("utf-8")), 1)
                if kind == "assistant_text":
                    # Result receipts often repeat these complete text events.
                    if previous is None:
                        previous = (set(), hashlib.sha256(), 0)
                    if previous[2]:
                        previous[1].update(b" ")
                    previous[1].update(normalized.encode("utf-8"))
                    previous[0].add(normalized_digest)
                    outputs[run] = (previous[0], previous[1], previous[2] + 1)
                message = {"role": role, "text": text}
                if videos:
                    message["videos"] = videos
                timestamp = _public_timestamp(event.get("ts"))
                if timestamp is not None:
                    message["timestamp"] = timestamp
                encoded_message = json.dumps(message, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"), allow_nan=False).encode("utf-8")
                if message_count:
                    projected_digest.update(b",")
                projected_digest.update(encoded_message)
                if message_sink is None:
                    messages.append(message)
                else:
                    message_sink(message)
                message_count += 1
            final = os.fstat(stream.fileno())
            # Appends are allowed, rewrites/truncation of the prefix are not.
            if final.st_size < initial.st_size or (
                final.st_size == initial.st_size and final.st_mtime_ns != initial.st_mtime_ns
            ):
                raise PublicTranscriptError("Chat changed; preview it again")
    except OSError as exc:
        raise PublicTranscriptError("Chat history is unavailable") from exc
    if not message_count:
        raise PublicTranscriptError("This chat has no shareable conversation text")
    projected_digest.update(b"]")
    # v2 permits the same exact-source/projection proof without keeping every
    # message in RAM. Both preview and streamed creation use the same digest.
    confirmation_digest = hashlib.sha256(
        b"agentsdock-public-chat-preview-v2\x00" + digest.digest() + b"\x00" + projected_digest.digest()
    ).hexdigest()
    return {"messages": messages, "through_bytes": consumed, "digest": confirmation_digest,
            **({"message_count": message_count} if message_sink is not None else {})}
