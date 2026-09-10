"""Passive Mail prerequisites only; run through the guarded temporary-state QA runner."""
from __future__ import annotations

import ast
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from agentsdock_team_hub.database import MIGRATIONS, _statements
from agentsdock_team_hub.mail_hints import (
    MAX_MAIL_SEQUENCE, MailArrival, MailHintBroker, MailHintCapacity, MailHintClosed,
)
from agentsdock_team_hub.security import canonical_json
from agentsdock_team_hub.store import HubError, HubStore


ROOT = Path(__file__).resolve().parent


class MailHintBrokerTests(unittest.TestCase):
    def arrival(self, sequence=1, *, recipient="server_recipient", arrival_id=None):
        return MailArrival("team_example", recipient, sequence,
            arrival_id if arrival_id is not None else f"tmsg_{sequence:032x}")

    def test_cursor_is_bounded_exact_metadata_with_safe_integer_domain(self):
        empty = MailArrival("team_example", "server_recipient", 0, None)
        self.assertEqual(MailArrival.from_dict(empty.as_dict(reset=True)), empty)
        maximum = self.arrival(MAX_MAIL_SEQUENCE)
        self.assertEqual(MailArrival.from_dict(maximum.as_dict()), maximum)
        self.assertEqual(set(maximum.as_dict()), {
            "version", "team_id", "recipient_server_id", "through_sequence", "arrival_id"})
        for changed in (
            {"version": True}, {"version": 2}, {"through_sequence": True},
            {"through_sequence": MAX_MAIL_SEQUENCE + 1}, {"through_sequence": -1},
            {"through_sequence": 0}, {"arrival_id": None}, {"arrival_id": "tmsg_bad"},
            {"arrival_id": "tmsg_" + "a" * 33}, {"arrival_id": "tmsg_" + "a" * 32 + "\n"},
            {"team_id": "a" * 129}, {"recipient_server_id": "../other"},
            {"reset": "true"}, {"body": "never a hint"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                MailArrival.from_dict({**maximum.as_dict(), **changed})

    def test_slow_recipient_coalesces_and_unrelated_waiter_is_never_notified(self):
        broker = MailHintBroker()
        self.addCleanup(broker.close)
        recipient = broker.subscribe("team_example", "server_recipient")
        other = broker.subscribe("team_example", "server_other")
        with mock.patch.object(other._changed, "notify") as notify:
            for sequence in range(1, 1001):
                broker.publish(self.arrival(sequence))
            self.assertEqual(recipient.take(0), self.arrival(1000))
            broker.publish(self.arrival(999))
            broker.publish(self.arrival(1000))
            self.assertIsNone(recipient.take(0))
            self.assertIsNone(other.take(0))
            notify.assert_not_called()
        self.assertEqual(broker._count, 2)
        self.assertIsNone(recipient._pending)

    def test_caps_are_separate_and_close_releases_capacity_and_wakes_reader(self):
        broker = MailHintBroker(max_subscriptions=2, max_per_recipient=1)
        self.addCleanup(broker.close)
        first = broker.subscribe("team_example", "server_recipient")
        with self.assertRaises(MailHintCapacity): broker.subscribe(*first.mailbox)
        other = broker.subscribe("team_example", "server_other")
        with self.assertRaises(MailHintCapacity): broker.subscribe("team_example", "server_third")
        entered = threading.Event()
        finished = threading.Event()
        def read():
            entered.set()
            try:
                first.take()
            except MailHintClosed:
                finished.set()
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        self.assertTrue(entered.wait(1))
        first.close()
        first.close()
        reader.join(1)
        self.assertTrue(finished.is_set())
        self.assertFalse(reader.is_alive())
        broker.subscribe("team_example", "server_third")
        broker.close()
        self.assertTrue(other.closed)
        self.assertEqual(broker._count, 0)
        self.assertEqual(broker._subscriptions, {})
        with self.assertRaises(MailHintClosed): broker.subscribe("team_example", "server_third")

    def test_conflicting_same_sequence_closes_only_exact_recipient(self):
        broker = MailHintBroker()
        self.addCleanup(broker.close)
        recipient = broker.subscribe("team_example", "server_recipient")
        other = broker.subscribe("team_example", "server_other")
        broker.publish(self.arrival())
        broker.publish(self.arrival(arrival_id="tmsg_" + "f" * 32))
        self.assertTrue(recipient.closed)
        self.assertFalse(other.closed)
        with self.assertRaises(MailHintClosed): recipient.take(0)


class TeamMailArrivalStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="team-mail-arrivals-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="mail-hint-host")
        self.addCleanup(self.store.mail_hint_broker.close)
        self.store.bootstrap_managed_network("Mail hint prerequisites")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.peer = self.add_peer("recipient")
        self.other = self.add_peer("other")
        self.address = self.snapshot()["recipient_server_id"]

    def add_peer(self, name):
        peer_id = str(uuid.uuid4())
        identity = "mail-hint-peer-" + name
        self.store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, display_name=name)
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        return self.store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600)

    def payload(self, **extra):
        return {"kind": "message", "body": "Private body must not enter hints",
            "recipients": [{"kind": "server", "id": self.address}],
            "idempotency_key": "hint-" + uuid.uuid4().hex, **extra}

    def send(self, **extra):
        return self.store.create_team_message(self.host, self.team, self.payload(**extra))["message"]

    def snapshot(self, previous=None, *, caller=None):
        return self.store.team_mail_arrival_snapshot(caller or self.peer, self.team, previous_cursor=previous)

    def subscribe(self, *, caller=None):
        subscription, snapshot = self.store.subscribe_team_mail_arrivals(caller or self.peer, self.team)
        self.addCleanup(subscription.close)
        return subscription, snapshot

    def page(self, **kwargs):
        return self.store.list_team_messages(self.peer, self.team,
            **{"box": "inbox", "include_mailbox_coverage": True, **kwargs})

    def dismiss(self, message):
        return self.store.dismiss_team_message(self.peer, self.team, message["id"], {
            "address_kind": "server", "address_id": self.address,
            "idempotency_key": "dismiss-" + uuid.uuid4().hex})

    def delete(self, message):
        return self.store.delete_team_message(self.host, self.team, message["id"],
            {"idempotency_key": "delete-" + uuid.uuid4().hex})

    def test_commit_hook_sees_committed_row_and_released_write_lock_exact_recipient_only(self):
        recipient, _ = self.subscribe()
        other, _ = self.subscribe(caller=self.other)
        original = self.store.mail_hint_broker.publish
        observed = []
        def publish(arrival):
            with closing(self.store.connect()) as db:
                db.execute("BEGIN IMMEDIATE")  # Must not contend with the creation transaction.
                row = db.execute("SELECT id FROM team_messages WHERE queue_ordinal=?",
                    (arrival.through_sequence,)).fetchone()
                observed.append(row["id"])
                db.execute("ROLLBACK")
            original(arrival)
        with mock.patch.object(self.store.mail_hint_broker, "publish", side_effect=publish):
            message = self.send(title="Private subject")
        self.assertEqual(observed, [message["id"]])
        self.assertEqual(recipient.take(0).arrival_id, message["id"])
        self.assertIsNone(other.take(0))
        self.assertEqual(self.snapshot()["through_sequence"], message["sequence"])

    def test_rollback_and_idempotent_replay_do_not_emit_or_advance(self):
        recipient, empty = self.subscribe()
        with mock.patch.object(self.store, "_outbox", side_effect=RuntimeError("rollback after recipients")):
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                self.send()
        self.assertEqual(self.snapshot(empty)["through_sequence"], 0)
        self.assertIsNone(recipient.take(0))
        request = self.payload(recipients=[{"kind": "all_servers"}])
        first = self.store.create_team_message(self.host, self.team, request)
        self.assertIsNotNone(recipient.take(0))
        newcomer = self.add_peer("newcomer")
        self.assertEqual(self.store.create_team_message(self.host, self.team, request), first)
        self.assertIsNone(recipient.take(0))
        self.assertEqual(self.snapshot(caller=newcomer)["through_sequence"], 0)

    def test_failed_publish_retires_subscription_but_durable_mail_succeeds(self):
        recipient, _ = self.subscribe()
        with mock.patch.object(self.store.mail_hint_broker, "publish", side_effect=RuntimeError("writer failed")):
            message = self.send()
        self.assertTrue(recipient.closed)
        replacement, snapshot = self.subscribe()
        self.assertEqual(snapshot["arrival_id"], message["id"])
        self.assertIsNone(replacement.take(0))

    def test_bulletin_and_skills_never_advance_server_arrivals(self):
        recipient, _ = self.subscribe()
        self.send(recipients=[{"kind": "all"}])
        self.send(kind="skill", title="Skill title", skill={"slug": "hint-test"},
            recipients=[{"kind": "all"}])
        self.assertEqual(self.snapshot()["through_sequence"], 0)
        self.assertIsNone(recipient.take(0))

    def test_human_only_mail_never_notifies_or_expands_into_server_mailbox(self):
        human = "human_" + uuid.uuid4().hex
        with closing(self.store.connect()) as db:
            db.execute("""INSERT INTO principals(id,kind,display_name,status,created_at,updated_at)
                VALUES (?,'human','Temporary human recipient','active',1,1)""", (human,))
            db.execute("INSERT INTO human_accounts(principal_id,email_normalized,created_at) VALUES (?,?,1)",
                (human, "hint-recipient@example.invalid"))
            db.execute("""INSERT INTO memberships(id,team_id,principal_id,role,status,created_at,updated_at)
                VALUES (?,?,?,'member','active',1,1)""", ("membership_" + uuid.uuid4().hex, self.team, human))
        recipient, _ = self.subscribe()
        self.send(recipients=[{"kind": "human", "id": human}])
        self.assertEqual(self.snapshot()["through_sequence"], 0)
        self.assertIsNone(recipient.take(0))
        with mock.patch.object(self.store.mail_hint_broker, "publish", wraps=self.store.mail_hint_broker.publish) as publish:
            mixed = self.send(recipients=[{"kind": "server", "id": self.address}, {"kind": "human", "id": human}])
        self.assertEqual(publish.call_count, 1)
        self.assertEqual(recipient.take(0).arrival_id, mixed["id"])
        self.assertEqual(len(self.page()["messages"]), 1)

    def test_deleted_dismissed_and_receipted_mail_retain_anchor_and_complete_empty_coverage(self):
        first, last = self.send(), self.send()
        previous = self.snapshot()
        recipient, _ = self.subscribe()
        self.store.set_team_message_mailbox_state(self.peer, self.team, last["id"], {
            "address_kind": "server", "address_id": self.address, "unread": False,
            "expected_version": 0, "idempotency_key": "read-last-hint"})
        self.dismiss(first)
        self.delete(last)
        self.assertIsNone(recipient.take(0))
        snapshot = self.snapshot(previous)
        self.assertFalse(snapshot["reset"])
        page = self.page()
        self.assertEqual(page["messages"], [])
        self.assertFalse(page["has_more"])
        self.assertEqual(page["mailbox_coverage"]["arrival_id"], last["id"])
        self.assertEqual(page["mailbox_coverage"]["through_sequence"], last["sequence"])

    def test_paged_coverage_never_acknowledges_unreturned_visible_mail(self):
        messages = [self.send() for _ in range(5)]
        self.delete(messages[-1])
        first = self.page(limit=2)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["mailbox_coverage"]["arrival_id"], messages[1]["id"])
        second = self.page(limit=2, after_sequence=messages[1]["sequence"], after_arrival_id=messages[1]["id"])
        self.assertFalse(second["has_more"])
        self.assertEqual([m["id"] for m in second["messages"]], [m["id"] for m in messages[2:4]])
        self.assertEqual(second["mailbox_coverage"]["arrival_id"], messages[4]["id"])

    def test_size_capped_page_coverage_tracks_actual_last_returned_row(self):
        messages = [self.send() for _ in range(3)]
        first_size = len(canonical_json(self.page(limit=1)))
        with mock.patch("agentsdock_team_hub.store.MAX_NETWORK_PAGE_RESPONSE_BYTES", first_size + 10):
            page = self.page(limit=3)
        self.assertEqual(len(page["messages"]), 1)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["mailbox_coverage"]["arrival_id"], messages[0]["id"])
        self.assertEqual(page["next_after_sequence"], messages[0]["sequence"])

    def test_unnegotiated_filtered_or_unproven_prefix_pages_cannot_cover(self):
        first, _ = self.send(), self.send()
        for kwargs in (
            {"include_mailbox_coverage": False}, {"unread": True}, {"box": "feed"}, {"box": "sent"},
            {"from_kind": "human", "from_id": "someone"}, {"since": "2000-01-01T00:00:00Z"},
            {"after_sequence": first["sequence"]},
            {"after_sequence": first["sequence"], "after_arrival_id": "tmsg_" + "f" * 32},
            {"after_sequence": 0, "after_arrival_id": first["id"]},
        ):
            with self.subTest(kwargs=kwargs): self.assertNotIn("mailbox_coverage", self.page(**kwargs))
        other_address = self.snapshot(caller=self.other)["recipient_server_id"]
        with self.assertRaises(HubError): self.page(address_kind="server", address_id=other_address)

    def test_snapshot_anchor_reset_and_exact_caller_scope(self):
        empty = self.snapshot()
        self.assertEqual((empty["through_sequence"], empty["arrival_id"], empty["reset"]), (0, None, True))
        self.assertFalse(self.snapshot(empty)["reset"])
        first = self.send()
        old = self.snapshot(empty)
        self.assertFalse(old["reset"])
        self.send()
        self.assertFalse(self.snapshot(old)["reset"])
        self.assertTrue(self.snapshot({**old, "arrival_id": "tmsg_" + "f" * 32})["reset"])
        self.assertTrue(self.snapshot({**old, "through_sequence": first["sequence"] + 100})["reset"])
        with self.assertRaises(HubError) as foreign:
            self.snapshot(self.snapshot(caller=self.other))
        self.assertEqual(foreign.exception.status_code, 403)
        for changed in ({"through_sequence": MAX_MAIL_SEQUENCE + 1}, {"through_sequence": True}, {"arrival_id": None}):
            with self.assertRaises(HubError) as invalid: self.snapshot({**old, **changed})
            self.assertEqual(invalid.exception.status_code, 422)
        for caller in (replace(self.peer, expires_at=0), replace(self.peer, team_id="team_wrong"),
                       replace(self.peer, scopes=frozenset())):
            with self.assertRaises(HubError): self.snapshot(caller=caller)

    def test_restored_branch_advanced_past_retained_max_still_requires_reset(self):
        self.send()
        with closing(sqlite3.connect(":memory:")) as backup:
            with closing(self.store.connect()) as db: db.backup(backup)
            lost = self.send()
            previous = self.snapshot()
            # Only this temporary test DB is restored. No production restore API
            # or filesystem state is invoked; transport closure models restart.
            self.store.mail_hint_broker.close()
            with closing(self.store.connect()) as db: backup.backup(db)
        self.store.mail_hint_broker = MailHintBroker()
        replacement = self.send()
        latest = self.send()
        self.assertEqual(replacement["sequence"], lost["sequence"])
        self.assertNotEqual(replacement["id"], lost["id"])
        self.assertGreater(latest["sequence"], previous["through_sequence"])
        self.assertTrue(self.snapshot(previous)["reset"])
        self.assertNotIn("mailbox_coverage", self.page(
            after_sequence=lost["sequence"], after_arrival_id=lost["id"]))

    def test_restart_and_subscribe_snapshot_race_use_fresh_durable_reads(self):
        created = self.send()
        restarted = HubStore(self.store.data_dir, managed_host_identity="mail-hint-host")
        self.addCleanup(restarted.mail_hint_broker.close)
        self.assertEqual(restarted.team_mail_arrival_snapshot(self.peer, self.team)["arrival_id"], created["id"])
        original = self.store.mail_hint_broker.subscribe
        raced = []
        def subscribe(*args):
            raced.append(self.send())  # Commit after identity lookup, before subscribe.
            subscription = original(*args)
            raced.append(self.send())  # Commit after subscribe, before fresh snapshot.
            return subscription
        with mock.patch.object(self.store.mail_hint_broker, "subscribe", side_effect=subscribe):
            subscription, snapshot = self.subscribe()
        self.assertEqual(snapshot["arrival_id"], raced[-1]["id"])
        self.assertEqual(subscription.take(0).arrival_id, raced[-1]["id"])

    def test_membership_revoked_between_binding_and_snapshot_releases_subscription(self):
        original = self.store.mail_hint_broker.subscribe
        def subscribe(*args):
            subscription = original(*args)
            self.store.revoke_secure_peer_service(peer_id=self.peer.peer_id, team_id=self.team)
            return subscription
        with mock.patch.object(self.store.mail_hint_broker, "subscribe", side_effect=subscribe):
            with self.assertRaises(HubError): self.subscribe()
        self.assertEqual(self.store.mail_hint_broker._count, 0)

    def test_legacy_projection_backfill_and_point_queries_do_not_scan_mailbox_history(self):
        first, last = self.send(), self.send()
        self.send(recipients=[{"kind": "all"}])
        self.dismiss(last)
        with closing(self.store.connect()) as db:
            # Re-run only migration 20 against this temporary legacy-shaped DB.
            db.execute("DROP TRIGGER team_mail_arrival_on_server_recipient")
            db.execute("DROP TABLE team_mail_arrivals")
            db.execute("DROP INDEX team_mail_server_arrival_lookup")
            for statement in _statements(MIGRATIONS[-1].source): db.execute(statement)
            self.assertEqual(self.store._team_mail_arrival(db, self.team, self.address).arrival_id, last["id"])
            queries = (
                ("SELECT through_sequence,arrival_id FROM team_mail_arrivals WHERE team_id=? AND recipient_node_id=?",
                 (self.team, self.address)),
                ("""SELECT 1 FROM team_messages AS m JOIN team_message_recipients AS r
                    ON r.team_id=m.team_id AND r.message_id=m.id
                    WHERE m.queue_ordinal=? AND m.id=? AND m.team_id=? AND m.kind='message'
                      AND r.recipient_kind='server' AND r.recipient_node_id=?""",
                 (first["sequence"], first["id"], self.team, self.address)),
            )
            for sql, params in queries:
                plan = [str(row["detail"]) for row in db.execute("EXPLAIN QUERY PLAN " + sql, params)]
                self.assertTrue(all("SEARCH " in item for item in plan), plan)
                self.assertFalse(any("SCAN " in item for item in plan), plan)
            db.execute("UPDATE team_mail_arrivals SET through_sequence=? WHERE team_id=? AND recipient_node_id=?",
                (MAX_MAIL_SEQUENCE + 1, self.team, self.address))
        with self.assertRaises(HubError) as unsafe: self.snapshot()
        self.assertEqual(unsafe.exception.code, "mail_cursor_unavailable")

    def test_no_transport_capability_or_existing_route_is_enabled(self):
        capability = self.store.team_messages_capability()
        self.assertNotIn("mailbox_coverage", capability)
        self.assertNotIn("mail_hints", capability)
        for filename in ("service.py", "secure_peer_hub.py", "secure_peer.py"):
            source = (ROOT / "agentsdock_team_hub" / filename).read_text()
            self.assertNotIn("subscribe_team_mail_arrivals", source)
            self.assertNotIn("include_mailbox_coverage", source)
        ast.parse((ROOT / "agentsdock_team_hub/store.py").read_text())


if __name__ == "__main__":
    unittest.main()
