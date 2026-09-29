"""Shift reminders and the schedule-import preview. Deterministic text, no LLM.

The evening-before ("eve") reminder, the day-of "starts in N minutes" reminder,
and the expiry warning all live here, driven by the per-minute tick. Delivery
is deduplicated in SQL (shift_reminders_sent), so a restart can never make a
shift reminder fire twice.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app import db, shift_parser
from app.log import get_logger, log_event
from app.scheduler import parse_hhmm
from app.tz import user_zone

log = get_logger("shifts")

#: strftime's %b is English; name the months ourselves, like tz.py does for days.
BULAN_SINGKAT = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Mei",
    "Jun",
    "Jul",
    "Agu",
    "Sep",
    "Okt",
    "Nov",
    "Des",
]

#: A preview stays valid for half an hour; after that the Simpan button expires.
PENDING_IMPORTS: dict[int, dict[str, Any]] = {}
PENDING_TTL = timedelta(minutes=30)


def fmt_day(moment: date) -> str:
    """'Kamis, 22 Okt' -- readable Indonesian date used across messages."""
    from app.tz import hari_name

    return f"{hari_name(moment)}, {moment.day} {BULAN_SINGKAT[moment.month - 1]}"


def describe_shift(row: dict[str, Any], hours: dict[str, str] | None = None) -> str:
    """'IGD malam bareng Jihan', or just 'libur'. Hours are appended if set."""
    if row["kind"] == "libur":
        return "libur"
    desc = " ".join(part for part in ((row.get("place") or "").upper(), row["kind"] or "") if part)
    if hours and row["kind"] in hours and hours[row["kind"]]:
        desc += f" ({hours[row['kind']]})"
    partners = row.get("partners")
    if partners:
        desc += f" bareng {partners}"
    return desc


def eve_message(row: dict[str, Any], tz: Any, hours: dict[str, str]) -> str:
    """The D-1 evening reminder template."""
    shift_date = date.fromisoformat(row["shift_date"])
    if row["kind"] == "libur":
        return "Besok libur."
    return f"Besok ({fmt_day(shift_date)}): {describe_shift(row, hours)}."


def before_message(row: dict[str, Any], tz: Any, hours: dict[str, str]) -> str:
    """The day-of 'starts soon' template. Requires that kind's hours."""
    shift_date = date.fromisoformat(row["shift_date"])
    start = hours.get(row["kind"], "").split("-")[0]
    desc = describe_shift(row, None)
    return f"Hari ini ({fmt_day(shift_date)}): {desc} mulai {start}."


def expiry_message(last_date: str) -> str:
    last_fmt = fmt_day(date.fromisoformat(last_date))
    return f"Jadwal tersimpan sampai {last_fmt}. Kirim jadwal baru ya."


def besok_text(user_id: int, tz: Any) -> str:
    """Answer for /besok: the shift starting tomorrow, or none."""
    tomorrow = datetime.now(tz).date() + timedelta(days=1)
    row = db.get_shift(user_id, tomorrow.isoformat())
    if row is None:
        return "Besok tidak ada jadwal."
    return eve_message(row, tz, db.get_shift_settings(user_id)["hours"])


def my_shifts_text(user_id: int, tz: Any, days: int) -> str:
    """Answer for /jadwal_saya: stored shifts from today for `days` days."""
    today = datetime.now(tz).date()
    till = today + timedelta(days=days - 1)
    rows = db.list_shifts(user_id, today.isoformat(), till.isoformat())
    if not rows:
        return f"Tidak ada jadwal {days} hari ke depan."
    hours = db.get_shift_settings(user_id)["hours"]
    lines = [
        f"- {fmt_day(date.fromisoformat(r['shift_date']))}: {describe_shift(r, hours)}"
        for r in rows
    ]
    return f"Jadwal {days} hari ({today:%d/%m} - {till:%d/%m}):\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# preview
# ---------------------------------------------------------------------------


def render_preview(parsed: shift_parser.ScheduleParse) -> str:
    """Group entries by week and report counts plus every warning line."""
    if parsed.base is None:
        title = "Pratinjau jadwal:"
    else:
        year, month = parsed.base
        title = f"Pratinjau jadwal ({month:02d}/{year}):"

    weeks: dict[tuple[int, int], list[shift_parser.ParsedLine]] = {}
    for entry in parsed.entries:
        if entry.date is None:
            continue
        weeks.setdefault(entry.date.isocalendar()[:2], []).append(entry)

    blocks = [title]
    for key in sorted(weeks):
        entries = sorted(weeks[key], key=lambda e: e.date)
        start = min(e.date for e in entries)
        week_start = start - timedelta(days=start.weekday())
        week_end = week_start + timedelta(days=6)
        blocks.append(f"Minggu {week_start:%d/%m} - {week_end:%d/%m}:")
        for entry in entries:
            mark = "⚠️" if entry.warnings else "  "
            blocks.append(f"{mark} {fmt_day(entry.date)}: {_preview_desc(entry)}")

    savable = parsed.savable
    empty = [e for e in parsed.entries if e.status == "empty"]
    unclear = [e for e in parsed.entries if e.status == "shift" and not e.is_savable]
    ignored = len(parsed.ignored) + sum(1 for e in parsed.entries if e.status == "ignored")
    blocks.append(
        f"\nSiap disimpan: {len(savable)} • belum ada jadwal: {len(empty)} "
        f"• perlu dicek: {len(unclear)} • diabaikan: {ignored}"
    )

    problem_lines = [e for e in parsed.entries if e.warnings]
    if problem_lines:
        blocks.append("\n⚠️ Perlu dicek:")
        for entry in problem_lines:
            detail = "; ".join(entry.warnings)
            blocks.append(f"- {entry.raw} — {detail}")
    return "\n".join(blocks)


