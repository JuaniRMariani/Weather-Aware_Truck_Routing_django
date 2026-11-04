import json
import logging
import math
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from routing.ports import PlanningError
from routing.providers import OpenMeteoForecaster, OpenMeteoGeocoder, OsrmRouter
from routing.service import PlanRequest, RoutePlanner

logger = logging.getLogger(__name__)


def error_response(code: str, message: str, status: int, fields: dict | None = None):
    error = {"code": code, "message": message}
    if fields:
        error["fields"] = fields
    return JsonResponse({"error": error}, status=status)


def health(request):
    if request.method != "GET":
        return error_response("method_not_allowed", "Use GET for this endpoint.", 405)
    return JsonResponse({"status": "ok"})


def parse_request(data: object, now: datetime) -> PlanRequest:
    if not isinstance(data, dict):
        raise ValueError("The request must be a JSON object.")
    origin, destination = data.get("origin"), data.get("destination")
    if not isinstance(origin, str) or not 2 <= len(origin.strip()) <= 160:
        raise ValueError("Enter an origin city between 2 and 160 characters.")
    if not isinstance(destination, str) or not 2 <= len(destination.strip()) <= 160:
        raise ValueError("Enter a destination city between 2 and 160 characters.")
    if not origin.replace(",", "").strip() or not destination.replace(",", "").strip():
        raise ValueError("Enter a city name for both origin and destination.")
    load = data.get("load_weight_lb")
    if isinstance(load, bool) or not isinstance(load, (int, float)) or not math.isfinite(load) or load < 0:
        raise ValueError("Load weight must be a finite, nonnegative number in pounds.")
    interval = data.get("sampling_interval_miles")
    if isinstance(interval, bool) or interval not in (10, 25, 50):
        raise ValueError("Sampling interval must be 10, 25, or 50 miles.")
    timestamp = data.get("departure_at")
    if not isinstance(timestamp, str):
        raise ValueError("Enter an ISO 8601 departure date and time with a timezone.")
    try:
        departure = datetime.fromisoformat(timestamp)
    except ValueError as error:
        raise ValueError("Enter an ISO 8601 departure date and time with a timezone.") from error
    if departure.tzinfo is None or departure.utcoffset() is None:
        raise ValueError("Departure time must include a timezone offset.")
    departure = departure.astimezone(timezone.utc)
    if departure < now - timedelta(minutes=5):
        raise ValueError("Departure time cannot be in the past.")
    if departure > now + timedelta(days=6):
        raise ValueError("Departure must be within the next six days of available forecast.")
    return PlanRequest(origin.strip(), destination.strip(), departure, float(load), int(interval))


def build_planner() -> RoutePlanner:
    return RoutePlanner(
        OpenMeteoGeocoder(settings.GEOCODING_BASE_URL),
        OsrmRouter(settings.OSRM_BASE_URL),
        OpenMeteoForecaster(settings.WEATHER_BASE_URL),
    )


@csrf_exempt
def route_plan(request):
    # Stateless JSON endpoint: no cookies, sessions or user-specific mutations.
    if request.method != "POST":
        return error_response("method_not_allowed", "Use POST for this endpoint.", 405)
    if request.content_type != "application/json":
        return error_response("unsupported_media_type", "Send an application/json request.", 415)
    try:
        now = datetime.now(timezone.utc)
        data = json.loads(request.body)
        plan_request = parse_request(data, now)
    except (ValueError, UnicodeDecodeError, OverflowError) as error:
        message = (
            str(error)
            if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError)
            else "Request body must be valid JSON with finite numbers."
        )
        return error_response("validation_error", message, 400)
    try:
        return JsonResponse(build_planner().plan(plan_request, now))
    except PlanningError as error:
        logger.info("Route planning failed: %s", error.code)
        return error_response(error.code, str(error), error.status)
    except Exception:
        logger.exception("Unexpected route planning failure")
        return error_response("internal_error", "The route plan could not be completed. Please try again.", 500)
