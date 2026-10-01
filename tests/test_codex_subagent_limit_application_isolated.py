"""Event-driven native limit application using real helpers and owned state."""
from __future__ import annotations
import ast
import asyncio
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock
from codex_app_server import CodexAppServerRequestError
from tests.test_codex_provider_sessions_isolated import make_namespace

NAMES = {"apply_codex_subagent_limit_when_idle", "schedule_codex_subagent_limit_application", "apply_pending_codex_subagent_limit"}
TREE = ast.parse(Path(__file__).resolve().parents[1].joinpath("agent_server.py").read_text())
CODE = compile(ast.fix_missing_locations(ast.Module(body=[node for node in TREE.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in NAMES], type_ignores=[])), "agent_server.py", "exec", flags=__import__('__future__').annotations.compiler_flag)

class Manager:
    def __init__(self):
        self.generation = 1
        self.ready = True
        self.loaded = {"native-parent"}
        self.active = {}
        self.terminals = {}
        self.unsubscribed = []
        self.resumed = []
        self.resume_gate = None
        self.resume_started = asyncio.Event()
    def is_thread_loaded(self, tid): return tid in self.loaded
    def active_turn(self, tid): return self.active.get(tid)
    async def list_background_terminals(self, tid): return self.terminals.get(tid, [])
    async def unsubscribe_thread(self, tid):
        self.unsubscribed.append(tid)
        self.loaded.remove(tid)
    async def resume_thread(self, tid, params):
        self.resumed.append((tid, params))
        self.resume_started.set()
        if self.resume_gate: await self.resume_gate.wait()
        self.loaded.add(tid)
        return tid

