"""Recipient-only Mail arrival metadata; no transport, polling, or authority.

Callers must authenticate the exact mailbox before subscribing, then read a
fresh durable snapshot. A subscription owns one replaceable pending cursor,
not a queue of messages. Closing wakes its reader; this module starts no
threads and never calls application callbacks or performs socket/database I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import threading
from typing import Any


MAX_MAIL_SEQUENCE = 9_007_199_254_740_991
MAIL_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
MAIL_ARRIVAL_ID_RE = re.compile(r"tmsg_[0-9a-f]{32}\Z")


def _valid_identity(value: Any) -> bool:
    return isinstance(value, str) and MAIL_IDENTIFIER_RE.fullmatch(value) is not None


@dataclass(frozen=True)
class MailArrival:
    team_id: str
    recipient_server_id: str
    through_sequence: int
    arrival_id: str | None

    def __post_init__(self) -> None:
        if not _valid_identity(self.team_id) or not _valid_identity(self.recipient_server_id):
            raise ValueError("Mail arrival mailbox identity is invalid")
        if type(self.through_sequence) is not int or not 0 <= self.through_sequence <= MAX_MAIL_SEQUENCE:
            raise ValueError("Mail arrival sequence is invalid")
        if self.through_sequence == 0:
            if self.arrival_id is not None:
                raise ValueError("An empty Mail cursor cannot have an arrival ID")
        elif not isinstance(self.arrival_id, str) or MAIL_ARRIVAL_ID_RE.fullmatch(self.arrival_id) is None:
            raise ValueError("Mail arrival ID is invalid")

    @property
    def mailbox(self) -> tuple[str, str]:
        return self.team_id, self.recipient_server_id

    def as_dict(self, *, reset: bool | None = None) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": 1,
            "team_id": self.team_id,
            "recipient_server_id": self.recipient_server_id,
            "through_sequence": self.through_sequence,
            "arrival_id": self.arrival_id,
        }
        if reset is not None:
            if type(reset) is not bool:
                raise ValueError("Mail cursor reset is invalid")
            value["reset"] = reset
        return value

    @classmethod
    def from_dict(cls, value: Any) -> MailArrival:
        required = {"version", "team_id", "recipient_server_id", "through_sequence", "arrival_id"}
        if (
            not isinstance(value, dict)
            or not required <= value.keys()
            or value.keys() - required - {"reset"}
            or type(value.get("version")) is not int
            or value["version"] != 1
            or ("reset" in value and type(value["reset"]) is not bool)
        ):
            raise ValueError("Mail arrival cursor is invalid")
        return cls(value["team_id"], value["recipient_server_id"], value["through_sequence"], value["arrival_id"])


class MailHintClosed(RuntimeError):
    """The stream must detach; reconnect requires a fresh durable snapshot."""


class MailHintCapacity(RuntimeError):
    """A separate bounded Mail subscriber budget is exhausted."""


class MailHintSubscription:
    def __init__(self, broker: MailHintBroker, mailbox: tuple[str, str]) -> None:
        self._broker = broker
        self._changed = threading.Condition(broker._guard)
        self.mailbox = mailbox
        self._pending: MailArrival | None = None
        self._latest: MailArrival | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        with self._broker._guard:
            return self._closed

    def take(self, timeout: float | None = None) -> MailArrival | None:
        """Wait for one coalesced hint (or return None on timeout).

        A future transport owns its reader/writer and cancellation. It must not
        put this lifetime wait in shared interactive HTTP/default-worker slots.
        """
        with self._changed:
            self._changed.wait_for(
                lambda: self._closed or self._pending is not None, timeout=timeout
            )
            if self._closed:
                raise MailHintClosed("Mail hint subscription is closed")
            pending, self._pending = self._pending, None
            return pending

    def close(self) -> None:
        self._broker._close_subscription(self)


class MailHintBroker:
    """Process-local, bounded, nonblocking publication to exact recipients.

    This is deliberately not an authorization registry or a durable event bus.
    Revoke/role/shutdown owners must close their subscriptions. Failed delivery
    must close the transport so its next subscribe reads durable state again.
    """

    def __init__(self, *, max_subscriptions: int = 256, max_per_recipient: int = 4) -> None:
        if type(max_subscriptions) is not int or not 1 <= max_subscriptions <= 4096:
            raise ValueError("Mail subscription limit is invalid")
        if type(max_per_recipient) is not int or not 1 <= max_per_recipient <= 64:
            raise ValueError("Mail recipient subscription limit is invalid")
        self._max_subscriptions = max_subscriptions
        self._max_per_recipient = max_per_recipient
        self._guard = threading.RLock()
        self._subscriptions: dict[tuple[str, str], set[MailHintSubscription]] = {}
        self._count = 0
        self._closed = False

    def subscribe(self, team_id: str, recipient_server_id: str) -> MailHintSubscription:
        # Identity syntax is not authority. Store/transport must bind claims.
        mailbox = MailArrival(team_id, recipient_server_id, 0, None).mailbox
        with self._guard:
            if self._closed:
                raise MailHintClosed("Mail hint broker is closed")
            current = self._subscriptions.get(mailbox, set())
            if self._count >= self._max_subscriptions or len(current) >= self._max_per_recipient:
                raise MailHintCapacity("Mail hint subscription limit reached")
            subscription = MailHintSubscription(self, mailbox)
            self._subscriptions.setdefault(mailbox, set()).add(subscription)
            self._count += 1
            return subscription

    def publish(self, arrival: MailArrival) -> None:
        if not isinstance(arrival, MailArrival):
            raise ValueError("Mail hint must be a validated arrival")
        with self._guard:
            if self._closed:
                return
            for subscription in tuple(self._subscriptions.get(arrival.mailbox, ())):
                previous = subscription._latest
                if previous is not None:
                    if arrival.through_sequence < previous.through_sequence:
                        continue
                    if arrival.through_sequence == previous.through_sequence:
                        if arrival.arrival_id != previous.arrival_id:
                            # Do not choose between incompatible histories.
                            self._close_locked(subscription)
                        continue
                subscription._latest = arrival
                subscription._pending = arrival
                subscription._changed.notify()

    def _close_locked(self, subscription: MailHintSubscription) -> None:
        if subscription._closed:
            return
        subscription._closed = True
        subscription._pending = None
        subscription._latest = None
        current = self._subscriptions.get(subscription.mailbox)
        if current is not None:
            current.discard(subscription)
            if not current:
                del self._subscriptions[subscription.mailbox]
        self._count -= 1
        subscription._changed.notify_all()

    def _close_subscription(self, subscription: MailHintSubscription) -> None:
        with self._guard:
            self._close_locked(subscription)

    def invalidate(self, team_id: str, recipient_server_id: str) -> None:
        """Wake and retire exact recipients after a missed publication/revoke."""
        with self._guard:
            for subscription in tuple(self._subscriptions.get((team_id, recipient_server_id), ())):
                self._close_locked(subscription)

    def close(self) -> None:
        with self._guard:
            self._closed = True
            for current in tuple(self._subscriptions.values()):
                for subscription in tuple(current):
                    self._close_locked(subscription)
