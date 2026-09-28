"""Daily SQLite backup using the online backup API.

A plain `cp` of a WAL-mode database can capture a torn state, so this uses
sqlite3's backup API, which takes a transactionally consistent snapshot.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from app import db
from app.log import get_logger, log_event

log = get_logger("backup")

KEEP = 7
BACKUP_HOUR = 3  # 03:00 WIB, well away from the 06:30 recap


def backup_dir(db_path: Path) -> Path:
    return Path(db_path).parent / "backups"


def run_backup(db_path: Path, now: datetime | None = None, keep: int = KEEP) -> Path | None:
    """Write data/backups/agent-YYYY-MM-DD.db and prune old ones.

    Returns the new file, or None if the backup failed (the caller logs; a
    failed backup must not raise into the scheduler).
    """
    now = now or datetime.now()
    target_dir = backup_dir(db_path)
    target = target_dir / f"agent-{now:%Y-%m-%d}.db"

    try:
        db.backup_to(target)
    except Exception as exc:  # noqa: BLE001
        log_event(log, logging.ERROR, "backup_failed", error=type(exc).__name__)
        return None

    prune(target_dir, keep=keep, now=now)
    log_event(log, logging.INFO, "backup_ok", file=target.name, bytes=target.stat().st_size)
    return target


def prune(target_dir: Path, keep: int = KEEP, now: datetime | None = None) -> list[Path]:
    """Keep the newest `keep` files, plus today's, and delete the rest."""
    now = now or datetime.now()
    today = f"agent-{now:%Y-%m-%d}.db"
    if not target_dir.exists():
        return []

    files = sorted(p for p in target_dir.glob("agent-*.db") if p.name != today)
    doomed = files[: max(0, len(files) - (keep - 1))]
    for path in doomed:
        try:
            path.unlink()
            log_event(log, logging.INFO, "backup_pruned", file=path.name)
        except OSError as exc:
            log_event(
                log,
                logging.WARNING,
                "backup_prune_failed",
                file=path.name,
                error=type(exc).__name__,
            )
    return doomed


def is_backup_due(now: datetime, last_run: datetime | None) -> bool:
    """True once per day, at or after BACKUP_HOUR.

    Uses >= rather than == so a container that was down at 03:00 still backs up
    when it comes back later that day.
    """
    if last_run is not None and last_run.date() == now.date():
        return False
    return now.hour >= BACKUP_HOUR
