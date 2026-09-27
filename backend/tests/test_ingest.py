"""Dataset clock, traffic.csv loading, the ingest pipeline (incl. NDTP/replay arbitration) and replay.

``fixtures/dataset/validate/traffic.csv`` is synthetic: the real file format and real unit/vehicle IDs
(so recorded emulator traffic maps onto it), with made-up coordinates and times.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from app.clock import DatasetClock
from app.ingest import Ingest, ReplaySource, load_traffic
from app.ndtp import NavCell, NdtpFix

TRAFFIC = Path(__file__).parent / "fixtures" / "dataset" / "validate" / "traffic.csv"
WALL = 1_790_496_000.0  # 2026-09-27 08:00:00 UTC = 11:00 MSK


def fix(unit: int, ts: float, *, received_at: float | None = None, valid: bool = True) -> NdtpFix:
    nav = NavCell(number=0, timestamp=int(ts), latitude=55.75, longitude=37.61, location_valid=valid, flags=0,
                  bat_voltage_mv=0, speed_avg_kmh=30, speed_max_kmh=30, course_deg=90, track_m=0, altitude_m=0,
                  nsat=0, pdop=0)
    return NdtpFix(unit_id=unit, nav=nav, received_at=ts if received_at is None else received_at, conn_id=1,
                   request_id=2)


# ----------------------------------------------------------------------------- clock


def test_clock_defaults_to_moscow_time_of_day_on_dataset_day() -> None:
    c = DatasetClock.starting_at(wall=WALL)
    assert c.anchor_dataset == datetime(2026, 1, 6, 11, 0)
    assert c.to_dataset(WALL + 90) == datetime(2026, 1, 6, 11, 1, 30)


def test_clock_explicit_start_speed_and_inverse() -> None:
    c = DatasetClock.starting_at(datetime(2026, 1, 6, 7, 30), speed=10, wall=WALL)
    assert c.to_dataset(WALL + 60) == datetime(2026, 1, 6, 7, 40)
    assert c.to_wall(datetime(2026, 1, 6, 7, 40)) == pytest.approx(WALL + 60)
    with pytest.raises(ValueError):
        DatasetClock.starting_at(speed=0, wall=WALL)


# ----------------------------------------------------------------------------- traffic.csv


def test_load_traffic() -> None:
    t = load_traffic(TRAFFIC)
    assert t.unit_to_tr == {664030: 115106, 794446: 116057}
    times = [p.event_time for p in t.pings]
    assert times == sorted(times) and len(times) == 7
    invalid = next(p for p in t.pings if not p.location_valid)
    assert (invalid.lat, invalid.lon, invalid.speed_kmh) == (None, None, None)
    hist = [p for p in t.pings if p.is_hist]
    assert len(hist) == 1 and hist[0].tr_id == 116057
    assert all(p.source == "replay" for p in t.pings)


# ----------------------------------------------------------------------------- pipeline


def make_ingest(now: list[float]) -> tuple[Ingest, list]:
    clock = DatasetClock.starting_at(datetime(2026, 1, 6, 8, 0), wall=WALL)
    ing = Ingest(clock, load_traffic(TRAFFIC).unit_to_tr, ndtp_fresh_s=60, max_skew_s=300, wall=lambda: now[0])
    out: list = []
    ing.subscribe(out.append)
    return ing, out


def test_ndtp_fix_becomes_ping_on_dataset_timeline() -> None:
    ing, out = make_ingest([WALL])
    ping = ing.accept_ndtp(fix(664030, WALL + 125))
    assert (ping.tr_id, ping.source, ping.event_time) == (115106, "ndtp", datetime(2026, 1, 6, 8, 2, 5))
    assert (ping.lat, ping.lon, ping.speed_kmh) == (55.75, 37.61, 30.0)
    assert out == [ping] and ing.latest[115106] is ping


def test_invalid_fix_has_no_position() -> None:
    ing, _ = make_ingest([WALL])
    ping = ing.accept_ndtp(fix(664030, WALL, valid=False))
    assert (ping.lat, ping.lon, ping.speed_kmh, ping.location_valid) == (None, None, None, False)


def test_unknown_unit_is_counted_not_emitted() -> None:
    ing, out = make_ingest([WALL])
    assert ing.accept_ndtp(fix(1166336, WALL)) is None
    assert out == [] and ing.unknown_units == {1166336: 1} and ing.stats.unknown_unit_fixes == 1


def test_terminal_clock_skew_falls_back_to_receive_time() -> None:
    ing, _ = make_ingest([WALL])
    ping = ing.accept_ndtp(fix(664030, WALL - 86_400, received_at=WALL + 60))
    assert ping.event_time == datetime(2026, 1, 6, 8, 1)
    assert ing.stats.clock_skew_fallbacks == 1


def test_live_ndtp_suppresses_replay_for_that_vehicle_only() -> None:
    now = [WALL]
    ing, out = make_ingest(now)
    pings = {p.tr_id: p for p in load_traffic(TRAFFIC).pings}
    ing.accept_ndtp(fix(664030, WALL))                       # 115106 goes live
    now[0] = WALL + 30
    assert not ing.accept_replay(pings[115106])              # live: replay dropped
    assert ing.accept_replay(pings[116057])                  # other vehicle unaffected
    assert ing.live_vehicles() == [115106]
    now[0] = WALL + 61                                       # NDTP went quiet: replay takes over
    assert ing.accept_replay(pings[115106])
    assert ing.live_vehicles() == []
    assert (ing.stats.replay_suppressed, ing.stats.replay_pings, ing.stats.ndtp_pings) == (1, 2, 1)
    assert [p.source for p in out] == ["ndtp", "replay", "replay"]


def test_latest_keeps_newest_event_time() -> None:
    ing, _ = make_ingest([WALL])
    new = ing.accept_ndtp(fix(664030, WALL + 600))           # 08:10
    old = next(p for p in load_traffic(TRAFFIC).pings if p.tr_id == 115106)
    ing._ndtp_seen.clear()                                   # let the old replayed row through arbitration
    ing.accept_replay(old)                                   # 08:00 arrives late
    assert ing.latest[115106] is new


def test_broken_subscriber_does_not_stop_ingest() -> None:
    ing, out = make_ingest([WALL])
    ing._subscribers.insert(0, lambda p: 1 / 0)
    ing.accept_ndtp(fix(664030, WALL))
    assert len(out) == 1 and ing.stats.subscriber_errors == 1


# ----------------------------------------------------------------------------- replay


def test_replay_backfills_window_then_follows_clock_to_the_end() -> None:
    async def run() -> None:
        pings = load_traffic(TRAFFIC).pings
        got: list = []
        clock = DatasetClock.starting_at(datetime(2026, 1, 6, 8, 2, 30), speed=120)  # 2 dataset min / wall s
        src = ReplaySource(pings, clock, lambda p: got.append(p) or True, backfill_s=75, tick_s=0.01)
        task = asyncio.create_task(src.run())
        await asyncio.sleep(0.005)
        first = [p.event_time.strftime("%H:%M:%S") for p in got]
        assert first == ["08:01:30", "08:02:00"]             # backfill window [~08:01:15, ~08:02:30] only
        await asyncio.wait_for(task, 5)
        assert [p.event_time.strftime("%H:%M:%S") for p in got] == ["08:01:30", "08:02:00", "08:03:30", "08:04:00"]
        assert src.snapshot()["status"] == "finished"

    asyncio.run(run())
