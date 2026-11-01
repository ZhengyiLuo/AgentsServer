"""Endpoint-save to native-owner handoff, using isolated production helpers."""
from __future__ import annotations
import ast
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from codex_app_server import CodexAppServerRequestError
from tests.test_codex_provider_sessions_isolated import make_namespace
NAMES={'replace_codex_provider_settings','schedule_codex_subagent_limit_application','apply_pending_codex_subagent_limit','reload_session_provider'}
TREE=ast.parse(Path(__file__).resolve().parents[1].joinpath('agent_server.py').read_text())
CODE=compile(ast.fix_missing_locations(ast.Module(body=[n for n in TREE.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in NAMES],type_ignores=[])),'agent_server.py','exec',flags=__import__('__future__').annotations.compiler_flag)
class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='endpoint-refresh-');self.addCleanup(tmp.cleanup)
        self.ns=make_namespace(Path(tmp.name))
        self.ns.update(CODEX_SUBAGENT_LIMIT_TASKS={},CODEX_SUBAGENT_LIMIT_REQUESTED=set(),CODEX_MANAGER_CLOSING=False,
            DELETING_SESSIONS=set(),CODEX_NATIVE_ACTION_TASKS={},existing_cwd=lambda path:path,
            retain_codex_manager_caller=Mock(),pin_codex_app_server_thread=AsyncMock(),unpin_codex_app_server_thread=AsyncMock(),
            apply_codex_subagent_limit_when_idle=AsyncMock(),ensure_session_not_deleting=Mock(),
            managed_server_update_admission_blocker=lambda:None,session_registry_has_live_tasks=lambda *args:False,
            clear_stale_provider_interactions=AsyncMock(return_value=0),codex_runtime_snapshot=AsyncMock(return_value={}),
            CLAUDE_TRANSPORT='sdk',CLAUDE_TRANSPORT_PRINT='print')
        exec(CODE,self.ns)
        self.selection={'base_url':'https://first.example.invalid/v1','api_key':'fake-initial','model':'custom-model'}
        self.store=self.ns['CODEX_PROVIDER_STORE'];self.store.save(self.selection)
        self.chat=await self.ns['STORE'].create(self.ns['CreateSessionRequest'](codex_provider='custom'));self.sid=self.chat['id']
        self.chat.update(codex_thread_id='thread-parent',session_id='thread-parent',codex_goal={'status':'paused','objective':'preserved'})
        self.original_revision=self.chat['codex_provider_revision'];self.store.record_thread('thread-parent',self.store.for_session(self.chat))
        self.loaded=True
        self.manager=SimpleNamespace(generation=1,_agentsdock_provider_revision=self.original_revision,is_thread_loaded=lambda tid:self.loaded,active_turn=lambda tid:None)
        self.ns['existing_codex_app_server_manager']=lambda sess:self.manager if self.loaded else None
        async def release(manager, sid, **kwargs):
            self.loaded=False
            await self.ns['release_codex_provider_writers'](manager,sid,['thread-parent'])
            return True
        self.release=AsyncMock(side_effect=release);self.ns['release_idle_codex_manager_session']=self.release
    async def asyncTearDown(self):
        tasks=tuple(self.ns['CODEX_SUBAGENT_LIMIT_TASKS'].values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    async def settle(self):
        for _ in range(10):
            tasks=tuple(self.ns['CODEX_SUBAGENT_LIMIT_TASKS'].values())
            if not tasks:return
            await asyncio.gather(*tasks)
        self.fail('did not settle')
    async def save(self,key='new-key',url=None):
        await self.ns['replace_codex_provider_settings']({**self.selection,'api_key':key,'base_url':url or self.selection['base_url']})
    def actual(self):return self.store.for_session(self.chat,include_key=True)
    async def test_save_rotates_same_url_credentials_and_preserves_history_goal(self):
        await self.save();await self.settle()
        self.assertEqual(self.actual()['api_key'],'new-key');self.assertEqual(self.chat['session_id'],'thread-parent')
        self.ns['release_codex_provider_writers'].assert_awaited_once_with(self.manager,self.sid,['thread-parent'])
        self.assertEqual(self.chat['codex_goal'],{'status':'paused','objective':'preserved'})
        self.store.require_thread('thread-parent',self.actual());self.release.assert_awaited_once()
        self.assertFalse(self.store.control(self.chat)['pending'])
        packet=self.ns['broadcast_provider_runtime_changed'].await_args.args[1]
        self.assertEqual(packet['runtime'],'codex_provider');self.assertNotIn('new-key',str(packet))
    async def test_busy_two_saves_apply_latest_after_original_task_finishes(self):
        gate=asyncio.Event();task=asyncio.create_task(gate.wait())
        self.ns['SESSION_TURN_TASKS'][self.sid]={task};self.ns['BUSY_SESSIONS'].add(self.sid)
        await self.save('second');await self.save('third','https://third.example.invalid/v1');await self.settle()
        self.assertEqual(self.actual()['api_key'],'fake-initial');self.release.assert_not_awaited()
        self.ns['BUSY_SESSIONS'].clear();gate.set();await task;await asyncio.sleep(0);await self.settle()
        self.assertEqual(self.actual()['api_key'],'third');self.assertEqual(self.actual()['base_url'],'https://third.example.invalid/v1')
    async def test_goal_and_descendants_keep_original_owner_until_idle(self):
        self.chat['codex_goal']['status']='active';await self.save();await self.settle();self.release.assert_not_awaited()
        self.chat['codex_goal']['status']='paused';self.ns['codex_session_has_live_subagents'].return_value=True
        self.ns['schedule_codex_subagent_limit_application'](self.sid);await self.settle();self.release.assert_not_awaited()
        self.ns['codex_session_has_live_subagents'].return_value=False
        self.ns['schedule_codex_subagent_limit_application'](self.sid);await self.settle();self.assertEqual(self.actual()['api_key'],'new-key')
    async def test_reload_adopts_setting_saved_before_this_fix(self):
        self.store.save({**self.selection,'api_key':'historical-current'})
        result=await self.ns['reload_session_provider'](self.sid)
        self.assertTrue(result['reloaded']);self.assertEqual(self.actual()['api_key'],'historical-current')
        self.assertEqual(self.chat['session_id'],'thread-parent')
    async def test_reload_recovers_pending_unarchive_before_reporting_unloaded(self):
        self.chat['_codex_provider_unarchive_pending']=['thread-parent']
        self.loaded=False
        self.ns['codex_app_server_manager']=AsyncMock(return_value=self.manager)
        self.manager.request=AsyncMock(return_value={})
        self.manager.list_descendant_threads=AsyncMock(return_value=[])
        self.ns['join_task_despite_caller_cancellation']=lambda task:task
        node=next(n for n in TREE.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='release_codex_provider_writers')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'agent_server.py','exec',flags=__import__('__future__').annotations.compiler_flag),self.ns)
        result=await self.ns['reload_session_provider'](self.sid)
        self.assertTrue(result['reloaded'])
        self.assertNotIn('_codex_provider_unarchive_pending',self.chat)
        self.assertEqual([x.args[0] for x in self.manager.request.await_args_list],
            ['thread/unarchive','thread/archive','thread/unarchive'])

    async def test_restart_applies_persisted_pending_without_loaded_owner(self):
        self.ns['BUSY_SESSIONS'].add(self.sid);await self.save();await self.settle();self.ns['BUSY_SESSIONS'].clear();self.loaded=False
        self.assertTrue(await self.ns['apply_codex_provider_when_idle'](self.sid));self.assertEqual(self.actual()['api_key'],'new-key')
    async def test_reset_never_uses_normal_account_and_restore_adopts_fresh_key(self):
        await self.ns['replace_codex_provider_settings'](None);await self.settle();self.assertEqual(self.chat['codex_provider'],'custom')
        with self.assertRaises(Exception) as error:self.actual()
        self.assertIn('Configure the custom',str(error.exception))
        await self.save('restored');await self.settle();self.assertEqual(self.actual()['api_key'],'restored')
    async def test_disconnected_credentials_do_not_block_switch_to_normal(self):
        # Forget revokes the credential epoch before an old idle chat hands off.
        self.store.reset()
        result = await self.ns['update_session'](self.sid, self.ns['UpdateSessionRequest'](codex_provider='default'))
        self.assertEqual(result['session']['codex_provider'], 'default')
        self.assertEqual(self.chat['codex_thread_id'], 'thread-parent')
        self.store.require_thread('thread-parent', None)

    async def test_switch_normal_and_back_preserves_thread_binding(self):
        result=await self.ns['update_session'](self.sid,self.ns['UpdateSessionRequest'](codex_provider='default'))
        self.assertEqual(result['session']['codex_provider'],'default');self.store.require_thread('thread-parent',None)
        self.assertEqual(self.ns['codex_thread_params'](self.chat,'/fixture')['modelProvider'],'openai')
        await self.ns['update_session'](self.sid,self.ns['UpdateSessionRequest'](codex_provider='custom'))
        self.assertEqual(self.chat['codex_thread_id'],'thread-parent');self.store.require_thread('thread-parent',self.actual())
    async def test_live_control_projection_reuses_validated_revision_cache(self):
        self.store.for_session(self.chat)
        read=self.store._read
        self.store._read=Mock(side_effect=AssertionError('no credential I/O on live projection'))
        try:
            self.assertEqual(self.store.control(self.chat)['active_base_url'],self.selection['base_url'])
        finally:
            self.store._read=read

    async def test_normal_chat_and_old_revision_record_are_unchanged(self):
        normal=await self.ns['STORE'].create(self.ns['CreateSessionRequest'](model='normal-model'))
        await self.save();await self.settle();self.assertEqual(normal['model'],'normal-model');self.assertIsNone(self.store.for_session(normal))
        self.assertEqual(self.store.selection(revision=self.original_revision,include_key=True)['api_key'],'fake-initial')
    async def test_failed_save_restores_binding_and_keeps_retryable_pending(self):
        self.ns['BUSY_SESSIONS'].add(self.sid);await self.save();await self.settle();self.ns['BUSY_SESSIONS'].clear()
        self.ns['STORE'].save.side_effect=OSError('disk failure')
        with self.assertRaises(OSError):await self.ns['apply_codex_provider_when_idle'](self.sid)
        self.assertEqual(self.actual()['api_key'],'fake-initial');self.store.require_thread('thread-parent',self.actual())
        self.assertIn('_codex_provider_pending',self.chat)

    async def test_unrelated_patch_does_not_change_running_owner_model(self):
        self.ns['BUSY_SESSIONS'].add(self.sid)
        await self.ns['update_session'](self.sid,self.ns['UpdateSessionRequest'](codex_provider='default'))
        await self.ns['update_session'](self.sid,self.ns['UpdateSessionRequest'](title='Renamed'))
        self.assertEqual(self.chat['model'],'custom-model')
        self.assertEqual(self.chat['codex_provider'],'custom')
        self.assertEqual(self.chat['_codex_provider_pending']['codex_provider'],'default')

class NativeWriterReleaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp=tempfile.TemporaryDirectory(prefix='writer-release-');self.addCleanup(tmp.cleanup)
        self.ns=make_namespace(Path(tmp.name));self.session={'id':'chat','codex_thread_id':'parent'};self.ns['STORE'].sessions['chat']=self.session
        node=next(n for n in TREE.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='release_codex_provider_writers')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'agent_server.py','exec',flags=__import__('__future__').annotations.compiler_flag),self.ns)
        self.ns['join_task_despite_caller_cancellation']=lambda task:task
        self.ns['CodexAppServerRequestError']=CodexAppServerRequestError
        self.ns['CODEX_SESSIONS_ROOT']=Path(tmp.name)/'sessions'
        self.ns['CODEX_SESSIONS_ROOT'].mkdir()
        self.manager=SimpleNamespace(request=AsyncMock(return_value={}),list_descendant_threads=AsyncMock(return_value=[]))
    async def test_parent_archive_restores_all_previously_unarchived_descendants_once(self):
        self.manager.list_descendant_threads.return_value=[{'id':'owned-child'},{'id':'unloaded-grandchild'}]
        markers=[]
        async def request(method,params):
            markers.append(list(self.session.get('_codex_provider_unarchive_pending') or []))
            return {}
        self.manager.request.side_effect=request
        await self.ns['release_codex_provider_writers'](self.manager,'chat',['parent','owned-child'])
        self.assertEqual([(x.args[0],x.args[1]['threadId']) for x in self.manager.request.await_args_list],
            [('thread/archive','parent'),('thread/unarchive','parent'),('thread/unarchive','owned-child'),('thread/unarchive','unloaded-grandchild')])
        self.assertEqual(markers[0],['parent','owned-child','unloaded-grandchild'])
        self.assertEqual(markers[-1],['unloaded-grandchild'])
        self.assertNotIn('_codex_provider_unarchive_pending',self.session)
    async def test_lost_archive_response_still_unarchives_and_reports_original_failure(self):
        self.manager.request.side_effect=[RuntimeError('lost response'),{}]
        with self.assertRaises(RuntimeError):await self.ns['release_codex_provider_writers'](self.manager,'chat',['parent'])
        self.assertEqual(self.manager.request.await_args.args[0],'thread/unarchive')
        self.assertNotIn('_codex_provider_unarchive_pending',self.session)
    async def test_restart_recovers_unarchive_before_releasing_writer(self):
        self.session['_codex_provider_unarchive_pending']=['parent']
        await self.ns['release_codex_provider_writers'](self.manager,'chat',[])
        self.assertEqual([x.args[0] for x in self.manager.request.await_args_list],['thread/unarchive','thread/archive','thread/unarchive'])
        self.assertNotIn('_codex_provider_unarchive_pending',self.session)

    def child_recovery(self, *, status='interrupted', descendant=True, archived=False, missing=False):
        self.session['_codex_provider_unarchive_pending']=['owned-child']
        root=self.ns['CODEX_SESSIONS_ROOT']
        if archived:
            root=root/'archived_sessions';root.mkdir()
        path=root/'rollout-owned-child.jsonl';path.write_text('synthetic rollout\n')
        if missing:path.unlink()
        self.manager.list_descendant_threads=AsyncMock(return_value=[{'id':'owned-child'}] if descendant else [])
        self.manager.read_thread=AsyncMock(return_value={'id':'owned-child','ephemeral':False,'path':str(path)})
        self.manager.list_turns=AsyncMock(return_value=[{'id':'child-turn','status':status}])
        async def request(method,params):
            if params['threadId']=='owned-child':
                raise CodexAppServerRequestError('thread/unarchive',{'code':-32000,'message':'no archived rollout found'})
            return {}
        self.manager.request.side_effect=request

    async def test_rejected_child_archive_marker_recovers_from_native_terminal_rollout(self):
        self.child_recovery()
        await self.ns['release_codex_provider_writers'](self.manager,'chat',['parent','owned-child'])
        self.assertNotIn('_codex_provider_unarchive_pending',self.session)
        self.assertEqual([(x.args[0],x.args[1]['threadId']) for x in self.manager.request.await_args_list],
            [('thread/unarchive','owned-child'),('thread/archive','parent'),('thread/unarchive','parent'),('thread/unarchive','owned-child')])

    async def test_failed_child_restore_keeps_only_that_marker_and_restores_other_descendants(self):
        self.manager.list_descendant_threads.return_value=[{'id':'owned-child'},{'id':'other-child'}]
        async def request(method,params):
            if method=='thread/unarchive' and params['threadId']=='owned-child':
                raise RuntimeError('synthetic lost restore')
            return {}
        self.manager.request.side_effect=request
        with self.assertRaisesRegex(RuntimeError,'synthetic lost restore'):
            await self.ns['release_codex_provider_writers'](self.manager,'chat',['parent'])
        self.assertEqual(self.session['_codex_provider_unarchive_pending'],['owned-child'])
        self.assertEqual(self.manager.request.await_args.args[1]['threadId'],'other-child')

    async def test_unverified_child_marker_keeps_parent_untouched(self):
        for options in ({'status':'inProgress'},{'descendant':False},{'archived':True},{'missing':True}):
            with self.subTest(options=options):
                self.child_recovery(**options)
                self.manager.request.reset_mock()
                with self.assertRaises(CodexAppServerRequestError):
                    await self.ns['release_codex_provider_writers'](self.manager,'chat',['parent'])
                self.assertEqual(self.session['_codex_provider_unarchive_pending'],['owned-child'])
                self.assertEqual([x.args[1]['threadId'] for x in self.manager.request.await_args_list],['owned-child'])

    async def test_native_parent_unarchive_error_cannot_use_unrelated_child_proof(self):
        self.child_recovery()
        self.session['_codex_provider_unarchive_pending']=['parent']
        self.manager.request.side_effect=CodexAppServerRequestError('thread/unarchive',{'code':-32000,'message':'no archived rollout found'})
        with self.assertRaises(CodexAppServerRequestError):
            await self.ns['release_codex_provider_writers'](self.manager,'chat',['parent'])
        self.manager.read_thread.assert_awaited_once_with('parent')
        self.assertEqual(self.session['_codex_provider_unarchive_pending'],['parent'])

    async def test_already_restored_parent_clears_stale_recovery_marker(self):
        self.session['_codex_provider_unarchive_pending']=['parent']
        path=self.ns['CODEX_SESSIONS_ROOT']/'rollout-parent.jsonl'
        path.write_text('synthetic parent rollout\n')
        self.manager.read_thread=AsyncMock(return_value={'id':'parent','path':str(path)})
        self.manager.list_turns=AsyncMock(return_value=[{'id':'parent-turn','status':'interrupted'}])
        self.manager.request.side_effect=[CodexAppServerRequestError('thread/unarchive',
            {'code':-32000,'message':'no archived rollout found'}), {}, {}]
        await self.ns['release_codex_provider_writers'](self.manager,'chat',[])
        self.assertNotIn('_codex_provider_unarchive_pending',self.session)
        self.manager.read_thread.assert_awaited_once_with('parent')
