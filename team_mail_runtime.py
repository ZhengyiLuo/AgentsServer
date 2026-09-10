"""Passive Mail stream ownership for one AgentsServer runtime.

This module is dormant unless its owner explicitly enables the next-beta
transport. One Member upstream serves all local subscribers. It owns a bounded
dedicated reader thread, never an interactive executor or request lease. A
failed upstream closes that cohort; client transport reconnects request a fresh
scalar proof. There is no reconnect timer, Inbox read, or background refresh.
"""
from __future__ import annotations

from contextlib import suppress
import threading
from typing import Any

from agentsdock_team_hub.mail_hints import MailArrival, MailHintBroker, MailHintClosed
from agentsdock_team_hub.mail_hint_streams import MailHintLease, MailHintScopeChanged, MailHintUnsupported, owned_mail_snapshot


class _MemberFeed:
    def __init__(self, owner: RuntimeMailHints, realm: dict[str, Any]) -> None:
        self.owner, self.realm = owner, dict(realm)
        self.broker = MailHintBroker(max_subscriptions=16, max_per_recipient=16)
        self.ready = threading.Event()
        self.guard = threading.RLock()
        self.stream = None
        self.snapshot = None
        self.closed = False
        self.close_code = 1012
        self.thread = threading.Thread(target=self._read, name="agentsdock-mail-upstream", daemon=True)

    def _read(self) -> None:
        try:
            stream = self.owner.runtime.client.open_mail_hint_stream(self.realm["connection_id"])
            with self.guard:
                if self.closed:
                    stream.close()
                    return
                self.stream = stream
            first = True
            while True:
                frame = stream.read()
                if frame is None:
                    break
                cursor = MailArrival.from_dict(frame["cursor"])
                if frame["hub_id"] != self.realm["hub_id"] or cursor.team_id != self.realm["team_id"]:
                    raise MailHintClosed("Member Mail stream changed realm")
                if first:
                    if frame["type"] != "snapshot":
                        raise MailHintClosed("Member Mail stream omitted snapshot")
                    with self.guard:
                        self.snapshot = dict(frame["cursor"])
                    self.ready.set()
                    first = False
                elif frame["type"] != "hint" or frame["cursor"].get("reset") is not False:
                    raise MailHintClosed("Member Mail stream changed generation")
                self.broker.publish(cursor)
        except Exception as exc:
            # The local sockets close without serializing peer errors/tokens.
            if getattr(exc, "status_code", None) in (401, 403):
                self.close_code = 4403
            elif getattr(exc, "status_code", None) in (404, 501):
                self.close_code = 4406
            elif isinstance(exc, ValueError) or getattr(exc, "code", None) == "remote_invalid":
                self.close_code = 1008
        finally:
            self.close()

    def close(self) -> None:
        with self.guard:
            self.closed = True
            stream, self.stream = self.stream, None
        self.broker.close()
        self.ready.set()
        if stream is not None:
            with suppress(Exception):
                stream.close()


