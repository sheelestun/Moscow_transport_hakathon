"""FastAPI-инференс ML-модуля: POST /predict, /predict/batch, /whatif/predict; GET /health, /metrics/model,
/model/info; POST /reload.

Модель — ансамбль CatBoost из ``ml/artifacts/catboost_seed*.cbm`` (``train_catboost.py --fit``) плюс модели
неопределённости: ``catboost_quantiles.cbm`` (интервал 10–90%, конформно откалиброван на 80% покрытия) и
``catboost_classes.cbm`` (вероятности early / ontime / late). Если моделей неопределённости нет — сервис
работает на одном ансамбле, риск считается сигмоидой от задержки (как в контракте §7.1).

Онлайн-фичи строятся тем же кодом, что и в батче (``features/tabular.build_features`` через
``point_features``; быстрый путь без DataFrame, см. ``_features``) — онлайн-прогноз совпадает с ``submission.csv``.

Клиент шлёт **сырой контекст**: буфер телеметрии (пакеты с ``event_time <= T``; более поздние сервис
отбрасывает сам) и слайс планового расписания ТС. Если ``schedule`` не передан, берётся план ТС из
``SCHEDULE_PATH`` (если файл есть).

Запуск::

    uvicorn inference_service:app --app-dir ml/src --host 0.0.0.0 --port 8001   # Swagger: /docs
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier, CatBoostRegressor, Pool
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from explain import REC_CODE, explain, reason_pattern, recommendation, risk_level  # noqa: E402
from features.tabular import CAT_FEATURES, FEATURES, dist_m, index_schedule, make_tele, point_features, to_sec  # noqa: E402

ARTIFACTS_DIR = Path(os.environ.get("ML_ARTIFACTS", Path(__file__).resolve().parents[1] / "artifacts"))
STATS_DIR = Path(os.environ.get("ML_STATS_DIR", Path(__file__).resolve().parents[2] / "statistics" / "tables"))
SCHEDULE_PATH = Path(os.environ.get("SCHEDULE_PATH",
                                    Path(__file__).resolve().parents[2] / "dataset" / "validate" / "schedule_plan.csv"))
MODEL_VERSION_ENV = os.environ.get("ML_MODEL_VERSION")
# режим подсказки по умолчанию: на сервере с живым потоком — gps (так считает backend), для сабмита — official
DEFAULT_CUR_DEV_MODE = os.environ.get("ML_DEFAULT_CUR_DEV_MODE", "official")
RISK_MID_SEC = float(os.environ.get("ML_RISK_MID_SEC", 120.0))
RISK_SLOPE_SEC = float(os.environ.get("ML_RISK_SLOPE_SEC", 60.0))
STALE_AFTER_S = 180  # нет свежих координат дольше — данные «устарели», уверенность снижаем
OFF_ROUTE_M = 3000   # ТС дальше от всех своих остановок (план ±1 ч) — позиция «не на маршруте» (~4% реальных точек:
                     # стоянка в парке перед рейсом; у случайных координат эмулятора — почти всегда)
HORIZON_S = (600, 900)  # прогноз по условию: первая остановка с планом в (T+10 мин, T+15 мин]
MAE_TARGET_FALLBACK = 78.0  # запасное значение, если в артефактах нет mae_target_estimate
MAE_TARGET = MAE_TARGET_FALLBACK  # текущее значение — при старте перезаписывается из catboost_metrics.json

# What-if — эвристические сдвиги задержки (секунды), не выученные моделью; для дашборда это «оценка сценария».
WHATIF_DELTA_MAP: dict[str, float] = {
    "add_reserve": -60.0,
    "adjust_interval": -30.0,
    "detour": -90.0,
    "signal_priority": -45.0,
    "hold_at_stop": 30.0,
}


# ------------------------------------------------------------------ Pydantic-схемы


class TelemetryPing(BaseModel):
    tr_id: int
    event_time: str
    lon: Optional[float] = None
    lat: Optional[float] = None
    speed: Optional[float] = None
    location_valid: bool | str = "true"
    is_hist_data: int | bool = 0


class ScheduleStop(BaseModel):
    tr_id: int
    tt_action_item_id: int
    time_begin: str
    geom: str
    manual_fill: bool | str = "false"
    building_address: Optional[str] = None


class PredictRequest(BaseModel):
    sample_id: str
    tr_id: int
    T: str = Field(..., description="Момент прогноза, ISO-8601 (локальное время, как в датасете)")
    target_stop_id: int
    target_time_begin: str = Field(..., description="Плановое время целевой остановки, ISO-8601")
    cur_dev_s: float = Field(..., description="Задержка на последней уже пройденной остановке (сек)")
    telemetry: list[TelemetryPing] = Field(default_factory=list,
                                           description="Пинги NDTP; пакеты позже T сервис отбрасывает")
    schedule: list[ScheduleStop] = Field(default_factory=list,
                                         description="Плановое расписание ТС; пусто — из SCHEDULE_PATH")
    cur_dev_mode: Optional[str] = Field(None, pattern="^(official|gps)$",
                                        description="как посчитана cur_dev_s: official — как в датасете (для сабмита); "
                                                    "gps — задержка на последнем прибытии по GPS, как считает backend "
                                                    "(нет прибытий -> 0). Не задано — ML_DEFAULT_CUR_DEV_MODE (official)")


class BatchRequest(BaseModel):
    requests: list[PredictRequest]


class TopFeature(BaseModel):
    name: str
    value: Optional[float] = Field(None, description="значение признака")
    contribution: float = Field(0.0, description="доля во вкладе в прогноз (|SHAP| / сумма |SHAP| топа), 0..1")
    contribution_sec: float = Field(0.0, description="вклад в прогноз задержки, сек (со знаком)")


class Cause(BaseModel):
    code: str
    text: str
    contribution_sec: float


class PredictResponse(BaseModel):
    sample_id: str
    delay_pred_sec: float = Field(..., description="прогноз задержки (факт − план), с; MAE считается по нему")
    risk_score: float = Field(..., description="P(опоздание > +120 с); светофор дашборда: ≥0.7 красный, ≥0.35 жёлтый")
    confidence: float
    top_features: list[TopFeature]
    reason_pattern: str
    recommendation: str = Field(..., description="код рекомендации для дашборда")
    model_version: str
    # --- расширение контракта
    lead_min: Optional[float] = None
    delay_interval_sec: list[float] = Field(default_factory=list, description="факт попадает сюда в ~80% случаев")
    p_early: Optional[float] = None
    p_ontime: Optional[float] = None
    p_late: Optional[float] = None
    risk_level: str = "green"
    causes: list[Cause] = Field(default_factory=list)
    recommendation_text: str = ""
    data_status: str = Field("live", description="live / stale / off_route / no_telemetry / fallback")
    horizon_ok: Optional[bool] = Field(None, description="целевая остановка в окне (T+10, T+15] мин — критерий горизонта")
    off_route_m: Optional[float] = Field(None, description="расстояние от ТС до ближайшей своей остановки (план ±1 ч), м")
    critical_trip: Optional[str] = Field(None, description="opening / closing — первый или последний рейс ТС за день: "
                                                           "опоздание на них критично для перевозчика")
    catchup_speed_kmh: Optional[float] = Field(None, description="средняя скорость, с которой ТС успеет к плану (при опоздании)")
    cur_dev_mode: str = "official"
    latency_ms: float = 0.0


class BatchResponse(BaseModel):
    responses: list[PredictResponse]


class HealthResponse(BaseModel):
    status: str
    n_models: int
    n_features: int
    model_version: str
    uncertainty_models: bool = False
    gps_models: bool = False
    default_cur_dev_mode: str = "official"
    vehicles_in_schedule: int = 0


class MetricsResponse(BaseModel):
    mae_train_s: Optional[float] = None
    mae_test_s: Optional[float] = None
    mae_baseline_train_s: Optional[float] = None
    mae_baseline_test_s: Optional[float] = None
    score_estimate: Optional[float] = None
    latency_ms_p50: Optional[float] = None
    latency_ms_p95: Optional[float] = None
    requests_served: int = 0
    validation: dict = Field(default_factory=dict, description="holdout / proxy / LOVO из train_catboost --eval")
    uncertainty: dict = Field(default_factory=dict, description="покрытие интервала, AUC вероятностей")
    n_models: int
    n_features: int
    model_version: str


class WhatIfRequest(PredictRequest):
    scenario: str = Field(..., description=f"Один из: {sorted(WHATIF_DELTA_MAP)}")


class WhatIfResponse(BaseModel):
    sample_id: str
    scenario: str
    delay_baseline_sec: float
    delay_scenario_sec: float
    delta_sec: float
    risk_baseline: float
    risk_scenario: float
    recommendation_still_applies: bool
    model_version: str


# ------------------------------------------------------------------ модели и метрики


class ModelSet:
    """Комплект моделей одного режима подсказки: ансамбль + интервал + вероятности."""

    def __init__(self, prefix: str) -> None:
        meta_path = ARTIFACTS_DIR / f"{prefix}meta.json"
        if not meta_path.exists():
            raise RuntimeError(f"meta не найден: {meta_path}. Сначала запусти train_catboost.py --fit")
        self.meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self.models = [CatBoostRegressor().load_model(str(ARTIFACTS_DIR / f"{prefix}seed{i}.cbm"))
                       for i in range(self.meta["n_models"])]
        self.q: Optional[CatBoostRegressor] = None
        self.clf: Optional[CatBoostClassifier] = None
        self.unc: dict = {}
        if all((ARTIFACTS_DIR / f"{prefix}{f}").exists() for f in ("quantiles.cbm", "classes.cbm", "uncertainty.json")):
            self.q = CatBoostRegressor().load_model(str(ARTIFACTS_DIR / f"{prefix}quantiles.cbm"))
            self.clf = CatBoostClassifier().load_model(str(ARTIFACTS_DIR / f"{prefix}classes.cbm"))
            self.unc = json.loads((ARTIFACTS_DIR / f"{prefix}uncertainty.json").read_text())

    @property
    def version(self) -> str:
        return self.meta.get("model_version", "catboost-ensemble-v1")


class State:
    """Всё, что сервис держит в памяти. ``load`` можно вызывать повторно (POST /reload)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.models: list[CatBoostRegressor] = []
        self.meta: dict = {}
        self.q: Optional[CatBoostRegressor] = None
        self.clf: Optional[CatBoostClassifier] = None
        self.unc: dict = {}
        self.metrics: dict = {}
        self.sets: dict[str, ModelSet] = {}   # official — всегда; gps — если обучена (train_catboost --gps)
        self.plan_by_tr: dict[int, list[dict]] = {}
        self.stops_by_tr: dict = {}          # разобранный план (features.tabular.Stops) — кеш на весь день
        self.latencies: deque = deque(maxlen=2000)
        self.n_requests = 0

    def load(self) -> None:
        sets = {"official": ModelSet("catboost_")}
        if (ARTIFACTS_DIR / "catboost_gps_meta.json").exists():
            sets["gps"] = ModelSet("catboost_gps_")
        off = sets["official"]
        models, meta, q, clf, unc = off.models, off.meta, off.q, off.clf, off.unc
        plan: dict[int, list[dict]] = {}
        if SCHEDULE_PATH.exists():
            s = pd.read_csv(SCHEDULE_PATH)
            cols = [c for c in ("tr_id", "tt_action_item_id", "time_begin", "geom", "manual_fill", "building_address")
                    if c in s]
            s = s[cols].astype(object).where(s[cols].notna(), None)
            plan = {int(k): g.to_dict("records") for k, g in s.groupby("tr_id")}
            stops = index_schedule(pd.read_csv(SCHEDULE_PATH, parse_dates=["time_begin"]))
        else:
            stops = {}
        with self.lock:
            self.models, self.meta, self.q, self.clf, self.unc, self.plan_by_tr = models, meta, q, clf, unc, plan
            self.stops_by_tr = stops
            self.sets = sets
            self.metrics = _load_metrics()

    @property
    def version(self) -> str:
        return MODEL_VERSION_ENV or self.meta.get("model_version", "catboost-ensemble-v1")


