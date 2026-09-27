"""Dashboard API: every call ``frontend/js/api.js`` makes, in the shapes ``frontend/js/mock.js`` defines."""

from __future__ import annotations

from datetime import datetime, timedelta

from test_app import client, wait_for

from app.api.views import RouteCatalog
from app.state import StopVisit, VehicleSchedule

# fields mock.js puts on a vehicle (pub), a route (getRoutes) and a schedule row (schedule)
VEHICLE_KEYS = {"vehicle_id", "route_id", "direction_id", "lat", "lon", "is_reserve", "speed", "heading",
                "delay_now_sec", "delay_pred_sec", "risk_score", "updated_at", "headway_prev_sec", "plan_headway_sec"}
ROUTE_KEYS = {"route_id", "name", "transport_type", "geometry", "stops", "directions"}


def running(**kw):
    return client(clock_start="2026-01-06T08:05:00", state_tick_s=0.05, predict_tick_s=0.05, ws_tick_s=0.05, **kw)


def ready(c) -> None:
    wait_for(lambda: c.get("/health").json()["predictor"]["vehicles_with_prediction"] == 1)


def test_routes_are_vehicles_with_a_direction_per_trip_shape() -> None:
    with running() as c:
        routes = c.get("/routes").json()
    assert [r["route_id"] for r in routes] == ["115106"]
    r = routes[0]
    assert ROUTE_KEYS <= set(r) and r["transport_type"] == "bus"
    assert [d["name"] for d in r["directions"]] == ["Остановка А → Остановка Г (ручной ввод)",
                                                    "Остановка В, обратный рейс → Остановка В, обратный рейс"]
    assert r["geometry"] == [[55.7, 37.6], [55.7, 37.602], [55.7, 37.604], [55.7, 37.605]]   # [lat, lon], as the map expects
    assert r["name"] == "Остановка А ↔ Остановка Г (ручной ввод)"


def test_vehicles_in_the_dashboard_shape() -> None:
    with running() as c:
        ready(c)
        vs = c.get("/vehicles").json()
    assert [v["vehicle_id"] for v in vs] == ["115106"]                  # 116057 has no schedule: not on the map
    v = vs[0]
    assert VEHICLE_KEYS <= set(v)
    assert (v["route_id"], v["direction_id"], v["lat"], v["lon"]) == ("115106", 0, 55.7, 37.604)
    assert (v["delay_now_sec"], v["delay_pred_sec"], v["risk_level"]) == (0, 0, "green")
    assert v["target_stop_name"] == "Остановка В, обратный рейс" and v["forecast_source"] == "fallback"
    assert v["updated_at"].endswith("Z")                                # wall clock, for the browser's Date.now()


def test_schedule_of_the_current_trip() -> None:
    with running() as c:
        ready(c)
        sch = c.get("/vehicles/115106/schedule").json()
        assert c.get("/vehicles/116057/schedule").status_code == 404
    assert (sch["vehicle_id"], sch["direction_id"]) == ("115106", 0)
    assert [(s["name"], s["status"]) for s in sch["stops"]] == [
        ("Остановка А", "passed"), ("Остановка Б", "passed"), ("Остановка В", "next"),
        ("Остановка Г (ручной ввод)", "upcoming")]
    assert sch["stops"][0]["delay_sec"] == 0 and sch["stops"][0]["time_fact"].endswith("Z")
    assert sch["stops"][2]["estimate"] == "current_deviation"


def test_metrics_endpoints_work_without_ml() -> None:
    with running() as c:
        ready(c)
        m = c.get("/metrics/model").json()
        assert m["model_version"] == "unavailable" and m["live"]["ml_available"] is False
        assert m["live"]["horizon_ok_share"] == 1.0
        assert c.get("/metrics/worst_stops?limit=5").json() == []       # forecast delay 0 s: nothing ≥ 30 s
        assert c.get("/metrics/bunching").json() == []
        assert c.get("/routes/115106/signals").json() == []


def test_whatif_needs_ml_and_validates_input() -> None:
    with running() as c:
        ready(c)
        assert c.post("/whatif", json={"scenario": "add_reserve", "route_id": "115106"}).status_code == 503
        assert c.post("/whatif", json={"scenario": "add_reserve", "route_id": "999"}).status_code == 404
        assert c.post("/whatif", json={"scenario": "teleport", "route_id": "115106"}).status_code == 422


def test_websocket_snapshot_then_periodic_updates() -> None:
    with running() as c:
        ready(c)
        with c.websocket_connect("/ws") as ws:
            first = ws.receive_json()
            assert first["type"] == "vehicle.update" and [v["vehicle_id"] for v in first["vehicles"]] == ["115106"]
            wait_for(lambda: c.get("/health").json()["ws_clients"] == 1)
            assert ws.receive_json()["type"] == "vehicle.update"          # pushed by the hub every ws_tick_s


# ----------------------------------------------------------------------------- directions


def _shuttle() -> VehicleSchedule:
    """A → B, B → A, A → B: three trips, two shapes."""
    day = datetime(2026, 1, 6, 8)
    stops = [(55.70, 37.60, "A"), (55.71, 37.61, "M"), (55.72, 37.62, "B")]
    visits, pos = [], 0
    for trip, seq in enumerate([stops, stops[::-1], stops]):
        for i, (lat, lon, name) in enumerate(seq):
            visits.append(StopVisit(pos=pos, stop_id=pos, plan=day + timedelta(minutes=30 * trip + 5 * i), lat=lat,
                                    lon=lon, manual_fill=False, name=name, geom="", trip=trip, idx_in_trip=i))
            pos += 1
    return VehicleSchedule(9, visits)


def test_trip_shapes_become_directions() -> None:
    cat = RouteCatalog({9: _shuttle()})
    r = cat.routes[9]
    assert [d["name"] for d in r["directions"]] == ["A → B", "B → A"]
    assert [cat.direction(9, t) for t in (0, 1, 2)] == [0, 1, 0]
    assert r["directions"][1]["geometry"] == [[55.72, 37.62], [55.71, 37.61], [55.70, 37.60]]
