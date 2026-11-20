"""Private logs, process locking and Telegram polling with durable checkpoints."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import GetUpdates
from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from python_socks.async_.asyncio import Proxy

from .group import ReplyCapacityExceeded
from .storage import DeliveryRetryDelay, StorageUnavailable

LOG = logging.getLogger(__name__)
TEMPORARY_ERRORS = (
    TelegramNetworkError, TelegramRetryAfter, TelegramServerError, StorageUnavailable,
    ReplyCapacityExceeded, DeliveryRetryDelay,
)
DELAYS = (1, 2, 4, 8, 15)


class AlreadyRunning(RuntimeError):
    pass


class RetryLimitExceeded(RuntimeError):
    def __init__(self, phase: str, attempts: int, error: Exception):
        self.phase = phase
        self.attempts = attempts
        self.error_type = type(error).__name__
        status = getattr(error, 'http_status', None)
        self.http_status = status if type(status) is int and 100 <= status <= 599 else None
        http = f', HTTP {self.http_status}' if self.http_status is not None else ''
        super().__init__(f'Исчерпаны повторы на этапе {phase}. Попыток {attempts}, '
                         f'повторов {attempts - 1}, последняя ошибка {self.error_type}{http}.')


def state_dir() -> Path:
    directory = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'ayaka'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


@contextmanager
def process_lock(name: str, *, directory: Path | None = None):
    if directory is None:
        directory = state_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / name
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunning from None
        yield
    finally:
        os.close(descriptor)


def configure_logs(filename: str) -> None:
    handler = RotatingFileHandler(state_dir() / filename, maxBytes=1024 * 1024,
                                  backupCount=4, encoding='utf-8', delay=True)
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True,
                        format='%(asctime)s %(levelname)s %(message)s')
    logging.getLogger('aiogram').setLevel(logging.CRITICAL + 1)


def notify(message: str) -> None:
    address = os.getenv('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0' + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
        client.connect(address)
        client.sendall(message.encode('utf-8'))


async def wait_for_stop(stop: asyncio.Event, delay: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), delay)
        return True
    except TimeoutError:
        return False


async def retry_delay(stop, error, failures, *, phase: str):
    if failures > len(DELAYS):
        raise RetryLimitExceeded(phase, failures, error) from None
    delay = DELAYS[failures - 1]
    if isinstance(error, (TelegramRetryAfter, DeliveryRetryDelay)):
        delay = max(delay, error.retry_after)
    LOG.warning('Повтор после временного сбоя %s, попытка %d, пауза %s с',
                type(error).__name__, failures, delay)
    return await wait_for_stop(stop, delay)


async def wait_for_tor(proxy_url: str, stop: asyncio.Event, timeout: float = 45) -> bool:
    proxy = Proxy.from_url(proxy_url, rdns=True)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not stop.is_set():
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError('Tor could not connect to Telegram over SOCKS5')
        try:
            conn = await proxy.connect(dest_host='api.telegram.org', dest_port=443,
                                       timeout=min(3, remaining))
            conn.close()
            return not stop.is_set()
        except (ProxyConnectionError, ProxyError, ProxyTimeoutError, OSError, TimeoutError):
            await wait_for_stop(stop, min(1, max(0, deadline - loop.time())))
    return False


async def telegram_ready(bot, stop: asyncio.Event) -> bool:
    failures = 0
    while not stop.is_set():
        phase = 'getMe'
        try:
            await bot.me()
            phase = 'deleteWebhook'
            await bot.delete_webhook(drop_pending_updates=False)
            return not stop.is_set()
        except TEMPORARY_ERRORS as error:
            failures += 1
            if await retry_delay(stop, error, failures, phase=phase):
                break
    return False


async def poll(bot, dispatcher, db, stop: asyncio.Event) -> None:
    offset = await db.polling_offset(bot.id)
    checkpoint = None
    failures = 0
    while not stop.is_set():
        phase = 'запись позиции опроса'
        try:
            if checkpoint is not None:
                await db.save_polling_offset(bot.id, checkpoint)
                checkpoint = None
            phase = 'getUpdates'
            updates = await bot(GetUpdates(offset=offset, timeout=12, limit=32,
                                           allowed_updates=['message']), request_timeout=18)
            if not updates:
                # That request confirmed the previous position. An idle bot may get a new sequence later.
                offset = None
            for update in updates:
                if stop.is_set():
                    return
                phase = 'обработка обновления'
                await dispatcher.feed_update(bot, update)
                # On the next poll Telegram acknowledges only completed updates.
                offset = update.update_id + 1
                checkpoint = offset
                phase = 'запись позиции опроса'
                await db.save_polling_offset(bot.id, checkpoint)
                checkpoint = None
                if stop.is_set():
                    return
            if failures:
                LOG.info('Связь с Telegram восстановлена')
            failures = 0
        except TEMPORARY_ERRORS as error:
            failures += 1
            if await retry_delay(stop, error, failures, phase=phase):
                return