STATE = State()


def _read_mae_cohort_all(csv_path: Path, mae_col: str) -> dict[str, Optional[float]]:
    """Читает `split,dimension,group,points,<mae_col>` и возвращает MAE для cohort=all."""
    out: dict[str, Optional[float]] = {"train": None, "test": None}
    if not csv_path.exists():
        return out
    try:
        df = pd.read_csv(csv_path)
        if mae_col not in df.columns:
            return out
        sel = df[(df["dimension"] == "cohort") & (df["group"] == "all")]
        for split in ("train", "test"):
            row = sel[sel["split"] == split]
            if not row.empty:
                out[split] = float(row[mae_col].iloc[0])
    except Exception:  # noqa: BLE001
        pass
    return out


def _load_metrics() -> dict:
    """Витринные метрики: свежие из ``train_catboost --eval`` поверх CSV из statistics/tables.

    Побочный эффект: обновляет глобальный ``MAE_TARGET`` из ``mae_target_estimate``
    в свежем артефакте — так формула score использует калиброванное значение,
    а не хардкод 78 с.
    """
    global MAE_TARGET
    m: dict = {"validation": {}}
    model_mae = _read_mae_cohort_all(STATS_DIR / "model_metrics.csv", "mae_model_s")
    base_mae = _read_mae_cohort_all(STATS_DIR / "baseline_metrics.csv", "mae_zero_s")
    m.update(mae_train_s=model_mae["train"], mae_test_s=model_mae["test"],
             mae_baseline_train_s=base_mae["train"], mae_baseline_test_s=base_mae["test"], score_estimate=None)
    mae_target = MAE_TARGET_FALLBACK
    fresh = ARTIFACTS_DIR / "catboost_metrics.json"
    if fresh.exists():
        v = json.loads(fresh.read_text())
        m["validation"] = v
        m["mae_test_s"] = v.get("holdout_mae", m["mae_test_s"])
        if isinstance(v.get("mae_target_estimate"), (int, float)) and v["mae_target_estimate"] > 0:
            mae_target = float(v["mae_target_estimate"])
        # в контейнере нет statistics/tables: mae_zero берём из метрик модели (или выводим из оценки MAE_TARGET)
        if m["mae_baseline_test_s"] is None:
            mz = v.get("holdout_zero_mae")
            if mz is None and {"holdout_baseline_mae", "mae_target_estimate"} <= v.keys():
                mz = (v["holdout_baseline_mae"] - 0.4 * v["mae_target_estimate"]) / 0.6  # score(бейзлайн) = 0.40
            m["mae_baseline_test_s"] = mz
    MAE_TARGET = mae_target
    m["mae_target_s"] = mae_target
    mae, mae_zero = m["mae_test_s"], m["mae_baseline_test_s"]
    if mae is not None and mae_zero is not None and mae_zero > mae_target:
        m["score_estimate"] = float(max(0.0, min(1.0, (mae_zero - mae) / (mae_zero - mae_target))))
    return m


