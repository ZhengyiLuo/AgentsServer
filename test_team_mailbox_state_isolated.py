"""Pure temporary Hub/AST checks; execute only through the safe QA runner."""
import ast
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid

from agentsdock_team_hub.store import HubError, HubStore
from agentsdock_team_hub.secure_peer import PeerAuthorization, ProxyRequest, SecurePeerError, sanitize_proxy_request
from agentsdock_team_hub.secure_peer_hub import SecurePeerHubAdapter


ROOT = Path(__file__).resolve().parent


class TeamMailboxStateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="mailbox-state-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name) / "hub", managed_host_identity="mailbox-host")
        self.store.bootstrap_managed_network("Mailbox tests")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.peer = self.add_peer("receiver")
        self.message = self.store.create_team_message(self.host, self.team, {
            "kind": "message", "body": "Exact mail", "recipients": [{"kind": "all_servers"}],
            "idempotency_key": "initial-mailbox-message",
        })["message"]
        self.message_id = self.message["id"]
        self.address = self.detail()["delivery"]["id"]

    def add_peer(self, name):
        peer_id = str(uuid.uuid4())
        identity = "mailbox-peer-" + name
        self.store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, display_name=name)
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        return self.store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600)

    def detail(self, include=True, caller=None):
        return self.store.get_team_message(caller or self.peer, self.team, self.message_id,
            include_mailbox_state=include)["message"]

    def request(self, unread, version, **extra):
        return {"address_kind": "server", "address_id": self.address, "unread": unread,
            "expected_version": version, "idempotency_key": "state-" + uuid.uuid4().hex, **extra}

    def apply(self, unread, version, **extra):
        return self.store.set_team_message_mailbox_state(self.peer, self.team, self.message_id,
            self.request(unread, version, **extra))

    def test_old_projection_and_receipts_stay_historical_through_read_unread_read(self):
        self.assertNotIn("mailbox_state", self.detail(False))
        self.assertTrue(self.detail()["mailbox_state"]["unread"])
        read = self.apply(False, 0)
        history = read["recipients"][0]
        self.assertEqual(history["state"], "read")
        self.assertIsNotNone(history["read_at"])
        self.assertEqual(self.apply(True, 1)["recipients"][0], history)
        self.assertTrue(self.detail()["mailbox_state"]["unread"])
        self.assertEqual(self.detail(False)["delivery"], history)
        self.assertEqual(self.apply(False, 2)["recipients"][0], history)
        self.assertFalse(self.detail()["mailbox_state"]["unread"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM outbox_events WHERE event_type='team.message.read'").fetchone()[0], 1)

    def test_late_legacy_read_cannot_clear_explicit_unread(self):
        self.apply(True, 0)
        receipt = {"state": "read", "address_kind": "server", "address_id": self.address,
            "idempotency_key": "legacy-read-after-unread"}
        for _ in range(2):
            self.store.record_team_message_receipt(self.peer, self.team, self.message_id, receipt)
        detail = self.detail()
        self.assertEqual(detail["delivery"]["state"], "read")
        self.assertEqual(detail["mailbox_state"], {"address_kind": "server", "address_id": self.address, "unread": True, "version": 1})

    def test_optimistic_version_and_idempotency_prevent_out_of_order_changes(self):
        request = self.request(False, 0)
        first = self.store.set_team_message_mailbox_state(self.peer, self.team, self.message_id, request)
        self.apply(True, 1)
        self.assertEqual(self.store.set_team_message_mailbox_state(self.peer, self.team, self.message_id, request), first)
        self.assertTrue(self.detail()["mailbox_state"]["unread"])
        with self.assertRaises(HubError) as conflict:
            self.apply(False, 0)
        self.assertEqual(conflict.exception.code, "mailbox_state_conflict")
        with self.assertRaises(HubError) as changed:
            self.store.set_team_message_mailbox_state(self.peer, self.team, self.message_id, {**request, "unread": True})
        self.assertEqual(changed.exception.code, "idempotency_conflict")

    def test_opt_in_filter_matches_attention_without_changing_old_filter(self):
        self.apply(False, 0)
        self.apply(True, 1)
        for include, count in ((False, 0), (True, 1)):
            result = self.store.list_team_messages(self.peer, self.team, box="inbox", unread=True,
                include_mailbox_state=include)
            self.assertEqual(len(result["messages"]), count)
        sent = self.store.list_team_messages(self.host, self.team, box="sent", include_mailbox_state=True)["messages"][0]
        self.assertNotIn("mailbox_state", sent)
        self.assertTrue(all("mailbox_state" not in row and "inbox_unread" not in row for row in sent["recipients"]))

    def test_state_belongs_to_one_server_copy(self):
        before = self.detail(caller=self.host)["mailbox_state"]
        self.apply(False, 0)
        self.assertEqual(self.detail(caller=self.host)["mailbox_state"], before)
        stranger = self.add_peer("newcomer")
        with self.assertRaises(HubError) as denied:
            self.store.set_team_message_mailbox_state(stranger, self.team, self.message_id, self.request(True, 1))
        self.assertEqual(denied.exception.status_code, 403)

    def test_deleted_and_dismissed_messages_cannot_change_attention(self):
        self.store.dismiss_team_message(self.peer, self.team, self.message_id,
            {"address_kind": "server", "address_id": self.address, "idempotency_key": "dismiss-this-mail"})
        with self.assertRaises(HubError) as dismissed:
            self.apply(False, 0)
        self.assertEqual(dismissed.exception.status_code, 404)
        self.assertNotIn("mailbox_state", self.detail())
        self.store.delete_team_message(self.host, self.team, self.message_id, {"idempotency_key": "delete-this-mail"})
        with self.assertRaises(HubError) as deleted:
            self.apply(False, 0)
        self.assertEqual(deleted.exception.status_code, 404)

    def test_capability_and_invalid_mutations_are_bounded(self):
        self.assertEqual(self.store.health()["capabilities"]["team_mailbox_state_v1"],
            {"available": True, "version": 1, "address_kinds": ["server"]})
        for change in ({"address_kind": "human"}, {"unread": 1}, {"unread": "true"},
                       {"expected_version": True}, {"expected_version": -1}, {"expected_version": 9_007_199_254_740_991}):
            with self.subTest(change=change), self.assertRaises(HubError) as denied:
                self.store.set_team_message_mailbox_state(self.peer, self.team, self.message_id, {**self.request(True, 0), **change})
            self.assertEqual(denied.exception.status_code, 422)
        self.assertEqual(self.detail()["mailbox_state"]["version"], 0)

    def test_service_functions_forward_exact_opt_in_and_mutation_without_importing_service(self):
        tree = ast.parse((ROOT / "agentsdock_team_hub/service.py").read_text())
        for name, method in (("team_messages", "list_team_messages"), ("team_message", "get_team_message"),
                             ("team_message_mailbox_state", "set_team_message_mailbox_state")):
            node = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)
            node.decorator_list = []
            node.returns = None
            for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
                argument.annotation = None
            namespace = {"store": mock.Mock()}
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), str(ROOT / "agentsdock_team_hub/service.py"), "exec"), namespace)
            arguments = {"team_id": self.team, "claims": self.peer}
            if name != "team_messages": arguments["message_id"] = self.message_id
            if name == "team_message_mailbox_state": arguments["body"] = mock.Mock(model_dump=lambda: self.request(True, 0))
            else: arguments["include_mailbox_state"] = True
            namespace[name](**arguments)
            call = getattr(namespace["store"], method).call_args
            if name != "team_message_mailbox_state": self.assertIs(call.kwargs["include_mailbox_state"], True)
            else: self.assertEqual(call.args[2], self.message_id)


