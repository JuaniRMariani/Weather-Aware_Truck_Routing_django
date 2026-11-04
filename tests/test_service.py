from datetime import datetime, timedelta, timezone

import pytest

from routing.domain import METERS_PER_MILE, Place, Point, RoadRoute, Weather
from routing.ports import PlanningError
from routing.service import PlanRequest, RoutePlanner, weather_hour

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)


class Geocoder:
    def geocode(self, query):
        return Place(query, Point(40, -80) if query == "Origin" else Point(41, -81))


class Router:
    def routes(self, origin, destination):
        return [
            RoadRoute(
                (origin, Point(40.5 + index * 0.1, -80.5), destination),
                (25 * METERS_PER_MILE, 5 * METERS_PER_MILE),
                (3600, 1800),
                5400,
                30 * METERS_PER_MILE,
            )
            for index in range(3)
        ]


class Forecaster:
    def __init__(self, wind=0, missing=False):
        self.wind = wind
        self.missing = missing

    def forecasts(self, points):
        return {
            point: {
                NOW + timedelta(hours=index): Weather(self.wind, 0, 0) for index in range(49 if not self.missing else 1)
            }
            for point in points
        }


def test_complete_plan_contract_and_heatmap():
    request = PlanRequest("Origin", "Destination", NOW + timedelta(minutes=15), 30000, 25)
    result = RoutePlanner(Geocoder(), Router(), Forecaster()).plan(request, NOW)
    assert len(result["routes"]) == 3
    assert result["recommended_route_id"] == "route-1"
    assert len(result["heatmap"]["hours"]) == 49
    assert result["heatmap"]["hours"][-1] == (NOW + timedelta(hours=48)).isoformat()
    assert all(len(point["risks"]) == 49 for point in result["heatmap"]["points"])
    assert len(result["routes"][0]["checkpoints"]) == 3
    assert result["routes"][0]["geometry"][0] == [-80, 40]


def test_all_routes_no_travel_has_no_recommendation():
    request = PlanRequest("Origin", "Destination", NOW, 0, 25)
    result = RoutePlanner(Geocoder(), Router(), Forecaster(wind=55)).plan(request, NOW)
    assert result["recommended_route_id"] is None
    assert all(not route["eligible"] for route in result["routes"])


def test_missing_forecast_fails_explicitly():
    request = PlanRequest("Origin", "Destination", NOW, 0, 25)
    with pytest.raises(PlanningError, match="does not cover"):
        RoutePlanner(Geocoder(), Router(), Forecaster(missing=True)).plan(request, NOW)


def test_hour_matching_uses_preceding_interval_end():
    assert weather_hour(NOW) == NOW
    assert weather_hour(NOW + timedelta(seconds=1)) == NOW + timedelta(hours=1)
    assert weather_hour(NOW + timedelta(minutes=59)) == NOW + timedelta(hours=1)
