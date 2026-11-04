# Weather-Aware Truck Routing API

Django backend for the Spotter full-stack assessment. Three road routes are evaluated using checkpoint weather at estimated arrival time and the supplied load rules. The companion React repository is [Weather-Aware_Truck_Routing_react](https://github.com/JuaniRMariani/Weather-Aware_Truck_Routing_react).

## Run locally

Requires Python 3.13. No database, API key, or migrations are required.

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.txt
Copy-Item .env.example .env
.\.venv\Scripts\python manage.py runserver 127.0.0.1:8000
```

On macOS/Linux use `source .venv/bin/activate`, `pip install -r requirements-dev.txt`, `cp .env.example .env`, and `python manage.py runserver`. Settings automatically load `.env`; existing environment variables take precedence. Start the frontend separately on port 5173.

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python manage.py check
```

## API

`GET /api/health/` returns `{"status":"ok"}`.

`POST /api/route-plans/` accepts JSON:

```json
{
  "origin": "Chicago, IL",
  "destination": "Indianapolis, IN",
  "departure_at": "2026-11-04T18:00:00Z",
  "load_weight_lb": 30000,
  "sampling_interval_miles": 25
}
```

Replace the example departure with a future ISO 8601 timestamp including its timezone. Departure must be within six days; every resulting ETA must also have forecast coverage. Place names support city, state/country qualifiers and US state abbreviations, not street addresses. Resolved full names are returned for verification. A bare city name uses the provider's highest-ranked result.

Response contains `origin`, `destination`, `departure_at`, `load_weight_lb`, `sampling_interval_miles`, exactly three `routes`, `recommended_route_id` (nullable), `recommendation_reason`, `warnings`, and `heatmap`. Route geometry uses GeoJSON `[longitude, latitude]`. Route summaries include miles, travel seconds, arrival timestamp, maximum risk, Severe miles, High miles, distance-weighted average risk, eligibility, and checkpoints with coordinates, distance, ETA, weather, and risk. Heatmap contains `base_time`, 49 hourly `hours`, and spatial `points` each with 49 `risks`.

Risk labels: `low`, `moderate`, `high`, `severe`, `no_travel`. Scores for distance-weighted averaging are respectively 0, 1, 2, 3, 4. Errors use `{"error":{"code":"...","message":"..."}}`; validation is 400, unavailable coverage/routes is 422, malformed provider data is 502, unavailable providers is 503. There is no fake-data fallback.

## Architecture

`routing/domain.py` contains framework-independent immutable values, weather classification, checkpoint sampling, risk aggregation, and lexicographic ranking. `routing/ports.py` defines the three narrow provider protocols. `routing/service.py` orchestrates the planning use case via constructor injection. `routing/providers.py` implements the external HTTP adapters. `routing/api.py` is the Django JSON transport and dependency composition boundary. This keeps business rules testable without HTTP, Django models, or a database. Domain rules are not duplicated in the frontend.

## Exact risk rules

| Condition | Low | Moderate | High | Severe | No Travel |
|---|---|---|---|---|---|
| Wind mph | <25 | 25 to <35 | 35 to <45 | 45 to <55 | >=55 |
| Rain in/hr | <0.10 | 0.10 to <0.25 | 0.25 to <0.50 | 0.50 to 1.00 inclusive | >1.00 |
| Snow in/hr | <0.5 | 0.5 to <1.0 | 1.0 to <2.0 | 2.0 to 3.0 inclusive | >3.0 |

- Wind >=55 mph means No Travel for every load.
- Wind 45 to <55 mph and load **strictly >30,000 lb** means No Travel.
- Wind 35 to <45 mph and load **strictly >40,000 lb** means Severe.
- The final checkpoint risk is the maximum of all condition risks, including load overrides.
- Shared rain/snow endpoints use the higher category, explicitly confirmed by the assessment owner. The PDF's integer wind bands are continuous intervals ending before the next threshold; values are never rounded before classification. Exactly 1.00 in/hr rain and 3.0 in/hr snow remain Severe.

Any No Travel checkpoint, including the destination, excludes a route from recommendation. Eligible routes are ordered **lexicographically**, by Severe miles, then High miles, then average risk, then travel seconds. There is no weighted safety/time tradeoff that can override this order. All blocked routes produce no recommendation.

## Sampling and weather interpretation

Checkpoints are at 0, each selected 10/25/50 miles, and the destination, with no duplicate endpoint. Locations and elapsed travel time interpolate within OSRM road segments using each segment's distance and duration annotations, normalized to provider totals. Checkpoint risk represents the following distance interval; the final partial interval contributes its actual miles. The destination contributes to eligibility and maximum risk even though it has no following interval.

Open-Meteo hourly precipitation represents the preceding hour. An ETA inside an hour selects the hour ending at its ceiling; an exact-hour ETA uses that hour. Wind uses the same selected forecast hour. Rain combines large-scale `rain` and convective `showers`, excluding snow, and converts mm to inches with `/25.4`. Snowfall is snow depth in cm, converted with `/2.54`. Wind is requested in mph. Null, negative, nonfinite or incomplete measurements never become Low risk.

The independent heatmap covers each route and points about eight miles to either side. It is bounded to at most 16 center stations per route, with three points per station, for at most 144 spatial forecast points before deduplication. This does **not** thin route checkpoints. All 49 hours, from the current UTC hour through +48, are returned once, so moving the frontend slider requires no new HTTP calls. The heatmap applies the same load and weather classification at each forecast time. The current-hour anchor differs intentionally from departure-dependent checkpoint ETAs.

## Providers and limitations

- [OSRM API](https://project-osrm.org/docs/v5.24.0/api/) provides roads, durations, and alternatives. When direct alternatives are insufficient, bounded intermediate-waypoint requests obtain real additional roads. Distinct road-edge sets are required; routes are never duplicated just to reach three. If three distinct routes cannot be obtained, the API explains the failure. The public driving profile does not model truck dimensions, restricted roads, traffic, or mandated breaks. Weather/load safety is the assessment's scope.
- [Open-Meteo forecast](https://open-meteo.com/en/docs) provides seven forecast days, multiple-location batches, and hourly units. [Open-Meteo geocoding](https://open-meteo.com/en/docs/geocoding-api) resolves city names. Public providers have usage limits and no availability guarantee. Production use should configure suitable provider infrastructure/terms via the environment variables.
- Requests use explicit connection/read timeouts, 10-minute in-process response caching, batches of 30 weather points, and at most three concurrent weather batches. A 650-point request budget protects provider usage; very long trips should use 50-mile sampling or be split into shorter trips. Forecast availability is still checked for the whole trip.
- No persistence, authentication, route editing, or unrelated features were added.

## Render deployment

Create a Render Blueprint from this repository using `render.yaml`. Set `CORS_ALLOWED_ORIGINS` to the exact frontend HTTPS origin, with no trailing slash. The generated secret and `DJANGO_DEBUG=false` are included in the blueprint. Render's assigned hostname is automatically allowed through `RENDER_EXTERNAL_HOSTNAME`. The process binds to `$PORT` using Gunicorn; production HTTP redirects to HTTPS. Health path: `/api/health/`.

For manual setup: Python web service, build `pip install -r requirements.txt`, start `gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120`. Supply `DJANGO_SECRET_KEY`, `DJANGO_DEBUG=false`, and `CORS_ALLOWED_ORIGINS`. A Dockerfile is also provided. Configuration is prepared; a public deployment URL must be verified after creating the service. See [Render's Django deployment documentation](https://render.com/docs/deploy-django).
