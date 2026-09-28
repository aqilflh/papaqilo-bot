"""The per-minute tick: decide what is due, then do it.

One job runs every 60 seconds and handles two things:

* daily recaps, per user, in that user's timezone
* the daily database backup

Both use the same ">= the target time, and not yet done today" rule, which
means a container that was down when the schedule was due catches up on restart
instead of silently skipping the day.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from app import backup as backup_mod
from app import db
from app.agent import user_zone
from app.log import get_logger, log_event
from app.recap import build_recap

log = get_logger("scheduler")

KIND_RECAP = "daily_recap"
DEFAULT_RECAP_TIME = "06:30"
TICK_SECONDS = 60

#: Strict zero-padded 24-hour clock.
HHMM_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class Sender(Protocol):
    """The slice of the bot that the scheduler needs."""

    async def send_message(self, user_id: int, text: str) -> None: ...


def parse_hhmm(value: str) -> tuple[int, int] | None:
    """'06:30' -> (6, 30). None unless the value is strictly zero-padded HH:MM.

    Strict on purpose: a schedule time is stored once and compared every minute
    for a year, so accepting '6:3' or ' 6:30 ' would only create surprises.
    """
    if not isinstance(value, str):
        return None
    match = HHMM_RE.fullmatch(value.strip())
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def is_due(schedule: dict[str, Any], tz: ZoneInfo, now: datetime) -> bool:
    """True when this schedule should fire at `now`.

    The >= comparison on minutes-since-midnight is what provides restart
    catch-up: a 06:30 recap still fires if the process only started at 09:00.
    """
    target = parse_hhmm(schedule.get("time_hhmm") or "")
    if target is None:
        return False
    local = now.astimezone(tz)
    minutes_now = local.hour * 60 + local.minute
    return minutes_now >= target[0] * 60 + target[1]


async def run_recaps(sender: Sender, agent: Any, http: Any, now: datetime | None = None) -> int:
    """Deliver every due recap. Returns how many were sent."""
    now = now or datetime.now(ZoneInfo("UTC"))
    sent = 0

    for schedule in db.list_due_schedules(KIND_RECAP):
        user_id = int(schedule["user_id"])
        user = db.get_user(user_id)
        if user is None:
            continue
        tz = user_zone(user.get("timezone"))
        today = now.astimezone(tz).date().isoformat()

        if not is_due(schedule, tz, now):
            continue
        if schedule.get("last_run_date") == today:
            continue

        # Claim before sending so a second tick cannot send a duplicate.
        if not db.claim_schedule_run(user_id, KIND_RECAP, today):
            continue

        try:
            recap = await build_recap(agent, user_id, http)
        except Exception as exc:  # noqa: BLE001 - one bad recap must not stop the tick
            log_event(log, logging.ERROR, "recap_failed", user_id=user_id, error=type(exc).__name__)
            # Release the claim: a transient LLM or network error should be
            # retried on the next tick, not silently skipped until tomorrow.
            db.release_schedule_claim(user_id, KIND_RECAP, today)
            continue

        try:
            await sender.send_message(user_id, recap.text)
        except Exception as exc:  # noqa: BLE001
            log_event(
                log,
                logging.ERROR,
                "recap_send_failed",
                user_id=user_id,
                error=type(exc).__name__,
            )
            # Release the claim so the next tick retries.
            db.release_schedule_claim(user_id, KIND_RECAP, today)
            continue

        # Scheduled runs count toward cost but never toward the daily limit.
        day = db.local_day(tz, now=now.astimezone(tz))
        db.record_usage(
            user_id, day, msg_count=0, tokens_in=recap.tokens_in, tokens_out=recap.tokens_out
        )
        sent += 1
        log_event(
            log,
            logging.INFO,
            "recap_sent",
            user_id=user_id,
            date=today,
            missing=len(recap.missing),
        )

    return sent


def run_backup_if_due(
    db_path: Any, now: datetime | None = None, last_run: datetime | None = None
) -> bool:
    """Back up if due. Returns True only if a backup file was actually written.

    The distinction matters: a failed backup that reported success would mark
    the day done and leave the database unprotected until the next attempt a
    full 24 hours later.
    """
    now = now or datetime.now()
    if not backup_mod.is_backup_due(now, last_run):
        return False
    return backup_mod.run_backup(db_path, now=now) is not None
