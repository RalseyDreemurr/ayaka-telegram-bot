"""Source-backed summaries and daily journals. All SQLite work runs off the event loop."""
import asyncio
from contextlib import closing
from datetime import date, datetime, time as clock, timedelta
import json
import logging
import re
import sqlite3
import time
from zoneinfo import ZoneInfo

from .ai import OllamaBusy, OllamaError
from .persona import OWNER_NAME
from .group import topic_keys

TOPICS = ('игры', 'аниме и манга', 'Linux', 'компьютерное железо')
DETAILS = re.compile(r'\b(Minecraft|Terraria|Steam|Arch|Linux|AMD|NVIDIA|Radeon|Ryzen|Intel|GeForce|SSD|аниме|манга|видеокарт[а-я]*)\b', re.I)
SECRET = re.compile(r'парол|токен|секрет|password|secret|api[_ -]?key|bearer|BEGIN .*KEY|://|\b\d{5,}:[\w-]{12,}|[\w+/=-]{24,}', re.I)
PREFERENCE = re.compile(r'^(?:я (?:люблю|предпочитаю)|моя любимая игра [—-])\s+(.+?)[.!]?$', re.I)
PUBLIC_LATIN = {'linux','arch','minecraft','terraria','steam','amd','nvidia','radeon','intel',
                'ryzen','python','rust','debian','fedora','git','windows','ssd','unity','godot'}
PRIVATE_FIELD = re.compile(r'ключ|логин|мост|bridge|адрес|телефон|почт[ауы]|паспорт|код доступ|пин.?код|личн.{0,5}данн',re.I)


def public_phrase(value):
    """Admit short ordinary phrases; drop credentials, contacts and opaque identifiers."""
    if len(value)>110 or SECRET.search(value) or PRIVATE_FIELD.search(value):
        return False
    if not re.fullmatch(r'[а-яёa-z0-9 ,:—-]+',value,re.I):
        return False
    if re.search(r'\d{4,}|[а-яёa-z]\d|\d[а-яёa-z]',value,re.I):
        return False
    return all(word.lower() in PUBLIC_LATIN for word in re.findall(r'[a-z]+',value,re.I))

GROUP_CONSENT_MIGRATION = "release_group_consent_v1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS diary_jobs(
 chat_id INTEGER NOT NULL, day TEXT NOT NULL, cursor INTEGER NOT NULL DEFAULT 0,
 dirty INTEGER NOT NULL DEFAULT 1, attempts INTEGER NOT NULL DEFAULT 0, retry INTEGER NOT NULL DEFAULT 0,
 lost INTEGER NOT NULL DEFAULT 0, local_attempts INTEGER NOT NULL DEFAULT 0,
 local_retry INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(chat_id,day));
CREATE INDEX IF NOT EXISTS diary_pending ON diary_jobs(dirty,retry,day);
CREATE TABLE IF NOT EXISTS diary_sources(
 chat_id INTEGER NOT NULL, day TEXT NOT NULL, raw_id INTEGER NOT NULL,
 mid INTEGER, uid INTEGER NOT NULL, ts INTEGER NOT NULL, topics TEXT NOT NULL,
 preference TEXT,detail TEXT, UNIQUE(chat_id,mid),PRIMARY KEY(chat_id,raw_id));
CREATE INDEX IF NOT EXISTS diary_source_day ON diary_sources(chat_id,day,ts);
CREATE TABLE IF NOT EXISTS diary_summary_index(
 chat_id INTEGER NOT NULL,day TEXT NOT NULL,summary_id INTEGER UNIQUE REFERENCES chat_summaries(id) ON DELETE CASCADE,
 PRIMARY KEY(chat_id,day));
CREATE TABLE IF NOT EXISTS chat_diary_entries(
 chat_id INTEGER NOT NULL, day TEXT NOT NULL, summary_id INTEGER REFERENCES chat_summaries(id) ON DELETE SET NULL,
 start INTEGER NOT NULL, end INTEGER NOT NULL, body TEXT NOT NULL, impression TEXT NOT NULL,
 topics TEXT NOT NULL, status TEXT NOT NULL, created INTEGER NOT NULL, updated INTEGER NOT NULL,
 PRIMARY KEY(chat_id,day));
