import ast
import asyncio
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from typing import Any

import httpx
from fastapi import HTTPException
import gemini_api
from provider_connections import ConnectionStore, selection
import provider_connections


def selected():
    return {"base_url": "https://generativelanguage.googleapis.com", "api_key": "test-not-a-real-key",
            "model": None, "protocol": "gemini", "auth_header": "x-goog-api-key", "expected_revision": 0}


class GeminiAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_accepts_official_auto_but_not_missing_gateway_model_or_revoked_key(self):
        # Exercise the actual admission function without starting global managers.
        source = ast.parse(Path(__file__).resolve().parents[1].joinpath("agent_server.py").read_text())
        function = next(node for node in source.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "ensure_runtime_available")
        store = Mock()
        namespace = {"Any": Any, "asyncio": asyncio, "HTTPException": HTTPException,
            "BACKEND_GEMINI": "gemini", "runner_env": lambda: {}, "store_runtime_diagnostic": Mock(),
            "PROVIDER_CONNECTION_STORE": store, "provider_connections": provider_connections}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "agent_server.py", "exec"), namespace)
        with patch("gemini_runtime.probe", return_value={"status": "ready"}):
            with self.assertRaises(HTTPException) as rejected:
                await namespace["ensure_runtime_available"]("gemini", session={"backend": "gemini"})
            self.assertEqual(rejected.exception.status_code, 409)
            self.assertIn("API connections only", rejected.exception.detail)
            store.for_session.assert_not_called()
            session = {"backend": "gemini", "provider_connection": "custom"}
            store.for_session.return_value = selected()
            self.assertEqual(await namespace["ensure_runtime_available"]("gemini", session=session), {"status": "ready"})
            store.for_session.return_value = {**selected(), "base_url": "https://gateway.example"}
            with self.assertRaises(HTTPException):
                await namespace["ensure_runtime_available"]("gemini", session=session)
            store.for_session.side_effect = HTTPException(409, "Disconnected")
            with self.assertRaises(HTTPException):
                await namespace["ensure_runtime_available"]("gemini", session=session)

    def test_protocol_and_header_not_interchangeable(self):
        self.assertEqual(selection("gemini", selected())["protocol"], "gemini")
        for patch_value in ({"auth_header": "bearer"}, {"protocol": "responses"}):
            with self.assertRaises(HTTPException): selection("gemini", {**selected(), **patch_value})
        with self.assertRaises(HTTPException): selection("opencode", selected())

    def test_catalog_filters_non_generation_models(self):
        values = gemini_api.parse_models({"models": [
            {"name": "models/gemini-test", "displayName": "Test", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embedding-test", "supportedGenerationMethods": ["embedContent"]}]})
        self.assertEqual(values, [{"value": "gemini-test", "label": "Test"}])
        self.assertEqual(gemini_api.api_base(selected()), "https://generativelanguage.googleapis.com/v1beta")

    def test_official_api_uses_native_auto_without_guessing_a_model_version(self):
        for suffix in ("", "/", "/v1", "/v1beta/", ":443/v1beta"):
            value = {**selected(), "base_url": selected()["base_url"] + suffix}
            self.assertEqual(gemini_api.default_model(value), "auto")
            self.assertEqual(gemini_api.model_options(value, [])[0]["value"], "auto")
        self.assertEqual(gemini_api.default_model({**selected(), "model": "gemini-chosen"}), "gemini-chosen")

    def test_gateway_does_not_invent_auto_or_pick_first_inventory_model(self):
        for url in ("https://gateway.example", "http://generativelanguage.googleapis.com",
                    "https://generativelanguage.googleapis.com.evil.test", "https://generativelanguage.googleapis.com/proxy",
                    "https://generativelanguage.googleapis.com:8443", "https://user@generativelanguage.googleapis.com"):
            value = {**selected(), "base_url": url}
            self.assertIsNone(gemini_api.default_model(value), url)
            self.assertEqual(gemini_api.model_options(value, []), [])

    def test_existing_empty_model_binding_catalog_gets_auto_without_mutating_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConnectionStore(Path(root) / "connections")
            store.write("gemini", 0, selected(), "verified")
            session = {"backend": "gemini", "provider_connection": "custom"}
            binding = store.bind(session)
            session["provider_connection_revision"] = binding["credential_id"]
            before = store.read("gemini", binding["credential_id"])
            result = store.catalog("gemini", session=session)
            self.assertEqual(result["default_model"], "auto")
            self.assertEqual(result["models"], [{"value": "auto", "label": "Auto (Gemini CLI)"}])
            self.assertEqual(store.read("gemini", binding["credential_id"]), before)
            # Replacement cannot change an old chat's model/account; Forget
            # still revokes it, including when it used the new Auto fallback.
            store.write("gemini", 1, {**selected(), "expected_revision": 1, "model": "gemini-other"}, "verified")
            self.assertEqual(store.catalog("gemini", session=session)["default_model"], "auto")
            self.assertEqual(store.catalog("gemini", session={**session, "model": "gemini-chosen"})["default_model"], "gemini-chosen")
            store.write("gemini", 2, None)
            self.assertFalse(store.catalog("gemini", session=session)["available"])
            with self.assertRaises(HTTPException): store.for_session(session)

    async def probe_with(self, handler, value=None):
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(gemini_api.httpx, "AsyncClient", return_value=client):
            return await gemini_api.probe(value or selected())

    async def test_key_check_does_not_treat_public_catalog_as_auth(self):
        def public(request): return httpx.Response(200, json={"models": []})
        self.assertEqual(await self.probe_with(public), "model_required")

    async def test_key_check_uses_header_without_url_or_body_secret(self):
        def check(request):
            self.assertNotIn(selected()["api_key"], str(request.url))
            self.assertEqual(request.method, "GET")
            return httpx.Response(200, json={"models": []}) if request.headers.get("x-goog-api-key") else httpx.Response(403)
        self.assertEqual(await self.probe_with(check), "verified")

    async def test_model_check_is_explicit_and_bounded(self):
        def check(request):
            self.assertEqual(request.method, "POST")
            self.assertTrue(request.url.path.endswith("/gemini-test:generateContent"))
            self.assertEqual(json.loads(request.content)["generationConfig"]["maxOutputTokens"], 256)
            return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}]})
        self.assertEqual(await self.probe_with(check, {**selected(), "model": "gemini-test"}), "verified")

    def test_forget_revokes_existing_binding_without_cli_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            store = ConnectionStore(Path(root) / "connections")
            store.write("gemini", 0, {**selected(), "model": "gemini-test"}, "verified")
            session = {"backend": "gemini", "provider_connection": "custom"}
            value = store.bind(session)
            session["provider_connection_revision"] = value["credential_id"]
            store.write("gemini", 1, None)
            with self.assertRaises(HTTPException): store.for_session(session)
            self.assertNotIn("api_key", store.public("gemini"))
