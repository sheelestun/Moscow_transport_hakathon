"""Forecast diagnostics: the latest forecast per vehicle and the most recent ones overall."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request

from ..predict import Prediction

router = APIRouter(prefix="/predictions", tags=["predictions"])

_PASSTHROUGH = ("model_version", "data_status", "reason_pattern", "recommendation", "recommendation_text", "causes",
                "top_features", "delay_interval_sec", "p_early", "p_ontime", "p_late", "off_route_m", "latency_ms")


@router.get("")
def latest(request: Request) -> dict:
    """Latest forecast per vehicle, plus predictor counters."""
    pred = request.app.state.predictor
    return {"summary": pred.snapshot(),
            "vehicles": {tr: view(request, p) for tr, p in sorted(pred.latest.items())}}


@router.get("/recent")
def recent(request: Request, limit: int = Query(50, ge=1, le=1000)) -> list[dict]:
    """The newest forecasts across all vehicles, newest first."""
    pred = request.app.state.predictor
    return [view(request, p) for p in list(pred.recent)[-limit:][::-1]]


def view(request: Request, p: Prediction) -> dict:
    s = request.app.state.fleet.schedules.get(p.tr_id)
    return {
        "tr_id": p.tr_id, "sample_id": p.sample_id, "T": p.T.isoformat(), "source": p.source,
        "target_stop_id": p.target_stop_id, "target_name": s.visits[p.target_pos].name if s else None,
        "target_plan": p.target_plan.isoformat(), "lead_s": p.lead_s, "horizon_ok": p.horizon_ok,
        "cur_dev_s": p.cur_dev_s, "delay_pred_s": p.delay_pred_s, "risk_score": p.risk_score,
        "risk_level": p.risk_level, "confidence": p.confidence,
        **{k: p.response[k] for k in _PASSTHROUGH if k in p.response},
    }
