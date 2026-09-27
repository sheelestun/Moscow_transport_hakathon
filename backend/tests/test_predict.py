"""Predictor: target selection in (T+10, T+15] min, one forecast per target, payloads, ML-down fallback.

The ML service is faked with ``httpx.MockTransport``. Against the real service (locally trained model),
payloads built from vehicle state reproduce the batch submission within 0.1 s on 104/151 validate points;
the rest differ because the service's online feature path isn't byte-identical to the batch pipeline —
its own reference payloads (``src/csv_replayer.py``) show the same kind of gap.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta

import httpx
import pytest

from app.clock import DatasetClock
from app.ingest import Ping
from app.predict import MlClient, Predictor
from app.predict.predictor import fallback_response
from app.state import Fleet, StopVisit, VehicleSchedule

DAY = datetime(2026, 1, 6)


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return DAY + timedelta(hours=hh, minutes=mm, seconds=ss)


def schedule(tr: int = 7) -> VehicleSchedule:
    """Stops every 2 minutes from 10:00 to 10:30, then a 20-minute gap, then 10:50."""
    plans = [at(10, m) for m in range(0, 31, 2)] + [at(10, 50)]
    return VehicleSchedule(tr, [StopVisit(pos=i, stop_id=1000 + i, plan=p, lat=55.75, lon=37.6 + i * 0.005,
                                          manual_fill=False, name=f"stop {i}", geom=f"POINT ({37.6 + i * 0.005} 55.75)",
                                          trip=0, idx_in_trip=i) for i, p in enumerate(plans)])


def ping(t: datetime, tr: int = 7) -> Ping:
    return Ping(tr_id=tr, unit_id=70, event_time=t, lat=55.75, lon=37.6, speed_kmh=20.0, heading_deg=0.0,
                location_valid=True, is_hist=False, source="replay")


class FakeMl:
    def __init__(self) -> None:
        self.down = False
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("refused")
        body = json.loads(request.content)["requests"]
        self.requests.extend(body)
        return httpx.Response(200, json={"responses": [
            {"sample_id": r["sample_id"], "delay_pred_sec": r["cur_dev_s"] + 100, "risk_score": 0.8, "confidence": 0.7,
             "risk_level": "red", "data_status": "live", "model_version": "fake"} for r in body]})


def make(pings: list[Ping], *, wall: list[float] | None = None) -> tuple[Predictor, FakeMl]:
    fleet = Fleet({7: schedule()}, DatasetClock.starting_at())
    for p in pings:
        fleet.on_ping(p)
    fake = FakeMl()
    client = MlClient("http://ml", transport=httpx.MockTransport(fake.handler))
    wall = wall or [1000.0]
    return Predictor(fleet, fleet.clock, client, retry_s=30, wall=lambda: wall[0]), fake


def run(coro):
    return asyncio.run(coro)


def test_target_is_first_stop_in_the_10_to_15_minute_window() -> None:
    pred, fake = make([ping(at(10, 4)), ping(at(10, 5))])
    assert run(pred.tick(at(10, 5))) == 1
    p = pred.latest[7]
    assert (p.target_stop_id, p.target_plan) == (1008, at(10, 16))      # (10:15, 10:20] → 10:16
    assert p.horizon_ok and p.lead_s == 660 and p.source == "ml" and p.delay_pred_s == 100.0
    assert fake.requests[0]["sample_id"] == f"7_{int((at(10, 5) - datetime(1970, 1, 1)).total_seconds())}"


def test_one_forecast_per_target_then_the_next_target() -> None:
    pred, fake = make([ping(at(10, 4)), ping(at(10, 5)), ping(at(10, 6))])
    run(pred.tick(at(10, 5)))
    assert run(pred.tick(at(10, 5, 30))) == 0                            # same target (10:16): already done
    assert run(pred.tick(at(10, 6, 30))) == 1                            # window moved: target 10:18
    assert [r["target_stop_id"] for r in fake.requests] == [1008, 1009]


def test_no_forecast_without_a_stop_in_the_window_or_without_recent_telemetry() -> None:
    pred, _ = make([ping(at(10, 20))])
    assert run(pred.tick(at(10, 21))) == 0                               # (10:31, 10:36]: gap until 10:50
    pred, _ = make([ping(at(9, 40))])
    assert run(pred.tick(at(10, 0))) == 0                                # last ping 20 min old: not in service


def test_payload_contents() -> None:
    pred, fake = make([ping(at(10, 4)), ping(at(10, 5)), ping(at(10, 6))])
    run(pred.tick(at(10, 5)))
    r = fake.requests[0]
    assert r["T"] == "2026-01-06T10:05:00.000000" and r["target_time_begin"] == "2026-01-06T10:16:00.000000"
    assert [t["event_time"] for t in r["telemetry"]] == ["2026-01-06T10:04:00.000000", "2026-01-06T10:05:00.000000"]
    assert len(r["schedule"]) == 17 and r["schedule"][0]["tt_action_item_id"] == 1000   # the whole day plan
    assert r["cur_dev_s"] == 0.0                                         # no arrival detected yet


def test_ml_down_fallback_backoff_and_recovery() -> None:
    wall = [1000.0]
    pred, fake = make([ping(at(10, 4)), ping(at(10, 5))], wall=wall)
    fake.down = True
    run(pred.tick(at(10, 5)))
    p = pred.latest[7]
    assert (p.source, p.response["data_status"], pred.ml_available) == ("fallback", "fallback", False)
    assert "ConnectError" in pred.last_error
    wall[0] += 10
    fake.down = False
    assert run(pred.tick(at(10, 5, 10))) == 0                            # within retry_s: no retry yet
    wall[0] += 25
    assert run(pred.tick(at(10, 5, 20))) == 1                            # retry: same target, now from ML
    assert pred.latest[7].source == "ml" and pred.ml_available is True
    assert (pred.stats.predictions_fallback, pred.stats.predictions_ml, pred.stats.ml_errors) == (1, 1, 1)


def test_listeners_and_snapshot() -> None:
    pred, _ = make([ping(at(10, 4)), ping(at(10, 5))])
    got = []
    pred.on_prediction(got.append)
    run(pred.tick(at(10, 5)))
    assert [p.target_stop_id for p in got] == [1008]
    snap = pred.snapshot()
    assert snap["horizon_ok_share"] == 1.0 and snap["ml_available"] is True and snap["ml_batch_ms_p50"] is not None


@pytest.mark.parametrize("dev,level", [(0.0, "green"), (120.0, "yellow"), (200.0, "red")])
def test_fallback_uses_the_contract_sigmoid(dev: float, level: str) -> None:
    r = fallback_response({"sample_id": "x", "cur_dev_s": dev})
    assert r["delay_pred_sec"] == dev and r["risk_level"] == level and r["model_version"] == "backend-fallback"
