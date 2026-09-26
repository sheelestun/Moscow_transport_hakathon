"""HTTP smoke-тесты бэкенда через FastAPI TestClient (без docker и ML).

Симулятор поднимается в процессе, ML недоступен → бэкенд использует фолбэк-эвристику
(см. `main._ml_get`). Проверяем, что контракт с фронтом не сломался.
"""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# ML недоступен — бэкенд должен корректно фолбэчить, а не падать
os.environ.setdefault("ML_URL", "http://127.0.0.1:1")

from main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # startup инициализирует _bg_task; TestClient его крутит через lifespan
    with TestClient(app) as c:
        yield c


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "ok"
    assert j["vehicles"] > 0


def test_routes(client: TestClient) -> None:
    r = client.get("/routes")
    assert r.status_code == 200
    routes = r.json()
    assert len(routes) >= 3
    for rt in routes:
        assert {"route_id", "geometry", "stops"} <= set(rt)
        assert len(rt["geometry"]) >= 2
        assert isinstance(rt.get("directions", []), list)


def test_vehicles(client: TestClient) -> None:
    r = client.get("/vehicles")
    assert r.status_code == 200
    vehicles = r.json()
    assert vehicles, "должны быть ТС"
    v = vehicles[0]
    # ключи, на которые опирается фронт (см. mock.js pub())
    for key in ("vehicle_id", "route_id", "direction_id", "lat", "lon",
                "delay_now_sec", "delay_pred_sec", "risk_score",
                "p_early", "p_ontime", "p_late",
                "headway_prev_sec", "plan_headway_sec"):
        assert key in v, f"vehicle без {key}"


def test_metrics_worst_stops(client: TestClient) -> None:
    r = client.get("/metrics/worst_stops?limit=5")
    assert r.status_code == 200
    stops = r.json()
    assert isinstance(stops, list)
    assert len(stops) <= 5


def test_metrics_bunching(client: TestClient) -> None:
    r = client.get("/metrics/bunching")
    assert r.status_code == 200
    pairs = r.json()
    assert isinstance(pairs, list)


def test_metrics_model_fallback(client: TestClient) -> None:
    """ML недоступен → возвращается LAST_METRICS или fallback dict, но не 500."""
    r = client.get("/metrics/model")
    assert r.status_code == 200
    j = r.json()
    assert isinstance(j, dict)


def test_route_signals(client: TestClient) -> None:
    routes = client.get("/routes").json()
    r = client.get(f"/routes/{routes[0]['route_id']}/signals")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_schedule_of_first_vehicle(client: TestClient) -> None:
    vehicles = client.get("/vehicles").json()
    r = client.get(f"/vehicles/{vehicles[0]['vehicle_id']}/schedule")
    assert r.status_code == 200
    sch = r.json()
    assert sch["vehicle_id"] == vehicles[0]["vehicle_id"]
    assert len(sch["stops"]) > 0


def test_schedule_404(client: TestClient) -> None:
    r = client.get("/vehicles/does-not-exist/schedule")
    assert r.status_code == 404


def test_whatif_shape(client: TestClient) -> None:
    routes = client.get("/routes").json()
    r = client.post("/whatif", json={"scenario": "signal_priority", "route_id": routes[0]["route_id"]})
    assert r.status_code == 200
    j = r.json()
    assert j["scenario"] == "signal_priority"
    assert "summary" in j and "vehicles" in j


def test_alerts_shape(client: TestClient) -> None:
    r = client.get("/alerts?active=true")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
