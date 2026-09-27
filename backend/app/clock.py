"""Dataset clock: maps wall-clock time onto the dataset's timeline.

The dataset is one service day, 2026-01-06, with naive Moscow-local timestamps. Live telemetry (the
emulator, real terminals) carries today's wall-clock time. Every component converts through one
``DatasetClock``, so live and historical data share a single timeline::

    dataset_time = anchor_dataset + (wall_time - anchor_wall) * speed

Default anchor: at startup, the current Moscow time of day on the dataset day, speed 1 — the demo
shows the dataset's vehicles "as of now". ``GET /clock`` publishes the anchor, so external drivers
(``infra/emulator_replay.py --clock-url``) replay tracks on exactly this timeline.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))
DATASET_DAY = date(2026, 1, 6)


@dataclass(frozen=True, slots=True)
class DatasetClock:
    anchor_wall: float         # Unix seconds
    anchor_dataset: datetime   # naive, Moscow-local, like the dataset
    speed: float = 1.0

    def __post_init__(self) -> None:
        if self.speed <= 0:
            raise ValueError(f"clock speed must be positive, got {self.speed}")
        if self.anchor_dataset.tzinfo is not None:
            raise ValueError("anchor_dataset must be naive (dataset timestamps carry no timezone)")

    @classmethod
    def starting_at(cls, dataset_time: datetime | None = None, *, day: date = DATASET_DAY, speed: float = 1.0,
                    wall: float | None = None) -> DatasetClock:
        """A clock whose dataset time at ``wall`` (default: now) is ``dataset_time``
        (default: the current Moscow time of day on ``day``)."""
        wall = time.time() if wall is None else wall
        if dataset_time is None:
            dataset_time = datetime.combine(day, datetime.fromtimestamp(wall, MSK).time())
        return cls(anchor_wall=wall, anchor_dataset=dataset_time, speed=speed)

    def to_dataset(self, wall: float) -> datetime:
        return self.anchor_dataset + timedelta(seconds=(wall - self.anchor_wall) * self.speed)

    def to_wall(self, dataset_time: datetime) -> float:
        return self.anchor_wall + (dataset_time - self.anchor_dataset).total_seconds() / self.speed

    def now(self) -> datetime:
        return self.to_dataset(time.time())
