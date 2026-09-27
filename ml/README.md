# ML-модуль

Работают: **Степан, Фёдор.**

Задача — регрессия `target_delay_s` (секунды, знак важен) на первой остановке в окне `(T+10, T+15]` мин. Метрика — MAE, скор в [0,1]. Baseline `prediction = cur_dev_s` даёт ≈ 0.40 (3 балла). Цель — ≥ 0.70 (6 баллов). **Текущий локальный score ≈ 1.0**, платформа подтвердила.

Подробнее про задачу и контракты — в корневом [`ARCHITECTURE_AND_ROLES.md`](../ARCHITECTURE_AND_ROLES.md).

## Структура

```
ml/
├── src/
│   ├── features/
│   │   ├── tabular.py         основная логика фичей (батч + онлайн)
│   │   ├── from_csv.py        (устарел, оставлен для истории — использует sequence-модель)
│   │   └── from_stream.py     тонкая обёртка над tabular для инференс-сервиса
│   ├── models/                torch_seq.py (устарел, оставлен для истории)
│   ├── train_catboost.py      обучение CatBoost-ансамбля + 3 схемы валидации
│   ├── train.py               (устарел) обучение sequence-модели
│   ├── train_ensemble.py      (устарел) sequence-ансамбль
│   ├── predict_submission.py  собирает validate → submission.csv
│   ├── export_onnx.py         ONNX-экспорт + INT8 + latency benchmark
│   ├── inference_service.py   FastAPI /predict + /predict/batch
│   └── eval.py                MAE + score-функция как в ТЗ
├── configs/
│   └── catboost.json          гиперы основного CatBoost-конфига
├── artifacts/                 веса, ONNX, meta (в .gitignore)
├── notebooks/                 EDA и эксперименты
├── Dockerfile
└── requirements.txt
```

## Данные

Ожидаются в `../dataset/` относительно корня репо. Скачать: `https://disk.yandex.ru/d/CA6tsj4aJJ4Aaw` (в git не коммитим).

## Быстрый старт

```bash
cd ml
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## CatBoost-трек (основной, submission ~1.0)

Признаки — `src/features/tabular.py`, функция `point_features(T, ...)` считает одну точку и годится для онлайна.
Только телеметрия `event_time <= T`, **плановое** расписание и `cur_dev_s`; `time_fact_begin` не читается вообще.

```bash
# 1. Оценить модель на 3 схемах валидации
python ml/src/train_catboost.py --dataset ./dataset --eval

# 2. Обучить финальный ансамбль на train+test → сабмит на validate
python ml/src/train_catboost.py --dataset ./dataset --fit --out submission.csv
```

Полученные артефакты в `ml/artifacts/`:
- `catboost_seed{0..4}.cbm` — 5 моделей ансамбля
- `catboost_meta.json` — фичи, cat_features, параметры, версия таргета
- `cache_features.pkl` — кэш посчитанных фичей (train + test + validate)

| Группа фичей | Что даёт |
|---|---|
| рейсы по плану (`n_trip_breaks`, `tgt_left_in_trip`, `cur_idx_in_trip`, ...) | разрыв плана > 5 мин = конечная; через неё корреляция задержки падает с 0.96 до 0.02 |
| GPS-история (`gps_*`, `overdue_*`) | фактические прибытия восстановлены из GPS (медианная ошибка ~3 с) |
| движение и ETA (`spd*`, `stop*`, `route_left_m`, `eta_dev_*`) | простой, скорость, «физический» прогноз прибытия |
| `manual_fill` | у таких остановок факт ≈ план, таргет ≈ 0 |
| `route`, `hour` | маршрут (клоны → свой прототип) и время суток |

**Схемы валидации** (`--eval`):
- **proxy** — K-fold блоками по 30 мин по реальным точкам train+test, клоны остаются в обучении (так устроен validate). Лучший прокси скора платформы.
- **holdout** — train → test.
- **LOVO** — leave-one-vehicle-out (ТС вместе с клонами отложено). Показывает качество на новом ТС.

Данные — один день, 13 реальных ТС; в train 26 синтетических клонов (по 2 на ТС, те же моменты T).
В `train/schedule.csv` лежат факты по целевым остановкам validate — это утечка из будущего, не используем.

### Два режима подсказки: `official` (сабмит) и `gps` (живой поток)

Официальная `cur_dev_s` у ~45% точек использует факт прибытия, случившийся после T. Backend на потоке считает её по
последнему GPS-прибытию — для этого есть второй комплект моделей:

```bash
python ml/src/train_catboost.py --dataset ./dataset --gps --fit   # catboost_gps_* + submission_gps.csv
```

| | MAE test |
|---|---|
| основная модель, официальная подсказка | 39.8 с |
| основная модель, GPS-подсказка (сервер до выкатки) | 66.8 с |
| gps-модель, GPS-подсказка (сервер после выкатки) | 45.3 с |

Сервис держит оба комплекта; режим — `ML_DEFAULT_CUR_DEV_MODE` (`official` по умолчанию, `gps` на сервере) или поле
`cur_dev_mode` в запросе. Подробности — `EMULATOR.md`.

### Проверено и не вошло в модель

- **«Физическая» ETA-модель на телеметрии** («за сколько секунд ТС проедет следующие D метров», без расписания;
  обучение перекрёстно по машинам, чтобы не было утечки). Время проезда на чужих машинах предсказывает с ошибкой
  33% против 47% у «расстояние / скорость», но основной модели прироста не дала: holdout 39.8 → 40.9,
  proxy 38.8 → 38.9, LOVO 78.9 → 79.2 с (в пределах шума). Информация уже есть в признаках скорости, простоев и
  оставшегося пути. Машины без разметки для такого самообучения почти бесполезны: большинство стоит в парке.
  Код удалён, восстановить можно из истории git (коммит `e25922c`).

## Инференс-сервис (FastAPI)

Для онлайн-контура (NDTP-поток → бэк → ML):

```bash
uvicorn src.inference_service:app --host 0.0.0.0 --port 8001

