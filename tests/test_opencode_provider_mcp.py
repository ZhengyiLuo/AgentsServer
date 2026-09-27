"""OpenCode native tool registration, run ownership and delivery isolation."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from opencode_agent_client import build_opencode_mcp_overrides, build_opencode_env_overrides, new_opencode_enforced_agent_name
from tests.test_cursor_provider_mcp import CursorOwnerTests


class ConfigurationTests(unittest.TestCase):
    def test_only_internal_tool_allowed_preserves_endpoint_and_denies(self):
        name = 'agentsdock_' + 'a' * 32
        base = {'provider': {'custom': {'api': 'fixture'}}, 'permission': {'bash': 'deny', '*': 'ask'},
                'mcp': {'other': {'enabled': False}}}
        for mode in ('default', 'plan'):
            agent = new_opencode_enforced_agent_name() if mode == 'plan' else None
            env = build_opencode_env_overrides(mode, json.dumps(base), enforced_agent_name=agent)
            result = json.loads(build_opencode_mcp_overrides(env.get('OPENCODE_CONFIG_CONTENT', json.dumps(base)),
                name=name, command=['python', 'bridge.py', '--mcp'], environment={'secret': 'synthetic'},
                enforced_agent_name=agent)['OPENCODE_CONFIG_CONTENT'])
            self.assertEqual(result['provider'], base['provider'])
            self.assertEqual(result['permission'], {**base['permission'], name + '_run': 'allow'})
            self.assertEqual(result['mcp']['other'], {'enabled': False})
            if agent:
                self.assertEqual(result['agent'][agent]['permission']['bash'], 'deny')
                self.assertEqual(result['agent'][agent]['permission'][name + '_run'], 'allow')
            else:
                self.assertNotIn('agent', result)

    def test_scalar_permission_preserved_and_malformed_config_fails(self):
        args = dict(name='agentsdock_' + '0' * 32, command=['bridge'], environment={})
        result = json.loads(build_opencode_mcp_overrides('{"permission":"deny"}', **args)['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(result['permission']['*'], 'deny')
        for config in ('[]', '{"permission":[]}', '{"mcp":[]}'):
            with self.subTest(config=config), self.assertRaises(ValueError):
                build_opencode_mcp_overrides(config, **args)


class OpenCodeOwnerTests(CursorOwnerTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.active['backend'] = 'opencode'
        self.active['opencode_mcp_owner_token'] = self.active.pop('cursor_mcp_owner_token')

    async def snapshot(self, **kwargs):
        return await self.server.provider_tool_capability_snapshot('chat', 'run_test', backend='opencode', cursor_owner_token=kwargs.get('owner', 'owner'))

    async def test_stopped_replaced_exited_or_foreign_owner_rejected(self):
        for key, value in (('stop_requested', True), ('run_id', 'other'), ('backend', 'cursor'),
                           ('transport', 'app-server'), ('opencode_mcp_owner_token', 'old'), ('provider_turn_ready', False)):
            original = self.active.get(key)
            self.active[key] = value
            with self.subTest(key=key), self.assertRaises(self.server.ProviderToolError):
                await self.snapshot()
            self.active[key] = original
        with self.assertRaises(self.server.ProviderToolError):
            await self.snapshot(owner='old')
        self.active['proc'].returncode = 0
        with self.assertRaises(self.server.ProviderToolError):
            await self.snapshot()

    async def test_delivery_sets_are_unique_and_backend_replacement_rejected(self):
        with patch.object(self.server, 'cross_chat_target_backend_supported', return_value=True):
            caps = self.server.cross_chat_delivery_client_capabilities({'backend': 'opencode'})
        self.assertFalse(self.server.cross_chat_delivery_target_runtime_changed(caps, {'backend': 'opencode'}))
        for other in ('cursor', 'codex', 'claude'):
            self.assertTrue(self.server.cross_chat_delivery_target_runtime_changed(caps, {'backend': other}))
        self.assertTrue(self.server.cross_chat_delivery_target_runtime_changed([], {'backend': 'opencode'}))

    async def test_no_authority_does_not_inject_tool(self):
        self.server.CROSS_CHAT_CAPABILITIES.clear()
        runner = AsyncMock()
        with patch.object(self.server, 'run_opencode_process', runner):
            await self.server.run_opencode('chat', 'run_test', 'hello', {}, None)
        self.assertEqual(runner.call_args.args[3], {})

    async def test_broker_revoked_even_on_cancel_and_cannot_cross_runs(self):
        from cursor_provider_mcp import ENV_PORT, ENV_SECRET
        port = None
        async def runner(session, run, prompt, selected, manifest, **kwargs):
            nonlocal port
            port = int(selected['_opencode_tool_env'][ENV_PORT])
            self.active['opencode_mcp_owner_token'] = selected['_opencode_mcp_owner_token']
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            args = {'helper': 'chats', 'arguments': ['inbox']}
            writer.write((json.dumps({'secret': selected['_opencode_tool_env'][ENV_SECRET], 'method': 'call', 'key': 'call1', 'arguments': args}) + '\n').encode())
            await writer.drain()
            reply = json.loads(await asyncio.wait_for(reader.readline(), 2))
            self.assertFalse(reply['result']['isError'])
            writer.close(); await writer.wait_closed()
            raise asyncio.CancelledError()
        execute = AsyncMock(return_value=('receipt', False))
        with patch.object(self.server, 'run_opencode_process', runner), patch.object(self.server, 'execute_provider_tool_once', execute):
            with self.assertRaises(asyncio.CancelledError):
                await self.server.run_opencode('chat', 'run_test', 'hello', {}, None)
        self.assertEqual(execute.call_args.kwargs['backend'], 'opencode')
        self.assertEqual(execute.call_args.kwargs['cursor_owner_token'], self.active['opencode_mcp_owner_token'])
        with self.assertRaises(OSError):
            await asyncio.open_connection('127.0.0.1', port)
