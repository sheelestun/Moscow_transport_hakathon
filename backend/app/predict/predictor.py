"""The predictor: every tick, finds each vehicle's target stop 10–15 minutes ahead and gets a delay forecast.

Target = the first planned visit in (T + 10 min, T + 15 min] — the dataset's own definition, so the
horizon criterion holds by construction. One prediction per (vehicle, target stop): as T moves on, the
target rolls forward and a fresh prediction follows, roughly every 1–3 minutes per vehicle.

If the ML service is unreachable, the backend issues its own baseline forecast (delay = current
deviation, risk from the same sigmoid the ML contract uses) marked ``source="fallback"`` and retries ML
after ``retry_s`` — the dashboard keeps working in degraded mode (criterion 5).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

from ..clock import DatasetClock
from ..state import Fleet, StopVisit, VehicleState
from ..state.geo import epoch_s
from .client import MlClient, MlUnavailable
from .payload import predict_request, schedule_rows

log = logging.getLogger(__name__)

HORIZON_S = (600, 900)
RISK_RED, RISK_YELLOW = 0.7, 0.35  # same thresholds as the dashboard (frontend/js/config.js)


@dataclass(frozen=True, slots=True)
class Prediction:
    tr_id: int
    sample_id: str
    T: datetime                      # dataset time the forecast was made at
    target_pos: int
    target_stop_id: int
    target_plan: datetime
    cur_dev_s: float | None          # current deviation sent to the model (None: unknown, 0 was sent)
    delay_pred_s: float
    risk_score: float
    risk_level: str
    confidence: float
    source: Literal["ml", "fallback"]
    made_at: float                   # wall clock
    response: dict = field(repr=False)  # the full ML response (or the fallback's equivalent)

    @property
    def lead_s(self) -> float:
        return (self.target_plan - self.T).total_seconds()

    @property
    def horizon_ok(self) -> bool:
        return HORIZON_S[0] < self.lead_s <= HORIZON_S[1]


@dataclass(slots=True)
class PredictorStats:
    batches: int = 0
    predictions_ml: int = 0
    predictions_fallback: int = 0
    ml_errors: int = 0
    horizon_ok: int = 0


class Predictor:
    def __init__(self, fleet: Fleet, clock: DatasetClock, client: MlClient, *, max_ping_age_s: float = 900.0,
                 telemetry_span_s: float = 4500.0, batch_max: int = 64, retry_s: float = 30.0,
                 wall: Callable[[], float] = time.time) -> None:
        self.fleet = fleet
        self.clock = clock
        self.client = client
        self.max_ping_age_s = max_ping_age_s
        self.telemetry_span_s = telemetry_span_s
        self.batch_max = batch_max
        self.retry_s = retry_s
        self.stats = PredictorStats()
        self.latest: dict[int, Prediction] = {}
        self.recent: deque[Prediction] = deque(maxlen=5000)
        self.ml_available: bool | None = None       # None: not tried yet
        self.last_error: str | None = None
        self.ml_batch_ms: deque[float] = deque(maxlen=500)
        self.last_requests: dict[int, dict] = {}     # tr_id → last request body (reused by what-if)
        self._done: dict[tuple[int, int], str] = {}  # (tr_id, target stop) → source of its prediction
        self._schedule_rows: dict[int, list[dict]] = {}
        self._retry_at = 0.0
        self._listeners: list[Callable[[Prediction], None]] = []
        self._wall = wall

    def on_prediction(self, fn: Callable[[Prediction], None]) -> None:
        self._listeners.append(fn)

    # ------------------------------------------------------------------ what's due

    def due(self, T: datetime) -> Iterator[tuple[VehicleState, StopVisit]]:
        t_s = math.floor(epoch_s(T))
        ml_retry_open = self._wall() >= self._retry_at
        for v in self.fleet.vehicles.values():
            s, last = v.schedule, v.last_ping
            if s is None or last is None or (T - last.event_time).total_seconds() > self.max_ping_age_s:
                continue  # not scheduled, or not in service right now
            i = s.first_after(t_s + HORIZON_S[0])
            if i is None or s.plan_s[i] > t_s + HORIZON_S[1]:
                continue  # no stop planned in the window
            target = s.visits[i]
            done = self._done.get((v.tr_id, target.stop_id))
            if done == "ml" or (done == "fallback" and not ml_retry_open):
                continue
            yield v, target

    # ------------------------------------------------------------------ one round

    async def tick(self, T: datetime | None = None) -> int:
        T = self.clock.now() if T is None else T
        due = list(self.due(T))
        for i in range(0, len(due), self.batch_max):
            await self._predict(T, due[i:i + self.batch_max])
        return len(due)

    async def _predict(self, T: datetime, chunk: list[tuple[VehicleState, StopVisit]]) -> None:
        reqs = []
        for v, target in chunk:
            rows = self._schedule_rows.get(v.tr_id)
            if rows is None:
                rows = self._schedule_rows[v.tr_id] = schedule_rows(v.schedule)
            cur = v.derived.cur_dev_s if v.derived is not None else None
            req = predict_request(v.tr_id, T, target, 0.0 if cur is None else cur,
                                  self.fleet.telemetry(v.tr_id, T, span_s=self.telemetry_span_s), rows)
            reqs.append(req)
            self.last_requests[v.tr_id] = req

        source: Literal["ml", "fallback"] = "fallback"
        responses = None
        if self._wall() >= self._retry_at:
            started = time.perf_counter()
            try:
                responses = await self.client.predict_batch(reqs)
                self.ml_batch_ms.append((time.perf_counter() - started) * 1000)
                source = "ml"
                if self.ml_available is not True:
                    log.info("ML service available at %s", self.client.base_url)
                self.ml_available, self.last_error = True, None
            except MlUnavailable as e:
                self.stats.ml_errors += 1
                if self.ml_available is not False:
                    log.warning("ML service unavailable, using fallback forecasts: %s", e)
                self.ml_available, self.last_error = False, str(e)
                self._retry_at = self._wall() + self.retry_s
        if responses is None:
            responses = [fallback_response(r) for r in reqs]

        self.stats.batches += 1
        now = self._wall()
        for (v, target), req, resp in zip(chunk, reqs, responses):
            cur = v.derived.cur_dev_s if v.derived is not None else None
            p = Prediction(tr_id=v.tr_id, sample_id=req["sample_id"], T=T, target_pos=target.pos,
                           target_stop_id=target.stop_id, target_plan=target.plan, cur_dev_s=cur,
                           delay_pred_s=float(resp["delay_pred_sec"]), risk_score=float(resp["risk_score"]),
                           risk_level=resp.get("risk_level") or level_for(float(resp["risk_score"])),
                           confidence=float(resp.get("confidence", 0.0)), source=source, made_at=now, response=resp)
            self._record(p)

    def _record(self, p: Prediction) -> None:
        self._done[(p.tr_id, p.target_stop_id)] = p.source
        self.latest[p.tr_id] = p
        self.recent.append(p)
        if p.source == "ml":
            self.stats.predictions_ml += 1
        else:
            self.stats.predictions_fallback += 1
        self.stats.horizon_ok += p.horizon_ok
        for fn in self._listeners:
            try:
                fn(p)
            except Exception:  # noqa: BLE001 — a broken consumer must not stop predictions
                log.exception("prediction listener %r failed", fn)

    async def run(self, tick_s: float = 5.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 — keep predicting on the next tick
                log.exception("predictor tick failed")
            await asyncio.sleep(tick_s)

    # ------------------------------------------------------------------ introspection

    def snapshot(self) -> dict:
        total = self.stats.predictions_ml + self.stats.predictions_fallback
        ms = sorted(self.ml_batch_ms)
        return {
            "ml_url": self.client.base_url,
            "ml_available": self.ml_available,
            "last_error": self.last_error,
            "stats": asdict(self.stats),
            "vehicles_with_prediction": len(self.latest),
            "horizon_ok_share": round(self.stats.horizon_ok / total, 3) if total else None,
            "ml_batch_ms_p50": round(ms[len(ms) // 2], 1) if ms else None,
            "ml_batch_ms_p95": round(ms[min(len(ms) - 1, int(len(ms) * 0.95))], 1) if ms else None,
        }


def level_for(risk: float) -> str:
    return "red" if risk >= RISK_RED else "yellow" if risk >= RISK_YELLOW else "green"


def fallback_response(req: dict) -> dict:
    """What the backend answers itself when ML is down: the baseline (delay = current deviation), same shape as ML."""
    d = float(req["cur_dev_s"])
    z = (d - 120.0) / 60.0
    risk = 1 / (1 + math.exp(-z)) if z >= 0 else math.exp(z) / (1 + math.exp(z))
    level = level_for(risk)
    return {
        "sample_id": req["sample_id"], "delay_pred_sec": round(d, 1), "risk_score": round(risk, 3), "confidence": 0.05,
        "top_features": [{"name": "cur_dev_s", "value": d, "contribution": 1.0, "contribution_sec": d}],
        "reason_pattern": "accumulated_delay" if level != "green" else "on_track",
        "recommendation": "release_reserve" if level == "red" else "monitor",
        "recommendation_text": "", "model_version": "backend-fallback", "risk_level": level,
        "delay_interval_sec": [d - 150, d + 150], "data_status": "fallback",
        "causes": [{"code": "ml_unavailable", "text": "ML-сервис недоступен: прогноз по текущему отклонению",
                    "contribution_sec": d}],
    }
