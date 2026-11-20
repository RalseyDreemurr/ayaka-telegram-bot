from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta
import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

import ayakactl
import main
from app.archive import bounds, GROUP_CONSENT_MIGRATION
from app.config import Settings
from app.db_tools import (
    RELEASE_SCHEMA_VERSION, check, check_release_database, load_release_settings, prepare_release,
)
from app.diary_reader import DiaryReader
from app.runtime import AlreadyRunning, process_lock, wait_for_tor
from app.storage import Storage


class ReleaseDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings = Settings(
            token='sample', owner_id=11, db_path=self.root / 'release.sqlite3',
            group_raw_days=14, private_raw_days=30, summary_days=180,
            per_chat_max_messages=100, db_max_mb=16, cleanup_minutes=60,
            max_text_chars=4000, max_memories=20,
        )

    async def message(self, db, chat, mid, user, ts=None):
        return await db.save_message(
            chat_id=chat, chat_type='group', chat_title='Тестовая группа',
            user_id=user, display_name='Участник', username=None, telegram_message_id=mid,
            text='Обсуждаем Minecraft', created_ts=ts,
        )

    async def test_fresh_schema_and_both_empty_diaries(self):
        await prepare_release(self.settings, ())
        db = Storage(self.settings)
        check(self.settings.db_path)
        check_release_database(self.settings.db_path)
        with closing(sqlite3.connect(self.settings.db_path)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone(), (RELEASE_SCHEMA_VERSION,))
            markers = conn.execute('SELECT key FROM diary_meta').fetchall()
            self.assertEqual({name for name, in markers}, {'backfill', GROUP_CONSENT_MIGRATION})

        await db.initialize()
        await db.archive.maintain()
        reader = DiaryReader(db.archive)
        self.assertIn('не нашла', await reader.answer('Что вчера было в чате -10012345?'))
        self.assertIn('недостаточно', await reader.answer('Покажи свой дневник за вчера'))
        self.assertFalse(await db.memories_for_context(11, True))
        self.assertIsNone(await db.polling_offset(12))
        self.assertFalse(await self.message(db, -10012345, 1, 20))
        self.assertFalse(await db.save_assistant_message(-10012345, 2, 'Ответ'))
        self.assertFalse(await db.set_preferred_name(20, 'Участник', None, 'Псевдоним', chat_id=-10012345))
        self.assertIsNone(await db.remember('Память', chat_id=-10012345))
        with closing(sqlite3.connect(self.settings.db_path)) as conn:
            for table, in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'"):
                if table != 'diary_meta':
                    with self.subTest(table=table):
                        self.assertIsNone(conn.execute(f'SELECT 1 FROM "{table}"').fetchone())

    async def test_copy_only_refusals_and_save_new_data_after_consent(self):
        legacy_paths = (self.root / 'original.sqlite3', self.root / 'copy.sqlite3')
        for index, path in enumerate(legacy_paths, start=1):
            legacy = Storage(replace(self.settings, db_path=path))
            await legacy.initialize()
            await self.message(legacy, -10, 1, 20)
            await legacy.remember('Тестовое воспоминание')
            await legacy.save_polling_offset(12, 42)
            await legacy.forget_user_in_chat(-10, 21)
            await legacy.archive.query(legacy.archive.group_history, -index, False)
            with self.assertRaises(RuntimeError):
                check_release_database(path)

        await prepare_release(self.settings, legacy_paths)
        with self.assertRaises(FileExistsError):
            await prepare_release(self.settings, legacy_paths)
        db = Storage(self.settings)
        await db.initialize()
        arc = db.archive
        self.assertFalse(await db.list_chats())
        self.assertFalse(await db.list_memories())
        self.assertIsNone(await db.polling_offset(12))
        for chat in (-1, -2, -10):
            self.assertFalse(await arc.query(arc.history_enabled, chat))
        with closing(sqlite3.connect(self.settings.db_path)) as conn:
            self.assertEqual(conn.execute('SELECT chat_id FROM diary_disabled ORDER BY chat_id').fetchall(),
                             [(-2,), (-1,)])
            self.assertEqual(conn.execute('SELECT chat_id, user_id FROM opt_outs').fetchall(),
                             [(-10, 21)])
        await arc.query(arc.group_history, -10, True)
        self.assertFalse(await self.message(db, -10, 1, 21))
        yesterday = (datetime.now(arc.zone).date() - timedelta(days=1)).isoformat()
        ts = bounds(yesterday, arc.zone)[0] + 60
        self.assertTrue(await self.message(db, -10, 2, 20, ts))
        self.assertTrue(await self.message(db, -10, 3, 22, ts+1))
        await arc.maintain()
        for table in ('chat_diary_entries', 'daily_diary_entries'):
            row = await arc.query(lambda c: c.execute(f'SELECT day FROM {table}').fetchone())
            self.assertIsNotNone(row)
            self.assertEqual(row['day'], yesterday)
        await db.save_polling_offset(12, 43)
        reopened = Storage(self.settings)
        await reopened.initialize()
        self.assertEqual(await reopened.polling_offset(12), 43)
        self.assertFalse(await self.message(reopened, -10, 1, 21))

    async def test_release_uses_configured_path_and_rejects_legacy_database(self):
        await prepare_release(self.settings, ())
        with patch('app.db_tools.load_settings', return_value=self.settings):
            self.assertIs(load_release_settings(), self.settings)
        alias = self.root / 'alias.sqlite3'
        alias.symlink_to(self.settings.db_path)
        with self.assertRaises(RuntimeError), patch(
            'app.db_tools.load_settings', return_value=replace(self.settings, db_path=alias),
        ):
            load_release_settings()
        legacy = replace(self.settings, db_path=self.root / 'old.sqlite3')
        await Storage(legacy).initialize()
        with self.assertRaises(RuntimeError), patch(
            'app.db_tools.load_settings', return_value=legacy,
        ):
            load_release_settings()

    def test_release_lock_is_independent_of_state_home(self):
        with process_lock('bot.lock', directory=self.root):
            with patch('app.runtime.state_dir', return_value=self.root / 'other'):
                with self.assertRaises(AlreadyRunning), process_lock('bot.lock', directory=self.root):
                    self.fail('Second process lock acquired')
        with process_lock('bot.lock', directory=self.root):
            pass


class ReleaseStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_tor_retries_then_checks_remote_connect(self):
        proxied_socket = Mock()
        client = Mock(connect=AsyncMock(side_effect=[OSError(), proxied_socket]))
        with (patch('app.runtime.Proxy.from_url', return_value=client) as build,
              patch('app.runtime.wait_for_stop', new_callable=AsyncMock)):
            self.assertTrue(await wait_for_tor('socks5://127.0.0.1:9050', asyncio.Event()))
        build.assert_called_once_with('socks5://127.0.0.1:9050', rdns=True)
        self.assertEqual(client.connect.await_count, 2)
        self.assertEqual(client.connect.call_args.kwargs['dest_host'], 'api.telegram.org')
        self.assertEqual(client.connect.call_args.kwargs['dest_port'], 443)
        proxied_socket.close.assert_called_once_with()

    async def test_telegram_stays_disconnected_when_tor_fails(self):
        settings = Mock(telegram_proxy_url='socks5://127.0.0.1:9050', telegram_transport='socks5')
        session = Mock(close=AsyncMock())
        with (patch.object(main, 'telegram_session', return_value=session),
              patch.object(main, 'wait_for_tor', new_callable=AsyncMock, side_effect=TimeoutError),
              patch.object(main, 'Bot') as bot, patch.object(main, 'Storage') as storage):
            with self.assertRaises(TimeoutError):
                await main.main(settings)
        bot.assert_not_called()
        storage.assert_not_called()
        session.close.assert_awaited_once()

    def test_transient_service_passes_release_flag(self):
        services = ayakactl.UserServices()
        with (patch.object(services, 'show', return_value={'LoadState': 'not-found'}),
              patch.object(services, 'command', return_value=Mock(returncode=0)) as run):
            services.start(ayakactl.BOT)
        self.assertEqual(run.call_args.args[0][-2:], [str(ayakactl.ROOT / 'main.py'), '--release'])


class InstallerTests(unittest.TestCase):
    def test_init_db_command_creates_custom_path_and_refuses_to_replace_it(self):
        with TemporaryDirectory() as tmp:
            database = Path(tmp) / 'private' / 'fresh.sqlite3'
            env = dict(os.environ, DB_PATH=str(database), OWNER_ID='11',
                       TELEGRAM_BOT_TOKEN='12345:offline-only', PYTHON_DOTENV_DISABLED='1')
            command = [sys.executable, '-B', str(ayakactl.ROOT / 'ayakactl.py'), 'init-db']
            first = subprocess.run(command, cwd=tmp, env=env, capture_output=True,
                                   text=True, timeout=12)
            self.assertEqual(first.returncode, 0, first.stdout)
            check_release_database(database)
            check(database)
            self.assertEqual(database.stat().st_mode & 0o777, 0o600)
            before = database.read_bytes()
            second = subprocess.run(command, cwd=tmp, env=env, capture_output=True,
                                    text=True, timeout=12)
            self.assertEqual(second.returncode, 2, second.stdout)
            self.assertEqual(database.read_bytes(), before)
