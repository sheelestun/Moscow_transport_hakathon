"""FastAPI backend: диспетчерский шлюз между ML-сервисом и фронтом.

Контракт вход/выход — `frontend/js/api.js` и `frontend/js/mock.js` (source of truth
для формата, чтобы live-режим совпадал с mock один в один).

Endpoints:
  GET  /health                    статус
  GET  /routes                    список маршрутов (геометрия + остановки)
  GET  /vehicles                  все ТС в текущий момент
  GET  /vehicles/{id}/schedule    расписание конкретного рейса
  GET  /alerts?active=true        активные алерты
  GET  /metrics/model             прокси на ML /metrics/model (с fallback)
  POST /whatif                    сценарий по маршруту: агрегат для дашборда
  WS   /ws                        push: vehicle.update / alert.new / alert.verified / whatif.result

ML-сервис вызывается точечно: `/whatif` пробрасывается пачкой в ML `/whatif/predict`
(если сервис недоступен — используется локальная эвристика симулятора, MEASURE_EFFECT
как во фронт-моке). Симуляция самих движений и delay_now — в `simulator.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from simulator import REC_FOR_REASON, RISK_RED, Simulator, Vehicle, _iso_ms, risk_from_delay  # noqa: E402


ML_URL = os.environ.get("ML_URL", "http://ml:8001").rstrip("/")
TICK_S = float(os.environ.get("BACKEND_TICK_S", 1.0))
SPEED_FACTOR = float(os.environ.get("BACKEND_SPEED_FACTOR", 5.0))
ALERT_MODEL_VERSION = os.environ.get("ML_MODEL_VERSION", "catboost-ensemble-v1")

app = FastAPI(
    title="Moscow Transport Backend",
    version="0.1.0",
    description="Диспетчерский шлюз между NDTP-эмулятором/фронтом и ML-сервисом.",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

SIM = Simulator(speed_factor=SPEED_FACTOR)
ALERTS: dict[str, dict] = {}
_alert_seq = 91000
CLIENTS: set[WebSocket] = set()
LAST_METRICS: dict = {}
_bg_task: Optional[asyncio.Task] = None


# ------------------------------------------------------------------ схемы

class WhatifRequest(BaseModel):
    scenario: str = Field(..., description="add_reserve|adjust_interval|detour|signal_priority|hold_at_stop")
    route_id: str
    at_stop_id: Optional[str] = None
    apply: bool = Field(False, description="если true — применить эффект в симуляции")


class ApplyRequest(BaseModel):
    scenario: str
    route_id: str


# ------------------------------------------------------------------ ML клиент

_http: Optional[httpx.AsyncClient] = None


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
    return _http


async def _ml_get(path: str) -> Optional[dict]:
    try:
        r = await _client().get(f"{ML_URL}{path}")
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


# ------------------------------------------------------------------ alerts

def _make_alert(v: Vehicle) -> dict:
    global _alert_seq
    sch = SIM.get_schedule(v.vehicle_id)
    target = next((s for s in sch["stops"] if s.get("is_target")), None) or sch["stops"][-1]
    _alert_seq += 1
    now_ms = SIM.now_ms()
    return {
        "type": "alert.new",
        "alert_id": f"a-{_alert_seq}",
        "vehicle_id": v.vehicle_id,
        "route_id": v.route_id,
        "target_stop_id": target["stop_id"],
        "target_stop_name": target["name"],
        "delay_pred_sec": target.get("delay_sec", v.delay_pred_s),
        "risk_score": v.risk_score,
        "confidence": v.confidence,
        "eta_incident": target.get("time_pred") or target.get("time_plan"),
        "reason_pattern": v.reason,
        "recommendation": REC_FOR_REASON.get(v.reason, "monitor"),
        "top_features": v.features,
        "model_version": ALERT_MODEL_VERSION,
        "created_at": _iso_ms(now_ms),
        "_trip": v.trip_id,
    }


def _refresh_alerts(events: list[dict]) -> None:
    now_ms = SIM.now_ms()
    # сверка alert.verified — если время инцидента прошло
    to_verify: list[str] = []
    for aid, a in list(ALERTS.items()):
        eta = a.get("eta_incident")
        if not eta:
            continue
        try:
            import datetime as _dt
            eta_ms = int(_dt.datetime.fromisoformat(eta.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            continue
        if eta_ms > now_ms:
            continue
        v = SIM.by_id.get(a["vehicle_id"])
        if v is None or v.trip_id != a["_trip"]:
            # рейс уже сменился — просто снимаем алерт (фактическую верификацию не делаем)
            ALERTS.pop(aid, None)
            events.append({"type": "alert.resolved", "alert_id": aid})
            continue
        sch = SIM.get_schedule(v.vehicle_id)
        st = next((s for s in sch["stops"] if s["stop_id"] == a["target_stop_id"]), None)
        if st is None or "delay_sec" not in st:
            ALERTS.pop(aid, None)
            events.append({"type": "alert.resolved", "alert_id": aid})
            continue
        events.append({
            "type": "alert.verified",
            "alert_id": aid,
            "vehicle_id": a["vehicle_id"],
            "route_id": a["route_id"],
            "target_stop_id": a["target_stop_id"],
            "target_stop_name": a["target_stop_name"],
            "delay_pred_sec": a["delay_pred_sec"],
            "delay_fact_sec": st["delay_sec"],
            "verified_at": _iso_ms(now_ms),
        })
        to_verify.append(aid)
    for aid in to_verify:
        ALERTS.pop(aid, None)

    # новые алерты: risk >= RED, один на (trip, target_stop)
    for v in SIM.vehicles:
        if v.risk_score < RISK_RED:
            continue
        already = any(a["vehicle_id"] == v.vehicle_id and a["_trip"] == v.trip_id
                      for a in ALERTS.values())
        if already:
            continue
        try:
            sch = SIM.get_schedule(v.vehicle_id)
        except KeyError:
            continue
        target = next((s for s in sch["stops"] if s.get("is_target")), None)
        if target is None:
            continue
        key = (v.vehicle_id, v.trip_id, target["stop_id"])
        if key in v.alerted_stops:  # type: ignore[operator]
            continue
        pred_risk = round(risk_from_delay(target.get("delay_sec", v.delay_pred_s)), 3)
        if pred_risk < RISK_RED:
            continue
        a = _make_alert(v)
        a["risk_score"] = pred_risk
        ALERTS[a["alert_id"]] = a
        v.alerted_stops.add(key)  # type: ignore[arg-type]
        events.append(a)


# ------------------------------------------------------------------ фоновой цикл

async def _sim_loop() -> None:
    """Тик каждый TICK_S. Обновляет симулятор, публикует vehicle.update и алерты."""
    last = time.time()
    while True:
        try:
            real = time.time()
            left = min(5.0, (real - last)) * SIM.speed_factor
            last = real
            while left > 0:
                dt = min(2.0, left)
                SIM.step(dt)
                left -= dt
            events: list[dict] = []
            _refresh_alerts(events)
            update = {"type": "vehicle.update", "vehicles": SIM.get_vehicles()}
            await _broadcast([update, *events])
        except Exception as e:  # noqa: BLE001
            print(f"[bg] error: {e}", flush=True)
        await asyncio.sleep(TICK_S)


async def _broadcast(msgs: list[dict]) -> None:
    if not CLIENTS or not msgs:
        return
    payloads = [json.dumps(m) for m in msgs]
    dead: list[WebSocket] = []
    for ws in list(CLIENTS):
        try:
            for p in payloads:
                await ws.send_text(p)
        except Exception:
            dead.append(ws)
    for ws in dead:
        CLIENTS.discard(ws)


# ------------------------------------------------------------------ жизненный цикл

@app.on_event("startup")
async def _on_startup() -> None:
    global _bg_task
    LAST_METRICS.update(await _ml_get("/metrics/model") or {})
    _bg_task = asyncio.create_task(_sim_loop())


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    if _bg_task:
        _bg_task.cancel()
        # CancelledError не Exception в 3.8+ — глотаем через BaseException,
        # иначе TestClient падает на выходе с ошибкой отмены фоновой задачи
        with contextlib.suppress(BaseException):
            await _bg_task
    if _http is not None:
        await _http.aclose()


# ------------------------------------------------------------------ endpoints

@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "vehicles": len(SIM.vehicles),
        "alerts": len(ALERTS),
        "clients": len(CLIENTS),
        "ml_url": ML_URL,
        "speed_factor": SIM.speed_factor,
    }


@app.get("/routes")
def get_routes() -> list[dict]:
    return SIM.get_routes()


@app.get("/routes/{route_id}/signals")
def get_signals(route_id: str) -> list[dict]:
    return SIM.get_signals(route_id)


@app.get("/vehicles")
def get_vehicles() -> list[dict]:
    return SIM.get_vehicles()


@app.get("/vehicles/{vehicle_id}/schedule")
def get_schedule(vehicle_id: str) -> dict:
    try:
        return SIM.get_schedule(vehicle_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"vehicle {vehicle_id} not found")


@app.get("/alerts")
def get_alerts(active: bool = Query(True)) -> list[dict]:
    return list(ALERTS.values()) if active else []


@app.get("/metrics/model")
async def metrics_model() -> dict:
    ml_metrics = await _ml_get("/metrics/model")
    if ml_metrics:
        LAST_METRICS.update(ml_metrics)
    return LAST_METRICS or {
        "mae_test_s": None, "latency_ms_p50": None,
        "model_version": f"{ALERT_MODEL_VERSION} (ml offline)",
    }


@app.get("/metrics/worst_stops")
def worst_stops(limit: int = Query(10, ge=1, le=50)) -> list[dict]:
    return SIM.get_worst_stops(limit=limit)


@app.get("/metrics/bunching")
def metrics_bunching() -> list[dict]:
    """Пары ТС, идущие «паровозиком» на одном маршруте/направлении."""
    return SIM.get_bunching()


@app.post("/whatif")
async def whatif(req: WhatifRequest) -> dict:
    """Сценарий по маршруту. Для каждого ТС считаем прогноз до/после.

    Пробуем /whatif/predict в ML для каждого ТС; на любой сбой — fallback на
    локальную эвристику `Simulator.measure_effect` (MEASURE_EFFECT из mock.js).
    Один и тот же shape ответа для мока и live-режима.
    """
    vehicles_in_route = [v for v in SIM.vehicles if v.route_id == req.route_id and not v.is_reserve]
    if not vehicles_in_route:
        return {"type": "whatif.result", "scenario": req.scenario, "route_id": req.route_id,
                "at_stop_id": req.at_stop_id,
                "summary": {"avg_delay_before_sec": 0, "avg_delay_after_sec": 0,
                            "red_before": 0, "red_after": 0},
                "vehicles": []}

    # локальный fallback + опционально ML
    result_vehicles: list[dict] = []
    for v in vehicles_in_route:
        before = v.delay_pred_s
        after = SIM.measure_effect(v, req.scenario)
        result_vehicles.append({
            "vehicle_id": v.vehicle_id,
            "delay_before_sec": before,
            "delay_after_sec": after,
            "risk_before": round(risk_from_delay(before), 3),
            "risk_after": round(risk_from_delay(after), 3),
        })
    # (best-effort) уточним через ML, если доступен
    ml_ok = await _ml_get("/health")
    if ml_ok is not None:
        await _refine_whatif_via_ml(vehicles_in_route, req.scenario, result_vehicles)

    if req.apply:
        SIM.apply_measure(req.route_id, req.scenario)

    resp = {
        "type": "whatif.result", "scenario": req.scenario, "route_id": req.route_id,
        "at_stop_id": req.at_stop_id,
        "summary": {
            "avg_delay_before_sec": round(sum(x["delay_before_sec"] for x in result_vehicles) / len(result_vehicles)),
            "avg_delay_after_sec": round(sum(x["delay_after_sec"] for x in result_vehicles) / len(result_vehicles)),
            "red_before": sum(1 for x in result_vehicles if x["risk_before"] >= RISK_RED),
            "red_after": sum(1 for x in result_vehicles if x["risk_after"] >= RISK_RED),
        },
        "vehicles": result_vehicles,
    }
    await _broadcast([resp])
    return resp


async def _refine_whatif_via_ml(vehicles: list[Vehicle], scenario: str, out: list[dict]) -> None:
    """Если ML жив — просим точечный /whatif/predict для каждого ТС и подменяем цифры.

    Полный PredictRequest мы отсюда не сформируем (нет буфера NDTP-телеметрии и
    планового расписания в формате schedule.csv), поэтому шлём **минимальный
    контекст** и полагаемся на `SCHEDULE_PATH` внутри ML-сервиса. Если контракт
    строгий и вернётся 422 — просто оставляем локальный fallback.
    """
    tasks = []
    for v in vehicles:
        payload = {
            "sample_id": f"{v.vehicle_id}_{SIM.now_ms() // 1000}",
            "tr_id": int(v.vehicle_id) if v.vehicle_id.isdigit() else 0,
            "T": _iso_ms(SIM.now_ms()),
            "target_stop_id": 0,
            "target_time_begin": _iso_ms(SIM.now_ms() + 12 * 60 * 1000),
            "cur_dev_s": float(v.delay_now_s),
            "telemetry": [],
            "schedule": [],
            "scenario": scenario,
        }
        tasks.append(_client().post(f"{ML_URL}/whatif/predict", json=payload))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for row, r in zip(out, results):
        if isinstance(r, Exception):
            continue
        try:
            if r.status_code != 200:
                continue
            j = r.json()
            row["delay_before_sec"] = round(j.get("delay_baseline_sec", row["delay_before_sec"]))
            row["delay_after_sec"] = round(j.get("delay_scenario_sec", row["delay_after_sec"]))
            row["risk_before"] = round(j.get("risk_baseline", row["risk_before"]), 3)
            row["risk_after"] = round(j.get("risk_scenario", row["risk_after"]), 3)
        except Exception:
            pass


@app.post("/apply")
async def apply_measure(req: ApplyRequest) -> dict:
    return SIM.apply_measure(req.route_id, req.scenario)


# ------------------------------------------------------------------ WebSocket

@app.websocket("/ws")
async def ws(ws: WebSocket) -> None:
    await ws.accept()
    CLIENTS.add(ws)
    # прогрев: одна пачка со всеми ТС + активными алертами
    try:
        await ws.send_text(json.dumps({"type": "vehicle.update", "vehicles": SIM.get_vehicles()}))
        for a in ALERTS.values():
            await ws.send_text(json.dumps(a))
        while True:
            # держим коннект — клиенту не нужно ничего слать
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        CLIENTS.discard(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
