"""Owner-private archive queries, with local calendar boundaries."""
import asyncio
from datetime import date, datetime, timedelta
import json
import re
import time

from .archive import DETAILS, bounds, evidence, event_detail, safe_name, title_label, topic_names

WORDS = {'один':1,'два':2,'три':3,'четыре':4,'пять':5,'шесть':6,'семь':7}
REQUEST = re.compile(r'дневник|как прош[её]л (?:твой )?день|что (?:тебе |интересного )?(?:сегодня |вчера )?(?:запомнилось|произошло|происходило|обсуждали|было)|что вчера происходило|кто .*спорил|кто .*обижал|кто-нибудь обижал|что происходило|впечатлени[ея]|что тебе .*понравилось|расскажи.*(?:чате|групп)|что интересного')
PERIOD = re.compile(r'за (?:последние |последних )?(\d{1,3}|один|два|три|четыре|пять|шесть|семь) (?:дня|дней|день)')

def period(text,today):
    match = PERIOD.search(text)
    if match:
        key = match[1]
        count = int(key) if key.isdecimal() else WORDS[key]
        return today-timedelta(days=max(0,min(179,count-1))),today
    if 'вчера' in text:
        day = today-timedelta(days=1)
        return day,day
    iso = re.search(r'\b\d{4}-\d{2}-\d{2}\b',text)
    if iso:
        day = date.fromisoformat(iso[0])
        return day,day
    if 'недел' in text:
        return today-timedelta(days=today.weekday() if 'эту недел' in text else 6),today
    return today,today


def event_terms(text):
    # Avoid interpreting a group name as the subject of a question.
    subject = re.split(r'\bв (?:чате|группе)\s+',text,maxsplit=1)[0]
    words = [m.group() for m in DETAILS.finditer(subject)]
    match = re.search(r'\b(?:про|об|о|насчёт)\s+(.+)',subject)
    if match and not any(w in match[1] for w in ('разговор','дневник','впечатлен')):
        tail = re.split(r'\b(?:за|вчера|сегодня|в чате|на этой)\b',match[1],maxsplit=1)[0]
        words.extend(re.findall(r'[а-яёa-z]{4,}',tail,re.I))
    words.extend(re.findall(r'\b(?:турнир|встреч|концерт|выставк)\w*',subject))
    return {w[:max(4,len(w)-2)] if re.fullmatch(r'[а-яё]+',w) else w for w in words}


def requested_chat(text):
    match = re.search(r'(?:в чат[еау]?|в групп[еуы]|из чата|в)\s+(.+)', text)
    if not match or 'остальн' in match[1] or 'наших групп' in match[1]:
        return None
    title = re.split(r'\s+(?:за |вчера|сегодня|кто |что )',match[1])[0]
    return title.strip(' «»"?.!') or None


def resolve(groups,title):
    title = title.casefold()
    exact = [g for g in groups if str(g['chat_id'])==title or g['title'].casefold()==title]
    if exact:
        return exact
    parts = re.findall(r'\w+',title)
    return [g for g in groups if parts and all(
        any(w.startswith(p[:max(3,len(p)-3)]) for w in re.findall(r'\w+',g['title'].casefold()))
        for p in parts)]


