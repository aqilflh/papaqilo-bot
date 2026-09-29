"""Route groups, waypoint wizard, tools, and bot handlers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app import actions, db, routes
from app.bot import Bot
from app.tools import ToolContext, run_tool

OWNER = 1001

A = (-6.123456, 106.789012)
P1 = (-6.124000, 106.790000)
P2 = (-6.126000, 106.793000)
B = (-6.135000, 106.801000)


@pytest.fixture(autouse=True)
def _clean(db_fixture):
    db.connect(db_fixture)
    db.upsert_user(OWNER, "Owner", role="owner", timezone="Asia/Jakarta")
    routes.clear_wizard(OWNER)
    actions.clear(OWNER)
    yield


@pytest.fixture
def db_fixture(tmp_path):
    return tmp_path / "routes.db"


def _ctx() -> ToolContext:
    return ToolContext(user_id=OWNER, saved_city=None, http=None, feeds=[])


def _run(name: str, args: dict) -> str:
    return asyncio.run(run_tool(name, args, _ctx(), owner=True))


def _seed_route(as_alternative: str = "") -> int:
    gid = db.create_route_group(OWNER, "Pulang")
    db.create_route(OWNER, gid, as_alternative, *A, *B, [P1, P2])
    return gid


# ------------------------------------------------------------------- db


def test_route_group_crud_scoped():
    gid = _seed_route("via Pasar Minggu")
    assert db.route_group_by_name(OWNER, "pulang")["id"] == gid
    assert db.list_route_groups(OWNER)[0]["route_count"] == 1
    assert db.rename_route_group(gid, OWNER, "Ke rumah") is True
    assert db.rename_route_group(gid, 9999, "x") is False
    assert db.route_group_by_name(OWNER, "ke rumah") is not None
    assert db.rename_route(9999, OWNER, "y") is False  # unknown route id


def test_route_and_waypoints_order():
    gid = _seed_route("via TB Simatupang")
    (rid,) = [r["id"] for r in db.list_routes(gid, OWNER)]
    wps = db.list_waypoints(rid, OWNER)
    assert [(w["sequence"], w["latitude"]) for w in wps] == [(1, P1[0]), (2, P2[0])]
    row = db.route_by_name(OWNER, "via tb simatupang")
    assert row is not None and row["id"] == rid
    assert db.rename_route(rid, OWNER, "via Ciputat") is True
    assert db.route_by_name(OWNER, "via Ciputat") is not None


def test_delete_cascades_empty_group():
    gid = _seed_route("x")
    rid = db.route_by_name(OWNER, "x")["id"]
    assert db.delete_route(rid, OWNER) is True
    assert db.list_waypoints(rid, OWNER) == []
    assert db.list_routes(gid, OWNER) == []
    assert db.delete_route_group(gid, OWNER) is True
    assert db.list_route_groups(OWNER) == []


# ------------------------------------------------------------------- wizard


def test_full_wizard_flow_persists_route():
    routes.begin_points(OWNER, "Pulang", alt_name="via Pasar Minggu")
    w = routes.get_wizard(OWNER)
    reply, _ = routes.record_location(w, *A)
    assert "Titik A" in reply
    reply, _ = routes.record_location(w, *P1)
    assert "Waypoint 1" in reply
    reply, _ = routes.record_location(w, *P1)
    assert "sama" in reply  # duplicate tap ignored, still on waypoints
    reply, _ = routes.record_location(w, *P2)
    assert "Waypoint 2" in reply
    reply, should = routes.handle_wizard_text(w, "done")
    assert "Titik B" in reply and not should
    reply, _ = routes.record_location(w, *B)
    assert "Preview" in reply and "/simpan" in reply
    assert routes.get_wizard(OWNER)["phase"] == "confirm"
    text = routes.save_wizard(OWNER, routes.get_wizard(OWNER))
    assert "berhasil disimpan" in text and "Pasar Minggu" in text
    groups = db.list_route_groups(OWNER)
    assert len(groups) == 1 and groups[0]["route_count"] == 1
    assert routes.get_wizard(OWNER) is None


def test_wizard_begin_with_name_hint_and_text_name():
    routes.set_wizard(
        OWNER,
        {"phase": "name", "kind": "create", "group_id": None, "group_name": "", "alt_name": ""},
    )
    w = routes.get_wizard(OWNER)
    reply, _ = routes.handle_wizard_text(w, "Pulang")
    assert "lokasi titik A" in reply
    w = routes.get_wizard(OWNER)
    assert w["phase"] == "points" and w["sub"] == "origin"
    reply, _ = routes.record_location(w, *A)
    assert "Titik A" in reply


def test_wizard_expiry():
    routes.begin_points(OWNER, "Pulang")
    w = routes.get_wizard(OWNER)
    w["expires_at"] = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    routes.ROUTE_WIZARDS[OWNER] = w
    assert routes.get_wizard(OWNER) is None


def test_wizard_cancel_clears():
    routes.begin_points(OWNER, "Pulang")
    assert routes.get_wizard(OWNER) is not None
    routes.clear_wizard(OWNER)
    assert routes.get_wizard(OWNER) is None


# ------------------------------------------------------------------- tools


def test_create_tool_no_name_asks():
    reply = _run("create_route_group", {})
    assert "Nama route" in reply
    assert routes.get_wizard(OWNER)["phase"] == "name"


def test_create_tool_with_name_starts_points():
    reply = _run("create_route_group", {"name": "Pulang"})
    assert "lokasi titik A" in reply
    assert routes.get_wizard(OWNER)["phase"] == "points"


def test_add_alternative_requires_group():
    reply = _run("add_route_alternative", {"name": "Pulang"})
    assert "Tidak ada route group" in reply


def test_add_alternative_with_label_starts_points():
    _seed_route("utama")
    reply = _run("add_route_alternative", {"name": "Pulang", "alternative": "via Ciputat"})
    assert "lokasi titik A" in reply
    assert routes.get_wizard(OWNER)["alt_name"] == "via Ciputat"


def test_list_and_show_tools():
    _seed_route("via Pasar Minggu")
    reply = _run("list_routes", {})
    assert "Pulang" in reply and "via Pasar Minggu" in reply
    assert "2 waypoints" in reply
    detail = _run("show_route", {"name": "Pulang"})
    assert "-6.135000" in detail and "📍 B" in detail


def test_rename_tool():
    _seed_route("via Pasar Minggu")
    reply = _run("rename_route", {"name": "via pasar minggu", "new_name": "via Ciputat"})
    assert "di-rename" in reply
    reply = _run("rename_route", {"name": "pulang", "new_name": "Ke rumah"})
    assert "group" in reply


def test_cancel_tool_clears_wizard():
    _run("create_route_group", {"name": "Pulang"})
    assert routes.get_wizard(OWNER) is not None
    reply = _run("cancel_route_creation", {})
    assert "dibatalkan" in reply
    assert routes.get_wizard(OWNER) is None


def test_delete_single_route_direct():
    _seed_route("via Pasar Minggu")
    reply = _run("delete_route", {"name": "via pasar minggu"})
    assert "dihapus" in reply
    assert db.route_by_name(OWNER, "via pasar minggu") is None
    assert actions.pending(OWNER) is None


def test_delete_group_staged_until_hapus():
    gid = _seed_route("via A")
    db.create_route(OWNER, gid, "via B", *A, *B, [P1])
    reply = _run("delete_route", {"name": "pulang"})
    assert "/hapus" in reply
    assert len(db.list_routes(gid, OWNER)) == 2
    assert actions.pending(OWNER)["kind"] == "route_group"
    actions.clear(OWNER)
    reply = _run("delete_route", {"all": True})
    assert "/hapus" in reply
    assert actions.pending(OWNER)["kind"] == "route_group"


# ------------------------------------------------------------------- bot


class _U:
    def __init__(self, user_id: int = OWNER):
        self.effective_user = type("U", (), {"id": user_id})()


class _Msg:
    def __init__(self, lat: float, lng: float):
        self.location = type("L", (), {"latitude": lat, "longitude": lng})()


class _LocUpdate(_U):
    def __init__(self, lat: float, lng: float, user_id: int = OWNER):
        super().__init__(user_id)
        self.effective_message = _Msg(lat, lng)


def _bot(replies: list[str]) -> Bot:
    bot = Bot.__new__(Bot)
    bot.config = type("C", (), {"owner_telegram_id": OWNER})()
    bot.application = object()

    async def send_message(user_id: int, text: str) -> None:
        replies.append(text)

    bot.send_message = send_message  # type: ignore[method-assign]
    return bot


def test_on_location_without_wizard_hints():
    replies: list[str] = []
    asyncio.run(_bot(replies).on_location(_LocUpdate(*A), None))
    assert "buat route" in replies[0]


def test_on_location_records_origin():
    routes.begin_points(OWNER, "Pulang")
    replies: list[str] = []
    asyncio.run(_bot(replies).on_location(_LocUpdate(*A), None))
    assert "Titik A" in replies[0]
    assert routes.get_wizard(OWNER)["sub"] == "waypoints"


def test_cmd_simpan_saves_route_preview():
    routes.begin_points(OWNER, "Pulang", alt_name="via Pasar Minggu")
    w = routes.get_wizard(OWNER)
    routes.record_location(w, *A)
    routes.record_location(w, *P1)
    routes.handle_wizard_text(w, "done")
    routes.record_location(w, *B)
    replies: list[str] = []
    asyncio.run(_bot(replies).cmd_simpan(_U(), None))
    assert replies[0].startswith("✅ Route")
    assert db.list_route_groups(OWNER)[0]["route_count"] == 1


def test_cmd_batal_cancels_wizard():
    routes.begin_points(OWNER, "Pulang")
    replies: list[str] = []
    asyncio.run(_bot(replies).cmd_batal(_U(), None))
    assert replies == ["Pembuatan route dibatalkan."]
    assert routes.get_wizard(OWNER) is None
