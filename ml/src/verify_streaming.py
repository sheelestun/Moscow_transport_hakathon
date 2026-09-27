"""Прогон validate через онлайн-контур (build_features_online + CatBoost-ансамбль).

Задача — доказать, что submission.csv можно воспроизвести стриминг-путём, а не только батчем.
Для каждой точки из validate/points.csv:

  1) фильтруем validate/traffic.csv по event_time ≤ T и tr_id (эмулируем историю пингов до T),
  2) берём слайс validate/schedule_plan.csv по tr_id,
  3) вызываем features/from_stream.build_features_online и предиктим ансамблем.

Затем сравниваем с текущим submission.csv поточечно. Если max|Δ| < 1e-3 сек — контур эквивалентен
батчу и решение честно работает в стриминг-режиме.

Запуск::

    python ml/src/verify_streaming.py --dataset ./dataset --submission ./submission.csv
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostRegressor, Pool

sys.path.insert(0, str(Path(__file__).resolve().parent))

from features.from_stream import build_features_online  # noqa: E402
from features.tabular import CAT_FEATURES  # noqa: E402


def load_models(art: Path) -> tuple[list[CatBoostRegressor], dict]:
    meta = json.loads((art / "catboost_meta.json").read_text())
    models = []
    for i in range(meta["n_models"]):
        m = CatBoostRegressor()
        m.load_model(str(art / f"catboost_seed{i}.cbm"))
        models.append(m)
    return models, meta


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=Path("./dataset"))
    ap.add_argument("--submission", type=Path, default=Path("./submission.csv"))
    ap.add_argument("--artifacts", type=Path, default=Path(__file__).resolve().parents[1] / "artifacts")
    ap.add_argument("--out", type=Path, default=Path("./submission_streaming.csv"))
    args = ap.parse_args()

    print(f"[load] models from {args.artifacts}")
    models, meta = load_models(args.artifacts)
    feats = meta["features"]
    cats = [c for c in CAT_FEATURES if c in feats]

    val = args.dataset / "validate"
    points = pd.read_csv(val / "points.csv", parse_dates=["T", "target_time_begin"])
    traffic = pd.read_csv(val / "traffic.csv", parse_dates=["event_time"])
    schedule = pd.read_csv(val / "schedule_plan.csv", parse_dates=["time_begin"])
    print(f"[load] points={len(points)} traffic={len(traffic)} schedule={len(schedule)}")

    ref = pd.read_csv(args.submission, sep=";").set_index("sample_id")["prediction"]

    traffic_by_tr = {tr: g for tr, g in traffic.groupby("tr_id")}
    schedule_by_tr = {tr: g for tr, g in schedule.groupby("tr_id")}

    preds = []
    for i, row in points.iterrows():
        tr = row["tr_id"]
        T = row["T"]
        tr_traffic = traffic_by_tr.get(tr, pd.DataFrame(columns=traffic.columns))
        tel = tr_traffic[tr_traffic["event_time"] <= T]
        sched = schedule_by_tr.get(tr, pd.DataFrame(columns=schedule.columns))
        sample = {
            "sample_id": row["sample_id"],
            "tr_id": int(tr),
            "T": row["T"],
            "target_stop_id": int(row["target_stop_id"]),
            "target_time_begin": row["target_time_begin"],
            "cur_dev_s": float(row["cur_dev_s"]),
        }
        X = build_features_online(sample, tel.to_dict("records"), sched.to_dict("records"))
        pool = Pool(X[feats], cat_features=cats)
        residual = float(np.mean([m.predict(pool)[0] for m in models]))
        preds.append(residual + sample["cur_dev_s"])
        if (i + 1) % 20 == 0:
            print(f"  {i+1}/{len(points)}")

    out = pd.DataFrame({"sample_id": points["sample_id"], "prediction": np.round(preds, 1)})
    out.to_csv(args.out, sep=";", index=False)

    aligned = out.set_index("sample_id")["prediction"].reindex(ref.index)
    diff = (aligned - ref).abs()
    print(f"\n[compare] n={len(diff)} max|Δ|={diff.max():.4f} mean|Δ|={diff.mean():.4f}")
    if diff.max() < 1e-3:
        print("[OK] стриминг-контур бит-в-бит воспроизводит батч → submission.csv честный")
    else:
        print(f"[MISMATCH] есть расхождение — топ-5:")
        print(diff.sort_values(ascending=False).head())


if __name__ == "__main__":
    main()
