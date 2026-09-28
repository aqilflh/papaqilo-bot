"""Database tests.

The isolation tests are the important ones: they prove that user A's data is
unreachable through user B's functions. Everything else is regression cover for
the migration, the limit counter, and the schedule claim.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import db

WIB = ZoneInfo("Asia/Jakarta")
OWNER = 100
FRIEND = 200
STRANGER = 300


@pytest.fixture()
def conn(tmp_path: Path):
    db.connect(tmp_path / "test.db")
    yield
    db.close()


@pytest.fixture()
def two_users(conn):
    db.upsert_user(OWNER, "Owner", role="owner", daily_limit=50)
    db.upsert_user(FRIEND, "Friend", role="user", daily_limit=3)
    return OWNER, FRIEND


# --------------------------------------------------------------------------
# connection + migrations
# --------------------------------------------------------------------------


def test_connect_enables_pragmas(tmp_path: Path):
    db.connect(tmp_path / "pragma.db")
    try:
        cur = db._conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        assert mode == "wal"
    finally:
        db.close()


def test_migrations_are_idempotent(tmp_path: Path):
    db.connect(tmp_path / "twice.db")
    db.close()
    # Reopening runs the migration check again; it must not raise or re-apply.
    db.connect(tmp_path / "twice.db")
    try:
        assert db._conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
        tables = {
            r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"users", "messages", "notes", "schedules", "usage"} <= tables
    finally:
        db.close()


def test_upsert_user_does_not_clobber_settings(conn):
    db.upsert_user(OWNER, "Owner", role="owner", daily_limit=50)
    db.set_city(OWNER, "Jakarta")
    # Re-running startup upsert must not wipe the city the user set.
    db.upsert_user(OWNER, "Owner Renamed", role="owner", daily_limit=50)
    user = db.get_user(OWNER)
    assert user["city"] == "Jakarta"
    assert user["name"] == "Owner Renamed"


# --------------------------------------------------------------------------
# isolation
# --------------------------------------------------------------------------


def test_recent_messages_never_cross_users(conn, two_users):
    owner, friend = two_users
    db.add_message(owner, "user", "rahasia owner")
    db.add_message(friend, "user", "rahasia friend")

    assert [m["content"] for m in db.recent_messages(owner, 10)] == ["rahasia owner"]
    assert [m["content"] for m in db.recent_messages(friend, 10)] == ["rahasia friend"]


def test_recent_messages_orders_oldest_first(conn):
    db.upsert_user(OWNER, "Owner")
    for i in range(5):
        db.add_message(OWNER, "user", f"m{i}")

    got = db.recent_messages(OWNER, 3)
    assert [m["content"] for m in got] == ["m2", "m3", "m4"]


def test_delete_history_only_touches_own_rows(conn, two_users):
    owner, friend = two_users
    db.add_message(owner, "user", "a")
    db.add_message(owner, "assistant", "b")
    db.add_message(friend, "user", "c")

    assert db.delete_history(owner) == 2
    assert db.recent_messages(owner, 10) == []
    assert [m["content"] for m in db.recent_messages(friend, 10)] == ["c"]


def test_notes_are_isolated(conn, two_users):
    owner, friend = two_users
    db.add_note(owner, "catatan owner")
    db.add_note(friend, "catatan friend")

    assert [n["content"] for n in db.list_notes(owner)] == ["catatan owner"]
    assert [n["content"] for n in db.list_notes(friend)] == ["catatan friend"]


def test_delete_note_cannot_cross_users(conn, two_users):
    owner, friend = two_users
    owner_note = db.add_note(owner, "milik owner")
    db.add_note(friend, "milik friend")

    # The friend guesses the owner's note id. It must not delete it.
    assert db.delete_note(friend, owner_note) is False
    assert len(db.list_notes(owner)) == 1
    assert db.delete_note(owner, owner_note) is True


def test_list_notes_query_cannot_reach_other_users(conn, two_users):
    owner, friend = two_users
    db.add_note(owner, "RahasiaOwner")
    db.add_note(friend, "RahasiaFriend")

    # The friend searches for the owner's exact text and must find nothing.
    assert db.list_notes(friend, query="RahasiaOwner") == []
    # The owner's own note is still there.
    assert len(db.list_notes(owner, query="RahasiaOwner")) == 1


def test_note_search_escapes_like_wildcards(conn):
    db.upsert_user(OWNER, "Owner")
    db.add_note(OWNER, "diskon 100%")
    db.add_note(OWNER, "diskon 50 persen")

    # A bare '%' would match everything if it were not escaped.
    found = db.list_notes(OWNER, query="100%")
    assert [n["content"] for n in found] == ["diskon 100%"]


def test_schedules_are_isolated(conn, two_users):
    owner, friend = two_users
    db.set_schedule(owner, "daily_recap", "06:30")
    db.set_schedule(friend, "daily_recap", "07:00")

    assert db.get_schedule(owner, "daily_recap")["time_hhmm"] == "06:30"
    assert db.get_schedule(friend, "daily_recap")["time_hhmm"] == "07:00"


def test_remove_user_keeps_other_users(conn, two_users):
    owner, friend = two_users
    db.add_message(owner, "user", "a")
    db.add_message(friend, "user", "b")
    db.add_note(owner, "n")

    db.remove_user(friend)

    assert db.get_user(friend) is None
    assert db.recent_messages(friend, 10) == []
    assert db.get_user(owner) is not None
    assert len(db.recent_messages(owner, 10)) == 1
    assert len(db.list_notes(owner)) == 1


def test_remove_user_commits_so_later_transactions_work(conn, two_users):
    # Regression: remove_user used to BEGIN without COMMIT, leaking an open
    # transaction. Every later BEGIN then raised "cannot start a transaction
    # within a transaction".
    owner, friend = two_users
    db.set_schedule(friend, "daily_recap", "06:30")
    db.remove_user(friend)

    db.set_schedule(owner, "daily_recap", "06:30")
    assert db.claim_schedule_run(owner, "daily_recap", "2026-09-28") is True


def test_transaction_rolls_back_and_stays_usable(conn):
    # A failure inside a transaction must roll back its work and leave the
    # connection usable for the next statement.
    db.upsert_user(OWNER, "Owner")

    with pytest.raises(RuntimeError, match="boom"), db._transaction() as cur:
        cur.execute("UPDATE users SET name = ? WHERE id = ?", ("Half Applied", OWNER))
        raise RuntimeError("boom")

    assert db.get_user(OWNER)["name"] == "Owner"
    # And the connection is not left holding a transaction.
    db.record_usage(OWNER, "2026-09-28", msg_count=1)
    assert db.get_usage(OWNER, "2026-09-28")["msg_count"] == 1


def test_foreign_key_rejects_unknown_user(conn):
    # No users row exists for STRANGER, so the FK must block the insert.
    with pytest.raises(sqlite3.IntegrityError):
        db.add_message(STRANGER, "user", "hello")


# --------------------------------------------------------------------------
# usage / daily limit
# --------------------------------------------------------------------------


def test_record_usage_accumulates(conn, two_users):
    owner, _ = two_users
    db.record_usage(owner, "2026-09-28", msg_count=1, tokens_in=100, tokens_out=20)
    db.record_usage(owner, "2026-09-28", msg_count=2, tokens_in=50, tokens_out=5)

    usage = db.get_usage(owner, "2026-09-28")
    assert usage == {"msg_count": 3, "tokens_in": 150, "tokens_out": 25}


def test_usage_is_per_user(conn, two_users):
    owner, friend = two_users
    db.record_usage(owner, "2026-09-28", msg_count=5, tokens_in=1, tokens_out=1)
    assert db.get_usage(friend, "2026-09-28")["msg_count"] == 0


def test_daily_limit_uses_the_users_own_limit(conn, two_users):
    owner, friend = two_users  # friend has daily_limit=3
    assert db.usage_remaining(owner, "2026-09-28") == 50
    assert db.usage_remaining(friend, "2026-09-28") == 3

    db.record_usage(friend, "2026-09-28", msg_count=3)
    assert db.usage_remaining(friend, "2026-09-28") == 0


def test_daily_limit_never_goes_negative(conn, two_users):
    _, friend = two_users
    db.record_usage(friend, "2026-09-28", msg_count=99)
    assert db.usage_remaining(friend, "2026-09-28") == 0


def test_daily_limit_resets_next_day(conn, two_users):
    _, friend = two_users
    db.record_usage(friend, "2026-09-28", msg_count=3)
    assert db.usage_remaining(friend, "2026-09-28") == 0
    assert db.usage_remaining(friend, "2026-09-29") == 3


def test_local_day_uses_the_given_timezone():
    from datetime import datetime

    # A timezone-aware now in WIB and one in New York can land on different
    # calendar days; local_day must reflect the timezone it was handed.
    wib = datetime(2026, 9, 28, 2, 0, tzinfo=WIB)
    assert db.local_day(WIB, now=wib) == "2026-09-28"
    ny = datetime(2026, 9, 27, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    assert db.local_day(ZoneInfo("America/New_York"), now=ny) == "2026-09-27"


# --------------------------------------------------------------------------
# schedule claim
# --------------------------------------------------------------------------


def test_claim_allows_exactly_one_run_per_day(conn, two_users):
    owner, _ = two_users
    db.set_schedule(owner, "daily_recap", "06:30")

    assert db.claim_schedule_run(owner, "daily_recap", "2026-09-28") is True
    # A second tick, or a restart later the same day, must lose the claim.
    assert db.claim_schedule_run(owner, "daily_recap", "2026-09-28") is False


def test_claim_allows_a_new_day(conn, two_users):
    owner, _ = two_users
    db.set_schedule(owner, "daily_recap", "06:30")
    assert db.claim_schedule_run(owner, "daily_recap", "2026-09-28") is True
    assert db.claim_schedule_run(owner, "daily_recap", "2026-09-29") is True


def test_claim_does_not_cross_users(conn, two_users):
    owner, friend = two_users
    db.set_schedule(owner, "daily_recap", "06:30")
    db.set_schedule(friend, "daily_recap", "06:30")

    assert db.claim_schedule_run(owner, "daily_recap", "2026-09-28") is True
    # The friend has their own row, so their claim is unaffected.
    assert db.claim_schedule_run(friend, "daily_recap", "2026-09-28") is True


def test_claim_respects_disabled(conn, two_users):
    owner, _ = two_users
    db.set_schedule(owner, "daily_recap", "06:30", enabled=False)
    assert db.claim_schedule_run(owner, "daily_recap", "2026-09-28") is False


def test_schedule_config_roundtrips(conn, two_users):
    owner, _ = two_users
    db.set_schedule(owner, "daily_recap", "06:30", config={"feeds": ["Antara News"], "limit": 5})
    assert db.get_schedule(owner, "daily_recap")["config"] == {
        "feeds": ["Antara News"],
        "limit": 5,
    }


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def test_backup_produces_a_readable_copy(conn, two_users, tmp_path: Path):
    owner, _ = two_users
    db.add_message(owner, "user", "terkunci di WAL")
    dest = tmp_path / "backups" / "agent-2026-09-28.db"

    db.backup_to(dest)

    assert dest.exists()
    check = sqlite3.connect(dest)
    try:
        count = check.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        check.close()
    assert count == 1