class RuntimeMailHints:
    def __init__(self, runtime: Any, *, enabled: bool = False) -> None:
        self.runtime = runtime
        self.enabled = enabled is True
        self.guard = threading.RLock()
        self.leases: set[MailHintLease] = set()
        self.pending = 0
        self.member: _MemberFeed | None = None
        self._scope: dict[str, Any] | None = None
        self._generation = 0

    def _member_negotiated(self, realm: dict[str, Any]) -> bool:
        # The client getter is a bounded in-memory authenticated health receipt,
        # not discovery I/O. Missing/expired/other-certificate receipts fail shut.
        getter = getattr(self.runtime.client, "mail_hint_capability", None)
        if not callable(getter):
            return False
        try:
            return getter(realm["connection_id"], realm["certificate_fingerprint"]) is True
        except Exception:
            return False

    def _capture(self, team_id: str | None = None) -> dict[str, Any]:
        if not self.enabled or self.runtime._completion_closing:
            raise MailHintClosed("Mail notifications are unavailable")
        realms = self.runtime.team_realms()
        choices = [realm for realm in realms if team_id is None or realm["team_id"] == team_id]
        if len(choices) != 1:
            raise MailHintScopeChanged("Mail notification realm is unavailable")
        with self.guard:
            generation = self._generation
        realm = {**choices[0], "epoch": self.runtime._team_authority_epoch,
                "generation": generation, "admission_epoch": getattr(self.runtime, "_host_admission_epoch", 0)}
        # Cache only metadata even if capability is not negotiated yet. Later
        # health renders can inspect the receipt without another realm DB read.
        with self.guard:
            if realm["generation"] == self._generation:
                self._scope = realm
        if realm["realm"] == "secure_peer" and not self._member_negotiated(realm):
            raise MailHintUnsupported("Member Mail notifications are not negotiated")
        return realm

    def capability(self) -> dict[str, Any]:
        result = {"enabled": False, "version": 1, "websocket_path": "/api/team-mail-hints/events",
                  "websocket_protocol": "agentsdock.team-mail-hints.v1", "mailbox_coverage": True,
                  "mailbox": None}
        if not self.enabled:
            return result
        try:
            # Metadata discovery is cached, not an arrival query in health.
            with self.guard:
                scope = self._scope
            if scope is None:
                scope = self._capture()
                with self.guard:
                    if scope["generation"] != self._generation:
                        return result
                    self._scope = scope
            if scope["realm"] == "secure_peer" and not self._member_negotiated(scope):
                return result
            result.update(enabled=True, mailbox={"hub_id": scope["hub_id"],
                "team_id": scope["team_id"], "recipient_server_id": None})
        except Exception:
            pass
        return result

    def _authorize(self, realm: dict[str, Any], store=None) -> None:
        runtime = self.runtime
        if (not self.enabled or runtime._completion_closing
                or runtime._team_authority_epoch != realm["epoch"]
                or realm["generation"] != self._generation):
            raise MailHintClosed("Mail notification authority changed")
        if realm["realm"] == "host":
            if (runtime._host_admission_closed or runtime._hub_store is not store
                    or store.hub_id != realm["hub_id"]
                    or runtime._host_admission_epoch != realm["admission_epoch"]):
                raise MailHintClosed("Host Mail admission closed")
        else:
            if runtime._host_role_active:
                raise MailHintClosed("Member Mail authority changed")
            if not self._member_negotiated(realm):
                raise MailHintUnsupported("Member Mail notifications are not negotiated")
            active = runtime._require_active_proxy_connection(realm["connection_id"])
            if (active.get("status") != "connected" or "teamspace.read" not in active.get("scopes", ())
                    or any(active.get(key) != realm.get(key) for key in (
                        "hub_id", "team_id", "host_server_identity", "certificate_fingerprint"))):
                raise MailHintClosed("Member Mail binding changed")

    def subscribe(self, team_id: str, previous_cursor=None) -> MailHintLease:
        realm = self._capture(team_id)
        with self.guard:
            if self.pending + len(self.leases) >= 16:
                raise MailHintClosed("Local Mail stream capacity reached")
            self.pending += 1
        subscription = lease = None
        feed = None
        try:
            if realm["realm"] == "host":
                store = self.runtime._hub_store
                self._authorize(realm, store)
                claims = store.local_agent_mail_claims(team_id)
                _owned, retained = owned_mail_snapshot(store, claims, team_id, previous_cursor)
                subscription, snapshot = store.subscribe_team_mail_arrivals(claims, team_id, previous_cursor=retained)

                def authorize():
                    self._authorize(realm, store)
                    live = store.team_mail_arrival_snapshot(store.local_agent_mail_claims(team_id), team_id)
                    if live["recipient_server_id"] != snapshot["recipient_server_id"]:
                        raise MailHintClosed("Host Mail recipient changed")

                expires_at = None  # Local control is role-owned, not a bearer lease.
            else:
                self._authorize(realm)
                with self.guard:
                    feed = self.member
                    if feed is None or feed.closed:
                        feed = _MemberFeed(self, realm)
                        self.member = feed
                        feed.thread.start()
                    elif feed.realm != realm:
                        raise MailHintClosed("Member Mail generation changed")
                if not feed.ready.wait(timeout=15) or feed.closed or feed.snapshot is None:
                    if feed.close_code == 4406:
                        raise MailHintUnsupported("Member Mail notifications are unsupported")
                    if feed.close_code == 4403:
                        raise MailHintScopeChanged("Member Mail authority is unavailable")
                    if feed.close_code == 1008:
                        raise ValueError("Member Mail protocol is unavailable")
                    raise MailHintClosed("Member Mail stream unavailable")
                # Subscribe BEFORE the per-desktop scalar proof. An upstream
                # cursor alone cannot validate this desktop's retained anchor.
                subscription = feed.broker.subscribe(team_id, feed.snapshot["recipient_server_id"])
                result = self.runtime.client.team_mail_hint_snapshot(realm["connection_id"], previous_cursor)
                snapshot = result["cursor"]
                if result["hub_id"] != realm["hub_id"] or snapshot["recipient_server_id"] != subscription.mailbox[1]:
                    raise MailHintClosed("Member Mail snapshot changed realm")

                def authorize():
                    self._authorize(realm)
                    if feed.closed or self.member is not feed:
                        raise MailHintClosed("Member Mail upstream closed")

                active = self.runtime._require_active_proxy_connection(realm["connection_id"])
                expires_at = float(active["certificate_expires_at"])

            def retired():
                with self.guard:
                    self.leases.discard(lease)
                    last = not self.leases and not self.pending
                    old = self.member if last else None
                    if last:
                        self.member = None
                if old is not None:
                    old.close()

            lease = MailHintLease(subscription, snapshot, hub_id=realm["hub_id"],
                authorize=authorize, expires_at=expires_at, on_close=retired,
                close_code=(lambda: feed.close_code) if feed is not None else None)
            with self.guard:
                self.leases.add(lease)
            lease.revalidate()
            return lease
        except BaseException:
            if lease is not None:
                lease.close()
            elif subscription is not None:
                subscription.close()
            raise
        finally:
            with self.guard:
                self.pending -= 1
                if not self.pending and not self.leases:
                    old, self.member = self.member, None
                else:
                    old = None
            if old is not None:
                old.close()

    def invalidate(self) -> None:
        with self.guard:
            self._generation += 1
            self._scope = None
            leases = tuple(self.leases)
            old, self.member = self.member, None
        if old is not None:
            old.close()
        for lease in leases:
            lease.close()
