"""Actual CLI + server + loopback API. Does not inject a native title."""
import json
import os
import unittest

from gemini_history import read_snapshot
from tests import test_gemini_lifecycle as lifecycle


@unittest.skipUnless(os.environ.get('AGENTSDOCK_TEST_GEMINI_BIN'), 'optional real Gemini CLI')
class NativeTitles(unittest.TestCase):
    auto_titles = True
    for _name in ('setUp', 'start_server', 'ready', 'cleanup', 'wait', 'new', 'events', 'send', 'finished'):
        locals()[_name] = getattr(lifecycle.Lifecycle, _name)

    def test_successful_first_turn_names_without_native_summary_and_resume_is_unchanged(self):
        sid = self.new()
        prompt = 'Unhelpful log header\n' + 'timestamp noise\n' * 600 + '请解释连接超时的原因'
        self.send(sid, prompt=prompt)
        self.assertFalse(self.finished(sid)[-1]['is_error'])
        def saved():
            value = json.loads((self.state / 'sessions.json').read_text())[sid]
            return value if value.get('_title_source') == 'generated' else None
        row = self.wait(saved)
        self.assertEqual(row['title'], 'Fixture complete.')
        native_id = row['gemini_session_id']
        transcript = next((self.state / 'admin/provider-connections').rglob(f'session-*{native_id[:8]}.jsonl'))
        self.assertFalse(read_snapshot(transcript).get('summary'))
        def title_bodies():
            return [b for b in self.fixture_type.bodies if any(p.get('text', '').startswith('Generate only a concise conversation title')
                for c in b.get('contents', []) for p in c.get('parts', []))]
        self.assertEqual(len(title_bodies()), 1)
        self.assertNotIn('tools', title_bodies()[0])
        self.assertIn('请解释连接超时的原因', title_bodies()[0]['contents'][0]['parts'][0]['text'])
        self.assertEqual(sum(e['type'] == 'turn_started' for e in self.events(sid)), 1)
        self.send(sid, prompt='Now a different ordinary follow-up.')
        self.assertFalse(self.finished(sid, 2)[-1]['is_error'])
        self.assertEqual(saved()['gemini_session_id'], native_id)
        self.assertEqual(len(title_bodies()), 1)
        response = self.client.patch(f'/api/sessions/{sid}', json={'title': 'My manual title'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get(f'/api/sessions/{sid}?limit=1').json()['session']['title'], 'My manual title')
