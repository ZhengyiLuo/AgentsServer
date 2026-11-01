"""Keep nonstandard custom Responses errors visible to native Codex.

An owned loopback transport forwards requests unchanged to one immutable endpoint.
Only untyped SSE error packets are normalized; native Codex owns all retries.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
import hmac
import json
import secrets
import socket
from urllib.parse import quote

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from codex_app_server import CodexAppServerManager
from codex_provider import ENV_KEY, PROVIDER_ID, config_args

# Larger frames pass through unchanged, rather than buffering arbitrary output.
# This is a parser memory bound, not a response or content size limit.
_ERROR_FRAME_BYTES = 64 * 1024
_HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
                'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}


class ErrorFrames:
    def __init__(self, secret: str):
        self.secret = secret
        self.pending = bytearray()
        self.passthrough = False
        self.response_id = 'resp_custom_endpoint_error'

    def frame(self, raw: bytes) -> bytes:
        try:
            data = b'\n'.join(line[5:].lstrip(b' ') for line in raw.splitlines() if line.startswith(b'data:'))
            packet = json.loads(data)
        except (ValueError, UnicodeError):
            return raw
        if not isinstance(packet, dict):
            return raw
        response = packet.get('response')
        if isinstance(response, dict) and isinstance(response.get('id'), str):
            self.response_id = response['id']
        error = packet.get('error')
        if packet.get('type') or not error:
            return raw
        if isinstance(error, dict):
            message = error.get('message')
            if not isinstance(message, str) or not message:
                return raw
            code = str(error.get('code') or error.get('type') or 'server_error')
        elif isinstance(error, str):
            message, code = error, 'server_error'
        else:
            return raw
        code = {'429': 'rate_limit_exceeded', '401': 'invalid_api_key',
                '403': 'permission_denied', '400': 'invalid_request_error',
                '500': 'server_error', '502': 'server_error', '503': 'server_error'}.get(code, code)
        for value in (self.secret, quote(self.secret, safe='')):
            if value:
                message = message.replace(value, '[redacted]')
        failed = {'type': 'response.failed', 'response': {
            'id': self.response_id, 'object': 'response', 'created_at': 0,
            'status': 'failed', 'output': [], 'error': {'code': code, 'message': message}}}
        return b'event: response.failed\ndata: ' + json.dumps(failed, ensure_ascii=False).encode() + b'\n\n'

    async def stream(self, chunks):
        # Split at SSE blank lines, including CRLF boundaries across chunks.
        async for chunk in chunks:
            self.pending.extend(chunk)
            while self.pending:
                lf = self.pending.find(b'\n\n')
                crlf = self.pending.find(b'\r\n\r\n')
                endings = [(i, n) for i, n in ((lf, 2), (crlf, 4)) if i >= 0]
                if endings:
                    i, n = min(endings)
                    raw = bytes(self.pending[:i+n]); del self.pending[:i+n]
                    yield raw if self.passthrough or len(raw) > _ERROR_FRAME_BYTES else self.frame(raw)
                    self.passthrough = False
                elif len(self.pending) > _ERROR_FRAME_BYTES:
                    # Retain only a delimiter tail; do not truncate a large frame.
                    yield bytes(self.pending[:-3]); del self.pending[:-3]
                    self.passthrough = True
                else:
                    break
        if self.pending:
            raw = bytes(self.pending); self.pending.clear()
            yield raw if self.passthrough else self.frame(raw)


class _OwnedConfig(uvicorn.Config):
    def configure_logging(self):
        # Do not change the host server's process-wide logging configuration.
        pass


class _OwnedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # This transport must never take over its host server's signal handlers.
        yield


class ResponseStreamBridge:
    def __init__(self, selected: dict):
        self.upstream = selected['base_url'].rstrip('/')
        self.key = selected['api_key']
        self.token = secrets.token_urlsafe(32)
        self.url = ''
        self.task = self.server = self.client = self.socket = None
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        app.add_api_route('/v1/{path:path}', self.forward, methods=['POST', 'GET', 'DELETE'])
        self.app = app

    async def start(self):
        if self.task is not None and not self.task.done():
            return
        self.client = httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=False)
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0)); self.socket.listen(128); self.socket.setblocking(False)
        self.url = f'http://127.0.0.1:{self.socket.getsockname()[1]}/v1'
        self.server = _OwnedServer(_OwnedConfig(self.app, log_config=None, access_log=False, lifespan='off', log_level='critical'))
        self.task = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        try:
            while not self.server.started:
                if self.task.done():
                    await self.task
                    raise RuntimeError('Custom response transport could not start')
                await asyncio.sleep(0)
        except BaseException:
            await self.close()
            raise

    async def forward(self, request: Request, path: str):
        if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + self.token):
            return Response(status_code=401)
        # The destination is immutable. A request cannot name a different origin.
        if not (path == 'responses' or path.startswith('responses/')) or '..' in path.split('/'):
            return Response(status_code=404)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS | {'authorization', 'accept-encoding', 'cookie'}}
        headers.update(authorization='Bearer ' + self.key, **{'accept-encoding': 'identity'})
        if 'content-length' in request.headers:
            headers['content-length'] = request.headers['content-length']
        uploaded = asyncio.Event()
        async def upload():
            try:
                async for chunk in request.stream(): yield chunk
            finally:
                uploaded.set()
        async def disconnected():
            # Wait for the upload reader to finish before reading disconnects.
            # No polling, execution deadline, or competing request-body reader.
            await uploaded.wait()
            while (await request.receive()).get('type') != 'http.disconnect':
                pass
        req = self.client.build_request(request.method, self.upstream + '/' + path,
            params=request.query_params.multi_items(), headers=headers, content=upload())
        sending = asyncio.create_task(self.client.send(req, stream=True))
        disconnect = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait((sending, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:
                if sending.done() and not sending.cancelled() and sending.exception() is None:
                    await sending.result().aclose()
                return Response(status_code=499)
            upstream = sending.result()
        except httpx.HTTPError:
            return Response('Custom endpoint connection failed', status_code=502)
        finally:
            for task in (sending, disconnect):
                if not task.done(): task.cancel()
            await asyncio.gather(sending, disconnect, return_exceptions=True)
        headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_HEADERS | {'content-encoding', 'set-cookie'}}
        async def body():
            try:
                chunks = upstream.aiter_bytes()
                if upstream.status_code == 200 and 'text/event-stream' in upstream.headers.get('content-type', '').lower():
                    chunks = ErrorFrames(self.key).stream(chunks)
                async for chunk in chunks:
                    yield chunk
            finally:
                await upstream.aclose()
        return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)

    async def close(self):
        if self.server is not None:
            self.server.should_exit = True
            # The owning native process has already closed; cancel only this
            # listener's in-flight transports, including a silent upstream.
            tasks = tuple(self.server.server_state.tasks)
            for task in tasks: task.cancel()
            if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        if self.task is not None:
            # Let Uvicorn close its asyncio listeners and remove their readers.
            # Cancelling serve() skips shutdown and can leave a stale selector
            # entry when the next provider reuses the same socket descriptor.
            with suppress(asyncio.CancelledError): await self.task
        if self.client is not None: await self.client.aclose()
        if self.socket is not None: self.socket.close()
        self.task = self.server = self.client = self.socket = None


class CustomCodexAppServerManager(CodexAppServerManager):
    """Native manager with an owned, credential-scoped stream normalizer."""
    def __init__(self, *args, selected: dict, **kwargs):
        self.response_bridge = ResponseStreamBridge(selected)
        prepare = kwargs.pop('before_start', None)
        environment = kwargs.pop('env_factory')
        original_args = tuple(kwargs.get('app_server_args', ()))
        async def before_start():
            if prepare is not None: await prepare()
            await self.response_bridge.start()
            self.client.app_server_args = (*original_args, *config_args({
                f'model_providers.{PROVIDER_ID}.base_url': self.response_bridge.url}))
        def env_factory():
            return {**environment(), ENV_KEY: self.response_bridge.token}
        kwargs['sensitive_values'] = (*kwargs.get('sensitive_values', ()), self.response_bridge.token)
        super().__init__(*args, before_start=before_start, env_factory=env_factory, **kwargs)

    async def close(self):
        try:
            await super().close()
        finally:
            await self.response_bridge.close()
