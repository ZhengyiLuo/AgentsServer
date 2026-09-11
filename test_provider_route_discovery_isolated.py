"""Bounded route discovery using extracted functions, never server startup."""
from __future__ import annotations

import ast
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unicodedata
import unittest
from unittest.mock import AsyncMock


class HTTPException(Exception):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail


class ProviderRouteDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        names = {"list_provider_cross_chat_routes", "provider_cross_chat_route_projection",
                 "sanitized_provider_route_label"}
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        tree = ast.parse(Path(__file__).with_name("agent_server.py").read_text())
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                node = deepcopy(node)
                node.decorator_list = []
                nodes.append(node)
        self.routes = {self.route_id(n): {
            "route_id": self.route_id(n), "alias": f"chat-{n:04}", "target_session_id": f"target-{n}",
            "actions": ["instruction", "request_reply"], "pair_id": f"pair-{n}",
        } for n in range(145)}
        self.capability = {"provider_route_grants": self.routes, "async_route_v1": True}
        self.revoked = set()
        self.live_checks = []
        self.locked = False
        self.authorize = AsyncMock(side_effect=self.authorized)
        sessions = {"source": {"backend": "codex"}, **{
            f"target-{n}": {"backend": "codex", "title": "🧪" * 160, "folder": "private-folder"}
            for n in range(145)
        }}
        self.namespace = {
            "HTTPException": HTTPException, "unicodedata": unicodedata,
            "PROVIDER_CROSS_CHAT_ROUTE_ID_RE": re.compile(r"^route_[0-9a-f]{32}$"),
            "PROVIDER_CROSS_CHAT_ROUTE_HANDOFF_LIMIT": 4,
            "provider_route_capability_source": AsyncMock(return_value="source"),
            "session_lifecycle_lock": self.lifecycle, "authorize_provider_action": self.authorize,
            "live_provider_cross_chat_route": self.live,
            "STORE": SimpleNamespace(sessions=sessions), "DEFAULT_BACKEND": "codex",
            "VALID_BACKENDS": {"codex", "claude"}, "cross_chat_target_backend_supported": lambda _: True,
            "provider_cross_chat_route_availability": lambda *_: (True, None),
        }
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     "<isolated-provider-route-discovery>", "exec"), self.namespace)

    @staticmethod
    def route_id(n):
        return f"route_{n:032x}"

    @asynccontextmanager
    async def lifecycle(self, source):
        self.assertEqual(source, "source")
        self.locked = True
        try:
            yield
        finally:
            self.locked = False

    async def authorized(self, _request, **kwargs):
        self.assertTrue(self.locked)
        self.assertEqual(kwargs, {"action": "agent_cross_chat_routes", "session_id": "source"})
        return self.capability

    def live(self, source, issued):
        self.assertTrue(self.locked)
        self.assertEqual(source, "source")
        self.live_checks.append(issued["route_id"])
        return None if issued["route_id"] in self.revoked else issued

    async def page(self, **query):
        return await self.namespace["list_provider_cross_chat_routes"](SimpleNamespace(query_params=query))

    async def test_all_permissions_accessible_in_bounded_pages(self):
        seen, cursor = [], None
        for expected_count in (64, 64, 17):
            result = await self.page(**({"cursor": cursor} if cursor else {}))
            self.assertEqual(len(result["routes"]), expected_count)
            self.assertIsNone(result["max_handoffs_per_run"])
            # The CLI's actual UTF-8 output remains below the provider limit,
            # including maximal four-byte titles and every projected field.
            self.assertLess(len(json.dumps(result, ensure_ascii=False).encode()), 128 * 1024)
            self.assertNotIn("private-folder", json.dumps(result))
            self.assertNotIn("target_session_id", json.dumps(result))
            self.assertTrue(all(row["mode"] == "async_route_v1" for row in result["routes"]))
            seen.extend(row["route_id"] for row in result["routes"])
            cursor = result["next_cursor"]
        self.assertIsNone(cursor)
        self.assertEqual(seen, list(self.routes))
        self.assertEqual(self.authorize.await_count, 3)

    async def test_empty_revoked_page_advances_and_revoked_cursor_still_works(self):
        self.revoked.update(list(self.routes)[:64])
        first = await self.page()
        self.assertEqual(first["routes"], [])
        self.assertEqual(first["next_cursor"], self.route_id(63))
        second = await self.page(cursor=first["next_cursor"])
        self.assertEqual(second["routes"][0]["route_id"], self.route_id(64))
        self.assertEqual(second["next_cursor"], self.route_id(127))

    async def test_exact_lookup_beyond_first_page_preserves_live_authorization(self):
        route_id = self.route_id(144)
        result = await self.page(route_id=route_id)
        self.assertEqual([row["route_id"] for row in result["routes"]], [route_id])
        self.assertEqual(self.live_checks, [route_id])
        self.assertIsNone(result["next_cursor"])
        self.revoked.add(route_id)
        self.assertEqual((await self.page(route_id=route_id))["routes"], [])
        self.assertEqual((await self.page(route_id=self.route_id(999)))["routes"], [])
        self.authorize.side_effect = HTTPException(403, "expired")
        with self.assertRaises(HTTPException) as expired:
            await self.page(route_id=route_id)
        self.assertEqual(expired.exception.status_code, 403)

    async def test_legacy_metadata_and_invalid_queries(self):
        self.capability["async_route_v1"] = False
        result = await self.page(route_id=self.route_id(1))
        self.assertEqual(result["max_handoffs_per_run"], 4)
        self.assertNotIn("mode", result["routes"][0])
        for query in ({"cursor": "bad"}, {"cursor": self.route_id(999)},
                      {"route_id": "bad"}, {"route_id": self.route_id(1), "cursor": self.route_id(0)}):
            with self.subTest(query=query), self.assertRaises(HTTPException) as invalid:
                await self.page(**query)
            self.assertEqual(invalid.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
