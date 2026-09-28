"""Scheduler due-logic, including restart catch-up and no double runs.

`now` is always injected, so nothing here depends on the wall clock.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.scheduler import (
    DEFAULT_RECAP_TIME,
    KIND_RECAP,
    is_due,
    parse_hhmm,
    run_backup_if_due,
    run_recaps,
)

WIB = ZoneInfo("Asia/Jakarta")
NYC = ZoneInfo("America/New_York")
OWNER = 100
FRIEND = 200


@pytest.fixture()
def conn(tmp_path):
    db.connect(tmp_path / "sched.db")
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta", daily_limit=50)
    db.upsert_user(FRIEND, "Friend", role="user", timezone="Asia/Jakarta", daily_limit=50)
    yield
    db.close()


class FakeSender:
    """Records what would have been sent."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, user_id: int, text: str) -> None:
        self.sent.append((user_id, text))


class FakeRecap:
    text = "ringkasan"
    tokens_in = 100
    tokens_out = 20
    missing: list[str] = []


class FakeAgent:
    feeds: list[dict[str, str]] = []

    async def complete(self, system, user):
        return "ok", 0, 0


class FakeHTTP:
    pass


# --------------------------------------------------------------------------
# hhmm parsing
# --------------------------------------------------------------------------


def test_parse_hhmm_accepts_valid():
    assert parse_hhmm("06:30") == (6, 30)
    assert parse_hhmm(" 23:59 ") == (23, 59)
    assert parse_hhmm("00:00") == (0, 0)


def test_parse_hhmm_rejects_invalid():
    for bad in ["6:3", "24:00", "12:60", "abc", "", "12", "12:30:45", "12;30"]:
        assert parse_hhmm(bad) is None, bad


def test_default_recap_time_is_valid():
    assert parse_hhmm(DEFAULT_RECAP_TIME) == (6, 30)


# --------------------------------------------------------------------------
# due logic
# --------------------------------------------------------------------------


def sched(hhmm: str) -> dict:
    return {"time_hhmm": hhmm}


def test_not_due_before_the_time():
    now = datetime(2026, 9, 28, 6, 29, tzinfo=WIB)
    assert is_due(sched("06:30"), WIB, now) is False


def test_due_exactly_at_the_time():
    now = datetime(2026, 9, 28, 6, 30, tzinfo=WIB)
    assert is_due(sched("06:30"), WIB, now) is True


def test_due_after_the_time():
    now = datetime(2026, 9, 28, 9, 0, tzinfo=WIB)
    assert is_due(sched("06:30"), WIB, now) is True


def test_restart_catchup_fires_late():
    # The container was down at 06:30 and came back at 09:00. It must still send.
    now = datetime(2026, 9, 28, 9, 0, tzinfo=WIB)
    assert is_due(sched("06:30"), WIB, now) is True


def test_schedule_in_another_timezone_uses_that_timezone():
    # 06:30 WIB on 28 Sep is 19:30 in New York on 27 Sep, so a 19:00 New York
    # schedule is already past due even though the wall clock in Jakarta says
    # it is only 06:30.
    now = datetime(2026, 9, 28, 6, 30, tzinfo=WIB)
    assert is_due(sched("19:00"), NYC, now) is True

    # A 20:00 New York schedule is not due yet at that moment.
    assert is_due(sched("20:00"), NYC, now) is False
    # ...and becomes due at 07:00 WIB (20:00 EDT).
    assert is_due(sched("20:00"), NYC, datetime(2026, 9, 28, 7, 0, tzinfo=WIB)) is True


def test_malformed_schedule_is_never_due():
    now = datetime(2026, 9, 28, 23, 59, tzinfo=WIB)
    assert is_due(sched("not-a-time"), WIB, now) is False


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------


