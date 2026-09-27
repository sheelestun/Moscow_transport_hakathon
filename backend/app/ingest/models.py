"""The normalized telemetry point every downstream component works with."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Source = Literal["ndtp", "replay"]


@dataclass(frozen=True, slots=True)
class Ping:
    """One telemetry point in ``traffic.csv`` terms, on the dataset timeline."""

    tr_id: int
    unit_id: int
    event_time: datetime          # dataset time, naive Moscow-local (as in traffic.csv)
    lat: float | None             # None when the fix is invalid
    lon: float | None
    speed_kmh: float | None
    heading_deg: float | None
    location_valid: bool
    is_hist: bool                 # terminal re-sent buffered data (traffic.csv ``is_hist_data``)
    source: Source
    received_at: float = 0.0      # wall clock when the backend accepted it
