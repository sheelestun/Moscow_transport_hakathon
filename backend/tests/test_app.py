"""App wiring: lifespan starts ingest, NDTP and replay reach the diagnostics, health reports status."""

from __future__ import annotations

import socket
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


def client(**overrides) -> TestClient:
    # Pinned clock (the fixture's pings are 08:00–08:04, so 12:00 replays nothing) and an ML URL that refuses
    # connections: tests must not depend on the time of day or on a running ML service.
    settings = {"ndtp_host": "127.0.0.1", "ndtp_port": 0, "dataset_dir": FIXTURES / "dataset",
                "clock_start": "2026-01-06T12:00:00", "ml_url": "http://127.0.0.1:9", **overrides}
    return TestClient(create_app(Settings(_env_file=None, **settings)))


def wait_for(cond, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.02)


def test_health_clock_and_swagger() -> None:
    with client(replay_enabled=False, clock_start="2026-01-06T07:30:00") as c:
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["issues"] == []
        assert h["ndtp"]["listening"] and h["ndtp"]["port"] > 0
        assert h["ingest"]["registry_units"] == 2 and h["replay"]["enabled"] is False
        clk = c.get("/clock").json()
        assert clk["anchor_dataset"] == "2026-01-06T07:30:00" and clk["speed"] == 1.0
        assert clk["dataset_now"].startswith("2026-01-06T07:30:0")
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/docs").status_code == 200


def test_without_dataset_the_service_runs_degraded() -> None:
    with client(dataset_dir=None) as c:
        h = c.get("/health").json()
        assert h["status"] == "degraded" and "DATASET_DIR not set" in h["issues"][0]
        assert h["ndtp"]["listening"] and h["replay"]["enabled"] is False


def test_emulator_stream_becomes_vehicle_pings() -> None:
    with client(replay_enabled=False) as c:
        port = c.get("/health").json()["ndtp"]["port"]
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall((FIXTURES / "emulator_explicit_moscow.bin").read_bytes())
            wait_for(lambda: c.get("/health").json()["ingest"]["vehicles_seen"] == 1)
            h = c.get("/health").json()
            body = c.get("/ingest/vehicles").json()
        assert (h["ndtp"]["fixes_total"], h["ingest"]["vehicles_live_ndtp"], h["ndtp"]["fixes_dropped"]) == (5, 1, 0)
        v = body["vehicles"]["115106"]                        # unit 664030 → tr 115106 via the registry
        assert (v["unit_id"], v["source"], v["lat"], v["lon"], v["valid"]) == (664030, "ndtp", 55.7551234, 37.617321, True)
        assert body["pipeline"]["stats"]["ndtp_pings"] == 5


def test_unknown_units_are_listed() -> None:
    with client(replay_enabled=False) as c:
        port = c.get("/health").json()["ndtp"]["port"]
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall((FIXTURES / "emulator_autogenerate.bin").read_bytes())  # unit 1166336: not in the registry
            wait_for(lambda: c.get("/health").json()["ingest"]["unknown_units"] == 1)
        body = c.get("/ingest/vehicles").json()
        assert body["vehicles"] == {} and body["pipeline"]["unknown_units"] == {"1166336": 5}


def test_replay_backfills_the_dataset() -> None:
    with client(clock_start="2026-01-06T08:05:00", replay_backfill_s=3600) as c:
        wait_for(lambda: c.get("/health").json()["replay"]["status"] == "finished")
        h = c.get("/health").json()
        body = c.get("/ingest/vehicles").json()
    assert (h["replay"]["position"], h["replay"]["total"], h["ingest"]["vehicles_seen"]) == (7, 7, 2)
    assert {v["source"] for v in body["vehicles"].values()} == {"replay"}
    assert body["vehicles"]["115106"]["event_time"] == "2026-01-06T08:04:00"


def test_ndtp_can_be_disabled() -> None:
    with client(ndtp_enabled=False) as c:
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["ndtp"]["enabled"] is False
        assert c.get("/ingest/ndtp").json() == {"enabled": False}


def test_state_derives_deviation_segment_speed_and_dwell() -> None:
    with client(clock_start="2026-01-06T08:05:00", state_tick_s=0.05) as c:
        wait_for(lambda: c.get("/health").json()["state"]["with_deviation"] == 1)
        h = c.get("/health").json()["state"]
        body = c.get("/state/vehicles").json()
        arrivals = c.get("/state/vehicles/115106/arrivals").json()
        assert c.get("/state/vehicles/116057/arrivals").status_code == 404   # has no schedule
    assert (h["vehicles"], h["with_schedule"], h["arrivals_detected"]) == (2, 1, 2)
    d = body["vehicles"]["115106"]["derived"]
    assert (d["last_stop"]["name"], d["cur_dev_s"], d["next_stop"]["name"]) == ("Остановка Б", 0.0, "Остановка В")
    assert d["segment_speed_kmh"] == 3.8                            # 125 m between А and Б in 120 s
    assert body["vehicles"]["116057"]["has_schedule"] is False
    assert [a["name"] for a in arrivals] == ["Остановка А", "Остановка Б"]


def test_forecast_falls_back_to_baseline_when_ml_is_down() -> None:
    with client(clock_start="2026-01-06T08:05:00", state_tick_s=0.05, predict_tick_s=0.05) as c:
        wait_for(lambda: c.get("/health").json()["predictor"]["predictions_fallback"] >= 1)
        h = c.get("/health").json()
        body = c.get("/predictions").json()
    assert h["status"] == "degraded" and any("ML service unavailable" in i for i in h["issues"])
    assert h["predictor"]["ml_available"] is False and h["predictor"]["horizon_ok_share"] == 1.0
    p = body["vehicles"]["115106"]
    assert (p["source"], p["target_name"], p["horizon_ok"]) == ("fallback", "Остановка В, обратный рейс", True)
    assert 600 < p["lead_s"] <= 900                                  # T is a few ms past 08:05, target 08:20
    assert p["delay_pred_s"] == p["cur_dev_s"] == 0.0 and p["data_status"] == "fallback"
