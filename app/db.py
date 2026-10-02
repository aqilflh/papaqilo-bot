"""SQLite storage: schema migrations and every data-access function.

Two invariants hold everywhere in this module:

1. **Isolation.** Every function takes ``user_id`` as its first argument and
   filters by it in SQL. Callers cannot ask for "all messages" because no such
   function exists. There is no raw SQL outside this file.
2. **Parameterized statements.** User input is always bound, never formatted
   into SQL text.

The connection is synchronous. SQLite operations here are sub-millisecond, and
wrapping them in a thread pool would add more complexity than it removes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


# --------------------------------------------------------------------------
# schema
# --------------------------------------------------------------------------

_MIGRATION_1 = """
CREATE TABLE users (
  id           INTEGER PRIMARY KEY,           -- Telegram user id
  name         TEXT NOT NULL,
  role         TEXT NOT NULL DEFAULT 'user',  -- 'owner' | 'user'
  timezone     TEXT NOT NULL DEFAULT 'Asia/Jakarta',
  city         TEXT,                          -- for weather
  daily_limit  INTEGER NOT NULL DEFAULT 50,
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  role       TEXT NOT NULL,                   -- 'user' | 'assistant'
  content    TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_messages_user ON messages(user_id, id);

CREATE TABLE notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id),
  content    TEXT NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_notes_user ON notes(user_id, id);

CREATE TABLE schedules (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  kind          TEXT NOT NULL,                -- 'daily_recap'
  time_hhmm     TEXT NOT NULL,                -- 'HH:MM' in the user's timezone
  enabled       INTEGER NOT NULL DEFAULT 1,
  config        TEXT,                         -- JSON
  last_run_date TEXT,                         -- 'YYYY-MM-DD', prevents double runs
  UNIQUE(user_id, kind)
);

CREATE TABLE usage (
  user_id    INTEGER NOT NULL REFERENCES users(id),
  day        TEXT NOT NULL,                  -- 'YYYY-MM-DD'
  msg_count  INTEGER NOT NULL DEFAULT 0,
  tokens_in  INTEGER NOT NULL DEFAULT 0,
  tokens_out INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (user_id, day)
);
"""

_MIGRATION_2 = """
CREATE TABLE reminders (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  text         TEXT NOT NULL,                 -- what to remind about
  repeat_kind  TEXT NOT NULL DEFAULT 'once',  -- 'once' | 'daily' | 'weekly'
  due_at       TEXT NOT NULL,                 -- ISO 8601 UTC, next occurrence
  local_hhmm   TEXT NOT NULL,                 -- 'HH:MM' in the user's timezone
  weekday      INTEGER,                       -- 0=Mon..6=Sun, weekly only
  active       INTEGER NOT NULL DEFAULT 1,
  last_sent_at TEXT,                          -- ISO 8601 UTC of last delivery
  created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
-- The delivery query is "active AND due_at <= now, oldest first".
CREATE INDEX idx_reminders_due ON reminders(active, due_at);
CREATE INDEX idx_reminders_user ON reminders(user_id, id);
"""

_MIGRATION_3 = """
CREATE TABLE shifts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id),
  shift_date  TEXT NOT NULL,           -- 'YYYY-MM-DD' (date the shift starts)
  kind        TEXT NOT NULL,           -- 'pagi' | 'siang' | 'malam' | 'libur'
  place       TEXT,                    -- 'igd' | 'klinik' | 'rs' ... lowercase
  note        TEXT,                    -- e.g. 'prolanis', 'ranap'
  partners    TEXT,                    -- comma-separated first names
  raw         TEXT NOT NULL,           -- the original pasted line
  UNIQUE(user_id, shift_date)
);
CREATE INDEX idx_shifts_user ON shifts(user_id, shift_date);

CREATE TABLE shift_settings (
  user_id      INTEGER PRIMARY KEY REFERENCES users(id),
  reminders_on INTEGER NOT NULL DEFAULT 1,
  eve_time     TEXT NOT NULL DEFAULT '20:00',  -- D-1 reminder time (user tz)
  before_min   INTEGER,                        -- day-of reminder N min before start, NULL = off
  hours_json   TEXT                            -- {"pagi":["07:00","14:00"],...}, NULL until set
);

