"""Arrival detection from GPS: when did a vehicle actually reach each planned stop.

A port of ``ml/src/features/tabular.gps_history`` with the same constants: the prediction model was
trained on arrivals found by exactly this rule, so the backend's current deviation must use it too.

For a moment T, walk the stops planned in [T − 1 h, T + 5 min] (skipping manually-filled ones) in
order. The arrival at a stop is the GPS fix closest to it, within ``ARR_RADIUS_M``, inside the window
[plan − 7 min, plan + 12 min] ∩ (previous arrival, T] — and only once the vehicle has since moved
``LEAVE_M`` further away (it has left the stop). Dwell = time spent within ``ARR_RADIUS_M``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geo import dist_m
from .schedule import VehicleSchedule

ARR_RADIUS_M = 60
LEAVE_M = 30
WIN_EARLY_S = 420
WIN_LATE_S = 720
HIST_BACK_S = 3600
LOOK_AHEAD_S = 300


@dataclass(frozen=True, slots=True)
class Arrival:
    pos: int           # visit index in the vehicle's plan
    plan_s: int        # planned time, epoch seconds
    arrival_s: int     # detected arrival, epoch seconds
    delay_s: float     # arrival − plan: + late, − early
    dwell_s: float


def detect_arrivals(ts: np.ndarray, lon: np.ndarray, lat: np.ndarray, sched: VehicleSchedule, T: int) -> list[Arrival]:
    """Arrivals at stops planned in [T − 1 h, T + 5 min], from cleaned telemetry (``ts`` sorted, int seconds,
    only fixes with ``ts <= T`` matter; invalid positions are NaN)."""
    plan = sched.plan_s
    cand = np.where((plan >= T - HIST_BACK_S) & (plan <= T + LOOK_AHEAD_S) & ~sched.mf)[0]
    out: list[Arrival] = []
    prev = -np.inf
    for i in cand:
        lo, hi = max(plan[i] - WIN_EARLY_S, prev + 1), min(plan[i] + WIN_LATE_S, T)
        a, b = np.searchsorted(ts, lo), np.searchsorted(ts, hi, side="right")
        if b - a < 2:
            continue
        d = dist_m(lon[a:b], lat[a:b], sched.lon[i], sched.lat[i])
        if np.all(np.isnan(d)):
            continue
        j = int(np.nanargmin(d))
        after = d[j + 1:]
        after = after[~np.isnan(after)]
        if d[j] < ARR_RADIUS_M and len(after) and after.max() > d[j] + LEAVE_M:
            near = np.where(d < ARR_RADIUS_M)[0]
            dwell = float(ts[a + near.max()] - ts[a + near.min()]) if len(near) else 0.0
            prev = ts[a + j]
            out.append(Arrival(int(i), int(plan[i]), int(prev), float(prev - plan[i]), dwell))
    return out
