"""Account-isolated inventories and explicit, bounded checks; synthetic keys only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
import httpx
import codex_provider
import provider_connections
import cursor_api_key
import gemini_api
from tests import test_provider_connections as fixtures
from tests.test_provider_connections import INPUT, NATIVE


class AccountCatalogTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.SettingsTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.store, self.client, self.check = fixture.store, fixture.client, fixture.check
        self.store.write("claude", 0, INPUT, "verified")
        self.route = "/api/admin/provider-models/claude"

    def test_fresh_catalog_never_reinserts_saved_default_and_empty_is_authoritative(self):
        for status, rows in [("ready", [{"value": "new", "label": "New"}]), ("empty", [])]:
            with patch.object(provider_connections, "endpoint_model_catalog", return_value={"models": rows, "discovery_status": status}):
                result = self.client.get(self.route, headers=NATIVE)
            self.assertEqual(result.status_code, 200)
            self.assertEqual(self.store.catalog("claude")["models"], rows)
            self.assertEqual(self.store.catalog("claude")["model"], INPUT["model"])

    def test_failure_preserves_only_same_connection_inventory(self):
        first = self.store.read("claude")
        rows = [{"value": "account-a", "label": "A"}]
        self.store.remember_catalog("claude", first, {"models": rows, "discovery_status": "ready"})
        self.store.remember_catalog("claude", first, {"models": [], "discovery_status": "unavailable"})
        self.assertEqual(self.store.catalog("claude")["models"], rows)
        self.assertEqual(self.store.catalog("claude")["discovery_status"], "unavailable")
        self.store.write("claude", 1, {**INPUT, "api_key": "another-synthetic-key", "expected_revision": 1}, "verified")
        with patch.object(provider_connections, "discover_catalog", return_value={"models": [], "discovery_status": "unavailable"}):
            self.assertEqual(self.store.catalog("claude")["models"], [])

    def test_explicit_model_check_is_nonmutating_scoped_and_recoverable(self):
        before = self.store.read("claude")
        rows = [{"value": "test/model", "label": "Test"}]
        self.store.remember_catalog("claude", before, {"models": rows, "discovery_status": "ready"})
        for status, locked in [("model_unavailable", True), ("rate_limited", False), ("authentication_failed", False), ("verified", False)]:
            self.check.return_value = status
            result = self.client.post(self.route + "/check", headers=NATIVE, json={"model": "test/model", "expected_revision": 1})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.json()["status"], status)
            self.assertEqual(bool(self.store.catalog("claude")["models"][0].get("locked")), locked)
            self.assertEqual(self.store.read("claude"), before)
        self.assertNotIn(INPUT["api_key"], result.text)
        self.check.reset_mock()
        for headers, body, code in [({}, {"model": "test/model", "expected_revision": 1}, 401),
                                    ({**NATIVE, "Origin": "https://browser.invalid"}, {"model": "test/model", "expected_revision": 1}, 403),
                                    (NATIVE, {"model": "test/model", "expected_revision": 2}, 409)]:
            self.assertEqual(self.client.post(self.route + "/check", headers=headers, json=body).status_code, code)
        self.check.assert_not_awaited()

    def test_late_inventory_does_not_publish_after_account_change(self):
        def replacement(*args, **kwargs):
            self.store.write("claude", 1, {**INPUT, "api_key": "new-account", "expected_revision": 1}, "verified")
            return {"models": [{"value": "old-account", "label": "Old"}], "discovery_status": "ready"}
        with patch.object(provider_connections, "endpoint_model_catalog", side_effect=replacement):
            self.assertEqual(self.client.get(self.route, headers=NATIVE).status_code, 409)
        self.assertFalse(self.store.catalog_cache)

    def test_default_only_change_preserves_status_and_model_observations(self):
        selected = self.store.read("claude")
        self.store.remember_catalog("claude", selected, {"models": [{"value": "old", "label": "Old"}], "discovery_status": "unavailable"})
        self.check.return_value = "model_unavailable"
        self.client.post(self.route + "/check", headers=NATIVE, json={"model": "old", "expected_revision": 1})
        result = self.client.put(self.route, headers=NATIVE, json={"model": "new", "expected_revision": 1})
        self.assertEqual(result.status_code, 200)
        catalog = self.store.catalog("claude")
        self.assertEqual(catalog["discovery_status"], "unavailable")
        self.assertTrue(catalog["models"][0]["locked"])

    def test_gemini_auto_survives_failed_refresh_of_previous_raw_inventory(self):
        selected = self.store.write("gemini", 0, {**INPUT, "base_url": "https://generativelanguage.googleapis.com",
            "protocol": "gemini", "auth_header": "x-goog-api-key", "model": None}, "verified")
        self.store.remember_catalog("gemini", selected, {"models": [{"value": "example", "label": "Example"}], "discovery_status": "ready"})
        with patch.object(gemini_api, "catalog", return_value={"models": [], "discovery_status": "unavailable"}):
            result = self.client.get("/api/admin/provider-models/gemini", headers=NATIVE)
        self.assertEqual(result.status_code, 200)
        self.assertEqual([row["value"] for row in result.json()["models"]], ["auto", "example"])


class ModelProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cursor_returns_270_models_and_reports_actual_truncation(self):
        for count, status in [(270, "ready"), (513, "partial")]:
            output = "Available models\n" + "\n".join(f"model-{n} - Model {n}" for n in range(count))
            with patch.object(cursor_api_key, "require_isolation", AsyncMock()), patch.object(cursor_api_key, "run_isolated_command", AsyncMock(return_value=output)):
                result = await cursor_api_key.catalog(INPUT, executable="fixture", env={})
            self.assertEqual(len(result["models"]), min(count, 512))
            self.assertEqual(result["discovery_status"], status)

    async def test_gemini_missing_quota_and_auth_failures_are_distinct(self):
        for http, expected in [(404, "model_unavailable"), (429, "rate_limited"), (403, "authentication_failed"), (400, "unsupported")]:
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(http)))
            with patch.object(gemini_api.httpx, "AsyncClient", return_value=client):
                result = await gemini_api.probe({"base_url": "https://fixture.invalid", "api_key": "synthetic", "model": "test"})
            self.assertEqual(result, expected)

    def test_gemini_audio_only_catalog_is_not_an_agent_inventory(self):
        names = ["gemini-future-flash", "gemini-future-tts", "lyria-future", "company-future"]
        rows = [{"name": "models/" + name, "supportedGenerationMethods": ["generateContent"]} for name in names]
        self.assertEqual([r["value"] for r in gemini_api.parse_models({"models": rows})], [names[0], names[3]])

    def test_codex_disk_catalog_expires_and_refresh_is_credential_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            store = codex_provider.ProviderStore(Path(root))
            store.save({"base_url": "https://fixture.invalid/v1", "api_key": "synthetic-key", "connection_verified": True})
            selected = store.registration(include_key=True)
            result = {"models": [{"value": "one", "label": "One"}], "discovery_status": "ready"}
            with patch.object(codex_provider.time, "time", return_value=1000):
                store.cache_catalog(selected, result)
            restored = codex_provider.ProviderStore(Path(root))
            with patch.object(codex_provider.time, "time", return_value=1400), patch.object(codex_provider, "endpoint_model_catalog", return_value={**result, "models": []}) as fetch:
                self.assertEqual(restored.cached_catalog()["discovery_status"], "unavailable")
                restored.refresh_catalog()
                self.assertEqual(restored.cached_catalog()["models"], [])
                restored.refresh_catalog()
                fetch.assert_called_once()
            store.save({"base_url": "https://fixture.invalid/v1", "api_key": "other-synthetic-key", "connection_verified": True})
            self.assertEqual(store.cached_catalog()["models"], [])
