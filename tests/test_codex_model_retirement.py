"""Model retirement is auth-scoped; migration must preserve native history."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import agent_server
import codex_auth


class CodexModelRetirementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.native = self.root / '.codex'
        self.native.mkdir()
        self.env = {'HOME': str(self.root), 'CODEX_HOME': str(self.native)}
        self.write_auth({'auth_mode': 'chatgpt', 'tokens': {'access_token': 'fixture-only'}})
        self.enterContext(patch.object(agent_server, 'codex_app_server_env', return_value=self.env))
        self.enterContext(patch.object(agent_server, 'DEFAULT_CWD', str(self.root)))
        self.enterContext(patch.object(agent_server, 'CODEX_MODEL_MIGRATION_TARGET', 'gpt-6-luna'))
        self.enterContext(patch.object(agent_server, 'CODEX_DEFAULT_MODEL', 'gpt-6-luna'))
        self.enterContext(patch.object(agent_server, 'codex_user_config_defaults', return_value=('', '', '')))

    def write_auth(self, payload):
        (self.native / 'auth.json').write_text(json.dumps(payload))

    def mode(self, observed=''):
        return codex_auth.native_model_auth_mode(self.env, cwd=str(self.root), observed_mode=observed)

    def test_auth_kind_is_passive_and_api_key_wins_over_old_chatgpt_tokens(self):
        self.assertEqual(self.mode(), 'chatgpt')
        self.write_auth({'OPENAI_API_KEY': 'fixture-only', 'auth_mode': 'chatgpt', 'tokens': {}})
        self.assertEqual(self.mode('chatgpt'), 'apiKey')
        self.assertEqual(agent_server.codex_retired_model_replacements(), {})
        self.assertEqual(agent_server.codex_available_model('gpt-5.5'), 'gpt-5.5')

    def test_custom_native_provider_profile_and_api_environment_are_not_filtered(self):
        for config in ('model_provider="local"', 'profile="work"\n[profiles.work]\nmodel_provider="local"',
                       '[model_providers.openai]\nbase_url="https://gateway.example/v1"'):
            (self.native / 'config.toml').write_text(config)
            self.assertEqual(self.mode('chatgpt'), 'custom')
            self.assertEqual(agent_server.codex_retired_model_replacements(), {})
        (self.native / 'config.toml').unlink()
        for key in ('OPENAI_API_KEY', 'CODEX_API_KEY'):
            with self.subTest(key=key), patch.dict(self.env, {key: 'fixture-only'}):
                self.assertEqual(self.mode('chatgpt'), 'apiKey')

    def test_unknown_auth_does_not_globally_ban_models(self):
        for data in ('{', '[]', '{}'):
            (self.native / 'auth.json').write_text(data)
            self.assertEqual(self.mode('chatgpt'), 'unknown')
            self.assertEqual(agent_server.codex_retired_model_replacements(), {})
        (self.native / 'auth.json').unlink()
        self.assertEqual(self.mode('chatgpt'), 'unknown')

    def test_opaque_store_ignores_leftover_auth_file_and_uses_only_recent_native_probe(self):
        (self.native / 'config.toml').write_text('cli_auth_credentials_store="keyring"')
        self.assertEqual(self.mode(), 'unknown')
        self.assertEqual(self.mode('apiKey'), 'apiKey')
        with patch.dict(agent_server.RUNTIME_DIAGNOSTICS, {'codex': {'backend': 'codex', 'checked_at_epoch': time.time(), '_codex_auth_mode': 'chatgpt'}}, clear=True):
            self.assertIn('gpt-5.5', agent_server.codex_retired_model_replacements())
            agent_server.record_runtime_success('codex')
            self.assertIn('gpt-5.5', agent_server.codex_retired_model_replacements())
            agent_server.RUNTIME_DIAGNOSTICS['codex']['_codex_auth_mode_checked_at_epoch'] = 0
            self.assertEqual(agent_server.codex_retired_model_replacements(), {})
            fresh = agent_server.runtime_diagnostic_payload('codex', 'ready', installed=True, authenticated=True)
            with patch.object(agent_server, 'probe_runtime', return_value=fresh) as probe:
                agent_server.runtime_diagnostic('codex')
            probe.assert_called_once_with('codex')
            agent_server.expire_codex_login_diagnostic()
            self.assertEqual(agent_server.codex_retired_model_replacements(), {})

    def test_catalog_filters_retired_config_and_fallback_and_publishes_migration_metadata(self):
        for native_result in ({'models': []}, {'models': [
            {'slug': 'gpt-5.5', 'visibility': 'list', 'supported_in_api': True},
            {'slug': 'gpt-6-luna', 'visibility': 'list', 'supported_in_api': True},
        ]}):
            with patch.object(agent_server, 'run_catalog_command', return_value=json.dumps(native_result)), patch.object(
                agent_server, 'codex_user_config_defaults', return_value=('gpt-5.5', 'xhigh', ''),
            ):
                catalog = agent_server.discover_codex_catalog()
                launched, effort, _ = agent_server.codex_runtime_settings({})
            values = {item['value'] for item in catalog['models']}
            self.assertFalse(values & codex_auth.CHATGPT_RETIRED_MODELS)
            self.assertNotIn('gpt-5.5', catalog['model_efforts'])
            self.assertEqual(catalog['default_model'], launched)
            self.assertEqual(launched, 'gpt-6-luna')
            self.assertEqual(catalog['model_replacements']['gpt-5.5'], launched)
            self.assertEqual(effort, 'xhigh')

    def test_catalog_can_migrate_to_visible_native_model_when_fallback_is_absent(self):
        with patch.object(agent_server, 'run_catalog_command', return_value=json.dumps({'models': [
            {'slug': 'gpt-5.5', 'priority': 0}, {'slug': 'available-native-model', 'priority': 1},
        ]})), patch.object(agent_server, 'codex_user_config_defaults', return_value=('gpt-5.5', '', '')):
            catalog = agent_server.discover_codex_catalog()
            self.assertEqual(catalog['default_model'], 'available-native-model')
            self.assertEqual(agent_server.codex_runtime_settings({'model': 'gpt-5.5'})[0], 'available-native-model')

    def test_api_catalog_preserves_explicit_gpt55(self):
        self.write_auth({'OPENAI_API_KEY': 'fixture-only'})
        with patch.object(agent_server, 'run_catalog_command', return_value='{"models": []}'), patch.object(
            agent_server, 'codex_user_config_defaults', return_value=('gpt-5.5', 'xhigh', ''),
        ):
            catalog = agent_server.discover_codex_catalog()
            self.assertEqual(catalog['default_model'], 'gpt-5.5')
            self.assertIn('gpt-5.5', {item['value'] for item in catalog['models']})
            self.assertEqual(catalog['model_replacements'], {})
            self.assertEqual(agent_server.codex_runtime_settings({})[0], 'gpt-5.5')

    def test_preview_keeps_native_ids_and_never_changes_original_history(self):
        original = {'id': 'fixture-chat', 'backend': 'codex', 'model': 'gpt-5.5', 'effort': 'high',
                    'codex_thread_id': 'fixture-native', 'session_id': 'fixture-native', 'last_event_seq': 25}
        preview = agent_server.preview_session_runtime_update(original, {'model': 'gpt-5.5'})
        self.assertEqual(preview['model'], 'gpt-6-luna')
        for key in ('id', 'codex_thread_id', 'session_id', 'last_event_seq'):
            self.assertEqual(preview[key], original[key])
        self.assertEqual(original['model'], 'gpt-5.5')

    def test_retired_saved_effort_is_clamped_but_invalid_input_still_fails(self):
        session = {'backend': 'codex', 'model': 'gpt-5.5', 'effort': 'ultra'}
        preview = agent_server.preview_session_runtime_update(session, {'model': 'gpt-5.5', 'effort': 'ultra'})
        self.assertEqual((preview['model'], preview['effort']), ('gpt-6-luna', 'max'))
        with self.assertRaises(agent_server.HTTPException):
            agent_server.preview_session_runtime_update(session, {'model': 'gpt-5.5', 'effort': 'not-an-effort'})

    async def test_admission_migrates_once_without_forking_or_deleting_history(self):
        original = {'id': 'fixture-chat', 'backend': 'codex', 'model': 'gpt-5.5', 'effort': 'high',
                    'codex_thread_id': 'fixture-native', 'session_id': 'fixture-native', 'last_event_seq': 25}
        update = AsyncMock(side_effect=lambda sid, value: {**original, **value})
        with patch.object(agent_server.STORE, 'update', update), patch.object(agent_server, 'append_event', AsyncMock()) as event:
            migrated = await agent_server.migrate_retired_codex_selection('fixture-chat', original)
            unchanged = await agent_server.migrate_retired_codex_selection('fixture-chat', migrated)
        self.assertEqual(migrated, unchanged)
        self.assertEqual(migrated['model'], 'gpt-6-luna')
        self.assertEqual(migrated['codex_thread_id'], original['codex_thread_id'])
        self.assertEqual(migrated['last_event_seq'], 25)
        update.assert_awaited_once()
        event.assert_awaited_once()
        self.assertEqual(event.call_args.args[1], 'runtime_model_migrated')

    async def test_standalone_scheduled_migration_does_not_mutate_parent(self):
        original = {'id': 'fixture-chat', 'backend': 'codex', 'model': 'gpt-5.5', 'effort': 'high'}
        with patch.object(agent_server.STORE, 'update', AsyncMock()) as update, patch.object(agent_server, 'append_event', AsyncMock()):
            migrated = await agent_server.migrate_retired_codex_selection('fixture-chat', original, standalone=True)
        self.assertEqual(migrated['model'], 'gpt-6-luna')
        self.assertEqual(original['model'], 'gpt-5.5')
        update.assert_not_awaited()

    async def test_custom_and_api_sessions_are_never_migrated(self):
        for mode in ('custom', 'api'):
            session = {'id': 'fixture-chat', 'backend': 'codex', 'model': 'gpt-5.5'}
            if mode == 'custom':
                session['codex_provider'] = 'custom'
            else:
                self.write_auth({'OPENAI_API_KEY': 'fixture-only'})
            with patch.object(agent_server.STORE, 'update', AsyncMock()) as update, patch.object(agent_server, 'append_event', AsyncMock()) as event:
                result = await agent_server.migrate_retired_codex_selection('fixture-chat', session)
            self.assertEqual(result, session)
            update.assert_not_awaited()
            event.assert_not_awaited()
