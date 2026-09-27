# backend

Production backend: NDTP ingest → schedule matching → ML orchestration → dispatcher API.
The old simulator-based demo backend lives in `../mock_backend` and is not part of this.

## Plan

Built piece by piece; each piece is tested before the next starts.

| # | Piece | Status |
|---|---|---|
| 1 | NDTP codec | done |
| 2 | NDTP TCP server + load test | done |
| 3 | Service shell: `main.py`, config, `/health`, Swagger | done |
| 4 | Dataset clock, normalized pings (unit → vehicle), replay source, NDTP/replay arbitration | done |
| 5 | State: schedule index, per-vehicle buffers, arrival detection → current deviation, segment speed, dwell | done |
| 6 | ML client + predictor (target stop in (T+10, T+15] min, `/predict/batch`) | done |
| 7 | Alerts: threshold, dedup, cause, verification against the actual arrival | done |
| 8 | Postgres history | done |
| 9 | Dashboard REST + WebSocket (timestamps sent to the UI in wall time, not dataset time) | done |
| 10 | Deploy: nginx vhosts (`api.` / `app.` / `docs.` mowtransit.ru), TLS, compose | |

## Layout (so far)

```
app/
  main.py         the service: FastAPI app, lifespan starts/stops ingest
  config.py       settings from env vars (DATASET_DIR, CLOCK_START, NDTP_PORT, ...)
  clock.py        DatasetClock: wall time ↔ the dataset's timeline (2026-01-06, Moscow-local)
  api/
    dashboard.py  the dashboard's REST API (frontend/js/api.js): routes, vehicles, schedule, metrics, what-if
    ws.py         WS /ws: vehicle.update every second, alert.* and whatif.result as they happen
    views.py      routes/vehicles/schedule payloads in the shapes frontend/js/mock.js defines
    health.py     GET /health (short status), GET /clock
    ingest.py     GET /ingest/ndtp (listener), GET /ingest/vehicles (latest ping per vehicle + source)
    state.py      GET /state/vehicles (derived features), GET /state/vehicles/{tr_id}/arrivals
    predictions.py  GET /predictions (latest per vehicle), GET /predictions/recent
    alerts.py     GET /alerts?active=true|false — dashboard alert payloads
    timefmt.py    dataset time → wall-clock ISO for everything user-facing
    history.py    GET /history/summary, GET /history/alerts (from Postgres: survives restarts)
  ndtp/
    protocol.py   NDTP wire codec: framing, CRC-16/MODBUS, handshake + G6CellNav00 decoding
    server.py     asyncio TCP server terminals connect to; emits NdtpFix onto a queue
    __main__.py   standalone debug listener: NDTP ingest only, logs every fix
  ingest/
    models.py     Ping: one traffic.csv-style point on the dataset timeline
    dataset.py    traffic.csv loader: unit → vehicle registry + pings for replay
    pipeline.py   NdtpFix / replayed row → Ping; per-vehicle NDTP/replay arbitration
    replay.py     plays traffic.csv as the clock passes (backfills the last hour on start)
  state/
    schedule.py   schedule_plan.csv → each vehicle's planned stop visits, trips (gap > 5 min = terminal)
    arrivals.py   GPS arrival detection — a port of ml/src/features/tabular.gps_history, same constants
    fleet.py      per-vehicle ping buffers, arrivals log, derived features; recomputes only vehicles with new pings
    geo.py        distance / time helpers (same formulas as ML)
  predict/
    client.py     HTTP client for the ML service
    payload.py    vehicle state → ML PredictRequest (telemetry window + the vehicle's whole day plan)
    predictor.py  target stop 10–15 min ahead, one forecast per target, ML-down fallback + retry
  alerts.py       red forecasts → alerts (one per vehicle), verified against the detected arrival
  db/
    schema.sql    telemetry, arrivals, predictions, alerts (applied idempotently at startup)
    history.py    write-behind writer: batches, COPY for append-only tables, upserts, buffering while Postgres is down
scripts/
  ndtp_loadtest.py  load test for NDTP ingest (see below)
tests/
  fixtures/       raw TCP captures from ndtp-telemetry-emulator:1.0 (see test_ndtp_protocol.py);
                  dataset/validate/traffic.csv is synthetic (real format and IDs, made-up positions)
src/csv_replayer.py   kept from the old backend: reference for the ML /predict payload
```

## Running

| What | Command | Ports |
|---|---|---|
| The service (Docker) | `docker compose --profile dev up -d --build backend-dev` (repo root) | API `:8010` → 8000, NDTP `:9201` |
| The service (local) | `DATASET_DIR=<dataset> uvicorn app.main:app --workers 1` (from `backend/`) | API `:8000`, NDTP `:9201` |
| NDTP only, for debugging | `python -m app.ndtp` (from `backend/`) | NDTP `:9201` |

