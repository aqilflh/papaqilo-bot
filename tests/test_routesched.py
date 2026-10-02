"""Route reminders: DB persistence, plain-code interception, daily delivery.

The `now` clock is always injected, so nothing here depends on the wall clock.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from app import db, routes
from app.routesched import deliver_due_route_reminders, reminder_text

OWNER = 100
GROUP_NAME = "Pulang via Gempol"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def conn(tmp_path):
    db.connect(tmp_path / "routesched.db")
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta", daily_limit=50)
    yield
    db.close()


def _seed_group_and_route(user_id: int = OWNER, waypoints: int = 2) -> int:
    gid = db.create_route_group(user_id, GROUP_NAME)
    route_id = db.create_route(
        user_id,
        gid,
        "via Shell",
        -6.214,
        106.68,
        -6.2146,
        106.6803,
        [(-6.2142 + i / 100_000, 106.6801 + i / 100_000) for i in range(waypoints)],
    )
    db.set_route_metrics(route_id, user_id, 4646, 903)
    return gid


class FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, user_id: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append((user_id, text))


# --------------------------------------------------------------------------
# DB layer
# --------------------------------------------------------------------------


def test_route_reminder_set_get_update_delete(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    row = db.get_route_reminder(OWNER, gid)
    assert row is not None
    assert row["time_hhmm"] == "07:00"
    assert row["enabled"] == 1
    assert row["last_run_date"] is None

    db.set_route_reminder(OWNER, gid, "19:30")
    row = db.get_route_reminder(OWNER, gid)
    assert row["time_hhmm"] == "19:30"

    reminder_list = db.list_route_reminders(OWNER)
    assert len(reminder_list) == 1
    assert reminder_list[0]["group_name"] == GROUP_NAME
    assert reminder_list[0]["time_hhmm"] == "19:30"

    assert db.delete_route_reminder(OWNER, gid) is True
    assert db.get_route_reminder(OWNER, gid) is None
    assert db.delete_route_reminder(OWNER, gid) is False


def test_route_reminder_claim_release(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    assert db.claim_route_reminder_run(OWNER, gid, "2026-10-02") is True
    assert db.claim_route_reminder_run(OWNER, gid, "2026-10-02") is False
    db.release_route_reminder_claim(OWNER, gid, "2026-10-02")
    assert db.claim_route_reminder_run(OWNER, gid, "2026-10-02") is True


def test_route_group_delete_cascades_reminder(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    assert db.delete_route_group(gid, OWNER) is True
    assert db.get_route_reminder(OWNER, gid) is None


# --------------------------------------------------------------------------
# reminder_text
# --------------------------------------------------------------------------


def test_reminder_text_builds_group_status(conn):
    gid = _seed_group_and_route()
    text = reminder_text(OWNER, gid)
    assert GROUP_NAME in text
    assert "via Shell" in text
    assert "km" in text
    assert "Matikan:" in text


def test_reminder_text_empty_when_group_gone(conn):
    gid = _seed_group_and_route()
    assert reminder_text(OWNER, gid)
    db.delete_route_group(gid, OWNER)
    assert reminder_text(OWNER, gid) == ""


# --------------------------------------------------------------------------
# plain-code interception (route_reminder_action)
# --------------------------------------------------------------------------


def test_action_set_reminder_pagi(conn):
    _seed_group_and_route()
    reply = routes.route_reminder_action(OWNER, "ingetin route pulang via gempol jam 7 pagi")
    assert reply is not None
    assert GROUP_NAME in reply
    assert "07:00" in reply
    rows = db.list_route_reminders(OWNER)
    assert rows and rows[0]["time_hhmm"] == "07:00"


def test_action_set_reminder_24h_and_malam(conn):
    _seed_group_and_route()
    reply = routes.route_reminder_action(OWNER, "ingetin gue pulang jam 19:30")
    assert "19:30" in reply
    reply = routes.route_reminder_action(OWNER, "remind route pulang jam 7 malam")
    assert "19:00" in reply
    reply = routes.route_reminder_action(OWNER, "pasang reminder pulang jam 1 siang bro")
    assert "13:00" in reply
    assert len(db.list_route_reminders(OWNER)) == 1


def test_action_set_reminder_updates_existing(conn):
    _seed_group_and_route()
    routes.route_reminder_action(OWNER, "ingetin route pulang jam 7 pagi")
    routes.route_reminder_action(OWNER, "ingetin route pulang jam 08:15")
    rows = db.list_route_reminders(OWNER)
    assert len(rows) == 1
    assert rows[0]["time_hhmm"] == "08:15"


def test_action_set_reminder_invalid_hour(conn):
    _seed_group_and_route()
    reply = routes.route_reminder_action(OWNER, "ingetin pulang jam 25:00")
    assert reply is not None
    assert "Format jam" in reply
    assert db.list_route_reminders(OWNER) == []


def test_action_set_reminder_pronoun_single_group(conn):
    _seed_group_and_route()
    reply = routes.route_reminder_action(OWNER, "ingetin itu jam 6 pagi")
    assert reply is not None
    assert "06:00" in reply
    assert GROUP_NAME in reply


def test_action_unset_reminder(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    reply = routes.route_reminder_action(OWNER, "hapus reminder route pulang")
    assert reply is not None
    assert "dimatikan" in reply
    assert db.list_route_reminders(OWNER) == []


def test_action_unset_without_existing(conn):
    _seed_group_and_route()
    reply = routes.route_reminder_action(OWNER, "hapus reminder route pulang")
    assert reply is not None
    assert "nggak punya reminder" in reply


def test_action_list_reminders(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    reply = routes.route_reminder_action(OWNER, "cek reminder")
    assert reply is not None
    assert GROUP_NAME in reply
    assert "07:00" in reply

    single = routes.route_reminder_action(OWNER, "cek reminder route pulang")
    assert single is not None
    assert "07:00" in single
    assert "\n" not in single


def test_action_list_empty(conn):
    _seed_group_and_route()
    reply = routes.route_reminder_action(OWNER, "cek reminder")
    assert reply is not None
    assert "Belum ada reminder route" in reply


def test_action_generic_reminder_not_stolen(conn):
    _seed_group_and_route()
    assert routes.route_reminder_action(OWNER, "ingetin aku jam 9 pagi") is None
    assert routes.route_reminder_action(OWNER, "ingetin aku besok meeting jam 9") is None


def test_action_route_ops_not_stolen(conn):
    _seed_group_and_route()
    assert routes.route_reminder_action(OWNER, "hapus route pulang") is None
    assert routes.route_reminder_action(OWNER, "cek route pulang") is None
    assert routes.route_reminder_action(OWNER, "buat route pulang") is None


# --------------------------------------------------------------------------
# scheduled delivery
# --------------------------------------------------------------------------


def test_deliver_sends_once_and_dedups(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    sender = FakeSender()
    now = datetime(2026, 10, 2, 0, 5, tzinfo=UTC)  # 07:05 WIB

    assert run(deliver_due_route_reminders(sender, now=now)) == 1
    assert len(sender.sent) == 1
    assert sender.sent[0][0] == OWNER
    assert GROUP_NAME in sender.sent[0][1]

    assert run(deliver_due_route_reminders(sender, now=now)) == 0
    assert len(sender.sent) == 1


def test_deliver_not_before_target_time(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    sender = FakeSender()
    before = datetime(2026, 10, 1, 23, 59, tzinfo=UTC)  # 06:59 WIB
    assert run(deliver_due_route_reminders(sender, now=before)) == 0
    assert sender.sent == []


def test_deliver_catches_up_after_restart(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    sender = FakeSender()
    late = datetime(2026, 10, 2, 2, 0, tzinfo=UTC)  # 09:00 WIB
    assert run(deliver_due_route_reminders(sender, now=late)) == 1


def test_deliver_reschedules_next_day(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    sender = FakeSender()
    day1 = datetime(2026, 10, 2, 0, 5, tzinfo=UTC)
    day2 = datetime(2026, 10, 3, 0, 5, tzinfo=UTC)
    assert run(deliver_due_route_reminders(sender, now=day1)) == 1
    assert run(deliver_due_route_reminders(sender, now=day1)) == 0
    assert run(deliver_due_route_reminders(sender, now=day2)) == 1
    assert len(sender.sent) == 2


def test_deliver_drops_reminder_for_empty_group(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    # Empty the group (group stays), so reminder_text has nothing to show.
    for route in db.list_routes(gid, OWNER):
        db.delete_route(route["id"], OWNER)
    sender = FakeSender()
    now = datetime(2026, 10, 2, 0, 5, tzinfo=UTC)
    assert run(deliver_due_route_reminders(sender, now=now)) == 0
    assert sender.sent == []
    assert db.list_route_reminders(OWNER) == []


def test_deliver_retries_after_send_failure(conn):
    gid = _seed_group_and_route()
    db.set_route_reminder(OWNER, gid, "07:00")
    failing = FakeSender(fail=True)
    ok_sender = FakeSender()
    now = datetime(2026, 10, 2, 0, 5, tzinfo=UTC)

    assert run(deliver_due_route_reminders(failing, now=now)) == 0
    assert ok_sender.sent == []
    # The claim was released, so the next tick retries and succeeds.
    assert run(deliver_due_route_reminders(ok_sender, now=now)) == 1
    assert len(ok_sender.sent) == 1
