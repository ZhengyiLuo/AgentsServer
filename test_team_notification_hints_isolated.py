"""Quiet v2 notifications under the guarded temporary-state test runner."""
from dataclasses import replace
import json
import sqlite3
import threading
import unittest
from unittest import mock

from agentsdock_team_hub.mail_hints import MailArrival, MailHintClosed
from agentsdock_team_hub.notification_hints import (
    BulletinChange, NotificationBroker, NotificationCursor, NotificationLease,
)
from agentsdock_team_hub.store import HubError
from agentsdock_team_hub.database import MIGRATIONS, _statements
import tests.test_team_mail_hints_isolated as fixtures


class NotificationPrimitiveTests(unittest.TestCase):
    def cursor(self, sequence=0):
        return NotificationCursor(MailArrival("team_test", "server_test", 0, None),
            BulletinChange("team_test", sequence, f"bchg_{sequence:032x}" if sequence else None,
                f"tmsg_{sequence:032x}" if sequence else None, "created" if sequence else None,
                1 if sequence else None))

    def test_exact_shape_preserves_independent_reset_flags_and_rejects_leaks(self):
        wire = replace(self.cursor(1), bulletin_reset=True).as_dict()
        self.assertEqual(NotificationCursor.from_dict(wire).as_dict(), wire)
        self.assertFalse(wire["mail"]["reset"])
        self.assertTrue(wire["bulletin"]["reset"])
        self.assertFalse(NotificationCursor.from_dict(wire).as_dict(reset=False)["bulletin"]["reset"])
        for changes in ({"body": "leak"}, {"through_sequence": True}, {"change_id": "bchg_bad"},
                        {"message_id": "another message"}, {"message_version": 0}, {"reset": 1},
                        {"team_id": "team_other"}, {"change_kind": "unknown"}, {"change_kind": ["created"]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                NotificationCursor.from_dict({**wire, "bulletin": {**wire["bulletin"], **changes}})

    def test_bounded_pair_coalesces_without_cross_recipient_mail(self):
        broker = NotificationBroker()
        self.addCleanup(broker.close)
        mine = broker.subscribe("team_test", "server_test")
        mine.seed(self.cursor())
        other = broker.subscribe("team_test", "server_other")
        other.seed(NotificationCursor(MailArrival("team_test", "server_other", 0, None), BulletinChange("team_test")))
        for sequence in range(1, 1001):
            broker.publish_bulletin(self.cursor(sequence).bulletin)
            broker.publish_mail(MailArrival("team_test", "server_test", sequence, f"tmsg_{sequence:032x}"))
        delivered = mine.take(0)
        self.assertEqual(delivered.mail.through_sequence, 1000)
        self.assertEqual(delivered.bulletin.through_sequence, 1000)
        self.assertEqual(other.take(0).mail.through_sequence, 0)
        self.assertIsNone(mine.take(0))
        self.assertIsNone(other.take(0))

    def test_seed_preserves_arrival_during_snapshot_and_drops_already_included(self):
        broker = NotificationBroker()
        self.addCleanup(broker.close)
        sub = broker.subscribe("team_test", "server_test")
        broker.publish_bulletin(self.cursor(2).bulletin)
        sub.seed(self.cursor(1))
        self.assertEqual(sub.take(0).bulletin.through_sequence, 2)
        second = broker.subscribe("team_test", "server_test")
        broker.publish_bulletin(self.cursor(3).bulletin)
        second.seed(self.cursor(3))
        self.assertIsNone(second.take(0))

    def test_conflicting_history_closes_only_affected_team(self):
        broker = NotificationBroker()
        self.addCleanup(broker.close)
        sub = broker.subscribe("team_test", "server_test")
        sub.seed(self.cursor(1))
        other = broker.subscribe("team_other", "server_other")
        other.seed(NotificationCursor(MailArrival("team_other", "server_other", 0, None), BulletinChange("team_other")))
        broker.publish_bulletin(replace(self.cursor(1).bulletin, change_id="bchg_" + "f" * 32))
        self.assertTrue(sub.closed)
        self.assertFalse(other.closed)

    def test_idle_lease_never_queries_authority_and_cancel_wakes(self):
        broker = NotificationBroker()
        self.addCleanup(broker.close)
        sub = broker.subscribe("team_test", "server_test")
        authorize = mock.Mock()
        lease = NotificationLease(sub, self.cursor().as_dict(reset=True), hub_id="hub_test",
            authorize=authorize, expires_at=None)
        self.addCleanup(lease.close)
        done = threading.Event()
        waiter = threading.Thread(target=lambda: (lease.take(), done.set()), daemon=True)
        waiter.start()
        self.assertFalse(done.wait(.02))
        authorize.assert_not_called()
        lease.cancel()
        self.assertTrue(done.wait(1))
        waiter.join(1)
        self.assertFalse(waiter.is_alive())


class NotificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TeamMailArrivalStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.team = self.fixture.store, self.fixture.team
        self.addCleanup(self.store.notification_broker.close)

    def subscribe(self, caller=None):
        sub, snapshot = self.store.subscribe_team_notifications(caller or self.fixture.peer, self.team)
        self.addCleanup(sub.close)
        return sub, snapshot

    def bulletin(self, **extra):
        return self.fixture.send(recipients=[{"kind": "all"}], **extra)

    def edit(self, message, version=1, **extra):
        return self.store.revise_team_message(self.fixture.host, self.team, message["id"], {
            "body": "Updated bulletin body", "body_format": "markdown", "expected_version": version,
            "idempotency_key": "revision-test-key", **extra})

    def test_create_edit_delete_are_distinct_committed_hints_not_mail(self):
        sub, snapshot = self.subscribe()
        legacy, _ = self.fixture.subscribe()
        observed = []
        publish = self.store.notification_broker.publish_bulletin
        def committed(change):
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT change_kind FROM team_bulletin_changes WHERE id=?", (change.change_id,)).fetchone()
                self.assertEqual(row[0], change.change_kind)
                db.rollback()
            observed.append(change)
            publish(change)
        with mock.patch.object(self.store.notification_broker, "publish_bulletin", side_effect=committed):
            message = self.bulletin(title="Title remains", body="Original bulletin body")
            created = sub.take(0)
            self.assertEqual(created.bulletin.change_kind, "created")
            self.assertEqual(created.bulletin.message_id, message["id"])
            self.edit(message)
            edited = sub.take(0)
            self.assertEqual((edited.bulletin.change_kind, edited.bulletin.message_version), ("revised", 2))
            self.fixture.delete(message)
            deleted = sub.take(0)
            self.assertEqual((deleted.bulletin.change_kind, deleted.bulletin.message_version), ("deleted", 2))
        self.assertEqual(len(observed), 3)
        for cursor in (created, edited, deleted):
            self.assertEqual(cursor.mail.as_dict(reset=True), snapshot["mail"])
            self.assertNotIn("body", json.dumps(cursor.as_dict()))
            self.assertNotIn("Title remains", json.dumps(cursor.as_dict()))
        self.assertIsNone(legacy.take(0))

    def test_retry_failed_cas_unauthorized_edit_and_rollback_do_not_notify(self):
        sub, _ = self.subscribe()
        message = self.bulletin(idempotency_key="original-post-key")
        sub.take(0)
        self.assertEqual(self.bulletin(idempotency_key="original-post-key")["id"], message["id"])
        self.assertIsNone(sub.take(0))
        self.edit(message)
        version_two = sub.take(0)
        self.edit(message)
        self.assertIsNone(sub.take(0))
        with self.assertRaises(HubError):
            self.edit(message, idempotency_key="stale-edit-key")
        with self.assertRaises(HubError):
            self.store.revise_team_message(self.fixture.peer, self.team, message["id"], {
                "body": "Other server cannot edit", "expected_version": 2, "idempotency_key": "foreign-edit-key"})
        with mock.patch.object(self.store, "_audit", side_effect=RuntimeError("transaction rollback")):
            with self.assertRaises(RuntimeError):
                self.bulletin()
            with self.assertRaises(RuntimeError):
                self.edit(message, version=2, body="Rolled back revision", idempotency_key="rollback-revision")
            with self.assertRaises(RuntimeError):
                self.fixture.delete(message)
        self.assertIsNone(sub.take(0))
        current = self.store.team_notification_snapshot(self.fixture.peer, self.team)
        self.assertEqual(current["bulletin"], version_two.bulletin.as_dict(reset=True))

    def test_failed_notification_does_not_fail_committed_message_and_reconnect_recovers(self):
        sub, before = self.subscribe()
        with mock.patch.object(self.store.notification_broker, "publish_bulletin", side_effect=RuntimeError("hint failure")):
            message = self.bulletin()
        self.assertTrue(sub.closed)
        after = self.store.team_notification_snapshot(self.fixture.peer, self.team, previous_cursor=before)
        self.assertEqual(after["bulletin"]["message_id"], message["id"])
        self.assertFalse(after["bulletin"]["reset"])
        self.assertEqual(self.store.get_team_message(self.fixture.peer, self.team, message["id"])["message"]["id"], message["id"])

    def test_hint_preparation_failure_cannot_rollback_create_edit_or_delete(self):
        sub, _ = self.subscribe()
        with mock.patch.object(self.store, "_team_bulletin_change", side_effect=ValueError("notification metadata unavailable")):
            message = self.bulletin()
            self.assertEqual(self.edit(message)["message"]["revision"]["version"], 2)
            self.assertTrue(self.fixture.delete(message)["deleted"])
        self.assertTrue(sub.closed)
        snapshot = self.store.team_notification_snapshot(self.fixture.peer, self.team)
        self.assertEqual(snapshot["bulletin"]["change_kind"], "deleted")
        self.assertEqual(snapshot["bulletin"]["message_version"], 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM team_bulletin_changes WHERE message_id=?", (message["id"],)).fetchone()[0], 3)

    def test_mail_recipient_isolation_all_servers_and_skills_have_separate_heads(self):
        sub, _ = self.subscribe()
        other, _ = self.subscribe(self.fixture.other)
        direct = self.fixture.send()
        self.assertEqual(sub.take(0).mail.arrival_id, direct["id"])
        self.assertIsNone(other.take(0))
        all_mail = self.fixture.send(recipients=[{"kind": "all_servers"}])
        self.assertEqual(sub.take(0).mail.arrival_id, all_mail["id"])
        self.assertEqual(other.take(0).mail.arrival_id, all_mail["id"])
        skill = self.bulletin(kind="skill", title="Example", skill={"slug": "example", "summary": "Skill summary", "tags": []})
        for recipient in (sub, other):
            cursor = recipient.take(0)
            self.assertEqual(cursor.bulletin.message_id, skill["id"])
            self.assertEqual(cursor.mail.arrival_id, all_mail["id"])

    def test_independent_restore_anchor_and_foreign_recipient_fencing(self):
        message = self.bulletin()
        self.fixture.send()
        previous = self.store.team_notification_snapshot(self.fixture.peer, self.team)
        restored = {**previous, "bulletin": {**previous["bulletin"], "change_id": "bchg_" + "f" * 32}}
        snapshot = self.store.team_notification_snapshot(self.fixture.peer, self.team, previous_cursor=restored)
        self.assertFalse(snapshot["mail"]["reset"])
        self.assertTrue(snapshot["bulletin"]["reset"])
        self.fixture.delete(message)
        snapshot = self.store.team_notification_snapshot(self.fixture.peer, self.team, previous_cursor=previous)
        self.assertFalse(snapshot["bulletin"]["reset"])
        with self.assertRaises(HubError) as error:
            self.store.team_notification_snapshot(self.fixture.other, self.team, previous_cursor=previous)
        self.assertEqual(error.exception.status_code, 403)

    def test_subscribe_snapshot_race_preserves_post_committed_after_snapshot(self):
        original = self.store.team_notification_snapshot
        calls = 0
        created = []
        def snapshot(*args, **kwargs):
            nonlocal calls
            result = original(*args, **kwargs)
            calls += 1
            if calls == 2:
                created.append(self.bulletin())
            return result
        with mock.patch.object(self.store, "team_notification_snapshot", side_effect=snapshot):
            sub, baseline = self.subscribe()
        self.assertEqual(baseline["bulletin"]["through_sequence"], 0)
        self.assertEqual(sub.take(0).bulletin.message_id, created[0]["id"])

    def test_migration_backfills_old_posts_and_keeps_original_bodies(self):
        message = self.bulletin(body="Original before migration")
        self.edit(message)
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        source = self.store.connect()
        try:
            source.backup(legacy)
        finally:
            source.close()
        # Reconstruct the prior schema in this in-memory copy only.
        for trigger in ("team_bulletin_created", "team_bulletin_revised", "team_bulletin_deleted",
                        "team_bulletin_changes_immutable", "team_bulletin_changes_retained"):
            legacy.execute("DROP TRIGGER " + trigger)
        legacy.execute("DROP TABLE team_bulletin_changes")
        for statement in _statements(next(item.source for item in MIGRATIONS if item.version == 22)):
            legacy.execute(statement)
        self.assertEqual(legacy.execute("SELECT message_id,change_kind,message_version FROM team_bulletin_changes").fetchall(),
                         [(message["id"], "created", 2)])
        self.assertEqual(legacy.execute("SELECT body FROM team_messages WHERE id=?", (message["id"],)).fetchone()[0],
                         "Original before migration")
        self.assertEqual(legacy.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_snapshot_queries_metadata_only_and_journal_is_immutable(self):
        self.bulletin()
        original_connect = self.store.connect
        statements = []
        def traced():
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection
        with mock.patch.object(self.store, "connect", side_effect=traced):
            snapshot = self.store.team_notification_snapshot(self.fixture.peer, self.team)
        self.assertFalse(any("current_body" in sql or "COUNT(" in sql.upper() for sql in statements))
        with self.store.connect() as db:
            plans = db.execute("EXPLAIN QUERY PLAN SELECT * FROM team_bulletin_changes WHERE team_id=? ORDER BY sequence DESC LIMIT 1", (self.team,)).fetchall()
            self.assertTrue(any("team_bulletin_changes_by_team" in row[3] for row in plans))
            for sql in ("UPDATE team_bulletin_changes SET message_version=9", "DELETE FROM team_bulletin_changes"):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)
        self.assertGreater(snapshot["bulletin"]["through_sequence"], 0)
