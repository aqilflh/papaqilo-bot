"""Entrypoint: load config, open the database, build the bot, start polling.

Long polling, not a webhook. That means no TLS certificate, no domain, and no
open port on the server: the only outbound connections are to api.telegram.org,
api.deepseek.com, and the two public data APIs in app/tools.py.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from contextlib import suppress
from datetime import datetime

import httpx
from telegram.ext import Application

from app import db
from app.agent import Agent
from app.bot import Bot
from app.config import Config, ConfigError, load_config
from app.log import get_logger, log_event, setup_logging
from app.scheduler import TICK_SECONDS, run_backup_if_due, run_recaps
from app.tools import load_feeds

log = get_logger("main")

#: One shared pool for the whole process; tools and the recap both use it.
HTTP_TIMEOUT = httpx.Timeout(15.0, connect=8.0)


def ensure_owner(config: Config) -> None:
    """Make sure the owner row exists at every startup."""
    db.upsert_user(
        config.owner_telegram_id,
        "Owner",
        role="owner",
        timezone=str(config.tz),
        daily_limit=config.default_daily_limit,
    )


async def tick(bot: Bot) -> None:
    """One 60-second scheduler tick: recaps first, then the daily backup."""
    now = datetime.now(bot.config.tz)
    try:
        sent = await run_recaps(bot, bot.agent, bot.http, now=now)
        if sent:
            log_event(log, logging.INFO, "tick_recaps", sent=sent)
    except Exception as exc:  # noqa: BLE001 - the job must survive anything
        log_event(log, logging.ERROR, "tick_recaps_failed", error=type(exc).__name__)

    try:
        if run_backup_if_due(bot.config.db_path, now=now, last_run=bot.last_backup):
            bot.last_backup = now
    except Exception as exc:  # noqa: BLE001
        log_event(log, logging.ERROR, "tick_backup_failed", error=type(exc).__name__)


def build(config: Config) -> tuple[Application, Bot, httpx.AsyncClient]:
    """Assemble the application without starting it, so tests can reuse this."""
    db.connect(config.db_path)
    ensure_owner(config)

    http = httpx.AsyncClient(
        timeout=HTTP_TIMEOUT,
        headers={"User-Agent": "tg-agent/0.1"},
        follow_redirects=True,
    )
    feeds = load_feeds(config.feeds_path)
    log_event(log, logging.INFO, "feeds_loaded", count=len(feeds))

    agent = Agent(config, http, feeds)
    bot = Bot(config, agent, http)

    application = Application.builder().token(config.telegram_bot_token).build()
    # Register handlers here, not via post_init. PTB only invokes post_init from
    # inside run_polling(), and this app drives the lifecycle by hand
    # (initialize + start + updater.start_polling) so it can handle SIGTERM
    # cleanly. With post_init, handlers were never registered: the poller
    # consumed every update and dispatched it to nothing, so the bot answered
    # nobody while looking perfectly healthy.
    bot.register(application)
    return application, bot, http


async def run(config: Config) -> None:
    application, bot, http = build(config)

    # The per-minute job. first=6 avoids colliding with every other job that
    # fires on the minute boundary.
    application.job_queue.run_repeating(
        lambda ctx: asyncio.create_task(tick(bot)),
        interval=TICK_SECONDS,
        first=6,
        name="scheduler-tick",
    )

    # Log the bot username and model, never the token.
    me = await application.bot.get_me()
    log_event(
        log,
        logging.INFO,
        "bot_started",
        username=me.username,
        model=config.deepseek_model,
        thinking=config.deepseek_thinking,
        owner_id=config.owner_telegram_id,
        # Logged so a missing registration is visible in the boot log instead
        # of showing up as a bot that silently ignores every message.
        handlers=sum(len(handlers) for handlers in application.handlers.values()),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        log_event(log, logging.INFO, "shutdown_requested")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        # add_signal_handler is POSIX-only; without it KeyboardInterrupt still works.
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_stop)

    try:
        await application.initialize()
        await application.start()
        await application.updater.start_polling(drop_pending_updates=True)
        log_event(log, logging.INFO, "polling_started")

        # Telegram kills the poller, rather than the process, when a second
        # instance polls the same token. The container would sit there "Up"
        # and deaf, which is indistinguishable from the bot ignoring messages.
        # Watch the poller task and exit so restart policy brings up one clean
        # instance instead of leaving a zombie holding the token.
        async def _watch_poller() -> None:
            while not stop.is_set():
                await asyncio.sleep(15)
                if application.updater._polling_task is None:
                    break
                if application.updater._polling_task.done():
                    exc = application.updater._polling_task.exception()
                    log_event(
                        log,
                        logging.CRITICAL,
                        "poller_died",
                        error=type(exc).__name__ if exc else "cancelled",
                    )
                    stop.set()
                    return

        watcher = asyncio.create_task(_watch_poller())
        try:
            await stop.wait()
        finally:
            watcher.cancel()
    finally:
        log_event(log, logging.INFO, "shutting_down")
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        await http.aclose()
        db.close()
        log_event(log, logging.INFO, "stopped")


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        # Deliberately not using the logger: logging is not configured yet.
        print(f"konfigurasi tidak valid: {exc}", file=sys.stderr)
        return 2

    setup_logging(config.log_level)
    log_event(
        log,
        logging.INFO,
        "config_loaded",
        tz=str(config.tz),
        db=str(config.db_path),
        model=config.deepseek_model,
    )

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