class MailboxStateTransportTests(unittest.TestCase):
    def setUp(self):
        self.peer = PeerAuthorization(str(uuid.uuid4()), str(uuid.uuid4()), "mailbox-peer-server", "mailbox-team-001",
            frozenset({"teamspace.read", "teamspace.write"}), "sha256:" + "a" * 64, int(time.time()) + 600, "Mailbox peer")
        self.store = mock.Mock()
        self.adapter = SecurePeerHubAdapter(self.store)
        self.enterContext(mock.patch.object(self.adapter, "_claims", return_value=object()))
        self.base = f"/v1/teams/{self.peer.team_id}/network/messages"

    def request(self, method, path, query="", body=None):
        return sanitize_proxy_request(self.peer, method, path, query,
            (("content-type", "application/json"),) if body is not None else (),
            json.dumps(body).encode() if body is not None else b"")

    def test_owned_projection_and_mutation_cross_both_secure_allowlists(self):
        for path, method in ((self.base, "list_team_messages"), (self.base + "/message-001", "get_team_message")):
            forwarded = getattr(self.store, method)
            forwarded.return_value = {"ok": True}
            result = self.adapter.forward(self.request("GET", path, "include_mailbox_state=true"))
            self.assertEqual(result.status, 200, result.body)
            self.assertIs(forwarded.call_args.kwargs["include_mailbox_state"], True)
        self.store.set_team_message_mailbox_state.return_value = {"ok": True}
        body = {"address_kind": "server", "address_id": "server-mailbox-001", "unread": True,
            "expected_version": 2, "idempotency_key": "exact-state-request"}
        result = self.adapter.forward(self.request("POST", self.base + "/message-001/mailbox-state", body=body))
        self.assertEqual(result.status, 200, result.body)
        self.assertEqual(self.store.set_team_message_mailbox_state.call_args.args[-1], body)

    def test_query_and_body_shapes_fail_closed(self):
        for query in ("include_mailbox_state=yes", "include_mailbox_state=true&include_mailbox_state=false"):
            with self.assertRaises(SecurePeerError): self.request("GET", self.base, query)
        for suffix in ("/message-001/revisions", "/message-001/mailbox-state"):
            with self.assertRaises(SecurePeerError): self.request("GET", self.base + suffix, "include_mailbox_state=true")
        body = {"address_kind": "server", "address_id": "server-mailbox-001", "unread": True,
            "expected_version": 2, "idempotency_key": "exact-state-request"}
        for patch in ({"address_kind": "human"}, {"unread": "true"}, {"expected_version": True}, {"unexpected": 1}):
            result = self.adapter.forward(self.request("POST", self.base + "/message-001/mailbox-state", body={**body, **patch}))
            self.assertEqual(result.status, 422, result.body)
        self.store.set_team_message_mailbox_state.assert_not_called()
        tree = ast.parse((ROOT / "agent_server.py").read_text())
        rules = next(node.value for node in tree.body if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name) and node.target.id == "TEAM_HUB_SERVER_SESSION_ROUTE_RULES")
        self.assertIn("/mailbox-state$", ast.unparse(rules))


if __name__ == "__main__":
    unittest.main()
