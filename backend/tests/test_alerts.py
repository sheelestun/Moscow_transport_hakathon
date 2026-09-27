"""Alert engine: raising (threshold, horizon, one per vehicle), verification against arrivals, resolution."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.alerts import AlertEngine
from app.api.alerts import alert_payload
from app.clock import DatasetClock
from app.ingest import Ping
from app.predict import Prediction
from app.state import Arrival, Fleet, StopVisit, VehicleSchedule
from app.state.geo import epoch_s

DAY = datetime(2026, 1, 6)
WALL = 1_790_496_000.0


def at(hh: int, mm: int, ss: int = 0) -> datetime:
    return DAY + timedelta(hours=hh, minutes=mm, seconds=ss)


def fleet_with_vehicle(mf_target: bool = False) -> Fleet:
    """Vehicle 7 with stops every 2 min from 10:00, last detected at stop 2 (+180 s), seen at 10:05."""
    visits = [StopVisit(pos=i, stop_id=1000 + i, plan=at(10, 2 * i), lat=55.75, lon=37.6 + i * 0.005,
                        manual_fill=(mf_target and i == 8), name=f"stop {i}", geom="", trip=0, idx_in_trip=i)
              for i in range(12)]
    fleet = Fleet({7: VehicleSchedule(7, visits)}, DatasetClock.starting_at(at(10, 5), wall=WALL))
    fleet.on_ping(Ping(tr_id=7, unit_id=70, event_time=at(10, 5), lat=55.75, lon=37.6, speed_kmh=20.0,
                       heading_deg=0.0, location_valid=True, is_hist=False, source="replay"))
    v = fleet.vehicles[7]
    v.arrivals[2] = Arrival(pos=2, plan_s=int(epoch_s(at(10, 4))), arrival_s=int(epoch_s(at(10, 7))), delay_s=180.0,
                            dwell_s=20.0)
    v.derived = Fleet._derive(v, at(10, 7, 30))
    return fleet


def prediction(risk: float = 0.85, *, T: datetime = at(10, 5), target_pos: int = 8, delay: float = 240.0) -> Prediction:
    target_plan = at(10, 2 * target_pos)
    return Prediction(tr_id=7, sample_id="7_x", T=T, target_pos=target_pos, target_stop_id=1000 + target_pos,
                      target_plan=target_plan, cur_dev_s=180.0, delay_pred_s=delay, risk_score=risk, risk_level="red",
                      confidence=0.7, source="ml", made_at=WALL,
                      response={"reason_pattern": "accumulated_delay", "recommendation": "release_reserve",
                                "causes": [{"code": "accumulated_delay", "text": "…", "contribution_sec": 180}],
                                "top_features": [{"name": "cur_dev_s", "contribution": 0.6}], "model_version": "m1",
                                "delay_interval_sec": [150, 330]})


def engine(fleet: Fleet) -> tuple[AlertEngine, list]:
    e = AlertEngine(fleet, fleet.clock, wall=lambda: WALL)
    events: list = []
    e.on_event(lambda kind, a: events.append((kind, a.alert_id)))
    return e, events


def test_raise_only_red_forecasts_inside_the_horizon_once_per_vehicle() -> None:
    e, events = engine(fleet_with_vehicle())
    e.on_prediction(prediction(risk=0.5))                              # not red
    e.on_prediction(prediction(T=at(10, 9)))                           # target 10:16 only 7 min ahead
    assert events == []
    e.on_prediction(prediction())                                      # 10:05 → 10:16: 11 min ahead, red
    e.on_prediction(prediction(target_pos=9, T=at(10, 7)))             # next red forecast, same vehicle
    assert events == [("alert.new", "a-1")]
    assert (e.stats.raised, e.stats.duplicates_suppressed) == (1, 1)
    a = e.active[7]
    assert (a.segment_from, a.segment_to) == ("stop 2", "stop 8")


def test_verified_when_the_target_arrival_is_detected() -> None:
    fleet = fleet_with_vehicle()
    e, events = engine(fleet)
    e.on_prediction(prediction(delay=240.0))
    e.check(at(10, 15))
    assert 7 in e.active                                               # target not reached yet
    fleet.vehicles[7].arrivals[8] = Arrival(pos=8, plan_s=int(epoch_s(at(10, 16))),
                                            arrival_s=int(epoch_s(at(10, 19, 30))), delay_s=210.0, dwell_s=15.0)
    e.check(at(10, 20))
    assert events[-1] == ("alert.verified", "a-1") and 7 not in e.active
    a = e.closed[-1]
    assert (a.status, a.delay_fact_s, a.hit) == ("verified", 210.0, True)
    snap = e.snapshot()
    assert (snap["precision"], snap["mae_verified_s"]) == (1.0, 30.0)


def test_false_alarm_is_counted() -> None:
    fleet = fleet_with_vehicle()
    e, _ = engine(fleet)
    e.on_prediction(prediction())
    fleet.vehicles[7].arrivals[8] = Arrival(pos=8, plan_s=0, arrival_s=0, delay_s=40.0, dwell_s=0.0)
    e.check(at(10, 17))
    assert e.snapshot()["precision"] == 0.0 and e.closed[-1].hit is False


def test_resolved_without_an_arrival_after_the_window() -> None:
    for mf, reason in [(False, "no arrival detected at the target stop"),
                       (True, "manual-fill stop: no GPS arrival to compare")]:
        e, events = engine(fleet_with_vehicle(mf_target=mf))
        e.on_prediction(prediction())
        e.check(at(10, 16) + timedelta(seconds=e.verify_grace_s - 1))
        assert 7 in e.active
        e.check(at(10, 16) + timedelta(seconds=e.verify_grace_s + 1))
        assert events[-1] == ("alert.resolved", "a-1") and e.closed[-1].resolution == reason
        e.on_prediction(prediction(target_pos=11, T=at(10, 10)))       # closed: the vehicle can alert again
        assert e.stats.raised == 2


def test_payload_matches_the_dashboard_and_uses_wall_time() -> None:
    fleet = fleet_with_vehicle()
    e, _ = engine(fleet)
    e.on_prediction(prediction(delay=240.0))
    out = alert_payload(fleet.clock, e.active[7], "alert.new")
    assert out["type"] == "alert.new" and out["vehicle_id"] == out["route_id"] == "7"
    assert (out["target_stop_id"], out["target_stop_name"], out["delay_pred_sec"]) == ("1008", "stop 8", 240)
    assert out["eta_incident"] == "2026-09-27T08:15:00Z"             # 10:16 + 4 min dataset = wall 08:15 UTC
    assert out["segment"] == {"from": "stop 2", "to": "stop 8"} and out["recommendation"] == "release_reserve"
    assert out["created_at"] == "2026-09-27T08:00:00Z"
