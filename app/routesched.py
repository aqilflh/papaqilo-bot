"""Per-route-group daily reminder ("ingetin route pulang jam 07:00").

The reminder text is deterministic: at fire time it is the group's route status
built from the DB, so the ETA/distance shown is whatever the last Google compute
left. No LLM involved. Delivery reuses the scheduler's catch-up rule (">= the
target time, and not yet done today") plus a per-row claim, so a container that
was down at 07:00 still fires the reminder when it restarts.

The timezone logic (including per-user daily dedup) mirrors run_recaps.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from app import db, geo, routes
from app.log import get_logger, log_event
from app.scheduler import is_due
from app.tz import user_zone

log = get_logger("routesched")


class Sender(Protocol):
    """The slice of the bot that route reminders need."""

    async def send_message(self, user_id: int, text: str) -> None: ...


def reminder_text(user_id: int, group_id: int) -> str:
    """Compact status for one route group, or '' when the group is gone/empty."""
    group = db.get_route_group(group_id, user_id)
    if group is None:
        return ""
    rows = db.list_routes(group_id, user_id)
    if not rows:
        return ""
    name = group["name"]
    lines = [f"⏰ Route: {name}"]
    lines.extend(
        "  " + routes.route_summary(row, db.list_waypoints(row["id"], user_id)) for row in rows
    )
    lines.append(f'Matikan: "hapus reminder {name}".')
    return "\n".join(lines)


async def _refresh_group_eta(user_id: int, group_id: int, *, http: Any, maps_api_key: str) -> None:
    """Best-effort recompute of ETA/distance for every route in the group.

    Called at delivery time so the reminder shows current traffic instead of the
    numbers cached from the last manual "ETA route ...". Failures are logged and
    ignored: a reminder with a slightly stale ETA beats no reminder at all.
    """
    for route in db.list_routes(group_id, user_id):
        if route["origin_lat"] is None or route["destination_lat"] is None:
            continue
        waypoints = [
            (w["latitude"], w["longitude"]) for w in db.list_waypoints(route["id"], user_id)
        ]
        try:
            metrics = await geo.compute_route(
                http,
                maps_api_key,
                (route["origin_lat"], route["origin_lng"]),
                waypoints,
                (route["destination_lat"], route["destination_lng"]),
            )
            db.set_route_metrics(
                route["id"], user_id, metrics["distance_meters"], metrics["duration_seconds"]
            )
        except Exception as exc:  # noqa: BLE001 - never block the reminder
            log_event(
                log,
                logging.WARNING,
                "route_reminder_eta_failed",
                user_id=user_id,
                group_id=group_id,
                route_id=route["id"],
                error=type(exc).__name__,
            )


async def deliver_due_route_reminders(
    sender: Sender,
    *,
    http: Any | None = None,
    maps_api_key: str | None = None,
    now: datetime | None = None,
) -> int:
    """Deliver every due route reminder. Returns how many were sent."""
    now = now or datetime.now(UTC)
    sent = 0

    for row in db.list_due_route_reminders():
        user_id = int(row["user_id"])
        user = db.get_user(user_id)
        if user is None:
            continue
        tz = user_zone(user.get("timezone"))
        today = now.astimezone(tz).date().isoformat()
        group_id = int(row["route_group_id"])

        if not is_due({"time_hhmm": row["time_hhmm"]}, tz, now):
            continue
        if row.get("last_run_date") == today:
            continue

        # Claim before sending so a second tick cannot duplicate a reminder.
        if not db.claim_route_reminder_run(user_id, group_id, today):
            continue

        # Refresh ETA at fire time so the reminder reflects real traffic, not
        # the stale cached numbers. Falls back to cached values on failure.
        if http is not None and maps_api_key:
            await _refresh_group_eta(user_id, group_id, http=http, maps_api_key=maps_api_key)

        text = reminder_text(user_id, group_id)
        if not text:
            # The group is gone or has no routes; stop retrying it.
            db.delete_route_reminder(user_id, group_id)
            log_event(
                log,
                logging.INFO,
                "route_reminder_group_empty",
                user_id=user_id,
                group_id=group_id,
            )
            continue

        try:
            await sender.send_message(user_id, text)
        except Exception as exc:  # noqa: BLE001 - one bad send must not stop the tick
            log_event(
                log,
                logging.ERROR,
                "route_reminder_send_failed",
                user_id=user_id,
                group_id=group_id,
                error=type(exc).__name__,
            )
            # Release the claim so the next tick retries.
            db.release_route_reminder_claim(user_id, group_id, today)
            continue

        sent += 1
        log_event(
            log,
            logging.INFO,
            "route_reminder_sent",
            user_id=user_id,
            group_id=group_id,
            date=today,
        )

    return sent
