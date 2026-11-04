from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from routing.domain import Point
from routing.ports import PlanningError
from routing.providers import OpenMeteoForecaster, OpenMeteoGeocoder, OsrmRouter


def weather_payload(rain=0, showers=0, snow=0, wind=0):
    return {
        "hourly_units": {"wind_speed_10m": "mp/h", "rain": "mm", "showers": "mm", "snowfall": "cm"},
        "hourly": {
            "time": ["2026-11-04T12:00"],
            "wind_speed_10m": [wind],
            "rain": [rain],
            "showers": [showers],
            "snowfall": [snow],
        },
    }


def test_weather_units_include_convective_rain():
    hours = OpenMeteoForecaster._parse_hours(weather_payload(rain=2.54, showers=2.54, snow=2.54, wind=55))
    weather = hours[datetime(2026, 11, 4, 12, tzinfo=timezone.utc)]
    assert weather.wind_mph == 55
    assert weather.rain_in_per_hour == pytest.approx(0.2)
    assert weather.snow_in_per_hour == pytest.approx(1)


def test_missing_weather_is_not_low():
    assert OpenMeteoForecaster._parse_hours(weather_payload(wind=None)) == {}


def test_unexpected_units_rejected():
    payload = weather_payload()
    payload["hourly_units"]["snowfall"] = "inch"
    with pytest.raises(ValueError):
        OpenMeteoForecaster._parse_hours(payload)


def test_geocoder_preserves_state_qualifier():
    payload = {
        "results": [
            {
                "name": "Springfield",
                "admin1": "Massachusetts",
                "country": "United States",
                "latitude": 1,
                "longitude": 2,
            },
            {"name": "Springfield", "admin1": "Illinois", "country": "United States", "latitude": 3, "longitude": 4},
        ]
    }
    with patch("routing.providers.get_json", return_value=payload):
        result = OpenMeteoGeocoder("unused").geocode("Springfield, IL")
    assert result.point == Point(3, 4)


def test_insufficient_routes_does_not_duplicate_fastest():
    with patch.object(OsrmRouter, "_request", return_value=[]):
        with pytest.raises(PlanningError, match="three distinct"):
            OsrmRouter("unused").routes(Point(40, -80), Point(41, -82))


def test_malformed_route_annotation_rejected():
    payload = {
        "geometry": {"coordinates": [[0, 0], [1, 1]]},
        "legs": [{"annotation": {"distance": [10, 20], "duration": [1, 2]}}],
        "distance": 30,
        "duration": 3,
    }
    with pytest.raises(ValueError, match="annotation"):
        OsrmRouter._parse_route(payload)


def test_empty_geocoding_query_returns_controlled_error():
    with pytest.raises(PlanningError, match="city name"):
        OpenMeteoGeocoder("unused").geocode(", ,")