def _preview_desc(entry: shift_parser.ParsedLine) -> str:
    if entry.status == "empty":
        return "belum ada jadwal"
    if entry.is_savable:
        base = (
            "libur"
            if entry.kind == "libur"
            else " ".join(part for part in ((entry.place or "").upper(), entry.kind or "") if part)
        )
        if entry.note:
            base += f" ({entry.note})"
        if entry.partners:
            base += f" bareng {', '.join(entry.partners)}"
        return base
    return entry.remainder or "?"


# ---------------------------------------------------------------------------
# import staging (preview -> Simpan button)
# ---------------------------------------------------------------------------


def stage_pending(user_id: int, parsed: shift_parser.ScheduleParse) -> None:
    PENDING_IMPORTS[user_id] = {"parsed": parsed, "at": datetime.now(UTC)}


def pending_import(user_id: int) -> shift_parser.ScheduleParse | None:
    item = PENDING_IMPORTS.get(user_id)
    if item is None:
        return None
    if datetime.now(UTC) - item["at"] > PENDING_TTL:
        PENDING_IMPORTS.pop(user_id, None)
        return None
    return item["parsed"]


def clear_pending(user_id: int) -> None:
    PENDING_IMPORTS.pop(user_id, None)


def apply_import(user_id: int, parsed: shift_parser.ScheduleParse) -> int:
    """Upsert every savable row. Returns how many shifts were stored."""
    for entry in parsed.savable:
        db.upsert_shift(
            user_id,
            entry.date.isoformat(),
            entry.kind,
            entry.place,
            entry.note,
            entry.partners,
            entry.raw,
        )
    return len(parsed.savable)


# ---------------------------------------------------------------------------
# reminder delivery (called from the per-minute tick)
# ---------------------------------------------------------------------------


async def _send(sender: Any, user_id: int, kind: str, target: str, text: str) -> bool:
    """Claim, send, release on failure. True when a message really went out."""
    if not db.claim_shift_reminder(user_id, kind, target):
        return False
    try:
        await sender.send_message(user_id, text)
    except Exception as exc:  # noqa: BLE001 - one failure must not stop the tick
        log_event(
            log,
            logging.ERROR,
            "shift_send_failed",
            user_id=user_id,
            kind=kind,
            error=type(exc).__name__,
        )
        db.release_shift_reminder(user_id, kind, target)
        return False
    log_event(log, logging.INFO, "shift_sent", user_id=user_id, kind=kind, target=target)
    return True


def _split_span(span: str) -> tuple[str, str] | None:
    """'07:00-14:00' -> ('07:00', '14:00'). None unless both sides are strict HH:MM."""
    parts = span.split("-")
    if len(parts) != 2:
        return None
    start = parse_hhmm(parts[0])
    end = parse_hhmm(parts[1])
    if start is None or end is None:
        return None
    return (f"{start[0]:02d}:{start[1]:02d}", f"{end[0]:02d}:{end[1]:02d}")


async def deliver_shift_reminders(sender: Any, now: datetime | None = None) -> int:
    """Send every shift reminder that is due right now. Returns how many sent.

    Three kinds of reminder, all deduplicated by (user_id, kind, target_date):

    * eve -- D-1 evening, when local time has reached `eve_time`
    * before -- day-of, within `before_min` minutes before the shift starts,
      and only while the start is still in the future (never after a restart)
    * expiry -- the stored schedule's last date is reaching its end
    """
    now = now or datetime.now(UTC)
    sent = 0

    for user_id in db.shift_user_ids():
        user = db.get_user(user_id)
        if user is None:
            continue
        tz = user_zone(user.get("timezone"))
        settings = db.get_shift_settings(user_id)
        local = now.astimezone(tz)
        today = local.date()
        tomorrow = today + timedelta(days=1)

        if settings["reminders_on"]:
            eve = parse_hhmm(settings["eve_time"])
            if eve is not None and local.hour * 60 + local.minute >= eve[0] * 60 + eve[1]:
                target = tomorrow.isoformat()
                row = db.get_shift(user_id, target)
                if row is not None:
                    text = eve_message(row, tz, settings["hours"])
                    if await _send(sender, user_id, "eve", target, text):
                        sent += 1

        if settings["before_min"]:
            today_row = db.get_shift(user_id, today.isoformat())
            if today_row is not None:
                span = _split_span(settings["hours"].get(today_row["kind"]) or "")
                if span is not None:
                    start_hour, start_minute = parse_hhmm(span[0])
                    start = local.replace(
                        hour=start_hour, minute=start_minute, second=0, microsecond=0
                    )
                    fire_at = start - timedelta(minutes=settings["before_min"])
                    if fire_at <= now < start:
                        text = before_message(today_row, tz, settings["hours"])
                        if await _send(sender, user_id, "before", today.isoformat(), text):
                            sent += 1
                    # now >= start: the reminder is stale after a restart; send nothing.

        last = db.last_shift_date(user_id)
        near_end = last is not None and date.fromisoformat(last) <= today + timedelta(days=2)
        if near_end and await _send(sender, user_id, "expiry", last, expiry_message(last)):
            sent += 1

    return sent
