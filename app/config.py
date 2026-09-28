"""Environment parsing and validation.

Every setting is read once at startup. A missing or malformed value raises
ConfigError immediately, so a misconfigured deployment fails loudly instead of
crashing later inside a handler.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or unusable."""


def _require(name: str) -> str:
    """Read a setting that has no sensible default."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required but was empty or not set")
    return value


def _str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean like 'true' or 'false', got {raw!r}")


@dataclass(frozen=True, slots=True)
class Config:
    """Validated settings for one process run."""

    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_thinking: bool
    telegram_bot_token: str
    owner_telegram_id: int
    tz: ZoneInfo
    db_path: Path
    feeds_path: Path
    history_messages: int
    max_tool_iterations: int
    max_output_tokens: int
    default_daily_limit: int
    tool_timeout: float
    request_timeout: float
    show_thinking: bool
    log_level: str


def load_config() -> Config:
    """Build a Config from the environment, or raise ConfigError."""
    tz_name = _str("TZ", "Asia/Jakarta")
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"TZ is not a known IANA timezone: {tz_name!r}") from exc

    owner_raw = _require("OWNER_TELEGRAM_ID")
    try:
        owner_id = int(owner_raw)
    except ValueError as exc:
        raise ConfigError(
            f"OWNER_TELEGRAM_ID must be a numeric Telegram user id, got {owner_raw!r}"
        ) from exc

    cfg = Config(
        deepseek_api_key=_require("DEEPSEEK_API_KEY"),
        deepseek_base_url=_str("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        deepseek_model=_str("DEEPSEEK_MODEL", "deepseek-flash"),
        deepseek_thinking=_bool("DEEPSEEK_THINKING", False),
        telegram_bot_token=_require("TELEGRAM_BOT_TOKEN"),
        owner_telegram_id=owner_id,
        tz=tz,
        db_path=Path(_str("DB_PATH", "./data/agent.db")),
        feeds_path=Path(_str("FEEDS_PATH", "./feeds.json")),
        history_messages=_int("HISTORY_MESSAGES", 20),
        max_tool_iterations=_int("MAX_TOOL_ITERATIONS", 6),
        max_output_tokens=_int("MAX_OUTPUT_TOKENS", 1200),
        default_daily_limit=_int("DEFAULT_DAILY_LIMIT", 50),
        tool_timeout=_float("TOOL_TIMEOUT_SECONDS", 10.0),
        request_timeout=_float("REQUEST_TIMEOUT_SECONDS", 60.0),
        show_thinking=_bool("SHOW_THINKING", False),
        log_level=_str("LOG_LEVEL", "INFO").upper(),
    )

    if cfg.max_tool_iterations < 1:
        raise ConfigError("MAX_TOOL_ITERATIONS must be at least 1")
    if cfg.max_output_tokens < 1:
        raise ConfigError("MAX_OUTPUT_TOKENS must be at least 1")
    if cfg.default_daily_limit < 1:
        raise ConfigError("DEFAULT_DAILY_LIMIT must be at least 1")
    return cfg
