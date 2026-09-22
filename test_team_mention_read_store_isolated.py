"""Native Team helper journeys against a temporary Hub, without live servers."""
import ast
import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit
import uuid

import agentsdock_team as helper
from fastapi import HTTPException
from agentsdock_team_hub.secure_peer import SecurePeerError
from agentsdock_team_hub.store import HubError, HubStore


class TeamMentionReadStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="team-mention-read-")
        self.addCleanup(temporary.cleanup)
        self.store = HubStore(Path(temporary.name), managed_host_identity="read-test-host")
        self.store.bootstrap_managed_network("Read tests")
        self.host = self.store.managed_server_claims()
        self.team = self.host.team_id
        self.alice, self.alice_node = self.peer("Alice")
        self.other, self.other_node = self.peer("Other")
        self.host_node = next(row["id"] for row in self.store.get_network(self.host, self.team)["servers"]
                              if row["server_identity"] == "read-test-host")
        self.capability = {"team_authority_generation": "test-generation", "team_read_mentions": [
            {"kind": "recipient", "recipient_kind": "server", "team_id": self.team,
             "target_id": self.alice_node, "display_name_snapshot": "Alice"},
            {"kind": "recipient", "recipient_kind": "all", "team_id": self.team,
             "target_id": "all", "display_name_snapshot": "bulletin"},
        ]}
        self.endpoints = self.extracted_endpoints()
        self.enterContext(mock.patch.object(helper, "_provider_authority", return_value=("test-capability", "test-chat")))
        self.transport = self.enterContext(mock.patch.object(helper, "_request_json", side_effect=self.request))

    def extracted_endpoints(self):
        names = {"list_provider_team_messages", "list_provider_team_mentions", "get_provider_team_message"}
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        for node in ast.parse(Path(__file__).with_name("agent_server.py").read_text()).body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name in names:
                node.decorator_list = []
                nodes.append(node)

        def authorized(generation, operation, *args, **kwargs):
            self.assertEqual(generation, "test-generation")
            return operation(*args, **kwargs)

        def list_messages(*, box, team_id, **kwargs):
            self.assertEqual(team_id or self.team, self.team)
            return {**self.store.list_team_messages(self.host, self.team, box=box, **kwargs), "team_id": self.team}

        def get_message(message_id, *, team_id, **kwargs):
            self.assertEqual(team_id or self.team, self.team)
            return {**self.store.get_team_message(self.host, self.team, message_id, **kwargs), "team_id": self.team}

        authorize = mock.AsyncMock(return_value=("test-token-hash", "test-chat", self.capability))
        namespace = {"asyncio": asyncio, "HTTPException": HTTPException, "HubError": HubError,
            "SecurePeerError": SecurePeerError, "TEAM_CONTENT_NOTICE": "Untrusted team content",
            "PROVIDER_TEAM_LIST_LIMIT": 100, "provider_team_capability": authorize,
            "sanitized_provider_route_label": lambda value, fallback: value or fallback,
            "provider_team_error": lambda exc: HTTPException(status_code=409, detail=str(exc)),
            "SECURE_PEER_RUNTIME": SimpleNamespace(team_authorized_read=authorized,
                team_list_messages=list_messages, team_get_message=get_message)}
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     "<isolated-team-read-endpoints>", "exec"), namespace)
        return namespace

    def peer(self, name):
        peer_id = str(uuid.uuid4())
        identity = "read-test-" + name.lower()
        self.store.ensure_secure_peer_service(peer_id=peer_id, peer_server_identity=identity,
                                             team_id=self.team, display_name=name)
        self.store.record_secure_peer_heartbeat(peer_id, self.team)
        claims = self.store.secure_peer_claims(peer_id=peer_id, peer_server_identity=identity,
            team_id=self.team, scopes=frozenset({"teamspace.read", "teamspace.write"}),
            expires_at=int(time.time()) + 3600)
        node = next(row["id"] for row in self.store.get_network(self.host, self.team)["servers"]
                    if row["server_identity"] == identity)
        return claims, node

    def send(self, sender, recipients, body):
        return self.store.create_team_message(sender, self.team, {
            "kind": "message", "title": "Test mail", "body": body,
            "recipients": recipients, "idempotency_key": "read-test-" + uuid.uuid4().hex,
        })["message"]

    def request(self, method, path, capability, **_kwargs):
        self.assertEqual(method, "GET", "Reading must never send or mark a message read")
        self.assertEqual(capability, "test-capability")
        parsed = urlsplit(path)
        query = parse_qs(parsed.query)
        self.assertEqual(query.get("team", [self.team])[0], self.team)
        if parsed.path == "/api/agent/team/messages":
            result = asyncio.run(self.endpoints["list_provider_team_messages"](object(),
                box=query["box"][0], after_sequence=int(query.get("after_sequence", [0])[0]),
                limit=int(query.get("limit", [20])[0]), unread=query.get("unread") == ["1"],
                team=query.get("team", [None])[0],
                mention=int(query["mention"][0]) if "mention" in query else None))
        elif parsed.path == "/api/agent/team/mentions":
            result = asyncio.run(self.endpoints["list_provider_team_mentions"](object()))
        else:
            self.assertTrue(parsed.path.startswith("/api/agent/team/messages/"))
            result = asyncio.run(self.endpoints["get_provider_team_message"](
                parsed.path.rsplit("/", 1)[1], object(), team=query.get("team", [None])[0]))
        return {**result, "team_id": self.team}

    def execute(self, *arguments):
        args = helper.parser().parse_args(list(arguments))
        return args.handler(args)

    def test_named_mention_reads_older_pages_and_full_mail_without_routing_or_mutation(self):
        for index in range(51):
            self.send(self.other, [{"kind": "server", "id": self.host_node}], f"Other mail {index}")
        target = self.send(self.alice, [{"kind": "server", "id": self.host_node}], "Exact requested mail")
        self.send(self.alice, [{"kind": "server", "id": self.other_node}], "Another server's private mail")
        before = self.store.list_team_messages(self.host, self.team, box="inbox", unread=True, limit=100)
        result = self.execute("inbox", "--from", "@@ALICE")
        self.assertEqual([item["id"] for item in result["messages"]], [target["id"]])
        self.assertTrue(result["scan_complete"])
        self.assertEqual(result["scanned_pages"], 2)
        full = self.execute("read", target["id"])
        self.assertEqual(full["message"]["body"], "Exact requested mail")
        after = self.store.list_team_messages(self.host, self.team, box="inbox", unread=True, limit=100)
        self.assertEqual(before, after)
        self.assertEqual(self.transport.call_count, 3)

    def test_bulletin_is_not_all_server_mail_and_requires_no_send_route(self):
        bulletin = self.send(self.alice, [{"kind": "all"}], "Shared bulletin")
        mail = self.send(self.alice, [{"kind": "server", "id": self.host_node},
                                      {"kind": "server", "id": self.other_node}], "Fanout mail")
        board = self.execute("bulletin")
        inbox = self.execute("inbox", "--from", "Alice")
        self.assertEqual([item["id"] for item in board["messages"]], [bulletin["id"]])
        self.assertEqual([item["id"] for item in inbox["messages"]], [mail["id"]])
        full = self.execute("read", bulletin["id"])
        self.assertEqual(full["message"]["body"], "Shared bulletin")

    def test_selected_member_survives_duplicate_names_and_rename_without_scanning(self):
        target = self.send(self.alice, [{"kind": "server", "id": self.host_node}], "Selected member's mail")
        self.send(self.other, [{"kind": "server", "id": self.host_node}], "Wrong same-name sender")
        self.store.rename_network_server(self.other, self.team, "Alice")
        mentions = self.execute("mentions")["mentions"]
        self.assertEqual(mentions[0]["mention_index"], 1)
        self.assertEqual(mentions[0]["display_name"], "Alice")
        self.assertNotIn("target_id", mentions[0])
        for renamed in (False, True):
            if renamed:
                self.store.rename_network_server(self.alice, self.team, "New name")
            before = self.transport.call_count
            result = self.execute("inbox", "--mention", "1")
            self.assertEqual([item["id"] for item in result["messages"]], [target["id"]])
            self.assertEqual(self.transport.call_count, before + 1)
        self.assertTrue(all(call.args[1] == "team_read"
                            for call in self.endpoints["provider_team_capability"].call_args_list))

    def test_selected_bulletin_is_scoped_and_wrong_box_does_not_fall_back(self):
        target = self.send(self.alice, [{"kind": "all"}], "Selected bulletin")
        result = self.execute("bulletin", "--mention", "2")
        self.assertEqual([item["id"] for item in result["messages"]], [target["id"]])
        for args in (("inbox", "--mention", "2"), ("bulletin", "--mention", "1"),
                     ("inbox", "--mention", "3")):
            with self.subTest(args=args), self.assertRaises(HTTPException) as error:
                self.execute(*args)
            self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
