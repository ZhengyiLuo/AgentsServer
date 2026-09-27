"""Synthetic catalog/default tests; no provider login or inference required."""
import asyncio
import ast
from contextlib import suppress
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
import codex_provider as provider
import provider_connections as connections
from tests import test_provider_connections as fixtures
from tests.test_provider_connections import INPUT, KEY, NATIVE, PATH


class CatalogTests(unittest.TestCase):
    def discover(self, handler, *, protocol="responses", base="https://fixture.invalid/v1"):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch.object(provider.httpx, "Client", return_value=client):
            return provider.endpoint_model_catalog({"base_url": base, "api_key": KEY}, protocol=protocol)

    def test_anthropic_paginates_and_preserves_titles_without_choosing_first(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.headers["authorization"], "Bearer " + KEY)
            self.assertEqual(request.url.path, "/v1/models")
            self.assertEqual(request.url.params["limit"], "1000")
            if len(requests) == 1:
                return httpx.Response(200, json={"data": [{"id": "fixture/first", "display_name": "Friendly first"}], "has_more": True, "last_id": "fixture/first", "next": "https://untrusted.invalid/steal"})
            self.assertEqual(request.url.params["after_id"], "fixture/first")
            return httpx.Response(200, json={"data": [{"id": "fixture/first"}, {"id": "fixture/second"}], "has_more": False})
        result = self.discover(handler, protocol="anthropic", base="https://fixture.invalid")
        self.assertEqual(len(requests), 2)
        self.assertEqual(result["models"], [{"value": "fixture/first", "label": "Friendly first"}, {"value": "fixture/second", "label": "fixture/second"}])
        self.assertEqual(result["default_model"], "")
        self.assertEqual(result["discovery_status"], "ready")

    def test_openrouter_user_inventory_and_tool_filter_no_native_fallback(self):
        def handler(request):
            self.assertEqual(request.url.path, "/api/v1/models/user")
            return httpx.Response(200, json={"data": [
                {"id": "fixture/chat", "name": "Readable", "supported_parameters": ["tools"]},
                {"id": "fixture/no-tools", "supports_tools": False},
                {"id": "fixture/image", "type": "image"},
                {"id": "fixture/unknown"}, {"id": "fixture/no-tools2", "supported_parameters": ["temperature"]}]})
        result = self.discover(handler, base="https://openrouter.ai/api", protocol="anthropic")
        self.assertEqual([x["value"] for x in result["models"]], ["fixture/chat", "fixture/unknown"])
        self.assertEqual(result["model_capabilities"]["fixture/chat"]["reasoning_efforts"], [])

    def test_errors_redirects_and_repeated_cursor_are_bounded(self):
        for status in (302, 401, 500):
            requests = []
            def handler(request):
                requests.append(request)
                return httpx.Response(status, headers={"location": "https://untrusted.invalid"}, json={"error": KEY})
            result = self.discover(handler)
            self.assertEqual(len(requests), 1)
            self.assertEqual(result["models"], [])
            self.assertNotIn(KEY, str(result))
        calls = []
        def repeated(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [{"id": "fixture/a"}], "has_more": True, "last_id": "a"})
        self.assertEqual(self.discover(repeated)["discovery_status"], "partial")
        self.assertEqual(len(calls), 2)

    def test_auth_failure_during_pagination_clears_partial_inventory(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"data": [{"id": "fixture/a"}], "has_more": True, "last_id": "a"}) if len(calls) == 1 else httpx.Response(401)
        result = self.discover(handler)
        self.assertEqual(result["models"], [])
        self.assertEqual(result["discovery_status"], "authentication_failed")

    def test_legacy_default_only_for_started_threads_survives_new_discovery_and_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = provider.ProviderStore(Path(temporary))
            store.save({"base_url": "https://fixture.invalid/v1", "api_key": KEY})
            selected = store.selection(include_key=True, include_revision=True)
            old = {"models": [{"value": "first/model", "label": "First model"}], "model_capabilities": {}, "default_model": "first/model"}
            store._save_model_catalog(selected, old)
            store.cache_catalog(selected, {**old, "default_model": ""})
            store = provider.ProviderStore(Path(temporary))
            new_chat = {"codex_provider": "custom", "codex_provider_revision": selected["credential_id"]}
            started = {**new_chat, "session_id": "native-thread"}
            self.assertEqual(store.catalog(available=True)["default_model"], "")
            self.assertFalse(store.for_session(new_chat).get("model"))
            self.assertEqual(store.for_session(started)["model"], "first/model")
            self.assertEqual(store.catalog(available=True, session=started)["default_model"], "first/model")
            self.assertEqual(store.cached_catalog()["models"][0]["label"], "First model")


class ModelSettingsTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.SettingsTests.setUp
    def test_model_read_is_separate_and_default_save_preserves_key_history_and_check_time(self):
        self.client.put(PATH, headers=NATIVE, json=INPUT)
        before = self.store.read("claude")
        binding = self.store.bind({"backend": "claude", "provider_connection": "custom"})
        chat = {"backend": "claude", "provider_connection": "custom", "provider_connection_revision": binding["credential_id"]}
        route = "/api/admin/provider-models/claude"
        listed = {"models": [{"value": "new/model", "label": "New Model"}], "discovery_status": "ready"}
        with patch.object(connections, "endpoint_model_catalog", return_value=listed) as discovery:
            response = self.client.get(route, headers=NATIVE)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(discovery.call_args.args[0]["api_key"], KEY)
        self.assertNotIn(KEY, response.text)
        self.check.reset_mock()
        saved = self.client.put(route, headers=NATIVE, json={"model": "new/model", "expected_revision": 1})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json(), {"backend": "claude", "revision": 2, "default_model": "new/model"})
        current = self.store.read("claude")
        self.assertEqual(current["api_key"], KEY)
        self.assertEqual(current["checked_at"], before["checked_at"])
        self.assertEqual(self.store.for_session(chat)["model"], INPUT["model"])
        self.check.assert_not_awaited()
        self.assertEqual(self.client.put(route, headers=NATIVE, json={"model": "wrong", "expected_revision": 1}).status_code, 409)
        for headers in ({}, {**NATIVE, "Origin": "https://example.invalid"}):
            self.assertNotEqual(self.client.get(route, headers=headers).status_code, 200)
            self.assertNotEqual(self.client.put(route, headers=headers, json={"model": "wrong", "expected_revision": 2}).status_code, 200)


class CodexSaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_save_callback_fences_revision_and_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = provider.ProviderStore(Path(temporary))
            store.save({"base_url": "https://fixture.invalid/v1", "api_key": KEY, "connection_verified": True})
            old = store.revision()
            async def replace(value): store.save(value)
            ns = {"asyncio": asyncio, "CODEX_PROVIDER_SETTINGS_LOCK": asyncio.Lock(), "CODEX_PROVIDER_STORE": store,
                  "HTTPException": HTTPException, "replace_codex_provider_settings": replace, "suppress": suppress}
            tree = ast.parse((Path(__file__).parents[1] / "agent_server.py").read_text())
            node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "set_codex_default_model")
            exec(compile(ast.Module(body=[node], type_ignores=[]), "agent_server.py", "exec"), ns)
            result = await ns["set_codex_default_model"]("new/model", old)
            self.assertEqual(result["default_model"], "new/model")
            self.assertNotEqual(result["revision"], old)
            self.assertEqual(store.selection(include_key=True)["api_key"], KEY)
            self.assertIsNone(store.selection(revision=old).get("model"))
            with self.assertRaises(HTTPException) as error: await ns["set_codex_default_model"]("wrong", old)
            self.assertEqual(error.exception.status_code, 409)
