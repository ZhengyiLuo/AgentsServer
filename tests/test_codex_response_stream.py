"""Custom stream normalization, pass-through, and exact credential routing."""
import asyncio
import json
import unittest
import httpx
from codex_response_stream import ErrorFrames, ResponseStreamBridge


async def chunks(*values):
    for value in values: yield value


async def collect(source):
    return b''.join([part async for part in source])


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_untyped_error_survives_every_chunk_boundary_and_eof(self):
        raw=b'data: {"error":{"code":"429","message":"TPM exceeded key-synthetic"}}\r\n\r\n'
        for i in range(len(raw)):
            result=await collect(ErrorFrames('key-synthetic').stream(chunks(raw[:i],raw[i:])))
            data=json.loads(result.split(b'data: ',1)[1]);self.assertEqual(data['type'],'response.failed')
            self.assertEqual(data['response']['error'],{'code':'rate_limit_exceeded','message':'TPM exceeded [redacted]'})
        result=await collect(ErrorFrames('key-synthetic').stream(chunks(raw.rstrip())))
        self.assertIn(b'response.failed',result)

    async def test_success_typed_errors_and_large_tool_frames_are_unchanged(self):
        for packet in [{'type':'response.output_text.delta','delta':'Hello'},
                {'type':'response.failed','response':{'error':{'code':'rate_limit_exceeded','message':'Wait'}}},
                {'type':'response.function_call_arguments.delta','delta':'x'*150000}]:
            raw=b'data: '+json.dumps(packet).encode()+b'\n\n'
            result=await collect(ErrorFrames('secret').stream(chunks(*(raw[i:i+777] for i in range(0,len(raw),777)))))
            self.assertEqual(result,raw)

    async def test_multiline_error_uses_original_response_identity(self):
        first=b'data: {"type":"response.created","response":{"id":"resp_original"}}\n\n'
        error=b'event: error\ndata: {"error":\ndata: {"message":"Failed","code":"500"}}\n\n'
        result=await collect(ErrorFrames('secret').stream(chunks(first+error)))
        self.assertTrue(result.startswith(first));self.assertIn(b'"id": "resp_original"',result[len(first):])
        self.assertNotIn(b'response.completed',result)

    async def test_two_bridges_forward_exact_bodies_to_their_own_endpoints(self):
        records=[]
        async def upstream(request):
            records.append((str(request.url),request.headers['authorization'],await request.aread()))
            return httpx.Response(200,headers={'content-type':'text/event-stream'},content=b'data: {"error":{"code":"429","message":"Wait"}}\n\n')
        for endpoint,key in [('https://one.invalid/v1','key-one'),('https://two.invalid/api','key-two')]:
            bridge=ResponseStreamBridge({'base_url':endpoint,'api_key':key})
            bridge.client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
                response=await client.post('/v1/responses',content=b'{"input":"unchanged"}',headers={'Authorization':'Bearer '+bridge.token})
                self.assertIn('response.failed',response.text)
                self.assertEqual((await client.post('/v1/responses',content=b'wrong')).status_code,401)
                self.assertEqual((await client.post('/v1/http://other.invalid',headers={'Authorization':'Bearer '+bridge.token})).status_code,404)
            await bridge.close()
        self.assertEqual(records,[(url+'/responses','Bearer '+key,b'{"input":"unchanged"}') for url,key in [('https://one.invalid/v1','key-one'),('https://two.invalid/api','key-two')]])

    async def test_replacing_bridge_releases_listener_and_accepts_new_requests(self):
        # Exercise real loopback sockets: a stale event-loop reader can strand
        # the next provider even though in-process ASGI requests still pass.
        async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
            for _ in range(3):
                bridge = ResponseStreamBridge({'base_url': 'https://upstream.invalid/v1', 'api_key': 'synthetic'})
                await bridge.start()
                await bridge.client.aclose()
                bridge.client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={'ok': True})))
                url = bridge.url + '/responses'
                try:
                    response = await client.post(url, json={'input': 'unchanged'},
                        headers={'Authorization': 'Bearer ' + bridge.token})
                    self.assertEqual(response.json(), {'ok': True})
                finally:
                    await asyncio.wait_for(bridge.close(), 3)
                with self.assertRaises(httpx.ConnectError):
                    await client.post(url, json={})

    async def test_http_error_status_and_retry_after_are_preserved(self):
        bridge=ResponseStreamBridge({'base_url':'https://one.invalid/v1','api_key':'key'})
        bridge.client=httpx.AsyncClient(transport=httpx.MockTransport(lambda req:httpx.Response(429,headers={'Retry-After':'7'},json={'error':{'message':'TPM'}})))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge.app),base_url='http://bridge') as client:
            response=await client.post('/v1/responses',headers={'Authorization':'Bearer '+bridge.token})
            self.assertEqual(response.status_code,429);self.assertEqual(response.headers['retry-after'],'7')
            self.assertEqual(response.json(),{'error':{'message':'TPM'}})
        await bridge.close()

    async def test_disconnect_cancels_upstream_waiting_for_headers(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def upstream(request):
            await request.aread()
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        bridge=ResponseStreamBridge({'base_url':'https://one.invalid/v1','api_key':'key'})
        bridge.client=httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        incoming=asyncio.Queue()
        incoming.put_nowait({'type':'http.request','body':b'{}','more_body':False})
        sent=[]
        async def send(message): sent.append(message)
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.3'},'http_version':'1.1',
            'method':'POST','scheme':'http','path':'/v1/responses','raw_path':b'/v1/responses',
            'query_string':b'','headers':[(b'authorization',('Bearer '+bridge.token).encode())],
            'client':('127.0.0.1',1),'server':('127.0.0.1',2),'root_path':''}
        task=asyncio.create_task(bridge.app(scope,incoming.get,send))
        try:
            await asyncio.wait_for(entered.wait(),2)
            incoming.put_nowait({'type':'http.disconnect'})
            await asyncio.wait_for(task,2)
            self.assertTrue(cancelled.is_set())
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task,return_exceptions=True)
            await bridge.close()
