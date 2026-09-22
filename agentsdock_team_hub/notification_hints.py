"""Bounded v2 Mail/Bulletin metadata; no polling, content reads, or I/O.

Mail keeps its existing recipient-only arrival cursor. Bulletin has a separate
team-scoped immutable change cursor, so edits never impersonate new mail.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import threading
import time
from typing import Any

from .mail_hints import (
    MAIL_IDENTIFIER_RE, MAIL_ARRIVAL_ID_RE, MAX_MAIL_SEQUENCE,
    MailArrival, MailHintCapacity, MailHintClosed,
)
from .mail_hint_streams import MailHintLease

BULLETIN_CHANGE_ID_RE = re.compile(r"bchg_[0-9a-f]{32}\Z")


@dataclass(frozen=True)
class BulletinChange:
    team_id: str
    through_sequence: int = 0
    change_id: str | None = None
    message_id: str | None = None
    change_kind: str | None = None
    message_version: int | None = None

    def __post_init__(self):
        if not isinstance(self.team_id, str) or MAIL_IDENTIFIER_RE.fullmatch(self.team_id) is None:
            raise ValueError("Bulletin team identity is invalid")
        if type(self.through_sequence) is not int or not 0 <= self.through_sequence <= MAX_MAIL_SEQUENCE:
            raise ValueError("Bulletin sequence is invalid")
        if self.through_sequence == 0:
            if any(value is not None for value in (self.change_id, self.message_id, self.change_kind, self.message_version)):
                raise ValueError("Empty Bulletin cursor cannot identify a change")
        elif (
            not isinstance(self.change_id, str) or BULLETIN_CHANGE_ID_RE.fullmatch(self.change_id) is None
            or not isinstance(self.message_id, str) or MAIL_ARRIVAL_ID_RE.fullmatch(self.message_id) is None
            or not isinstance(self.change_kind, str) or self.change_kind not in {"created", "revised", "deleted"}
            or type(self.message_version) is not int or not 1 <= self.message_version <= MAX_MAIL_SEQUENCE
        ):
            raise ValueError("Bulletin change metadata is invalid")

    def as_dict(self, *, reset: bool | None = None) -> dict[str, Any]:
        result = {"version": 1, "team_id": self.team_id, "through_sequence": self.through_sequence,
                  "change_id": self.change_id, "message_id": self.message_id,
                  "change_kind": self.change_kind, "message_version": self.message_version}
        if reset is not None:
            if type(reset) is not bool:
                raise ValueError("Bulletin reset is invalid")
            result["reset"] = reset
        return result

    @classmethod
    def from_dict(cls, value: Any):
        fields = {"version", "team_id", "through_sequence", "change_id", "message_id", "change_kind", "message_version"}
        if (not isinstance(value, dict) or not fields <= value.keys() or value.keys() - fields - {"reset"}
                or type(value["version"]) is not int or value["version"] != 1
                or ("reset" in value and type(value["reset"]) is not bool)):
            raise ValueError("Bulletin cursor is invalid")
        return cls(**{key: value[key] for key in fields - {"version"}})


@dataclass(frozen=True)
class NotificationCursor:
    mail: MailArrival
    bulletin: BulletinChange
    mail_reset: bool = False
    bulletin_reset: bool = False

    def __post_init__(self):
        if (not isinstance(self.mail, MailArrival) or not isinstance(self.bulletin, BulletinChange)
                or self.mail.team_id != self.bulletin.team_id
                or type(self.mail_reset) is not bool or type(self.bulletin_reset) is not bool):
            raise ValueError("Notification cursor binding is invalid")

    @property
    def mailbox(self):
        return self.mail.mailbox

    @property
    def recipient_server_id(self):
        return self.mail.recipient_server_id

    @property
    def team_id(self):
        return self.mail.team_id

    def as_dict(self, *, reset: bool | None = None):
        if reset is not None and type(reset) is not bool:
            raise ValueError("Notification reset is invalid")
        return {"version": 2,
                "mail": self.mail.as_dict(reset=self.mail_reset if reset is None else reset),
                "bulletin": self.bulletin.as_dict(reset=self.bulletin_reset if reset is None else reset)}

    @classmethod
    def from_dict(cls, value: Any):
        if (not isinstance(value, dict) or set(value) != {"version", "mail", "bulletin"}
                or type(value["version"]) is not int or value["version"] != 2):
            raise ValueError("Notification cursor is invalid")
        mail = MailArrival.from_dict(value["mail"])
        bulletin = BulletinChange.from_dict(value["bulletin"])
        if any(type(value[key].get("reset")) is not bool for key in ("mail", "bulletin")):
            raise ValueError("Notification cursors require independent reset flags")
        return cls(mail, bulletin, value["mail"]["reset"], value["bulletin"]["reset"])


class NotificationSubscription:
    def __init__(self, broker, mailbox):
        self._broker, self.mailbox = broker, mailbox
        self._changed = threading.Condition(broker._guard)
        self._mail = self._bulletin = None
        self._seeded = self._pending = self._closed = False

    @property
    def closed(self):
        with self._broker._guard:
            return self._closed

    def _publish(self, field, value):
        previous = getattr(self, field)
        if previous is not None:
            if value.through_sequence < previous.through_sequence:
                return
            if value.through_sequence == previous.through_sequence:
                if value != previous:
                    self._broker._close_locked(self)
                return
        setattr(self, field, value)
        self._pending = True
        self._changed.notify()

    def seed(self, cursor: NotificationCursor):
        if not isinstance(cursor, NotificationCursor) or cursor.mailbox != self.mailbox:
            raise ValueError("Notification subscription snapshot changed mailbox")
        with self._changed:
            if self._closed:
                raise MailHintClosed("Notification subscription is closed")
            if self._seeded:
                return
            raced = False
            for field, value in (("_mail", cursor.mail), ("_bulletin", cursor.bulletin)):
                pending = getattr(self, field)
                if pending is not None and pending.through_sequence == value.through_sequence and pending != value:
                    self._broker._close_locked(self)
                    raise MailHintClosed("Notification snapshot changed history")
                if pending is not None and pending.through_sequence > value.through_sequence:
                    raced = True
                else:
                    setattr(self, field, value)
            self._seeded, self._pending = True, raced
            self._changed.notify()

    def take(self, timeout=None):
        with self._changed:
            self._changed.wait_for(lambda: self._closed or (self._seeded and self._pending), timeout)
            if self._closed:
                raise MailHintClosed("Notification subscription is closed")
            if not self._seeded or not self._pending:
                return None
            self._pending = False
            return NotificationCursor(self._mail, self._bulletin)

    def close(self):
        with self._broker._guard:
            self._broker._close_locked(self)


class NotificationBroker:
    def __init__(self, *, max_subscriptions=256, max_per_recipient=4):
        if (type(max_subscriptions) is not int or not 1 <= max_subscriptions <= 4096
                or type(max_per_recipient) is not int or not 1 <= max_per_recipient <= 64):
            raise ValueError("Notification capacity is invalid")
        self._max_subscriptions, self._max_per_recipient = max_subscriptions, max_per_recipient
        self._guard = threading.RLock()
        self._subscriptions = {}
        self._count = 0
        self._closed = False

    def subscribe(self, team_id, recipient_server_id):
        mailbox = MailArrival(team_id, recipient_server_id, 0, None).mailbox
        with self._guard:
            if self._closed:
                raise MailHintClosed("Notification broker is closed")
            current = self._subscriptions.get(mailbox, set())
            if self._count >= self._max_subscriptions or len(current) >= self._max_per_recipient:
                raise MailHintCapacity("Notification capacity reached")
            sub = NotificationSubscription(self, mailbox)
            self._subscriptions.setdefault(mailbox, set()).add(sub)
            self._count += 1
            return sub

    def publish_mail(self, arrival: MailArrival):
        if not isinstance(arrival, MailArrival):
            raise ValueError("Invalid Mail hint")
        with self._guard:
            for sub in tuple(self._subscriptions.get(arrival.mailbox, ())):
                sub._publish("_mail", arrival)

    def publish_bulletin(self, change: BulletinChange):
        if not isinstance(change, BulletinChange):
            raise ValueError("Invalid Bulletin hint")
        with self._guard:
            for (team_id, _recipient), subscribers in tuple(self._subscriptions.items()):
                if team_id == change.team_id:
                    for sub in tuple(subscribers):
                        sub._publish("_bulletin", change)

    def _close_locked(self, sub):
        if sub._closed:
            return
        sub._closed, sub._pending = True, False
        sub._mail = sub._bulletin = None
        current = self._subscriptions.get(sub.mailbox)
        if current is not None:
            current.discard(sub)
            if not current:
                del self._subscriptions[sub.mailbox]
        self._count -= 1
        sub._changed.notify_all()

    def invalidate(self, team_id, recipient_server_id=None):
        with self._guard:
            for mailbox, subscribers in tuple(self._subscriptions.items()):
                if mailbox[0] == team_id and (recipient_server_id is None or mailbox[1] == recipient_server_id):
                    for sub in tuple(subscribers):
                        self._close_locked(sub)

    def close(self):
        with self._guard:
            self._closed = True
            for subscribers in tuple(self._subscriptions.values()):
                for sub in tuple(subscribers):
                    self._close_locked(sub)


class NotificationLease(MailHintLease):
    """Use the same cancellation/authorization ownership as v1, with two heads."""
    def __init__(self, subscription, snapshot, *, hub_id, authorize, expires_at,
                 on_close=None, clock=time.time, close_code=None):
        cursor = NotificationCursor.from_dict(snapshot)
        if cursor.mailbox != subscription.mailbox or not isinstance(hub_id, str) or not hub_id or len(hub_id) > 240:
            raise ValueError("Notification lease binding is invalid")
        self.snapshot, self.hub_id = cursor.as_dict(), hub_id
        self.recipient_server_id, self.expires_at = cursor.recipient_server_id, expires_at
        self._subscription, self._authorize, self._on_close = subscription, authorize, on_close
        self._clock, self._close_code, self._latest = clock, close_code, cursor
        self._guard, self._writer = threading.RLock(), threading.RLock()
        self._closed, self._aborter = False, None
        subscription.seed(cursor)

    def take(self, timeout=None):
        deadline = None if timeout is None else self._clock() + max(0.0, timeout)
        while not self.closed:
            boundaries = [value for value in (deadline, self.expires_at) if value is not None]
            remaining = max(0.0, min(boundaries) - self._clock()) if boundaries else None
            if remaining == 0:
                self.close()
                return None
            try:
                cursor = self._subscription.take(timeout=remaining)
            except MailHintClosed:
                self.close()
                return None
            if cursor is None:
                self.close()
                return None
            previous = self._latest
            pairs = ((cursor.mail, previous.mail), (cursor.bulletin, previous.bulletin))
            if cursor.mailbox != previous.mailbox or any(
                current.through_sequence < old.through_sequence
                or (current.through_sequence == old.through_sequence and current != old)
                for current, old in pairs
            ):
                self.close()
                return None
            if all(current == old for current, old in pairs):
                continue
            self._latest = cursor
            return cursor
        return None

    def write(self, writer, cursor):
        value = NotificationCursor.from_dict(cursor)
        if value.mailbox != self._subscription.mailbox:
            raise ValueError("Notification write changed mailbox")
        with self._writer:
            self.revalidate()
            writer(value.as_dict())


def owned_notification_snapshot(store, claims, team_id, previous_cursor):
    previous = NotificationCursor.from_dict(previous_cursor) if previous_cursor is not None else None
    if previous is not None and previous.team_id != team_id:
        raise ValueError("Notification cursor belongs to another team")
    owned = store.team_notification_snapshot(claims, team_id)
    retained = previous_cursor
    if previous is not None and previous.recipient_server_id != owned["mail"]["recipient_server_id"]:
        retained = None
    if retained is not None:
        owned = store.team_notification_snapshot(claims, team_id, previous_cursor=retained)
    return owned, retained
