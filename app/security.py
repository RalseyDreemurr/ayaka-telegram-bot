"""Validation and protection of sensitive values."""
import re
from urllib.parse import unquote, urlsplit

MAX_ID = 2**63 - 1
REDACTED = '[скрыто]'
_CREDENTIAL = re.compile(
    r'-----BEGIN [^\r\n-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^\r\n-]*PRIVATE KEY-----|\Z)'
    r'|(?im:^[ \t]*(?:export[ \t]+)?[A-Z_][A-Z0-9_]{1,63}[ \t]*=[^\r\n]*)'
    r'|(?i:\b(?:парол[ьяю]|токен|секрет|password|passwd|api[_ -]?key|access[_ -]?token|client[_ -]?secret)'
    r'["\x27]?\s*[:=]\s*[^\r\n]+)'
    r'|(?i:\bBearer[ \t]+[^\s,;]+)'
    r'|(?i:\b(?:https?|socks5h?)://[^\s/@]+:[^\s/@]+@[^\s]+)'
    r'|(?<!\w)[0-9]{5,}:[A-Za-z0-9_-]{12,}(?!\w)'
    r'|(?<!\w)eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?!\w)'
    r'|(?<!\w)(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16})(?!\w)'
)
_FRAMING = re.compile(r'<\|[^\r\n<>]{0,64}\|>|\[/?INST\]|<</?SYS>>|</?s>', re.I)


def valid_id(value: object, *, positive: bool = False) -> bool:
    if type(value) is not int:
        return False
    lower = 1 if positive else -(MAX_ID + 1)
    return lower <= value <= MAX_ID and value != 0


def parse_id(text: str, *, positive: bool = False) -> int | None:
    if not re.fullmatch(r'-?[0-9]{1,19}', text):
        return None
    value = int(text)
    return value if valid_id(value, positive=positive) else None


class SecretGuard:
    def __init__(self, settings):
        known = {settings.token}
        for url in (settings.telegram_proxy_url, settings.ollama_base_url):
            if not url:
                continue
            try:
                parsed = urlsplit(url)
                if parsed.password:
                    known.update((url, parsed.password, unquote(parsed.password)))
            except ValueError:
                continue
        self.known = tuple(sorted(filter(None, known), key=len, reverse=True))

    def contains(self, text: str) -> bool:
        return any(value in text for value in self.known) or _CREDENTIAL.search(text) is not None

    def redact(self, text: str) -> str:
        # Withhold the whole field, including unlabelled continuation lines.
        return REDACTED if self.contains(text) else text


def model_data(text: str) -> str:
    return _FRAMING.sub('[служебная метка скрыта]', text)
