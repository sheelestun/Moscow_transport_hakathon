"""Runtime settings, read from environment variables (and ``.env`` in the working directory, if present).

Every field maps to the upper-case env var of the same name, e.g. ``NDTP_PORT=9201``.
Lists are JSON: ``CORS_ORIGINS='["https://app.mowtransit.ru"]'``.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .clock import DATASET_DAY


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    log_level: str = "INFO"
    cors_origins: list[str] = Field(default=["*"], description="browser origins allowed to call the API")

    # NDTP listener (terminals / emulator connect here)
    ndtp_enabled: bool = True
    ndtp_host: str = "0.0.0.0"
    ndtp_port: int = 9201
    ndtp_idle_timeout_s: float = 300.0
    ndtp_max_connections: int = 20_000
    ndtp_backlog: int = 4096

    # Dataset (the organizers' archive): unit registry and replay source
    dataset_dir: Path | None = Field(default=None, description="folder containing train/ test/ validate/ labels/")
    dataset_split: str = "validate"

    # Dataset clock (see app/clock.py)
    clock_day: date = DATASET_DAY
    clock_start: datetime | None = Field(default=None, description="dataset time at startup, e.g. "
                                         "2026-01-06T07:30:00; default: current Moscow time of day on clock_day")
    clock_speed: float = Field(default=1.0, gt=0)

    # Ingest
    ingest_queue_size: int = Field(default=100_000, description="fixes buffered between the listener and ingest")
    replay_enabled: bool = True
    replay_backfill_s: float = Field(default=3600.0, description="dataset history replayed at once on startup")
    ndtp_fresh_s: float = Field(default=60.0, description="a vehicle with NDTP this recent ignores replayed rows")
    ndtp_max_clock_skew_s: float = Field(default=300.0, description="beyond this terminal-vs-server clock "
                                         "difference, the server receive time is used")

    # Vehicle state
    state_tick_s: float = Field(default=2.0, description="how often arrivals/derived features are recomputed")
