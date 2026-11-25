"""Native checkpoint admission before changing a custom Codex connection.

The transport/compaction integration is exercised separately by the disposable
native protocol probe. These tests own only synthetic session and task state.
"""
from __future__ import annotations
import ast
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock
import codex_provider
from tests.test_codex_provider_sessions_isolated import make_namespace

SOURCE = Path(__file__).resolve().parents[1] / 'agent_server.py'
NAMES = {'prepare_codex_provider_context', 'settle_codex_provider_context', 'join_task_despite_caller_cancellation'}
TREE = ast.parse(SOURCE.read_text())
CODE = compile(ast.fix_missing_locations(ast.Module(body=[node for node in TREE.body
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES], type_ignores=[])),
    str(SOURCE), 'exec', flags=__import__('__future__').annotations.compiler_flag)

class ProviderContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='provider-context-')
        self.addCleanup(temporary.cleanup)
        self.ns = make_namespace(Path(temporary.name))
        exec(CODE, self.ns)
        self.rollout = Path(temporary.name) / 'synthetic-rollout.jsonl'
        self.rollout.write_text(json.dumps({'type': 'response_item', 'payload': {
            'type': 'reasoning', 'encrypted_content': 'synthetic-ciphertext'}}) + '\n')
        self.ns['codex_rollout_path'] = Mock(return_value=self.rollout)
        self.ns['_post_codex_compact_locked'] = AsyncMock(return_value={'accepted': True, 'operation_id': 'compact-fixture'})
        self.ns['CODEX_PROVIDER_STORE'].save({'base_url': 'https://fixture.invalid/v1', 'api_key': 'synthetic-key', 'model': 'fixture-model'})
        self.chat = await self.ns['STORE'].create(self.ns['CreateSessionRequest'](codex_provider='custom'))
        self.sid = self.chat['id']
        self.chat.update(codex_thread_id='original-native-thread', session_id='original-native-thread',
                         codex_goal={'status': 'paused', 'objective': 'Keep exact goal'})
        self.ns['CODEX_PROVIDER_STORE'].record_thread('original-native-thread', self.ns['CODEX_PROVIDER_STORE'].for_session(self.chat))
        self.chat['_codex_provider_pending'] = self.ns['codex_provider'].runtime_selection({**self.chat, 'codex_provider': 'default'})
        self.before = copy.deepcopy(self.chat)

    async def prepare(self):
        return await self.ns['prepare_codex_provider_context'](self.sid)

    def checkpoint(self):
        with self.rollout.open('a') as stream:
            stream.write(json.dumps({'type': 'compacted', 'payload': {'replacement_history': [
                {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'native summary'}]}]}}) + '\n')

    async def test_switch_waits_for_native_checkpoint_and_retains_source_binding(self):
        self.assertFalse(await self.ns['apply_codex_provider_when_idle'](self.sid))
        context = self.chat['_codex_provider_context']
        self.assertEqual(context['status'], 'running')
        self.assertEqual(self.chat['_codex_provider_pending_reason'], 'preparing_context')
        self.assertEqual(self.chat['codex_provider'], 'custom')
        self.ns['release_idle_codex_manager_session'].assert_not_awaited()
        self.ns['_post_codex_compact_locked'].assert_awaited_once_with(self.sid, provider_handoff_context=context)
        self.ns['CODEX_PROVIDER_STORE'].require_thread('original-native-thread', self.ns['CODEX_PROVIDER_STORE'].for_session(self.chat))
        self.checkpoint()
        await self.ns['settle_codex_provider_context'](self.sid, context, completed=True)
        self.assertTrue(await self.ns['apply_codex_provider_when_idle'](self.sid))
        self.assertEqual(self.chat['codex_provider'], 'default')
        self.assertEqual(self.chat['codex_thread_id'], self.before['codex_thread_id'])
        self.assertEqual(self.chat['codex_goal'], self.before['codex_goal'])
        self.assertNotIn('_codex_provider_context', self.chat)
        self.ns['CODEX_PROVIDER_STORE'].require_thread('original-native-thread', None)

    async def test_running_preparation_does_not_start_another_native_operation(self):
        await self.prepare()
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        self.ns['CODEX_NATIVE_ACTION_TASKS'][(self.sid, 'compact-fixture')] = task
        try:
            self.assertFalse(await self.prepare())
            self.ns['_post_codex_compact_locked'].assert_awaited_once()
        finally:
            gate.set(); await task

    async def test_failed_or_stopped_checkpoint_does_not_switch_or_loop(self):
        await self.prepare()
        await self.ns['settle_codex_provider_context'](self.sid, self.chat['_codex_provider_context'], completed=False)
        for _ in range(3):
            self.assertFalse(await self.ns['apply_codex_provider_when_idle'](self.sid))
        self.assertEqual(self.chat['codex_provider'], 'custom')
        self.assertEqual(self.chat['_codex_provider_pending_reason'], 'context_preparation_failed')
        self.ns['_post_codex_compact_locked'].assert_awaited_once()
        await self.ns['STORE'].update(self.sid, {'codex_provider': 'default'})
        self.assertNotIn('_codex_provider_context', self.chat)
        self.assertFalse(await self.prepare())
        self.assertEqual(self.ns['_post_codex_compact_locked'].await_count, 2)

    async def test_stale_completion_cannot_authorize_changed_native_thread(self):
        await self.prepare()
        old = self.chat['_codex_provider_context']
        self.chat['codex_thread_id'] = self.chat['session_id'] = 'replacement-thread'
        await self.prepare()
        replacement = copy.deepcopy(self.chat['_codex_provider_context'])
        await self.ns['settle_codex_provider_context'](self.sid, old, completed=True)
        self.assertEqual(self.chat['_codex_provider_context'], replacement)

    async def test_cancelling_selection_does_not_resurrect_pending_state(self):
        await self.prepare()
        old = self.chat['_codex_provider_context']
        await self.ns['STORE'].update(self.sid, {'codex_provider': 'custom'})
        await self.ns['settle_codex_provider_context'](self.sid, old, completed=True)
        self.assertNotIn('_codex_provider_pending', self.chat)
        self.assertNotIn('_codex_provider_context', self.chat)

    async def test_same_connection_model_change_and_new_chat_do_not_compact(self):
        self.chat['_codex_provider_pending'] = {**self.ns['codex_provider'].runtime_selection(self.chat), 'model': 'another-model'}
        self.assertTrue(await self.prepare())
        self.chat['codex_thread_id'] = self.chat['session_id'] = None
        self.chat['_codex_provider_pending']['codex_provider'] = 'default'
        self.assertTrue(await self.prepare())
        self.ns['_post_codex_compact_locked'].assert_not_awaited()

    async def test_native_start_failure_keeps_original_connection_for_explicit_retry(self):
        self.ns['_post_codex_compact_locked'].side_effect = RuntimeError('synthetic endpoint unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            await self.prepare()
        self.assertEqual(self.chat['_codex_provider_context']['status'], 'failed')
        self.assertEqual(self.chat['codex_provider'], 'custom')
        self.assertFalse(await self.prepare())
        self.ns['_post_codex_compact_locked'].assert_awaited_once()

    async def test_plaintext_chat_switches_without_spending_a_compaction_request(self):
        self.rollout.write_text(json.dumps({'type': 'response_item', 'payload': {
            'type': 'message', 'content': [{'type': 'input_text', 'text': 'fixture'}]}}) + '\n')
        self.assertTrue(await self.ns['apply_codex_provider_when_idle'](self.sid))
        self.ns['_post_codex_compact_locked'].assert_not_awaited()
        self.assertEqual(self.chat['codex_provider'], 'default')

    async def test_recovery_can_return_to_original_source_before_preparing_switch_back(self):
        # A pre-fix handoff already changed the binding and the target rejected
        # its ciphertext. An explicit selection of the source restores it.
        original = self.ns['codex_provider'].runtime_selection(self.chat)
        self.chat.update(self.chat.pop('_codex_provider_pending'))
        self.ns['CODEX_PROVIDER_STORE'].record_thread('original-native-thread', None)
        self.chat['_codex_provider_pending'] = original
        self.assertTrue(await self.ns['apply_codex_provider_when_idle'](self.sid))
        self.assertEqual(self.chat['codex_provider'], 'custom')
        self.chat['_codex_provider_pending'] = self.ns['codex_provider'].runtime_selection({**self.chat, 'codex_provider': 'default'})
        self.assertFalse(await self.ns['apply_codex_provider_when_idle'](self.sid))
        self.checkpoint()
        await self.ns['settle_codex_provider_context'](self.sid, self.chat['_codex_provider_context'], completed=True)
        self.assertTrue(await self.ns['apply_codex_provider_when_idle'](self.sid))
        self.assertEqual(self.chat['codex_thread_id'], self.before['codex_thread_id'])
        self.assertEqual(self.chat['codex_goal'], self.before['codex_goal'])

    async def test_new_ciphertext_invalidates_an_earlier_ready_checkpoint(self):
        await self.prepare()
        context = self.chat['_codex_provider_context']
        self.checkpoint()
        await self.ns['settle_codex_provider_context'](self.sid, context, completed=True)
        with self.rollout.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {
                'type': 'reasoning', 'encrypted_content': 'new-native-reasoning'}}) + '\n')
        self.assertFalse(await self.prepare())
        self.assertEqual(self.ns['_post_codex_compact_locked'].await_count, 2)
        self.assertNotEqual(self.chat['_codex_provider_context']['id'], context['id'])

    async def test_same_endpoint_credential_rotation_does_not_compact(self):
        self.chat['_codex_provider_pending'] = {**self.ns['codex_provider'].runtime_selection(self.chat),
            'codex_provider_revision': 'new-credential-revision'}
        self.assertTrue(await self.prepare())
        self.ns['_post_codex_compact_locked'].assert_not_awaited()

    async def test_remote_checkpoint_is_not_retried_automatically_or_used_for_handoff(self):
        await self.prepare()
        context = self.chat['_codex_provider_context']
        await self.ns['settle_codex_provider_context'](self.sid, context, completed=True)
        self.assertEqual(self.chat['_codex_provider_context']['status'], 'failed')
        self.assertFalse(await self.prepare())
        self.ns['_post_codex_compact_locked'].assert_awaited_once()

    async def test_failed_checkpoint_persistence_cannot_leave_ready_in_memory(self):
        await self.prepare()
        context = copy.deepcopy(self.chat['_codex_provider_context'])
        self.checkpoint()
        self.ns['STORE'].save.side_effect = OSError('synthetic disk failure')
        with self.assertRaises(OSError):
            await self.ns['settle_codex_provider_context'](self.sid, context, completed=True)
        self.assertEqual(self.chat['_codex_provider_context'], context)
        self.assertEqual(self.chat['_codex_provider_context']['status'], 'running')

    async def test_cancelled_admission_finishes_registering_native_consumer(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_start(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'accepted': True, 'operation_id': 'accepted-native-operation'}
        self.ns['_post_codex_compact_locked'].side_effect = delayed_start
        task = asyncio.create_task(self.prepare())
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.chat['_codex_provider_context']['status'], 'running')
        self.assertEqual(self.chat['_codex_provider_context']['operation_id'], 'accepted-native-operation')


