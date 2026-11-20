"""SQLite-хранилище Аяки: изоляция чатов, память, квоты, очистка.

Все операции с SQLite выполняются в отдельных потоках, чтобы не блокировать
Telegram-обработчики. Параллельные операции над одной базой сериализованы.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
from math import ceil, isfinite
from pathlib import Path
import logging
import os
import re
import sqlite3
import tempfile
import threading
import time
from uuid import uuid4

from .config import Settings
from .archive import Archive, SECRET
from .group import ReplyKind
from .security import SecretGuard, valid_id

LOG = logging.getLogger(__name__)
DAY = 86400
RESPONSE_RETENTION = 2 * DAY


@dataclass(frozen=True)
class PendingReply:
    message_id: int
    kind: ReplyKind | None


@dataclass(frozen=True)
class TelegramResponse:
    chat_id: int
    message_id: int
    created_ts: int


@dataclass(frozen=True)
class CleanupReport:
    deleted_raw: int
    deleted_summaries: int
    deleted_over_quota: int
    bytes_total: int


class StorageUnavailable(RuntimeError):
    pass


class DeliveryRetryDelay(RuntimeError):
    def __init__(self, seconds: int) -> None:
        super().__init__('Ожидание после отказа Telegram')
        self.retry_after = seconds


class Storage:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.db_path
        self._rejection_directory = self.path.parent / '.ayaka-delivery' / self.path.name
        self._mutex = threading.RLock()
        self.guard = SecretGuard(settings)
        self.archive = Archive(self)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.create_function('diary_day', 1, self.archive.day)
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA secure_delete=ON')
        conn.execute('PRAGMA busy_timeout=5000')
        conn.execute('PRAGMA journal_size_limit=1048576')
        # max_page_count applies to a connection, so set it for writes as well.
        page_size = conn.execute('PRAGMA page_size').fetchone()[0]
        max_pages = self.settings.db_max_mb * 1024 * 1024 // page_size
        conn.execute(f'PRAGMA max_page_count={max_pages}')
        return conn

    def _checkpoint(self, bot_id: int, offset: int | None = None) -> int | None:
        try:
            with self._mutex, closing(self._connect()) as conn:
                if offset is not None:
                    conn.execute("""INSERT INTO telegram_checkpoints VALUES (?,?,?)
                        ON CONFLICT(bot_id) DO UPDATE SET next_offset=excluded.next_offset,
                        updated_ts=excluded.updated_ts""", (bot_id, offset, int(time.time())))
                    conn.commit()
                    return offset
                # Telegram can choose a new sequence after a week without updates.
                row = conn.execute('SELECT next_offset FROM telegram_checkpoints WHERE bot_id=? AND updated_ts>=?',
                                   (bot_id, int(time.time()) - 6 * DAY)).fetchone()
                return row[0] if row else None
        except sqlite3.Error:
            raise StorageUnavailable('Позиция опроса временно недоступна') from None

    async def polling_offset(self, bot_id: int) -> int | None:
        return await asyncio.to_thread(self._checkpoint, bot_id)

    async def save_polling_offset(self, bot_id: int, offset: int) -> None:
        await asyncio.to_thread(self._checkpoint, bot_id, offset)

    def _quick_check(self) -> bool:
        with self._mutex, closing(self._connect()) as conn:
            return [row[0] for row in conn.execute('PRAGMA quick_check')] == ['ok']

    async def quick_check(self) -> bool:
        return await asyncio.to_thread(self._quick_check)

    async def initialize(self) -> None:
        await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> None:
        with self._mutex:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists()
            with closing(self._connect()) as conn:
                # Автовакуум требует настроить БД до создания таблиц.
                if is_new:
                    conn.execute('PRAGMA auto_vacuum=INCREMENTAL')
                    conn.execute('VACUUM')
                conn.execute('PRAGMA journal_mode=WAL')
                conn.execute('PRAGMA wal_autocheckpoint=100')
                conn.execute('PRAGMA synchronous=NORMAL')
                conn.executescript('''
                    CREATE TABLE IF NOT EXISTS telegram_checkpoints (
                        bot_id INTEGER PRIMARY KEY,
                        next_offset INTEGER NOT NULL,
                        updated_ts INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS chats (
                        chat_id INTEGER PRIMARY KEY,
                        chat_type TEXT NOT NULL CHECK(chat_type IN ('private','group','supergroup')),
                        title TEXT NOT NULL,
                        last_seen_ts INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS users (
                        user_id INTEGER PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        username TEXT,
                        preferred_name TEXT,
                        last_seen_ts INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS chat_profiles (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        display_name TEXT NOT NULL,
                        preferred_name TEXT,
                        last_seen_ts INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, user_id)
                    );
                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL REFERENCES chats(chat_id),
                        user_id INTEGER,
                        telegram_message_id INTEGER,
                        role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                        text TEXT NOT NULL,
                        created_ts INTEGER NOT NULL,
                        UNIQUE(chat_id, telegram_message_id)
                    );
                    CREATE TABLE IF NOT EXISTS pending_replies (
                        message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                        kind TEXT CHECK(kind IN ('direct','initiative','boundary')),
                        sending INTEGER NOT NULL DEFAULT 0 CHECK(sending IN (0,1)),
                        delivery_attempt TEXT,
                        retry_at REAL NOT NULL DEFAULT 0,
                        CHECK(sending=0 OR kind IS NOT NULL)
                    );
                    CREATE TABLE IF NOT EXISTS telegram_responses (
                        chat_id INTEGER NOT NULL,
                        telegram_message_id INTEGER NOT NULL,
                        created_ts INTEGER NOT NULL,
                        state INTEGER NOT NULL DEFAULT 0 CHECK(state IN (0,1,2,3)),
                        is_history_turn INTEGER NOT NULL DEFAULT 0 CHECK(is_history_turn IN (0,1)),
                        delivery_attempt TEXT,
                        retry_at REAL NOT NULL DEFAULT 0,
                        PRIMARY KEY(chat_id,telegram_message_id)
                    ) WITHOUT ROWID;
                    CREATE INDEX IF NOT EXISTS idx_telegram_responses_expiry ON telegram_responses(created_ts);
                    CREATE INDEX IF NOT EXISTS idx_messages_chat_time
                        ON messages(chat_id, created_ts DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(created_ts);
                    CREATE INDEX IF NOT EXISTS idx_messages_user_chat ON messages(user_id,chat_id);
                    CREATE TABLE IF NOT EXISTS chat_summaries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_id INTEGER NOT NULL REFERENCES chats(chat_id),
                        from_ts INTEGER NOT NULL,
                        to_ts INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        created_ts INTEGER NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_summaries_chat_time
                        ON chat_summaries(chat_id, to_ts DESC);
                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope_chat_id INTEGER REFERENCES chats(chat_id),
                        subject_user_id INTEGER,
                        content TEXT NOT NULL,
                        pinned INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0,1)),
                        created_ts INTEGER NOT NULL,
                        expires_ts INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope_chat_id);
                    CREATE TABLE IF NOT EXISTS opt_outs (
                        chat_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        created_ts INTEGER NOT NULL,
                        PRIMARY KEY(chat_id, user_id)
                    );
                ''')
                self._migrate_delivery(conn)
                self.archive.install(conn)
                conn.commit()

    @staticmethod
    def _migrate_delivery(conn: sqlite3.Connection) -> None:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            fields = {row['name'] for row in conn.execute('PRAGMA table_info(pending_replies)')}
            if 'delivery_attempt' not in fields:
                conn.execute('ALTER TABLE pending_replies ADD COLUMN delivery_attempt TEXT')
            if 'retry_at' not in fields:
                conn.execute('ALTER TABLE pending_replies ADD COLUMN retry_at REAL NOT NULL DEFAULT 0')

    async def save_message(self, *, chat_id: int, chat_type: str, chat_title: str,
                           user_id: int, display_name: str, username: str | None,
                           telegram_message_id: int, text: str,
                           created_ts: int | None = None) -> bool:
        saved, _ = await asyncio.to_thread(
            self._save_message_sync, chat_id, chat_type, chat_title, user_id,
            display_name, telegram_message_id, text, created_ts, False,
        )
        return saved

    async def receive_message(self, *, chat_id: int, chat_type: str, chat_title: str,
                              user_id: int, display_name: str, telegram_message_id: int,
                              text: str, created_ts: int) -> PendingReply | None:
        """Atomically save an incoming message and return its unfinished reply, if any."""
        _, reply = await asyncio.to_thread(
            self._save_message_sync, chat_id, chat_type, chat_title, user_id,
            display_name, telegram_message_id, text, created_ts, True,
        )
        return reply

    def _save_message_sync(self, chat_id, chat_type, chat_title, user_id,
                           display_name, telegram_message_id, text, created_ts, track_reply):
        if (not valid_id(chat_id) or not valid_id(user_id, positive=True)
                or not valid_id(telegram_message_id, positive=True)):
            return False, None
        if chat_type == 'private':
            if chat_id != self.settings.owner_id or user_id != chat_id:
                return False, None
        elif chat_type not in ('group', 'supergroup') or chat_id >= 0:
            return False, None
        if self.guard.contains(text):
            return False, None
        clean = text.strip()[:self.settings.max_text_chars]
        if not clean:
            return False, None
        ts = int(time.time()) if created_ts is None else int(created_ts)
        with self._mutex, closing(self._connect()) as conn:
            try:
                conn.execute('BEGIN IMMEDIATE')
                if not self.archive.history_enabled(conn, chat_id):
                    return False, None
                if conn.execute('SELECT 1 FROM opt_outs WHERE chat_id=? AND user_id=?',
                                (chat_id, user_id)).fetchone():
                    return False, None
                if conn.execute('SELECT 1 FROM messages WHERE chat_id=? AND telegram_message_id=?',
                                (chat_id, telegram_message_id)).fetchone():
                    reply, recovery = self._pending_reply(
                        conn, chat_id, user_id, telegram_message_id, clean,
                    ) if track_reply else (None, None)
                    conn.commit()
                    if recovery is not None:
                        self._remove_rejection(recovery)
                    return False, reply
                if track_reply and conn.execute("""SELECT 1 FROM telegram_responses
                    WHERE chat_id=? AND telegram_message_id=?""", (chat_id, telegram_message_id)).fetchone():
                    # The source message was removed; never recreate a previously attempted turn.
                    return False, None
                conn.execute('''INSERT INTO chats(chat_id,chat_type,title,last_seen_ts)
                    VALUES (?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET
                    chat_type=excluded.chat_type,title=excluded.title,
                    last_seen_ts=excluded.last_seen_ts WHERE excluded.last_seen_ts>=chats.last_seen_ts''',
                    (chat_id, chat_type, self.guard.redact(chat_title)[:150], ts))
                conn.execute('''INSERT INTO chat_profiles(chat_id,user_id,display_name,last_seen_ts)
                    VALUES (?,?,?,?) ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    display_name=excluded.display_name,last_seen_ts=excluded.last_seen_ts
                    WHERE excluded.last_seen_ts>=chat_profiles.last_seen_ts''',
                    (chat_id, user_id, self.guard.redact(display_name)[:120], ts))
                cursor = conn.execute('''INSERT INTO messages
                    (chat_id,user_id,telegram_message_id,role,text,created_ts)
                    VALUES (?,?,?,'user',?,?)''',
                    (chat_id, user_id, telegram_message_id, clean, ts))
                self.archive.enqueue(conn, chat_id, ts)
                reply = None
                if track_reply:
                    kind = ReplyKind.DIRECT if chat_type == 'private' else None
                    reply = PendingReply(cursor.lastrowid, kind)
                    conn.execute('INSERT INTO pending_replies(message_id,kind) VALUES (?,?)',
                                 (reply.message_id, kind.value if kind is not None else None))
                conn.commit()
                return True, reply
            except sqlite3.Error as exc:
                conn.rollback()
                raise StorageUnavailable('Запись сообщения отложена') from None

    def _pending_reply(self, conn, chat_id, user_id, telegram_message_id, text):
        row = conn.execute("""SELECT p.message_id, p.kind, p.sending, p.delivery_attempt, p.retry_at
            FROM pending_replies p JOIN messages m ON m.id=p.message_id
            WHERE m.chat_id=? AND m.user_id=? AND m.telegram_message_id=?
              AND m.role='user' AND m.text=?""",
            (chat_id, user_id, telegram_message_id, text)).fetchone()
        if row is None:
            return None, None

        ready, recovery = self._recover_delivery(conn, row, 'pending_replies', 'sending',
                                                 'message_id=?', (row['message_id'],))
        if not ready:
            return None, None
        kind = ReplyKind(row['kind']) if row['kind'] is not None else None
        return PendingReply(row['message_id'], kind), recovery

    async def response_ready(self, response: TelegramResponse, *, reserve: bool = False) -> bool | None:
        """None means no receipt yet. False means stale, closed, or delivery is uncertain."""
        return await asyncio.to_thread(self._response_ready_sync, response, reserve)

    def _response_ready_sync(self, response, reserve):
        if (response.created_ts <= time.time() - RESPONSE_RETENTION
                or not valid_id(response.chat_id) or not valid_id(response.message_id, positive=True)
                or response.chat_id > 0 and response.chat_id != self.settings.owner_id):
            return False
        table, state, key, params = self._delivery_key(response)
        with self._mutex, closing(self._connect()) as conn, conn:
            if not reserve and conn.execute(f'SELECT 1 FROM {table} WHERE {key} AND is_history_turn=0', params).fetchone() is None:
                return None
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute(f'SELECT {state}, delivery_attempt, retry_at, is_history_turn FROM {table} WHERE {key}', params).fetchone()
            if row is None:
                if not reserve:
                    return None
                conn.execute("""INSERT INTO telegram_responses(chat_id,telegram_message_id,created_ts)
                    VALUES (?,?,?)""", (*params, response.created_ts))
                return True
            if row['is_history_turn']:
                return False if reserve else None
            ready, recovery = self._recover_delivery(conn, row, table, state, key, params)
            conn.commit()
            if recovery is not None:
                self._remove_rejection(recovery)
            return ready

    @staticmethod
    def _delivery_key(reply: PendingReply | TelegramResponse):
        # Only fixed identifiers enter the SQL. Telegram IDs are bound parameters.
        if isinstance(reply, TelegramResponse):
            return 'telegram_responses', 'state', 'chat_id=? AND telegram_message_id=?', (reply.chat_id, reply.message_id)
        return 'pending_replies', 'sending', 'message_id=?', (reply.message_id,)

    # State 1 covers both active and uncertain sends. Only attempt-specific rejection proof resets it.
    def _recover_delivery(self, conn, row, table, state, key, params):
        retry_at = row['retry_at']
        recovery = None
        if row[state] >= 2:
            return False, None
        if row[state] == 1:
            attempt = row['delivery_attempt']
            if not attempt or not re.fullmatch(r'[0-9a-f]{32}', attempt):
                return False, None
            recovery = self._rejection_directory / (attempt + '.rejected')
            retry_at = self._read_rejection(recovery)
            if retry_at is None:
                return False, None

        remaining = ceil(retry_at - time.time())
        if remaining > 0:
            raise DeliveryRetryDelay(remaining)
        if recovery is not None:
            conn.execute(f"""UPDATE {table} SET {state}=0, delivery_attempt=NULL, retry_at=?
                WHERE {key} AND {state}=1 AND delivery_attempt=?""", (retry_at, *params, attempt))
        return True, recovery

    @staticmethod
    def _read_rejection(path: Path) -> float | None:
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
                value = float(stream.read(64))
        except (FileNotFoundError, ValueError):
            return None
        except OSError:
            raise StorageUnavailable('Не удалось проверить отказ Telegram') from None
        return value if isfinite(value) and value > 0 else None

    @staticmethod
    def _remove_rejection(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOG.warning('Не удалось удалить обработанный отказ Telegram')

    async def remember_rejection(self, attempt: str, retry_at: float) -> None:
        await asyncio.to_thread(self._remember_rejection_sync, attempt, retry_at)

    def _remember_rejection_sync(self, attempt: str, retry_at: float) -> None:
        with self._mutex:
            directory = self._rejection_directory
            directory.parent.mkdir(mode=0o700, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.rejected-', dir=directory)
            path = Path(temporary)
            try:
                with os.fdopen(fd, 'w', encoding='ascii') as stream:
                    stream.write(str(retry_at))
                    stream.flush()
                    os.fsync(stream.fileno())
                path.replace(directory / (attempt + '.rejected'))
                dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                path.unlink(missing_ok=True)

    def _update_reply(self, sql, params):
        with self._mutex, closing(self._connect()) as conn, conn:
            return conn.execute(sql, params).rowcount == 1

    async def choose_reply(self, reply: PendingReply, kind: ReplyKind) -> bool:
        return await asyncio.to_thread(
            self._update_reply, 'UPDATE pending_replies SET kind=? WHERE message_id=? AND kind IS NULL',
            (kind.value, reply.message_id),
        )

    async def begin_delivery(self, reply: PendingReply | TelegramResponse) -> str | None:
        """Commit a unique send attempt before contacting Telegram."""
        return await asyncio.to_thread(self._begin_delivery_sync, reply)

    def _begin_delivery_sync(self, reply):
        table, state, key, params = self._delivery_key(reply)
        history_turn = isinstance(reply, PendingReply)
        eligible = 'AND kind IS NOT NULL' if history_turn else 'AND is_history_turn=0'
        attempt = uuid4().hex
        with self._mutex, closing(self._connect()) as conn, conn:
            started = conn.execute(
                f"""UPDATE {table} SET {state}=1, delivery_attempt=?, retry_at=0
                    WHERE {key} AND {state}=0 AND retry_at<=? {eligible}""",
                (attempt, *params, time.time()),
            ).rowcount == 1
            if not started:
                return None
            if history_turn:
                # Keep a text-free replay guard if retention or erasure removes the source row.
                conn.execute("""INSERT INTO telegram_responses
                    (chat_id,telegram_message_id,created_ts,state,is_history_turn)
                    SELECT chat_id,telegram_message_id,created_ts,2,1 FROM messages WHERE id=?
                    ON CONFLICT(chat_id,telegram_message_id) DO NOTHING""", (reply.message_id,))
            return attempt

    async def retry_delivery(self, reply: PendingReply | TelegramResponse, attempt: str, retry_at: float) -> None:
        """Release the same send attempt only after a definitive rejection."""
        table, state, key, params = self._delivery_key(reply)
        await asyncio.to_thread(
            self._update_reply,
            f"""UPDATE {table} SET {state}=0, delivery_attempt=NULL, retry_at=?
                WHERE {key} AND {state}=1 AND delivery_attempt=?""",
            (retry_at, *params, attempt),
        )
        await asyncio.to_thread(self._remove_rejection, self._rejection_directory / (attempt + '.rejected'))

    async def discard_reply(self, reply: PendingReply) -> bool:
        return await asyncio.to_thread(
            self._update_reply, 'DELETE FROM pending_replies WHERE message_id=? AND sending=0',
            (reply.message_id,),
        )

    async def finish_reply(self, reply: PendingReply | TelegramResponse, attempt: str,
                           sent: tuple[int, int, str, int] | None, *, delivered: bool = True) -> bool:
        if isinstance(reply, TelegramResponse):
            table, state, key, params = self._delivery_key(reply)
            return await asyncio.to_thread(
                self._update_reply,
                f"""UPDATE {table} SET {state}=?, delivery_attempt=NULL
                    WHERE {key} AND {state}=1 AND delivery_attempt=?""",
                (2 if delivered else 3, *params, attempt),
            )
        return await asyncio.to_thread(self._finish_reply_sync, reply.message_id, attempt, sent)

    def _finish_reply_sync(self, message_id, attempt, sent):
        with self._mutex, closing(self._connect()) as conn, conn:
            deleted = conn.execute("""DELETE FROM pending_replies
                WHERE message_id=? AND sending=1 AND delivery_attempt=?""", (message_id, attempt))
            if not deleted.rowcount:
                return False
            # Either both the assistant message and completion commit, or send intent stays.
            return sent is None or self._insert_assistant(conn, *sent)

    async def save_assistant_message(self, chat_id: int, telegram_message_id: int,
                                     text: str, created_ts: int | None = None) -> bool:
        """Store a Telegram-acknowledged AI reply; no fake user profile is created."""
        return await asyncio.to_thread(self._save_assistant_sync, chat_id,
                                       telegram_message_id, text, created_ts)

    def _save_assistant_sync(self, chat_id, telegram_message_id, text, created_ts):
        try:
            with self._mutex, closing(self._connect()) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                return self._insert_assistant(conn, chat_id, telegram_message_id, text, created_ts)
        except sqlite3.Error as exc:
            LOG.warning('Ошибка записи ответа: %s', type(exc).__name__)
            return False

    def _insert_assistant(self, conn, chat_id, telegram_message_id, text, created_ts):
        if (not valid_id(chat_id) or not valid_id(telegram_message_id, positive=True)
                or self.guard.contains(text)):
            return False
        clean = text.strip()[:self.settings.max_text_chars]
        if not clean or not self.archive.history_enabled(conn, chat_id):
            return False
        ts = int(time.time()) if created_ts is None else int(created_ts)
        cursor = conn.execute("""INSERT OR IGNORE INTO messages
            (chat_id,user_id,telegram_message_id,role,text,created_ts)
            VALUES (?,NULL,?,'assistant',?,?)""",
            (chat_id, telegram_message_id, clean, ts))
        return cursor.rowcount > 0

    async def memories_for_context(self, chat_id: int, is_owner_private: bool,
                                   limit: int = 8) -> list[sqlite3.Row]:
        """Global owner notes are NEVER revealed to a group chat."""
        if not valid_id(chat_id) or chat_id > 0 and chat_id != self.settings.owner_id:
            return []
        return await asyncio.to_thread(self._memories_for_context_sync,
                                       chat_id, is_owner_private, limit)

    def _memories_for_context_sync(self, chat_id, is_owner_private, limit):
        with self._mutex, closing(self._connect()) as conn:
            if is_owner_private and chat_id == self.settings.owner_id:
                return list(conn.execute("""SELECT CASE WHEN subject_user_id IS NULL THEN content
                    ELSE COALESCE((SELECT display_name FROM chat_profiles WHERE chat_id=scope_chat_id AND user_id=subject_user_id),'Участник') || ': ' || content END AS content FROM memories
                    WHERE (scope_chat_id IS NULL OR scope_chat_id=?)
                    AND NOT EXISTS(SELECT 1 FROM memory_sources s WHERE s.memory_id=memories.id
                                   AND s.chat_id>0 AND s.chat_id!=?)
                    ORDER BY pinned DESC,id DESC LIMIT ?""",
                    (chat_id, self.settings.owner_id, min(limit, 15))).fetchall())
            return list(conn.execute("""SELECT CASE WHEN subject_user_id IS NULL THEN content
                ELSE COALESCE((SELECT display_name FROM chat_profiles WHERE chat_id=scope_chat_id AND user_id=subject_user_id),'Участник') || ': ' || content END AS content FROM memories
                WHERE scope_chat_id=? AND EXISTS(SELECT 1 FROM memory_sources s
                    WHERE s.memory_id=memories.id AND s.chat_id=scope_chat_id AND s.kind='self-report')
                ORDER BY pinned DESC,id DESC LIMIT ?""",
                (chat_id, min(limit, 15))).fetchall())

    async def set_preferred_name(self, user_id: int, display_name: str, username: str | None,
                                 preferred_name: str | None, *, chat_id: int | None = None) -> bool:
        if not valid_id(user_id, positive=True) or (chat_id is not None and not valid_id(chat_id)):
            return False
        if preferred_name and self.guard.contains(preferred_name):
            return False
        return await asyncio.to_thread(self._set_preferred_name_sync, user_id, self.guard.redact(display_name),
                                       username, preferred_name, chat_id)

    def _set_preferred_name_sync(self, user_id, display_name, username, preferred_name, chat_id):
        now = int(time.time())
        with self._mutex, closing(self._connect()) as conn:
            if chat_id is not None:
                if (not self.archive.history_enabled(conn, chat_id)
                        or conn.execute('SELECT 1 FROM opt_outs WHERE chat_id=? AND user_id=?',
                                        (chat_id,user_id)).fetchone()):
                    return False
                conn.execute('''INSERT INTO chat_profiles VALUES (?,?,?,?,?)
                    ON CONFLICT(chat_id,user_id) DO UPDATE SET display_name=excluded.display_name,
                    preferred_name=excluded.preferred_name,last_seen_ts=excluded.last_seen_ts''',
                    (chat_id,user_id,display_name[:120],preferred_name,now))
                conn.commit()
                return True
            conn.execute('''INSERT INTO users(user_id,display_name,username,preferred_name,last_seen_ts)
                VALUES (?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                display_name=excluded.display_name,username=excluded.username,
                preferred_name=excluded.preferred_name,last_seen_ts=excluded.last_seen_ts''',
                (user_id, display_name[:120], username, preferred_name, now))
            conn.commit()

            return True

    async def get_preferred_name(self, user_id: int, *, chat_id: int | None = None) -> str | None:
        if not valid_id(user_id, positive=True) or (chat_id is not None and not valid_id(chat_id)):
            return None
        return await asyncio.to_thread(self._get_preferred_name_sync, user_id, chat_id)

    def _get_preferred_name_sync(self, user_id, chat_id):
        with self._mutex, closing(self._connect()) as conn:
            if chat_id is None:
                row = conn.execute('SELECT preferred_name FROM users WHERE user_id=?', (user_id,)).fetchone()
            else:
                row = conn.execute('SELECT preferred_name FROM chat_profiles WHERE chat_id=? AND user_id=?',
                                   (chat_id,user_id)).fetchone()
            name = row['preferred_name'] if row else None
            return name if name and not self.guard.contains(name) else None

    async def remember(self, content: str, *, chat_id: int | None = None,
                       pinned: bool = True, source_mid: int | None = None) -> int | None:
        return await asyncio.to_thread(self._remember_sync, content, chat_id, pinned, source_mid)

    def _remember_sync(self, content, chat_id, pinned, source_mid):
        if (SECRET.search(content) or self.guard.contains(content)
                or chat_id is not None and not valid_id(chat_id)
                or source_mid is not None and not valid_id(source_mid, positive=True)):
            return None
        now = int(time.time())
        with self._mutex, closing(self._connect()) as conn:
            if chat_id is not None and not self.archive.history_enabled(conn, chat_id):
                return None
            conn.execute('BEGIN IMMEDIATE')
            if source_mid is not None:
                known = conn.execute("""SELECT memory_id FROM memory_sources
                    WHERE chat_id=? AND uid=? AND mid=? AND kind IN ('owner-confirmed','owner-corrected')
                    ORDER BY memory_id LIMIT 1""",
                    (chat_id or self.settings.owner_id, self.settings.owner_id, source_mid)).fetchone()
                if known is not None:
                    return known[0]
            count = conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
            if count >= self.settings.max_memories:
                return None
            cursor = conn.execute('''INSERT INTO memories
                (scope_chat_id,content,pinned,created_ts,expires_ts)
                VALUES(?,?,?,?,?)''',
                (chat_id, content[:500], int(pinned), now,
                 None if pinned else now + 365 * DAY))
            conn.execute("INSERT INTO memory_sources VALUES(?,?,?,?,?,'owner-confirmed')",
                         (cursor.lastrowid,chat_id or self.settings.owner_id,self.settings.owner_id,source_mid,now))
            conn.commit()
            return int(cursor.lastrowid)

    async def list_memories(self, limit: int = 10) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._list_memories_sync, limit)

    def _list_memories_sync(self, limit):
        with self._mutex, closing(self._connect()) as conn:
            return list(conn.execute('''SELECT m.id,m.scope_chat_id,m.content,m.pinned,m.created_ts,
                s.chat_id AS source_chat,s.mid AS source_mid,s.kind AS source_kind,m.subject_user_id
                FROM memories m LEFT JOIN memory_sources s ON m.id=s.memory_id
                WHERE (m.scope_chat_id IS NULL OR m.scope_chat_id<0 OR m.scope_chat_id=?)
                AND (s.chat_id IS NULL OR s.chat_id<0 OR s.chat_id=?)
                ORDER BY m.id DESC LIMIT ?''',
                (self.settings.owner_id,self.settings.owner_id,max(1,min(limit,50)))).fetchall())

    async def forget_memory(self, memory_id: int) -> bool:
        if not valid_id(memory_id, positive=True):
            return False
        return await asyncio.to_thread(self._forget_memory_sync, memory_id)

    def _forget_memory_sync(self, memory_id):
        with self._mutex, closing(self._connect()) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if not self.visible_memory(conn, memory_id):
                return False
            cursor = conn.execute('DELETE FROM memories WHERE id=?', (memory_id,))
            conn.commit()
            return cursor.rowcount > 0

    def visible_memory(self, conn, memory_id: int) -> bool:
        owner = self.settings.owner_id
        return conn.execute("""SELECT 1 FROM memories m LEFT JOIN memory_sources s ON m.id=s.memory_id
            WHERE m.id=? AND (m.scope_chat_id IS NULL OR m.scope_chat_id<0 OR m.scope_chat_id=?)
            AND (s.chat_id IS NULL OR s.chat_id<0 OR s.chat_id=?)""", (memory_id,owner,owner)).fetchone() is not None

    async def forget_user_in_chat(self, chat_id: int, user_id: int) -> int:
        if not valid_id(chat_id) or chat_id >= 0 or not valid_id(user_id, positive=True):
            return 0
        return await asyncio.to_thread(self._forget_user_in_chat_sync, chat_id, user_id)

    def _forget_user_in_chat_sync(self, chat_id, user_id):
        with self._mutex, closing(self._connect()) as conn:
            cursor = conn.execute('DELETE FROM messages WHERE chat_id=? AND user_id=?',
                                  (chat_id, user_id))
            conn.execute('''INSERT OR IGNORE INTO opt_outs(chat_id,user_id,created_ts)
                VALUES (?,?,?)''', (chat_id, user_id, int(time.time())))
            conn.execute('DELETE FROM chat_profiles WHERE chat_id=? AND user_id=?', (chat_id,user_id))
            conn.execute('DELETE FROM users WHERE user_id=?', (user_id,))  # Legacy profile had no chat scope.
            self.archive.erase(conn,chat_id,user_id)
            conn.commit()
            return cursor.rowcount

    async def opt_in(self, chat_id: int, user_id: int) -> None:
        if not valid_id(chat_id) or chat_id >= 0 or not valid_id(user_id, positive=True):
            return
        await asyncio.to_thread(self._opt_in_sync, chat_id, user_id)

    def _opt_in_sync(self, chat_id, user_id):
        with self._mutex, closing(self._connect()) as conn:
            conn.execute('DELETE FROM opt_outs WHERE chat_id=? AND user_id=?',
                         (chat_id, user_id))
            conn.commit()

    async def recent_chat_messages(self, chat_id: int, limit: int = 20, *,
                                   before_user_message_id: int | None = None) -> list[sqlite3.Row]:
        """Keep earlier assistant replies and exclude user turns at or after the trigger."""
        if (not valid_id(chat_id) or chat_id > 0 and chat_id != self.settings.owner_id
                or (before_user_message_id is not None
                                    and not valid_id(before_user_message_id, positive=True))):
            return []
        return await asyncio.to_thread(
            self._recent_chat_messages_sync, chat_id, limit, before_user_message_id,
        )

    def _recent_chat_messages_sync(self, chat_id, limit, before_user_message_id):
        with self._mutex, closing(self._connect()) as conn:
            return list(reversed(conn.execute('''SELECT m.text,m.created_ts,m.user_id,m.role,m.telegram_message_id,
                COALESCE(u.preferred_name,u.display_name,
                    CASE WHEN m.role='assistant' THEN 'Аяка' ELSE 'Участник' END) AS speaker
                FROM messages m LEFT JOIN chat_profiles u ON m.chat_id=u.chat_id AND m.user_id=u.user_id
                WHERE m.chat_id=?
                  AND (? IS NULL OR m.role='assistant' OR m.telegram_message_id IS NULL
                       OR m.telegram_message_id < ?)
                ORDER BY m.created_ts DESC,m.id DESC LIMIT ?''',
                (chat_id, before_user_message_id, before_user_message_id,
                 max(1, min(limit, 100)))).fetchall()))

    async def list_chats(self, limit: int = 30) -> list[sqlite3.Row]:
        return await asyncio.to_thread(self._list_chats_sync, limit)

    def _list_chats_sync(self, limit):
        with self._mutex, closing(self._connect()) as conn:
            return list(conn.execute('''SELECT chat_id,chat_type,title,last_seen_ts
                FROM chats WHERE chat_type!='private' OR chat_id=? ORDER BY last_seen_ts DESC LIMIT ?''',
                (self.settings.owner_id, max(1, min(limit, 50)))).fetchall())

    async def stats(self) -> dict:
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self):
        with self._mutex, closing(self._connect()) as conn:
            return {
                'chats': conn.execute('SELECT COUNT(*) FROM chats').fetchone()[0],
                'messages': conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                'memories': conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0],
                'summaries': conn.execute('SELECT COUNT(*) FROM chat_summaries').fetchone()[0],
                'bytes_total': self._total_size(),
                'limit_mb': self.settings.db_max_mb,
            }

    def _total_size(self) -> int:
        total = 0
        for suffix in ('', '-wal', '-shm'):
            try:
                total += Path(str(self.path) + suffix).stat().st_size
            except FileNotFoundError:
                continue  # SQLite can remove WAL/SHM when another connection closes.
        return total

    async def cleanup(self, *, now: int | None = None) -> CleanupReport:
        return await asyncio.to_thread(self._cleanup_sync, now)

    def _cleanup_sync(self, now: int | None = None) -> CleanupReport:
        now = int(time.time()) if now is None else now
        raw_deleted = summary_deleted = quota_deleted = 0
        with self._mutex, closing(self._connect()) as conn:
            # A temporary trigger counts losses caused by retention or size limits only.
            conn.execute("""CREATE TEMP TRIGGER diary_loss BEFORE DELETE ON messages
                WHEN OLD.role='user' AND OLD.id > COALESCE((SELECT cursor FROM diary_jobs
                WHERE chat_id=OLD.chat_id AND day=diary_day(OLD.created_ts)),0)
                BEGIN INSERT INTO diary_jobs(chat_id,day,lost) VALUES(OLD.chat_id,diary_day(OLD.created_ts),1)
                ON CONFLICT(chat_id,day) DO UPDATE SET lost=lost+1,dirty=1; END""")
            prior_loss = conn.execute('SELECT COALESCE(SUM(lost),0) FROM diary_jobs').fetchone()[0]
            group_cutoff = now - self.settings.group_raw_days * DAY
            private_cutoff = now - self.settings.private_raw_days * DAY
            cur = conn.execute('''DELETE FROM messages WHERE id IN (
                SELECT m.id FROM messages m JOIN chats c ON c.chat_id=m.chat_id
                WHERE (c.chat_type='private' AND m.created_ts < ?)
                   OR (c.chat_type IN ('group','supergroup') AND m.created_ts < ?)
            )''', (private_cutoff, group_cutoff))
            raw_deleted += cur.rowcount

            # Лимит числа сообщений в каждом чате, независимо от его активности.
            chat_ids = [r[0] for r in conn.execute('SELECT chat_id FROM chats')]
            for chat_id in chat_ids:
                cur = conn.execute('''DELETE FROM messages WHERE id IN (
                    SELECT id FROM messages WHERE chat_id=?
                    ORDER BY created_ts DESC, id DESC LIMIT -1 OFFSET ?
                )''', (chat_id, self.settings.per_chat_max_messages))
                raw_deleted += cur.rowcount
            summary_deleted = conn.execute('DELETE FROM chat_summaries WHERE created_ts < ?',
                                           (now - self.settings.summary_days * DAY,)).rowcount
            conn.execute('DELETE FROM memories WHERE expires_ts IS NOT NULL AND expires_ts < ?', (now,))
            # Удаляем давно неактивные неименованные профили, если сообщений о них нет.
            conn.execute('''DELETE FROM users WHERE preferred_name IS NULL AND last_seen_ts < ?
                AND NOT EXISTS(SELECT 1 FROM messages WHERE messages.user_id=users.user_id)''',
                (now - 365 * DAY,))
            conn.execute('''DELETE FROM chat_profiles WHERE last_seen_ts<?
                AND NOT EXISTS(SELECT 1 FROM messages m WHERE m.chat_id=chat_profiles.chat_id AND m.user_id=chat_profiles.user_id)
                AND NOT EXISTS(SELECT 1 FROM diary_sources s WHERE s.chat_id=chat_profiles.chat_id AND s.uid=chat_profiles.user_id)
                AND NOT EXISTS(SELECT 1 FROM memories x WHERE x.scope_chat_id=chat_profiles.chat_id AND x.subject_user_id=chat_profiles.user_id)''',
                (now - 365 * DAY,))
            # Реестр чатов хранится, пока есть связанные данные; пустые старые записи удаляем.
            conn.execute('''DELETE FROM chats WHERE last_seen_ts < ?
                AND NOT EXISTS(SELECT 1 FROM messages WHERE messages.chat_id=chats.chat_id)
                AND NOT EXISTS(SELECT 1 FROM chat_summaries WHERE chat_summaries.chat_id=chats.chat_id)
                AND NOT EXISTS(SELECT 1 FROM memories WHERE memories.scope_chat_id=chats.chat_id)''',
                (now - 365 * DAY,))
            conn.commit()

            self._reclaim(conn)
            budget = self.settings.db_max_mb * 1024 * 1024
            # Мягкий лимит файлов (в дополнение к SQLite max_page_count).
            # Сохраняем ручные воспоминания: удаляем только самые старые сырые сообщения.
            attempts = 0
            while self._total_size() > int(budget * 0.95) and attempts < 100:
                cur = conn.execute('''DELETE FROM messages WHERE id IN
                    (SELECT id FROM messages ORDER BY created_ts,id LIMIT 1000)''')
                if cur.rowcount == 0 and not self.archive.prune_for_space(conn):
                    break
                quota_deleted += cur.rowcount
                attempts += 1
                conn.commit()
                self._reclaim(conn)
            loss = conn.execute('SELECT COALESCE(SUM(lost),0) FROM diary_jobs').fetchone()[0] - prior_loss
            if loss>0:
                LOG.warning('Удалено до обработки дневником: %d сообщений', loss)
            conn.execute('DELETE FROM telegram_responses WHERE created_ts<=?', (now - RESPONSE_RETENTION,))
            self.archive.expire(conn, now - self.settings.summary_days * DAY)
            conn.commit()
            self._prune_rejections(conn, now)
            total = self._total_size()
            if total > budget:
                LOG.warning('Размер SQLite + WAL = %s байт при лимите %s; требуется внимание',
                            total, budget)
            return CleanupReport(raw_deleted, summary_deleted, quota_deleted, total)

    def _prune_rejections(self, conn: sqlite3.Connection, now: int) -> None:
        for path in self._rejection_directory.glob('*.rejected'):
            if not conn.execute('''SELECT 1 FROM pending_replies WHERE delivery_attempt=?
                UNION ALL SELECT 1 FROM telegram_responses WHERE delivery_attempt=? LIMIT 1''',
                                (path.stem, path.stem)).fetchone():
                self._remove_rejection(path)
        for path in self._rejection_directory.glob('.rejected-*'):
            try:
                if path.stat().st_mtime < now - DAY:
                    self._remove_rejection(path)
            except OSError:
                continue

    def _reclaim(self, conn: sqlite3.Connection) -> None:
        # Работает на созданной нами БД с auto_vacuum=INCREMENTAL.
        # SQLite advances the vacuum one page per result row.
        conn.execute('PRAGMA incremental_vacuum(5000)').fetchall()
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
