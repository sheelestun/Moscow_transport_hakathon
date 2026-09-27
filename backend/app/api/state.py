"""Vehicle state diagnostics: derived features per vehicle (criterion 3) and the detected arrivals."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Request

from ..state import Arrival, VehicleSchedule, VehicleState

router = APIRouter(prefix="/state", tags=["state"])

_EPOCH = datetime(1970, 1, 1)


@router.get("/vehicles")
def vehicles(request: Request) -> dict:
    """Per vehicle: last ping, current deviation from the schedule, segment speed, dwell, current segment."""
    fleet = request.app.state.fleet
    return {"summary": fleet.snapshot(),
            "vehicles": {tr: _vehicle(v) for tr, v in sorted(fleet.vehicles.items())}}


@router.get("/vehicles/{tr_id}/arrivals")
def arrivals(request: Request, tr_id: int) -> list[dict]:
    """All arrivals detected for the vehicle today, in order."""
    v = request.app.state.fleet.vehicles.get(tr_id)
    if v is None or v.schedule is None:
        raise HTTPException(404, f"no scheduled vehicle {tr_id}")
    return [_arrival(v.schedule, a) for a in sorted(v.arrivals.values(), key=lambda a: a.arrival_s)]


def _vehicle(v: VehicleState) -> dict:
    last, pos = v.last_ping, v.last_position
    out = {
        "has_schedule": v.schedule is not None,
        "pings_buffered": len(v.pings),
        "last_ping": None if last is None else {"event_time": last.event_time.isoformat(), "source": last.source},
        "position": None if pos is None else {"lat": pos.lat, "lon": pos.lon, "speed_kmh": pos.speed_kmh,
                                              "event_time": pos.event_time.isoformat()},
        "derived": None,
    }
    d, s = v.derived, v.schedule
    if d is not None and s is not None:
        nxt = s.visits[d.next_pos] if d.next_pos is not None else None
        out["derived"] = {
            "computed_for": d.computed_for.isoformat(),
            "cur_dev_s": d.cur_dev_s,
            "segment_speed_kmh": d.segment_speed_kmh,
            "dwell_s": d.dwell_s,
            "last_stop": None if d.last_arrival is None else _arrival(s, d.last_arrival),
            "next_stop": None if nxt is None else {"stop_id": nxt.stop_id, "name": nxt.name, "plan": nxt.plan.isoformat()},
        }
    return out


def _arrival(s: VehicleSchedule, a: Arrival) -> dict:
    visit = s.visits[a.pos]
    return {"stop_id": visit.stop_id, "name": visit.name, "plan": visit.plan.isoformat(),
            "arrival": (_EPOCH + timedelta(seconds=a.arrival_s)).isoformat(), "delay_s": a.delay_s,
            "dwell_s": a.dwell_s, "trip": visit.trip}
