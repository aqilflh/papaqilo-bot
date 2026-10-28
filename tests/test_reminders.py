"""Reminders: recurrence maths, catch-up, and user isolation.

The interesting cases are all about time passing without the process running:
a daily reminder must fire once after a restart, not once per missed day.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.reminders import (
    MAX_CATCHUP,
    deliver_due,
    describe_when,
    format_reminder,
    next_occurrence,
    parse_due,
)

WIB = ZoneInfo("Asia/Jakarta")
OWNER = 1001
OTHER = 1002


class FakeSender:
    """Records what would have been sent to Telegram."""

    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, user_id: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((user_id, text))


@pytest.fixture(autouse=True)
def _clean_db(tmp_path):
    db.connect(tmp_path / "test.db")
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta")
    db.upsert_user(OTHER, "Other", role="user", timezone="Asia/Jakarta")
    yield


def _once(text: str, due: datetime, hhmm: str | None = None) -> int:
    return db.add_reminder(OWNER, text, "once", due, hhmm or due.astimezone(WIB).strftime("%H:%M"))


# --------------------------------------------------------------- parse_due


def test_parse_due_naive_is_local_time():
    """'2026-09-29T09:00' with no offset means 09:00 in the user's zone."""
    got = parse_due("2026-09-29T09:00", WIB)
    assert got == datetime(2026, 9, 29, 2, 0, tzinfo=UTC)
    assert got.tzinfo is UTC


def test_parse_due_accepts_trailing_z():
    assert parse_due("2026-09-29T02:00Z", WIB) == datetime(2026, 9, 29, 2, 0, tzinfo=UTC)


def test_parse_due_accepts_explicit_offset():
    assert parse_due("2026-09-29T09:00+07:00", WIB) == datetime(2026, 9, 29, 2, 0, tzinfo=UTC)


def test_parse_due_rejects_garbage():
    with pytest.raises(ValueError):
        parse_due("besok pagi", WIB)


# ---------------------------------------------------------- next_occurrence


def test_once_has_no_next_occurrence():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    row = {"repeat_kind": "once", "local_hhmm": "09:00", "weekday": None}
    assert next_occurrence(row, WIB, now) is None


def test_daily_rolls_to_tomorrow_when_time_already_passed():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    row = {"repeat_kind": "daily", "local_hhmm": "09:00", "weekday": None}
    got = next_occurrence(row, WIB, now)
    assert got.astimezone(WIB) == datetime(2026, 9, 29, 9, 0, tzinfo=WIB)


def test_daily_rolls_forward_even_if_called_at_the_exact_minute():
    """Firing at 09:00 must schedule the next one for tomorrow, not today."""
    now = datetime(2026, 9, 28, 9, 0, tzinfo=WIB)
    row = {"repeat_kind": "daily", "local_hhmm": "09:00", "weekday": None}
    assert next_occurrence(row, WIB, now).astimezone(WIB) == datetime(2026, 9, 29, 9, 0, tzinfo=WIB)


def test_weekly_finds_the_next_matching_weekday():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)  # Monday
    row = {"repeat_kind": "weekly", "local_hhmm": "08:00", "weekday": 4}  # Friday
    got = next_occurrence(row, WIB, now)
    assert got.astimezone(WIB).weekday() == 4
    assert got.astimezone(WIB) == datetime(2026, 10, 2, 8, 0, tzinfo=WIB)


def test_weekly_without_weekday_gives_up_instead_of_looping():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    row = {"repeat_kind": "weekly", "local_hhmm": "08:00", "weekday": None}
    assert next_occurrence(row, WIB, now) is None


def test_weekly_goes_to_next_week_when_it_fires_on_the_matching_day():
    """Fired at 08:00 on Monday, so the next one is a week later, not today."""
    now = datetime(2026, 9, 28, 8, 0, tzinfo=WIB)  # Monday
    row = {"repeat_kind": "weekly", "local_hhmm": "08:00", "weekday": 0}
    got = next_occurrence(row, WIB, now)
    assert got.astimezone(WIB) == datetime(2026, 10, 5, 8, 0, tzinfo=WIB)


