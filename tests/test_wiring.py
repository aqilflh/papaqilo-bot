"""The application must actually have handlers attached.

Regression guard for a bug that made the bot look healthy while answering
nobody: `post_init` is only invoked by `run_polling()`, and this app drives
the lifecycle by hand, so `bot.register` was never called. The poller kept
consuming updates and dispatched every one of them to an empty handler list.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

os.environ.setdefault("DEEPSEEK_API_KEY", "test-key")
os.environ.setdefault("DEEPSEEK_MODEL", "deepseek-flash")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:test")
os.environ.setdefault("OWNER_TELEGRAM_ID", "100")

from app import main as main_mod  # noqa: E402
from app.config import load_config  # noqa: E402


def _teardown(http) -> None:
    """Close what build() opened, without initializing the telegram Application.

    Application.initialize() would hit the network, so the Application is left
    alone; only the httpx client and the DB connection need closing.
    """
    asyncio.run(http.aclose())
    from app import db

    db.close()


@pytest.fixture()
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "wiring.db"))
    monkeypatch.setenv("FEEDS_PATH", "feeds.json")
    return load_config()


def test_build_registers_handlers(config):
    """A bot with zero handlers is the failure this test exists to prevent."""
    application, bot, http = main_mod.build(config)
    try:
        registered = sum(len(handlers) for handlers in application.handlers.values())
        assert registered > 0, "no handlers attached: the bot would ignore every message"

        # Every command in the help text must be routable.
        callbacks = {
            getattr(handler.callback, "__name__", "")
            for handlers in application.handlers.values()
            for handler in handlers
        }
        for name in (
            "cmd_start",
            "cmd_help",
            "cmd_city",
            "cmd_recap_on",
            "cmd_recap_off",
            "cmd_recap_now",
            "cmd_notes",
            "cmd_usage",
            "cmd_forget",
            "cmd_check_server",
            "cmd_adduser",
            "cmd_removeuser",
            "cmd_users",
            "on_text",
            "on_voice",
        ):
            assert name in callbacks, f"{name} is not registered"

        # add_error_handler keys the dict by the callback, not the value.
        assert bot.on_error in application.error_handlers

        # The catch-all text handler must be in the last group, or it would
        # swallow commands before the command handlers see them.
        groups = sorted(application.handlers)
        assert groups[-1] == 0, f"catch-all handler is in group {groups[-1]}, expected group 0"
    finally:
        _teardown(http)


def test_post_init_is_not_used(config):
    """Guard the root cause: post_init never fires outside run_polling()."""
    application, bot, http = main_mod.build(config)
    try:
        assert application.post_init is None
        # register() is what actually wires the handlers.
        assert bot.application is application
    finally:
        _teardown(http)


def test_feeds_file_is_present():
    assert Path("feeds.json").is_file()