app = FastAPI(title="Delay predictor — ML module", version="1.1",
              description="Прогноз задержки ТС на остановке в горизонте 10–15 минут (CatBoost-ансамбль).")


@app.on_event("startup")
def _load_on_startup() -> None:
    STATE.load()


# ------------------------------------------------------------------ утилиты


def _sigmoid_risk(delay: float) -> float:
    """Риск из контракта §7.1 — запасной вариант, если нет классификатора. delay=RISK_MID_SEC → 0.5."""
    z = (delay - RISK_MID_SEC) / max(RISK_SLOPE_SEC, 1e-6)
    return 1.0 / (1.0 + math.exp(-z)) if z >= 0 else math.exp(z) / (1.0 + math.exp(z))


def _p_late_shifted(delay: float, lo: float, hi: float, delta: float) -> float:
    """P(задержка + delta > 120) при нормальном приближении по 80%-интервалу — для what-if."""
    sigma = max((hi - lo) / (2 * 1.2816), 1.0)
    return float(0.5 * math.erfc((120.0 - (delay + delta)) / (sigma * math.sqrt(2))))


def _naive(ts) -> pd.Timestamp:
    t = pd.to_datetime(ts)
    return t.tz_localize(None) if t.tzinfo is not None else t


def _features(req: PredictRequest) -> tuple[pd.DataFrame, dict]:
    """Признаки точки той же ``point_features``, что в батче (``tabular.build_features``).

    Быстрый путь: план ТС разобран один раз при старте (``STATE.stops_by_tr``), телеметрия собирается прямо
    в массивы с теми же правилами очистки (``make_tele``). ~10 мс вместо ~180 мс через DataFrame-адаптер
    ``from_stream.build_features_online`` (он остаётся для verify_streaming и как эталон).
    """
    T = _naive(req.T)
    t_sec = int(to_sec([T.to_datetime64()])[0])
    if req.schedule:
        df = pd.DataFrame([s.model_dump() for s in req.schedule])
        df["time_begin"] = pd.to_datetime(df["time_begin"], format="ISO8601")
        sg = index_schedule(df).get(req.tr_id)
    else:
        sg = STATE.stops_by_tr.get(req.tr_id)
    if sg is None:
        raise HTTPException(status_code=422, detail=f"нет планового расписания для ТС {req.tr_id}: передайте schedule")
    tele = None
    if req.telemetry:
        # один векторный разбор времени на весь буфер (поштучный pd.to_datetime — сотни мс на запрос)
        ev = pd.to_datetime(pd.Series([p.event_time for p in req.telemetry]), format="ISO8601")
        if ev.dt.tz is not None:
            ev = ev.dt.tz_localize(None)
        ts = (ev.values.astype("datetime64[us]").astype(np.int64)) / 1e6
        keep = ev.values <= T.to_datetime64()  # анти-утечка: только event_time <= T
        if keep.any():
            hist = np.array([int(bool(p.is_hist_data)) for p in req.telemetry])
            order = np.lexsort((hist, ts))  # как clean_traffic: по времени, при равенстве — не-исторический первым
            order = order[keep[order]]
            arr = lambda attr: np.array([np.nan if getattr(p, attr) is None else getattr(p, attr)
                                         for p in req.telemetry], dtype=float)[order]
            valid = np.array([str(p.location_valid).lower() == "true" for p in req.telemetry])[order]
            tele = make_tele(ts[order], arr("lon"), arr("lat"), arr("speed"), valid)
    tgt_plan = int(to_sec([_naive(req.target_time_begin).to_datetime64()])[0])
    f = point_features(t_sec, req.target_stop_id, tgt_plan, float(req.cur_dev_s), tele, sg)
    f["route"] = str(req.tr_id)
    X = pd.DataFrame([f], index=[req.sample_id])
    for c in FEATURES:
        if c not in X:
            X[c] = np.nan
    return X[FEATURES], {"off_route_m": _off_route_m(tele, sg, t_sec),
                         "critical_trip": _critical_trip(sg, req.target_stop_id)}