CREATE TABLE IF NOT EXISTS daily_diary_entries(
 day TEXT PRIMARY KEY, start INTEGER NOT NULL,end INTEGER NOT NULL,body TEXT NOT NULL,
 status TEXT NOT NULL,created INTEGER NOT NULL,updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS daily_diary_sources(
 day TEXT NOT NULL REFERENCES daily_diary_entries(day) ON DELETE CASCADE, chat_id INTEGER NOT NULL,
 PRIMARY KEY(day,chat_id), FOREIGN KEY(chat_id,day) REFERENCES chat_diary_entries(chat_id,day) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS memory_sources(
 memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
 chat_id INTEGER, uid INTEGER, mid INTEGER, ts INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memory_corrections(
 memory_id INTEGER PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE, editor INTEGER NOT NULL,
 mid INTEGER,ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS diary_enabled(chat_id INTEGER PRIMARY KEY CHECK(chat_id < 0));
CREATE TABLE IF NOT EXISTS diary_disabled(chat_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS diary_meta(key TEXT PRIMARY KEY);
"""
def bounds(day, zone):
    start = date.fromisoformat(day)
    return tuple(int(datetime.combine(d, clock.min, zone).timestamp())
                 for d in (start, start + timedelta(days=1)))


def safe_name(value):
    value = (value or '').strip()
    return value if not SECRET.search(value) and not re.search(r'\d{5,}',value) and re.fullmatch(r'[\w .—-]{1,48}', value) else 'участник'


# Only export bounded, recognisable event phrases. Surrounding conversation stays in raw history.
ACTION = re.compile(
    r'\b(?:перенесли|назначили|отменили|провели|выиграли|начали|закончили|обсудили|'
    r'установил[аи]?|обновил[аи]?|починил[аи]?|восстановил[аи]?|настроил[аи]?)\s+'
    r'(?:(?:новый|игровой|наш|вечерний)\s+)?'
    r'(?:турнир|матч|встречу|сервер|проект|сборку|драйвер|ядро|Arch(?: Linux)?|Linux|Minecraft|Terraria)'
    r'(?:\s+(?:по|в|на)\s+(?:Minecraft|Terraria|пятницу|субботу|воскресенье|понедельник|завтра))?'
    r'(?:\s+в\s+(?:[01]?\d|2[0-3]):[0-5]\d)?\b', re.I)
def event_detail(text):
    if SECRET.search(text) or '?' in text or re.search(r'\b(?:не|если|возможно|наверное|будто)\b',text,re.I):
        return None
    # An event embedded in a wish, quotation or allegation is not a self-report.
    body = re.sub(r'^(?:(?:сегодня|вчера|наконец|утром|вечером)\s+)?(?:(?:я|мы)\s+)?',
                  '',text.strip(),flags=re.I)
    match = ACTION.match(body)
    if match and not body[match.end():].strip(' .!'):
        return match[0]
    phrase = body.rstrip('.!')
    if (re.match(r'^(?:договорились|встретились|перенесли|отменили|завершили|'
                 r'открыли|нашли|починили|подготовили|собрались|устроили|провели|'
                 r'назначили|решили|выиграли|начали|обсудили)\s+',phrase,re.I)
            and public_phrase(phrase)):
        return phrase
    return None


def title_label(value, chat=None):
    if SECRET.search(value or '') or PRIVATE_FIELD.search(value or ''):
        return 'группа' if chat is None else f'группа {chat}'
    cleaned = ' '.join((value or '').split())
    return cleaned[:96] or 'группа'


def evidence(text):
    if SECRET.search(text):
        return [], None
    labels = [TOPICS[i] for i in topic_keys(text)]
    labels.extend(match.group().casefold() for match in DETAILS.finditer(text))
    if re.search(r'\b(?:спасибо|благодарю)\b', text, re.I):
        labels.append('благодарность')
    if re.search(r'\b(?:не согласен|не согласна|возражаю)\b', text, re.I):
        labels.append('возражение')
    if re.search(r'\b(?:заткнись|идиотка|дебилка)\b', text, re.I):
        labels.append('резкие слова')
    if event_detail(text):
        labels.append('событие')
    pref = PREFERENCE.fullmatch(text.strip())
    stated = pref[1].casefold() if pref and public_phrase(pref[1]) else None
    if stated:
        labels.append('личное предпочтение')
    topic = re.match(r'^(?:сегодня\s+)?(?:обсуждаем|обсудили|говорили про|поговорили про)\s+(.+?)[!.]?$',text.strip(),re.I)
    if topic and public_phrase(topic[1]):
        labels.append(topic[1].casefold())
    return list(dict.fromkeys(labels)), stated


COMMON_WORDS = frozenset("""
сегодня вчера завтра просто очень можно нужно тоже совсем сейчас тогда уже ещё
этого этом этих этой этого такой такая такие таким таких вроде потому поэтому после перед
когда чтобы которое который которая которые которых своих тебя меня вами нами будет
будут было были была были есть здесь каждая каждый только хорошо вообще кстати потом
думаю кажется правда вроде точно даже если можно куда значит снова просто спасибо
""".split())


def topic_names(labels):
    spelling = {'minecraft':'Minecraft','terraria':'Terraria','arch':'Arch','linux':'Linux',
                'amd':'AMD','nvidia':'NVIDIA','steam':'Steam','ssd':'SSD'}
    technical = {'событие','личное предпочтение','благодарность','резкие слова','возражение'}
    return list(dict.fromkeys(spelling.get(t.casefold(),t) for t in labels if t not in technical))


def recurring_topics(rows):
    """Select repeated public words across speakers without keeping unselected conversation."""
    authors = {}
    candidates = {}
    for row in rows:
        body = row['text']
        if SECRET.search(body) or PRIVATE_FIELD.search(body) or '@' in body:
            continue
        words = set(re.findall(r'(?<!\w)[а-яё]{5,18}(?!\w)',body))-COMMON_WORDS
        candidates[row['id']] = words
        for word in words:
            authors.setdefault(word,set()).add(row['user_id'])
    return {ident: sorted((word for word in words if len(authors[word])>=2),
                           key=lambda word:(-len(authors[word]),word))[:3]
            for ident,words in candidates.items()}


class Archive:
    def __init__(self, db):
        self.db = db
        self.zone = ZoneInfo(db.settings.diary_timezone)

    def day(self, ts):
        return datetime.fromtimestamp(ts, self.zone).date().isoformat()

    async def query(self, function, *args):
        return await asyncio.to_thread(self._transaction, function, args)

    def _transaction(self, function, args):
        with self.db._mutex, closing(self.db._connect()) as c:
            c.execute('BEGIN IMMEDIATE')
            result = function(c, *args)
            c.commit()
            return result

    def install(self, c):
        c.executescript(SCHEMA)
        if c.execute("SELECT 1 FROM diary_meta WHERE key='backfill'").fetchone():
            return
        c.execute("""INSERT OR IGNORE INTO diary_jobs(chat_id,day)
            SELECT chat_id,diary_day(created_ts) FROM messages WHERE role='user'
            GROUP BY chat_id,diary_day(created_ts)""")
        c.execute("""INSERT OR IGNORE INTO memory_sources
            SELECT id,COALESCE(scope_chat_id,?),CASE WHEN scope_chat_id IS NULL THEN ? ELSE subject_user_id END,NULL,created_ts,'legacy'
            FROM memories""", (self.db.settings.owner_id, self.db.settings.owner_id))
        c.execute("INSERT INTO diary_meta VALUES ('backfill')")

    def enqueue(self, c, chat, ts):
        c.execute("""INSERT INTO diary_jobs(chat_id,day) VALUES (?,?)
            ON CONFLICT(chat_id,day) DO UPDATE SET dirty=1,retry=0,attempts=0,local_retry=0,local_attempts=0""", (chat,self.day(ts)))

        if chat<0:
            day = self.day(ts)
            c.execute("UPDATE chat_diary_entries SET status='partial' WHERE chat_id=? AND day=? AND status='ready'",(chat,day))
            c.execute("UPDATE daily_diary_entries SET status='partial' WHERE day=? AND status='ready'",(day,))

    def batch(self, c):
        job = c.execute("""SELECT * FROM diary_jobs WHERE dirty=1 AND local_retry<=?
            ORDER BY day,chat_id LIMIT 1""",(int(time.time()),)).fetchone()
        if not job:
            return False
        c.execute('SAVEPOINT diary_batch')
        try:
            self.consume(c,job)
        except (ValueError,sqlite3.Error) as exc:
            c.execute('ROLLBACK TO diary_batch')
            attempts = job['local_attempts']+1
            c.execute("""UPDATE diary_jobs SET local_attempts=?,local_retry=?,dirty=?
                WHERE chat_id=? AND day=?""",(attempts,int(time.time())+600*attempts,
                2 if attempts>=3 else 1,job['chat_id'],job['day']))
            logging.warning('Отложена обработка архива: %s',type(exc).__name__)
        finally:
            c.execute('RELEASE diary_batch')
        return True

    def consume(self,c,job):
        chat, day = job['chat_id'], job['day']
        lo, hi = bounds(day, self.zone)
        rows = c.execute("""SELECT * FROM messages WHERE chat_id=? AND created_ts>=? AND created_ts<?
            AND id>? AND role='user' ORDER BY id LIMIT 128""", (chat,lo,hi,job['cursor'])).fetchall()
        now = int(time.time())
        # A short overlap allows a later batch to identify a topic introduced just before it.
        overlap = c.execute("""SELECT * FROM messages WHERE chat_id=? AND created_ts>=? AND created_ts<?
            AND id<=? AND role='user' ORDER BY id DESC LIMIT 16""",
            (chat,lo,hi,job['cursor'])).fetchall() if rows and job['cursor'] else []
        labels = recurring_topics([row for row in [*overlap,*rows]
                                   if not self.db.guard.contains(row['text'])])
        for row in [*overlap,*rows]:
            if self.db.guard.contains(row['text']):
                continue
            topics, pref = evidence(row['text'])
            if row['id']<=job['cursor'] and (topics or pref):
                continue
            if not topics:
                topics = labels.get(row['id'],[])
            if not topics and not pref:
                continue
            c.execute('INSERT OR IGNORE INTO diary_sources VALUES (?,?,?,?,?,?,?,?,?)',
                      (chat,day,row['id'],row['telegram_message_id'],row['user_id'],row['created_ts'],
                       json.dumps(topics,ensure_ascii=False),pref,event_detail(row['text'])))
            milestone = event_detail(row['text'])
            if pref or (milestone and re.match(r'^(?:я|мы)\s',row['text'],re.I)):
                fact = (f'По словам участника, ему нравится {pref}.' if pref else
                        f'Участник сообщил: «{milestone}».')
                if (not c.execute('SELECT 1 FROM memories WHERE scope_chat_id=? AND subject_user_id=? AND content=?',
                                  (chat,row['user_id'],fact)).fetchone()
                        and c.execute('SELECT COUNT(*) FROM memories').fetchone()[0] < self.db.settings.max_memories):
                    mid = c.execute("""INSERT INTO memories(scope_chat_id,subject_user_id,content,pinned,created_ts)
                        VALUES(?,?,?,0,?)""", (chat,row['user_id'],fact,row['created_ts'])).lastrowid
                    c.execute("INSERT INTO memory_sources VALUES(?,?,?,?,?,'self-report')",
                              (mid,chat,row['user_id'],row['telegram_message_id'],row['created_ts']))
        more = len(rows)==128
        c.execute('UPDATE diary_jobs SET cursor=?,dirty=? WHERE chat_id=? AND day=?',
                  (rows[-1]['id'] if rows else job['cursor'],int(more),chat,day))
        if not more:
            self.materialize(c,chat,day,now)

    def materialize(self,c,chat,day,now):
        rows = c.execute("""SELECT s.*,u.display_name FROM diary_sources s LEFT JOIN chat_profiles u
            ON u.chat_id=s.chat_id AND u.user_id=s.uid WHERE s.chat_id=? AND s.day=? ORDER BY ts,raw_id""",(chat,day)).fetchall()
        if not rows:
            self.aggregate(c,day,now)
            return
        job = c.execute('SELECT lost FROM diary_jobs WHERE chat_id=? AND day=?',(chat,day)).fetchone()
        all_topics = list(dict.fromkeys(t for r in rows for t in json.loads(r['topics'])))
        topics = list(dict.fromkeys(topic for topic in all_topics[:12] + all_topics[-6:]
                                    if not self.db.guard.contains(topic)))
        excerpts = []
        seen_events = set()
        for r in (rows if len(rows)<=6 else rows[:3]+rows[-3:]):
            if r['detail']:
                key = r['detail'].casefold()
                if key in seen_events:
                    continue
                seen_events.add(key)
            labels = [label for label in json.loads(r['topics']) if not self.db.guard.contains(label)]
            if labels:
                stamp = datetime.fromtimestamp(r['ts'],self.zone).strftime('%H:%M')
                name = (f'{OWNER_NAME} (создатель)' if r['uid']==self.db.settings.owner_id
                        else safe_name(r['display_name']))
                line = (f"В {stamp} {name} пишет: «{self.db.guard.redact(r['detail'])}»" if r['detail'] else
                        f"В {stamp} в реплике от «{name}» встречаются темы: {', '.join(labels)}")
                if not r['detail']:
                    names = topic_names(labels)
                    if names:
                        line = f"В {stamp} {name} упоминает {', '.join(names)}"
                    elif 'благодарность' in labels:
                        line = f"В {stamp} от участника «{name}» пришли слова благодарности"
                    elif 'возражение' in labels:
                        line = f"В {stamp} {name} прямо выражает несогласие"
                    elif 'резкие слова' in labels:
                        line = f"В {stamp} в реплике от «{name}» прозвучали резкие слова"
                if r['preference']:
                    line = f"В {stamp} {name} пишет о своём предпочтении: «{self.db.guard.redact(r['preference'])}»"
                excerpts.append(f"{line} (№{r['mid']})")
        body = '. '.join(excerpts) or 'Сохранено высказывание о предпочтении.'
        old = c.execute('SELECT summary_id FROM diary_summary_index WHERE chat_id=? AND day=?',(chat,day)).fetchone()
        # Private summaries also use this index, but never contribute to the public-chat journal.
        if old and old[0]:
            summary = old[0]
            c.execute('UPDATE chat_summaries SET summary=?,from_ts=?,to_ts=? WHERE id=?',
                      (body,rows[0]['ts'],rows[-1]['ts'],summary))
        else:
            summary = c.execute('INSERT INTO chat_summaries(chat_id,from_ts,to_ts,summary,created_ts) VALUES(?,?,?,?,?)',
                                (chat,rows[0]['ts'],rows[-1]['ts'],body,now)).lastrowid
        c.execute('INSERT OR REPLACE INTO diary_summary_index VALUES(?,?,?)',(chat,day,summary))
        private = c.execute('SELECT chat_type FROM chats WHERE chat_id=?',(chat,)).fetchone()[0]=='private'
        if private or len(rows)<2:
            self.aggregate(c,day,now)
            return
        status = 'partial' if job[0] or day>=self.day(now) else 'ready'
        impression = ('Меня зацепила резкость некоторых реплик.' if 'резкие слова' in topics else
                      'Мне было приятно увидеть слова благодарности.' if 'благодарность' in topics else
                      'Мне запомнилось обсуждение темы «'+topics[0]+'».' if topics else '')
        c.execute("""INSERT INTO chat_diary_entries VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,day) DO UPDATE SET summary_id=excluded.summary_id,start=excluded.start,
            end=excluded.end,body=excluded.body,impression=excluded.impression,topics=excluded.topics,
            status=excluded.status,updated=excluded.updated""",
            (chat,day,summary,rows[0]['ts'],rows[-1]['ts'],body,impression,json.dumps(topics,ensure_ascii=False),
             status,now,now))
        self.aggregate(c,day,now)

    def aggregate(self,c,day,now):
        groups = c.execute("""SELECT e.*,c.title FROM chat_diary_entries e JOIN chats c USING(chat_id)
            WHERE day=? AND status IN ('ready','partial') AND c.chat_type!='private' ORDER BY start,chat_id""",(day,)).fetchall()
        if not groups:
            c.execute('DELETE FROM daily_diary_entries WHERE day=?',(day,))
            return
        warm = any('благодарность' in json.loads(g['topics']) for g in groups)
        tense = any('резкие слова' in json.loads(g['topics']) for g in groups)
        opening = ('День оставил у меня смешанное впечатление.' if warm and tense else
                   'Этот день вспоминается мне тёплым.' if warm else
                   'Я всё ещё вспоминаю один резкий разговор.' if tense else
                   'Хочу записать, что мне сегодня запомнилось.')
        pieces = [opening]
        for g in groups:
            topics = [topic for topic in json.loads(g['topics']) if not self.db.guard.contains(topic)]
            title = title_label(self.db.guard.redact(g['title']),g['chat_id'])
            if sum(x['title']==g['title'] for x in groups)>1:
                title += f" ({g['chat_id']})"
            names = [safe_name(r[0]) for r in c.execute("""SELECT u.display_name FROM diary_sources s
                JOIN chat_profiles u ON u.chat_id=s.chat_id AND u.user_id=s.uid WHERE s.chat_id=? AND day=?
                GROUP BY s.uid ORDER BY count(*) DESC,s.uid LIMIT 2""",(g['chat_id'],day))]
            highlight = c.execute("""SELECT detail FROM diary_sources WHERE chat_id=? AND day=?
                AND detail IS NOT NULL ORDER BY ts DESC LIMIT 1""",(g['chat_id'],day)).fetchone()
            public_topics = topic_names(topics)
            if highlight and self.db.guard.contains(highlight[0]):
                highlight = None
            if highlight and highlight[0].lower().startswith('провели турнир'):
                text = f"В чате «{title}» рассказывали, как прошёл турнир{highlight[0][len('провели турнир'):]}."
            elif highlight:
                text = f"Из чата «{title}» я запомнила сообщение «{highlight[0]}»."
            else:
                text = f"Я заглянула в записи чата «{title}»."
            extra = [t for t in public_topics if t.casefold() not in text.casefold()]
            if extra:
                text += ' В разговоре всплыли '+', '.join(extra)+'.'
            if names:
                text += ' Среди участников — '+' и '.join(names)+'.'
            pieces.append(text)
        if any('благодарность' in json.loads(g['topics']) for g in groups):
            pieces.append('Мне были приятны слова благодарности в переписке.')
        if tense:
            pieces.append('А резкие слова я бы обсудила с Создателем отдельно.')
        else:
            last_topics = topic_names([topic for topic in json.loads(groups[-1]['topics'])
                                      if not self.db.guard.contains(topic)])
            if last_topics:
                pieces.append('Про '+last_topics[-1]+' мне было бы интересно поговорить и с Создателем.')
        pending = c.execute("""SELECT 1 FROM diary_jobs j JOIN chats c USING(chat_id)
            WHERE day=? AND c.chat_type!='private' AND (dirty OR lost) LIMIT 1""",(day,)).fetchone()
        status = 'partial' if pending or day>=self.day(now) else 'ready'
        c.execute("""INSERT INTO daily_diary_entries VALUES(?,?,?,?,?,?,?)
          ON CONFLICT(day) DO UPDATE SET start=excluded.start,end=excluded.end,body=excluded.body,
          updated=excluded.updated,status=excluded.status""",
          (day,min(g['start'] for g in groups),max(g['end'] for g in groups),' '.join(pieces),status,now,now))

        c.execute('DELETE FROM daily_diary_sources WHERE day=?',(day,))
        c.executemany('INSERT INTO daily_diary_sources VALUES(?,?)',[(day,g['chat_id']) for g in groups])

    def prune_for_space(self,c):
        row = c.execute('SELECT chat_id,day FROM diary_jobs ORDER BY day,chat_id LIMIT 1').fetchone()
        if not row:
            return False
        chat,day = row
        c.execute('DELETE FROM chat_summaries WHERE id IN (SELECT summary_id FROM diary_summary_index WHERE chat_id=? AND day=?)',(chat,day))
        for table in ('chat_diary_entries','diary_sources','diary_jobs'):
            c.execute(f'DELETE FROM {table} WHERE chat_id=? AND day=?',(chat,day))
        self.aggregate(c,day,int(time.time()))
        logging.warning('Сокращён архив за один день из-за бюджета SQLite')
        return True

    def recent_summary(self,c,chat):
        # Legacy summaries rendered shared profile names. Use scoped evidence without those names.
        rows = c.execute("""SELECT topics FROM diary_sources WHERE chat_id=?
            ORDER BY ts DESC, raw_id DESC LIMIT 8""", (chat,)).fetchall()
        topics = list(dict.fromkeys(topic for row in rows for topic in json.loads(row['topics'])
                                    if not self.db.guard.contains(topic)))
        return 'Из сводки этого чата: ' + ', '.join(topics)[:400] if topics else None

    def erase(self,c,chat,user=None):
        if user is not None:
            affected = [r[0] for r in c.execute('SELECT DISTINCT day FROM diary_sources WHERE chat_id=? AND uid=?',(chat,user))]
            c.execute('DELETE FROM diary_sources WHERE chat_id=? AND uid=?',(chat,user))
            c.execute('DELETE FROM memories WHERE scope_chat_id=? AND (subject_user_id=? OR subject_user_id IS NULL)',(chat,user))
            c.execute("""DELETE FROM chat_summaries WHERE chat_id=? AND id NOT IN
                (SELECT summary_id FROM diary_summary_index WHERE summary_id IS NOT NULL)""",(chat,))
            # A generated reply may paraphrase deleted text without mentioning its author.
            c.execute("DELETE FROM messages WHERE chat_id=? AND role='assistant'",(chat,))
            for day in affected:
                c.execute("""DELETE FROM chat_summaries WHERE id IN (SELECT summary_id
                    FROM diary_summary_index WHERE chat_id=? AND day=?)""",(chat,day))
                c.execute('DELETE FROM chat_diary_entries WHERE chat_id=? AND day=?',(chat,day))
                self.materialize(c,chat,day,int(time.time()))
                self.aggregate(c,day,int(time.time()))
            return
        # A group-wide withdrawal invalidates all derivatives of this chat.
        days = [r[0] for r in c.execute('SELECT day FROM diary_jobs WHERE chat_id=?',(chat,))]
        c.execute('DELETE FROM chat_summaries WHERE chat_id=?',(chat,))
        c.execute('DELETE FROM chat_diary_entries WHERE chat_id=?',(chat,))
        c.execute('DELETE FROM diary_sources WHERE chat_id=?',(chat,))
        c.execute('DELETE FROM chat_profiles WHERE chat_id=?',(chat,))
        c.execute('DELETE FROM diary_jobs WHERE chat_id=?',(chat,))
        c.execute('DELETE FROM memories WHERE scope_chat_id=? OR id IN (SELECT memory_id FROM memory_sources WHERE chat_id=?)',(chat,chat))
        c.execute("DELETE FROM messages WHERE chat_id=? AND role='assistant'",(chat,))
        for day in days:
            self.aggregate(c,day,int(time.time()))

    def expire(self,c,cutoff):
        c.execute('DELETE FROM chat_summaries WHERE to_ts<?',(cutoff,))
        for table in ('chat_diary_entries','daily_diary_entries'):
            c.execute(f'DELETE FROM {table} WHERE end<?',(cutoff,))
        c.execute('DELETE FROM diary_sources WHERE ts<?',(cutoff,))
        c.execute('DELETE FROM diary_jobs WHERE day<?',(self.day(cutoff),))

    def requires_group_consent(self, c):
        return c.execute('SELECT 1 FROM diary_meta WHERE key=?',
                         (GROUP_CONSENT_MIGRATION,)).fetchone() is not None

    def history_enabled(self, c, chat):
        if c.execute('SELECT 1 FROM diary_disabled WHERE chat_id=?', (chat,)).fetchone():
            return False
        if chat >= 0 or not self.requires_group_consent(c):
            return True
        return c.execute('SELECT 1 FROM diary_enabled WHERE chat_id=?', (chat,)).fetchone() is not None

    def group_history(self,c,chat,enabled):
        if type(chat) is not int or not -(2**63) <= chat < 0:
            return
        if enabled:
            c.execute('DELETE FROM diary_disabled WHERE chat_id=?',(chat,))
            c.execute('INSERT OR IGNORE INTO diary_enabled VALUES(?)', (chat,))
        else:
            c.execute('DELETE FROM diary_enabled WHERE chat_id=?', (chat,))
            c.execute('INSERT OR IGNORE INTO diary_disabled VALUES(?)',(chat,))
            c.execute('DELETE FROM messages WHERE chat_id=?',(chat,))
            self.erase(c,chat)
            c.execute('DELETE FROM chats WHERE chat_id=?', (chat,))

    def finalize_days(self,c,now):
        today = self.day(now)
        days = [r[0] for r in c.execute("""SELECT DISTINCT e.day FROM chat_diary_entries e
            JOIN diary_jobs j USING(chat_id,day) WHERE e.status='partial' AND e.day<?
            AND j.dirty=0 AND j.lost=0""",(today,))]
        for day in days:
            c.execute("""UPDATE chat_diary_entries SET status='ready',updated=? WHERE day=?
                AND chat_id IN (SELECT chat_id FROM diary_jobs WHERE day=? AND dirty=0 AND lost=0)""",
                (now,day,day))
            self.aggregate(c,day,now)

    def next_review(self,c,now):
        return c.execute("""SELECT j.chat_id,j.day,j.cursor,e.summary_id,e.topics FROM diary_jobs j
            JOIN chat_diary_entries e USING(chat_id,day) WHERE j.dirty=0 AND j.attempts<3
            AND j.retry>=0 AND j.retry<=? AND j.day<? ORDER BY j.day,j.chat_id LIMIT 1""",
            (now,self.day(now))).fetchone()

    def finish_review(self,c,job,choice,now):
        current = self.next_row(c, job)
        if not current:
            return
        if choice is None:
            c.execute('UPDATE diary_jobs SET attempts=attempts+1,retry=? WHERE chat_id=? AND day=?',
                      (now+600*(2**current['attempts']),job['chat_id'],job['day']))
            return
        c.execute('UPDATE diary_jobs SET retry=-1 WHERE chat_id=? AND day=?',(job['chat_id'],job['day']))
        c.execute('UPDATE chat_diary_entries SET impression=?,updated=? WHERE chat_id=? AND day=?',
                  (choice,now,job['chat_id'],job['day']))
        self.aggregate(c,job['day'],now)

    def next_row(self,c,job):
        return c.execute("""SELECT j.* FROM diary_jobs j JOIN chat_diary_entries e USING(chat_id,day)
            WHERE j.chat_id=? AND j.day=? AND j.cursor=? AND j.dirty=0 AND e.summary_id=? AND j.retry>=0""",
            (job['chat_id'],job['day'],job['cursor'],job['summary_id'])).fetchone()

    async def maintain(self,ai=None,batches=8):
        for _ in range(batches):
            if not await self.query(self.batch):
                break
        now = int(time.time())
        await self.query(self.finalize_days,now)
        if ai is None or ai.busy:
            return
        job = await self.query(self.next_review,now)
        if job is None or ai.busy:
            return
        topics = [topic for topic in json.loads(job['topics']) if not self.db.guard.contains(topic)]
        options = ['Мне запомнилось, что обсуждали ' + t + '.' for t in topics[:8]]
        if not options:
            return
        # Model ranks verified alternatives. Untrusted generated prose is never persisted.
        try:
            answer = await ai.answer([
                {'role':'system','content':'Выбери одно впечатление Аяки о разговоре. Верни только номер, начиная с 1.'},
                {'role':'user','content':json.dumps(options,ensure_ascii=False)},
            ], summary=True, background=True)
            number = int(answer.strip())
            choice = options[number-1] if 1<=number<=len(options) else None
        except OllamaBusy:
            return
        except (ValueError,OllamaError):
            choice = None
        await self.query(self.finish_review,job,choice,int(time.time()))

    def allowed_turn(self,c,chat,user,mid):
        if not self.history_enabled(c, chat):
            return False
        return c.execute("""SELECT 1 FROM messages WHERE chat_id=? AND user_id=? AND telegram_message_id=?
            AND NOT EXISTS(SELECT 1 FROM opt_outs WHERE chat_id=? AND user_id=?)""",
            (chat,user,mid,chat,user)).fetchone() is not None

    def edit(self,c,mid,text,source_mid=None):
        if (not text or SECRET.search(text) or self.db.guard.contains(text)
                or not self.db.visible_memory(c,mid)):
            return False
        if source_mid is not None and c.execute("""SELECT 1 FROM memory_corrections
            WHERE memory_id=? AND editor=? AND mid=?""", (mid, self.db.settings.owner_id, source_mid)).fetchone():
            return True
        changed = c.execute('UPDATE memories SET content=? WHERE id=?',(text[:500],mid)).rowcount
        if changed:
            c.execute('INSERT OR REPLACE INTO memory_corrections VALUES(?,?,?,?)',(mid,self.db.settings.owner_id,source_mid,int(time.time())))
            c.execute("UPDATE memory_sources SET kind='owner-corrected' WHERE memory_id=?",(mid,))
        return bool(changed)
