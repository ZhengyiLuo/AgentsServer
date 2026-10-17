"""Native imported history plus explicit API binding, without copying history."""
import asyncio
import json
import os
from pathlib import Path
import unittest

from gemini_acp_client import GeminiACPClient
from gemini_history import native_snapshot
from gemini_runtime import build_environment
from tests import test_gemini_lifecycle as lifecycle


@unittest.skipUnless(os.environ.get('AGENTSDOCK_TEST_GEMINI_BIN'), 'optional real Gemini CLI')
class NativeImportAPI(unittest.TestCase):
    for _name in ('setUp', 'start_server', 'ready', 'cleanup', 'wait', 'new', 'events', 'send', 'finished'):
        locals()[_name] = getattr(lifecycle.Lifecycle, _name)

    def setUp(self):
        lifecycle.Lifecycle.setUp(self)
        self.api_keys = []
        original = self.fixture_type.do_POST
        def capture(request):
            self.api_keys.append(request.headers.get('x-goog-api-key'))
            original(request)
        self.fixture_type.do_POST = capture

    def seed_native(self):
        folder = self.root / '.gemini'; folder.mkdir(exist_ok=True)
        settings = folder / 'settings.json'
        settings.write_text('{"security":{"auth":{"selectedType":"gemini-api-key"}}}')
        async def seed():
            env = build_environment(self.env, cwd=str(self.project), api_home=self.root/'unused',
                api_key='synthetic-fixture-key', base_url=f'http://127.0.0.1:{self.fixture.server_port}', native_history=True)
            async def update(_): pass
            client = GeminiACPClient(on_update=update)
            try:
                await client.start([self.env['AGENTSDOCK_GEMINI_BIN'], '--acp', '--model', 'gemini-2.5-flash'], cwd=str(self.project), env=env)
                await client.open_session(cwd=str(self.project))
                await client.prompt([{'type':'text', 'text':'Remember ORCHID-27. Reply OK; no tools.'}])
                return client.session_id
            finally:
                await client.close()
        native_id = asyncio.run(seed())
        # A different current terminal login must not select a different account
        # or rewrite the user's saved login when the API is explicitly bound.
        settings.write_text('{"security":{"auth":{"selectedType":"oauth-personal"}}}')
        return native_id, settings, settings.read_bytes()

    def test_import_bind_resume_preserves_original_native_identity_and_settings(self):
        native_id, settings, before = self.seed_native()
        response = self.client.post('/api/sessions/bulk-import', json={'items':[{
            'backend':'gemini', 'provider_session_id':native_id, 'cwd':str(self.project), 'provider_connection':'custom'}]})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()['results'][0]
        self.assertTrue(result['ok'], result)
        sid = result['session_id']
        count = sum(e['type']=='turn_finished' for e in self.events(sid))
        self.send(sid, prompt='What did I ask you to remember? No tools.')
        self.assertFalse(self.finished(sid, count+1)[-1]['is_error'], [e for e in self.events(sid) if e['type']=='error'])
        record = json.loads((self.state/'sessions.json').read_text())[sid]
        self.assertEqual(record['gemini_session_id'], native_id)
        self.assertTrue(record['gemini_native_history'])
        after = native_snapshot(self.env, str(self.project), native_id)
        self.assertTrue(any('What did I ask' in str(m.get('content')) for m in after['messages']))
        self.assertEqual(settings.read_bytes(), before)
        self.assertTrue(self.api_keys)
        self.assertEqual(set(self.api_keys), {'synthetic-fixture-key'})
        self.assertFalse(list((self.state/'admin/provider-connections').rglob('session-*.jsonl')))
        # A follow-up appends to the same native transcript, with original context.
        self.send(sid, prompt='Another ordinary follow-up, no tools.')
        self.assertFalse(self.finished(sid, count+2)[-1]['is_error'])
        self.assertEqual(json.loads((self.state/'sessions.json').read_text())[sid]['gemini_session_id'], native_id)
        self.assertTrue(any('ORCHID-27' in str(body) for body in self.fixture_type.bodies[1:]))
        self.assertEqual(settings.read_bytes(), before)
        snapshot = native_snapshot(self.env, str(self.project), native_id)
        self.assertEqual(self.client.delete(f'/api/sessions/{sid}').status_code, 200)
        self.assertEqual(native_snapshot(self.env, str(self.project), native_id), snapshot)

    def test_existing_history_only_chat_can_bind_but_not_rebind_or_ignore_forget(self):
        native_id, settings, before = self.seed_native()
        result = self.client.post('/api/sessions/bulk-import', json={'items':[{
            'backend':'gemini', 'provider_session_id':native_id, 'cwd':str(self.project)}]}).json()['results'][0]
        self.assertTrue(result['ok'], result)
        sid = result['session_id']
        # Existing history-only imports use the same explicit connection action.
        response = self.client.patch(f'/api/sessions/{sid}', json={'gemini_connect_api':True})
        self.assertEqual(response.status_code, 200, response.text)
        binding = json.loads((self.state/'sessions.json').read_text())[sid]['provider_connection_revision']
        self.assertEqual(response.json()['session']['provider_connection'], 'custom')
        again = self.client.patch(f'/api/sessions/{sid}', json={'gemini_connect_api':True})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(json.loads((self.state/'sessions.json').read_text())[sid]['provider_connection_revision'], binding)
        count = sum(e['type']=='turn_finished' for e in self.events(sid))
        self.send(sid); self.assertFalse(self.finished(sid,count+1)[-1]['is_error'])
        revision = self.client.get('/api/admin/provider-connections/gemini').json()['revision']
        response = self.client.request('DELETE', '/api/admin/provider-connections/gemini', json={'expected_revision':revision})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.patch(f'/api/sessions/{sid}',json={'gemini_connect_api':True}).status_code,409)
        self.assertEqual(settings.read_bytes(), before)

    def test_bind_rejects_missing_native_history_without_changing_connection(self):
        native_id, _, _ = self.seed_native()
        result = self.client.post('/api/sessions/bulk-import',json={'items':[{'backend':'gemini','provider_session_id':native_id,'cwd':str(self.project)}]}).json()['results'][0]
        sid = result['session_id']
        response = self.client.patch(f'/api/sessions/{sid}',json={'gemini_connect_api':True,'title':'Must not rename'})
        self.assertEqual(response.status_code,400)
        for path in (self.root/'.gemini/tmp').rglob(f'session-*{native_id[:8]}.jsonl'):
            path.rename(path.with_suffix('.missing'))
        response = self.client.patch(f'/api/sessions/{sid}',json={'gemini_connect_api':True})
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(self.client.get(f'/api/sessions/{sid}?limit=1').json()['session']['provider_connection'],'default')

    def test_busy_import_cannot_rebind_and_stop_preserves_native_history(self):
        native_id, settings, before = self.seed_native()
        result = self.client.post('/api/sessions/bulk-import', json={'items':[{
            'backend':'gemini', 'provider_session_id':native_id, 'cwd':str(self.project),
            'provider_connection':'custom'}]}).json()['results'][0]
        self.assertTrue(result['ok'], result)
        sid = result['session_id']; route = f'/api/sessions/{sid}'
        count = sum(e['type']=='turn_finished' for e in self.events(sid))
        self.fixture_type.mode = 'tool'
        self.send(sid)
        self.wait(lambda: self.client.get(route+'/gemini/runtime').json()['pending_interactions'])
        self.assertEqual(self.client.patch(route,json={'gemini_connect_api':True}).status_code,409)
        self.assertEqual(self.client.post(route+'/stop').status_code,200)
        self.assertTrue(self.finished(sid,count+1)[-1]['stopped'])
        self.assertFalse((self.project/'must-not-exist.txt').exists())
        self.assertEqual(settings.read_bytes(),before)
        self.fixture_type.mode = 'normal'
        self.send(sid)
        self.assertFalse(self.finished(sid,count+2)[-1]['is_error'])
        self.assertEqual(self.client.get(route).json()['session']['session_id'],native_id)
