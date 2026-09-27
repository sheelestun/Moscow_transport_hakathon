"""Ingest diagnostics: the NDTP listener, the pipeline, and the latest ping per vehicle."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.get("/ndtp")
def ndtp(request: Request) -> dict:
    """NDTP listener: open connections, counters, framing errors."""
    state = request.app.state
    if state.ndtp is None:
        return {"enabled": False}
    return {"enabled": True, **state.ndtp.snapshot()}


@router.get("/vehicles")
def vehicles(request: Request) -> dict:
    """Pipeline counters, replay progress, and the newest ping per vehicle with the source it came from."""
    state = request.app.state
    now = state.clock.now()
    return {
        "dataset_now": now.isoformat(),
        "pipeline": state.ingest.snapshot(),
        "replay": state.replay.snapshot() if state.replay is not None else {"status": "disabled"},
        "vehicles": {
            tr_id: {
                "unit_id": p.unit_id,
                "source": p.source,
                "event_time": p.event_time.isoformat(),
                "age_s": round((now - p.event_time).total_seconds(), 1),
                "lat": p.lat,
                "lon": p.lon,
                "valid": p.location_valid,
                "speed_kmh": p.speed_kmh,
                "heading_deg": p.heading_deg,
            }
            for tr_id, p in sorted(state.ingest.latest.items())
        },
    }