async def test_due_recap_is_sent_once(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    sender = FakeSender()
    agent, http = FakeAgent(), FakeHTTP()
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    now = datetime(2026, 9, 28, 6, 30, tzinfo=WIB)
    assert await run_recaps(sender, agent, http, now=now) == 1
    assert [uid for uid, _ in sender.sent] == [OWNER]

    # A second tick the same minute must not resend.
    assert await run_recaps(sender, agent, http, now=now) == 0
    assert len(sender.sent) == 1


async def test_not_due_recap_is_not_sent(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    sender = FakeSender()
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    now = datetime(2026, 9, 28, 6, 29, tzinfo=WIB)
    assert await run_recaps(sender, FakeAgent(), FakeHTTP(), now=now) == 0
    assert sender.sent == []


async def test_disabled_schedule_is_skipped(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30", enabled=False)
    sender = FakeSender()
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    now = datetime(2026, 9, 28, 7, 0, tzinfo=WIB)
    assert await run_recaps(sender, FakeAgent(), FakeHTTP(), now=now) == 0


async def test_restart_does_not_duplicate_the_same_day(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    sender = FakeSender()
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    first = datetime(2026, 9, 28, 6, 31, tzinfo=WIB)
    later = datetime(2026, 9, 28, 14, 0, tzinfo=WIB)  # after a container restart
    await run_recaps(sender, FakeAgent(), FakeHTTP(), now=first)
    assert await run_recaps(sender, FakeAgent(), FakeHTTP(), now=later) == 0
    assert len(sender.sent) == 1


async def test_next_day_sends_again(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    sender = FakeSender()
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    await run_recaps(sender, FakeAgent(), FakeHTTP(), now=datetime(2026, 9, 28, 6, 30, tzinfo=WIB))
    await run_recaps(sender, FakeAgent(), FakeHTTP(), now=datetime(2026, 9, 29, 6, 30, tzinfo=WIB))
    assert len(sender.sent) == 2


async def test_each_user_gets_their_own_recap(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    db.set_schedule(FRIEND, KIND_RECAP, "07:00")
    sender = FakeSender()
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    now = datetime(2026, 9, 28, 7, 30, tzinfo=WIB)
    assert await run_recaps(sender, FakeAgent(), FakeHTTP(), now=now) == 2
    assert sorted(uid for uid, _ in sender.sent) == [OWNER, FRIEND]


async def test_scheduled_run_counts_tokens_but_not_messages(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    now = datetime(2026, 9, 28, 6, 30, tzinfo=WIB)
    await run_recaps(FakeSender(), FakeAgent(), FakeHTTP(), now=now)

    usage = db.get_usage(OWNER, "2026-09-28")
    assert usage["msg_count"] == 0
    assert usage["tokens_in"] == 100
    assert usage["tokens_out"] == 20


async def test_failed_send_releases_the_claim_for_retry(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)

    class BrokenSender:
        async def send_message(self, user_id, text):
            raise RuntimeError("telegram down")

    now = datetime(2026, 9, 28, 6, 30, tzinfo=WIB)
    assert await run_recaps(BrokenSender(), FakeAgent(), FakeHTTP(), now=now) == 0
    # The claim was released, so a healthy sender can deliver on the next tick.
    sender = FakeSender()
    assert await run_recaps(sender, FakeAgent(), FakeHTTP(), now=now) == 1
    assert len(sender.sent) == 1


async def test_recap_failure_does_not_stop_other_users(conn, monkeypatch):
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    db.set_schedule(FRIEND, KIND_RECAP, "06:30")

    calls: list[int] = []

    async def sometimes_fails(agent, user_id, http):
        calls.append(user_id)
        if user_id == OWNER:
            raise RuntimeError("boom")
        return FakeRecap()

    monkeypatch.setattr("app.scheduler.build_recap", sometimes_fails)
    sender = FakeSender()
    now = datetime(2026, 9, 28, 6, 30, tzinfo=WIB)
    await run_recaps(sender, FakeAgent(), FakeHTTP(), now=now)
    assert [uid for uid, _ in sender.sent] == [FRIEND]


async def test_failed_recap_build_releases_the_claim_for_retry(conn, monkeypatch):
    # A transient LLM error must not cost the user the whole day: the claim has
    # to be released exactly like a failed send, so the next tick retries.
    db.set_schedule(OWNER, KIND_RECAP, "06:30")
    sender = FakeSender()

    async def build_fails(_agent, _user_id, _http):
        raise RuntimeError("llm down")

    monkeypatch.setattr("app.scheduler.build_recap", build_fails)
    now = datetime(2026, 9, 28, 6, 30, tzinfo=WIB)
    assert await run_recaps(sender, FakeAgent(), FakeHTTP(), now=now) == 0
    assert sender.sent == []

    monkeypatch.setattr("app.scheduler.build_recap", _fake_build)
    assert await run_recaps(sender, FakeAgent(), FakeHTTP(), now=now) == 1
    assert [uid for uid, _ in sender.sent] == [OWNER]


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def test_backup_not_due_before_the_hour():
    assert run_backup_if_due(Path("x.db"), now=datetime(2026, 9, 28, 2, 59)) is False


def test_backup_due_after_the_hour(conn, monkeypatch, tmp_path):
    monkeypatch.setattr("app.backup.run_backup", lambda *a, **k: Path("written.db"))
    assert run_backup_if_due(tmp_path / "a.db", now=datetime(2026, 9, 28, 3, 0)) is True


def test_failed_backup_is_not_reported_as_done(conn, monkeypatch, tmp_path):
    # run_backup returns None on failure. Reporting success here would mark the
    # day done and leave the database unprotected for the next 24 hours.
    monkeypatch.setattr("app.backup.run_backup", lambda *a, **k: None)
    assert run_backup_if_due(tmp_path / "a.db", now=datetime(2026, 9, 28, 4, 0)) is False


def test_backup_not_repeated_on_the_same_day(conn, monkeypatch, tmp_path):
    calls: list[datetime] = []

    def record(_db_path, now=None, **_kwargs):
        calls.append(now)
        return Path("written.db")

    monkeypatch.setattr("app.backup.run_backup", record)
    first = datetime(2026, 9, 28, 3, 5)
    assert run_backup_if_due(tmp_path / "a.db", now=first) is True
    assert run_backup_if_due(tmp_path / "a.db", now=datetime(2026, 9, 28, 9, 0), last_run=first) is False
    assert len(calls) == 1


def _fake_build(agent, user_id, http):
    async def _inner():
        return FakeRecap()

    return _inner()
