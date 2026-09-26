"""CSV-replayer — фолбэк для NDTP-парсинга (ARCHITECTURE_AND_ROLES.md §13).

Читает dataset/{split}/{traffic,schedule*,points|labels}.csv, воспроизводит события во
времени по колонке T и вызывает POST /predict для каждой точки. Полученные предикты
складывает в CSV (`sample_id;prediction_sec;risk_score;reason_pattern`).

Пример запуска::

    # Демо на validate, ускорение x60 (минута датасета == 1 секунда wall-clock),
    # ML-сервис на 8001:
    python backend/src/csv_replayer.py --dataset ./dataset --split validate --speed 60

    # Быстрый dry-run без похода в ML:
    python backend/src/csv_replayer.py --dataset ./dataset --split validate \\
        --speed 3600 --dry-run
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd


TELEMETRY_COLS = ["tr_id", "event_time", "lon", "lat", "speed", "location_valid", "is_hist_data"]
SCHEDULE_COLS = ["tr_id", "tt_action_item_id", "time_begin", "geom", "manual_fill"]


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_split(dataset: Path, split: str):
    traffic = pd.read_csv(dataset / split / "traffic.csv", parse_dates=["event_time"])
    sched_path = dataset / split / ("schedule_plan.csv" if split == "validate" else "schedule.csv")
    schedule = pd.read_csv(sched_path, parse_dates=["time_begin"])
    if split == "validate":
        points = pd.read_csv(dataset / split / "points.csv", parse_dates=["T", "target_time_begin"])
    else:
        points = pd.read_csv(dataset / "labels" / f"labels_{split}.csv",
                             parse_dates=["T", "target_time_begin"])
    return points, traffic, schedule


def _reason_pattern(top_features: list[dict]) -> str:
    if not top_features:
        return "n/a"
    ranked = sorted(top_features, key=lambda f: abs(float(f.get("value", 0.0))), reverse=True)
    return ",".join(f["name"] for f in ranked[:3])


def _build_payload(point, traffic_buf: pd.DataFrame, sched_slice: pd.DataFrame) -> dict:
    tele = traffic_buf[[c for c in TELEMETRY_COLS if c in traffic_buf.columns]].copy()
    tele["event_time"] = tele["event_time"].dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    tele["location_valid"] = tele["location_valid"].astype(str).str.lower()
    tele["is_hist_data"] = tele["is_hist_data"].astype(bool).astype(int)

    sch = sched_slice[[c for c in SCHEDULE_COLS if c in sched_slice.columns]].copy()
    sch["time_begin"] = sch["time_begin"].dt.strftime("%Y-%m-%d %H:%M:%S")
    sch["manual_fill"] = sch["manual_fill"].astype(str).str.lower()

    return {
        "sample_id": str(point["sample_id"]),
        "tr_id": int(point["tr_id"]),
        "T": point["T"].strftime("%Y-%m-%d %H:%M:%S"),
        "target_stop_id": int(point["target_stop_id"]),
        "target_time_begin": point["target_time_begin"].strftime("%Y-%m-%d %H:%M:%S"),
        "cur_dev_s": float(point["cur_dev_s"]) if pd.notna(point["cur_dev_s"]) else 0.0,
        "telemetry": tele.to_dict(orient="records"),
        "schedule": sch.to_dict(orient="records"),
    }


def _wait(target_dt: pd.Timestamp, epoch_dt: pd.Timestamp, wall_start: float, speed: float) -> None:
    dataset_elapsed = (target_dt - epoch_dt).total_seconds()
    wall_target = wall_start + dataset_elapsed / max(speed, 1e-6)
    dt = wall_target - time.monotonic()
    if dt > 0:
        time.sleep(dt)


def main() -> None:
    ap = argparse.ArgumentParser(description="CSV-replayer для онлайн-контура")
    ap.add_argument("--dataset", type=Path, default=Path("./dataset"))
    ap.add_argument("--split", choices=["validate", "test", "train"], default="validate")
    ap.add_argument("--ml-url", default="http://localhost:8001")
    ap.add_argument("--speed", type=float, default=60.0)
    ap.add_argument("--from", dest="from_ts", default=None)
    ap.add_argument("--out", type=Path, default=Path("replay_predictions.csv"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    _log(f"loading split={args.split} from {args.dataset}")
    points, traffic, schedule = _load_split(args.dataset, args.split)
    points = points.sort_values("T").reset_index(drop=True)
    _log(f"points={len(points)} traffic={len(traffic)} schedule={len(schedule)}")

    epoch_dt = pd.to_datetime(args.from_ts) if args.from_ts else points["T"].iloc[0]
    points = points[points["T"] >= epoch_dt].reset_index(drop=True)
    _log(f"epoch={epoch_dt}  playing {len(points)} points at speed x{args.speed}")

    traffic_by_tr = {tr: g.sort_values("event_time") for tr, g in traffic.groupby("tr_id")}
    schedule_by_tr = {tr: g.sort_values("time_begin") for tr, g in schedule.groupby("tr_id")}

    results: list[dict] = []
    wall_start = time.monotonic()
    client = None if args.dry_run else httpx.Client(timeout=5.0)

    try:
        for _, point in points.iterrows():
            tr_id = int(point["tr_id"])
            tr_traffic = traffic_by_tr.get(tr_id)
            if tr_traffic is None or tr_traffic.empty:
                _log(f"WARN sample_id={point['sample_id']} tr_id={tr_id}: пустая телеметрия, skip")
                continue
            traffic_buf = tr_traffic[tr_traffic["event_time"] <= point["T"]]
            if traffic_buf.empty:
                _log(f"WARN sample_id={point['sample_id']} tr_id={tr_id}: буфер пуст на T={point['T']}, skip")
                continue
            sched_slice = schedule_by_tr.get(tr_id, schedule.iloc[0:0])

            payload = _build_payload(point, traffic_buf, sched_slice)
            _wait(point["T"], epoch_dt, wall_start, args.speed)

            if args.dry_run:
                preview = {**payload, "telemetry": f"<{len(payload['telemetry'])} pings>",
                           "schedule": f"<{len(payload['schedule'])} stops>"}
                _log(f"DRY sample_id={payload['sample_id']} T={payload['T']} "
                     f"tr_id={payload['tr_id']} payload={json.dumps(preview, ensure_ascii=False)}")
                continue

            try:
                r = client.post(f"{args.ml_url}/predict", json=payload)
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                _log(f"ERR sample_id={payload['sample_id']}: {e}")
                continue

            reason = _reason_pattern(data.get("top_features", []))
            results.append({
                "sample_id": data["sample_id"],
                "prediction_sec": data["delay_pred_sec"],
                "risk_score": data["risk_score"],
                "reason_pattern": reason,
            })
            _log(f"sample_id={data['sample_id']} delay={data['delay_pred_sec']:.1f}s "
                 f"risk={data['risk_score']:.2f} reason={reason}")
    finally:
        if client is not None:
            client.close()

    if not args.dry_run:
        with args.out.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["sample_id", "prediction_sec", "risk_score", "reason_pattern"],
                               delimiter=";")
            w.writeheader()
            w.writerows(results)
        _log(f"wrote {len(results)} predictions -> {args.out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
