"""Точка входа подготовленного релиза Аяки."""
import argparse
import asyncio
from contextlib import AsyncExitStack, suppress
import logging
import os
import signal
import sys

from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError

from app.bot import protect_telegram_text, setup_dispatcher
from app.ai import OllamaClient, OllamaError
from app.config import RELEASE_DB_PATH, Settings, TelegramConfigurationError, telegram_proxy
from app.db_tools import load_release_settings
from app.storage import Storage
from app.runtime import (
    AlreadyRunning, RetryLimitExceeded, configure_logs, notify, poll, process_lock, telegram_ready,
    wait_for_tor,
)


def archive_status(conn):
    tables = ('chat_summaries', 'chat_diary_entries', 'daily_diary_entries')
    counts = [conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in tables]
    pending = conn.execute('SELECT count(*) FROM diary_jobs WHERE dirty=1').fetchone()[0]
    return (*counts, pending)


async def cleanup_forever(db: Storage, interval_minutes: int, ai: OllamaClient) -> None:
    while True:
        try:
            before = await db.archive.query(archive_status)
            await db.archive.maintain(ai, batches=1 if ai.busy else 8)
            after = await db.archive.query(archive_status)
            logging.info('Архив: сводки=%d, группы=%d, дни=%d, ожидают=%d, новых=%d',
                         *after, sum(max(0, a-b) for a,b in zip(after[:3],before[:3])))
        except Exception as exc:
            logging.warning('Обработка архива отложена: %s',type(exc).__name__)
        try:
            result = await db.cleanup()
            logging.info('Очистка: raw=%s, summaries=%s, quota=%s, bytes=%s',
                         result.deleted_raw, result.deleted_summaries,
                         result.deleted_over_quota, result.bytes_total)
        except Exception as exc:
            logging.warning('Сбой обслуживания базы: %s', type(exc).__name__)
        await asyncio.sleep(interval_minutes * 60)


async def normalize_proxy_error(make_request, bot, method):
    try:
        return await make_request(bot, method)
    except (ProxyConnectionError, ProxyError, ProxyTimeoutError):
        raise TelegramNetworkError(method=method, message='Ошибка соединения через Tor SOCKS5') from None
    except TelegramNetworkError:
        raise TelegramNetworkError(method=method, message='Ошибка сети Telegram') from None


class TelegramSession(AiohttpSession):
    def check_response(self, bot, method, status_code, content):
        try:
            return super().check_response(bot, method, status_code, content)
        except TelegramAPIError as exc:
            exc.http_status = status_code
            raise


def telegram_session(proxy_url: str | None, *, transport: str = 'socks5') -> AiohttpSession:
    # aiogram uses remote DNS resolution for SOCKS proxies.
    proxy = telegram_proxy(transport, proxy_url)
    try:
        session = TelegramSession(proxy=proxy, timeout=8)
    except (TypeError, ValueError):
        raise TelegramConfigurationError('Некорректный адрес в TELEGRAM_PROXY_URL.') from None
    session.middleware.register(normalize_proxy_error)
    return session


async def main(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    async with AsyncExitStack() as resources:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
            resources.callback(loop.remove_signal_handler, sig)
        proxy = settings.telegram_proxy_url
        session = telegram_session(proxy, transport=settings.telegram_transport)
        resources.push_async_callback(session.close)
        route = 'через SOCKS5' if proxy else 'напрямую'
        logging.info('Проверка соединения с Telegram %s', route)
        if proxy and not await wait_for_tor(proxy, stop):
            return
        bot = Bot(token=settings.token, session=session)

        ai = OllamaClient(settings)
        resources.push_async_callback(ai.close)
        await ai.start()
        db = Storage(settings)
        session.middleware.register(protect_telegram_text(db.guard))
        await db.initialize()
        if not await db.quick_check():
            raise RuntimeError('SQLite не прошла быструю проверку')
        logging.info('SQLite готова')
        dispatcher = setup_dispatcher(settings, db, ai)

        if not await telegram_ready(bot, stop):
            return
        ollama_status = 'недоступна'
        try:
            installed = await ai.models()
            ollama_status = 'готова' if settings.ollama_model in installed else 'модель отсутствует'
        except OllamaError:
            pass
        status = f'Telegram готов {route}. Ollama {ollama_status}'
        logging.info('%s', status)
        notify('READY=1\nSTATUS=' + status)
        maintenance_task = asyncio.create_task(cleanup_forever(db, settings.cleanup_minutes, ai))
        try:
            await poll(bot, dispatcher, db, stop)
        finally:
            notify('STOPPING=1')
            logging.info('Завершение фонового обслуживания')
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', action='store_true', required=True)
    parser.parse_args()
    os.umask(0o077)
    try:
        with process_lock('bot.lock', directory=RELEASE_DB_PATH.parent):
            configure_logs('bot.log')
            settings = load_release_settings()
            logging.info('Запуск Аяки с SQLite релиза')
            asyncio.run(main(settings))
            logging.info('Аяка остановлена')
    except AlreadyRunning:
        print('Аяка уже запущена.')
        raise SystemExit(2) from None
    except TelegramConfigurationError as exc:
        logging.error('%s', exc)
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None
    except RetryLimitExceeded as exc:
        logging.error('%s', exc)
        print(exc, file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as exc:
        logging.error('Сбой Аяки: %s', type(exc).__name__)
        raise SystemExit(1) from None
