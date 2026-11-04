from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from math import cos, radians

from routing.domain import Point, RoadRoute, classify_weather, recommend, sample_route, summarize_risk
from routing.ports import Forecaster, Geocoder, PlanningError, Router


@dataclass(frozen=True)
class PlanRequest:
    origin: str
    destination: str
    departure_at: datetime
    load_weight_lb: float
    sampling_interval_miles: int


def weather_hour(eta: datetime) -> datetime:
    hour = eta.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return hour if hour == eta else hour + timedelta(hours=1)


def corridor_points(routes: list[RoadRoute], departure: datetime) -> list[Point]:
    result = []
    for route in routes:
        centers = sample_route(route, departure, 25)
        if len(centers) > 16:
            centers = [centers[round(index * (len(centers) - 1) / 15)] for index in range(16)]
        for index, sample in enumerate(centers):
            before = centers[max(0, index - 1)].point
            after = centers[min(len(centers) - 1, index + 1)].point
            delta_lat = after.latitude - before.latitude
            scale = max(0.2, cos(radians(sample.point.latitude)))
            delta_lon = (after.longitude - before.longitude) * scale
            length = max(0.001, (delta_lat**2 + delta_lon**2) ** 0.5)
            # A spatial corridor extends about eight miles to each side of roads.
            for side in (-1, 0, 1):
                latitude = sample.point.latitude + side * 0.116 * delta_lon / length
                longitude = sample.point.longitude - side * 0.116 * delta_lat / length / scale
                result.append(Point(round(max(-85, min(85, latitude)), 4), round(((longitude + 180) % 360) - 180, 4)))
    return list(dict.fromkeys(result))


class RoutePlanner:
    def __init__(self, geocoder: Geocoder, router: Router, forecaster: Forecaster):
        self.geocoder = geocoder
        self.router = router
        self.forecaster = forecaster

    def plan(self, request: PlanRequest, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        with ThreadPoolExecutor(max_workers=2) as executor:
            origin, destination = executor.map(self.geocoder.geocode, (request.origin, request.destination))
        if origin.point == destination.point:
            raise PlanningError("same_location", "Origin and destination must be different cities.")
        roads = self.router.routes(origin.point, destination.point)
        if len(roads) != 3:
            raise PlanningError("insufficient_routes", "Three distinct route options are required.")
        samples = [sample_route(road, request.departure_at, request.sampling_interval_miles) for road in roads]
        grid = corridor_points(roads, request.departure_at)
        points = list(dict.fromkeys([sample.point for route in samples for sample in route] + grid))
        if len(points) > 650:
            raise PlanningError(
                "route_too_long",
                "This trip exceeds the weather provider request budget. Try a shorter trip or 50-mile sampling.",
            )
        forecasts = self.forecaster.forecasts(points)

        def weather_at(point, hour):
            try:
                return forecasts[point][hour]
            except KeyError as error:
                raise PlanningError(
                    "forecast_unavailable",
                    "The weather forecast does not cover every checkpoint ETA and the complete 48-hour corridor. Choose an earlier departure or shorter trip.",
                ) from error

        routes = []
        for index, (road, checkpoints) in enumerate(zip(roads, samples)):
            weather = [weather_at(sample.point, weather_hour(sample.eta)) for sample in checkpoints]
            risks = [classify_weather(item, request.load_weight_lb) for item in weather]
            routes.append(
                {
                    "id": f"route-{index + 1}",
                    "name": f"Route {index + 1}",
                    "geometry": [[point.longitude, point.latitude] for point in road.geometry],
                    "distance_miles": checkpoints[-1].distance_miles,
                    "duration_seconds": road.duration_seconds,
                    "arrival_at": checkpoints[-1].eta.isoformat(),
                    **summarize_risk(checkpoints, risks),
                    "checkpoints": [
                        {
                            **asdict(sample.point),
                            "distance_miles": sample.distance_miles,
                            "eta": sample.eta.isoformat(),
                            "risk": risk.label,
                            "weather": asdict(measurement),
                        }
                        for sample, risk, measurement in zip(checkpoints, risks, weather)
                    ],
                }
            )
        best = recommend(routes)
        base = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
        hours = [base + timedelta(hours=offset) for offset in range(49)]
        return {
            "origin": {"name": origin.name, **asdict(origin.point)},
            "destination": {"name": destination.name, **asdict(destination.point)},
            "departure_at": request.departure_at.isoformat(),
            "load_weight_lb": request.load_weight_lb,
            "sampling_interval_miles": request.sampling_interval_miles,
            "routes": routes,
            "recommended_route_id": best["id"] if best else None,
            "recommendation_reason": "Eligible routes are compared by fewest Severe miles, then High miles, lowest distance-weighted average risk, and shortest travel time."
            if best
            else "No route is recommended: every option contains a No Travel checkpoint.",
            "warnings": [
                "Road routes use OSRM's driving profile; truck height, width, road restrictions and driving breaks are not modeled.",
                "Weather is an hourly forecast. Checkpoint risk uses estimated arrival time; the corridor slider shows time from the current UTC hour.",
            ],
            "heatmap": {
                "base_time": base.isoformat(),
                "hours": [hour.isoformat() for hour in hours],
                "points": [
                    {
                        **asdict(point),
                        "risks": [
                            classify_weather(weather_at(point, hour), request.load_weight_lb).label for hour in hours
                        ],
                    }
                    for point in grid
                ],
            },
        }
