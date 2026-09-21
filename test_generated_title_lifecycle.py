"""Server metadata ownership/scheduling with synthetic sessions and providers."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import agent_server as server

REAL_GENERATE_TITLE = server.title_generation.generate_title
REAL_AUTONOMOUS_ADMISSION = server.managed_server_update_scheduled_job_blocker


class GeneratedTitleLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = server.SessionStore()
        self.store.save = AsyncMock()
        self.sess = {
            'id': 'title-chat', 'backend': 'cursor', 'session_id': 'native-parent',
            'title': 'Write a song', '_title_source': 'prompt',
            '_title_auto_value': 'Write a song', '_title_seed': 'Write a song about cats',
            'cwd': '/tmp', 'folder': 'General', 'model': 'selected-model',
        }
        self.store.sessions = {'title-chat': self.sess}
        self.tasks = {}
        self.generator = AsyncMock(return_value='Song About Two Cats')
        self.provider_store = SimpleNamespace(for_session=Mock(return_value=None), require_thread=Mock())
        for target, value in (
            ('STORE', self.store), ('GENERATED_TITLE_TASKS', self.tasks),
            ('GENERATED_TITLE_SLOTS', asyncio.Semaphore(2)), ('SERVER_SHUTTING_DOWN', False),
            ('DELETING_SESSIONS', set()), ('DELETED_SESSION_TOMBSTONES', set()),
            ('HISTORY_SEARCH_DIRTY', set()), ('CODEX_PROVIDER_STORE', self.provider_store),
            ('INTERACTIVE_CHAT_LIVE', SimpleNamespace(notify=Mock())),
        ):
            self.enterContext(patch.object(server, target, value))
        self.enterContext(patch.dict(os.environ, {'AGENTSDOCK_AUTO_TITLES': '1'}))
        self.enterContext(patch.object(server.title_generation, 'generate_title', self.generator))
        self.enterContext(patch.object(server, 'runner_env', return_value={'PATH': '/synthetic'}))
        self.enterContext(patch.object(server, 'resolve_cursor_executable', return_value='cursor'))
        self.admission = self.enterContext(patch.object(
            server, 'managed_server_update_scheduled_job_blocker', return_value=None))
        self.runtime_settings = self.enterContext(patch.object(
            server, 'codex_runtime_settings', return_value=('selected-model', 'medium', '')))
        self.append = self.enterContext(patch.object(server, 'append_event', new_callable=AsyncMock))
        self.event = {'exit_code': 0, 'run_id': 'first-run', 'result_text': 'A playful song'}

    async def asyncTearDown(self):
        await server.close_generated_session_titles()

    def schedule(self, **changes):
        server.schedule_generated_session_title('title-chat', {**self.event, **changes})

    async def drain(self):
        await asyncio.gather(*tuple(self.tasks.values()))
        await asyncio.sleep(0)

    async def test_success_once_no_timeline_event_or_main_thread_mutation(self):
        self.schedule()
        self.schedule()
        await self.drain()
        self.assertEqual(self.sess['title'], 'Song About Two Cats')
        self.assertEqual(self.sess['_title_source'], 'generated')
        self.assertEqual(self.sess['session_id'], 'native-parent')
        self.assertTrue(self.sess['_title_generation_attempted'])
        self.assertNotIn('_title_seed', self.sess)
        self.generator.assert_awaited_once_with('cursor', 'Write a song about cats', 'A playful song',
            executable='cursor', model='selected-model', env={'PATH': '/synthetic'})
        self.append.assert_not_awaited()
        self.schedule()
        self.assertFalse(self.tasks)

    async def test_codex_uses_same_explicit_provider_binding(self):
        self.sess.update(backend='codex', codex_provider='custom', codex_provider_revision='revision')
        self.provider_store.for_session.return_value = {'api_key': 'synthetic'}
        self.schedule()
        await self.drain()
        self.assertEqual(self.generator.call_args.args[0], 'codex')
        self.assertEqual(self.generator.call_args.kwargs['provider_selection'], {'api_key': 'synthetic'})
        self.assertTrue(self.provider_store.for_session.call_args.kwargs['include_key'])

    async def test_codex_server_default_model_is_not_replaced_by_cli_default(self):
        self.sess.update(backend='codex', model=None)
        self.runtime_settings.return_value = ('server-configured-model', 'medium', '')
        self.schedule()
        await self.drain()
        self.assertEqual(self.generator.call_args.kwargs['model'], 'server-configured-model')

    async def test_failure_is_not_retried_and_fallback_remains(self):
        self.generator.side_effect = TimeoutError('synthetic')
        self.schedule()
        await self.drain()
        self.assertEqual(self.sess['title'], 'Write a song')
        self.assertTrue(self.sess['_title_generation_attempted'])
        self.schedule()
        self.assertFalse(self.tasks)
        self.generator.assert_awaited_once()

    async def test_invalid_output_keeps_fallback_without_retry(self):
        self.generator.return_value = None
        self.schedule()
        await self.drain()
        self.assertEqual(self.sess['title'], 'Write a song')
        self.schedule()
        self.assertFalse(self.tasks)

    async def test_terminal_is_persisted_and_returns_without_waiting_for_title(self):
        started = asyncio.Event()
        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.generator.side_effect = pending
        self.append.return_value = self.event
        with patch.object(server, 'refresh_native_session_title', new_callable=AsyncMock), patch.object(
            server, 'finalize_cross_chat_terminal', new_callable=AsyncMock,
        ):
            returned = await asyncio.wait_for(server.append_turn_finished_event('title-chat', self.event), 1)
        self.assertIs(returned, self.event)
        self.append.assert_awaited_once_with('title-chat', 'turn_finished', self.event)
        await asyncio.wait_for(started.wait(), 1)
        self.assertFalse(self.tasks['title-chat'].done())
        self.assertEqual(self.sess['title'], 'Write a song')

    async def test_claim_must_be_persisted_before_provider_usage(self):
        self.store.save.side_effect = OSError('synthetic full disk')
        self.schedule()
        await self.drain()
        self.generator.assert_not_awaited()
        self.assertNotIn('_title_generation_attempted', self.sess)

    async def test_ineligible_sessions_do_not_make_requests(self):
        for fields in (
            {'auto_title_enabled': False}, {'archived': True}, {'parent_id': 'parent'},
            {'fork_from': 'source'}, {'_title_source': 'manual'}, {'_title_source': 'provider'},
            {'_title_source': None}, {'backend': 'claude'}, {'_title_generation_attempted': True},
            {'_title_seed': ''}, {'session_id': None}, {'title': 'Untracked manual name'},
        ):
            original = dict(self.sess)
            with self.subTest(fields=fields):
                self.sess.update(fields)
                self.schedule()
                self.assertFalse(self.tasks)
            self.sess.clear()
            self.sess.update(original)
        self.generator.assert_not_awaited()

    async def test_failed_imported_automated_empty_stopped_turns_are_ignored(self):
        for fields in (
            {'exit_code': 1}, {'exit_code': None}, {'stopped': True}, {'purpose': 'peer-mail'},
            {'job_id': 'job'}, {'imported': True}, {'run_id': 'import_history'}, {'result_text': ''},
        ):
            with self.subTest(fields=fields):
                self.schedule(**fields)
                self.assertFalse(self.tasks)

    async def test_global_opt_out_shutdown_and_deletion(self):
        with patch.dict(os.environ, {'AGENTSDOCK_AUTO_TITLES': 'off'}):
            self.schedule()
        with patch.object(server, 'SERVER_SHUTTING_DOWN', True):
            self.schedule()
        server.DELETING_SESSIONS.add('title-chat')
        self.schedule()
        server.DELETING_SESSIONS.clear()
        server.DELETED_SESSION_TOMBSTONES.add('title-chat')
        self.schedule()
        self.assertFalse(self.tasks)

    async def test_late_result_cannot_overwrite_manual_name_provider_or_model(self):
        for fields in (
            {'_title_source': 'manual'}, {'session_id': 'replacement'}, {'backend': 'codex'},
            {'model': 'different'}, {'codex_provider_revision': 'replacement'},
            {'auto_title_enabled': False}, {'archived': True},
        ):
            original = dict(self.sess)
            async def result(*args, **kwargs):
                self.sess.update(fields)
                return 'Stale title'
            with self.subTest(fields=fields):
                self.generator.side_effect = result
                self.schedule()
                await self.drain()
                self.assertEqual(self.sess['title'], 'Write a song')
            self.sess.clear()
            self.sess.update(original)

    async def test_manual_rename_cancels_pending_generation(self):
        started = asyncio.Event()
        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.generator.side_effect = pending
        self.schedule()
        await started.wait()
        task = self.tasks['title-chat']
        await self.store.update('title-chat', {'title': 'My own title'})
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.sess['title'], 'My own title')

    async def test_opt_out_cancels_pending_generation(self):
        started = asyncio.Event()
        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.generator.side_effect = pending
        self.schedule()
        await started.wait()
        task = self.tasks['title-chat']
        await self.store.update('title-chat', {'auto_title_enabled': False})
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.sess['auto_title_enabled'])

    async def test_shutdown_cancels_and_joins_owned_requests(self):
        started = asyncio.Event()
        async def pending(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        self.generator.side_effect = pending
        self.schedule()
        await started.wait()
        task = self.tasks['title-chat']
        await server.close_generated_session_titles()
        self.assertTrue(task.cancelled())
        self.assertFalse(self.tasks)

    async def test_queued_work_rechecks_ownership_before_spending(self):
        with patch.object(server, 'GENERATED_TITLE_SLOTS', asyncio.Semaphore(0)):
            self.schedule()
            await asyncio.sleep(0)
            self.sess['_title_source'] = 'manual'
            server.GENERATED_TITLE_SLOTS.release()
            await self.drain()
        self.generator.assert_not_awaited()

    async def test_pending_update_or_restart_does_not_admit_optional_titles(self):
        for reason in ('update pending', 'update starting', 'server restarting'):
            self.admission.return_value = reason
            self.schedule()
            self.assertFalse(self.tasks)
            self.assertNotIn('_title_generation_attempted', self.sess)
        self.generator.assert_not_awaited()

    async def test_queued_title_skips_pending_update_without_claiming_attempt(self):
        with patch.object(server, 'GENERATED_TITLE_SLOTS', asyncio.Semaphore(0)):
            self.schedule()
            await asyncio.sleep(0)
            self.admission.return_value = 'update pending'
            server.GENERATED_TITLE_SLOTS.release()
            await self.drain()
        self.generator.assert_not_awaited()
        self.assertNotIn('_title_generation_attempted', self.sess)
        self.assertFalse(self.tasks)
        # A later eligible user turn can retry if the optional request never ran.
        self.admission.return_value = None
        self.schedule()
        await self.drain()
        self.generator.assert_awaited_once()

    async def test_restart_during_saved_claim_cannot_start_provider(self):
        async def saved_claim():
            self.admission.return_value = 'server restarting'
        self.store.save.side_effect = saved_claim
        self.schedule()
        await self.drain()
        self.generator.assert_not_awaited()
        self.assertFalse(self.tasks)

    def real_title_process(self):
        """Exercise the actual adapter/process owner with no provider or account."""
        temporary = tempfile.TemporaryDirectory(prefix='title-lifecycle-process-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        executable = root / 'synthetic-cursor'
        executable.write_text(f'''#!{sys.executable}
import json, os, pathlib, signal, subprocess, sys, time
if '--version' in sys.argv:
    print('2026.09.18-synthetic')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
child = subprocess.Popen([sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])
root = pathlib.Path({str(root)!r})
(root / 'process.json').write_text(json.dumps({{'pid': os.getpid(), 'child': child.pid, 'home': os.environ['HOME']}}))
while not (root / 'release').exists():
    time.sleep(0.01)
child.kill()
child.wait()
print(json.dumps({{'type': 'result', 'subtype': 'success', 'result': 'Song About Two Cats'}}), flush=True)
''')
        executable.chmod(0o700)
        self.enterContext(patch.object(server.title_generation, 'generate_title', REAL_GENERATE_TITLE))
        self.enterContext(patch.object(server, 'resolve_cursor_executable', return_value=str(executable)))
        self.enterContext(patch.object(server, 'runner_env', return_value={
            'HOME': str(root / 'unused-home'), 'CURSOR_API_KEY': 'synthetic-not-a-credential',
        }))
        return root

    async def await_process(self, root):
        async def ready():
            while True:
                try:
                    return json.loads((root / 'process.json').read_text())
                except (FileNotFoundError, json.JSONDecodeError):
                    await asyncio.sleep(0.01)
        return await asyncio.wait_for(ready(), 5)

    @staticmethod
    def process_running(pid):
        result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True)
        return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith('Z')

    @unittest.skipUnless(os.name == 'posix', 'owned process groups require POSIX')
    async def test_actual_title_process_defers_update_and_restart_until_completion(self):
        root = self.real_title_process()
        for target, value in (
            ('SERVER_VERSION', '1.0.0'), ('SERVER_UPDATE_STATUS_FILE', root / 'status.json'),
            ('SERVER_RESTART_STATUS_FILE', root / 'restart.json'),
            ('SERVER_UPDATE_RUNNER', Path(server.__file__).with_name('update_runner.py')),
            ('SERVER_UPDATE_PUBLIC_KEY', Path(server.__file__).with_name('release-public-key.pem')),
            ('AGENT_TOKEN', ''),
            ('BUSY_SESSIONS', set()), ('SERVER_MAINTENANCE_SESSIONS', set()),
            ('QUEUED_TURNS', {}), ('RUN_NOW_TURNS', {}), ('CLAUDE_SDK_MANAGER', None),
            ('CODEX_APP_SERVER_MANAGER', None), ('CODEX_CUSTOM_APP_SERVER_MANAGERS', {}),
        ):
            self.enterContext(patch.object(server, target, value))
        self.enterContext(patch.object(server, 'managed_server_update_scheduled_job_blocker', REAL_AUTONOMOUS_ADMISSION))
        self.enterContext(patch.object(server, 'server_update_is_active', return_value=False))
        self.enterContext(patch.object(server, 'working_tmux_bin', return_value='/synthetic/tmux'))
        self.enterContext(patch.object(server, 'ensure_managed_update_tmux_isolated', return_value=None))
        launcher = self.enterContext(patch.object(server, 'run_tmux'))
        quiesce = self.enterContext(patch.object(server, 'quiesce_managed_update_service_cgroup', new_callable=AsyncMock))
        self.schedule()
        process = await self.await_process(root)
        title_task = self.tasks['title-chat']
        self.assertTrue(self.process_running(process['pid']))
        snapshot = await server.prepare_provider_background_work_snapshot()
        for labels in (server.active_provider_background_work_labels(), server.provider_background_work_labels_from_snapshot(snapshot)):
            self.assertIn('Automatic title for title-chat', labels)
        restart = server.server_restart_blocker_snapshot_locked(tmux_cgroup_state={})
        self.assertEqual(restart['provider_background_count'], 1)
        self.assertTrue(restart['has_forceable_blockers'])
        pending = await server.start_server_update(server.ServerUpdateRequest(version='1.1.0', when_idle=True))
        self.assertEqual(pending['phase'], 'pending')
        self.assertEqual(pending['blocker_counts']['provider_background_tasks'], 1)
        self.assertEqual(pending['blocker_counts']['active_runs'], 0)
        launcher.assert_not_called()
        quiesce.assert_not_awaited()
        self.assertFalse(title_task.done())
        (root / 'release').touch()
        await asyncio.wait_for(title_task, 5)
        await asyncio.sleep(0)
        self.assertEqual(self.sess['title'], 'Song About Two Cats')
        self.assertFalse(server.active_generated_title_work_labels())
        self.assertFalse(server.server_restart_blocker_snapshot_locked(tmux_cgroup_state={})['has_forceable_blockers'])
        self.assertFalse(self.process_running(process['pid']))
        self.assertFalse(self.process_running(process['child']))
        self.assertFalse(Path(process['home']).exists())
        started = await server.advance_pending_server_update_once()
        self.assertEqual(started['phase'], 'starting')
        self.assertEqual(started['schedule_id'], pending['schedule_id'])
        launcher.assert_called_once()

    @unittest.skipUnless(os.name == 'posix', 'owned process groups require POSIX')
    async def test_actual_title_shutdown_reaps_term_ignoring_process_group(self):
        root = self.real_title_process()
        self.schedule()
        process = await self.await_process(root)
        task = self.tasks['title-chat']
        self.assertTrue(self.process_running(process['pid']))
        completed = await server.bounded_shutdown_phase(
            'title-test', server.close_generated_session_titles(), timeout=5)
        self.assertTrue(completed)
        self.assertTrue(task.cancelled())
        self.assertFalse(self.tasks)
        self.assertFalse(self.process_running(process['pid']))
        self.assertFalse(self.process_running(process['child']))
        self.assertFalse(Path(process['home']).exists())

    async def test_reply_is_bounded_before_background_work(self):
        self.schedule(result_text='r' * 10000)
        await self.drain()
        self.assertEqual(len(self.generator.call_args.args[2]), 800)

    async def test_queue_has_a_hard_bound(self):
        with patch.object(server, 'GENERATED_TITLE_TASKS', {str(i): None for i in range(16)}):
            self.schedule()
        self.generator.assert_not_awaited()
        self.assertFalse(self.tasks)

    async def test_at_most_two_independent_provider_requests_run_together(self):
        release = asyncio.Event()
        both_started = asyncio.Event()
        calls = 0
        async def pending(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                both_started.set()
            await release.wait()
            return 'Cat Song'
        self.generator.side_effect = pending
        for index in range(3):
            sid = f'chat-{index}'
            self.store.sessions[sid] = {**self.sess, 'id': sid}
            server.schedule_generated_session_title(sid, self.event)
        await both_started.wait()
        await asyncio.sleep(0)
        self.assertEqual(calls, 2)
        release.set()
        await self.drain()
        self.assertEqual(calls, 3)

    async def test_new_prompt_seed_is_bounded_and_private(self):
        self.sess.update(title='New chat', _title_source='placeholder', _title_auto_value='New chat')
        self.sess.pop('_title_seed')
        await self.store.adopt_auto_title('title-chat', 'First prompt', source='prompt', prompt_seed='u' * 5000)
        self.assertEqual(len(self.sess['_title_seed']), 1600)
        self.assertNotIn('_title_seed', server.public_session(self.sess))
        self.assertNotIn('_title_seed', server.public_session(self.sess, summary=True))

    async def test_enabled_by_default_and_explicit_creation_opt_out(self):
        with patch.object(server, 'ensure_dirs'):
            for enabled in (None, False, True):
                created = await self.store.create(server.CreateSessionRequest(
                    cwd='/tmp', backend='codex', auto_title_enabled=enabled))
                self.assertEqual(created['auto_title_enabled'], enabled is not False)


if __name__ == '__main__':
    unittest.main()