CREATE TABLE shift_reminders_sent (
  user_id     INTEGER NOT NULL REFERENCES users(id),
  kind        TEXT NOT NULL,           -- 'eve' | 'before' | 'expiry'
  target_date TEXT NOT NULL,           -- 'YYYY-MM-DD'
  sent_at     TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, kind, target_date)
);
"""

_MIGRATION_4 = """
CREATE TABLE route_groups (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  description TEXT,
  created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_route_groups_user ON route_groups(user_id, name);

CREATE TABLE routes (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id         INTEGER NOT NULL REFERENCES route_groups(id) ON DELETE CASCADE,
  name             TEXT NOT NULL,            -- alternative label, e.g. 'via Pasar Minggu'
  origin_lat       REAL,
  origin_lng       REAL,
  destination_lat  REAL,
  destination_lng  REAL,
  created_at       TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_routes_user ON routes(user_id, group_id);

CREATE TABLE route_waypoints (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  route_id   INTEGER NOT NULL REFERENCES routes(id) ON DELETE CASCADE,
  sequence   INTEGER NOT NULL,
  latitude   REAL NOT NULL,
  longitude  REAL NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_waypoints_route ON route_waypoints(route_id, sequence);
"""

_MIGRATION_5 = """
ALTER TABLE routes ADD COLUMN distance_meters INTEGER;
ALTER TABLE routes ADD COLUMN duration_seconds INTEGER;
ALTER TABLE routes ADD COLUMN eta_updated_at TEXT;
"""

_MIGRATION_6 = """
CREATE TABLE route_reminders (
  user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  route_group_id  INTEGER NOT NULL REFERENCES route_groups(id) ON DELETE CASCADE,
  time_hhmm       TEXT NOT NULL,
  enabled         INTEGER NOT NULL DEFAULT 1,
  last_run_date   TEXT,
  created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at      TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id, route_group_id)
);
"""

# Each entry is (target_user_version, sql). Append only; never edit a shipped one.
MIGRATIONS: Sequence[tuple[int, str]] = (
    (1, _MIGRATION_1),
    (2, _MIGRATION_2),
    (3, _MIGRATION_3),
    (4, _MIGRATION_4),
    (5, _MIGRATION_5),
    (6, _MIGRATION_6),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


# --------------------------------------------------------------------------
# connection lifecycle
# --------------------------------------------------------------------------


def connect(path: Path) -> None:
    """Open (or reopen) the process-wide connection and run migrations."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        # foreign_keys is per-connection and off by default in sqlite3.
        # WAL lets the scheduler read while a request writes.
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        _conn = conn

    _migrate()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


@contextmanager
def _cursor() -> Iterator[sqlite3.Cursor]:
    if _conn is None:
        raise RuntimeError("db.connect() has not been called")
    with _lock:
        cur = _conn.cursor()
        try:
            yield cur
        finally:
            cur.close()


@contextmanager
def _transaction() -> Iterator[sqlite3.Cursor]:
    """Run a block inside one explicit transaction.

    The connection uses isolation_level=None (autocommit), so Python will not
    commit for us. Every BEGIN therefore needs a matching COMMIT, otherwise the
    open transaction leaks and the next BEGIN raises "cannot start a
    transaction within a transaction".

    IMMEDIATE takes the write lock up front, so two concurrent claims cannot
    both read-then-write.
    """
    if _conn is None:
        raise RuntimeError("db.connect() has not been called")
    with _lock:
        cur = _conn.cursor()
        try:
            cur.execute("BEGIN IMMEDIATE")
            yield cur
            cur.execute("COMMIT")
        except BaseException:
            # Nothing may have been open; the original error is the useful one.
            with suppress(sqlite3.Error):
                cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()


def _migrate() -> None:
    """Apply pending migrations, tracked in PRAGMA user_version.

    Statements are executed one by one inside a single transaction rather than
    via executescript(), because executescript() commits first and would drop
    the atomicity the surrounding BEGIN relies on.
    """
    with _transaction() as cur:
        current = int(cur.execute("PRAGMA user_version").fetchone()[0])
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            for statement in _split_statements(sql):
                cur.execute(statement)
            # version is an int we control, not user input.
            cur.execute(f"PRAGMA user_version = {int(version)}")


def _split_statements(sql: str) -> list[str]:
    """Split a migration blob into individual statements."""
    return [stmt.strip() for stmt in sql.split(";") if stmt.strip()]


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------


def upsert_user(
    user_id: int,
    name: str,
    role: str = "user",
    timezone: str = "Asia/Jakarta",
    daily_limit: int = 50,
) -> None:
    """Create the user, or refresh name/role without touching their settings."""
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (id, name, role, timezone, daily_limit)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name  = excluded.name,
                role  = excluded.role
            """,
            (user_id, name, role, timezone, daily_limit),
        )


def get_user(user_id: int) -> dict[str, Any] | None:
    with _cursor() as cur:
        row = cur.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return _row_to_dict(row)


def list_users() -> list[dict[str, Any]]:
    with _cursor() as cur:
        rows = cur.execute("SELECT * FROM users ORDER BY role DESC, id").fetchall()
    return [dict(r) for r in rows]


def user_exists(user_id: int) -> bool:
    with _cursor() as cur:
        row = cur.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None


def set_city(user_id: int, city: str | None) -> None:
    """Save or clear the user's weather city."""
    with _cursor() as cur:
        cur.execute("UPDATE users SET city = ? WHERE id = ?", (city, user_id))


def set_timezone(user_id: int, timezone: str) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE users SET timezone = ? WHERE id = ?", (timezone, user_id))


def set_daily_limit(user_id: int, daily_limit: int) -> None:
    with _cursor() as cur:
        cur.execute("UPDATE users SET daily_limit = ? WHERE id = ?", (daily_limit, user_id))


def remove_user(user_id: int) -> int:
    """Delete a user and cascade their rows. Returns messages removed."""
    with _transaction() as cur:
        cur.execute("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,))
        removed = int(cur.fetchone()[0])
        # ON DELETE CASCADE is not declared, so delete children explicitly.
        for table in (
            "messages",
            "notes",
            "schedules",
            "usage",
            "reminders",
            "shifts",
            "shift_settings",
            "shift_reminders_sent",
            "routes",
            "route_groups",
        ):
            cur.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
        cur.execute(
            "DELETE FROM route_waypoints WHERE route_id IN "
            "(SELECT id FROM routes WHERE user_id = ?)",
            (user_id,),
        )
        cur.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return removed


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


def add_message(user_id: int, role: str, content: str) -> int:
    """Append one chat message. Returns its row id."""
    with _cursor() as cur:
        cur = cur.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        return int(cur.lastrowid or 0)


def recent_messages(user_id: int, limit: int) -> list[dict[str, Any]]:
    """Return the most recent `limit` messages, oldest first."""
    with _cursor() as cur:
        rows = cur.execute(
            """
            SELECT role, content FROM (
                SELECT role, content, id
                FROM messages WHERE user_id = ?
                ORDER BY id DESC LIMIT ?
            ) ORDER BY id ASC
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def count_messages(user_id: int) -> int:
    with _cursor() as cur:
        row = cur.execute("SELECT COUNT(*) FROM messages WHERE user_id = ?", (user_id,)).fetchone()
    return int(row[0])


def delete_history(user_id: int) -> int:
    """Wipe this user's chat history. Notes are kept."""
    with _cursor() as cur:
        cur.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        return cur.rowcount


# --------------------------------------------------------------------------
# notes
# --------------------------------------------------------------------------


def add_note(user_id: int, content: str) -> int:
    with _cursor() as cur:
        cur.execute("INSERT INTO notes (user_id, content) VALUES (?, ?)", (user_id, content))
        return int(cur.lastrowid or 0)


def list_notes(user_id: int, query: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """List this user's notes, newest first. `query` is a substring filter."""
    with _cursor() as cur:
        if query:
            rows = cur.execute(
                """
                SELECT id, content, created_at FROM notes
                WHERE user_id = ? AND content LIKE ? ESCAPE '\\'
                ORDER BY id DESC LIMIT ?
                """,
                (user_id, f"%{_escape_like(query)}%", limit),
            ).fetchall()
        else:
            rows = cur.execute(
                """
                SELECT id, content, created_at FROM notes
                WHERE user_id = ? ORDER BY id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
    return [dict(r) for r in rows]


def delete_note(user_id: int, note_id: int) -> bool:
    """Delete one note belonging to this user. False if it is not theirs."""
    with _cursor() as cur:
        cur.execute("DELETE FROM notes WHERE id = ? AND user_id = ?", (note_id, user_id))
        return cur.rowcount > 0


def _escape_like(text: str) -> str:
    """Escape LIKE wildcards so a user searching '100%' does not match everything."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------
# reminders
# --------------------------------------------------------------------------

REPEAT_KINDS = ("once", "daily", "weekly")


def add_reminder(
    user_id: int,
    text: str,
    repeat_kind: str,
    due_at: datetime,
    local_hhmm: str,
    weekday: int | None = None,
) -> int:
    """Store a reminder. `due_at` is UTC; the local fields drive recurrence.

    Store UTC for delivery but keep the user's wall-clock time, otherwise a
    daily 07:00 WIB reminder would drift by the UTC offset once a DST-shifting
    zone is involved.
    """
    if repeat_kind not in REPEAT_KINDS:
        raise ValueError(f"repeat_kind must be one of {REPEAT_KINDS}, got {repeat_kind!r}")
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO reminders (user_id, text, repeat_kind, due_at, local_hhmm, weekday)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                text,
                repeat_kind,
                due_at.astimezone(UTC).isoformat(timespec="seconds"),
                local_hhmm,
                weekday,
            ),
        )
        return int(cur.lastrowid or 0)


def list_reminders(user_id: int, active_only: bool = True) -> list[dict[str, Any]]:
    """This user's reminders, soonest due first."""
    sql = "SELECT * FROM reminders WHERE user_id = ?"
    if active_only:
        sql += " AND active = 1"
    sql += " ORDER BY due_at ASC, id ASC"
    with _cursor() as cur:
        rows = cur.execute(sql, (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_reminder(reminder_id: int, user_id: int | None = None) -> dict[str, Any] | None:
    """One reminder by id, optionally scoped to an owner so users cannot read
    each other's rows."""
    sql = "SELECT * FROM reminders WHERE id = ?"
    params: tuple[Any, ...] = (reminder_id,)
    if user_id is not None:
        sql += " AND user_id = ?"
        params = (reminder_id, user_id)
    with _cursor() as cur:
        row = cur.execute(sql, params).fetchone()
    return _row_to_dict(row)


def disable_reminder(reminder_id: int, user_id: int | None = None) -> bool:
    """Stop a reminder without deleting history. False if not found or not theirs."""
    sql = "UPDATE reminders SET active = 0 WHERE id = ? AND active = 1"
    params: tuple[Any, ...] = (reminder_id,)
    if user_id is not None:
        sql += " AND user_id = ?"
        params = (reminder_id, user_id)
    with _cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount > 0


def delete_reminder(reminder_id: int, user_id: int) -> bool:
    with _cursor() as cur:
        cur.execute("DELETE FROM reminders WHERE id = ? AND user_id = ?", (reminder_id, user_id))
        return cur.rowcount > 0


def disable_all_reminders(user_id: int) -> int:
    """Cancel every active reminder for one user. Returns how many were hit.

    Scoped by user_id on purpose: a bulk delete that ignored it would let one
    user wipe everyone's reminders.
    """
    with _cursor() as cur:
        cur.execute("UPDATE reminders SET active = 0 WHERE user_id = ? AND active = 1", (user_id,))
        return cur.rowcount


def claim_due_reminders(now: datetime) -> list[dict[str, Any]]:
    """Claim every reminder whose due_at has passed and return them.

    Marks `last_sent_at` inside the same transaction as the read, so two ticks
    racing cannot both deliver the same reminder. Advancing the recurrence is
    the caller's job, and it must happen even if sending later fails, otherwise
    a Telegram outage would replay yesterday's reminder forever.
    """
    stamp = now.astimezone(UTC).isoformat(timespec="seconds")
    with _transaction() as cur:
        rows = cur.execute(
            """
            SELECT * FROM reminders
            WHERE active = 1 AND due_at <= ?
              AND (last_sent_at IS NULL OR last_sent_at != ?)
            ORDER BY due_at ASC, id ASC
            """,
            (stamp, stamp),
        ).fetchall()
        claimed = [dict(r) for r in rows]
        if claimed:
            cur.executemany(
                "UPDATE reminders SET last_sent_at = ? WHERE id = ?",
                [(stamp, r["id"]) for r in claimed],
            )
    # Hand back the stamp we just wrote, not the pre-update value, so the
    # caller can pass it to release_reminder_claim on a send failure.
    for item in claimed:
        item["last_sent_at"] = stamp
    return claimed


def advance_reminder(reminder_id: int, due_at: datetime) -> None:
    """Move a reminder to its next occurrence after delivery."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE reminders SET due_at = ? WHERE id = ?",
            (due_at.astimezone(UTC).isoformat(timespec="seconds"), reminder_id),
        )


def release_reminder_claim(reminder_id: int, sent_at: str) -> None:
    """Undo a claim when delivery failed, so the next tick retries."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE reminders SET last_sent_at = NULL WHERE id = ? AND last_sent_at = ?",
            (reminder_id, sent_at),
        )


# --------------------------------------------------------------------------
# schedules
# --------------------------------------------------------------------------


def get_schedule(user_id: int, kind: str) -> dict[str, Any] | None:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT * FROM schedules WHERE user_id = ? AND kind = ?", (user_id, kind)
        ).fetchone()
    result = _row_to_dict(row)
    if result is not None and result.get("config"):
        try:
            result["config"] = json.loads(result["config"])
        except json.JSONDecodeError:
            result["config"] = {}
    return result


def set_schedule(
    user_id: int,
    kind: str,
    time_hhmm: str,
    enabled: bool = True,
    config: dict[str, Any] | None = None,
) -> None:
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO schedules (user_id, kind, time_hhmm, enabled, config)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, kind) DO UPDATE SET
                time_hhmm = excluded.time_hhmm,
                enabled    = excluded.enabled,
                config     = excluded.config
            """,
            (user_id, kind, time_hhmm, int(enabled), json.dumps(config or {})),
        )


def set_schedule_enabled(user_id: int, kind: str, enabled: bool) -> None:
    with _cursor() as cur:
        cur.execute(
            "UPDATE schedules SET enabled = ? WHERE user_id = ? AND kind = ?",
            (int(enabled), user_id, kind),
        )


def list_due_schedules(kind: str) -> list[dict[str, Any]]:
    """All enabled schedules of a kind. The caller applies timezone logic."""
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM schedules WHERE kind = ? AND enabled = 1", (kind,)
        ).fetchall()
    results = []
    for row in rows:
        item = dict(row)
        if item.get("config"):
            try:
                item["config"] = json.loads(item["config"])
            except json.JSONDecodeError:
                item["config"] = {}
        results.append(item)
    return results


def claim_schedule_run(user_id: int, kind: str, run_date: str) -> bool:
    """Atomically claim today's run. True if this caller won the claim.

    The UPDATE only matches when last_run_date differs from run_date, so two
    ticks firing at once cannot both send a recap.
    """
    with _transaction() as cur:
        cur.execute(
            """
            UPDATE schedules SET last_run_date = ?
            WHERE user_id = ? AND kind = ?
              AND enabled = 1
              AND (last_run_date IS NULL OR last_run_date != ?)
            """,
            (run_date, user_id, kind, run_date),
        )
        won = cur.rowcount == 1
    return won


def release_schedule_claim(user_id: int, kind: str, run_date: str) -> None:
    """Undo a claim so a failed run can be retried.

    run_date must be the same value that was claimed, which is the date in the
    user's timezone, not UTC. Only clears when the column already holds that
    date, so a concurrent successful run is never rolled back.
    """
    with _cursor() as cur:
        cur.execute(
            """
            UPDATE schedules SET last_run_date = NULL
            WHERE user_id = ? AND kind = ? AND last_run_date = ?
            """,
            (user_id, kind, run_date),
        )


# --------------------------------------------------------------------------
# usage / daily limits
# --------------------------------------------------------------------------


def local_day(tz: tzinfo, now: datetime | None = None) -> str:
    """The 'YYYY-MM-DD' counter key for a timezone.

    Callers pass the *user's* timezone, not UTC: a daily limit that resets at
    07:00 WIB is friendlier than one that resets at 07:00 UTC. `now` exists so
    tests can pin the clock.
    """
    return (now or datetime.now(tz)).astimezone(tz).date().isoformat()


def get_usage(user_id: int, day: str) -> dict[str, int]:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT msg_count, tokens_in, tokens_out FROM usage WHERE user_id = ? AND day = ?",
            (user_id, day),
        ).fetchone()
    if row is None:
        return {"msg_count": 0, "tokens_in": 0, "tokens_out": 0}
    return {k: int(row[k]) for k in ("msg_count", "tokens_in", "tokens_out")}


def usage_remaining(user_id: int, day: str) -> int:
    """Messages left today. Never negative."""
    user = get_user(user_id)
    if user is None:
        return 0
    limit = int(user["daily_limit"])
    return max(0, limit - get_usage(user_id, day)["msg_count"])


def record_usage(
    user_id: int,
    day: str,
    msg_count: int = 0,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """Add to today's counters, creating the row on first use."""
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO usage (user_id, day, msg_count, tokens_in, tokens_out)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET
                msg_count  = usage.msg_count  + excluded.msg_count,
                tokens_in  = usage.tokens_in  + excluded.tokens_in,
                tokens_out = usage.tokens_out + excluded.tokens_out
            """,
            (user_id, day, msg_count, tokens_in, tokens_out),
        )


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def backup_to(dest: Path) -> None:
    """Write a consistent copy using SQLite's online backup API.

    A plain file copy of a WAL-mode database can capture a torn state, so use
    the backup API instead.
    """
    if _conn is None:
        raise RuntimeError("db.connect() has not been called")
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        target = sqlite3.connect(dest)
        try:
            _conn.backup(target)
        finally:
            target.close()


def now_utc_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# shift schedules
# --------------------------------------------------------------------------

SHIFT_KINDS = ("pagi", "siang", "malam", "libur")
DEFAULT_EVE_TIME = "20:00"


def upsert_shift(
    user_id: int,
    shift_date: str,
    kind: str,
    place: str | None = None,
    note: str | None = None,
    partners: Sequence[str] | None = None,
    raw: str = "",
) -> None:
    """Insert a shift, or overwrite an existing one for the same date.

    Upsert by (user_id, shift_date) on purpose: the import flow re-pastes a
    whole month and an edited line must replace the old one, not duplicate it.
    """
    partners_csv = ", ".join(partners) if partners else None
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO shifts (user_id, shift_date, kind, place, note, partners, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, shift_date) DO UPDATE SET
                kind     = excluded.kind,
                place    = excluded.place,
                note     = excluded.note,
                partners = excluded.partners,
                raw      = excluded.raw
            """,
            (user_id, shift_date, kind, place, note, partners_csv, raw),
        )


def delete_shift(user_id: int, shift_date: str) -> bool:
    """Delete one shift. False if it does not exist or belongs to someone else."""
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM shifts WHERE user_id = ? AND shift_date = ?", (user_id, shift_date)
        )
        return cur.rowcount > 0


def delete_shifts_range(user_id: int, start_date: str, end_date: str) -> int:
    """Delete every shift in a closed date range for one user."""
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM shifts WHERE user_id = ? AND shift_date BETWEEN ? AND ?",
            (user_id, start_date, end_date),
        )
        return cur.rowcount


