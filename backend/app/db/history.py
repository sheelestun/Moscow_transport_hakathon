"""Write-behind history store.

The live pipeline only enqueues rows (sync, O(1)); a background task flushes them to Postgres in batches
every ``flush_s``. The database can therefore never slow down ingest, arrival detection or forecasts.

If Postgres is unreachable the service keeps running: rows stay buffered (up to ``buffer_max`` per table,
then the oldest are dropped and counted), the writer reconnects every ``retry_s``, and ``/health`` reports
``degraded`` meanwhile (criterion 5).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..alerts import Alert
from ..ingest import Ping
from ..predict import Prediction
from ..state import Arrival, VehicleSchedule

log = logging.getLogger(__name__)

SCHEMA = Path(__file__).with_name("schema.sql")
_EPOCH = datetime(1970, 1, 1)

TELEMETRY_COLS = ["tr_id", "unit_id", "event_time", "lat", "lon", "speed_kmh", "heading_deg", "location_valid",
                  "is_hist", "source", "received_at"]
PREDICTION_COLS = ["sample_id", "tr_id", "t", "target_stop_id", "target_plan", "lead_s", "horizon_ok", "cur_dev_s",
                   "delay_pred_s", "risk_score", "risk_level", "confidence", "source", "model_version", "data_status",
                   "reason_pattern", "response", "made_at"]
UPSERT_ARRIVAL = """
    INSERT INTO arrivals (tr_id, stop_id, stop_name, plan, arrival, delay_s, dwell_s, trip, detected_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (tr_id, stop_id) DO UPDATE SET arrival = EXCLUDED.arrival, delay_s = EXCLUDED.delay_s,
        dwell_s = EXCLUDED.dwell_s, detected_at = EXCLUDED.detected_at"""
UPSERT_ALERT = """
    INSERT INTO alerts (alert_id, tr_id, sample_id, target_stop_id, target_plan, segment_from, segment_to,
                        delay_pred_s, risk_score, status, delay_fact_s, hit, resolution, created_at, closed_at)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
    ON CONFLICT (alert_id) DO UPDATE SET status = EXCLUDED.status, delay_fact_s = EXCLUDED.delay_fact_s,
        hit = EXCLUDED.hit, resolution = EXCLUDED.resolution, closed_at = EXCLUDED.closed_at"""


class Pool(Protocol):
    """The part of ``asyncpg.Pool`` the writer uses (faked in tests)."""

    async def execute(self, query: str, *args: Any) -> str: ...
    async def executemany(self, query: str, args: list[tuple]) -> None: ...
    async def copy_records_to_table(self, table: str, *, records: list[tuple], columns: list[str]) -> str: ...
    async def close(self) -> None: ...


@dataclass(slots=True)
class HistoryStats:
    written: dict[str, int] = field(default_factory=lambda: {t: 0 for t in ("telemetry", "arrivals", "predictions", "alerts")})
    dropped: int = 0          # rows discarded because the buffer was full while the database was down
    flush_errors: int = 0


def _wall(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def _dataset(epoch_s: float) -> datetime:
    return _EPOCH + timedelta(seconds=epoch_s)


class History:
    def __init__(self, dsn: str, schedules: dict[int, VehicleSchedule], *, flush_s: float = 1.0,
                 batch_max: int = 5000, buffer_max: int = 200_000, retry_s: float = 5.0, connect=None) -> None:
        self.dsn = dsn
        self.schedules = schedules
        self.flush_s = flush_s
        self.batch_max = batch_max
        self.buffer_max = buffer_max
        self.retry_s = retry_s
        self.stats = HistoryStats()
        self.connected: bool | None = None   # None: not tried yet
        self.last_error: str | None = None
        self.pool: Pool | None = None
        self._queues: dict[str, deque] = {t: deque() for t in self.stats.written}
        self._connect = connect or self._asyncpg_connect

    # ------------------------------------------------------------------ enqueue (called by the pipeline)

    def _put(self, table: str, row: tuple) -> None:
        q = self._queues[table]
        if len(q) >= self.buffer_max:
            q.popleft()
            self.stats.dropped += 1
        q.append(row)

    def ping(self, p: Ping) -> None:
        self._put("telemetry", (p.tr_id, p.unit_id, p.event_time, p.lat, p.lon, p.speed_kmh, p.heading_deg,
                                p.location_valid, p.is_hist, p.source, _wall(p.received_at)))

    def arrivals(self, tr_id: int, arrivals: list[Arrival]) -> None:
        s = self.schedules[tr_id]
        now = datetime.now(UTC)
        for a in arrivals:
            v = s.visits[a.pos]
            self._put("arrivals", (tr_id, v.stop_id, v.name, v.plan, _dataset(a.arrival_s), a.delay_s, a.dwell_s,
                                   v.trip, now))

    def prediction(self, p: Prediction) -> None:
        r = p.response
        self._put("predictions", (p.sample_id, p.tr_id, p.T, p.target_stop_id, p.target_plan, p.lead_s, p.horizon_ok,
                                  p.cur_dev_s, p.delay_pred_s, p.risk_score, p.risk_level, p.confidence, p.source,
                                  r.get("model_version"), r.get("data_status"), r.get("reason_pattern"),
                                  json.dumps(r, ensure_ascii=False), _wall(p.made_at)))

    def alert(self, _kind: str, a: Alert) -> None:
        p = a.prediction
        self._put("alerts", (a.alert_id, a.tr_id, p.sample_id, p.target_stop_id, p.target_plan, a.segment_from,
                             a.segment_to, p.delay_pred_s, p.risk_score, a.status, a.delay_fact_s, a.hit, a.resolution,
                             _wall(a.created_at), None if a.closed_at is None else _wall(a.closed_at)))

    # ------------------------------------------------------------------ flushing

    async def _asyncpg_connect(self) -> Pool:
        import asyncpg  # imported lazily: history is optional
        return await asyncpg.create_pool(self.dsn, min_size=1, max_size=4, command_timeout=30)

    async def _ensure_pool(self) -> Pool:
        if self.pool is None:
            pool = await self._connect()
            await pool.execute(SCHEMA.read_text())
            self.pool = pool
            if self.connected is not True:
                log.info("history: connected to Postgres, schema ready")
            self.connected, self.last_error = True, None
        return self.pool

    async def flush(self) -> int:
        """Write everything buffered (in batches). On failure the batch goes back to the buffer and it raises."""
        pool = await self._ensure_pool()
        n = 0
        for table, q in self._queues.items():
            while q:
                batch = [q.popleft() for _ in range(min(len(q), self.batch_max))]
                try:
                    if table == "telemetry":
                        await pool.copy_records_to_table("telemetry", records=batch, columns=TELEMETRY_COLS)
                    elif table == "predictions":
                        await pool.copy_records_to_table("predictions", records=batch, columns=PREDICTION_COLS)
                    elif table == "arrivals":
                        await pool.executemany(UPSERT_ARRIVAL, batch)
                    else:
                        await pool.executemany(UPSERT_ALERT, batch)
                except BaseException:
                    q.extendleft(reversed(batch))
                    raise
                self.stats.written[table] += len(batch)
                n += len(batch)
        return n

    async def run(self) -> None:
        while True:
            try:
                await self.flush()
                await asyncio.sleep(self.flush_s)
            except Exception as e:  # noqa: BLE001 — database trouble must never take the service down
                self.stats.flush_errors += 1
                if self.connected is not False:
                    log.warning("history: Postgres unavailable, buffering: %s: %s", type(e).__name__, e)
                self.connected, self.last_error = False, f"{type(e).__name__}: {e}"
                await self._drop_pool()
                await asyncio.sleep(self.retry_s)

    async def _drop_pool(self) -> None:
        pool, self.pool = self.pool, None
        if pool is not None:
            try:
                await pool.close()
            except Exception:  # noqa: BLE001
                pass

    async def close(self) -> None:
        """Last flush on shutdown, then disconnect."""
        try:
            if self.connected:
                await asyncio.wait_for(self.flush(), timeout=5)
        except Exception as e:  # noqa: BLE001
            log.warning("history: final flush failed: %s", e)
        await self._drop_pool()

    # ------------------------------------------------------------------ introspection

    def snapshot(self) -> dict:
        return {"connected": self.connected, "last_error": self.last_error, "stats": asdict(self.stats),
                "pending": {t: len(q) for t, q in self._queues.items()}}
