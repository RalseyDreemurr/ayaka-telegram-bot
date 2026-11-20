import os
import unittest
from dataclasses import replace
from unittest.mock import patch
from pathlib import Path
from aiohttp import web

from app.ai import OllamaClient, OllamaError
from app.config import Settings


class OllamaProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.seen_payload = None
        self.tags_body = {'models': [{'name': 'qwen3:8b-q4_K_M'}]}
        self.chat_body = {
            'message': {'role': 'assistant', 'content': 'Я очень рада! 😺'},
            'done': True,
        }
        self.chat_response = None

        async def tags(request):
            return web.json_response(self.tags_body)

        async def chat(request):
            self.seen_payload = await request.json()
            if self.chat_response is not None:
                return self.chat_response
            return web.json_response(self.chat_body)

        application = web.Application()
        application.router.add_get('/api/tags', tags)
        application.router.add_post('/api/chat', chat)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        settings = Settings(token='fake', owner_id=1,
                            db_path=Path('/tmp/ayaka-fake.db'),
                            group_raw_days=14, private_raw_days=30,
                            summary_days=180, per_chat_max_messages=100,
                            db_max_mb=16, cleanup_minutes=60,
                            max_text_chars=4000, max_memories=3,
                            ollama_base_url=f'http://127.0.0.1:{port}/v1')
        self.client = OllamaClient(settings)
        await self.client.start()

    async def asyncTearDown(self):
        await self.client.close()
        await self.runner.cleanup()

    async def test_direct_native_ollama_api(self):
        models = await self.client.models()
        self.assertIn('qwen3:8b-q4_K_M', models)
        response = await self.client.answer([{'role': 'user', 'content': 'Привет'}])
        self.assertEqual(response, 'Я очень рада!')
        self.assertFalse(self.seen_payload['think'])
        self.assertFalse(self.seen_payload['stream'])
        self.assertEqual(self.seen_payload['options']['num_ctx'], 4096)
        self.assertEqual(self.seen_payload['model'], 'qwen3:8b-q4_K_M')

    async def test_invalid_response_shapes(self):
        for body in (None, [], {'models': None}, {'models': [None]}):
            with self.subTest(tags=body):
                self.tags_body = body
                with self.assertRaisesRegex(OllamaError, 'некорректный список'):
                    await self.client.models()
        for body in (None, {'message': None}, {'message': {'content': 7}},
                     {'message': {'content': '<think>hidden</think>'}}):
            with self.subTest(chat=body):
                self.chat_body = body
                with self.assertRaises(OllamaError):
                    await self.client.answer([])

    async def test_invalid_json_becomes_service_error(self):
        self.chat_response = web.Response(text='{bad json')
        with self.assertRaisesRegex(OllamaError, 'некорректный JSON'):
            await self.client.answer([])

    async def test_http_error_never_logs_response_body(self):
        self.chat_response = web.Response(status=500, text='private test marker')
        with self.assertLogs('app.ai', level='WARNING') as log:
            with self.assertRaisesRegex(OllamaError, 'HTTP 500'):
                await self.client.answer([])
        self.assertNotIn('private test marker', '\n'.join(log.output))

    async def test_redirect_is_not_followed(self):
        self.chat_response = web.Response(status=307, headers={'Location': '/api/chat'})
        with self.assertLogs('app.ai', level='WARNING'):
            with self.assertRaisesRegex(OllamaError, 'HTTP 307'):
                await self.client.answer([])


    async def test_background_and_foreground_admission_are_bounded(self):
        from app.ai import OllamaBusy
        import asyncio
        entered = asyncio.Event()
        release = asyncio.Event()

        async def wait(*args, **kwargs):
            entered.set()
            await release.wait()
            return 'Готово.'

        with patch.object(self.client, '_answer', side_effect=wait):
            async with asyncio.TaskGroup() as group:
                for _ in range(5):
                    group.create_task(self.client.answer([]))
                await entered.wait()
                with self.assertRaises(OllamaBusy):
                    await self.client.answer([], background=True)
                with self.assertRaises(OllamaBusy):
                    await self.client.answer([])
                release.set()
        self.assertFalse(self.client.busy)

    async def test_model_tool_metadata_cannot_request_application_actions(self):
        self.chat_body = {'message': {'content': 'Пример ответа.',
                                      'tool_calls': [{'function': {'name': 'fixture_action'}}]}}
        self.assertEqual(await self.client.answer([]), 'Пример ответа.')
        self.assertNotIn('tools', self.seen_payload)

    async def test_model_credential_reply_and_large_response_are_blocked(self):
        self.chat_body = {'message': {'content': 'TELEGRAM_BOT_TOKEN=fixture-value'}}
        self.assertEqual(await self.client.answer([]), '[скрыто]')
        self.chat_response = web.Response(body=b'x'*(1024*1024+1))
        with self.assertRaisesRegex(OllamaError, 'слишком большой'):
            await self.client.answer([])

    async def test_ollama_ignores_tor_and_environment_proxy(self):
        settings = replace(self.client.settings, telegram_proxy_url='socks5://127.0.0.1:1')
        direct = OllamaClient(settings)
        self.addAsyncCleanup(direct.close)
        with patch.dict(os.environ, {
            'HTTP_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': '', 'no_proxy': '',
        }):
            await direct.start()
            self.assertIn(settings.ollama_model, await direct.models())
            self.assertEqual(await direct.answer([]), 'Я очень рада!')