def _monthly(hhmm: str, monthday: int | None) -> dict:
    return {"repeat_kind": "monthly", "local_hhmm": hhmm, "weekday": None, "monthday": monthday}


def test_monthly_moves_to_the_requested_day_of_the_next_month():
    """'Tanggal 1' means the 1st, even if the user set it up in late September."""
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    got = next_occurrence(_monthly("09:00", 1), WIB, now)
    assert got.astimezone(WIB) == datetime(2026, 10, 1, 9, 0, tzinfo=WIB)


def test_monthly_rolls_into_the_next_year_after_december():
    now = datetime(2026, 12, 2, 10, 0, tzinfo=WIB)
    got = next_occurrence(_monthly("09:00", 1), WIB, now)
    assert got.astimezone(WIB) == datetime(2027, 1, 1, 9, 0, tzinfo=WIB)


def test_monthly_clamps_the_31st_to_the_last_day_of_a_short_month():
    now = datetime(2027, 1, 31, 10, 0, tzinfo=WIB)
    got = next_occurrence(_monthly("09:00", 31), WIB, now)
    assert got.astimezone(WIB) == datetime(2027, 2, 28, 9, 0, tzinfo=WIB)


def test_monthly_returns_to_the_31st_after_a_clamped_month():
    """February must not ratchet a 'tanggal 31' reminder onto the 28th for good."""
    now = datetime(2027, 2, 28, 10, 0, tzinfo=WIB)
    got = next_occurrence(_monthly("09:00", 31), WIB, now)
    assert got.astimezone(WIB) == datetime(2027, 3, 31, 9, 0, tzinfo=WIB)


def test_monthly_without_monthday_gives_up_instead_of_looping():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    assert next_occurrence(_monthly("09:00", None), WIB, now) is None


# ------------------------------------------------------------- claim/dedup


def test_claim_marks_last_sent_so_a_second_claim_returns_nothing():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("minum obat", now - timedelta(minutes=1))
    assert len(db.claim_due_reminders(now)) == 1
    assert db.claim_due_reminders(now) == []


def test_claim_returns_the_stamp_it_wrote():
    """release_reminder_claim has to match, so the stamp must be the new one."""
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("x", now - timedelta(minutes=1))
    claimed = db.claim_due_reminders(now)[0]
    db.release_reminder_claim(reminder_id, claimed["last_sent_at"])
    assert len(db.claim_due_reminders(now)) == 1, "claim was not actually released"


def test_future_reminder_is_not_claimed():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("besok", now + timedelta(minutes=5))
    assert db.claim_due_reminders(now) == []


# ------------------------------------------------------------------ deliver


def test_due_once_is_sent_then_deactivated():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("kirim laporan", now - timedelta(minutes=1))
    sender = FakeSender()

    assert asyncio.run(deliver_due(sender, now=now)) == 1
    assert sender.sent[0][0] == OWNER
    assert "kirim laporan" in sender.sent[0][1]
    assert db.list_reminders(OWNER) == []


def test_deliver_is_not_repeated_on_the_next_tick():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("kirim laporan", now - timedelta(minutes=1))
    sender = FakeSender()

    asyncio.run(deliver_due(sender, now=now))
    later = now + timedelta(minutes=1)
    assert asyncio.run(deliver_due(sender, now=later)) == 0
    assert len(sender.sent) == 1


def test_daily_advances_to_tomorrow_and_sends_again_next_day():
    first = datetime(2026, 9, 28, 9, 0, tzinfo=WIB)
    db.add_reminder(OWNER, "sarapan", "daily", first, "09:00")
    sender = FakeSender()

    assert asyncio.run(deliver_due(sender, now=first)) == 1
    pending = db.list_reminders(OWNER)
    assert datetime.fromisoformat(pending[0]["due_at"]).astimezone(WIB) == datetime(
        2026, 9, 29, 9, 0, tzinfo=WIB
    )

    second = first + timedelta(days=1)
    assert asyncio.run(deliver_due(sender, now=second)) == 1
    pending = db.list_reminders(OWNER)
    assert datetime.fromisoformat(pending[0]["due_at"]).astimezone(WIB) == datetime(
        2026, 9, 30, 9, 0, tzinfo=WIB
    )
    assert len(pending) == 1
    assert len(sender.sent) == 2


