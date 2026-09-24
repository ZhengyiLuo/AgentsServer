"""Real helper subprocess + loopback HTTP + actual mailbox ledger/admission.

Never import/start AgentsServer or invoke a provider. All identities and bodies
are synthetic. This reproduces the missing --message provider-tool call.
"""
from __future__ import annotations

import ast
import asyncio
from contextlib import suppress
import io
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import agentsdock_chats
from tests import test_chat_mailbox_runtime_isolated as mailbox_fixture


class ProviderChatStdinTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mail = mailbox_fixture.ChatMailboxRuntimeTests()
        await self.mail.asyncSetUp()
        self._cleanups.extend(self.mail._cleanups)
        self.mail._cleanups.clear()
        self.posts = []
        self.http_errors = []
        self.http_tasks = set()
        self.pause_http = None
        self.http_paused = asyncio.Event()
        self.http_resume = asyncio.Event()
        self.runtime_envs = {'sender': {}, 'recipient': {}}
        self.http = await asyncio.start_server(self.handle_http, '127.0.0.1', 0)
        self.addAsyncCleanup(self.close_http)
        port = self.http.sockets[0].getsockname()[1]
        self.origin = f'http://127.0.0.1:{port}'
        import tempfile
        self.temp = tempfile.TemporaryDirectory(prefix='provider-stdin-')
        self.addCleanup(self.temp.cleanup)
        self.authorities = {}
        for owner in ('sender', 'recipient'):
            path = Path(self.temp.name) / f'{owner}.json'
            path.write_text(json.dumps({'provider_capability': f'synthetic-{owner}', 'source_session_id': owner}))
            path.chmod(0o600)
            self.authorities[owner] = path
        names = {'validate_provider_tool_input', 'execute_provider_tool',
                 'resolve_provider_tool_arguments', 'provider_tool_argument_value'}
        constants = {'PROVIDER_TOOL_HELPERS', 'PROVIDER_TOOL_MAX_ARGUMENTS',
                     'PROVIDER_TOOL_MAX_ARGUMENT_CHARS', 'PROVIDER_TOOL_MAX_ARGUMENT_BYTES',
                     'PROVIDER_TOOL_MAX_STDIN_BYTES', 'PROVIDER_TOOL_MAX_OUTPUT_BYTES',
                     'PROVIDER_TOOL_TIMEOUT_SECONDS', 'PROVIDER_CROSS_CHAT_ROUTE_ID_RE'}
        nodes = [ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)]
        for node in mailbox_fixture.TREE.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                nodes.append(node)
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
                nodes.append(node)
        self.helper = dict(asyncio=asyncio, sys=sys, re=re, json=json, Path=Path,
                           ProviderToolError=ValueError, CROSS_CHAT_HANDOFF_BODY_MAX_CHARS=100_000,
                           SERVER_ROOT=Path(__file__).resolve().parents[1],
                           redact_provider_tool_output=lambda text, _path: text)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     '<isolated-provider-chat-stdin>', 'exec'), self.helper)
        self.helper['provider_tool_capability_snapshot'] = AsyncMock(
            side_effect=lambda owner, *_args, **_kwargs: (self.authorities[owner], self.runtime_envs[owner].copy()))
        self.helper['agent_runner_env'] = lambda owner, runtime: {
            'PATH': os.environ.get('PATH', ''), 'PYTHONDONTWRITEBYTECODE': '1',
            'AGENTSDOCK_SERVER_URL': self.origin, 'AGENTSDOCK_CROSS_CHAT_MODE': 'async_route_v1',
            'AGENTSDOCK_CHAT_ID': owner,
            **runtime,
        }
        async def terminate(proc, **_kwargs):
            with suppress(ProcessLookupError): proc.kill()
            await proc.wait()
        self.helper['terminate_process_tree'] = terminate

    async def close_http(self):
        self.http.close()
        await self.http.wait_closed()
        self.http_resume.set()
        tasks = list(self.http_tasks)
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)

    async def handle_http(self, reader, writer):
        task = asyncio.current_task()
        self.http_tasks.add(task)
        try:
            headers = (await reader.readuntil(b'\r\n\r\n')).decode('ascii').split('\r\n')
            method, path, _version = headers[0].split()
            fields = {key.lower(): value for key, value in (line.split(': ', 1) for line in headers[1:] if ': ' in line)}
            owner = fields.get('x-agentsdock-provider-capability', '').removeprefix('synthetic-')
            if owner not in self.authorities: raise ValueError('unrecognized fixture identity')
            route = mailbox_fixture.ROUTE if owner == 'sender' else mailbox_fixture.RETURN_ROUTE
            if method == 'GET' and path.startswith('/api/agent/cross-chat/routes?'):
                if self.pause_http == 'before-post':
                    self.http_paused.set()
                    await asyncio.wait_for(self.http_resume.wait(), 5)
                result = {'routes': [{'route_id': route, 'available': True, 'mode': 'async_route_v1'}]}
            elif method == 'POST' and path == f'/api/agent/cross-chat/routes/{route}/handoffs':
                payload = json.loads(await reader.readexactly(int(fields['content-length'])))
                self.posts.append(payload)
                payload.setdefault('reply_to_message_id', None)
                result = await self.mail.ns['submit_provider_route_handoff'](
                    route, SimpleNamespace(**payload), SimpleNamespace(owner=owner))
                if self.pause_http == 'after-post':
                    self.http_paused.set()
                    await asyncio.wait_for(self.http_resume.wait(), 5)
            elif method == 'POST' and path == '/api/agent/cross-chat/exchanges/exchange_' + 'a' * 32 + '/responses':
                payload = json.loads(await reader.readexactly(int(fields['content-length'])))
                self.posts.append(payload)
                result = {'ok': True, 'action': 'response', 'accepted': True}
            else: raise ValueError('unexpected fixture request')
            wire = json.dumps(result).encode()
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n'
                         + f'Content-Length: {len(wire)}\r\n\r\n'.encode() + wire)
            await writer.drain()
        except Exception as exc:
            if not (self.pause_http and isinstance(exc, ConnectionError)):
                self.http_errors.append(type(exc).__name__ + ': ' + str(exc))
                writer.write(b'HTTP/1.1 500 Error\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}')
                with suppress(ConnectionError):
                    await writer.drain()
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            self.http_tasks.discard(task)

    async def invoke(self, owner, body, *, backend='codex', verb='send', parent=None, key='synthetic-send-1', inline=False):
        route = mailbox_fixture.ROUTE if owner == 'sender' else mailbox_fixture.RETURN_ROUTE
        args = [verb, '--route', route, '--idempotency-key', key]
        if parent: args += ['--reply-to', parent]
        value = {'helper': 'chats', 'arguments': args}
        if inline: args += ['--message', body]
        else: value['stdin'] = body
        output, error = await self.helper['execute_provider_tool'](owner, f'{owner}-run', value, backend=backend)
        self.assertEqual(self.http_errors, [])
        self.assertFalse(error, output)
        result = json.loads(output)
        self.assertTrue(result['accepted'])
        self.assertRegex(result['message_id'], r'^handoff_[a-f0-9]{32}$')
        return result

    async def test_reported_stdin_reply_round_trip_has_both_events_and_idle_wakes(self):
        self.mail.set_recipient('idle')
        opener = await self.invoke('sender', 'Synthetic opening\n第二行', backend='claude')
        events = self.mail.ns['append_cross_chat_event_once'].await_args_list
        self.assertEqual([(c.args[0], c.args[2]) for c in events], [
            ('sender', 'chat_conversation_message_registered'), ('recipient', 'chat_conversation_message_received')])
        self.mail.launches = []
        async def start(session_id, req, **kwargs):
            self.assertEqual(req.purpose, 'chat_mailbox_wake')
            self.assertEqual(req.display_prompt, '')
            self.assertFalse(kwargs['queue_if_busy'])
            claim = kwargs['mailbox_wake_claim']
            self.assertEqual(claim['target_session_id'], session_id)
            run_id = f'synthetic-wake-{len(self.mail.launches) + 1}'
            await self.mail.ns['admit_mailbox_wake'](session_id, claim, run_id)
            self.mail.launches.append({'session_id': session_id, 'run_id': run_id})
            self.mail.ns['ACTIVE'][session_id] = {'run_id': run_id}
            self.mail.ns['CURRENT_TURNS'][session_id] = {'run_id': run_id}
            self.mail.ns['BUSY_SESSIONS'].add(session_id)
        self.mail.ns['_start_turn_locked'] = AsyncMock(side_effect=start)
        await self.mail.drain_idle_check()
        self.assertEqual(len(self.mail.launches), 1)
        await self.mail.read()
        # Busy receiving agent can reply through its own exact active pair.
        self.mail.capabilities['recipient']['source_run_id'] = self.mail.ns['ACTIVE']['recipient']['run_id']
        reply = await self.invoke('recipient', 'Synthetic reply\nNo private content.', parent=opener['message_id'])
        replay = await self.invoke('recipient', 'Synthetic reply\nNo private content.', parent=opener['message_id'])
        self.assertTrue(replay['duplicate'])
        self.assertEqual(reply['message_id'], replay['message_id'])
        row = (await self.mail.ledger.mailbox_envelopes(message_id=reply['message_id']))[0]
        self.assertEqual(row['target_session_id'], 'sender')
        self.assertEqual(row['reply_to_message_id'], opener['message_id'])
        self.assertIsNone(row['read_at'])
        # A busy sender is not interrupted; its normal idle transition wakes it.
        self.assertEqual(len(self.mail.launches), 1)
        for mapping in ('ACTIVE', 'CURRENT_TURNS'): self.mail.ns[mapping].pop('sender', None)
        self.mail.ns['BUSY_SESSIONS'].discard('sender')
        await self.mail.ns['_start_next_queued_turn_locked']('sender', admission_backend='claude')
        self.assertEqual(len(self.mail.launches), 2)
        with self.mail.ledger._transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM chat_mailbox_messages').fetchone()[0], 2)
        for target, event in (('recipient', 'chat_conversation_message_registered'), ('sender', 'chat_conversation_message_received')):
            self.assertTrue(any(c.args[0] == target and c.args[1]['id'] == reply['message_id'] and c.args[2] == event
                                for c in self.mail.ns['append_cross_chat_event_once'].await_args_list))

    async def test_ask_stdin_and_explicit_message_preserve_exact_body(self):
        for backend in ('codex', 'claude'):
            for inline in (False, True):
                body = 'Synthetic <tag> & "quoted" \n unicode 世界'
                receipt = await self.invoke('sender', body, backend=backend, verb='ask', inline=inline, key=f'{backend}-{inline}')
                row = await self.mail.ledger.get(receipt['message_id'])
                self.assertEqual(row['body'], body)
                self.assertFalse(receipt['execution_started'])
                self.assertNotIn('wait_for_response', self.posts[-1])

    async def test_cancelled_receipt_retry_round_trips_through_real_helpers(self):
        for backend in ('codex', 'claude'):
            with self.subTest(backend=backend):
                key = f'cancelled-helper-{backend}'
                first = await self.invoke('sender', 'Synthetic cancelled message', backend=backend, key=key)
                await self.mail.ledger.mailbox_call('cancel_message', first['message_id'], now=mailbox_fixture.NOW)
                self.mail.set_recipient('idle')
                self.mail.ns['schedule_next_queued_turn'].reset_mock()
                repeated = await self.invoke('sender', 'Synthetic cancelled message', backend=backend, key=key)
                self.assertEqual((repeated['message_id'], repeated['state'], repeated['duplicate']),
                                 (first['message_id'], 'cancelled', True))
                self.assertFalse(repeated['execution_started'])
                self.mail.ns['schedule_next_queued_turn'].assert_not_called()
        with self.mail.ledger._transaction() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM chat_mailbox_messages').fetchone()[0], 2)

    async def test_respond_current_stdin_uses_actual_async_and_legacy_grants(self):
        self.mail.set_recipient('busy')
        for backend in ('codex', 'claude'):
            for legacy in (False, True):
                with self.subTest(backend=backend, legacy=legacy):
                    self.runtime_envs['recipient'] = ({
                        'AGENTSDOCK_CROSS_CHAT_RESPONSE_EXCHANGE_ID': 'exchange_' + 'a' * 32,
                        'AGENTSDOCK_CROSS_CHAT_RESPONSE_INBOUND_LEG_ID': 'leg_' + 'b' * 32,
                        'AGENTSDOCK_CROSS_CHAT_RESPONSE_FOLLOWUP': 'none',
                    } if legacy else {
                        'AGENTSDOCK_CROSS_CHAT_RESPONSE_MODE': 'async_route_v1',
                        'AGENTSDOCK_CROSS_CHAT_RESPONSE_ROUTE_ID': mailbox_fixture.RETURN_ROUTE,
                    })
                    body = f'Synthetic {backend} reply\nUnicode 世界, legacy={legacy}'
                    value = {'helper': 'chats', 'arguments': ['respond-current', '--idempotency-key', f'{backend}-{legacy}'], 'stdin': body}
                    output, error = await self.helper['execute_provider_tool'](
                        'recipient', 'recipient-run', value, backend=backend)
                    self.assertFalse(error, output)
                    receipt = json.loads(output)
                    self.assertIs(receipt['accepted'], True)
                    self.assertEqual(self.posts[-1]['body'], body)
                    self.assertNotIn('wait_for_response', self.posts[-1])
                    if legacy:
                        self.assertEqual(self.posts[-1]['inbound_leg_id'], 'leg_' + 'b' * 32)
                        self.assertIs(self.posts[-1]['request_response'], False)
                    else:
                        self.assertEqual(receipt['route_id'], mailbox_fixture.RETURN_ROUTE)
                        row = await self.mail.ledger.get(receipt['message_id'])
                        self.assertEqual((row['source_session_id'], row['target_session_id']), ('recipient', 'sender'))
                        self.assertEqual(row['body'], body)
        self.assertEqual(self.http_errors, [])

    async def test_respond_current_missing_grant_fails_before_any_request(self):
        for backend in ('codex', 'claude'):
            with self.subTest(backend=backend), self.assertRaisesRegex(ValueError, 'grant is unavailable'):
                await self.helper['execute_provider_tool']('recipient', 'recipient-run', {
                    'helper': 'chats', 'arguments': ['respond-current'], 'stdin': 'Synthetic reply',
                }, backend=backend)
        self.assertEqual(self.posts, [])
        self.assertEqual(self.http_errors, [])

    async def test_cancelled_stdin_helper_does_not_report_success_or_retry_after_commit(self):
        for stage in ('before-post', 'after-post'):
            with self.subTest(stage=stage):
                self.pause_http = stage
                self.http_paused.clear()
                self.http_resume.clear()
                key = f'synthetic-cancel-{stage}'
                value = {'helper': 'chats', 'arguments': ['send', '--route', mailbox_fixture.ROUTE,
                         '--idempotency-key', key], 'stdin': f'Synthetic cancellation {stage}'}
                call = asyncio.create_task(self.helper['execute_provider_tool'](
                    'sender', 'sender-run', value, backend='codex'))
                try:
                    await asyncio.wait_for(self.http_paused.wait(), 5)
                    call.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(call, 5)
                finally:
                    self.http_resume.set()
                    if not call.done():
                        call.cancel()
                    await asyncio.gather(call, return_exceptions=True)
                    if self.http_tasks:
                        await asyncio.wait_for(asyncio.gather(*list(self.http_tasks)), 5)
                self.pause_http = None
                self.assertEqual(len(self.posts), 0 if stage == 'before-post' else 1)
                self.assertEqual(self.http_errors, [])
                if stage == 'after-post':
                    # Cancellation cannot retract an already committed POST.
                    # Retrying the same request identity must return its receipt,
                    # not store another message or falsely claim cancellation undid it.
                    receipt = await self.invoke('sender', value['stdin'], key=key)
                    self.assertIs(receipt['duplicate'], True)
                    with self.mail.ledger._transaction() as connection:
                        self.assertEqual(connection.execute('SELECT COUNT(*) FROM chat_mailbox_messages').fetchone()[0], 1)

    async def test_invalid_or_ambiguous_bodies_never_launch_or_store(self):
        validate = self.helper['validate_provider_tool_input']
        for args, stdin in [(['send', '--route', mailbox_fixture.ROUTE, '--message', 'one'], 'two'),
                            (['send', '--route', mailbox_fixture.ROUTE, '--message-stdin'], ''),
                            (['inbox'], 'misplaced body'), (['send'], '\0'), (['send'], '   ')]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                validate({'helper': 'chats', 'arguments': args, 'stdin': stdin})
        self.helper['provider_tool_capability_snapshot'].assert_not_awaited()
        self.assertEqual(self.posts, [])

    def test_explicit_stdin_rejects_empty_invalid_utf8_and_oversize_before_send(self):
        for raw in (b'', b'  ', b'\xff', b'\0', b'x' * 100_001):
            stream = io.TextIOWrapper(io.BytesIO(raw), encoding='utf-8')
            with self.subTest(size=len(raw)), patch.object(sys, 'stdin', stream), self.assertRaises(agentsdock_chats.ChatsCLIError):
                agentsdock_chats.read_message_stdin()


if __name__ == '__main__':
    unittest.main()
