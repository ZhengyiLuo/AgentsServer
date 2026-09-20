"""Synthetic title requests: no provider, account, network, or live state."""
import asyncio
import base64
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import title_generation as titles


def schema():
    return {'definitions': {
        'ThreadStartParams': {'properties': {
            'ephemeral': {'type': 'boolean'},
            **{k: {} for k in ('runtimeWorkspaceRoots', 'baseInstructions', 'developerInstructions',
                               'config', 'sandbox', 'approvalPolicy')},
        }},
        'TurnStartParams': {'properties': {'environments': {
            'type': ['array', 'null'], 'description': 'Empty disables environment access for this turn.',
        }}},
    }}


class FormattingTests(unittest.TestCase):
    def test_bounded_quoted_context(self):
        prompt = titles.title_prompt('u' * 10000, 'a' * 10000)
        data = json.loads(prompt.split('\n\n', 1)[1])
        self.assertEqual(len(data['user_message']), 1600)
        self.assertEqual(len(data['assistant_reply']), 800)
        self.assertIn('not instructions', prompt)

    def test_invalid_titles_are_not_truncated_into_bad_names(self):
        for value in (None, {}, '', 'a\nb', 'x' * 73, 'x\x00x', 'a\u202eb', '```title```', 'New chat'):
            with self.subTest(value=value):
                self.assertIsNone(titles.clean_title(value))
        self.assertEqual(titles.clean_title(' “Somi and Nami 的歌” '), 'Somi and Nami 的歌')

    def test_codex_schema_fails_closed(self):
        self.assertTrue(titles.supports_codex_titles(schema()))
        data = schema()
        data['definitions']['TurnStartParams']['properties']['environments']['description'] = 'Optional'
        self.assertFalse(titles.supports_codex_titles(data))
        self.assertFalse(titles.supports_codex_titles({}))

    def test_cursor_auth_does_not_refresh_expired_tokens(self):
        def credentials(expiry):
            body = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
            return {'accessToken': 'header.' + body + '.signature', 'refreshToken': 'synthetic-refresh'}
        fresh = credentials(time.time() + 3600)
        self.assertEqual(titles.cursor_credentials_snapshot(fresh), fresh)
        for bad in (None, [], {}, credentials(time.time()), {'accessToken': 'opaque', 'refreshToken': 'test'}):
            with self.assertRaises(titles.TitleGenerationError):
                titles.cursor_credentials_snapshot(bad)

    def test_cursor_inherits_only_model_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cli-config.json'
            path.write_text(json.dumps({'selectedModel': {'modelId': 'selected-native-model'},
                'model': {'modelId': 'old-model'}, 'permissions': {'allow': ['Shell(*)']}}))
            self.assertEqual(titles.cursor_default_model({'CURSOR_CONFIG_DIR': directory}), 'selected-native-model')

    def test_native_settings_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'real.json'
            target.write_text('{}')
            link = Path(directory) / 'link.json'
            link.symlink_to(target)
            with self.assertRaises(OSError):
                titles._private_json(link)

    def test_cursor_profile_no_authority_or_inherited_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'original'
            original.mkdir()
            (original / '.cursor').mkdir()
            (original / '.cursor' / 'hooks.json').write_text('dangerous original hooks')
            (original / '.cursor' / 'mcp.json').write_text('original integrations')
            target = root / 'isolated'
            target.mkdir()
            cwd, env = titles.cursor_profile(target, {
                'HOME': str(original), 'PATH': '/bin', 'AGENTSDOCK_TOKEN': 'secret',
                'OPENAI_API_KEY': 'unrelated-secret', 'CURSOR_API_KEY': 'cursor-key',
                'NODE_OPTIONS': '--require unsafe.js', 'CURSOR_CONFIG_DIR': '/unsafe',
            })
            self.assertEqual(cwd, target / 'workspace')
            self.assertEqual(env['HOME'], str(target / 'home'))
            for key in ('AGENTSDOCK_TOKEN', 'OPENAI_API_KEY', 'NODE_OPTIONS'):
                self.assertNotIn(key, env)
            self.assertEqual(env['CURSOR_API_KEY'], 'cursor-key')
            config = Path(env['CURSOR_CONFIG_DIR'])
            self.assertEqual(json.loads((config / 'mcp.json').read_text()), {'mcpServers': {}})
            hooks = json.loads((config / 'hooks.json').read_text())['hooks']
            self.assertTrue(hooks['preToolUse'][0]['failClosed'])
            self.assertIn('sys.exit(2)', hooks['preToolUse'][0]['command'])
            permissions = json.loads((config / 'cli-config.json').read_text())['permissions']
            self.assertEqual(permissions['allow'], [])
            for token in ('Read(/**)', 'Write(/**)', 'Shell(*)', 'Mcp(*:*)', 'WebFetch(*)'):
                self.assertIn(token, permissions['deny'])
            self.assertEqual((original / '.cursor' / 'hooks.json').read_text(), 'dangerous original hooks')


class CursorAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = {'HOME': '/nonexistent-title-test-home', 'CURSOR_API_KEY': 'synthetic-key'}
        self.calls = []
        self.output = json.dumps({'type': 'result', 'subtype': 'success', 'result': 'Song for the cats'})

        async def run(command, **kwargs):
            self.calls.append((command, kwargs))
            return '2026.09.18-tested' if '--version' in command else self.output

        self.enterContext(patch.object(titles, 'run_isolated_command', side_effect=run))

    async def generate(self):
        return await titles.generate_cursor_title('synthetic prompt', executable='cursor', model='chosen-model', env=self.env)

    async def test_separate_request_no_resume_or_escalation_and_cleans_profile(self):
        self.assertEqual(await self.generate(), 'Song for the cats')
        argv, options = self.calls[-1]
        self.assertEqual(argv, ['cursor', '--print', '--mode', 'ask', '--trust', '--output-format', 'stream-json', '--model', 'chosen-model'])
        self.assertEqual(options['prompt'], 'synthetic prompt')
        self.assertFalse(Path(options['cwd']).exists())
        self.assertFalse(Path(options['env']['HOME']).exists())

    async def test_any_tool_attempt_is_rejected_even_with_success_result(self):
        self.output = json.dumps({'type': 'tool_call', 'tool': 'read'}) + '\n' + self.output
        with self.assertRaises(titles.TitleGenerationError):
            await self.generate()
        self.assertFalse(Path(self.calls[-1][1]['env']['HOME']).exists())

    async def test_no_known_isolation_version_no_model_request(self):
        with patch.object(titles, 'run_isolated_command', new_callable=AsyncMock, return_value='2025.10.01-old') as run:
            with self.assertRaises(titles.TitleGenerationError):
                await self.generate()
        self.assertEqual(run.await_count, 1)

    async def test_cancel_cleans_temporary_auth(self):
        async def cancelled(command, **options):
            self.calls.append((command, options))
            raise asyncio.CancelledError
        with patch.object(titles, 'run_isolated_command', side_effect=cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await self.generate()
        self.assertFalse(Path(self.calls[-1][1]['env']['HOME']).exists())


class CodexAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = SimpleNamespace(
            request=AsyncMock(return_value={'config': {'mcp_servers': {'dangerous.server': {}}}}),
            start_thread=AsyncMock(return_value='ephemeral-title'),
            read_thread=AsyncMock(return_value={'ephemeral': True, 'path': None}),
            close=AsyncMock(),
        )
        self.turn = SimpleNamespace(close=AsyncMock(), next_notification=AsyncMock(side_effect=[
            {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'id': 'answer', 'text': 'Cat Song'}}},
            {'method': 'turn/completed', 'params': {'turn': {'status': 'completed'}}},
        ]))
        self.client.start_turn = AsyncMock(return_value=self.turn)
        self.factory = self.enterContext(patch.object(titles, 'CodexAppServerClient', return_value=self.client))

        async def write_schema(command, **kwargs):
            target = Path(command[-1])
            target.mkdir()
            (target / 'codex_app_server_protocol.v2.schemas.json').write_text(json.dumps(schema()))
            return ''

        self.enterContext(patch.object(titles, 'run_isolated_command', side_effect=write_schema))

    async def generate(self):
        return await titles.generate_codex_title('only quoted context', executable='codex', model='selected-model',
                                                env={'HOME': '/auth-home', 'AGENTSDOCK_TOKEN': 'private'})

    async def test_ephemeral_new_thread_no_fork_no_environment(self):
        self.assertEqual(await self.generate(), 'Cat Song')
        options = self.factory.call_args.kwargs
        self.assertNotIn('AGENTSDOCK_TOKEN', options['env_factory']())
        params = self.client.start_thread.call_args.args[0]
        self.assertTrue(params['ephemeral'])
        self.assertEqual(params['model'], 'selected-model')
        self.assertEqual(params['runtimeWorkspaceRoots'], [])
        self.assertEqual(params['config']['mcp_servers'], {'dangerous.server': {'enabled': False}})
        self.assertFalse(params['config']['features.hooks'])
        self.assertEqual(self.client.start_turn.call_args.kwargs['overrides'], {'environments': []})
        self.client.close.assert_awaited_once()
        self.turn.close.assert_awaited_once()
        self.assertFalse(Path(params['cwd']).exists())

    async def test_unconfirmed_ephemeral_refuses_model_request(self):
        self.client.read_thread.return_value = {'ephemeral': False, 'path': '/real/history'}
        with self.assertRaises(titles.TitleGenerationError):
            await self.generate()
        self.client.start_turn.assert_not_awaited()
        self.client.close.assert_awaited_once()

    async def test_tool_notification_is_not_accepted_as_title(self):
        self.turn.next_notification.side_effect = [
            {'method': 'item/started', 'params': {'item': {'type': 'commandExecution'}}},
        ]
        with self.assertRaises(titles.TitleGenerationError):
            await self.generate()
        self.client.close.assert_awaited_once()

    async def test_cancel_reaps_owned_client(self):
        self.turn.next_notification.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.generate()
        self.client.close.assert_awaited_once()

    async def test_turn_close_failure_still_reaps_owned_client(self):
        self.turn.close.side_effect = RuntimeError('synthetic close failure')
        with self.assertRaises(RuntimeError):
            await self.generate()
        self.client.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