def _critical_trip(sg, target_stop_id) -> Optional[str]:
    """Открывающий (первый за день) или закрывающий (последний) рейс ТС — по плану."""
    k = np.where(sg.id == target_stop_id)[0]
    if not len(k):
        return None
    trip = sg.trip[k[0]]
    if trip == sg.trip.min():
        return "opening"
    return "closing" if trip == sg.trip.max() else None


def _off_route_m(tele, sg, t_sec: int) -> Optional[float]:
    """Расстояние от последней валидной координаты до ближайшей остановки ТС с планом в [T−1 ч, T+1 ч]."""
    if tele is None:
        return None
    ok = np.where(~np.isnan(tele.lat))[0]
    near = (sg.plan >= t_sec - 3600) & (sg.plan <= t_sec + 3600)
    if not len(ok) or not near.any():
        return None
    j = ok[-1]
    return float(dist_m(sg.lon[near], sg.lat[near], tele.lon[j], tele.lat[j]).min())


def _num(x) -> Optional[float]:
    if x is None or isinstance(x, str):
        return None
    return None if pd.isna(x) else float(x)


def _predict(reqs: list[PredictRequest]) -> list[PredictResponse]:
    """Пачка запросов; режимы подсказки (official / gps) считаются своими комплектами моделей."""
    out: dict[int, PredictResponse] = {}
    modes = [r.cur_dev_mode or DEFAULT_CUR_DEV_MODE for r in reqs]
    for mode in dict.fromkeys(modes):
        idx = [i for i, m in enumerate(modes) if m == mode]
        used = mode if mode in STATE.sets else "official"  # gps не обучена -> основная модель
        for i, resp in zip(idx, _predict_set([reqs[i] for i in idx], STATE.sets[used], used)):
            out[i] = resp
    return [out[i] for i in range(len(reqs))]


