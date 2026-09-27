"""Ingest pipeline: NDTP fixes and replayed rows become ``Ping``s on one timeline, one stream.

Both sources feed the same pipeline, arbitrated per vehicle: while a vehicle has sent NDTP within
``ndtp_fresh_s``, replayed rows for it are dropped. When its live feed goes quiet, replay takes over;
when NDTP returns, it takes back over (criterion 5: degrade to historical data, recover after reconnect).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace

from ..clock import DatasetClock
from ..ndtp import NdtpFix
from .models import Ping

log = logging.getLogger(__name__)

Subscriber = Callable[[Ping], None]


@dataclass(slots=True)
class IngestStats:
    ndtp_pings: int = 0
    replay_pings: int = 0
    replay_suppressed: int = 0      # replayed rows dropped because the vehicle is live on NDTP
    unknown_unit_fixes: int = 0     # NDTP fixes from units not in the registry
    clock_skew_fallbacks: int = 0   # terminal clock too far off: server receive time used instead
    subscriber_errors: int = 0


class Ingest:
    def __init__(self, clock: DatasetClock, unit_to_tr: dict[int, int], *, ndtp_fresh_s: float = 60.0,
                 max_skew_s: float = 300.0, wall: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.unit_to_tr = unit_to_tr
        self.ndtp_fresh_s = ndtp_fresh_s
        self.max_skew_s = max_skew_s
        self.stats = IngestStats()
        self.latest: dict[int, Ping] = {}          # tr_id → newest ping by event_time
        self.unknown_units: dict[int, int] = {}    # unit_id → fixes received
        self._ndtp_seen: dict[int, float] = {}     # tr_id → wall time of its last NDTP ping
        self._subscribers: list[Subscriber] = []
        self._wall = wall

    def subscribe(self, fn: Subscriber) -> None:
        self._subscribers.append(fn)

    # ------------------------------------------------------------------ sources

    def accept_ndtp(self, fix: NdtpFix) -> Ping | None:
        tr_id = self.unit_to_tr.get(fix.unit_id)
        if tr_id is None:
            self.stats.unknown_unit_fixes += 1
            self.unknown_units[fix.unit_id] = self.unknown_units.get(fix.unit_id, 0) + 1
            return None
        nav = fix.nav
        event_wall = float(nav.timestamp)
        if abs(fix.received_at - event_wall) > self.max_skew_s:
            self.stats.clock_skew_fallbacks += 1
            event_wall = fix.received_at
        valid = nav.location_valid
        ping = Ping(
            tr_id=tr_id,
            unit_id=fix.unit_id,
            event_time=self.clock.to_dataset(event_wall),
            lat=nav.latitude if valid else None,
            lon=nav.longitude if valid else None,
            speed_kmh=float(nav.speed_avg_kmh) if valid else None,
            heading_deg=float(nav.course_deg) if valid else None,
            location_valid=valid,
            is_hist=False,
            source="ndtp",
            received_at=fix.received_at,
        )
        self._ndtp_seen[tr_id] = fix.received_at
        self.stats.ndtp_pings += 1
        self._emit(ping)
        return ping

    def accept_replay(self, ping: Ping) -> bool:
        now = self._wall()
        seen = self._ndtp_seen.get(ping.tr_id)
        if seen is not None and now - seen < self.ndtp_fresh_s:
            self.stats.replay_suppressed += 1
            return False
        self.stats.replay_pings += 1
        self._emit(replace(ping, received_at=now))
        return True

    async def consume_ndtp(self, fixes: asyncio.Queue[NdtpFix]) -> None:
        while True:
            self.accept_ndtp(await fixes.get())

    # ------------------------------------------------------------------ output

    def _emit(self, ping: Ping) -> None:
        prev = self.latest.get(ping.tr_id)
        if prev is None or ping.event_time >= prev.event_time:
            self.latest[ping.tr_id] = ping
        for fn in self._subscribers:
            try:
                fn(ping)
            except Exception:  # noqa: BLE001 — one broken consumer must not stop ingest
                self.stats.subscriber_errors += 1
                log.exception("ingest subscriber %r failed", fn)

    # ------------------------------------------------------------------ introspection

    def live_vehicles(self) -> list[int]:
        """Vehicles currently fed by NDTP (not replay)."""
        now = self._wall()
        return sorted(tr for tr, seen in self._ndtp_seen.items() if now - seen < self.ndtp_fresh_s)

    def snapshot(self) -> dict:
        return {
            "stats": asdict(self.stats),
            "vehicles_seen": len(self.latest),
            "vehicles_live_ndtp": self.live_vehicles(),
            "registry_units": len(self.unit_to_tr),
            "unknown_units": dict(sorted(self.unknown_units.items(), key=lambda kv: -kv[1])[:50]),
        }
