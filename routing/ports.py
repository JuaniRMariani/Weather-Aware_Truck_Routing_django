from datetime import datetime
from typing import Protocol

from routing.domain import Place, Point, RoadRoute, Weather


class PlanningError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.status = status


class Geocoder(Protocol):
    def geocode(self, query: str) -> Place: ...


class Router(Protocol):
    def routes(self, origin: Point, destination: Point) -> list[RoadRoute]: ...


class Forecaster(Protocol):
    def forecasts(self, points: list[Point]) -> dict[Point, dict[datetime, Weather]]: ...
