"""Manually exercise gateway replacement with real Codex/Claude foreground tools.

Run with the installed server Python environment and an explicit --backend.
Each selected backend receives one model turn and creates disposable native
history. Existing provider logins are used without changing credentials/config.
Only child processes started by this script are signaled. Reports and isolated
chat state are private and retained for inspection; no live service is updated.
This is provider acceptance, not installed migration or native UI acceptance.
"""
import argparse
import asyncio
import traceback
from contextlib import suppress
import json
import os
from pathlib import Path
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parents[2]
PYTHON = Path(sys.executable)
OPTIONS = argparse.ArgumentParser(description=__doc__)
OPTIONS.add_argument('--backend', choices=('codex', 'claude', 'both'), required=True)
OPTIONS.add_argument('--http-only', action='store_true')
OPTIONS.add_argument('--output', type=Path, default=ROOT / 'dist/qa/execution-providers')
ARGS = OPTIONS.parse_args()
REPORT = ARGS.output.expanduser().resolve()


def private_json(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, indent=2)
        output.write('\n')


def process_identity(pid):
    def field(name):
        return subprocess.check_output(['ps', '-p', str(pid), '-o', name + '='], text=True).strip()
    return {'pid': pid, 'ppid': int(field('ppid')), 'started_at': field('lstart'), 'executable': field('comm')}


def ancestors(pid, worker_pid):
    result = []
    for _ in range(12):
        item = process_identity(pid)
        result.append(item)
        if pid == worker_pid:
            return result
        pid = item['ppid']
        if pid <= 1:
            break
    raise RuntimeError('Test tool is not a descendant of the owned worker')


