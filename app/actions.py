"""Pending destructive actions that /hapus carries out after confirmation.

The LLM may delete a single clearly-named target directly (a shift date, one
reminder id, one note id). Anything broad or "all" is only *staged*, and the
staged work is executed by the /hapus command once the user confirms (or
discarded by /batal).

The staging store is per-user and in-memory, exactly like pending shift
imports: fine for the single-process bot, gone on restart, and never written
to disk.
"""

from __future__ import annotations

from typing import Any

from app import db

#: user_id -> {"kind": "shift"|"reminder"|"note", "targets": [(key, label), ...]}
PENDING: dict[int, dict[str, Any]] = {}


def stage(user_id: int, kind: str, targets: list[tuple[Any, str]]) -> None:
    """Queue a broad deletion as a /hapus-confirmed proposal."""
    PENDING[user_id] = {"kind": kind, "targets": targets}


def pending(user_id: int) -> dict[str, Any] | None:
    return PENDING.get(user_id)


def clear(user_id: int) -> None:
    PENDING.pop(user_id, None)


def proposal_text(targets: list[tuple[Any, str]]) -> str:
    """Preview of a staged deletion, ending with the /hapus confirmation ask."""
    lines = [f"- {label}" for _, label in targets]
    return "\n".join(lines) + "\n\nKirim /hapus untuk menghapus, atau /batal untuk membatalkan."


def apply(user_id: int, kind: str, key: Any) -> int:
    """Perform one staged deletion. Returns 1 when it existed, otherwise 0."""
    if kind == "shift":
        return int(db.delete_shift(user_id, str(key)))
    if kind == "reminder":
        return int(db.disable_reminder(int(key), user_id))
    if kind == "note":
        return int(db.delete_note(user_id, int(key)))
    if kind == "route":
        return int(db.delete_route(int(key), user_id))
    if kind == "route_group":
        return int(db.delete_route_group(int(key), user_id))
    return 0
