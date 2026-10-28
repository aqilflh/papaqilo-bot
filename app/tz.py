"""Timezone resolution, in its own module so nothing has to import the agent.

`tools` needs to render a reminder's local time, and `agent` needs the same
helper, but agent already imports tools for the tool registry. Keeping the
helper here breaks what would otherwise be a circular import.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: A bad or missing timezone in the database must never crash a message.
FALLBACK_TZ = "Asia/Jakarta"

#: strftime's %A and %B are locale-dependent and take no locale argument, so
#: they render "Tuesday" in an Indonesian chat. Name the days ourselves.
HARI = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]

BULAN = [
    "Januari",
    "Februari",
    "Maret",
    "April",
    "Mei",
    "Juni",
    "Juli",
    "Agustus",
    "September",
    "Oktober",
    "November",
    "Desember",
]


def hari_name(moment: datetime) -> str:
    """Indonesian weekday name for a Monday-indexed weekday()."""
    return HARI[moment.weekday()]


def bulan_name(month: int) -> str:
    """Indonesian month name, 1-based, for labels like 'setiap tahun 1 Februari'."""
    return BULAN[(int(month) - 1) % 12]


def user_zone(name: str | None) -> ZoneInfo:
    """A user's timezone, falling back to WIB rather than raising."""
    if not name:
        return ZoneInfo(FALLBACK_TZ)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(FALLBACK_TZ)
