"""HTTP adapters. Provider failures never become invented weather or routes."""

import hashlib
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from django.core.cache import cache

from routing.domain import Place, Point, RoadRoute, Weather
from routing.ports import PlanningError


def get_json(url: str, params: dict, provider: str):
    key = "http:" + hashlib.sha256((url + repr(sorted(params.items()))).encode()).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = requests.get(
            url, params=params, timeout=(5, 35), headers={"User-Agent": "WeatherAwareRoutingAssessment/1.0"}
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise PlanningError(
            "provider_unavailable", f"{provider} is temporarily unavailable. Please try again.", 503
        ) from error
    cache.set(key, payload, 600)
    return payload


class OpenMeteoGeocoder:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def geocode(self, query: str) -> Place:
        # Open-Meteo searches place names, not free-form street addresses.
        parts = [part.strip() for part in query.split(",") if part.strip()]
        if not parts:
            raise PlanningError("location_not_found", "Enter a city name.")
        payload = get_json(
            self.base_url, {"name": parts[0], "count": 100, "language": "en", "format": "json"}, "Geocoding"
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results", []), list):
            raise PlanningError("invalid_provider_response", "Geocoding returned an invalid response.", 502)
        results = payload.get("results", [])
        if not results:
            raise PlanningError("location_not_found", f'No city was found for "{query}". Enter a city name.')
        if len(parts) > 1:
            qualifiers = [STATE_NAMES.get(part.upper(), part).casefold() for part in parts[1:]]
            matches = [
                item
                for item in results
                if all(
                    qualifier
                    in [
                        str(item.get(field, "")).casefold() for field in ("admin1", "admin2", "country", "country_code")
                    ]
                    for qualifier in qualifiers
                )
            ]
            if not matches:
                raise PlanningError(
                    "location_not_found",
                    f'No matching city was found for "{query}". Use the city and full state or country name.',
                )
            results = matches
        try:
            item = results[0]
            point = valid_point(item["latitude"], item["longitude"])
            name = ", ".join(
                dict.fromkeys(str(item[field]) for field in ("name", "admin1", "country") if item.get(field))
            )
        except (KeyError, TypeError, ValueError) as error:
            raise PlanningError("invalid_provider_response", "Geocoding returned an invalid location.", 502) from error
        return Place(name, point)


STATE_NAMES = dict(
    zip(
        "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(),
        "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia".split(
            "|"
        ),
    )
)


def valid_point(latitude, longitude) -> Point:
    lat, lon = float(latitude), float(longitude)
    if not math.isfinite(lat) or not math.isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Invalid coordinate")
    return Point(lat, lon)


class OsrmRouter:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def _request(self, points: list[Point], alternatives: bool = False) -> list[RoadRoute]:
        coordinates = ";".join(f"{point.longitude:.6f},{point.latitude:.6f}" for point in points)
        payload = get_json(
            f"{self.base_url}/route/v1/driving/{coordinates}",
            {
                "alternatives": "3" if alternatives else "false",
                "overview": "full",
                "geometries": "geojson",
                "annotations": "distance,duration",
                "steps": "false",
            },
            "Routing",
        )
        if not isinstance(payload, dict):
            raise PlanningError("invalid_provider_response", "Routing returned an invalid response.", 502)
        if payload.get("code") in ("NoRoute", "NoSegment"):
            return []
        if payload.get("code") != "Ok":
            raise PlanningError("invalid_provider_response", "Routing could not process these locations.", 502)
        try:
            return [self._parse_route(item) for item in payload["routes"]]
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as error:
            raise PlanningError(
                "invalid_provider_response", "Routing returned incomplete road or timing data.", 502
            ) from error

    @staticmethod
    def _parse_route(item: dict) -> RoadRoute:
        geometry = tuple(valid_point(lat, lon) for lon, lat in item["geometry"]["coordinates"])
        distances = tuple(float(value) for leg in item["legs"] for value in leg["annotation"]["distance"])
        durations = tuple(float(value) for leg in item["legs"] for value in leg["annotation"]["duration"])
        total_distance, total_duration = float(item["distance"]), float(item["duration"])
        values = (*distances, *durations, total_distance, total_duration)
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("Invalid road values")
        if (
            len(geometry) != len(distances) + 1
            or len(distances) != len(durations)
            or sum(distances) <= 0
            or total_distance <= 0
        ):
            raise ValueError("Inconsistent road annotation")
        return RoadRoute(geometry, distances, durations, total_duration, total_distance)

    def routes(self, origin: Point, destination: Point) -> list[RoadRoute]:
        candidates = self._request([origin, destination], alternatives=True)
        unique: list[RoadRoute] = []
        self._append_distinct(unique, candidates)
        # Some road networks expose no OSRM alternatives. Request actual roads via
        # bounded intermediate points rather than copying the fastest geometry.
        if len(unique) < 3:
            lat_delta = destination.latitude - origin.latitude
            lon_delta = destination.longitude - origin.longitude
            scale = max(0.2, math.cos(math.radians((origin.latitude + destination.latitude) / 2)))
            length = math.hypot(lat_delta, lon_delta * scale)
            offset = min(0.5, max(0.08, length * 0.12))
            via_points = []
            for fraction, side in ((0.5, 1), (0.5, -1), (0.33, 1), (0.67, -1)):
                lat = origin.latitude + lat_delta * fraction + (lon_delta * scale / max(length, 0.001)) * offset * side
                lon = origin.longitude + lon_delta * fraction - (lat_delta / max(length, 0.001)) * offset * side / scale
                via_points.append(Point(max(-85, min(85, lat)), ((lon + 180) % 360) - 180))
            with ThreadPoolExecutor(max_workers=2) as executor:
                for result in executor.map(lambda point: self._request([origin, point, destination]), via_points):
                    self._append_distinct(unique, result)
        if len(unique) < 3:
            raise PlanningError(
                "insufficient_routes",
                "The routing provider could not produce three distinct road routes for these locations. Try another origin or destination.",
            )
        return sorted(unique, key=lambda route: route.duration_seconds)[:3]

    @staticmethod
    def _append_distinct(existing: list[RoadRoute], candidates: list[RoadRoute]) -> None:
        for route in candidates:
            # Reversed edge identity would incorrectly count out-and-back detours
            # as a new road; compare undirected edges and require real differences.
            edges = road_edges(route)
            if all(len(edges - road_edges(other)) >= max(2, len(edges) * 0.01) for other in existing):
                existing.append(route)


def road_edges(route: RoadRoute) -> set:
    return {
        tuple(
            sorted(
                (
                    (round(start.latitude, 5), round(start.longitude, 5)),
                    (round(end.latitude, 5), round(end.longitude, 5)),
                )
            )
        )
        for start, end in zip(route.geometry, route.geometry[1:])
    }


class OpenMeteoForecaster:
    def __init__(self, base_url: str):
        self.base_url = base_url

    def forecasts(self, points: list[Point]) -> dict[Point, dict[datetime, Weather]]:
        unique = list(dict.fromkeys(points))
        batches = [unique[index : index + 30] for index in range(0, len(unique), 30)]
        result = {}
        with ThreadPoolExecutor(max_workers=3) as executor:
            for batch in executor.map(self._batch, batches):
                result.update(batch)
        return result

    def _batch(self, points: list[Point]) -> dict[Point, dict[datetime, Weather]]:
        payload = get_json(
            self.base_url,
            {
                "latitude": ",".join(str(point.latitude) for point in points),
                "longitude": ",".join(str(point.longitude) for point in points),
                "hourly": "wind_speed_10m,rain,showers,snowfall",
                "wind_speed_unit": "mph",
                "precipitation_unit": "mm",
                "timezone": "UTC",
                "forecast_days": 7,
            },
            "Weather",
        )
        items = payload if isinstance(payload, list) else [payload]
        if len(items) != len(points):
            raise PlanningError("invalid_provider_response", "Weather returned incomplete locations.", 502)
        try:
            return {point: self._parse_hours(item) for point, item in zip(points, items)}
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise PlanningError(
                "invalid_provider_response", "Weather returned missing or invalid measurements.", 502
            ) from error

    @staticmethod
    def _parse_hours(item: dict) -> dict[datetime, Weather]:
        units = item["hourly_units"]
        if (
            units["wind_speed_10m"] != "mp/h"
            or units["rain"] != "mm"
            or units["showers"] != "mm"
            or units["snowfall"] != "cm"
        ):
            raise ValueError("Unexpected weather units")
        hourly = item["hourly"]
        result = {}
        for index, timestamp in enumerate(hourly["time"]):
            raw = [hourly[field][index] for field in ("wind_speed_10m", "rain", "showers", "snowfall")]
            # Missing hours are not classified as Low; coverage checks fail if used.
            if any(value is None for value in raw):
                continue
            wind, rain, showers, snow = [float(value) for value in raw]
            if any(not math.isfinite(value) or value < 0 for value in (wind, rain, showers, snow)):
                raise ValueError("Invalid weather measurement")
            hour = datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
            result[hour] = Weather(wind, (rain + showers) / 25.4, snow / 2.54)
        return result
