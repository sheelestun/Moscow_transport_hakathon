"""The dashboard's REST API — the calls ``frontend/js/api.js`` makes, in the shapes ``mock.js`` defines."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from ..predict import MlUnavailable
from ..predict.predictor import RISK_RED
from ..state.geo import epoch_s
from .timefmt import wall_iso
from .views import schedule_payload, vehicle_payload, worst_stops

router = APIRouter(tags=["dashboard"])

METRICS_TTL_S = 30.0


@router.get("/routes")
def routes(request: Request) -> list[dict]:
    """One "route" per scheduled vehicle (the dataset has no route ids); each distinct trip shape is a direction."""
    return list(request.app.state.routes.routes.values())


@router.get("/vehicles")
def vehicles(request: Request) -> list[dict]:
    """Vehicles in service: position, current deviation, the forecast for their target stop and its risk."""
    return vehicles_now(request.app.state)


def vehicles_now(state) -> list[dict]:
    T = state.clock.now()
    max_age = state.settings.ui_max_ping_age_s
    return [p for v in state.fleet.vehicles.values() if (p := vehicle_payload(state, v, T, max_age)) is not None]


@router.get("/vehicles/{vehicle_id}/schedule")
def schedule(request: Request, vehicle_id: int) -> dict:
    """The vehicle's current trip, stop by stop: actual arrivals behind it, forecasts ahead."""
    state = request.app.state
    v = state.fleet.vehicles.get(vehicle_id)
    if v is None or v.schedule is None:
        raise HTTPException(404, f"no scheduled vehicle {vehicle_id}")
    return schedule_payload(state, v, state.clock.now())


@router.get("/metrics/model")
async def metrics_model(request: Request) -> dict:
    """The ML service's model metrics (cached; last known copy while ML is down) plus live backend metrics."""
    state = request.app.state
    cache = state.metrics_cache
    if time.time() - cache["at"] > METRICS_TTL_S:
        try:
            cache["ml"] = await state.predictor.client.metrics()
            cache["at"] = time.time()
        except MlUnavailable:
            cache["at"] = time.time() - METRICS_TTL_S + 5  # retry in 5 s, keep the last copy
    pred, alerts = state.predictor.snapshot(), state.alerts.snapshot()
    ml = cache["ml"] or {"model_version": "unavailable"}
    return {**ml, "live": {
        "ml_available": pred["ml_available"],
        "predictions": pred["stats"]["predictions_ml"] + pred["stats"]["predictions_fallback"],
        "horizon_ok_share": pred["horizon_ok_share"],
        "ml_batch_ms_p50": pred["ml_batch_ms_p50"], "ml_batch_ms_p95": pred["ml_batch_ms_p95"],
        "alerts_raised": alerts["stats"]["raised"], "alerts_verified": alerts["stats"]["verified"],
        "alert_precision": alerts["precision"], "alert_mae_s": alerts["mae_verified_s"],
    }}


@router.get("/metrics/worst_stops")
def metrics_worst_stops(request: Request, limit: int = Query(10, ge=1, le=50)) -> list[dict]:
    """Stops with the largest forecast delays over the last 30 minutes (average ≥ 30 s)."""
    state = request.app.state
    return worst_stops(state, state.clock.now(), limit)


@router.get("/routes/{route_id}/signals")
def route_signals(route_id: str) -> list[dict]:
    """Always empty: the dataset has no traffic-light data (the dashboard's signal layer is a mock-mode feature)."""
    return []


@router.get("/routes/{route_id}/hours")
def route_hours(request: Request, route_id: str) -> dict:
    """First and last planned visit of the route's day — so the UI can tell "line runs 5:38–00:47".

    Route id is the vehicle id (see views.py), so hours are just the plan bounds of that vehicle's schedule.
    ``next_departure`` is the closest future visit relative to the dataset clock, or None after the last run.
    """
    state = request.app.state
    try:
        tr_id = int(route_id)
    except ValueError:
        raise HTTPException(404, f"unknown route {route_id}")
    s = state.fleet.schedules.get(tr_id)
    if s is None or not s.visits:
        raise HTTPException(404, f"no schedule for route {route_id}")
    first, last = s.visits[0], s.visits[-1]
    now = state.clock.now()
    i = s.first_after(int(epoch_s(now))) if now >= first.plan else 0
    next_dep = s.visits[i].plan if i is not None and i < len(s.visits) else None
    return {
        "route_id": route_id,
        "first_plan": wall_iso(state.clock, first.plan),
        "last_plan": wall_iso(state.clock, last.plan),
        "first_hhmm": first.plan.strftime("%H:%M"),
        "last_hhmm": last.plan.strftime("%H:%M"),
        "next_departure": wall_iso(state.clock, next_dep) if next_dep is not None else None,
        "trips": max(v.trip for v in s.visits) + 1,
    }


@router.get("/metrics/bunching")
def metrics_bunching() -> list[dict]:
    """Always empty: bunching needs vehicles sharing a route and direction, and the dataset has no route relations
    between vehicles (each vehicle is its own "route")."""
    return []


class WhatifRequest(BaseModel):
    scenario: Literal["add_reserve", "adjust_interval", "detour", "signal_priority", "hold_at_stop"]
    route_id: str
    at_stop_id: str | None = None


@router.post("/whatif")
async def whatif(request: Request, body: WhatifRequest) -> dict:
    """Scenario effect on the vehicle's latest forecast, from ML ``/whatif/predict``; also pushed as ``whatif.result``."""
    state = request.app.state
    try:
        tr_id = int(body.route_id)
    except ValueError:
        raise HTTPException(404, f"unknown route {body.route_id}")
    req = state.predictor.last_requests.get(tr_id)
    if req is None:
        raise HTTPException(404, f"no forecast yet for vehicle {tr_id}")
    try:
        r = await state.predictor.client.whatif({**req, "scenario": body.scenario})
    except MlUnavailable as e:
        raise HTTPException(503, f"ML service unavailable: {e}")
    before, after = r["delay_baseline_sec"], r["delay_scenario_sec"]
    result = {
        "type": "whatif.result", "scenario": body.scenario, "route_id": body.route_id, "at_stop_id": body.at_stop_id,
        "summary": {"avg_delay_before_sec": round(before), "avg_delay_after_sec": round(after),
                    "red_before": int(r["risk_baseline"] >= RISK_RED), "red_after": int(r["risk_scenario"] >= RISK_RED)},
        "vehicles": [{"vehicle_id": str(tr_id), "delay_before_sec": round(before), "delay_after_sec": round(after),
                      "risk_before": round(r["risk_baseline"], 3), "risk_after": round(r["risk_scenario"], 3)}],
        "model_version": r.get("model_version"),
    }
    state.hub.publish(result)
    return result
