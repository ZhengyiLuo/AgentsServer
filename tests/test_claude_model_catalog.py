"""Passive native catalog: no probe processes or account metadata."""
import json
import os
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import claude_model_catalog as catalog

class NativeModelLabelTests(unittest.TestCase):
    def parse(self, *models):
        return catalog.parse_native_models({"models": list(models)})

    def test_versioned_alias_keeps_value_and_follows_future_versions(self):
        for version in ("5-5", "6", "6-1", "10-12"):
            with self.subTest(version=version):
                self.assertEqual(self.parse({
                    "value": "opus", "displayName": "Opus",
                    "resolvedModel": "claude-opus-" + version,
                }), [{"value": "opus", "label": "Opus " + version.replace("-", ".")}])

    def test_current_native_picker_default_context_and_dated_model(self):
        rows = self.parse(
            {"value": "default", "displayName": "Default (recommended)", "resolvedModel": "claude-opus-5-5[1m]"},
            {"value": "opus[1m]", "displayName": "Opus (1M context)", "resolvedModel": "claude-opus-5-5[1m]"},
            {"value": "claude-fable-5-1[1m]", "displayName": "Fable", "resolvedModel": "claude-fable-5-1"},
            {"value": "sonnet", "displayName": "Sonnet", "resolvedModel": "claude-sonnet-5"},
            {"value": "haiku", "displayName": "Haiku", "resolvedModel": "claude-haiku-4-5-20251001"},
        )
        self.assertEqual([r["label"] for r in rows], [
            "Default — Opus 5.5 (1M context)", "Opus 5.5 (1M context)",
            "Fable 5.1 (1M context)", "Sonnet 5", "Haiku 4.5",
        ])

    def test_old_sdk_uses_native_description_without_guessing(self):
        self.assertEqual(self.parse(
            {"value": "opus", "displayName": "Opus", "description": "Opus 4.8 · Most capable"},
            {"value": "sonnet", "displayName": "Sonnet", "description": "Sonnet 5 with 1M context · Fast"},
            {"value": "default", "displayName": "Default", "description": "Opus 5.5 · Recommended"},
        ), [
            {"value": "opus", "label": "Opus 4.8"},
            {"value": "sonnet", "label": "Sonnet 5 (1M context)"},
            {"value": "default", "label": "Default — Opus 5.5"},
        ])

    def test_unknown_gateway_ids_keep_native_name(self):
        self.assertEqual(self.parse({
            "value": "opus", "resolvedModel": "company/prod-v2", "displayName": "Company deployment",
            "description": "Opus 5.5 · Not an authoritative mapping for this gateway",
        }), [{"value": "opus", "label": "Company deployment"}])

    def test_missing_resolution_and_description_does_not_invent_version(self):
        self.assertEqual(self.parse({"value": "opus", "displayName": "Opus"}),
                         [{"value": "opus", "label": "Opus"}])
        self.assertEqual(self.parse({"value": "custom", "displayName": "Custom",
                                    "description": "Faster than Opus 5.5"}),
                         [{"value": "custom", "label": "Custom"}])

    def test_empty_list_is_authoritative_but_missing_schema_is_not(self):
        self.assertEqual(self.parse(), [])
        for invalid in (None, {}, {"models": None}, {"models": {}}, {"models": [None]}):
            with self.subTest(invalid=invalid), self.assertRaises(catalog.ClaudeModelCatalogUnavailable):
                catalog.parse_native_models(invalid)

    def test_bad_rows_duplicates_and_private_metadata_are_not_forwarded(self):
        rows = catalog.parse_native_models({
            "account": {"email": "private@example.com"}, "commands": ["private-command"],
            "models": [None, {"value": "bad id"}, {"value": "bad\n"}, {"value": 42},
                       {"value": "sonnet", "displayName": "Sonnet", "resolvedModel": "claude-sonnet-5",
                        "private": "secret"}, {"value": "sonnet", "displayName": "Duplicate"}],
        })
        self.assertEqual(rows, [{"value": "sonnet", "label": "Sonnet 5"}])

    def test_disabled_rows_are_not_selectable_including_an_all_disabled_list(self):
        self.assertEqual(self.parse({"value": "opus", "disabled": True}), [])
        self.assertEqual(self.parse({"value": "opus", "disabled": True},
                                    {"value": "sonnet", "displayName": "Sonnet"}),
                         [{"value": "sonnet", "label": "Sonnet"}])

    def test_unsafe_or_unbounded_display_fields_fall_back_to_id(self):
        for name in ("Bad\nName", "Bad\u202eName", "x" * 161, 123):
            with self.subTest(name=name):
                self.assertEqual(self.parse({"value": "custom", "displayName": name}),
                                 [{"value": "custom", "label": "custom"}])
        with self.assertRaises(catalog.ClaudeModelCatalogUnavailable):
            catalog.parse_native_models({"models": [{}] * (catalog.MAX_MODELS + 1)})
        self.assertEqual(len(self.parse({"value": "x" * 256})[0]["label"]), 160)
        self.assertEqual(self.parse({"value": "opus", "displayName": "Opus",
                                    "description": "Opus 5 with " + "X" * 200 + " context · Example"}),
                         [{"value": "opus", "label": "Opus"}])



class NativeSDKCatalogCaptureTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_factory_reuses_connect_metadata_without_extra_process(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        from claude_sdk_client import default_claude_sdk_client_factory
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"HOME": root, "PATH": ""}, clear=True):
            catalog.clear_native_models()
            self.addCleanup(catalog.clear_native_models)
            executable = str(Path(root) / "claude")
            Path(executable).write_text("fixture")
            options = ClaudeAgentOptions(cwd=root, cli_path=executable, env={"HOME": root})
            async def connected(client, prompt=None):
                client._query = SimpleNamespace(_initialization_result={
                    "models": [{"value": "opus", "displayName": "Opus", "resolvedModel": "claude-opus-5"}],
                    "account": {"email": "private@example.com"},
                })
            with patch.object(ClaudeSDKClient, "connect", connected), patch("subprocess.Popen") as popen:
                client = default_claude_sdk_client_factory(options)
                await client.connect()
                self.assertEqual(catalog.cached_native_models(executable, env=dict(os.environ)),
                                 [{"value": "opus", "label": "Opus 5"}])
                popen.assert_not_called()

    async def test_failed_connect_does_not_seed_cache(self):
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions
        from claude_sdk_client import default_claude_sdk_client_factory
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"HOME": root, "PATH": ""}, clear=True):
            catalog.clear_native_models()
            self.addCleanup(catalog.clear_native_models)
            options = ClaudeAgentOptions(cwd=root, cli_path="claude", env={"HOME": root})
            with patch.object(ClaudeSDKClient, "connect", side_effect=RuntimeError("failed")):
                client = default_claude_sdk_client_factory(options)
                with self.assertRaises(RuntimeError):
                    await client.connect()
            self.assertEqual(len(catalog._CACHE), 0)


class PassiveNativeModelCacheTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.env = {"HOME": str(self.root), "PATH": "", "ANTHROPIC_API_KEY": "private-key"}
        self.executable = str(self.root / "claude")
        Path(self.executable).write_text("fixture")
        catalog.clear_native_models()
        self.addCleanup(catalog.clear_native_models)
        self.info = {"models": [{"value": "opus", "displayName": "Opus", "resolvedModel": "claude-opus-5"}],
                     "account": {"email": "private@example.com"}, "commands": [{"secret": "private"}]}

    def remember(self, **kwargs):
        args = dict(key=catalog.native_catalog_key(self.executable, self.env),
                    executable=self.executable, env=self.env, cwd=str(self.root))
        args.update(kwargs)
        catalog.remember_native_models(self.info, **args)

    def read(self, env=None):
        return catalog.cached_native_models(self.executable, env=self.env if env is None else env)

    def test_refresh_is_passive_and_only_keeps_models(self):
        with patch("subprocess.Popen") as popen:
            self.assertIsNone(self.read())
            self.remember()
            rows = self.read()
            self.assertEqual(rows, [{"value": "opus", "label": "Opus 5"}])
            rows.clear()
            self.assertEqual(len(self.read()), 1)
            popen.assert_not_called()
        self.assertNotIn("private", repr(catalog._CACHE))

    def test_cache_expires_and_is_bounded(self):
        with patch.object(catalog.time, "monotonic", return_value=1):
            self.remember()
        with patch.object(catalog.time, "monotonic", return_value=1 + catalog.CACHE_TTL_SECONDS):
            self.assertIsNone(self.read())
        for n in range(catalog.CACHE_LIMIT + 5):
            self.env["ANTHROPIC_API_KEY"] = str(n)
            self.remember()
        self.assertEqual(len(catalog._CACHE), catalog.CACHE_LIMIT)

    def test_empty_native_list_is_authoritative(self):
        self.info = {"models": []}
        self.remember()
        self.assertEqual(self.read(), [])

    def test_provider_credentials_config_and_runtime_changes_invalidate(self):
        self.remember()
        for change in ({"ANTHROPIC_API_KEY": "another"}, {"ANTHROPIC_BASE_URL": "https://other.test"},
                       {"CLAUDE_CONFIG_DIR": str(self.root / "other")}):
            self.assertIsNone(self.read({**self.env, **change}))
        settings = self.root / ".claude/settings.json"
        settings.parent.mkdir()
        settings.write_text('{"availableModels": []}')
        self.assertIsNone(self.read())
        self.remember()
        Path(self.executable).write_text("new fixture runtime")
        self.assertIsNone(self.read())

    def test_late_connect_cannot_publish_under_new_configuration(self):
        old_key = catalog.native_catalog_key(self.executable, self.env)
        Path(self.executable).write_text("new runtime")
        self.remember(key=old_key)
        self.assertIsNone(self.read())

    def test_native_startup_counters_do_not_invalidate_but_account_switch_does(self):
        path = self.root / ".claude.json"
        path.write_text(json.dumps({"oauthAccount": {"accountUuid": "one"}, "numStartups": 1}))
        before = catalog.native_catalog_key(self.executable, self.env)
        path.write_text(json.dumps({"oauthAccount": {"accountUuid": "one"}, "numStartups": 2,
                                    "additionalModelOptionsCache": []}))
        self.remember(key=before)
        self.assertIsNotNone(self.read())
        path.write_text(json.dumps({"oauthAccount": {"accountUuid": "two"}, "numStartups": 2}))
        self.assertIsNone(self.read())

    def test_project_settings_are_not_promoted_to_global_catalog(self):
        folder = self.root / "project/.claude"
        folder.mkdir(parents=True)
        (folder / "settings.json").write_text('{"modelPicker": {"options": []}}')
        self.remember(cwd=str(folder.parent / "src"))
        self.assertIsNone(self.read())

    def test_user_settings_can_seed_catalog_and_clear_invalidates(self):
        folder = self.root / ".claude"
        folder.mkdir()
        (folder / "settings.json").write_text('{}')
        self.remember(cwd=str(self.root / "project"))
        self.assertIsNotNone(self.read())
        catalog.clear_native_models()
        self.assertIsNone(self.read())


if __name__ == "__main__":
    unittest.main()