def _predict_set(reqs: list[PredictRequest], ms: ModelSet, mode: str) -> list[PredictResponse]:
    t0 = time.perf_counter()
    built = [_features(r) for r in reqs]
    X = pd.concat([b[0] for b in built])
    extra = [b[1] for b in built]
    feats = ms.meta["features"]
    cats = [c for c in CAT_FEATURES if c in feats]
    cur = np.array([r.cur_dev_s for r in reqs], dtype=float)
    if mode == "gps":
        # подсказка = задержка на последнем GPS-прибытии, посчитанная тем же правилом, что при обучении
        # (как у backend: features.tabular.gps_history -> gps_last_dev; нет прибытий -> 0)
        c = X["gps_last_dev"].astype(float).fillna(0.0)
        X["cur_dev_s"] = c.to_numpy()
        X["gps_dev_minus_cur"] = X["gps_med3"].astype(float) - c
        cur = c.to_numpy()
    pool = Pool(X[feats], cat_features=cats)
    per_model = np.array([m.predict(pool) for m in ms.models])          # (n_models, n)
    resid = per_model.mean(axis=0)
    delay = cur + resid

    if ms.q is not None:
        qs = np.sort(ms.q.predict(pool), axis=1) + cur[:, None]
        margin = ms.unc.get("conformal_margin_s", 0.0)
        lo, hi = np.minimum(qs[:, 0] - margin, delay), np.maximum(qs[:, 2] + margin, delay)
        classes = ms.meta.get("classes", ["early", "ontime", "late"])
        order = [list(ms.clf.classes_).index(c) for c in classes]
        proba = ms.clf.predict_proba(pool)[:, order]
    else:  # без моделей неопределённости: разброс сидов и сигмоида из контракта
        spread = per_model.std(axis=0)
        lo, hi = delay - 150 - 2 * spread, delay + 150 + 2 * spread
        p_late = np.array([_sigmoid_risk(d) for d in delay])
        proba = np.column_stack([np.zeros_like(p_late), 1 - p_late, p_late])

    # причины: приближённый SHAP одной модели (~35 мс/точка); база сдвигается к среднему ансамбля
    shap = ms.models[0].get_feature_importance(data=pool, type="ShapValues", shap_calc_type="Approximate")
    shap[:, -1] += resid - shap.sum(axis=1)
    elapsed = (time.perf_counter() - t0) * 1000 / len(reqs)

    out = []
    for i, req in enumerate(reqs):
        f = X.iloc[i].to_dict()
        age = _num(f.get("last_fix_age_s"))
        off, crit = extra[i]["off_route_m"], extra[i]["critical_trip"]
        status = ("no_telemetry" if age is None else "stale" if age > STALE_AFTER_S
                  else "off_route" if off is not None and off > OFF_ROUTE_M else "live")
        conf = float(np.clip(1 - (hi[i] - lo[i]) / 600, 0.05, 0.99)) * (1.0 if status == "live" else 0.5)
        d, pe, pl = float(delay[i]), float(proba[i, 0]), float(proba[i, 2])
        ex = explain(shap[i], feats, f, d)
        if status == "off_route":
            ex["causes"].insert(0, {"code": "off_route", "contribution_sec": 0.0,
                                 "text": f"ТС в {off / 1000:.1f} км от своих остановок: прогноз опирается на расписание "
                                         f"и последнее отклонение, позицию стоит проверить"})
        lead_s = (_naive(req.target_time_begin) - _naive(req.T)).total_seconds()
        level = risk_level(d, pe, pl)
        reason = reason_pattern(level, d, ex["causes"], f)
        rec_text = recommendation(level, d, ex["causes"])
        catchup = None
        need, now = _num(f.get("req_speed_kmh")), _num(f.get("moving_spd15")) or _num(f.get("spd15"))
        if d >= 60 and status == "live" and need is not None and 5 <= need <= 70:
            catchup = round(need, 0)
            rec_text += (f"; чтобы вернуться в график, нужна средняя скорость ~{need:.0f} км/ч"
                         + (f" (сейчас ~{now:.0f})" if now is not None else ""))
        if crit and level != "green":
            rec_text = (f"{'открывающий' if crit == 'opening' else 'закрывающий'} рейс — опоздание особенно "
                        f"критично для перевозчика; " + rec_text)
        total = sum(abs(t["contribution"]) for t in ex["top_features"]) or 1.0
        top = [TopFeature(name=t["name"], value=_num(f.get(t["name"])),
                          contribution=round(abs(t["contribution"]) / total, 3), contribution_sec=t["contribution"])
               for t in ex["top_features"]]
        out.append(PredictResponse(
            sample_id=req.sample_id, delay_pred_sec=round(d, 1), risk_score=round(pl, 3), confidence=round(conf, 3),
            top_features=top, reason_pattern=reason, recommendation=REC_CODE.get(reason, "monitor"),
            model_version=MODEL_VERSION_ENV or ms.version,
            lead_min=round((_naive(req.target_time_begin) - _naive(req.T)).total_seconds() / 60, 1),
            delay_interval_sec=[round(float(lo[i]), 1), round(float(hi[i]), 1)],
            p_early=round(pe, 3), p_ontime=round(float(proba[i, 1]), 3), p_late=round(pl, 3), risk_level=level,
            causes=[Cause(**c) for c in ex["causes"]], recommendation_text=rec_text,
            data_status=status, horizon_ok=bool(HORIZON_S[0] < lead_s <= HORIZON_S[1]),
            off_route_m=None if off is None else round(off, 0), critical_trip=crit, catchup_speed_kmh=catchup,
            cur_dev_mode=mode, latency_ms=round(elapsed, 2)))
    with STATE.lock:
        STATE.latencies.extend([elapsed] * len(reqs))
        STATE.n_requests += len(reqs)
    return out


