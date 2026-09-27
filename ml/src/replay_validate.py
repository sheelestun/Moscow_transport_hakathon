"""Проигрывает точки validate через ML-сервис так, как это делает backend, и сверяет с батчем.

Для каждой точки (tr_id, T) собирает запрос: телеметрия ТС за последние ``--history-min`` минут до T
(только ``event_time <= T``), целевая остановка, ``cur_dev_s``. Проверяет:

* онлайн-прогноз совпадает с ``submission.csv`` (одна функция признаков в батче и онлайн);
* задержку ответа (latency) по одной точке и пачкой.

Запуск (сервис поднимается в процессе, HTTP не нужен)::

    python ml/src/replay_validate.py --dataset ./dataset --submission submission.csv
    python ml/src/replay_validate.py --url http://localhost:8001 ...   # против запущенного сервиса
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))


def build_requests(dataset: Path, history_min: int = 90, send_schedule: bool = False,
                   cur_dev_mode: str = "official") -> list[dict]:
    """Запросы в формате контракта сервиса: пакеты телеметрии ТС за ``history_min`` минут до T (0 — все до T).

    ``send_schedule`` — передавать плановое расписание ТС в запросе (как backend/csv_replayer); иначе сервис
    берёт план из SCHEDULE_PATH.
    """
    pts = pd.read_csv(dataset / "validate" / "points.csv", parse_dates=["T", "target_time_begin"])
    tr = pd.read_csv(dataset / "validate" / "traffic.csv", parse_dates=["event_time"], low_memory=False)
    by_tr = {k: g for k, g in tr.groupby("tr_id")}
    plan = {}
    if send_schedule:
        sp = pd.read_csv(dataset / "validate" / "schedule_plan.csv")
        sp = sp.astype(object).where(sp.notna(), None)
        plan = {int(k): [{"tr_id": int(r["tr_id"]), "tt_action_item_id": int(r["tt_action_item_id"]),
                          "time_begin": str(r["time_begin"]), "geom": r["geom"], "manual_fill": str(r["manual_fill"]),
                          "building_address": r["building_address"]} for _, r in g.iterrows()]
                for k, g in sp.groupby("tr_id")}
    reqs = []
    for r in pts.itertuples(index=False):
        g = by_tr.get(r.tr_id)
        tele = []
        if g is not None:
            w = g[g.event_time <= r.T]
            if history_min:
                w = w[w.event_time > r.T - pd.Timedelta(minutes=history_min)]
            for p in w.itertuples(index=False):
                tele.append({"tr_id": int(p.tr_id), "event_time": p.event_time.isoformat(),
                             "lat": None if pd.isna(p.lat) else float(p.lat),
                             "lon": None if pd.isna(p.lon) else float(p.lon),
                             "speed": None if pd.isna(p.speed) else float(p.speed),
                             "location_valid": str(p.location_valid).lower(), "is_hist_data": int(bool(p.is_hist_data))})
        reqs.append({"sample_id": r.sample_id, "tr_id": int(r.tr_id), "T": r.T.isoformat(),
                     "target_stop_id": int(r.target_stop_id), "target_time_begin": r.target_time_begin.isoformat(),
                     "cur_dev_s": float(r.cur_dev_s), "cur_dev_mode": cur_dev_mode, "telemetry": tele,
                     **({"schedule": plan.get(int(r.tr_id), [])} if send_schedule else {})})
    return reqs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=Path("./dataset"))
    ap.add_argument("--submission", type=Path, default=Path("submission.csv"))
    ap.add_argument("--url", default=None, help="адрес запущенного сервиса; по умолчанию — в процессе")
    ap.add_argument("--history-min", type=int, default=90)
    ap.add_argument("--send-schedule", action="store_true", help="передавать план ТС в запросе (как backend)")
    ap.add_argument("--cur-dev-mode", default="official", choices=["official", "gps"],
                    help="режим подсказки в запросе (для сверки с submission.csv — official)")
    ap.add_argument("--save-example", type=Path, default=None, help="сохранить пример запроса/ответа в JSON")
    args = ap.parse_args()

    import httpx

    reqs = build_requests(args.dataset, args.history_min, args.send_schedule, args.cur_dev_mode)
    url = args.url
    if url is None:  # поднимаем сервис локально в фоновом потоке (настоящий HTTP)
        import threading

        import uvicorn

        from inference_service import app
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        url = "http://127.0.0.1:8765"
        for _ in range(600):
            try:
                if httpx.get(url + "/health", timeout=1).json().get("models_loaded"):
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    client = httpx.Client(base_url=url, timeout=120)

    print("health:", client.get("/health").json())
    single, t_single = [], []
    for q in reqs:
        t0 = time.perf_counter()
        single.append(client.post("/predict", json=q).json())
        t_single.append((time.perf_counter() - t0) * 1000)
    t0 = time.perf_counter()
    batch = client.post("/predict/batch", json={"requests": reqs}).json()["responses"]
    t_batch = (time.perf_counter() - t0) * 1000

    on = pd.DataFrame([{"sample_id": r["sample_id"], "online": r["delay_pred_sec"], "status": r["data_status"],
                        "level": r["risk_level"]} for r in single])
    b = pd.DataFrame([{"sample_id": r["sample_id"], "batch_api": r["delay_pred_sec"]} for r in batch])
    sub = pd.read_csv(args.submission, sep=";").rename(columns={"prediction": "submission"})
    m = on.merge(b, on="sample_id").merge(sub, on="sample_id")
    diff = (m.online - m.submission).abs()
    print(f"точек: {len(m)} | |онлайн − submission|: медиана {diff.median():.2f} с, максимум {diff.max():.2f} с, "
          f"совпало до 1 с: {(diff <= 1).mean():.0%}")
    print(f"|/predict − /predict/batch|: максимум {(m.online - m.batch_api).abs().max():.3f} с")
    print(f"latency /predict (одна точка, с HTTP-обвязкой): p50 {np.percentile(t_single, 50):.0f} мс, "
          f"p95 {np.percentile(t_single, 95):.0f} мс")
    print(f"latency /predict/batch ({len(reqs)} точек): {t_batch:.0f} мс всего, {t_batch / len(reqs):.1f} мс на точку")
    print("статусы данных:", m.status.value_counts().to_dict(), "| светофор:", m.level.value_counts().to_dict())
    print("метрики сервиса:", {k: v for k, v in client.get("/metrics/model").json().items() if k in ("latency_ms_p50", "latency_ms_p95", "requests_served", "score_estimate")})
    if args.save_example:
        i = int(np.argmax([r["p_late"] for r in single]))
        ex_req = dict(reqs[i])
        ex_req["telemetry"] = ex_req["telemetry"][-5:]
        args.save_example.write_text(json.dumps({"request (телеметрия урезана до 5 пакетов)": ex_req,
                                                 "response": single[i]}, ensure_ascii=False, indent=2), encoding="utf-8")
        print("пример сохранён:", args.save_example)


if __name__ == "__main__":
    main()
