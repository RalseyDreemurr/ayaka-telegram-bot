import asyncio
from contextlib import closing
from datetime import datetime, timedelta, date
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

from app.ai import OllamaError
from app.archive import bounds
from app.config import Settings
from app.diary_reader import DiaryReader, period
from app.storage import Storage


class DiaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.settings = Settings(token='unused', owner_id=11, db_path=Path(tmp.name)/'test.sqlite',
                                 group_raw_days=14,private_raw_days=30,summary_days=180,
                                 per_chat_max_messages=100,db_max_mb=16,cleanup_minutes=60,
                                 max_text_chars=4000,max_memories=40)
        self.db = Storage(self.settings)
        await self.db.initialize()
        self.a = self.db.archive
        self.day = (datetime.now(self.a.zone).date()-timedelta(days=1)).isoformat()
        self.ts = bounds(self.day,self.a.zone)[0]+3600
        self.reader = DiaryReader(self.a)

    async def put(self,chat,mid,text,user=20,title='Таверна'):
        return await self.db.save_message(chat_id=chat,chat_type='private' if chat>0 else 'group',
                chat_title=title,user_id=user,display_name='Лина' if user==20 else 'Максим',
                username=None,telegram_message_id=mid,text=text,created_ts=self.ts+mid)

    async def rows(self,table):
        return await self.a.query(lambda c:list(c.execute('SELECT * FROM '+table)))

    async def test_summary_and_source_survive_raw_retention(self):
        await self.put(-1,1,'Я люблю Minecraft.')
        await self.put(-1,2,'Спасибо за помощь с Arch',21)
        await self.a.maintain()
        await self.db.cleanup(now=self.ts+16*86400)
        self.assertFalse(await self.rows('messages'))
        self.assertEqual(len(await self.rows('chat_summaries')),1)
        self.assertEqual([r['mid'] for r in await self.rows('diary_sources')],[1,2])
        self.assertIn('minecraft',(await self.reader.answer('Что вчера было в чате Таверна?')).lower())

    async def test_no_hallucination_or_secret_and_self_report_only(self):
        await self.put(-1,1,'Наверное, Лина любит Minecraft')
        await self.put(-1,2,'пароль с сервера: Minecraft-secret')
        await self.put(-1,3,'Я люблю Linux.')
        await self.a.maintain()
        self.assertEqual([r['mid'] for r in await self.rows('diary_sources')],[1,3])
        self.assertEqual(len(await self.rows('memories')),1)
        source, = await self.rows('memory_sources')
        self.assertEqual((source['uid'],source['mid'],source['chat_id']),(20,3,-1))
        answer = await self.reader.answer('Аяка, кто вчера спорил о видеокартах?')
        self.assertIn('недостаточно',answer)

    async def test_two_levels_private_sparse_restart_and_late_data(self):
        for chat in (-1,-2,11):
            await self.put(chat,1,'Обсуждаем Minecraft',11 if chat==11 else 20)
            await self.put(chat,2,'Спасибо за помощь с Linux',11 if chat==11 else 21)
        await self.put(-3,1,'Просто привет')
        await self.a.maintain()
        self.assertEqual({r['chat_id'] for r in await self.rows('chat_diary_entries')},{-1,-2})
        self.assertEqual(len(await self.rows('daily_diary_entries')),1)
        self.db = Storage(self.settings)
        await self.db.initialize()
        self.a = self.db.archive
        await self.put(-1,3,'Обновили видеокарту')
        await self.a.maintain()
        self.assertEqual(len(await self.rows('chat_summaries')),3)
        self.assertEqual(len(await self.rows('daily_diary_entries')),1)
        self.assertIn('видеокарт',(await self.rows('daily_diary_entries'))[0]['body'])
        self.assertEqual(len(await self.rows('diary_sources')),7)

    async def test_ambiguous_names_and_scoped_answer(self):
        await self.put(-1,1,'Про Terraria')
        await self.put(-2,1,'Про Minecraft')
        answer = await self.reader.answer('Что вчера было в чате Таверна?')
        self.assertIn('Уточни группу',answer)
        answer = await self.reader.answer('-2')
        self.assertIn('minecraft',answer.lower())
        self.assertNotIn('terraria',answer.lower())

    async def test_erasure_rebuilds_without_deleted_person(self):
        await self.put(-1,1,'Я люблю Minecraft.')
        await self.put(-1,2,'Обсуждаем Linux',21)
        await self.a.maintain()
        await self.db.forget_user_in_chat(-1,20)
        await self.a.maintain()
        self.assertFalse(await self.rows('daily_diary_entries'))
        self.assertFalse(await self.rows('memories'))
        self.assertNotIn('minecraft',str([tuple(r) for r in await self.rows('chat_summaries')]).lower())
        self.assertFalse(await self.put(-1,3,'Опять Minecraft'))

    async def test_bounded_failed_reviews_and_foreground_priority(self):
        await self.put(-1,1,'В Arch')
        await self.put(-1,2,'Про Linux')
        ai = Mock(busy=True,answer=AsyncMock(side_effect=OllamaError('offline')))
        await self.a.maintain(ai)
        ai.answer.assert_not_awaited()
        ai.busy = False
        for _ in range(5):
            await self.a.query(lambda c:c.execute('UPDATE diary_jobs SET retry=0'))
            await self.a.maintain(ai)
        self.assertEqual(ai.answer.await_count,3)
        self.assertEqual(len(await self.rows('chat_diary_entries')),1)

    async def test_group_optout_cascades_and_expiry(self):
        await self.put(-1,1,'Про Linux')
        await self.put(-1,2,'Про Arch')
        await self.a.maintain()
        await self.a.query(self.a.group_history,-1,False)
        self.assertFalse(await self.put(-1,3,'Про Arch'))
        for table in ('daily_diary_entries','diary_sources','messages','chat_summaries'):
            self.assertFalse(await self.rows(table))
        await self.a.query(self.a.group_history,-1,True)
        await self.put(-1,4,'Про Arch')
        await self.a.maintain()
        await self.db.cleanup(now=self.ts+182*86400)
        self.assertFalse(await self.rows('diary_sources'))
        self.assertFalse(await self.rows('chat_summaries'))

    async def test_loss_before_processing_is_marked_without_logging_content(self):
        await self.put(-1,1,'Я люблю Minecraft.')
        with self.assertLogs('app.storage',level='WARNING') as log:
            await self.db.cleanup(now=self.ts+16*86400)
        self.assertNotIn('Minecraft',' '.join(log.output))
        self.assertEqual((await self.rows('diary_jobs'))[0]['lost'],1)

    async def test_correct_and_forget_fact_and_group_event(self):
        await self.put(-1,1,'Мы провели турнир по Terraria в 19:30.')
        await self.put(-1,2,'Спасибо за помощь')
        await self.a.maintain()
        fact, = await self.rows('memories')
        self.assertIn('турнир',fact['content'])
        self.assertTrue(await self.a.query(self.a.edit,fact['id'],'Турнир завершён.'))
        self.assertTrue(await self.db.forget_memory(fact['id']))
        self.assertFalse(await self.rows('memory_sources'))
        day, = await self.rows('daily_diary_entries')
        self.assertIn('Terraria',day['body'])

    async def test_forgetting_one_author_preserves_other_archived_evidence(self):
        await self.put(-1,1,'Я люблю Terraria.')
        await self.put(-1,2,'Я люблю Linux.',21)
        await self.a.maintain()
        await self.db.cleanup(now=self.ts+16*86400)
        await self.db.forget_user_in_chat(-1,20)
        fact, = await self.rows('memories')
        self.assertEqual(fact['subject_user_id'],21)
        summary, = await self.rows('chat_summaries')
        self.assertNotIn('terraria',summary['summary'].lower())
        self.assertIn('linux',summary['summary'].lower())

    async def test_wish_is_not_promoted_to_confirmed_event(self):
        await self.put(-1,1,'Я хочу, чтобы мы провели турнир по Terraria.')
        await self.a.maintain()
        self.assertFalse(await self.rows('memory_sources'))
        self.assertIsNone((await self.rows('diary_sources'))[0]['detail'])

    async def test_successful_review_is_grounded_and_not_repeated(self):
        await self.put(-1,1,'В Arch')
        await self.put(-1,2,'Про Linux')
        ai = Mock(busy=False, answer=AsyncMock(return_value='1'))
        await self.a.maintain(ai)
        await self.a.maintain(ai)
        ai.answer.assert_awaited_once()
        self.assertEqual(len(await self.rows('daily_diary_sources')),1)

    async def test_open_topic_and_scheduled_meeting_are_attributed(self):
        await self.put(-1,1,'Обсуждаем выставку керамики.')
        await self.put(-1,2,'Мы договорились встретиться возле музея в субботу.')
        await self.a.maintain()
        result = await self.reader.answer('Что вчера было в чате Таверна?')
        self.assertIn('выставку керамики', result)
        self.assertIn('возле музея',result)
        self.assertIn('Лина',result)

    def test_context_reserves_space_and_keeps_identity_on_small_window(self):
        from app.ai import fit_context, prepare_messages
        messages = prepare_messages(history=[],new_text='Как дела?',speaker='Владелец',
                                    current_message_id=1,author_is_owner=True,is_group=False,memories=[])
        messages.insert(1,{'role':'user','content':'устаревший контекст '*3000})
        result = fit_context(messages,1024,320)
        self.assertEqual([item['role'] for item in result],['system','user'])
        self.assertIn('подтверждённый создатель',result[0]['content'])
        self.assertIn('Как дела?',result[-1]['content'])

    async def test_daily_partial_is_replaced_when_other_group_finishes(self):
        for chat in (-1,-2):
            await self.put(chat,1,'Новости Minecraft')
            await self.put(chat,2,'Обновили ядро')
        await self.a.maintain(batches=1)
        self.assertEqual((await self.rows('daily_diary_entries'))[0]['status'],'partial')
        await self.a.maintain()
        self.assertEqual((await self.rows('daily_diary_entries'))[0]['status'],'ready')
        self.assertEqual(len(await self.rows('daily_diary_sources')),2)

    async def test_pre_diary_database_is_backfilled_once(self):
        import sqlite3
        from dataclasses import replace
        legacy = replace(self.settings,db_path=self.settings.db_path.with_name('legacy.sqlite3'))
        # Populate a legacy owner note before the diary tables exist.
        with closing(sqlite3.connect(legacy.db_path)) as c, c:
            c.execute("""CREATE TABLE memories(id INTEGER PRIMARY KEY,scope_chat_id INTEGER,
                      subject_user_id INTEGER,content TEXT,pinned INTEGER,created_ts INTEGER,expires_ts INTEGER)""")
            c.execute("INSERT INTO memories VALUES(1,NULL,NULL,'Старая заметка',1,?,NULL)",(self.ts,))
        db = Storage(legacy)
        await db.initialize()
        await db.initialize()
        refs = await db.archive.query(lambda c:list(c.execute('SELECT * FROM memory_sources')))
        self.assertEqual(len(refs),1)
        self.assertEqual((refs[0]['kind'],refs[0]['mid']),('legacy',None))
        self.assertEqual((await db.list_memories())[0]['content'],'Старая заметка')

    async def test_unlisted_topic_is_supported_by_two_independent_speakers(self):
        await self.put(-1,1,'У нас на столе керамика и фарфор.')
        await self.put(-1,2,'Мне тоже интересна керамика, особенно местная.',21)
        await self.a.maintain()
        entry, = await self.rows('chat_diary_entries')
        self.assertIn('керамика',entry['body'])
        self.assertEqual(len(await self.rows('diary_sources')),2)

    async def test_event_question_filters_evidence_across_groups(self):
        await self.put(-1,1,'Обсуждаем Minecraft',title='Таверна')
        await self.put(-2,2,'Обсуждаем Terraria',21,title='Клуб')
        await self.a.maintain()
        result = await self.reader.answer('Кто вчера говорил про Terraria?')
        self.assertIn('Клуб',result)
        self.assertNotIn('Таверна',result)

    async def test_stage_four_schema_upgrade_preserves_archive_and_links(self):
        await self.put(-1,1,'Мы провели турнир по Terraria')
        await self.put(-1,2,'Спасибо за помощь',21)
        await self.a.maintain()
        before = [tuple(row) for row in await self.rows('daily_diary_entries')]
        self.assertTrue(before)
        # The fixture represents a pre-stage-five database on the temporary path.
        await self.a.query(lambda conn: conn.execute('DROP TABLE chat_profiles'))
        await self.db.initialize()
        await self.db.initialize()
        self.assertEqual([tuple(row) for row in await self.rows('daily_diary_entries')], before)
        self.assertEqual(len(await self.rows('daily_diary_sources')), 1)
        self.assertEqual(await self.a.query(lambda c: c.execute('PRAGMA foreign_key_check').fetchall()), [])
        self.assertEqual(await self.rows('chat_profiles'), [])

    def test_calendar_and_dst(self):
        lo,hi=bounds('2025-03-30',ZoneInfo('Europe/Berlin'))
        self.assertEqual(hi-lo,23*3600)
        self.assertEqual(period('за последние три дня',date(2025,1,1)),
                         (date(2024,12,30),date(2025,1,1)))
