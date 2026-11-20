import asyncio
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiogram.exceptions import TelegramNetworkError, TelegramServerError
from aiogram.methods import GetUpdates

import ayakactl
from app.config import Settings
from app.db_tools import backup, check
from app.runtime import (
    AlreadyRunning, RetryLimitExceeded, configure_logs, poll, process_lock, telegram_ready,
)
from app.storage import Storage, StorageUnavailable


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_locked_storage_retries_same_update_and_commits_before_ack(self):
        updates = [SimpleNamespace(update_id=40), SimpleNamespace(update_id=41)]
        stop = asyncio.Event()
        dispatcher = SimpleNamespace(feed_update=AsyncMock(side_effect=[StorageUnavailable(), None, None]))
        offsets = []

        class Bot:
            id = 10

            async def __call__(self, method, **kwargs):
                offsets.append(method.offset)
                if method.offset == 42:
                    stop.set()
                    return []
                return updates

        db = SimpleNamespace(polling_offset=AsyncMock(return_value=40), save_polling_offset=AsyncMock())
        with patch('app.runtime.wait_for_stop', return_value=False):
            await poll(Bot(), dispatcher, db, stop)
        self.assertEqual(offsets, [40, 40, 42])
        self.assertEqual([call.args[1] for call in db.save_polling_offset.await_args_list], [41, 42])

    async def test_checkpoint_lock_does_not_repeat_completed_handler(self):
        stop = asyncio.Event()
        dispatcher = SimpleNamespace(feed_update=AsyncMock())
        offsets = []

        class Bot:
            id = 10

            async def __call__(self, method, **kwargs):
                offsets.append(method.offset)
                if method.offset == 42:
                    stop.set()
                    return []
                return [SimpleNamespace(update_id=41)]

        db = SimpleNamespace(polling_offset=AsyncMock(return_value=41),
                             save_polling_offset=AsyncMock(side_effect=[StorageUnavailable(), None]))
        with patch('app.runtime.wait_for_stop', return_value=False):
            await poll(Bot(), dispatcher, db, stop)
        self.assertEqual(offsets, [41, 42])
        dispatcher.feed_update.assert_awaited_once()

    async def test_tor_error_recovers_and_logs_only_error_type(self):
        stop = asyncio.Event()
        network = TelegramNetworkError(GetUpdates(), 'synthetic-private-marker')
        bot = SimpleNamespace(me=AsyncMock(side_effect=[network, None]), delete_webhook=AsyncMock())
        with self.assertLogs('app.runtime', level='WARNING') as messages:
            with patch('app.runtime.wait_for_stop', return_value=False):
                self.assertTrue(await telegram_ready(bot, stop))
        self.assertEqual(bot.me.await_count, 2)
        bot.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)
        self.assertNotIn('synthetic-private-marker', str(messages.output))

    async def test_exhausted_network_retries_exit(self):
        failure = TelegramNetworkError(GetUpdates(), 'synthetic-private-marker')
        bot = SimpleNamespace(me=AsyncMock(side_effect=failure), delete_webhook=AsyncMock())
        with self.assertLogs('app.runtime', level='WARNING'):
            with patch('app.runtime.wait_for_stop', return_value=False) as wait:
                with self.assertRaises(RetryLimitExceeded) as exhausted:
                    await telegram_ready(bot, asyncio.Event())
        self.assertEqual(exhausted.exception.phase, 'getMe')
        self.assertEqual(exhausted.exception.attempts, 6)
        self.assertIsNone(exhausted.exception.http_status)
        self.assertNotIn('synthetic-private-marker', str(exhausted.exception))
        self.assertEqual(bot.me.await_count, 6)
        self.assertEqual(wait.await_count, 5)

    async def test_webhook_retry_reports_phase_and_safe_http_status(self):
        failure = TelegramServerError(GetUpdates(), 'fixture-only-response-detail')
        failure.http_status = 502
        bot = SimpleNamespace(me=AsyncMock(), delete_webhook=AsyncMock(side_effect=failure))
        with self.assertLogs('app.runtime', level='WARNING'):
            with patch('app.runtime.wait_for_stop', return_value=False):
                with self.assertRaises(RetryLimitExceeded) as exhausted:
                    await telegram_ready(bot, asyncio.Event())
        error = exhausted.exception
        self.assertEqual(error.phase, 'deleteWebhook')
        self.assertEqual(error.http_status, 502)
        self.assertIn('повторов 5', str(error))
        self.assertIn('TelegramServerError', str(error))
        self.assertIn('HTTP 502', str(error))
        self.assertNotIn('fixture-only-response-detail', str(error))

    async def test_poll_retry_reports_get_updates_phase(self):
        failure = TelegramNetworkError(GetUpdates(), 'fixture-only-network-detail')
        bot = AsyncMock(side_effect=failure)
        bot.id = 11
        db = SimpleNamespace(polling_offset=AsyncMock(return_value=None))
        with self.assertLogs('app.runtime', level='WARNING'):
            with patch('app.runtime.wait_for_stop', return_value=False):
                with self.assertRaises(RetryLimitExceeded) as exhausted:
                    await poll(bot, object(), db, asyncio.Event())
        self.assertEqual(exhausted.exception.phase, 'getUpdates')
        self.assertEqual(exhausted.exception.attempts, 6)
        self.assertEqual(bot.await_count, 6)

    async def test_empty_poll_releases_in_memory_offset(self):
        stop = asyncio.Event()
        offsets = []

        class Bot:
            id = 10

            async def __call__(self, method, **kwargs):
                offsets.append(method.offset)
                if len(offsets) == 2:
                    stop.set()
                return []

        db = SimpleNamespace(polling_offset=AsyncMock(return_value=42), save_polling_offset=AsyncMock())
        await poll(Bot(), SimpleNamespace(feed_update=AsyncMock()), db, stop)
        self.assertEqual(offsets, [42, None])

    async def test_graceful_stop_finishes_current_update_and_leaves_rest_pending(self):
        stop = asyncio.Event()
        handled = []

        async def feed(bot, update):
            handled.append(update.update_id)
            stop.set()

        class Bot:
            id = 10

            async def __call__(self, method, **kwargs):
                return [SimpleNamespace(update_id=40), SimpleNamespace(update_id=41)]

        db = SimpleNamespace(polling_offset=AsyncMock(return_value=40), save_polling_offset=AsyncMock())
        await poll(Bot(), SimpleNamespace(feed_update=feed), db, stop)
        self.assertEqual(handled, [40])
        db.save_polling_offset.assert_awaited_once_with(10, 41)


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_wal_backup_preserves_data_and_polling_position(self):
        with TemporaryDirectory() as temporary:
            folder = Path(temporary)
            settings = Settings(token='sample', owner_id=11, db_path=folder/'live.db',
                group_raw_days=14, private_raw_days=30, summary_days=180,
                per_chat_max_messages=100, db_max_mb=16, cleanup_minutes=60,
                max_text_chars=4000, max_memories=10)
            db = Storage(settings)
            await db.initialize()
            await db.save_polling_offset(10, 42)
            self.assertEqual(await Storage(settings).polling_offset(10), 42)
            with patch('app.storage.time.time', return_value=__import__('time').time()+7*86400):
                self.assertIsNone(await Storage(settings).polling_offset(10))
            with closing(sqlite3.connect(settings.db_path)) as writer:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('PRAGMA wal_autocheckpoint=0')
                writer.execute('PRAGMA user_version=6')
                writer.execute("INSERT INTO chats VALUES (-1,'group','fixture',12345)")
                writer.commit()
                self.assertTrue(Path(str(settings.db_path)+'-wal').exists())
                archive = folder/'backup.db'
                backup(settings.db_path, archive)
                check(archive)
                self.assertEqual(writer.execute('SELECT count(*) FROM chats').fetchone()[0], 1)
                with closing(sqlite3.connect(archive)) as copy:
                    self.assertEqual(copy.execute('SELECT count(*) FROM chats').fetchone()[0], 1)
                    self.assertEqual(copy.execute('SELECT next_offset FROM telegram_checkpoints').fetchone()[0], 42)
                    self.assertEqual(copy.execute('PRAGMA user_version').fetchone()[0], 6)
                    self.assertEqual(copy.execute('SELECT count(*) FROM diary_meta').fetchone()[0], 1)
                with self.assertRaises(ValueError):
                    backup(settings.db_path, settings.db_path)
                with self.assertRaises(ValueError):
                    backup(settings.db_path, archive)


