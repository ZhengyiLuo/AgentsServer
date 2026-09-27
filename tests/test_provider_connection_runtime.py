"""Synthetic credentials only. No native login or model requests."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
import provider_connections as connections


class RuntimeBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = connections.ConnectionStore(Path(self.temp.name) / "private")
        self.input = {"base_url": "https://gateway.invalid/api", "api_key": "synthetic-first-key",
                      "model": "vendor/model", "protocol": "anthropic", "auth_header": "bearer", "expected_revision": 0}

    def chat(self, backend="claude"):
        self.store.write(backend, 0, self.input, "verified")
        session = {"backend": backend, "provider_connection": "custom"}
        selected = self.store.bind(session)
        return {**session, "provider_connection_revision": selected["credential_id"], "model": selected["model"]}

    def test_binding_survives_replacement_and_forget_without_native_fallback(self):
        chat = self.chat()
        self.store.write("claude", 1, {**self.input, "expected_revision": 1, "api_key": "synthetic-second-key"}, "verified")
        self.assertEqual(self.store.for_session(chat)["api_key"], "synthetic-first-key")
        self.store.write("claude", 2, None)
        self.assertEqual(self.store.for_session(chat)["api_key"], "synthetic-first-key")
        with self.assertRaises(HTTPException): self.store.bind({"backend": "claude", "provider_connection": "custom"})

    def test_missing_invalid_cross_backend_and_symlink_bindings_fail_closed(self):
        chat = self.chat()
        for bad in [{**chat, "provider_connection_revision": None}, {**chat, "provider_connection_revision": "../claude"}, {**chat, "backend": "opencode"}]:
            with self.assertRaises(HTTPException): self.store.for_session(bad)
        record = self.store.root / f"claude-{chat['provider_connection_revision']}.json"
        record.unlink()
        record.symlink_to(self.store.root / "claude.json")
        with self.assertRaises(HTTPException): self.store.for_session(chat)

    def test_native_chat_has_no_overrides_or_writes(self):
        self.assertEqual(self.store.for_session({"backend": "claude"}), {})
        self.assertEqual(self.store.claude_overrides({"backend": "claude"}), ({}, None))
        self.assertEqual(self.store.opencode_overrides({"backend": "opencode"}, {}), {})
        self.assertFalse(self.store.root.exists())

    def test_custom_api_key_is_removed_from_error_and_event_projection(self):
        chat = self.chat()
        self.assertEqual(self.store.redact(chat, {"error": "rejected synthetic-first-key", "blocks": ["synthetic-first-key"]}),
                         {"error": "rejected <api-key>", "blocks": ["<api-key>"]})

    def test_claude_private_settings_pin_endpoint_auth_model_and_subagent_limit(self):
        chat = {**self.chat(), "subagent_limit": 2}
        env, path = self.store.claude_overrides(chat)
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
        self.assertNotIn("synthetic-first-key", path)
        self.assertEqual(json.loads(Path(path).read_text())["env"], env)
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "synthetic-first-key")
        self.assertEqual(env["ANTHROPIC_API_KEY"], "")
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "vendor/model")
        self.assertEqual(env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"], "2")

    def test_opencode_uses_dedicated_provider_and_preserves_permission_config(self):
        chat = self.chat("opencode")
        env = self.store.opencode_overrides(chat, {"OPENCODE_CONFIG_CONTENT": '{"permission":{"bash":"deny"}}'})
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        self.assertNotIn("synthetic-first-key", env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(config["permission"]["bash"], "deny")
        self.assertEqual(config["enabled_providers"], ["agentsdock_custom"])
        self.assertEqual(config["model"], "agentsdock_custom/vendor/model")
        self.assertEqual(env["AGENTSDOCK_CUSTOM_API_KEY"], "synthetic-first-key")

    def test_custom_without_model_cannot_borrow_native_default(self):
        self.input["model"] = None
        chat = self.chat()
        with self.assertRaisesRegex(HTTPException, "Choose a model"):
            self.store.claude_overrides(chat)

    def test_public_catalog_has_no_keys_and_session_projection_never_networks(self):
        chat = self.chat()
        with patch.object(connections, "discover_models", return_value=[{"value": "catalog/model", "label": "catalog/model"}]) as discover:
            self.assertTrue(self.store.catalog("claude")["available"])
            self.assertEqual(discover.call_count, 1)
            self.store.catalog("claude")
            self.store.catalog("claude", session=chat)
            self.assertEqual(discover.call_count, 1)
        self.assertNotIn("synthetic-first-key", json.dumps(self.store.public("claude")))
        self.assertEqual(self.store.public("claude")["scope"], "per_chat")

    def test_opencode_free_runtime_is_not_mislabeled_signed_in(self):
        with patch.object(Path, "home", return_value=Path(self.temp.name)), patch.dict(connections.os.environ, {}, clear=True):
            self.assertFalse(connections.native_credentials_present("opencode", {"installed": True, "authenticated": True}))

    def test_custom_claude_success_is_not_proof_of_native_login(self):
        with patch.object(Path, "home", return_value=Path(self.temp.name)), patch.dict(connections.os.environ, {}, clear=True), patch.object(connections.sys, "platform", "linux"):
            self.assertFalse(connections.native_credentials_present("claude", {"installed": True, "authenticated": True}))