Always one worker: the NDTP listener and in-memory state live in the process. Swagger: `/docs`.
`backend-dev` doesn't depend on `ml` in compose on purpose — without ML the backend degrades, it doesn't wait.

Compose mounts the dataset (the organizers' archive, unpacked) from `./dataset`; elsewhere set
`DATASET_HOST_DIR=/path/to/dataset`. Without a dataset the service runs but reports `degraded`: NDTP
fixes can't be matched to vehicles.

### Time

The dataset is one day, 2026-01-06; live telemetry carries today's time. `app/clock.py` maps one onto
the other. By default the dataset clock starts at the current Moscow time of day, speed 1 — so **at
night the city is empty**, and after midnight MSK the clock runs into 2026-01-07 where there's no data.
For a demo, pin it: `CLOCK_START=2026-01-06T08:00:00` (morning rush); `CLOCK_SPEED` speeds it up.

### Live NDTP with real tracks

`autoGenerate` in the emulator is a random walk that matches no vehicle. `infra/emulator_replay.py`
feeds the emulator real tracks from `validate/traffic.csv`; `--clock-url` makes it follow this
backend's clock (without it, the two clocks can differ by up to a minute):

```
python infra/emulator_replay.py --dataset <dataset> --emu-url http://localhost:18080 \
    --target-host ndtp.mowtransit.ru --target-port 9201 --clock-url https://api.mowtransit.ru
```

Vehicles on live NDTP override the replay; if their feed stops for `NDTP_FRESH_S` (60 s), replay takes
over again (`GET /ingest/vehicles` shows each vehicle's source).

Tests: `pytest backend/tests` from the repo root (`pytest.ini` puts `backend/` on the path). The Postgres
test runs when `TEST_DATABASE_URL` is set (see `tests/test_history.py`), otherwise it's skipped.

## Derived features (criterion 3)

`app/state` matches telemetry to the planned schedule and derives, per vehicle: **current deviation**
(delay at the latest GPS-detected arrival — `cur_dev_s` for the model), **segment speed** (distance along
the plan / time between the last two arrivals of a trip) and **dwell** (time within 60 m of the last stop).
See `GET /state/vehicles`.

Measured on the real validate day (2026-09-27):

- **Parity with ML**: arrivals are identical to `ml/src/features/tabular.gps_history` — 1,560 vehicle×moment
  checks, 23,664 arrivals, 0 mismatches — so the model sees the arrivals it was trained on.
- **Arrival accuracy**: 4,100 detected arrivals vs the actual arrival facts in `test/schedule.csv` (same day,
  same telemetry): median error 3 s, 95% within 30 s, bias −2 s.
- **`cur_dev_s` online vs the organizers' value**: median 45 s apart (mean 79 s) at the 151 validate points.
  The organizers' `cur_dev_s` is the delay at the last stop *planned* ≤ T (97% exact match), whose actual
  arrival can be *after* T, and includes manually-filled stops — neither is knowable from GPS at T. Online
  predictions therefore start from a less precise `cur_dev_s` than the model was trained on; a model
  variant anchored on the GPS deviation would close this gap (ML team's call).

## Forecasts (criterion 2)

Every `PREDICT_TICK_S` (5 s) the predictor picks, per vehicle in service, the first planned stop in
**(T + 10 min, T + 15 min]** — the dataset's own definition — and asks the ML service
(`ML_URL`, `POST /predict/batch`). One forecast per (vehicle, target stop): as time moves the target rolls
forward, so each vehicle gets a fresh forecast every 1–3 minutes, always 10–15 minutes before the stop.
`GET /health` reports the share of forecasts inside the horizon (`horizon_ok_share`) and ML latency.

If ML is unreachable the backend forecasts the baseline itself (delay = current deviation, the contract's
risk sigmoid), marked `source: fallback`, reports `degraded`, and retries ML after `PREDICT_RETRY_S`.

Checked against a locally trained model (2026-09-27): with the organizers' `cur_dev_s` substituted,
backend-built payloads reproduce the batch `submission.csv` within 0.1 s on 104/151 validate points (mean
gap 1.6 s). The rest is inside the ML service — its online feature path isn't byte-identical to the batch
pipeline; its own reference payloads (`src/csv_replayer.py`) differ from the batch too. Live on the real
day: 100% of forecasts inside the horizon, ~21 ms per forecast in batches.

## Alerts

A forecast becomes an alert when the model's probability of arriving > 2 min late reaches
`ALERT_RISK_THRESHOLD` (0.7, the dashboard's red) and its target stop is 10–15 min ahead — so there are no
after-the-fact alerts. One active alert per vehicle. Each alert carries the predicted delay, the model's
cause and recommendation, and the route segment (last detected stop → target stop).

When the target arrival is detected the alert is `verified` with the actual delay and scored as a hit
(actually > 2 min late) or a false alarm; `GET /health` shows live precision and forecast error. No arrival
within the detection window → `resolved`.

Live run on the real day (dataset 14:55–15:29 at ×10, locally trained model): 170 forecasts, 100% in the
horizon; 4 alerts, 3 verified, all 3 hits (actual delays 253–278 s), forecast error 58 s.

## History

With `DATABASE_URL` set, telemetry, detected arrivals, every forecast (with the full ML response) and alerts
(with their outcome) go to Postgres. The pipeline only enqueues rows; a background task flushes them every
second (`COPY` for telemetry and forecasts, upserts for arrivals and alerts), so the database can't slow the
live path. If Postgres goes down the service keeps working, buffers rows in memory (capped; oldest dropped
and counted), reports `degraded`, and catches up after reconnecting — checked live by stopping and
restarting Postgres mid-run: nothing lost.

## NDTP ingest performance

`scripts/ndtp_loadtest.py`: N simulated terminals against a real `NdtpServer` (server and clients in
separate processes; every packet timestamped at send and matched at the consumer). Measured
2026-09-27 in a Linux container (Docker Desktop, Apple M1, 7 vCPU), uvloop, backlog 4096, one core
for the server:

| Load | Delivered | Latency send → queue | Server CPU |
|---|---|---|---|
| 8,000 units every 1 s, send times spread | 120,000 / 120,000 | p50 0.3 ms, p99 1.2 ms | 44% |
| 16,000 units every 1 s, all on the same tick | 240,000 / 240,000 | p99 0.6 s (burst queueing), each burst drained in ≤ 0.7 s | 51% |
| 16,000 units every 0.5 s, all on the same tick | 320,000 / 320,000 | p99 1.3 s — **saturated** (bursts no longer drain within the interval) | 99% |

- Ceiling ≈ 25–30k packets/s per core. The codec is 7.6 µs/packet; the rest is asyncio socket overhead.
- Memory: 149 MB peak for 16,000 connections (≈ 9.5 KB each).
- Connect storm: all 16,000 connect with backlog 4096; with asyncio's default backlog 100, 8,000
  simultaneous connects took up to 4.1 s (Linux) or mostly timed out (macOS, `somaxconn=128`).
- For scale: the dataset's terminals report every 12–15 s, so 10,000 vehicles ≈ 700–800 packets/s —
  ~3% of one core for ingest.

Re-run on the VPS: `python backend/scripts/ndtp_loadtest.py --units 8000 --loop uvloop` (needs `uvloop`;
check `sysctl net.core.somaxconn` ≥ 4096).

## Dashboard API (criterion 4)

Everything `frontend/js/api.js` calls, in the shapes `frontend/js/mock.js` defines — the real dashboard runs
against it unchanged (`index.html?mode=live&api=…&ws=…`), checked in a browser with the locally trained model.

- **Routes**: the dataset has no route ids, so each scheduled vehicle is a "route", and every distinct trip
  shape (first stop → last stop) is a direction with its own line. The map projects the vehicle and its
  schedule onto the line of the vehicle's *current* trip. Lines are stop-to-stop straight segments (no road
  geometry in the dataset).
- **Vehicles**: position, current deviation (`delay_now_sec`), the model's forecast for the target stop
  (`delay_pred_sec`, `risk_score`, cause, recommendation).
- **Schedule** of the current trip: actual (GPS-detected) arrivals behind the vehicle; the target stop with
  the model's forecast; other upcoming stops with the current deviation carried forward (`estimate` says
  which).
- **What-if**: ML `/whatif/predict` on the vehicle's latest forecast request.
- **WebSocket** `/ws`: snapshot on connect, `vehicle.update` every `WS_TICK_S`, alert events as they happen.
- All user-facing timestamps are wall-clock (the dashboard compares them with `Date.now()`).

Always empty, by design: `GET /metrics/bunching` (needs vehicles sharing a route and direction; the dataset
has no route relations) and `GET /routes/{id}/signals` (no traffic-light data). Both return `[]` rather than
404 so the dashboard's widgets stay quiet.

### Frontend bug (for Вероника)

`frontend/js/app.js:305` calls `source.getSignals(r.route_id).forEach(...)` synchronously. In `mock.js`
`getSignals` is synchronous; in `api.js` it's `async`, so in live mode this throws
`TypeError: source.getSignals(...).forEach is not a function` on **every** `vehicle.update` — before
`renderVehicle()` runs — and also during the initial load (`app.js:728`: "Не удалось загрузить начальные
данные"). The dashboard recovers from the WebSocket stream, but the selected vehicle's card doesn't refresh
from it. Smallest fix: remove `getSignals` from the live source in `frontend/js/api.js` (live mode has no
signals, and `app.js` already checks `if (source.getSignals)`).