class ServiceTests(unittest.TestCase):
    def test_process_lock_blocks_duplicate(self):
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'XDG_STATE_HOME': tmp}):
            with process_lock('bot.lock'):
                with self.assertRaises(AlreadyRunning):
                    with process_lock('bot.lock'):
                        self.fail('Second lock was acquired')

    def test_rotating_log_configuration(self):
        import logging
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        aiogram = logging.getLogger('aiogram')
        previous_aiogram_level = aiogram.level
        # Do not let force=True close the test runner's original handlers.
        root.handlers = []
        with TemporaryDirectory() as tmp, patch.dict('os.environ', {'XDG_STATE_HOME': tmp}):
            try:
                configure_logs('fixture.log')
                handler = root.handlers[0]
                self.assertEqual(handler.maxBytes, 1024 * 1024)
                self.assertEqual(handler.backupCount, 4)
            finally:
                for handler in root.handlers:
                    handler.close()
                root.handlers = handlers
                root.setLevel(level)
                aiogram.setLevel(previous_aiogram_level)

    def test_existing_service_and_external_socks_are_not_started_twice(self):
        settings = SimpleNamespace(telegram_proxy_url='socks5://127.0.0.1:9050', telegram_transport='socks5')
        services = Mock()
        services.show.return_value = {'ActiveState':'active'}
        with patch.object(ayakactl, 'wait_ready', return_value=0):
            self.assertEqual(ayakactl.start(services, settings), 0)
        services.start.assert_not_called()

        services.show.return_value = {'ActiveState':'inactive'}
        with (patch.object(ayakactl, 'wait_ready', return_value=0),
              patch.object(ayakactl, 'manual_bot_present', return_value=False),
              patch.object(ayakactl, 'socks_ready', return_value=True)):
            self.assertEqual(ayakactl.start(services, settings), 0)
        services.start.assert_called_once_with(ayakactl.BOT)

    def test_direct_and_external_proxy_do_not_start_local_tor(self):
        for transport, proxy in (('direct', None), ('socks5', 'socks5://proxy.invalid:9050')):
            with self.subTest(transport=transport, proxy=proxy):
                settings = SimpleNamespace(telegram_transport=transport, telegram_proxy_url=proxy)
                services = Mock()
                services.show.return_value = {'ActiveState': 'inactive'}
                with (patch.object(ayakactl, 'manual_bot_present', return_value=False),
                      patch.object(ayakactl, 'socks_ready') as socks,
                      patch.object(ayakactl, 'wait_ready', return_value=0)):
                    self.assertEqual(ayakactl.start(services, settings), 0)
                services.start.assert_called_once_with(ayakactl.BOT)
                socks.assert_not_called()

    def test_status_reports_manual_process_without_user_systemd(self):
        settings = SimpleNamespace(telegram_transport='direct', telegram_proxy_url=None,
                                   db_path=Path('/tmp/fixture.sqlite3'))
        with (patch('sys.argv', ['ayakactl.py', 'status']),
              patch.object(ayakactl, 'load_release_settings', return_value=settings),
              patch.object(ayakactl.UserServices, 'show', side_effect=ayakactl.UserSystemdUnavailable),
              patch.object(ayakactl, 'manual_bot_present', return_value=True),
              patch.object(ayakactl, 'check_ollama', new_callable=AsyncMock, return_value='готова'),
              patch('builtins.print') as output):
            self.assertEqual(ayakactl.cli(), 0)
        output.assert_any_call('Аяка: запущена вручную')
        output.assert_any_call('Telegram: прямое подключение')

    def test_process_management_orders_tor_and_bot(self):
        settings = SimpleNamespace(telegram_proxy_url='socks5://127.0.0.1:9050', telegram_transport='socks5')
        services = Mock()
        services.show.return_value = {'ActiveState':'inactive'}
        with (patch.object(ayakactl, 'manual_bot_present', return_value=False),
              patch.object(ayakactl, 'socks_ready', return_value=False),
              patch.object(ayakactl, 'native_tor_present', return_value=False),
              patch.object(ayakactl, 'wait_ready', return_value=0)):
            self.assertEqual(ayakactl.start(services, settings), 0)
            self.assertEqual([c.args[0] for c in services.start.call_args_list], [ayakactl.TOR, ayakactl.BOT])
            self.assertEqual(ayakactl.stop(services), 0)
            self.assertEqual([c.args[0] for c in services.stop.call_args_list], [ayakactl.BOT, ayakactl.TOR])
