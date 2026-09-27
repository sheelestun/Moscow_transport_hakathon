"""History writer. Unit tests use a fake pool; the Postgres tests run when ``TEST_DATABASE_URL`` is set, e.g.::

    docker run -d --name pg-test -e POSTGRES_USER=msk -e POSTGRES_PASSWORD=msk -e POSTGRES_DB=msk_transport \\
        -p 55432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql://msk:msk@127.0.0.1:55432/msk_transport pytest backend/tests/test_history.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import pytest

from app.alerts import Alert
from app.db import History
from app.ingest import Ping
from app.predict import Prediction
from app.state import Arrival, StopVisit, VehicleSchedule

DSN = os.environ.get("TEST_DATABASE_URL")
T = datetime(2026, 1, 6, 10, 5)
WALL = 1_790_496_000.0

SCHED = {7: VehicleSchedule(7, [StopVisit(pos=i, stop_id=1000 + i, plan=datetime(2026, 1, 6, 10, 2 * i), lat=55.75,
                                          lon=37.6, manual_fill=False, name=f"stop {i}", geom="", trip=0, idx_in_trip=i)
                                for i in range(10)])}


def ping(i: int = 0) -> Ping:
    return Ping(tr_id=7, unit_id=70, event_time=T.replace(second=i), lat=55.75, lon=37.6, speed_kmh=20.0,
                heading_deg=90.0, location_valid=True, is_hist=False, source="ndtp", received_at=WALL)


def prediction() -> Prediction:
    return Prediction(tr_id=7, sample_id="7_1767693900", T=T, target_pos=8, target_stop_id=1008,
                      target_plan=datetime(2026, 1, 6, 10, 16), cur_dev_s=180.0, delay_pred_s=240.0, risk_score=0.9,
                      risk_level="red", confidence=0.7, source="ml", made_at=WALL,
                      response={"model_version": "m1", "data_status": "live", "reason_pattern": "accumulated_delay",
                                "causes": [{"code": "x", "text": "задержка копится"}]})


def arrival(delay: float = 30.0) -> Arrival:
    return Arrival(pos=2, plan_s=1767693840, arrival_s=1767693840 + int(delay), delay_s=delay, dwell_s=12.0)


class FakePool:
    def __init__(self) -> None:
        self.fail = False
        self.calls: list[tuple[str, int]] = []

    async def execute(self, query, *args):
        if self.fail:
            raise ConnectionError("down")
        self.calls.append(("schema", 0))

    async def executemany(self, query, args):
        if self.fail:
            raise ConnectionError("down")
        self.calls.append(("alerts" if "alerts" in query else "arrivals", len(args)))

    async def copy_records_to_table(self, table, *, records, columns):
        if self.fail:
            raise ConnectionError("down")
        assert all(len(r) == len(columns) for r in records)
        self.calls.append((table, len(records)))

    async def close(self):
        pass


def history(pool: FakePool, **kw) -> History:
    async def connect():
        if pool.fail:
            raise ConnectionError("refused")
        return pool
    return History("fake://", SCHED, connect=connect, **kw)


def fill(h: History) -> None:
    for i in range(3):
        h.ping(ping(i))
    h.arrivals(7, [arrival()])
    h.prediction(prediction())
    a = Alert(alert_id="a-x-1", tr_id=7, prediction=prediction(), segment_from="stop 2", segment_to="stop 8",
              created_at=WALL)
    h.alert("alert.new", a)


def test_flush_writes_each_table_in_batches() -> None:
    pool = FakePool()
    h = history(pool, batch_max=2)
    fill(h)
    assert asyncio.run(h.flush()) == 6
    assert pool.calls == [("schema", 0), ("telemetry", 2), ("telemetry", 1), ("arrivals", 1), ("predictions", 1),
                          ("alerts", 1)]
    assert h.snapshot()["pending"] == {"telemetry": 0, "arrivals": 0, "predictions": 0, "alerts": 0}


def test_failed_flush_keeps_rows_and_they_are_written_after_reconnect() -> None:
    async def run() -> None:
        pool = FakePool()
        h = history(pool, retry_s=0.01, flush_s=0.01)
        fill(h)
        pool.fail = True
        task = asyncio.create_task(h.run())
        await asyncio.sleep(0.05)
        assert h.connected is False and h.snapshot()["pending"]["telemetry"] == 3 and h.stats.flush_errors >= 1
        pool.fail = False
        await asyncio.sleep(0.05)
        assert h.connected is True and h.stats.written == {"telemetry": 3, "arrivals": 1, "predictions": 1, "alerts": 1}
        task.cancel()

    asyncio.run(run())


def test_buffer_cap_drops_the_oldest_rows() -> None:
    h = history(FakePool(), buffer_max=2)
    for i in range(5):
        h.ping(ping(i))
    assert h.stats.dropped == 3
    assert [row[2].second for row in h._queues["telemetry"]] == [3, 4]


# ----------------------------------------------------------------------------- real Postgres

pg = pytest.mark.skipif(not DSN, reason="set TEST_DATABASE_URL to run against Postgres")


@pg
def test_round_trip_and_upserts_on_postgres() -> None:
    import asyncpg

    async def run() -> None:
        conn = await asyncpg.connect(DSN)
        await conn.execute("DROP TABLE IF EXISTS telemetry, arrivals, predictions, alerts")
        h = History(DSN, SCHED)
        fill(h)
        await h.flush()
        await h._drop_pool()
        await h.flush()                                               # schema applied again: idempotent
        h.arrivals(7, [arrival(delay=45.0)])                          # re-detected: same stop, new delay
        a = Alert(alert_id="a-x-1", tr_id=7, prediction=prediction(), segment_from="stop 2", segment_to="stop 8",
                  created_at=WALL, status="verified", delay_fact_s=210.0, closed_at=WALL + 600)
        h.alert("alert.verified", a)
        await h.flush()
        counts = {t: await conn.fetchval(f"SELECT count(*) FROM {t}") for t in ("telemetry", "arrivals", "predictions", "alerts")}
        assert counts == {"telemetry": 3, "arrivals": 1, "predictions": 1, "alerts": 1}
        assert await conn.fetchval("SELECT delay_s FROM arrivals") == 45.0
        row = await conn.fetchrow("SELECT status, delay_fact_s, hit, closed_at IS NOT NULL AS closed FROM alerts")
        assert (row["status"], row["delay_fact_s"], row["hit"], row["closed"]) == ("verified", 210.0, True, True)
        resp = await conn.fetchval("SELECT response->'causes'->0->>'text' FROM predictions")
        assert resp == "задержка копится"
        assert await conn.fetchval("SELECT event_time FROM telemetry ORDER BY event_time LIMIT 1") == T
        await h.close()
        await conn.close()

    asyncio.run(run())
