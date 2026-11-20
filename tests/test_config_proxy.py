import asyncio
import os
from pathlib import Path
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiogram import Bot
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.methods import GetMe

import main
from app.config import Settings, TelegramConfigurationError, load_settings
from app.storage import Storage


class TelegramProxyTests(unittest.IsolatedAsyncioTestCase):
    def test_settings_without_proxy_cannot_start_telegram_session(self):
        settings = Settings(
            token='fake', owner_id=1, db_path=Path('/tmp/fake.sqlite3'),
            group_raw_days=14, private_raw_days=30, summary_days=180,
            per_chat_max_messages=100, db_max_mb=16, cleanup_minutes=60,
            max_text_chars=4000, max_memories=100,
        )
        self.assertIsNone(settings.telegram_proxy_url)
        for url in (settings.telegram_proxy_url, '', 'http://127.0.0.1:1', 'socks5://'):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, 'TELEGRAM_PROXY_URL'):
                    main.telegram_session(url)

    def test_legacy_settings_keep_socks5_and_direct_requires_explicit_selection(self):
        fixture = {'TELEGRAM_BOT_TOKEN': 'sample', 'OWNER_ID': '11',
                   'TELEGRAM_PROXY_URL': 'socks5://127.0.0.1:9050'}
        with patch.dict(os.environ, fixture, clear=True), patch('app.config.load_dotenv'):
            legacy = load_settings()
            self.assertEqual(legacy.telegram_transport, 'socks5')
            self.assertEqual(legacy.telegram_proxy_url, fixture['TELEGRAM_PROXY_URL'])
            os.environ.update(TELEGRAM_PROXY_URL='', TELEGRAM_TRANSPORT='direct')
            direct = load_settings()
            self.assertEqual(direct.telegram_transport, 'direct')
            self.assertIsNone(direct.telegram_proxy_url)

    def test_transport_rejects_contradictory_direct_and_invalid_modes(self):
        with self.assertRaisesRegex(TelegramConfigurationError, 'TELEGRAM_PROXY_URL'):
            main.telegram_session('socks5://127.0.0.1:9050', transport='direct')
        with self.assertRaisesRegex(TelegramConfigurationError, 'TELEGRAM_TRANSPORT'):
            main.telegram_session(None, transport='unknown')

    async def test_aiogram_direct_transport_uses_local_api_without_environment_proxy(self):
        requests = []

        async def get_me(request):
            requests.append(request.match_info['method'])
            return web.json_response({'ok': True, 'result': {
                'id': 12345, 'is_bot': True, 'first_name': 'Offline',
            }})

        app = web.Application()
        app.router.add_post('/bot{token}/{method}', get_me)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = runner.addresses[0][1]
        session = main.telegram_session(None, transport='direct')
        session.api = TelegramAPIServer.from_base(f'http://127.0.0.1:{port}')
        bot = Bot(token='12345:synthetic-for-testing', session=session)
        with patch.dict(os.environ, {'HTTP_PROXY':'http://127.0.0.1:1', 'NO_PROXY':''}):
            async with session:
                self.assertEqual((await bot(GetMe(), request_timeout=3)).id, 12345)
        self.assertEqual(requests, ['getMe'])

    async def test_direct_startup_does_not_attempt_socks_readiness(self):
        session = SimpleNamespace(close=AsyncMock(), middleware=Mock())
        storage = AsyncMock(spec=Storage)
        storage.guard = Mock()
        storage.quick_check.return_value = True
        ai = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        settings = SimpleNamespace(token='sample', telegram_proxy_url=None, telegram_transport='direct')
        with (
            patch.object(main, 'telegram_session', return_value=session) as session_factory,
            patch.object(main, 'Storage', return_value=storage),
            patch.object(main, 'Bot'),
            patch.object(main, 'OllamaClient', return_value=ai),
            patch.object(main, 'setup_dispatcher'),
            patch.object(main, 'telegram_ready', new_callable=AsyncMock, return_value=False) as ready,
            patch.object(main, 'wait_for_tor', new_callable=AsyncMock) as wait,
        ):
            await main.main(settings)
        session_factory.assert_called_once_with(None, transport='direct')
        wait.assert_not_called()
        ready.assert_awaited_once()
        session.close.assert_awaited_once()

    async def test_telegram_session_preserves_received_http_status(self):
        session = main.telegram_session('socks5://127.0.0.1:1')
        bot = Bot(token='12345:synthetic-for-testing', session=session)
        async with session:
            with self.assertRaises(TelegramServerError) as error:
                session.check_response(bot, GetMe(), 502,
                    '{"ok": false, "error_code": 502, "description": "fixture-only-text"}')
        self.assertEqual(error.exception.http_status, 502)

    async def test_aiogram_uses_socks5_remote_dns_and_does_not_fall_back(self):
        handshake = asyncio.get_running_loop().create_future()

        async def refuse_via_socks(reader, writer):
            try:
                version, method_count = await reader.readexactly(2)
                methods = await reader.readexactly(method_count)
                writer.write(bytes((5, 0)))
                await writer.drain()
                header = await reader.readexactly(4)
                size = (await reader.readexactly(1))[0]
                host = await reader.readexactly(size)
                port = int.from_bytes(await reader.readexactly(2), 'big')
                writer.write(bytes((5, 5, 0, 1, 0, 0, 0, 0, 0, 0)))
                await writer.drain()
                handshake.set_result((version, methods, header, host, port))
            except Exception as exc:
                if not handshake.done():
                    handshake.set_exception(exc)
            finally:
                writer.close()
                await writer.wait_closed()

        getaddrinfo = socket.getaddrinfo

        def loopback_dns(host, *args, **kwargs):
            if host not in ('127.0.0.1', '::1', 'localhost', None):
                self.fail('Unexpected local resolution of Telegram hostname')
            return getaddrinfo(host, *args, **kwargs)

        server = await asyncio.start_server(refuse_via_socks, '127.0.0.1', 0)
        async with server:
            port = server.sockets[0].getsockname()[1]
            session = main.telegram_session(f'socks5://127.0.0.1:{port}')
            session.api = TelegramAPIServer.from_base('http://telegram.invalid')
            bot = Bot(token='12345:synthetic-for-testing', session=session)
            with patch('socket.getaddrinfo', side_effect=loopback_dns):
                async with session:
                    with self.assertRaisesRegex(TelegramNetworkError, 'Tor SOCKS5'):
                        await bot(GetMe(), request_timeout=3)
                    version, methods, header, host, port = await asyncio.wait_for(handshake, 3)

        self.assertEqual(version, 5)
        self.assertIn(0, methods)
        self.assertEqual(header, bytes((5, 1, 0, 3)))
        self.assertEqual((host, port), (b'telegram.invalid', 80))

    async def test_startup_failure_closes_telegram_and_ollama_sessions(self):
        session = SimpleNamespace(close=AsyncMock(), middleware=Mock())
        storage = AsyncMock(spec=Storage)
        storage.guard = Mock()
        storage.quick_check.return_value = True
        storage.cleanup.return_value = SimpleNamespace(bytes_total=0)
        ai = SimpleNamespace(start=AsyncMock(), close=AsyncMock())
        bot = SimpleNamespace(me=AsyncMock(side_effect=RuntimeError('synthetic failure')))
        settings = SimpleNamespace(token='fake', telegram_proxy_url='socks5://127.0.0.1:1',
                                   telegram_transport='socks5')
        with (
            patch.object(main, 'wait_for_tor', new_callable=AsyncMock, return_value=True),
            patch.object(main, 'telegram_session', return_value=session),
            patch.object(main, 'Storage', return_value=storage),
            patch.object(main, 'Bot', return_value=bot),
            patch.object(main, 'OllamaClient', return_value=ai),
            patch.object(main, 'setup_dispatcher'),
            patch.object(main.logging, 'basicConfig'),
        ):
            with self.assertRaisesRegex(RuntimeError, 'synthetic failure'):
                await main.main(settings)

        ai.start.assert_awaited_once()
        ai.close.assert_awaited_once()
        session.close.assert_awaited_once()
