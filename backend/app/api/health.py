"""Service status and ingest diagnostics."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["health"])


class NdtpStatus(BaseModel):
    enabled: bool
    listening: bool = False
    port: int | None = None
    connections: int = 0
    units_connected: int = 0
    fixes_total: int = 0
    fixes_dropped: int = Field(0, description="fixes lost because the ingest queue was full")
    queue_depth: int = 0
    last_fix_age_s: float | None = Field(None, description="seconds since the last fix from any unit")


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    uptime_s: float
    ndtp: NdtpStatus


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Liveness and a short status. ``degraded`` = up, but a component isn't working."""
    state = request.app.state
    now = time.time()
    ndtp = NdtpStatus(enabled=state.ndtp is not None)
    if state.ndtp is not None:
        snap = state.ndtp.snapshot()
        ndtp = NdtpStatus(
            enabled=True,
            listening=snap["listening"],
            port=snap["port"],
            connections=len(snap["connections"]),
            units_connected=len(snap["units_connected"]),
            fixes_total=snap["stats"]["fixes"],
            fixes_dropped=snap["stats"]["fixes_dropped"],
            queue_depth=snap["sink_queue_depth"],
            last_fix_age_s=None if state.last_fix_at is None else round(now - state.last_fix_at, 1),
        )
    status = "degraded" if ndtp.enabled and not ndtp.listening else "ok"
    return HealthResponse(status=status, version=request.app.version, uptime_s=round(now - state.started_at, 1),
                          ndtp=ndtp)


@router.get("/ingest/ndtp")
def ndtp_diagnostics(request: Request) -> dict:
    """Full NDTP listener state: open connections, counters, framing errors and the last fix per unit."""
    state = request.app.state
    if state.ndtp is None:
        return {"enabled": False}
    snap = state.ndtp.snapshot()
    snap["last_fix"] = {
        unit: {
            "terminal_time": _iso(fix.nav.timestamp),
            "received_at": _iso(fix.received_at),
            "request_id": fix.request_id,
            "lat": fix.nav.latitude,
            "lon": fix.nav.longitude,
            "valid": fix.nav.location_valid,
            "speed_kmh": fix.nav.speed_avg_kmh,
            "course_deg": fix.nav.course_deg,
        }
        for unit, fix in sorted(state.last_fix.items())
    }
    return {"enabled": True, **snap}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
