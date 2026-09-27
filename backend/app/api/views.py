"""Dashboard views: routes, vehicles and schedules in the shapes ``frontend/js/mock.js`` defines.

The dataset has no routes — only each vehicle's planned stop visits for the day. So ``route_id`` is the
vehicle id, and every distinct trip shape (grouped by its first and last stop) is one "direction" with
its own line: the map projects the vehicle and its schedule onto the line of the vehicle's current trip,
which a back-and-forth whole-day polyline would make ambiguous. Base geometry is stop-to-stop straight
segments (there is no road geometry in the dataset); ``route_shapes.json`` overrides it with polylines that follow
the actual streets, and ``route_names.json`` gives the real route number of each vehicle. Both are built by
``infra/build_route_shapes_2gis.py`` from 2GIS (the route passing through the vehicle's stops and its road geometry,
with OSRM segments from ``infra/build_route_shapes.py`` where 2GIS has none).
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from ..predict import Prediction
from ..predict.predictor import level_for, risk_from_delay
from ..state import StopVisit, VehicleSchedule, VehicleState
from ..state.geo import epoch_s
from .timefmt import wall_iso

log = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1)
STALE_ARRIVAL_S = 1800  # last detected arrival older than this: locate the vehicle in its plan by time instead
ROUTE_SHAPES = Path(__file__).with_name("route_shapes.json")
ROUTE_NAMES = Path(__file__).with_name("route_names.json")


def _load_road_shapes() -> dict[str, dict[str, list[list[float]]]]:
    """OSRM-derived polylines for each (tr_id, direction_id). Missing/broken file → stop-to-stop fallback."""
    if not ROUTE_SHAPES.exists():
        log.info("route_shapes.json not found — routes will be drawn stop-to-stop")
        return {}
    try:
        shapes = json.loads(ROUTE_SHAPES.read_text(encoding="utf-8"))
        log.info("route_shapes.json: %d routes with road geometry", len(shapes))
        return shapes
    except (OSError, ValueError) as e:
        log.warning("route_shapes.json unreadable (%s) — falling back to stop-to-stop", e)
        return {}


def _load_route_names() -> dict[str, dict]:
    """Real route numbers per tr_id (``route_number``, ``route_numbers``, per-direction). Missing → none."""
    if not ROUTE_NAMES.exists():
        return {}
    try:
        return json.loads(ROUTE_NAMES.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("route_names.json unreadable (%s) — routes stay unnumbered", e)
        return {}


class RouteCatalog:
    """Built once from the schedules: route payloads and the direction of every trip."""

    def __init__(self, schedules: dict[int, VehicleSchedule]) -> None:
        self.routes: dict[int, dict] = {}
        self._direction: dict[tuple[int, int], int] = {}
        self._road_shapes = _load_road_shapes()
        self._route_names = _load_route_names()
        for tr_id, s in schedules.items():
            self.routes[tr_id] = self._build(tr_id, s)

    def direction(self, tr_id: int, trip: int) -> int:
        return self._direction.get((tr_id, trip), 0)

    def _build(self, tr_id: int, s: VehicleSchedule) -> dict:
        trips: dict[int, list[StopVisit]] = {}
        for v in s.visits:
            trips.setdefault(v.trip, []).append(v)
        shape = {t: (_key(vs[0]), _key(vs[-1])) for t, vs in trips.items()}
        count = Counter(shape.values())
        first_seen = {k: min(t for t in trips if shape[t] == k) for k in count}
        order = sorted(count, key=lambda k: (-count[k], first_seen[k]))
        index = {k: i for i, k in enumerate(order)}
        for t in trips:
            self._direction[(tr_id, t)] = index[shape[t]]
        route_shapes = self._road_shapes.get(str(tr_id), {})
        names = self._route_names.get(str(tr_id), {})
        directions = []
        for i, k in enumerate(order):
            rep = max((vs for t, vs in trips.items() if shape[t] == k), key=len)  # the fullest trip of that shape
            geometry = route_shapes.get(str(i)) or [[v.lat, v.lon] for v in rep]
            directions.append({"direction_id": i, "name": f"{rep[0].label} → {rep[-1].label}",
                               "route_number": (names.get("directions") or {}).get(str(i)),
                               "geometry": geometry,
                               "stops": [_stop(v) for v in rep]})
        main = directions[0]
        ends = main["name"].split(" → ")
        return {"route_id": str(tr_id), "name": f"{ends[0]} ↔ {ends[-1]}", "transport_type": "bus",
                "route_number": names.get("route_number"), "route_numbers": names.get("route_numbers") or [],
                "geometry": main["geometry"], "stops": main["stops"], "directions": directions}


def _key(v: StopVisit) -> tuple[float, float]:
    return (round(v.lat, 4), round(v.lon, 4))


def _stop(v: StopVisit) -> dict:
    return {"stop_id": str(v.stop_id), "name": v.label, "lat": v.lat, "lon": v.lon}


# ----------------------------------------------------------------------------- where is the vehicle in its plan


def next_pos(v: VehicleState, T: datetime) -> int:
    """Next planned visit: after the last detected arrival if that's recent, otherwise by plan time and current deviation."""
    s, d = v.schedule, v.derived
    t_s = epoch_s(T)
    if d is not None and d.last_arrival is not None and t_s - d.last_arrival.arrival_s <= STALE_ARRIVAL_S \
            and d.next_pos is not None:
        return d.next_pos
    cur = d.cur_dev_s if d is not None and d.cur_dev_s is not None else 0.0
    i = s.first_after(math.floor(t_s - cur))
    return len(s.visits) - 1 if i is None else i