def _fallback(req: PredictRequest, err: Exception) -> PredictResponse:
    """Деградация: ошибка модели/фичей -> прогноз = cur_dev_s (бейзлайн), сервис отвечает, а не падает."""
    d = float(req.cur_dev_s)
    risk = _sigmoid_risk(d)
    level = risk_level(d, 0.0, risk)
    return PredictResponse(
        sample_id=req.sample_id, delay_pred_sec=round(d, 1), risk_score=round(risk, 3), confidence=0.05,
        top_features=[TopFeature(name="cur_dev_s", value=d, contribution=1.0, contribution_sec=d)],
        reason_pattern="accumulated_delay" if level != "green" else "on_track",
        recommendation="release_reserve" if level == "red" else "monitor", model_version="baseline-cur_dev",
        delay_interval_sec=[d - 150, d + 150], risk_level=level,
        causes=[Cause(code="fallback", text=f"модель недоступна ({type(err).__name__}): прогноз по последнему отклонению",
                      contribution_sec=d)],
        recommendation_text=recommendation(level, d, []), data_status="fallback")


# ------------------------------------------------------------------ endpoints


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok" if STATE.models else "cold", n_models=len(STATE.models),
                          n_features=len(STATE.meta.get("features", [])), model_version=STATE.version,
                          uncertainty_models=STATE.q is not None, gps_models="gps" in STATE.sets,
                          default_cur_dev_mode=DEFAULT_CUR_DEV_MODE,
                          vehicles_in_schedule=len(STATE.plan_by_tr))


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest) -> PredictResponse:
    try:
        return _predict([req])[0]
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — деградация вместо HTTP 500
        return _fallback(req, e)