def delete_all_shifts(user_id: int) -> int:
    """Delete every shift this user has stored."""
    with _cursor() as cur:
        cur.execute("DELETE FROM shifts WHERE user_id = ?", (user_id,))
        return cur.rowcount


def get_shift(user_id: int, shift_date: str) -> dict[str, Any] | None:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT * FROM shifts WHERE user_id = ? AND shift_date = ?", (user_id, shift_date)
        ).fetchone()
    return _row_to_dict(row)


def list_shifts(user_id: int, start_date: str, end_date: str | None = None) -> list[dict[str, Any]]:
    """This user's shifts from a start date, optionally capped by an end date."""
    sql = "SELECT * FROM shifts WHERE user_id = ? AND shift_date >= ?"
    params: list[Any] = [user_id, start_date]
    if end_date is not None:
        sql += " AND shift_date <= ?"
        params.append(end_date)
    sql += " ORDER BY shift_date ASC"
    with _cursor() as cur:
        rows = cur.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def last_shift_date(user_id: int) -> str | None:
    """The furthest-out stored shift, used for the expiry warning."""
    with _cursor() as cur:
        row = cur.execute(
            "SELECT MAX(shift_date) FROM shifts WHERE user_id = ?", (user_id,)
        ).fetchone()
    value = row[0] if row else None
    return value if value is not None else None


