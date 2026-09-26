"""App wiring: lifespan starts NDTP, fixes reach the diagnostics endpoint, health reports status."""

from __future__ import annotations

import socket
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


def client(**overrides) -> TestClient:
    return TestClient(create_app(Settings(_env_file=None, ndtp_host="127.0.0.1", ndtp_port=0, **overrides)))


def test_health_and_swagger() -> None:
    with client() as c:
        h = c.get("/health").json()
        assert h["status"] == "ok"
        assert h["ndtp"]["listening"] and h["ndtp"]["port"] > 0
        assert h["ndtp"]["last_fix_age_s"] is None
        assert c.get("/openapi.json").status_code == 200
        assert c.get("/docs").status_code == 200


def test_emulator_stream_reaches_diagnostics() -> None:
    with client() as c:
        port = c.get("/health").json()["ndtp"]["port"]
        with socket.create_connection(("127.0.0.1", port)) as s:
            s.sendall((FIXTURES / "emulator_explicit_moscow.bin").read_bytes())
            deadline = time.time() + 2
            while c.get("/health").json()["ndtp"]["fixes_total"] < 5 and time.time() < deadline:
                time.sleep(0.02)
            h = c.get("/health").json()["ndtp"]
            assert (h["fixes_total"], h["units_connected"], h["fixes_dropped"]) == (5, 1, 0)
            assert h["last_fix_age_s"] is not None
            diag = c.get("/ingest/ndtp").json()
        fix = diag["last_fix"]["664030"]
        assert (fix["lat"], fix["lon"], fix["valid"], fix["request_id"]) == (55.7551234, 37.617321, True, 6)
        assert diag["stats"]["handshakes"] == 1 and diag["framing"]["crc_errors"] == 0


def test_ndtp_can_be_disabled() -> None:
    with client(ndtp_enabled=False) as c:
        h = c.get("/health").json()
        assert h["status"] == "ok" and h["ndtp"]["enabled"] is False
        assert c.get("/ingest/ndtp").json() == {"enabled": False}
