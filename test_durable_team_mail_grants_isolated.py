"""Small source-extracted mail grant checks; never starts/imports the server."""
from __future__ import annotations

import ast
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
import uuid
from unittest.mock import AsyncMock

from fastapi import HTTPException
from agentsdock_team_hub.store import HubError
from agentsdock_team_hub.secure_peer import SecurePeerError
import team_mail_grants as grants


def route(index=1):
    return {
        "route_id": "mailgrant_" + f"{index:032x}", "revision": "rev_" + f"{index:032x}",
        "team_id": "qa-team", "target_id": f"qa-node-{index}", "recipient_kind": "server",
        "display_name": f"QA mail recipient {index}", "created_at": "2026-09-10T00:00:00Z",
        "updated_at": "2026-09-10T00:00:00Z", "durable_server_binding": {
            "version": 1, "team_id": "qa-team", "hub_id": "qa-hub", "target_id": f"qa-node-{index}",
            "server_identity": f"qa-server-{index}", "lifecycle_id": str(index) * 64,
        },
    }


def endpoint_fixture():
    names = {"list_agent_team_mail_routes", "delete_agent_team_mail_route", "resolve_provider_durable_team_reference",
             "stage_provider_team_mail_grants", "settle_provider_team_mail_grants",
             "reconcile_pending_provider_team_mail_grant", "provider_cross_chat_grant_admission_event"}
    tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
    session = {"backend": "claude", grants.ROUTES_KEY: [route(1), route(2)]}
    lock = asyncio.Lock()
    @asynccontextmanager
    async def lifecycle(_source):
        async with lock:
            yield
    def resolve(binding):
        if binding["target_id"] == "qa-node-2":
            raise ValueError("synthetic departed peer")
        return {"kind": "recipient", "recipient_kind": "server", "team_id": binding["team_id"],
                "target_id": binding["target_id"], "display_name_snapshot": "QA available recipient",
                "durable_server_binding": dict(binding)}
    runtime = SimpleNamespace(
        team_authority_generation=lambda: "qa-generation",
        team_authorized_read=lambda _generation, operation, *args: operation(*args),
        resolve_durable_server_reference=resolve,
    )
    namespace = {
        "TeamReference": Any, "uuid": uuid,
        "now_iso": lambda: "2026-09-10T00:00:00Z",
        "team_reference_dicts": lambda refs: [vars(reference) for reference in refs],
        "Any": Any, "asyncio": asyncio, "team_mail_grants": grants, "AGENT_TOKEN": True,
        "HTTPException": HTTPException, "HubError": HubError, "SecurePeerError": SecurePeerError,
        "SECURE_PEER_RUNTIME": runtime, "session_lifecycle_lock": lifecycle,
        "ensure_session_not_deleting": lambda _source: None,
        "STORE": SimpleNamespace(sessions={"qa-away-chat": session}, _lock=asyncio.Lock(),
                                 save=AsyncMock(), persist_restored_state=AsyncMock()),
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "<isolated-mail-grants>", "exec"), namespace)
    return namespace


async def actual_admin_fixture():
    namespace = endpoint_fixture()
    result = await namespace["list_agent_team_mail_routes"]("qa-away-chat")
    return {"origin": "actual-server-projection", "sessionId": "qa-away-chat", **result}