class NativeContextInspectionTests(unittest.TestCase):
    def inspect(self, records):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'fixture.jsonl'
            path.write_text(''.join(json.dumps(record) + '\n' for record in records))
            return codex_provider.context_has_encrypted_items(path)

    def test_native_plaintext_checkpoint_supersedes_old_ciphertext(self):
        encrypted = {'type': 'response_item', 'payload': {'type': 'reasoning', 'encrypted_content': 'fixture'}}
        checkpoint = {'type': 'compacted', 'payload': {'replacement_history': [
            {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'native summary'}]}]}}
        self.assertTrue(self.inspect([encrypted]))
        self.assertFalse(self.inspect([encrypted, checkpoint]))
        self.assertTrue(self.inspect([encrypted, checkpoint, encrypted]))

    def test_remote_encrypted_checkpoint_remains_encrypted(self):
        self.assertTrue(self.inspect([{'type': 'compacted', 'payload': {'replacement_history': [
            {'type': 'compaction', 'encrypted_content': 'fixture'}]}}]))

    def test_unknown_checkpoint_and_rollback_are_inconclusive(self):
        self.assertIsNone(codex_provider.context_has_encrypted_items(None))
        self.assertIsNone(self.inspect([{'type': 'compacted', 'payload': {'message': 'legacy'}}]))
        self.assertIsNone(self.inspect([{'type': 'event_msg', 'payload': {'type': 'thread_rolled_back', 'num_turns': 1}}]))

if __name__ == '__main__': unittest.main()
