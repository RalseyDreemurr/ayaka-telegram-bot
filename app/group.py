"""Bounded, per-chat decisions before calling Ollama."""

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import random
import re
from time import monotonic

from .config import Settings


GROUP_REPLY_GAP = 8
USER_REPLY_GAP = 30
MAX_ACTIVE_GROUP_REPLIES = 3
TOPIC_WINDOW = 180
STALE_UPDATE_SECONDS = 120
INITIATIVE_CHECK_GAP = 15
ABUSE_WINDOW = 300
MUTE_SECONDS = 600
BOUNDARY_REPLY = 'Давай без оскорблений. Я лучше поспорю с ядром Linux — оно хотя бы пишет, где сбой.'

_NAME = re.compile(r'(?<!\w)аяка(?!\w)', re.I)
_ABUSE = re.compile(
    r'\b(?:заткнись|пошла\s+(?:нах\w*|к\s+ч[её]рту))\b'
    r'|\bты\s+(?:(?:просто|совсем|такая)\s+)?(?:тупая|тупица|идиотка|дебилка)\b'
    r'|^\s*(?:(?:аяка|@\w+)[\s,:—-]+)?(?:тупая|тупица|идиотка|дебилка)[!?.\s]*$',
    re.I,
)
_TOPIC_SIGNALS = tuple(re.compile(pattern, re.I) for pattern in (
    r'\b(игр|геймпле|квест|босс|кооп|steam(?=\b)|minecraft(?=\b)|майнкрафт)\w*\b',
    r'\b(аниме|манг|манхв|озвучк|сэйю|сёнэн)\w*\b',
    r'\b(linux(?=\b)|линукс|arch(?=\b)|арх(?=\b)|ufw(?=\b)|pacman(?=\b)|'
    r'systemd(?=\b)|дистрибутив|ядр|драйвер|пакетн)\w*\b',
    r'\b(видеокарт|процессор|оперативк|озу(?=\b)|radeon|geforce|ryzen|intel|'
    r'материнк|железо|ssd|кулер)\w*\b',
))


def addressed_by_name(text: str) -> bool:
    return bool(_NAME.search(text))


def mentioned_username(text: str, username: str) -> bool:
    return bool(username and re.search(r'(?i)(?<![\w@/])@' + re.escape(username) + r'(?!\w)', text))


def topic_keys(text: str) -> dict[int, set[str]]:
    return {index: {match.casefold() for match in matches}
            for index, pattern in enumerate(_TOPIC_SIGNALS)
            if (matches := pattern.findall(text))}


class ReplyCapacityExceeded(RuntimeError):
    pass


class ReplyKind(Enum):
    DIRECT = 'direct'
    INITIATIVE = 'initiative'
    BOUNDARY = 'boundary'


@dataclass
class TopicTurn:
    message_id: int
    user_id: int
    when: float
    signals: dict[int, set[str]]


@dataclass
class ChatActivity:
    recent: deque[TopicTurn] = field(default_factory=lambda: deque(maxlen=6))
    new_messages: int = 0
    last_reply: float = float('-inf')
    last_initiative: float = float('-inf')
    last_check: float = float('-inf')
    active: ReplyKind | None = None
    last_direct: dict[int, float] = field(default_factory=dict)
    incidents: dict[int, float] = field(default_factory=dict)
    muted_until: dict[int, float] = field(default_factory=dict)


class GroupReplies:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.chats: dict[int, ChatActivity] = {}
        self.active = 0

    def forget(self, chat_id: int, user_id: int) -> None:
        chat = self.chats.get(chat_id)
        if chat is not None:
            chat.recent = deque(
                (turn for turn in chat.recent if turn.user_id != user_id),
                maxlen=chat.recent.maxlen,
            )

    def consider(self, *, chat_id: int, user_id: int, message_id: int, text: str,
                 direct: bool, fresh: bool, may_initiate: bool = True) -> ReplyKind | None:
        chat = self.chats.get(chat_id)
        if chat is None:
            chat = self.chats[chat_id] = ChatActivity()
        limits = self.settings.initiative_for(chat_id)
        now = monotonic()
        abuse = direct and bool(_ABUSE.search(text))
        muted = chat.muted_until.get(user_id, float('-inf')) > now
        if limits.enabled and limits.chance_percent and fresh:
            chat.new_messages = min(chat.new_messages + 1, limits.min_messages)
            signals = topic_keys(text) if may_initiate and not (abuse or muted) else {}
            chat.recent.append(TopicTurn(message_id, user_id, now, signals))

        if not fresh or muted:
            return None
        if abuse:
            chat.incidents = {user: ts for user, ts in chat.incidents.items() if now - ts < ABUSE_WINDOW}
            chat.muted_until = {user: ts for user, ts in chat.muted_until.items() if ts > now}
            if user_id in chat.incidents:
                chat.muted_until[user_id] = now + MUTE_SECONDS
                del chat.incidents[user_id]
                self.forget(chat_id, user_id)
                return None
            chat.incidents[user_id] = now

        if chat.active or self.active >= MAX_ACTIVE_GROUP_REPLIES or now - chat.last_reply < GROUP_REPLY_GAP:
            return None
        if direct:
            if now - chat.last_direct.get(user_id, float('-inf')) < USER_REPLY_GAP:
                return None
            chat.last_direct = {user: ts for user, ts in chat.last_direct.items() if now - ts < USER_REPLY_GAP}
            chat.last_direct[user_id] = now
            kind = ReplyKind.BOUNDARY if abuse else ReplyKind.DIRECT
        else:
            if (not may_initiate or not limits.enabled or not limits.chance_percent
                    or self.active or chat.new_messages < limits.min_messages
                    or now - chat.last_initiative < limits.cooldown_seconds
                    or now - chat.last_check < INITIATIVE_CHECK_GAP):
                return None
            current = chat.recent[-1]
            if not self._ongoing_topic(chat.recent, current):
                return None
            chat.last_check = now
            if random.randrange(100) >= limits.chance_percent:
                return None
            kind = ReplyKind.INITIATIVE

        self._reserve(chat, kind)
        return kind

    def resume(self, chat_id: int, user_id: int, kind: ReplyKind) -> None:
        """Reuse a prior admission without counting this turn or rolling again."""
        chat = self.chats.setdefault(chat_id, ChatActivity())
        if (chat.active or self.active >= MAX_ACTIVE_GROUP_REPLIES
                or kind == ReplyKind.INITIATIVE and self.active):
            raise ReplyCapacityExceeded('Групповые ответы заняты')
        if kind != ReplyKind.INITIATIVE:
            chat.last_direct[user_id] = monotonic()
        self._reserve(chat, kind)

    def _reserve(self, chat: ChatActivity, kind: ReplyKind) -> None:
        chat.active = kind
        self.active += 1

    @staticmethod
    def _ongoing_topic(recent: deque[TopicTurn], current: TopicTurn) -> bool:
        for turn in recent:
            if (turn.user_id == current.user_id or turn.message_id >= current.message_id
                    or current.when - turn.when > TOPIC_WINDOW):
                continue
            for topic, keys in current.signals.items():
                earlier = turn.signals.get(topic)
                if earlier and keys != earlier and len(keys | earlier) >= 2:
                    return True
        return False

    def finish(self, chat_id: int) -> None:
        chat = self.chats[chat_id]
        now = monotonic()
        if chat.active == ReplyKind.INITIATIVE:
            chat.last_initiative = now
            chat.new_messages = 0
        chat.active = None
        chat.last_reply = now
        self.active -= 1