def current_prediction(p: Prediction | None, T: datetime) -> Prediction | None:
    """The latest forecast, while its target stop is still ahead."""
    return p if p is not None and p.target_plan >= T else None


# ----------------------------------------------------------------------------- payloads


def vehicle_payload(state, v: VehicleState, T: datetime, max_age_s: float) -> dict | None:
    s, pos, last = v.schedule, v.last_position, v.last_ping
    if s is None or pos is None or (T - last.event_time).total_seconds() > max_age_s:
        return None  # no route to draw it on, or not in service
    d = v.derived
    cur = d.cur_dev_s if d is not None else None
    delay_now = round(cur) if cur is not None else 0
    p = current_prediction(state.predictor.latest.get(v.tr_id), T)
    if p is not None:
        delay_pred, risk, level = round(p.delay_pred_s), p.risk_score, p.risk_level
    else:
        risk = risk_from_delay(delay_now)
        delay_pred, level = delay_now, level_for(risk)
    trip = s.visits[next_pos(v, T)].trip
    out = {
        "vehicle_id": str(v.tr_id), "route_id": str(v.tr_id), "direction_id": state.routes.direction(v.tr_id, trip),
        "lat": pos.lat, "lon": pos.lon, "is_reserve": False, "speed": pos.speed_kmh, "heading": pos.heading_deg,
        "delay_now_sec": delay_now, "delay_pred_sec": delay_pred, "risk_score": round(risk, 3), "risk_level": level,
        "updated_at": wall_iso(state.clock, last.event_time), "headway_prev_sec": None, "plan_headway_sec": None,
        "source": last.source,
    }
    if p is not None:
        out.update(target_stop_id=str(p.target_stop_id), target_stop_name=s.visits[p.target_pos].label,
                   target_time_plan=wall_iso(state.clock, p.target_plan), forecast_source=p.source)
        if level != "green":
            r = p.response
            out.update(reason_pattern=r.get("reason_pattern"), recommendation=r.get("recommendation"),
                       recommendation_text=r.get("recommendation_text"), top_features=r.get("top_features", []),
                       causes=r.get("causes", []), confidence=p.confidence)
    return out


def schedule_payload(state, v: VehicleState, T: datetime) -> dict:
    """The vehicle's current trip: passed stops with the detected arrival, the target stop with the model's
    forecast, other upcoming stops with the current deviation carried forward."""
    s, d = v.schedule, v.derived
    nxt = next_pos(v, T)
    trip = s.visits[nxt].trip
    p = current_prediction(state.predictor.latest.get(v.tr_id), T)
    cur = d.cur_dev_s if d is not None and d.cur_dev_s is not None else 0.0
    rows = []
    for x in (x for x in s.visits if x.trip == trip):
        row = {"stop_id": str(x.stop_id), "name": x.label, "lat": x.lat, "lon": x.lon,
               "time_plan": wall_iso(state.clock, x.plan)}
        if x.pos < nxt:
            row["status"] = "passed"
            a = v.arrivals.get(x.pos)
            if a is not None:
                row.update(time_fact=wall_iso(state.clock, _EPOCH + timedelta(seconds=a.arrival_s)),
                           delay_sec=round(a.delay_s))
        else:
            is_target = p is not None and x.pos == p.target_pos
            est = p.delay_pred_s if is_target else cur
            row.update(status="next" if x.pos == nxt else "upcoming", delay_sec=round(est),
                       time_pred=wall_iso(state.clock, x.plan + timedelta(seconds=est)),
                       estimate="model" if is_target else "current_deviation")
            if is_target:
                row["is_target"] = True
        rows.append(row)
    route = state.routes.routes[v.tr_id]
    direction_id = state.routes.direction(v.tr_id, trip)
    return {"vehicle_id": str(v.tr_id), "route_id": str(v.tr_id), "direction_id": direction_id,
            "direction": route["directions"][direction_id]["name"], "stops": rows}


def worst_stops(state, T: datetime, limit: int, window_s: float = 1800) -> list[dict]:
    """Stops with the largest forecast delays over the last ``window_s`` of dataset time."""
    groups: dict[tuple, dict] = {}
    latest: dict[tuple[int, int], Prediction] = {}
    for p in state.predictor.recent:
        if (T - p.T).total_seconds() <= window_s:
            latest[(p.tr_id, p.target_stop_id)] = p
    for p in latest.values():
        x = state.fleet.schedules[p.tr_id].visits[p.target_pos]
        g = groups.setdefault(_key(x) + (x.label,), {"route_id": str(p.tr_id), "direction_id": 0,
                                                     "stop_id": str(x.stop_id), "name": x.label, "lat": x.lat,
                                                     "lon": x.lon, "delays": [], "vehicles": set()})
        g["delays"].append(p.delay_pred_s)
        g["vehicles"].add(p.tr_id)
    out = [{**{k: v for k, v in g.items() if k not in ("delays", "vehicles")},
            "avg_delay_sec": round(sum(g["delays"]) / len(g["delays"])), "max_delay_sec": round(max(g["delays"])),
            "vehicles": len(g["vehicles"])} for g in groups.values()]
    return sorted((r for r in out if r["avg_delay_sec"] >= 30), key=lambda r: -r["avg_delay_sec"])[:limit]
