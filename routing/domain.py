"""Framework-independent assessment rules and route sampling."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import IntEnum
from math import isfinite

METERS_PER_MILE = 1609.344


class Risk(IntEnum):
    LOW = 0
    MODERATE = 1
    HIGH = 2
    SEVERE = 3
    NO_TRAVEL = 4

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Point:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Place:
    name: str
    point: Point


@dataclass(frozen=True)
class Weather:
    wind_mph: float
    rain_in_per_hour: float
    snow_in_per_hour: float


@dataclass(frozen=True)
class RoadRoute:
    geometry: tuple[Point, ...]
    segment_meters: tuple[float, ...]
    segment_seconds: tuple[float, ...]
    duration_seconds: float
    distance_meters: float


@dataclass(frozen=True)
class Sample:
    point: Point
    distance_miles: float
    eta: datetime


def classify_weather(weather: Weather, load_weight_lb: float) -> Risk:
    values = (weather.wind_mph, weather.rain_in_per_hour, weather.snow_in_per_hour, load_weight_lb)
    if any(not isfinite(value) or value < 0 for value in values):
        raise ValueError("Weather and load must be finite, nonnegative values.")
    wind, rain, snow = values[:3]
    wind_risk = (
        Risk.NO_TRAVEL
        if wind >= 55
        else Risk.SEVERE
        if wind >= 45
        else Risk.HIGH
        if wind >= 35
        else Risk.MODERATE
        if wind >= 25
        else Risk.LOW
    )
    if 45 <= wind < 55 and load_weight_lb > 30000:
        wind_risk = Risk.NO_TRAVEL
    elif 35 <= wind < 45 and load_weight_lb > 40000:
        wind_risk = Risk.SEVERE
    rain_risk = (
        Risk.NO_TRAVEL
        if rain > 1
        else Risk.SEVERE
        if rain >= 0.5
        else Risk.HIGH
        if rain >= 0.25
        else Risk.MODERATE
        if rain >= 0.1
        else Risk.LOW
    )
    snow_risk = (
        Risk.NO_TRAVEL
        if snow > 3
        else Risk.SEVERE
        if snow >= 2
        else Risk.HIGH
        if snow >= 1
        else Risk.MODERATE
        if snow >= 0.5
        else Risk.LOW
    )
    return max(wind_risk, rain_risk, snow_risk)


def sample_route(route: RoadRoute, departure: datetime, interval_miles: int) -> list[Sample]:
    if interval_miles not in (10, 25, 50):
        raise ValueError("Sampling interval must be 10, 25, or 50 miles.")
    total = route.distance_meters
    if total <= 0 or len(route.geometry) != len(route.segment_meters) + 1:
        raise ValueError("Route must contain consistent positive road geometry.")
    step = interval_miles * METERS_PER_MILE
    targets = [index * step for index in range(int(total // step) + 1)]
    if total - targets[-1] > 1e-6:
        targets.append(total)
    else:
        targets[-1] = total
    # OSRM totals may differ slightly from rounded annotation sums.
    distance_scale = total / sum(route.segment_meters)
    time_total = sum(route.segment_seconds)
    time_scale = route.duration_seconds / time_total if time_total else 0
    result = []
    index = 0
    traveled = 0.0
    elapsed = 0.0
    for target in targets:
        while (
            index < len(route.segment_meters) - 1 and traveled + route.segment_meters[index] * distance_scale < target
        ):
            traveled += route.segment_meters[index] * distance_scale
            elapsed += route.segment_seconds[index] * time_scale
            index += 1
        length = route.segment_meters[index] * distance_scale
        fraction = min(1.0, max(0.0, (target - traveled) / length)) if length else 0
        start, end = route.geometry[index : index + 2]
        point = Point(
            start.latitude + (end.latitude - start.latitude) * fraction,
            start.longitude + (end.longitude - start.longitude) * fraction,
        )
        seconds = elapsed + route.segment_seconds[index] * time_scale * fraction
        if target == total:
            point, seconds = route.geometry[-1], route.duration_seconds
        result.append(Sample(point, target / METERS_PER_MILE, departure + timedelta(seconds=seconds)))
    return result


def summarize_risk(samples: list[Sample], risks: list[Risk]) -> dict:
    if not samples or len(samples) != len(risks):
        raise ValueError("Every checkpoint must have a risk.")
    severe = high = weighted = 0.0
    for index in range(len(samples) - 1):
        miles = samples[index + 1].distance_miles - samples[index].distance_miles
        risk = risks[index]
        severe += miles if risk == Risk.SEVERE else 0
        high += miles if risk == Risk.HIGH else 0
        weighted += miles * int(risk)
    total = samples[-1].distance_miles
    return {
        "risk": max(risks).label,
        "severe_miles": severe,
        "high_miles": high,
        "average_risk": weighted / total if total else float(risks[0]),
        "eligible": Risk.NO_TRAVEL not in risks,
    }


def recommend(routes: list[dict]) -> dict | None:
    eligible = [route for route in routes if route["eligible"]]
    return min(
        eligible,
        key=lambda route: (
            route["severe_miles"],
            route["high_miles"],
            route["average_risk"],
            route["duration_seconds"],
        ),
        default=None,
    )
