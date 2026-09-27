"""Alerts: turns red forecasts into dispatcher alerts and checks them against what actually happened.

* **Raise** when a forecast's risk (the model's probability of arriving > 2 min late) reaches
  ``risk_threshold`` and its target stop is 10–15 minutes ahead — so an alert is never about a stop the
  vehicle already reached (criterion 2: no after-the-fact alerts).
* **One active alert per vehicle**: later red forecasts for it are counted, not re-raised.
* **Verify** once the fleet detects the arrival at the target stop: ``verified`` with the actual delay, scored
  as a hit (actually > 2 min late) or a false alarm. No arrival detected by the end of the detection window
  → ``resolved`` with the reason. Precision and forecast error of alerts are therefore measured live.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Literal

from .clock import DatasetClock
from .predict import Prediction
from .state import Fleet
from .state.arrivals import WIN_LATE_S

log = logging.getLogger(__name__)

LATE_S = 120  # the dataset's "late" class boundary (target_class: late > +120 s)

Status = Literal["active", "verified", "resolved"]
Event = Literal["alert.new", "alert.verified", "alert.resolved"]


@dataclass(slots=True)
class Alert:
    alert_id: str
    tr_id: int
    prediction: Prediction
    segment_from: str | None      # last stop the vehicle was detected at when the alert was raised
    segment_to: str | None        # the target stop
    created_at: float             # wall clock
    status: Status = "active"
    delay_fact_s: float | None = None
    closed_at: float | None = None
    resolution: str | None = None

    @property
    def hit(self) -> bool | None:
        return None if self.delay_fact_s is None else self.delay_fact_s > LATE_S


@dataclass(slots=True)
class AlertStats:
    raised: int = 0
    duplicates_suppressed: int = 0   # red forecast for a vehicle that already has an active alert
    verified: int = 0
    hits: int = 0                    # verified and actually > 2 min late
    resolved_unverified: int = 0
    abs_error_sum_s: float = 0.0     # |forecast − actual| over verified alerts


class AlertEngine:
    def __init__(self, fleet: Fleet, clock: DatasetClock, *, risk_threshold: float = 0.7,
                 verify_grace_s: float = WIN_LATE_S + 120, id_prefix: str = "a",
                 wall: Callable[[], float] = time.time) -> None:
        self.fleet = fleet
        self.clock = clock
        self.risk_threshold = risk_threshold
        self.verify_grace_s = verify_grace_s
        self.stats = AlertStats()
        self.active: dict[int, Alert] = {}          # tr_id → its active alert
        self.closed: deque[Alert] = deque(maxlen=1000)
        self.id_prefix = id_prefix              # unique per run, so ids don't collide in the history table
        self._ids = itertools.count(1)
        self._listeners: list[Callable[[Event, Alert], None]] = []
        self._wall = wall

    def on_event(self, fn: Callable[[Event, Alert], None]) -> None:
        self._listeners.append(fn)

    # ------------------------------------------------------------------ raising

    def on_prediction(self, p: Prediction) -> None:
        if p.risk_score < self.risk_threshold or not p.horizon_ok:
            return
        if p.tr_id in self.active:
            self.stats.duplicates_suppressed += 1
            return
        v = self.fleet.vehicles.get(p.tr_id)
        s = v.schedule if v else None
        last = v.derived.last_arrival if v and v.derived else None
        alert = Alert(alert_id=f"{self.id_prefix}-{next(self._ids)}", tr_id=p.tr_id, prediction=p,
                      segment_from=s.visits[last.pos].label if s and last else None,
                      segment_to=s.visits[p.target_pos].label if s else None, created_at=self._wall())
        self.active[p.tr_id] = alert
        self.stats.raised += 1
        log.info("alert %s: vehicle %d, %+.0f s at %s (risk %.2f)", alert.alert_id, p.tr_id, p.delay_pred_s,
                 alert.segment_to, p.risk_score)
        self._emit("alert.new", alert)

    # ------------------------------------------------------------------ verification

    def check(self, T: datetime | None = None) -> None:
        T = self.clock.now() if T is None else T
        for tr_id, alert in list(self.active.items()):
            p = alert.prediction
            v = self.fleet.vehicles.get(tr_id)
            arrival = v.arrivals.get(p.target_pos) if v else None
            if arrival is not None:
                self._close(alert, "verified", "arrival detected", arrival.delay_s)
            elif T > p.target_plan + timedelta(seconds=self.verify_grace_s):
                manual = v is not None and v.schedule is not None and v.schedule.visits[p.target_pos].manual_fill
                self._close(alert, "resolved", "manual-fill stop: no GPS arrival to compare" if manual
                            else "no arrival detected at the target stop")

    def _close(self, alert: Alert, status: Status, resolution: str, delay_fact_s: float | None = None) -> None:
        alert.status, alert.resolution, alert.closed_at = status, resolution, self._wall()
        alert.delay_fact_s = delay_fact_s
        del self.active[alert.tr_id]
        self.closed.append(alert)
        if status == "verified":
            self.stats.verified += 1
            self.stats.hits += bool(alert.hit)
            self.stats.abs_error_sum_s += abs(alert.prediction.delay_pred_s - delay_fact_s)
            self._emit("alert.verified", alert)
        else:
            self.stats.resolved_unverified += 1
            self._emit("alert.resolved", alert)

    def _emit(self, kind: Event, alert: Alert) -> None:
        for fn in self._listeners:
            try:
                fn(kind, alert)
            except Exception:  # noqa: BLE001 — a broken consumer must not stop alerting
                log.exception("alert listener %r failed", fn)

    async def run(self, tick_s: float = 5.0) -> None:
        while True:
            self.check()
            await asyncio.sleep(tick_s)

    # ------------------------------------------------------------------ introspection

    def snapshot(self) -> dict:
        s = self.stats
        return {
            "active": len(self.active),
            "stats": asdict(s),
            "precision": round(s.hits / s.verified, 3) if s.verified else None,
            "mae_verified_s": round(s.abs_error_sum_s / s.verified, 1) if s.verified else None,
        }
