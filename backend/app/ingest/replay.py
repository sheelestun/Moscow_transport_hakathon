"""Replay source: plays ``traffic.csv`` into the ingest pipeline as the dataset clock passes each row.

On start it backfills the last ``backfill_s`` of dataset time at once — the prediction features need
recent GPS history (about an hour), and a live feed alone would leave the model blind until it builds up.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_left
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Literal

from ..clock import DatasetClock
from .models import Ping

log = logging.getLogger(__name__)


class ReplaySource:
    def __init__(self, pings: Sequence[Ping], clock: DatasetClock, accept: Callable[[Ping], bool], *,
                 backfill_s: float = 3600.0, tick_s: float = 1.0) -> None:
        self.pings = pings                  # sorted by event_time
        self.clock = clock
        self.accept = accept
        self.backfill_s = backfill_s
        self.tick_s = tick_s
        self.cursor = 0
        self.status: Literal["pending", "running", "finished"] = "pending"

    async def run(self) -> None:
        start = self.clock.now() - timedelta(seconds=self.backfill_s)
        self.cursor = bisect_left(self.pings, start, key=lambda p: p.event_time)
        self.status = "running"
        log.info("replay from %s (row %d of %d)", start, self.cursor, len(self.pings))
        while self.cursor < len(self.pings):
            now = self.clock.now()
            emitted = 0
            while self.cursor < len(self.pings) and self.pings[self.cursor].event_time <= now:
                self.accept(self.pings[self.cursor])
                self.cursor += 1
                emitted += 1
                if emitted % 1000 == 0:  # the backfill can be thousands of rows: let the loop breathe
                    await asyncio.sleep(0)
            await asyncio.sleep(self.tick_s)
        self.status = "finished"
        log.info("replay finished: end of dataset")

    def snapshot(self) -> dict:
        nxt = self.pings[self.cursor].event_time.isoformat() if self.cursor < len(self.pings) else None
        return {"status": self.status, "position": self.cursor, "total": len(self.pings), "next_event_time": nxt}
