"""Small structured logger.

The format is `LEVEL logger event key=value ...` so a log line is greppable
without a parser. Message *contents* are never logged: only user ids, event
names, counts, and token totals. See redaction rules in docs/HOW_IT_WORKS.md.
"""

from __future__ import annotations

import logging
import sys
from typing import Any


class _Formatter(logging.Formatter):
    """Render `event key=value` with values quoted only when needed."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname.lower():<7} {record.name:<16} {record.getMessage()}"
        extras = getattr(record, "context", None)
        if isinstance(extras, dict) and extras:
            pairs = " ".join(f"{k}={_fmt_value(v)}" for k, v in extras.items())
            base = f"{base} {pairs}"
        if record.exc_info:
            base = f"{base}\n{self.formatException(record.exc_info)}"
        return base


def _fmt_value(value: Any) -> str:
    text = str(value)
    if text == "" or any(ch.isspace() for ch in text):
        return f'"{text}"'
    return text


def setup_logging(level: str = "INFO") -> None:
    """Install the single stdout handler. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_Formatter())
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # python-telegram-bot logs every update at INFO, which includes message
    # text. Keep it at WARNING so message contents never reach the log file.
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, event: str, **context: Any) -> None:
    """Log a structured event.

    Keywords named in _SECRET_KEYS are replaced with a placeholder so a future
    caller cannot leak a token by passing it in as context.
    """
    safe = {k: ("<redacted>" if k.lower() in _SECRET_KEYS else v) for k, v in context.items()}
    logger.log(level, event, extra={"context": safe})


# Exact names only. A substring match would swallow legitimate metrics such as
# tokens_in / tokens_out, which are safe to log and useful for cost tracking.
_SECRET_KEYS = frozenset(
    {
        "token",
        "bot_token",
        "telegram_token",
        "api_key",
        "apikey",
        "secret",
        "password",
        "authorization",
        "cookie",
    }
)
