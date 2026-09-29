"""Route creation wizard and route text formatting.

Routes are captured as a deterministic multi-step conversation: the LLM only
*starts* the wizard through a tool (or cancels it). Every transition after
that -- naming, Telegram Location messages, "done", the save/confirm step --
is plain code here, so coordinates are never invented by the model.

State is per-user and in-memory and expires, exactly like pending shift
imports: single-process bot, gone on restart, never written to disk.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
from typing import Any

from app import db
from app.log import get_logger, log_event

log = get_logger("routes")

WIZ_TTL = timedelta(minutes=30)
#: Rounding for point labels, enough to identify a street corner on Telegram.
COORD_DP = 6

#: A user explicitly re-sending the same spot within this distance is a
#: duplicate tap, not a waypoint. Real nearby turns stay (they are farther).
DUP_IGNORE_M = 10.0


# ---------------------------------------------------------------------------
# wizard state
# ---------------------------------------------------------------------------

#: user_id -> state dict (see the phases below).
ROUTE_WIZARDS: dict[int, dict[str, Any]] = {}

# Phases:
#   "name"     -- awaiting the group name
#   "alt_name" -- awaiting the alternative label for an existing group
#   "points"   -- recording origin -> waypoints -> destination
#   "confirm"  -- preview shown, awaiting /simpan or /batal


def _new_points_state(
    group_name: str,
    alt_name: str = "",
    kind: str = "create",
    group_id: int | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "group_id": group_id,
        "group_name": group_name,
        "alt_name": alt_name,
        "phase": "points",
        "sub": "origin",
        "origin": None,
        "waypoints": [],
        "destination": None,
    }


def set_wizard(user_id: int, state: dict[str, Any]) -> None:
    state["expires_at"] = (datetime.now(UTC) + WIZ_TTL).isoformat()
    ROUTE_WIZARDS[user_id] = state


def get_wizard(user_id: int) -> dict[str, Any] | None:
    state = ROUTE_WIZARDS.get(user_id)
    if state is None:
        return None
    expires = datetime.fromisoformat(state["expires_at"])
    if datetime.now(UTC) > expires:
        ROUTE_WIZARDS.pop(user_id, None)
        return None
    return state


def clear_wizard(user_id: int) -> None:
    ROUTE_WIZARDS.pop(user_id, None)


def begin_points(
    user_id: int,
    group_name: str,
    alt_name: str = "",
    kind: str = "create",
    group_id: int | None = None,
) -> None:
    """Start (or restart) waypoint recording for a known group name."""
    set_wizard(user_id, _new_points_state(group_name, alt_name, kind, group_id))


# ---------------------------------------------------------------------------
# coordinates
# ---------------------------------------------------------------------------


def _valid_coord(lat: float, lng: float) -> bool:
    return -90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = (math.radians(v) for v in (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(
        (lon2 - lon1) / 2
    ) ** 2
    return 2 * 6371000 * math.asin(min(1.0, math.sqrt(h)))


def fmt_coord(lat: float, lng: float) -> str:
    return f"{lat:.{COORD_DP}f}, {lng:.{COORD_DP}f}"


# ---------------------------------------------------------------------------
# text builders
# ---------------------------------------------------------------------------


def _wp_noun(n: int) -> str:
    return "waypoint" if n == 1 else "waypoints"


def route_summary(route: dict[str, Any], waypoints: list[dict[str, Any]]) -> str:
    """One-line: `via Pasar Minggu (A -> 2 waypoint -> B)`."""
    label = route["name"] or "(utama)"
    wps = len(waypoints)
    return f"{label} (A → {_wp_noun(wps)} → B)"


def list_routes_text(user_id: int) -> str:
    """Full listing: every group and its alternatives."""
    groups = db.list_route_groups(user_id)
    if not groups:
        return "Belum ada route. Bilang \"buat route pulang\" untuk mulai."
    lines = ["🛣️ Route lo:"]
    for group in groups:
        routes = db.list_routes(group["id"], user_id)
        if not routes:
            lines.append(f"- {group['name']} (kosong)")
            continue
        lines.append(group["name"])
        lines.extend(
            "  " + route_summary(route, db.list_waypoints(route["id"], user_id))
            for route in routes
        )
    return "\n".join(lines)


def show_route_text(user_id: int, group: dict[str, Any]) -> str:
    """Detailed view of one group with exact coordinates per alternative."""
    routes = db.list_routes(group["id"], user_id)
    lines = [f"🛣️ {group['name']}"]
    for route in routes:
        wp = db.list_waypoints(route["id"], user_id)
        lines.append("")
        lines.append(route["name"] or "(utama)")
        lines.append(f"📍 A   {fmt_coord(route['origin_lat'], route['origin_lng'])}")
        for point in wp:
            lines.append(
                f"   P{point['sequence']} {fmt_coord(point['latitude'], point['longitude'])}"
            )
        lines.append(f"📍 B   {fmt_coord(route['destination_lat'], route['destination_lng'])}")
        lines.append(f"Total titik: {len(wp) + 2}")
    return "\n".join(lines)


def preview_text(state: dict[str, Any]) -> str:
    """Preview right before saving, ending with the /simpan ask."""
    wps = state["waypoints"]
    lines = ["🛣️ Route Preview", ""]
    if state.get("group_name"):
        lines += ["Group:", state["group_name"]]
    if state.get("alt_name"):
        lines += ["", "Alternatif:", state["alt_name"]]
    lines.append("")
    lines.append(f"📍 A   {fmt_coord(*state['origin'])}")
    for i, (lat, lng) in enumerate(wps, start=1):
        lines.append(f"   P{i} {fmt_coord(lat, lng)}")
    lines.append(f"📍 B   {fmt_coord(*state['destination'])}")
    lines.append("")
    lines.append(f"Total titik: {len(wps) + 2}")
    lines.append("")
    lines.append("Kirim /simpan untuk menyimpan, atau /batal untuk membatalkan.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# wizard transitions (called by the bot's text / Location handlers)
# ---------------------------------------------------------------------------


def record_location(wizard: dict[str, Any], lat: float, lng: float) -> tuple[str, bool]:
    """Feed one Telegram Location into the active wizard.

    Returns (reply_text, changed_phase_to_confirm). Duplicate taps within
    DUP_IGNORE_M of the last recorded point are ignored.
    """
    if wizard["phase"] != "points":
        return "Lagi menunggu langkah lain. Ketik /batal kalau mau mulai ulang.", False
    if not _valid_coord(lat, lng):
        return "Koordinat di luar jangkauan. Kirim lagi ya.", False

    sub = wizard["sub"]
    if sub == "origin":
        wizard["origin"] = (lat, lng)
        wizard["sub"] = "waypoints"
        return (
            "✅ Titik A tersimpan.\n\n"
            "Sekarang kirim lokasi waypoint pertama.\n"
            "Kirim setiap titik yang ingin dilewati, lalu ketik \"done\" kalau sudah "
            "sampai titik B.",
            False,
        )

    if sub == "destination":
        wizard["destination"] = (lat, lng)
        wizard["phase"] = "confirm"
        return preview_text(wizard), True

    if wizard["waypoints"]:
        last = wizard["waypoints"][-1]
        if _haversine_m((lat, lng), last) < DUP_IGNORE_M:
            return (
                "Titik itu kayaknya sama dengan yang barusan. Kirim titik yang berbeda ya.",
                False,
            )
    wizard["waypoints"].append((lat, lng))
    n = len(wizard["waypoints"])
    return f"✅ Waypoint {n} tersimpan.\n\nKetik \"done\" kalau sudah sampai titik B.", False


def handle_wizard_text(wizard: dict[str, Any], text: str) -> tuple[str, bool]:
    """Drive the wizard with a plain (non-command) text message.

    Returns (reply_text, should_save). `should_save` is only True when the
    user confirmed the preview, and the caller then persists the state with
    save_wizard().
    """
    phase = wizard["phase"]
    lowered = text.strip().lower()

    if phase in ("name", "alt_name"):
        name = text.strip()[:60] or "Route"
        if phase == "name":
            wizard["group_name"] = name
        else:
            wizard["alt_name"] = name
        expires_at = wizard.get("expires_at")
        points = _new_points_state(
            wizard.get("group_name", ""),
            wizard.get("alt_name", ""),
            wizard.get("kind", "create"),
            wizard.get("group_id"),
        )
        if expires_at is not None:
            points["expires_at"] = expires_at
        wizard.clear()
        wizard.update(points)
        return "Oke. Sekarang kirim 📍 lokasi titik A.", False

    if phase == "points":
        if wizard["sub"] == "waypoints" and lowered in {"done", "selesai", "udah", "sudah"}:
            if not wizard["waypoints"]:
                return "Belum ada waypoint. Kirim dulu satu titik, atau /batal.", False
            wizard["sub"] = "destination"
            return "📍 Titik B sudah?\n\nKirim lokasi titik B.", False
        if wizard["sub"] == "destination":
            return "Kirim lokasi titik B.", False
        return (
            "Pilih: kirim 📍 lokasi untuk menambah titik, atau ketik \"done\" kalau "
            "sudah sampai titik B.",
            False,
        )

    if phase == "confirm":
        if lowered in {"iya", "ya", "yes", "ok", "simpan", "save"}:
            return "", True
        if lowered in {"tidak", "gak", "no", "batal"}:
            return "Kirim /batal untuk membatalkan.", False
        return "Kirim /simpan untuk menyimpan route ini, atau /batal.", False

    return "", False


def save_wizard(user_id: int, wizard: dict[str, Any]) -> str:
    """Persist the wizard's route and return the confirmation message."""
    if wizard.get("kind") == "alt" and wizard.get("group_id"):
        group = db.get_route_group(wizard["group_id"], user_id)
        if group is None:
            clear_wizard(user_id)
            return "Group route-nya sudah tidak ada. Mulai lagi dari awal ya."
        group_id, group_name = group["id"], group["name"]
    else:
        group_name = wizard.get("group_name") or "Route"
        group = db.route_group_by_name(user_id, group_name)
        if group is None:
            group_id = db.create_route_group(user_id, group_name)
        else:
            group_id = group["id"]
    alt_name = wizard.get("alt_name", "")
    origin = wizard["origin"]
    dest = wizard["destination"]
    wps = wizard["waypoints"]
    db.create_route(
        user_id, group_id, alt_name, origin[0], origin[1], dest[0], dest[1], wps
    )
    label = f"{group_name} via {alt_name}" if alt_name else group_name
    log_event(
        log, logging.INFO, "route_saved", user_id=user_id, route_label=label, waypoints=len(wps)
    )
    clear_wizard(user_id)
    return f"✅ Route \"{label}\" berhasil disimpan."