class DurableMailGrantTests(unittest.TestCase):
    def test_pending_admission_hidden_and_exact_event_settlement(self):
        original, added = route(1), route(2)
        mutation = {"admission_id": "grant_admission_" + "a" * 32, "event_type": "turn_queued",
                    "before": [original], "after": [original, added]}
        session = {grants.ROUTES_KEY: deepcopy(mutation["after"]), grants.PENDING_KEY: mutation}
        self.assertEqual(grants.live_routes(session), [original])
        self.assertFalse(grants.settle(session, "wrong", accepted=True))
        self.assertTrue(grants.settle(session, mutation["admission_id"], accepted=True))
        self.assertEqual(grants.live_routes(session), [original, added])

    def test_revocation_and_new_revision_cannot_expand_old_snapshot(self):
        original = route()
        ceiling = grants.snapshot([original])
        session = {grants.ROUTES_KEY: []}
        self.assertEqual(grants.intersect(session, ceiling), [])
        replacement = {**original, "revision": "rev_" + "f" * 32}
        session[grants.ROUTES_KEY] = [replacement]
        self.assertEqual(grants.intersect(session, ceiling), [])

    def test_malformed_and_bulk_bindings_fail_closed(self):
        for patch in ({"recipient_kind": "all"}, {"recipient_kind": "all_servers"},
                      {"durable_server_binding": {**route()["durable_server_binding"], "token": "no"}}):
            self.assertEqual(grants.normalize_routes([{**route(), **patch}]), [])
        self.assertEqual(grants.normalize_routes([route()] * 17), [])

    def test_actual_admin_projection_and_revision_revoke(self):
        async def check():
            namespace = endpoint_fixture()
            result = await namespace["list_agent_team_mail_routes"]("qa-away-chat")
            self.assertEqual([item["available"] for item in result["routes"]], [True, False])
            self.assertNotIn("durable_server_binding", result["routes"][0])
            with self.assertRaises(HTTPException) as caught:
                await namespace["delete_agent_team_mail_route"]("qa-away-chat", route()["route_id"], "rev_" + "f" * 32)
            self.assertEqual(caught.exception.status_code, 409)
            await namespace["delete_agent_team_mail_route"]("qa-away-chat", route()["route_id"], route()["revision"])
            self.assertEqual(grants.intersect(namespace["STORE"].sessions["qa-away-chat"], grants.snapshot([route()])), [])
        asyncio.run(check())

    def test_revoke_blocks_existing_provider_reference(self):
        async def check():
            namespace = endpoint_fixture()
            reference = {"durable_mail_grant": grants.snapshot([route()])[0],
                         "durable_server_binding": route()["durable_server_binding"]}
            current = await namespace["resolve_provider_durable_team_reference"]("qa-away-chat", reference, "qa-generation")
            self.assertEqual(current["display_name_snapshot"], "QA available recipient")
            namespace["STORE"].sessions["qa-away-chat"][grants.ROUTES_KEY] = []
            with self.assertRaises(HTTPException):
                await namespace["resolve_provider_durable_team_reference"]("qa-away-chat", reference, "qa-generation")
        asyncio.run(check())

    def test_actual_stage_and_crash_recovery_require_exact_server_admission(self):
        async def check():
            namespace = endpoint_fixture()
            session = namespace["STORE"].sessions["qa-away-chat"]
            session[grants.ROUTES_KEY] = []
            selected = SimpleNamespace(kind="recipient", recipient_kind="server", team_id="qa-team",
                                       target_id="qa-node-1", display_name_snapshot="QA mail recipient 1")
            runtime = namespace["SECURE_PEER_RUNTIME"]
            runtime.resolve_team_references = lambda refs: [
                {**refs[0], "durable_server_binding": route()["durable_server_binding"]}]
            admission = "grant_admission_" + "b" * 32
            mutation = await namespace["stage_provider_team_mail_grants"](
                "qa-away-chat", [selected], admission_id=admission, event_type="turn_queued")
            self.assertEqual(grants.live_routes(session), [])
            staged = deepcopy(session)
            namespace["events_path"] = lambda _source: None
            event = {"type": "turn_queued", "purpose": None,
                     "provider_team_mail_grant_admission_id": admission,
                     "provider_team_mail_route_snapshot": grants.snapshot(mutation["after"])}
            namespace["reversed_jsonl_events"] = lambda _path: iter([event])
            self.assertTrue(namespace["reconcile_pending_provider_team_mail_grant"]("qa-away-chat", session))
            self.assertEqual(len(grants.live_routes(session)), 1)
            event["imported"] = True
            namespace["reconcile_pending_provider_team_mail_grant"]("qa-away-chat", staged)
            self.assertEqual(grants.live_routes(staged), [])
        asyncio.run(check())

    def test_old_hub_retains_exact_admitted_one_use_reference_without_grant(self):
        async def check():
            namespace = endpoint_fixture()
            session = namespace["STORE"].sessions["qa-away-chat"]
            session[grants.ROUTES_KEY] = []
            selected = SimpleNamespace(kind="recipient", recipient_kind="server", team_id="qa-team",
                                       target_id="qa-node-1", display_name_snapshot="QA recipient")
            namespace["SECURE_PEER_RUNTIME"].resolve_team_references = lambda refs: refs
            mutation = await namespace["stage_provider_team_mail_grants"](
                "qa-away-chat", [selected], admission_id="grant_admission_" + "c" * 32, event_type="turn_queued")
            ceiling = grants.admission_snapshot(mutation, session)
            self.assertEqual(ceiling, [{"kind": "legacy_reference", "team_id": "qa-team",
                                       "target_id": "qa-node-1", "display_name": "QA recipient"}])
            self.assertEqual(grants.live_routes(session), [])
            self.assertNotIn(grants.PENDING_KEY, session)
            self.assertEqual(grants.snapshot(ceiling), ceiling)
            self.assertEqual(grants.intersect(session, ceiling), [])
            # A revoked modern route has no legacy marker to recover from.
            self.assertEqual(grants.snapshot(grants.snapshot([route()])), grants.snapshot([route()]))
            self.assertFalse(any(item.get("kind") == "legacy_reference" for item in grants.snapshot([route()])))
            namespace["SECURE_PEER_RUNTIME"].resolve_team_references = lambda refs: [{**refs[0], "durable_server_binding": {}}]
            with self.assertRaises(HTTPException):
                await namespace["stage_provider_team_mail_grants"](
                    "qa-away-chat", [selected], admission_id="grant_admission_" + "d" * 32, event_type="turn_queued")
        asyncio.run(check())

    def test_actual_send_endpoint_rechecks_durable_grant_before_write(self):
        from test_agent_team_reply_endpoint_isolated import TeamReplyEndpointTests
        endpoint = TeamReplyEndpointTests()
        endpoint.setUp()
        namespace = endpoint_fixture()
        namespace["STORE"] = endpoint.namespace["STORE"]
        session = namespace["STORE"].sessions["isolated-session"]
        session[grants.ROUTES_KEY] = [route()]
        endpoint.reference.update({
            "team_id": route()["team_id"], "target_id": route()["target_id"],
            "durable_mail_grant": grants.snapshot([route()])[0],
            "durable_server_binding": route()["durable_server_binding"],
        })
        endpoint.namespace.update({
            "asyncio": asyncio,
            "resolve_provider_durable_team_reference": namespace["resolve_provider_durable_team_reference"],
            "session_lifecycle_lock": namespace["session_lifecycle_lock"],
        })
        result = endpoint.send()
        self.assertTrue(result["ok"])
        self.assertEqual(endpoint.runtime.team_send_message.call_count, 1)
        sent_reference = endpoint.runtime.team_send_message.call_args.args[0]
        self.assertEqual(sent_reference["durable_server_binding"], route()["durable_server_binding"])
        endpoint.live["team_send_consumed"] = {}
        endpoint.live["team_routes_used"] = {}
        session[grants.ROUTES_KEY] = []
        with self.assertRaises(HTTPException) as caught:
            endpoint.send(idempotency_key="another-synthetic-send")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(endpoint.runtime.team_send_message.call_count, 1)

    def test_downgraded_hub_cannot_bypass_revocation_of_existing_grant(self):
        async def check():
            namespace = endpoint_fixture()
            session = namespace["STORE"].sessions["qa-away-chat"]
            session[grants.ROUTES_KEY] = [route()]
            selected = SimpleNamespace(kind="recipient", recipient_kind="server", team_id="qa-team",
                                       target_id="qa-node-1", display_name_snapshot="QA recipient")
            namespace["SECURE_PEER_RUNTIME"].resolve_team_references = lambda refs: refs
            mutation = await namespace["stage_provider_team_mail_grants"](
                "qa-away-chat", [selected], admission_id="grant_admission_" + "e" * 32, event_type="turn_queued")
            ceiling = grants.admission_snapshot(mutation, session)
            self.assertEqual(ceiling, grants.snapshot([route()]))
            await namespace["delete_agent_team_mail_route"](
                "qa-away-chat", route()["route_id"], route()["revision"])
            # Execute the actual issuer's reference filter and Team handle
            # projection only; no authority creation, server import or I/O.
            tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
            issuer = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                          and node.name == "issue_cross_chat_capability")
            filter_node = next(node for node in issuer.body if isinstance(node, ast.If)
                               and ast.unparse(node.test) == "team_mail_route_snapshot is not None")
            projection_index = next(index for index, node in enumerate(issuer.body)
                                    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                                    and node.target.id == "team_routes")
            scope = {"team_mail_grants": grants, "team_mail_route_snapshot": ceiling,
                     "validated_team_references": [selected], "AGENT_TOKEN": True,
                     "source_session_id": "qa-away-chat", "STORE": namespace["STORE"],
                     "secrets": SimpleNamespace(token_hex=lambda size: "0" * (size * 2)), "Any": Any}
            exec(compile(ast.Module(body=[filter_node], type_ignores=[]), "<issuer-filter>", "exec"), scope)
            scope["resolved_team_references"] = [vars(item) for item in scope["validated_team_references"]]
            exec(compile(ast.Module(body=issuer.body[projection_index:projection_index + 2], type_ignores=[]),
                         "<issuer-routes>", "exec"), scope)
            self.assertEqual(scope["team_routes"], {})
            with self.assertRaises(HTTPException):
                await namespace["resolve_provider_durable_team_reference"]("qa-away-chat", {
                    "durable_mail_grant": grants.snapshot([route()])[0],
                    "durable_server_binding": route()["durable_server_binding"],
                }, "qa-generation")
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
