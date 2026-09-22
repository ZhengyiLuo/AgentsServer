"""AST-only Team mention scopes: no server import, startup, threads, or I/O."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unicodedata
import unittest
from unittest import mock
from urllib.parse import quote


ROOT = Path(__file__).parent


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


class SecurePeerError(Exception):
    def __init__(self, code, message, status_code):
        self.code, self.message, self.status_code = code, message, status_code
        super().__init__(message)


class HubError(SecurePeerError):
    pass


def extracted_endpoints():
    names = {"list_provider_team_mentions", "list_provider_team_messages", "provider_team_error",
             "sanitized_provider_route_label"}
    constants = {"TEAM_CONTENT_NOTICE", "PROVIDER_TEAM_LIST_LIMIT"}
    tree = ast.parse((ROOT / "agent_server.py").read_text())
    nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node = deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in constants for target in node.targets
        ):
            nodes.append(node)
    namespace = {"HTTPException": HTTPException, "HubError": HubError, "SecurePeerError": SecurePeerError,
                 "unicodedata": unicodedata}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "<isolated-team-mention-endpoints>", "exec"), namespace)
    return namespace


def extracted_runtime():
    tree = ast.parse((ROOT / "secure_peer_runtime.py").read_text())
    original = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SecurePeerRuntime")
    selected = ast.ClassDef(name="Runtime", bases=[], keywords=[], decorator_list=[], body=[
        deepcopy(node) for node in original.body
        if isinstance(node, ast.FunctionDef) and node.name == "team_list_messages"
    ])
    prefix = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    namespace = {"quote": quote, "SecurePeerError": SecurePeerError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[prefix, selected], type_ignores=[])),
                 "<isolated-team-mention-runtime>", "exec"), namespace)
    return namespace["Runtime"]


def reference(team_id, target_id, name="Pat", recipient_kind="server", **extra):
    return {
        "kind": "recipient", "recipient_kind": recipient_kind,
        "team_id": team_id, "target_id": target_id,
        "display_name_snapshot": name, "grant_intent": True,
        "private_token": "never-project-reference-token", **extra,
    }


class TeamMentionScopeEndpointTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.extracted = extracted_endpoints()

    def setUp(self):
        self.namespace = self.extracted
        self.references = [
            reference("private-team-a", "private-node-one"),
            reference("private-team-a", "private-node-two"),
            reference("private-team-b", "private-node-three"),
            reference("private-team-b", "all", "bulletin", "all"),
        ]
        self.capability = {
            "team_read_mentions": self.references,
            "team_authority_generation": "private-authority-generation",
            "actions": {"team_read"},
            "source_session_id": "private-source-session",
            "token": "never-project-capability-token",
            "team_send_routes": {"private-send-route": {"target_id": "private-node-one"}},
        }
        self.request = object()
        self.authorize = mock.AsyncMock(return_value=("private-token-hash", "private-source-session", self.capability))
        self.messages = [
            {"id": "message-one", "team_id": "private-team-a", "box": "inbox",
             "sender": {"kind": "server", "id": "private-node-one", "display_name": "Pat"}},
            {"id": "message-two", "team_id": "private-team-a", "box": "inbox",
             "sender": {"kind": "server", "id": "private-node-two", "display_name": "Pat"}},
            {"id": "message-three", "team_id": "private-team-b", "box": "inbox",
             "sender": {"kind": "server", "id": "private-node-three", "display_name": "Pat"}},
            {"id": "bulletin-b", "team_id": "private-team-b", "box": "feed",
             "sender": {"kind": "server", "id": "private-node-four", "display_name": "Other"}},
        ]
        self.runtime = mock.Mock()
        self.runtime.team_list_messages.side_effect = self.filtered_messages
        self.runtime.team_authorized_read.side_effect = self.authorized_read
        self.to_thread = mock.AsyncMock(side_effect=lambda function, *args, **kwargs: function(*args, **kwargs))
        self.namespace.update(
            provider_team_capability=self.authorize,
            SECURE_PEER_RUNTIME=self.runtime,
            asyncio=SimpleNamespace(to_thread=self.to_thread),
        )

    def authorized_read(self, generation, function, *args, **kwargs):
        self.assertEqual(generation, "private-authority-generation")
        self.assertIs(function, self.runtime.team_list_messages)
        return function(*args, **kwargs)

    def filtered_messages(self, **kwargs):
        messages = [deepcopy(item) for item in self.messages
                    if item["team_id"] == kwargs["team_id"] and item["box"] == kwargs["box"]]
        if kwargs.get("from_kind") is not None:
            messages = [item for item in messages if (item["sender"]["kind"], item["sender"]["id"])
                        == (kwargs["from_kind"], kwargs["from_id"])]
        return {"messages": messages, "team_id": kwargs["team_id"]}

    async def read(self, **kwargs):
        return await self.namespace["list_provider_team_messages"](self.request, **kwargs)

    async def mentions(self):
        return await self.namespace["list_provider_team_mentions"](self.request)

    def assert_no_runtime(self):
        self.assertEqual(self.runtime.mock_calls, [])
        self.to_thread.assert_not_awaited()

    async def test_duplicate_display_names_select_only_exact_node_identity(self):
        for index, expected in ((1, "message-one"), (2, "message-two")):
            with self.subTest(mention=index):
                result = await self.read(mention=index)
                self.assertEqual([item["id"] for item in result["messages"]], [expected])
                call = self.runtime.team_authorized_read.call_args
                self.assertEqual(call.kwargs["from_kind"], "server")
                self.assertEqual(call.kwargs["from_id"], self.references[index - 1]["target_id"])
                self.authorize.assert_awaited_with(self.request, "team_read")

    async def test_sender_rename_does_not_change_selected_snapshot_identity(self):
        self.messages[0]["sender"]["display_name"] = "Renamed node"
        self.references[0]["display_name_snapshot"] = "Old selected label"
        result = await self.read(mention=1)
        self.assertEqual([item["id"] for item in result["messages"]], ["message-one"])
        self.assertEqual(result["messages"][0]["sender"]["display_name"], "Renamed node")

    async def test_selected_reference_selects_exact_team_with_or_without_matching_team_option(self):
        for team in (None, "private-team-b"):
            with self.subTest(team=team):
                result = await self.read(mention=3, team=team)
                self.assertEqual([item["id"] for item in result["messages"]], ["message-three"])
                self.assertEqual(self.runtime.team_authorized_read.call_args.kwargs["team_id"], "private-team-b")

    async def test_bulletin_reference_reads_selected_feed_without_sender_filter(self):
        result = await self.read(mention=4, box="feed")
        self.assertEqual([item["id"] for item in result["messages"]], ["bulletin-b"])
        call = self.runtime.team_authorized_read.call_args
        self.assertEqual(call.kwargs["team_id"], "private-team-b")
        self.assertIsNone(call.kwargs.get("from_kind"))
        self.assertIsNone(call.kwargs.get("from_id"))
        self.assertEqual(result["notice"], self.namespace["TEAM_CONTENT_NOTICE"])

    async def test_discovery_projects_only_index_kind_and_display_label(self):
        before = deepcopy(self.capability)
        result = await self.mentions()
        expected = [{"mention_index": index, "kind": item["kind"],
                     "recipient_kind": item["recipient_kind"], "display_name": item["display_name_snapshot"]}
                    for index, item in enumerate(self.references, 1)]
        self.assertEqual(result, {"mentions": expected, "notice": self.namespace["TEAM_CONTENT_NOTICE"]})
        encoded = json.dumps(result)
        for secret in ("private-team", "private-node", "private-source", "private-token",
                       "private-authority", "private-send-route", "never-project", "grant_intent"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(self.capability, before)
        self.authorize.assert_awaited_once_with(self.request, "team_read")
        self.assert_no_runtime()

    async def test_capability_without_selected_mentions_has_empty_discovery(self):
        del self.capability["team_read_mentions"]
        self.assertEqual((await self.mentions())["mentions"], [])
        self.assert_no_runtime()

    async def test_discovery_preserves_unsupported_reference_indexes_and_sanitizes_labels(self):
        self.references[0].update(kind="skill", recipient_kind=None, display_name_snapshot="\u202eUnsafe\nlabel\x00")
        self.references[1]["display_name_snapshot"] = "X" * 1000
        result = await self.mentions()
        self.assertEqual(result["mentions"][0], {
            "mention_index": 1, "kind": "skill", "recipient_kind": None, "display_name": "Unsafe label",
        })
        self.assertEqual(result["mentions"][1]["display_name"], "X" * 160)
        self.assertEqual(result["mentions"][2]["mention_index"], 3)
        self.assert_no_runtime()

    async def test_discovery_bound_and_read_index_bound_agree(self):
        self.references.extend(reference("private-team-a", f"private-node-{index}") for index in range(20))
        result = await self.mentions()
        self.assertEqual([item["mention_index"] for item in result["mentions"]], list(range(1, 17)))
        with self.assertRaises(HTTPException) as invalid:
            await self.read(mention=17)
        self.assertEqual(invalid.exception.status_code, 422)
        self.assert_no_runtime()

    async def test_invalid_mention_indexes_fail_before_thread_or_runtime(self):
        for index in (True, False, 0, -1, len(self.references) + 1, "1", 1.0):
            with self.subTest(mention=index):
                with self.assertRaises(HTTPException) as invalid:
                    await self.read(mention=index)
                self.assertEqual(invalid.exception.status_code, 422)
                self.assert_no_runtime()

    async def test_box_or_team_mismatch_cannot_fall_back_to_unscoped_read(self):
        for options in (
            {"mention": 1, "box": "feed"}, {"mention": 1, "box": "sent"},
            {"mention": 4, "box": "inbox"}, {"mention": 4, "box": "sent"},
            {"mention": 1, "team": "private-team-b"},
            {"mention": 1, "team": ""},
            {"mention": 4, "box": "feed", "team": "private-team-a"},
            {"mention": 1, "box": "invalid"},
        ):
            with self.subTest(options=options):
                with self.assertRaises(HTTPException) as invalid:
                    await self.read(**options)
                self.assertEqual(invalid.exception.status_code, 422)
                self.assert_no_runtime()

    async def test_unsupported_references_never_expand_into_other_recipients(self):
        for changes in (
            {"kind": "skill"}, {"recipient_kind": "human"}, {"recipient_kind": "all_servers"},
            {"recipient_kind": "unknown"}, {"recipient_kind": "all", "target_id": "private-node-one"},
            {"team_id": ""}, {"target_id": ""}, {"team_id": None}, {"target_id": True},
        ):
            with self.subTest(changes=changes):
                self.references[0] = {**reference("private-team-a", "private-node-one"), **changes}
                with self.assertRaises(HTTPException) as invalid:
                    await self.read(mention=1, box="feed" if changes.get("recipient_kind") == "all" else "inbox")
                self.assertEqual(invalid.exception.status_code, 422)
                self.assert_no_runtime()

    async def test_expired_revoked_or_missing_read_permission_blocks_both_endpoints(self):
        for reason in ("expired capability", "revoked capability", "team_read is not authorized"):
            self.authorize.side_effect = HTTPException(403, reason)
            for endpoint in (self.mentions, lambda: self.read(mention=1)):
                with self.subTest(reason=reason, endpoint=endpoint):
                    with self.assertRaises(HTTPException) as denied:
                        await endpoint()
                    self.assertEqual(denied.exception.status_code, 403)
                    self.authorize.assert_awaited_with(self.request, "team_read")
                    self.assert_no_runtime()

    async def test_runtime_authority_revocation_prevents_message_operation(self):
        self.runtime.team_authorized_read.side_effect = SecurePeerError("revoked", "Authority changed", 403)
        with self.assertRaises(HTTPException) as denied:
            await self.read(mention=1)
        self.assertEqual(denied.exception.status_code, 403)
        self.runtime.team_list_messages.assert_not_called()

    async def test_unscoped_reads_retain_explicit_team_and_existing_options(self):
        await self.read(team="private-team-a", unread=True, since="2026-09-01", after_sequence=19,
                        limit=1000000, include_mail_subject=True)
        call = self.runtime.team_authorized_read.call_args
        self.assertEqual(call.kwargs["team_id"], "private-team-a")
        self.assertEqual(call.kwargs["after_sequence"], 19)
        self.assertEqual(call.kwargs["limit"], self.namespace["PROVIDER_TEAM_LIST_LIMIT"])
        self.assertEqual(call.kwargs["since"], "2026-09-01")
        self.assertTrue(call.kwargs["unread"])
        self.assertTrue(call.kwargs["include_mail_subject"])
        self.assertIsNone(call.kwargs.get("from_kind"))
        self.assertIsNone(call.kwargs.get("from_id"))


class TeamMentionRuntimeFilterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime_type = extracted_runtime()

    def setUp(self):
        self.runtime = self.runtime_type()
        self.realm = {"realm": "host", "team_id": "private-team-a"}
        self.runtime.team_realm = mock.Mock(return_value=self.realm)
        self.runtime._team_hub_get = mock.Mock(return_value={"messages": []})

    def test_exact_sender_pair_is_forwarded_for_host_and_peer_realms(self):
        for realm_kind in ("host", "secure_peer"):
            with self.subTest(realm=realm_kind):
                self.realm["realm"] = realm_kind
                result = self.runtime.team_list_messages(
                    box="inbox", team_id="private-team-a", from_kind="server", from_id="private-node-one",
                    unread=True, since="2026-09-01", after_sequence=19, limit=7, include_mail_subject=True,
                )
                self.assertEqual(result, {"messages": [], "team_id": "private-team-a"})
                self.runtime.team_realm.assert_called_with("private-team-a")
                call = self.runtime._team_hub_get.call_args
                self.assertEqual(call.args[:2], (self.realm, "/v1/teams/private-team-a/network/messages"))
                self.assertEqual(call.args[2], {
                    "box": "inbox", "from_kind": "server", "from_id": "private-node-one",
                    "unread": True, "since": "2026-09-01", "after_sequence": 19, "limit": 7,
                    "include_mail_subject": True,
                })

    def test_absent_sender_pair_does_not_add_filters_to_existing_query(self):
        self.runtime.team_list_messages(box="feed", team_id="private-team-a")
        query = self.runtime._team_hub_get.call_args.args[2]
        self.assertNotIn("from_kind", query)
        self.assertNotIn("from_id", query)

    def test_invalid_sender_pairs_fail_before_realm_or_hub_lookup(self):
        for options in (
            {"from_kind": "server"}, {"from_id": "private-node-one"},
            {"from_kind": "all", "from_id": "all"},
            {"from_kind": "server", "from_id": ""},
            {"from_kind": "server", "from_id": True},
        ):
            with self.subTest(options=options):
                with self.assertRaises(SecurePeerError) as invalid:
                    self.runtime.team_list_messages(box="inbox", **options)
                self.assertEqual(invalid.exception.status_code, 422)
                self.runtime.team_realm.assert_not_called()
                self.runtime._team_hub_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
