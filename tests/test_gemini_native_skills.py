"""Real pinned CLI, synthetic credentials, local API; no Google-account claims."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest

from gemini_acp_client import GeminiACPClient
from gemini_runtime import build_environment, list_native_skills
from provider_commands import gemini_provider_skill_inventory


class ToolAPI(BaseHTTPRequestHandler):
    target = ''
    responses = []
    def log_message(self, *_args): pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        contents = body.get('contents', [])
        responses = [p['functionResponse'] for c in contents for p in c.get('parts', []) if 'functionResponse' in p]
        self.responses.extend(responses)
        prompt = ' '.join(p.get('text', '') for c in contents for p in c.get('parts', []))
        if responses:
            parts = [{'text': 'Fixture tool completed.'}]
        elif 'FIXTURE_SKILL' in prompt or 'BUILTIN_SKILL' in prompt:
            parts = [{'functionCall': {'name': 'activate_skill', 'args': {'name': 'skill-creator' if 'BUILTIN_SKILL' in prompt else 'fixture-skill'}}}]
        else:
            parts = [{'functionCall': {'name': 'write_file', 'args': {'file_path': self.target, 'content': 'fixture only'}}}]
        result = {'candidates': [{'index': 0, 'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP'}],
                  'usageMetadata': {'promptTokenCount': 5, 'candidatesTokenCount': 5, 'totalTokenCount': 10}}
        stream = 'streamGenerateContent' in self.path
        data = ('data: ' + json.dumps(result) + '\n\n').encode() if stream else json.dumps(result).encode()
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream' if stream else 'application/json')
        self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)


@unittest.skipUnless(os.environ.get('AGENTSDOCK_TEST_GEMINI_BIN'), 'optional real native skill/tool contract')
class NativeSkillContract(unittest.IsolatedAsyncioTestCase):
    async def test_native_inventory_activation_write_approval_and_denial(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), ToolAPI)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                home = root / 'native'
                skill = home / '.gemini/skills/fixture-skill/SKILL.md'
                skill.parent.mkdir(parents=True)
                skill.write_text('---\nname: fixture-skill\ndescription: Harmless acceptance skill.\n---\nReply SKILL_OK; do not modify files.')
                project = root / 'project'; project.mkdir()
                ToolAPI.target = str(project / 'approved.txt')
                ToolAPI.responses = []
                env = build_environment({'PATH': os.environ['PATH'], 'HOME': str(home)}, cwd=str(project),
                    api_home=root / 'api', api_key='synthetic-key', base_url=f'http://127.0.0.1:{server.server_port}')
                executable = os.environ['AGENTSDOCK_TEST_GEMINI_BIN']
                raw = await asyncio.to_thread(list_native_skills, executable, env, str(project))
                inventory = gemini_provider_skill_inventory(raw, cwd=str(project), selector_secret='test', binding_context='chat')
                self.assertEqual([x['name'] for x in inventory.commands if x['scope'] == 'native'], ['fixture-skill'])
                self.assertEqual({x['name'] for x in inventory.commands if x['scope'] == 'built-in'}, {'skill-creator', 'antigravity-support'})
                for prompt, decision in [('FIXTURE_SKILL', 'allow_once'), ('BUILTIN_SKILL', 'allow_once'), ('FIXTURE_WRITE', 'reject_once'), ('FIXTURE_WRITE', 'allow_once')]:
                    events, permissions = [], []
                    async def update(value): events.append(value)
                    async def approve(value):
                        permissions.append(value)
                        option = next(x for x in value['options'] if x['kind'] == decision)
                        return {'outcome': {'outcome': 'selected', 'optionId': option['optionId']}}
                    client = GeminiACPClient(on_update=update, on_permission=approve)
                    try:
                        await client.start([executable, '--acp', '--model', 'gemini-2.5-flash'], cwd=str(project), env=env)
                        await client.open_session(cwd=str(project))
                        result = await client.prompt([{'type': 'text', 'text': prompt}], timeout=30)
                        self.assertEqual(result['stopReason'], 'end_turn')
                        if prompt != 'BUILTIN_SKILL':
                            self.assertTrue(permissions, 'Native tool must request explicit approval')
                        self.assertTrue(any(e['type'] == 'tool' for e in events))
                        if prompt == 'BUILTIN_SKILL':
                            self.assertTrue(any(e.get('status') == 'completed' for e in events if e['type'] == 'tool'))
                            self.assertIn('skill-creator', json.dumps(ToolAPI.responses[-1]))
                        if prompt == 'FIXTURE_WRITE':
                            self.assertEqual(Path(ToolAPI.target).exists(), decision == 'allow_once')
                    finally:
                        await client.close()
                        self.assertIsNotNone(client.process.returncode)
                self.assertTrue(any(r['name'] == 'activate_skill' for r in ToolAPI.responses))
                self.assertTrue(skill.exists(), 'Original skill must not be moved or reinstalled')
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)

    async def test_native_scope_precedence_trust_and_disabled_skills(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); home = root / 'home'; project = root / 'project'
            paths = [home / '.gemini/skills/shared/SKILL.md', home / '.agents/skills/shared/SKILL.md',
                     project / '.gemini/skills/shared/SKILL.md', project / '.agents/skills/shared/SKILL.md']
            for i, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f'---\nname: shared\ndescription: Scope {i}\n---\nNo actions.')
            env = build_environment({'HOME': str(home), 'PATH': os.environ['PATH']}, cwd=str(project),
                api_home=root / 'api', api_key='synthetic-key', base_url='http://127.0.0.1:1')
            async def inventory(overrides=None):
                raw = await asyncio.to_thread(list_native_skills, os.environ['AGENTSDOCK_TEST_GEMINI_BIN'],
                    {**env, **(overrides or {})}, str(project))
                return gemini_provider_skill_inventory(raw, cwd=str(project), selector_secret='test', binding_context='scope')
            # Fixture-only trust flags exercise the native resolver; the server
            # never supplies a trust grant to ordinary user workspaces.
            user = await inventory({'GEMINI_CLI_TRUST_WORKSPACE': 'false'})
            self.assertEqual(user.records[0].native['path'], str(paths[1]))
            trusted = await inventory({'GEMINI_CLI_TRUST_WORKSPACE': 'true'})
            self.assertEqual(trusted.records[0].native['path'], str(paths[3]))
            (project / '.gemini/settings.json').write_text('{"skills":{"disabled":["shared"]}}')
            disabled = await inventory({'GEMINI_CLI_TRUST_WORKSPACE': 'true'})
            self.assertFalse(any(x['name'] == 'shared' for x in disabled.commands))
            self.assertTrue(any(x['scope'] == 'built-in' for x in disabled.commands))

    async def test_native_builtin_override_and_disable_without_scanning_foreign_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); home = root / 'home'; project = root / 'project'
            project.mkdir()
            override = home / '.agents/skills/skill-creator/SKILL.md'
            foreign = home / '.codex/skills/codex-only/SKILL.md'
            for path, name in [(override, 'skill-creator'), (foreign, 'codex-only')]:
                path.parent.mkdir(parents=True)
                path.write_text(f'---\nname: {name}\ndescription: Native inventory boundary fixture\n---\nNo actions.')
            async def inventory():
                env = build_environment({'HOME': str(home), 'PATH': os.environ['PATH']}, cwd=str(project),
                    api_home=root / 'api', api_key='synthetic-key', base_url='http://127.0.0.1:1')
                raw = await asyncio.to_thread(list_native_skills, os.environ['AGENTSDOCK_TEST_GEMINI_BIN'], env, str(project))
                return gemini_provider_skill_inventory(raw, cwd=str(project), selector_secret='test', binding_context='override')
            selected = await inventory()
            creator = [r for r in selected.records if r.public['name'] == 'skill-creator']
            self.assertEqual(len(creator), 1)
            self.assertEqual(creator[0].public['scope'], 'native')
            self.assertEqual(creator[0].native['path'], str(override))
            self.assertFalse(any(c['name'] == 'codex-only' for c in selected.commands))
            (home / '.gemini').mkdir(exist_ok=True)
            (home / '.gemini/settings.json').write_text('{"skills":{"disabled":["skill-creator","antigravity-support"]}}')
            self.assertEqual((await inventory()).commands, [])
