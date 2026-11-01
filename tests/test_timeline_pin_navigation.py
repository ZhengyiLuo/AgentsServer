"""Exact pin anchors through the production HTTP route, without provider I/O."""
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest

from fastapi import FastAPI, HTTPException
import httpx


class PinNavigationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        tree = ast.parse((Path(__file__).resolve().parents[1] / "agent_server.py").read_text())
        names = {"find_timeline_event_anchor", "get_timeline_event_anchor"}
        cls.code = compile(ast.Module(body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                                           and n.name in names], type_ignores=[]), "<pin-navigation>", "exec")

    async def test_identity_not_preview_or_search_index_controls_navigation(self):
        rows = [
            {"id": "repeat", "session_id": "chat", "seq": 9, "text": "same preview"},
            {"id": "old", "session_id": "chat", "seq": 2, "text": "**same** preview"},
            {"id": "foreign", "session_id": "other", "seq": 3},
        ]
        ns = {"app": FastAPI(), "Any": Any, "asyncio": asyncio, "HTTPException": HTTPException,
              "STORE": SimpleNamespace(sessions={"chat": {}}), "iter_session_events": lambda _: iter(rows)}
        exec(self.code, ns)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ns["app"]), base_url="http://test") as client:
            response = await client.get('/api/sessions/chat/timeline-event/old')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["event"]["seq"], 2)
            self.assertNotIn("text", response.json()["event"])
            for event in ("foreign", "missing"):
                self.assertIsNone((await client.get('/api/sessions/chat/timeline-event/' + event)).json()["event"])
            self.assertEqual((await client.get('/api/sessions/other/timeline-event/old')).status_code, 404)