def shift_user_ids() -> list[int]:
    """Every user who owns at least one shift row."""
    with _cursor() as cur:
        rows = cur.execute("SELECT DISTINCT user_id FROM shifts").fetchall()
    return [int(r[0]) for r in rows]


def get_shift_settings(user_id: int) -> dict[str, Any]:
    """The user's reminder settings with sane defaults when never touched.

    `hours` is always a dict so the rest of the code can call .get() freely.
    """
    with _cursor() as cur:
        row = cur.execute("SELECT * FROM shift_settings WHERE user_id = ?", (user_id,)).fetchone()
    result = _row_to_dict(row)
    if result is None:
        return {
            "reminders_on": True,
            "eve_time": DEFAULT_EVE_TIME,
            "before_min": None,
            "hours": {},
        }
    hours: dict[str, str] = {}
    if result.get("hours_json"):
        try:
            parsed = json.loads(result["hours_json"])
            if isinstance(parsed, dict):
                hours = {str(k): str(v) for k, v in parsed.items()}
        except json.JSONDecodeError:
            hours = {}
    return {
        "reminders_on": bool(result.get("reminders_on", 1)),
        "eve_time": str(result.get("eve_time") or DEFAULT_EVE_TIME),
        "before_min": (int(result["before_min"]) if result.get("before_min") is not None else None),
        "hours": hours,
    }


