"""Google Routes API client: request shape and response parsing."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app import geo


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_compute_route_parses_duration_and_distance():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["origin"]["location"]["latLng"]["latitude"] == pytest.approx(-6.1)
        assert body["travelMode"] == "DRIVE"
        assert len(body["intermediates"]) == 2
        return httpx.Response(200, json={"routes": [{"duration": "1660s", "distanceMeters": 9736}]})

    http = _client(handler)
    metrics = asyncio.run(
        geo.compute_route(
            http,
            "key",
            (-6.1, 106.7),
            [(-6.11, 106.71), (-6.12, 106.72)],
            (-6.13, 106.73),
        )
    )
    assert metrics == {"distance_meters": 9736, "duration_seconds": 1660}


def test_compute_route_requires_key():
    with pytest.raises(geo.RouteError):
        asyncio.run(
            geo.compute_route(
                _client(lambda r: httpx.Response(200, json={})), "", (-6.1, 106.7), [], (-6.13, 106.73)
            )
        )


def test_compute_route_empty_routes_raises():
    with pytest.raises(geo.RouteError):
        asyncio.run(
            geo.compute_route(
                _client(lambda r: httpx.Response(200, json={"routes": []})),
                "key",
                (-6.1, 106.7),
                [],
                (-6.13, 106.73),
            )
        )


def test_compute_route_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad"}})

    with pytest.raises(geo.RouteError):
        asyncio.run(
            geo.compute_route(_client(handler), "key", (-6.1, 106.7), [], (-6.13, 106.73))
        )
