"""Deleting things: staged proposal, /hapus confirmation, per-kind dispatch."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app import actions, db, shifts
from app.bot import Bot
from app.tools import (
    ToolContext,
    ValidationError,
    delete_all_reminders,
    delete_note,
    delete_schedule,
)

WIB = ZoneInfo("Asia/Jakarta")
OWNER = 1001


@pytest.fixture(autouse=True)
def _clean_db(tmp_path):
    db.connect(tmp_path / "test.db")
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta")
    actions.clear(OWNER)
    shifts.clear_pending(OWNER)
    yield


def _add(when: date, kind: str = "malam", place: str = "igd") -> None:
    db.upsert_shift(OWNER, when.isoformat(), kind, place, raw=str(when.day))


def _seed_october() -> None:
    _add(date(2026, 10, 1))
    _add(date(2026, 10, 3), "pagi", "klinik")
    _add(date(2026, 10, 6), "siang", "rs")


def _ctx() -> ToolContext:
    return ToolContext(user_id=OWNER, saved_city=None, http=None, feeds=[])


# ------------------------------------------------------------------- db


def test_delete_shift_returns_false_for_foreign_row():
    db.upsert_user(9999, "X", role="user", timezone="Asia/Jakarta")
    db.upsert_shift(9999, "2026-10-01", "malam", "igd", raw="x")
    assert db.delete_shift(OWNER, "2026-10-01") is False
    assert db.get_shift(9999, "2026-10-01") is not None


def test_delete_shifts_range_and_all():
    _seed_october()
    assert db.delete_shifts_range(OWNER, "2026-10-01", "2026-10-03") == 2
    remaining = db.list_shifts(OWNER, "0001-01-01")
    assert len(remaining) == 1
    assert remaining[0]["shift_date"] == "2026-10-06"
    assert db.delete_all_shifts(OWNER) == 1
    assert db.list_shifts(OWNER, "0001-01-01") == []


# ------------------------------------------------------------------- stage


def test_stage_roundtrip_and_clear():
    actions.stage(OWNER, "shift", [("2026-10-01", "Kamis, 1 Okt: IGD")])
    assert actions.pending(OWNER) == {
        "kind": "shift",
        "targets": [("2026-10-01", "Kamis, 1 Okt: IGD")],
    }
    actions.clear(OWNER)
    assert actions.pending(OWNER) is None


def test_proposal_text_asks_for_hapus():
    text = actions.proposal_text([("x", "Kamis, 1 Okt")])
    assert "Kamis, 1 Okt" in text
    assert "/hapus" in text and "/batal" in text


# ------------------------------------------------------------------- tool


def test_tool_stages_but_never_deletes():
    _seed_october()
    text = asyncio.run(delete_schedule(_ctx(), {"when": "2026-10-01", "days": 7}))
    assert "IGD" in text and "KLINIK" in text
    assert "/hapus" in text
    assert len(db.list_shifts(OWNER, "0001-01-01")) == 3
    assert actions.pending(OWNER) is not None


def test_tool_all_stages_every_row():
    _seed_october()
    text = asyncio.run(delete_schedule(_ctx(), {"all": True}))
    assert "/hapus" in text
    assert len(actions.pending(OWNER)["targets"]) == 3
    assert len(db.list_shifts(OWNER, "0001-01-01")) == 3


def test_tool_empty_targets_does_not_stage():
    text = asyncio.run(delete_schedule(_ctx(), {"when": "2026-11-01"}))
    assert "Tidak ada jadwal yang cocok" in text
    assert actions.pending(OWNER) is None


# ------------------------------------------------------------------- reminders


def _reminder() -> None:
    db.add_reminder(OWNER, "Minum obat", "once", datetime(2026, 9, 30, 2, 0), "09:00", 2)


def test_reminder_delete_all_stages_and_asks_hapus():
    _reminder()
    db.add_reminder(OWNER, "Kontrol bulanan", "once", datetime(2026, 10, 5, 3, 0), "10:00", 6)
    text = asyncio.run(delete_all_reminders(_ctx(), {}))
    assert "Minum obat" in text and "Kontrol bulanan" in text
    assert "/hapus" in text
    assert len(db.list_reminders(OWNER, active_only=True)) == 2
    assert actions.pending(OWNER)["kind"] == "reminder"


def test_reminder_delete_all_empty():
    text = asyncio.run(delete_all_reminders(_ctx(), {}))
    assert "Tidak ada pengingat aktif" in text
    assert actions.pending(OWNER) is None


def test_cmd_hapus_applies_reminder_staging():
    _reminder()
    actions.stage(OWNER, "reminder", [("1", "minum")])
    replies: list[str] = []
    asyncio.run(_cmd_bot(replies).cmd_hapus(_CmdUpdate(), None))
    assert replies == ["Terhapus 1 pengingat."]
    assert db.list_reminders(OWNER, active_only=True) == []


# ------------------------------------------------------------------- notes


def _note() -> int:
    return db.add_note(OWNER, "pasien A jadwal kontrol Kamis")


def test_note_single_delete_direct():
    note_id = _note()
    text = asyncio.run(delete_note(_ctx(), {"id": note_id}))
    assert f"Catatan #{note_id} dihapus." in text
    assert actions.pending(OWNER) is None
    assert db.list_notes(OWNER, limit=10) == []


def test_note_delete_all_stages_and_asks_hapus():
    _note()
    _note()
    text = asyncio.run(delete_note(_ctx(), {"all": True}))
    assert "/hapus" in text and "pasien A" in text
    assert len(db.list_notes(OWNER, limit=10)) == 2
    assert actions.pending(OWNER)["kind"] == "note"


def test_note_tool_requires_id_or_all():
    with pytest.raises(ValidationError):
        asyncio.run(delete_note(_ctx(), {}))


# ------------------------------------------------------------------- commands


class _CmdUpdate:
    def __init__(self, user_id: int = OWNER) -> None:
        self.effective_user = type("U", (), {"id": user_id})()


def _cmd_bot(replies: list[str]) -> Bot:
    bot = Bot.__new__(Bot)
    bot.config = type("C", (), {"owner_telegram_id": OWNER})()
    bot.application = object()

    async def send_message(user_id: int, text: str) -> None:
        replies.append(text)

    bot.send_message = send_message  # type: ignore[method-assign]
    return bot


def test_cmd_hapus_executes_staged_delete():
    _seed_october()
    actions.stage(OWNER, "shift", [("2026-10-01", "x"), ("2026-10-03", "y")])
    replies: list[str] = []
    asyncio.run(_cmd_bot(replies).cmd_hapus(_CmdUpdate(), None))
    assert replies == ["Terhapus 2 jadwal."]
    remaining = db.list_shifts(OWNER, "0001-01-01")
    assert [r["shift_date"] for r in remaining] == ["2026-10-06"]
    assert actions.pending(OWNER) is None


def test_cmd_hapus_without_staging_says_nothing_pending():
    replies: list[str] = []
    asyncio.run(_cmd_bot(replies).cmd_hapus(_CmdUpdate(), None))
    assert "Tidak ada penghapusan" in replies[0]


def test_cmd_hapus_notes_partial_gone_informs():
    actions.stage(OWNER, "note", [("123", "ghost"), ("999", "ghost2")])
    replies: list[str] = []
    asyncio.run(_cmd_bot(replies).cmd_hapus(_CmdUpdate(), None))
    assert replies[0] == "Terhapus 0 catatan."
    assert "Beberapa tidak ada lagi" in replies[1]


def test_cmd_batal_cancels_staged_delete_and_keeps_rows():
    _seed_october()
    actions.stage(OWNER, "shift", [("2026-10-01", "x")])
    replies: list[str] = []
    asyncio.run(_cmd_bot(replies).cmd_batal(_CmdUpdate(), None))
    assert replies == ["Penghapusan dibatalkan."]
    assert len(db.list_shifts(OWNER, "0001-01-01")) == 3
    assert actions.pending(OWNER) is None