def _put_shift_settings(user_id: int, fields: dict[str, Any]) -> None:
    columns = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    updates = ", ".join(f"{key} = excluded.{key}" for key in fields)
    with _cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO shift_settings (user_id, {columns})
            VALUES (?, {placeholders})
            ON CONFLICT(user_id) DO UPDATE SET {updates}
            """,
            (user_id, *fields.values()),
        )


def set_shift_reminders_on(user_id: int, enabled: bool) -> None:
    _put_shift_settings(user_id, {"reminders_on": int(enabled)})


def set_shift_eve_time(user_id: int, hhmm: str) -> None:
    _put_shift_settings(user_id, {"eve_time": hhmm})


def set_shift_before_min(user_id: int, minutes: int | None) -> None:
    _put_shift_settings(user_id, {"before_min": minutes})


def set_shift_hours(user_id: int, hours: dict[str, str]) -> None:
    """Replace every kind's hours at once (the /jam command edits one key)."""
    _put_shift_settings(user_id, {"hours_json": json.dumps(hours)})


def claim_shift_reminder(user_id: int, kind: str, target_date: str) -> bool:
    """Claim a reminder delivery. False if it was already sent.

    Like claim_due_reminders, the PK does the dedup: INSERT OR IGNORE succeeds
    (rowcount 1) only the first time, so Duplicate across restarts cannot fire
    twice even if two ticks race.
    """
    with _transaction() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO shift_reminders_sent "
            "(user_id, kind, target_date) VALUES (?, ?, ?)",
            (user_id, kind, target_date),
        )
        return cur.rowcount == 1


