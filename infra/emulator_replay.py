"""Проигрывает реальные треки ТС из датасета через NDTP-эмулятор (для живого демо на осмысленных данных).

В режиме ``autoGenerate`` эмулятор шлёт случайное блуждание около Москвы — оно не лежит на маршрутах, и прогноз по
нему не содержателен. Этот скрипт раз в ``--interval`` секунд перезаливает конфиг эмулятора (``POST /api/config``)
с **явными** координатами каждого ТС из ``validate/traffic.csv`` на текущий момент «датасетного» времени. Эмулятор
по TCP шлёт это backend'у как обычные NDTP-пакеты.

Часы: датасетное время = ``--start`` + (сейчас − момент запуска) × ``--speed``. По умолчанию ``--start`` — текущее
время суток 6 января 2026, ``--speed 1``: тогда backend сопоставляет пакет (``timestamp`` эмулятора = текущее UTC)
с расписанием простым правилом «UTC → МСК (+3 ч), дата → 2026-01-06». Смещение пишется в ``--clock-out`` (JSON).

Особенности эмулятора (проверено на образе ``ndtp-telemetry-emulator:1.0``):

* поля ячейки задаются **внутри** ``"fields"``: ``{"type": "G6CellNav00", "fields": {"latitude": ..., ...}}``;
  поля на верхнем уровне ячейки эмулятор молча отбрасывает и шлёт нули с ``valid = false``;
* ``latitude``/``longitude`` — целые, градусы × 1e7; знак — биты ``extraDopBit5`` (N) и ``extraDopBit6`` (E);
* ``unitId`` = ``unit_id`` из ``traffic.csv`` — по нему backend находит ``tr_id``.

Запуск (эмулятор уже поднят и видит NDTP-сервер backend'а)::

    python infra/emulator_replay.py --dataset ./dataset --emu-url http://localhost:18080 \\
        --target-host host.docker.internal --target-port 9201
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

DATASET_DAY = pd.Timestamp("2026-01-06")
MSK = timezone(timedelta(hours=3))


def load_tracks(dataset: Path, tr_ids: list[int] | None) -> dict[int, dict]:
    t = pd.read_csv(dataset / "validate" / "traffic.csv", parse_dates=["event_time"], low_memory=False)
    real = set(pd.read_csv(dataset / "validate" / "schedule_plan.csv", usecols=["tr_id"]).tr_id)
    t = t[t.tr_id.isin(tr_ids or real)]
    valid = t["location_valid"].astype(str).str.lower().eq("true") & t.lat.gt(1) & t.lon.gt(1)
    t = t[valid].sort_values(["tr_id", "event_time"])
    return {int(k): {"unit_id": int(g.unit_id.iloc[0]), "ts": g.event_time.values, "lat": g.lat.to_numpy(),
                     "lon": g.lon.to_numpy(), "speed": g.speed.fillna(0).to_numpy(), "course": g.heading.fillna(0).to_numpy()}
            for k, g in t.groupby("tr_id")}


def nav_cell(tr: dict, at: np.datetime64, max_age_s: int) -> dict:
    """Ячейка G6CellNav00 на момент ``at``: последняя точка трека не старше ``max_age_s``, иначе «нет координат»."""
    i = np.searchsorted(tr["ts"], at, side="right") - 1
    if i < 0 or (at - tr["ts"][i]) / np.timedelta64(1, "s") > max_age_s:
        return {"type": "G6CellNav00", "fields": {"extraDopBit5": True, "extraDopBit6": True, "extraDopBit7": False}}
    return {"type": "G6CellNav00", "fields": {
        "latitude": int(round(tr["lat"][i] * 1e7)), "longitude": int(round(tr["lon"][i] * 1e7)),
        "extraDopBit5": True, "extraDopBit6": True, "extraDopBit7": True,
        "speedAvg": int(tr["speed"][i]), "speedMax": int(tr["speed"][i]), "course": int(tr["course"][i]) % 361,
        "altitude": 150, "nsat": 12, "pdop": 10}}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=Path("./dataset"))
    ap.add_argument("--emu-url", default="http://localhost:18080")
    ap.add_argument("--target-host", default="host.docker.internal", help="где слушает NDTP-сервер backend'а")
    ap.add_argument("--target-port", type=int, default=9201)
    ap.add_argument("--interval", type=float, default=5.0, help="секунд между обновлениями координат")
    ap.add_argument("--start", default=None, help="датасетное время старта, HH:MM (по умолчанию — текущее МСК)")
    ap.add_argument("--speed", type=float, default=1.0, help="ускорение датасетных часов (1 — реальное время)")
    ap.add_argument("--tr", type=int, nargs="*", default=None, help="какие ТС (по умолчанию все 13 реальных)")
    ap.add_argument("--duration", type=float, default=0, help="секунд работы (0 — пока не остановят)")
    ap.add_argument("--max-age", type=int, default=60, help="точка трека старше — считаем, что координат нет")
    ap.add_argument("--clock-out", type=Path, default=Path("emulator_clock.json"))
    ap.add_argument("--clock-url", default=None,
                    help="URL бэкенда (напр. https://api.mowtransit.ru): взять датасетные часы из его GET /clock, "
                         "чтобы треки шли ровно по шкале бэкенда (--start и --speed тогда игнорируются)")
    args = ap.parse_args()

    tracks = load_tracks(args.dataset, args.tr)
    if args.clock_url:
        backend_clock = httpx.get(args.clock_url.rstrip("/") + "/clock", timeout=10).raise_for_status().json()
        ds_start = pd.Timestamp(backend_clock["anchor_dataset"])
        wall_start = float(backend_clock["anchor_wall"])
        args.speed = float(backend_clock["speed"])
    else:
        now_msk = datetime.now(MSK)
        hh, mm = (map(int, args.start.split(":")) if args.start else (now_msk.hour, now_msk.minute))
        ds_start = DATASET_DAY + pd.Timedelta(hours=hh, minutes=mm)
        wall_start = time.time()
    clock = {"wall_start_utc": datetime.fromtimestamp(wall_start, timezone.utc).isoformat(),
             "dataset_start": ds_start.isoformat(), "speed": args.speed,
             "rule": "dataset_time = dataset_start + (packet_utc - wall_start_utc) * speed",
             "units": {str(v["unit_id"]): k for k, v in tracks.items()}}
    args.clock_out.write_text(json.dumps(clock, ensure_ascii=False, indent=2))
    print(f"ТС: {len(tracks)} | датасетные часы стартуют с {ds_start} ×{args.speed} | сопоставление -> {args.clock_out}")

    client = httpx.Client(base_url=args.emu_url, timeout=10)
    run_start = time.time()  # --duration считается от запуска скрипта, а не от якоря часов
    try:
        while True:
            elapsed = time.time() - wall_start
            if args.duration and time.time() - run_start > args.duration:
                break
            at = np.datetime64(ds_start + pd.Timedelta(seconds=elapsed * args.speed))
            units = [{"unitId": tr["unit_id"], "intervalMs": int(args.interval * 1000), "autoGenerate": False,
                      "cells": [nav_cell(tr, at, args.max_age)]} for tr in tracks.values()]
            r = client.post("/api/config", json={"targetHost": args.target_host, "targetPort": args.target_port,
                                                  "units": units})
            live = sum(u["cells"][0]["fields"].get("extraDopBit7", False) for u in units)
            print(f"{pd.Timestamp(at):%H:%M:%S} датасета | HTTP {r.status_code} | с координатами {live}/{len(units)}", flush=True)
            time.sleep(max(0.0, args.interval - (time.time() - wall_start - elapsed)))
    finally:
        client.post("/api/config", json={"targetHost": args.target_host, "targetPort": args.target_port, "units": []})
        print("эмуляция остановлена")


if __name__ == "__main__":
    main()
