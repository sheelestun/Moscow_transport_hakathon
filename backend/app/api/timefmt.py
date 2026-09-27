"""Dataset time → what the dashboard shows.

The dashboard compares timestamps with the browser's ``Date.now()``, so everything user-facing is sent as
wall-clock UTC ISO strings: a dataset time is mapped back through the dataset clock. Diagnostic endpoints
keep dataset times.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..clock import DatasetClock


def wall_iso(clock: DatasetClock, dataset_time: datetime) -> str:
    return unix_iso(clock.to_wall(dataset_time))


def unix_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
