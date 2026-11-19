"""Keep API keys and bot tokens out of log lines.

httpx logs full request URLs at INFO, which would print the Helius key and the
Telegram token. Those loggers are pinned to WARNING, and a filter redacts the
values if something logs them anyway.
"""
from __future__ import annotations

import logging
import re

_API_KEY = re.compile(r"(api-key=)[^&\s\"']+", re.I)
_BOT = re.compile(r"(bot)\d{6,12}:[A-Za-z0-9_-]{10,}")
_LOGGERS = ("httpx", "httpcore", "websockets", "urllib3")


def redact(text: str) -> str:
    text = _API_KEY.sub(r"\1REDACTED", text)
    text = _BOT.sub(r"\1REDACTED", text)
    return text


def _redact_obj(value):
    if isinstance(value, str):
        return redact(value)
    try:
        text = str(value)
    except Exception:
        return value
    cleaned = redact(text)
    return cleaned if cleaned != text else value


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact_obj(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_obj(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: _redact_obj(v) for k, v in record.args.items()}
        return True


def install_redaction() -> None:
    filt = RedactFilter()
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        if not any(isinstance(existing, RedactFilter) for existing in logger.filters):
            logger.addFilter(filt)
    root = logging.getLogger()
    if not any(isinstance(existing, RedactFilter) for existing in root.filters):
        root.addFilter(filt)
