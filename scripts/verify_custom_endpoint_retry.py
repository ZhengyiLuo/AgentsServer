"""Native retry acceptance against an isolated synthetic Responses endpoint."""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_response_stream import CustomCodexAppServerManager
from codex_provider import native_args, ENV_KEY
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--codex', required=True, help='Native Codex executable to test')
parser.add_argument('--output', required=True, type=Path, help='Directory for disposable state and receipt')
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
ROOT = Path(tempfile.mkdtemp(prefix='custom-retry-', dir=args.output.resolve()))
os.chmod(ROOT, 0o700)
BIN = str(Path(args.codex).resolve())
print('Evidence: ' + str(ROOT), flush=True)

class Handler(BaseHTTPRequestHandler):

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        self.server.calls += 1
        case = self.server.case
        n = self.server.calls
        print('REQUEST ' + case + ' ' + str(n), flush=True)
        self.server.records.append({'call': n, 'has_tool_result': any((i.get('type') == 'function_call_output' for i in request.get('input', [])))})
        if case == 'http503' and n == 1:
            body = b'{"error":{"message":"Synthetic transient overload"}}'
            self.send_response(503)
            self.send_header('Retry-After', '0')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        response = {'id': 'resp_retry', 'object': 'response', 'created_at': int(time.time()), 'status': 'in_progress', 'output': []}
        events = [{'type': 'response.created', 'response': response}]
        if case == 'tool_once' and n == 1:
            output = [{'type': 'function_call', 'id': 'fc_once', 'call_id': 'call_once', 'name': 'record_once', 'arguments': '{}', 'status': 'completed'}]
        elif case == 'exhausted' or (case in ('malformed', 'eof') and n == 1) or (case == 'tool_once' and n == 2):
            if case != 'eof':
                events.append({'error': {'code': '429', 'message': 'Synthetic upstream TPM limit'}})
            output = None
        else:
            output = [{'type': 'message', 'id': 'msg_retry', 'role': 'assistant', 'phase': 'final_answer', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'RETRY_OK', 'annotations': []}]}]
        if output is not None:
            for i, item in enumerate(output):
                events.extend([{'type': 'response.output_item.added', 'output_index': i, 'item': item}, {'type': 'response.output_item.done', 'output_index': i, 'item': item}])
            events.append({'type': 'response.completed', 'response': {**response, 'status': 'completed', 'output': output, 'usage': {'input_tokens': 10, 'output_tokens': 2, 'total_tokens': 12}}})
        body = ''.join(('data: ' + json.dumps(e) + '\n\n' for e in events)).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

async def run(case):
    home = ROOT / case
    home.mkdir()
    ch = home / 'codex'
    ch.mkdir()
    (ch / 'config.toml').write_text('check_for_update_on_startup=false\n[analytics]\nenabled=false\n')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.case = case
    server.calls = 0
    server.records = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    selected = {'base_url': f'http://127.0.0.1:{server.server_port}/v1', 'api_key': 'synthetic-only', 'model': 'gpt-5.4'}
    env = {'HOME': str(home), 'CODEX_HOME': str(ch), 'PATH': '/usr/bin:/bin', 'RUST_LOG': 'off', 'OTEL_SDK_DISABLED': 'true', ENV_KEY: selected['api_key']}
    tool_calls = 0

    async def tool(request_id, method, params):
        nonlocal tool_calls
        assert method == 'item/tool/call' and params['tool'] == 'record_once', (method, params)
        tool_calls += 1
        return {'success': True, 'contentItems': [{'type': 'inputText', 'text': 'RECORDED_ONCE'}]}
    manager = CustomCodexAppServerManager(BIN, selected=selected, cwd=str(home), env_factory=lambda: dict(env), app_server_args=native_args(selected), server_request_handler=tool, initialize_params={'clientInfo': {'name': 'retry_acceptance', 'version': '1'}, 'capabilities': {'experimentalApi': True}})
    try:
        tid = await manager.start_thread({'cwd': str(home), 'approvalPolicy': 'never', 'sandbox': 'read-only', 'model': 'gpt-5.4', 'dynamicTools': [{'name': 'record_once', 'description': 'Record one synthetic side effect', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}]})
        turn = await manager.start_turn(tid, [{'type': 'text', 'text': 'Complete the isolated retry check.'}], overrides={'effort': 'low'})
        # Bound only this synthetic acceptance run, never production work.
        async with asyncio.timeout(90):
            while True:
                event = await turn.next_notification(timeout=30)
                if event.get('method') == 'turn/completed':
                    result = event['params']['turn']
                    break
        if case == 'exhausted':
            assert result['status'] == 'failed' and 'Synthetic upstream TPM limit' in result['error']['message'], result
            assert server.calls == 6, server.calls
        else:
            assert result['status'] == 'completed', result
            assert server.calls == (3 if case == 'tool_once' else 2), server.calls
        assert tool_calls == (1 if case == 'tool_once' else 0), tool_calls
        if case == 'tool_once':
            assert all((x['has_tool_result'] for x in server.records[1:])), server.records
        receipt = {'case': case, 'status': result['status'], 'requests': server.calls, 'tool_calls': tool_calls, 'requests_after_tool_contain_result': case == 'tool_once', 'error': result.get('error')}
        print(json.dumps(receipt), flush=True)
        await turn.close()
        return receipt
    finally:
        await manager.close()
        server.shutdown()
        server.server_close()

async def main():
    results = []
    for case in ('malformed', 'eof', 'http503', 'tool_once', 'exhausted'):
        results.append(await run(case))
    (ROOT / 'receipt.json').write_text(json.dumps({'native': BIN, 'provider': 'synthetic loopback Responses endpoint', 'passed': True, 'cases': results}, indent=2))
    print('PASSED ' + str(ROOT), flush=True)
if __name__ == '__main__':
    asyncio.run(main())