def release_shift_reminder(user_id: int, kind: str, target_date: str) -> None:
    """Undo a claim after a send failure so the next tick retries."""
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM shift_reminders_sent WHERE user_id = ? AND kind = ? AND target_date = ?",
            (user_id, kind, target_date),
        )


# --------------------------------------------------------------------------
# routes (route groups -> alternatives -> waypoints)
# --------------------------------------------------------------------------


def create_route_group(user_id: int, name: str, description: str | None = None) -> int:
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO route_groups (user_id, name, description) VALUES (?, ?, ?)",
            (user_id, name.strip(), description),
        )
        return int(cur.lastrowid or 0)


def get_route_group(group_id: int, user_id: int | None = None) -> dict[str, Any] | None:
    sql = "SELECT * FROM route_groups WHERE id = ?"
    params: list[Any] = [group_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    with _cursor() as cur:
        row = cur.execute(sql, params).fetchone()
    return _row_to_dict(row)


def route_group_by_name(user_id: int, name: str) -> dict[str, Any] | None:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT * FROM route_groups WHERE user_id = ? AND lower(name) = lower(?)",
            (user_id, name.strip()),
        ).fetchone()
    return _row_to_dict(row)


def list_route_groups(user_id: int) -> list[dict[str, Any]]:
    with _cursor() as cur:
        rows = cur.execute(
            """
            SELECT g.*, COUNT(r.id) AS route_count
            FROM route_groups g
            LEFT JOIN routes r ON r.group_id = g.id
            WHERE g.user_id = ?
            GROUP BY g.id
            ORDER BY g.name COLLATE NOCASE ASC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def rename_route_group(group_id: int, user_id: int, name: str) -> bool:
    with _cursor() as cur:
        cur.execute(
            "UPDATE route_groups SET name = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            (name.strip(), group_id, user_id),
        )
        return cur.rowcount > 0


def delete_route_group(group_id: int, user_id: int) -> bool:
    """Delete a group and every route + waypoint under it."""
    with _transaction() as cur:
        groupe = cur.execute(
            "SELECT id FROM route_groups WHERE id = ? AND user_id = ?", (group_id, user_id)
        ).fetchone()
        if groupe is None:
            return False
        cur.execute(
            "DELETE FROM route_waypoints WHERE route_id IN "
            "(SELECT id FROM routes WHERE group_id = ? AND user_id = ?)",
            (group_id, user_id),
        )
        cur.execute("DELETE FROM routes WHERE group_id = ? AND user_id = ?", (group_id, user_id))
        cur.execute("DELETE FROM route_groups WHERE id = ? AND user_id = ?", (group_id, user_id))
    return True


def create_route(
    user_id: int,
    group_id: int,
    name: str,
    origin_lat: float,
    origin_lng: float,
    destination_lat: float,
    destination_lng: float,
    waypoints: Sequence[tuple[float, float]],
) -> int:
    """Persist one alternative route (origin, waypoints, destination)."""
    with _transaction() as cur:
        cur.execute(
            """
            INSERT INTO routes (user_id, group_id, name, origin_lat, origin_lng,
                                destination_lat, destination_lng)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                group_id,
                name.strip(),
                origin_lat,
                origin_lng,
                destination_lat,
                destination_lng,
            ),
        )
        route_id = int(cur.lastrowid or 0)
        for seq, (lat, lng) in enumerate(waypoints, start=1):
            cur.execute(
                "INSERT INTO route_waypoints (route_id, sequence, latitude, longitude) "
                "VALUES (?, ?, ?, ?)",
                (route_id, seq, lat, lng),
            )
    return route_id


