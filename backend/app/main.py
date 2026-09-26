"""FastAPI application: starts NDTP ingest and serves the dispatcher API.

Run with exactly one worker. The NDTP listener and all in-memory state live in this process; a second
worker would try to bind the NDTP port again and hold its own copy of the state::

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1      # from backend/

Swagger UI at ``/docs``, OpenAPI schema at ``/openapi.json``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import health
from .config import Settings
from .ndtp import NdtpFix, NdtpServer

VERSION = "0.1.0"

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = app.state
        state.started_at = time.time()
        state.last_fix = {}
        state.last_fix_at = None
        state.ndtp = None
        fixes: asyncio.Queue[NdtpFix] = asyncio.Queue(maxsize=settings.ingest_queue_size)
        if settings.ndtp_enabled:
            state.ndtp = NdtpServer(fixes, host=settings.ndtp_host, port=settings.ndtp_port,
                                    idle_timeout=settings.ndtp_idle_timeout_s,
                                    max_connections=settings.ndtp_max_connections, backlog=settings.ndtp_backlog)
            await state.ndtp.start()
        consumer = asyncio.create_task(_consume_fixes(fixes, state), name="consume-fixes")
        log.info("backend %s started", VERSION)
        try:
            yield
        finally:
            consumer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await consumer
            if state.ndtp is not None:
                await state.ndtp.stop()

    app = FastAPI(
        title="mowtransit backend",
        version=VERSION,
        description="NDTP telemetry ingest, schedule matching, delay-prediction orchestration and the dispatcher API.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.include_router(health.router)
    return app


async def _consume_fixes(fixes: asyncio.Queue[NdtpFix], state) -> None:
    """Stand-in consumer: keeps the latest fix per unit, so fixes are visible in ``/ingest/ndtp``.

    Replaced by the ingest pipeline (unit → vehicle mapping, dataset clock, schedule matching).
    """
    while True:
        fix = await fixes.get()
        state.last_fix[fix.unit_id] = fix
        state.last_fix_at = fix.received_at


app = create_app()
