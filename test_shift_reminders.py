"""Shift reminders: eve, before-start, expiry, dedup, and isolation.

Most tests seed a far-future shift as a "schedule end" so the expiry warning
does not fire and muddy the message the test is looking at.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.shift_parser import parse_schedule
from app.shifts import apply_import, deliver_shift_reminders, render_preview

WIB = ZoneInfo("Asia/Jakarta")
OWNER = 1001
OTHER = 1002
FIXTURE = Path(__file__).parent / "fixtures" / "jadwal_sample.txt"


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, user_id: int, text: str) -> None:
        self.sent.append((user_id, text))


@pytest.fixture(autouse=True)
def _clean_db(tmp_path):
    db.connect(tmp_path / "test.db")
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta")
    db.upsert_user(OTHER, "Other", role="user", timezone="Asia/Jakarta")
    yield


def _add(
    user_id: int, when: date, kind: str = "pagi", place: str = "klinik", partners=None
) -> None:
    db.upsert_shift(
        user_id, when.isoformat(), kind, place, partners=partners, raw=f"{when.day} {kind}"
    )


def _seed_far_end(user_id: int) -> None:
    """A shift two weeks out, so the expiry warning stays quiet in other tests."""
    _add(user_id, date(2026, 10, 12), "pagi", "klinik")


# ----------------------------------------------------------------- settings


def test_settings_defaults_when_never_touched():
    settings = db.get_shift_settings(OWNER)
    assert settings["reminders_on"] is True
    assert settings["eve_time"] == "20:00"
    assert settings["before_min"] is None
    assert settings["hours"] == {}


def test_settings_roundtrip():
    db.set_shift_reminders_on(OWNER, False)
    db.set_shift_eve_time(OWNER, "21:30")
    db.set_shift_before_min(OWNER, 45)
    db.set_shift_hours(OWNER, {"malam": "22:00-06:00"})
    settings = db.get_shift_settings(OWNER)
    assert settings["reminders_on"] is False
    assert settings["eve_time"] == "21:30"
    assert settings["before_min"] == 45
    assert settings["hours"] == {"malam": "22:00-06:00"}


def test_upsert_overwrites_same_date_not_duplicates():
    _add(OWNER, date(2026, 9, 30), "pagi", "klinik")
    _add(OWNER, date(2026, 9, 30), "malam", "igd")
    rows = db.list_shifts(OWNER, "2026-09-01")
    assert len(rows) == 1
    assert rows[0]["kind"] == "malam"
    assert rows[0]["place"] == "igd"


def test_delete_shift_only_deletes_own():
    _add(OWNER, date(2026, 9, 30), "pagi", "klinik")
    assert db.delete_shift(OTHER, "2026-09-30") is False
    assert db.get_shift(OWNER, "2026-09-30") is not None


# ------------------------------------------------------------------ eve


def test_eve_reminder_sent_once_and_only_once():
    _seed_far_end(OWNER)
    _add(OWNER, date(2026, 9, 29), "malam", "igd", partners=["Jihan"])
    now = datetime(2026, 9, 28, 20, 5, tzinfo=WIB)  # past the default 20:00
    sender = FakeSender()

    assert asyncio.run(deliver_shift_reminders(sender, now=now)) == 1
    assert sender.sent[0][0] == OWNER
    assert "IGD malam" in sender.sent[0][1]
    assert "bareng Jihan" in sender.sent[0][1]
    # A second tick (or restart) must not resend.
    assert asyncio.run(deliver_shift_reminders(sender, now=now + timedelta(minutes=1))) == 0
    assert len(sender.sent) == 1


def test_eve_restart_catchup_after_eve_time():
    _seed_far_end(OWNER)
    _add(OWNER, date(2026, 9, 29), "pagi", "igd", partners=["Ani"])
    now = datetime(2026, 9, 28, 23, 15, tzinfo=WIB)  # container was down at 20:00
    sender = FakeSender()

    assert asyncio.run(deliver_shift_reminders(sender, now=now)) == 1
    assert sender.sent[0][1].startswith("Besok")


def test_eve_not_sent_before_eve_time():
    _seed_far_end(OWNER)
    _add(OWNER, date(2026, 9, 29), "pagi", "klinik")
    now = datetime(2026, 9, 28, 19, 0, tzinfo=WIB)
    assert asyncio.run(deliver_shift_reminders(FakeSender(), now=now)) == 0


def test_eve_libur_says_besok_libur():
    _seed_far_end(OWNER)
    _add(OWNER, date(2026, 9, 29), "libur")
    now = datetime(2026, 9, 28, 20, 30, tzinfo=WIB)
    sender = FakeSender()

    assert asyncio.run(deliver_shift_reminders(sender, now=now)) == 1
    assert sender.sent[0][1] == "Besok libur."


def test_eve_includes_hours_when_set():
    _seed_far_end(OWNER)
    db.set_shift_hours(OWNER, {"malam": "22:00-06:00"})
    _add(OWNER, date(2026, 9, 29), "malam", "igd", partners=["Jihan"])
    now = datetime(2026, 9, 28, 20, 30, tzinfo=WIB)
    sender = FakeSender()

    asyncio.run(deliver_shift_reminders(sender, now=now))
    assert "22:00-06:00" in sender.sent[0][1]


def test_eve_sends_nothing_when_tomorrow_has_no_shift():
    _seed_far_end(OWNER)
    _add(OWNER, date(2026, 10, 1), "pagi", "klinik")  # far enough to skip expiry too
    now = datetime(2026, 9, 28, 20, 30, tzinfo=WIB)
    sender = FakeSender()

    assert asyncio.run(deliver_shift_reminders(sender, now=now)) == 0
    assert all("Besok" not in text for _, text in sender.sent)


# ---------------------------------------------------------------- before-start


def _today_morning_shift(kind: str = "pagi", place: str = "klinik", partners=None) -> None:
    _seed_far_end(OWNER)
    db.set_shift_hours(OWNER, {kind: "07:00-14:00"})
    db.set_shift_before_min(OWNER, 30)
    _add(OWNER, date(2026, 9, 28), kind, place, partners=partners)


def test_before_reminder_sent_inside_window():
    _today_morning_shift(partners=["Ani"])
    now = datetime(2026, 9, 28, 6, 45, tzinfo=WIB)
    sender = FakeSender()

    assert asyncio.run(deliver_shift_reminders(sender, now=now)) == 1
    text = sender.sent[0][1]
    assert "mulai 07:00" in text
    assert "bareng Ani" in text


def test_before_reminder_not_sent_when_shift_already_started():
    _today_morning_shift()
    now = datetime(2026, 9, 28, 7, 30, tzinfo=WIB)  # stale after a restart
    assert asyncio.run(deliver_shift_reminders(FakeSender(), now=now)) == 0


def test_before_reminder_not_sent_too_early():
    _today_morning_shift()
    now = datetime(2026, 9, 28, 6, 10, tzinfo=WIB)
    assert asyncio.run(deliver_shift_reminders(FakeSender(), now=now)) == 0


def test_before_reminder_needs_hours_and_setting():
    _seed_far_end(OWNER)
    _add(OWNER, date(2026, 9, 28), "pagi", "klinik")  # no /jam, no /sebelum
    now = datetime(2026, 9, 28, 6, 45, tzinfo=WIB)
    assert asyncio.run(deliver_shift_reminders(FakeSender(), now=now)) == 0


# ------------------------------------------------------------------- expiry


def test_expiry_warning_sent_once_near_the_end():
    _add(OWNER, date(2026, 9, 30))  # today + 2
    now = datetime(2026, 9, 28, 9, 0, tzinfo=WIB)
    sender = FakeSender()

    assert asyncio.run(deliver_shift_reminders(sender, now=now)) == 1
    assert "Kirim jadwal baru ya" in sender.sent[0][1]
    assert asyncio.run(deliver_shift_reminders(sender, now=now + timedelta(hours=3))) == 0


def test_expiry_quiet_when_schedule_still_long():
    _add(OWNER, date(2026, 10, 5))  # today + 7
    now = datetime(2026, 9, 28, 9, 0, tzinfo=WIB)
    assert asyncio.run(deliver_shift_reminders(FakeSender(), now=now)) == 0


# ----------------------------------------------------------------- isolation


def test_eve_reminders_are_isolated_between_users():
    _seed_far_end(OTHER)
    _add(OTHER, date(2026, 9, 29), "malam", "igd", partners=["Cici"])
    now = datetime(2026, 9, 28, 20, 30, tzinfo=WIB)
    sender = FakeSender()

    assert asyncio.run(deliver_shift_reminders(sender, now=now)) == 1
    assert sender.sent == [(OTHER, "Besok (Selasa, 29 Sep): IGD malam bareng Cici.")]


# ---------------------------------------------------- import -> stored rows


def test_apply_import_stores_fixture_rows():
    text = FIXTURE.read_text(encoding="utf-8")
    parsed = parse_schedule(text, date(2026, 9, 28))
    assert parsed.ambiguous is False
    assert len(parsed.savable) == 10  # 11 entries, one is "belum ada jadwal" (24 sabtu)

    saved = apply_import(OWNER, parsed)
    assert saved == 10
    oct_shift = db.get_shift(OWNER, "2026-10-01")
    assert oct_shift["kind"] == "malam"
    assert oct_shift["place"] == "igd"
    assert oct_shift["partners"] == "Kang Dodi"
    # The empty "24 sabtu" line did not become a row.
    assert db.get_shift(OWNER, "2026-10-24") is None
    # Rollover line landed in November.
    assert db.get_shift(OWNER, "2026-11-02") is not None


def test_preview_reports_counts_and_ignored_lines():
    text = FIXTURE.read_text(encoding="utf-8")
    parsed = parse_schedule(text, date(2026, 9, 28))
    preview = render_preview(parsed)
    assert "Siap disimpan: 10" in preview
    assert "belum ada jadwal: 1" in preview
    assert "diabaikan: 1" in preview


def test_preview_flags_weekday_mismatch():
    parsed = parse_schedule("OKTOBER\n28 senin klinik siang", date(2026, 9, 28))
    preview = render_preview(parsed)
    assert "Perlu dicek" in preview
    assert "bukan hari senin" in preview
