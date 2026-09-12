"""Demand-driven live projections for open shared-chat pages, never a poller."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import secrets

from public_chat_transcript import PublicTranscriptError


@dataclass
class _OpenChat:
    reader: object
    generation: int = 0
    identity: str = field(default_factory=lambda: secrets.token_hex(8))
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiters: set = field(default_factory=set)
    reset_reader: bool = False

    @property
    def revision(self):
        return f"{self.identity}:{self.generation}"


class InteractiveChatLiveState:
    """One bounded incremental reader per recently viewed chat.

    The existing event hub calls notify synchronously. It does only an O(1)
    lookup for unrelated chats and never reads disk, sends a socket, or waits
    for a guest while the owner's event delivery lock is held.
    """

    def __init__(self, reader_factory, session_exists, public_state=None, *, max_cached=16, native_snapshot=None):
        self.reader_factory = reader_factory
        self.session_exists = session_exists
        self.public_state = public_state
        self.native_snapshot = native_snapshot
        self.max_cached = max_cached
        self.entries = OrderedDict()

    def notify(self, session_id, event):
        entry = self.entries.get(session_id)
        if entry is None:
            return
        kind = event.get("type", "")
        if not isinstance(kind, str):
            return
        if self.native_snapshot is None and not (
            kind in {"turn_started", "turn_finished", "turn_stopped", "turn_steered",
                     "assistant_text", "history_imported", "history_reconciled", "session_deleted", "turn_unqueued"}
            or kind.startswith("turn_queue")
            or kind == "reasoning_summary" and event.get("phase") == "commentary"
        ):
            return
        if kind in {"history_reconciled", "history_imported"}:
            # Existing bytes can acquire stricter provenance without changing
            # their size. Rebuild only when a viewer next asks for the text.
            entry.reset_reader = True
            if self.native_snapshot is not None:
                # The native renderer must replace an older history prefix,
                # not merge repaired same-ID events into a stale generation.
                entry.identity = secrets.token_hex(8)
        entry.generation += 1
        for waiter in tuple(entry.waiters):
            if not waiter.done():
                waiter.set_result(True)

    async def load(self, session_id):
        if not self.session_exists(session_id):
            raise PublicTranscriptError("Chat is unavailable")
        entry = self.entries.get(session_id)
        if entry is None:
            if len(self.entries) >= self.max_cached:
                candidate = next((key for key, item in self.entries.items()
                                  if not item.waiters and not item.lock.locked()), None)
                if candidate is None:
                    raise PublicTranscriptError("Shared chat viewers are busy; retry shortly")
                self.entries.pop(candidate)
            entry = _OpenChat(None if self.native_snapshot is not None else self.reader_factory(session_id))
            self.entries[session_id] = entry
        self.entries.move_to_end(session_id)
        async with entry.lock:
            if entry.reset_reader:
                if self.native_snapshot is None:
                    entry.reader = self.reader_factory(session_id)
                entry.reset_reader = False
            # Capture before I/O: a concurrent append then makes wait() return
            # immediately, closing the initial snapshot-to-subscription gap.
            revision = entry.revision
            # Native views need tool/progress/settings/approval updates too;
            # notify still performs no I/O and unopened chats have no entry.
            task = asyncio.create_task(self.native_snapshot(session_id) if self.native_snapshot is not None
                else asyncio.to_thread(entry.reader.load))
            try:
                snapshot = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not release the reader's lock while its thread is alive.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not task.cancelled():
                    task.exception()
                raise
            except Exception:
                self.entries.pop(session_id, None)
                raise
            if not self.session_exists(session_id):
                self.entries.pop(session_id, None)
                raise PublicTranscriptError("Chat is unavailable")
            if self.native_snapshot is not None:
                return {**snapshot, "revision": revision}
            state = self.public_state(session_id)
            return {"messages": [*snapshot["messages"], *state.get("queued", [])],
                    "revision": revision, "busy": state.get("busy") is True}

    async def wait(self, session_id, revision, timeout):
        entry = self.entries.get(session_id)
        if entry is None or entry.revision != revision:
            return True
        waiter = asyncio.get_running_loop().create_future()
        entry.waiters.add(waiter)
        try:
            return await asyncio.wait_for(waiter, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            entry.waiters.discard(waiter)
