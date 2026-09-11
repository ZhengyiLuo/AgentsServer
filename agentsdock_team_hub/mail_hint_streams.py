"""Bounded passive Mail stream leases; no sockets, workers, timers or polling.

The transport owns its dedicated reader and bounded writer. Authority owners
close leases at revoke/role/maintenance boundaries, waking idle readers and
aborting an in-progress write before waiting for that writer to settle.
"""

from __future__ import annotations

from contextlib import suppress
import threading
import time
from typing import Any, Callable

from .mail_hints import MailArrival, MailHintClosed, MailHintSubscription


class MailHintScopeChanged(MailHintClosed):
    status_code = 403


class MailHintUnsupported(MailHintClosed):
    status_code = 501


class MailHintLease:
    def __init__(
        self, subscription: MailHintSubscription, snapshot: dict[str, Any], *,
        hub_id: str, authorize: Callable[[], None], expires_at: float | None,
        on_close: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
        close_code: Callable[[], int] | None = None,
    ) -> None:
        arrival = MailArrival.from_dict(snapshot)
        if type(snapshot.get("reset")) is not bool or arrival.mailbox != subscription.mailbox:
            raise ValueError("Mail stream snapshot is invalid")
        if not isinstance(hub_id, str) or not hub_id or len(hub_id) > 240:
            raise ValueError("Mail stream Hub identity is invalid")
        self.snapshot = dict(snapshot)
        self.hub_id = hub_id
        self.recipient_server_id = arrival.recipient_server_id
        self.expires_at = expires_at
        self._subscription = subscription
        self._authorize = authorize
        self._on_close = on_close
        self._clock = clock
        self._close_code = close_code
        self._latest = arrival
        self._guard = threading.RLock()
        self._writer = threading.RLock()
        self._closed = False
        self._aborter: Callable[[], None] | None = None

    @property
    def closed(self) -> bool:
        with self._guard:
            return self._closed

    @property
    def close_code(self) -> int:
        return self._close_code() if self._close_code is not None else 1012

    def set_aborter(self, aborter: Callable[[], None]) -> None:
        if not callable(aborter):
            raise ValueError("Mail stream aborter is invalid")
        with self._guard:
            self._aborter = aborter
            closed = self._closed
        if closed:
            with suppress(Exception):
                aborter()

    def revalidate(self) -> None:
        with self._guard:
            if self._closed or (
                self.expires_at is not None and self._clock() >= self.expires_at
            ):
                raise MailHintClosed("Mail stream authority is unavailable")
        # No invocation on an idle wait. The authority owner rechecks current
        # exact claims/binding; the gateway additionally rechecks the mTLS cert.
        self._authorize()
        with self._guard:
            if self._closed:
                raise MailHintClosed("Mail stream authority changed")

    def take(self, timeout: float | None = None) -> MailArrival | None:
        deadline = None if timeout is None else self._clock() + max(0.0, timeout)
        while not self.closed:
            now = self._clock()
            boundaries = [value for value in (deadline, self.expires_at) if value is not None]
            remaining = max(0.0, min(boundaries) - now) if boundaries else None
            if remaining == 0:
                self.close()
                return None
            try:
                arrival = self._subscription.take(timeout=remaining)
            except MailHintClosed:
                self.close()
                return None
            if arrival is None:
                self.close()
                return None
            with self._guard:
                if self._closed:
                    return None
                previous = self._latest
                if arrival.mailbox != previous.mailbox or (
                    arrival.through_sequence == previous.through_sequence
                    and arrival.arrival_id != previous.arrival_id
                ):
                    invalid = True
                else:
                    invalid = False
                    if arrival.through_sequence <= previous.through_sequence:
                        continue
                    self._latest = arrival
            if invalid:
                self.close()
                return None
            return arrival
        return None

    def write(self, writer: Callable[[dict[str, Any]], None], cursor: dict[str, Any]) -> None:
        value = MailArrival.from_dict(cursor)
        if value.mailbox != self._subscription.mailbox or type(cursor.get("reset")) is not bool:
            raise ValueError("Mail stream cursor is invalid")
        with self._writer:
            self.revalidate()
            # The transport must bound write time and attach its socket aborter.
            # close() marks closed before aborting, then drains this exact lock.
            writer(dict(cursor))

    def cancel(self) -> None:
        """Wake/abort without waiting for a writer (safe on its event loop)."""
        with self._guard:
            if self._closed:
                first = False
                aborter = None
            else:
                first = True
                self._closed = True
                aborter, self._aborter = self._aborter, None
        if first:
            self._subscription.close()
            if aborter is not None:
                with suppress(Exception):
                    aborter()

    def close(self) -> None:
        self.cancel()
        with self._writer:
            pass
        with self._guard:
            on_close, self._on_close = self._on_close, None
        if on_close is not None:
            on_close()


def owned_mail_snapshot(store: Any, claims: Any, team_id: str,
                        previous_cursor: dict[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Derive ownership before handling a retained cursor from an old binding.

    A changed recipient resets to current owned metadata, never querying the
    old recipient's anchor. A supplied foreign team is still rejected. This is
    transport recovery only; the public store cursor API remains strict.
    """
    previous = MailArrival.from_dict(previous_cursor) if previous_cursor is not None else None
    if previous is not None and previous.team_id != team_id:
        raise ValueError("Mail stream cursor belongs to another team")
    owned = store.team_mail_arrival_snapshot(claims, team_id)
    retained = previous_cursor
    if previous is not None and previous.recipient_server_id != owned["recipient_server_id"]:
        retained = None
    if retained is not None:
        owned = store.team_mail_arrival_snapshot(claims, team_id, previous_cursor=retained)
    return owned, retained