# или через Docker (лёгкий образ ~1 ГБ: requirements-inference.txt, без PyTorch)
docker compose up -d --build ml
```

Замеры скорости, холодного старта и поведения при плохих данных — [`PERFORMANCE.md`](PERFORMANCE.md).
Эмулятор NDTP: что он шлёт, как проиграть через него настоящие треки и как ML отвечает на его поток — [`EMULATOR.md`](EMULATOR.md).

Эндпоинты:
- `GET /health` — статус, число моделей, есть ли модели неопределённости
- `POST /predict` — одна точка; `POST /predict/batch` — `{"requests": [...]}` → `{"responses": [...]}`
- `POST /whatif/predict` — сценарий (`add_reserve`, `adjust_interval`, `detour`, `signal_priority`, `hold_at_stop`)
- `GET /metrics/model` — MAE по схемам валидации, живая latency p50/p95, покрытие интервала
- `GET /model/info` — признаки, параметры, версия; `POST /reload` — подхватить переобученные модели
- `GET /docs` — Swagger UI

Запрос: `sample_id, tr_id, T, target_stop_id, target_time_begin, cur_dev_s`, буфер `telemetry` (пакеты NDTP;
пакеты позже `T` сервис отбрасывает сам) и `schedule` (плановое расписание ТС; если не передан — берётся из
`SCHEDULE_PATH`). Время — ISO-8601, с микросекундами или без. Полный пример запроса и ответа —
[`examples/predict_example.json`](examples/predict_example.json).

Ответ (поля контракта §7.1 сохранены, остальное — расширение):

| Поле | Что это |
|---|---|
| `delay_pred_sec` | прогноз задержки, с (по нему MAE) |
| `delay_interval_sec` | `[от, до]`: факт попадает сюда в ~80% случаев (конформная калибровка на holdout) |
| `p_early`, `p_ontime`, `p_late` | вероятности классов (< −60 с / норма / > +120 с), CatBoost MultiClass, AUC late 0.97 |
| `risk_score` | = `p_late`; светофор дашборда: ≥ 0.7 красный, ≥ 0.35 жёлтый. `risk_level` — готовый цвет |
| `reason_pattern`, `recommendation` | коды для дашборда (`accumulated_delay`, `long_dwell`, `speed_drop`, `traffic_jam_ahead`, **новые**: `terminal_turnaround`, `ahead_of_schedule`, `on_track`) |
| `causes`, `recommendation_text` | причины и рекомендация текстом (из SHAP-вкладов признаков) |
| `top_features` | `name`, `value`, `contribution` (доля 0..1), `contribution_sec` (вклад в секундах) |
| `confidence` | 1 − ширина интервала / 600 с; ×0.5 при устаревших данных |
| `data_status` | `live` / `stale` (нет координат > 3 мин) / `no_telemetry` / `fallback` (модель недоступна → прогноз = `cur_dev_s`, сервис не падает) |

Проверка онлайн-контура против батча и latency (поднимает сервис локально, настоящий HTTP)::

```bash
python ml/src/replay_validate.py --dataset ./dataset --submission submission.csv
# 151/151 прогноз совпадает с submission.csv (≤ 0.05 с); /predict p50 ~60 мс с HTTP, ~25 мс внутри; batch ~13 мс/точка
```

Онлайн-фичи считаются той же `features/tabular.point_features`, что и в батче: план ТС разбирается один раз при
старте, телеметрия собирается прямо в массивы с правилами `clean_traffic`. `features/from_stream.build_features_online`
(DataFrame-адаптер) остаётся эталоном для `verify_streaming.py`.

Env-переменные:
- `ML_ARTIFACTS` — путь к папке артефактов (по умолчанию `ml/artifacts`)
- `SCHEDULE_PATH` — плановое расписание для запросов без `schedule` (по умолчанию `dataset/validate/schedule_plan.csv`)
- `ML_MODEL_VERSION` — строка версии для ответа (по умолчанию из `catboost_meta.json`)
- `ML_RISK_MID_SEC`, `ML_RISK_SLOPE_SEC` — сигмоида риска, только если нет моделей неопределённости

## ONNX-экспорт и latency

```bash
python ml/src/export_onnx.py --artifacts ml/artifacts
```

Что делает:
1. Обучает noroute-версию (без категориальной `route` — CatBoost ONNX её не поддерживает) на 5 сидах.
2. Экспортирует в ONNX (fp32) и пытается в INT8.
3. Замеряет per-sample latency: catboost native vs onnx fp32 vs onnx int8.

Текущие цифры (см. `ml/artifacts/bench_latency.json`):

| Формат | Latency, мс | Speed-up | Размер, МБ (сумма 5 моделей) |
|---|---|---|---|
| catboost native (с route) | 9.38 | 1× | 12.4 |
| onnx fp32 (noroute)       | **1.57** | **6.0×** | 58.7 |
| onnx int8                 | — | — | — |

`p95 latency < 200 мс` из DoD выполнено с запасом (у нас **~1.5 мс** на fp32-ONNX).

**Про INT8:** `onnxruntime.quantization.quantize_dynamic` падает с `Failed to find proper ai.onnx domain` — CatBoost использует свой ONNX-домен `ai.catboost`, стандартный quantizer его не понимает. Обходной путь есть (ручной rewrite через `onnx.utils.rebuild`), но fp32 уже даёт 6× ускорение и укладывается в требование, поэтому не тратим время.

**Про noroute vs full:** ONNX-контур работает без `route` (это MAE ~48 vs 40 у full-model).

**Итог (26–27.09): в проде ONNX не используется, сервис работает на нативном CatBoost.** Причины: CatBoost не
выгружает в ONNX категориальные признаки (отсюда модель без `route` и MAE ~48 вместо ~40); SHAP-причины для карточки
диспетчера в ONNX недоступны; ONNX-файлы больше (58.7 МБ против 12.4 МБ), а INT8-квантизация к деревьям CatBoost не
применяется; сама модель — ~3–5 мс из ~25 мс ответа сервиса (остальное — признаки и причины), требование ТЗ < 1–2 с
выполнено с запасом. Скрипт оставлен как проверенная альтернатива.

## Правила по сабмитам

- Лимит 36 попыток / 24 успешных в день, до 27 сент 23:59 МСК.
- **Не спамить**: сабмитить только когда MAE на `labels_test.csv` улучшился на ≥ 3 секунды.
- Всегда держим последний рабочий `submission.csv` в корне репо, чтобы в любой момент можно было отправить.

## Антиутечка (не забыть)

При построении фичей для точки `T` использовать **только** `event_time ≤ T`. `cur_dev_s` — единственная информация о задержке, разрешённая к использованию. `time_fact_begin` из `schedule.csv` не используется вообще (`tabular.load_split` его дропает).
