from datetime import datetime, timedelta, timezone

import pytest

from routing.domain import (
    METERS_PER_MILE,
    Point,
    Risk,
    RoadRoute,
    Sample,
    Weather,
    classify_weather,
    recommend,
    sample_route,
    summarize_risk,
)


@pytest.mark.parametrize(
    ("wind", "expected"),
    [
        (0, 0),
        (24.999, 0),
        (25, 1),
        (34, 1),
        (34.999, 1),
        (35, 2),
        (44.999, 2),
        (45, 3),
        (54, 3),
        (54.999, 3),
        (55, 4),
        (80, 4),
    ],
)
def test_wind_boundaries(wind, expected):
    assert classify_weather(Weather(wind, 0, 0), 0) == expected


@pytest.mark.parametrize(
    ("rain", "expected"),
    [(0, 0), (0.09999, 0), (0.1, 1), (0.24999, 1), (0.25, 2), (0.49999, 2), (0.5, 3), (1, 3), (1.00001, 4)],
)
def test_rain_boundaries(rain, expected):
    assert classify_weather(Weather(0, rain, 0), 0) == expected


@pytest.mark.parametrize(
    ("snow", "expected"),
    [(0, 0), (0.49999, 0), (0.5, 1), (0.99999, 1), (1, 2), (1.99999, 2), (2, 3), (3, 3), (3.00001, 4)],
)
def test_snow_boundaries(snow, expected):
    assert classify_weather(Weather(0, 0, snow), 0) == expected


@pytest.mark.parametrize(
    ("wind", "load", "expected"),
    [
        (45, 30000, 3),
        (45, 30000.01, 4),
        (54.99, 30001, 4),
        (35, 40000, 2),
        (35, 40000.01, 3),
        (44.99, 40001, 3),
        (34.99, 100000, 1),
        (55, 0, 4),
        (55, 30000, 4),
    ],
)
def test_load_boundaries(wind, load, expected):
    assert classify_weather(Weather(wind, 0, 0), load) == expected


def test_load_rule_never_downgrades_precipitation():
    assert classify_weather(Weather(35, 1.001, 0), 50000) == Risk.NO_TRAVEL


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_measurements_rejected(value):
    with pytest.raises(ValueError):
        classify_weather(Weather(value, 0, 0), 0)


@pytest.mark.parametrize("interval", [10, 25, 50])
def test_sampling_spacing_partial_last_interval_and_segment_eta(interval):
    departure = datetime(2026, 11, 4, tzinfo=timezone.utc)
    route = RoadRoute(
        (Point(0, 0), Point(0, 1), Point(0, 2)),
        (25 * METERS_PER_MILE, 30 * METERS_PER_MILE),
        (3600, 1800),
        5400,
        55 * METERS_PER_MILE,
    )
    samples = sample_route(route, departure, interval)
    assert samples[0].distance_miles == 0
    assert samples[-1].distance_miles == pytest.approx(55)
    assert samples[-1].eta == departure + timedelta(seconds=5400)
    assert all(
        b.distance_miles - a.distance_miles == pytest.approx(interval) for a, b in zip(samples[:-2], samples[1:-1])
    )
    if interval == 25:
        assert samples[1].eta == departure + timedelta(hours=1)
        assert samples[2].eta == departure + timedelta(seconds=5100)


def test_endpoint_no_travel_excludes_route_even_with_zero_interval():
    now = datetime.now(timezone.utc)
    samples = [Sample(Point(0, 0), 0, now), Sample(Point(0, 1), 25, now), Sample(Point(0, 2), 30, now)]
    summary = summarize_risk(samples, [Risk.SEVERE, Risk.HIGH, Risk.NO_TRAVEL])
    assert summary == {
        "risk": "no_travel",
        "severe_miles": 25,
        "high_miles": 5,
        "average_risk": 85 / 30,
        "eligible": False,
    }


def test_recommendation_is_lexicographic_and_excludes_no_travel():
    def route(name, severe, high, average, duration, eligible=True):
        return dict(
            id=name,
            severe_miles=severe,
            high_miles=high,
            average_risk=average,
            duration_seconds=duration,
            eligible=eligible,
        )

    cases = [
        route("blocked", 0, 0, 0, 1, False),
        route("severe", 1, 0, 0, 1),
        route("high", 0, 1, 0, 1),
        route("average", 0, 0, 1, 1),
        route("slow", 0, 0, 0, 10),
        route("winner", 0, 0, 0, 9),
    ]
    assert recommend(cases)["id"] == "winner"
    assert recommend([cases[0]]) is None
