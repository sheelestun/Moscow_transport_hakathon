"""The planned schedule: every vehicle's stop visits for the day, indexed for arrival detection.

``schedule_plan.csv`` rows are planned visits: ``tt_action_item_id`` identifies one visit (the same id
the ML contract calls ``target_stop_id``), ``building_address`` is the only stop name there is.
A planned gap longer than ``TRIP_GAP_S`` between consecutive visits is a terminal: a new trip begins.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .geo import dist_m, epoch_s

TRIP_GAP_S = 300  # same as ml/src/features/tabular.py

_POINT = re.compile(r"POINT \(([-\d.]+) ([-\d.]+)\)")
STOP_NAMES = Path(__file__).with_name("stop_names.json")

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StopVisit:
    pos: int               # index in the vehicle's day plan
    stop_id: int           # tt_action_item_id
    plan: datetime         # dataset time, naive Moscow-local
    lat: float
    lon: float
    manual_fill: bool      # fact entered by hand in the source system; arrival detection skips these
    name: str              # building_address
    geom: str              # original WKT, passed through to ML
    trip: int
    idx_in_trip: int

    @property
    def label(self) -> str:
        """What the dispatcher sees: the address, or a placeholder where the dataset has none."""
        return self.name or "остановка без адреса"


@dataclass(slots=True)
class VehicleSchedule:
    tr_id: int
    visits: list[StopVisit]
    plan_s: np.ndarray = field(init=False)    # epoch seconds of the plan (naive time read as UTC, as ML does)
    lon: np.ndarray = field(init=False)
    lat: np.ndarray = field(init=False)
    mf: np.ndarray = field(init=False)
    cum_dist_m: np.ndarray = field(init=False)  # straight-line distance along the plan; none added across a terminal

    def __post_init__(self) -> None:
        self.plan_s = np.array([epoch_s(v.plan) for v in self.visits], dtype=np.int64)
        self.lon = np.array([v.lon for v in self.visits])
        self.lat = np.array([v.lat for v in self.visits])
        self.mf = np.array([v.manual_fill for v in self.visits], dtype=bool)
        step = np.r_[0.0, dist_m(self.lon[1:], self.lat[1:], self.lon[:-1], self.lat[:-1])]
        new_trip = np.r_[False, np.diff([v.trip for v in self.visits]) != 0]
        step[new_trip] = 0.0
        self.cum_dist_m = np.cumsum(step)

    def first_after(self, t_s: int) -> int | None:
        """Position of the first visit planned strictly after ``t_s``."""
        i = int(np.searchsorted(self.plan_s, t_s, side="right"))
        return i if i < len(self.visits) else None


def _load_stop_names() -> dict[tuple[float, float], str]:
    """Real stop names. Key is (lat, lon) rounded to 5dp.
    Built by ``infra/build_route_shapes_2gis.py`` from the 2GIS stops of each vehicle's route (on top of the
    OSM names from ``infra/build_stop_names.py``); missing file → building addresses from the dataset."""
    if not STOP_NAMES.exists():
        return {}
    try:
        raw = json.loads(STOP_NAMES.read_text(encoding="utf-8"))
        out = {tuple(float(x) for x in k.split(",")): v for k, v in raw.items()}
        log.info("stop_names.json: %d названий остановок (2ГИС / OSM)", len(out))
        return out
    except (OSError, ValueError) as e:
        log.warning("stop_names.json unreadable (%s) — leaving unnamed stops as-is", e)
        return {}


def load_schedule(path: Path) -> dict[int, VehicleSchedule]:
    rows_by_tr: dict[int, list[dict]] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows_by_tr.setdefault(int(row["tr_id"]), []).append(row)

    stop_names = _load_stop_names()
    out: dict[int, VehicleSchedule] = {}
    for tr_id, rows in rows_by_tr.items():
        rows.sort(key=lambda r: r["time_begin"])
        visits: list[StopVisit] = []
        trip = idx = 0
        prev_plan: datetime | None = None
        for pos, r in enumerate(rows):
            plan = datetime.fromisoformat(r["time_begin"])
            if prev_plan is not None:
                if (plan - prev_plan).total_seconds() > TRIP_GAP_S:
                    trip, idx = trip + 1, 0
                else:
                    idx += 1
            prev_plan = plan
            m = _POINT.match(r["geom"])
            if m is None:
                raise ValueError(f"{path}: bad geom {r['geom']!r} for tt_action_item_id {r['tt_action_item_id']}")
            lon, lat = float(m[1]), float(m[2])
            # настоящее название остановки (2ГИС / OSM) важнее адреса дома из датасета
            name = stop_names.get((round(lat, 5), round(lon, 5))) or r["building_address"] or ""
            visits.append(StopVisit(pos=pos, stop_id=int(r["tt_action_item_id"]), plan=plan, lon=lon, lat=lat,
                                    manual_fill=r["manual_fill"] == "True", name=name, geom=r["geom"],
                                    trip=trip, idx_in_trip=idx))
        out[tr_id] = VehicleSchedule(tr_id, visits)
    return out
