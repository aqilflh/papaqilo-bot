"""Config validation and the daily backup helper."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from app import backup
from app.config import ConfigError, load_config

BASE = {
    "DEEPSEEK_API_KEY": "sk-test",
    "DEEPSEEK_MODEL": "deepseek-flash",
    "TELEGRAM_BOT_TOKEN": "123:abc",
    "OWNER_TELEGRAM_ID": "100",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for key in [
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "DEEPSEEK_THINKING",
        "TELEGRAM_BOT_TOKEN",
        "OWNER_TELEGRAM_ID",
        "TZ",
        "DB_PATH",
        "FEEDS_PATH",
        "HISTORY_MESSAGES",
        "MAX_TOOL_ITERATIONS",
        "MAX_OUTPUT_TOKENS",
        "DEFAULT_DAILY_LIMIT",
        "TOOL_TIMEOUT_SECONDS",
        "REQUEST_TIMEOUT_SECONDS",
        "SHOW_THINKING",
        "LOG_LEVEL",
    ]:
        monkeypatch.delenv(key, raising=False)
    for key, value in BASE.items():
        monkeypatch.setenv(key, value)


def test_minimal_valid_config():
    cfg = load_config()
    assert cfg.deepseek_model == "deepseek-flash"
    assert cfg.deepseek_base_url == "https://api.deepseek.com"
    assert cfg.owner_telegram_id == 100
    assert cfg.default_daily_limit == 50
    assert cfg.max_tool_iterations == 6
    assert cfg.db_path == Path("./data/agent.db")


def test_base_url_can_be_overridden(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://gateway.example/v1")
    assert load_config().deepseek_base_url == "https://gateway.example/v1"


@pytest.mark.parametrize("missing", sorted(BASE))
def test_missing_required_value_is_fatal(missing):
    import os

    saved = os.environ.pop(missing)
    try:
        with pytest.raises(ConfigError, match=missing):
            load_config()
    finally:
        os.environ[missing] = saved


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_required_value_is_fatal(monkeypatch, blank):
    monkeypatch.setenv("DEEPSEEK_API_KEY", blank)
    with pytest.raises(ConfigError, match="DEEPSEEK_API_KEY"):
        load_config()


def test_owner_id_must_be_numeric(monkeypatch):
    monkeypatch.setenv("OWNER_TELEGRAM_ID", "aqil")
    with pytest.raises(ConfigError, match="OWNER_TELEGRAM_ID"):
        load_config()


def test_unknown_timezone_is_fatal(monkeypatch):
    monkeypatch.setenv("TZ", "Jakarta/Asia")
    with pytest.raises(ConfigError, match="TZ"):
        load_config()


def test_known_timezone_is_accepted(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Makassar")
    assert str(load_config().tz) == "Asia/Makassar"


def test_non_integer_is_fatal(monkeypatch):
    monkeypatch.setenv("HISTORY_MESSAGES", "banyak")
    with pytest.raises(ConfigError, match="HISTORY_MESSAGES"):
        load_config()


def test_non_numeric_is_fatal(monkeypatch):
    monkeypatch.setenv("TOOL_TIMEOUT_SECONDS", "sepuluh")
    with pytest.raises(ConfigError, match="TOOL_TIMEOUT_SECONDS"):
        load_config()


def test_non_boolean_is_fatal(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_THINKING", "maybe")
    with pytest.raises(ConfigError, match="DEEPSEEK_THINKING"):
        load_config()


@pytest.mark.parametrize(
    "raw,expected",
    [("true", True), ("1", True), ("on", True), ("false", False), ("0", False), ("no", False)],
)
def test_boolean_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("DEEPSEEK_THINKING", raw)
    assert load_config().deepseek_thinking is expected


def test_thinking_defaults_to_off(monkeypatch):
    assert load_config().deepseek_thinking is False


def test_zero_iteration_cap_is_fatal(monkeypatch):
    monkeypatch.setenv("MAX_TOOL_ITERATIONS", "0")
    with pytest.raises(ConfigError, match="MAX_TOOL_ITERATIONS"):
        load_config()


def test_negative_daily_limit_is_fatal(monkeypatch):
    monkeypatch.setenv("DEFAULT_DAILY_LIMIT", "-1")
    with pytest.raises(ConfigError, match="DEFAULT_DAILY_LIMIT"):
        load_config()


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def test_backup_due_after_3am(monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_HOUR", 3)
    assert backup.is_backup_due(datetime(2026, 9, 28, 3, 0), None) is True
    assert backup.is_backup_due(datetime(2026, 9, 28, 23, 59), None) is True


def test_backup_not_due_before_3am(monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_HOUR", 3)
    assert backup.is_backup_due(datetime(2026, 9, 28, 2, 59), None) is False


def test_backup_only_once_per_day(monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_HOUR", 3)
    last = datetime(2026, 9, 28, 3, 0)
    assert backup.is_backup_due(datetime(2026, 9, 28, 20, 0), last) is False
    assert backup.is_backup_due(datetime(2026, 9, 29, 3, 0), last) is True


def test_backup_catches_up_after_a_restart(monkeypatch):
    monkeypatch.setattr(backup, "BACKUP_HOUR", 3)
    # Was down at 03:00, came back at 11:00 with no run today.
    assert backup.is_backup_due(datetime(2026, 9, 28, 11, 0), None) is True


def test_prune_keeps_seven(tmp_path: Path):
    target_dir = tmp_path / "backups"
    target_dir.mkdir()
    for day in range(1, 13):
        (target_dir / f"agent-2026-09-{day:02d}.db").write_bytes(b"x")

    today = datetime(2026, 9, 28)
    removed = backup.prune(target_dir, keep=7, now=today)

    remaining = sorted(p.name for p in target_dir.glob("agent-*.db"))
    assert len(remaining) == 6
    assert removed


def test_prune_never_removes_todays_file(tmp_path: Path):
    target_dir = tmp_path / "backups"
    target_dir.mkdir()
    for day in range(1, 10):
        (target_dir / f"agent-2026-09-{day:02d}.db").write_bytes(b"x")

    today = datetime(2026, 9, 5)
    backup.prune(target_dir, keep=3, now=today)
    assert (target_dir / "agent-2026-09-05.db").exists()


def test_prune_on_a_missing_directory_is_a_no_op(tmp_path: Path):
    assert backup.prune(tmp_path / "nope", keep=7) == []
