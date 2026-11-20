import asyncio
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest

from app.config import Settings
from app.storage import Storage, DAY


class StorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            token='fake', owner_id=111, db_path=Path(self.temp.name) / 'test.sqlite3',
            group_raw_days=14, private_raw_days=30, summary_days=180,
            per_chat_max_messages=100, db_max_mb=16, cleanup_minutes=60,
            max_text_chars=4000, max_memories=3,
        )
        self.db = Storage(self.settings)
        await self.db.initialize()

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def put(self, chat_id=-99, user_id=1, message_id=1, age_days=0,
                  chat_type='group', text='Сообщение'):
        return await self.db.save_message(
            chat_id=chat_id, chat_type=chat_type, chat_title='Тест',
            user_id=user_id, display_name='Тестер', username='tester',
            telegram_message_id=message_id, text=text,
            created_ts=int(time.time()) - age_days * DAY,
        )

    async def test_deduplicate_and_isolation(self):
        self.assertTrue(await self.put(chat_id=-1, message_id=1, text='В первом'))
        self.assertFalse(await self.put(chat_id=-1, message_id=1, text='Дубликат'))
        self.assertTrue(await self.put(chat_id=-2, message_id=1, text='Во втором'))
        self.assertEqual([r['text'] for r in await self.db.recent_chat_messages(-1)], ['В первом'])
        self.assertEqual([r['text'] for r in await self.db.recent_chat_messages(-2)], ['Во втором'])

    async def test_duplicate_does_not_change_chat_metadata(self):
        await self.put(chat_id=-1, message_id=1, chat_type='supergroup')
        self.assertFalse(await self.put(chat_id=-1, message_id=1, chat_type='group', age_days=2))
        chats = await self.db.list_chats()
        self.assertEqual(chats[0]['chat_type'], 'supergroup')

    async def test_page_limit_applies_to_new_sqlite_connections(self):
        with closing(self.db._connect()) as conn:
            page_size = conn.execute('PRAGMA page_size').fetchone()[0]
            max_pages = conn.execute('PRAGMA max_page_count').fetchone()[0]
        self.assertEqual(max_pages * page_size, self.settings.db_max_mb * 1024 ** 2)

    async def test_reclaim_consumes_all_vacuum_steps(self):
        with closing(self.db._connect()) as conn:
            conn.execute('CREATE TABLE vacuum_test(payload BLOB)')
            conn.executemany('INSERT INTO vacuum_test VALUES (zeroblob(4000))', [()] * 20)
            conn.execute('DELETE FROM vacuum_test')
            conn.commit()
            self.assertGreater(conn.execute('PRAGMA freelist_count').fetchone()[0], 1)
            self.db._reclaim(conn)
            self.assertEqual(conn.execute('PRAGMA freelist_count').fetchone()[0], 0)

    async def test_retention_group_vs_private(self):
        await self.put(chat_id=-1, message_id=1, age_days=20)
        await self.put(chat_id=111, user_id=111, message_id=1, age_days=20, chat_type='private')
        await self.put(chat_id=-1, message_id=2, age_days=1)
        report = await self.db.cleanup()
        self.assertEqual(report.deleted_raw, 1)
        self.assertEqual(len(await self.db.recent_chat_messages(-1)), 1)
        self.assertEqual(len(await self.db.recent_chat_messages(111)), 1)

    async def test_opt_out_removes_data_and_blocks_future(self):
        await self.put(chat_id=-1, message_id=1)
        await self.put(chat_id=-1, message_id=2, user_id=2)
        removed = await self.db.forget_user_in_chat(-1, 1)
        self.assertEqual(removed, 1)
        self.assertFalse(await self.put(chat_id=-1, message_id=3))
        self.assertEqual(len(await self.db.recent_chat_messages(-1)), 1)
        await self.db.opt_in(-1, 1)
        self.assertTrue(await self.put(chat_id=-1, message_id=3))

    async def test_long_term_notes_and_cap(self):
        ids = [await self.db.remember(f'Факт {i}') for i in range(3)]
        self.assertEqual(len(set(ids)), 3)
        self.assertIsNone(await self.db.remember('лишний'))
        await self.db.cleanup()
        self.assertEqual(len(await self.db.list_memories()), 3)
        self.assertTrue(await self.db.forget_memory(ids[0]))
        self.assertIsNotNone(await self.db.remember('Новый факт'))

    async def test_per_chat_limit(self):
        for i in range(105):
            await self.put(message_id=i + 1)
        result = await self.db.cleanup()
        self.assertEqual(result.deleted_raw, 5)
        self.assertEqual(len(await self.db.recent_chat_messages(-99, limit=100)), 100)

    async def test_preferred_name_persists(self):
        await self.db.set_preferred_name(55, 'Имя', 'user55', 'Котик')
        self.assertEqual(await self.db.get_preferred_name(55), 'Котик')
        await self.db.set_preferred_name(55, 'Имя', 'user55', None)
        self.assertIsNone(await self.db.get_preferred_name(55))

    async def test_assistant_reply_saved_with_role(self):
        await self.put(chat_id=-1, message_id=1, text='Привет')
        self.assertTrue(await self.db.save_assistant_message(-1, 2, 'Я рада'))
        rows = await self.db.recent_chat_messages(-1)
        self.assertEqual([r['role'] for r in rows], ['user', 'assistant'])
        self.assertEqual(rows[1]['speaker'], 'Аяка')


    async def test_malformed_identifiers_do_not_reach_sql_writes(self):
        from app.security import parse_id
        for value in ('-1,2', '-14;', '1 + 1', '0', '１２３', str(2**63), '9'*1000):
            with self.subTest(value=value):
                self.assertIsNone(parse_id(value))
        self.assertEqual(await self.db.recent_chat_messages('-1,2'), [])
        self.assertEqual(await self.db.forget_user_in_chat(-1, True), 0)
        self.assertFalse(await self.put(chat_id=-(2**63)-1, message_id=1))

    async def test_legacy_profiles_are_never_assigned_to_an_unverified_chat(self):
        await self.db.set_preferred_name(1, 'Лина', None, 'Старая подпись')
        await self.put(chat_id=-1, message_id=1, user_id=1)
        await self.db.initialize()
        self.assertIsNone(await self.db.get_preferred_name(1, chat_id=-1))
        rows = await self.db.recent_chat_messages(-1)
        self.assertNotEqual(rows[0]['speaker'], 'Старая подпись')

    async def test_foreign_private_history_stays_hidden_after_owner_change(self):
        from dataclasses import replace
        await self.put(chat_id=111, user_id=111, message_id=1, chat_type='private')
        await self.db.remember('Личная заметка')
        other = Storage(replace(self.settings, owner_id=222))
        await other.initialize()
        self.assertEqual(await other.recent_chat_messages(111), [])
        self.assertEqual(await other.list_chats(), [])
        self.assertEqual(await other.list_memories(), [])

    async def test_owner_global_notes_not_in_group(self):
        await self.put(chat_id=-1, message_id=1)
        await self.db.remember('Люблю спокойные игры', pinned=True)
        self.assertEqual(len(await self.db.memories_for_context(-1, False)), 0)
        self.assertEqual(len(await self.db.memories_for_context(111, True)), 1)


if __name__ == '__main__':
    unittest.main()
