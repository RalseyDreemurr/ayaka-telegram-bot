"""Systemd user-service controller for Fish and the Windows logon task."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.parse import urlsplit

from app.ai import OllamaClient, OllamaError
from app.config import TelegramConfigurationError, load_settings, telegram_proxy
from app.db_tools import backup, check, load_release_settings, prepare_release
from app.runtime import AlreadyRunning, configure_logs, process_lock

ROOT = Path(__file__).resolve().parent
BOT = 'ayaka.service'
TOR = 'ayaka-tor.service'
START_WAIT = 160
COMMON = {
    'Restart': 'on-failure', 'RestartSec': '35s', 'StartLimitIntervalSec': '15min',
    'StartLimitBurst': '3', 'UMask': '0077', 'StandardOutput': 'null', 'StandardError': 'null',
    'KillMode': 'mixed', 'TimeoutStopSec': '210s',
}


def socks_ready(proxy: str | None) -> bool:
    if not proxy:
        return False
    try:
        endpoint = urlsplit(proxy)
        if endpoint.scheme != 'socks5' or not endpoint.hostname or not endpoint.port:
            return False
        method = 2 if endpoint.username is not None else 0
        with socket.create_connection((endpoint.hostname, endpoint.port), timeout=0.6) as conn:
            conn.sendall(bytes((5, 1, method)))
            return conn.recv(2, socket.MSG_WAITALL) == bytes((5, method))
    except (ValueError, OSError):
        return False


def needs_local_tor(settings):
    proxy = telegram_proxy(settings.telegram_transport, settings.telegram_proxy_url)
    return proxy is not None and urlsplit(proxy).hostname in {'127.0.0.1', '::1', 'localhost'}


def native_tor_present() -> bool:
    for path in Path('/proc').glob('[0-9]*/comm'):
        try:
            if path.read_text().strip() == 'tor':
                return True
        except OSError:
            continue
    return False


def manual_bot_present() -> bool:
    for process in Path('/proc').glob('[0-9]*'):
        try:
            parts = (process / 'cmdline').read_bytes().split(b'\0')
            if not parts or 'python' not in Path(os.fsdecode(parts[0])).name.lower():
                continue
            cwd = (process / 'cwd').resolve(strict=True)
            for arg in parts[1:]:
                entry = Path(os.fsdecode(arg))
                if entry.name == 'main.py' and (cwd / entry).resolve() == ROOT / 'main.py':
                    return True
        except OSError:
            continue
    return False


class UserSystemdUnavailable(RuntimeError):
    pass


class UserServices:
    @staticmethod
    def command(args, timeout=12):
        return subprocess.run(args, timeout=timeout, capture_output=True, text=True)

    def show(self, unit):
        try:
            result = self.command(['systemctl', '--user', 'show', unit,
                                   '-p', 'LoadState,ActiveState,StatusText'])
        except (FileNotFoundError, subprocess.TimeoutExpired):
            raise UserSystemdUnavailable from None
        values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if 'LoadState' not in values:
            raise UserSystemdUnavailable
        return values

    def start(self, unit):
        status = self.show(unit)
        if status.get('ActiveState') in {'active', 'activating', 'reloading'}:
            return
        if status.get('LoadState') == 'loaded':
            result = self.command(['systemctl', '--user', '--no-block', 'start', unit])
        else:
            properties = dict(COMMON, WorkingDirectory=str(ROOT))
            if unit == BOT:
                properties.update(Type='notify', NotifyAccess='main', TimeoutStartSec='150s')
                action = [str(ROOT / 'venv/bin/python'), '-B', str(ROOT / 'main.py'), '--release']
            else:
                properties.update(Type='simple', TimeoutStopSec='30s')
                action = [str(ROOT / 'venv/bin/python'), '-B', str(ROOT / 'ayakactl.py'), 'serve-tor']
            result = self.command(['systemd-run', '--user', '--no-block', '--unit='+unit,
                                   *(f'--property={key}={value}' for key,value in properties.items()),
                                   *action])
        if result.returncode:
            raise RuntimeError('systemd отклонил запуск')

    def stop(self, unit):
        if self.show(unit).get('LoadState') != 'not-found':
            if self.command(['systemctl','--user','stop',unit], timeout=225).returncode:
                raise RuntimeError('systemd не подтвердил остановку')


def serve_tor(settings):
    configure_logs('tor.log')
    if socks_ready(settings.telegram_proxy_url):
        logging.info('Используется уже доступный SOCKS5')
        return 0
    if native_tor_present():
        logging.info('Tor уже запущен. Готовность проверит Аяка')
        return 0
    config = Path(os.getenv('AYAKA_TORRC') or Path.home() / '.config/tor-ayaka/torrc').expanduser()
    if not config.is_absolute():
        config = ROOT / config
    program = shutil.which('tor')
    if not program or not config.is_file():
        logging.error('Tor или torrc недоступен')
        return 1
    logging.info('Запуск Tor с существующей конфигурацией')
    with open(os.devnull, 'wb') as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    os.execv(program, [program, '-f', str(config), '--RunAsDaemon', '0', '--Log', 'err stdout'])


async def check_ollama(settings):
    client = OllamaClient(settings)
    try:
        await client.start()
        return 'готова' if settings.ollama_model in await client.models() else 'модель отсутствует'
    except OllamaError:
        return 'недоступна'
    finally:
        await client.close()


def wait_ready(services):
    deadline = time.monotonic() + START_WAIT
    while time.monotonic() < deadline:
        state = services.show(BOT)
        if state.get('ActiveState') == 'active':
            status = state.get('StatusText', '')
            for route in ('напрямую', 'через SOCKS5', 'через Tor'):
                for label in ('готова', 'недоступна', 'модель отсутствует'):
                    if status == f'Telegram готов {route}. Ollama {label}':
                        print(f'Аяка готова. Telegram {route}. Ollama {label}.')
                        return 0
            print('Служба Аяки активна.')
            return 0
        if state.get('ActiveState') == 'failed':
            break
        time.sleep(0.5)
    print('Готовность Аяки не подтверждена. Подробности в техническом журнале.')
    return 1


def start(services, settings):
    current = services.show(BOT).get('ActiveState')
    if current not in {'active', 'activating', 'reloading'}:
        if manual_bot_present():
            print('Аяка уже запущена вручную. Сначала останови тот экземпляр.')
            return 2
        if (needs_local_tor(settings) and not socks_ready(settings.telegram_proxy_url)
                and not native_tor_present()):
            services.start(TOR)
        services.start(BOT)
    return wait_ready(services)


def stop(services):
    services.stop(BOT)
    if manual_bot_present():
        print('Обнаружен ручной экземпляр Аяки. Его можно остановить в исходном сеансе.')
        return 2
    services.stop(TOR)
    print('Управляемые службы Аяки и Tor остановлены.')
    return 0


def hold(services):
    while True:
        state = services.show(BOT).get('ActiveState')
        if state not in {'active','activating','reloading','deactivating'}:
            return int(state == 'failed')
        time.sleep(3)


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start','stop','restart','status','serve-tor','tor-required',
                                           'backup','verify-backup','init-db'))
    parser.add_argument('path', nargs='?', type=Path)
    parser.add_argument('--wait', action='store_true')
    parser.add_argument('--release', action='store_true')
    args = parser.parse_args()
    if args.action in ('start', 'restart') and not args.release:
        parser.error('Для запуска релиза явно укажи --release.')
    if args.release and args.action not in ('start', 'restart'):
        parser.error('--release доступен с start или restart.')
    if args.wait and args.action != 'start':
        parser.error('--wait доступен с start')
    if args.action == 'verify-backup' and args.path:
        check(args.path)
        print('Проверка целостности копии прошла.')
        return 0
    if args.action == 'init-db':
        if args.path is not None:
            parser.error('Укажи путь к новой SQLite через DB_PATH.')
        settings = load_settings()
        try:
            asyncio.run(prepare_release(settings, ()))
        except FileExistsError:
            print('Файл SQLite уже существует. Укажи новый путь в DB_PATH.')
            return 2
        print('Чистая SQLite проверена: ' + str(settings.db_path))
        return 0
    if args.action in ('start', 'restart', 'status', 'backup'):
        settings = load_release_settings()
    elif args.action != 'stop':
        settings = load_settings()
    if args.action in ('start', 'restart', 'status'):
        telegram_proxy(settings.telegram_transport, settings.telegram_proxy_url)
    if args.action == 'tor-required':
        return 0 if needs_local_tor(settings) else 1
    if args.action == 'serve-tor':
        return serve_tor(settings) if needs_local_tor(settings) else 0
    if args.action == 'backup' and args.path:
        backup(settings.db_path, args.path)
        print('Подготовлена и проверена отдельная SQLite.')
        return 0
    if args.path or args.action in ('backup','verify-backup'):
        parser.error('Укажи путь для операции с SQLite.')
    services = UserServices()
    if args.action == 'status':
        try:
            state = services.show(BOT).get('ActiveState', 'unknown')
        except UserSystemdUnavailable:
            state = 'no-systemd'
        label = {'active':'работает','activating':'запускается','failed':'сбой',
                 'inactive':'остановлена','deactivating':'останавливается',
                 'no-systemd':'пользовательский systemd недоступен'}.get(state,'неизвестно')
        if state in ('inactive', 'unknown', 'no-systemd') and manual_bot_present():
            label = 'запущена вручную'
        print('Аяка: '+label)
        print('SQLite релиза: '+str(settings.db_path))
        if settings.telegram_transport == 'direct':
            print('Telegram: прямое подключение')
        else:
            print('Telegram SOCKS5: '+('доступен' if socks_ready(settings.telegram_proxy_url) else 'недоступен'))
        print('Ollama: '+asyncio.run(check_ollama(settings)))
        return 0
    with process_lock('control.lock'):
        if args.action in ('stop','restart'):
            result = stop(services)
            if result or args.action == 'stop':
                return result
        result = start(services, settings)
    return hold(services) if args.wait and result == 0 else result


if __name__ == '__main__':
    os.umask(0o077)
    try:
        raise SystemExit(cli())
    except AlreadyRunning:
        print('Операция управления Аякой уже выполняется.')
        raise SystemExit(2) from None
    except TelegramConfigurationError as exc:
        print(exc)
        raise SystemExit(1) from None
    except UserSystemdUnavailable:
        print('Пользовательский systemd недоступен. Ручной запуск: ./venv/bin/python -B main.py --release')
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception as exc:
        # Config, connection and subprocess exceptions can carry sensitive values.
        print('Операция не завершена. Тип ошибки: '+type(exc).__name__)
        raise SystemExit(1) from None
