"""FastAPI application: starts telemetry ingest (NDTP listener + dataset replay) and serves the API.

Run with exactly one worker. The NDTP listener and all in-memory state live in this process; a second
worker would try to bind the NDTP port again and hold its own copy of the state::

    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1      # from backend/

Swagger UI at ``/docs``, OpenAPI schema at ``/openapi.json``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .alerts import AlertEngine
from .api import alerts, health, history, ingest, predictions, state as state_api
from .clock import DatasetClock
from .config import Settings
from .db import History
from .ingest import Ingest, ReplaySource, Traffic, load_traffic
from .ndtp import NdtpFix, NdtpServer
from .predict import MlClient, Predictor
from .state import Fleet, VehicleSchedule, load_schedule

VERSION = "0.1.0"

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = app.state
        state.started_at = time.time()
        state.issues = []
        state.clock = DatasetClock.starting_at(settings.clock_start, day=settings.clock_day, speed=settings.clock_speed)
        log.info("dataset clock: %s at startup, speed x%g", state.clock.anchor_dataset, state.clock.speed)

        traffic = await _load_traffic(settings, state.issues)
        schedules = await _load_schedule(settings, state.issues)
        state.ingest = Ingest(state.clock, traffic.unit_to_tr if traffic else {}, ndtp_fresh_s=settings.ndtp_fresh_s,
                              max_skew_s=settings.ndtp_max_clock_skew_s)
        state.fleet = Fleet(schedules, state.clock)
        state.ingest.subscribe(state.fleet.on_ping)
        tasks: list[asyncio.Task] = [_background(state.fleet.run(settings.state_tick_s), "fleet")]

        ml = MlClient(settings.ml_url, timeout_s=settings.ml_timeout_s)
        state.predictor = Predictor(state.fleet, state.clock, ml, max_ping_age_s=settings.predict_max_ping_age_s,
                                    retry_s=settings.predict_retry_s)
        tasks.append(_background(state.predictor.run(settings.predict_tick_s), "predictor"))

        state.alerts = AlertEngine(state.fleet, state.clock, risk_threshold=settings.alert_risk_threshold,
                                   id_prefix=f"a-{_base36(int(state.started_at))}")
        state.predictor.on_prediction(state.alerts.on_prediction)
        tasks.append(_background(state.alerts.run(settings.alert_tick_s), "alerts"))

        state.history = None
        if settings.database_url:
            state.history = History(settings.database_url, schedules, flush_s=settings.history_flush_s)
            state.ingest.subscribe(state.history.ping)
            state.fleet.on_arrivals(state.history.arrivals)
            state.predictor.on_prediction(state.history.prediction)
            state.alerts.on_event(state.history.alert)
            tasks.append(_background(state.history.run(), "history"))

        state.ndtp = None
        if settings.ndtp_enabled:
            fixes: asyncio.Queue[NdtpFix] = asyncio.Queue(maxsize=settings.ingest_queue_size)
            state.ndtp = NdtpServer(fixes, host=settings.ndtp_host, port=settings.ndtp_port,
                                    idle_timeout=settings.ndtp_idle_timeout_s,
                                    max_connections=settings.ndtp_max_connections, backlog=settings.ndtp_backlog)
            await state.ndtp.start()
            tasks.append(_background(state.ingest.consume_ndtp(fixes), "ingest-ndtp"))

        state.replay = None
        if settings.replay_enabled and traffic is not None:
            state.replay = ReplaySource(traffic.pings, state.clock, state.ingest.accept_replay,
                                        backfill_s=settings.replay_backfill_s)
            tasks.append(_background(state.replay.run(), "replay"))

        log.info("backend %s started%s", VERSION, f" with issues: {state.issues}" if state.issues else "")
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if state.history is not None:
                await state.history.close()
            if state.ndtp is not None:
                await state.ndtp.stop()
            await ml.aclose()

    app = FastAPI(
        title="mowtransit backend",
        version=VERSION,
        description="NDTP telemetry ingest, schedule matching, delay-prediction orchestration and the dispatcher API.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(state_api.router)
    app.include_router(predictions.router)
    app.include_router(alerts.router)
    app.include_router(history.router)
    return app


async def _load_traffic(settings: Settings, issues: list[str]) -> Traffic | None:
    if settings.dataset_dir is None:
        issues.append("DATASET_DIR not set: no unit registry (NDTP fixes can't be matched to vehicles), no replay")
        return None
    path = settings.dataset_dir / settings.dataset_split / "traffic.csv"
    try:
        return await asyncio.to_thread(load_traffic, path)
    except (OSError, ValueError, KeyError) as e:
        issues.append(f"cannot load {path}: {type(e).__name__}: {e}")
        return None


async def _load_schedule(settings: Settings, issues: list[str]) -> dict[int, VehicleSchedule]:
    if settings.dataset_dir is None:
        return {}
    name = "schedule_plan.csv" if settings.dataset_split == "validate" else "schedule.csv"
    path = settings.dataset_dir / settings.dataset_split / name
    try:
        return await asyncio.to_thread(load_schedule, path)
    except (OSError, ValueError, KeyError) as e:
        issues.append(f"cannot load {path}: {type(e).__name__}: {e} — no arrivals, deviations or predictions")
        return {}


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


def _background(coro: Coroutine, name: str) -> asyncio.Task:
    task = asyncio.create_task(coro, name=name)

    def done(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.error("background task %s crashed", name, exc_info=t.exception())

    task.add_done_callback(done)
    return task


app = create_app()
