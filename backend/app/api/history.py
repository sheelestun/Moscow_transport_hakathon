"""Read side of the Postgres history (what survives restarts)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

router = APIRouter(prefix="/history", tags=["history"])


def _pool(request: Request):
    h = request.app.state.history
    if h is None:
        raise HTTPException(404, "history is off (DATABASE_URL not set)")
    if h.pool is None:
        raise HTTPException(503, f"Postgres unavailable: {h.last_error}")
    return h.pool


@router.get("/summary")
async def summary(request: Request) -> dict:
    """Rows per table and the time span they cover."""
    pool = _pool(request)
    out = {}
    for table, col in (("telemetry", "event_time"), ("arrivals", "arrival"), ("predictions", "t"), ("alerts", "created_at")):
        row = await pool.fetchrow(f"SELECT count(*) AS n, min({col}) AS first, max({col}) AS last FROM {table}")
        out[table] = {"rows": row["n"], "first": row["first"], "last": row["last"]}
    return out


@router.get("/alerts")
async def alerts(request: Request, limit: int = Query(50, ge=1, le=1000)) -> list[dict]:
    """Alerts from all runs, newest first, with their outcome."""
    rows = await _pool(request).fetch(
        "SELECT alert_id, tr_id, target_stop_id, target_plan, segment_from, segment_to, delay_pred_s, risk_score, "
        "status, delay_fact_s, hit, resolution, created_at, closed_at FROM alerts ORDER BY created_at DESC LIMIT $1",
        limit)
    return [dict(r) for r in rows]