def get_route(route_id: int, user_id: int | None = None) -> dict[str, Any] | None:
    sql = "SELECT * FROM routes WHERE id = ?"
    params: list[Any] = [route_id]
    if user_id is not None:
        sql += " AND user_id = ?"
        params.append(user_id)
    with _cursor() as cur:
        row = cur.execute(sql, params).fetchone()
    return _row_to_dict(row)


def route_by_name(user_id: int, name: str) -> dict[str, Any] | None:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT * FROM routes WHERE user_id = ? AND lower(name) = lower(?)",
            (user_id, name.strip()),
        ).fetchone()
    return _row_to_dict(row)


def list_routes(group_id: int, user_id: int) -> list[dict[str, Any]]:
    with _cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM routes WHERE group_id = ? AND user_id = ? ORDER BY id ASC",
            (group_id, user_id),
        ).fetchall()
    return [dict(r) for r in rows]


def rename_route(route_id: int, user_id: int, name: str) -> bool:
    with _cursor() as cur:
        cur.execute(
            "UPDATE routes SET name = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ? AND user_id = ?",
            (name.strip(), route_id, user_id),
        )
        return cur.rowcount > 0


def delete_route(route_id: int, user_id: int) -> bool:
    """Delete one alternative route (and its waypoints)."""
    with _transaction() as cur:
        owned = cur.execute(
            "SELECT id FROM routes WHERE id = ? AND user_id = ?", (route_id, user_id)
        ).fetchone()
        if owned is None:
            return False
        cur.execute("DELETE FROM route_waypoints WHERE route_id = ?", (route_id,))
        cur.execute("DELETE FROM routes WHERE id = ? AND user_id = ?", (route_id, user_id))
    return True