class DiaryReader:
    def __init__(self, archive):
        self.archive = archive
        self.pending = None
        self.last_chat = None
        self.lock = asyncio.Lock()

    def matches(self, text):
        q = text.casefold().strip()
        if self.pending and time.monotonic() < self.pending[0] and q in self.pending[2]:
            return True
        return bool(REQUEST.search(q) or re.search(
            r'\bкто\b.*(?:обсуждал|говорил|писал|выиграл|пров[её]л|упом[ия]н)', q,
        ) or ('вчера' in q and 'что' in q and ' в ' in q))

    async def prepare(self, text, *, created_at):
        # Clarification remains available if this attempt is explicitly rejected.
        async with self.lock:
            saved = self.pending, self.last_chat
            try:
                day = created_at.astimezone(self.archive.zone).date()
                answer = await self._answer(text, today=day)
                return answer, (self.pending, self.last_chat)
            finally:
                self.pending, self.last_chat = saved

    def confirm(self, context):
        self.pending, self.last_chat = context

    async def answer(self,text):
        async with self.lock:
            return await self._answer(text)

    async def _answer(self,text, *, today=None):
        q = text.casefold().strip()
        chosen = None
        if self.pending and time.monotonic() < self.pending[0]:
            _, prior, ids = self.pending
            if q in ids:
                chosen,q = int(q),prior
        if chosen is None and not self.matches(q):
            return None
        self.pending = None
        if today is None:
            today = datetime.now(self.archive.zone).date()
        try:
            first,last = period(q,today)
        except ValueError:
            return 'Уточни дату в формате ГГГГ-ММ-ДД.'
        groups = await self.archive.query(lambda c:list(c.execute("""SELECT chat_id,title FROM chats
            WHERE chat_type!='private' AND chat_id NOT IN (SELECT chat_id FROM diary_disabled)""")))
        explicit = re.search(r'(?<![\w-])-\d{1,19}\b',q)
        title = explicit[0] if explicit else requested_chat(q)
        referring_back = any(word in q for word in ('в этом чате','в той группе','в том чате','что там','об этом разговоре'))
        if chosen is None and referring_back:
            if self.last_chat is None:
                return 'Уточни название группы или её Telegram ID.'
            chosen = self.last_chat
        if chosen is None and title:
            matches = resolve(groups,title)
            if not matches:
                return 'Такой группы в доступной истории я не нашла.'
            if len(matches)>1:
                self.pending = (time.monotonic()+300,q,{str(g['chat_id']) for g in matches})
                return 'Уточни группу, пришли её ID:\n'+'\n'.join(
                    f"{title_label(g['title'])} — {g['chat_id']}" for g in matches[:20])
            chosen = matches[0]['chat_id']
        exclude = self.last_chat if 'остальн' in q else None
        if chosen is not None:
            self.last_chat = chosen
        return await self.archive.query(self.read,q,chosen,exclude,first,last)

    def read(self,c,q,chat,exclude,first,last):
        if chat is not None and not c.execute("""SELECT 1 FROM chats WHERE chat_id=? AND chat_type!='private'
                AND NOT EXISTS(SELECT 1 FROM diary_disabled WHERE chat_id=?)""", (chat,chat)).fetchone():
            return 'Такой группы в доступной истории я не нашла.'
        redact = self.archive.db.guard.redact
        duplicates = {r[0] for r in c.execute("""SELECT title FROM chats WHERE chat_type!='private'
            AND chat_id NOT IN (SELECT chat_id FROM diary_disabled) GROUP BY title HAVING count(*)>1""")}
        def label(row):
            name = title_label(redact(row['title']))
            return f"{name} ({row['chat_id']})" if row['title'] in duplicates else name

        lo = bounds(first.isoformat(),self.archive.zone)[0]
        hi = bounds(last.isoformat(),self.archive.zone)[1]
        dispute = 'спор' in q or 'обиж' in q
        target = event_terms(q)
        feelings = any(word in q for word in ('понравил','впечатлен','запомнил'))
        if feelings and not dispute and not target:
            entries = list(c.execute("""SELECT e.chat_id,e.impression,e.status,c.title FROM chat_diary_entries e
                JOIN chats c USING(chat_id) WHERE day BETWEEN ? AND ? AND (? IS NULL OR e.chat_id=?)
                AND (? IS NULL OR e.chat_id!=?) ORDER BY day DESC,start DESC LIMIT 6""",
                (str(first),str(last),chat,chat,exclude,exclude)))
            if entries:
                return '\n'.join(f"Про «{label(e)}»: {redact(e['impression'])}" for e in entries)
            return 'За этот период я ещё не записала отдельного впечатления.'
        if chat is None and not exclude and not dispute and not target:
            daily = list(c.execute('SELECT * FROM daily_diary_entries WHERE day BETWEEN ? AND ? ORDER BY day',
                                   (str(first),str(last))))
            dirty = c.execute("""SELECT 1 FROM diary_jobs j JOIN chats c USING(chat_id) WHERE day BETWEEN ? AND ?
                AND c.chat_type!='private' AND (dirty OR lost) LIMIT 1""",(str(first),str(last))).fetchone()
            if daily and not dirty:
                if 'покажи' in q:
                    return '\n\n'.join(r['day']+' — '+redact(r['body']) for r in daily)[:3600]
                entries = c.execute("""SELECT e.chat_id,e.day,c.title,e.topics,e.impression,
                    (SELECT s.detail FROM diary_sources s WHERE s.chat_id=e.chat_id AND s.day=e.day
                     AND s.detail IS NOT NULL ORDER BY s.ts DESC LIMIT 1) AS highlight FROM daily_diary_sources d
                    JOIN chat_diary_entries e USING(day,chat_id) JOIN chats c USING(chat_id)
                    WHERE day BETWEEN ? AND ? ORDER BY day,e.start LIMIT 10""",(str(first),str(last))).fetchall()
                lines = []
                for e in entries:
                    topics = topic_names([topic for topic in json.loads(e['topics']) if not self.archive.db.guard.contains(topic)])
                    if e['highlight']:
                        lines.append(f"Из чата «{label(e)}» за {e['day']} мне запомнилась реплика «{redact(e['highlight'])}».")
                    elif topics:
                        lines.append(f"{e['day']} в чате «{label(e)}» говорили про "+', '.join(topics[:5])+'.')
                    else:
                        lines.append(f"О чате «{label(e)}» у меня сохранилось впечатление за {e['day']}.")
                if lines:
                    lines.append(redact(entries[-1]['impression']))
                    if any(r['status']=='partial' for r in daily):
                        lines.append('Записи за этот период пока неполные.')
                    return '\n'.join(lines)[:3800]
        if chat is not None and not dispute and not target:
            pending = c.execute('SELECT 1 FROM diary_jobs WHERE chat_id=? AND day BETWEEN ? AND ? AND dirty LIMIT 1',
                                (chat,str(first),str(last))).fetchone()
            stored = list(c.execute("""SELECT i.chat_id,i.day,s.summary,e.impression,e.status,c.title,j.lost
                FROM diary_summary_index i JOIN chat_summaries s ON s.id=i.summary_id
                JOIN chats c ON c.chat_id=i.chat_id JOIN diary_jobs j ON j.chat_id=i.chat_id AND j.day=i.day
                LEFT JOIN chat_diary_entries e ON e.chat_id=i.chat_id AND e.day=i.day
                WHERE i.chat_id=? AND i.day BETWEEN ? AND ? ORDER BY i.day LIMIT 15""",(chat,str(first),str(last))))
            if stored and not pending:
                parts = [f"{r['day']} в группе «{label(r)}». {redact(r['summary'])}" for r in stored]
                if str(chat).startswith('-100'):
                    refs = list(c.execute("""SELECT s.mid FROM diary_sources s JOIN messages m ON m.id=s.raw_id
                        WHERE s.chat_id=? AND s.ts>=? AND s.ts<? AND s.mid>0 ORDER BY s.ts DESC LIMIT 3""",
                        (chat,lo,hi)))
                    if refs:
                        parts.append('Сообщения: '+' '.join(f"https://t.me/c/{str(chat)[4:]}/{r[0]}" for r in refs))
                if stored[-1]['impression']:
                    parts.append(redact(stored[-1]['impression']))
                if any(r['status']=='partial' or r['lost'] for r in stored):
                    parts.append('Я вижу только часть переписки.')
                return '\n\n'.join(parts)[:3800]
        # Union raw and archived references by message identity. A deleted raw row need not be present.
        conditions = """c.chat_type!='private' AND c.chat_id NOT IN (SELECT chat_id FROM diary_disabled)
                        AND (? IS NULL OR c.chat_id=?) AND (? IS NULL OR c.chat_id!=?)"""
        args = (chat,chat,exclude,exclude,lo,hi)
        sql = f"""SELECT s.chat_id,s.mid,s.ts,s.topics,NULL AS text,c.title,u.display_name,s.detail,
            EXISTS(SELECT 1 FROM messages m WHERE m.id=s.raw_id) AS available
            FROM diary_sources s JOIN chats c USING(chat_id) LEFT JOIN chat_profiles u ON u.chat_id=s.chat_id AND u.user_id=s.uid
            WHERE {conditions} AND s.ts>=? AND s.ts<?
            UNION ALL SELECT m.chat_id,m.telegram_message_id,m.created_ts,NULL,m.text,c.title,u.display_name,NULL,1
            FROM messages m JOIN chats c USING(chat_id) LEFT JOIN chat_profiles u ON u.chat_id=m.chat_id AND u.user_id=m.user_id
            WHERE {conditions} AND m.created_ts>=? AND m.created_ts<? AND m.role='user'
            AND NOT EXISTS(SELECT 1 FROM diary_sources s WHERE s.chat_id=m.chat_id AND s.raw_id=m.id)
            ORDER BY 3"""
        lines = []
        truncated = False
        for row in c.execute(sql,args+args):
            if row['text'] and self.archive.db.guard.contains(row['text']):
                continue
            labels = json.loads(row['topics']) if row['topics'] else evidence(row['text'])[0]
            labels = [label for label in labels if not self.archive.db.guard.contains(label)]
            detail = (redact(row['detail']) if row['detail'] else None) or (event_detail(row['text']) if row['text'] else None)
            haystack = (' '.join(labels)+' '+(detail or '')).casefold()
            if not labels or any(term not in haystack for term in target):
                continue
            if 'обиж' in q and 'резкие слова' not in labels:
                continue
            if 'спор' in q and 'возражение' not in labels:
                continue
            if len(lines)==8:
                truncated=True
                break
            stamp = datetime.fromtimestamp(row['ts'],self.archive.zone).strftime('%d.%m %H:%M')
            ref = f"сообщение №{row['mid']}"
            if row['available'] and str(row['chat_id']).startswith('-100') and row['mid']:
                ref = f"https://t.me/c/{str(row['chat_id'])[4:]}/{row['mid']}"
            what = 'сообщение «'+detail+'»' if detail else 'упоминание: '+', '.join(labels)
            lines.append(f"{stamp} в «{label(row)}» {safe_name(redact(row['display_name'] or ''))} — {what} ({ref}).")
        if not lines:
            return 'За этот период у меня недостаточно подтверждённых записей, чтобы ответить.'
        diary = c.execute("""SELECT impression FROM chat_diary_entries WHERE day BETWEEN ? AND ?
            AND (? IS NULL OR chat_id=?) AND (? IS NULL OR chat_id!=?) ORDER BY day DESC LIMIT 1""",
            (str(first),str(last),chat,chat,exclude,exclude)).fetchone()
        result = 'Вот что сохранилось в переписке.\n'+'\n'.join(lines)
        if dispute:
            result += ' По таким репликам я не могу установить мотивы участников или адресата резких слов.'
        if diary and not dispute and not target:
            result += '\n\n'+redact(diary[0])
        if truncated or c.execute("""SELECT 1 FROM diary_jobs WHERE day BETWEEN ? AND ? AND lost>0
            AND (? IS NULL OR chat_id=?) LIMIT 1""",(str(first),str(last),chat,chat)).fetchone():
            result += ' Это только часть истории.'
        return result[:3800]
