"""Reminder scheduling: work out when a reminder is next due, and deliver it.

The recurrence maths lives here rather than in the SQL because a repeating
reminder is a calendar problem, not a storage problem: a daily 07:00 reminder
has to survive a restart, a timezone with DST, and a machine that was switched
off for three days, and it must still fire exactly once per day afterwards.
"""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app import db
from app.log import get_logger, log_event
from app.tz import hari_name, user_zone

log = get_logger("reminders")

#: How far ahead a recurring reminder can be rolled before its first delivery.
#: A user who was offline for a week gets one message on restart, then back on
#: schedule, rather than seven catch-up messages at once.
MAX_CATCHUP = timedelta(days=7)


def parse_due(at: str, tz: ZoneInfo) -> datetime:
    """Parse an ISO 8601 instant into an aware UTC datetime.

    Accepts a trailing Z, an explicit offset, or a naive string (read as local
    time in `tz`, which is what a user naming a wall-clock time means).
    """
    raw = at.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


#: %A and %B are locale-dependent and strftime has no locale argument, so they
#: render "Tuesday" in an Indonesian chat. Name the days ourselves.
def describe_when(at: datetime, tz: ZoneInfo) -> str:
    """Human-facing local time, so the user can catch a bad parse."""
    local = at.astimezone(tz)
    return f"{hari_name(local)}, {local:%d/%m/%Y %H:%M}"


REPEAT_LABEL = {
    "once": "sekali",
    "daily": "setiap hari",
    "weekly": "setiap minggu",
    "monthly": "setiap bulan",
    "yearly": "setiap tahun",
}


def format_reminder(reminder: dict[str, Any], tz: ZoneInfo) -> str:
    """One-line summary used by the bot's reply and the /reminders list."""
    due = datetime.fromisoformat(reminder["due_at"]).astimezone(tz)
    repeat = REPEAT_LABEL.get(reminder["repeat_kind"], reminder["repeat_kind"])
    return f"#{reminder['id']} - {reminder['text']} ({repeat}, {due.strftime('%d/%m %H:%M')})"


def next_occurrence(reminder: dict[str, Any], tz: ZoneInfo, now: datetime) -> datetime | None:
    """The next time this reminder should fire, or None if it is finished.

    Always returns something strictly after `now`, which is what makes a missed
    run collapse into a single delivery instead of a backlog.
    """
    kind = reminder["repeat_kind"]
    if kind == "once":
        return None

    hh, mm = (int(p) for p in reminder["local_hhmm"].split(":"))
    local_now = now.astimezone(tz)
    base = local_now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    if kind == "daily":
        candidate = base
        while candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    if kind == "weekly":
        weekday = reminder.get("weekday")
        if weekday is None:
            return None
        candidate = base
        while candidate <= local_now or candidate.weekday() != weekday:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    if kind == "monthly":
        monthday = reminder.get("monthday")
        if monthday is None:
            return None
        year, month = local_now.year, local_now.month
        # Twelve tries covers a full cycle; the spare two are slack in case the
        # requested day has just passed twice over a month boundary.
        for _ in range(14):
            day = min(int(monthday), calendar.monthrange(year, month)[1])
            candidate = datetime(year, month, day, hh, mm, tzinfo=tz)
            if candidate > local_now:
                return candidate.astimezone(UTC)
            month += 1
            if month > 12:
                month = 1
                year += 1
        return None

    if kind == "yearly":
        monthday = reminder.get("monthday")
        month = reminder.get("month")
        if monthday is None or month is None:
            return None
        year = local_now.year
        # Two tries is enough; the third is slack for a 29 February reminder
        # landing on a non-leap year.
        for _ in range(3):
            day = min(int(monthday), calendar.monthrange(year, int(month))[1])
            candidate = datetime(year, int(month), day, hh, mm, tzinfo=tz)
            if candidate > local_now:
                return candidate.astimezone(UTC)
            year += 1
        return None

    return None


async def deliver_due(sender: Any, now: datetime | None = None) -> int:
    """Send every reminder that is due. Returns how many were sent.

    Ordering matters: the claim happens in SQL, then the message goes out, then
    the recurrence advances. A send failure releases the claim so the next tick
    retries, but the recurrence is still advanced for reminders that are merely
    stale, so a machine that was off for a week does not replay it.
    """
    now = now or datetime.now(UTC)
    sent = 0

    for reminder in db.claim_due_reminders(now):
        reminder_id = int(reminder["id"])
        user_id = int(reminder["user_id"])
        user = db.get_user(user_id)
        tz = user_zone(user.get("timezone") if user else None)

        overdue = now - datetime.fromisoformat(reminder["due_at"])
        if overdue > MAX_CATCHUP:
            # Too stale to be worth sending. Retire it rather than deliver a
            # week-old "stand up in 5 minutes".
            if reminder["repeat_kind"] == "once":
                db.disable_reminder(reminder_id)
            else:
                following = next_occurrence(reminder, tz, now)
                if following is None:
                    db.disable_reminder(reminder_id)
                else:
                    db.advance_reminder(reminder_id, following)
            log_event(
                log,
                logging.INFO,
                "reminder_skipped_stale",
                user_id=user_id,
                reminder_id=reminder_id,
                overdue_days=overdue.days,
            )
            continue

        try:
            await sender.send_message(user_id, reminder_message(reminder, tz, overdue))
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the rest
            log_event(
                log,
                logging.ERROR,
                "reminder_send_failed",
                user_id=user_id,
                reminder_id=reminder_id,
                error=type(exc).__name__,
            )
            db.release_reminder_claim(reminder_id, str(reminder["last_sent_at"]))
            continue

        if reminder["repeat_kind"] == "once":
            db.disable_reminder(reminder_id)
        else:
            following = next_occurrence(reminder, tz, now)
            if following is None:
                db.disable_reminder(reminder_id)
            else:
                db.advance_reminder(reminder_id, following)

        sent += 1
        log_event(
            log,
            logging.INFO,
            "reminder_sent",
            user_id=user_id,
            reminder_id=reminder_id,
            repeat_kind=reminder["repeat_kind"],
            overdue_minutes=int(overdue.total_seconds() // 60),
        )

    return sent


def reminder_message(reminder: dict[str, Any], tz: ZoneInfo, overdue: timedelta) -> str:
    """The message body. Says how late it is so a catch-up is never confusing.

    The id is included so a follow-up like "tunda 30 menit" has something to
    point at: the row is deactivated the moment it fires, so it is no longer in
    list_reminders, and the model must not guess an id from memory.
    """
    if overdue >= timedelta(minutes=1):
        late = f" (terlambat {int(overdue.total_seconds() // 60)} menit)"
    else:
        late = ""
    return f"Pengingat{late} #{reminder['id']}: {reminder['text']}"


def list_for_user(user_id: int) -> list[dict[str, Any]]:
    return db.list_reminders(user_id)
