"""Service status and the dataset clock."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

router = APIRouter(tags=["health"])


class ClockStatus(BaseModel):
    dataset_now: datetime = Field(description="current time on the dataset timeline (naive, Moscow-local)")
    speed: float


class NdtpStatus(BaseModel):
    enabled: bool
    listening: bool = False
    port: int | None = None
    connections: int = 0
    units_connected: int = 0
    fixes_total: int = 0
    fixes_dropped: int = Field(0, description="fixes lost because the ingest queue was full")
    queue_depth: int = 0


class IngestStatus(BaseModel):
    vehicles_seen: int
    vehicles_live_ndtp: int = Field(description="vehicles currently fed by NDTP rather than replay")
    registry_units: int
    unknown_units: int = Field(description="NDTP units not in the registry (their fixes are ignored)")
    last_ping_age_s: float | None = Field(None, description="wall seconds since the newest ping from any source")


class ReplayStatus(BaseModel):
    enabled: bool
    status: str | None = None
    position: int = 0
    total: int = 0


class StateStatus(BaseModel):
    vehicles: int
    with_schedule: int
    with_deviation: int = Field(description="vehicles with a current deviation from the schedule")
    arrivals_detected: int
    update_ms_p50: float | None = None
    update_ms_max: float | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    issues: list[str]
    version: str
    uptime_s: float
    clock: ClockStatus
    ndtp: NdtpStatus
    ingest: IngestStatus
    replay: ReplayStatus
    state: StateStatus


class ClockResponse(BaseModel):
    dataset_now: datetime
    speed: float
    anchor_wall: float = Field(description="Unix seconds")
    anchor_dataset: datetime
    rule: str = "dataset_time = anchor_dataset + (wall_time - anchor_wall) * speed"


@router.get("/health", response_model=HealthResponse)
def health(request: Request) -> HealthResponse:
    """Liveness and a short status. ``degraded`` = up, but something needs attention (see ``issues``)."""
    state = request.app.state
    now = time.time()
    issues = list(state.issues)

    ndtp = NdtpStatus(enabled=state.ndtp is not None)
    if state.ndtp is not None:
        snap = state.ndtp.snapshot()
        ndtp = NdtpStatus(enabled=True, listening=snap["listening"], port=snap["port"],
                          connections=len(snap["connections"]), units_connected=len(snap["units_connected"]),
                          fixes_total=snap["stats"]["fixes"], fixes_dropped=snap["stats"]["fixes_dropped"],
                          queue_depth=snap["sink_queue_depth"])
        if not ndtp.listening:
            issues.append("NDTP listener is not listening")

    ing = state.ingest.snapshot()
    newest = max((p.received_at for p in state.ingest.latest.values()), default=None)
    replay = ReplayStatus(enabled=state.replay is not None)
    if state.replay is not None:
        replay = ReplayStatus(enabled=True, **{k: v for k, v in state.replay.snapshot().items() if k != "next_event_time"})

    return HealthResponse(
        status="degraded" if issues else "ok",
        issues=issues,
        version=request.app.version,
        uptime_s=round(now - state.started_at, 1),
        clock=ClockStatus(dataset_now=state.clock.now(), speed=state.clock.speed),
        ndtp=ndtp,
        ingest=IngestStatus(vehicles_seen=ing["vehicles_seen"], vehicles_live_ndtp=len(ing["vehicles_live_ndtp"]),
                            registry_units=ing["registry_units"], unknown_units=len(state.ingest.unknown_units),
                            last_ping_age_s=None if newest is None else round(now - newest, 1)),
        replay=replay,
        state=StateStatus(**state.fleet.snapshot()),
    )


@router.get("/clock", response_model=ClockResponse)
def clock(request: Request) -> ClockResponse:
    """The dataset clock. External drivers (``infra/emulator_replay.py --clock-url``) sync to it."""
    c = request.app.state.clock
    return ClockResponse(dataset_now=c.now(), speed=c.speed, anchor_wall=c.anchor_wall, anchor_dataset=c.anchor_dataset)
