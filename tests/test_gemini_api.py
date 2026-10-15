import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
import gemini_api
from provider_connections import ConnectionStore, selection


def selected():
    return {"base_url": "https://generativelanguage.googleapis.com", "api_key": "test-not-a-real-key",
            "model": None, "protocol": "gemini", "auth_header": "x-goog-api-key", "expected_revision": 0}


class GeminiAPITests(unittest.IsolatedAsyncioTestCase):
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
