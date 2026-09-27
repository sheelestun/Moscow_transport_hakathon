"""Schedule index, arrival detection and derived vehicle features.

Parity with ``ml/src/features/tabular.gps_history`` was checked on the real validate data (1,560
vehicle×moment checks, 23,664 arrivals, 0 mismatches); these tests pin the behaviour on synthetic tracks
whose expected values can be computed by hand.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.clock import DatasetClock
from app.ingest import Ping
from app.state import Fleet, StopVisit, VehicleSchedule, load_schedule

SCHEDULE = Path(__file__).parent / "fixtures" / "dataset" / "validate" / "schedule_plan.csv"
LAT = 55.75
M_PER_DEG_LON = 6371000.0 * math.pi / 180 * math.cos(LAT * math.pi / 180)
DAY = datetime(2026, 1, 6)


def at(hh: int, mm: int, ss: float = 0) -> datetime:
    return DAY + timedelta(hours=hh, minutes=mm, seconds=ss)


def lon(x_m: float) -> float:
    return 37.6 + x_m / M_PER_DEG_LON


def line_schedule(*plans: datetime, stops_m=(0, 500, 1000), mf=(False, False, False), gap_trip=False) -> VehicleSchedule:
    visits = [StopVisit(pos=i, stop_id=100 + i, plan=p, lat=LAT, lon=lon(x), manual_fill=m, name=f"stop {i}",
                        geom=f"POINT ({lon(x)} {LAT})", trip=1 if (gap_trip and i == 2) else 0, idx_in_trip=i)
              for i, (p, x, m) in enumerate(zip(plans, stops_m, mf))]
    return VehicleSchedule(7, visits)


def drive(t0: datetime, legs: list[tuple[float, float, float]], every_s: float = 5.0) -> list[Ping]:
    """Pings for a vehicle that holds position / moves: legs of (x_from_m, x_to_m, duration_s)."""
    out, t = [], t0
    for x0, x1, dur in legs:
        n = max(1, round(dur / every_s))
        for k in range(n):
            x = x0 + (x1 - x0) * k / n
            out.append(Ping(tr_id=7, unit_id=70, event_time=t, lat=LAT, lon=lon(x), speed_kmh=30.0, heading_deg=90.0,
                            location_valid=True, is_hist=False, source="replay"))
            t += timedelta(seconds=every_s)
    return out


# bus: at stop 0 from 10:00:30 for 20 s, drives 500 m in 150 s (12 km/h), parks at stop 1 from 10:03:20 for 40 s,
# then drives on past stop 2. Hand-derived expectations (pings every 5 s):
#   stop 0: arrival 10:00:30 (+30 s); within 60 m from 10:00:30 to 10:01:05 (leaving at 3.3 m/s) → dwell 35 s
#   stop 1: arrival 10:03:20 (+80 s); within 60 m from 10:03:05 (approach) to 10:04:15 (departure) → dwell 70 s
TRACK = drive(at(10, 0, 30), [(0, 0, 20), (0, 500, 150), (500, 500, 40), (500, 1100, 180)])
PLANS = (at(10, 0), at(10, 2), at(10, 4))


def test_load_schedule_orders_visits_and_splits_trips() -> None:
    s = load_schedule(SCHEDULE)[115106]
    assert [v.stop_id for v in s.visits] == [900000001, 900000002, 900000003, 900000004, 900000005]
    assert [v.trip for v in s.visits] == [0, 0, 0, 0, 1]          # 08:05 → 08:20 is a 15-min gap: terminal
    assert [v.idx_in_trip for v in s.visits] == [0, 1, 2, 3, 0]
    assert s.visits[3].manual_fill and not s.visits[0].manual_fill
    assert (s.visits[0].lat, s.visits[0].lon, s.visits[0].name) == (55.7, 37.6, "Остановка А")
    assert s.cum_dist_m[1] == pytest.approx(125.4, abs=0.5)        # 0.002° of longitude at 55.7° N
    assert s.cum_dist_m[4] == s.cum_dist_m[3]                      # no distance is added across a terminal


def test_arrivals_delay_and_dwell() -> None:
    fleet = Fleet({7: line_schedule(*PLANS)}, DatasetClock.starting_at())
    for p in TRACK:
        fleet.on_ping(p)
    fleet.update(at(10, 10))
    arr = sorted(fleet.vehicles[7].arrivals.values(), key=lambda a: a.pos)
    assert [a.pos for a in arr] == [0, 1, 2]
    assert (arr[0].delay_s, arr[0].dwell_s) == (30, 35)
    assert (arr[1].delay_s, arr[1].dwell_s) == (80, 70)


def test_derived_features() -> None:
    fleet = Fleet({7: line_schedule(*PLANS)}, DatasetClock.starting_at())
    for p in TRACK:
        fleet.on_ping(p)
    fleet.update(at(10, 4, 10))                                    # left stop 1, not yet at stop 2
    d = fleet.vehicles[7].derived
    assert d.last_arrival.pos == 1 and d.cur_dev_s == 80
    assert d.dwell_s == 65                                         # only pings up to T=10:04:10 count so far
    a0, a1 = sorted(fleet.vehicles[7].arrivals.values(), key=lambda a: a.pos)
    assert d.segment_speed_kmh == pytest.approx(500 / (a1.arrival_s - a0.arrival_s) * 3.6, abs=0.1)
    assert d.next_pos == 2


def test_nothing_is_detected_before_the_vehicle_leaves_the_stop() -> None:
    fleet = Fleet({7: line_schedule(*PLANS)}, DatasetClock.starting_at())
    for p in TRACK:
        fleet.on_ping(p)
    fleet.update(at(10, 0, 45))                                    # still standing at stop 0
    assert fleet.vehicles[7].arrivals == {} and fleet.vehicles[7].derived.cur_dev_s is None


def test_manual_fill_stops_are_skipped_and_segment_speed_stays_within_a_trip() -> None:
    fleet = Fleet({7: line_schedule(*PLANS, mf=(False, True, False), gap_trip=True)}, DatasetClock.starting_at())
    for p in TRACK:
        fleet.on_ping(p)
    fleet.update(at(10, 10))
    assert sorted(fleet.vehicles[7].arrivals) == [0, 2]
    assert fleet.vehicles[7].derived.segment_speed_kmh is None     # stop 2 starts another trip


def test_out_of_order_and_duplicate_pings() -> None:
    fleet = Fleet({7: line_schedule(*PLANS)}, DatasetClock.starting_at())
    for p in reversed(TRACK):
        fleet.on_ping(p)
    fleet.on_ping(TRACK[10])
    assert [p.event_time for p in fleet.vehicles[7].pings] == sorted(p.event_time for p in fleet.vehicles[7].pings)
    fleet.update(at(10, 10))
    assert sorted(fleet.vehicles[7].arrivals) == [0, 1, 2]


def test_only_dirty_vehicles_are_recomputed_and_listeners_get_new_arrivals() -> None:
    fleet = Fleet({7: line_schedule(*PLANS)}, DatasetClock.starting_at())
    got: list = []
    fleet.on_arrivals(lambda tr, arrs: got.append((tr, sorted(a.pos for a in arrs))))
    for p in TRACK:
        fleet.on_ping(p)
    assert fleet.update(at(10, 10)) == 1
    assert fleet.update(at(10, 11)) == 0                           # no new pings: nothing to do
    assert got == [(7, [0, 1, 2])]


def test_buffer_is_trimmed_and_telemetry_respects_T() -> None:
    fleet = Fleet({7: line_schedule(*PLANS)}, DatasetClock.starting_at(), buffer_s=60)
    for p in TRACK:
        fleet.on_ping(p)
    pings = fleet.vehicles[7].pings
    assert (pings[-1].event_time - pings[0].event_time).total_seconds() <= 60
    T = at(10, 7)
    window = Fleet({7: line_schedule(*PLANS)}, DatasetClock.starting_at())
    for p in TRACK:
        window.on_ping(p)
    seen = window.telemetry(7, T, span_s=120)
    assert seen and all(T - timedelta(seconds=120) < p.event_time <= T for p in seen)
