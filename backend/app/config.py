"""Runtime settings, read from environment variables (and ``.env`` in the working directory, if present).

Every field maps to the upper-case env var of the same name, e.g. ``NDTP_PORT=9201``.
Lists are JSON: ``CORS_ORIGINS='["https://app.mowtransit.ru"]'``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    cors_origins: list[str] = Field(default=["*"], description="browser origins allowed to call the API")

    # NDTP listener (terminals / emulator connect here)
    ndtp_enabled: bool = True
    ndtp_host: str = "0.0.0.0"
    ndtp_port: int = 9201
    ndtp_idle_timeout_s: float = 300.0
    ndtp_max_connections: int = 20_000
    ndtp_backlog: int = 4096

    ingest_queue_size: int = Field(default=100_000, description="fixes buffered between the listener and the consumer")
