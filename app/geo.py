"""Google Routes API client: distance and duration for one route.

The model is never allowed to invent an ETA. The only source of travel time in
this project is this module, which calls Google's Routes API with the exact
coordinates the user recorded via Telegram Location.

Coordinates are always sent as ``latLng`` waypoints, so no Geocoding API is
needed (and none is enabled on the project).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.log import get_logger, log_event

log = get_logger("geo")

#: computeRoutes endpoint. Field mask asks only for what we store.
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
FIELD_MASK = "routes.duration,routes.distanceMeters"

#: Google caps intermediates at 25 per request.
MAX_INTERMEDIATES = 25


class RouteError(Exception):
    """The routing provider could not answer. Safe to show to the user."""


def _latlng(lat: float, lng: float) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": float(lat), "longitude": float(lng)}}}


def _parse_seconds(value: str | int | None) -> int:
    """Google returns protobuf durations like ``"1660s"``."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(str(value).rstrip("s") or 0)


async def compute_route(
    http: httpx.AsyncClient,
    api_key: str,
    origin: tuple[float, float],
    waypoints: list[tuple[float, float]],
    destination: tuple[float, float],
) -> dict[str, int]:
    """Return ``{"distance_meters": int, "duration_seconds": int}``.

    Raises RouteError on any failure so callers can degrade gracefully instead
    of showing a made-up number.
    """
    if not api_key:
        raise RouteError("API key Google Maps belum diset.")

    body: dict[str, Any] = {
        "origin": _latlng(*origin),
        "destination": _latlng(*destination),
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_AWARE",
        "computeAlternativeRoutes": False,
        "languageCode": "id",
        "units": "METRIC",
    }
    if waypoints:
        body["intermediates"] = [_latlng(lat, lng) for lat, lng in waypoints][:MAX_INTERMEDIATES]

    headers = {"X-Goog-Api-Key": api_key, "X-Goog-FieldMask": FIELD_MASK}
    try:
        resp = await http.post(ROUTES_URL, json=body, headers=headers, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        log_event(log, logging.WARNING, "route_eta_failed", error=type(exc).__name__)
        raise RouteError(f"Gagal menghitung ETA ({type(exc).__name__}).") from exc

    routes = data.get("routes") or []
    if not routes:
        raise RouteError("Provider tidak mengembalikan rute.")
    route = routes[0]
    return {
        "distance_meters": int(route.get("distanceMeters") or 0),
        "duration_seconds": _parse_seconds(route.get("duration")),
    }
