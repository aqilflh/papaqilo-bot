"""Inline "Simpan/Batal" preview buttons for pasted shift schedules."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from app import db, shifts
from app.bot import Bot
from app.shift_parser import parse_schedule

OWNER = 1001
OTHER = 1002
FIXTURE = Path(__file__).parent / "fixtures" / "jadwal_sample.txt"


class _Config:
    owner_telegram_id = OWNER


class FakeQuery:
    def __init__(self, data: str, user_id: int) -> None:
        self.data = data
        self.from_user = type("U", (), {"id": user_id})()
        self.answered: str | None = None
        self.edited: str | None = None

    async def answer(self, text: str | None = None) -> None:
        self.answered = text or ""

    async def edit_message_text(self, text: str) -> None:
        self.edited = text


class FakeUpdate:
    def __init__(self, query: FakeQuery) -> None:
        self.callback_query = query


def _bot() -> Bot:
    bot = Bot.__new__(Bot)
    bot.config = _Config()
    return bot


def _click(data: str, user_id: int = OWNER) -> FakeQuery:
    query = FakeQuery(data, user_id)
    asyncio.run(_bot().on_shift_callback(FakeUpdate(query), None))
    return query


def _click_any(data: str, user_id: int = OWNER) -> FakeQuery:
    query = FakeQuery(data, user_id)
    asyncio.run(_bot().on_any_callback(FakeUpdate(query), None))
    return query


@pytest.fixture(autouse=True)
def _clean_db(tmp_path):
    db.connect(tmp_path / "test.db")
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta")
    db.upsert_user(OTHER, "Other", role="user", timezone="Asia/Jakarta")
    yield


def _stage(user_id: int = OWNER) -> None:
    parsed = parse_schedule(FIXTURE.read_text(encoding="utf-8"), date(2026, 9, 28))
    shifts.stage_pending(user_id, parsed)


def _rows() -> list:
    return db.list_shifts(OWNER, "2026-09-28", "2026-11-02")


def test_save_answers_stores_rows_and_edits():
    _stage()
    q = _click("shift_save:1001")
    assert q.answered == "Tersimpan 10 shift."
    assert q.edited == "Jadwal tersimpan: 10 shift. Cek /besok."
    assert len(_rows()) == 10
    assert shifts.pending_import(OWNER) is None


def test_cancel_clears_pending_without_saving():
    _stage()
    q = _click("shift_cancel:1001")
    assert q.answered == "Import jadwal dibatalkan."
    assert q.edited == "Import jadwal dibatalkan."
    assert shifts.pending_import(OWNER) is None
    assert _rows() == []


def test_expired_session_tells_user_to_resend():
    q = _click("shift_save:1001")
    assert "kedaluwarsa" in q.answered
    assert q.edited == "Import jadwal kedaluwarsa."
    assert _rows() == []


def test_wrong_owner_button_is_rejected():
    q = _click("shift_save:9999")
    assert q.answered == "Tombol ini untuk sesi import yang lain."
    assert q.edited is None


def test_unauthorized_user_is_rejected():
    q = _click("shift_save:7777", user_id=7777)
    assert q.answered == "Bukan untuk kamu."
    assert q.edited is None


def test_unknown_callback_data_is_answered_as_safety_net():
    q = _click_any("menu:open")
    assert q.answered == "Tombol ini sudah tidak aktif."
    assert q.edited is None


# ----------------------------------------------------------------- commands


class _CmdUpdate:
    def __init__(self, user_id: int) -> None:
        self.effective_user = type("U", (), {"id": user_id})()


def _cmd_bot(replies: list[str]) -> Bot:
    bot = _bot()
    bot.application = object()

    async def send_message(user_id: int, text: str) -> None:
        replies.append(text)

    bot.send_message = send_message  # type: ignore[method-assign]
    return bot


def test_cmd_simpan_saves_and_replies():
    _stage()
    replies: list[str] = []
    bot = _cmd_bot(replies)
    asyncio.run(bot.cmd_simpan(_CmdUpdate(OWNER), None))
    assert replies == ["Jadwal tersimpan: 10 shift. Cek /besok."]
    assert len(_rows()) == 10
    assert shifts.pending_import(OWNER) is None


def test_cmd_simpan_without_session_says_expired():
    replies: list[str] = []
    bot = _cmd_bot(replies)
    asyncio.run(bot.cmd_simpan(_CmdUpdate(OWNER), None))
    assert "kedaluwarsa" in replies[0]
    assert _rows() == []


def test_cmd_batal_clears_pending():
    _stage()
    replies: list[str] = []
    bot = _cmd_bot(replies)
    asyncio.run(bot.cmd_batal(_CmdUpdate(OWNER), None))
    assert replies == ["Import jadwal dibatalkan."]
    assert shifts.pending_import(OWNER) is None
    assert _rows() == []
