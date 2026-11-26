"""Журналирование: токены и полные тексты клиентских сообщений в логи не пишем."""
from __future__ import annotations

import logging
import re

_SECRET_PATTERNS = [
    re.compile(r"vk1\.a\.[A-Za-z0-9_-]+"),
    re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}"),  # токен Telegram-бота
]


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in _SECRET_PATTERNS:
            msg = pat.sub("***", msg)
        record.msg = msg
        record.args = ()
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    handler.addFilter(SecretFilter())
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers = [handler]
    # Отрезаем подробный http-лог с URL, где могли бы быть параметры
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.WARNING)


def clip(text: str | None, limit: int = 60) -> str:
    """Укороченный фрагмент текста для логов (не полный текст клиента)."""
    if not text:
        return ""
    text = text.replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")
