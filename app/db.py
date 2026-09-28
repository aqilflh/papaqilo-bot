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

# Each entry is (target_user_version, sql). Append only; never edit a shipped one.
MIGRATIONS: Sequence[tuple[int, str]] = ((1, _MIGRATION_1),)
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
        for table in ("messages", "notes", "schedules", "usage"):
            cur.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))
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
