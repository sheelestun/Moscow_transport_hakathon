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
| 6 | ML client + predictor (target stop in (T+10, T+15] min, `/predict/batch`) | |
| 7 | Alerts: threshold, dedup, cause, verification against the actual arrival | |
| 8 | Postgres history | |
| 9 | Dashboard REST + WebSocket (timestamps sent to the UI in wall time, not dataset time) | |
| 10 | Deploy: nginx vhosts (`api.` / `app.` / `docs.` mowtransit.ru), TLS, compose | |

## Layout (so far)

```
app/
  main.py         the service: FastAPI app, lifespan starts/stops ingest
  config.py       settings from env vars (DATASET_DIR, CLOCK_START, NDTP_PORT, ...)
  clock.py        DatasetClock: wall time ↔ the dataset's timeline (2026-01-06, Moscow-local)
  api/
    health.py     GET /health (short status), GET /clock
    ingest.py     GET /ingest/ndtp (listener), GET /ingest/vehicles (latest ping per vehicle + source)
    state.py      GET /state/vehicles (derived features), GET /state/vehicles/{tr_id}/arrivals
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

Tests: `pytest backend/tests` from the repo root (`pytest.ini` puts `backend/` on the path).

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

## Deferred

### `/metrics/worst_stops`, `/metrics/bunching`, `/routes/{route_id}/signals`

Not implemented yet. The dashboard calls all three through `try/catch` in `frontend/js/api.js`
and hides the widgets on failure, so the dashboard works without them. Shapes to match are in
`frontend/js/mock.js`:

- **`GET /metrics/worst_stops?limit=10`** (`mock.js` `getWorstStops`) — list of
  `{route_id, direction_id, stop_id, name, lat, lon, avg_delay_sec, max_delay_sec, vehicles}`,
  only `avg_delay_sec >= 30`, sorted by `avg_delay_sec` desc. Mock aggregates predicted delays of
  upcoming stops; with real data this is a query over the `predictions` table.
- **`GET /metrics/bunching`** (`mock.js` `getBunching`) — pairs of vehicles on the same
  route/direction whose headway < `max(45 s, 0.6 × planned headway)`:
  `{route_id, direction_id, leader_id, follower_id, headway_sec, plan_headway_sec, ratio,
  leader: {lat, lon}, follower: {lat, lon}}`, sorted by `ratio` asc. Needs a real notion of
  route + direction, which the dataset doesn't have (route_id := tr_id) — likely stays unimplemented.
- **`GET /routes/{route_id}/signals`** (`mock.js` `getSignals`) — traffic lights with phase state.
  The dataset has no traffic-light data: **not implementing**.

**Frontend bug to hand to Вероника** (blocks `signals` regardless of the backend):
`frontend/js/app.js:305` calls `source.getSignals(r.route_id).forEach(...)` synchronously. In the
mock `getSignals` is synchronous; in `api.js` it is `async` and returns a Promise, so in live mode
this throws `TypeError` on every `vehicle.update` — before `renderVehicle()` runs, so the selected
vehicle's card stops refreshing. Fix on her side: drop `getSignals` from the live source, or
`await` it outside the per-message path.
