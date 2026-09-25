"""Claude Code's native /btw control on an already connected SDK client.

Claude Code 2.1.261 supports ``side_question`` and request-scoped
``control_cancel_request`` on its stream-json control channel. Python Agent SDK
0.2.130 does not wrap this control yet. This small compatibility adapter uses
the SDK's existing response router; it never consumes the main message stream,
submits a user turn, forks a session, or interrupts/disconnects the parent.

The caller must run on the client owner's event loop and hold its lifecycle
lease for this entire await. The native provider snapshots its current full
conversation (including tool results), denies tools, and skips transcript writes.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import re
import uuid
from typing import Any

from side_questions import (
    MAX_OUTPUT_BYTES,
    MAX_QUESTION_CHARS,
    SideQuestionError,
)


CONTEXT_NOTE = (
    "Uses Claude's native conversation context, including completed tool results. "
    "The reply currently being written may not be included. "
    "Side questions have no tools and do not enter the main conversation."
)
CANCEL_TIMEOUT_SECONDS = 5.0
_OWNED_REQUEST_ID = re.compile(r"agentsdock_side_[0-9a-f]{32}\Z")


def is_native_side_question_progress(message: Any) -> bool:
    """Keep only our native control progress out of the main-turn stream.

    Recognize the reserved request namespace even after cancellation removes
    its pending entry, since a late progress packet still belongs to the side
    request. Other native controls and ordinary system messages remain visible.
    """
    if isinstance(message, dict):
        if message.get("type") != "system":
            return False
        subtype, data = message.get("subtype"), message
    else:
        subtype, data = getattr(message, "subtype", None), getattr(message, "data", None)
    if subtype != "control_request_progress" or not isinstance(data, dict):
        return False
    request_id = data.get("request_id")
    return isinstance(request_id, str) and _OWNED_REQUEST_ID.fullmatch(request_id) is not None


def _native_query(client: Any) -> Any:
    """Fail closed if this SDK cannot route our separate native control."""
    query = getattr(client, "_query", None)
    if (
        query is None
        or not getattr(query, "is_streaming_mode", False)
        or not getattr(query, "_initialized", False)
        or getattr(query, "_closed", True)
        or not isinstance(getattr(query, "pending_control_responses", None), dict)
        or not isinstance(getattr(query, "pending_control_results", None), dict)
        or not callable(getattr(getattr(query, "transport", None), "write", None))
    ):
        raise SideQuestionError(503, "Claude native side questions require a connected compatible SDK session")
    return query


def _request(question: str, history: list[dict] | None) -> dict:
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise SideQuestionError(400, "Side questions must contain 1–8000 characters")
    try:
        question.encode("utf-8")
    except UnicodeEncodeError:
        raise SideQuestionError(400, "Side questions must contain valid Unicode text") from None
    history = [] if history is None else history
    if not isinstance(history, list) or len(history) > 20:
        raise SideQuestionError(400, "Native side history must contain at most 20 exchanges")
    pairs = []
    for item in history:
        if not isinstance(item, dict) or set(item) != {"question", "response"}:
            raise SideQuestionError(400, "Native side history must contain question/response pairs")
        for text in item.values():
            if not isinstance(text, str) or not text.strip():
                raise SideQuestionError(400, "Native side-history exchanges must contain text")
            try:
                text.encode("utf-8")
            except UnicodeEncodeError:
                raise SideQuestionError(400, "Native side history must contain valid Unicode text") from None
        pairs.append(dict(item))
    request = {"subtype": "side_question", "question": question}
    if pairs:
        request["history"] = pairs
    return request


def _answer(result: Any) -> dict:
    # Provider errors can contain paths, credentials or raw upstream content.
    # They are deliberately not returned to an HTTP caller.
    if isinstance(result, Exception):
        raise SideQuestionError(503, "Claude native side question failed or is unsupported by this CLI")
    if not isinstance(result, dict) or result.get("subtype") != "success":
        raise SideQuestionError(502, "Claude returned an invalid native side-question response")
    payload = result.get("response")
    if not isinstance(payload, dict):
        raise SideQuestionError(502, "Claude returned an invalid native side-question response")
    answer = payload.get("response")
    if payload.get("synthetic") is not False:
        raise SideQuestionError(502, "Claude could not answer this side question from its current context")
    if not isinstance(answer, str) or not answer.strip():
        raise SideQuestionError(502, "Claude returned no native side-question answer")
    try:
        encoded = answer.encode("utf-8")
    except UnicodeEncodeError:
        raise SideQuestionError(502, "Claude returned an invalid native side-question answer") from None
    if len(encoded) > MAX_OUTPUT_BYTES:
        raise SideQuestionError(502, "Claude returned an oversized native side-question answer")
    return {"answer": answer, "context_note": CONTEXT_NOTE}


async def ask_native_side_question(
    client: Any,
    question: str,
    *,
    history: list[dict] | None = None,
) -> dict:
    """Ask native /btw without acquiring the parent's main-turn authority.

    Cancellation sends only this control's opaque ID. The parent's existing SDK
    reader delivers the response into the same event tables it uses for all
    other controls. No second stream reader or main-turn command is created.
    """
    request = _request(question, history)
    query = _native_query(client)
    request_id = f"agentsdock_side_{uuid.uuid4().hex}"
    event = asyncio.Event()
    query.pending_control_responses[request_id] = event
    wire = json.dumps({"type": "control_request", "request_id": request_id, "request": request}) + "\n"
    sending = asyncio.create_task(query.transport.write(wire), name=f"claude-side-send:{request_id}")

    async def exchange() -> Any:
        await asyncio.shield(sending)
        await event.wait()
        return query.pending_control_results.get(request_id)

    receiving = asyncio.create_task(exchange(), name=f"claude-side-result:{request_id}")

    async def cancel_exact_request() -> None:
        # Join the original write before cancellation so even a slow transport
        # cannot deliver our cancellation before the side question exists.
        with suppress(Exception):
            await asyncio.shield(sending)
        if event.is_set():
            return
        await query.transport.write(json.dumps({
            "type": "control_cancel_request", "request_id": request_id,
        }) + "\n")
        await event.wait()

    async def cancel_and_settle() -> None:
        cleanup = asyncio.create_task(cancel_exact_request(), name=f"claude-side-cancel:{request_id}")
        deadline = asyncio.get_running_loop().time() + CANCEL_TIMEOUT_SECONDS
        try:
            while not cleanup.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), remaining)
                except asyncio.CancelledError:
                    # A duplicate disconnect must not interrupt exact-ID cleanup.
                    continue
                except Exception:
                    break
        finally:
            if not cleanup.done():
                cleanup.cancel()
            with suppress(BaseException):
                await cleanup

    try:
        result = await asyncio.shield(receiving)
        return _answer(result)
    except asyncio.CancelledError:
        await cancel_and_settle()
        raise
    except SideQuestionError:
        raise
    except Exception:
        # A transport can report failure after delivering a frame. Attempt the
        # exact native cancellation without changing the main connection.
        await cancel_and_settle()
        raise SideQuestionError(503, "Claude native side-question connection is unavailable") from None
    finally:
        query.pending_control_responses.pop(request_id, None)
        query.pending_control_results.pop(request_id, None)
        for task in (receiving, sending):
            if not task.done():
                task.cancel()
            with suppress(BaseException):
                await task