async def main():
    os.umask(0o077)
    REPORT.mkdir(mode=0o700, parents=True, exist_ok=True)
    run = REPORT / ('real-' + time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(3))
    run.mkdir(mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix='ad-real-', dir='/tmp')).resolve()
    runtime = temporary / 'state' / 'execution'
    for name in ('state', 'config', 'codex-history-view', 'claude-history-view', 'tmp', 'workspaces'):
        (temporary / name).mkdir(mode=0o700)
    runtime.mkdir(mode=0o700)
    token = secrets.token_hex(32)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('AGENTSDOCK_', 'AGENTS_SERVER_', 'ZENITHDOCK_', 'ZENITHBOT_'))}
    env.update({
        'AGENTSDOCK_STATE_DIR': str(temporary / 'state'),
        'AGENTS_SERVER_CONFIG_DIR': str(temporary / 'config'),
        'CODEX_SESSIONS_ROOT': str(temporary / 'codex-history-view'),
        'CLAUDE_PROJECTS_ROOT': str(temporary / 'claude-history-view'),
        'TMPDIR': str(temporary / 'tmp'),
        'AGENTSDOCK_AGENT_CWD': str(temporary / 'workspaces'),
        'AGENTSDOCK_AGENT_TOKEN': token,
        'AGENTSDOCK_TEAM_HUB_MODE': 'disabled',
        'AGENTSDOCK_TEAM_HUB_TRANSPORT': 'loopback',
        'AGENTSDOCK_AUTO_TITLES': '0',
        'PYTHONDONTWRITEBYTECODE': '1',
    })
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    headers = {'X-AgentsDock-Token': token}
    processes, logs = [], []
    result = {'passed': False, 'owned_root': str(temporary), 'providers': {},
              'scope': 'Production agent_server app, execution_service worker/gateway, native provider SDK transports, actual foreground tool and WebSocket events',
              'limitations': ['Unmanaged isolated local processes; no installed launchd/systemd upgrade', 'No worker crash/reboot survival claim', 'No app UI or multi-subagent acceptance in this run', 'Normal new disposable native provider history may be created; no auth/config/login mutation requested']}
    client = httpx.AsyncClient(base_url=origin, headers=headers, timeout=40, trust_env=False)
    worker = gateway = None
    def launch(role):
        logfile = (run / f'{role}-{len(processes)}.log').open('wb')
        logs.append(logfile)
        command = [str(PYTHON), '-B', str(ROOT / 'server/execution_service.py'), role,
                   '--runtime-dir', str(runtime), '--bind', '127.0.0.1', '--port', str(port), '--callback-port', '0']
        process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=logfile, stderr=subprocess.STDOUT)
        processes.append(process)
        return process
    async def wait_for(check, timeout=120):
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            value = check()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return value
            await asyncio.sleep(0.2)
        raise TimeoutError('Bounded acceptance deadline exceeded')
    async def health():
        try:
            response = await client.get('/api/health', timeout=8)
            if response.status_code == 200:
                return response.json()
        except httpx.TransportError:
            return None
    async def ready():
        value = await health()
        return value if value and value.get('gateway', {}).get('pid') == gateway.pid else None
    def save():
        private_json(run / 'acceptance.json', result)
    try:
        worker = launch('worker')
        def worker_ready():
            if worker.poll() is not None:
                raise RuntimeError('Worker exited before readiness; inspect owned startup log')
            return (runtime / 'worker.json').is_file()
        await wait_for(worker_ready)
        gateway = launch('gateway')
        initial = await wait_for(ready)
        result['initial_components'] = {k: initial[k] for k in ('server_instance_id', 'execution_service', 'gateway')}
        print(json.dumps({'stage': 'ready', 'report': str(run / 'acceptance.json')}), flush=True)
        if ARGS.http_only:
            outcomes = []
            for number in range(60):
                try:
                    response = await client.get('/api/health', timeout=10)
                    response.raise_for_status()
                    assert response.json()['execution_service']['instance_id'] == initial['execution_service']['instance_id']
                    outcomes.append({'request': number, 'status': response.status_code})
                except Exception as error:
                    outcomes.append({'request': number, 'error_type': type(error).__name__})
            for backend in ('codex', 'claude'):
                try:
                    response = await client.post('/api/sessions', json={'backend': backend, 'cwd': str(temporary / 'workspaces'), 'title': 'Owned zero-turn HTTP acceptance', 'auto_title_enabled': False, 'import_history': False, 'provider_jobs_access': 'blocked'})
                    response.raise_for_status()
                    outcomes.append({'operation': 'create_session', 'backend': backend, 'status': response.status_code})
                except Exception as error:
                    outcomes.append({'operation': 'create_session', 'backend': backend, 'error_type': type(error).__name__})
            result['http_only'] = outcomes
            result['model_calls'] = 0
            result['passed'] = all('error_type' not in outcome for outcome in outcomes)
            if not result['passed']:
                raise RuntimeError('HTTP acceptance failed')
        catalog = {}
        if not ARGS.http_only:
            catalog_response = await client.get('/api/runtime/catalog', timeout=60)
            catalog_response.raise_for_status()
            catalog = catalog_response.json()
        backends = () if ARGS.http_only else (('codex', 'claude') if ARGS.backend == 'both' else (ARGS.backend,))
        for backend in backends:
            item = {'submitted_turns': 0, 'passed': False, 'backend': backend}
            result['providers'][backend] = item
            stream_tasks = []
            workspace = temporary / 'workspaces' / backend
            workspace.mkdir(mode=0o700)
            barrier = workspace / 'barrier.py'
            barrier.write_text('''import json, os, pathlib, time
p=pathlib.Path(__file__).parent
with (p/'invocations.jsonl').open('a') as f:
 f.write(json.dumps({'pid':os.getpid()})+'\\n');f.flush();os.fsync(f.fileno())
fd=os.open(p/'started.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
 json.dump({'pid':os.getpid(),'ppid':os.getppid(),'started_ns':time.time_ns()},f);f.flush();os.fsync(f.fileno())
deadline=time.monotonic()+90
while not (p/'release').exists():
 if time.monotonic()>deadline: raise SystemExit('owned barrier timed out')
 time.sleep(0.1)
with (p/'completed.jsonl').open('a') as f:
 f.write(json.dumps({'pid':os.getpid(),'completed_ns':time.time_ns()})+'\\n');f.flush();os.fsync(f.fileno())
print('OWNED_BARRIER_COMPLETE',flush=True)
''')
            marker = 'PERSISTENT_WORKER_' + backend.upper() + '_OK_' + secrets.token_hex(4)
            observed = []
            last_seq = 0
            stream_errors = []
            session_id = None
            events_file = None
            try:
                backend_catalog = catalog.get('backends', {}).get(backend, {})
                item['model'] = backend_catalog.get('default_model')
                body = {'backend': backend, 'cwd': str(workspace), 'title': f'Owned persistent execution QA {backend}',
                        'auto_title_enabled': False, 'import_history': False, 'provider_jobs_access': 'blocked', 'subagent_limit': 1,
                        'system_prompt': 'This is an authorized isolated acceptance test. Execute only the requested local barrier script once and return its final marker. Do not inspect other files, credentials or settings, spawn subagents, create jobs, or contact other chats.'}
                if item['model']:
                    body['model'] = item['model']
                if backend == 'codex':
                    body.update(codex_approval_policy='on-request', codex_sandbox_mode='workspace-write', codex_approvals_reviewer='user')
                else:
                    body.update(claude_permission_mode='default')
                response = await client.post('/api/sessions', json=body)
                response.raise_for_status()
                session_id = response.json()['session']['id']
                item['session_id'] = session_id
                events_file = (run / f'{backend}-observed-events.jsonl').open('w')
                async def consume(after, connected):
                    nonlocal last_seq
                    try:
                        async with connect(f'ws://127.0.0.1:{port}/api/sessions/{session_id}/events?after={after}',
                                           additional_headers=headers, proxy=None, open_timeout=20) as ws:
                            connected.set()
                            async for raw in ws:
                                event = json.loads(raw)
                                seq = event.get('seq')
                                if isinstance(seq, int):
                                    if seq <= last_seq:
                                        stream_errors.append('Non-increasing or repeated event sequence')
                                    last_seq = seq
                                    observed.append(event)
                                    events_file.write(json.dumps(event)+'\n'); events_file.flush()
                    except ConnectionClosed:
                        pass
                    except Exception as error:
                        stream_errors.append(type(error).__name__)
                        connected.set()
                connected = asyncio.Event()
                stream_tasks.append(asyncio.create_task(consume(0, connected)))
                await asyncio.wait_for(connected.wait(), 25)
                command = shlex.join([str(PYTHON), str(barrier)])
                prompt = ('Run exactly this command once as a foreground tool, allowing at least 120 seconds: ' + command
                          + '. The script deliberately waits for this test harness to release it; do not create the release file, kill it, retry it, or run any other command. '
                          + 'Wait for its OWNED_BARRIER_COMPLETE output. Then reply with only ' + marker + '.')
                item['submitted_turns'] += 1
                response = await client.post(f'/api/sessions/{session_id}/turns', json={'prompt': prompt,
                                            'client_capabilities': ['codex_interactive_v1', 'claude_sdk_interactive_v1']})
                response.raise_for_status()
                item['run_id'] = response.json()['run_id']
                save()
                async def started():
                    if (workspace / 'started.json').is_file():
                        return json.loads((workspace / 'started.json').read_text())
                    terminal = [e for e in observed if e.get('type') in ('turn_finished','turn_stopped','error')]
                    if terminal:
                        private_json(run / f'{backend}-early-terminal.json', terminal)
                        raise RuntimeError('Provider ended before reaching owned tool barrier; see private terminal evidence')
                    status = await client.get(f'/api/sessions/{session_id}/{backend}/runtime')
                    status.raise_for_status()
                    for pending in status.json().get('pending_interactions', []):
                        if (pending.get('method') != 'item/commandExecution/requestApproval'
                                or pending.get('params', {}).get('command') != command):
                            raise RuntimeError('Unexpected approval request; not authorizing unrelated work')
                        resolved = await client.post(f'/api/sessions/{session_id}/{backend}/interactions/{pending["id"]}/resolve',
                                                     json={'response': {'decision': 'accept'}})
                        resolved.raise_for_status()
                        item.setdefault('approval_ids', []).append(pending['id'])
                    return None
                tool = await wait_for(started, timeout=120)
                chain = ancestors(tool['pid'], worker.pid)
                provider = next((p for p in chain[1:] if backend in p['executable'].lower()), None)
                if provider is None:
                    raise RuntimeError('Could not identify owned native provider in tool ancestry')
                item.update(tool=tool, provider=provider, ancestry=chain)
                item['gateway_replacements'] = []
                print(json.dumps({'stage': 'barrier', 'backend': backend, 'run_id': item['run_id']}), flush=True)
                for number in (signal.SIGTERM, signal.SIGKILL):
                    prior_gateway = gateway.pid
                    gateway.send_signal(number)
                    await asyncio.wait_for(asyncio.to_thread(gateway.wait), 8)
                    for task in stream_tasks:
                        if not task.done():
                            with suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(asyncio.shield(task), 5)
                            if not task.done():
                                task.cancel()
                                with suppress(asyncio.CancelledError): await task
                    stream_tasks.clear()
                    assert worker.poll() is None
                    assert process_identity(provider['pid']) == provider
                    assert process_identity(tool['pid'])['started_at'] == chain[0]['started_at']
                    receipt = json.loads((runtime / 'worker.json').read_text())
                    async with httpx.AsyncClient(base_url=receipt['callback_origin'], headers=headers, trust_env=False) as direct:
                        live = (await direct.get('/api/health', timeout=15)).json()
                    assert live['execution_service']['instance_id'] == initial['execution_service']['instance_id']
                    assert live['server_instance_id'] == initial['server_instance_id']
                    gateway = launch('gateway')
                    restored = await wait_for(ready, timeout=30)
                    assert restored['execution_service']['instance_id'] == initial['execution_service']['instance_id']
                    connected = asyncio.Event()
                    stream_tasks.append(asyncio.create_task(consume(last_seq, connected)))
                    await asyncio.wait_for(connected.wait(), 25)
                    item['gateway_replacements'].append({'signal': number.name, 'old_pid': prior_gateway, 'new_pid': gateway.pid,
                         'replay_after_seq': last_seq, 'callback_health_during_gap': True, 'worker_provider_tool_unchanged': True})
                (workspace / 'release').touch()
                await wait_for(lambda: (workspace / 'completed.jsonl').is_file(), timeout=20)
                await wait_for(lambda: any(e.get('type')=='turn_finished' and e.get('run_id')==item['run_id'] for e in observed), timeout=90)
                page = await client.get(f'/api/sessions/{session_id}', params={'after':0, 'tail':'false','limit':1000})
                page.raise_for_status()
                events = page.json()['events']
                private_json(run / f'{backend}-final-events.json', events)
                started_events = [e for e in events if e.get('type')=='turn_started']
                finished_events = [e for e in events if e.get('type')=='turn_finished']
                failures = [e for e in events if e.get('type') in ('error','turn_stopped')]
                assert len(started_events)==1 and len(finished_events)==1 and not failures
                assert started_events[0]['run_id']==finished_events[0]['run_id']==item['run_id']
                assert marker in finished_events[0].get('result_text', ''), 'Final result marker is absent'
                assert len((workspace/'invocations.jsonl').read_text().splitlines())==1
                assert len((workspace/'completed.jsonl').read_text().splitlines())==1
                durable_sequences = [event['seq'] for event in events]
                await wait_for(lambda: last_seq >= durable_sequences[-1], timeout=10)
                assert [event['seq'] for event in observed if event['seq'] <= durable_sequences[-1]] == durable_sequences
                assert not stream_errors, stream_errors
                item['all_durable_events_observed_in_order'] = True
                item.update(passed=True, accepted_runs=1, completed_runs=1, tool_invocations=1, tool_completions=1,
                            websocket_sequences_ordered_unique=True, observed_event_count=len(observed), final_marker_present=True)
                print(json.dumps({'stage':'passed','backend':backend,'observed_events':len(observed)}),flush=True)
            except Exception as error:
                item['error'] = type(error).__name__ + ': ' + str(error)[:350]
                item['traceback'] = traceback.format_exc()
                print(json.dumps({'stage':'failed','backend':backend,'error_type':type(error).__name__}),flush=True)
            finally:
                (workspace/'release').touch()
                for task in stream_tasks:
                    task.cancel()
                    with suppress(asyncio.CancelledError): await task
                if events_file is not None: events_file.close()
                if not item['passed'] and session_id:
                    with suppress(Exception):
                        await client.post(f'/api/sessions/{session_id}/stop', json={})
                save()
        result['passed'] = all(item['passed'] for item in result['providers'].values())
    except Exception as error:
        result['setup_error'] = type(error).__name__ + ': ' + str(error)[:350]
    finally:
        for workspace in (temporary/'workspaces').glob('*'):
            if workspace.is_dir(): (workspace/'release').touch()
        await client.aclose()
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try: await asyncio.wait_for(asyncio.to_thread(process.wait), 30)
                except (TimeoutError, asyncio.TimeoutError):
                    process.kill()
                    await asyncio.to_thread(process.wait)
        for logfile in logs: logfile.close()
        result['owned_worker_and_gateways_exited'] = all(p.poll() is not None for p in processes)
        result['owned_provider_and_tool_exited'] = True
        for item in result['providers'].values():
            for identity in item.get('ancestry', [])[:-1]:
                try:
                    alive = process_identity(identity['pid']) == identity
                except subprocess.CalledProcessError:
                    alive = False
                if alive:
                    result['owned_provider_and_tool_exited'] = False
        result['passed'] = (result['passed'] and result['owned_worker_and_gateways_exited']
                            and result['owned_provider_and_tool_exited'])
        result['state_retained_for_review'] = str(temporary)
        save()
        print(json.dumps({'stage':'complete','passed':result['passed'],'receipt':str(run/'acceptance.json')}),flush=True)
    return result['passed']

if __name__=='__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
