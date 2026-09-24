"""Pure isolated Hub storage regressions; never import the live server."""
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
import uuid

from agentsdock_team_hub.store import HubError, HubStore


class TeamMailSubjectStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mail-subject-store-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="subject-host")
        self.store.bootstrap_managed_network("Subject tests")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id

    def peer(self, name):
        peer_id = str(uuid.uuid4())
        identity = "subject-peer-" + name
        self.store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, display_name=name)
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        return self.store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600)

    def payload(self, **extra):
        return {"kind": "message", "body": "Unchanged body", "recipients": [{"kind": "all"}],
            "idempotency_key": "subject-" + uuid.uuid4().hex, **extra}

    def create(self, **extra):
        return self.store.create_team_message(self.host, self.team, self.payload(**extra))["message"]

    def test_capability_is_separate_and_exact(self):
        caps = self.store.health()["capabilities"]
        self.assertEqual(caps["team_mail_subjects_v1"], {
            "available": True, "version": 1, "max_subject_chars": 160})
        self.assertNotIn("mail_subjects", caps["team_messages_v1"])

    def test_opt_in_create_detail_feed_and_sent_preserve_codepoints(self):
        subject = "Café  e\u0301 📨"
        created = self.create(title="  " + subject + "  ")
        self.assertEqual(created["title"], subject)
        for include in (False, True):
            expected = subject if include else None
            detail = self.store.get_team_message(self.host, self.team, created["id"], include_mail_subject=include)["message"]
            self.assertEqual(detail["title"], expected)
            for box in ("feed", "sent"):
                rows = self.store.list_team_messages(self.host, self.team, box=box, include_mail_subject=include)["messages"]
                self.assertEqual(rows[0]["title"], expected)
                self.assertEqual(rows[0]["id"], created["id"])
        with self.store.connect() as db:
            row = db.execute("SELECT title,mail_subject FROM team_messages WHERE id=?", (created["id"],)).fetchone()
            self.assertIsNone(row["title"])
            self.assertEqual(row["mail_subject"], subject)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE team_messages SET mail_subject='changed' WHERE id=?", (created["id"],))

    def test_subject_validation_bounds_and_invalid_unicode(self):
        self.assertEqual(self.create(title=" " + "📨" * 160 + " ")["title"], "📨" * 160)
        for subject in ("", "  ", "x" * 161, "a\nb", "a\rb", "a\tb", "a\0b", "a\x7fb",
                        "a\x85b", "a\u2028b", "a\u2029b", "\ud800", 123, True, []):
            with self.subTest(subject=repr(subject)), self.assertRaises(HubError) as caught:
                self.create(title=subject)
            self.assertEqual(caught.exception.code, "invalid_request")

    def test_subject_is_part_of_idempotency_but_outer_space_is_not(self):
        request = self.payload(title="  Subject  ")
        first = self.store.create_team_message(self.host, self.team, request)
        self.assertEqual(first, self.store.create_team_message(self.host, self.team, {**request, "title": "Subject"}))
        for title in (None, "Different", "Subject ́"):
            with self.subTest(title=title), self.assertRaises(HubError) as caught:
                self.store.create_team_message(self.host, self.team, {**request, "title": title})
            self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_untitled_and_legacy_title_never_become_subjects(self):
        created = self.create()
        self.assertIsNone(created["title"])
        with self.store.connect() as db:
            db.execute("DROP TRIGGER team_messages_are_immutable")
            db.execute("UPDATE team_messages SET title='legacy corrupt title' WHERE id=?", (created["id"],))
        for include in (False, True):
            self.assertIsNone(self.store.get_team_message(self.host, self.team, created["id"], include_mail_subject=include)["message"]["title"])
            self.assertIsNone(self.store.list_team_messages(self.host, self.team, box="feed", include_mail_subject=include)["messages"][0]["title"])

    def test_all_servers_subject_does_not_change_frozen_delivery_or_visibility(self):
        peer = self.peer("offline")
        request = self.payload(title="All servers", recipients=[{"kind": "all_servers"}])
        created = self.store.create_team_message(self.host, self.team, request)["message"]
        self.assertEqual(created["destination"], "all_servers")
        for caller in (self.host, peer):
            old = self.store.list_team_messages(caller, self.team, box="inbox")["messages"]
            new = self.store.list_team_messages(caller, self.team, box="inbox", include_mail_subject=True)["messages"]
            self.assertIsNone(old[0]["title"])
            self.assertEqual(new[0]["title"], "All servers")
            self.assertEqual(old[0]["recipients"], new[0]["recipients"])
        newcomer = self.peer("newcomer")
        self.assertEqual(created, self.store.create_team_message(self.host, self.team, request)["message"])
        self.assertEqual(self.store.list_team_messages(newcomer, self.team, box="inbox", include_mail_subject=True)["messages"], [])
        with self.assertRaises(HubError) as caught:
            self.store.get_team_message(newcomer, self.team, created["id"], include_mail_subject=True)
        self.assertEqual(caught.exception.status_code, 404)

    def test_direct_mail_subject_preserves_exact_recipient_visibility(self):
        peer = self.peer("recipient")
        stranger = self.peer("stranger")
        roster = self.store.get_network(self.host, self.team)["servers"]
        node = next(row["id"] for row in roster if row["display_name"] == "recipient")
        created = self.create(title="Private subject", recipients=[{"kind": "server", "id": node}])
        self.assertEqual(self.store.get_team_message(peer, self.team, created["id"], include_mail_subject=True)["message"]["title"], "Private subject")
        with self.assertRaises(HubError) as caught:
            self.store.get_team_message(stranger, self.team, created["id"], include_mail_subject=True)
        self.assertEqual(caught.exception.status_code, 404)

    def test_revision_retry_negotiates_immutable_subject_without_new_history_fields(self):
        created = self.create(title="Kept subject")
        request = {"body": "Revised body", "body_format": "plain", "expected_version": 1,
            "idempotency_key": "subject-body-revision"}
        for include in (True, False, True):
            result = self.store.revise_team_message(self.host, self.team, created["id"], request,
                include_mail_subject=include)["message"]
            self.assertEqual(result["title"], "Kept subject" if include else None)
            self.assertEqual(result["revision"]["version"], 2)
        history = self.store.list_team_message_revisions(self.host, self.team, created["id"], version=1)
        self.assertEqual(set(history), {"message_id", "versions"})
        self.assertNotIn("title", history["versions"][0])
        self.assertEqual(history["versions"][0]["body"], "Unchanged body")

    def test_skill_title_and_version_semantics_are_unchanged(self):
        created = self.create(kind="skill", title="Skill\n title", skill={"slug": "subject-test"})
        self.assertEqual(created["title"], "Skill title")
        for include in (True, False):
            detail = self.store.get_team_message(self.host, self.team, created["id"], include_mail_subject=include)["message"]
            self.assertEqual(detail["title"], "Skill title")
            self.assertEqual(detail["skill"]["version"], 1)