class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory(prefix="native-limit-application-")
        self.addCleanup(temp.cleanup)
        self.ns = make_namespace(Path(temp.name))
        self.manager = Manager()
        self.ns.update(CODEX_SUBAGENT_LIMIT_TASKS={}, CODEX_SUBAGENT_LIMIT_REQUESTED=set(),
            CODEX_MANAGER_CLOSING=False, SERVER_MAINTENANCE_SESSIONS=set(), DELETING_SESSIONS=set(),
            CODEX_NATIVE_ACTION_TASKS={}, CODEX_SUBAGENT_STATE={}, CODEX_SUBAGENT_INDEX_LOCK=threading.RLock(),
            CodexAppServerRequestError=CodexAppServerRequestError,
            codex_session_has_live_subagents=Mock(return_value=False),
            existing_codex_app_server_manager=lambda sess: self.manager,
            retain_codex_manager_caller=Mock(), existing_cwd=lambda path: path,
            pin_codex_app_server_thread=AsyncMock(), unpin_codex_app_server_thread=AsyncMock())
        exec(CODE, self.ns)
        self.sess = await self.ns['STORE'].create(self.ns['CreateSessionRequest'](subagent_limit=1))
        self.sid = self.sess['id']
        self.sess.update(session_id='native-parent', codex_thread_id='native-parent')
        await self.record(1)
        self.ns['broadcast_provider_runtime_changed'].reset_mock()
    async def asyncTearDown(self):
        tasks = tuple(self.ns['CODEX_SUBAGENT_LIMIT_TASKS'].values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    async def record(self, cap):
        await self.ns['record_codex_subagent_limit_application'](self.manager, self.sid, self.sess, thread_id='native-parent', applied_limit=cap)
    async def save(self, cap):
        return await self.ns['update_session'](self.sid, self.ns['UpdateSessionRequest'](subagent_limit=cap))
    async def settle(self):
        for _ in range(20):
            tasks = tuple(self.ns['CODEX_SUBAGENT_LIMIT_TASKS'].values())
            if not tasks: return
            await asyncio.wait_for(asyncio.gather(*tasks), 2)
        self.fail('application did not settle')
    def control(self): return self.ns['session_subagent_limit_control'](self.sess)
    async def test_idle_save_applies_and_broadcasts_ack_without_manual_reload(self):
        result = await self.save(2)
        self.assertEqual(result['session']['subagent_limit_control']['application_state'], 'pending')
        await self.settle()
        self.assertEqual(self.manager.unsubscribed, ['native-parent'])
        self.assertEqual(self.control()['effective_limit'], 2)
        self.assertEqual(self.control()['application_state'], 'applied')
        packet = self.ns['broadcast_provider_runtime_changed'].await_args.args[1]
        self.assertEqual(packet['runtime'], 'subagent_limit')
        self.assertEqual(packet['subagent_limit_control']['effective_limit'], 2)
        self.ns['unpin_codex_app_server_thread'].assert_awaited()
    async def test_running_parent_and_children_are_not_reloaded(self):
        for blocker in ('parent', 'child', 'terminal', 'descendant_terminal', 'native_child_with_stale_status'):
            with self.subTest(blocker=blocker):
                self.manager.active.clear(); self.manager.terminals.clear()
                self.ns['codex_session_has_live_subagents'].return_value = blocker == 'child'
                if blocker == 'parent': self.manager.active['native-parent'] = object()
                if blocker == 'terminal': self.manager.terminals['native-parent'] = [{}]
                if blocker == 'descendant_terminal':
                    self.manager.loaded.add('child')
                    self.ns['CODEX_SUBAGENT_STATE']['child'] = {'session_id': self.sid, 'subagent_status': 'completed'}
                    self.manager.terminals['child'] = [{}]
                if blocker == 'native_child_with_stale_status':
                    self.ns['CODEX_SUBAGENT_STATE']['child'] = {'session_id': self.sid, 'subagent_status': 'completed'}
                    self.manager.active['child'] = object()
                await self.save(2); await self.settle()
                self.assertEqual(self.manager.unsubscribed, [])
                self.assertEqual(self.control()['effective_limit'], 1)
                self.assertEqual(self.control()['application_state'], 'pending')
    async def test_busy_task_completion_applies_without_polling(self):
        gate = asyncio.Event()
        task = asyncio.create_task(gate.wait())
        self.ns['SESSION_TURN_TASKS'][self.sid] = {task}
        self.ns['BUSY_SESSIONS'].add(self.sid)
        await self.save(3); await self.settle()
        self.assertEqual(self.manager.unsubscribed, [])
        self.ns['BUSY_SESSIONS'].clear()
        gate.set(); await task; await asyncio.sleep(0)
        await self.settle()
        self.assertEqual(self.control()['effective_limit'], 3)
    async def test_initial_preparation_task_also_defers_without_busy_owner(self):
        gate = asyncio.Event(); task = asyncio.create_task(gate.wait())
        self.ns['SESSION_TURN_TASKS'][self.sid] = {task}
        await self.save(2); await self.settle()
        self.assertEqual(self.manager.unsubscribed, [])
        gate.set(); await task; await asyncio.sleep(0); await self.settle()
        self.assertEqual(self.control()['effective_limit'], 2)
    async def test_save_back_to_old_cap_while_resume_is_unloaded_is_not_lost(self):
        self.manager.resume_gate = asyncio.Event()
        await self.save(2)
        await asyncio.wait_for(self.manager.resume_started.wait(), 2)
        self.assertFalse(self.manager.is_thread_loaded('native-parent'))
        await self.save(1)
        self.manager.resume_gate.set()
        await self.settle()
        caps = [params['config']['agents.max_concurrent_threads_per_session'] for _, params in self.manager.resumed]
        self.assertEqual(caps, [2, 1])
        self.assertEqual(self.control()['effective_limit'], 1)
        self.assertEqual(self.control()['application_state'], 'applied')
    async def test_lower_then_clear_uses_full_reload_and_omits_override(self):
        await self.record(8)
        await self.save(2); await self.settle()
        await self.save(None); await self.settle()
        self.assertEqual(len(self.manager.unsubscribed), 2)
        self.assertNotIn('agents.max_concurrent_threads_per_session', self.manager.resumed[-1][1]['config'])
        self.assertIsNone(self.control()['effective_limit'])
        self.assertEqual(self.control()['application_state'], 'applied')
    async def test_global_inheritance_and_per_chat_override_precedence(self):
        self.ns['CODEX_SETTINGS_FILE'].write_text('{"thread_config":{"agents":{"max_concurrent_threads_per_session":7}}}')
        await self.save(None); await self.settle()
        self.assertEqual(self.control()['effective_limit'], 7)
        await self.save(4); await self.settle()
        self.assertEqual(self.control()['effective_limit'], 4)
        self.ns['CODEX_SETTINGS_FILE'].write_text('{"thread_config":{"agents":{"max_concurrent_threads_per_session":9}}}')
        self.ns['schedule_codex_subagent_limit_application'](self.sid); await self.settle()
        self.assertEqual(self.control()['effective_limit'], 4)
        self.assertEqual(len(self.manager.resumed), 2)
    async def test_active_goal_gap_preserves_native_owner_until_goal_is_inactive(self):
        self.sess['codex_goal'] = {'status': 'active'}
        await self.save(2); await self.settle()
        self.assertEqual(self.manager.unsubscribed, [])
        self.assertEqual(self.control()['application_state'], 'pending')
        self.sess['codex_goal'] = {'status': 'paused'}
        self.ns['schedule_codex_subagent_limit_application'](self.sid)
        await self.settle()
        self.assertEqual(self.control()['effective_limit'], 2)
    async def test_goal_activation_during_terminal_read_uses_current_store_not_snapshot(self):
        captured = dict(self.sess)
        captured['subagent_limit'] = 2
        async def list_terminals(tid):
            self.sess['codex_goal'] = {'status': 'active'}
            return []
        self.manager.list_background_terminals = list_terminals
        applied = await self.ns['apply_codex_subagent_limit_when_idle'](self.manager, self.sid, captured, '/repo')
        self.assertFalse(applied)
        self.assertEqual(self.manager.unsubscribed, [])
    async def test_manager_generation_change_does_not_claim_old_ack(self):
        self.manager.generation += 1
        self.assertEqual(self.control()['application_state'], 'pending')
        self.assertIsNone(self.control()['effective_limit'])
        await self.save(2); await self.settle()
        self.assertEqual(self.control()['effective_limit'], 2)
    async def test_resume_failure_does_not_loop_and_releases_pin(self):
        self.manager.resume_thread = AsyncMock(side_effect=RuntimeError('synthetic failure'))
        await self.save(2); await self.settle()
        self.manager.resume_thread.assert_awaited_once()
        self.ns['unpin_codex_app_server_thread'].assert_awaited_once()
        self.assertEqual(self.control()['application_state'], 'next_start')
