"""CatBoost-трек: признаки -> оценка по трём схемам -> финальная модель -> submission.csv.

Схемы валидации (данные — один день, 13 реальных ТС + 26 синтетических клонов в train):

* ``proxy`` — **как validate**: K-fold по реальным точкам train+test блоками по времени; клоны и остальные
  точки остаются в обучении. Validate устроен так же (соседние моменты T тех же ТС), поэтому это лучший
  прокси скора платформы. Дополнительно печатается классический holdout train -> test.
* ``lovo`` — **честная**: откладываем реальное ТС вместе с клонами. Качество на новом ТС / другом дне.

Запуск::

    python ml/src/train_catboost.py --dataset ./dataset --eval          # оценка
    python ml/src/train_catboost.py --dataset ./dataset --fit --out submission.csv
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor, Pool

sys.path.insert(0, str(Path(__file__).resolve().parent))

from features.tabular import CAT_FEATURES, FEATURES, build_features, clone_sources, load_split  # noqa: E402

ART = Path(__file__).resolve().parents[1] / "artifacts"

PARAMS = dict(iterations=1500, learning_rate=0.03, depth=6, loss_function="RMSE", l2_leaf_reg=3.0,
              verbose=0, thread_count=-1, allow_writing_files=False)


def mae(y, p) -> float:
    return float(np.mean(np.abs(np.asarray(y, float) - np.asarray(p, float))))


LEAD_BINS = [(0, 180, "0-3м"), (180, 300, "3-5м"), (300, 600, "5-10м"),
             (600, 900, "10-15м"), (900, float("inf"), "15м+")]


def mae_by_lead(y: pd.Series, p: pd.Series, lead_s: pd.Series) -> list[dict]:
    """MAE по бинам горизонта прогноза (lead_s = сколько сек до целевого момента).

    Диспетчер должен понимать, где модель точнее, а где — фактически «предупреждает
    заранее». Обычно MAE растёт с горизонтом; это надо видеть, а не усреднять.
    """
    y = np.asarray(y, float); p = np.asarray(p, float); lead = np.asarray(lead_s, float)
    out: list[dict] = []
    for lo, hi, label in LEAD_BINS:
        m = (lead >= lo) & (lead < hi)
        n = int(m.sum())
        if n == 0:
            continue
        out.append({"bin": label, "lead_lo_s": lo, "lead_hi_s": None if hi == float("inf") else hi,
                    "points": n, "mae_s": float(np.mean(np.abs(y[m] - p[m])))})
    return out


def score(y, p, mae_target: float) -> float:
    mz = mae(y, 0)
    return float(np.clip((mz - mae(y, p)) / (mz - mae_target), 0, 1))


# ----------------------------------------------------------------------------- данные


def load_all(dataset: Path, cache: bool = True) -> dict:
    """Признаки для train/test/validate (+ кеш в artifacts/cache)."""
    ART.mkdir(parents=True, exist_ok=True)
    cpath = ART / "cache_features.pkl"
    if cache and cpath.exists() and cpath.stat().st_mtime > Path(__file__).with_name("features").joinpath("tabular.py").stat().st_mtime:
        return pickle.loads(cpath.read_bytes())
    out = {}
    _, _, sched_train = load_split(dataset, "train")
    src = clone_sources(sched_train)
    for split in ("train", "test", "validate"):
        t0 = time.time()
        pts, tr, sch = load_split(dataset, split)
        X = build_features(pts, tr, sch, route_of=src)
        meta = pts.set_index("sample_id")[["tr_id", "T", "cur_dev_s"]].copy()
        meta["source"] = meta["tr_id"].map(lambda t: src.get(int(t), int(t)))
        meta["is_real"] = meta["tr_id"] < 9_000_000
        if "target_delay_s" in pts:
            meta["y"] = pts.set_index("sample_id")["target_delay_s"]
        out[split] = (X.loc[meta.index], meta)
        print(f"[features] {split}: {X.shape} за {time.time() - t0:.0f} c")
    cpath.write_bytes(pickle.dumps(out))
    return out


# ----------------------------------------------------------------------------- модель


def fit(X: pd.DataFrame, y_resid: np.ndarray, feats: list[str], params: dict, seed: int = 0,
        weight: np.ndarray | None = None) -> CatBoostRegressor:
    cats = [c for c in CAT_FEATURES if c in feats]
    m = CatBoostRegressor(**{**params, "random_seed": seed})
    m.fit(Pool(X[feats], y_resid, cat_features=cats, weight=weight))
    return m


def predict(models: list[CatBoostRegressor], X: pd.DataFrame, feats: list[str], cur_dev: np.ndarray) -> np.ndarray:
    cats = [c for c in CAT_FEATURES if c in feats]
    r = np.mean([m.predict(Pool(X[feats], cat_features=cats)) for m in models], axis=0)
    return r + cur_dev


def real_weight(meta: pd.DataFrame, w_syn: float) -> np.ndarray:
    return np.where(meta["is_real"], 1.0, w_syn)


# ----------------------------------------------------------------------------- схемы оценки


def pooled(data: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    X = pd.concat([data["train"][0], data["test"][0]])
    M = pd.concat([data["train"][1], data["test"][1]])
    return X, M


def eval_proxy(data: dict, feats: list[str], params: dict, k: int = 5, block_min: int = 30,
               w_syn: float = 1.0, seeds=(0,)) -> dict:
    """K-fold по реальным точкам блоками по времени (как validate: соседние T, клоны остаются в обучении)."""
    X, M = pooled(data)
    real = M.index[M["is_real"]]
    blocks = (M.loc[real, "tr_id"].astype(str) + "_" + (M.loc[real, "T"].astype("int64") // (block_min * 60 * 10**9)).astype(str))
    ub = blocks.unique()
    rng = np.random.RandomState(0)
    fold_of = dict(zip(ub, rng.randint(0, k, len(ub))))
    folds = blocks.map(fold_of)
    pred = pd.Series(np.nan, index=real)
    for f in range(k):
        te = folds.index[folds == f]
        tr = M.index.difference(te)
        y = (M.loc[tr, "y"] - M.loc[tr, "cur_dev_s"]).to_numpy()
        ms = [fit(X.loc[tr], y, feats, params, s, real_weight(M.loc[tr], w_syn)) for s in seeds]
        pred.loc[te] = predict(ms, X.loc[te], feats, M.loc[te, "cur_dev_s"].to_numpy())
    y = M.loc[real, "y"]
    return {"proxy_mae": mae(y, pred), "proxy_base": mae(y, M.loc[real, "cur_dev_s"]), "pred": pred}


def eval_holdout(data: dict, feats: list[str], params: dict, w_syn: float = 1.0, seeds=(0,)) -> dict:
    (Xtr, Mtr), (Xte, Mte) = data["train"], data["test"]
    y = (Mtr["y"] - Mtr["cur_dev_s"]).to_numpy()
    ms = [fit(Xtr, y, feats, params, s, real_weight(Mtr, w_syn)) for s in seeds]
    p = predict(ms, Xte, feats, Mte["cur_dev_s"].to_numpy())
    return {"test_mae": mae(Mte["y"], p), "test_base": mae(Mte["y"], Mte["cur_dev_s"]), "pred": p, "models": ms}


def eval_lovo(data: dict, feats: list[str], params: dict, w_syn: float = 1.0) -> dict:
    X, M = pooled(data)
    errs, base = [], []
    for v in sorted(M.loc[M["is_real"], "source"].unique()):
        te = M.index[(M["source"] == v) & M["is_real"]]
        tr = M.index[M["source"] != v]
        y = (M.loc[tr, "y"] - M.loc[tr, "cur_dev_s"]).to_numpy()
        m = fit(X.loc[tr], y, feats, {**params, "iterations": min(params["iterations"], 800)}, 0, real_weight(M.loc[tr], w_syn))
        p = predict([m], X.loc[te], feats, M.loc[te, "cur_dev_s"].to_numpy())
        errs += list(np.abs(M.loc[te, "y"] - p))
        base += list(np.abs(M.loc[te, "y"] - M.loc[te, "cur_dev_s"]))
    return {"lovo_mae": float(np.mean(errs)), "lovo_base": float(np.mean(base))}


def mae_target_estimate(data: dict) -> float:
    """MAE_TARGET по условию «бейзлайн cur_dev_s даёт score 0.40» (оценка на test)."""
    _, Mte = data["test"]
    mz, mb = mae(Mte["y"], 0), mae(Mte["y"], Mte["cur_dev_s"])
    return mz - (mz - mb) / 0.40


# ----------------------------------------------------------------------------- неопределённость

CLASSES = ["early", "ontime", "late"]      # пороги как в разметке: < −60 с / > +120 с
QUANTILES = (0.1, 0.5, 0.9)


def delay_class(y) -> np.ndarray:
    y = np.asarray(y, float)
    return np.where(y < -60, "early", np.where(y > 120, "late", "ontime"))


def fit_uncertainty(X: pd.DataFrame, M: pd.DataFrame, feats: list[str], params: dict, seed: int = 0):
    """Квантильная модель (интервал 10–90%) на поправку к cur_dev_s + классификатор early/ontime/late."""
    cats = [c for c in CAT_FEATURES if c in feats]
    base = {k: v for k, v in params.items() if k != "loss_function"}
    alphas = ",".join(str(a) for a in QUANTILES)
    q = CatBoostRegressor(**base, loss_function=f"MultiQuantile:alpha={alphas}", random_seed=seed)
    q.fit(Pool(X[feats], (M["y"] - M["cur_dev_s"]).to_numpy(), cat_features=cats))
    clf = CatBoostClassifier(**base, loss_function="MultiClass", class_names=CLASSES, random_seed=seed)
    clf.fit(Pool(X[feats], delay_class(M["y"]), cat_features=cats))
    return q, clf


def predict_uncertainty(q: CatBoostRegressor, clf: CatBoostClassifier, X: pd.DataFrame, feats: list[str],
                        cur_dev: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """-> (квантили задержки N×3 в секундах, вероятности классов N×3 в порядке CLASSES)."""
    cats = [c for c in CAT_FEATURES if c in feats]
    pool = Pool(X[feats], cat_features=cats)
    qs = np.sort(q.predict(pool), axis=1) + cur_dev[:, None]
    proba = clf.predict_proba(pool)
    order = [list(clf.classes_).index(c) for c in CLASSES]
    return qs, proba[:, order]


COVERAGE = 0.8  # цель: факт попадает в интервал в 80% случаев


def conformal_margin(qs: np.ndarray, y: np.ndarray, coverage: float = COVERAGE) -> float:
    """На сколько секунд расширить интервал [q10, q90], чтобы накрыть ``coverage`` фактов (CQR)."""
    s = np.maximum(qs[:, 0] - y, y - qs[:, 2])
    n = len(s)
    return float(np.quantile(s, min(1.0, np.ceil((n + 1) * coverage) / n)))


def eval_uncertainty(data: dict, feats: list[str], params: dict) -> dict:
    """Holdout train -> test: покрытие интервала (сырое и после конформной калибровки), качество вероятностей.

    Калибровочный запас считается на test; честное покрытие проверяется перекрёстно на двух половинах test.
    """
    from sklearn.metrics import roc_auc_score

    (Xtr, Mtr), (Xte, Mte) = data["train"], data["test"]
    q, clf = fit_uncertainty(Xtr, Mtr, feats, params)
    qs, proba = predict_uncertainty(q, clf, Xte, feats, Mte["cur_dev_s"].to_numpy())
    y = Mte["y"].to_numpy()
    half = np.random.RandomState(0).rand(len(y)) < 0.5
    cov_cross = []
    for a, b in ((half, ~half), (~half, half)):
        e = conformal_margin(qs[a], y[a])
        cov_cross.append(np.mean((y[b] >= qs[b, 0] - e) & (y[b] <= qs[b, 2] + e)))
    margin = conformal_margin(qs, y)
    cls = delay_class(y)
    p_late = proba[:, 2]
    bins = pd.cut(p_late, [0, 0.25, 0.5, 0.75, 1.0], include_lowest=True)
    calib = pd.DataFrame({"p": p_late, "late": cls == "late"}).groupby(bins, observed=True).agg(
        n=("late", "size"), p_mean=("p", "mean"), late_share=("late", "mean")).round(2)
    return {
        "coverage_10_90": float(np.mean((y >= qs[:, 0]) & (y <= qs[:, 2]))),
        "interval_width_median": float(np.median(qs[:, 2] - qs[:, 0])),
        "conformal_margin_s": margin,
        "coverage_calibrated": float(np.mean(cov_cross)),
        "interval_width_calibrated": float(np.median(qs[:, 2] - qs[:, 0]) + 2 * margin),
        "auc_late": float(roc_auc_score(cls == "late", p_late)),
        "auc_early": float(roc_auc_score(cls == "early", proba[:, 0])),
        "class_accuracy": float(np.mean(np.array(CLASSES)[proba.argmax(1)] == cls)),
        "calibration_late": calib,
    }


# ----------------------------------------------------------------------------- gps-режим (подсказка бэкенда)


def apply_gps(data: dict) -> dict:
    """Копия данных, где ``cur_dev_s`` (признак и база прогноза) = подсказка, которую считает backend на потоке.

    Официальная ``cur_dev_s`` = (факт − план) на последней остановке с **планом** <= T, и факт этой остановки у ~45%
    точек наступает уже после T: подсказка частично из будущего, на потоке её не посчитать. Backend
    (``backend/app/state/fleet.py``) берёт задержку на **последнем прибытии, найденном по GPS** тем же правилом, что
    ``features.tabular.gps_history`` (это признак ``gps_last_dev``), а если прибытий нет — шлёт 0. Модель,
    обученная на официальной подсказке, на такой ошибается сильнее (test: 68.7 с против 44.8 у этой).
    Используется только телеметрия до T — факты расписания не нужны.
    """
    out = dict(data)
    for split in ("train", "test", "validate"):
        X, M = data[split]
        c = X["gps_last_dev"].fillna(0.0).to_numpy()
        X, M = X.copy(), M.copy()
        X["cur_dev_s"] = c
        X["gps_dev_minus_cur"] = X["gps_med3"] - c  # признак считается от подсказки — пересчитываем, иначе утечка
        M["cur_dev_s"] = c
        out[split] = (X, M)
    return out


# ----------------------------------------------------------------------------- финал


def fit_final(data: dict, feats: list[str], params: dict, w_syn: float, seeds) -> list[CatBoostRegressor]:
    X, M = pooled(data)
    y = (M["y"] - M["cur_dev_s"]).to_numpy()
    return [fit(X, y, feats, params, s, real_weight(M, w_syn)) for s in seeds]


def write_submission(dataset: Path, ids: pd.Index, pred: np.ndarray, out: Path) -> pd.DataFrame:
    sample = pd.read_csv(dataset / "sample_submission.csv", sep=";")
    sub = sample[["sample_id"]].merge(pd.DataFrame({"sample_id": ids, "prediction": np.round(pred, 1)}),
                                      on="sample_id", how="left")
    assert len(sub) == len(sample) and sub["sample_id"].is_unique and sub["prediction"].notna().all()
    sub.to_csv(out, sep=";", index=False, encoding="utf-8", lineterminator="\r\n")
    return sub


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=Path, default=Path("./dataset"))
    ap.add_argument("--eval", action="store_true", help="оценить по трём схемам")
    ap.add_argument("--fit", action="store_true", help="обучить на train+test и записать сабмит")
    ap.add_argument("--eval-uncertainty", action="store_true", help="проверить интервал и вероятности (holdout)")
    ap.add_argument("--fit-uncertainty", action="store_true", help="обучить только интервал+классификатор")
    ap.add_argument("--out", type=Path, default=Path("submission.csv"))
    ap.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "configs" / "catboost.json")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--gps", action="store_true",
                    help="модель для потока: cur_dev_s = задержка на последнем GPS-прибытии, как считает backend; "
                         "файлы catboost_gps_*, сабмит не пишется")
    args = ap.parse_args()

    data = load_all(args.dataset, cache=not args.no_cache)
    cfg = json.loads(args.config.read_text()) if args.config.exists() else {}
    feats = cfg.get("features", FEATURES)
    params = {**PARAMS, **cfg.get("params", {})}
    w_syn = cfg.get("w_syn", 1.0)
    seeds = list(range(cfg.get("n_seeds", 5)))
    mt = mae_target_estimate(data)
    pre = "catboost_gps_" if args.gps else "catboost_"
    if args.gps:
        data = apply_gps(data)
        print("[gps] cur_dev_s = задержка на последнем GPS-прибытии (как у backend), нет прибытий -> 0")

    if args.eval:
        ho = eval_holdout(data, feats, params, w_syn)
        px = eval_proxy(data, feats, params, w_syn=w_syn)
        lv = eval_lovo(data, feats, params, w_syn)
        print(f"MAE_TARGET ≈ {mt:.1f}")
        print(f"holdout train->test: MAE {ho['test_mae']:.2f} (бейзлайн {ho['test_base']:.2f})  score≈{score(data['test'][1]['y'], ho['pred'], mt):.3f}")
        print(f"proxy K-fold (как validate): MAE {px['proxy_mae']:.2f} (бейзлайн {px['proxy_base']:.2f})")
        print(f"LOVO (честная): MAE {lv['lovo_mae']:.2f} (бейзлайн {lv['lovo_base']:.2f})")
        ART.mkdir(parents=True, exist_ok=True)
        # разбивка по горизонту прогноза: где модель точнее «на подлёте», где — за 10 мин
        Xte, Mte = data["test"]
        by_lead_ho = mae_by_lead(Mte["y"], ho["pred"], Xte["lead_s"]) if "lead_s" in Xte.columns else []
        (ART / f"{pre}metrics.json").write_text(json.dumps({
            "holdout_mae": ho["test_mae"], "holdout_baseline_mae": ho["test_base"],
            "holdout_zero_mae": mae(data["test"][1]["y"], 0),
            "proxy_mae": px["proxy_mae"], "proxy_baseline_mae": px["proxy_base"],
            "lovo_mae": lv["lovo_mae"], "lovo_baseline_mae": lv["lovo_base"], "mae_target_estimate": mt,
            "mae_by_lead": by_lead_ho,
        }, indent=2))

    if args.eval_uncertainty or args.fit or args.fit_uncertainty:
        u = eval_uncertainty(data, feats, params)
        print(f"интервал 10–90% без калибровки: факт попал в {u['coverage_10_90']:.0%} (цель {COVERAGE:.0%}), "
              f"ширина {u['interval_width_median']:.0f} с")
        print(f"после конформной калибровки (+{u['conformal_margin_s']:.0f} с с каждой стороны): "
              f"покрытие {u['coverage_calibrated']:.0%}, ширина {u['interval_width_calibrated']:.0f} с")
        print(f"вероятности: AUC late {u['auc_late']:.3f}, AUC early {u['auc_early']:.3f}, "
              f"точность класса {u['class_accuracy']:.1%}")
        print("калибровка p_late (прогнозируемая вероятность vs реальная доля опозданий):")
        print(u["calibration_late"].to_string())

    if args.fit or args.fit_uncertainty:
        X, M = pooled(data)
        q, clf = fit_uncertainty(X, M, feats, params)
        ART.mkdir(parents=True, exist_ok=True)
        q.save_model(str(ART / f"{pre}quantiles.cbm"))
        clf.save_model(str(ART / f"{pre}classes.cbm"))
        (ART / f"{pre}uncertainty.json").write_text(json.dumps({
            "conformal_margin_s": u["conformal_margin_s"], "coverage_target": COVERAGE,
            "coverage_calibrated_holdout": u["coverage_calibrated"], "auc_late_holdout": u["auc_late"],
            "auc_early_holdout": u["auc_early"], "class_accuracy_holdout": u["class_accuracy"]}, indent=2))
        print(f"[uncertainty] сохранены {pre}quantiles.cbm, {pre}classes.cbm, {pre}uncertainty.json")

    if args.fit:
        models = fit_final(data, feats, params, w_syn, seeds)
        ART.mkdir(parents=True, exist_ok=True)
        for i, m in enumerate(models):
            m.save_model(str(ART / f"{pre}seed{i}.cbm"))
        (ART / f"{pre}meta.json").write_text(json.dumps(
            {"features": feats, "cat_features": [c for c in CAT_FEATURES if c in feats], "params": params,
             "w_syn": w_syn, "n_models": len(models), "target": "target_delay_s - cur_dev_s",
             "classes": CLASSES, "quantiles": list(QUANTILES),
             "cur_dev_mode": "gps" if args.gps else "official",
             "model_version": f"cb-tab{'-gps' if args.gps else ''}-{time.strftime('%Y%m%d-%H%M')}"},
            ensure_ascii=False, indent=2))
        # основной сабмит — официальная подсказка из validate; --gps пишет альтернативный сабмит, где подсказка
        # посчитана только по телеметрии validate (как на потоке), по умолчанию в submission_gps.csv
        out = args.out if not (args.gps and args.out == Path("submission.csv")) else Path("submission_gps.csv")
        Xv, Mv = data["validate"]
        pred = predict(models, Xv, feats, Mv["cur_dev_s"].to_numpy())
        sub = write_submission(args.dataset, Xv.index, pred, out)
        print(f"[submission] {out}: {len(sub)} строк; прогноз {sub['prediction'].describe().round(1).to_dict()}")
        print(f"[models] сохранены {pre}seed0..{len(models) - 1}.cbm, {pre}meta.json")


if __name__ == "__main__":
    main()