@app.post("/predict/batch", response_model=BatchResponse)
def predict_batch(batch: BatchRequest) -> BatchResponse:
    if not batch.requests:
        return BatchResponse(responses=[])
    try:
        return BatchResponse(responses=_predict(batch.requests))
    except Exception:  # noqa: BLE001 — одна плохая точка не валит пачку: считаем поштучно
        return BatchResponse(responses=[predict(r) for r in batch.requests])


@app.get("/metrics/model", response_model=MetricsResponse)
def metrics_model() -> MetricsResponse:
    """Витринные метрики: MAE по схемам валидации, live-latency, покрытие интервала."""
    lat = np.array(STATE.latencies) if STATE.latencies else None
    return MetricsResponse(
        mae_train_s=STATE.metrics.get("mae_train_s"), mae_test_s=STATE.metrics.get("mae_test_s"),
        mae_baseline_train_s=STATE.metrics.get("mae_baseline_train_s"),
        mae_baseline_test_s=STATE.metrics.get("mae_baseline_test_s"),
        score_estimate=STATE.metrics.get("score_estimate"),
        latency_ms_p50=None if lat is None else round(float(np.percentile(lat, 50)), 2),
        latency_ms_p95=None if lat is None else round(float(np.percentile(lat, 95)), 2),
        requests_served=STATE.n_requests, validation=STATE.metrics.get("validation", {}), uncertainty=STATE.unc,
        n_models=len(STATE.models), n_features=len(STATE.meta.get("features", [])), model_version=STATE.version)


