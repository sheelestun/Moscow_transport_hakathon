"""Dispatcher alerts, in the shape the dashboard renders (``frontend/js/sidebar.js``, ``mock.js`` ``makeAlert``)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Query, Request

from ..alerts import Alert
from ..clock import DatasetClock
from .timefmt import unix_iso, wall_iso

router = APIRouter(tags=["alerts"])


@router.get("/alerts")
def alerts(request: Request, active: bool = Query(True, description="true: active alerts; false: recently closed")) -> list[dict]:
    """Active alerts (``alert.new`` payloads), or the most recently verified / resolved ones."""
    engine, clock = request.app.state.alerts, request.app.state.clock
    if active:
        return [alert_payload(clock, a, "alert.new") for a in engine.active.values()]
    return [alert_payload(clock, a, "alert.verified" if a.status == "verified" else "alert.resolved")
            for a in reversed(engine.closed)]


def alert_payload(clock: DatasetClock, a: Alert, kind: str) -> dict:
    p, r = a.prediction, a.prediction.response
    out = {
        "type": kind,
        "alert_id": a.alert_id,
        "vehicle_id": str(a.tr_id),
        "route_id": str(a.tr_id),
        "target_stop_id": str(p.target_stop_id),
        "target_stop_name": a.segment_to,
        "segment": {"from": a.segment_from, "to": a.segment_to},
        "delay_pred_sec": round(p.delay_pred_s),
        "delay_interval_sec": r.get("delay_interval_sec"),
        "risk_score": round(p.risk_score, 3),
        "risk_level": p.risk_level,
        "confidence": p.confidence,
        "eta_incident": wall_iso(clock, p.target_plan + timedelta(seconds=p.delay_pred_s)),
        "target_time_plan": wall_iso(clock, p.target_plan),
        "lead_min": round(p.lead_s / 60, 1),
        "reason_pattern": r.get("reason_pattern"),
        "recommendation": r.get("recommendation"),
        "recommendation_text": r.get("recommendation_text"),
        "causes": r.get("causes", []),
        "top_features": r.get("top_features", []),
        "model_version": r.get("model_version"),
        "source": p.source,
        "created_at": unix_iso(a.created_at),
        "status": a.status,
    }
    if a.status == "verified":
        out.update(delay_fact_sec=round(a.delay_fact_s), hit=a.hit, verified_at=unix_iso(a.closed_at))
    elif a.status == "resolved":
        out.update(resolution=a.resolution, resolved_at=unix_iso(a.closed_at))
    return out
