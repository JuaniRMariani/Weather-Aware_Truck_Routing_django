import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from routing.ports import PlanningError


@pytest.fixture
def payload():
    return {
        "origin": "Chicago, IL",
        "destination": "Indianapolis, IN",
        "departure_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "load_weight_lb": 30000,
        "sampling_interval_miles": 25,
    }


def test_health(client):
    assert client.get("/api/health/").json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", ""),
        ("origin", 123),
        ("destination", None),
        ("departure_at", "2026-11-04T12:00:00"),
        ("departure_at", "wrong"),
        ("load_weight_lb", -1),
        ("load_weight_lb", True),
        ("load_weight_lb", float("nan")),
        ("load_weight_lb", float("inf")),
        ("sampling_interval_miles", 15),
        ("sampling_interval_miles", True),
    ],
)
def test_input_validation(client, payload, field, value):
    payload[field] = value
    response = client.post("/api/route-plans/", json.dumps(payload), content_type="application/json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


def test_invalid_json(client):
    assert client.post("/api/route-plans/", "{", content_type="application/json").status_code == 400


@pytest.mark.parametrize("days", [-1, 7])
def test_forecast_departure_window(client, payload, days):
    payload["departure_at"] = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    assert client.post("/api/route-plans/", payload, content_type="application/json").status_code == 400


def test_plan_calls_application_service(client, payload):
    planner = Mock()
    planner.plan.return_value = {"routes": []}
    with patch("routing.api.build_planner", return_value=planner):
        response = client.post("/api/route-plans/", payload, content_type="application/json")
    assert response.status_code == 200
    assert planner.plan.call_args.args[0].load_weight_lb == 30000


def test_provider_failure_is_structured(client, payload):
    planner = Mock()
    planner.plan.side_effect = PlanningError("provider_unavailable", "Weather unavailable", 503)
    with patch("routing.api.build_planner", return_value=planner):
        response = client.post("/api/route-plans/", payload, content_type="application/json")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_unavailable"


def test_content_type_and_method_validation(client):
    assert client.get("/api/route-plans/").status_code == 405
    assert client.post("/api/route-plans/", "text", content_type="text/plain").status_code == 415


@pytest.mark.parametrize(("field", "value"), [("origin", ", ,"), ("load_weight_lb", 10**400)])
def test_malformed_edge_case_inputs_return_json(client, payload, field, value):
    payload[field] = value
    response = client.post("/api/route-plans/", json.dumps(payload), content_type="application/json")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"
