"""Loading ``traffic.csv``: the unit → vehicle registry, and the pings the replay source plays back."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .models import Ping

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Traffic:
    pings: list[Ping]             # sorted by event_time
    unit_to_tr: dict[int, int]    # terminal (NDTP peer address) → vehicle


def load_traffic(path: Path) -> Traffic:
    """Parse ``traffic.csv``. Rows whose unit already maps to another vehicle are skipped and logged."""
    pings: list[Ping] = []
    unit_to_tr: dict[int, int] = {}
    conflicts = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tr_id, unit_id = int(row["tr_id"]), int(row["unit_id"])
            if unit_to_tr.setdefault(unit_id, tr_id) != tr_id:
                conflicts += 1
                continue
            valid = row["location_valid"] == "True"
            pings.append(Ping(
                tr_id=tr_id,
                unit_id=unit_id,
                event_time=datetime.fromisoformat(row["event_time"]),
                lat=_num(row["lat"]) if valid else None,
                lon=_num(row["lon"]) if valid else None,
                speed_kmh=_num(row["speed"]) if valid else None,
                heading_deg=_num(row["heading"]) if valid else None,
                location_valid=valid,
                is_hist=row["is_hist_data"] == "True",
                source="replay",
            ))
    if conflicts:
        log.warning("%s: skipped %d rows whose unit_id already maps to another tr_id", path, conflicts)
    pings.sort(key=lambda p: p.event_time)
    log.info("loaded %s: %d pings, %d units", path, len(pings), len(unit_to_tr))
    return Traffic(pings=pings, unit_to_tr=unit_to_tr)


def _num(s: str) -> float | None:
    return float(s) if s else None
