"""The get_schedule agent tool: chat lookups against stored shifts."""

from __future__ import annotations

import asyncio
from datetime import date
from zoneinfo import ZoneInfo

import pytest

from app import db, shifts
from app.tools import ToolContext, ValidationError, get_schedule

WIB = ZoneInfo("Asia/Jakarta")
OWNER = 1001


@pytest.fixture(autouse=True)
def _clean_db(tmp_path):
    db.connect(tmp_path / "test.db")
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta")
    yield


def _add(when: date, kind: str = "malam", place: str = "igd", partners=None) -> None:
    db.upsert_shift(OWNER, when.isoformat(), kind, place, partners=partners, raw=str(when.day))


def _ctx() -> ToolContext:
    return ToolContext(user_id=OWNER, saved_city=None, http=None, feeds=[])


async def _call(when=None, days=None):
    args = {k: v for k, v in (("when", when), ("days", days)) if v is not None}
    return await get_schedule(_ctx(), args)


# ------------------------------------------------------------------ engine


def test_when_date_weekday_and_besok():
    today = date(2026, 9, 29)  # Selasa
    assert shifts._when_date(today, "kamis") == date(2026, 10, 1)
    assert shifts._when_date(today, "besok") == date(2026, 9, 30)
    assert shifts._when_date(today, "2026-10-01") == date(2026, 10, 1)
    assert shifts._when_date(today, "senin") == date(2026, 10, 5)
    assert shifts._when_date(today, None) == today


def test_schedule_text_single_day():
    day = date(2026, 10, 1)
    _add(day, "malam", "igd", ["Jihan"])
    text = shifts.schedule_text(OWNER, WIB, when=day.isoformat())
    assert "IGD malam" in text
    assert "Jihan" in text


def test_schedule_text_libur_single_day():
    _add(date(2026, 10, 1), "libur")
    text = shifts.schedule_text(OWNER, WIB, when="2026-10-01")
    assert "libur" in text


def test_schedule_text_window():
    _add(date(2026, 10, 1), "malam", "igd")
    _add(date(2026, 10, 3), "pagi", "klinik")
    text = shifts.schedule_text(OWNER, WIB, when="2026-10-01", days=3)
    assert "IGD" in text and "KLINIK" in text
    assert "Kamis" in text


def test_schedule_text_empty_day():
    text = shifts.schedule_text(OWNER, WIB, when="2026-10-15")
    assert text.startswith("Tidak ada jadwal")


def test_schedule_text_defaults_to_today():
    today = date.today()
    _add(today)
    text = shifts.schedule_text(OWNER, WIB)
    assert "Jadwal" in text


# ------------------------------------------------------------------ tool


def test_get_schedule_tool_single_lookup():
    target = date(2026, 10, 1)
    _add(target, "pagi", "klinik")
    text = asyncio.run(_call(when=target.isoformat()))
    assert "KLINIK" in text


def test_get_schedule_tool_empty():
    text = asyncio.run(_call(when="2026-10-02"))
    assert "Tidak ada jadwal" in text


def test_get_schedule_tool_rejects_bad_days():
    with pytest.raises(ValidationError):
        asyncio.run(_call(days=0))
    with pytest.raises(ValidationError):
        asyncio.run(_call(days=61))


def test_get_schedule_tool_rejects_nonstring_when():
    with pytest.raises(ValidationError):
        asyncio.run(_call(when=123))