def list_waypoints(route_id: int, user_id: int) -> list[dict[str, Any]]:
    with _cursor() as cur:
        rows = cur.execute(
            """
            SELECT w.* FROM route_waypoints w
            JOIN routes r ON r.id = w.route_id
            WHERE w.route_id = ? AND r.user_id = ?
            ORDER BY w.sequence ASC
            """,
            (route_id, user_id),
        ).fetchall()
    return [dict(r) for r in rows]


def set_route_metrics(
    route_id: int,
    user_id: int,
    distance_meters: int,
    duration_seconds: int,
) -> bool:
    """Store the routing provider's distance/duration for one route."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE routes SET distance_meters = ?, duration_seconds = ?, "
            "eta_updated_at = ? WHERE id = ? AND user_id = ?",
            (int(distance_meters), int(duration_seconds), now_utc_iso(), route_id, user_id),
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------
# route reminders (per route group, daily at a chosen time)
# --------------------------------------------------------------------------


def set_route_reminder(user_id: int, route_group_id: int, time_hhmm: str) -> None:
    """Create/update a daily reminder for one route group."""
    with _cursor() as cur:
        cur.execute(
            """
            INSERT INTO route_reminders (user_id, route_group_id, time_hhmm, enabled)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(user_id, route_group_id) DO UPDATE SET
                time_hhmm = excluded.time_hhmm,
                enabled   = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (user_id, route_group_id, time_hhmm),
        )


def get_route_reminder(user_id: int, route_group_id: int) -> dict[str, Any] | None:
    with _cursor() as cur:
        row = cur.execute(
            "SELECT * FROM route_reminders WHERE user_id = ? AND route_group_id = ?",
            (user_id, route_group_id),
        ).fetchone()
    return _row_to_dict(row)


def delete_route_reminder(user_id: int, route_group_id: int) -> bool:
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM route_reminders WHERE user_id = ? AND route_group_id = ?",
            (user_id, route_group_id),
        )
        return cur.rowcount > 0


def list_route_reminders(user_id: int) -> list[dict[str, Any]]:
    """All reminders for a user, ordered by time, with the group name."""
    with _cursor() as cur:
        rows = cur.execute(
            """
            SELECT rr.user_id, rr.route_group_id, rr.time_hhmm, rr.enabled,
                   rr.last_run_date, g.name AS group_name
            FROM route_reminders rr
            JOIN route_groups g ON g.id = rr.route_group_id
            WHERE rr.user_id = ?
            ORDER BY rr.time_hhmm ASC
            """,
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_due_route_reminders() -> list[dict[str, Any]]:
    """Every enabled reminder. The caller applies per-user timezone logic."""
    with _cursor() as cur:
        rows = cur.execute("SELECT * FROM route_reminders WHERE enabled = 1").fetchall()
    return [dict(r) for r in rows]


def claim_route_reminder_run(user_id: int, route_group_id: int, run_date: str) -> bool:
    """Atomically claim today's delivery. True if this caller won the claim."""
    with _transaction() as cur:
        cur.execute(
            """
            UPDATE route_reminders SET last_run_date = ?
            WHERE user_id = ? AND route_group_id = ?
              AND enabled = 1
              AND (last_run_date IS NULL OR last_run_date != ?)
            """,
            (run_date, user_id, route_group_id, run_date),
        )
        won = cur.rowcount == 1
    return won


def release_route_reminder_claim(user_id: int, route_group_id: int, run_date: str) -> None:
    """Undo a claim so a failed delivery can be retried."""
    with _cursor() as cur:
        cur.execute(
            "UPDATE route_reminders SET last_run_date = NULL "
            "WHERE user_id = ? AND route_group_id = ? AND last_run_date = ?",
            (user_id, route_group_id, run_date),
        )
