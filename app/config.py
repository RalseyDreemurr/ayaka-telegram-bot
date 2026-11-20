"""Настройки проекта. Все секреты остаются в .env (не в git)."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from urllib.parse import urlsplit
import os
import re

from dotenv import load_dotenv

from .security import parse_id


BASE_DIR = Path(__file__).resolve().parents[1]
RELEASE_DB_PATH = BASE_DIR / "data" / "ayaka-release.sqlite3"


class TelegramConfigurationError(ValueError):
    pass


def telegram_proxy(transport: str, proxy_url: str | None) -> str | None:
    if transport == 'direct':
        if proxy_url:
            raise TelegramConfigurationError(
                'При TELEGRAM_TRANSPORT=direct оставь TELEGRAM_PROXY_URL пустым.',
            )
        return None
    if transport != 'socks5':
        raise TelegramConfigurationError('TELEGRAM_TRANSPORT должен быть direct или socks5.')
    if proxy_url:
        try:
            endpoint = urlsplit(proxy_url)
            if (endpoint.scheme == 'socks5' and endpoint.hostname and endpoint.port
                    and endpoint.path in ('', '/') and not (endpoint.query or endpoint.fragment)):
                return proxy_url
        except ValueError:
            pass
    raise TelegramConfigurationError(
        'Для TELEGRAM_TRANSPORT=socks5 укажи TELEGRAM_PROXY_URL=socks5://хост:порт.',
    )


def _integer(name: str, default: int, min_value: int, max_value: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        number = int(raw)
    except ValueError:
        raise ValueError(f'{name}: ожидается целое число') from None
    if not min_value <= number <= max_value:
        raise ValueError(f'{name}: допустимо {min_value}..{max_value}')
    return number


@dataclass(frozen=True)
class GroupInitiative:
    enabled: bool = True
    cooldown_seconds: int = 900
    min_messages: int = 30
    chance_percent: int = 10


def group_initiative_overrides(raw: str, defaults: GroupInitiative) -> dict[int, GroupInitiative]:
    try:
        groups = json.loads(raw)
    except ValueError:
        raise ValueError('GROUP_INITIATIVE_JSON: ожидается JSON-объект') from None
    if not isinstance(groups, dict):
        raise ValueError('GROUP_INITIATIVE_JSON: ожидается объект с ID групп')

    ranges = {'cooldown_seconds': (60, 86400), 'min_messages': (5, 500),
              'chance_percent': (0, 100)}
    overrides = {}
    for chat_id, record in groups.items():
        if not re.fullmatch(r'-[1-9][0-9]{0,18}', chat_id) or int(chat_id) < -(2**63):
            raise ValueError('GROUP_INITIATIVE_JSON: ID группы должен быть отрицательным целым')
        if not isinstance(record, dict) or record.keys() - (ranges.keys() | {'enabled'}):
            raise ValueError('GROUP_INITIATIVE_JSON: неизвестная настройка группы')
        enabled = record.get('enabled', defaults.enabled)
        if type(enabled) is not bool:
            raise ValueError('GROUP_INITIATIVE_JSON: enabled должен быть true или false')
        values = {}
        for key, (low, high) in ranges.items():
            value = record.get(key, getattr(defaults, key))
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'GROUP_INITIATIVE_JSON: для {key} допустимо {low}..{high}')
            values[key] = value
        overrides[int(chat_id)] = GroupInitiative(enabled=enabled, **values)
    return overrides


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    owner_id: int
    db_path: Path
    group_raw_days: int
    private_raw_days: int
    summary_days: int
    per_chat_max_messages: int
    db_max_mb: int
    cleanup_minutes: int
    max_text_chars: int
    max_memories: int
    telegram_proxy_url: str | None = field(default=None, repr=False)
    telegram_transport: str = 'socks5'
    # Native Ollama /api/chat is accessed directly, never through Tor.
    ollama_base_url: str = field(default='http://127.0.0.1:11434', repr=False)
    ollama_model: str = 'qwen3:8b-q4_K_M'
    ollama_num_ctx: int = 4096
    ollama_max_output: int = 320
    ollama_timeout: int = 180
    ollama_history_limit: int = 16
    diary_timezone: str = 'Europe/Moscow'
    spontaneous_enabled: bool = True
    spontaneous_cooldown: int = 900
    spontaneous_min_messages: int = 30
    spontaneous_chance: int = 10
    group_initiative: dict[int, GroupInitiative] = field(default_factory=dict)

    def initiative_for(self, chat_id: int) -> GroupInitiative:
        return self.group_initiative.get(chat_id) or GroupInitiative(
            self.spontaneous_enabled, self.spontaneous_cooldown,
            self.spontaneous_min_messages, self.spontaneous_chance,
        )


def load_settings() -> Settings:
    load_dotenv(BASE_DIR / '.env')
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    owner_raw = os.getenv('OWNER_ID', '').strip()
    if not token:
        raise ValueError('В .env не задан TELEGRAM_BOT_TOKEN')
    owner_id = parse_id(owner_raw, positive=True)
    if owner_id is None:
        raise ValueError('В .env должен быть один числовой OWNER_ID')

    path = Path(os.getenv('DB_PATH') or RELEASE_DB_PATH).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path

    initiative = GroupInitiative(
        enabled=os.getenv('SPONTANEOUS_ENABLED', 'true').lower() in ('1', 'true', 'yes', 'on'),
        cooldown_seconds=_integer('SPONTANEOUS_COOLDOWN_SECONDS', 900, 60, 86400),
        min_messages=_integer('SPONTANEOUS_MIN_MESSAGES', 30, 5, 500),
        chance_percent=_integer('SPONTANEOUS_CHANCE_PERCENT', 10, 0, 100),
    )
    return Settings(
        token=token,
        diary_timezone=os.getenv('DIARY_TIMEZONE', 'Europe/Moscow'),
        owner_id=owner_id,
        db_path=Path(os.path.abspath(path)),
        group_raw_days=_integer('GROUP_RAW_DAYS', 14, 1, 365),
        private_raw_days=_integer('PRIVATE_RAW_DAYS', 30, 1, 365),
        summary_days=_integer('SUMMARY_DAYS', 180, 1, 3650),
        per_chat_max_messages=_integer('PER_CHAT_MAX_MESSAGES', 10000, 100, 100000),
        db_max_mb=_integer('DB_MAX_MB', 128, 16, 4096),
        cleanup_minutes=_integer('CLEANUP_MINUTES', 60, 5, 1440),
        max_text_chars=_integer('MAX_TEXT_CHARS', 4000, 200, 10000),
        max_memories=_integer('MAX_MEMORIES', 200, 1, 2000),
        telegram_proxy_url=os.getenv('TELEGRAM_PROXY_URL', '').strip() or None,
        telegram_transport=os.getenv('TELEGRAM_TRANSPORT', 'socks5').strip().lower(),
        ollama_base_url=os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').strip(),
        ollama_model=os.getenv('OLLAMA_MODEL', 'qwen3:8b-q4_K_M').strip(),
        ollama_num_ctx=_integer('OLLAMA_NUM_CTX', 4096, 1024, 32768),
        ollama_max_output=_integer('OLLAMA_MAX_OUTPUT', 320, 64, 1024),
        ollama_timeout=_integer('OLLAMA_TIMEOUT_SECONDS', 180, 10, 600),
        ollama_history_limit=_integer('OLLAMA_HISTORY_LIMIT', 16, 2, 80),
        spontaneous_enabled=initiative.enabled,
        spontaneous_cooldown=initiative.cooldown_seconds,
        spontaneous_min_messages=initiative.min_messages,
        spontaneous_chance=initiative.chance_percent,
        group_initiative=group_initiative_overrides(os.getenv('GROUP_INITIATIVE_JSON', '').strip() or '{}', initiative),
    )