@app.get("/model/info")
def model_info() -> dict:
    return {"model_version": STATE.version, "n_models": len(STATE.models), "target": STATE.meta.get("target"),
            "features": STATE.meta.get("features"), "params": STATE.meta.get("params"),
            "uncertainty": STATE.unc, "validation": STATE.metrics.get("validation", {}),
            "modes": {k: {"model_version": v.version, "uncertainty": v.unc} for k, v in STATE.sets.items()}}


@app.post("/reload", response_model=HealthResponse)
def reload() -> HealthResponse:
    """Подхватить переобученные модели из ML_ARTIFACTS без перезапуска контейнера."""
    STATE.load()
    return health()


@app.post("/whatif/predict", response_model=WhatIfResponse)
def whatif_predict(req: WhatIfRequest) -> WhatIfResponse:
    """«Что если применить сценарий»: эвристический сдвиг задержки, риск пересчитывается по интервалу модели."""
    if req.scenario not in WHATIF_DELTA_MAP:
        raise HTTPException(status_code=422, detail=f"unknown scenario, allowed: {sorted(WHATIF_DELTA_MAP)}")
    base = predict(PredictRequest(**{k: v for k, v in req.model_dump().items() if k != "scenario"}))
    delta = WHATIF_DELTA_MAP[req.scenario]
    lo, hi = (base.delay_interval_sec or [base.delay_pred_sec - 150, base.delay_pred_sec + 150])[:2]
    risk_scn = _p_late_shifted(base.delay_pred_sec, lo, hi, delta)
    return WhatIfResponse(
        sample_id=base.sample_id, scenario=req.scenario, delay_baseline_sec=round(base.delay_pred_sec, 1),
        delay_scenario_sec=round(base.delay_pred_sec + delta, 1), delta_sec=round(delta, 1),
        risk_baseline=round(base.risk_score, 3), risk_scenario=round(risk_scn, 3),
        recommendation_still_applies=bool(risk_scn >= 0.5), model_version=base.model_version)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("inference_service:app", host="0.0.0.0", port=8001)
