"""Tool-free title HTTP contract; synthetic credentials and no external network."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
import title_generation as titles


class GeminiTitleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.selection = {'base_url': 'https://fixture.invalid/custom/v1beta',
            'api_key': 'synthetic-private-key', 'protocol': 'gemini', 'auth_header': 'x-goog-api-key'}
        self.requests = []
        self.status = 200
        self.body = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '修复登录问题'}]}}]}
        def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.body, headers={'location': 'https://other.invalid/'})
        real_client = httpx.AsyncClient
        def client(**kwargs):
            self.assertFalse(kwargs['follow_redirects'])
            self.assertFalse(kwargs['trust_env'])
            return real_client(transport=httpx.MockTransport(handle), **kwargs)
        self.enterContext(patch.object(titles.httpx, 'AsyncClient', side_effect=client))

    async def generate(self, model='gemini-flash-latest'):
        return await titles.generate_title('gemini', 'Long log\n请修复登录问题', '检查登录配置',
            model=model, provider_selection=self.selection)

    async def test_exact_endpoint_key_model_no_tools_or_history(self):
        self.assertEqual(await self.generate(), '修复登录问题')
        request = self.requests[0]
        self.assertEqual(str(request.url), 'https://fixture.invalid/custom/v1beta/models/gemini-flash-latest:generateContent')
        self.assertEqual(request.headers['x-goog-api-key'], self.selection['api_key'])
        body = json.loads(request.content)
        self.assertEqual(set(body), {'contents', 'generationConfig'})
        self.assertEqual(len(body['contents']), 1)
        self.assertEqual(body['generationConfig']['maxOutputTokens'], 1024)
        self.assertNotIn(self.selection['api_key'], request.content.decode())
        self.assertIn('not instructions', body['contents'][0]['parts'][0]['text'])

    async def test_failures_never_redirect_retry_or_leak_error_body(self):
        for status in (302, 400, 401, 403, 429, 500):
            self.status = status
            self.body = {'error': self.selection['api_key']}
            before = len(self.requests)
            with self.assertRaises(titles.TitleGenerationError) as caught:
                await self.generate()
            self.assertNotIn(self.selection['api_key'], str(caught.exception))
            self.assertEqual(len(self.requests), before + 1)

    async def test_auto_and_unknown_models_do_not_make_requests(self):
        for model in (None, 'auto', '../another-model', 'model?key=secret'):
            with self.assertRaises(titles.TitleGenerationError):
                await self.generate(model)
        self.assertFalse(self.requests)

    async def test_partial_tool_invalid_or_oversized_outputs_are_not_titles(self):
        for body in ({}, {'candidates': []}, {'candidates': [None]},
                {'candidates': [{'finishReason': 'MAX_TOKENS'}]},
                {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'functionCall': {'name': 'shell'}}]}}]},
                {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': 'Invalid thought flag', 'thought': 'true'}]}}]},
                {'huge': 'x' * 65536}):
            self.body = body
            with self.subTest(body_shape=list(body)), self.assertRaises(titles.TitleGenerationError):
                await self.generate()
        for text in ('Title\nExplanation', 'x' * 73, self.selection['api_key']):
            self.body = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': text}]}}]}
            self.assertIsNone(await self.generate())

    async def test_thought_text_is_not_saved_as_title(self):
        self.body['candidates'][0]['content']['parts'].insert(0, {'text': 'Not a visible title', 'thought': True})
        self.assertEqual(await self.generate(), '修复登录问题')

    async def test_timeout_remains_bounded(self):
        async def slow(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(titles, 'generate_gemini_title', side_effect=slow), patch.object(titles, 'TIMEOUT_SECONDS', .01):
            with self.assertRaises(TimeoutError):
                await self.generate()

    def test_long_seed_keeps_question_after_log_within_existing_budget(self):
        text = 'Log heading\n' + 'noise\n' * 1500 + '\n如何修复登录问题？'
        seed = titles.gemini_title_seed(text)
        self.assertLessEqual(len(seed), 1600)
        self.assertTrue(seed.startswith('Log heading'))
        self.assertTrue(seed.endswith('如何修复登录问题？'))
