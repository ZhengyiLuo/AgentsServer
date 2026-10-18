"""Atomic binding failures; run with an isolated server HOME/state."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import agent_server as api


class ImportBinding(unittest.IsolatedAsyncioTestCase):
    async def test_failed_save_rolls_back_connection_model_and_native_marker(self):
        session = {'id':'fixture','backend':'gemini','provider_connection':'default',
            'gemini_session_id':'12345678-1234-1234-1234-123456789abc',
            'session_id':'12345678-1234-1234-1234-123456789abc','cwd':'/fixture','model':'native-model'}
        before = deepcopy(session)
        store = SimpleNamespace(sessions={'fixture':session},_lock=asyncio.Lock(),save=AsyncMock(side_effect=OSError('disk full')))
        connections = SimpleNamespace(bind=lambda _: {'credential_id':'test-binding','model':'api-model'})
        with ExitStack() as stack:
            for name,value in {'STORE':store,'PROVIDER_CONNECTION_STORE':connections,'BUSY_SESSIONS':set(),
                    'ACTIVE':{},'QUEUED_TURNS':{},'DELETING_SESSIONS':set(),'SESSION_TURN_TASKS':{}}.items():
                stack.enter_context(patch.object(api,name,value))
            stack.enter_context(patch.object(api,'ensure_session_not_initializing'))
            stack.enter_context(patch.object(api,'session_lifecycle_lock',return_value=asyncio.Lock()))
            stack.enter_context(patch('gemini_import.initial_history',return_value=(None,[])))
            stack.enter_context(patch('gemini_history.native_snapshot',return_value={'messages':[]}))
            with self.assertRaises(OSError):
                await api.connect_gemini_import_api('fixture')
            self.assertEqual(session,before)
            store.save.assert_awaited_once_with(durable=True)

    async def test_paused_queue_still_blocks_connection_without_reading_history(self):
        session = {'id':'fixture','backend':'gemini','provider_connection':'default','gemini_session_id':'native'}
        with patch.object(api,'STORE',SimpleNamespace(sessions={'fixture':session})), \
                patch.object(api,'QUEUED_TURNS',{'fixture':[{'paused':True}]}), \
                patch.object(api,'session_lifecycle_lock',return_value=asyncio.Lock()), \
                patch.object(api,'ensure_session_not_initializing'), \
                patch('gemini_import.initial_history') as history:
            with self.assertRaises(api.HTTPException) as error:
                await api.connect_gemini_import_api('fixture')
            self.assertEqual(error.exception.status_code,409)
            history.assert_not_called()
            self.assertEqual(session['provider_connection'],'default')
