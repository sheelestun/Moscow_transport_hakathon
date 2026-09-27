"""Per-vehicle state: telemetry buffer, arrivals log, and the features derived from them.

``Fleet`` subscribes to the ingest pipeline. Each ping goes into its vehicle's time-ordered buffer and
marks the vehicle dirty; ``update()`` re-runs arrival detection for dirty vehicles only (cost follows the
incoming data, not the fleet size) and merges the result into the vehicle's arrivals log — stops still
inside the detection window are re-detected, older ones are final.

Derived per vehicle (criterion 3 — "текущее отклонение, средняя скорость на сегменте, время простоя"):

* current deviation: delay at the latest detected arrival (this is ``cur_dev_s`` for the model);
* segment speed: distance along the plan / time between the last two arrivals of the same trip;
* dwell: time spent at the last detected stop;
* current segment: last detected stop → next planned stop.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from bisect import bisect_left, insort
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from ..clock import DatasetClock
from ..ingest import Ping
from .arrivals import HIST_BACK_S, Arrival, detect_arrivals
from .geo import epoch_s
from .schedule import VehicleSchedule

log = logging.getLogger(__name__)

BUFFER_S = 2 * 3600 + 900   # telemetry kept per vehicle: the model looks back 1 h, detection windows reach 12 min further


def _ping_key(p: Ping) -> tuple[datetime, bool]:
    return (p.event_time, p.is_hist)  # same instant: the live fix sorts before the re-sent one


@dataclass(frozen=True, slots=True)
class Derived:
    computed_for: datetime             # dataset time T the features describe
    last_arrival: Arrival | None
    cur_dev_s: float | None            # delay at the last detected arrival
    segment_speed_kmh: float | None
    dwell_s: float | None
    next_pos: int | None               # next planned visit after the last detected arrival


@dataclass(slots=True)
class VehicleState:
    tr_id: int
    schedule: VehicleSchedule | None
    pings: list[Ping] = field(default_factory=list)       # sorted by (event_time, is_hist)
    arrivals: dict[int, Arrival] = field(default_factory=dict)
    dirty: bool = False
    derived: Derived | None = None

    @property
    def last_ping(self) -> Ping | None:
        return self.pings[-1] if self.pings else None

    @property
    def last_position(self) -> Ping | None:
        return next((p for p in reversed(self.pings) if p.lat is not None), None)


class Fleet:
    def __init__(self, schedules: dict[int, VehicleSchedule], clock: DatasetClock, *, buffer_s: float = BUFFER_S) -> None:
        self.schedules = schedules
        self.clock = clock
        self.buffer_s = buffer_s
        self.vehicles: dict[int, VehicleState] = {}
        self.update_ms: deque[float] = deque(maxlen=500)
        self._listeners: list[Callable[[int, list[Arrival]], None]] = []

    def on_arrivals(self, fn: Callable[[int, list[Arrival]], None]) -> None:
        """Called with (tr_id, newly detected arrivals) after each update — for the history store."""
        self._listeners.append(fn)

    # ------------------------------------------------------------------ input

    def on_ping(self, ping: Ping) -> None:
        v = self.vehicles.get(ping.tr_id)
        if v is None:
            v = self.vehicles[ping.tr_id] = VehicleState(ping.tr_id, self.schedules.get(ping.tr_id))
        pings = v.pings
        if pings and _ping_key(ping) >= _ping_key(pings[-1]):
            pings.append(ping)
        else:
            insort(pings, ping, key=_ping_key)
        cut = bisect_left(pings, epoch_s(pings[-1].event_time) - self.buffer_s, key=lambda p: epoch_s(p.event_time))
        if cut:
            del pings[:cut]
        v.dirty = True

    # ------------------------------------------------------------------ derivation

    def update(self, T: datetime | None = None) -> int:
        """Recompute dirty vehicles as of dataset time ``T`` (default: now). Returns how many were updated."""
        T = self.clock.now() if T is None else T
        started = time.perf_counter()
        n = 0
        for v in self.vehicles.values():
            if v.dirty and v.schedule is not None:
                self._recompute(v, T)
                n += 1
            v.dirty = False
        if n:
            self.update_ms.append((time.perf_counter() - started) * 1000)
        return n

    def _recompute(self, v: VehicleState, T: datetime) -> None:
        t_s = math.floor(epoch_s(T))
        ts, lon, lat = self._arrays(v.pings, t_s)
        detected = detect_arrivals(ts, lon, lat, v.schedule, t_s)
        window_start = t_s - HIST_BACK_S
        old = {pos: a for pos, a in v.arrivals.items() if a.plan_s < window_start}  # outside the window: final
        new = {a.pos: a for a in detected}
        fresh = [a for pos, a in new.items() if v.arrivals.get(pos) != a]
        v.arrivals = old | new
        v.derived = self._derive(v, T)
        if fresh:
            for fn in self._listeners:
                fn(v.tr_id, fresh)

    @staticmethod
    def _arrays(pings: list[Ping], t_s: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Telemetry up to T with ML's cleaning rules (``tabular.make_tele``): bad fixes → NaN, exact-duplicate
        timestamps dropped (first kept), times rounded up to whole seconds."""
        ts, lon, lat, last = [], [], [], None
        for p in pings:
            t = epoch_s(p.event_time)
            if t == last:
                continue
            last = t
            if math.ceil(t) > t_s:
                break
            ok = p.lat is not None and p.lon is not None and abs(p.lat) >= 1 and abs(p.lon) >= 1
            ts.append(math.ceil(t))
            lon.append(p.lon if ok else np.nan)
            lat.append(p.lat if ok else np.nan)
        return np.array(ts, dtype=np.int64), np.array(lon, dtype=float), np.array(lat, dtype=float)

    @staticmethod
    def _derive(v: VehicleState, T: datetime) -> Derived:
        s = v.schedule
        ordered = sorted(v.arrivals.values(), key=lambda a: a.arrival_s)
        last = ordered[-1] if ordered else None
        speed = None
        if len(ordered) >= 2:
            prev = ordered[-2]
            same_trip = s.visits[prev.pos].trip == s.visits[last.pos].trip
            dt = last.arrival_s - prev.arrival_s
            if same_trip and dt > 0:
                speed = round(float(s.cum_dist_m[last.pos] - s.cum_dist_m[prev.pos]) / dt * 3.6, 1)
        if last is not None:
            next_pos = last.pos + 1 if last.pos + 1 < len(s.visits) else None
        else:
            next_pos = s.first_after(math.floor(epoch_s(T)))
        return Derived(computed_for=T, last_arrival=last, cur_dev_s=None if last is None else last.delay_s,
                       segment_speed_kmh=speed, dwell_s=None if last is None else last.dwell_s, next_pos=next_pos)

    async def run(self, tick_s: float = 2.0) -> None:
        while True:
            self.update()
            await asyncio.sleep(tick_s)

    # ------------------------------------------------------------------ queries

    def telemetry(self, tr_id: int, T: datetime, span_s: float = 3600.0) -> list[Ping]:
        """The vehicle's pings with ``T − span < event_time <= T`` (what the model may see at T)."""
        v = self.vehicles.get(tr_id)
        if v is None:
            return []
        lo = epoch_s(T) - span_s
        return [p for p in v.pings if lo < epoch_s(p.event_time) and p.event_time <= T]

    def snapshot(self) -> dict:
        vs = self.vehicles.values()
        ms = sorted(self.update_ms)
        return {
            "vehicles": len(self.vehicles),
            "with_schedule": sum(v.schedule is not None for v in vs),
            "with_deviation": sum(v.derived is not None and v.derived.cur_dev_s is not None for v in vs),
            "arrivals_detected": sum(len(v.arrivals) for v in vs),
            "update_ms_p50": round(ms[len(ms) // 2], 2) if ms else None,
            "update_ms_max": round(ms[-1], 2) if ms else None,
        }