def test_deliver_advances_a_monthly_reminder_to_the_next_month():
    first = datetime(2026, 10, 1, 9, 0, tzinfo=WIB)
    db.add_reminder(OWNER, "jalankan prepaid switching", "monthly", first, "09:00", None, 1)
    sender = FakeSender()

    assert asyncio.run(deliver_due(sender, now=first)) == 1
    pending = db.list_reminders(OWNER)
    assert len(pending) == 1
    assert pending[0]["monthday"] == 1
    assert datetime.fromisoformat(pending[0]["due_at"]).astimezone(WIB) == datetime(
        2026, 11, 1, 9, 0, tzinfo=WIB
    )


def test_send_failure_releases_the_claim_for_retry():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("kirim laporan", now - timedelta(minutes=1))

    assert asyncio.run(deliver_due(FakeSender(fail=True), now=now)) == 0
    good = FakeSender()
    assert asyncio.run(deliver_due(good, now=now + timedelta(minutes=1))) == 1
    assert len(good.sent) == 1


def test_a_failed_send_does_not_stop_other_reminders():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("yang pertama", now - timedelta(minutes=2))
    _once("yang kedua", now - timedelta(minutes=1))
    # Both go to OWNER, so make the sender fail only for the first call.
    sender = FakeSender()
    original = sender.send_message
    calls = {"n": 0}

    async def flaky(user_id: int, text: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await original(user_id, text)

    sender.send_message = flaky  # type: ignore[method-assign]
    assert asyncio.run(deliver_due(sender, now=now)) == 1
    assert "yang kedua" in sender.sent[0][1]


def test_catchup_after_downtime_sends_one_message_not_a_backlog():
    """A daily reminder missed for three days fires once, then reschedules."""
    missed = datetime(2026, 9, 25, 9, 0, tzinfo=WIB)
    db.add_reminder(OWNER, "minum obat", "daily", missed, "09:00")
    restart = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    sender = FakeSender()

    assert asyncio.run(deliver_due(sender, now=restart)) == 1
    assert len(sender.sent) == 1, "a three-day backlog must not become three messages"

    pending = db.list_reminders(OWNER)
    assert datetime.fromisoformat(pending[0]["due_at"]).astimezone(WIB) == datetime(
        2026, 9, 29, 9, 0, tzinfo=WIB
    )


def test_a_very_stale_once_reminder_is_dropped_not_delivered():
    """Better silent than a week-old 'go outside in 5 minutes'."""
    ancient = datetime.now(UTC) - (MAX_CATCHUP + timedelta(days=1))
    _once("duluan banget", ancient)
    sender = FakeSender()

    assert asyncio.run(deliver_due(sender, now=datetime.now(UTC))) == 0
    assert sender.sent == []
    assert db.list_reminders(OWNER) == []


def test_a_very_stale_daily_reminder_is_reshaped_not_dropped():
    ancient = datetime.now(UTC) - (MAX_CATCHUP + timedelta(days=1))
    db.add_reminder(OWNER, "minum obat", "daily", ancient, "09:00")
    sender = FakeSender()

    assert asyncio.run(deliver_due(sender, now=datetime.now(UTC))) == 0
    pending = db.list_reminders(OWNER)
    assert len(pending) == 1, "a recurring reminder must survive a long outage"
    assert datetime.fromisoformat(pending[0]["due_at"]) > datetime.now(UTC)


def test_overdue_message_says_how_late_it_is():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("telepon ibu", now - timedelta(minutes=25))
    sender = FakeSender()
    asyncio.run(deliver_due(sender, now=now))
    assert "terlambat 25 menit" in sender.sent[0][1]


def test_punctual_message_has_no_late_claim():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("kirim laporan", now - timedelta(seconds=10))
    sender = FakeSender()
    asyncio.run(deliver_due(sender, now=now))
    assert "terlambat" not in sender.sent[0][1]


# ------------------------------------------------------------------ scoping


def test_users_cannot_see_or_cancel_each_others_reminders():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("punya owner", now - timedelta(minutes=1))
    assert db.disable_reminder(reminder_id, OTHER) is False
    assert db.get_reminder(reminder_id, OTHER) is None
    assert db.get_reminder(reminder_id, OWNER) is not None
    # Still deliverable, since the stranger did not manage to cancel it.
    sender = FakeSender()
    asyncio.run(deliver_due(sender, now=now))
    assert sender.sent[0][0] == OWNER


def test_disabled_reminder_is_never_delivered():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("batal", now - timedelta(minutes=1))
    db.disable_reminder(reminder_id, OWNER)
    sender = FakeSender()
    assert asyncio.run(deliver_due(sender, now=now)) == 0
    assert sender.sent == []


def test_bulk_disable_only_touches_the_requesting_user():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("bungkus", now + timedelta(days=1))
    _once("sarapan", now + timedelta(days=2))
    _once("punya yang lain", now + timedelta(days=3))
    db.add_reminder(OTHER, "jangan tersentuh", "once", now + timedelta(days=4), "10:00")

    assert db.disable_all_reminders(OWNER) == 3
    assert db.list_reminders(OWNER) == []
    assert len(db.list_reminders(OTHER)) == 1


def test_bulk_disable_reports_zero_when_nothing_active():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    _once("sudah selesai", now + timedelta(days=1))
    db.disable_all_reminders(OWNER)
    assert db.disable_all_reminders(OWNER) == 0


def test_bulk_disable_does_not_resurrect_an_inactive_reminder():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("lama", now + timedelta(days=1))
    db.disable_reminder(reminder_id, OWNER)
    assert db.disable_all_reminders(OWNER) == 0


def test_add_reminder_rejects_an_unknown_repeat_kind():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    with pytest.raises(ValueError):
        db.add_reminder(OWNER, "x", "fortnightly", now, "10:00")


def test_add_reminder_rejects_an_impossible_monthday():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    with pytest.raises(ValueError):
        db.add_reminder(OWNER, "x", "monthly", now, "10:00", None, 32)


# ----------------------------------------------------------------- display


def test_describe_when_uses_the_users_zone_and_a_numeric_date():
    at = datetime(2026, 9, 29, 9, 0, tzinfo=WIB)
    assert describe_when(at, WIB) == "Selasa, 29/09/2026 09:00"


def test_format_reminder_labels_the_repeat():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = db.add_reminder(OWNER, "kopi", "daily", now + timedelta(hours=2), "12:00")
    row = db.get_reminder(reminder_id, OWNER)
    assert "setiap hari" in format_reminder(row, WIB)
    assert "kopi" in format_reminder(row, WIB)


def test_format_reminder_labels_a_monthly_reminder():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = db.add_reminder(
        OWNER, "prepaid", "monthly", now + timedelta(days=3), "09:00", None, 1
    )
    row = db.get_reminder(reminder_id, OWNER)
    assert "setiap bulan" in format_reminder(row, WIB)


# ------------------------------------------------------------ yearly


def _yearly(hhmm: str, monthday: int | None, month: int | None) -> dict:
    return {
        "repeat_kind": "yearly",
        "local_hhmm": hhmm,
        "weekday": None,
        "monthday": monthday,
        "month": month,
    }


def test_yearly_moves_to_next_year_when_this_years_date_has_passed():
    """A tax reminder set for 1 February must not fire again until next February."""
    now = datetime(2027, 2, 2, 10, 0, tzinfo=WIB)
    got = next_occurrence(_yearly("09:00", 1, 2), WIB, now)
    assert got.astimezone(WIB) == datetime(2028, 2, 1, 9, 0, tzinfo=WIB)


def test_yearly_stays_in_the_same_year_when_the_date_is_still_ahead():
    now = datetime(2027, 1, 15, 10, 0, tzinfo=WIB)
    got = next_occurrence(_yearly("09:00", 1, 2), WIB, now)
    assert got.astimezone(WIB) == datetime(2027, 2, 1, 9, 0, tzinfo=WIB)


def test_yearly_clamps_29_february_on_a_non_leap_year():
    """Same rule as monthly: clamp to the last valid day, never skip a year."""
    now = datetime(2028, 3, 1, 10, 0, tzinfo=WIB)
    got = next_occurrence(_yearly("09:00", 29, 2), WIB, now)
    assert got.astimezone(WIB) == datetime(2029, 2, 28, 9, 0, tzinfo=WIB)


def test_yearly_returns_to_29_february_in_the_next_leap_year():
    """The clamp must not ratchet a 29 February reminder onto the 28th for good."""
    now = datetime(2031, 3, 1, 10, 0, tzinfo=WIB)
    got = next_occurrence(_yearly("09:00", 29, 2), WIB, now)
    assert got.astimezone(WIB) == datetime(2032, 2, 29, 9, 0, tzinfo=WIB)


def test_yearly_without_month_gives_up_instead_of_looping():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    assert next_occurrence(_yearly("09:00", 1, None), WIB, now) is None


def test_deliver_advances_a_yearly_reminder_to_next_year():
    first = datetime(2026, 11, 30, 9, 0, tzinfo=WIB)
    db.add_reminder(OWNER, "bayar pajak", "yearly", first, "09:00", None, 30, 11)
    sender = FakeSender()

    assert asyncio.run(deliver_due(sender, now=first)) == 1
    pending = db.list_reminders(OWNER)
    assert len(pending) == 1
    assert pending[0]["monthday"] == 30
    assert pending[0]["month"] == 11
    assert datetime.fromisoformat(pending[0]["due_at"]).astimezone(WIB) == datetime(
        2027, 11, 30, 9, 0, tzinfo=WIB
    )


def test_delivered_message_carries_the_id_so_it_can_be_snoozed():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("telepon ibu", now - timedelta(minutes=1))
    sender = FakeSender()
    asyncio.run(deliver_due(sender, now=now))
    assert f"#{reminder_id}" in sender.sent[0][1]


# ------------------------------------------------------------------ editing


def test_update_reminder_reactivates_a_reminder_that_already_fired():
    """Snoozing happens after delivery, when the row is already inactive."""
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("telepon ibu", now - timedelta(minutes=1))
    asyncio.run(deliver_due(FakeSender(), now=now))
    assert db.list_reminders(OWNER) == []

    later = now + timedelta(minutes=30)
    assert db.update_reminder(
        reminder_id,
        OWNER,
        text="telepon ibu",
        repeat_kind="once",
        due_at=later,
        local_hhmm="10:30",
    )
    pending = db.list_reminders(OWNER)
    assert len(pending) == 1, "a snoozed reminder must come back to life"
    assert pending[0]["last_sent_at"] is None


def test_update_reminder_is_scoped_to_the_owner():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("punya owner", now + timedelta(hours=1))
    assert (
        db.update_reminder(
            reminder_id,
            OTHER,
            text="dibajak",
            repeat_kind="once",
            due_at=now + timedelta(hours=2),
            local_hhmm="11:00",
        )
        is False
    )
    assert db.get_reminder(reminder_id, OWNER)["text"] == "punya owner"


def test_update_reminder_rejects_an_impossible_month():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=WIB)
    reminder_id = _once("x", now + timedelta(hours=1))
    with pytest.raises(ValueError):
        db.update_reminder(
            reminder_id,
            OWNER,
            text="x",
            repeat_kind="yearly",
            due_at=now + timedelta(days=1),
            local_hhmm="10:00",
            monthday=1,
            month=13,
        )
