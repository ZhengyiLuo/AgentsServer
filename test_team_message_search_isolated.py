"""Temporary Hub/AST search checks; execute through the isolated QA runner.

These tests do not import the monolithic server, bind a listener, or contact a
provider. The search index is exercised through the same store as real mail.
"""

from __future__ import annotations

import ast
from contextlib import closing
from dataclasses import replace
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import quote, urlencode
import uuid

from agentsdock_team_hub import database
from agentsdock_team_hub.secure_peer import (
    PeerAuthorization,
    SecurePeerError,
    sanitize_proxy_request,
)
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter
from agentsdock_team_hub.store import HubError, HubStore


ROOT = Path(__file__).resolve().parent


def _key() -> str:
    return "search-" + uuid.uuid4().hex


class TeamMessageSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="team-message-search-")
        self.addCleanup(temporary.cleanup)
        self.data_dir = Path(temporary.name)
        self.store = HubStore(self.data_dir / "hub", managed_host_identity="search-host")
        self.store.bootstrap_managed_network("Search fixture")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.sender = self.add_peer("Original Sender")
        self.recipient = self.add_peer("Recipient")
        self.recipient_id = self.address(self.recipient)

    def add_peer(self, name):
        peer_id = str(uuid.uuid4())
        identity = "search-peer-" + uuid.uuid4().hex
        self.store.ensure_secure_peer_service(
            peer_id=peer_id,
            peer_server_identity=identity,
            team_id=self.team,
            display_name=name,
        )
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        return self.store.secure_peer_claims(
            peer_id=peer_id,
            peer_server_identity=identity,
            team_id=self.team,
            scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600,
        )

    def address(self, caller) -> str:
        return next(
            row["id"]
            for row in self.store.get_network(caller, self.team)["servers"]
            if row["owned_by_caller"]
        )

    def send(self, body, *, caller=None, recipients=None, **extra):
        return self.store.create_team_message(caller or self.sender, self.team, {
            "kind": "message",
            "body": body,
            "recipients": recipients if recipients is not None else [{"kind": "all"}],
            "idempotency_key": _key(),
            **extra,
        })["message"]

    def mail(self, body, **extra):
        return self.send(body, recipients=[{"kind": "server", "id": self.recipient_id}], **extra)

    def search(self, q, *, caller=None, box="feed", **extra):
        return self.store.list_team_messages(
            caller or self.recipient, self.team, box=box, q=q, **extra,
        )

    @staticmethod
    def ids(page):
        return [message["id"] for message in page["messages"]]

    def test_full_current_body_is_searchable_beyond_the_list_preview(self):
        message = self.send("Padding " * 70 + "Nebula telescope")
        page = self.search("nebul telesc")
        self.assertEqual(self.ids(page), [message["id"]])
        self.assertNotIn("Nebula", page["messages"][0]["preview"])
        self.assertNotIn("body", page["messages"][0])
        self.assertEqual(self.search("nonexistentword")["messages"], [])

    def test_search_is_an_explicit_bounded_sibling_capability(self):
        capabilities = self.store.health()["capabilities"]
        self.assertEqual(capabilities["team_message_search_v1"], {
            "available": True, "version": 1,
            "fields": ["subject", "body", "sender"], "max_query_chars": 200,
        })
        self.assertNotIn("search", capabilities["team_messages_v1"])

    def test_subject_and_body_are_all_word_prefix_matches_not_fts_operators(self):
        matching = self.send("Betatron OR equipment", title="Alpha observatory")
        self.send("Alpha alone")
        self.send("Betatron alone")
        self.assertEqual(self.ids(self.search("alp bet", include_mail_subject=True)), [matching["id"]])
        self.assertEqual(self.ids(self.search("alpha OR beta")), [matching["id"]])
        self.assertEqual(self.ids(self.search('"alpha" -betatron')), [matching["id"]])
        self.assertEqual(self.search("*** : () \" -")["messages"], [])

    def test_current_sender_name_follows_rename_without_rewriting_old_messages(self):
        message = self.send("Content without either display label")
        self.assertEqual(self.ids(self.search("orig send")), [message["id"]])
        self.store.rename_network_server(self.sender, self.team, "Renamed Observatory")
        page = self.search("renam obser")
        self.assertEqual(self.ids(page), [message["id"]])
        self.assertEqual(page["messages"][0]["sender"]["display_name"], "Renamed Observatory")
        self.assertEqual(self.search("original sender")["messages"], [])

    def test_current_human_sender_name_uses_principal_index(self):
        human_store = HubStore(self.data_dir / "human")
        bundle = human_store.bootstrap(
            human_store.bootstrap_proof_path.read_text().strip(),
            "search-owner@example.invalid", "Former Human", "Isolated device",
        )
        human = human_store.verify_access(bundle["access_token"])
        team = bundle["teams"][0]["id"]
        message = human_store.create_team_message(human, team, {
            "kind": "message", "body": "Content with unrelated words",
            "recipients": [{"kind": "all"}], "idempotency_key": _key(),
        })["message"]
        self.assertEqual(self.ids(human_store.list_team_messages(human, team, box="feed", q="former hum")), [message["id"]])
        with closing(human_store.connect()) as connection:
            connection.execute("UPDATE principals SET display_name=? WHERE id=?", ("Café Observer", human.principal_id))
        self.assertEqual(self.ids(human_store.list_team_messages(human, team, box="feed", q="café obser")), [message["id"]])
        self.assertEqual(human_store.list_team_messages(human, team, box="feed", q="former human")["messages"], [])

    def test_revisions_remove_old_body_terms_and_index_only_current_body(self):
        message = self.send("Obsoletequasar version", title="Immutable subject")
        revision_requests = []
        for version, body in ((1, "Intermediatepulsar version"), (2, "Currentnebula version")):
            request = {
                "body": body, "expected_version": version, "idempotency_key": _key(),
            }
            revision_requests.append(request)
            self.store.revise_team_message(self.sender, self.team, message["id"], request)
        for old in ("obsoletequasar", "intermediatepulsar"):
            with self.subTest(old=old):
                self.assertEqual(self.search(old)["messages"], [])
        current = self.search("currentneb", include_revision=True)
        self.assertEqual(self.ids(current), [message["id"]])
        self.assertEqual(current["messages"][0]["preview"], "Currentnebula version")
        self.assertNotIn("body", current["messages"][0])
        self.assertEqual(current["messages"][0]["revision"]["version"], 3)
        detail = self.store.get_team_message(self.recipient, self.team, message["id"], include_revision=True)["message"]
        self.assertEqual(detail["body"], "Currentnebula version")
        self.assertEqual(detail["revision"]["version"], 3)
        self.assertEqual(self.ids(self.search("immutable subject")), [message["id"]])
        self.store.delete_team_message(self.sender, self.team, message["id"], {"idempotency_key": _key()})
        # A historical idempotency receipt may be replayed, but must never
        # reinsert old content into the current-content index after deletion.
        try:
            self.store.revise_team_message(self.sender, self.team, message["id"], revision_requests[0])
        except HubError as unavailable:
            self.assertEqual(unavailable.status_code, 404)
        for q in ("obsoletequasar", "intermediatepulsar", "currentneb", "immutable subject"):
            with self.subTest(deleted_query=q):
                self.assertEqual(self.search(q, include_revision=True)["messages"], [])
        with self.assertRaises(HubError) as deleted:
            self.store.get_team_message(self.recipient, self.team, message["id"], include_revision=True)
        self.assertEqual(deleted.exception.status_code, 404)

    def test_failed_message_and_revision_transactions_do_not_leave_search_rows(self):
        with mock.patch.object(self.store, "_outbox", side_effect=RuntimeError("forced search rollback")):
            with self.assertRaisesRegex(RuntimeError, "forced search rollback"):
                self.send("Failedcreationneedle")
        self.assertEqual(self.search("failedcreationneedle")["messages"], [])
        message = self.send("Preservedversionneedle")
        with mock.patch.object(self.store, "_outbox", side_effect=RuntimeError("forced search rollback")):
            with self.assertRaisesRegex(RuntimeError, "forced search rollback"):
                self.store.revise_team_message(self.sender, self.team, message["id"], {
                    "body": "Failedrevisionneedle", "expected_version": 1, "idempotency_key": _key(),
                })
        self.assertEqual(self.search("failedrevisionneedle")["messages"], [])
        self.assertEqual(self.ids(self.search("preservedversionneedle")), [message["id"]])

    def test_deleted_messages_and_dismissed_inbox_copies_are_not_returned(self):
        deleted = self.send("Tombstoneneedle")
        dismissed = self.mail("Dismissedneedle")
        self.store.delete_team_message(self.sender, self.team, deleted["id"], {"idempotency_key": _key()})
        self.store.dismiss_team_message(self.recipient, self.team, dismissed["id"], {
            "address_kind": "server", "address_id": self.recipient_id, "idempotency_key": _key(),
        })
        self.assertEqual(self.search("tombstoneneedle")["messages"], [])
        self.assertEqual(self.search("tombstoneneedle", caller=self.sender, box="sent")["messages"], [])
        self.assertEqual(self.search("dismissedneedle", box="inbox")["messages"], [])
        self.assertEqual(self.ids(self.search("dismissedneedle", caller=self.sender, box="sent")), [dismissed["id"]])

    def test_private_matches_never_escape_recipient_or_sender_authorization(self):
        message = self.mail("Confidentialneedle")
        self.assertEqual(self.ids(self.search("confidentialneedle", box="inbox")), [message["id"]])
        self.assertEqual(self.ids(self.search("confidentialneedle", caller=self.sender, box="sent")), [message["id"]])
        for caller, box in ((self.host, "inbox"), (self.host, "sent"), (self.recipient, "feed"), (self.recipient, "sent")):
            with self.subTest(caller=caller.principal_id, box=box):
                page = self.search("confidentialneedle", caller=caller, box=box)
                self.assertEqual(page["messages"], [])
                self.assertEqual(page["next_after_sequence"], 0)
                self.assertFalse(page["has_more"])
        with self.assertRaises(HubError) as denied:
            self.search("confidentialneedle", caller=self.host, box="inbox",
                        address_kind="server", address_id=self.recipient_id)
        self.assertEqual(denied.exception.status_code, 403)

    def test_keyset_pages_skip_nonmatches_without_leaking_or_skipping_matches(self):
        expected = []
        for number in range(3):
            self.send("Irrelevant filler " + str(number))
            expected.append(self.send("Paginationneedle " + str(number)))
        self.send("Unrelated trailing row")
        cursor = 0
        found = []
        for index, expected_message in enumerate(expected):
            page = self.search("paginationneedle", after_sequence=cursor, limit=1)
            self.assertEqual(self.ids(page), [expected_message["id"]])
            self.assertEqual(page["next_after_sequence"], expected_message["sequence"])
            self.assertEqual(page["has_more"], index < 2)
            found.extend(self.ids(page))
            cursor = page["next_after_sequence"]
        self.assertEqual(found, [message["id"] for message in expected])
        empty = self.search("paginationneedle", after_sequence=cursor, limit=1)
        self.assertEqual(empty["messages"], [])
        self.assertEqual(empty["next_after_sequence"], cursor)
        self.assertFalse(empty["has_more"])

    def test_unread_search_uses_the_existing_opt_in_mailbox_state_contract(self):
        message = self.mail("Unreadneedle")
        request = {
            "address_kind": "server", "address_id": self.recipient_id,
            "unread": False, "expected_version": 0, "idempotency_key": _key(),
        }
        self.store.set_team_message_mailbox_state(self.recipient, self.team, message["id"], request)
        self.store.set_team_message_mailbox_state(self.recipient, self.team, message["id"], {
            **request, "unread": True, "expected_version": 1, "idempotency_key": _key(),
        })
        self.assertEqual(self.search("unreadneedle", box="inbox", unread=True)["messages"], [])
        explicit = self.search("unreadneedle", box="inbox", unread=True, include_mailbox_state=True)
        self.assertEqual(self.ids(explicit), [message["id"]])
        self.assertTrue(explicit["messages"][0]["mailbox_state"]["unread"])

    def test_search_never_claims_mailbox_coverage_and_does_not_mutate_attention(self):
        message = self.mail("Coverageneedle")
        before = self.store.get_team_message(self.recipient, self.team, message["id"], include_mailbox_state=True)
        unfiltered = self.store.list_team_messages(self.recipient, self.team, box="inbox", include_mailbox_coverage=True)
        self.assertIn("mailbox_coverage", unfiltered)
        for q in ("coverageneedle", "absentneedle", "***"):
            with self.subTest(q=q):
                self.assertNotIn("mailbox_coverage", self.search(q, box="inbox", include_mailbox_coverage=True))
        after = self.store.get_team_message(self.recipient, self.team, message["id"], include_mailbox_state=True)
        self.assertEqual(after, before)

    def test_absent_query_preserves_existing_shape_and_query_is_bounded(self):
        message = self.send("Unfilteredneedle")
        old = self.store.list_team_messages(self.recipient, self.team, box="feed")
        self.assertEqual(self.search(None), old)
        self.assertEqual(set(old), {"box", "address", "messages", "next_after_sequence", "has_more"})
        self.assertEqual(self.ids(self.search("  unfilteredneedle  ")), [message["id"]])
        self.assertEqual(self.search("z" * 200)["messages"], [])
        for q in ("", "   ", "x" * 201, 7, True, ["needle"], "needle\x00", "needle\nword", "\ud800"):
            with self.subTest(q=repr(q)), self.assertRaises(HubError) as rejected:
                self.search(q)
            self.assertEqual(rejected.exception.status_code, 422)

    def test_expired_search_work_budget_is_an_error_and_does_not_poison_reads(self):
        expected = [self.send("Deadlinebudgetneedle " + str(index), caller=self.host)["id"] for index in range(30)]
        with mock.patch("agentsdock_team_hub.store.MAX_TEAM_MESSAGE_SEARCH_SECONDS", -1):
            with self.assertRaises(HubError) as exhausted:
                self.search("deadlinebudgetneedle")
        self.assertEqual(exhausted.exception.code, "search_too_broad")
        self.assertEqual(exhausted.exception.status_code, 503)
        self.assertEqual(self.ids(self.search(None)), expected)
        self.assertEqual(self.ids(self.search("deadlinebudgetneedle")), expected)

    def test_missing_index_disables_capability_and_fails_search_without_breaking_normal_reads(self):
        message = self.send("Missingindexneedle")
        with closing(self.store.connect()) as connection:
            connection.execute("DROP TABLE team_message_search")
        self.assertFalse(self.store.health()["capabilities"]["team_message_search_v1"]["available"])
        with self.assertRaises(HubError) as unavailable:
            self.search("missingindexneedle")
        self.assertEqual(unavailable.exception.code, "search_unavailable")
        self.assertEqual(unavailable.exception.status_code, 503)
        self.assertEqual(self.ids(self.search(None)), [message["id"]])

    def test_hidden_legacy_title_and_provenance_are_not_search_fields(self):
        message = self.send("Visible body", provenance={"via": "agent", "chat_id": "Provenanceneedle"})
        with closing(self.store.connect()) as connection:
            # Model a title written by a historical schema. Modern message
            # subjects live in mail_subject and never expose this old column.
            connection.execute("DROP TRIGGER team_messages_are_immutable")
            connection.execute("UPDATE team_messages SET title=? WHERE id=?", ("Legacytitleneedle", message["id"]))
        for q in ("legacytitleneedle", "provenanceneedle"):
            with self.subTest(q=q):
                self.assertEqual(self.search(q)["messages"], [])
        self.assertEqual(self.ids(self.search("visible body")), [message["id"]])

    def test_migration_backfills_current_bodies_and_safe_subjects_from_schema_22(self):
        prefix = tuple(migration for migration in database.MIGRATIONS if migration.version <= 22)
        self.assertEqual(prefix[-1].version, 22)
        with mock.patch.object(database, "MIGRATIONS", prefix), mock.patch.object(database, "LATEST_SCHEMA_VERSION", 22):
            old = HubStore(self.data_dir / "legacy", managed_host_identity="legacy-search-host")
            old.bootstrap_managed_network("Legacy search fixture")
            host = old.managed_server_claims()
            message = old.create_team_message(host, host.team_id, {
                "kind": "message", "body": "Oldbackfillneedle", "title": "Backfillsubject",
                "recipients": [{"kind": "all"}], "idempotency_key": _key(),
            })["message"]
            old.revise_team_message(host, host.team_id, message["id"], {
                "body": "Currentbackfillneedle", "expected_version": 1, "idempotency_key": _key(),
            })
            with closing(old.connect()) as connection:
                connection.execute("DROP TRIGGER team_messages_are_immutable")
                connection.execute("UPDATE team_messages SET title=? WHERE id=?", ("Hiddenbackfillneedle", message["id"]))
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 22)
        reopened = HubStore(self.data_dir / "legacy", managed_host_identity="legacy-search-host")
        for q in ("currentbackfillneedle", "backfillsubject"):
            self.assertEqual(self.ids(reopened.list_team_messages(host, host.team_id, box="feed", q=q)), [message["id"]])
        for q in ("oldbackfillneedle", "hiddenbackfillneedle"):
            self.assertEqual(reopened.list_team_messages(host, host.team_id, box="feed", q=q)["messages"], [])
        with closing(reopened.connect()) as connection:
            self.assertGreaterEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_failed_search_migration_rolls_back_exact_schema_22_and_retry_succeeds(self):
        migrations = database.MIGRATIONS
        self.assertEqual(migrations[-1].version, 23)
        prefix = migrations[:-1]
        with mock.patch.object(database, "MIGRATIONS", prefix), mock.patch.object(database, "LATEST_SCHEMA_VERSION", 22):
            old = HubStore(self.data_dir / "rollback", managed_host_identity="rollback-search-host")
            old.bootstrap_managed_network("Migration rollback fixture")
            host = old.managed_server_claims()
            message = old.create_team_message(host, host.team_id, {
                "kind": "message", "body": "Oldrollbackneedle", "title": "Rollback subject",
                "recipients": [{"kind": "all"}], "idempotency_key": _key(),
            })["message"]
            old.revise_team_message(host, host.team_id, message["id"], {
                "body": "Currentrollbackneedle", "expected_version": 1, "idempotency_key": _key(),
            })
            connection = old.connect()
        with closing(connection):
            schema_sql = "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
            ledger_sql = "SELECT * FROM schema_migrations ORDER BY version"
            message_sql = "SELECT * FROM team_messages ORDER BY queue_ordinal"
            revision_sql = "SELECT * FROM team_message_revisions ORDER BY sequence"
            before = {
                query: [tuple(row) for row in connection.execute(query)]
                for query in (schema_sql, ledger_sql, message_sql, revision_sql)
            }
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 22)
            # Fail after the FTS tables, backfill, indexes and triggers were
            # created, but before the migration ledger can commit version 23.
            broken_source = migrations[-1].source + "\nSELECT * FROM deliberately_missing_search_migration_table;\n"
            broken = replace(migrations[-1], source=broken_source,
                             sha256=hashlib.sha256(broken_source.encode()).hexdigest())
            with mock.patch.object(database, "MIGRATIONS", (*prefix, broken)):
                with self.assertRaisesRegex(sqlite3.OperationalError, "deliberately_missing_search_migration_table"):
                    database.apply_migrations(connection)
            self.assertFalse(connection.in_transaction)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 22)
            for query, expected in before.items():
                with self.subTest(snapshot=query):
                    self.assertEqual([tuple(row) for row in connection.execute(query)], expected)
            self.assertEqual(connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'team_message_search%' "
                "OR name LIKE 'team_message_sender_%'"
            ).fetchall(), [])
            self.assertEqual(database.apply_migrations(connection), 23)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(self.ids(old.list_team_messages(host, host.team_id, box="feed", q="currentrollbackneedle")), [message["id"]])
        self.assertEqual(old.list_team_messages(host, host.team_id, box="feed", q="oldrollbackneedle")["messages"], [])


class TeamMessageSearchTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.peer = PeerAuthorization(
            str(uuid.uuid4()), str(uuid.uuid4()), "search-peer-server", "search-team-001",
            frozenset({"teamspace.read", "teamspace.write"}), "sha256:" + "a" * 64,
            int(time.time()) + 600, "Search peer",
        )
        self.store = mock.Mock()
        self.store.list_team_messages.return_value = {"messages": []}
        self.adapter = SecurePeerHubAdapter(self.store)
        self.enterContext(mock.patch.object(self.adapter, "_claims", return_value=object()))
        self.base = f"/v1/teams/{self.peer.team_id}/network/messages"

    def request(self, query, *, path=None, method="GET"):
        return sanitize_proxy_request(self.peer, method, path or self.base, query, (), b"")

    def test_query_crosses_both_peer_allowlists_without_reinterpretation(self):
        self.store.list_team_messages.return_value = {"messages": []}
        q = '"Alpha" OR café *'
        response = self.adapter.forward(self.request(urlencode({"box": "feed", "q": q})))
        self.assertEqual(response.status, 200, response.body)
        self.assertEqual(self.store.list_team_messages.call_args.kwargs["q"], q)
        self.assertEqual(self.store.list_team_messages.call_args.kwargs["box"], "feed")

    def test_query_bounds_duplicates_and_non_list_routes_fail_closed(self):
        for query in ("q=a&q=b", urlencode({"q": "x" * 201}), urlencode({"q": "needle\x00"})):
            with self.subTest(query=query), self.assertRaises(SecurePeerError):
                self.request(query)
        for suffix in ("/message-001", "/message-001/revisions", "/message-001/thread"):
            with self.subTest(suffix=suffix), self.assertRaises(SecurePeerError):
                self.request("q=needle", path=self.base + suffix)
        with self.assertRaises(SecurePeerError):
            self.request("q=needle", method="POST")
        self.store.list_team_messages.assert_not_called()

    def test_adapter_independently_rejects_malformed_search_queries(self):
        request = self.request("box=feed")
        for query in ("q=a&q=b", urlencode({"q": "x" * 201}), urlencode({"q": "needle\x00"}), "q="):
            with self.subTest(query=query):
                response = self.adapter.forward(replace(request, query=query))
                self.assertEqual(response.status, 422, response.body)
        self.store.list_team_messages.assert_not_called()

    def test_service_function_forwards_query_without_importing_service(self):
        path = ROOT / "agentsdock_team_hub/service.py"
        tree = ast.parse(path.read_text())
        node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "team_messages")
        node.decorator_list = []
        node.returns = None
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            argument.annotation = None
        namespace = {"store": self.store}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), "exec"), namespace)
        namespace["team_messages"](team_id=self.peer.team_id, claims=object(), box="feed", q="exact query")
        self.assertEqual(self.store.list_team_messages.call_args.kwargs["q"], "exact query")

    def test_local_host_function_forwards_query_without_importing_runtime(self):
        path = ROOT / "secure_peer_runtime.py"
        tree = ast.parse(path.read_text())
        node = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "_team_host_call_admitted")
        node.decorator_list = []
        node.returns = None
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            argument.annotation = None
        namespace = {"quote": quote, "SecurePeerError": SecurePeerError}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(path), "exec"), namespace)
        claims = object()
        self.store.local_agent_mail_claims.return_value = claims
        for query in ({"box": "feed", "q": '"Exact" café *'}, {"box": "feed"}):
            with self.subTest(query=query):
                self.store.list_team_messages.reset_mock()
                result = namespace["_team_host_call_admitted"](
                    object(), self.store, {"team_id": self.peer.team_id}, "GET", self.base, query, None,
                )
                self.assertEqual(result, {"messages": []})
                self.store.list_team_messages.assert_called_once()
                call = self.store.list_team_messages.call_args
                self.assertEqual(call.args, (claims, self.peer.team_id))
                self.assertEqual(call.kwargs["box"], "feed")
                self.assertEqual(call.kwargs["q"], query.get("q"))


if __name__ == "__main__":
    unittest.main()
